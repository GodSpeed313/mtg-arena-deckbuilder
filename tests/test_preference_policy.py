from __future__ import annotations

from copy import deepcopy
from unittest.mock import patch
import unittest

from services.preference_policy import (
    PREFERENCE_CRITERIA,
    PREFERENCE_RULE_REGISTRY_VERSION,
    STRATEGIC_PREFERENCE_POLICY_MODEL_VERSION,
    build_preference_policy,
)


CRITERIA = {item.criterion_id: item for item in PREFERENCE_CRITERIA}


def criterion_source(criterion_id):
    return CRITERIA[criterion_id].source()


def rule(
    rule_id="policy.rule.mana.v1",
    criterion_id="criterion.mana.value.v1",
    direction="prefer_lower",
    *,
    prerequisite=None,
    unknown_behavior="indeterminate",
):
    return {
        "policy_rule_id": rule_id,
        "criterion_id": criterion_id,
        "criterion_source": criterion_source(criterion_id),
        "prerequisite": deepcopy(prerequisite or {}),
        "behavior": {
            "kind": "lexicographic_order",
            "direction": direction,
        },
        "unknown_behavior": unknown_behavior,
        "explanation": f"Apply the explicit {direction} direction for {criterion_id}.",
        "source_provenance": {"reference": f"declaration:{rule_id}"},
    }


def policy(rules=None, *, source_kind="explicit_user", zones=None, need_keys=None):
    return {
        "policy_id": "policy.test.v1",
        "policy_source": {
            "kind": source_kind,
            "provenance": {"reference": "test declaration"},
        },
        "scope": {
            "zones": deepcopy(zones or []),
            "need_keys": deepcopy(need_keys or []),
        },
        "eligibility_handling": {
            "deterministic_ineligibility": "reject_malformed_input",
            "unresolved_eligibility": "preserve_unresolved",
        },
        "rules": deepcopy(rules or []),
        "serialization": {
            "method": "casefolded_name_then_title_id",
            "non_strategic": True,
        },
    }


class PreferencePolicyTests(unittest.TestCase):
    def test_versions_empty_ruleset_and_no_implicit_default(self):
        result = build_preference_policy(policy())
        self.assertEqual(STRATEGIC_PREFERENCE_POLICY_MODEL_VERSION, "2")
        self.assertEqual(PREFERENCE_RULE_REGISTRY_VERSION, "2")
        self.assertEqual(result["strategic_preference_policy_model_version"], "2")
        self.assertEqual(result["preference_rule_registry_version"], "2")
        self.assertEqual(result["rules"], [])
        self.assertIn("no strategic preference", " ".join(result["limitations"]).casefold())

    def test_registry_is_closed_unique_and_uses_reviewed_sources(self):
        expected = {
            "criterion.need.observed_match_count.v1",
            "criterion.mana.value.v1",
            "criterion.package.named_presence.v1",
            "criterion.package.count.v1",
            "criterion.support.unconditional_presence.v1",
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
        }
        self.assertEqual(set(CRITERIA), expected)
        self.assertEqual(len(CRITERIA), len(PREFERENCE_CRITERIA))
        self.assertTrue(all(
            item.source_model in {"candidate_comparison", "strategic_fit"}
            and item.source_version == "2"
            and item.allowed_directions
            for item in PREFERENCE_CRITERIA
        ))

    def test_explicit_rule_order_is_preserved_as_precedence(self):
        first = rule()
        second = rule(
            "policy.rule.coverage.v1",
            "criterion.need.observed_match_count.v1",
            "prefer_higher",
        )
        result = build_preference_policy(policy([first, second]))
        self.assertEqual(
            [item["policy_rule_id"] for item in result["rules"]],
            ["policy.rule.mana.v1", "policy.rule.coverage.v1"],
        )
        self.assertEqual(result["rules"][0]["behavior"]["kind"], "lexicographic_order")

    def test_two_policies_accept_opposite_mana_directions(self):
        lower = build_preference_policy(policy([rule(direction="prefer_lower")]))
        higher = build_preference_policy(policy([
            rule("policy.rule.mana.higher.v1", direction="prefer_higher")
        ]))
        self.assertEqual(lower["rules"][0]["criterion_id"], higher["rules"][0]["criterion_id"])
        self.assertEqual(lower["rules"][0]["behavior"]["direction"], "prefer_lower")
        self.assertEqual(higher["rules"][0]["behavior"]["direction"], "prefer_higher")

    def test_duplicate_ids_definitions_and_conflicts_are_rejected(self):
        duplicate_id = [rule(), rule()]
        with self.assertRaises(ValueError):
            build_preference_policy(policy(duplicate_id))
        conflict = [rule(), rule("policy.rule.other.v1", direction="prefer_higher")]
        with self.assertRaises(ValueError):
            build_preference_policy(policy(conflict))

    def test_unknown_criterion_is_rejected(self):
        item = rule()
        item["criterion_id"] = "criterion.unknown.v1"
        with self.assertRaises(ValueError):
            build_preference_policy(policy([item]))

    def test_arbitrary_field_path_and_signal_id_are_rejected(self):
        item = rule()
        item["criterion_source"]["field_path"] = "title_index.secret_value"
        with self.assertRaises(ValueError):
            build_preference_policy(policy([item]))
        item = rule(
            "policy.rule.support.v1",
            "criterion.support.unconditional_presence.v1",
            "prefer_present",
        )
        item["criterion_source"]["signal_id"] = "fit.support.invented.v1"
        with self.assertRaises(ValueError):
            build_preference_policy(policy([item]))

    def test_policy_source_and_provenance_are_required(self):
        for source_kind in ("inferred_user", "deck_derived", "built_in"):
            with self.subTest(source_kind=source_kind), self.assertRaises(ValueError):
                build_preference_policy(policy(source_kind=source_kind))
        missing = policy()
        missing["policy_source"]["provenance"] = {}
        with self.assertRaises(ValueError):
            build_preference_policy(missing)
        missing_rule = rule()
        missing_rule["source_provenance"] = {}
        with self.assertRaises(ValueError):
            build_preference_policy(policy([missing_rule]))

    def test_direction_and_behavior_are_explicit_and_bounded(self):
        missing = rule()
        del missing["behavior"]["direction"]
        with self.assertRaises(ValueError):
            build_preference_policy(policy([missing]))
        invalid = rule(direction="prefer_fastest")
        with self.assertRaises(ValueError):
            build_preference_policy(policy([invalid]))
        invalid = rule()
        invalid["behavior"]["kind"] = "weighted_order"
        with self.assertRaises(ValueError):
            build_preference_policy(policy([invalid]))

    def test_unknown_behavior_is_explicit_and_bounded(self):
        for unknown in ("indeterminate", "equal_for_this_rule"):
            result = build_preference_policy(policy([
                rule(unknown_behavior=unknown)
            ]))
            self.assertEqual(result["rules"][0]["unknown_behavior"], unknown)
        missing = rule()
        del missing["unknown_behavior"]
        with self.assertRaises(ValueError):
            build_preference_policy(policy([missing]))
        with self.assertRaises(ValueError):
            build_preference_policy(policy([rule(unknown_behavior="last")]))

    def test_hidden_numeric_and_candidate_decision_fields_are_rejected(self):
        forbidden = {
            "weight": 2,
            "utility": 10,
            "aggregate_score": 4,
            "rank": 1,
            "ordered_candidates": [1, 2],
            "quantity": 3,
        }
        for key, value in forbidden.items():
            spec = policy()
            spec[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                build_preference_policy(spec)

    def test_eligibility_handling_is_separate_and_fixed(self):
        result = build_preference_policy(policy([rule()]))
        self.assertNotIn("eligibility_handling", result["rules"][0])
        self.assertEqual(result["eligibility_handling"], {
            "deterministic_ineligibility": "reject_malformed_input",
            "unresolved_eligibility": "preserve_unresolved",
        })
        for field, value in (
            ("deterministic_ineligibility", "sort_last"),
            ("unresolved_eligibility", "assume_resolved"),
        ):
            spec = policy()
            spec["eligibility_handling"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                build_preference_policy(spec)

    def test_serialization_is_always_non_strategic(self):
        result = build_preference_policy(policy())
        self.assertEqual(result["serialization"], {
            "method": "casefolded_name_then_title_id",
            "non_strategic": True,
        })
        for mutation in (
            {"method": "casefolded_name_then_title_id", "non_strategic": False},
            {"method": "policy_rule_order", "non_strategic": True},
        ):
            spec = policy()
            spec["serialization"] = mutation
            with self.assertRaises(ValueError):
                build_preference_policy(spec)

    def test_zone_and_structured_need_scope_are_normalized(self):
        need_a = {
            "zone": "sideboard", "finding_id": "need.token.enabler.v1",
            "dependency_id": "dependency.token.v1",
        }
        need_b = {
            "zone": "main", "finding_id": "need.lifegain.enabler.v1",
            "dependency_id": "dependency.lifegain.v1",
        }
        result = build_preference_policy(policy(
            zones=["sideboard", "main"], need_keys=[need_a, need_b],
        ))
        self.assertEqual(result["scope"]["zones"], ["main", "sideboard"])
        self.assertEqual(result["scope"]["need_keys"], [need_b, need_a])

    def test_malformed_or_ambiguous_scope_is_rejected(self):
        invalid_keys = [
            {"zone": "main", "finding_id": "need.x"},
            {"zone": "library", "finding_id": "need.x", "dependency_id": "dependency.x"},
            {"zone": "main", "finding_id": "", "dependency_id": "dependency.x"},
        ]
        for need_key in invalid_keys:
            with self.subTest(need_key=need_key), self.assertRaises(ValueError):
                build_preference_policy(policy(need_keys=[need_key]))
        with self.assertRaises(ValueError):
            build_preference_policy(policy(zones=["main", "main"]))
        with self.assertRaises(ValueError):
            build_preference_policy(policy(
                zones=["main"],
                need_keys=[{
                    "zone": "sideboard", "finding_id": "need.x",
                    "dependency_id": "dependency.x",
                }],
            ))

    def test_named_package_criterion_requires_reviewed_package(self):
        package_rule = rule(
            "policy.rule.package.v1", "criterion.package.named_presence.v1",
            "prefer_present", prerequisite={"package_id": "package.lifegain.v1"},
        )
        result = build_preference_policy(policy([package_rule]))
        self.assertEqual(
            result["rules"][0]["prerequisite"],
            {"package_id": "package.lifegain.v1"},
        )
        package_rule["prerequisite"]["package_id"] = "package.invented.v1"
        with self.assertRaises(ValueError):
            build_preference_policy(policy([package_rule]))
        with self.assertRaises(ValueError):
            build_preference_policy(policy([rule(prerequisite={"package_id": "package.lifegain.v1"})]))

    def test_input_is_unchanged_and_unordered_scope_input_is_deterministic(self):
        first = policy(
            [rule()], zones=["sideboard", "main"],
            need_keys=[{
                "zone": "sideboard", "finding_id": "need.b",
                "dependency_id": "dependency.b",
            }, {
                "zone": "main", "finding_id": "need.a",
                "dependency_id": "dependency.a",
            }],
        )
        before = deepcopy(first)
        expected = build_preference_policy(first)
        self.assertEqual(first, before)
        second = deepcopy(first)
        second["scope"]["zones"].reverse()
        second["scope"]["need_keys"].reverse()
        self.assertEqual(build_preference_policy(second), expected)

    def test_builder_has_no_database_or_candidate_dependency(self):
        with patch("sqlite3.connect", side_effect=AssertionError("database access")):
            result = build_preference_policy(policy([rule()]))
        serialized_keys = set()

        def visit(value):
            if isinstance(value, dict):
                serialized_keys.update(value)
                for item in value.values():
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        visit(result)
        self.assertTrue({
            "candidate", "candidates", "ordered_candidates", "winner", "title_id",
        }.isdisjoint(serialized_keys))

    def test_authored_contract_has_no_hidden_decision_semantics(self):
        result = build_preference_policy(policy([
            rule(),
            rule(
                "policy.rule.support.v1",
                "criterion.support.unconditional_presence.v1",
                "prefer_present",
            ),
        ]))
        forbidden_keys = {
            "weight", "score", "utility", "points", "rank", "tier", "bonus",
            "penalty", "recommendation", "replacement", "winner", "quantity",
        }

        def keys(value):
            if isinstance(value, dict):
                return set(value) | set().union(*(keys(item) for item in value.values()), set())
            if isinstance(value, list):
                return set().union(*(keys(item) for item in value), set())
            return set()

        self.assertTrue(forbidden_keys.isdisjoint(keys(result)))
        self.assertNotIn("ordering_result", result)
        self.assertNotIn("selected", result)
        item = rule()
        item["explanation"] = "Recommend the best candidate."
        with self.assertRaises(ValueError):
            build_preference_policy(policy([item]))


if __name__ == "__main__":
    unittest.main()
