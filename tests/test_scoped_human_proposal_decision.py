"""Scoped review acceptance and rejection at unchanged execution authorities."""
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
import unittest
from unittest.mock import patch

from mtgadb.deck_identity import build_deck_snapshot_identity
from mtgadb.model import Deck
from services import scoped_human_proposal_decision as scoped
from services.human_proposal_decision import (
    build_human_proposal_decision, require_human_proposal_decision,
)
from services.proposal_presentation import require_proposal_presentation_v2, _identity
from services.candidate_comparison import _support_context
from services.pre_execution_revalidation import require_pre_execution_revalidation
from services.local_deck_application_intent import require_local_deck_application_intent
from services.local_deck_application import require_local_deck_application_result
from services import local_deck_application_outcome as outcome
from services.local_execution_validation import LocalDeckApplicationError
from tests import test_deck_review as review_tests
from tests import test_pre_execution_revalidation as revalidation_tests
from tests import test_local_deck_application_intent as intent_tests
from tests import test_local_deck_application as application_tests
from tests import test_local_deck_application_outcome as outcome_tests
from tests.test_human_proposal_decision import decision_spec


build = scoped.build_human_proposal_decision_v2
require = scoped.require_human_proposal_decision_v2
PAYLOAD_FIELDS = {
    "human_proposal_decision_model_version", "source_proposal_presentation_model_version",
    "decision", "decision_source", "proposal_identity", "presentation_identity",
}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def rehash_presentation(value):
    value["presentation_identity"] = _identity("presentation_identity_version", "1", {
        "proposal_identity": value["proposal_identity"], "review_artifact": value["review_artifact"],
    })


def substitute_decision(value, decision):
    """Insert a complete actual v2 record at the proper nested legacy field."""
    if isinstance(value, dict):
        if "source_human_proposal_decision" in value:
            value["source_human_proposal_decision"] = deepcopy(decision)
        for item in value.values():
            substitute_decision(item, decision)
    elif isinstance(value, list):
        for item in value:
            substitute_decision(item, decision)


class ScopedHumanProposalDecisionTests(unittest.TestCase):
    def fixture(self, cls):
        value = cls(methodName="runTest")
        value.setUp()
        self.addCleanup(value.doCleanups)
        return value

    def setUp(self):
        self.review = self.fixture(review_tests.DeckReviewTests)
        self.presentation = self.produce()

    def produce(self):
        report = self.review.run_review()
        self.assertEqual(report["run_status"], "completed")
        self.assertEqual(report["artifacts"]["proposal_result_projection"]["status"], "accepted")
        presentation = report["artifacts"]["presentation"]
        self.assertEqual(require_proposal_presentation_v2(presentation), presentation)
        return presentation

    def decision(self, choice="approved", presentation=None):
        return build(self.presentation if presentation is None else presentation, decision_spec(choice))

    def different_scope(self):
        changed = deepcopy(self.presentation)
        evidence = changed["review_artifact"]["evidence_context"]
        evidence["source_need"]["evidence_boundary"]["unsupported_text_card_copies"] += 1
        evidence["source_evidence_completeness"]["source_evidence_boundary"] = deepcopy(
            evidence["source_need"]["evidence_boundary"])
        rehash_presentation(changed)
        self.assertEqual(require_proposal_presentation_v2(changed), changed)
        return changed

    def assert_new_review(self, changed):
        before = self.decision()
        after = self.decision(presentation=changed)
        self.assertNotEqual(before["presentation_identity"], after["presentation_identity"])
        self.assertNotEqual(before["human_proposal_decision_identity"], after["human_proposal_decision_identity"])
        transplanted = deepcopy(before)
        transplanted["source_presentation"] = deepcopy(changed)
        with self.assertRaises(ValueError):
            require(transplanted)
        self.assertEqual(require(after), after)

    def test_actual_scoped_producer_approved_declined_and_exact_shape(self):
        approved, declined = self.decision(), self.decision("declined")
        for record in (approved, declined):
            self.assertEqual(set(record), PAYLOAD_FIELDS | {
                "source_presentation", "human_proposal_decision_identity", "limitations"})
            self.assertEqual(record["human_proposal_decision_model_version"], "2")
            self.assertEqual(record["source_proposal_presentation_model_version"], "2")
            self.assertEqual(require(record), record)
        self.assertNotEqual(approved["human_proposal_decision_identity"], declined["human_proposal_decision_identity"])

    def test_identity_one_exact_envelope_and_six_field_payload(self):
        record = self.decision()
        identity = record["human_proposal_decision_identity"]
        self.assertEqual(set(identity), {"human_proposal_decision_identity_version", "digest_algorithm", "canonical_payload", "digest"})
        self.assertEqual(identity["human_proposal_decision_identity_version"], "1")
        self.assertEqual(identity["digest_algorithm"], "sha256")
        self.assertEqual(set(identity["canonical_payload"]), PAYLOAD_FIELDS)
        self.assertEqual(identity["canonical_payload"], {key: record[key] for key in PAYLOAD_FIELDS})
        self.assertEqual(identity["digest"], hashlib.sha256(encoded(identity["canonical_payload"])).hexdigest())

    def test_deterministic_rebuild_json_round_trip_and_mapping_order(self):
        first = self.decision()
        self.assertEqual(first, self.decision())
        self.assertEqual(require(json.loads(json.dumps(first))), first)
        def reversed_maps(value):
            if type(value) is dict:
                return {key: reversed_maps(item) for key, item in reversed(list(value.items()))}
            if type(value) is list:
                return [reversed_maps(item) for item in value]
            return value
        second = build(reversed_maps(self.presentation), reversed_maps(decision_spec()))
        self.assertEqual(encoded(first), encoded(second))
        self.assertEqual(require(reversed_maps(first)), first)

    def test_explicit_source_normalization_is_identity_bearing(self):
        for kind in ("explicit_user", "explicit_operator"):
            spec = decision_spec(kind=kind, reference="  scoped  review café  ")
            record = build(self.presentation, spec)
            self.assertEqual(record["decision_source"]["provenance"]["reference"], "scoped  review café")
            self.assertEqual(require(record), record)
        other = build(self.presentation, decision_spec(reference="different assertion"))
        self.assertNotEqual(self.decision()["human_proposal_decision_identity"], other["human_proposal_decision_identity"])
        bad = deepcopy(record)
        bad["decision_source"]["provenance"]["reference"] = " padded "
        with self.assertRaises(ValueError): require(bad)

    def test_no_default_alias_or_extra_decision_states(self):
        for value in (None, True, 1, [], {}, "approve", "reject", "defer", "abstain", "cancel", "revoke", "supersede", "pending", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                build(self.presentation, decision_spec(value))
        for value in ({}, {"decision": "approved"}, None):
            with self.assertRaises(ValueError): build(self.presentation, value)

    def test_invalid_source_scalars_and_vocabularies(self):
        for kind in (None, True, 2, [], "implicit_user", "explicit_operator_profile"):
            with self.assertRaises(ValueError): build(self.presentation, decision_spec(kind=kind))
        for reference in (None, True, 1, [], {}, "", "  "):
            with self.assertRaises(ValueError): build(self.presentation, decision_spec(reference=reference))

    def test_closed_spec_source_and_output_structures(self):
        spec = decision_spec()
        for path in ((), ("decision_source",), ("decision_source", "provenance")):
            node = spec
            for key in path: node = node[key]
            for missing in [*node, None]:
                bad = deepcopy(spec); target = bad
                for key in path: target = target[key]
                if missing is None: target["comment"] = "not modeled"
                else: del target[missing]
                with self.assertRaises(ValueError): build(self.presentation, bad)
        record = self.decision()
        for path in ((), ("human_proposal_decision_identity",), ("human_proposal_decision_identity", "canonical_payload")):
            node = record
            for key in path: node = node[key]
            for missing in [*node, None]:
                bad = deepcopy(record); target = bad
                for key in path: target = target[key]
                if missing is None: target["expiry"] = "not modeled"
                else: del target[missing]
                with self.assertRaises(ValueError): require(bad)

    def test_unknown_model_source_identity_versions_and_algorithms(self):
        record = self.decision()
        for path, value in (
            (("human_proposal_decision_model_version",), "1"),
            (("human_proposal_decision_model_version",), 2),
            (("source_proposal_presentation_model_version",), "1"),
            (("source_proposal_presentation_model_version",), "3"),
            (("human_proposal_decision_identity", "human_proposal_decision_identity_version"), "2"),
            (("human_proposal_decision_identity", "digest_algorithm"), "sha512"),
            (("human_proposal_decision_identity", "digest"), "0" * 64),
            (("limitations",), []),
        ):
            bad = deepcopy(record); node = bad
            for key in path[:-1]: node = node[key]
            node[path[-1]] = value
            with self.subTest(path=path), self.assertRaises(ValueError): require(bad)

    def test_json_type_drift_and_nonfinite_values_fail(self):
        record = self.decision()
        for value in (True, 1.0, float("nan"), float("inf"), float("-inf")):
            bad = deepcopy(record)
            bad["proposal_identity"]["canonical_payload"]["delta"]["quantity"] = value
            with self.assertRaises(ValueError): require(bad)
            bad = deepcopy(self.presentation)
            bad["review_artifact"]["proposal"]["quantity"] = value
            with self.assertRaises(ValueError): build(bad, decision_spec())
        for value in (tuple(record["limitations"]), set(record["limitations"]), object()):
            bad = deepcopy(record); bad["limitations"] = value
            with self.assertRaises(ValueError): require(bad)
        bad = deepcopy(record); bad["decision_source"][1] = "wrong key type"
        with self.assertRaises(ValueError): require(bad)

    def test_inputs_output_and_verified_copy_are_detached(self):
        presentation, spec = deepcopy(self.presentation), decision_spec()
        before_pres, before_spec = deepcopy(presentation), deepcopy(spec)
        record = build(presentation, spec)
        self.assertEqual(presentation, before_pres); self.assertEqual(spec, before_spec)
        verified = require(record)
        frozen = deepcopy(record)
        presentation.clear(); spec.clear(); verified["source_presentation"].clear()
        self.assertEqual(record, frozen)
        record["presentation_identity"]["canonical_payload"].clear()
        self.assertEqual(record["source_presentation"], before_pres)
        self.assertEqual(record["human_proposal_decision_identity"]["canonical_payload"]["presentation_identity"], frozen["presentation_identity"])

    def test_build_and_require_use_public_scoped_owner_verifier(self):
        with patch.object(scoped, "require_proposal_presentation_v2", wraps=require_proposal_presentation_v2) as verifier:
            record = self.decision()
            verifier.assert_called_once_with(self.presentation)
            verifier.reset_mock(); require(record)
            verifier.assert_called_once_with(record["source_presentation"])

    def test_purity_no_io_clock_network_or_upstream_work(self):
        record = self.decision()
        targets = (
            "builtins.open", "sqlite3.connect", "time.time", "time.monotonic",
            "time.perf_counter", "socket.socket", "socket.create_connection",
            "services.validator.validate_deck", "services.deck_review.review_deck",
            "services.proposal.build_scoped_proposal", "services.proposal.build_proposal",
            "services.proposal_presentation.build_proposal_presentation_v2",
            "services.recommendation.build_recommendation_decisions",
            "services.pre_execution_revalidation.build_pre_execution_revalidation",
            "services.local_deck_application_intent.build_local_deck_application_intent",
            "services.local_deck_application.apply_local_deck_application",
            "services.local_deck_application_outcome.prepare_local_deck_application",
            "services.exporter.export_arena_deck", "mtgadb.snapshot_store.save_snapshot",
            "mtgadb.managed_deck_store.read_destination_state",
        )
        with ExitStack() as stack:
            for target in targets:
                stack.enter_context(patch(target, side_effect=AssertionError(target)))
            self.assertEqual(self.decision(), record)
            self.assertEqual(require(record), record)

    def test_established_conditional_unestablished_and_mixed_routes_preserved(self):
        cases = (
            ("You gain 1 life.", ["unconditional"]),
            ("{T}: You gain 1 life.", ["conditional"]),
            ("You gain 1 life. Unmodeled rider.", ["unestablished"]),
            ("You gain 1 life.\nYou gain 2 life. Unmodeled rider.", ["unconditional", "unestablished"]),
        )
        for text, availability in cases:
            self.review.sql("UPDATE cards SET rules_text=? WHERE title_id=2", (text,))
            presentation = self.produce()
            evidence = presentation["review_artifact"]["evidence_context"]
            self.assertEqual([e["assessment"]["availability"] for e in evidence["support_context"]["entries"]], availability)
            for choice in ("approved", "declined"):
                record = self.decision(choice, presentation)
                self.assertEqual(encoded(record["source_presentation"]), encoded(presentation))
                self.assertEqual(require(record)["source_presentation"]["review_artifact"]["evidence_context"], evidence)

    def test_partial_activated_without_parsed_cost_remains_unestablished(self):
        self.review.sql("UPDATE cards SET rules_text='{4}: You gain 1 life.' WHERE title_id=2")
        presentation = self.produce()
        entry = presentation["review_artifact"]["evidence_context"]["support_context"]["entries"][0]
        origin = entry["dependency_context"]["origin"]
        self.assertEqual((origin["ability_kind"], origin["parse_status"], origin["costs"]), ("activated", "partial", []))
        self.assertTrue(any(r["scope"] == "cost" and r["reason"] == "unsupported_cost" for r in origin["unsupported_remainder"]))
        self.assertEqual(entry["assessment"]["establishment"], "unestablished")
        self.assertEqual(entry["assessment"]["availability"], "unestablished")
        self.assertEqual(require(self.decision(presentation=presentation))["source_presentation"], presentation)

    def test_array_order_and_four_identical_clause_routes_survive(self):
        self.review.sql("UPDATE cards SET rules_text=? WHERE title_id=2", ("\n".join(["You gain 1 life. Unmodeled rider."] * 4),))
        presentation = self.produce()
        evidence = presentation["review_artifact"]["evidence_context"]
        self.assertEqual(len(evidence["support_context"]["entries"]), 4)
        self.assertEqual(len(evidence["support_context"]["unresolved"]), 4)
        self.assertEqual(self.decision(presentation=presentation)["source_presentation"], presentation)
        self.assertEqual(require(self.decision(presentation=presentation))["source_presentation"], presentation)
        changed = deepcopy(presentation)
        changed["review_artifact"]["evidence_context"]["matching_feature_evidence"].reverse()
        rehash_presentation(changed)
        # Presentation's owner rejects reordered routes that contradict its
        # captured support entries. Review never normalizes away that change.
        with self.assertRaises(ValueError): build(changed, decision_spec())
        evidence = changed["review_artifact"]["evidence_context"]
        evidence["support_context"] = _support_context(evidence["matching_feature_evidence"], evidence["source_need"])
        rehash_presentation(changed)
        self.assertEqual(require_proposal_presentation_v2(changed), changed)
        first = self.decision(presentation=presentation)
        second = self.decision(presentation=changed)
        self.assertNotEqual(first["human_proposal_decision_identity"], second["human_proposal_decision_identity"])
        self.assertEqual(second["source_presentation"], changed)
        transplant = deepcopy(first); transplant["source_presentation"] = changed
        with self.assertRaises(ValueError): require(transplant)

    def test_unknown_completeness_does_not_veto_or_become_complete(self):
        evidence = self.presentation["review_artifact"]["evidence_context"]
        self.assertEqual(evidence["source_evidence_completeness"]["status"], "unknown")
        self.assertEqual(evidence["source_evidence_completeness"]["reason"], "reviewed_features_not_exhaustive")
        self.assertEqual(self.decision()["source_presentation"]["review_artifact"]["evidence_context"], evidence)

    def test_malformed_source_rejected_for_both_decisions_even_rehashed(self):
        variants = [None, {}, self.presentation["presentation_identity"], self.presentation["review_artifact"]]
        for field in ("scope", "assessment", "candidate", "disclosure", "version"):
            bad = deepcopy(self.presentation); evidence = bad["review_artifact"]["evidence_context"]
            if field == "scope": evidence["source_need"]["evidence_boundary"]["unsupported_text_card_copies"] += 1
            elif field == "assessment": evidence["support_context"]["entries"][0]["assessment"]["availability"] = "unestablished"
            elif field == "candidate": evidence["candidate_title_id"] += 1
            elif field == "disclosure": bad["review_artifact"]["validation"]["freshness_disclosure"] = "current"
            else: bad["proposal_presentation_model_version"] = "3"
            rehash_presentation(bad); variants.append(bad)
        for bad in variants:
            for choice in ("approved", "declined"):
                with self.assertRaises(ValueError): build(bad, decision_spec(choice))

    def test_decision_and_provenance_tampering_and_coherent_new_assertion(self):
        record = self.decision()
        for field, value in (("decision", "declined"), ("decision_source", decision_spec(reference="altered")["decision_source"])):
            bad = deepcopy(record); bad[field] = value
            with self.assertRaises(ValueError): require(bad)
            payload = bad["human_proposal_decision_identity"]["canonical_payload"]
            payload[field] = deepcopy(value)
            bad["human_proposal_decision_identity"]["digest"] = hashlib.sha256(encoded(payload)).hexdigest()
            # A coherent reassertion is structurally valid, never authentication.
            self.assertEqual(require(bad), bad)
            self.assertNotEqual(record["human_proposal_decision_identity"], bad["human_proposal_decision_identity"])

    def test_transplant_and_recomputed_contradictory_payload_fail(self):
        changed = self.different_scope()
        record = self.decision()
        for field in ("source_presentation", "proposal_identity", "presentation_identity"):
            other = self.decision(presentation=changed)
            if record[field] == other[field]: continue
            bad = deepcopy(record); bad[field] = deepcopy(other[field])
            payload = bad["human_proposal_decision_identity"]["canonical_payload"]
            if field in payload: payload[field] = deepcopy(other[field])
            bad["human_proposal_decision_identity"]["digest"] = hashlib.sha256(encoded(payload)).hexdigest()
            with self.assertRaises(ValueError): require(bad)

    def test_scope_only_change_requires_review_not_new_action_identity(self):
        changed = self.different_scope()
        self.assertEqual(changed["proposal_identity"], self.presentation["proposal_identity"])
        self.assert_new_review(changed)

    def test_candidate_change_requires_new_action_presentation_review(self):
        self.review.sql("UPDATE cards SET cmc=4 WHERE title_id=2")
        changed = self.produce()
        self.assertNotEqual(changed["review_artifact"]["proposal"]["card"]["title_id"], self.presentation["review_artifact"]["proposal"]["card"]["title_id"])
        self.assertNotEqual(changed["proposal_identity"], self.presentation["proposal_identity"])
        self.assert_new_review(changed)

    def test_printing_change_requires_new_review(self):
        self.review.sql("UPDATE printings SET arena_id=202 WHERE arena_id=201")
        changed = self.produce()
        self.assertEqual(changed["review_artifact"]["proposal"]["card"]["title_id"], self.presentation["review_artifact"]["proposal"]["card"]["title_id"])
        self.assertNotEqual(changed["review_artifact"]["proposal"]["card"]["arena_id"], self.presentation["review_artifact"]["proposal"]["card"]["arena_id"])
        self.assert_new_review(changed)

    def test_supported_zone_action_change_requires_new_review(self):
        spec = deepcopy(self.review.fx["proposal"]); spec["target_zone"] = "sideboard"
        self.review.write(self.review.prop, spec)
        changed = self.produce()
        self.assertEqual(changed["review_artifact"]["proposal"]["target_zone"], "sideboard")
        self.assert_new_review(changed)

    def test_baseline_gameplay_change_requires_new_review(self):
        self.review.deck.write_text(self.review.deck.read_text(encoding="utf-8").replace("24 Plains", "25 Plains"), encoding="utf-8")
        changed = self.produce()
        self.assertNotEqual(changed["review_artifact"]["source_baseline_deck_identity"], self.presentation["review_artifact"]["source_baseline_deck_identity"])
        self.assert_new_review(changed)

    def test_explicit_policy_semantics_change_requires_review(self):
        spec = deepcopy(self.review.fx["proposal"])
        spec["policy_id"] += ".new-explicit-declaration"
        self.review.write(self.review.prop, spec)
        changed = self.produce()
        self.assertEqual(changed["review_artifact"]["proposal"], self.presentation["review_artifact"]["proposal"])
        self.assertNotEqual(changed["proposal_identity"], self.presentation["proposal_identity"])
        self.assert_new_review(changed)

    def test_unsupported_quantity_cannot_be_approved(self):
        bad = deepcopy(self.presentation)
        bad["review_artifact"]["proposal"]["quantity"] = 2
        rehash_presentation(bad)
        with self.assertRaises(ValueError): build(bad, decision_spec())

    def test_presentation_version_binding_even_when_digest_unchanged(self):
        bad = deepcopy(self.presentation); bad["proposal_presentation_model_version"] = "3"
        self.assertEqual(bad["presentation_identity"], self.presentation["presentation_identity"])
        with self.assertRaises(ValueError): build(bad, decision_spec())
        record = self.decision(); record["source_proposal_presentation_model_version"] = "3"
        payload = record["human_proposal_decision_identity"]["canonical_payload"]
        payload["source_proposal_presentation_model_version"] = "3"
        record["human_proposal_decision_identity"]["digest"] = hashlib.sha256(encoded(payload)).hexdigest()
        with self.assertRaises(ValueError): require(record)

    def test_external_deck_changes_do_not_retroactively_invalidate_review(self):
        record = self.decision()
        other = build_deck_snapshot_identity(Deck(main={401: 100}))
        self.assertNotEqual(other, record["source_presentation"]["review_artifact"]["source_baseline_deck_identity"])
        self.assertEqual(require(record), record)

    def test_legacy_preservation_and_both_directions_of_crossing(self):
        fixture = self.fixture(revalidation_tests.PreExecutionRevalidationTests)
        legacy = build_human_proposal_decision(fixture.presentation, decision_spec())
        self.assertEqual(require_human_proposal_decision(legacy), legacy)
        for presentation, builder in ((self.presentation, build_human_proposal_decision), (fixture.presentation, build)):
            with self.assertRaises(ValueError): builder(presentation, decision_spec())
        with self.assertRaises(ValueError): require(legacy)
        with self.assertRaises(ValueError): require_human_proposal_decision(self.decision())
        restamped = self.decision(); restamped["human_proposal_decision_model_version"] = "1"
        restamped["source_proposal_presentation_model_version"] = "1"
        with self.assertRaises(ValueError): require_human_proposal_decision(restamped)

    def test_actual_review_two_stops_at_legacy_revalidation_before_current_checks(self):
        fixture = self.fixture(revalidation_tests.PreExecutionRevalidationTests)
        for choice in ("approved", "declined"):
            with patch("services.pre_execution_revalidation.build_deck_snapshot_identity", side_effect=AssertionError("baseline access")), patch("services.pre_execution_revalidation.validate_deck", side_effect=AssertionError("validation")):
                result = fixture.evaluate(human_decision=self.decision(choice), con=None, current_baseline_deck=None)
            self.assertEqual((result["status"], result["reason"]), ("rejected", "unsupported_version"))
            self.assertIsNone(result["pre_execution_revalidation_identity"])
            self.assertIsNone(result["source_human_proposal_decision"])
        bad = deepcopy(fixture.evaluate()); substitute_decision(bad, self.decision())
        with self.assertRaises(ValueError): require_pre_execution_revalidation(bad)

    def test_intent_and_application_reject_nested_actual_review_without_io(self):
        fixture = self.fixture(intent_tests.LocalDeckApplicationIntentTests)
        bad = deepcopy(fixture.revalidation); substitute_decision(bad, self.decision())
        before = hashlib.sha256(fixture.path.read_bytes()).hexdigest()
        with self.assertRaises(ValueError): fixture.build(revalidation=bad)
        self.assertEqual(hashlib.sha256(fixture.path.read_bytes()).hexdigest(), before)
        fixture = self.fixture(application_tests.LocalDeckApplicationTests)
        bad = deepcopy(fixture.intent); substitute_decision(bad, self.decision())
        before = hashlib.sha256(fixture.path.read_bytes()).hexdigest()
        with self.assertRaises(ValueError): require_local_deck_application_intent(bad)
        with patch("mtgadb.managed_deck_store._connection", side_effect=AssertionError("store access")):
            with self.assertRaises(LocalDeckApplicationError) as caught: fixture.apply(intent=bad)
        self.assertEqual(caught.exception.code, "invalid_intent")
        self.assertEqual(hashlib.sha256(fixture.path.read_bytes()).hexdigest(), before)
        self.assertEqual(fixture.read()["revision"], 1)

    def test_prepare_recovery_prepared_execution_and_receipt_firewall(self):
        fixture = self.fixture(outcome_tests.LocalDeckApplicationOutcomeTests)
        bad = deepcopy(fixture.intent); substitute_decision(bad, self.decision())
        before = hashlib.sha256(fixture.path.read_bytes()).hexdigest()
        with patch("mtgadb.managed_deck_store._connection", side_effect=AssertionError("store access")):
            for action in (lambda: fixture.prepare(intent=bad), lambda: fixture.recover(intent=bad)):
                with self.assertRaises(LocalDeckApplicationError) as caught: action()
                self.assertEqual(caught.exception.code, "invalid_intent")
        self.assertEqual(fixture.count(), 0)
        self.assertEqual(hashlib.sha256(fixture.path.read_bytes()).hexdigest(), before)
        operation = fixture.prepare()
        bad_op = deepcopy(operation); substitute_decision(bad_op, self.decision())
        before = hashlib.sha256(fixture.path.read_bytes()).hexdigest()
        with patch("mtgadb.managed_deck_store._connection", side_effect=AssertionError("store access")):
            with self.assertRaises(outcome.LocalDeckApplicationOutcomeError) as caught: fixture.execute(bad_op)
        self.assertEqual(caught.exception.code, "invalid_operation")
        self.assertEqual(hashlib.sha256(fixture.path.read_bytes()).hexdigest(), before)
        receipt = fixture.execute(operation)  # The unchanged legacy chain still works.
        bad_receipt = deepcopy(receipt); substitute_decision(bad_receipt, self.decision())
        with self.assertRaises(ValueError): require_local_deck_application_result(bad_receipt["application_result"])
        with self.assertRaises(ValueError): outcome.require_local_deck_application_receipt(bad_receipt)
        recovered = fixture.recover()
        self.assertEqual(recovered["status"], "committed")
        self.assertEqual(recovered["receipt"], receipt)


if __name__ == "__main__":
    unittest.main()
