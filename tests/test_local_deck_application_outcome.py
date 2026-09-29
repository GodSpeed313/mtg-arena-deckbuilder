from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, ExitStack
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import sqlite3
import subprocess
import sys
from threading import Barrier, Event
import unittest
from unittest.mock import patch
from uuid import uuid4

from mtgadb import managed_deck_store as store
from mtgadb.model import Collection
from mtgadb.modes import OperatingMode
from services import local_deck_application as application
from services import local_deck_application_outcome as outcome
from services.local_deck_application_intent import build_local_deck_application_intent
from services.local_execution_validation import LocalDeckApplicationError, LocalExecutionValidationAuthorityV1
from tests import test_local_deck_application as application_tests


ID = "local_deck_application_receipt_identity"
SERVICE = "services.local_deck_application_outcome."
CRASH_SCRIPT = r'''
import json, os, sqlite3, sys, unittest
from pathlib import Path
from unittest.mock import patch
from tests.test_pre_execution_revalidation import PreExecutionRevalidationTests
from services.local_execution_validation import LocalExecutionValidationAuthorityV1
from mtgadb.modes import OperatingMode
from services.local_deck_application_outcome import execute_prepared_local_deck_application
fixture = PreExecutionRevalidationTests(methodName="runTest")
fixture.setUp()
authority = LocalExecutionValidationAuthorityV1(card_database_path=sys.argv[4], collection=fixture.owned,
    format=fixture.format, rules=fixture.rules, mode=OperatingMode.FULL_COLLECTION)
operation = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
original = sqlite3.connect
class Crash(sqlite3.Connection):
    def commit(self):
        if sys.argv[3] == "after":
            super().commit()
        os._exit(73)
with patch("mtgadb.managed_deck_store.sqlite3.connect", side_effect=lambda *a, **kw: original(*a, factory=Crash, **kw)):
    execute_prepared_local_deck_application(operation, store_path=sys.argv[1], validation_authority=authority)
os._exit(74)
'''


class LocalDeckApplicationOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.fx = application_tests.LocalDeckApplicationTests(methodName="runTest")
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.path = self.fx.path
        self.intent = self.fx.intent
        self.authority = self.fx.fx.authority
        self.state = self.fx.state
        store.migrate_store_v1_to_v2(self.path)

    def prepare(self, intent=None):
        return outcome.prepare_local_deck_application(self.intent if intent is None else intent, store_path=self.path)

    def execute(self, operation, authority=None):
        return outcome.execute_prepared_local_deck_application(operation, store_path=self.path,
                            validation_authority=self.authority if authority is None else authority)

    def recover(self, intent=None):
        return outcome.recover_local_deck_application(self.intent if intent is None else intent, store_path=self.path)

    def error(self, code, action):
        with self.assertRaises((outcome.LocalDeckApplicationOutcomeError, LocalDeckApplicationError, store.ManagedDeckStoreError)) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def count(self):
        with closing(sqlite3.connect(self.path)) as con:
            return con.execute("SELECT count(*) FROM managed_application_operations").fetchone()[0]

    def other_intent(self):
        request = deepcopy(self.intent["application_request"])
        request["request_source"]["provenance"]["reference"] = "another explicit instruction"
        return build_local_deck_application_intent(self.intent["source_pre_execution_revalidation"],
                                                  self.intent["source_destination_state"], request)

    def rehash(self, receipt):
        payload = {k: deepcopy(v) for k, v in receipt.items() if k != ID}
        receipt[ID]["canonical_payload"] = payload
        receipt[ID]["digest"] = hashlib.sha256(outcome._encoded(payload)).hexdigest()

    def test_prepare_binds_exact_intent_without_deck_change(self):
        before = deepcopy(self.intent)
        operation = self.prepare()
        self.assertEqual(operation["source_intent"], self.intent)
        self.assertEqual(operation["record_id"], self.state["record_id"])
        self.assertEqual(operation["expected_revision"], 1)
        self.assertEqual(self.fx.read(), self.state)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.recover()["status"], "prepared")
        self.assertEqual(self.intent, before)

    def test_duplicate_registration_returns_same_handle_without_extra_rows(self):
        first = self.prepare()
        self.assertEqual(self.prepare(), first)
        self.execute(first)
        self.assertEqual(self.prepare(), first)
        self.assertEqual(self.count(), 1)

    def test_complete_binding_compared_even_if_digest_matches(self):
        self.prepare()
        changed = deepcopy(self.intent)
        changed["application_request"]["request_source"]["provenance"]["reference"] = "different"
        with patch(SERVICE + "_intent", return_value=changed):
            self.error("operation_binding_conflict", self.prepare)
            self.error("operation_binding_conflict", self.recover)
        self.assertEqual(self.fx.read(), self.state)

    def test_preparation_and_recovery_do_not_acquire_authority_or_validate(self):
        with ExitStack() as stack:
            for name in ("services.local_execution_validation.LocalExecutionValidationAuthorityV1._guarded",
                         "services.local_deck_application.build_pre_execution_revalidation",
                         "services.pre_execution_revalidation.validate_deck", "services.validator.validate_deck"):
                stack.enter_context(patch(name, side_effect=AssertionError(name)))
            self.assertEqual(self.recover()["status"], "not_found")
            self.prepare()
            self.assertEqual(self.recover()["status"], "prepared")

    def test_execution_atomically_commits_exact_receipt_and_deck(self):
        operation = self.prepare()
        receipt = self.execute(operation)
        self.assertEqual(receipt["status"], "committed")
        self.assertEqual(receipt["operation_id"], operation["operation_id"])
        self.assertEqual(self.fx.read()["revision"], 2)
        self.assertEqual(self.fx.read()["metadata"], self.state["metadata"])
        self.assertEqual(outcome.require_local_deck_application_receipt(receipt), receipt)
        recovery = self.recover()
        self.assertEqual(recovery["receipt"], receipt)
        self.assertEqual(recovery["operation"], operation)
        self.assertEqual(recovery["status"], "committed")

    def test_replay_returns_state_conflict_not_second_success(self):
        operation = self.prepare()
        receipt = self.execute(operation)
        self.error("operation_state_conflict", lambda: self.execute(operation))
        self.assertEqual(self.fx.read()["deck"].main[101], 1)
        self.assertEqual(self.recover()["receipt"], receipt)

    def test_new_executor_never_calls_public_6o_or_replacement(self):
        operation = self.prepare()
        with patch.object(application, "apply_local_deck_application", side_effect=AssertionError("public #6O")), \
             patch.object(store, "conditional_replace", side_effect=AssertionError("nested replace")):
            self.execute(operation)

    def test_original_6o_on_v2_remains_receipt_free(self):
        self.fx.apply()
        self.assertEqual(self.count(), 0)
        recovery = self.recover()
        self.assertEqual(recovery["status"], "not_found")
        self.assertIsNone(recovery["receipt"])
        self.assertEqual(self.fx.read()["gameplay_snapshot_identity"], self.intent["expected_result_deck_identity"])

    def test_original_6o_after_prepare_does_not_transition_operation(self):
        operation = self.prepare()
        self.fx.apply()
        self.assertEqual(self.recover()["status"], "prepared")
        self.error("revision_mismatch", lambda: self.execute(operation))

    def test_original_6o_on_v1_still_works_and_migration_does_not_backfill(self):
        path = self.fx.fx.root / "legacy.db"
        store.initialize_store(path)
        state = store.create_managed_deck(path, self.state["deck"], "Legacy")
        intent = self.fx.make_intent(state=state)
        application.apply_local_deck_application(intent, store_path=path, validation_authority=self.authority)
        store.migrate_store_v1_to_v2(path)
        self.assertEqual(outcome.recover_local_deck_application(intent, store_path=path)["status"], "not_found")

    def test_v1_requires_explicit_migration(self):
        path = self.fx.fx.root / "v1.db"
        store.initialize_store(path)
        state = store.create_managed_deck(path, self.state["deck"], "Old")
        intent = self.fx.make_intent(state=state)
        self.error("unsupported_schema", lambda: outcome.prepare_local_deck_application(intent, store_path=path))
        self.error("unsupported_schema", lambda: outcome.recover_local_deck_application(intent, store_path=path))
        self.assertEqual(store.open_store(path)["schema_version"], "1")

    def test_invalid_intent_and_operation_rejected_before_io(self):
        operation = self.prepare()
        bad = deepcopy(operation)
        bad["expected_revision"] = True
        with patch("sqlite3.connect", side_effect=AssertionError("IO")):
            self.error("invalid_intent", lambda: self.prepare({}))
            self.error("invalid_intent", lambda: self.recover({}))
            self.error("invalid_operation", lambda: self.execute(bad))
            self.error("invalid_operation", lambda: self.execute({}))

    def test_missing_operation_and_binding_conflict(self):
        operation = self.prepare()
        unknown = {**operation, "operation_id": str(uuid4())}
        self.error("operation_not_found", lambda: self.execute(unknown))
        other = self.prepare(self.other_intent())
        contradiction = {**other, "operation_id": operation["operation_id"]}
        self.error("operation_binding_conflict", lambda: self.execute(contradiction))

    def test_operation_uuid_collision_does_not_overwrite(self):
        operation = self.prepare()
        with patch(SERVICE + "uuid4", return_value=operation["operation_id"]):
            self.error("operation_binding_conflict", lambda: self.prepare(self.other_intent()))
        self.assertEqual(self.count(), 1)

    def test_fresh_ownership_and_policy_rejection_leaves_prepared(self):
        operation = self.prepare()
        self.fx.fx.publish(collection=Collection({}))
        self.error("fresh_validation_failed", lambda: self.execute(operation))
        self.fx.fx.publish(rules=replace(self.fx.upstream.rules, max_main=200))
        self.error("execution_context_conflict", lambda: self.execute(operation))
        self.assertEqual(self.recover()["status"], "prepared")
        self.assertEqual(self.fx.read(), self.state)

    def test_affordable_spend_rejected_in_new_path(self):
        intent = self.fx.make_intent(mode=OperatingMode.WILDCARD_BUDGET)
        operation = self.prepare(intent)
        self.fx.fx.publish(mode=OperatingMode.WILDCARD_BUDGET, inventory=self.fx.upstream.inventory, collection=Collection({}))
        self.error("current_spend_required", lambda: self.execute(operation))
        self.assertEqual(self.recover(intent)["status"], "prepared")
        self.assertEqual(self.fx.read(), self.state)

    def test_stale_metadata_and_deleted_destination_block_execution(self):
        operation = self.prepare()
        current = store.conditional_replace(self.path, self.state["record_id"], self.state, self.state["deck"],
                                             name="Renamed", format_label=None)["destination_state"]
        self.error("revision_mismatch", lambda: self.execute(operation))
        store.conditional_delete(self.path, current["record_id"], current)
        self.error("deleted_record", lambda: self.execute(operation))
        self.assertEqual(self.recover()["status"], "prepared")

    def test_recovery_survives_later_edit_and_tombstone(self):
        receipt = self.execute(self.prepare())
        current = self.fx.read()
        changed = store.conditional_replace(self.path, current["record_id"], current, self.state["deck"],
                                             name="Later", format_label=None)["destination_state"]
        self.assertEqual(self.recover()["receipt"], receipt)
        store.conditional_delete(self.path, changed["record_id"], changed)
        self.assertEqual(self.recover()["receipt"], receipt)

    def test_committed_recovery_uses_no_deck_read_validator_or_authority(self):
        receipt = self.execute(self.prepare())
        with ExitStack() as stack:
            for name in ("mtgadb.managed_deck_store._read", "services.local_deck_application.build_pre_execution_revalidation",
                         "services.local_execution_validation.LocalExecutionValidationAuthorityV1._guarded",
                         "services.pre_execution_revalidation.validate_deck"):
                stack.enter_context(patch(name, side_effect=AssertionError(name)))
            self.assertEqual(self.recover()["receipt"], receipt)

    def test_lost_preparation_response_recovered_by_intent(self):
        original = sqlite3.connect
        class After(sqlite3.Connection):
            def commit(self):
                super().commit()
                raise sqlite3.OperationalError("response lost")
        with patch.object(store.sqlite3, "connect", side_effect=lambda *a, **kw: original(*a, factory=After, **kw)):
            self.error("commit_failed", self.prepare)
        recovered = self.recover()
        self.assertEqual(recovered["status"], "prepared")
        self.assertEqual(self.prepare(), recovered["operation"])
        self.assertEqual(self.count(), 1)

    def test_execution_commit_before_error_is_durably_recoverable(self):
        operation = self.prepare()
        original = sqlite3.connect
        class After(sqlite3.Connection):
            def commit(self):
                super().commit()
                raise sqlite3.OperationalError("response lost")
        with patch.object(store.sqlite3, "connect", side_effect=lambda *a, **kw: original(*a, factory=After, **kw)):
            self.error("commit_failed", lambda: self.execute(operation))
        self.assertEqual(self.recover()["status"], "committed")
        self.assertEqual(self.fx.read()["revision"], 2)
        self.error("operation_state_conflict", lambda: self.execute(operation))

    def test_commit_failure_before_persistence_leaves_prepared(self):
        operation = self.prepare()
        original = sqlite3.connect
        class Before(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError("no commit")
        with patch.object(store.sqlite3, "connect", side_effect=lambda *a, **kw: original(*a, factory=Before, **kw)):
            self.error("commit_failed", lambda: self.execute(operation))
        self.assertEqual(self.recover()["status"], "prepared")
        self.assertEqual(self.fx.read(), self.state)

    def test_receipt_persistence_failure_rolls_back_deck_and_state(self):
        operation = self.prepare()
        original = sqlite3.connect
        class Failing(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql.startswith("UPDATE managed_application_operations SET state"):
                    super().execute(sql, parameters)
                    raise sqlite3.OperationalError("partial receipt failure")
                return super().execute(sql, parameters)
        with patch.object(store.sqlite3, "connect", side_effect=lambda *a, **kw: original(*a, factory=Failing, **kw)):
            self.error("transaction_failed", lambda: self.execute(operation))
        self.assertEqual(self.fx.read(), self.state)
        self.assertEqual(self.recover()["status"], "prepared")

    def test_receipt_verification_failure_rolls_back_deck(self):
        operation = self.prepare()
        with patch(SERVICE + "require_local_deck_application_receipt", side_effect=ValueError("bad receipt")):
            self.error("receipt_mismatch", lambda: self.execute(operation))
        self.assertEqual(self.fx.read(), self.state)
        self.assertEqual(self.recover()["status"], "prepared")

    def test_persisted_receipt_reread_failure_rolls_back_both(self):
        operation = self.prepare()
        original = outcome._read_operation
        def wrong(*args, **kwargs):
            value = original(*args, **kwargs)
            if value and value["state"] == "committed":
                value["receipt"]["reason"] = "wrong"
            return value
        with patch(SERVICE + "_read_operation", side_effect=wrong):
            self.error("receipt_mismatch", lambda: self.execute(operation))
        self.assertEqual(self.fx.read(), self.state)
        self.assertEqual(self.recover()["status"], "prepared")

    def test_process_termination_before_and_after_commit_delete_and_wal(self):
        for journal in ("DELETE", "WAL"):
            self.fx.sql("PRAGMA journal_mode=" + journal)
            for phase in ("before", "after"):
                state = store.create_managed_deck(self.path, self.state["deck"], journal + phase)
                intent = self.fx.make_intent(state=state)
                operation = self.prepare(intent)
                request = self.fx.fx.root / (journal + phase + ".json")
                request.write_text(json.dumps(operation), encoding="utf-8")
                process = subprocess.run([sys.executable, "-c", CRASH_SCRIPT, str(self.path), str(request), phase, str(self.fx.fx.cards)],
                                         capture_output=True, text=True, timeout=20)
                self.assertEqual(process.returncode, 73, process.stdout + process.stderr)
                observed = self.recover(intent)
                self.assertEqual(observed["status"], "prepared" if phase == "before" else "committed")
                after = store.read_destination_state(self.path, state["record_id"])
                self.assertEqual(after["revision"], 1 if phase == "before" else 2)
                self.assertEqual(after["deck"].main.get(101, 0), 0 if phase == "before" else 1)

    def test_duplicate_registration_concurrency_delete_and_wal(self):
        for journal in ("DELETE", "WAL"):
            self.fx.sql("PRAGMA journal_mode=" + journal)
            state = store.create_managed_deck(self.path, self.state["deck"], journal)
            intent = self.fx.make_intent(state=state)
            gate = Barrier(2)
            def prepare():
                gate.wait(3)
                return self.prepare(intent)
            before = self.count()
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(prepare) for _ in range(2)]
                results = [f.result(10) for f in futures]
            self.assertEqual(results[0], results[1])
            self.assertEqual(self.count(), before + 1)

    def test_same_operation_concurrency_delete_and_wal_independent_authorities(self):
        other = LocalExecutionValidationAuthorityV1(**self.fx.fx.args)
        self.addCleanup(other.close)
        for journal in ("DELETE", "WAL"):
            self.fx.sql("PRAGMA journal_mode=" + journal)
            state = store.create_managed_deck(self.path, self.state["deck"], journal)
            operation = self.prepare(self.fx.make_intent(state=state))
            gate = Barrier(2)
            def execute(authority):
                gate.wait(3)
                try:
                    return self.execute(operation, authority)["status"]
                except outcome.LocalDeckApplicationOutcomeError as exc:
                    return exc.code
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(execute, authority) for authority in (self.authority, other)]
                self.assertCountEqual([f.result(15) for f in futures], ["committed", "operation_state_conflict"])
            self.assertEqual(store.read_destination_state(self.path, state["record_id"])["revision"], 2)

    def test_different_operations_same_revision_concurrency_delete_and_wal(self):
        for journal in ("DELETE", "WAL"):
            self.fx.sql("PRAGMA journal_mode=" + journal)
            state = store.create_managed_deck(self.path, self.state["deck"], journal)
            left = self.fx.make_intent(state=state)
            request = deepcopy(left["application_request"])
            request["request_source"]["provenance"]["reference"] = "second approval"
            right = build_local_deck_application_intent(left["source_pre_execution_revalidation"], left["source_destination_state"], request)
            operations = [self.prepare(intent) for intent in (left, right)]
            gate = Barrier(2)
            def execute(operation):
                gate.wait(3)
                try:
                    return self.execute(operation)["status"]
                except store.ManagedDeckStoreError as exc:
                    return exc.code
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(execute, operation) for operation in operations]
                self.assertCountEqual([f.result(15) for f in futures], ["committed", "revision_mismatch"])
            self.assertCountEqual([self.recover(intent)["status"] for intent in (left, right)], ["committed", "prepared"])

    def test_execution_versus_recovery_waits_for_commit_delete_and_wal(self):
        original = application.build_pre_execution_revalidation
        for journal in ("DELETE", "WAL"):
            self.fx.sql("PRAGMA journal_mode=" + journal)
            state = store.create_managed_deck(self.path, self.state["deck"], journal)
            intent = self.fx.make_intent(state=state)
            operation = self.prepare(intent)
            entered, release, recovering, recovered = Event(), Event(), Event(), Event()
            def validate(*args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(5))
                return original(*args, **kwargs)
            def recover():
                recovering.set()
                value = self.recover(intent)
                recovered.set()
                return value
            with patch.object(application, "build_pre_execution_revalidation", side_effect=validate), ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.execute, operation)
                self.assertTrue(entered.wait(3))
                second = pool.submit(recover)
                self.assertTrue(recovering.wait(3))
                self.assertFalse(recovered.wait(.05))
                release.set()
                receipt = first.result(10)
                self.assertEqual(second.result(10)["receipt"], receipt)

    def test_recovery_busy_is_not_not_found(self):
        original = sqlite3.connect
        with closing(original(self.path, isolation_level=None)) as con:
            con.execute("BEGIN IMMEDIATE")
            with patch.object(store.sqlite3, "connect", side_effect=lambda *a, **kw: original(*a, timeout=0, **kw)):
                self.error("storage_busy", self.recover)
            con.rollback()

    def test_recovery_and_preparation_wrong_store_generation_rejected(self):
        self.prepare()
        self.fx.sql("UPDATE managed_store_meta SET store_generation=?", (str(uuid4()),))
        self.error("store_generation_mismatch", self.recover)
        self.error("store_generation_mismatch", self.prepare)

    def test_receipt_verifier_closed_types_versions_and_reconciliation(self):
        original = self.execute(self.prepare())
        for field, replacement in (("status", "prepared"), ("reason", "other"),
                                   ("store_id", str(uuid4())), ("store_generation", str(uuid4())),
                                   ("local_deck_application_receipt_model_version", "2"), ("limitations", []),
                                   ("source_intent_identity", {}), ("operation_id", True), ("extra", 1)):
            value = deepcopy(original)
            value[field] = replacement
            self.rehash(value)
            with self.assertRaises(ValueError):
                outcome.require_local_deck_application_receipt(value)
        for field in original:
            value = deepcopy(original)
            del value[field]
            with self.assertRaises(ValueError):
                outcome.require_local_deck_application_receipt(value)
        value = deepcopy(original)
        value["application_result"]["new_revision"] = True
        self.rehash(value)
        with self.assertRaises(ValueError):
            outcome.require_local_deck_application_receipt(value)

    def test_receipt_identity_digest_and_python_shapes(self):
        original = self.execute(self.prepare())
        for field, replacement in (("digest", "0" * 64), ("digest_algorithm", "sha512"),
                                   ("local_deck_application_receipt_identity_version", "2"), ("canonical_payload", {})):
            value = deepcopy(original)
            value[ID][field] = replacement
            with self.assertRaises(ValueError):
                outcome.require_local_deck_application_receipt(value)
        value = deepcopy(original)
        value["limitations"] = tuple(value["limitations"])
        with self.assertRaises(ValueError):
            outcome.require_local_deck_application_receipt(value)

    def test_receipt_is_detached_historical_and_performs_no_io(self):
        receipt = self.execute(self.prepare())
        with patch("sqlite3.connect", side_effect=AssertionError("IO")), \
             patch.object(application, "build_pre_execution_revalidation", side_effect=AssertionError("fresh validation")):
            verified = outcome.require_local_deck_application_receipt(json.loads(json.dumps(receipt)))
        verified["application_result"]["execution_context"]["collection"].clear()
        self.assertNotEqual(verified, receipt)
        self.assertEqual(self.recover()["receipt"], receipt)

    def test_stored_receipt_tampering_is_error_not_absence(self):
        self.execute(self.prepare())
        self.fx.sql("UPDATE managed_application_operations SET receipt_json='{}'")
        self.error("receipt_mismatch", self.recover)

    def test_stored_binding_tampering_rejected(self):
        operation = self.prepare()
        self.fx.sql("UPDATE managed_application_operations SET expected_revision=2")
        self.error("malformed_operation", self.recover)
        self.error("malformed_operation", lambda: self.execute(operation))

    def test_database_constraints_reject_illegal_lifecycle_and_duplicate_identity(self):
        self.prepare()
        for sql in ("UPDATE managed_application_operations SET state='committed'",
                    "UPDATE managed_application_operations SET state='other'",
                    "UPDATE managed_application_operations SET receipt_json='{}'",
                    "UPDATE managed_application_operations SET expected_revision=0"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.fx.sql(sql)
        with closing(sqlite3.connect(self.path)) as con, con:
            row = list(con.execute("SELECT * FROM managed_application_operations").fetchone())
            row[0] = str(uuid4())
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("INSERT INTO managed_application_operations VALUES (?,?,?,?,?,?,?,?)", row)

    def test_recovery_performs_no_logical_write_or_deck_content_inference(self):
        operation = self.prepare()
        receipt = self.execute(operation)
        before = self.path.read_bytes()
        self.assertEqual(self.recover()["receipt"], receipt)
        self.assertEqual(self.path.read_bytes(), before)

    def test_preparation_insert_failure_rolls_back_registration(self):
        original = sqlite3.connect
        class Failing(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                value = super().execute(sql, parameters)
                if sql.startswith("INSERT INTO managed_application_operations"):
                    raise sqlite3.OperationalError("failed registration")
                return value
        with patch.object(store.sqlite3, "connect", side_effect=lambda *a, **kw: original(*a, factory=Failing, **kw)):
            self.error("transaction_failed", self.prepare)
        self.assertEqual(self.recover()["status"], "not_found")
        self.assertEqual(self.fx.read(), self.state)

    def test_application_result_failure_leaves_no_receipt(self):
        operation = self.prepare()
        with patch.object(application, "require_local_deck_application_result", side_effect=ValueError("bad result")):
            self.error("result_mismatch", lambda: self.execute(operation))
        self.assertEqual(self.recover()["status"], "prepared")
        self.assertEqual(self.fx.read(), self.state)

    def test_coherently_rewritten_receipt_is_not_durable_proof(self):
        receipt = self.execute(self.prepare())
        rewritten = deepcopy(receipt)
        rewritten["operation_id"] = str(uuid4())
        self.rehash(rewritten)
        self.assertEqual(outcome.require_local_deck_application_receipt(rewritten), rewritten)
        self.fx.sql("UPDATE managed_application_operations SET receipt_json=?", (outcome._encoded(rewritten).decode(),))
        self.error("receipt_mismatch", self.recover)

    def test_missing_or_wrong_store_never_report_not_found(self):
        missing = self.fx.fx.root / "missing.db"
        self.error("missing_store", lambda: outcome.recover_local_deck_application(self.intent, store_path=missing))
        other = self.fx.fx.root / "different.db"
        store.initialize_store(other)
        store.migrate_store_v1_to_v2(other)
        self.error("store_identity_mismatch", lambda: outcome.recover_local_deck_application(self.intent, store_path=other))

    def test_new_preparation_requires_current_live_destination(self):
        self.fx.apply()
        self.error("revision_mismatch", self.prepare)
        self.assertEqual(self.count(), 0)

    def test_closed_authority_and_spoofed_objects_cannot_execute(self):
        operation = self.prepare()
        self.error("invalid_execution_context", lambda: self.execute(operation, object()))
        self.authority.close()
        self.error("execution_context_unavailable", lambda: self.execute(operation))
        self.assertEqual(self.recover()["status"], "prepared")

    def test_authority_publication_blocked_through_receipt_commit(self):
        operation = self.prepare()
        original = sqlite3.connect
        started, finished = Event(), Event()
        futures = []
        def publish():
            started.set()
            result = self.fx.fx.publish(collection=Collection({}))
            finished.set()
            return result
        test = self
        with ThreadPoolExecutor(max_workers=1) as pool:
            class Commit(sqlite3.Connection):
                def commit(self):
                    # The receipt already exists inside this transaction.
                    test.assertEqual(self.execute("SELECT state FROM managed_application_operations").fetchone()[0], "committed")
                    futures.append(pool.submit(publish))
                    test.assertTrue(started.wait(3))
                    test.assertFalse(finished.wait(.05))
                    return super().commit()
            with patch.object(store.sqlite3, "connect", side_effect=lambda *a, **kw: original(*a, factory=Commit, **kw)):
                receipt = self.execute(operation)
            self.assertEqual(futures[0].result(5)["generation"], 2)
        self.assertEqual(receipt["application_result"]["execution_context"]["generation"], 1)

    def test_execution_rollback_versus_recovery_delete_and_wal(self):
        original = store._replace_in_transaction
        for journal in ("DELETE", "WAL"):
            self.fx.sql("PRAGMA journal_mode=" + journal)
            state = store.create_managed_deck(self.path, self.state["deck"], journal)
            intent = self.fx.make_intent(state=state)
            operation = self.prepare(intent)
            entered, release, recovering = Event(), Event(), Event()
            def fail(*args):
                original(*args)
                entered.set()
                self.assertTrue(release.wait(5))
                raise store.ManagedDeckStoreError("result_mismatch")
            def execute():
                try:
                    self.execute(operation)
                except store.ManagedDeckStoreError as exc:
                    return exc.code
            def recover():
                recovering.set()
                return self.recover(intent)
            with patch.object(store, "_replace_in_transaction", side_effect=fail), ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(execute)
                self.assertTrue(entered.wait(3))
                second = pool.submit(recover)
                self.assertTrue(recovering.wait(3))
                release.set()
                self.assertEqual(first.result(10), "result_mismatch")
                self.assertEqual(second.result(10)["status"], "prepared")
            self.assertEqual(store.read_destination_state(self.path, state["record_id"]), state)

    def test_unsupported_operation_version_and_extra_fields_before_io(self):
        operation = self.prepare()
        for field, value in (("local_deck_application_operation_model_version", "2"), ("extra", 1),
                             ("store_id", str(uuid4())), ("record_id", str(uuid4())),
                             ("source_intent_identity", {})):
            changed = {**operation, field: value}
            with patch("sqlite3.connect", side_effect=AssertionError("IO")):
                self.error("invalid_operation", lambda: self.execute(changed))

    def test_no_excluded_side_effects_during_full_lifecycle(self):
        with ExitStack() as stack:
            for name in ("services.exporter.export_arena_deck", "services.exporter.import_arena_deck",
                         "mtgadb.managed_deck_store.create_managed_deck", "mtgadb.managed_deck_store.conditional_delete",
                         "socket.socket"):
                stack.enter_context(patch(name, side_effect=AssertionError(name)))
            receipt = self.execute(self.prepare())
            self.assertEqual(self.recover()["receipt"], receipt)


if __name__ == "__main__":
    unittest.main()
