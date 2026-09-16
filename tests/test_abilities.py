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
        self.assertEqual(rows[1].parse_status, "supported")
        self.assertEqual(rows[1].effects[1].kind, "create_noncreature_token")
        self.assertEqual(rows[1].effects[1].token.name, "Treasure")
        self.assertEqual(rows[1].effects[1].token.quantity, 1)
        self.assertFalse(rows[1].unsupported_remainder)
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
        self.assertEqual((token.token.power, token.token.toughness), (1, 1))
        self.assertEqual(token.token.colors, ("red",))
        self.assertEqual(token.token.subtypes, ("Elemental",))
        self.assertFalse(token.token.keywords)

    def test_creature_token_quantity_characteristics_and_keywords_are_explicit(self):
        token = decompose_abilities(
            "Create two 2/2 green Wolf creature tokens with vigilance and trample.",
            card_types="Sorcery",
        )[0].effects[0].token
        self.assertEqual(token.kind, "creature")
        self.assertEqual(token.quantity, 2)
        self.assertEqual((token.power, token.toughness), (2, 2))
        self.assertEqual(token.colors, ("green",))
        self.assertEqual(token.subtypes, ("Wolf",))
        self.assertEqual(token.keywords, ("vigilance", "trample"))

        variable = decompose_abilities(
            "Create X 1/1 colorless Thopter artifact creature tokens with flying.",
            card_types="Sorcery",
        )[0].effects[0].token
        self.assertEqual(variable.quantity, "X")
        self.assertEqual(variable.colors, ("colorless",))
        self.assertEqual(variable.subtypes, ("Thopter",))
        self.assertTrue(variable.artifact)
        self.assertEqual(variable.keywords, ("flying",))

    def test_friendly_token_entry_trigger_keeps_frequency_qualifier(self):
        ability = decompose_abilities(
            "Whenever one or more tokens you control enter, draw a card. "
            "This ability triggers only once each turn.",
            card_name="Caretaker's Talent",
            card_types="Enchantment",
        )[0]
        self.assertEqual(ability.kind, "triggered")
        self.assertEqual(ability.trigger.event, "token_enters")
        self.assertTrue(ability.trigger.friendly)
        self.assertEqual(ability.effects[0].kind, "draw")
        self.assertEqual(ability.qualifiers[0].kind, "once_each_turn")

    def test_modal_layout_does_not_expose_option_as_standalone_effect(self):
        rows = decompose_abilities(
            "Choose one —\nDraw two cards.", card_types="Sorcery"
        )
        self.assertTrue(all(row.parse_status == "unsupported" for row in rows))
        self.assertTrue(all(not row.effects for row in rows))
        self.assertTrue(all(
            row.unsupported_remainder[0].reason == "unsupported_modal" for row in rows
        ))

    def test_direct_damage_preserves_amount_target_and_source(self):
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
        self.assertEqual(effect.source, "self")
        self.assertEqual(effect.controller, "source_controller")
        self.assertIsNone(effect.friendly)

        cases = (
            ("This spell deals 2 damage to target creature.", "target_creature"),
            ("This spell deals 2 damage to target player.", "target_player"),
            ("This spell deals 2 damage to target opponent.", "target_opponent"),
            ("This spell deals 2 damage to target planeswalker.",
             "target_planeswalker"),
            ("This spell deals 2 damage to target battle.", "target_battle"),
            ("This spell deals 2 damage to each opponent.", "each_opponent"),
        )
        for text, target in cases:
            with self.subTest(target=target):
                parsed = decompose_abilities(text, card_types="Sorcery")[0].effects[0]
                self.assertEqual(parsed.target, target)
                self.assertEqual(parsed.source, "spell")
                self.assertIsNone(parsed.friendly)

        symbolic = decompose_abilities(
            "This spell deals X damage to target creature.", card_types="Sorcery"
        )[0].effects[0]
        self.assertEqual(symbolic.amount, "X")

    def test_tap_activation_preserves_damage_capability_without_frequency(self):
        ability = decompose_abilities(
            "{T}: Synthetic Pinger deals 1 damage to any target.",
            card_name="Synthetic Pinger",
            card_types="Creature",
        )[0]
        self.assertEqual(ability.kind, "activated")
        self.assertEqual(ability.parse_status, "supported")
        self.assertEqual(
            [(cost.kind, cost.subject, cost.timing) for cost in ability.costs],
            [("tap", "self", "activated")],
        )
        self.assertEqual(
            (ability.effects[0].kind, ability.effects[0].amount,
             ability.effects[0].target, ability.effects[0].source),
            ("damage", 1, "any_target", "self"),
        )

    def test_unsupported_damage_semantics_remain_unsupported(self):
        cases = (
            "Whenever Synthetic Card deals combat damage to a player, draw a card.",
            "Synthetic Card assigns combat damage as though it weren't blocked.",
            "Prevent the next 3 damage that would be dealt to target creature.",
            "If damage would be dealt to you, prevent that damage.",
            "Damage can't be prevented.",
            "Synthetic Card instead deals 3 damage to target creature.",
            "Redirect that damage to target player.",
            "You may have it deal that damage to target creature.",
            "Synthetic Card deals that much damage to target opponent.",
            "Synthetic Card deals damage equal to its power to target creature.",
            "Synthetic Card deals damage equal to the number of cards in your hand "
            "to target player.",
            "Synthetic Card deals 3 damage divided as you choose among any number "
            "of targets.",
            "Synthetic Card deals excess damage to target player.",
            "Double that damage.",
            "Target creature you control fights target creature you don't control.",
            "Target creature deals damage equal to its power to another target creature.",
            "Flying (Damage dealt by this creature is combat damage.)",
        )
        for text in cases:
            with self.subTest(text=text):
                row = decompose_abilities(
                    text, card_name="Synthetic Card", card_types="Creature"
                )[0]
                self.assertEqual(row.parse_status, "unsupported")
                self.assertFalse(row.effects)

    def test_direct_damage_with_unknown_tail_stays_partial(self):
        row = decompose_abilities(
            "This spell deals 3 damage to target creature. Unmodeled rider.",
            card_types="Sorcery",
        )[0]
        self.assertEqual(row.parse_status, "partial")
        self.assertEqual(row.effects[0].kind, "damage")
        self.assertEqual(row.unsupported_remainder[0].text, "Unmodeled rider.")

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

    def test_named_noncreature_tokens_are_distinct_from_creature_tokens(self):
        for name in ("Treasure", "Clue", "Food", "Blood", "Map"):
            with self.subTest(name=name):
                row = decompose_abilities(
                    f"Create two {name} tokens.", card_types="Sorcery"
                )[0]
                self.assertEqual(row.parse_status, "supported")
                self.assertEqual(row.effects[0].kind, "create_noncreature_token")
                self.assertEqual(row.effects[0].token.kind, "named_noncreature")
                self.assertEqual(row.effects[0].token.name, name)
                self.assertEqual(row.effects[0].token.quantity, 2)
                self.assertIsNone(row.effects[0].token.power)
                self.assertIsNone(row.effects[0].token.toughness)

    def test_intrinsic_keywords_are_whole_line_and_keep_ward_cost(self):
        keywords = (
            "Flying", "Vigilance", "Trample", "Deathtouch", "Lifelink",
            "Haste", "Reach", "Menace", "Defender", "First strike",
            "Double strike", "Hexproof", "Indestructible",
        )
        for text in keywords:
            with self.subTest(text=text):
                row = decompose_abilities(text, card_types="Creature")[0]
                self.assertEqual(row.kind, "static_keyword")
                self.assertEqual(row.parse_status, "supported")
                self.assertEqual(row.keywords[0].name, text.casefold().replace(" ", "_"))
                self.assertEqual(row.keywords[0].subject, "self")
                self.assertEqual(row.keywords[0].evidence, text)

        ward = decompose_abilities("Ward {2}", card_types="Creature")[0]
        self.assertEqual(ward.keywords[0].name, "ward")
        self.assertEqual(ward.keywords[0].value, "{2}")

    def test_granted_conditional_and_reminder_keywords_remain_unsupported(self):
        cases = (
            "Target creature gains flying until end of turn.",
            "Creatures you control have flying.",
            "If you control an artifact, this creature has flying.",
            "Flying (This creature can't be blocked except by creatures with flying or reach.)",
        )
        for text in cases:
            with self.subTest(text=text):
                row = decompose_abilities(text, card_types="Creature")[0]
                self.assertEqual(row.parse_status, "unsupported")
                self.assertFalse(row.keywords)

    def test_token_keyword_never_becomes_source_keyword(self):
        row = decompose_abilities(
            "Create a 1/1 white Bird creature token with flying.",
            card_types="Sorcery",
        )[0]
        self.assertFalse(row.keywords)
        self.assertEqual(row.effects[0].token.keywords, ("flying",))

    def test_mixed_supported_and_unsupported_effect_text_stays_partial(self):
        row = decompose_abilities(
            "Create a Treasure token. Unmodeled rider.", card_types="Sorcery"
        )[0]
        self.assertEqual(row.parse_status, "partial")
        self.assertEqual(row.effects[0].kind, "create_noncreature_token")
        self.assertEqual(row.unsupported_remainder[0].text, "Unmodeled rider.")

        conditional = decompose_abilities(
            "If you control an artifact, create a 1/1 colorless Construct creature token.",
            card_types="Sorcery",
        )[0]
        self.assertEqual(conditional.parse_status, "unsupported")
        self.assertFalse(conditional.effects)

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

    def test_explicit_lifegain_preserves_amount_and_controller(self):
        friendly = decompose_abilities(
            "You gain 3 life.", card_types="Sorcery"
        )[0]
        self.assertEqual(friendly.parse_status, "supported")
        self.assertEqual(friendly.effects[0].kind, "gain_life")
        self.assertEqual(friendly.effects[0].amount, 3)
        self.assertEqual(friendly.effects[0].controller, "you")
        self.assertTrue(friendly.effects[0].friendly)

        symbolic = decompose_abilities(
            "You gain X life.", card_types="Sorcery"
        )[0]
        self.assertEqual(symbolic.effects[0].amount, "X")

        opponent = decompose_abilities(
            "Target opponent gains 2 life.", card_types="Sorcery"
        )[0]
        self.assertEqual(opponent.effects[0].kind, "gain_life")
        self.assertEqual(opponent.effects[0].controller, "target_opponent")
        self.assertFalse(opponent.effects[0].friendly)

    def test_lifegain_trigger_is_listener_not_gain_effect(self):
        row = decompose_abilities(
            "Whenever you gain life, draw a card.", card_types="Enchantment"
        )[0]
        self.assertEqual(row.parse_status, "supported")
        self.assertEqual(row.trigger.event, "life_gained")
        self.assertEqual(row.trigger.controller, "you")
        self.assertTrue(row.trigger.friendly)
        self.assertEqual([effect.kind for effect in row.effects], ["draw"])

    def test_plus1_counter_placement_preserves_quantity_and_target(self):
        self_counter = decompose_abilities(
            "Put a +1/+1 counter on Synthetic Card.",
            card_name="Synthetic Card",
            card_types="Creature",
        )[0].effects[0]
        self.assertEqual(self_counter.kind, "put_counter")
        self.assertEqual(self_counter.amount, 1)
        self.assertEqual(self_counter.counter_type, "+1/+1")
        self.assertEqual(self_counter.target, "self")

        target_counter = decompose_abilities(
            "Put two +1/+1 counters on target creature you control.",
            card_types="Sorcery",
        )[0].effects[0]
        self.assertEqual(target_counter.amount, 2)
        self.assertEqual(target_counter.counter_type, "+1/+1")
        self.assertEqual(target_counter.target, "target_creature_you_control")

        another_counter = decompose_abilities(
            "Put three +1/+1 counters on another target creature.",
            card_types="Sorcery",
        )[0].effects[0]
        self.assertEqual(another_counter.amount, 3)
        self.assertEqual(another_counter.target, "another_target_creature")

    def test_plus1_counter_trigger_is_listener_not_producer(self):
        row = decompose_abilities(
            "Whenever one or more +1/+1 counters are put on Synthetic Card, "
            "draw a card.",
            card_name="Synthetic Card",
            card_types="Creature",
        )[0]
        self.assertEqual(row.trigger.event, "counter_placed")
        self.assertEqual(row.trigger.subject, "self")
        self.assertEqual([effect.kind for effect in row.effects], ["draw"])

        controlled = decompose_abilities(
            "Whenever one or more +1/+1 counters are put on a creature you "
            "control, draw a card.",
            card_types="Enchantment",
        )[0]
        self.assertEqual(controlled.trigger.event, "counter_placed")
        self.assertEqual(controlled.trigger.subject, "creature_you_control")

    def test_lifegain_and_counter_false_positives_remain_unsupported(self):
        cases = (
            "You lose 3 life.",
            "Players can't gain life.",
            "If you would gain life, draw a card instead.",
            "You gain that much life.",
            "Remove a +1/+1 counter from target creature.",
            "Move a +1/+1 counter from one creature onto another.",
            "Proliferate.",
            "Put a charge counter on target artifact.",
            "Put a loyalty counter on target planeswalker.",
            "If you control an artifact, put a +1/+1 counter on target creature.",
        )
        for text in cases:
            with self.subTest(text=text):
                row = decompose_abilities(text, card_types="Sorcery")[0]
                self.assertEqual(row.parse_status, "unsupported")
                self.assertFalse(row.effects)

    def test_lifegain_mixed_with_unknown_text_stays_partial(self):
        row = decompose_abilities(
            "You gain 3 life. Unmodeled rider.", card_types="Sorcery"
        )[0]
        self.assertEqual(row.parse_status, "partial")
        self.assertEqual(row.effects[0].kind, "gain_life")
        self.assertEqual(row.unsupported_remainder[0].text, "Unmodeled rider.")

    def test_exact_exile_forms_preserve_target_and_zones(self):
        cases = (
            ("creature", "target_creature"),
            ("permanent", "target_permanent"),
            ("artifact", "target_artifact"),
            ("enchantment", "target_enchantment"),
        )
        for wording, target in cases:
            with self.subTest(wording=wording):
                row = decompose_abilities(
                    f"Exile target {wording}.", card_types="Instant"
                )[0]
                self.assertEqual(row.parse_status, "supported")
                effect = row.effects[0]
                self.assertEqual((effect.kind, effect.target), ("exile", target))
                self.assertEqual(effect.destination_zone, "exile")
                self.assertIsNone(effect.friendly)

        graveyard = decompose_abilities(
            "Exile target card from a graveyard.", card_types="Instant"
        )[0].effects[0]
        self.assertEqual((graveyard.kind, graveyard.target), ("exile", "card"))
        self.assertEqual(graveyard.origin_zone, "graveyard")
        self.assertEqual(graveyard.destination_zone, "exile")

    def test_bounce_and_opponent_discard_preserve_bounded_details(self):
        for wording, target in (
            ("creature", "target_creature"),
            ("permanent", "target_permanent"),
        ):
            with self.subTest(wording=wording):
                effect = decompose_abilities(
                    f"Return target {wording} to its owner’s hand.",
                    card_types="Instant",
                )[0].effects[0]
                self.assertEqual((effect.kind, effect.target), ("return_to_hand", target))
                self.assertEqual(effect.destination_zone, "owner_hand")
                self.assertIsNone(effect.friendly)

        for wording, amount in (("a card", 1), ("two cards", 2), ("7 cards", 7)):
            with self.subTest(wording=wording):
                effect = decompose_abilities(
                    f"Target opponent discards {wording}.", card_types="Sorcery"
                )[0].effects[0]
                self.assertEqual((effect.kind, effect.amount), ("discard", amount))
                self.assertEqual(effect.controller, "target_opponent")
                self.assertEqual(effect.origin_zone, "opponent_hand")
                self.assertEqual(effect.destination_zone, "graveyard")
                self.assertFalse(effect.friendly)

    def test_recursion_and_forced_sacrifice_are_structurally_distinct(self):
        recursion = decompose_abilities(
            "Return target creature card from your graveyard to your hand.",
            card_types="Sorcery",
        )[0].effects[0]
        self.assertEqual(recursion.kind, "return_from_graveyard")
        self.assertEqual(recursion.target, "creature_card")
        self.assertEqual(recursion.origin_zone, "your_graveyard")
        self.assertEqual(recursion.destination_zone, "your_hand")
        self.assertTrue(recursion.friendly)

        for target in ("creature", "permanent"):
            with self.subTest(target=target):
                row = decompose_abilities(
                    f"Target opponent sacrifices a {target}.",
                    card_types="Sorcery",
                )[0]
                effect = row.effects[0]
                self.assertEqual(effect.kind, "force_sacrifice")
                self.assertEqual(effect.target, target)
                self.assertEqual(effect.amount, 1)
                self.assertFalse(effect.friendly)
                self.assertFalse(row.costs)

        cost = decompose_abilities(
            "Sacrifice a creature: Draw a card.", card_types="Creature"
        )[0]
        self.assertEqual(cost.costs[0].kind, "sacrifice")
        self.assertFalse(any(effect.kind == "force_sacrifice" for effect in cost.effects))

    def test_broader_interaction_false_positives_remain_unsupported(self):
        cases = (
            "Exile all creatures.",
            "Exile target creature until Synthetic Card leaves the battlefield.",
            "Exile target creature, then return it to the battlefield.",
            "If a card would be put into a graveyard, exile it instead.",
            "Exile a card from target opponent's hand.",
            "Target opponent discards a card at random.",
            "Target opponent discards a card unless they pay {2}.",
            "Each opponent discards a card.",
            "You may sacrifice a creature.",
            "Each opponent sacrifices a creature.",
            "Target opponent sacrifices a creature of their choice.",
            "Mill three cards.",
            "Surveil 2.",
            "Return target creature card from your graveyard to the battlefield.",
            "Flashback {2}{U}",
            "Choose one — Exile target creature.",
        )
        forbidden = {"exile", "return_to_hand", "discard", "force_sacrifice",
                     "return_from_graveyard"}
        for text in cases:
            with self.subTest(text=text):
                row = decompose_abilities(text, card_types="Sorcery")[0]
                self.assertEqual(row.parse_status, "unsupported")
                self.assertTrue(forbidden.isdisjoint(
                    effect.kind for effect in row.effects
                ))

        discard_cost = decompose_abilities(
            "Discard a card: Draw a card.", card_types="Creature"
        )[0]
        self.assertEqual(discard_cost.parse_status, "partial")
        self.assertEqual([effect.kind for effect in discard_cost.effects], ["draw"])
        self.assertTrue(forbidden.isdisjoint(
            effect.kind for effect in discard_cost.effects
        ))

    def test_interaction_effect_with_unknown_tail_stays_partial(self):
        row = decompose_abilities(
            "Exile target creature. Unmodeled rider.", card_types="Instant"
        )[0]
        self.assertEqual(row.parse_status, "partial")
        self.assertEqual(row.effects[0].kind, "exile")
        self.assertEqual(row.unsupported_remainder[0].text, "Unmodeled rider.")

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
