"""#6U durable Model 2 composition, atomicity and frozen v1 coexistence."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, ExitStack
from copy import deepcopy
from dataclasses import replace
import hashlib
import inspect
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Barrier, Event
import unittest
from unittest.mock import patch
from uuid import uuid4

from mtgadb import managed_deck_store as store
from mtgadb.model import Collection
from services import local_deck_application as application
from services import local_deck_application_outcome as outcome
from services.local_execution_validation import LocalDeckApplicationError, LocalExecutionValidationAuthorityV1
from tests import test_scoped_execution_composition as scoped_tests
from tests import test_local_deck_application_outcome as legacy_tests


class DurableScopedExecutionCompositionTests(unittest.TestCase):
    def setUp(self):
        self.fx = scoped_tests.ScopedExecutionCompositionTests(methodName="runTest")
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.path = self.fx.path
        self.state = self.fx.state
        self.intent = self.fx.intent
        self.legacy_intent = self.fx.legacy.intent
        self.authority = self.fx.authority
        store.migrate_store_v1_to_v2(self.path)

    def prepare(self, intent=None):
        return outcome.prepare_local_deck_application_v2(self.intent if intent is None else intent, store_path=self.path)

    def execute(self, operation):
        return outcome.execute_prepared_local_deck_application_v2(operation, store_path=self.path, validation_authority=self.authority)

    def recover(self, intent=None):
        return outcome.recover_local_deck_application_v2(self.intent if intent is None else intent, store_path=self.path)

    def sql(self, sql, args=()):
        with closing(sqlite3.connect(self.path)) as con, con: con.execute(sql, args)

    def rows(self):
        with closing(sqlite3.connect(self.path)) as con:
            return con.execute("SELECT operation_id,state,receipt_json FROM managed_application_operations ORDER BY operation_id").fetchall()

    def error(self, code, action):
        with self.assertRaises((outcome.LocalDeckApplicationOutcomeError, LocalDeckApplicationError, store.ManagedDeckStoreError)) as caught: action()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def assert_prepared_unchanged(self, before):
        self.assertEqual(store.serialize_destination_state(self.fx.read()), before)
        observed = self.recover()
        self.assertEqual(observed["status"], "prepared")
        self.assertIsNone(observed["receipt"])

    def other(self, model=2):
        state = store.create_managed_deck(self.path, self.state["deck"], "Other", "Test")
        return self.fx.make_intent(state=state) if model == 2 else self.fx.legacy.make_intent(state=state)

    def test_v2_prepare_execute_recover_exact_atomic_receipt(self):
        frozen = deepcopy(self.intent)
        before = self.fx.read()
        operation = self.prepare()
        self.assertEqual(operation["local_deck_application_operation_model_version"], "2")
        self.assertEqual(self.fx.read(), before)
        receipt = self.execute(operation)
        current = self.fx.read()
        self.assertEqual(receipt["local_deck_application_receipt_model_version"], "2")
        self.assertEqual(receipt["application_result"]["local_deck_application_result_model_version"], "2")
        self.assertEqual(current["revision"], before["revision"] + 1)
        self.assertEqual(current["metadata"], before["metadata"])
        self.assertEqual(current["gameplay_snapshot_identity"], self.intent["expected_result_deck_identity"])
        self.assertEqual(receipt, outcome.require_local_deck_application_receipt_v2(json.loads(json.dumps(receipt))))
        self.assertEqual(self.recover()["receipt"], receipt)
        self.assertEqual(self.intent, frozen)
        self.assertEqual(sum(receipt["application_result"]["execution_pre_execution_revalidation"]["fresh_validation"]["wildcard_cost"].values()), 0)
        self.error("operation_state_conflict", lambda: self.execute(operation))

    def test_v2_prepare_is_idempotent_without_authority_validation(self):
        before = self.fx.read()
        with patch.object(application, "build_pre_execution_revalidation_v2", side_effect=AssertionError("validation")), patch.object(type(self.authority), "_guarded", side_effect=AssertionError("guard")):
            operation = self.prepare()
            self.assertEqual(self.prepare(), operation)
            self.assertEqual(self.recover()["status"], "prepared")
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.fx.read(), before)
        self.execute(operation)
        self.assertEqual(self.prepare(), operation)
        self.assertEqual(len(self.rows()), 1)

    def test_expected_model_is_fixed_at_every_persisted_read(self):
        reads = []
        original = outcome._read_operation_v2
        def observed(*args, **kwargs):
            reads.append("intent" if kwargs.get("intent_digest") is not None else "operation")
            return original(*args, **kwargs)
        with patch.object(outcome, "_read_operation_v2", side_effect=observed), patch.object(outcome, "_read_operation", side_effect=AssertionError("wrong owner")):
            operation = self.prepare(); self.execute(operation); self.recover()
        self.assertEqual(reads, ["intent", "operation", "operation", "operation", "operation", "intent"])

    def test_wrong_model_prepared_rows_fail_closed_both_directions(self):
        op1 = outcome.prepare_local_deck_application(self.legacy_intent, store_path=self.path)
        op2 = self.prepare()
        with store._connection(self.path, write=True, mutation=True) as con:
            meta = outcome._store(con, self.intent)
            for reader, wrong, intent in ((outcome._read_operation_v2, op1, self.legacy_intent), (outcome._read_operation, op2, self.intent)):
                self.error("malformed_operation", lambda: reader(con, meta, operation_id=wrong["operation_id"]))
                self.error("malformed_operation", lambda: reader(con, meta, intent_digest=intent["local_deck_application_intent_identity"]["digest"]))
        self.error("invalid_intent", lambda: self.recover(self.legacy_intent))
        self.error("invalid_intent", lambda: outcome.recover_local_deck_application(self.intent, store_path=self.path))
        self.assertTrue(all(row[1:] == ("prepared", None) for row in self.rows()))

    def test_wrong_model_committed_receipts_fail_closed_both_directions(self):
        op1 = outcome.prepare_local_deck_application(self.legacy_intent, store_path=self.path)
        receipt1 = outcome.execute_prepared_local_deck_application(op1, store_path=self.path, validation_authority=self.authority)
        other = self.other()
        op2 = self.prepare(other); receipt2 = self.execute(op2)
        for op, wrong_receipt, recover in ((op1, receipt2, lambda: outcome.recover_local_deck_application(self.legacy_intent, store_path=self.path)),
                                          (op2, receipt1, lambda: self.recover(other))):
            self.sql("UPDATE managed_application_operations SET receipt_json=? WHERE operation_id=?", (outcome._encoded(wrong_receipt).decode(), op["operation_id"]))
            self.error("receipt_mismatch", recover)
        with self.assertRaises(ValueError): outcome.require_local_deck_application_receipt_v2(receipt1)
        with self.assertRaises(ValueError): outcome.require_local_deck_application_receipt(receipt2)

    def test_operation_uuid_collision_never_overwrites_other_model(self):
        op1 = outcome.prepare_local_deck_application(self.legacy_intent, store_path=self.path)
        before = self.rows()
        with patch.object(outcome, "uuid4", return_value=op1["operation_id"]):
            self.error("malformed_operation", self.prepare)
        self.assertEqual(self.rows(), before)
        op2 = self.prepare()
        other1 = self.other(model=1)
        before = self.rows()
        with patch.object(outcome, "uuid4", return_value=op2["operation_id"]):
            self.error("malformed_operation", lambda: outcome.prepare_local_deck_application(other1, store_path=self.path))
        self.assertEqual(self.rows(), before)

    def test_intent_models_have_canonical_key_domain_separation(self):
        one = self.legacy_intent["local_deck_application_intent_identity"]["canonical_payload"]
        two = self.intent["local_deck_application_intent_identity"]["canonical_payload"]
        self.assertEqual(one["local_deck_application_intent_model_version"], "1")
        self.assertEqual(two["local_deck_application_intent_model_version"], "2")
        self.assertNotEqual(outcome._encoded(one), outcome._encoded(two))
        self.assertNotEqual(self.legacy_intent["local_deck_application_intent_identity"]["digest"], self.intent["local_deck_application_intent_identity"]["digest"])
        self.assertEqual(one["destination_state"], two["destination_state"])
        op1 = outcome.prepare_local_deck_application(self.legacy_intent, store_path=self.path)
        op2 = self.prepare()
        self.assertNotEqual(op1["operation_id"], op2["operation_id"])
        self.assertEqual(op1["expected_revision"], op2["expected_revision"])

    def test_stored_lookup_bindings_and_canonical_json_are_verified(self):
        operation = self.prepare()
        for field, value in (("intent_digest", "0" * 64), ("expected_revision", 2), ("store_generation", str(uuid4())),
                             ("intent_json", "{}"), ("intent_json", " " + outcome._encoded(self.intent).decode())):
            with self.subTest(field=field, value=value[:50] if isinstance(value,str) else value):
                with store._connection(self.path, write=True, mutation=True) as con:
                    meta = outcome._store(con, self.intent)
                    previous = con.execute("SELECT " + field + " FROM managed_application_operations WHERE operation_id=?", (operation["operation_id"],)).fetchone()[0]
                    con.execute("UPDATE managed_application_operations SET " + field + "=? WHERE operation_id=?", (value, operation["operation_id"]))
                    self.error("malformed_operation", lambda: outcome._read_operation_v2(con, meta, operation_id=operation["operation_id"]))
                    con.execute("UPDATE managed_application_operations SET " + field + "=? WHERE operation_id=?", (previous, operation["operation_id"]))

    def test_coherent_model_restamping_cannot_cross_owner_boundaries(self):
        operation = self.prepare()
        bad = deepcopy(operation); bad["local_deck_application_operation_model_version"] = "1"
        self.error("invalid_operation", lambda: self.execute(bad))
        self.error("invalid_operation", lambda: outcome.execute_prepared_local_deck_application(operation, store_path=self.path, validation_authority=self.authority))
        receipt = self.execute(operation)
        bad = deepcopy(receipt); bad["local_deck_application_receipt_model_version"] = "1"
        payload = {key:value for key,value in bad.items() if key != "local_deck_application_receipt_identity"}
        bad["local_deck_application_receipt_identity"]["canonical_payload"] = payload
        bad["local_deck_application_receipt_identity"]["digest"] = hashlib.sha256(outcome._encoded(payload)).hexdigest()
        with self.assertRaises(ValueError): outcome.require_local_deck_application_receipt_v2(bad)
        with self.assertRaises(ValueError): outcome.require_local_deck_application_receipt(bad)

    def test_unhashable_and_malformed_versions_never_select_owners(self):
        operation = self.prepare()
        before = self.rows()
        for value in ([], {}, None, True, 2, "3"):
            for model in ("local_deck_application_operation_model_version",):
                bad = deepcopy(operation); bad[model] = value
                with patch.object(store, "_connection", side_effect=AssertionError("IO")):
                    self.error("invalid_operation", lambda: self.execute(bad))
        receipt = self.execute(operation)
        for value in ([], {}, None, True, 2, "3"):
            bad = deepcopy(receipt); bad["local_deck_application_receipt_model_version"] = value
            with self.assertRaises(ValueError): outcome.require_local_deck_application_receipt_v2(bad)
        self.assertEqual(len(self.rows()), len(before))

    def test_v2_revalidation_runs_under_guard_and_writer_before_replace(self):
        operation = self.prepare(); events = []
        original = application.build_pre_execution_revalidation_v2
        replacement = store._replace_in_transaction
        def fresh(*args, **kwargs):
            self.assertFalse(self.authority._lock.acquire(blocking=False))
            with closing(sqlite3.connect(self.path, timeout=0)) as competitor:
                with self.assertRaises(sqlite3.OperationalError): competitor.execute("BEGIN IMMEDIATE")
            events.append("revalidation2")
            return original(*args, **kwargs)
        def replace_deck(*args):
            self.assertEqual(events, ["revalidation2"])
            events.append("replacement")
            return replacement(*args)
        with patch.object(application, "build_pre_execution_revalidation_v2", side_effect=fresh), patch.object(application, "build_pre_execution_revalidation", side_effect=AssertionError("v1")), patch.object(store, "_replace_in_transaction", side_effect=replace_deck):
            self.execute(operation)
        self.assertEqual(events, ["revalidation2", "replacement"])

    def test_v2_resource_policy_printing_and_baseline_failures_leave_prepared(self):
        operation = self.prepare(); before = store.serialize_destination_state(self.fx.read())
        self.fx.legacy.fx.publish(collection=Collection({}))
        self.error("fresh_validation_failed", lambda: self.execute(operation)); self.assert_prepared_unchanged(before)
        self.fx.legacy.fx.publish(rules=replace(self.fx.upstream.rules, min_main=1))
        self.error("execution_context_conflict", lambda: self.execute(operation)); self.assert_prepared_unchanged(before)
        with closing(sqlite3.connect(self.fx.legacy.fx.cards)) as con, con:
            con.execute("UPDATE printings SET title_id=2 WHERE arena_id=101")
        self.fx.legacy.fx.publish()
        self.error("fresh_validation_failed", lambda: self.execute(operation)); self.assert_prepared_unchanged(before)

    def test_v2_result_and_receipt_verification_failures_roll_back(self):
        operation = self.prepare(); before = store.serialize_destination_state(self.fx.read())
        with patch.object(application, "require_local_deck_application_result_v2", side_effect=ValueError("bad")):
            self.error("result_mismatch", lambda: self.execute(operation))
        self.assert_prepared_unchanged(before)
        with patch.object(outcome, "require_local_deck_application_receipt_v2", side_effect=ValueError("bad")):
            self.error("receipt_mismatch", lambda: self.execute(operation))
        self.assert_prepared_unchanged(before)

    def test_v2_receipt_update_and_reread_failures_roll_back(self):
        operation = self.prepare(); before = store.serialize_destination_state(self.fx.read())
        original = sqlite3.connect
        class Failing(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql.startswith("UPDATE managed_application_operations SET state"):
                    super().execute(sql, parameters)
                    raise sqlite3.OperationalError("partial receipt failure")
                return super().execute(sql, parameters)
        with patch.object(store.sqlite3, "connect", side_effect=lambda *a,**kw: original(*a,factory=Failing,**kw)):
            self.error("transaction_failed", lambda: self.execute(operation))
        self.assert_prepared_unchanged(before)
        reader = outcome._read_operation_v2
        def wrong(*args, **kwargs):
            value = reader(*args, **kwargs)
            if value and value["state"] == "committed": value["receipt"]["reason"] = "wrong"
            return value
        with patch.object(outcome, "_read_operation_v2", side_effect=wrong):
            self.error("receipt_mismatch", lambda: self.execute(operation))
        self.assert_prepared_unchanged(before)

    def test_v2_process_interruption_before_after_commit_delete_and_wal(self):
        script = legacy_tests.CRASH_SCRIPT.replace(
            "from services.local_deck_application_outcome import execute_prepared_local_deck_application",
            "from services.local_deck_application_outcome import execute_prepared_local_deck_application_v2 as execute_prepared_local_deck_application")
        for journal in ("DELETE", "WAL"):
            self.sql("PRAGMA journal_mode=" + journal)
            for phase in ("before", "after"):
                intent = self.other(); operation = self.prepare(intent)
                operation_file = self.fx.legacy.fx.root / (journal + phase + ".json")
                operation_file.write_text(json.dumps(operation))
                run = subprocess.run([sys.executable, "-B", "-c", script, str(self.path), str(operation_file), phase, str(self.fx.legacy.fx.cards)], capture_output=True, timeout=20)
                self.assertEqual(run.returncode, 73, run.stderr.decode())
                observed = self.recover(intent)
                self.assertEqual(observed["status"], "prepared" if phase == "before" else "committed")
                self.assertEqual(observed["receipt"] is None, phase == "before")
                current = store.read_destination_state(self.path, operation["record_id"])
                self.assertEqual(current["revision"], 1 if phase == "before" else 2)

    def test_v2_uncertain_commit_requires_recovery_not_inferred_success(self):
        operation = self.prepare(); before = store.serialize_destination_state(self.fx.read()); original = sqlite3.connect
        class Before(sqlite3.Connection):
            def commit(self): raise sqlite3.OperationalError("not committed")
        with patch.object(store.sqlite3, "connect", side_effect=lambda *a,**kw: original(*a,factory=Before,**kw)):
            self.error("commit_failed", lambda: self.execute(operation))
        self.assert_prepared_unchanged(before)
        class After(sqlite3.Connection):
            def commit(self):
                super().commit()
                raise sqlite3.OperationalError("acknowledgment lost")
        with patch.object(store.sqlite3, "connect", side_effect=lambda *a,**kw: original(*a,factory=After,**kw)):
            self.error("commit_failed", lambda: self.execute(operation))
        self.assertEqual(self.recover()["status"], "committed")
        self.assertEqual(self.fx.read()["revision"], 2)

    def test_v2_recovery_is_observation_only_and_survives_destination_changes(self):
        operation = self.prepare(); receipt = self.execute(operation)
        current = self.fx.read()
        store.conditional_replace(self.path, current["record_id"], current, current["deck"], name="Later", format_label="Test")
        with patch.object(store, "_read", side_effect=AssertionError("deck read")), patch.object(application, "build_pre_execution_revalidation_v2", side_effect=AssertionError("validation")), patch.object(type(self.authority), "_guarded", side_effect=AssertionError("guard")), patch.object(outcome, "execute_prepared_local_deck_application_v2", side_effect=AssertionError("retry")):
            self.assertEqual(self.recover()["receipt"], receipt)

    def test_v2_recovery_busy_wrong_store_and_corruption_are_errors(self):
        operation = self.prepare()
        original = sqlite3.connect
        class Busy(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql == "BEGIN IMMEDIATE":
                    exc = sqlite3.OperationalError("database is locked")
                    exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
                    raise exc
                return super().execute(sql, parameters)
        with patch.object(store.sqlite3, "connect", side_effect=lambda *a,**kw: original(*a,factory=Busy,**kw)):
            self.error("storage_busy", self.recover)
        self.sql("UPDATE managed_application_operations SET intent_json='{}'")
        self.error("malformed_operation", self.recover)
        self.sql("UPDATE managed_store_meta SET store_generation=?", (str(uuid4()),))
        self.error("store_generation_mismatch", self.recover)

    def test_v2_concurrent_execute_and_recovery_observe_atomic_state(self):
        for journal in ("DELETE", "WAL"):
            self.sql("PRAGMA journal_mode=" + journal)
            intent = self.other(); operation = self.prepare(intent)
            entered, release, recovering, finished = Event(), Event(), Event(), Event()
            original = application.build_pre_execution_revalidation_v2
            def paused(*a, **kw):
                entered.set(); self.assertTrue(release.wait(5)); return original(*a, **kw)
            def recover():
                recovering.set(); result = self.recover(intent); finished.set(); return result
            with patch.object(application, "build_pre_execution_revalidation_v2", side_effect=paused), ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.execute, operation)
                try:
                    self.assertTrue(entered.wait(3)); second = pool.submit(recover)
                    self.assertTrue(recovering.wait(3)); self.assertFalse(finished.wait(.05))
                finally: release.set()
                receipt = first.result(10); self.assertEqual(second.result(10)["receipt"], receipt)
            self.error("operation_state_conflict", lambda: self.execute(operation))

    def test_v2_authority_publication_waits_through_receipt_commit(self):
        operation = self.prepare(); started, finished = Event(), Event(); original = sqlite3.connect; test = self; futures = []
        def publish():
            started.set(); result = test.fx.legacy.fx.publish(collection=Collection({})); finished.set(); return result
        with ThreadPoolExecutor(max_workers=1) as pool:
            class Commit(sqlite3.Connection):
                def commit(self):
                    test.assertEqual(self.execute("SELECT state FROM managed_application_operations").fetchone()[0], "committed")
                    futures.append(pool.submit(publish)); test.assertTrue(started.wait(3)); test.assertFalse(finished.wait(.05))
                    return super().commit()
            with patch.object(store.sqlite3, "connect", side_effect=lambda *a,**kw: original(*a,factory=Commit,**kw)):
                receipt = self.execute(operation)
            futures[0].result(5)
        self.assertEqual(receipt["application_result"]["execution_context"]["generation"], 2)

    def test_v1_global_lookup_patch_points_remain_at_original_stages(self):
        operation = outcome.prepare_local_deck_application(self.legacy_intent, store_path=self.path); events = []
        with ExitStack() as stack:
            for target, name in ((outcome,"_read_operation"),(application,"_reconstruct"),(application,"build_pre_execution_revalidation"),(application,"_fresh"),(application,"_result"),(application,"require_local_deck_application_result"),(outcome,"_receipt"),(outcome,"require_local_deck_application_receipt")):
                original = getattr(target,name)
                def spy(*a, _name=name, _original=original, **kw): events.append(_name); return _original(*a,**kw)
                stack.enter_context(patch.object(target,name,side_effect=spy))
            outcome.execute_prepared_local_deck_application(operation, store_path=self.path, validation_authority=self.authority)
        self.assertEqual(events[0], "_read_operation")
        self.assertLess(events.index("_reconstruct"), events.index("build_pre_execution_revalidation"))
        self.assertLess(events.index("build_pre_execution_revalidation"), events.index("_fresh"))
        self.assertLess(events.index("_fresh"), events.index("_result"))
        self.assertEqual(events.count("_read_operation"), 2)
        self.assertIn("require_local_deck_application_receipt", events)

    def test_v1_signatures_failure_order_and_owner_selection_remain_exact(self):
        self.assertEqual(str(inspect.signature(outcome.prepare_local_deck_application)), "(intent: 'dict', *, store_path) -> 'dict'")
        self.assertEqual(str(inspect.signature(outcome.recover_local_deck_application)), "(intent: 'dict', *, store_path) -> 'dict'")
        self.assertEqual(str(inspect.signature(outcome._read_operation)), "(con, meta, *, operation_id=None, intent_digest=None)")
        with patch.object(store, "_connection", side_effect=AssertionError("IO")):
            self.error("invalid_intent", lambda: outcome.prepare_local_deck_application(self.intent, store_path=self.path))
            self.error("invalid_intent", lambda: outcome.recover_local_deck_application(self.intent, store_path=self.path))
            self.error("invalid_intent", lambda: self.prepare(self.legacy_intent))
        operation = self.prepare()
        self.error("invalid_operation", lambda: outcome.execute_prepared_local_deck_application(operation, store_path=self.path, validation_authority=self.authority))

    def test_mixed_rows_coexist_without_schema_or_v1_byte_changes(self):
        op1 = outcome.prepare_local_deck_application(self.legacy_intent, store_path=self.path); op2 = self.prepare()
        self.assertEqual(op1, outcome._operation(self.legacy_intent, op1["operation_id"]))
        self.assertEqual(self.recover()["operation"], op2)
        with closing(sqlite3.connect(self.path)) as con:
            self.assertEqual(len(con.execute("PRAGMA table_info(managed_application_operations)").fetchall()), 8)
            self.assertEqual(con.execute("SELECT schema_version FROM managed_store_meta").fetchone()[0], "2")
        self.assertEqual(outcome.recover_local_deck_application(self.legacy_intent, store_path=self.path)["operation"], op1)

    def test_durable_v2_preserves_scoped_provenance_and_non_consumption(self):
        operation = self.prepare()
        with patch("services.intelligence.analyze_deck", side_effect=AssertionError("analysis")), patch("services.candidates.discover_candidates", side_effect=AssertionError("discovery")):
            receipt = self.execute(operation)
        self.assertEqual(receipt["application_result"]["execution_pre_execution_revalidation"]["source_human_proposal_decision"], self.fx.decision)
        text = " ".join(receipt["limitations"])
        for phrase in ("historical digest-bound provenance", "resolved or improved", "not destination-bound", "not globally consumed", "not globally single-use", "another explicitly requested matching destination"):
            self.assertIn(phrase,text)
        other = self.other(); self.assertEqual(self.execute(self.prepare(other))["status"], "committed")

    def test_non_durable_v2_does_not_backfill_or_transition_durable_operation(self):
        operation = self.prepare(); result = self.fx.apply()
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.recover()["status"], "prepared")
        self.assertIsNone(self.recover()["receipt"])
        self.error("revision_mismatch", lambda: self.execute(operation))

    def cross_chain(self, prepared_model, winner):
        operation = self.prepare() if prepared_model == 2 else outcome.prepare_local_deck_application(self.legacy_intent, store_path=self.path)
        if winner == "non_durable":
            self.fx.apply(); winning_receipt = None
        elif winner == 2:
            winning_receipt = self.execute(self.prepare())
        else:
            winning_operation = outcome.prepare_local_deck_application(self.legacy_intent, store_path=self.path)
            winning_receipt = outcome.execute_prepared_local_deck_application(winning_operation, store_path=self.path, validation_authority=self.authority)
        before = store.serialize_destination_state(self.fx.read())
        loser = (lambda: self.execute(operation)) if prepared_model == 2 else (lambda: outcome.execute_prepared_local_deck_application(operation, store_path=self.path, validation_authority=self.authority))
        self.error("revision_mismatch", loser)
        self.assertEqual(store.serialize_destination_state(self.fx.read()), before)
        observed = self.recover() if prepared_model == 2 else outcome.recover_local_deck_application(self.legacy_intent, store_path=self.path)
        self.assertEqual(observed["status"], "prepared"); self.assertIsNone(observed["receipt"])
        receipts = [row for row in self.rows() if row[2] is not None]
        self.assertEqual(len(receipts), 0 if winning_receipt is None else 1)

    def test_prepare_v1_then_non_durable_v2_then_execute_v1(self): self.cross_chain(1,"non_durable")
    def test_prepare_v2_then_non_durable_v2_then_execute_v2(self): self.cross_chain(2,"non_durable")
    def test_prepare_v1_then_durable_v2_then_execute_v1(self): self.cross_chain(1,2)
    def test_prepare_v2_then_durable_v1_then_execute_v2(self): self.cross_chain(2,1)

    def test_prepare_both_models_same_baseline_first_committer_wins(self):
        for winner in (1,2):
            with self.subTest(winner=winner):
                state = store.create_managed_deck(self.path,self.state["deck"],"Contended","Test")
                one = self.fx.legacy.make_intent(state=state); two = self.fx.make_intent(state=state)
                op1 = outcome.prepare_local_deck_application(one,store_path=self.path); op2 = self.prepare(two)
                if winner == 1:
                    outcome.execute_prepared_local_deck_application(op1,store_path=self.path,validation_authority=self.authority)
                    self.error("revision_mismatch",lambda:self.execute(op2))
                else:
                    self.execute(op2)
                    self.error("revision_mismatch",lambda:outcome.execute_prepared_local_deck_application(op1,store_path=self.path,validation_authority=self.authority))
                observed1=outcome.recover_local_deck_application(one,store_path=self.path); observed2=self.recover(two)
                self.assertEqual(observed1["status"],"committed" if winner==1 else "prepared")
                self.assertEqual(observed2["status"],"committed" if winner==2 else "prepared")
                loser=observed2 if winner==1 else observed1; self.assertIsNone(loser["receipt"])
                self.assertEqual(store.read_destination_state(self.path,state["record_id"])["revision"],2)


    def test_persisted_scoped_evidence_and_versions_cannot_survive_tampering(self):
        operation = self.prepare()
        original = outcome._encoded(self.intent).decode()
        for value in ([], {}, "1", 2):
            bad = deepcopy(self.intent); bad["local_deck_application_intent_model_version"] = value
            self.sql("UPDATE managed_application_operations SET intent_json=?", (outcome._encoded(bad).decode(),))
            self.error("malformed_operation", self.recover)
        bad = deepcopy(self.intent)
        bad["source_pre_execution_revalidation"]["source_human_proposal_decision"]["source_presentation"]["review_artifact"]["evidence_context"]["candidate_title_id"] += 1
        self.sql("UPDATE managed_application_operations SET intent_json=?", (outcome._encoded(bad).decode(),))
        self.error("malformed_operation", lambda: self.execute(operation))
        self.error("malformed_operation", self.recover)
        self.sql("UPDATE managed_application_operations SET intent_json=?", (original,))
        receipt = self.execute(operation)
        bad = deepcopy(receipt)
        bad["application_result"]["execution_pre_execution_revalidation"]["source_human_proposal_decision"]["source_presentation"]["review_artifact"]["evidence_context"]["candidate_title_id"] += 1
        self.sql("UPDATE managed_application_operations SET receipt_json=?", (outcome._encoded(bad).decode(),))
        self.error("receipt_mismatch", self.recover)
        self.assertEqual(self.fx.read()["revision"], 2)

    def test_v2_same_operation_concurrency_has_one_commit(self):
        second = LocalExecutionValidationAuthorityV1(**self.fx.legacy.fx.args)
        self.addCleanup(second.close)
        for journal in ("DELETE", "WAL"):
            self.sql("PRAGMA journal_mode=" + journal)
            intent = self.other(); operation = self.prepare(intent); barrier = Barrier(2)
            def execute(authority):
                barrier.wait(3)
                try:
                    return outcome.execute_prepared_local_deck_application_v2(operation, store_path=self.path, validation_authority=authority)["status"]
                except (outcome.LocalDeckApplicationOutcomeError, store.ManagedDeckStoreError) as exc: return exc.code
            with ThreadPoolExecutor(max_workers=2) as pool:
                a=pool.submit(execute,self.authority); b=pool.submit(execute,second)
                self.assertCountEqual([a.result(15),b.result(15)], ["committed","operation_state_conflict"])
            self.assertEqual(self.recover(intent)["status"],"committed")
            self.assertEqual(store.read_destination_state(self.path,operation["record_id"])["revision"],2)

    def test_concurrent_cross_model_first_commit_stales_other_without_receipt(self):
        second = LocalExecutionValidationAuthorityV1(**self.fx.legacy.fx.args)
        self.addCleanup(second.close)
        for journal in ("DELETE", "WAL"):
            self.sql("PRAGMA journal_mode=" + journal)
            state=store.create_managed_deck(self.path,self.state["deck"],"Concurrent","Test")
            one=self.fx.legacy.make_intent(state=state);two=self.fx.make_intent(state=state)
            op1=outcome.prepare_local_deck_application(one,store_path=self.path);op2=self.prepare(two);barrier=Barrier(2)
            def execute(fn,op,authority):
                barrier.wait(3)
                try:return fn(op,store_path=self.path,validation_authority=authority)["status"]
                except store.ManagedDeckStoreError as exc:return exc.code
            with ThreadPoolExecutor(max_workers=2) as pool:
                a=pool.submit(execute,outcome.execute_prepared_local_deck_application,op1,self.authority)
                b=pool.submit(execute,outcome.execute_prepared_local_deck_application_v2,op2,second)
                self.assertCountEqual([a.result(15),b.result(15)],["committed","revision_mismatch"])
            observed=[outcome.recover_local_deck_application(one,store_path=self.path),self.recover(two)]
            self.assertCountEqual([item["status"] for item in observed],["committed","prepared"])
            self.assertEqual(sum(item["receipt"] is not None for item in observed),1)
            self.assertEqual(store.read_destination_state(self.path,state["record_id"])["revision"],2)

    def test_v2_recovery_not_found_and_prepared_perform_no_logical_write(self):
        original=sqlite3.connect; statements=[]
        def connected(*args,**kwargs):
            con=original(*args,**kwargs);con.set_trace_callback(statements.append);return con
        with patch.object(store.sqlite3,"connect",side_effect=connected), patch.object(store,"_replace_in_transaction",side_effect=AssertionError("mutation")):
            self.assertEqual(self.recover()["status"],"not_found")
        self.assertFalse(any(sql.lstrip().split()[0].upper() in ("INSERT","UPDATE","DELETE") for sql in statements))
        self.prepare();statements.clear()
        with patch.object(store.sqlite3,"connect",side_effect=connected), patch.object(outcome,"execute_prepared_local_deck_application_v2",side_effect=AssertionError("retry")):
            self.assertEqual(self.recover()["status"],"prepared")
        self.assertFalse(any(sql.lstrip().split()[0].upper() in ("INSERT","UPDATE","DELETE") for sql in statements))
