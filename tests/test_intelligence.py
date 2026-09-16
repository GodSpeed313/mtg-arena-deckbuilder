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

    def test_damage_targets_project_as_non_strategic_capabilities(self):
        cases = (
            ("target creature", "effect.damage.creature.v1", "damage_creature"),
            ("target player", "effect.damage.player.v1", "damage_player"),
            ("target opponent", "effect.damage.opponent.v1", "damage_opponent"),
            ("any target", "effect.damage.any_target.v1", "damage_any_target"),
            ("target planeswalker", "effect.damage.planeswalker.v1",
             "damage_planeswalker"),
            ("target battle", "effect.damage.battle.v1", "damage_battle"),
            ("each opponent", "effect.damage.each_opponent.v1",
             "damage_each_opponent"),
        )
        for target, rule_id, label in cases:
            with self.subTest(target=target):
                result = classify_card(card(
                    f"This spell deals 3 damage to {target}.", types="Sorcery"
                ))
                damage = [
                    feature for feature in result["features"]
                    if feature["rule_id"].startswith("effect.damage.")
                ]
                self.assertEqual(len(damage), 1)
                self.assertEqual(
                    (damage[0]["rule_id"], damage[0]["dimension"],
                     damage[0]["label"], damage[0]["relationship"]),
                    (rule_id, "ability", label, "one_shot"),
                )
                self.assertNotIn("removal", labels(result))
                self.assertNotIn("burn", labels(result, "theme"))
                self.assertEqual(interactions([result]), [])

    def test_any_target_damage_does_not_expand_into_other_target_labels(self):
        result = classify_card(card(
            "This spell deals X damage to any target.", types="Sorcery"
        ))
        damage_ids = {
            feature["rule_id"] for feature in result["features"]
            if feature["rule_id"].startswith("effect.damage.")
        }
        self.assertEqual(damage_ids, {"effect.damage.any_target.v1"})
        effect = result["abilities"][0]["effects"][0]
        self.assertEqual((effect["amount"], effect["target"]), ("X", "any_target"))

    def test_activated_damage_is_distinct_without_repeatability_claim(self):
        result = classify_card(Card(
            1,
            "Synthetic Pinger",
            types="Creature",
            rules_text="{T}: Synthetic Pinger deals 1 damage to any target.",
        ))
        ids = {feature["rule_id"] for feature in result["features"]}
        self.assertIn("effect.damage.any_target.v1", ids)
        self.assertIn("ability.activated_damage.v1", ids)
        target_feature = next(
            feature for feature in result["features"]
            if feature["rule_id"] == "effect.damage.any_target.v1"
        )
        activated = next(
            feature for feature in result["features"]
            if feature["rule_id"] == "ability.activated_damage.v1"
        )
        self.assertEqual(target_feature["relationship"], "activated")
        self.assertEqual(activated["relationship"], "activated")
        self.assertIn("frequency is not inferred", activated["explanation"])
        self.assertNotIn("removal", labels(result))
        self.assertEqual(interactions([result]), [])

    def test_unsupported_damage_wording_does_not_project(self):
        cases = (
            "Whenever Synthetic Card deals combat damage to a player, draw a card.",
            "Prevent the next 3 damage that would be dealt to target creature.",
            "Synthetic Card deals that much damage to target opponent.",
            "Synthetic Card deals damage equal to its power to target creature.",
            "Synthetic Card deals 3 damage divided as you choose among any number "
            "of targets.",
            "Target creature you control fights target creature you don't control.",
            "Flying (Damage dealt by this creature is combat damage.)",
        )
        for text in cases:
            with self.subTest(text=text):
                result = classify_card(card(text, types="Creature"))
                self.assertFalse(any(
                    feature["rule_id"].startswith((
                        "effect.damage.", "ability.activated_damage."
                    ))
                    for feature in result["features"]
                ))
                self.assertIn(text, result["unsupported_text"])

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

    def test_named_noncreature_tokens_project_without_creature_interactions(self):
        treasure = classify_card(card("Create two Treasure tokens.", types="Sorcery"))
        ids = {feature["rule_id"] for feature in treasure["features"]}
        self.assertIn("effect.noncreature_token.v1", ids)
        self.assertNotIn("effect.token.v1", ids)

        outlet = classify_card(replace(card(
            "Sacrifice another creature: Draw a card.", types="Creature"
        ), title_id=2))
        payoff = classify_card(replace(card(
            "Whenever a creature token enters the battlefield under your control, draw a card.",
            types="Enchantment",
        ), title_id=3))
        self.assertEqual(interactions([treasure, outlet, payoff]), [])

    def test_intrinsic_keyword_features_do_not_absorb_granted_or_token_keywords(self):
        intrinsic = classify_card(card("Flying\nWard {2}", types="Creature"))
        keyword_features = [
            feature for feature in intrinsic["features"]
            if feature["dimension"] == "ability"
        ]
        self.assertEqual(
            [(feature["rule_id"], feature["label"], feature["relationship"])
             for feature in keyword_features],
            [
                ("ability.keyword.flying.v1", "flying", "intrinsic"),
                ("ability.keyword.ward.v1", "ward", "intrinsic"),
            ],
        )

        for text in (
            "Target creature gains flying until end of turn.",
            "Creatures you control have flying.",
            "Create a 1/1 white Bird creature token with flying.",
        ):
            with self.subTest(text=text):
                result = classify_card(card(text, types="Sorcery"))
                self.assertFalse(any(
                    feature["rule_id"] == "ability.keyword.flying.v1"
                    for feature in result["features"]
                ))

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

    def test_lifegain_producer_listener_and_lifelink_stay_distinct(self):
        producer = classify_card(card("You gain 3 life.", types="Sorcery"))
        listener = classify_card(card(
            "Whenever you gain life, draw a card.", types="Enchantment"
        ))
        lifelink = classify_card(card("Lifelink", types="Creature"))
        opponent = classify_card(card("Target opponent gains 3 life.", types="Sorcery"))

        self.assertIn("effect.lifegain.v1", {
            feature["rule_id"] for feature in producer["features"]
        })
        producer_feature = next(
            feature for feature in producer["features"]
            if feature["rule_id"] == "effect.lifegain.v1"
        )
        self.assertEqual(
            (producer_feature["label"], producer_feature["relationship"]),
            ("lifegain", "producer"),
        )
        self.assertIn("trigger.lifegain.v1", {
            feature["rule_id"] for feature in listener["features"]
        })
        listener_feature = next(
            feature for feature in listener["features"]
            if feature["rule_id"] == "trigger.lifegain.v1"
        )
        self.assertEqual(
            (listener_feature["label"], listener_feature["relationship"]),
            ("lifegain", "payoff"),
        )
        self.assertNotIn("effect.lifegain.v1", {
            feature["rule_id"] for feature in listener["features"]
        })
        self.assertIn("ability.keyword.lifelink.v1", {
            feature["rule_id"] for feature in lifelink["features"]
        })
        self.assertNotIn("effect.lifegain.v1", {
            feature["rule_id"] for feature in lifelink["features"]
        })
        self.assertNotIn("effect.lifegain.v1", {
            feature["rule_id"] for feature in opponent["features"]
        })

    def test_plus1_counter_producer_and_listener_stay_distinct(self):
        producer = classify_card(card(
            "Put two +1/+1 counters on target creature you control.",
            types="Sorcery",
        ))
        listener = classify_card(card(
            "Whenever one or more +1/+1 counters are put on Synthetic Card, "
            "draw a card.",
            types="Creature",
        ))
        producer_ids = {feature["rule_id"] for feature in producer["features"]}
        listener_ids = {feature["rule_id"] for feature in listener["features"]}
        self.assertIn("effect.counter.v1", producer_ids)
        self.assertNotIn("trigger.counter.v1", producer_ids)
        self.assertIn("trigger.counter.v1", listener_ids)
        self.assertNotIn("effect.counter.v1", listener_ids)
        self.assertEqual(
            next(feature for feature in producer["features"]
                 if feature["rule_id"] == "effect.counter.v1")["relationship"],
            "producer",
        )
        self.assertEqual(
            next(feature for feature in listener["features"]
                 if feature["rule_id"] == "trigger.counter.v1")["relationship"],
            "payoff",
        )

    def test_lifegain_and_counter_vocabulary_add_no_interaction_families(self):
        producer = classify_card(card("You gain 3 life.", types="Sorcery"))
        listener = classify_card(replace(card(
            "Whenever you gain life, draw a card.", types="Enchantment"
        ), title_id=2))
        counter_source = classify_card(replace(card(
            "Put a +1/+1 counter on Synthetic Card.", types="Creature"
        ), title_id=3))
        counter_listener = classify_card(replace(card(
            "Whenever one or more +1/+1 counters are put on Synthetic Card, "
            "draw a card.",
            types="Creature",
        ), title_id=4))
        self.assertEqual(
            interactions([producer, listener, counter_source, counter_listener]), []
        )

    def test_lifegain_and_counter_negative_boundaries_do_not_project(self):
        cases = (
            "If you would gain life, draw a card instead.",
            "You gain that much life.",
            "Remove a +1/+1 counter from target creature.",
            "Put a charge counter on target artifact.",
            "Proliferate.",
        )
        forbidden = {
            "effect.lifegain.v1", "trigger.lifegain.v1",
            "effect.counter.v1", "trigger.counter.v1",
        }
        for text in cases:
            with self.subTest(text=text):
                result = classify_card(card(text, types="Sorcery"))
                self.assertTrue(forbidden.isdisjoint(
                    feature["rule_id"] for feature in result["features"]
                ))
                self.assertIn(text, result["unsupported_text"])

    def test_exact_interaction_effects_project_as_capabilities(self):
        cases = (
            ("Exile target creature.", "effect.exile.creature.v1", "exile_creature"),
            ("Exile target permanent.", "effect.exile.permanent.v1", "exile_permanent"),
            ("Exile target artifact.", "effect.exile.artifact.v1", "exile_artifact"),
            ("Exile target enchantment.", "effect.exile.enchantment.v1", "exile_enchantment"),
            ("Exile target card from a graveyard.",
             "effect.exile.graveyard_card.v1", "exile_graveyard_card"),
            ("Return target creature to its owner's hand.",
             "effect.bounce.creature.v1", "bounce_creature"),
            ("Return target permanent to its owner's hand.",
             "effect.bounce.permanent.v1", "bounce_permanent"),
            ("Target opponent discards two cards.",
             "effect.discard.opponent.v1", "discard_opponent"),
            ("Target opponent sacrifices a creature.",
             "effect.force_sacrifice.creature.v1", "force_sacrifice_creature"),
            ("Target opponent sacrifices a permanent.",
             "effect.force_sacrifice.permanent.v1", "force_sacrifice_permanent"),
        )
        for text, rule_id, label in cases:
            with self.subTest(text=text):
                result = classify_card(card(text, types="Sorcery"))
                feature = next(
                    feature for feature in result["features"]
                    if feature["rule_id"] == rule_id
                )
                self.assertEqual(
                    (feature["dimension"], feature["label"],
                     feature["relationship"], feature["evidence"]),
                    ("ability", label, "capability", text),
                )
                self.assertEqual(labels(result), set())
                self.assertEqual(interactions([result]), [])

    def test_recursion_and_temporary_protection_compatibility(self):
        recursion_text = (
            "Return target creature card from your graveyard to your hand."
        )
        recursion = classify_card(card(recursion_text, types="Sorcery"))
        recursion_features = [
            feature for feature in recursion["features"]
            if feature["rule_id"] == "effect.recursion.v1"
        ]
        self.assertEqual(
            {(feature["dimension"], feature["label"], feature["relationship"])
             for feature in recursion_features},
            {("role", "recursion", "effect"),
             ("theme", "graveyard", "consumer")},
        )
        self.assertEqual(
            recursion["abilities"][0]["effects"][0]["kind"],
            "return_from_graveyard",
        )

        for keyword in ("hexproof", "indestructible"):
            with self.subTest(keyword=keyword):
                text = (
                    f"Target creature you control gains {keyword} until end of turn."
                )
                protection = classify_card(card(text, types="Instant"))
                self.assertIn("protection", labels(protection))
                self.assertIn("effect.protection.v1", {
                    feature["rule_id"] for feature in protection["features"]
                })

    def test_costs_do_not_project_opponent_interaction_capabilities(self):
        sacrifice = classify_card(card(
            "Sacrifice a creature: Draw a card.", types="Creature"
        ))
        discard = classify_card(card(
            "Discard a card: Draw a card.", types="Creature"
        ))
        forbidden = {
            "effect.discard.opponent.v1",
            "effect.force_sacrifice.creature.v1",
            "effect.force_sacrifice.permanent.v1",
        }
        for result in (sacrifice, discard):
            self.assertTrue(forbidden.isdisjoint(
                feature["rule_id"] for feature in result["features"]
            ))

    def test_unsupported_interaction_forms_do_not_project(self):
        cases = (
            "Exile all creatures.",
            "Exile target creature, then return it to the battlefield.",
            "Target opponent discards a card at random.",
            "Target opponent discards a card unless they pay {2}.",
            "Each opponent discards a card.",
            "Target opponent sacrifices a creature of their choice.",
            "Return target creature card from your graveyard to the battlefield.",
            "Choose one — Exile target creature.",
        )
        prefixes = (
            "effect.exile.", "effect.bounce.", "effect.discard.",
            "effect.force_sacrifice.", "effect.recursion.",
        )
        for text in cases:
            with self.subTest(text=text):
                result = classify_card(card(text, types="Sorcery"))
                self.assertFalse(any(
                    feature["rule_id"].startswith(prefixes)
                    for feature in result["features"]
                ))
                self.assertIn(text, result["unsupported_text"])

    def test_interaction_capabilities_add_no_family_and_keep_partial_text(self):
        source = classify_card(card(
            "Exile target creature. Unmodeled rider.", types="Instant"
        ))
        discard = classify_card(replace(card(
            "Target opponent discards a card.", types="Sorcery"
        ), title_id=2))
        sacrifice = classify_card(replace(card(
            "Target opponent sacrifices a permanent.", types="Sorcery"
        ), title_id=3))
        self.assertIn("effect.exile.creature.v1", {
            feature["rule_id"] for feature in source["features"]
        })
        self.assertEqual(source["unsupported_text"], ["Unmodeled rider."])
        self.assertEqual(interactions([source, discard, sacrifice]), [])
        self.assertEqual(
            [family.family_id for family in INTERACTION_FAMILIES],
            [
                "interaction.token_draw.v1",
                "interaction.spell_draw.v1",
                "interaction.token_sacrifice.v1",
            ],
        )

    def test_threat_is_only_potential(self):
        result = classify_card(card(types="Creature", power="2", toughness="2"))
        self.assertEqual(result["features"][0]["relationship"], "potential")
        self.assertNotIn("threat", labels(classify_card(card(types="Creature", power="*"))))

    def test_threat_respects_explicit_attack_prohibitions(self):
        defender = classify_card(card(
            "Defender", types="Creature", power="5", toughness="5"
        ))
        self.assertNotIn("threat", labels(defender))
        self.assertNotIn("Defender", defender["unsupported_text"])
        self.assertIn(
            "ability.keyword.defender.v1",
            {feature["rule_id"] for feature in defender["features"]},
        )

        for text in ("Synthetic Card can't attack.", "This creature can't attack."):
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
        self.assertIn("effect.noncreature_token.v1", ids(deadly))
        self.assertIn("effect.noncreature_token.v1", ids(treasure))
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
        unknown_token = classify_card(card("Create a mysterious token.", types="Sorcery"))
        unknown = classify_card(card("Unmodeled text.", types="Enchantment"))
        self.assertEqual(unknown_token["ability_coverage"]["structural_recognition"], 1.0)
        self.assertEqual(unknown_token["ability_coverage"]["meaningful_understanding"], 0.0)
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
