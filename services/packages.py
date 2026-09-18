"""Deterministic functional packages derived from reviewed card features.

Packages describe jobs supported by existing feature evidence.  They do not
infer deck needs, archetypes, synergy, card quality, or recommendations.
"""
from __future__ import annotations

from dataclasses import dataclass


PACKAGE_MODEL_VERSION = "1"


@dataclass(frozen=True, slots=True)
class FunctionalPackage:
    package_id: str
    label: str
    feature_rule_ids: tuple[str, ...]
    explanation: str


FUNCTIONAL_PACKAGES = (
    FunctionalPackage(
        "package.threats.v1",
        "threats",
        ("type.threat.v1", "type.conditional_threat.v1"),
        "Recognized potential or explicitly conditional combat threat.",
    ),
    FunctionalPackage(
        "package.interaction.v1",
        "interaction",
        (
            "effect.destroy.v1",
            "effect.counterspell.v1",
            "effect.damage.creature.v1",
            "effect.damage.any_target.v1",
            "effect.damage.planeswalker.v1",
            "effect.damage.battle.v1",
            "effect.exile.creature.v1",
            "effect.exile.permanent.v1",
            "effect.exile.artifact.v1",
            "effect.exile.enchantment.v1",
            "effect.exile.graveyard_card.v1",
            "effect.bounce.creature.v1",
            "effect.bounce.permanent.v1",
            "effect.discard.opponent.v1",
            "effect.force_sacrifice.creature.v1",
            "effect.force_sacrifice.permanent.v1",
        ),
        "Recognized capability that can affect an opposing spell, permanent, "
        "hand, graveyard card, planeswalker, battle, or legal any target.",
    ),
    FunctionalPackage(
        "package.card_advantage.v1",
        "card_advantage",
        (
            "effect.draw.v1",
            "trigger.token_draw.v1",
            "trigger.spells.v1",
            "cost.sacrifice_draw.v1",
        ),
        "Recognized unconditional or conditional card-draw job; net cards are "
        "not calculated.",
    ),
    FunctionalPackage(
        "package.mana_ramp.v1",
        "mana_ramp",
        (
            "effect.land_ramp.v1",
            "ability.mana_ramp.v1",
            "ability.any_ramp.v1",
            "ability.fixing.v1",
        ),
        "Recognized mana acceleration or fixing capability; adequacy is not assessed.",
    ),
    FunctionalPackage(
        "package.protection.v1",
        "protection",
        ("effect.protection.v1",),
        "Recognized temporary protection job.",
    ),
    FunctionalPackage(
        "package.recursion.v1",
        "recursion",
        ("effect.recursion.v1",),
        "Recognized graveyard-to-hand recursion job.",
    ),
    FunctionalPackage(
        "package.token_production.v1",
        "token_production",
        ("effect.token.v1", "effect.noncreature_token.v1"),
        "Recognized creature or reviewed named noncreature token production.",
    ),
    FunctionalPackage(
        "package.lifegain.v1",
        "lifegain",
        ("effect.lifegain.v1", "trigger.lifegain.v1"),
        "Recognized lifegain production or lifegain listener job.",
    ),
    FunctionalPackage(
        "package.plus1_counters.v1",
        "plus1_counters",
        ("effect.counter.v1", "trigger.counter.v1"),
        "Recognized +1/+1 counter production or listener job.",
    ),
    FunctionalPackage(
        "package.sacrifice.v1",
        "sacrifice",
        ("cost.sacrifice_draw.v1", "cost.self_sacrifice.v1"),
        "Recognized sacrifice outlet or self-sacrifice cost job.",
    ),
    FunctionalPackage(
        "package.spell_matters.v1",
        "spell_matters",
        ("type.spells.v1", "trigger.spells.v1"),
        "Recognized instant/sorcery enabler or spell-cast listener job.",
    ),
    FunctionalPackage(
        "package.graveyard_interaction.v1",
        "graveyard_interaction",
        ("effect.recursion.v1", "effect.exile.graveyard_card.v1"),
        "Recognized targeted graveyard recursion or graveyard-card exile job.",
    ),
)


def functional_package_contributions(features: list[dict]) -> list[dict]:
    """Return source-ordered package evidence from stable feature rule IDs."""
    contributions = []
    for package in FUNCTIONAL_PACKAGES:
        evidence = [
            feature for feature in features
            if feature.get("rule_id") in package.feature_rule_ids
        ]
        if evidence:
            contributions.append({
                "package_id": package.package_id,
                "label": package.label,
                "explanation": package.explanation,
                "evidence": evidence,
            })
    return contributions
