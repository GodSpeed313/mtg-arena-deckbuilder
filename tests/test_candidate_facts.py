from __future__ import annotations

from copy import deepcopy
import sqlite3
import unittest
from unittest.mock import patch

from mtgadb import canonical
from mtgadb.model import Card, CardPrinting, Collection, Deck, Format
from services.candidate_facts import (
    CANDIDATE_FACTS_MODEL_VERSION,
    derive_candidate_facts,
)
from services.candidates import discover_candidates
from services.diagnosis import diagnose_analysis
from services.intelligence import analyze_deck, classify_card


LIFE_PAYOFF = "Whenever you gain life, draw a card."
TOKEN_PAYOFF = (
    "Whenever a creature token enters the battlefield under your control, draw a card."
)
SACRIFICE = "Sacrifice another creature: Draw a card."
SPELL_PAYOFF = "Whenever you cast an instant or sorcery spell, draw a card."


class CandidateFactsTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(canonical.SCHEMA)
        canonical.load_cards(self.con, [
            Card(1, "Life Payoff", types="Enchantment", rules_text=LIFE_PAYOFF),
            Card(
                2,
                "Vital Study",
                mana_cost="{1}{W}",
                cmc=2,
                types="Sorcery",
                color_identity="W",
                rules_text="You gain 3 life.\nDraw a card.",
            ),
            Card(3, "Token Payoff", types="Enchantment", rules_text=TOKEN_PAYOFF),
            Card(4, "Sacrifice Outlet", types="Creature", rules_text=SACRIFICE),
            Card(
                5,
                "Shared Token Maker",
                mana_cost="{2}{W}",
                cmc=3,
                types="Sorcery",
                color_identity="W",
                rules_text="Create a 1/1 white Soldier creature token.",
            ),
            Card(
                6,
                "Unsupported Sage",
                types="Enchantment",
                rules_text="Whenever life is gained, celebrate.",
            ),
            Card(
                7,
                "Spare Life",
                mana_cost="{W}",
                cmc=1,
                types="Sorcery",
                color_identity="W",
                rules_text="You gain 1 life.",
            ),
            Card(8, "Spell Payoff", types="Enchantment", rules_text=SPELL_PAYOFF),
        ])
        printings = [
            CardPrinting(title_id * 100 + 1, title_id, "LEG", str(title_id), "common")
            for title_id in range(1, 9)
        ]
        printings.append(CardPrinting(202, 2, "OLD", "202", "rare"))
        canonical.load_printings(self.con, printings)
        canonical.load_formats(self.con, {
            "Test": Format("Test", legal_sets=frozenset({"LEG"})),
        })
        self.con.commit()
        self.addCleanup(self.con.close)

    @staticmethod
    def multi_need_deck() -> Deck:
        return Deck(main={101: 1, 301: 1, 401: 1})

    def candidates(
        self,
        deck: Deck | None = None,
        *,
        collection: Collection | None = None,
        limit_per_need: int | None = None,
    ) -> tuple[dict, dict]:
        deck = deck or self.multi_need_deck()
        analysis = analyze_deck(deck, self.con)
        pools = discover_candidates(
            analysis,
            deck,
            self.con,
            format_name="Test",
            collection=collection,
            limit_per_need=limit_per_need,
        )
        return analysis, pools

    @staticmethod
    def title(result: dict, title_id: int) -> dict:
        return next(
            item for item in result["candidate_facts_by_title"]
            if item["title_id"] == title_id
        )

    def test_version_input_contract_and_deterministic_output(self):
        _, pools = self.candidates()
        first = derive_candidate_facts(pools, self.con)
        second = derive_candidate_facts(pools, self.con)
        self.assertEqual(CANDIDATE_FACTS_MODEL_VERSION, "2")
        self.assertEqual(first, second)
        self.assertEqual(first["candidate_facts_model_version"], "2")
        self.assertEqual(first["source_candidate_model_version"], "2")
        self.assertEqual(first["functional_package_model_version"], "1")
        self.assertEqual(
            first["candidate_title_count"], len(first["candidate_facts_by_title"])
        )

        wrong = deepcopy(pools)
        wrong["candidate_model_version"] = "1"
        with self.assertRaises(ValueError):
            derive_candidate_facts(wrong, self.con)

        future_trigger = deepcopy(pools)
        future_trigger["trigger_finding_type"] = "future_finding"
        with self.assertRaises(ValueError):
            derive_candidate_facts(future_trigger, self.con)

    def test_identity_canonical_fields_features_and_package_evidence(self):
        _, pools = self.candidates()
        result = derive_candidate_facts(pools, self.con)
        vital = self.title(result, 2)
        self.assertEqual(vital["title_id"], 2)
        self.assertEqual(vital["name"], "Vital Study")
        self.assertEqual(vital["canonical_facts"], {
            "title_id": 2,
            "name": "Vital Study",
            "mana_cost": "{1}{W}",
            "mana_value": 2,
            "types": "Sorcery",
            "subtypes": "",
            "colors": "",
            "color_identity": "W",
            "power": "",
            "toughness": "",
        })
        feature_ids = {feature["rule_id"] for feature in vital["reviewed_features"]}
        self.assertEqual(
            feature_ids,
            {"effect.draw.v1", "effect.lifegain.v1", "type.spells.v1"},
        )
        packages = {row["label"]: row for row in vital["functional_packages"]}
        self.assertEqual(
            set(packages), {"card_advantage", "lifegain", "spell_matters"},
        )
        self.assertEqual(
            {feature["rule_id"] for feature in packages["card_advantage"]["evidence"]},
            {"effect.draw.v1"},
        )
        self.assertEqual(
            {feature["rule_id"] for feature in packages["lifegain"]["evidence"]},
            {"effect.lifegain.v1"},
        )

    def test_source_provenance_and_exact_need_match_are_preserved(self):
        _, pools = self.candidates()
        result = derive_candidate_facts(pools, self.con)
        life_pool = next(
            pool for pool in result["per_need"]
            if pool["source_need"]["dependency_label"] == "lifegain"
        )
        vital = next(card for card in life_pool["candidates"] if card["title_id"] == 2)
        source = vital["source_need"]
        self.assertEqual(source["finding_id"], "need.lifegain.enabler.v1")
        self.assertEqual(source["dependency_id"], "dependency.lifegain.v1")
        self.assertEqual(source["zone"], "main")
        self.assertEqual(source["missing_side_name"], "enabler")
        self.assertEqual(source["required_feature_rule_ids"], ["effect.lifegain.v1"])
        self.assertEqual(source["required_relationship"], "producer")
        self.assertEqual(
            [(feature["rule_id"], feature["relationship"])
             for feature in vital["matching_feature_evidence"]],
            [("effect.lifegain.v1", "producer")],
        )

    def test_optional_support_does_not_create_candidate_fact_occurrence(self):
        _, pools = self.candidates()
        result = derive_candidate_facts(pools, self.con)
        token = self.title(result, 5)
        self.assertEqual(token["matched_need_ids"], [
            "need.creature_token_entry.enabler.v2",
        ])
        self.assertEqual(token["matched_dependency_ids"], [
            "dependency.creature_token_entry.v2",
        ])
        self.assertEqual(len(token["per_need_matches"]), 1)
        self.assertEqual(
            {match["source_need"]["dependency_label"]
             for match in token["per_need_matches"]},
            {"creature_token_entry"},
        )
        self.assertTrue(all(
            {feature["rule_id"] for feature in match["matching_feature_evidence"]}
            == {"effect.token.v1"}
            for match in token["per_need_matches"]
        ))

    def test_per_need_evidence_may_differ_without_contradicting_shared_facts(self):
        deck = Deck(main={101: 1, 801: 1})
        _, pools = self.candidates(deck)
        result = derive_candidate_facts(pools, self.con)
        vital = self.title(result, 2)
        self.assertEqual(vital["matched_need_ids"], [
            "need.lifegain.enabler.v1",
            "need.spell_cast.enabler.v1",
        ])
        evidence_by_dependency = {
            match["source_need"]["dependency_label"]: {
                feature["rule_id"] for feature in match["matching_feature_evidence"]
            }
            for match in vital["per_need_matches"]
        }
        self.assertEqual(evidence_by_dependency, {
            "lifegain": {"effect.lifegain.v1"},
            "spell_cast": {"type.spells.v1"},
        })
        self.assertNotIn("required_feature", vital["eligibility"])

    def test_printings_rarity_and_neutral_ordering(self):
        _, pools = self.candidates()
        result = derive_candidate_facts(pools, self.con)
        titles = result["candidate_facts_by_title"]
        self.assertEqual(
            [(item["name"].casefold(), item["title_id"]) for item in titles],
            sorted((item["name"].casefold(), item["title_id"]) for item in titles),
        )
        vital = self.title(result, 2)
        self.assertEqual(
            [(row["arena_id"], row["rarity"]) for row in vital["known_printings"]],
            [(201, "common"), (202, "rare")],
        )
        self.assertEqual(sum(item["title_id"] == 2 for item in titles), 1)
        self.assertEqual(
            result["ordering"], "casefolded_card_name_then_title_id_non_ranking",
        )

    def test_eligibility_ownership_copy_and_crafting_are_carried_forward(self):
        _, pools = self.candidates(collection=Collection())
        result = derive_candidate_facts(pools, self.con)
        source_vital = next(
            candidate
            for pool in pools["pools"]
            for candidate in pool["candidates"]
            if candidate["title_id"] == 2
        )
        vital = self.title(result, 2)
        self.assertEqual(vital["eligibility_status"], source_vital["eligibility_status"])
        self.assertEqual(vital["eligibility"], {
            key: source_vital["eligibility"][key]
            for key in (
                "format_legality", "color_identity", "playset", "ownership", "crafting"
            )
        })
        per_need_vital = next(
            candidate
            for pool in result["per_need"]
            for candidate in pool["candidates"]
            if candidate["title_id"] == 2
        )
        self.assertEqual(per_need_vital["eligibility"], source_vital["eligibility"])
        self.assertEqual(vital["unresolved_eligibility"], [])
        self.assertEqual(vital["eligibility"]["format_legality"]["status"], "legal")
        self.assertEqual(vital["eligibility"]["color_identity"]["status"], "not_applicable")
        self.assertEqual(vital["eligibility"]["playset"]["current_deck_copies"], 0)
        self.assertEqual(vital["eligibility"]["playset"]["remaining_capacity"], 4)
        self.assertEqual(vital["eligibility"]["ownership"], {
            "status": "known", "owned_copies": 0,
        })
        self.assertEqual(vital["eligibility"]["crafting"], {"status": "not_evaluated"})

        _, unknown_pools = self.candidates()
        unknown = self.title(derive_candidate_facts(unknown_pools, self.con), 2)
        self.assertEqual(unknown["eligibility"]["ownership"], {
            "status": "unknown", "owned_copies": None,
        })

    def test_source_truncation_is_preserved(self):
        deck = Deck(main={101: 1})
        _, pools = self.candidates(deck, limit_per_need=1)
        source_pool = next(
            pool for pool in pools["pools"]
            if pool["source_need"]["dependency_label"] == "lifegain"
        )
        self.assertTrue(source_pool["summary"]["truncated"])
        result = derive_candidate_facts(pools, self.con)
        fact_pool = next(
            pool for pool in result["per_need"]
            if pool["source_need"]["dependency_label"] == "lifegain"
        )
        self.assertEqual(fact_pool["source_pool_summary"], source_pool["summary"])
        self.assertTrue(fact_pool["source_pool_summary"]["truncated"])
        self.assertEqual(len(fact_pool["candidates"]), 1)

    def test_only_returned_unique_titles_are_classified(self):
        _, pools = self.candidates(limit_per_need=1)
        returned_ids = {
            candidate["title_id"]
            for pool in pools["pools"]
            for candidate in pool["candidates"]
        }
        with patch("services.candidate_facts.classify_card", wraps=classify_card) as mocked:
            result = derive_candidate_facts(pools, self.con)
        self.assertEqual(mocked.call_count, len(returned_ids))
        self.assertEqual(
            {item["title_id"] for item in result["candidate_facts_by_title"]},
            returned_ids,
        )

    def test_unsupported_text_does_not_create_candidate_capabilities(self):
        _, pools = self.candidates()
        result = derive_candidate_facts(pools, self.con)
        self.assertNotIn(6, {
            item["title_id"] for item in result["candidate_facts_by_title"]
        })
        self.assertEqual(classify_card(
            Card(6, "Unsupported Sage", types="Enchantment",
                 rules_text="Whenever life is gained, celebrate.")
        )["features"], [
            {
                "rule_id": "type.enchantments.v1",
                "dimension": "theme",
                "label": "enchantments",
                "relationship": "member",
                "evidence": "Enchantment",
                "explanation": "Card has the Enchantment type; no payoff is inferred.",
            }
        ])

    def test_read_only_and_existing_outputs_remain_unchanged(self):
        deck = self.multi_need_deck()
        analysis, pools = self.candidates(deck)
        analysis_before = deepcopy(analysis)
        pools_before = deepcopy(pools)
        deck_before = deepcopy(deck)
        database_before = tuple(self.con.iterdump())
        diagnosis_before = diagnose_analysis(analysis)
        derive_candidate_facts(pools, self.con)
        self.assertEqual(analysis, analysis_before)
        self.assertEqual(pools, pools_before)
        self.assertEqual(deck, deck_before)
        self.assertEqual(tuple(self.con.iterdump()), database_before)
        self.assertEqual(diagnose_analysis(analysis), diagnosis_before)
        self.assertEqual(analysis["needs_model_version"], "2")
        self.assertEqual(analysis["dependency_model_version"], "2")
        self.assertEqual(analysis["functional_package_model_version"], "1")

    def test_contradictory_duplicate_candidate_facts_fail_closed(self):
        _, pools = self.candidates(Deck(main={101: 1, 801: 1}))
        token_occurrences = [
            candidate
            for pool in pools["pools"]
            for candidate in pool["candidates"]
            if candidate["title_id"] == 2
        ]
        self.assertEqual(len(token_occurrences), 2)
        token_occurrences[1]["eligibility"]["ownership"] = {
            "status": "known", "owned_copies": 0,
        }
        with self.assertRaises(ValueError):
            derive_candidate_facts(pools, self.con)

    def test_output_has_no_strategic_fields(self):
        _, pools = self.candidates()
        result = derive_candidate_facts(pools, self.con)
        forbidden = {
            "rank", "ranking", "score", "tier", "recommendation", "replacement",
            "best", "preferred", "efficiency", "quality", "power_score",
            "synergy_score", "fit_score", "overlap_score",
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
