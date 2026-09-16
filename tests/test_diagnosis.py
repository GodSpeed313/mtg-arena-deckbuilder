from __future__ import annotations

from collections import Counter
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from mtgadb import canonical
from mtgadb.model import Card, CardPrinting, Deck
from services.diagnosis import diagnose_analysis
from services.intelligence import ROLES, THEMES, analyze_deck, classify_card, interactions
from workbench import main


def entry(title_id, quantity, *, text="", types="", cmc=1, power=""):
    result = classify_card(Card(
        title_id, f"Synthetic {title_id}", cmc=cmc, types=types,
        power=power, rules_text=text,
    ))
    result["quantity"] = quantity
    return result, cmc


def zone(entries=(), *, unresolved=0):
    cards = [item[0] for item in entries]
    cmcs = {item[0]["title_id"]: item[1] for item in entries}
    roles, themes, curve = Counter(), Counter(), Counter()
    lands = 0
    for card in cards:
        is_land = any(feature["rule_id"] == "type.lands.v1" for feature in card["features"])
        if is_land:
            lands += card["quantity"]
        else:
            curve[cmcs[card["title_id"]]] += card["quantity"]
        for dimension, counts in (("role", roles), ("theme", themes)):
            for label in {
                feature["label"] for feature in card["features"]
                if feature["dimension"] == dimension
            }:
                counts[label] += card["quantity"]
    resolved = sum(card["quantity"] for card in cards)
    return {
        "total_count": resolved + unresolved,
        "resolved_count": resolved,
        "land_count": lands,
        "nonland_mana_curve": {str(value): curve[value] for value in sorted(curve)},
        "role_counts": {role: roles[role] for role in ROLES},
        "theme_counts": {theme: themes[theme] for theme in THEMES},
        "cards": cards,
        "diagnostics": ([{"code": "unknown_printing", "quantity": unresolved}]
                        if unresolved else []),
        "coverage": "partial" if unresolved else "resolved",
    }


def analysis(main_entries=(), *, unresolved=0, sideboard_entries=(), commander_entries=()):
    main = zone(main_entries, unresolved=unresolved)
    return {
        "analysis_version": "1",
        "legality": "not_evaluated",
        "zones": {
            "main": main,
            "sideboard": zone(sideboard_entries),
            "commander": zone(commander_entries),
        },
        "interactions": interactions(main["cards"]),
        "limitations": [],
    }


TOKEN = "Create a 1/1 white Soldier creature token."
TOKEN_PAYOFF = "Whenever a creature token enters the battlefield under your control, draw a card."
SAC_OUTLET = "Sacrifice another creature: Draw a card."
SPELL_PAYOFF = "Whenever you cast an instant or sorcery spell, draw a card."


def land(quantity):
    return entry(900, quantity, types="Land", cmc=0)


class DiagnosisTests(unittest.TestCase):
    def test_token_value_moderate_plan(self):
        report = diagnose_analysis(analysis([
            entry(1, 4, text=TOKEN, types="Sorcery"),
            entry(2, 2, text=TOKEN_PAYOFF, types="Enchantment"),
            land(50),
        ]))
        self.assertEqual(report["status"], "diagnosed")
        self.assertEqual(report["plan"]["probable_plan"], "token_value")
        self.assertEqual(report["plan"]["confidence"], "moderate")
        self.assertEqual(report["plan"]["evidence_family_ids"], ["interaction.token_draw.v1"])

    def test_high_confidence_requires_density_titles_and_edges(self):
        report = diagnose_analysis(analysis([
            entry(1, 4, text=TOKEN, types="Sorcery"),
            entry(6, 4, text=TOKEN, types="Sorcery"),
            entry(2, 4, text=TOKEN_PAYOFF, types="Enchantment"),
            land(48),
        ]))
        self.assertEqual(report["plan"]["confidence"], "high")
        candidate = next(c for c in report["plan"]["candidates"] if c["plan"] == "token_value")
        self.assertEqual(candidate["interaction_pair_count"], 2)
        self.assertEqual(candidate["supporting_title_count"], 3)
        self.assertEqual(candidate["support_density"], 0.2)

    def test_other_two_approved_plans(self):
        sacrifice = diagnose_analysis(analysis([
            entry(1, 4, text=TOKEN, types="Sorcery"),
            entry(3, 2, text=SAC_OUTLET, types="Creature", power="1"),
            land(50),
        ]))
        spells = diagnose_analysis(analysis([
            entry(4, 4, text="Draw a card.", types="Instant"),
            entry(5, 2, text=SPELL_PAYOFF, types="Enchantment"),
            land(50),
        ]))
        self.assertEqual(sacrifice["plan"]["probable_plan"], "token_sacrifice")
        self.assertEqual(spells["plan"]["probable_plan"], "spells_matter")

    def test_insufficient_evidence_is_explicit(self):
        report = diagnose_analysis(analysis([land(60)]))
        self.assertEqual(report["status"], "insufficient_evidence")
        self.assertEqual(report["message"], "insufficient evidence to diagnose")
        self.assertIsNone(report["plan"]["probable_plan"])

    def test_resolution_and_unclassified_coverage_limit_confidence(self):
        unresolved_report = diagnose_analysis(analysis([
            entry(1, 4, text=TOKEN, types="Sorcery"),
            entry(2, 2, text=TOKEN_PAYOFF, types="Enchantment"),
            land(50),
        ], unresolved=4))
        self.assertEqual(unresolved_report["status"], "insufficient_evidence")
        candidate = next(c for c in unresolved_report["plan"]["candidates"]
                         if c["plan"] == "token_value")
        self.assertIn("resolution_coverage_below_95_percent", candidate["gate_failures"])

        low_coverage = diagnose_analysis(analysis([
            entry(1, 4, text=TOKEN, types="Sorcery"),
            entry(2, 2, text=TOKEN_PAYOFF, types="Enchantment"),
            entry(7, 50, text="Unsupported ability.", types="Enchantment"),
            land(4),
        ]))
        self.assertEqual(low_coverage["status"], "insufficient_evidence")
        candidate = next(c for c in low_coverage["plan"]["candidates"]
                         if c["plan"] == "token_value")
        self.assertIn("strategic_coverage_below_50_percent", candidate["gate_failures"])
        self.assertEqual(low_coverage["coverage"]["main"]["unsupported_text_copies"], 50)

    def test_unsupported_text_caps_high_confidence(self):
        report = diagnose_analysis(analysis([
            entry(1, 4, text=TOKEN + "\nUnsupported ability.", types="Sorcery"),
            entry(6, 4, text=TOKEN + "\nUnsupported ability.", types="Sorcery"),
            entry(2, 4, text=TOKEN_PAYOFF, types="Enchantment"),
            land(48),
        ]))
        self.assertEqual(report["status"], "diagnosed")
        self.assertEqual(report["plan"]["confidence"], "moderate")
        self.assertEqual(report["coverage"]["main"]["unsupported_nonland_coverage"], 0.666667)

    def test_resolution_gate_uses_exact_ratio_not_display_rounding(self):
        exact = diagnose_analysis(analysis([
            entry(1, 950_000, text=TOKEN, types="Sorcery"),
            entry(2, 950_000, text=TOKEN_PAYOFF, types="Enchantment"),
        ], unresolved=100_000))
        below = diagnose_analysis(analysis([
            entry(1, 950_000, text=TOKEN, types="Sorcery"),
            entry(2, 950_000, text=TOKEN_PAYOFF, types="Enchantment"),
        ], unresolved=100_001))
        self.assertEqual(exact["coverage"]["main"]["resolution_coverage"], 0.95)
        self.assertEqual(exact["status"], "diagnosed")
        self.assertEqual(below["coverage"]["main"]["resolution_coverage"], 0.95)
        self.assertEqual(below["status"], "insufficient_evidence")
        candidate = next(c for c in below["plan"]["candidates"]
                         if c["plan"] == "token_value")
        self.assertIn("resolution_coverage_below_95_percent", candidate["gate_failures"])

    def test_strategic_gate_uses_exact_ratio_not_display_rounding(self):
        exact = diagnose_analysis(analysis([
            entry(1, 500_000, text=TOKEN, types="Sorcery"),
            entry(2, 500_000, text=TOKEN_PAYOFF, types="Enchantment"),
            entry(7, 1_000_000, text="Unsupported ability.", types="Enchantment"),
        ]))
        below = diagnose_analysis(analysis([
            entry(1, 500_000, text=TOKEN, types="Sorcery"),
            entry(2, 500_000, text=TOKEN_PAYOFF, types="Enchantment"),
            entry(7, 1_000_001, text="Unsupported ability.", types="Enchantment"),
        ]))
        self.assertEqual(exact["coverage"]["main"]["strategic_coverage"], 0.5)
        self.assertEqual(exact["status"], "diagnosed")
        self.assertEqual(below["coverage"]["main"]["strategic_coverage"], 0.5)
        self.assertEqual(below["status"], "insufficient_evidence")
        candidate = next(c for c in below["plan"]["candidates"]
                         if c["plan"] == "token_value")
        self.assertIn("strategic_coverage_below_50_percent", candidate["gate_failures"])

    def test_high_confidence_ratio_boundaries_are_exact(self):
        support_exact = diagnose_analysis(analysis([
            entry(1, 100_000, text=TOKEN, types="Creature"),
            entry(6, 100_000, text=TOKEN, types="Creature"),
            entry(2, 200_000, text=TOKEN_PAYOFF, types="Enchantment"),
            entry(8, 1_600_000, types="Creature", power="1"),
        ]))
        support_below = diagnose_analysis(analysis([
            entry(1, 100_000, text=TOKEN, types="Creature"),
            entry(6, 100_000, text=TOKEN, types="Creature"),
            entry(2, 200_000, text=TOKEN_PAYOFF, types="Enchantment"),
            entry(8, 1_600_001, types="Creature", power="1"),
        ]))
        self.assertEqual(
            next(c for c in support_exact["plan"]["candidates"]
                 if c["plan"] == "token_value")["support_density"], 0.2,
        )
        self.assertEqual(support_exact["plan"]["confidence"], "high")
        self.assertEqual(
            next(c for c in support_below["plan"]["candidates"]
                 if c["plan"] == "token_value")["support_density"], 0.2,
        )
        self.assertEqual(support_below["plan"]["confidence"], "moderate")

        strategic_exact = diagnose_analysis(analysis([
            entry(1, 200_000, text=TOKEN, types="Creature"),
            entry(6, 200_000, text=TOKEN, types="Creature"),
            entry(2, 200_000, text=TOKEN_PAYOFF, types="Enchantment"),
            entry(8, 900_000, types="Creature", power="1"),
            entry(9, 500_000, types="Enchantment"),
        ]))
        strategic_below = diagnose_analysis(analysis([
            entry(1, 200_000, text=TOKEN, types="Creature"),
            entry(6, 200_000, text=TOKEN, types="Creature"),
            entry(2, 200_000, text=TOKEN_PAYOFF, types="Enchantment"),
            entry(8, 900_000, types="Creature", power="1"),
            entry(9, 500_001, types="Enchantment"),
        ]))
        self.assertEqual(strategic_exact["coverage"]["main"]["strategic_coverage"], 0.75)
        self.assertEqual(strategic_exact["plan"]["confidence"], "high")
        self.assertEqual(strategic_below["coverage"]["main"]["strategic_coverage"], 0.75)
        self.assertEqual(strategic_below["plan"]["confidence"], "moderate")

    def test_unsupported_text_cap_uses_exact_ratio(self):
        exact = diagnose_analysis(analysis([
            entry(1, 200_000, text=TOKEN, types="Creature"),
            entry(6, 200_000, text=TOKEN, types="Creature"),
            entry(2, 200_000, text=TOKEN_PAYOFF, types="Enchantment"),
            entry(8, 900_000, types="Creature", power="1"),
            entry(9, 500_000, text="Unsupported ability.", types="Creature", power="1"),
        ]))
        above = diagnose_analysis(analysis([
            entry(1, 200_000, text=TOKEN, types="Creature"),
            entry(6, 200_000, text=TOKEN, types="Creature"),
            entry(2, 200_000, text=TOKEN_PAYOFF, types="Enchantment"),
            entry(8, 900_000, types="Creature", power="1"),
            entry(9, 500_001, text="Unsupported ability.", types="Creature", power="1"),
        ]))
        self.assertEqual(exact["coverage"]["main"]["unsupported_nonland_coverage"], 0.25)
        self.assertEqual(exact["plan"]["confidence"], "high")
        self.assertEqual(above["coverage"]["main"]["unsupported_nonland_coverage"], 0.25)
        self.assertEqual(above["plan"]["confidence"], "moderate")

    def test_scaled_support_thresholds(self):
        source = analysis([
            entry(1, 4, text=TOKEN, types="Sorcery"),
            entry(2, 2, text=TOKEN_PAYOFF, types="Enchantment"),
            land(54),
        ])
        results = {
            size: next(c for c in diagnose_analysis(source, reference_main_size=size)["plan"]["candidates"]
                       if c["plan"] == "token_value")
            for size in (40, 60, 100)
        }
        self.assertEqual([results[size]["minimum_supporting_copies"] for size in (40, 60, 100)],
                         [4, 6, 10])
        self.assertTrue(results[40]["passes_plan_gate"])
        self.assertTrue(results[60]["passes_plan_gate"])
        self.assertFalse(results[100]["passes_plan_gate"])

    def test_supported_hybrid_and_close_ambiguity(self):
        hybrid = diagnose_analysis(analysis([
            entry(1, 4, text=TOKEN, types="Sorcery"),
            entry(2, 2, text=TOKEN_PAYOFF, types="Enchantment"),
            entry(5, 2, text=SPELL_PAYOFF, types="Enchantment"),
            land(52),
        ]))
        self.assertEqual(hybrid["status"], "diagnosed")
        self.assertEqual(hybrid["plan"]["state"], "hybrid")
        self.assertEqual(hybrid["plan"]["probable_plans"], ["spells_matter", "token_value"])

        ambiguous = diagnose_analysis(analysis([
            entry(1, 4, text=TOKEN, types="Sorcery"),
            entry(2, 2, text=TOKEN_PAYOFF, types="Enchantment"),
            entry(5, 1, text=SPELL_PAYOFF, types="Enchantment"),
            land(53),
        ]))
        self.assertEqual(ambiguous["status"], "insufficient_evidence")
        self.assertEqual(ambiguous["plan"]["state"], "ambiguous")
        self.assertIn("unknown.deck_plan_ambiguous.v1",
                      {unknown["id"] for unknown in ambiguous["unknowns"]})

    def test_clearly_stronger_plan_is_not_hidden_by_weaker_runner(self):
        report = diagnose_analysis(analysis([
            entry(1, 8, text=TOKEN, types="Creature"),
            entry(2, 4, text=TOKEN_PAYOFF, types="Enchantment"),
            entry(4, 4, text="Draw a card.", types="Instant"),
            entry(5, 1, text=SPELL_PAYOFF, types="Enchantment"),
            land(43),
        ]))
        self.assertEqual(report["status"], "diagnosed")
        self.assertEqual(report["plan"]["state"], "named")
        self.assertEqual(report["plan"]["probable_plan"], "token_value")

    def test_package_warnings_use_exact_relationship_sides(self):
        unsupported_payoff = diagnose_analysis(analysis([
            entry(2, 2, text=TOKEN_PAYOFF, types="Enchantment"), land(58),
        ]))
        unsupported_consumer = diagnose_analysis(analysis([
            entry(3, 2, text=SAC_OUTLET, types="Creature", power="1"), land(58),
        ]))
        disconnected = diagnose_analysis(analysis([
            entry(8, 4, text=TOKEN + "\n" + SAC_OUTLET, types="Creature", power="1"),
            land(56),
        ]))
        self.assertIn("warning.unsupported_payoff.v1",
                      {warning["id"] for warning in unsupported_payoff["warnings"]})
        self.assertIn("warning.unsupported_consumer.v1",
                      {warning["id"] for warning in unsupported_consumer["warnings"]})
        self.assertIn("warning.disconnected_package.v1",
                      {warning["id"] for warning in disconnected["warnings"]})

    def test_role_redundancy_and_concentration_are_interpretations(self):
        report = diagnose_analysis(analysis([
            entry(10, 2, text="Draw a card.", types="Sorcery"),
            entry(11, 2, text="Draw two cards.", types="Sorcery"),
            entry(12, 2, text="Destroy target creature.", types="Sorcery"),
            land(54),
        ]))
        interpretations = {(item["id"], item["role"]) for item in report["interpretations"]}
        self.assertIn(("interpretation.role_redundancy.v1", "card_draw"), interpretations)
        self.assertIn(("interpretation.role_concentration.v1", "removal"), interpretations)
        self.assertFalse(any("redundancy" in warning["id"] or "concentration" in warning["id"]
                             for warning in report["warnings"]))

    def test_curve_facts_and_explicit_unknowns(self):
        report = diagnose_analysis(analysis([
            entry(10, 2, text="Draw a card.", types="Sorcery", cmc=2),
            entry(11, 3, text="Destroy target creature.", types="Sorcery", cmc=5),
            land(55),
        ]))
        curve = next(fact for fact in report["facts"] if fact["id"] == "fact.nonland_curve.v1")
        self.assertEqual(curve["mana_value_counts"], {"2": 2, "5": 3})
        self.assertEqual(curve["weighted_median_mana_value"], 5)
        self.assertEqual(curve["interpretation"], "descriptive_only")
        unknown_ids = {item["id"] for item in report["unknowns"]}
        self.assertIn("unknown.mana_source_adequacy.v1", unknown_ids)
        self.assertIn("unknown.mechanical_conflicts.v1", unknown_ids)

    def test_sideboard_and_commander_do_not_influence_main_plan(self):
        report = diagnose_analysis(analysis(
            [land(60)],
            sideboard_entries=[entry(1, 4, text=TOKEN, types="Sorcery")],
            commander_entries=[entry(2, 1, text=TOKEN_PAYOFF, types="Enchantment")],
        ))
        self.assertEqual(report["status"], "insufficient_evidence")
        unknown_ids = {item["id"] for item in report["unknowns"]}
        self.assertIn("unknown.sideboard_purpose.v1", unknown_ids)
        self.assertIn("unknown.commander_integration.v1", unknown_ids)

    def test_input_is_not_mutated_and_versions_are_required(self):
        source = analysis([land(60)])
        before = deepcopy(source)
        diagnose_analysis(source)
        self.assertEqual(source, before)
        with self.assertRaises(ValueError):
            diagnose_analysis({**source, "analysis_version": "999"})
        with self.assertRaises(ValueError):
            diagnose_analysis({**source, "legality": "evaluated"})

    def test_multiple_printings_do_not_inflate_distinct_title_support(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "cards.db"
            con = sqlite3.connect(database)
            con.executescript(canonical.SCHEMA)
            canonical.load_cards(con, [
                Card(1, "Synthetic Maker", cmc=1, types="Sorcery", rules_text=TOKEN),
                Card(2, "Synthetic Payoff", cmc=2, types="Enchantment",
                     rules_text=TOKEN_PAYOFF),
                Card(3, "Synthetic Land", types="Land"),
            ])
            canonical.load_printings(con, [
                CardPrinting(101, 1), CardPrinting(102, 1),
                CardPrinting(201, 2), CardPrinting(301, 3),
            ])
            con.commit()
            con.close()
            con = canonical.open_db(database)
            try:
                report = diagnose_analysis(analyze_deck(Deck(
                    main={101: 2, 102: 2, 201: 2, 301: 54},
                ), con))
            finally:
                con.close()
            candidate = next(c for c in report["plan"]["candidates"]
                             if c["plan"] == "token_value")
            self.assertEqual(candidate["source_title_ids"], [1])
            self.assertEqual(candidate["supporting_title_ids"], [1, 2])
            self.assertEqual(candidate["supporting_title_count"], 2)
            self.assertEqual(candidate["supporting_copies"], 6)

    def test_cli_diagnoses_without_writing_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "cards.db"
            deck = root / "deck.txt"
            con = sqlite3.connect(database)
            con.executescript(canonical.SCHEMA)
            canonical.load_cards(con, [
                Card(1, "Synthetic Maker", "{1}", 1, "Sorcery", rules_text=TOKEN),
                Card(2, "Synthetic Payoff", "{2}", 2, "Enchantment",
                     rules_text=TOKEN_PAYOFF),
                Card(3, "Synthetic Land", types="Land"),
            ])
            canonical.load_printings(con, [
                CardPrinting(101, 1), CardPrinting(201, 2), CardPrinting(301, 3),
            ])
            con.commit()
            con.close()
            deck.write_text(
                "Deck\n4 Synthetic Maker\n2 Synthetic Payoff\n54 Synthetic Land\n",
                encoding="utf-8",
            )
            before = database.read_bytes()
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["--database", str(database), "diagnose-deck", str(deck)])
            result = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(result["status"], "diagnosed")
            self.assertEqual(result["plan"]["probable_plan"], "token_value")
            self.assertEqual(database.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
