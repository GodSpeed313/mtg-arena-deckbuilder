from __future__ import annotations

from copy import deepcopy
from unittest.mock import patch
import unittest

from services.candidate_ordering import build_candidate_ordering
from services.recommendation import build_recommendation_decisions
from services.recommendation_context import (
    RECOMMENDATION_CONTEXT_MODEL_VERSION, build_recommendation_context,
)
from tests.test_candidate_comparison import candidate_facts
from tests.test_candidate_ordering import inputs, pool as ordering_pool
from tests.test_preference_policy import rule
from tests.test_recommendation import make_indeterminate
from tests.test_strategic_fit import set_title_eligibility


def models(rules=None, *, facts=None, zones=None):
    comparison, fit, policy = inputs(rules, facts=facts, zones=zones)
    ordering = build_candidate_ordering(comparison, fit, policy)
    return build_recommendation_decisions(ordering), comparison


def context(result, zone="main", finding="need.lifegain.enabler.v1"):
    return next(item for item in result["contexts"]
                if item["need_key"]["zone"] == zone
                and item["need_key"]["finding_id"] == finding)


def candidate(row, title_id):
    return next(item for item in row["returned_candidate_facts"]
                if item["title_id"] == title_id)


def set_eligible_ids(facts, title_id, ids):
    for title in facts["candidate_facts_by_title"]:
        if title["title_id"] == title_id:
            title["eligibility"]["format_legality"]["eligible_printing_ids"] = list(ids)
    for pool in facts["per_need"]:
        for item in pool["candidates"]:
            if item["title_id"] == title_id:
                item["eligibility"]["format_legality"]["eligible_printing_ids"] = list(ids)


def set_ownership(facts, title_id, status, copies):
    for title in facts["candidate_facts_by_title"]:
        if title["title_id"] == title_id:
            title["eligibility"]["ownership"] = {"status": status, "owned_copies": copies}
    for pool in facts["per_need"]:
        for item in pool["candidates"]:
            if item["title_id"] == title_id:
                item["eligibility"]["ownership"] = {"status": status, "owned_copies": copies}


class RecommendationContextTests(unittest.TestCase):
    def test_complete_pool_positive_decision_and_exact_need_evidence(self):
        decisions, comparison = models([rule()])
        result = build_recommendation_context(decisions, comparison)
        row = context(result)
        source = comparison["need_matrices"][0]
        self.assertEqual(RECOMMENDATION_CONTEXT_MODEL_VERSION, "1")
        self.assertEqual(result["recommendation_context_model_version"], "1")
        self.assertEqual(row["decision"], decisions["decisions"][0])
        self.assertEqual(row["decision"]["outcome"], "recommendable")
        self.assertEqual(row["source_need"], source["source_need"])
        self.assertEqual(row["source_pool_summary"], source["source_pool_summary"])
        self.assertEqual(row["source_pool_summary"]["truncated"], False)
        self.assertEqual(row["pool_context_conditions"], [])
        alpha = candidate(row, 1)
        self.assertEqual(alpha["matching_feature_evidence"], source["candidates"][0][
            "matching_feature_evidence"])
        self.assertEqual(alpha["required_feature_eligibility"], source["candidates"][0][
            "required_feature_eligibility"])
        self.assertEqual(result["source_candidate_facts_model_version"], "2")
        self.assertEqual(row["decision"]["policy_source"],
                         decisions["decisions"][0]["policy_source"])

    def test_truncated_pool_preserves_positive_decision_and_scope_limit(self):
        decisions, comparison = models([rule()])
        matrix = comparison["need_matrices"][0]
        matrix["source_pool_summary"]["truncated"] = True
        matrix["source_pool_summary"]["included"] = 4
        row = context(build_recommendation_context(decisions, comparison))
        self.assertEqual(row["decision"]["outcome"], "recommendable")
        self.assertTrue(row["source_pool_summary"]["truncated"])
        self.assertEqual(row["pool_context_conditions"][0]["condition_id"],
                         "candidate_pool_truncated")
        self.assertEqual(row["pool_context_conditions"][0]["evidence"], True)

    def test_single_and_multiple_eligible_printings_preserved_as_sets(self):
        facts = candidate_facts()
        set_eligible_ids(facts, 1, [101])
        decisions, comparison = models([rule()], facts=facts)
        alpha = candidate(context(build_recommendation_context(decisions, comparison)), 1)
        self.assertEqual(alpha["eligible_printing_ids"], [101])
        self.assertNotIn("multiple_eligible_printings",
                         [item["condition_id"] for item in alpha["context_conditions"]])
        facts = candidate_facts()
        set_eligible_ids(facts, 1, [101, 102])
        decisions, comparison = models([rule()], facts=facts)
        alpha = candidate(context(build_recommendation_context(decisions, comparison)), 1)
        self.assertEqual(alpha["eligible_printing_ids"], [101, 102])
        self.assertEqual(alpha["context_conditions"][0]["condition_id"],
                         "multiple_eligible_printings")
        self.assertNotIn("selected_printing_id", alpha)

    def test_ownership_unknown_zero_positive_and_copy_capacity(self):
        decisions, comparison = models([rule()])
        row = context(build_recommendation_context(decisions, comparison))
        alpha, beta, gamma = (candidate(row, item) for item in (1, 2, 3))
        self.assertEqual(alpha["eligibility"]["ownership"],
                         {"status": "unknown", "owned_copies": None})
        self.assertIn("ownership_unknown", [item["condition_id"]
                                            for item in alpha["context_conditions"]])
        self.assertEqual(beta["eligibility"]["ownership"],
                         {"status": "known", "owned_copies": 0})
        self.assertIn("ownership_known_zero", [item["condition_id"]
                                               for item in beta["context_conditions"]])
        self.assertEqual(gamma["eligibility"]["ownership"],
                         {"status": "known", "owned_copies": 2})
        self.assertEqual(alpha["eligibility"]["playset"]["remaining_capacity"], 4)
        self.assertEqual(alpha["eligibility"]["playset"]["status"], "capacity_available")

    def test_unlimited_capacity_is_preserved(self):
        facts = candidate_facts()
        set_title_eligibility(facts, 1, playset={
            "status": "unlimited", "current_deck_copies": 0,
            "copy_limit": None, "remaining_capacity": None,
        })
        decisions, comparison = models([rule()], facts=facts)
        alpha = candidate(context(build_recommendation_context(decisions, comparison)), 1)
        self.assertEqual(alpha["eligibility"]["playset"], {
            "status": "unlimited", "current_deck_copies": 0,
            "copy_limit": None, "remaining_capacity": None,
        })

    def test_finite_zero_capacity_is_rejected_as_upstream_contradiction(self):
        decisions, comparison = models([rule()])
        title = comparison["title_index"][0]
        title["eligibility"]["playset"]["remaining_capacity"] = 0
        with self.assertRaises(ValueError):
            build_recommendation_context(decisions, comparison)

    def test_unresolved_eligibility_and_blocked_decision_preserved(self):
        facts = candidate_facts()
        set_title_eligibility(facts, 1, unresolved=["format_legality"])
        decisions, comparison = models([rule()], facts=facts)
        row = context(build_recommendation_context(decisions, comparison))
        self.assertEqual(row["decision"]["outcome"], "unresolved_eligibility")
        self.assertIsNone(row["decision"]["candidate"])
        self.assertEqual(row["decision"]["blocked_candidate"]["title_id"], 1)
        alpha = candidate(row, 1)
        self.assertEqual(alpha["unresolved_eligibility"], ["format_legality"])
        self.assertIn("unresolved_eligibility", [item["condition_id"]
                                                 for item in alpha["context_conditions"]])

    def test_negative_decisions_remain_negative_without_resurrection(self):
        cases = [
            ("no_declared_preference", models()),
            ("policy_not_applicable", models([rule()], zones=["sideboard"])),
        ]
        tie_rule = rule("r.life", "criterion.package.named_presence.v1", "prefer_present",
                        prerequisite={"package_id": "package.lifegain.v1"})
        cases.append(("top_tie", models([tie_rule])))
        for expected, (decisions, comparison) in cases:
            row = context(build_recommendation_context(decisions, comparison))
            self.assertEqual(row["decision"]["outcome"], expected)
            self.assertIsNone(row["decision"]["candidate"])
            self.assertEqual(len(row["returned_candidate_facts"]), 3)
        decisions, comparison = models([rule()])
        single = context(build_recommendation_context(decisions, comparison),
                         finding="need.spell_cast.enabler.v1")
        self.assertEqual(single["decision"]["outcome"], "single_candidate_no_preference")
        self.assertIsNone(single["decision"]["candidate"])

    def test_indeterminate_inconsistent_and_no_candidates_preserved(self):
        comparison, fit, policy = inputs([rule()])
        ordering = build_candidate_ordering(comparison, fit, policy)
        make_indeterminate(ordering, 1, 2)
        decisions = build_recommendation_decisions(ordering)
        row = context(build_recommendation_context(decisions, comparison))
        self.assertEqual(row["decision"]["outcome"], "indeterminate_ordering")
        self.assertIsNone(row["decision"]["candidate"])
        comparison, fit, policy = inputs([rule()])
        ordering = build_candidate_ordering(comparison, fit, policy)
        pool = ordering_pool(ordering)
        pair = next(item for item in pool["pairwise_results"]
                    if {item["left"]["title_id"], item["right"]["title_id"]} == {1, 2})
        pair["rule_trace"][-1]["direction"] = "prefer_higher"
        pair["rule_trace"][-1]["outcome"] = "right_precedes"
        pair["preceding_title_id"] = 2
        pool["status"] = "indeterminate"
        pool["ordered_groups"] = None
        decisions = build_recommendation_decisions(ordering)
        inconsistent = context(build_recommendation_context(decisions, comparison))
        self.assertEqual(inconsistent["decision"]["outcome"], "inconsistent_ordering")
        self.assertIsNone(inconsistent["decision"]["candidate"])
        decisions, comparison = models([rule()])
        empty_key = next(row["need_key"] for row in decisions["decisions"]
                         if row["need_key"]["finding_id"] == "need.spell_cast.enabler.v1")
        for row in decisions["decisions"]:
            if row["need_key"] == empty_key:
                row["outcome"] = "no_candidates"
                row["reason"] = "no_candidates"
                row["considered_candidate_count"] = 0
        for matrix in comparison["need_matrices"]:
            if matrix["need_key"] == empty_key:
                matrix["candidates"] = []
                matrix["candidate_count"] = 0
                matrix["source_pool_summary"]["returned"] = 0
        empty = context(build_recommendation_context(decisions, comparison),
                        finding="need.spell_cast.enabler.v1")
        self.assertEqual(empty["decision"]["outcome"], "no_candidates")
        self.assertEqual(empty["returned_candidate_facts"], [])

    def test_need_and_recommended_title_mismatch_fail_closed(self):
        decisions, comparison = models([rule()])
        bad = deepcopy(decisions)
        bad["decisions"][0]["need_key"]["finding_id"] = "need.other.v1"
        with self.assertRaises(ValueError):
            build_recommendation_context(bad, comparison)
        bad = deepcopy(decisions)
        bad["decisions"][0]["candidate"]["title_id"] = 999
        with self.assertRaises(ValueError):
            build_recommendation_context(bad, comparison)
        bad = deepcopy(comparison)
        bad["need_matrices"][0]["candidates"][0]["title_id"] = 999
        with self.assertRaises(ValueError):
            build_recommendation_context(decisions, bad)

    def test_versions_duplicates_and_source_references_fail_closed(self):
        decisions, comparison = models([rule()])
        bad = deepcopy(decisions)
        bad["recommendation_decision_model_version"] = "2"
        with self.assertRaises(ValueError):
            build_recommendation_context(bad, comparison)
        bad = deepcopy(comparison)
        bad["candidate_comparison_model_version"] = "2"
        with self.assertRaises(ValueError):
            build_recommendation_context(decisions, bad)
        bad = deepcopy(comparison)
        bad["title_index"].append(deepcopy(bad["title_index"][0]))
        with self.assertRaises(ValueError):
            build_recommendation_context(decisions, bad)
        bad = deepcopy(comparison)
        bad["title_index"][0]["canonical_facts"]["title_id"] = 999
        with self.assertRaises(ValueError):
            build_recommendation_context(decisions, bad)
        bad = deepcopy(comparison)
        bad["need_matrices"][0]["candidates"].append(deepcopy(bad["need_matrices"][0]["candidates"][0]))
        bad["need_matrices"][0]["candidate_count"] += 1
        bad["need_matrices"][0]["source_pool_summary"]["returned"] += 1
        with self.assertRaises(ValueError):
            build_recommendation_context(decisions, bad)
        bad = deepcopy(decisions)
        bad["decisions"][0]["source_need_reference"] = "need_matrices[other].source_need"
        with self.assertRaises(ValueError):
            build_recommendation_context(bad, comparison)

    def test_input_is_unchanged_deterministic_and_no_recomputation(self):
        decisions, comparison = models([rule()])
        before = deepcopy((decisions, comparison))
        with patch("sqlite3.connect", side_effect=AssertionError("database access")), \
             patch("services.candidates.discover_candidates",
                   side_effect=AssertionError("candidate rediscovery")), \
             patch("services.candidate_comparison.build_candidate_comparisons",
                   side_effect=AssertionError("comparison recomputed")), \
             patch("services.recommendation.build_recommendation_decisions",
                   side_effect=AssertionError("decision recomputed")):
            first = build_recommendation_context(decisions, comparison)
            self.assertEqual(first, build_recommendation_context(decisions, comparison))
        self.assertEqual((decisions, comparison), before)

    def test_no_action_fields_or_readiness_score(self):
        result = build_recommendation_context(*models([rule()]))
        keys = set()
        def visit(value):
            if isinstance(value, dict):
                keys.update(value)
                for item in value.values():
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)
        visit(result)
        self.assertTrue(keys.isdisjoint({
            "selected_printing_id", "add_quantity", "remove_quantity",
            "selected_removal_card", "replacement_pair", "crafting_instruction",
            "wildcard_expenditure", "mutated_deck", "proposed_deck",
            "legality_conclusion", "readiness_score", "confidence_score",
        }))


if __name__ == "__main__":
    unittest.main()
