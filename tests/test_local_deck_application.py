from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import sqlite3
from threading import Barrier, Event
import unittest
from unittest.mock import patch
from uuid import uuid4

from mtgadb import managed_deck_store as store
from mtgadb.model import Collection, Deck
from mtgadb.modes import OperatingMode
from services import local_deck_application as application
from services.local_deck_application_intent import build_local_deck_application_intent
from services.local_execution_validation import LocalDeckApplicationError
from services.local_execution_validation import LocalExecutionValidationAuthorityV1
from mtgadb.deck_identity import build_deck_snapshot_identity
from services.human_proposal_decision import build_human_proposal_decision
from services.proposal_presentation import build_proposal_presentation
from services.validator import ValidationIssue, ValidationReport
from tests.test_human_proposal_decision import decision_spec
from tests.test_local_execution_validation import AuthorityFixture


SERVICE = "services.local_deck_application."
ID = "local_deck_application_result_identity"


class LocalDeckApplicationTests(unittest.TestCase):
    def setUp(self):
        self.fx = AuthorityFixture(self)
        self.upstream = self.fx.upstream
        self.path = self.fx.root / "managed.db"
        store.initialize_store(self.path)
        self.state = store.create_managed_deck(self.path, self.upstream.deck, "Selected", "Test")
        self.intent = self.make_intent()

    def make_intent(self, *, state=None, mode=OperatingMode.FULL_COLLECTION, target="main"):
        state = self.state if state is None else state
        proposal = self.upstream.fixture.accepted(target=target, deck=state["deck"])
        decision = build_human_proposal_decision(build_proposal_presentation(proposal), decision_spec())
        fresh = self.upstream.evaluate(human_decision=decision, current_baseline_deck=state["deck"],
                                      mode=mode, collection=self.upstream.owned,
                                      inventory=self.upstream.inventory if mode is OperatingMode.WILDCARD_BUDGET else None)
        request = {"request": "apply", "request_source": {"kind": "explicit_user",
                    "provenance": {"reference": "apply explicitly"}}, "selected_destination": {
                        key: deepcopy(state[key]) for key in ("store_id", "store_generation", "record_id", "revision", "gameplay_snapshot_identity")}}
        return build_local_deck_application_intent(fresh, state, request)

    def apply(self, **overrides):
        args = dict(intent=self.intent, store_path=self.path, validation_authority=self.fx.authority)
        args.update(overrides)
        return application.apply_local_deck_application(**args)

    def read(self):
        return store.read_destination_state(self.path, self.state["record_id"])

    def error(self, code, action=None):
        with self.assertRaises((store.ManagedDeckStoreError, LocalDeckApplicationError)) as caught:
            (self.apply if action is None else action)()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def sql(self, sql, args=()):
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute(sql, args)

    def rehash(self, result):
        payload = {k: deepcopy(v) for k, v in result.items() if k != ID}
        result[ID]["canonical_payload"] = payload
        result[ID]["digest"] = hashlib.sha256(application._encoded(payload)).hexdigest()

    def test_exact_success_and_historical_result_verification(self):
        before = deepcopy(self.intent)
        result = self.apply()
        after = self.read()
        self.assertEqual(result["status"], "applied")
        self.assertEqual(after["revision"], 2)
        self.assertEqual(after["metadata"], self.state["metadata"])
        self.assertEqual(after["record_id"], self.state["record_id"])
        self.assertEqual(after["deck"].main, {201: 1, 101: 1})
        self.assertEqual(after["deck"].sideboard, {301: 1})
        self.assertEqual(after["gameplay_snapshot_identity"], self.intent["expected_result_deck_identity"])
        self.assertEqual(application.require_local_deck_application_result(result), result)
        self.assertEqual(application.require_local_deck_application_result(json.loads(json.dumps(result))), result)
        self.assertEqual(self.intent, before)
        self.assertNotIn(str(self.path), json.dumps(result))

    def test_wildcard_zero_cost_success(self):
        self.intent = self.make_intent(mode=OperatingMode.WILDCARD_BUDGET)
        self.fx.publish(mode=OperatingMode.WILDCARD_BUDGET, inventory=self.upstream.inventory)
        result = self.apply()
        self.assertEqual(result["execution_pre_execution_revalidation"]["resource_assessment"]["resource_status"], "no_spend_required")

    def test_each_zone_and_nullable_metadata(self):
        for zone in ("main", "sideboard", "commander"):
            with self.subTest(zone=zone):
                state = store.create_managed_deck(self.path, self.upstream.deck, "Unicode Ω", None)
                intent = self.make_intent(state=state, target=zone)
                result = self.apply(intent=intent)
                after = store.require_destination_state(result["resulting_destination_state"])
                self.assertEqual(getattr(after["deck"], zone)[101], 1)
                self.assertEqual(after["metadata"], {"name": "Unicode Ω", "format_label": None})

    def test_existing_printing_adds_exactly_one(self):
        self.upstream.owned.cards[101] = 2
        state = store.create_managed_deck(self.path, Deck(main={101: 1, 201: 1}, sideboard={301: 1}), "Existing")
        self.fx.publish()
        result = self.apply(intent=self.make_intent(state=state))
        self.assertEqual(dict(result["resulting_destination_state"]["deck"]["main"])[101], 2)

    def test_invalid_intent_before_any_io_or_authority_access(self):
        for field, replacement in (("local_deck_application_intent_model_version", "2"),
                                   ("derived_action", {}), ("extra", True)):
            intent = deepcopy(self.intent)
            intent[field] = replacement
            with patch("sqlite3.connect", side_effect=AssertionError("IO")), \
                 patch.object(type(self.fx.authority), "_guarded", side_effect=AssertionError("guard")):
                self.error("invalid_intent", lambda: self.apply(intent=intent))

    def test_no_action_or_replacement_overrides_and_fake_authority(self):
        for key in ("replacement_deck", "quantity", "arena_id", "zone", "name", "format_label", "cost", "valid"):
            with self.assertRaises(TypeError):
                self.apply(**{key: None})
        self.error("invalid_execution_context", lambda: self.apply(validation_authority=object()))
        self.assertEqual(self.read(), self.state)

    def test_closed_authority_rejected(self):
        self.fx.authority.close()
        self.error("execution_context_unavailable")

    def test_policy_change_fails_closed(self):
        for args in ({"rules": replace(self.upstream.rules, max_main=249)},
                     {"format": replace(self.upstream.format, banned_title_ids=frozenset({1}))},
                     {"mode": OperatingMode.WILDCARD_BUDGET, "inventory": self.upstream.inventory}):
            self.fx.publish(**args)
            self.error("execution_context_conflict")
            self.assertEqual(self.read(), self.state)

    def test_historical_ownership_does_not_authorize_current_execution(self):
        self.fx.publish(collection=Collection({201: 1, 301: 1}))
        self.error("fresh_validation_failed")
        self.assertEqual(self.read(), self.state)

    def test_affordable_and_unaffordable_positive_costs_rejected(self):
        self.intent = self.make_intent(mode=OperatingMode.WILDCARD_BUDGET)
        for inventory in (self.upstream.inventory, replace(self.upstream.inventory, wildcards={})):
            self.fx.publish(mode=OperatingMode.WILDCARD_BUDGET, collection=Collection({}), inventory=inventory)
            self.error("current_spend_required")
            self.assertEqual(self.read(), self.state)

    def test_whole_deck_ownership_checked_not_just_added_printing(self):
        self.fx.publish(collection=Collection({101: 1}))
        self.error("fresh_validation_failed")

    def test_cross_zone_quantities_require_two_owned_copies(self):
        self.upstream.owned.cards[101] = 2
        state = store.create_managed_deck(self.path, Deck(main={201: 1}, sideboard={101: 1}), "Cross zone")
        intent = self.make_intent(state=state)
        self.fx.publish(collection=Collection({101: 1, 201: 1}))
        self.error("fresh_validation_failed", lambda: self.apply(intent=intent))

    def test_current_card_disappears_or_title_mapping_changes(self):
        for sql in ("UPDATE printings SET title_id=2 WHERE arena_id=101", "DELETE FROM printings WHERE arena_id=101"):
            with closing(sqlite3.connect(self.fx.cards)) as con, con:
                con.execute(sql)
            self.fx.publish()
            self.error("fresh_validation_failed")
            self.assertEqual(self.read(), self.state)

    def test_fresh_card_legality_and_color_change(self):
        with closing(sqlite3.connect(self.fx.cards)) as con, con:
            con.execute("UPDATE printings SET set_code='ZZZ' WHERE title_id=1")
        self.fx.publish()
        self.error("fresh_validation_failed")

    def test_validator_invoked_under_guard_and_write_transaction(self):
        original = application.build_pre_execution_revalidation
        calls = []
        def check(*args, **kwargs):
            self.assertTrue(self.fx.authority._lock.locked())
            con = sqlite3.connect(self.path, timeout=0, isolation_level=None)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    con.execute("BEGIN IMMEDIATE")
            finally:
                con.close()
            calls.append(True)
            return original(*args, **kwargs)
        with patch(SERVICE + "build_pre_execution_revalidation", side_effect=check):
            self.apply()
        self.assertEqual(calls, [True])

    def test_bad_validator_cost_types_rejected(self):
        for cost in ({"common": True}, {"common": -1}, {"common": "0"}, {"common": 0.0}):
            with patch("services.pre_execution_revalidation.validate_deck", return_value=ValidationReport(wildcard_cost=cost)):
                self.error("validation_unavailable")
            self.assertEqual(self.read(), self.state)

    def test_positive_full_collection_cost_rejected(self):
        with patch("services.pre_execution_revalidation.validate_deck", return_value=ValidationReport(wildcard_cost={"common": 1})):
            self.error("current_spend_required")

    def test_validation_unavailable_and_warnings(self):
        with patch("services.pre_execution_revalidation.validate_deck", side_effect=ValueError("private deck content")):
            self.error("validation_unavailable")
        with patch("services.pre_execution_revalidation.validate_deck", return_value=ValidationReport(warnings=(ValidationIssue("notice", "Review"),))):
            result = self.apply()
        self.assertEqual(result["execution_pre_execution_revalidation"]["fresh_validation"]["warnings"][0]["code"], "notice")

    def test_wrong_store_generation_schema_and_missing_path(self):
        self.error("missing_store", lambda: self.apply(store_path=self.fx.root / "absent.db"))
        self.assertFalse((self.fx.root / "absent.db").exists())
        other = self.fx.root / "other.db"
        store.initialize_store(other)
        self.error("store_identity_mismatch", lambda: self.apply(store_path=other))
        self.sql("UPDATE managed_store_meta SET store_generation=?", (str(uuid4()),))
        self.error("store_generation_mismatch")
        self.sql("UPDATE managed_store_meta SET schema_version='999'")
        self.error("unsupported_schema")

    def test_same_contents_other_record_untouched(self):
        other = store.create_managed_deck(self.path, self.state["deck"], "Selected", "Test")
        self.apply()
        self.assertEqual(store.read_destination_state(self.path, other["record_id"]), other)

    def test_metadata_only_revision_and_direct_metadata_corruption(self):
        store.conditional_replace(self.path, self.state["record_id"], self.state, self.state["deck"], name="New", format_label="Test")
        self.error("revision_mismatch")
        self.sql("UPDATE managed_decks SET revision=1")
        self.error("metadata_mismatch")

    def test_baseline_changed_without_revision(self):
        self.sql("UPDATE managed_deck_cards SET quantity=2")
        self.error("baseline_mismatch")

    def test_deleted_and_missing(self):
        store.conditional_delete(self.path, self.state["record_id"], self.state)
        self.error("deleted_record")
        self.sql("DELETE FROM managed_deck_cards")
        self.sql("DELETE FROM managed_decks")
        self.error("missing_record")

    def test_revision_exhaustion(self):
        self.sql("UPDATE managed_decks SET revision=?", (store.MAX_INTEGER,))
        current = self.read()
        self.intent = self.make_intent(state=current)
        self.error("revision_exhausted")
        self.assertEqual(self.read(), current)

    def test_replay_fails_no_already_applied_inference(self):
        self.apply()
        self.error("revision_mismatch")
        self.assertEqual(self.read()["deck"].main[101], 1)

    def test_changed_then_restored_still_stale(self):
        changed = store.conditional_replace(self.path, self.state["record_id"], self.state, Deck(main={201: 2}), name="Selected", format_label="Test")["destination_state"]
        store.conditional_replace(self.path, changed["record_id"], changed, self.state["deck"], name="Selected", format_label="Test")
        self.error("revision_mismatch")

    def test_no_nested_public_mutation_or_excluded_side_effects(self):
        with ExitStack() as stack:
            for name in ("mtgadb.managed_deck_store.conditional_replace", "mtgadb.managed_deck_store.conditional_delete",
                         "mtgadb.managed_deck_store.create_managed_deck", "services.exporter.export_arena_deck",
                         "services.exporter.import_arena_deck", "socket.socket"):
                stack.enter_context(patch(name, side_effect=AssertionError(name)))
            self.apply()

    def test_same_intent_concurrency_delete_and_wal(self):
        for journal in ("DELETE", "WAL"):
            self.sql("PRAGMA journal_mode=" + journal)
            state = store.create_managed_deck(self.path, self.upstream.deck, "Concurrent")
            intent = self.make_intent(state=state)
            gate = Barrier(2)
            def writer():
                gate.wait(3)
                try:
                    return self.apply(intent=intent)["status"]
                except store.ManagedDeckStoreError as exc:
                    return exc.code
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(writer) for _ in range(2)]
                outcomes = [f.result(10) for f in futures]
            self.assertCountEqual(outcomes, ["applied", "revision_mismatch"])
            after = store.read_destination_state(self.path, state["record_id"])
            self.assertEqual(after["revision"], 2)
            self.assertEqual(after["deck"].main[101], 1)

    def test_busy_transaction_no_change(self):
        original = sqlite3.connect
        holder = original(self.path, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        try:
            with patch.object(store.sqlite3, "connect", side_effect=lambda *a, **kw: original(*a, timeout=0, **kw)):
                self.error("storage_busy")
        finally:
            holder.rollback()
            holder.close()
        self.assertEqual(self.read(), self.state)

    def test_partial_insert_and_result_failure_rollback(self):
        original = sqlite3.connect
        class Partial(sqlite3.Connection):
            def executemany(self, sql, rows):
                rows = list(rows)
                super().execute(sql, rows[0])
                raise sqlite3.OperationalError("private contents")
        with patch.object(store.sqlite3, "connect", side_effect=lambda *a, **kw: original(*a, factory=Partial, **kw)):
            self.error("transaction_failed")
        self.assertEqual(self.read(), self.state)
        with patch(SERVICE + "require_local_deck_application_result", side_effect=ValueError("bad result")):
            self.error("result_mismatch")
        self.assertEqual(self.read(), self.state)

    def test_persisted_reread_mismatch_rollback(self):
        original = store._read
        calls = []
        def wrong(*args, **kwargs):
            result = original(*args, **kwargs)
            calls.append(True)
            if len(calls) == 2:
                result["revision"] += 1
            return result
        with patch.object(store, "_read", side_effect=wrong):
            self.error("result_mismatch")
        self.assertEqual(self.read(), self.state)

    def test_commit_failure_and_commit_then_exception_uncertainty(self):
        original = sqlite3.connect
        class Before(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError("before")
        with patch.object(store.sqlite3, "connect", side_effect=lambda *a, **kw: original(*a, factory=Before, **kw)):
            self.error("commit_failed")
        self.assertEqual(self.read(), self.state)
        class After(sqlite3.Connection):
            def commit(self):
                super().commit()
                raise sqlite3.OperationalError("ack lost")
        with patch.object(store.sqlite3, "connect", side_effect=lambda *a, **kw: original(*a, factory=After, **kw)):
            self.error("commit_failed")
        self.assertEqual(self.read()["revision"], 2)
        self.error("revision_mismatch")

    def test_authority_guard_held_through_commit(self):
        original = sqlite3.connect
        started, finished = Event(), Event()
        def publish():
            started.set()
            result = self.fx.publish(collection=Collection({}))
            finished.set()
            return result
        futures = []
        test = self
        with ThreadPoolExecutor(max_workers=1) as pool:
            class Commit(sqlite3.Connection):
                def commit(self):
                    futures.append(pool.submit(publish))
                    test.assertTrue(started.wait(3))
                    test.assertFalse(finished.wait(.05))
                    test.assertTrue(test.fx.authority._lock.locked())
                    return super().commit()
            with patch.object(store.sqlite3, "connect", side_effect=lambda *a, **kw: original(*a, factory=Commit, **kw)):
                result = self.apply()
            self.assertEqual(futures[0].result(5)["generation"], 2)
        self.assertEqual(result["execution_context"]["generation"], 1)

    def test_result_closed_schema_tampering_even_rehashed(self):
        original = self.apply()
        for key, value in (("status", "intent_recorded"), ("new_revision", True), ("new_revision", 3),
                           ("local_deck_application_result_model_version", "2"), ("limitations", []),
                           ("applied_action", {}), ("execution_scope", {}), ("extra", True)):
            result = deepcopy(original)
            result[key] = value
            self.rehash(result)
            with self.assertRaises(ValueError):
                application.require_local_deck_application_result(result)
        for key in original:
            result = deepcopy(original)
            del result[key]
            with self.assertRaises(ValueError):
                application.require_local_deck_application_result(result)

    def test_result_context_types_and_metadata_tampering(self):
        original = self.apply()
        for key, value in (("collection", [[101, True]]), ("collection", [[101, 1], [101, 1]]),
                           ("generation", True), ("inventory", {"wildcards": {}}),
                           ("card_database_identity", {}), ("authority_id", "fake")):
            result = deepcopy(original)
            result["execution_context"][key] = value
            self.rehash(result)
            with self.assertRaises(ValueError):
                application.require_local_deck_application_result(result)
        result = deepcopy(original)
        result["resulting_destination_state"]["metadata"]["format_label"] = None
        self.rehash(result)
        with self.assertRaises(ValueError):
            application.require_local_deck_application_result(result)

    def test_result_detachment_historical_no_io(self):
        result = self.apply()
        with patch("sqlite3.connect", side_effect=AssertionError("IO")), \
             patch("services.pre_execution_revalidation.validate_deck", side_effect=AssertionError("validate")):
            verified = application.require_local_deck_application_result(result)
        verified["execution_context"]["collection"].clear()
        self.assertNotEqual(verified, result)
        store.conditional_delete(self.path, self.state["record_id"], self.read())
        self.assertEqual(application.require_local_deck_application_result(result), result)

    def test_independent_authorities_still_use_destination_cas(self):
        other = LocalExecutionValidationAuthorityV1(**self.fx.args)
        self.addCleanup(other.close)
        for journal in ("DELETE", "WAL"):
            self.sql("PRAGMA journal_mode=" + journal)
            state = store.create_managed_deck(self.path, self.upstream.deck, "Separate authority")
            intent = self.make_intent(state=state)
            gate = Barrier(2)
            def writer(authority):
                gate.wait(3)
                try:
                    return self.apply(intent=intent, validation_authority=authority)["status"]
                except store.ManagedDeckStoreError as exc:
                    return exc.code
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(writer, authority) for authority in (self.fx.authority, other)]
                self.assertCountEqual([f.result(10) for f in futures], ["applied", "revision_mismatch"])
            self.assertEqual(store.read_destination_state(self.path, state["record_id"])["revision"], 2)

    def test_first_writer_failure_allows_second_success(self):
        original = store._replace_in_transaction
        entered, release = Event(), Event()
        calls = []
        def write(*args):
            calls.append(True)
            result = original(*args)
            if len(calls) == 1:
                entered.set()
                self.assertTrue(release.wait(3))
                raise store.ManagedDeckStoreError("result_mismatch")
            return result
        def attempt():
            try:
                return self.apply()["status"]
            except store.ManagedDeckStoreError as exc:
                return exc.code
        with patch.object(store, "_replace_in_transaction", side_effect=write), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(attempt)
            self.assertTrue(entered.wait(3))
            second = pool.submit(attempt)
            release.set()
            self.assertEqual(first.result(10), "result_mismatch")
            self.assertEqual(second.result(10), "applied")
        self.assertEqual(self.read()["revision"], 2)

    def test_concurrent_delete_and_metadata_update_blocked_inside_validation(self):
        original = application.build_pre_execution_revalidation
        entered, release = Event(), Event()
        def validate(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(*args, **kwargs)
        def mutate():
            try:
                store.conditional_delete(self.path, self.state["record_id"], self.state)
                return "deleted"
            except store.ManagedDeckStoreError as exc:
                return exc.code
        with patch(SERVICE + "build_pre_execution_revalidation", side_effect=validate), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.apply)
            self.assertTrue(entered.wait(3))
            second = pool.submit(mutate)
            release.set()
            self.assertEqual(first.result(10)["status"], "applied")
            self.assertEqual(second.result(10), "revision_mismatch")

    def test_cleanup_failure_preserves_closed_original_error(self):
        original = sqlite3.connect
        class BrokenCleanup(sqlite3.Connection):
            def rollback(self):
                super().rollback()
                raise sqlite3.OperationalError("private rollback contents")
            def close(self):
                super().close()
                raise sqlite3.OperationalError("private close contents")
        self.fx.publish(collection=Collection({}))
        with patch.object(store.sqlite3, "connect", side_effect=lambda *a, **kw: original(*a, factory=BrokenCleanup, **kw)):
            self.error("fresh_validation_failed")
        self.assertEqual(self.read(), self.state)

    def test_post_commit_cleanup_failure_reports_uncertainty(self):
        original = sqlite3.connect
        class BrokenClose(sqlite3.Connection):
            def close(self):
                super().close()
                raise sqlite3.OperationalError("private close contents")
        with patch.object(store.sqlite3, "connect", side_effect=lambda *a, **kw: original(*a, factory=BrokenClose, **kw)):
            self.error("commit_failed")
        self.assertEqual(self.read()["revision"], 2)
        self.error("revision_mismatch")

    def test_fresh_result_identity_mismatch_cannot_mutate(self):
        original = application.build_pre_execution_revalidation
        def change(*args, **kwargs):
            value = original(*args, **kwargs)
            value["reconstructed_result_deck_identity"] = build_deck_snapshot_identity(Deck())
            return value
        with patch(SERVICE + "build_pre_execution_revalidation", side_effect=change):
            self.error("validation_unavailable")
        self.assertEqual(self.read(), self.state)
        # Direct reconstruction check exercises the independent executor guard.
        value = deepcopy(self.intent)
        value["expected_result_deck_identity"] = build_deck_snapshot_identity(Deck())
        self.error("expected_result_mismatch", lambda: application._reconstruct(value, self.state))

    def test_reconstruction_signed_integer_limits(self):
        state = deepcopy(self.state)
        state["deck"].main[101] = store.MAX_INTEGER
        self.error("malformed_replacement", lambda: application._reconstruct(self.intent, state))
        state = deepcopy(self.state)
        state["deck"].main[store.MAX_INTEGER + 1] = 1
        self.error("malformed_replacement", lambda: application._reconstruct(self.intent, state))

    def test_basic_land_exemption_uses_fresh_validator(self):
        with closing(sqlite3.connect(self.fx.cards)) as con, con:
            con.execute("UPDATE cards SET name='Plains' WHERE title_id=1")
        self.fx.publish(collection=Collection({201: 1, 301: 1}))
        self.assertEqual(self.apply()["status"], "applied")

    def test_validator_size_copy_color_and_commander_rejections(self):
        # These outcomes are generated inside the executor by the owning
        # validator path; no caller can pass a report to the public API.
        for code in ("main_size", "copy_limit", "color_identity", "commander_not_allowed"):
            with patch("services.pre_execution_revalidation.validate_deck",
                       return_value=ValidationReport(errors=(ValidationIssue(code, "Rejected"),))):
                self.error("fresh_validation_failed")
            self.assertEqual(self.read(), self.state)

    def test_result_digest_version_algorithm_and_python_shapes(self):
        original = self.apply()
        for field, value in (("digest", "0" * 64), ("digest_algorithm", "sha512"),
                             ("local_deck_application_result_identity_version", "2"), ("canonical_payload", {})):
            result = deepcopy(original)
            result[ID][field] = value
            with self.assertRaises(ValueError):
                application.require_local_deck_application_result(result)
        result = deepcopy(original)
        result["limitations"] = tuple(result["limitations"])
        with self.assertRaises(ValueError):
            application.require_local_deck_application_result(result)

    def test_source_update_does_not_mutate_published_authority_and_next_publish_does(self):
        with closing(sqlite3.connect(self.fx.cards)) as con, con:
            con.execute("DELETE FROM printings WHERE arena_id=101")
        result = self.apply()
        self.assertEqual(result["execution_context"]["generation"], 1)
        self.fx.publish()
        state = store.create_managed_deck(self.path, self.upstream.deck, "After publish")
        self.error("fresh_validation_failed", lambda: self.apply(intent=self.make_intent(state=state)))

    def test_exhausted_revision_and_noop_not_success(self):
        original = application._reconstruct
        with patch(SERVICE + "_reconstruct", return_value=self.state["deck"]):
            self.error("result_mismatch")
        self.assertEqual(self.read(), self.state)


if __name__ == "__main__":
    unittest.main()
