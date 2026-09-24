from __future__ import annotations

import sqlite3
import unittest

from mtgadb import canonical
from mtgadb.model import Card, CardPrinting, Deck
from services.dependencies import (
    DEPENDENCIES,
    DEPENDENCY_MODEL_VERSION,
    INTERACTION_FAMILIES,
    dependency_findings,
)
from services.diagnosis import diagnose_analysis
from services.intelligence import analyze_deck, classify_card


LIFE = "You gain 3 life."
LIFE_PAYOFF = "Whenever you gain life, draw a card."
COUNTER = "Put a +1/+1 counter on target creature you control."
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


def row(title_id: int, quantity: int, text: str, *, types: str) -> dict:
    result = classify_card(Card(
        title_id, f"Synthetic {title_id}", types=types, rules_text=text,
    ))
    result["quantity"] = quantity
    return result


def finding(rows: list[dict], label: str) -> dict:
    return next(item for item in dependency_findings(rows) if item["label"] == label)


class DependencyModelTests(unittest.TestCase):
    def test_registry_version_ids_and_shared_interaction_definitions_are_stable(self):
        self.assertEqual(DEPENDENCY_MODEL_VERSION, "2")
        self.assertIsInstance(DEPENDENCIES, tuple)
        self.assertEqual(
            [item.dependency_id for item in DEPENDENCIES],
            [
                "dependency.lifegain.v1",
                "dependency.plus1_counters.v2",
                "dependency.creature_token_entry.v2",
                "dependency.creature_token_sacrifice.v2",
                "dependency.spell_cast.v1",
            ],
        )
        self.assertEqual(
            len(DEPENDENCIES), len({item.dependency_id for item in DEPENDENCIES}),
        )
        self.assertTrue(all(
            item.dependency_id.rsplit(".v", 1)[-1] in {"1", "2"}
            and item.policy in {"strict", "optional_support"}
            and item.enabler_rule_ids
            and item.payoff_rule_ids
            and item.explanation
            for item in DEPENDENCIES
        ))
        projected = {
            item.interaction_family_id: (
                item.enabler_rule_ids, item.payoff_rule_ids
            )
            for item in DEPENDENCIES if item.interaction_family_id
        }
        self.assertEqual(
            {
                family.family_id: (
                    family.source_feature_rule_ids,
                    family.beneficiary_feature_rule_ids,
                )
                for family in INTERACTION_FAMILIES
            },
            projected,
        )

    def test_lifegain_support_states(self):
        producer = row(1, 1, LIFE, types="Sorcery")
        payoff = row(2, 1, LIFE_PAYOFF, types="Enchantment")
        self.assertEqual(finding([producer, payoff], "lifegain")["state"], "supported")
        self.assertEqual(
            finding([payoff], "lifegain")["state"], "payoff_without_enabler",
        )
        self.assertEqual(
            finding([producer], "lifegain")["state"], "enabler_without_payoff",
        )

    def test_plus1_counter_support_and_missing_producer(self):
        producer = row(1, 2, COUNTER, types="Sorcery")
        payoff = row(2, 3, COUNTER_PAYOFF, types="Enchantment")
        supported = finding([producer, payoff], "plus1_counters")
        self.assertEqual(supported["state"], "supported")
        self.assertEqual(supported["enabler_side"]["copy_count"], 2)
        self.assertEqual(supported["payoff_side"]["copy_count"], 3)
        self.assertEqual(
            finding([payoff], "plus1_counters")["state"],
            "payoff_without_enabler",
        )

    def test_existing_token_and_spell_dependencies_are_supported(self):
        rows = [
            row(1, 4, TOKEN, types="Sorcery"),
            row(2, 2, TOKEN_PAYOFF, types="Enchantment"),
            row(3, 2, SACRIFICE, types="Creature"),
            row(4, 2, SPELL_PAYOFF, types="Enchantment"),
        ]
        states = {item["label"]: item["state"] for item in dependency_findings(rows)}
        self.assertEqual(states["creature_token_entry"], "supported")
        self.assertEqual(states["creature_token_sacrifice"], "supported")
        self.assertEqual(states["spell_cast"], "supported")

    def test_noncreature_tokens_lifelink_and_unsupported_text_do_not_enable(self):
        rows = [
            row(1, 2, "Create two Treasure tokens.", types="Enchantment"),
            row(2, 2, "Lifelink", types="Creature"),
            row(3, 2, "Counters matter.", types="Enchantment"),
            row(4, 2, TOKEN_PAYOFF, types="Enchantment"),
            row(5, 2, LIFE_PAYOFF, types="Enchantment"),
        ]
        token = finding(rows, "creature_token_entry")
        life = finding(rows, "lifegain")
        self.assertEqual(token["state"], "payoff_without_compatible_enabler")
        self.assertEqual(token["compatible_pairs"], [])
        self.assertEqual(life["state"], "payoff_without_enabler")
        self.assertEqual(life["enabler_side"]["cards"], [])
        self.assertFalse(any(card["title_id"] == 3 for item in dependency_findings(rows)
                             for side in (item["enabler_side"], item["payoff_side"])
                             for card in side["cards"]))

    def test_copy_counts_deduplicate_features_and_scale_quantities(self):
        producer = row(1, 4, "You gain 2 life.\nYou gain 3 life.", types="Sorcery")
        payoff = row(2, 3, LIFE_PAYOFF, types="Enchantment")
        result = finding([producer, payoff], "lifegain")
        self.assertEqual(result["enabler_side"]["copy_count"], 4)
        self.assertEqual(result["payoff_side"]["copy_count"], 3)
        self.assertEqual(len(result["enabler_side"]["cards"]), 1)
        self.assertEqual(len(result["enabler_side"]["cards"][0]["evidence"]), 2)

    def test_one_card_can_participate_in_multiple_families(self):
        maker = row(1, 4, TOKEN, types="Instant")
        results = dependency_findings([
            maker,
            row(2, 2, TOKEN_PAYOFF, types="Enchantment"),
            row(3, 2, SACRIFICE, types="Creature"),
            row(4, 2, SPELL_PAYOFF, types="Enchantment"),
        ])
        supported = {item["label"] for item in results if item["state"] == "supported"}
        self.assertEqual(supported, {
            "creature_token_entry", "creature_token_sacrifice", "spell_cast",
        })

    def test_evidence_is_exact_and_output_has_no_future_layer_claims(self):
        result = finding([
            row(1, 4, LIFE, types="Sorcery"),
            row(2, 2, LIFE_PAYOFF, types="Enchantment"),
        ], "lifegain")
        self.assertEqual(
            result["enabler_side"]["cards"][0]["evidence"][0]["rule_id"],
            "effect.lifegain.v1",
        )
        self.assertEqual(
            result["payoff_side"]["cards"][0]["evidence"][0]["relationship"],
            "payoff",
        )
        forbidden = {
            "score", "recommendations", "candidates", "needs", "sufficiency",
            "strength", "threshold",
        }

        def keys(value):
            if isinstance(value, dict):
                return set(value) | set().union(*(keys(item) for item in value.values()))
            if isinstance(value, list):
                return set().union(*(keys(item) for item in value), set())
            return set()

        self.assertTrue(forbidden.isdisjoint(keys(result)))

    def test_absent_families_are_omitted_and_output_is_deterministic(self):
        rows = [row(1, 2, LIFE, types="Sorcery")]
        first = dependency_findings(rows)
        second = dependency_findings(list(reversed(rows)))
        self.assertEqual(first, second)
        self.assertEqual([item["label"] for item in first], ["lifegain", "spell_cast"])


class DependencyAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(canonical.SCHEMA)
        cards = [
            Card(1, "Life", types="Sorcery", rules_text=LIFE),
            Card(2, "Life Payoff", types="Enchantment", rules_text=LIFE_PAYOFF),
            Card(3, "Counter", types="Sorcery", rules_text=COUNTER),
            Card(4, "Counter Payoff", types="Enchantment", rules_text=COUNTER_PAYOFF),
            Card(5, "Token", types="Sorcery", rules_text=TOKEN),
            Card(6, "Token Payoff", types="Enchantment", rules_text=TOKEN_PAYOFF),
            Card(7, "Land", types="Land"),
        ]
        canonical.load_cards(self.con, cards)
        canonical.load_printings(
            self.con, [CardPrinting(title_id * 100 + 1, title_id) for title_id in range(1, 8)],
        )
        self.con.commit()
        self.addCleanup(self.con.close)

    def test_analysis_is_zone_local_and_additive_version_two(self):
        result = analyze_deck(Deck(
            main={101: 4, 201: 2},
            sideboard={301: 2},
            commander={401: 1},
        ), self.con)
        self.assertEqual(result["analysis_version"], "4")
        self.assertEqual(result["dependency_model_version"], "2")
        self.assertEqual(
            next(item for item in result["zones"]["main"]["dependencies"]
                 if item["label"] == "lifegain")["state"],
            "supported",
        )
        self.assertEqual(
            next(item for item in result["zones"]["sideboard"]["dependencies"]
                 if item["label"] == "plus1_counters")["state"],
            "enabler_without_payoff",
        )
        self.assertEqual(
            next(item for item in result["zones"]["commander"]["dependencies"]
                 if item["label"] == "plus1_counters")["state"],
            "payoff_without_enabler",
        )

    def test_analysis_determinism_package_outputs_and_diagnosis_stay_stable(self):
        first = analyze_deck(Deck(main={501: 4, 601: 2, 701: 54}), self.con)
        second = analyze_deck(Deck(main={701: 54, 601: 2, 501: 4}), self.con)
        self.assertEqual(first, second)
        self.assertEqual(first["functional_package_model_version"], "1")
        self.assertEqual(
            first["zones"]["main"]["functional_package_counts"]["token_production"],
            4,
        )
        report = diagnose_analysis(first)
        self.assertEqual(report["plan"]["probable_plan"], "token_value")


if __name__ == "__main__":
    unittest.main()
