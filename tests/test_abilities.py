from __future__ import annotations

from dataclasses import asdict
import unittest

from services.abilities import decompose_abilities


class AbilityDecompositionTests(unittest.TestCase):
    def test_deadly_dispute_separates_additional_cost_and_partial_effect(self):
        rows = decompose_abilities(
            "As an additional cost to cast this spell, sacrifice an artifact or creature.\n"
            "Draw two cards and create a Treasure token.",
            card_name="Deadly Dispute",
            card_types="Instant",
        )
        self.assertEqual([row.ability_id for row in rows], ["ability.001", "ability.002"])
        self.assertEqual(rows[0].kind, "spell_effect")
        self.assertEqual(rows[0].costs[0].timing, "additional_spell")
        self.assertEqual(rows[0].costs[0].subject, "artifact_or_creature")
        self.assertEqual(rows[1].effects[0].kind, "draw")
        self.assertEqual(rows[1].effects[0].amount, 2)
        self.assertEqual(rows[1].parse_status, "partial")
        self.assertEqual(rows[1].unsupported_remainder[0].reason, "noncreature_token")
        self.assertNotIn("create_creature_token", {effect.kind for effect in rows[1].effects})

    def test_agency_coroner_keeps_activation_cost_effect_and_condition_attached(self):
        rows = decompose_abilities(
            "{2}{B}, Sacrifice another creature: Draw a card. "
            "If the sacrificed creature was suspected, draw two cards instead.",
            card_name="Agency Coroner",
            card_types="Creature",
        )
        ability = rows[0]
        self.assertEqual(ability.kind, "activated")
        self.assertEqual(ability.parse_status, "partial")
        self.assertEqual([(cost.kind, cost.subject, cost.timing) for cost in ability.costs], [
            ("sacrifice", "another_creature", "activated"),
        ])
        self.assertEqual(ability.unsupported_remainder[0].text, "{2}{B}")
        self.assertEqual([effect.amount for effect in ability.effects], [1, 2])
        self.assertFalse(ability.effects[0].conditions)
        self.assertEqual(ability.effects[1].conditions[0].value, "suspected")
        self.assertEqual(ability.effects[1].qualifiers[0].kind, "instead")

    def test_morbid_opportunist_death_trigger_and_frequency(self):
        rows = decompose_abilities(
            "Whenever one or more other creatures die, draw a card. "
            "This ability triggers only once each turn.",
            card_name="Morbid Opportunist",
            card_types="Creature",
        )
        ability = rows[0]
        self.assertEqual(ability.parse_status, "supported")
        self.assertEqual(ability.trigger.event, "creature_dies")
        self.assertEqual(ability.trigger.subject, "other_creatures")
        self.assertIsNone(ability.trigger.friendly)
        self.assertEqual(ability.effects[0].kind, "draw")
        self.assertEqual(ability.qualifiers[0].kind, "once_each_turn")

    def test_young_pyromancer_cast_trigger_creates_creature_token(self):
        rows = decompose_abilities(
            "Whenever you cast an instant or sorcery spell, create a 1/1 red "
            "Elemental creature token.",
            card_name="Young Pyromancer",
            card_types="Creature",
        )
        ability = rows[0]
        self.assertEqual(ability.trigger.event, "spell_cast")
        self.assertEqual(ability.trigger.subject, "instant_or_sorcery")
        self.assertTrue(ability.trigger.friendly)
        token = ability.effects[0]
        self.assertEqual(token.kind, "create_creature_token")
        self.assertTrue(token.friendly)
        self.assertEqual(token.token.quantity, 1)
        self.assertEqual(token.token.colors, ("red",))
        self.assertEqual(token.token.subtypes, ("Elemental",))

    def test_lightning_bolt_numeric_damage(self):
        rows = decompose_abilities(
            "Lightning Bolt deals 3 damage to any target.",
            card_name="Lightning Bolt",
            card_types="Instant",
        )
        effect = rows[0].effects[0]
        self.assertEqual(rows[0].kind, "spell_effect")
        self.assertEqual(rows[0].parse_status, "supported")
        self.assertEqual((effect.kind, effect.amount, effect.target),
                         ("damage", 3, "any_target"))

    def test_archmage_neighboring_unsupported_ability_does_not_suppress_trigger(self):
        rows = decompose_abilities(
            "Instant and sorcery spells you cast cost {1} less to cast.\n"
            "Whenever you cast an instant or sorcery spell, draw a card.",
            card_name="Archmage of Runes",
            card_types="Creature",
        )
        self.assertEqual(rows[0].parse_status, "unsupported")
        self.assertFalse(rows[0].effects)
        self.assertEqual(rows[1].parse_status, "supported")
        self.assertEqual(rows[1].trigger.event, "spell_cast")
        self.assertEqual(rows[1].effects[0].kind, "draw")

    def test_opponent_events_and_tokens_are_never_friendly(self):
        cast = decompose_abilities(
            "Whenever an opponent casts an instant or sorcery spell, draw a card.",
            card_types="Enchantment",
        )[0]
        token = decompose_abilities(
            "Target opponent creates a 1/1 white Soldier creature token.",
            card_types="Sorcery",
        )[0]
        self.assertEqual(cast.trigger.controller, "opponent")
        self.assertFalse(cast.trigger.friendly)
        self.assertEqual(token.effects[0].controller, "opponent")
        self.assertFalse(token.effects[0].friendly)

    def test_treasure_only_production_is_not_creature_token_creation(self):
        row = decompose_abilities(
            "Create a Treasure token.", card_types="Sorcery"
        )[0]
        self.assertEqual(row.parse_status, "unsupported")
        self.assertFalse(row.effects)
        self.assertEqual(row.unsupported_remainder[0].reason, "noncreature_token")

    def test_self_and_another_sacrifice_remain_distinct(self):
        self_cost = decompose_abilities(
            "Sacrifice this creature: Draw a card.", card_types="Creature"
        )[0]
        another_cost = decompose_abilities(
            "Sacrifice another creature: Draw a card.", card_types="Creature"
        )[0]
        self.assertEqual(self_cost.costs[0].subject, "self_creature")
        self.assertEqual(another_cost.costs[0].subject, "another_creature")
        self.assertEqual(self_cost.effects[0].kind, "draw")
        self.assertEqual(another_cost.effects[0].kind, "draw")

    def test_unknown_trigger_does_not_detach_supported_looking_effect(self):
        row = decompose_abilities(
            "Whenever you gain life, draw a card.", card_types="Enchantment"
        )[0]
        self.assertEqual(row.parse_status, "unsupported")
        self.assertIsNone(row.trigger)
        self.assertFalse(row.effects)
        self.assertEqual(row.unsupported_remainder[0].scope, "trigger")

    def test_colon_inside_triggered_effect_does_not_become_activation(self):
        row = decompose_abilities(
            "Whenever you cast an instant or sorcery spell, "
            "sacrifice another creature: Draw a card.",
            card_types="Enchantment",
        )[0]
        self.assertEqual(row.kind, "triggered")
        self.assertEqual(row.trigger.event, "spell_cast")
        self.assertFalse(row.costs)
        self.assertFalse(row.effects)
        self.assertEqual(row.unsupported_remainder[0].scope, "effect")

    def test_canonical_anomaly_retains_text_without_semantics(self):
        rows = decompose_abilities(
            "Draw a card.\nWhenever you cast an instant or sorcery spell, draw a card.",
            card_types="Instant",
            canonical_anomaly=True,
        )
        self.assertEqual([row.parse_status for row in rows], ["anomalous", "anomalous"])
        self.assertTrue(all(not row.effects and row.trigger is None for row in rows))
        self.assertTrue(all(row.unsupported_remainder[0].reason == "canonical_anomaly"
                            for row in rows))

    def test_output_is_deterministic_and_preserves_source_evidence(self):
        text = (
            "Whenever you cast an instant or sorcery spell, create a 1/1 red "
            "Elemental creature token.\nUnknown ability."
        )
        first = decompose_abilities(text, card_name="Example", card_types="Creature")
        second = decompose_abilities(text, card_name="Example", card_types="Creature")
        self.assertEqual(first, second)
        self.assertEqual(asdict(first[0])["raw_text"], text.splitlines()[0])
        self.assertEqual(first[1].unsupported_remainder[0].text, "Unknown ability.")


if __name__ == "__main__":
    unittest.main()
