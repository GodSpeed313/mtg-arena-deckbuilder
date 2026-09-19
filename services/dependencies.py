"""Deterministic dependency support derived from reviewed card features.

Dependencies report whether exact reviewed relationship sides coexist within
one deck zone.  They do not parse rules text or assess strength, sufficiency,
quality, candidates, or recommendations.
"""
from __future__ import annotations

from dataclasses import dataclass


DEPENDENCY_MODEL_VERSION = "1"


@dataclass(frozen=True, slots=True)
class DependencyDefinition:
    dependency_id: str
    label: str
    enabler_rule_ids: tuple[str, ...]
    enabler_relationship: str
    payoff_rule_ids: tuple[str, ...]
    payoff_relationship: str
    explanation: str
    interaction_family_id: str | None = None
    interaction_explanation: str | None = None


@dataclass(frozen=True, slots=True)
class InteractionFamily:
    """Backward-compatible interaction projection of a dependency definition."""

    family_id: str
    source_feature_rule_id: str
    beneficiary_feature_rule_id: str
    explanation: str


DEPENDENCIES = (
    DependencyDefinition(
        "dependency.lifegain.v1",
        "lifegain",
        ("effect.lifegain.v1",),
        "producer",
        ("trigger.lifegain.v1",),
        "payoff",
        "Explicit lifegain production supports an exact reviewed lifegain listener.",
    ),
    DependencyDefinition(
        "dependency.plus1_counters.v1",
        "plus1_counters",
        ("effect.counter.v1",),
        "producer",
        ("trigger.counter.v1",),
        "payoff",
        "Explicit +1/+1 counter placement supports an exact reviewed counter listener.",
    ),
    DependencyDefinition(
        "dependency.creature_token_entry.v1",
        "creature_token_entry",
        ("effect.token.v1",),
        "producer",
        ("trigger.token_draw.v1",),
        "payoff",
        "Friendly creature-token production supports an exact reviewed token-entry payoff.",
        "interaction.token_draw.v1",
        "Creating this creature token can trigger the other card's draw ability "
        "while that payoff is on the battlefield.",
    ),
    DependencyDefinition(
        "dependency.creature_token_sacrifice.v1",
        "creature_token_sacrifice",
        ("effect.token.v1",),
        "producer",
        ("cost.sacrifice_draw.v1",),
        "consumer",
        "Friendly creature-token production structurally supports an exact reviewed "
        "creature-sacrifice outlet.",
        "interaction.token_sacrifice.v1",
        "The created creature token can pay the other permanent's sacrifice cost "
        "to draw a card; the token is consumed.",
    ),
    DependencyDefinition(
        "dependency.spell_cast.v1",
        "spell_cast",
        ("type.spells.v1",),
        "enabler",
        ("trigger.spells.v1",),
        "payoff",
        "An instant or sorcery supports an exact reviewed spell-cast payoff.",
        "interaction.spell_draw.v1",
        "Casting this instant/sorcery can trigger the other card's draw ability "
        "while that payoff is on the battlefield.",
    ),
)


def _interaction_families() -> tuple[InteractionFamily, ...]:
    by_id = {
        definition.interaction_family_id: definition
        for definition in DEPENDENCIES
        if definition.interaction_family_id is not None
    }
    # Preserve the established public interaction ordering.
    return tuple(
        InteractionFamily(
            family_id,
            by_id[family_id].enabler_rule_ids[0],
            by_id[family_id].payoff_rule_ids[0],
            by_id[family_id].interaction_explanation or "",
        )
        for family_id in (
            "interaction.token_draw.v1",
            "interaction.spell_draw.v1",
            "interaction.token_sacrifice.v1",
        )
    )


INTERACTION_FAMILIES = _interaction_families()


def _side(cards: list[dict], rule_ids: tuple[str, ...], relationship: str) -> dict:
    participants = []
    for card in sorted(cards, key=lambda item: item["title_id"]):
        evidence = [
            feature for feature in card["features"]
            if feature.get("rule_id") in rule_ids
            and feature.get("relationship") == relationship
        ]
        if evidence:
            participants.append({
                "title_id": card["title_id"],
                "name": card["name"],
                "quantity": card["quantity"],
                "evidence": evidence,
            })
    return {
        "relationship": relationship,
        "feature_rule_ids": list(rule_ids),
        "copy_count": sum(card["quantity"] for card in participants),
        "cards": participants,
    }


def dependency_findings(cards: list[dict]) -> list[dict]:
    """Return active, zone-local dependency facts from classified card rows."""
    findings = []
    for definition in DEPENDENCIES:
        enabler = _side(
            cards, definition.enabler_rule_ids, definition.enabler_relationship,
        )
        payoff = _side(
            cards, definition.payoff_rule_ids, definition.payoff_relationship,
        )
        if not enabler["cards"] and not payoff["cards"]:
            continue
        state = (
            "supported"
            if enabler["cards"] and payoff["cards"]
            else "payoff_without_enabler"
            if payoff["cards"]
            else "enabler_without_payoff"
        )
        findings.append({
            "dependency_id": definition.dependency_id,
            "label": definition.label,
            "state": state,
            "explanation": definition.explanation,
            "enabler_side": enabler,
            "payoff_side": payoff,
        })
    return findings
