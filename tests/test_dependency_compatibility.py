from __future__ import annotations

import sqlite3
import unittest
from copy import deepcopy

from mtgadb import canonical
from mtgadb.model import Card, CardPrinting, Deck
from services.candidates import discover_candidates
from services.candidate_facts import derive_candidate_facts
from services.dependencies import dependency_findings
from services.intelligence import analyze_deck, classify_card, interactions
from services.needs import needs_findings


CREATURE_TOKEN_PAYOFF = (
    "Whenever a creature token enters the battlefield under your control, draw a card."
)
ANY_TOKEN_PAYOFF = "Whenever one or more tokens you control enter, draw a card."
SACRIFICE = "Sacrifice another creature: Draw a card."


def classified(title_id: int, name: str, text: str, types: str = "Enchantment") -> dict:
    result = classify_card(Card(title_id, name, types=types, rules_text=text))
    result["quantity"] = 1
    return result


def finding(rows: list[dict], label: str) -> dict:
    return next(item for item in dependency_findings(rows) if item["label"] == label)


class CompatibilityModelTests(unittest.TestCase):
    def test_generic_sacrifice_has_only_optional_token_support(self):
        ordinary = classified(1, "Ordinary", "", "Creature")
        outlet = classified(2, "Outlet", SACRIFICE, "Creature")
        result = finding([ordinary, outlet], "creature_token_sacrifice")
        self.assertEqual(result["policy"], "optional_support")
        self.assertEqual(result["state"], "optional_support_absent")
        needs = needs_findings([result], {"claim_scope": "reviewed_features_only"})
        self.assertEqual(needs[0]["finding_type"], "optional_support_observation")
        self.assertFalse(any(item["finding_type"] == "support_need" for item in needs))
        serialized = repr(result).casefold()
        self.assertNotIn("threshold", serialized)
        self.assertNotIn("ratio", serialized)

    def test_token_sacrifice_interaction_remains_recognized(self):
        token = classified(1, "Maker", "Create a 1/1 white Soldier creature token.", "Sorcery")
        outlet = classified(2, "Outlet", SACRIFICE, "Creature")
        result = finding([token, outlet], "creature_token_sacrifice")
        self.assertEqual(result["state"], "supported")
        self.assertEqual(
            {item["rule_id"] for item in interactions([token, outlet])},
            {"interaction.token_sacrifice.v1"},
        )

    def test_counter_subject_target_compatibility(self):
        broad = classified(
            1, "Broad Listener",
            "Whenever one or more +1/+1 counters are put on a creature you control, draw a card.",
        )
        target = classified(
            2, "Targeter", "Put a +1/+1 counter on target creature you control.", "Sorcery",
        )
        self.assertEqual(finding([broad, target], "plus1_counters")["state"], "supported")

        self_listener = classified(
            3, "Listener",
            "Whenever one or more +1/+1 counters are put on Listener, draw a card.", "Creature",
        )
        unrelated_self = classified(
            4, "Other", "Put a +1/+1 counter on Other.", "Creature",
        )
        incompatible = finding([self_listener, unrelated_self], "plus1_counters")
        self.assertEqual(incompatible["state"], "payoff_without_compatible_enabler")
        self.assertEqual(incompatible["compatible_pairs"], [])

        compatible = finding([self_listener, target], "plus1_counters")
        self.assertEqual(compatible["state"], "supported")
        self.assertEqual(compatible["compatible_pairs"][0]["payoff_title_id"], 3)

    def test_token_listener_scope_uses_compatible_producers(self):
        creature = classified(1, "Creature Maker", "Create a 1/1 white Soldier creature token.", "Sorcery")
        treasure = classified(2, "Treasure Maker", "Create a Treasure token.", "Sorcery")
        creature_listener = classified(3, "Creature Listener", CREATURE_TOKEN_PAYOFF)
        any_listener = classified(4, "Any Listener", ANY_TOKEN_PAYOFF)
        self.assertEqual(finding([creature, creature_listener], "creature_token_entry")["state"], "supported")
        creature_only = finding([treasure, creature_listener], "creature_token_entry")
        self.assertEqual(creature_only["state"], "payoff_without_compatible_enabler")
        self.assertEqual(creature_only["compatible_pairs"], [])
        self.assertEqual(finding([creature, any_listener], "creature_token_entry")["state"], "supported")
        any_supported = finding([treasure, any_listener], "creature_token_entry")
        self.assertEqual(any_supported["state"], "supported")
        self.assertEqual(needs_findings(
            [any_supported], {"claim_scope": "reviewed_features_only"},
        ), [])

    def test_prerequisites_are_retained_and_support_is_conditional(self):
        unconditional = classified(
            1, "Plain Maker", "Create a 1/1 white Soldier creature token.", "Sorcery",
        )
        triggered = classified(
            2, "Triggered Maker",
            "Whenever you cast an instant or sorcery spell, create a 1/1 red Elemental creature token.",
            "Creature",
        )
        activated = classified(
            3, "Activated Maker", "{T}: Create a 1/1 white Soldier creature token.", "Creature",
        )
        listener = classified(4, "Listener", CREATURE_TOKEN_PAYOFF)

        def token_context(row):
            return next(feature for feature in row["features"]
                        if feature["rule_id"] == "effect.token.v1")["dependency_context"]

        self.assertEqual(token_context(unconditional)["availability"], "unconditional")
        trigger_context = token_context(triggered)
        self.assertEqual(trigger_context["availability"], "conditional")
        self.assertEqual(trigger_context["prerequisites"]["trigger"]["event"], "spell_cast")
        activated_context = token_context(activated)
        self.assertEqual(activated_context["availability"], "conditional")
        self.assertEqual(activated_context["prerequisites"]["costs"][0]["kind"], "tap")
        conditional = finding([triggered, listener], "creature_token_entry")
        self.assertEqual(conditional["state"], "conditionally_supported")
        observations = needs_findings(
            [conditional], {"claim_scope": "reviewed_features_only"},
        )
        self.assertEqual(observations[0]["finding_type"], "conditional_support_observation")
        self.assertNotIn("frequency", repr(conditional).casefold())

    def test_unsupported_prerequisite_does_not_create_positive_support(self):
        unsupported = classified(
            1, "Unknown Maker",
            "Whenever you cast a creature spell, create a 1/1 white Soldier creature token.",
            "Creature",
        )
        listener = classified(2, "Listener", CREATURE_TOKEN_PAYOFF)
        self.assertFalse(any(
            feature["rule_id"] == "effect.token.v1" for feature in unsupported["features"]
        ))
        self.assertEqual(
            finding([unsupported, listener], "creature_token_entry")["state"],
            "payoff_without_enabler",
        )
        partial = classified(
            3, "Partial Maker",
            "Create a Treasure token. Unmodeled rider.", "Sorcery",
        )
        context = next(feature for feature in partial["features"]
                       if feature["rule_id"] == "effect.noncreature_token.v1")["dependency_context"]
        self.assertEqual(context["availability"], "partially_reviewed")
        self.assertTrue(context["unsupported_remainder"])

    def test_malformed_or_contradictory_context_fails_closed(self):
        producer = classified(
            1, "Maker", "Create a 1/1 white Soldier creature token.", "Sorcery",
        )
        listener = classified(2, "Listener", CREATURE_TOKEN_PAYOFF)

        malformed = deepcopy(producer)
        token_feature = next(
            feature for feature in malformed["features"]
            if feature["rule_id"] == "effect.token.v1"
        )
        token_feature["dependency_context"] = "not structured evidence"
        result = finding([malformed, listener], "creature_token_entry")
        self.assertEqual(result["state"], "payoff_without_compatible_enabler")
        self.assertEqual(result["compatible_pairs"], [])

        contradictory = deepcopy(producer)
        token_feature = next(
            feature for feature in contradictory["features"]
            if feature["rule_id"] == "effect.token.v1"
        )
        token_feature["dependency_context"]["effect"]["kind"] = "gain_life"
        result = finding([contradictory, listener], "creature_token_entry")
        self.assertEqual(result["state"], "payoff_without_compatible_enabler")
        self.assertEqual(result["compatible_pairs"], [])

    def test_interaction_registry_order_and_legacy_attributes_remain_stable(self):
        from services.dependencies import INTERACTION_FAMILIES

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


class CompatibilityIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(canonical.SCHEMA)
        cards = [
            Card(1, "Any Listener", types="Enchantment", rules_text=ANY_TOKEN_PAYOFF),
            Card(2, "Treasure Maker", types="Sorcery", rules_text="Create a Treasure token."),
            Card(3, "Creature Maker", types="Sorcery", rules_text="Create a 1/1 white Soldier creature token."),
            Card(4, "Triggered Maker", types="Creature", rules_text=(
                "Whenever you cast an instant or sorcery spell, create a 1/1 red Elemental creature token."
            )),
            Card(5, "Outlet", types="Creature", rules_text=SACRIFICE),
            Card(6, "Ordinary", types="Creature"),
        ]
        canonical.load_cards(self.con, cards)
        canonical.load_printings(self.con, [
            CardPrinting(card.title_id * 100 + 1, card.title_id, "TST", str(card.title_id), "common")
            for card in cards
        ])
        self.con.commit()
        self.addCleanup(self.con.close)

    def test_any_token_need_has_explicit_or_alternatives_and_candidate_context(self):
        deck = Deck(main={101: 1})
        analysis = analyze_deck(deck, self.con)
        need = next(item for item in analysis["zones"]["main"]["needs"]
                    if item["dependency_label"] == "creature_token_entry")
        self.assertEqual(need["missing_side"]["matching_semantics"], "any")
        self.assertEqual(set(need["missing_side"]["acceptable_feature_rule_ids"]), {
            "effect.token.v1", "effect.noncreature_token.v1",
        })
        result = discover_candidates(analysis, deck, self.con)
        pool = next(item for item in result["pools"]
                    if item["source_need"]["dependency_label"] == "creature_token_entry")
        self.assertEqual(pool["source_need"]["feature_matching_semantics"], "any")
        self.assertTrue({2, 3, 4} <= {item["title_id"] for item in pool["candidates"]})
        triggered = next(item for item in pool["candidates"] if item["title_id"] == 4)
        context = triggered["matching_feature_evidence"][0]["dependency_context"]
        self.assertEqual(context["availability"], "conditional")
        self.assertEqual(context["prerequisites"]["trigger"]["event"], "spell_cast")
        self.assertEqual(
            triggered["eligibility"]["required_feature"]["matching_semantics"],
            "any",
        )

        facts = derive_candidate_facts(result, self.con)
        fact_pool = next(item for item in facts["per_need"]
                         if item["source_need"]["dependency_label"] == "creature_token_entry")
        self.assertEqual(fact_pool["source_need"]["feature_matching_semantics"], "any")
        treasure = next(item for item in fact_pool["candidates"] if item["title_id"] == 2)
        self.assertEqual(
            treasure["eligibility"]["required_feature"]["matching_semantics"],
            "any",
        )

        contradictory = deepcopy(result)
        contradictory["pools"][0]["source_need"]["feature_matching_semantics"] = "all"
        with self.assertRaises(ValueError):
            derive_candidate_facts(contradictory, self.con)

    def test_contradictory_alternative_contract_fails_closed(self):
        deck = Deck(main={101: 1})
        analysis = analyze_deck(deck, self.con)
        need = next(item for item in analysis["zones"]["main"]["needs"]
                    if item["dependency_label"] == "creature_token_entry")
        need["missing_side"]["feature_rule_ids"] = ["effect.token.v1"]
        with self.assertRaises(ValueError):
            discover_candidates(analysis, deck, self.con)

    def test_optional_sacrifice_observation_never_starts_candidate_search(self):
        deck = Deck(main={501: 1, 601: 20})
        analysis = analyze_deck(deck, self.con)
        sacrifice = next(item for item in analysis["zones"]["main"]["needs"]
                         if item["dependency_label"] == "creature_token_sacrifice")
        self.assertEqual(sacrifice["finding_type"], "optional_support_observation")
        result = discover_candidates(analysis, deck, self.con)
        self.assertFalse(any(
            pool["source_need"]["dependency_label"] == "creature_token_sacrifice"
            for pool in result["pools"]
        ))
