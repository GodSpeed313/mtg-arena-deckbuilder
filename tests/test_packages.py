import sqlite3
import unittest

from mtgadb import canonical
from mtgadb.model import Card, CardPrinting, Deck
from services.intelligence import analyze_deck, classify_card
from services.packages import (
    FUNCTIONAL_PACKAGES,
    PACKAGE_MODEL_VERSION,
    functional_package_contributions,
)


def card(text="", **kwargs):
    return Card(1, "Synthetic Card", rules_text=text, **kwargs)


def package_labels(result):
    return {package["label"] for package in result["functional_packages"]}


class FunctionalPackageTests(unittest.TestCase):
    def test_registry_is_stable_unique_and_complete(self):
        self.assertEqual(PACKAGE_MODEL_VERSION, "1")
        self.assertEqual(
            [package.label for package in FUNCTIONAL_PACKAGES],
            [
                "threats",
                "interaction",
                "card_advantage",
                "mana_ramp",
                "protection",
                "recursion",
                "token_production",
                "lifegain",
                "plus1_counters",
                "sacrifice",
                "spell_matters",
                "graveyard_interaction",
            ],
        )
        self.assertEqual(
            len(FUNCTIONAL_PACKAGES),
            len({package.package_id for package in FUNCTIONAL_PACKAGES}),
        )
        self.assertTrue(all(
            package.package_id.startswith("package.")
            and package.package_id.endswith(".v1")
            and package.feature_rule_ids
            and package.explanation
            for package in FUNCTIONAL_PACKAGES
        ))

    def test_one_card_can_contribute_to_multiple_packages(self):
        result = classify_card(card(
            "Sacrifice another creature: Draw a card.",
            types="Creature",
            power="2",
            toughness="2",
        ))
        self.assertEqual(
            package_labels(result),
            {"threats", "card_advantage", "sacrifice"},
        )
        sacrifice = next(
            package for package in result["functional_packages"]
            if package["label"] == "sacrifice"
        )
        self.assertEqual(
            {feature["rule_id"] for feature in sacrifice["evidence"]},
            {"cost.sacrifice_draw.v1"},
        )

    def test_package_evidence_preserves_feature_relationships(self):
        producer = classify_card(card("You gain 3 life.", types="Sorcery"))
        listener = classify_card(card(
            "Whenever you gain life, draw a card.", types="Enchantment"
        ))
        producer_package = next(
            package for package in producer["functional_packages"]
            if package["label"] == "lifegain"
        )
        listener_package = next(
            package for package in listener["functional_packages"]
            if package["label"] == "lifegain"
        )
        self.assertEqual(
            [(feature["rule_id"], feature["relationship"])
             for feature in producer_package["evidence"]],
            [("effect.lifegain.v1", "producer")],
        )
        self.assertEqual(
            [(feature["rule_id"], feature["relationship"])
             for feature in listener_package["evidence"]],
            [("trigger.lifegain.v1", "payoff")],
        )

    def test_exact_features_drive_overlapping_packages(self):
        recursion = classify_card(card(
            "Return target creature card from your graveyard to your hand.",
            types="Sorcery",
        ))
        self.assertEqual(
            package_labels(recursion),
            {"recursion", "spell_matters", "graveyard_interaction"},
        )

        graveyard_exile = classify_card(card(
            "Exile target card from a graveyard.", types="Instant"
        ))
        self.assertEqual(
            package_labels(graveyard_exile),
            {"interaction", "spell_matters", "graveyard_interaction"},
        )

    def test_interaction_package_keeps_conservative_damage_boundary(self):
        creature = classify_card(card(
            "This spell deals 3 damage to target creature.", types="Instant"
        ))
        opponent = classify_card(card(
            "This spell deals 3 damage to target opponent.", types="Instant"
        ))
        any_target = classify_card(card(
            "This spell deals 3 damage to any target.", types="Instant"
        ))
        self.assertIn("interaction", package_labels(creature))
        self.assertIn("interaction", package_labels(any_target))
        self.assertNotIn("interaction", package_labels(opponent))

    def test_unknown_text_and_unmapped_features_do_not_create_packages(self):
        unknown = classify_card(card("Counters matter.", types="Enchantment"))
        selection = classify_card(card("Scry 2.", types="Enchantment"))
        self.assertEqual(unknown["functional_packages"], [])
        self.assertNotIn("card_advantage", package_labels(selection))
        self.assertEqual(functional_package_contributions([]), [])

    def test_packages_do_not_emit_needs_scores_edges_or_recommendations(self):
        result = classify_card(card(
            "Exile target creature.", types="Instant"
        ))
        self.assertTrue(result["functional_packages"])
        for package in result["functional_packages"]:
            self.assertEqual(
                set(package),
                {"package_id", "label", "explanation", "evidence"},
            )
            self.assertTrue({
                "need", "score", "dependencies", "interactions",
                "recommendations",
            }.isdisjoint(package))

    def test_conditional_threat_is_a_job_not_an_evaluated_state(self):
        result = classify_card(Card(
            1,
            "Heliod, Sun-Crowned",
            types="Enchantment Creature",
            power="5",
            toughness="5",
            rules_text=(
                "As long as your devotion to white is less than five, "
                "Heliod isn't a creature."
            ),
        ))
        threat = next(
            package for package in result["functional_packages"]
            if package["label"] == "threats"
        )
        self.assertEqual(
            {feature["rule_id"] for feature in threat["evidence"]},
            {"type.conditional_threat.v1"},
        )
        self.assertEqual(threat["evidence"][0]["relationship"], "conditional")


class FunctionalPackageAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(canonical.SCHEMA)
        self.addCleanup(self.con.close)
        canonical.load_cards(self.con, [
            Card(1, "Synthetic Draw", types="Sorcery",
                 rules_text="Draw a card.\nDraw two cards."),
            Card(2, "Synthetic Token", types="Sorcery",
                 rules_text="Create a 1/1 white Soldier creature token."),
        ])
        canonical.load_printings(self.con, [CardPrinting(101, 1), CardPrinting(201, 2)])
        self.con.commit()

    def test_zone_counts_each_copy_once_per_package(self):
        result = analyze_deck(Deck(main={101: 3, 201: 4}), self.con)
        self.assertEqual(result["functional_package_model_version"], "1")
        counts = result["zones"]["main"]["functional_package_counts"]
        self.assertEqual(counts["card_advantage"], 3)
        self.assertEqual(counts["token_production"], 4)
        self.assertEqual(counts["spell_matters"], 7)
        self.assertEqual(counts["interaction"], 0)

    def test_zone_package_counts_are_independent_and_deterministic(self):
        first = analyze_deck(Deck(main={201: 2}, sideboard={101: 1}), self.con)
        second = analyze_deck(Deck(main={201: 2}, sideboard={101: 1}), self.con)
        self.assertEqual(first, second)
        self.assertEqual(
            first["zones"]["main"]["functional_package_counts"]["token_production"],
            2,
        )
        self.assertEqual(
            first["zones"]["sideboard"]["functional_package_counts"]["card_advantage"],
            1,
        )
        self.assertEqual(
            first["zones"]["main"]["functional_package_counts"]["card_advantage"],
            0,
        )


if __name__ == "__main__":
    unittest.main()
