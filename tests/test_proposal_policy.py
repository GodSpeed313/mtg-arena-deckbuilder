from __future__ import annotations

from copy import deepcopy
import json
from unittest.mock import patch
import unittest

from services.proposal_policy import (
    PROPOSAL_POLICY_MODEL_VERSION, build_proposal_policy,
)


def policy():
    return {
        "policy_id": "proposal.test.v1",
        "policy_source": {
            "kind": "explicit_user",
            "provenance": {"reference": "test declaration"},
        },
        "need_key": {
            "zone": "main",
            "finding_id": "need.lifegain.enabler.v1",
            "dependency_id": "dependency.lifegain.v1",
        },
        "recommendation_requirement": "recommendable",
        "candidate_pool_requirement": "complete_only",
        "eligibility_requirement": "resolved",
        "printing_requirement": "single_eligible_printing",
        "operation": "add_only",
        "quantity": 1,
        "target_zone": "sideboard",
        "resource_mode": "unlimited",
    }


class ProposalPolicyTests(unittest.TestCase):
    def test_valid_explicit_policy_and_closed_v2_vocabulary(self):
        result = build_proposal_policy(policy())
        self.assertEqual(PROPOSAL_POLICY_MODEL_VERSION, "2")
        self.assertEqual(result["proposal_policy_model_version"], "2")
        self.assertEqual(result["required_recommendation_context_model_version"], "2")
        self.assertEqual(result["recommendation_requirement"], "recommendable")
        self.assertEqual(result["candidate_pool_requirement"], "complete_only")
        self.assertEqual(result["eligibility_requirement"], "resolved")
        self.assertEqual(result["printing_requirement"], "single_eligible_printing")
        self.assertEqual(result["operation"], "add_only")
        self.assertEqual(result["quantity"], 1)
        self.assertEqual(result["target_zone"], "sideboard")
        self.assertEqual(result["resource_mode"], "unlimited")
        self.assertEqual(result["policy_source"], policy()["policy_source"])

    def test_empty_policy_and_all_missing_fields_have_no_authority(self):
        with self.assertRaises(ValueError):
            build_proposal_policy({})
        for field in policy():
            with self.subTest(field=field):
                spec = policy()
                del spec[field]
                with self.assertRaises(ValueError):
                    build_proposal_policy(spec)

    def test_only_explicit_source_and_exact_provenance_are_accepted(self):
        for kind in ("inferred_from_deck", "inferred_from_recommendation", "auto", ""):
            with self.subTest(kind=kind):
                spec = policy()
                spec["policy_source"]["kind"] = kind
                with self.assertRaises(ValueError):
                    build_proposal_policy(spec)
        spec = policy()
        spec["policy_source"]["kind"] = "explicit_operator_profile"
        self.assertEqual(build_proposal_policy(spec)["policy_source"]["kind"],
                         "explicit_operator_profile")
        for bad in ({}, {"reference": ""}, {"reference": "   "},
                    {"reference": "test", "source": "deck"}, "test"):
            with self.subTest(provenance=bad):
                spec = policy()
                spec["policy_source"]["provenance"] = bad
                with self.assertRaises(ValueError):
                    build_proposal_policy(spec)
        spec = policy()
        spec["policy_source"]["deck_inferred"] = True
        with self.assertRaises(ValueError):
            build_proposal_policy(spec)

    def test_one_explicit_need_and_independent_explicit_target_zone(self):
        result = build_proposal_policy(policy())
        self.assertEqual(result["need_key"], policy()["need_key"])
        self.assertEqual(result["need_key"]["zone"], "main")
        self.assertEqual(result["target_zone"], "sideboard")
        for zone in ("main", "sideboard", "commander"):
            spec = policy()
            spec["target_zone"] = zone
            self.assertEqual(build_proposal_policy(spec)["target_zone"], zone)
        for bad in (None, "", "command", "graveyard", 1):
            with self.subTest(zone=bad):
                spec = policy()
                spec["target_zone"] = bad
                with self.assertRaises(ValueError):
                    build_proposal_policy(spec)
        for bad in ("graveyard", "", None):
            spec = policy()
            spec["need_key"]["zone"] = bad
            with self.assertRaises(ValueError):
                build_proposal_policy(spec)
        spec = policy()
        spec["need_key"] = [policy()["need_key"], policy()["need_key"]]
        with self.assertRaises(ValueError):
            build_proposal_policy(spec)

    def test_operation_and_quantity_are_explicit_and_fixed(self):
        for operation in ("swap", "remove", "replace", "add_then_remove", ""):
            with self.subTest(operation=operation):
                spec = policy()
                spec["operation"] = operation
                with self.assertRaises(ValueError):
                    build_proposal_policy(spec)
        for quantity in (0, 2, 4, -1, True, 1.0, "1", None):
            with self.subTest(quantity=quantity):
                spec = policy()
                spec["quantity"] = quantity
                with self.assertRaises(ValueError):
                    build_proposal_policy(spec)

    def test_no_truncation_printing_tiebreak_or_negative_recommendation_route(self):
        alternatives = {
            "recommendation_requirement": (
                "top_tie", "indeterminate_ordering", "inconsistent_ordering",
                "no_declared_preference", "policy_not_applicable", "no_candidates",
                "single_candidate_no_preference", "unresolved_eligibility", "any",
            ),
            "candidate_pool_requirement": ("allow_truncated", "any", "truncated"),
            "eligibility_requirement": ("allow_unresolved", "unknown"),
            "printing_requirement": ("first_eligible", "lowest_arena_id", "any"),
        }
        for field, values in alternatives.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    spec = policy()
                    spec[field] = value
                    with self.assertRaises(ValueError):
                        build_proposal_policy(spec)

    def test_only_unlimited_mode_and_no_crafting_or_spending_fields(self):
        for mode in ("full_collection", "owned_only", "wildcard_budget", "crafting", ""):
            with self.subTest(mode=mode):
                spec = policy()
                spec["resource_mode"] = mode
                with self.assertRaises(ValueError):
                    build_proposal_policy(spec)
        for field in ("crafting", "spend_wildcards", "wildcard_budget", "collection"):
            spec = policy()
            spec[field] = True
            with self.assertRaises(ValueError):
                build_proposal_policy(spec)

    def test_unknown_action_fields_and_version_override_are_rejected(self):
        for field in (
            "remove_card", "remove_quantity", "replacement_pair", "selected_printing_id",
            "proposed_deck", "deck_mutation", "validator_result", "readiness_score",
            "proposal_policy_model_version", "rules", "scope", "target_zones",
        ):
            with self.subTest(field=field):
                spec = policy()
                spec[field] = "unsupported"
                with self.assertRaises(ValueError):
                    build_proposal_policy(spec)
        spec = policy()
        spec["need_key"]["title_id"] = 1
        with self.assertRaises(ValueError):
            build_proposal_policy(spec)

    def test_deterministic_serialization_input_nonmutation_and_no_recomputation(self):
        spec = policy()
        original = deepcopy(spec)
        with patch("sqlite3.connect", side_effect=AssertionError("database access")), \
             patch("services.validator.validate_deck",
                   side_effect=AssertionError("deck validation")), \
             patch("services.candidates.discover_candidates",
                   side_effect=AssertionError("candidate rediscovery")), \
             patch("services.candidate_ordering.build_candidate_ordering",
                   side_effect=AssertionError("strategic reordering")), \
             patch("services.recommendation_context.build_recommendation_context",
                   side_effect=AssertionError("context recomputation")):
            first = build_proposal_policy(spec)
            self.assertEqual(json.dumps(first), json.dumps(build_proposal_policy(spec)))
        self.assertEqual(spec, original)
        self.assertTrue(set(first).isdisjoint({
            "deck", "proposed_deck", "selected_printing_id", "remove_card",
            "replacement_pair", "crafting_decision", "validation_result",
        }))
        self.assertIn("future builder", " ".join(first["limitations"]).casefold())


if __name__ == "__main__":
    unittest.main()
