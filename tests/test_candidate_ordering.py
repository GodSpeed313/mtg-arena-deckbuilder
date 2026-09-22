from __future__ import annotations

from copy import deepcopy
from unittest.mock import patch
import unittest

from services.candidate_comparison import build_candidate_comparisons
from services.candidate_ordering import (
    CANDIDATE_ORDERING_MODEL_VERSION, build_candidate_ordering,
)
from services.preference_policy import build_preference_policy
from services.strategic_fit import build_strategic_fit_signals
from tests.test_candidate_comparison import candidate_facts
from tests.test_preference_policy import policy, rule
from tests.test_strategic_fit import set_title_eligibility


def inputs(rules=None, *, zones=None, need_keys=None, facts=None):
    comparison = build_candidate_comparisons(facts or candidate_facts())
    fit = build_strategic_fit_signals(comparison)
    preference = build_preference_policy(policy(rules, zones=zones, need_keys=need_keys))
    return comparison, fit, preference


def order(*args):
    return build_candidate_ordering(*args)


def pool(result, zone="main", finding="need.lifegain.enabler.v1"):
    return next(row for row in result["need_orderings"]
                if row["need_key"]["zone"] == zone
                and row["need_key"]["finding_id"] == finding)


def pair(row, a=1, b=2):
    return next(item for item in row["pairwise_results"]
                if {item["left"]["title_id"], item["right"]["title_id"]} == {a, b})


class CandidateOrderingTests(unittest.TestCase):
    def test_opposite_mana_policies_reverse_precedence(self):
        lower = pair(pool(order(*inputs([rule(direction="prefer_lower")]))))
        higher = pair(pool(order(*inputs([rule(direction="prefer_higher")]))))
        self.assertEqual(CANDIDATE_ORDERING_MODEL_VERSION, "1")
        self.assertEqual(lower["preceding_title_id"], 1)
        self.assertEqual(higher["preceding_title_id"], 2)
        self.assertEqual(lower["status"], higher["status"])
        self.assertEqual(lower["status"], "precedes")

    def test_signal_presence_and_named_package(self):
        signal_rule = rule("r.support", "criterion.support.unconditional_presence.v1",
                           "prefer_present")
        signal = pair(pool(order(*inputs([signal_rule]))))
        self.assertEqual(signal["preceding_title_id"], 1)
        self.assertEqual(signal["rule_trace"][0]["left_fact"]["value"], True)
        self.assertEqual(signal["rule_trace"][0]["right_fact"]["value"], False)
        named = rule("r.package", "criterion.package.named_presence.v1",
                     "prefer_present", prerequisite={"package_id": "package.card_advantage.v1"})
        self.assertEqual(pair(pool(order(*inputs([named]))))["preceding_title_id"], 1)
        absent = rule("r.absent", "criterion.support.unconditional_presence.v1",
                      "prefer_absent")
        self.assertEqual(pair(pool(order(*inputs([absent]))))["preceding_title_id"], 2)

    def test_package_count_and_observed_need_count(self):
        for criterion in ("criterion.package.count.v1",
                          "criterion.need.observed_match_count.v1"):
            with self.subTest(criterion=criterion):
                item = pair(pool(order(*inputs([rule("r.count", criterion, "prefer_higher")]))))
                self.assertEqual(item["preceding_title_id"], 1)

    def test_lexicographic_precedence_and_first_decisive_provenance(self):
        rules = [rule("r.mana", direction="prefer_lower"),
                 rule("r.package", "criterion.package.count.v1", "prefer_lower")]
        item = pair(pool(order(*inputs(rules))))
        self.assertEqual(item["decisive_policy_rule_id"], "r.mana")
        self.assertEqual(len(item["rule_trace"]), 1)
        step = item["rule_trace"][0]
        self.assertEqual(step["criterion_id"], "criterion.mana.value.v1")
        self.assertEqual(step["direction"], "prefer_lower")
        self.assertEqual(step["left_fact"]["value"], 2)
        self.assertEqual(step["right_fact"]["value"], 4)
        self.assertEqual(step["source_provenance"], {"reference": "declaration:r.mana"})
        self.assertEqual(item["need_key"]["zone"], "main")
        self.assertEqual(item["policy_id"], "policy.test.v1")

    def test_exact_tie_survives_canonical_serialization(self):
        r = rule("r.life", "criterion.package.named_presence.v1", "prefer_present",
                 prerequisite={"package_id": "package.lifegain.v1"})
        result = pool(order(*inputs([r])))
        self.assertEqual(pair(result)["status"], "tie")
        self.assertIsNone(pair(result)["preceding_title_id"])
        self.assertEqual([item["title_id"] for item in result["serialized_candidates"]], [1, 2, 3])
        self.assertEqual(result["status"], "tied")

    def test_unknown_equal_continues_and_indeterminate_stops(self):
        facts = candidate_facts()
        for row in facts["candidate_facts_by_title"]:
            if row["title_id"] == 1:
                row["canonical_facts"]["mana_value"] = None
        for source in facts["per_need"]:
            for candidate in source["candidates"]:
                if candidate["title_id"] == 1:
                    candidate["canonical_facts"]["mana_value"] = None
        second = rule("r.package", "criterion.package.count.v1", "prefer_higher")
        equal = rule("r.mana", unknown_behavior="equal_for_this_rule")
        item = pair(pool(order(*inputs([equal, second], facts=facts))))
        self.assertEqual(item["preceding_title_id"], 1)
        self.assertEqual([step["outcome"] for step in item["rule_trace"]],
                         ["equal_for_this_rule", "left_precedes"])
        unknown = rule("r.mana", unknown_behavior="indeterminate")
        item = pair(pool(order(*inputs([unknown, second], facts=facts))))
        self.assertEqual(item["status"], "indeterminate")
        self.assertIsNone(item["preceding_title_id"])
        self.assertEqual(len(item["rule_trace"]), 1)
        self.assertEqual(item["rule_trace"][0]["left_fact"]["fact_status"], "unknown")
        self.assertIsNone(item["rule_trace"][0]["left_fact"]["value"])

    def test_unknown_skip_can_make_pool_order_indeterminate_without_erasing_pairs(self):
        facts = candidate_facts()
        alpha = facts["candidate_facts_by_title"][0]
        alpha["canonical_facts"]["mana_value"] = None
        for source in facts["per_need"]:
            for candidate in source["candidates"]:
                if candidate["title_id"] == 1:
                    candidate["canonical_facts"]["mana_value"] = None
        # All titles have the named package, so Alpha ties each known-mana
        # title after skipping its unknown mana value. Beta and Gamma differ.
        rules = [rule("r.mana", unknown_behavior="equal_for_this_rule"),
                 rule("r.life", "criterion.package.named_presence.v1", "prefer_present",
                      prerequisite={"package_id": "package.lifegain.v1"})]
        result = pool(order(*inputs(rules, facts=facts)))
        self.assertEqual(result["status"], "indeterminate")
        self.assertIsNone(result["ordered_groups"])
        self.assertEqual(pair(result, 1, 2)["status"], "tie")
        self.assertEqual(pair(result, 1, 3)["status"], "tie")
        self.assertEqual(pair(result, 2, 3)["preceding_title_id"], 3)

    def test_all_reviewed_signal_criteria_use_exact_signal_presence(self):
        signal_criteria = [
            "criterion.support.prerequisites_presence.v1",
            "criterion.support.conditional_presence.v1",
            "criterion.support.triggered_presence.v1",
            "criterion.support.activated_presence.v1",
            "criterion.support.partial_presence.v1",
            "criterion.support.unsupported_remainder_presence.v1",
            "criterion.eligibility.unresolved_presence.v1",
            "criterion.ownership.known_presence.v1",
            "criterion.copy.finite_capacity_presence.v1",
            "criterion.copy.unlimited_presence.v1",
        ]
        for criterion in signal_criteria:
            with self.subTest(criterion=criterion):
                result = pool(order(*inputs([rule("r.signal", criterion, "prefer_present")])))
                item = pair(result)
                self.assertEqual(item["rule_trace"][0]["left_fact"]["fact_status"], "known")
                self.assertEqual(item["rule_trace"][0]["right_fact"]["fact_status"], "known")
                self.assertIn(item["status"], {"precedes", "tie"})

    def test_empty_policy_and_scope(self):
        result = order(*inputs())
        life = pool(result)
        self.assertEqual(life["status"], "no_declared_preference")
        self.assertEqual(life["pairwise_results"], [])
        self.assertIsNone(life["ordered_groups"])
        item = rule()
        zoned = order(*inputs([item], zones=["sideboard"]))
        self.assertEqual(pool(zoned)["status"], "policy_not_applicable")
        self.assertEqual(pool(zoned, "sideboard")["status"], "insufficient_candidates")
        scoped_key = deepcopy(zoned["need_orderings"][0]["need_key"])
        scoped = order(*inputs([item], need_keys=[scoped_key]))
        self.assertEqual(pool(scoped)["status"], "strategically_ordered")
        self.assertEqual(pool(scoped, "sideboard")["status"], "policy_not_applicable")

    def test_unresolved_eligibility_is_preserved_as_recorded(self):
        facts = candidate_facts()
        set_title_eligibility(facts, 1, unresolved=["format_legality"])
        result = pool(order(*inputs([rule()], facts=facts)))
        alpha = result["serialized_candidates"][0]
        self.assertEqual(alpha["eligibility_status"], "eligibility_unknown")
        self.assertEqual(alpha["unresolved_eligibility"], ["format_legality"])
        self.assertEqual(pair(result)["preceding_title_id"], 1)

    def test_no_cross_need_pairs_and_ordered_groups(self):
        result = order(*inputs([rule()]))
        life = pool(result)
        self.assertEqual(life["ordered_groups"], [[1], [3], [2]])
        self.assertEqual(len(life["pairwise_results"]), 3)
        self.assertEqual(pool(result, finding="need.spell_cast.enabler.v1")["pairwise_results"], [])
        self.assertEqual(pool(result, finding="need.spell_cast.enabler.v1")["status"],
                         "insufficient_candidates")
        self.assertEqual(pool(result, "sideboard")["pairwise_results"], [])

    def test_malformed_versions_identity_and_eligibility_rejected(self):
        c, f, p = inputs([rule()])
        bad = deepcopy(c)
        bad["candidate_comparison_model_version"] = "2"
        with self.assertRaises(ValueError):
            order(bad, f, p)
        bad = deepcopy(f)
        bad["strategic_fit_model_version"] = "2"
        with self.assertRaises(ValueError):
            order(c, bad, p)
        bad = deepcopy(p)
        bad["strategic_preference_policy_model_version"] = "2"
        with self.assertRaises(ValueError):
            order(c, f, bad)
        bad = deepcopy(p)
        bad["rules"][0]["criterion_source"]["field_path"] = "title_index.secret"
        with self.assertRaises(ValueError):
            order(c, f, bad)
        bad = deepcopy(f)
        bad["need_signal_sets"][0]["candidates"][0]["title_id"] = 999
        with self.assertRaises(ValueError):
            order(c, bad, p)
        bad = deepcopy(c)
        bad["title_index"][0]["eligibility_status"] = "ineligible"
        with self.assertRaises(ValueError):
            order(bad, f, p)

    def test_inputs_unchanged_deterministic_and_no_database(self):
        arguments = inputs([rule()])
        original = deepcopy(arguments)
        with patch("sqlite3.connect", side_effect=AssertionError("database access")):
            first = order(*arguments)
            self.assertEqual(first, order(*arguments))
        self.assertEqual(arguments, original)
        self.assertEqual(first["candidate_ordering_model_version"], "1")
        self.assertEqual(first["source_strategic_preference_policy_model_version"], "1")

    def test_no_hidden_decision_or_deck_fields(self):
        result = order(*inputs([rule()]))
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
        self.assertTrue(keys.isdisjoint({"score", "weight", "utility", "points", "rank",
                                         "winner", "recommendation", "replacement", "quantity",
                                         "deck", "mutated_deck"}))


if __name__ == "__main__":
    unittest.main()
