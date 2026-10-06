"""#6T scoped non-durable composition and preserved durable-v1 isolation."""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import sqlite3
import unittest
from unittest.mock import patch

from mtgadb import canonical
from mtgadb import managed_deck_store as store
from mtgadb.model import Collection
from mtgadb.modes import OperatingMode
from services import pre_execution_revalidation as revalidation
from services import local_deck_application_intent as intents
from services import local_deck_application as application
from services import local_deck_application_outcome as outcome
from services.local_execution_validation import LocalDeckApplicationError
from services.scoped_human_proposal_decision import build_human_proposal_decision_v2, require_human_proposal_decision_v2
from services.proposal_presentation import build_proposal_presentation_v2, require_proposal_presentation_v2
from services.intelligence import analyze_deck
from services.candidates import discover_candidates
from services.candidate_facts import derive_candidate_facts
from services.candidate_comparison import build_candidate_comparisons
from services.strategic_fit import build_strategic_fit_signals
from services.candidate_ordering import build_candidate_ordering
from services.recommendation import build_recommendation_decisions
from services.recommendation_context import build_recommendation_context
from services.preference_policy import build_preference_policy
from services.proposal_policy import build_proposal_policy_v3
from services.proposal import build_scoped_proposal
from tests import test_local_deck_application as legacy_tests
from tests.test_human_proposal_decision import decision_spec
from tests.test_preference_policy import policy as preference_spec, rule
from tests.test_proposal_policy import policy as proposal_spec


class ScopedExecutionCompositionTests(unittest.TestCase):
    def setUp(self):
        # A real v1 chain is built first with the unchanged legacy fixture.
        self.legacy = legacy_tests.LocalDeckApplicationTests(methodName="runTest")
        self.legacy.setUp()
        self.addCleanup(self.legacy.doCleanups)
        self.path = self.legacy.path
        self.state = self.legacy.state
        self.authority = self.legacy.fx.authority
        self.upstream = self.legacy.upstream
        # Publish a new temporary card projection, not a repository database edit.
        # Gameplay baseline remains identical. Historical D1 remains valid.
        with closing(sqlite3.connect(self.legacy.fx.cards)) as con, con:
            con.execute("UPDATE cards SET types='Sorcery' WHERE title_id IN (1,3)")
            con.execute("UPDATE cards SET types='Enchantment',rules_text='Whenever you gain life, draw a card.' WHERE title_id=2")
            con.execute("UPDATE cards SET cmc=title_id")
            canonical.load_formats(con, {"Test": self.upstream.format})
        self.legacy.fx.publish()
        with closing(sqlite3.connect(self.legacy.fx.cards)) as con:
            con.row_factory = sqlite3.Row
            analysis = analyze_deck(self.state["deck"], con)
            pools = discover_candidates(analysis, self.state["deck"], con, format_name=self.upstream.format.name)
            facts = derive_candidate_facts(pools, con, source_analysis=analysis)
            comparison = build_candidate_comparisons(facts)
            fit = build_strategic_fit_signals(comparison)
            ordering = build_candidate_ordering(comparison, fit, build_preference_policy(preference_spec([rule()])))
            context = build_recommendation_context(build_recommendation_decisions(ordering), comparison)
            proposal = build_scoped_proposal(context, build_proposal_policy_v3(proposal_spec()),
                self.state["deck"], con, format=self.upstream.format, rules=self.upstream.rules)
            self.assertEqual((proposal["status"], proposal["reason"]), ("accepted", "validated"), proposal)
            self.presentation = build_proposal_presentation_v2(proposal)
        self.decision = build_human_proposal_decision_v2(self.presentation, decision_spec())
        self.fresh = self.evaluate()
        self.assertEqual(self.fresh["status"], "revalidated")
        self.intent = self.make_intent()

    def evaluate(self, decision=None, deck=None, **overrides):
        with self.authority._guarded() as view:
            args = dict(human_decision=self.decision if decision is None else decision,
                current_baseline_deck=self.state["deck"] if deck is None else deck,
                con=view["con"], format=view["format"], rules=view["rules"],
                mode=view["mode"], collection=view["collection"], inventory=view["inventory"])
            args.update(overrides)
            return revalidation.build_pre_execution_revalidation_v2(**args)

    def make_intent(self, fresh=None, state=None):
        state = self.state if state is None else state
        request = deepcopy(self.legacy.intent["application_request"])
        request["selected_destination"] = {key: deepcopy(state[key]) for key in request["selected_destination"]}
        return intents.build_local_deck_application_intent_v2(self.fresh if fresh is None else fresh, state, request)

    def apply(self, intent=None):
        return application.apply_local_deck_application_v2(self.intent if intent is None else intent,
            store_path=self.path, validation_authority=self.authority)

    def read(self):
        return store.read_destination_state(self.path, self.state["record_id"])

    def assert_error(self, code, action):
        with self.assertRaises((store.ManagedDeckStoreError, LocalDeckApplicationError)) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def test_scoped_approved_non_durable_end_to_end(self):
        frozen = deepcopy(self.intent)
        result = self.apply()
        current = self.read()
        self.assertEqual(current["revision"], self.state["revision"] + 1)
        self.assertEqual(current["metadata"], self.state["metadata"])
        self.assertEqual(current["gameplay_snapshot_identity"], self.intent["expected_result_deck_identity"])
        self.assertEqual(result["local_deck_application_result_model_version"], "2")
        self.assertEqual(application.require_local_deck_application_result_v2(json.loads(json.dumps(result))), result)
        self.assertEqual(self.intent, frozen)
        self.assertEqual(sum(result["execution_pre_execution_revalidation"]["fresh_validation"]["wildcard_cost"].values()), 0)

    def test_v2_stale_baseline_returns_typed_mismatch(self):
        changed = deepcopy(self.state["deck"])
        changed.main[301] = 1
        fresh = self.evaluate(deck=changed)
        self.assertEqual((fresh["status"], fresh["reason"]), ("not_ready", "baseline_snapshot_mismatch"))
        self.assertEqual(revalidation.require_pre_execution_revalidation_v2(fresh), fresh)
        self.assertIsNone(fresh["pre_execution_revalidation_identity"])
        with self.assertRaises(ValueError): self.make_intent(fresh=fresh)

    def test_v2_declined_refuses_before_current_state_checks(self):
        decision = build_human_proposal_decision_v2(self.presentation, decision_spec("declined"))
        with patch.object(revalidation, "build_deck_snapshot_identity", side_effect=AssertionError("baseline")), patch.object(revalidation, "validate_deck", side_effect=AssertionError("validator")):
            fresh = revalidation.build_pre_execution_revalidation_v2(decision, None, None,
                format=None, rules=None, mode=None)
        self.assertEqual((fresh["status"], fresh["reason"]), ("not_ready", "decision_declined"))
        self.assertEqual(revalidation.require_pre_execution_revalidation_v2(fresh), fresh)
        with self.assertRaises(ValueError): self.make_intent(fresh=fresh)

    def test_v2_unhashable_and_malformed_versions_return_typed_rejection(self):
        for value in ([], {}, None, True, 2, "3", float("nan")):
            with self.subTest(value=value):
                bad = deepcopy(self.decision)
                bad["human_proposal_decision_model_version"] = value
                fresh = self.evaluate(decision=bad)
                self.assertEqual(fresh["status"], "rejected")
                self.assertIn(fresh["reason"], ("unsupported_version", "malformed_input"))
                self.assertEqual(revalidation.require_pre_execution_revalidation_v2(fresh), fresh)
                with self.assertRaises(ValueError): self.make_intent(fresh=fresh)

    def test_v2_stale_destination_before_application_fails_closed(self):
        self.legacy.apply()
        before = store.serialize_destination_state(self.read())
        self.assert_error("revision_mismatch", self.apply)
        self.assertEqual(store.serialize_destination_state(self.read()), before)

    def test_v2_execution_revalidates_inside_guard_and_writer_transaction(self):
        original = application.build_pre_execution_revalidation_v2
        current_check = store._current_for_mutation
        events = []
        def checked(con, expected):
            events.append("current")
            return current_check(con, expected)
        def rechecked(*args, **kwargs):
            self.assertEqual(events, ["current"])
            # The current thread holds the authority lock and store writer lock.
            self.assertFalse(self.authority._lock.acquire(blocking=False))
            with closing(sqlite3.connect(self.path, timeout=0)) as competing:
                with self.assertRaises(sqlite3.OperationalError): competing.execute("BEGIN IMMEDIATE")
            events.append("revalidation2")
            return original(*args, **kwargs)
        replace = store._replace_in_transaction
        def replaced(*args):
            self.assertEqual(events, ["current", "revalidation2"])
            events.append("replace")
            return replace(*args)
        with patch.object(store, "_current_for_mutation", side_effect=checked), patch.object(application, "build_pre_execution_revalidation_v2", side_effect=rechecked), patch.object(application, "build_pre_execution_revalidation", side_effect=AssertionError("v1 builder")), patch.object(store, "_replace_in_transaction", side_effect=replaced):
            self.apply()
        self.assertEqual(events, ["current", "revalidation2", "replace"])

    def test_v2_validation_and_result_failure_roll_back_mutation(self):
        before = store.serialize_destination_state(self.read())
        with patch.object(application, "build_pre_execution_revalidation_v2", side_effect=ValueError("unavailable")):
            self.assert_error("validation_unavailable", self.apply)
        self.assertEqual(store.serialize_destination_state(self.read()), before)
        with patch.object(application, "require_local_deck_application_result_v2", side_effect=ValueError("bad result")):
            self.assert_error("result_mismatch", self.apply)
        self.assertEqual(store.serialize_destination_state(self.read()), before)

    def test_v2_same_intent_replay_fails_after_application(self):
        self.apply()
        before = store.serialize_destination_state(self.read())
        self.assert_error("revision_mismatch", self.apply)
        self.assertEqual(store.serialize_destination_state(self.read()), before)

    def test_v2_same_approval_separate_matching_destination_can_apply(self):
        other = store.create_managed_deck(self.path, self.state["deck"], "Other", "Test")
        other_intent = self.make_intent(state=other)
        self.apply()
        result = self.apply(other_intent)
        self.assertEqual(result["resulting_deck_identity"], self.intent["expected_result_deck_identity"])
        self.assertEqual(require_human_proposal_decision_v2(self.decision), self.decision)

    def test_v2_evidence_tampering_invalidates_non_durable_chain(self):
        result = self.apply()
        evidence_path = ("source_human_proposal_decision", "source_presentation", "review_artifact", "evidence_context")
        def corrupt(value, path):
            for key in path: value = value[key]
            value["candidate_title_id"] += 1
        cases = [(self.presentation, require_proposal_presentation_v2, ("review_artifact", "evidence_context")),
            (self.decision, require_human_proposal_decision_v2, evidence_path[1:]),
            (self.fresh, revalidation.require_pre_execution_revalidation_v2, evidence_path),
            (self.intent, intents.require_local_deck_application_intent_v2, ("source_pre_execution_revalidation",) + evidence_path),
            (result, application.require_local_deck_application_result_v2, ("source_local_deck_application_intent", "source_pre_execution_revalidation") + evidence_path)]
        for value, verifier, path in cases:
            bad = deepcopy(value); corrupt(bad, path)
            with self.assertRaises(ValueError): verifier(bad)
        bad = deepcopy(self.intent); corrupt(bad, cases[3][2])
        with patch.object(store, "_connection", side_effect=AssertionError("IO")):
            self.assert_error("invalid_intent", lambda: self.apply(bad))

    def test_v1_v2_non_durable_authority_crossing_fails_closed(self):
        for verifier, value in ((revalidation.require_pre_execution_revalidation, self.fresh),
            (intents.require_local_deck_application_intent, self.intent),
            (revalidation.require_pre_execution_revalidation_v2, self.legacy.intent["source_pre_execution_revalidation"]),
            (intents.require_local_deck_application_intent_v2, self.legacy.intent)):
            with self.assertRaises(ValueError): verifier(value)
        self.assertEqual(self.evaluate(decision=self.upstream.decision)["reason"], "unsupported_version")
        with patch.object(store, "_connection", side_effect=AssertionError("IO")):
            self.assert_error("invalid_intent", lambda: self.apply(self.legacy.intent))
            self.assert_error("invalid_intent", lambda: self.legacy.apply(intent=self.intent))
        result = self.apply()
        with self.assertRaises(ValueError): application.require_local_deck_application_result(result)
        bad = deepcopy(self.intent); bad["local_deck_application_intent_model_version"] = "1"
        with self.assertRaises(ValueError): intents.require_local_deck_application_intent_v2(bad)

    def test_v2_single_approval_cannot_aggregate_deltas(self):
        for action in ([self.intent["derived_action"]] * 2,
                       {**self.intent["derived_action"], "quantity": 2},
                       {**self.intent["derived_action"], "operation": "remove"}):
            bad = deepcopy(self.intent); bad["derived_action"] = action
            with self.assertRaises(ValueError): intents.require_local_deck_application_intent_v2(bad)
            self.assert_error("invalid_intent", lambda: self.apply(bad))
        bad = deepcopy(self.intent); bad["neighboring_proposals"] = [self.decision]
        with self.assertRaises(ValueError): intents.require_local_deck_application_intent_v2(bad)
        self.assertEqual(self.read()["revision"], 1)

    def test_v2_models_are_bound_in_canonical_identities(self):
        result = self.apply()
        for value, model, identity, verifier in (
            (self.fresh, "pre_execution_revalidation_model_version", "pre_execution_revalidation_identity", revalidation.require_pre_execution_revalidation_v2),
            (self.intent, "local_deck_application_intent_model_version", "local_deck_application_intent_identity", intents.require_local_deck_application_intent_v2),
            (result, "local_deck_application_result_model_version", "local_deck_application_result_identity", application.require_local_deck_application_result_v2)):
            self.assertEqual(value[identity]["canonical_payload"][model], "2")
            bad = deepcopy(value); bad[model] = "1"
            with self.assertRaises(ValueError): verifier(bad)

    def test_v2_preserves_historical_evidence_without_analysis_rerun(self):
        with patch("services.intelligence.analyze_deck", side_effect=AssertionError("analysis")), patch("services.candidates.discover_candidates", side_effect=AssertionError("discovery")):
            result = self.apply()
        actual = result["execution_pre_execution_revalidation"]["source_human_proposal_decision"]
        self.assertEqual(actual, self.decision)
        self.assertEqual(actual["source_presentation"]["review_artifact"]["evidence_context"], self.presentation["review_artifact"]["evidence_context"])

    def test_v2_limitations_disclose_destination_consumption_and_non_durable_scope(self):
        result = self.apply()
        for value in (self.intent, result):
            text = " ".join(value["limitations"])
            for phrase in ("not destination-bound", "selects one destination", "not globally consumed", "not globally single-use", "revision/baseline checks", "another matching destination", "non-durable acknowledgment", "not a durable receipt", "resolved or improved"):
                self.assertIn(phrase, text)
        self.assertIn("Current legacy human-decision, revalidation, and execution authorities reject this scoped model.", self.decision["limitations"])

    def test_v2_is_rejected_by_all_durable_v1_boundaries(self):
        store.migrate_store_v1_to_v2(self.path)
        operation = outcome.prepare_local_deck_application(self.legacy.intent, store_path=self.path)
        bad_operation = deepcopy(operation); bad_operation["source_intent"] = deepcopy(self.intent)
        with patch.object(store, "_connection", side_effect=AssertionError("IO")):
            for action in (lambda: outcome.prepare_local_deck_application(self.intent, store_path=self.path),
                           lambda: outcome.recover_local_deck_application(self.intent, store_path=self.path)):
                self.assert_error("invalid_intent", action)
            with self.assertRaises(outcome.LocalDeckApplicationOutcomeError) as caught:
                outcome.execute_prepared_local_deck_application(bad_operation, store_path=self.path, validation_authority=self.authority)
            self.assertEqual(caught.exception.code, "invalid_operation")
        receipt = outcome.execute_prepared_local_deck_application(operation, store_path=self.path, validation_authority=self.authority)
        bad_receipt = deepcopy(receipt)
        bad_receipt["application_result"]["source_local_deck_application_intent"] = self.intent
        with self.assertRaises(ValueError): outcome.require_local_deck_application_receipt(bad_receipt)
        self.assertEqual(outcome.recover_local_deck_application(self.legacy.intent, store_path=self.path)["status"], "committed")

    def test_v2_application_invalidates_pending_v1_prepared_operation(self):
        store.migrate_store_v1_to_v2(self.path)
        # Required order: prepare D1 first, apply D2 second, attempt D1 third.
        operation = outcome.prepare_local_deck_application(self.legacy.intent, store_path=self.path)
        self.assertEqual(outcome.recover_local_deck_application(self.legacy.intent, store_path=self.path)["status"], "prepared")
        result = self.apply()
        self.assertEqual(result["status"], "applied")
        before = store.serialize_destination_state(self.read())
        self.assert_error("revision_mismatch", lambda: outcome.execute_prepared_local_deck_application(
            operation, store_path=self.path, validation_authority=self.authority))
        self.assertEqual(store.serialize_destination_state(self.read()), before)
        recovered = outcome.recover_local_deck_application(self.legacy.intent, store_path=self.path)
        self.assertEqual((recovered["status"], recovered["reason"]), ("prepared", "no_committed_receipt_observed"))
        self.assertIsNone(recovered["receipt"])
        self.assertEqual(recovered["operation"], operation)
        with closing(sqlite3.connect(self.path)) as con:
            self.assertEqual(con.execute("SELECT state,receipt_json FROM managed_application_operations").fetchall(), [("prepared", None)])

    def test_v2_approval_cannot_retarget_changed_gameplay(self):
        changed = deepcopy(self.state["deck"]); changed.main[301] = 1
        other = store.create_managed_deck(self.path, changed, "Changed", "Test")
        with self.assertRaises(ValueError): self.make_intent(state=other)
        bad = deepcopy(self.intent)
        bad["source_destination_state"] = store.serialize_destination_state(other)
        with self.assertRaises(ValueError): intents.require_local_deck_application_intent_v2(bad)
        self.assert_error("invalid_intent", lambda: self.apply(bad))
        bad_decision = deepcopy(self.decision)
        bad_decision["proposal_identity"]["canonical_payload"]["source_baseline_deck_identity"] = other["gameplay_snapshot_identity"]
        self.assertEqual(self.evaluate(decision=bad_decision, deck=changed)["status"], "rejected")
        self.assertEqual(self.decision, require_human_proposal_decision_v2(self.decision))

    def test_v2_intent_binds_explicit_destination_without_consuming_approval(self):
        frozen = deepcopy(self.decision)
        request = deepcopy(self.intent["application_request"])
        request["selected_destination"]["record_id"] = "wrong"
        with self.assertRaises(ValueError):
            intents.build_local_deck_application_intent_v2(self.fresh, self.state, request)
        self.apply()
        self.assertEqual(self.decision, frozen)
        self.assertEqual(set(self.intent), set(self.legacy.intent))

    def test_v2_declined_cannot_build_intent(self):
        decision = build_human_proposal_decision_v2(self.presentation, decision_spec("declined"))
        fresh = self.evaluate(decision=decision)
        with self.assertRaises(ValueError): self.make_intent(fresh=fresh)
        bad = deepcopy(self.intent); bad["source_pre_execution_revalidation"] = fresh
        with patch.object(store, "_connection", side_effect=AssertionError("IO")):
            self.assert_error("invalid_intent", lambda: self.apply(bad))

    def test_v2_current_resource_loss_fails_without_mutation(self):
        before = store.serialize_destination_state(self.read())
        self.legacy.fx.publish(collection=Collection({201: 1, 301: 1}))
        self.assert_error("fresh_validation_failed", self.apply)
        self.assertEqual(store.serialize_destination_state(self.read()), before)

    def test_v2_wildcard_no_spend_and_unlimited_intent_refusal(self):
        self.legacy.fx.publish(mode=OperatingMode.WILDCARD_BUDGET, inventory=self.upstream.inventory)
        fresh = self.evaluate()
        self.assertEqual(fresh["resource_assessment"]["resource_status"], "no_spend_required")
        result = self.apply(self.make_intent(fresh=fresh))
        self.assertEqual(result["execution_pre_execution_revalidation"]["resource_assessment"]["wildcard_cost"], fresh["resource_assessment"]["wildcard_cost"])
        unlimited = self.evaluate(mode=OperatingMode.UNLIMITED)
        with self.assertRaises(ValueError): self.make_intent(fresh=unlimited)

    def test_v2_rehashed_model_substitution_and_nested_crossing_rejected(self):
        cases = [(self.fresh, "pre_execution_revalidation_model_version", "pre_execution_revalidation_identity", revalidation.require_pre_execution_revalidation_v2),
                 (self.intent, "local_deck_application_intent_model_version", "local_deck_application_intent_identity", intents.require_local_deck_application_intent_v2)]
        for value, model, identity, verifier in cases:
            bad = deepcopy(value)
            bad[model] = bad[identity]["canonical_payload"][model] = "1"
            payload = json.dumps(bad[identity]["canonical_payload"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            bad[identity]["digest"] = hashlib.sha256(payload).hexdigest()
            with self.assertRaises(ValueError): verifier(bad)
        bad = deepcopy(self.fresh)
        bad["source_human_proposal_decision"] = self.upstream.decision
        with self.assertRaises(ValueError): revalidation.require_pre_execution_revalidation_v2(bad)

    def test_v2_destination_identity_generation_and_metadata_mismatches(self):
        before = store.serialize_destination_state(self.read())
        for field, code in (("store_id", "store_identity_mismatch"),
                            ("store_generation", "store_generation_mismatch"),
                            ("revision", "revision_mismatch"),
                            ("metadata", "metadata_mismatch")):
            with self.subTest(field=field):
                from uuid import uuid4
                native = deepcopy(self.state)
                if field in ("store_id", "store_generation"): native[field] = str(uuid4())
                elif field == "revision": native[field] += 1
                else:
                    native[field]["name"] = "Different metadata"
                    native["deck"] = replace(native["deck"], name="Different metadata")
                intent = self.make_intent(state=native)
                self.assert_error(code, lambda: self.apply(intent))
                self.assertEqual(store.serialize_destination_state(self.read()), before)
