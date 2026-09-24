from __future__ import annotations

from copy import deepcopy
import sqlite3
import unittest

from mtgadb import canonical
from mtgadb.model import Card, CardPrinting, Deck
from services.dependencies import dependency_findings
from services.diagnosis import diagnose_analysis
from services.intelligence import analyze_deck
from services.needs import NEEDS_MODEL_VERSION, needs_findings


LIFE = "You gain 3 life."
LIFE_PAYOFF = "Whenever you gain life, draw a card."
COUNTER_PAYOFF = (
    "Whenever one or more +1/+1 counters are put on a creature you control, "
    "draw a card."
)
TOKEN = "Create a 1/1 white Soldier creature token."
TOKEN_PAYOFF = (
    "Whenever a creature token enters the battlefield under your control, draw a card."
)
SACRIFICE = "Sacrifice another creature: Draw a card."
SPELL_PAYOFF = "Whenever you cast an instant or sorcery spell, draw a card."

DEPENDENCY_RULES = {
    "lifegain": ("effect.lifegain.v1", "producer", "trigger.lifegain.v1", "payoff"),
    "plus1_counters": ("effect.counter.v1", "producer", "trigger.counter.v1", "payoff"),
    "creature_token_entry": (
        "effect.token.v1", "producer", "trigger.token_draw.v1", "payoff",
    ),
    "creature_token_sacrifice": (
        "effect.token.v1", "producer", "cost.sacrifice_draw.v1", "consumer",
    ),
    "spell_cast": ("type.spells.v1", "enabler", "trigger.spells.v1", "payoff"),
}


def boundary(**overrides) -> dict:
    result = {
        "claim_scope": "reviewed_features_only",
        "zone_resolution": "resolved",
        "unresolved_printing_copies": 0,
        "unclassified_card_copies": 0,
        "partially_classified_card_copies": 0,
        "unsupported_text_card_copies": 0,
        "rules_text_coverage": {"meaningful_understanding": 1.0},
    }
    result.update(overrides)
    return result


def side(rule_id: str, relationship: str, quantity: int, title_id: int) -> dict:
    cards = [] if quantity == 0 else [{
        "title_id": title_id,
        "name": f"Synthetic {title_id}",
        "quantity": quantity,
        "evidence": [{
            "rule_id": rule_id,
            "dimension": "theme",
            "label": "synthetic",
            "relationship": relationship,
            "evidence": "Reviewed evidence.",
            "explanation": "Reviewed fixture feature.",
        }],
    }]
    return {
        "relationship": relationship,
        "acceptable_feature_rule_ids": [rule_id],
        "matching_semantics": "any",
        "feature_rule_ids": [rule_id],
        "copy_count": quantity,
        "cards": cards,
    }


def dependency(label: str, state: str, *, quantity: int = 2) -> dict:
    enabler_rule, enabler_relationship, payoff_rule, payoff_relationship = (
        DEPENDENCY_RULES[label]
    )
    has_enabler = state in {"supported", "conditionally_supported", "enabler_without_payoff"}
    has_payoff = state in {"supported", "conditionally_supported", "payoff_without_enabler", "optional_support_absent"}
    return {
        "dependency_id": f"dependency.{label}.v1",
        "label": label,
        "policy": "optional_support" if label == "creature_token_sacrifice" else "strict",
        "state": state,
        "explanation": "Synthetic dependency fixture.",
        "enabler_side": side(
            enabler_rule, enabler_relationship, quantity if has_enabler else 0, 1,
        ),
        "payoff_side": side(
            payoff_rule, payoff_relationship, quantity if has_payoff else 0, 2,
        ),
    }


class NeedsModelTests(unittest.TestCase):
    def test_version_and_stable_finding_ids(self):
        self.assertEqual(NEEDS_MODEL_VERSION, "2")
        findings = needs_findings([
            dependency("lifegain", "payoff_without_enabler"),
            dependency("plus1_counters", "enabler_without_payoff"),
        ], boundary())
        self.assertEqual(
            [item["finding_id"] for item in findings],
            ["need.lifegain.enabler.v1", "opportunity.plus1_counters.payoff.v1"],
        )

    def test_lifegain_state_mapping_is_asymmetric(self):
        need = needs_findings([
            dependency("lifegain", "payoff_without_enabler"),
        ], boundary())
        opportunity = needs_findings([
            dependency("lifegain", "enabler_without_payoff"),
        ], boundary())
        supported = needs_findings([
            dependency("lifegain", "supported"),
        ], boundary())
        self.assertEqual(need[0]["finding_type"], "support_need")
        self.assertEqual(need[0]["missing_side_name"], "enabler")
        self.assertEqual(opportunity[0]["finding_type"], "unused_support_opportunity")
        self.assertEqual(opportunity[0]["missing_side_name"], "payoff")
        self.assertIn("not a deck need", opportunity[0]["explanation"])
        self.assertEqual(supported, [])

    def test_four_strict_families_create_needs_and_optional_support_does_not(self):
        findings = needs_findings([
            dependency(label, "payoff_without_enabler")
            for label in DEPENDENCY_RULES if label != "creature_token_sacrifice"
        ] + [
            dependency("creature_token_sacrifice", "optional_support_absent")
        ], boundary())
        self.assertEqual(
            {item["dependency_label"] for item in findings if item["finding_type"] == "support_need"},
            set(DEPENDENCY_RULES) - {"creature_token_sacrifice"},
        )
        sacrifice = next(item for item in findings if item["dependency_label"] == "creature_token_sacrifice")
        self.assertEqual(sacrifice["finding_type"], "optional_support_observation")

    def test_spell_enabler_only_is_an_opportunity_not_a_need(self):
        result = needs_findings([
            dependency("spell_cast", "enabler_without_payoff"),
        ], boundary())
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["finding_type"], "unused_support_opportunity")
        self.assertNotEqual(result[0]["finding_type"], "support_need")

    def test_findings_preserve_dependency_evidence_counts_and_coverage(self):
        source = dependency("lifegain", "payoff_without_enabler", quantity=4)
        coverage = boundary(
            zone_resolution="partial",
            unresolved_printing_copies=2,
            unclassified_card_copies=3,
            unsupported_text_card_copies=3,
        )
        before = deepcopy(source)
        result = needs_findings([source], coverage)[0]
        self.assertEqual(source, before)
        self.assertEqual(result["dependency_id"], source["dependency_id"])
        self.assertEqual(result["dependency_state"], "payoff_without_enabler")
        self.assertEqual(result["existing_side"]["copy_count"], 4)
        self.assertEqual(
            result["existing_side"]["cards"][0]["evidence"][0]["rule_id"],
            "trigger.lifegain.v1",
        )
        self.assertEqual(result["existing_side"]["relationship"], "payoff")
        self.assertEqual(result["missing_side"]["feature_rule_ids"], ["effect.lifegain.v1"])
        self.assertEqual(result["evidence_boundary"], coverage)
        self.assertIn("no compatible reviewed matching enabler", result["explanation"])

    def test_output_has_no_future_layer_fields(self):
        result = needs_findings([
            dependency("lifegain", "payoff_without_enabler"),
        ], boundary())[0]
        forbidden = {
            "candidate", "candidates", "score", "severity", "priority",
            "recommendation", "recommendations", "threshold", "ratio", "optimal",
        }

        def keys(value):
            if isinstance(value, dict):
                return set(value) | set().union(*(keys(item) for item in value.values()))
            if isinstance(value, list):
                return set().union(*(keys(item) for item in value), set())
            return set()

        self.assertTrue(forbidden.isdisjoint(keys(result)))

    def test_same_input_is_deterministic_and_unknown_states_fail_closed(self):
        source = [dependency("lifegain", "payoff_without_enabler")]
        self.assertEqual(
            needs_findings(source, boundary()), needs_findings(source, boundary()),
        )
        invalid = {**source[0], "state": "new_unreviewed_state"}
        with self.assertRaises(ValueError):
            needs_findings([invalid], boundary())


class NeedsAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(canonical.SCHEMA)
        cards = [
            Card(1, "Life", types="Sorcery", rules_text=LIFE),
            Card(2, "Life Payoff", types="Enchantment", rules_text=LIFE_PAYOFF),
            Card(3, "Counter Payoff", types="Enchantment", rules_text=COUNTER_PAYOFF),
            Card(4, "Token Payoff", types="Enchantment", rules_text=TOKEN_PAYOFF),
            Card(5, "Sacrifice", types="Creature", rules_text=SACRIFICE),
            Card(6, "Spell Payoff", types="Enchantment", rules_text=SPELL_PAYOFF),
            Card(7, "Token", types="Instant", rules_text=TOKEN),
            Card(8, "Treasure", types="Enchantment", rules_text="Create two Treasure tokens."),
            Card(9, "Lifelink", types="Creature", rules_text="Lifelink"),
            Card(10, "Unknown", rules_text="Unreviewed life words."),
            Card(11, "Land", types="Land"),
        ]
        canonical.load_cards(self.con, cards)
        canonical.load_printings(self.con, [
            CardPrinting(title_id * 100 + 1, title_id)
            for title_id in range(1, 12)
        ])
        self.con.commit()
        self.addCleanup(self.con.close)

    def test_lifelink_noncreature_tokens_and_unknown_text_do_not_fill_needs(self):
        result = analyze_deck(Deck(main={201: 2, 401: 2, 901: 2, 801: 2, 1001: 3}), self.con)
        needs = {
            item["dependency_label"]: item
            for item in result["zones"]["main"]["needs"]
            if item["finding_type"] == "support_need"
        }
        self.assertIn("lifegain", needs)
        self.assertIn("creature_token_entry", needs)
        self.assertEqual(needs["lifegain"]["missing_side"]["cards"], [])
        self.assertEqual(
            needs["creature_token_entry"]["dependency_state"],
            "payoff_without_compatible_enabler",
        )
        evidence = needs["lifegain"]["evidence_boundary"]
        self.assertEqual(evidence["unclassified_card_copies"], 3)
        self.assertEqual(evidence["unsupported_text_card_copies"], 3)
        self.assertEqual(evidence["claim_scope"], "reviewed_features_only")

    def test_supported_relationships_create_no_false_needs(self):
        result = analyze_deck(Deck(main={101: 1, 201: 4}), self.con)
        lifegain = [
            item for item in result["zones"]["main"]["needs"]
            if item["dependency_label"] == "lifegain"
        ]
        dependency_row = next(
            item for item in result["zones"]["main"]["dependencies"]
            if item["label"] == "lifegain"
        )
        self.assertEqual(dependency_row["state"], "supported")
        self.assertEqual(lifegain, [])

    def test_zones_are_independent_and_partial_coverage_is_retained(self):
        result = analyze_deck(Deck(
            main={201: 2, 999999: 3},
            sideboard={101: 2},
            commander={201: 1},
        ), self.con)
        main_need = next(
            item for item in result["zones"]["main"]["needs"]
            if item["dependency_label"] == "lifegain"
        )
        side_opportunity = next(
            item for item in result["zones"]["sideboard"]["needs"]
            if item["dependency_label"] == "lifegain"
        )
        commander_need = next(
            item for item in result["zones"]["commander"]["needs"]
            if item["dependency_label"] == "lifegain"
        )
        self.assertEqual(main_need["finding_type"], "support_need")
        self.assertEqual(main_need["evidence_boundary"]["zone_resolution"], "partial")
        self.assertEqual(main_need["evidence_boundary"]["unresolved_printing_copies"], 3)
        self.assertEqual(side_opportunity["finding_type"], "unused_support_opportunity")
        self.assertEqual(commander_need["finding_type"], "support_need")

    def test_one_card_can_participate_in_multiple_opportunities(self):
        result = analyze_deck(Deck(main={701: 4}), self.con)
        opportunities = {
            item["dependency_label"] for item in result["zones"]["main"]["needs"]
            if item["finding_type"] == "unused_support_opportunity"
        }
        self.assertEqual(opportunities, {
            "creature_token_entry", "creature_token_sacrifice", "spell_cast",
        })

    def test_existing_outputs_and_diagnosis_remain_stable(self):
        result = analyze_deck(Deck(main={701: 4, 401: 2, 1101: 54}), self.con)
        self.assertEqual(result["analysis_version"], "4")
        self.assertEqual(result["needs_model_version"], "2")
        self.assertEqual(result["dependency_model_version"], "2")
        self.assertEqual(result["functional_package_model_version"], "1")
        self.assertEqual(
            {item["rule_id"] for item in result["interactions"]},
            {"interaction.token_draw.v1"},
        )
        self.assertEqual(
            result["zones"]["main"]["functional_package_counts"]["token_production"],
            4,
        )
        before_dependencies = dependency_findings(result["zones"]["main"]["cards"])
        self.assertEqual(result["zones"]["main"]["dependencies"], before_dependencies)
        report = diagnose_analysis(result)
        self.assertEqual(report["plan"]["probable_plan"], "token_value")


if __name__ == "__main__":
    unittest.main()
