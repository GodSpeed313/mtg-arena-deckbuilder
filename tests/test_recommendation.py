from __future__ import annotations

from copy import deepcopy
from unittest.mock import patch
import unittest

from services.candidate_ordering import build_candidate_ordering
from services.recommendation import (
    RECOMMENDATION_DECISION_MODEL_VERSION, build_recommendation_decisions,
)
from tests.test_candidate_comparison import candidate_facts
from tests.test_candidate_ordering import inputs, pool as ordering_pool
from tests.test_preference_policy import rule
from tests.test_strategic_fit import set_title_eligibility


def ordering(rules=None, *, facts=None, zones=None):
    return build_candidate_ordering(*inputs(rules, facts=facts, zones=zones))


def decision(result, zone="main", finding="need.lifegain.enabler.v1"):
    return next(item for item in result["decisions"]
                if item["need_key"]["zone"] == zone
                and item["need_key"]["finding_id"] == finding)


def make_indeterminate(model, a, b):
    row = ordering_pool(model)
    item = next(pair for pair in row["pairwise_results"]
                if {pair["left"]["title_id"], pair["right"]["title_id"]} == {a, b})
    item["status"] = "indeterminate"
    item["preceding_title_id"] = None
    item["rule_trace"][-1]["outcome"] = "indeterminate"
    item["rule_trace"][-1]["left_fact"]["fact_status"] = "unknown"
    item["rule_trace"][-1]["left_fact"]["value"] = None
    row["ordered_groups"] = None
    row["status"] = "indeterminate"


class RecommendationDecisionTests(unittest.TestCase):
    def test_clear_two_and_three_candidate_precedence(self):
        model = ordering([rule()])
        result = build_recommendation_decisions(model)
        life = decision(result)
        self.assertEqual(RECOMMENDATION_DECISION_MODEL_VERSION, "1")
        self.assertEqual(life["outcome"], "recommendable")
        self.assertEqual(life["candidate"]["title_id"], 1)
        self.assertEqual(life["ordered_first_group"], [1])
        self.assertEqual(life["considered_candidate_count"], 3)
        self.assertEqual(len(life["relation_evidence"]), 2)
        two = deepcopy(model)
        row = ordering_pool(two)
        row["serialized_candidates"] = row["serialized_candidates"][:2]
        row["pairwise_results"] = [item for item in row["pairwise_results"]
                                   if 3 not in {item["left"]["title_id"],
                                                item["right"]["title_id"]}]
        row["ordered_groups"] = [[1], [2]]
        self.assertEqual(decision(build_recommendation_decisions(two))["candidate"]["title_id"], 1)

    def test_unique_first_with_lower_tie(self):
        preference = rule("r.packages", "criterion.package.count.v1", "prefer_higher")
        result = decision(build_recommendation_decisions(ordering([preference])))
        self.assertEqual(result["outcome"], "recommendable")
        self.assertEqual(result["ordered_first_group"], [1])
        self.assertEqual(result["candidate"]["title_id"], 1)

    def test_top_tie_blocks_recommendation(self):
        preference = rule("r.activated", "criterion.support.activated_presence.v1",
                          "prefer_absent")
        model = ordering([preference])
        self.assertEqual(ordering_pool(model)["ordered_groups"][0], [1, 2])
        result = decision(build_recommendation_decisions(model))
        self.assertEqual(result["outcome"], "top_tie")
        self.assertIsNone(result["candidate"])
        self.assertEqual(result["ordered_first_group"], [1, 2])

    def test_all_tied_and_serialization_never_selects_first(self):
        preference = rule("r.life", "criterion.package.named_presence.v1",
                          "prefer_present", prerequisite={"package_id": "package.lifegain.v1"})
        model = ordering([preference])
        self.assertEqual(ordering_pool(model)["status"], "tied")
        result = decision(build_recommendation_decisions(model))
        self.assertEqual(result["outcome"], "top_tie")
        self.assertIsNone(result["candidate"])
        self.assertEqual(ordering_pool(model)["serialized_candidates"][0]["title_id"], 1)

    def test_indeterminate_relation_that_contests_first_blocks(self):
        model = ordering([rule()])
        make_indeterminate(model, 1, 2)
        result = decision(build_recommendation_decisions(model))
        self.assertEqual(result["outcome"], "indeterminate_ordering")
        self.assertIsNone(result["candidate"])
        self.assertTrue(any(item["status"] == "indeterminate"
                            for item in result["relation_evidence"]))

    def test_indeterminate_relation_below_unique_first_does_not_block(self):
        model = ordering([rule()])
        make_indeterminate(model, 2, 3)
        result = decision(build_recommendation_decisions(model))
        self.assertEqual(result["ordering_status"], "indeterminate")
        self.assertEqual(result["outcome"], "recommendable")
        self.assertEqual(result["candidate"]["title_id"], 1)
        self.assertTrue(all(item["status"] == "precedes"
                            for item in result["relation_evidence"]))

    def test_inconsistent_pair_cycle_has_explicit_negative_reason(self):
        model = ordering([rule()])
        row = ordering_pool(model)
        item = next(pair for pair in row["pairwise_results"]
                    if {pair["left"]["title_id"], pair["right"]["title_id"]} == {1, 2})
        item["rule_trace"][-1]["direction"] = "prefer_higher"
        item["rule_trace"][-1]["outcome"] = "right_precedes"
        item["preceding_title_id"] = 2
        row["status"] = "indeterminate"
        row["ordered_groups"] = None
        result = decision(build_recommendation_decisions(model))
        self.assertEqual(result["outcome"], "inconsistent_ordering")
        self.assertIsNone(result["candidate"])

    def test_no_candidates_and_single_candidate(self):
        model = ordering([rule()])
        single = decision(build_recommendation_decisions(model),
                          finding="need.spell_cast.enabler.v1")
        self.assertEqual(single["outcome"], "single_candidate_no_preference")
        self.assertIsNone(single["candidate"])
        empty = deepcopy(model)
        row = ordering_pool(empty, finding="need.spell_cast.enabler.v1")
        row["serialized_candidates"] = []
        result = decision(build_recommendation_decisions(empty),
                          finding="need.spell_cast.enabler.v1")
        self.assertEqual(result["outcome"], "no_candidates")

    def test_empty_policy_and_policy_not_applicable(self):
        empty = decision(build_recommendation_decisions(ordering()))
        self.assertEqual(empty["outcome"], "no_declared_preference")
        self.assertIsNone(empty["candidate"])
        scoped = decision(build_recommendation_decisions(ordering([rule()], zones=["sideboard"])))
        self.assertEqual(scoped["outcome"], "policy_not_applicable")

    def test_winning_candidate_with_unresolved_eligibility_is_blocked(self):
        facts = candidate_facts()
        set_title_eligibility(facts, 1, unresolved=["format_legality"])
        result = decision(build_recommendation_decisions(ordering([rule()], facts=facts)))
        self.assertEqual(result["outcome"], "unresolved_eligibility")
        self.assertIsNone(result["candidate"])
        self.assertEqual(result["blocked_candidate"]["title_id"], 1)
        self.assertEqual(result["unresolved_conditions"], ["format_legality"])

    def test_opposite_explicit_policies_change_decision(self):
        lower = decision(build_recommendation_decisions(ordering([rule(direction="prefer_lower")])))
        higher = decision(build_recommendation_decisions(ordering([rule(direction="prefer_higher")])))
        self.assertEqual(lower["candidate"]["title_id"], 1)
        self.assertEqual(higher["candidate"]["title_id"], 2)

    def test_positive_and_negative_provenance(self):
        positive = decision(build_recommendation_decisions(ordering([rule()])))
        self.assertEqual(positive["reason"], "unique_first_under_explicit_policy")
        self.assertEqual(positive["policy_id"], "policy.test.v1")
        self.assertEqual(positive["ordering_model_version"], "1")
        self.assertIn("need_matrices[need_key=main|", positive["source_need_reference"])
        self.assertEqual({item["decisive_policy_rule_id"]
                          for item in positive["relation_evidence"]},
                         {"policy.rule.mana.v1"})
        for item in positive["relation_evidence"]:
            self.assertEqual(item["rule_trace"][-1]["criterion_id"], "criterion.mana.value.v1")
            self.assertIn("source_path", item["rule_trace"][-1]["left_fact"])
        negative = decision(build_recommendation_decisions(ordering()))
        self.assertEqual(negative["reason"], "no_declared_preference")
        self.assertEqual(negative["relation_evidence"], [])

    def test_multiple_needs_are_independent(self):
        result = build_recommendation_decisions(ordering([rule()]))
        self.assertEqual(len(result["decisions"]), 3)
        self.assertEqual(decision(result)["outcome"], "recommendable")
        self.assertEqual(decision(result, "sideboard")["outcome"],
                         "single_candidate_no_preference")
        self.assertEqual(decision(result, finding="need.spell_cast.enabler.v1")["outcome"],
                         "single_candidate_no_preference")

    def test_malformed_versions_and_candidate_identity_fail_closed(self):
        original = ordering([rule()])
        for field in ("candidate_ordering_model_version",
                      "source_candidate_comparison_model_version",
                      "source_strategic_fit_model_version",
                      "source_strategic_preference_policy_model_version"):
            bad = deepcopy(original)
            bad[field] = "2"
            with self.subTest(field=field), self.assertRaises(ValueError):
                build_recommendation_decisions(bad)
        bad = deepcopy(original)
        ordering_pool(bad)["serialized_candidates"][1]["title_id"] = 1
        with self.assertRaises(ValueError):
            build_recommendation_decisions(bad)
        bad = deepcopy(original)
        ordering_pool(bad)["pairwise_results"][0]["right"]["name"] = "Other"
        with self.assertRaises(ValueError):
            build_recommendation_decisions(bad)

    def test_malformed_relation_group_and_trace_fail_closed(self):
        original = ordering([rule()])
        mutations = []
        bad = deepcopy(original)
        ordering_pool(bad)["pairwise_results"].pop()
        mutations.append(bad)
        bad = deepcopy(original)
        ordering_pool(bad)["ordered_groups"] = [[1], [2], [3]]
        mutations.append(bad)
        bad = deepcopy(original)
        ordering_pool(bad)["pairwise_results"][0]["rule_trace"][-1]["outcome"] = "equal"
        mutations.append(bad)
        bad = deepcopy(original)
        ordering_pool(bad)["pairwise_results"][0]["rule_trace"][-1]["direction"] = "prefer_higher"
        mutations.append(bad)
        bad = deepcopy(original)
        ordering_pool(bad)["pairwise_results"][0]["preceding_title_id"] = 999
        mutations.append(bad)
        bad = deepcopy(original)
        ordering_pool(bad)["pairwise_results"][0]["need_key"]["zone"] = "sideboard"
        mutations.append(bad)
        for index, bad in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(ValueError):
                build_recommendation_decisions(bad)

    def test_deterministic_read_only_and_no_upstream_recomputation(self):
        model = ordering([rule()])
        before = deepcopy(model)
        with patch("sqlite3.connect", side_effect=AssertionError("database access")), \
             patch("services.candidate_ordering.build_candidate_ordering",
                   side_effect=AssertionError("ordering recomputed")), \
             patch("services.candidates.discover_candidates",
                   side_effect=AssertionError("candidate rediscovery")):
            first = build_recommendation_decisions(model)
            self.assertEqual(first, build_recommendation_decisions(model))
        self.assertEqual(model, before)

    def test_no_scoring_or_deck_change_fields(self):
        result = build_recommendation_decisions(ordering([rule()]))
        forbidden = {"score", "weight", "confidence", "utility", "points", "rank",
                     "add", "remove", "quantity", "replacement", "mutated_deck", "deck"}
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
        self.assertTrue(keys.isdisjoint(forbidden))


if __name__ == "__main__":
    unittest.main()
