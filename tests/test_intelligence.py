from __future__ import annotations

from collections import Counter
from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from mtgadb import canonical
from mtgadb.model import Card, CardPrinting, Deck, Resolution
from services.intelligence import (
    INTERACTION_FAMILIES, RULES, analyze_deck, classify_card, interactions,
)
from workbench import main


def card(text="", **kwargs):
    return Card(1, "Synthetic Card", rules_text=text, **kwargs)


def labels(result, dimension="role"):
    return {f["label"] for f in result["features"] if f["dimension"] == dimension}


class ClassificationTests(unittest.TestCase):
    def test_role_positives_and_evidence(self):
        cases = {
            "Destroy target creature.": "removal",
            "Draw two cards.": "card_draw",
            "Scry 2.": "card_selection",
            "Counter target spell.": "counterspell",
            "Target creature you control gains hexproof until end of turn.": "protection",
            "Return target creature card from your graveyard to your hand.": "recursion",
        }
        for text, role in cases.items():
            with self.subTest(text=text):
                result = classify_card(card(text, types="Sorcery"))
                self.assertIn(role, labels(result))
                for f in result["features"]:
                    self.assertTrue(f["rule_id"].endswith(".v1"))
                    self.assertTrue(f["evidence"])
                    self.assertTrue(f["explanation"])

    def test_negative_and_ambiguous_text(self):
        for text in ("Target opponent draws two cards.", "You can't draw cards.",
                     "If you control a creature, draw two cards.", "Draw two cards, then discard two cards.",
                     "This spell can't be countered.", "Destroy all creatures.",
                     "Choose one —\nDraw two cards."):
            with self.subTest(text=text):
                result = classify_card(card(text))
                self.assertEqual(labels(result), set())
                self.assertEqual(result["status"], "unclassified")
                self.assertTrue(result["unsupported_text"])

        additional_cost = classify_card(card(
            "As an additional cost to cast this spell, sacrifice a creature.\n"
            "Draw two cards.",
            types="Sorcery",
        ))
        self.assertEqual(labels(additional_cost), set())
        self.assertEqual(additional_cost["status"], "classified")
        self.assertEqual(additional_cost["unsupported_text"], [])
        self.assertEqual(
            additional_cost["ability_coverage"]["meaningfully_understood_ability_count"],
            2,
        )

    def test_draw_and_selection_distinct(self):
        self.assertEqual(labels(classify_card(card("Scry 2."))), {"card_selection"})
        self.assertEqual(labels(classify_card(card("Draw a card."))), {"card_draw"})

    def test_ramp_and_fixing_distinct(self):
        mana = classify_card(card("{T}: Add {G}.", types="Creature"))
        self.assertEqual(labels(mana), {"ramp"})
        land = classify_card(card("{T}: Add one mana of any color.", types="Land"))
        self.assertEqual(labels(land), {"mana_fixing"})
        rock = classify_card(card("{T}: Add one mana of any color.", types="Artifact"))
        self.assertEqual(labels(rock), {"mana_fixing", "ramp"})
        self.assertNotIn("ramp", labels(classify_card(card("{T}: Add {G}.", types="Land"))))

    def test_land_search_battlefield_not_hand(self):
        good = "Search your library for a basic land card, put it onto the battlefield tapped, then shuffle."
        self.assertEqual(labels(classify_card(card(good))), {"ramp", "mana_fixing"})
        self.assertEqual(labels(classify_card(card(good.replace("onto the battlefield tapped", "into your hand")))), set())

    def test_multiple_roles(self):
        result = classify_card(card("Destroy target creature.\nDraw a card."))
        self.assertEqual(labels(result), {"removal", "card_draw"})

    def test_token_producer_vs_payoff(self):
        a = classify_card(card("Create a 1/1 white Soldier creature token."))
        b = classify_card(card("Whenever a creature token enters the battlefield under your control, draw a card.", types="Enchantment"))
        self.assertEqual(a["features"][0]["relationship"], "producer")
        self.assertTrue(any(f["label"] == "tokens" and f["relationship"] == "payoff" for f in b["features"]))

    def test_sacrifice_cost_vs_outlet(self):
        for text, relation in (("Sacrifice this creature: Draw a card.", "cost"),
                               ("Sacrifice another creature: Draw a card.", "consumer")):
            result = classify_card(card(text, types="Creature"))
            self.assertTrue(any(f["label"] == "sacrifice" and f["relationship"] == relation for f in result["features"]))
        self.assertEqual(labels(classify_card(card("Sacrifice another creature: Draw a card.", types="Sorcery"))), set())

    def test_themes_not_just_words(self):
        for text in ("Counters matter.", "Tokens matter.", "Sacrifice matters.", "Lands matter."):
            self.assertEqual(labels(classify_card(card(text)), "theme"), set())
        self.assertIn("counters", labels(classify_card(card("Put a +1/+1 counter on target creature you control.")), "theme"))
        self.assertNotIn("counters", labels(classify_card(card("Counter target spell.")), "theme"))
        self.assertIn("typal", labels(classify_card(card("Other Soldiers you control get +1/+1.")), "theme"))

    def test_threat_is_only_potential(self):
        result = classify_card(card(types="Creature", power="2", toughness="2"))
        self.assertEqual(result["features"][0]["relationship"], "potential")
        self.assertNotIn("threat", labels(classify_card(card(types="Creature", power="*"))))

    def test_threat_respects_explicit_attack_prohibitions(self):
        for text in ("Defender", "Synthetic Card can't attack.", "This creature can't attack."):
            with self.subTest(text=text):
                result = classify_card(card(text, types="Creature", power="5", toughness="5"))
                self.assertNotIn("threat", labels(result))
                self.assertIn(text, result["unsupported_text"])
        other_target = classify_card(card(
            "Target creature can't attack.", types="Creature", power="5", toughness="5"
        ))
        self.assertIn("threat", labels(other_target))

    def test_named_conditional_creature_state_marks_threat_conditional(self):
        condition = (
            "As long as your devotion to white is less than five, "
            "Heliod isn't a creature."
        )
        heliod = classify_card(Card(
            1,
            "Heliod, Sun-Crowned",
            types="Enchantment Creature",
            power="5",
            toughness="5",
            rules_text=condition,
        ))
        threats = [
            feature for feature in heliod["features"]
            if feature["dimension"] == "role" and feature["label"] == "threat"
        ]
        self.assertEqual(len(threats), 1)
        self.assertEqual(threats[0]["rule_id"], "type.conditional_threat.v1")
        self.assertEqual(threats[0]["relationship"], "conditional")
        self.assertEqual(threats[0]["evidence"], condition)
        self.assertNotIn("type.threat.v1", {feature["rule_id"] for feature in threats})
        self.assertIn(condition, heliod["unsupported_text"])

        unrelated = classify_card(Card(
            2,
            "Synthetic Card",
            types="Creature",
            power="5",
            toughness="5",
            rules_text=condition,
        ))
        unrelated_threat = next(
            feature for feature in unrelated["features"]
            if feature["dimension"] == "role" and feature["label"] == "threat"
        )
        self.assertEqual(unrelated_threat["rule_id"], "type.threat.v1")
        self.assertEqual(unrelated_threat["relationship"], "potential")

    def test_anomaly_and_no_text(self):
        anomalous = classify_card(card("Draw a card.", resolution=Resolution.ANOMALY))
        self.assertEqual(labels(anomalous), set())
        self.assertEqual(anomalous["text_status"], "anomalous")
        self.assertEqual(classify_card(card())["text_status"], "no_text")

    def test_contextual_unsupported_text_is_not_anomalous(self):
        result = classify_card(card("Choose one —\nDraw two cards."))
        self.assertEqual(result["status"], "unclassified")
        self.assertEqual(result["text_status"], "unsupported")
        self.assertEqual(result["unsupported_text"], ["Choose one —", "Draw two cards."])

    def test_v2_projects_real_card_abilities_conservatively(self):
        young = classify_card(Card(
            1, "Young Pyromancer", types="Creature", power="2", toughness="1",
            rules_text=("Whenever you cast an instant or sorcery spell, create a "
                        "1/1 red Elemental creature token."),
        ))
        caretaker = classify_card(Card(
            2, "Caretaker's Talent", types="Enchantment",
            rules_text=("Whenever one or more tokens you control enter, draw a card. "
                        "This ability triggers only once each turn."),
        ))
        agency = classify_card(Card(
            3, "Agency Coroner", types="Creature", power="3", toughness="2",
            rules_text=("{2}{B}, Sacrifice another creature: Draw a card. "
                        "If the sacrificed creature was suspected, draw two cards instead."),
        ))
        archmage = classify_card(Card(
            4, "Archmage of Runes", types="Creature", power="3", toughness="6",
            rules_text=("Instant and sorcery spells you cast cost {1} less to cast.\n"
                        "Whenever you cast an instant or sorcery spell, draw a card."),
        ))
        deadly = classify_card(Card(
            5, "Deadly Dispute", types="Instant",
            rules_text=("As an additional cost to cast this spell, sacrifice an artifact "
                        "or creature.\nDraw two cards and create a Treasure token."),
        ))
        treasure = classify_card(Card(
            6, "Treasure Spell", types="Sorcery", rules_text="Create a Treasure token."
        ))
        opponent = classify_card(Card(
            7, "Opponent Trigger", types="Enchantment",
            rules_text=("Whenever an opponent casts an instant or sorcery spell, create a "
                        "1/1 white Soldier creature token."),
        ))

        ids = lambda row: {feature["rule_id"] for feature in row["features"]}
        self.assertIn("effect.token.v1", ids(young))
        self.assertIn("trigger.token_draw.v1", ids(caretaker))
        self.assertIn("cost.sacrifice_draw.v1", ids(agency))
        self.assertIn("trigger.spells.v1", ids(archmage))
        self.assertNotIn("cost.sacrifice_draw.v1", ids(deadly))
        self.assertNotIn("effect.token.v1", ids(treasure))
        self.assertNotIn("effect.token.v1", ids(opponent))
        self.assertEqual(agency["abilities"][0]["costs"][0]["timing"], "activated")
        self.assertEqual(
            agency["abilities"][0]["effects"][1]["conditions"][0]["value"],
            "suspected",
        )
        self.assertEqual(
            caretaker["abilities"][0]["qualifiers"][0]["kind"], "once_each_turn"
        )

        rows = interactions([young, caretaker, agency, archmage, deadly])
        self.assertEqual(
            {row["rule_id"] for row in rows},
            {
                "interaction.token_draw.v1",
                "interaction.spell_draw.v1",
                "interaction.token_sacrifice.v1",
            },
        )

    def test_structural_and_meaningful_ability_coverage_are_distinct(self):
        treasure = classify_card(card("Create a Treasure token.", types="Sorcery"))
        unknown = classify_card(card("Unmodeled text.", types="Enchantment"))
        self.assertEqual(treasure["ability_coverage"]["structural_recognition"], 1.0)
        self.assertEqual(treasure["ability_coverage"]["meaningful_understanding"], 0.0)
        self.assertEqual(unknown["ability_coverage"]["structural_recognition"], 0.0)

    def test_rule_ids_unique(self):
        self.assertEqual(len(RULES), len({r[0] for r in RULES}))

    def test_interactions_require_exact_mechanics(self):
        source = classify_card(card("Create a 1/1 white Soldier creature token.", types="Sorcery"))
        payoff = classify_card(replace(card("Whenever a creature token enters the battlefield under your control, draw a card.", types="Enchantment"), title_id=2))
        outlet = classify_card(replace(card("Sacrifice another creature: Draw a card.", types="Creature"), title_id=3))
        spell = classify_card(replace(card("Whenever you cast an instant or sorcery spell, draw a card.", types="Enchantment"), title_id=4))
        rows = interactions([source, payoff, outlet, spell])
        self.assertEqual({r["rule_id"] for r in rows}, {"interaction.token_draw.v1", "interaction.token_sacrifice.v1", "interaction.spell_draw.v1"})
        self.assertTrue(all(len(r["evidence"]) == 2 for r in rows))
        token_draw = next(r for r in rows if r["rule_id"] == "interaction.token_draw.v1")
        token_draw_family = next(
            family for family in INTERACTION_FAMILIES
            if family.family_id == "interaction.token_draw.v1"
        )
        self.assertEqual(set(token_draw), {
            "rule_id", "source_title_id", "target_title_id", "explanation", "evidence",
        })
        self.assertEqual((token_draw["source_title_id"], token_draw["target_title_id"]), (1, 2))
        self.assertEqual(token_draw["explanation"], token_draw_family.explanation)
        self.assertEqual(
            [feature["rule_id"] for feature in token_draw["evidence"]],
            [token_draw_family.source_feature_rule_id,
             token_draw_family.beneficiary_feature_rule_id],
        )
        self.assertFalse(any(
            r["source_title_id"] == 2 and r["target_title_id"] == 1 for r in rows
        ))
        self.assertEqual(rows, interactions([spell, outlet, payoff, source]))
        self.assertEqual(interactions([source, dict(source, title_id=7)]), [])
        self.assertEqual(interactions([source]), [])

    def test_interaction_family_metadata_is_stable_and_reusable(self):
        self.assertIsInstance(INTERACTION_FAMILIES, tuple)
        self.assertEqual(
            [family.family_id for family in INTERACTION_FAMILIES],
            [
                "interaction.token_draw.v1",
                "interaction.spell_draw.v1",
                "interaction.token_sacrifice.v1",
            ],
        )
        self.assertEqual(
            [
                (family.source_feature_rule_id, family.beneficiary_feature_rule_id)
                for family in INTERACTION_FAMILIES
            ],
            [
                ("effect.token.v1", "trigger.token_draw.v1"),
                ("type.spells.v1", "trigger.spells.v1"),
                ("effect.token.v1", "cost.sacrifice_draw.v1"),
            ],
        )
        self.assertTrue(all(family.explanation for family in INTERACTION_FAMILIES))

    def test_repeated_pairs_group_by_interaction_family(self):
        first_source = classify_card(card(
            "Create a 1/1 white Soldier creature token.", types="Sorcery"
        ))
        second_source = classify_card(replace(card(
            "Create a 1/1 white Soldier creature token.", types="Sorcery"
        ), title_id=6))
        token_payoff = classify_card(replace(card(
            "Whenever a creature token enters the battlefield under your control, draw a card.",
            types="Enchantment"), title_id=2))
        spell_payoff = classify_card(replace(card(
            "Whenever you cast an instant or sorcery spell, draw a card.",
            types="Enchantment"), title_id=5))

        rows = interactions([first_source, second_source, token_payoff, spell_payoff])
        self.assertEqual(Counter(row["rule_id"] for row in rows), {
            "interaction.token_draw.v1": 2,
            "interaction.spell_draw.v1": 2,
        })
        self.assertEqual(
            {row["source_title_id"] for row in rows
             if row["rule_id"] == "interaction.token_draw.v1"},
            {1, 6},
        )

    def test_interactions_reject_near_match_prerequisites(self):
        token_source = classify_card(card("Create a 1/1 white Soldier creature token.", types="Sorcery"))
        nontoken_payoff = classify_card(replace(card(
            "Whenever a nontoken creature enters the battlefield under your control, draw a card.",
            types="Enchantment"), title_id=2))
        self_sacrifice = classify_card(replace(card(
            "Sacrifice this creature: Draw a card.", types="Creature"), title_id=3))
        nonspell = classify_card(replace(card("Draw a card.", types="Creature"), title_id=4))
        spell_payoff = classify_card(replace(card(
            "Whenever you cast an instant or sorcery spell, draw a card.",
            types="Enchantment"), title_id=5))

        rule_ids = {
            row["rule_id"] for row in interactions(
                [token_source, nontoken_payoff, self_sacrifice, nonspell, spell_payoff]
            )
        }
        self.assertNotIn("interaction.token_draw.v1", rule_ids)
        self.assertNotIn("interaction.token_sacrifice.v1", rule_ids)
        self.assertIn("interaction.spell_draw.v1", rule_ids)
        self.assertEqual(interactions([nonspell, spell_payoff]), [])


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(canonical.SCHEMA)
        self.addCleanup(self.con.close)
        canonical.load_cards(self.con, [Card(1,"Synthetic Draw", "{1}{U}", 2, "Sorcery", rules_text="Draw a card.\nDraw two cards."),
                                        Card(2,"Synthetic Land",types="Land"),
                                        Card(3,"Synthetic Token", "{W}", 1,"Sorcery", rules_text="Create a 1/1 white Soldier creature token.")])
        canonical.load_printings(self.con, [CardPrinting(101,1), CardPrinting(102,1), CardPrinting(201,2), CardPrinting(301,3)])
        self.con.commit()

    def test_curve_weighting_and_zone_separation(self):
        result = analyze_deck(Deck(main={101:2,102:1,201:24,301:4},sideboard={101:2}),self.con)
        main_zone=result["zones"]["main"]
        self.assertEqual(main_zone["land_count"],24)
        self.assertEqual(main_zone["nonland_mana_curve"],{"1":4,"2":3})
        self.assertEqual(main_zone["role_counts"]["card_draw"],3)
        self.assertEqual(main_zone["theme_counts"]["spells"],7)
        self.assertEqual(result["zones"]["sideboard"]["role_counts"]["card_draw"],2)
        self.assertEqual(result["legality"],"not_evaluated")

    def test_analysis_v2_includes_abilities_and_copy_weighted_coverage(self):
        result = analyze_deck(Deck(main={101: 2, 301: 4}), self.con)
        self.assertEqual(result["analysis_version"], "2")
        cards = result["zones"]["main"]["cards"]
        self.assertTrue(all("abilities" in row and "ability_coverage" in row for row in cards))
        coverage = result["zones"]["main"]["rules_text_coverage"]
        self.assertEqual(coverage["ability_count"], 8)
        self.assertEqual(coverage["structurally_recognized_ability_count"], 8)
        self.assertEqual(coverage["meaningfully_understood_ability_count"], 8)
        self.assertEqual(coverage["weighting"], "card_copy_times_ability")

    def test_order_and_no_writes(self):
        before = self.con.total_changes
        deck = Deck(main={101:2,201:4}, sideboard={301:1}, commander={})
        zones_before = (deck.main.copy(), deck.sideboard.copy(), deck.commander.copy())
        a = analyze_deck(deck,self.con)
        b = analyze_deck(Deck(main={201:4,101:2}),self.con)
        self.assertEqual(a["zones"]["main"],b["zones"]["main"])
        self.assertEqual((deck.main, deck.sideboard, deck.commander), zones_before)
        self.assertEqual(before,self.con.total_changes)

    def test_unknown_printing_is_partial(self):
        zone=analyze_deck(Deck(main={999:3}),self.con)["zones"]["main"]
        self.assertEqual(zone["coverage"],"partial")
        self.assertEqual(zone["resolved_count"],0)
        self.assertEqual(zone["diagnostics"][0]["quantity"],3)

    def test_invalid_quantities(self):
        for quantity in (0,-1,True,1.5):
            with self.assertRaises(ValueError):
                analyze_deck(Deck(main={101:quantity}),self.con)

    def test_cli_read_only_and_bad_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); db=root/"cards.db"; deck=root/"deck.txt"
            dest=sqlite3.connect(db);self.con.backup(dest);dest.close()
            deck.write_text("Deck\n4 Synthetic Draw\n24 Synthetic Land\n",encoding="utf-8")
            before=db.read_bytes()
            out=io.StringIO()
            with redirect_stdout(out):
                code=main(["--database",str(db),"analyze-deck",str(deck)])
            self.assertEqual(code,0)
            self.assertEqual(json.loads(out.getvalue())["zones"]["main"]["land_count"],24)
            self.assertEqual(before,db.read_bytes())
            deck.write_text("Deck\n4 Not A Card\n",encoding="utf-8")
            out=io.StringIO()
            with redirect_stdout(out):
                code=main(["--database",str(db),"analyze-deck",str(deck)])
            self.assertEqual(code,2)
            self.assertNotIn("zones",json.loads(out.getvalue()))


if __name__ == "__main__":
    unittest.main()
