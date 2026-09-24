from __future__ import annotations

from copy import deepcopy
import sqlite3
import unittest
from unittest.mock import patch

from mtgadb import canonical
from mtgadb.deck_identity import build_deck_snapshot_identity
from mtgadb.model import Card, CardPrinting, Collection, Deck, Format
from services.candidates import CANDIDATE_MODEL_VERSION, discover_candidates
from services.diagnosis import diagnose_analysis
from services.intelligence import analyze_deck


LIFE_PAYOFF = "Whenever you gain life, draw a card."
COUNTER_PAYOFF = (
    "Whenever one or more +1/+1 counters are put on a creature you control, "
    "draw a card."
)
TOKEN_PAYOFF = (
    "Whenever a creature token enters the battlefield under your control, draw a card."
)
SACRIFICE = "Sacrifice another creature: Draw a card."
SPELL_PAYOFF = "Whenever you cast an instant or sorcery spell, draw a card."


class CandidatePoolTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(canonical.SCHEMA)
        canonical.load_cards(self.con, [
            Card(1, "Life Payoff", types="Enchantment", rules_text=LIFE_PAYOFF),
            Card(2, "Alpha Life", types="Sorcery", color_identity="W",
                 rules_text="You gain 3 life."),
            Card(3, "Lifelink Body", types="Creature", rules_text="Lifelink"),
            Card(4, "Suggestive Words", types="Enchantment",
                 rules_text="Whenever life is gained, celebrate."),
            Card(5, "Counter Payoff", types="Enchantment", rules_text=COUNTER_PAYOFF),
            Card(6, "Beta Counter", types="Sorcery",
                 rules_text="Put a +1/+1 counter on target creature you control."),
            Card(7, "Token Payoff", types="Enchantment", rules_text=TOKEN_PAYOFF),
            Card(8, "Gamma Token", types="Instant",
                 rules_text="Create a 1/1 white Soldier creature token."),
            Card(9, "Treasure Spell", types="Sorcery",
                 rules_text="Create two Treasure tokens."),
            Card(10, "Sacrifice Outlet", types="Creature", rules_text=SACRIFICE),
            Card(11, "Spell Payoff", types="Enchantment", rules_text=SPELL_PAYOFF),
            Card(12, "Delta Spell", types="Instant", rules_text="Draw a card."),
            Card(13, "Blue Life", types="Sorcery", color_identity="U",
                 rules_text="You gain 2 life."),
            Card(14, "Banned Life", types="Sorcery",
                 rules_text="You gain 1 life."),
            Card(15, "Synthetic Land", types="Land"),
            Card(16, "Echo Life", types="Sorcery",
                 rules_text="You gain 2 life.\nYou gain 3 life."),
        ])
        printings = [
            CardPrinting(title_id * 100 + 1, title_id, "LEG", str(title_id), "common")
            for title_id in range(1, 17)
        ]
        printings.append(CardPrinting(202, 2, "OLD", "202", "common"))
        canonical.load_printings(self.con, printings)
        self.format = Format(
            "Test",
            legal_sets=frozenset({"LEG"}),
            banned_title_ids=frozenset({14}),
            individual_card_quotas={16: 0},
        )
        canonical.load_formats(self.con, {self.format.name: self.format})
        self.con.commit()
        self.addCleanup(self.con.close)

    def analysis_for(self, deck: Deck) -> dict:
        return analyze_deck(deck, self.con)

    def pool(self, result: dict, label: str) -> dict:
        return next(
            pool for pool in result["pools"]
            if pool["source_need"]["dependency_label"] == label
        )

    def test_version_stability_trigger_and_neutral_ordering(self):
        deck = Deck(main={101: 2, 1501: 58})
        first = discover_candidates(
            self.analysis_for(deck), deck, self.con, format_name="Test",
        )
        second = discover_candidates(
            self.analysis_for(deck), deck, self.con, format_name="Test",
        )
        self.assertEqual(CANDIDATE_MODEL_VERSION, "3")
        self.assertEqual(first, second)
        self.assertEqual(first["candidate_model_version"], "3")
        self.assertEqual(first["analyzed_deck_identity"], build_deck_snapshot_identity(deck))
        self.assertEqual(first["trigger_finding_type"], "support_need")
        self.assertEqual(first["ordering"], "casefolded_card_name_then_title_id_non_ranking")
        names = [card["name"] for card in self.pool(first, "lifegain")["candidates"]]
        self.assertEqual(names, sorted(names, key=str.casefold))

    def test_discovery_rejects_a_deck_different_from_the_analyzed_snapshot(self):
        analyzed = Deck(main={101: 2, 1501: 58})
        changed = Deck(main={101: 2, 1501: 57}, sideboard={1501: 1})
        with self.assertRaisesRegex(ValueError, "differs from analyzed Deck snapshot"):
            discover_candidates(
                self.analysis_for(analyzed), changed, self.con, format_name="Test",
            )

    def test_lifegain_uses_exact_reviewed_evidence_and_canonical_identity(self):
        deck = Deck(main={101: 2, 1501: 58})
        result = discover_candidates(
            self.analysis_for(deck), deck, self.con, format_name="Test",
        )
        pool = self.pool(result, "lifegain")
        candidates = {card["title_id"]: card for card in pool["candidates"]}
        self.assertIn(2, candidates)
        self.assertIn(13, candidates)
        self.assertNotIn(3, candidates)  # lifelink is not a producer
        self.assertNotIn(4, candidates)  # suggestive unsupported text is not proof
        self.assertNotIn(14, candidates)  # banned
        self.assertNotIn(16, candidates)  # format quota is zero
        alpha = candidates[2]
        self.assertEqual(
            {feature["rule_id"] for feature in alpha["matching_feature_evidence"]},
            {"effect.lifegain.v1"},
        )
        self.assertEqual(alpha["matching_feature_evidence"][0]["relationship"], "producer")
        self.assertEqual(alpha["source_need"]["finding_id"], "need.lifegain.enabler.v1")
        self.assertEqual(alpha["source_need"]["dependency_id"], "dependency.lifegain.v1")
        self.assertEqual(
            alpha["source_need"]["missing_side"]["feature_rule_ids"],
            ["effect.lifegain.v1"],
        )
        self.assertEqual(alpha["title_id"], 2)
        self.assertEqual(
            [printing["arena_id"] for printing in alpha["known_printings"]],
            [201, 202],
        )
        self.assertEqual(
            alpha["eligibility"]["format_legality"]["eligible_printing_ids"], [201],
        )

    def test_counter_token_and_spell_needs_match_existing_feature_contract(self):
        cases = (
            (Deck(main={501: 2, 1501: 58}), "plus1_counters", {6}),
            (Deck(main={701: 2, 1501: 58}), "creature_token_entry", {8}),
        )
        for deck, label, expected in cases:
            with self.subTest(label=label):
                result = discover_candidates(
                    self.analysis_for(deck), deck, self.con, format_name="Test",
                )
                self.assertEqual(
                    {card["title_id"] for card in self.pool(result, label)["candidates"]},
                    expected,
                )

        spell_deck = Deck(main={1101: 2, 1501: 58})
        spell = discover_candidates(
            self.analysis_for(spell_deck), spell_deck, self.con, format_name="Test",
        )
        spell_ids = {
            card["title_id"] for card in self.pool(spell, "spell_cast")["candidates"]
        }
        self.assertIn(12, spell_ids)
        self.assertIn(9, spell_ids)
        self.assertNotIn(4, spell_ids)

    def test_named_noncreature_token_does_not_match_creature_token_need(self):
        deck = Deck(main={701: 2, 1501: 58})
        result = discover_candidates(
            self.analysis_for(deck), deck, self.con, format_name="Test",
        )
        ids = {card["title_id"] for card in self.pool(result, "creature_token_entry")["candidates"]}
        self.assertIn(8, ids)
        self.assertNotIn(9, ids)

    def test_opportunities_do_not_trigger_and_unknown_types_fail_closed(self):
        deck = Deck(main={201: 2, 1501: 58})
        analysis = self.analysis_for(deck)
        self.assertTrue(analysis["zones"]["main"]["needs"])
        with patch("services.candidates.classify_card") as classifier:
            result = discover_candidates(analysis, deck, self.con, format_name="Test")
        classifier.assert_not_called()
        self.assertEqual(result["pools"], [])
        self.assertGreater(result["ignored_non_trigger_findings"], 0)

        changed = deepcopy(analysis)
        changed["zones"]["main"]["needs"][0]["finding_type"] = "future_type"
        with self.assertRaises(ValueError):
            discover_candidates(changed, deck, self.con, format_name="Test")

    def test_optional_sacrifice_support_does_not_trigger_candidate_discovery(self):
        deck = Deck(main={701: 2, 1001: 2, 1501: 56})
        result = discover_candidates(
            self.analysis_for(deck), deck, self.con, format_name="Test",
        )
        self.pool(result, "creature_token_entry")
        self.assertFalse(any(
            pool["source_need"]["dependency_label"] == "creature_token_sacrifice"
            for pool in result["pools"]
        ))
        self.assertGreaterEqual(result["ignored_non_trigger_findings"], 1)

    def test_format_unknown_legal_and_illegal_states_are_conservative(self):
        deck = Deck(main={101: 2, 1501: 58})
        analysis = self.analysis_for(deck)
        unknown = discover_candidates(analysis, deck, self.con)
        unknown_candidates = self.pool(unknown, "lifegain")["candidates"]
        self.assertTrue(unknown_candidates)
        self.assertTrue(all(
            card["eligibility_status"] == "eligibility_unknown"
            and card["eligibility"]["format_legality"]["status"] == "unknown"
            for card in unknown_candidates
        ))

        known = discover_candidates(analysis, deck, self.con, format_name="Test")
        known_candidates = self.pool(known, "lifegain")["candidates"]
        self.assertTrue(all(card["eligibility_status"] == "eligible"
                            for card in known_candidates))
        summary = self.pool(known, "lifegain")["summary"]
        self.assertGreaterEqual(summary["excluded_reasons"]["format_illegal"], 1)

    def test_explicit_color_identity_and_playset_filters(self):
        deck = Deck(main={101: 2, 1501: 58})
        result = discover_candidates(
            self.analysis_for(deck), deck, self.con,
            format_name="Test", allowed_colors=frozenset("W"),
        )
        ids = {card["title_id"] for card in self.pool(result, "lifegain")["candidates"]}
        self.assertIn(2, ids)
        self.assertNotIn(13, ids)
        summary = self.pool(result, "lifegain")["summary"]
        self.assertGreaterEqual(summary["excluded_reasons"]["color_identity_incompatible"], 1)
        self.assertGreaterEqual(summary["excluded_reasons"]["playset_cap_reached"], 1)

    def test_ownership_unknown_and_known_zero_are_distinct_and_nonfiltering(self):
        deck = Deck(main={101: 2, 1501: 58})
        analysis = self.analysis_for(deck)
        unknown = discover_candidates(analysis, deck, self.con, format_name="Test")
        known_zero = discover_candidates(
            analysis, deck, self.con, format_name="Test", collection=Collection(),
        )
        unknown_alpha = next(
            card for card in self.pool(unknown, "lifegain")["candidates"]
            if card["title_id"] == 2
        )
        zero_alpha = next(
            card for card in self.pool(known_zero, "lifegain")["candidates"]
            if card["title_id"] == 2
        )
        self.assertEqual(unknown_alpha["eligibility"]["ownership"], {
            "status": "unknown", "owned_copies": None,
        })
        self.assertEqual(zero_alpha["eligibility"]["ownership"], {
            "status": "known", "owned_copies": 0,
        })
        self.assertEqual(unknown_alpha["eligibility_status"], "eligible")
        self.assertEqual(zero_alpha["eligibility_status"], "eligible")

    def test_discovery_is_read_only_and_preserves_existing_outputs(self):
        deck = Deck(main={701: 4, 1001: 2, 1501: 54})
        analysis = self.analysis_for(deck)
        analysis_before = deepcopy(analysis)
        deck_before = deepcopy(deck)
        database_before = tuple(self.con.iterdump())
        diagnosis_before = diagnose_analysis(analysis)
        result = discover_candidates(
            analysis, deck, self.con, format_name="Test", limit_per_need=1,
        )
        self.assertEqual(analysis, analysis_before)
        self.assertEqual(deck, deck_before)
        self.assertEqual(tuple(self.con.iterdump()), database_before)
        self.assertEqual(diagnose_analysis(analysis), diagnosis_before)
        self.assertEqual(analysis["needs_model_version"], "2")
        self.assertEqual(analysis["dependency_model_version"], "2")
        self.assertEqual(analysis["functional_package_model_version"], "1")
        self.assertEqual(
            {row["rule_id"] for row in analysis["interactions"]},
            set(),
        )
        self.assertTrue(all(pool["summary"]["returned"] <= 1 for pool in result["pools"]))

    def test_output_contains_no_ranking_scoring_or_recommendations(self):
        deck = Deck(main={101: 2, 1501: 58})
        result = discover_candidates(
            self.analysis_for(deck), deck, self.con, format_name="Test",
        )
        forbidden = {
            "rank", "ranking", "score", "tier", "recommendation", "replacement",
            "best", "quality", "power_score", "synergy_score", "fit_score",
        }

        def keys(value):
            if isinstance(value, dict):
                return set(value) | set().union(*(keys(item) for item in value.values()))
            if isinstance(value, list):
                return set().union(*(keys(item) for item in value), set())
            return set()

        self.assertTrue(forbidden.isdisjoint(keys(result)))


if __name__ == "__main__":
    unittest.main()
