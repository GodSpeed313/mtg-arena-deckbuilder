from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import sqlite3
import unittest
from unittest.mock import patch

from mtgadb.deck_identity import build_deck_snapshot_identity
from mtgadb.model import Collection, Deck, Inventory
from mtgadb.modes import OperatingMode
from services.human_proposal_decision import (
    build_human_proposal_decision, require_human_proposal_decision,
)
from services.pre_execution_revalidation import (
    build_pre_execution_revalidation, require_pre_execution_revalidation,
)
from services.proposal_presentation import build_proposal_presentation
from services.validator import ValidationIssue, ValidationReport, validate_deck
from tests import test_proposal_presentation as presentation_tests
from tests.test_human_proposal_decision import decision_spec


SERVICE = "services.pre_execution_revalidation."


def rehash(identity):
    identity["digest"] = hashlib.sha256(json.dumps(
        identity["canonical_payload"], sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")).hexdigest()


class PreExecutionRevalidationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = presentation_tests.ProposalPresentationTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.con = self.fixture.con
        self.deck = self.fixture.deck
        self.format = self.fixture.format
        self.rules = self.fixture.rules
        self.proposal = self.fixture.accepted()
        self.presentation = build_proposal_presentation(self.proposal)
        self.decision = build_human_proposal_decision(self.presentation, decision_spec())
        self.owned = Collection({101: 1, 201: 1, 301: 1})
        self.inventory = Inventory({"common": 4, "uncommon": 0, "rare": 0, "mythic": 0})

    def evaluate(self, **overrides):
        args = dict(
            human_decision=self.decision, current_baseline_deck=self.deck,
            con=self.con, format=self.format, rules=self.rules,
            mode=OperatingMode.UNLIMITED,
        )
        args.update(overrides)
        return build_pre_execution_revalidation(**args)

    def budget(self, **overrides):
        args = dict(mode=OperatingMode.WILDCARD_BUDGET,
                    collection=self.owned, inventory=self.inventory)
        args.update(overrides)
        return self.evaluate(**args)

    def assertOutcome(self, result, status, reason):
        self.assertEqual((result["status"], result["reason"]), (status, reason))
        if status != "revalidated":
            self.assertIsNone(result["pre_execution_revalidation_identity"])

    def assertValidationIssue(self, result, code):
        self.assertOutcome(result, "not_ready", "current_validation_failed")
        self.assertIn(code, {issue["code"] for issue in result["fresh_validation"]["errors"]})

    def test_approved_unchanged_baseline_revalidates_with_complete_evidence(self):
        result = self.evaluate()
        self.assertOutcome(result, "revalidated", "fresh_validation_passed")
        self.assertEqual(set(result), {
            "pre_execution_revalidation_model_version",
            "source_human_proposal_decision_model_version", "status", "reason", "evidence",
            "source_human_proposal_decision", "human_proposal_decision_identity",
            "proposal_identity", "presentation_identity", "approved_delta",
            "current_baseline_deck_identity", "reconstructed_result_deck_identity",
            "current_validation_context", "current_printing_fact", "fresh_validation",
            "resource_assessment", "destination_assessment",
            "pre_execution_revalidation_identity", "limitations",
        })
        self.assertEqual(result["pre_execution_revalidation_model_version"], "1")
        self.assertEqual(result["source_human_proposal_decision_model_version"], "1")
        self.assertEqual(result["source_human_proposal_decision"], self.decision)
        self.assertTrue(result["fresh_validation"]["valid"])
        self.assertEqual(result["current_printing_fact"], {
            "arena_id": 101, "title_id": 1, "name": "Alpha", "set_code": "AAA",
            "collector_number": "1", "rarity": "common",
        })
        self.assertEqual(result["current_baseline_deck_identity"],
                         build_deck_snapshot_identity(self.deck))
        self.assertEqual(result["reconstructed_result_deck_identity"],
                         self.decision["proposal_identity"]["canonical_payload"][
                             "resulting_deck_identity"])

    def test_declined_short_circuits_before_current_state_or_database_access(self):
        decision = build_human_proposal_decision(self.presentation, decision_spec("declined"))
        queries = []
        self.con.set_trace_callback(queries.append)
        with ExitStack() as stack:
            for function in ("Deck", "validate_deck", "build_deck_snapshot_identity",
                             "_resource_assessment", "canonical_validation_context"):
                stack.enter_context(patch(SERVICE + function, side_effect=AssertionError(function)))
            result = self.evaluate(human_decision=decision, current_baseline_deck=None,
                                   format=None, rules=None, mode=None,
                                   collection=object(), inventory=object())
        self.assertOutcome(result, "not_ready", "decision_declined")
        self.assertEqual(queries, [])
        self.assertIsNone(result["approved_delta"])

    def test_tampered_decline_is_rejected_not_short_circuited(self):
        decision = deepcopy(self.decision)
        decision["decision"] = "declined"
        self.assertOutcome(self.evaluate(human_decision=decision), "rejected", "identity_mismatch")

    def test_tampered_decision_digest(self):
        decision = deepcopy(self.decision)
        decision["human_proposal_decision_identity"]["digest"] = "0" * 64
        self.assertOutcome(self.evaluate(human_decision=decision), "rejected", "identity_mismatch")

    def test_tampered_decision_provenance(self):
        decision = deepcopy(self.decision)
        decision["decision_source"]["provenance"]["reference"] = "different review"
        self.assertOutcome(self.evaluate(human_decision=decision), "rejected", "identity_mismatch")

    def test_tampered_embedded_presentation(self):
        decision = deepcopy(self.decision)
        decision["source_presentation"]["review_artifact"]["proposal"]["card"]["name"] = "Other"
        self.assertOutcome(self.evaluate(human_decision=decision), "rejected", "identity_mismatch")

    def test_type_substitution_in_embedded_review_cannot_hide_behind_python_equality(self):
        for value in (True, 1.0):
            decision = deepcopy(self.decision)
            decision["source_presentation"]["review_artifact"]["proposal"]["quantity"] = value
            self.assertOutcome(self.evaluate(human_decision=decision),
                               "rejected", "identity_mismatch")

    def test_tampered_proposal_and_presentation_identities(self):
        for field in ("proposal_identity", "presentation_identity"):
            with self.subTest(field=field):
                decision = deepcopy(self.decision)
                decision[field]["digest"] = "0" * 64
                self.assertOutcome(self.evaluate(human_decision=decision),
                                   "rejected", "identity_mismatch")

    def test_valid_identities_from_another_review_cannot_be_swapped(self):
        other = build_human_proposal_decision(build_proposal_presentation(
            self.fixture.accepted(policy_id="proposal.other.v1")), decision_spec())
        for field in ("proposal_identity", "presentation_identity", "source_presentation",
                      "human_proposal_decision_identity"):
            with self.subTest(field=field):
                decision = deepcopy(self.decision)
                decision[field] = other[field]
                self.assertOutcome(self.evaluate(human_decision=decision),
                                   "rejected", "identity_mismatch")

    def test_closed_shape_missing_extra_fields_and_malformed_decisions(self):
        for value in (None, [], "approved", True, {}):
            with self.subTest(value=value):
                self.assertOutcome(self.evaluate(human_decision=value),
                                   "rejected", "malformed_input")
        for field in self.decision:
            changed = deepcopy(self.decision)
            del changed[field]
            with self.subTest(missing=field):
                self.assertOutcome(self.evaluate(human_decision=changed),
                                   "rejected", "malformed_input")
        for extra in ("override", "delta", "proposed_deck", "execution_authorized"):
            changed = deepcopy(self.decision)
            changed[extra] = True
            self.assertOutcome(self.evaluate(human_decision=changed),
                               "rejected", "malformed_input")

    def test_unsupported_and_unhashable_versions_fail_closed(self):
        for field in ("human_proposal_decision_model_version",
                      "source_proposal_presentation_model_version"):
            for value in ("2", [], {}, True, 1, None):
                with self.subTest(field=field, value=value):
                    decision = deepcopy(self.decision)
                    decision[field] = value
                    self.assertOutcome(self.evaluate(human_decision=decision),
                                       "rejected", "unsupported_version")
        for field, value in (("human_proposal_decision_identity_version", "2"),
                             ("digest_algorithm", "sha512")):
            decision = deepcopy(self.decision)
            decision["human_proposal_decision_identity"][field] = value
            self.assertOutcome(self.evaluate(human_decision=decision),
                               "rejected", "unsupported_version")

    def test_nested_unsupported_versions_and_algorithms_are_rejected(self):
        for field, version_field in (("proposal_identity", "proposal_identity_version"),
                                     ("presentation_identity", "presentation_identity_version")):
            for key, value in ((version_field, "99"), ("digest_algorithm", "md5")):
                decision = deepcopy(self.decision)
                decision["source_presentation"][field][key] = value
                self.assertOutcome(self.evaluate(human_decision=decision),
                                   "rejected", "unsupported_version")

    def test_rehashed_payload_contradictions_and_boolean_integer_substitution(self):
        for value in (True, 1.0, 2):
            decision = deepcopy(self.decision)
            identity = decision["human_proposal_decision_identity"]
            identity["canonical_payload"]["proposal_identity"]["canonical_payload"][
                "delta"]["quantity"] = value
            rehash(identity)
            with self.subTest(value=value):
                self.assertOutcome(self.evaluate(human_decision=decision),
                                   "rejected", "identity_mismatch")

    def test_noncanonical_provenance_and_fixed_limitations(self):
        for value in (" review 42 ", "", None):
            decision = deepcopy(self.decision)
            decision["decision_source"]["provenance"]["reference"] = value
            self.assertOutcome(self.evaluate(human_decision=decision),
                               "rejected", "malformed_input")
        decision = deepcopy(self.decision)
        decision["limitations"] = []
        self.assertOutcome(self.evaluate(human_decision=decision), "rejected", "malformed_input")

    def test_public_verifier_returns_detached_valid_approved_and_declined_records(self):
        for choice in ("approved", "declined"):
            decision = build_human_proposal_decision(self.presentation, decision_spec(choice))
            verified = require_human_proposal_decision(decision)
            self.assertEqual(verified, decision)
            verified["decision_source"]["provenance"]["reference"] = "changed copy"
            self.assertEqual(decision["decision_source"]["provenance"]["reference"], "review 42")

    def test_changed_baseline_quantity(self):
        self.assertOutcome(self.evaluate(current_baseline_deck=replace(self.deck, main={201: 2})),
                           "not_ready", "baseline_snapshot_mismatch")

    def test_changed_baseline_printing_even_for_same_title(self):
        deck = replace(self.deck, main={101: 1, 201: 1})
        decision = build_human_proposal_decision(build_proposal_presentation(
            self.fixture.accepted(deck=deck)), decision_spec())
        changed = replace(deck, main={102: 1, 201: 1})
        self.assertOutcome(self.evaluate(human_decision=decision, current_baseline_deck=changed),
                           "not_ready", "baseline_snapshot_mismatch")

    def test_changed_baseline_zone(self):
        for changed in (replace(self.deck, main={}, sideboard={201: 1, 301: 1}),
                        replace(self.deck, main={}, commander={201: 1})):
            self.assertOutcome(self.evaluate(current_baseline_deck=changed),
                               "not_ready", "baseline_snapshot_mismatch")

    def test_unrelated_baseline_card_changes_and_removals(self):
        for changed in (replace(self.deck, sideboard={301: 2}),
                        replace(self.deck, sideboard={}),
                        replace(self.deck, main={201: 1, 301: 1})):
            self.assertOutcome(self.evaluate(current_baseline_deck=changed),
                               "not_ready", "baseline_snapshot_mismatch")

    def test_baseline_mismatch_prevents_database_and_validator_calls(self):
        with patch(SERVICE + "validate_deck", side_effect=AssertionError("validator called")):
            result = self.evaluate(current_baseline_deck=replace(self.deck, main={}), con=None)
        self.assertOutcome(result, "not_ready", "baseline_snapshot_mismatch")

    def test_metadata_only_differences_leave_entire_output_unchanged(self):
        expected = self.evaluate()
        for changed in (replace(self.deck, name="Renamed"),
                        replace(self.deck, deck_id="different destination")):
            self.assertEqual(self.evaluate(current_baseline_deck=changed), expected)

    def test_insertion_order_is_nonsemantic_for_baseline_and_current_context(self):
        deck = replace(self.deck, main={101: 1, 201: 1})
        decision = build_human_proposal_decision(build_proposal_presentation(
            self.fixture.accepted(deck=deck)), decision_spec())
        first = self.evaluate(human_decision=decision, current_baseline_deck=deck,
                              format=replace(self.format, individual_card_quotas={1: 4, 2: 4}))
        second = self.evaluate(human_decision=decision,
                               current_baseline_deck=replace(deck, main={201: 1, 101: 1}),
                               format=replace(self.format, individual_card_quotas={2: 4, 1: 4}))
        self.assertOutcome(first, "revalidated", "fresh_validation_passed")
        self.assertEqual(first, second)

    def test_fresh_reconstruction_is_exactly_one_in_each_approved_target_zone(self):
        for target in ("main", "sideboard", "commander"):
            decision = build_human_proposal_decision(build_proposal_presentation(
                self.fixture.accepted(target=target)), decision_spec())
            with patch(SERVICE + "validate_deck", wraps=validate_deck) as validator:
                result = self.evaluate(human_decision=decision)
            self.assertOutcome(result, "revalidated", "fresh_validation_passed")
            reconstructed = validator.call_args.args[0]
            self.assertIsNot(reconstructed, self.deck)
            for zone in ("main", "sideboard", "commander"):
                expected = dict(getattr(self.deck, zone))
                if zone == target:
                    expected[101] = 1
                self.assertEqual(getattr(reconstructed, zone), expected)
                self.assertIsNot(getattr(reconstructed, zone), getattr(self.deck, zone))
            self.assertEqual(result["approved_delta"]["quantity"], 1)
            self.assertEqual(result["approved_delta"]["zone"], target)

    def test_historical_proposed_deck_cannot_override_reconstruction(self):
        self.proposal["proposal"]["proposed_deck"].main.clear()
        self.assertOutcome(self.evaluate(), "revalidated", "fresh_validation_passed")
        with self.assertRaises(TypeError):
            self.evaluate(proposed_deck=Deck())
        with self.assertRaises(TypeError):
            self.evaluate(delta={"quantity": 2})

    def test_unexpected_reconstructed_identity_is_rejected(self):
        with patch(SERVICE + "build_deck_snapshot_identity", side_effect=[
            build_deck_snapshot_identity(self.deck), build_deck_snapshot_identity(Deck()),
        ]), patch(SERVICE + "validate_deck", side_effect=AssertionError("validator called")):
            self.assertOutcome(self.evaluate(), "rejected", "result_identity_mismatch")

    def test_current_format_ban_overrides_historical_validation(self):
        result = self.evaluate(format=replace(self.format, banned_title_ids=frozenset({1})))
        self.assertValidationIssue(result, "format_banned")
        self.assertEqual(result["current_validation_context"]["format"]["banned_title_ids"], [1])
        self.assertTrue(self.presentation["review_artifact"]["validation"]["evidence"]["valid"])

    def test_current_rules_make_deck_illegal_without_fallback_zone(self):
        result = self.evaluate(rules=replace(self.rules, max_main=1))
        self.assertValidationIssue(result, "main_size")
        self.assertEqual(result["approved_delta"]["zone"], "main")
        self.assertEqual(result["current_validation_context"]["rules"]["max_main"], 1)

    def test_current_color_and_copy_rules_are_used(self):
        self.assertValidationIssue(self.evaluate(rules=replace(
            self.rules, allowed_colors=frozenset({"U"}))), "color_identity")
        self.assertValidationIssue(self.evaluate(format=replace(
            self.format, individual_card_quotas={1: 0})), "copy_limit")

    def test_missing_approved_printing_never_chooses_other_printing(self):
        self.con.execute("DELETE FROM printings WHERE arena_id = 101")
        result = self.budget(collection=Collection({102: 4, 201: 1, 301: 1}))
        self.assertOutcome(result, "not_ready", "current_printing_unavailable")
        self.assertEqual(result["approved_delta"]["arena_id"], 101)
        self.assertIsNotNone(self.con.execute("SELECT 1 FROM printings WHERE arena_id = 102").fetchone())

    def test_current_printing_mapped_to_other_title(self):
        self.con.execute("UPDATE printings SET title_id = 2 WHERE arena_id = 101")
        result = self.evaluate()
        self.assertOutcome(result, "not_ready", "current_printing_identity_mismatch")
        self.assertEqual(result["current_printing_fact"]["title_id"], 2)

    def test_fresh_validator_checks_unrelated_baseline_cards(self):
        self.con.execute("DELETE FROM printings WHERE arena_id = 301")
        self.assertValidationIssue(self.evaluate(), "unknown_printing")

    def test_validation_unavailable_for_missing_closed_or_wrong_schema_database(self):
        closed = sqlite3.connect(":memory:")
        closed.close()
        empty = sqlite3.connect(":memory:")
        self.addCleanup(empty.close)
        for con in (None, closed, empty):
            self.assertOutcome(self.evaluate(con=con), "rejected", "validation_unavailable")

    def test_malformed_database_metadata_and_unsupported_rows_fail_closed(self):
        self.con.execute("UPDATE printings SET collector_number = ? WHERE arena_id = 101",
                         (b"not text",))
        self.assertOutcome(self.evaluate(), "rejected", "validation_unavailable")
        self.con.row_factory = lambda cursor, row: {"invalid": row}
        self.assertOutcome(self.evaluate(), "rejected", "validation_unavailable")

    def test_validator_failure_or_missing_report_cannot_be_ready(self):
        for error in (sqlite3.OperationalError("unavailable"), ValueError("unsupported state")):
            with patch(SERVICE + "validate_deck", side_effect=error):
                self.assertOutcome(self.evaluate(), "rejected", "validation_unavailable")
        with patch(SERVICE + "validate_deck", return_value=None):
            self.assertOutcome(self.evaluate(), "rejected", "validation_unavailable")

    def test_explicit_current_objects_required_and_malformed_inputs_rejected(self):
        for override in (
            {"current_baseline_deck": None}, {"current_baseline_deck": replace(self.deck, main={201: True})},
            {"format": None}, {"rules": None}, {"mode": "unlimited"},
            {"rules": replace(self.rules, copy_limit=True)},
            {"rules": replace(self.rules, min_main=10, max_main=1)},
            {"collection": Collection({101: -1})}, {"collection": Collection({101: None})},
            {"inventory": Inventory({"common": -1})}, {"inventory": Inventory({"common": None})},
        ):
            with self.subTest(override=override):
                self.assertOutcome(self.evaluate(**override), "rejected", "malformed_input")

    def test_unlimited_is_legality_only_without_ownership_claim(self):
        result = self.evaluate()
        self.assertEqual(result["resource_assessment"], {
            "resource_mode": "unlimited", "resource_status": "not_evaluated",
            "wildcard_cost": {}, "spending_authorized": False,
        })

    def test_full_collection_sufficient_owned_path(self):
        result = self.evaluate(mode=OperatingMode.FULL_COLLECTION, collection=self.owned)
        self.assertOutcome(result, "revalidated", "fresh_validation_passed")
        self.assertEqual(result["resource_assessment"]["resource_status"], "owned_no_crafting_required")

    def test_full_collection_missing_incomplete_and_insufficient(self):
        for collection, code in ((None, "collection_required"), (Collection(), "not_owned"),
                                 (Collection({101: 1, 201: 1}), "not_owned"),
                                 (Collection({101: 0, 201: 1, 301: 1}), "not_owned")):
            with self.subTest(collection=collection):
                self.assertValidationIssue(self.evaluate(
                    mode=OperatingMode.FULL_COLLECTION, collection=collection), code)

    def test_owned_alternate_printing_does_not_replace_approved_printing(self):
        result = self.evaluate(mode=OperatingMode.FULL_COLLECTION,
                               collection=Collection({102: 4, 201: 1, 301: 1}))
        self.assertValidationIssue(result, "not_owned")
        self.assertEqual(result["approved_delta"]["arena_id"], 101)

    def test_wildcard_budget_fully_owned_no_spend_path(self):
        result = self.budget(inventory=Inventory({}))
        self.assertOutcome(result, "revalidated", "fresh_validation_passed")
        self.assertEqual(result["resource_assessment"]["resource_status"], "no_spend_required")
        self.assertEqual(result["fresh_validation"]["wildcard_cost"], {})

    def test_partially_owned_affordable_wildcard_requires_separate_authority(self):
        deck = replace(self.deck, main={101: 1, 201: 1})
        decision = build_human_proposal_decision(build_proposal_presentation(
            self.fixture.accepted(deck=deck)), decision_spec())
        before = deepcopy((decision, deck, self.owned, self.inventory))
        result = self.budget(human_decision=decision, current_baseline_deck=deck)
        self.assertEqual((decision, deck, self.owned, self.inventory), before)
        self.assertOutcome(result, "not_ready", "resource_authorization_required")
        self.assertEqual(result["fresh_validation"]["wildcard_cost"], {"common": 1})
        self.assertEqual(result["approved_delta"]["quantity"], 1)
        self.assertEqual(result["resource_assessment"]["resource_status"], "affordable_spend_not_authorized")
        self.assertFalse(result["resource_assessment"]["spending_authorized"])

    def test_zero_owned_affordable_wildcards_cover_entire_reconstructed_deck(self):
        result = self.budget(collection=Collection({}))
        self.assertOutcome(result, "not_ready", "resource_authorization_required")
        self.assertEqual(result["fresh_validation"]["wildcard_cost"], {"common": 3})

    def test_insufficient_wildcards(self):
        result = self.budget(collection=Collection({201: 1, 301: 1}),
                             inventory=Inventory({"common": 0}))
        self.assertValidationIssue(result, "wildcard_shortage")
        self.assertEqual(result["fresh_validation"]["wildcard_cost"], {"common": 1})

    def test_missing_collection_never_assumes_zero_owned(self):
        self.assertValidationIssue(self.budget(collection=None), "collection_required")

    def test_missing_inventory_fails_even_when_fully_owned(self):
        self.assertValidationIssue(self.budget(inventory=None), "inventory_required")

    def test_unknown_required_rarity_count_fails_closed(self):
        result = self.budget(collection=Collection({201: 1, 301: 1}),
                             inventory=Inventory({"rare": 99}))
        self.assertValidationIssue(result, "wildcard_inventory_incomplete")

    def test_current_rarity_requires_exact_resource_not_historical_or_alternate_rarity(self):
        self.con.execute("UPDATE printings SET rarity = 'rare' WHERE arena_id = 101")
        result = self.budget(collection=Collection({201: 1, 301: 1}))
        self.assertValidationIssue(result, "wildcard_shortage")
        self.assertEqual(result["current_printing_fact"]["rarity"], "rare")
        self.assertEqual(result["fresh_validation"]["wildcard_cost"], {"rare": 1})
        affordable = self.budget(collection=Collection({201: 1, 301: 1}),
                                  inventory=Inventory({"rare": 1}))
        self.assertOutcome(affordable, "not_ready", "resource_authorization_required")

    def test_unresolved_rarity_cannot_establish_affordability(self):
        self.con.execute("UPDATE printings SET rarity = '' WHERE arena_id = 101")
        result = self.budget(collection=Collection({201: 1, 301: 1}))
        self.assertValidationIssue(result, "wildcard_inventory_incomplete")
        self.assertEqual(result["fresh_validation"]["wildcard_cost"], {"unknown": 1})

    def test_resource_evaluation_is_fresh_on_each_call(self):
        self.assertOutcome(self.budget(), "revalidated", "fresh_validation_passed")
        self.assertOutcome(self.budget(collection=Collection({201: 1, 301: 1})),
                           "not_ready", "resource_authorization_required")
        self.assertValidationIssue(self.budget(collection=Collection({201: 1, 301: 1}),
                                               inventory=Inventory({"common": 0})), "wildcard_shortage")

    def test_repeated_results_and_canonical_sha256_identity_are_deterministic(self):
        result = self.budget()
        self.assertEqual(result, self.budget())
        identity = deepcopy(result["pre_execution_revalidation_identity"])
        self.assertEqual(identity["pre_execution_revalidation_identity_version"], "1")
        self.assertEqual(identity["digest_algorithm"], "sha256")
        original_digest = identity["digest"]
        rehash(identity)
        self.assertEqual(identity["digest"], original_digest)
        payload = identity["canonical_payload"]
        self.assertEqual(set(payload), {
            "pre_execution_revalidation_model_version", "status", "reason",
            "human_proposal_decision_identity", "proposal_identity", "presentation_identity",
            "approved_delta", "current_baseline_deck_identity", "reconstructed_result_deck_identity",
            "current_validation_context", "current_printing_fact", "fresh_validation",
            "resource_assessment", "destination_assessment",
        })
        for field, value in payload.items():
            self.assertEqual(value, result[field])

    def test_identity_changes_with_current_format_rules_mode_and_printing_fact(self):
        original = self.evaluate()["pre_execution_revalidation_identity"]["digest"]
        for kwargs in ({"format": replace(self.format, name="Current Test")},
                       {"rules": replace(self.rules, max_main=249)},
                       {"mode": OperatingMode.FULL_COLLECTION, "collection": self.owned},
                       {"mode": OperatingMode.WILDCARD_BUDGET, "collection": self.owned,
                        "inventory": self.inventory}):
            result = self.evaluate(**kwargs)
            self.assertOutcome(result, "revalidated", "fresh_validation_passed")
            self.assertNotEqual(original, result["pre_execution_revalidation_identity"]["digest"])
        self.con.execute("UPDATE printings SET collector_number = 'new' WHERE arena_id = 101")
        self.assertNotEqual(original, self.evaluate()["pre_execution_revalidation_identity"]["digest"])

    def test_fresh_validator_evidence_is_bound_and_issue_order_is_canonical(self):
        a = ValidationIssue("a", "First")
        b = ValidationIssue("b", "Second")
        with patch(SERVICE + "validate_deck", return_value=ValidationReport(warnings=(a, b))):
            first = self.evaluate()
        with patch(SERVICE + "validate_deck", return_value=ValidationReport(warnings=(b, a))):
            second = self.evaluate()
        self.assertEqual(first, second)
        self.assertNotEqual(first["pre_execution_revalidation_identity"],
                            self.evaluate()["pre_execution_revalidation_identity"])

    def test_all_inputs_and_balances_unchanged_and_outputs_detached(self):
        inputs = (self.decision, self.deck, self.format, self.rules, self.owned, self.inventory)
        before = deepcopy(inputs)
        result = self.budget()
        self.assertEqual(inputs, before)
        result["source_human_proposal_decision"]["decision_source"]["provenance"]["reference"] = "edit"
        result["approved_delta"]["quantity"] = 99
        result["current_validation_context"]["format"]["legal_sets"].clear()
        self.assertEqual(inputs, before)
        self.assertEqual(self.budget()["approved_delta"]["quantity"], 1)

    def test_no_mutation_io_rebuild_or_external_calls(self):
        statements = []
        self.con.set_trace_callback(statements.append)
        before = self.con.total_changes
        with ExitStack() as stack:
            for target in (
                "builtins.open", "io.open", "sqlite3.connect", "socket.socket",
                "subprocess.Popen", "services.proposal.build_proposal",
                "services.proposal_presentation.build_proposal_presentation",
                "services.human_proposal_decision.build_human_proposal_decision",
                "services.exporter.export_arena_deck", "mtgadb.snapshot_store.save_snapshot",
                "mtgadb.canonical.load_decks", "mtgadb.canonical.load_ownership",
                "mtgadb.canonical.load_inventory", "services.intelligence.analyze_deck",
                "services.candidates.discover_candidates",
                "services.candidate_comparison.build_candidate_comparisons",
                "services.candidate_ordering.build_candidate_ordering",
                "services.recommendation.build_recommendation_decisions",
            ):
                stack.enter_context(patch(target, side_effect=AssertionError(target)))
            result = self.budget()
        self.assertOutcome(result, "revalidated", "fresh_validation_passed")
        self.assertEqual(self.con.total_changes, before)
        self.assertTrue(statements)
        self.assertTrue(all(sql.lstrip().upper().startswith("SELECT ") for sql in statements))
        # An entirely JSON result cannot expose a mutable Deck or execution callable.
        json.dumps(result, allow_nan=False)
        self.assertEqual(result["destination_assessment"], {
            "destination_status": "deferred", "destination_reason": "no_destination_contract",
        })
        limitations = " ".join(result["limitations"])
        for phrase in ("not execution authorization", "time-of-check/time-of-use",
                       "not authentication", "does not authorize", "immediately before mutation"):
            self.assertIn(phrase, limitations)


class PreExecutionRevalidationVerifierTests(unittest.TestCase):
    def setUp(self):
        self.fixture = PreExecutionRevalidationTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.artifact = self.fixture.evaluate()

    def verify(self, artifact):
        before = deepcopy(artifact)
        actual = require_pre_execution_revalidation(artifact)
        self.assertEqual(actual, artifact)
        self.assertEqual(before, artifact)
        self.assertIsNot(actual, artifact)
        if artifact["status"] != "revalidated":
            self.assertIsNone(actual["pre_execution_revalidation_identity"])
        return actual

    def changed(self, path, replacement, *, rebind=False):
        value = deepcopy(self.artifact)
        target = value
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = replacement
        if rebind:
            identity = value["pre_execution_revalidation_identity"]
            identity["canonical_payload"] = {
                key: deepcopy(value[key]) for key in identity["canonical_payload"]
            }
            rehash(identity)
        return value

    def reject(self, value):
        before = deepcopy(value)
        with self.assertRaises(ValueError):
            require_pre_execution_revalidation(value)
        self.assertEqual(before, value)

    def test_positive_and_repeat(self):
        self.verify(self.artifact)
        self.assertEqual(self.artifact, self.fixture.evaluate())
        self.verify(self.fixture.evaluate())

    def test_all_positive_resource_modes(self):
        self.verify(self.fixture.budget())
        self.verify(self.fixture.evaluate(mode=OperatingMode.FULL_COLLECTION,
                                          collection=self.fixture.owned))

    def test_detached_copy_and_serialization(self):
        verified = self.verify(json.loads(json.dumps(self.artifact)))
        verified["approved_delta"]["quantity"] = 99
        self.assertEqual(self.artifact["approved_delta"]["quantity"], 1)
        self.verify(deepcopy(self.artifact))

    def test_dictionary_order_is_nonsemantic(self):
        def reverse(value):
            if isinstance(value, dict):
                return {key: reverse(item) for key, item in reversed(list(value.items()))}
            if isinstance(value, list):
                return [reverse(item) for item in value]
            return value
        self.verify(reverse(self.artifact))

    def test_no_external_or_validation_calls(self):
        with ExitStack() as stack:
            for name in (SERVICE + "validate_deck", SERVICE + "build_pre_execution_revalidation",
                         "sqlite3.connect", "builtins.open", "socket.socket",
                         "services.exporter.export_arena_deck", "mtgadb.snapshot_store.save_snapshot"):
                stack.enter_context(patch(name, side_effect=AssertionError(name)))
            self.verify(self.artifact)

    def test_embedded_decision_uses_owning_verifier(self):
        with patch(SERVICE + "require_human_proposal_decision",
                   wraps=require_human_proposal_decision) as verifier:
            self.verify(self.artifact)
        verifier.assert_called_once_with(self.artifact["source_human_proposal_decision"])

    def test_versions_algorithms_digests_payload(self):
        identity = "pre_execution_revalidation_identity"
        for path, replacement in (
            (("pre_execution_revalidation_model_version",), "2"),
            ((identity, "pre_execution_revalidation_identity_version"), "2"),
            ((identity, "digest_algorithm"), "sha512"),
            ((identity, "digest"), "0" * 64),
            ((identity, "canonical_payload", "status"), "not_ready"),
        ):
            with self.subTest(path=path):
                self.reject(self.changed(path, replacement))

    def test_embedded_chain_tampering(self):
        source = "source_human_proposal_decision"
        for path, replacement in (
            ((source, "decision"), "declined"),
            ((source, "decision_source", "provenance", "reference"), "other person"),
            ((source, "source_presentation", "review_artifact", "limitations"), []),
            ((source, "proposal_identity", "digest"), "0" * 64),
            (("human_proposal_decision_identity", "digest"), "0" * 64),
            (("presentation_identity", "digest"), "0" * 64),
            (("proposal_identity", "digest"), "0" * 64),
        ):
            with self.subTest(path=path):
                self.reject(self.changed(path, replacement, rebind=True))

    def test_exact_delta_rejects_rehashed_replacement(self):
        for key, replacement in (("arena_id", 102), ("title_id", 2), ("zone", "sideboard"),
                                 ("quantity", 2), ("operation", "remove"), ("name", "Other")):
            with self.subTest(key=key):
                self.reject(self.changed(("approved_delta", key), replacement, rebind=True))

    def test_baseline_and_result_tampering(self):
        for field in ("current_baseline_deck_identity", "reconstructed_result_deck_identity"):
            self.reject(self.changed((field, "digest"), "0" * 64, rebind=True))
            self.reject(self.changed((field,), build_deck_snapshot_identity(Deck()), rebind=True))

    def test_printing_tampering(self):
        for key, value in (("arena_id", 102), ("title_id", 2), ("name", ""), ("rarity", 1)):
            self.reject(self.changed(("current_printing_fact", key), value, rebind=True))
        self.reject(self.changed(("current_printing_fact", "name"), "Altered name"))

    def test_validation_contradictions_even_when_rehashed(self):
        for key, value in (("valid", False), ("errors", [{"code": "bad", "message": "bad",
                            "zone": None, "title_id": None}]), ("wildcard_cost", {"common": True}),
                           ("warnings", [{"code": "bad"}])):
            self.reject(self.changed(("fresh_validation", key), value, rebind=True))

    def test_resource_contradictions_even_when_rehashed(self):
        for key, value in (("spending_authorized", True), ("resource_status", "no_spend_required"),
                           ("resource_mode", "full_collection"), ("wildcard_cost", {"common": 1})):
            self.reject(self.changed(("resource_assessment", key), value, rebind=True))

    def test_status_reason_and_missing_identity(self):
        for key, value in (("status", "not_ready"), ("reason", "current_validation_failed"),
                           ("pre_execution_revalidation_identity", None)):
            self.reject(self.changed((key,), value))

    def test_fixed_non_authorities_even_when_rehashed(self):
        self.reject(self.changed(("destination_assessment", "destination_status"), "bound", rebind=True))
        self.reject(self.changed(("destination_assessment", "destination_reason"), "selected", rebind=True))
        self.reject(self.changed(("limitations",), []))

    def test_unknown_fields_at_closed_boundaries(self):
        for path in ((), ("pre_execution_revalidation_identity",),
                     ("pre_execution_revalidation_identity", "canonical_payload"),
                     ("current_validation_context",), ("current_validation_context", "format"),
                     ("current_validation_context", "rules"), ("fresh_validation",),
                     ("resource_assessment",), ("destination_assessment",),
                     ("current_printing_fact",), ("approved_delta",)):
            self.reject(self.changed(path + ("unexpected",), True))

    def test_type_confusion_rehashed(self):
        for path in (("approved_delta", "quantity"), ("current_printing_fact", "title_id"),
                     ("current_validation_context", "rules", "copy_limit")):
            for value in (True, 1.0, "1"):
                with self.subTest(path=path, value=value):
                    self.reject(self.changed(path, value, rebind=True))
        for value in (0, 0.0, "false"):
            self.reject(self.changed(("resource_assessment", "spending_authorized"), value, rebind=True))
        self.reject(self.changed(("fresh_validation", "valid"), 1, rebind=True))
        self.reject(self.changed(("limitations",), tuple(self.artifact["limitations"])))

    def test_current_context_rejects_noncanonical_or_malformed(self):
        for path, value in (
            (("mode",), "unsupported"), (("rules", "max_main"), -1),
            (("format", "legal_sets"), ["AAA", "AAA"]),
            (("format", "individual_card_quotas"), [[1, 1], [1, 1]]),
            (("format", "banned_title_ids"), [True]),
            (("format", "color_restrictions_internal"), "bad"),
        ):
            self.reject(self.changed(("current_validation_context",) + path, value, rebind=True))

    def test_declined_negative_and_forbidden_later_evidence(self):
        decision = build_human_proposal_decision(self.fixture.presentation, decision_spec("declined"))
        value = self.fixture.evaluate(human_decision=decision)
        self.verify(value)
        for field in ("approved_delta", "fresh_validation", "pre_execution_revalidation_identity"):
            changed = deepcopy(value)
            changed[field] = deepcopy(self.artifact[field])
            self.reject(changed)

    def test_source_rejections(self):
        for decision in (None, {}, {**self.fixture.decision, "human_proposal_decision_model_version": "2"},
                         {**self.fixture.decision, "proposal_identity": {}}):
            self.verify(self.fixture.evaluate(human_decision=decision))

    def test_malformed_input_stages(self):
        for overrides in ({"current_baseline_deck": None}, {"format": None},
                          {"collection": {}}, {"inventory": {}},
                          {"rules": replace(self.fixture.rules, copy_limit=0)}):
            self.verify(self.fixture.evaluate(**overrides))

    def test_baseline_mismatch_diagnostic(self):
        value = self.fixture.evaluate(current_baseline_deck=Deck())
        self.verify(value)
        value["evidence"]["current_digest"] = "0" * 64
        self.reject(value)

    def test_result_mismatch_diagnostic(self):
        with patch(SERVICE + "build_deck_snapshot_identity", side_effect=[
            build_deck_snapshot_identity(self.fixture.deck), build_deck_snapshot_identity(Deck()),
        ]):
            value = self.fixture.evaluate()
        self.verify(value)
        value["reconstructed_result_deck_identity"] = deepcopy(self.artifact["reconstructed_result_deck_identity"])
        self.reject(value)

    def test_validation_unavailable_before_and_after_printing(self):
        self.verify(self.fixture.evaluate(con=None))
        with patch(SERVICE + "validate_deck", side_effect=ValueError("unavailable")):
            value = self.fixture.evaluate()
        self.verify(value)

    def test_printing_unavailable(self):
        self.fixture.con.execute("DELETE FROM printings WHERE arena_id = 101")
        self.verify(self.fixture.evaluate())

    def test_printing_title_mismatch(self):
        self.fixture.con.execute("UPDATE printings SET title_id = 2 WHERE arena_id = 101")
        value = self.fixture.evaluate()
        self.verify(value)
        value["evidence"]["current_title_id"] = True
        self.reject(value)

    def test_validation_failed_all_modes(self):
        for mode in OperatingMode:
            value = self.fixture.evaluate(mode=mode, rules=replace(self.fixture.rules, min_main=60))
            self.verify(value)
            self.assertEqual(value["reason"], "current_validation_failed")

    def test_affordable_spend_remains_negative(self):
        value = self.fixture.budget(collection=Collection({}))
        self.verify(value)
        self.assertEqual(value["reason"], "resource_authorization_required")
        value["status"], value["reason"] = "revalidated", "fresh_validation_passed"
        self.reject(value)

    def test_canonical_warnings_and_errors(self):
        a, b = ValidationIssue("a", "First"), ValidationIssue("b", "Second")
        for report in (ValidationReport(warnings=(b, a)), ValidationReport(errors=(b, a))):
            with patch(SERVICE + "validate_deck", return_value=report):
                value = self.fixture.evaluate()
            self.verify(value)
            key = "warnings" if report.valid else "errors"
            value["fresh_validation"][key].reverse()
            self.reject(value)

    def test_reconstructs_each_approved_zone(self):
        for zone in ("main", "sideboard", "commander"):
            proposal = self.fixture.fixture.accepted(target=zone)
            decision = build_human_proposal_decision(build_proposal_presentation(proposal), decision_spec())
            self.verify(self.fixture.evaluate(human_decision=decision))

    def test_negative_stages_reject_extra_evidence_and_identity(self):
        negatives = [self.fixture.evaluate(human_decision={}),
                     self.fixture.evaluate(current_baseline_deck=None),
                     self.fixture.evaluate(current_baseline_deck=Deck()),
                     self.fixture.evaluate(format=None), self.fixture.evaluate(con=None),
                     self.fixture.budget(collection=Collection({})),
                     self.fixture.budget(inventory=None)]
        for value in negatives:
            for key in ("pre_execution_revalidation_identity", "fresh_validation", "current_printing_fact"):
                if value[key] is None:
                    if key == "current_printing_fact" and value["reason"] == "validation_unavailable":
                        continue  # This outcome legitimately occurs before or after lookup.
                    changed = deepcopy(value)
                    changed[key] = deepcopy(self.artifact[key])
                    self.reject(changed)

    def test_missing_fields_and_nonjson_shapes(self):
        for key in self.artifact:
            value = deepcopy(self.artifact)
            del value[key]
            self.reject(value)
        for value in (None, [], "artifact", {"reason": []}):
            with self.assertRaises(ValueError):
                require_pre_execution_revalidation(value)


if __name__ == "__main__":
    unittest.main()
