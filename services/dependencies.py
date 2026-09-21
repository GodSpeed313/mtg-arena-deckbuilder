"""Deterministic, compatibility-aware support from reviewed card features."""
from __future__ import annotations

from dataclasses import dataclass

DEPENDENCY_MODEL_VERSION = "2"


@dataclass(frozen=True, slots=True)
class DependencyDefinition:
    dependency_id: str
    label: str
    enabler_rule_ids: tuple[str, ...]
    enabler_relationship: str
    payoff_rule_ids: tuple[str, ...]
    payoff_relationship: str
    explanation: str
    policy: str = "strict"
    interaction_family_id: str | None = None
    interaction_explanation: str | None = None


@dataclass(frozen=True, slots=True)
class InteractionFamily:
    family_id: str
    source_feature_rule_ids: tuple[str, ...]
    beneficiary_feature_rule_ids: tuple[str, ...]
    explanation: str

    @property
    def source_feature_rule_id(self) -> str:
        return self.source_feature_rule_ids[0]

    @property
    def beneficiary_feature_rule_id(self) -> str:
        return self.beneficiary_feature_rule_ids[0]


DEPENDENCIES = (
    DependencyDefinition(
        "dependency.lifegain.v1", "lifegain", ("effect.lifegain.v1",), "producer",
        ("trigger.lifegain.v1",), "payoff",
        "Explicit lifegain production supports an exact reviewed lifegain listener.",
    ),
    DependencyDefinition(
        "dependency.plus1_counters.v2", "plus1_counters", ("effect.counter.v1",), "producer",
        ("trigger.counter.v1",), "payoff",
        "Compatible reviewed +1/+1 counter placement supports a reviewed counter listener.",
    ),
    DependencyDefinition(
        "dependency.creature_token_entry.v2", "creature_token_entry",
        ("effect.token.v1", "effect.noncreature_token.v1"), "producer",
        ("trigger.token_draw.v1",), "payoff",
        "Reviewed token production supports a token-entry listener only when token scope is compatible.",
        interaction_family_id="interaction.token_draw.v1",
        interaction_explanation="Creating a compatible token can trigger the other card's reviewed draw ability while that payoff is on the battlefield.",
    ),
    DependencyDefinition(
        "dependency.creature_token_sacrifice.v2", "creature_token_sacrifice",
        ("effect.token.v1",), "producer", ("cost.sacrifice_draw.v1",), "consumer",
        "Friendly creature-token production is one optional support route for a reviewed generic creature-sacrifice outlet.",
        policy="optional_support", interaction_family_id="interaction.token_sacrifice.v1",
        interaction_explanation="The created creature token can pay the other permanent's sacrifice cost to draw a card; the token is consumed.",
    ),
    DependencyDefinition(
        "dependency.spell_cast.v1", "spell_cast", ("type.spells.v1",), "enabler",
        ("trigger.spells.v1",), "payoff",
        "An instant or sorcery supports an exact reviewed spell-cast payoff.",
        interaction_family_id="interaction.spell_draw.v1",
        interaction_explanation="Casting this instant/sorcery can trigger the other card's draw ability while that payoff is on the battlefield.",
    ),
)


def _interaction_families() -> tuple[InteractionFamily, ...]:
    by_id = {d.interaction_family_id: d for d in DEPENDENCIES if d.interaction_family_id}
    return tuple(InteractionFamily(
        family_id, by_id[family_id].enabler_rule_ids,
        by_id[family_id].payoff_rule_ids,
        by_id[family_id].interaction_explanation or "",
    ) for family_id in (
        "interaction.token_draw.v1", "interaction.spell_draw.v1",
        "interaction.token_sacrifice.v1",
    ))


INTERACTION_FAMILIES = _interaction_families()


def _matching_features(card: dict, rule_ids: tuple[str, ...], relationship: str) -> list[dict]:
    return [feature for feature in card["features"]
            if feature.get("rule_id") in rule_ids
            and feature.get("relationship") == relationship]


def _side(cards: list[dict], rule_ids: tuple[str, ...], relationship: str) -> dict:
    participants = []
    for card in sorted(cards, key=lambda item: item["title_id"]):
        evidence = _matching_features(card, rule_ids, relationship)
        if evidence:
            participants.append({"title_id": card["title_id"], "name": card["name"],
                                 "quantity": card["quantity"], "evidence": evidence})
    return {"relationship": relationship,
            "acceptable_feature_rule_ids": list(rule_ids),
            "matching_semantics": "any",
            "feature_rule_ids": list(rule_ids),
            "copy_count": sum(card["quantity"] for card in participants),
            "cards": participants}


def _context(feature: dict) -> dict | None:
    value = feature.get("dependency_context")
    return value if isinstance(value, dict) else None


def _reviewed_context(feature: dict) -> dict | None:
    """Return structurally valid classifier context, otherwise fail closed."""
    context = _context(feature)
    if context is None:
        return None
    prerequisites = context.get("prerequisites")
    if (
        context.get("availability")
        not in {"unconditional", "conditional", "partially_reviewed"}
        or context.get("parse_status") not in {"supported", "partial"}
        or not isinstance(context.get("unsupported_remainder"), list)
        or not isinstance(prerequisites, dict)
        or prerequisites.get("trigger") is not None
        and not isinstance(prerequisites.get("trigger"), dict)
        or not isinstance(prerequisites.get("costs"), list)
        or not isinstance(prerequisites.get("conditions"), list)
        or not isinstance(prerequisites.get("qualifiers"), list)
    ):
        return None
    return context


def _context_matches_rule(feature: dict, context: dict, *, payoff: bool) -> bool:
    """Confirm that a feature ID agrees with its reviewed structured evidence."""
    rule_id = feature.get("rule_id")
    effect = context.get("effect")
    trigger = context["prerequisites"].get("trigger")
    costs = context["prerequisites"]["costs"]
    if rule_id == "effect.lifegain.v1":
        return (
            isinstance(effect, dict)
            and effect.get("kind") == "gain_life"
            and effect.get("controller") == "you"
            and effect.get("friendly") is True
        )
    if rule_id == "effect.counter.v1":
        return (
            isinstance(effect, dict)
            and effect.get("kind") == "put_counter"
            and effect.get("counter_type") == "+1/+1"
        )
    if rule_id == "effect.token.v1":
        return (
            isinstance(effect, dict)
            and effect.get("kind") == "create_creature_token"
            and isinstance(effect.get("token"), dict)
            and effect["token"].get("kind") == "creature"
        )
    if rule_id == "effect.noncreature_token.v1":
        return (
            isinstance(effect, dict)
            and effect.get("kind") == "create_noncreature_token"
            and isinstance(effect.get("token"), dict)
            and effect["token"].get("kind") == "named_noncreature"
        )
    if rule_id == "type.spells.v1":
        return (
            not payoff
            and context.get("ability_kind") == "card_type"
            and context.get("availability") == "unconditional"
            and effect is None
        )
    if rule_id == "trigger.lifegain.v1":
        return (
            payoff and isinstance(trigger, dict)
            and trigger.get("event") == "life_gained"
            and trigger.get("subject") == "you"
        )
    if rule_id == "trigger.counter.v1":
        return (
            payoff and isinstance(trigger, dict)
            and trigger.get("event") == "counter_placed"
            and trigger.get("subject") in {"self", "creature_you_control"}
        )
    if rule_id == "trigger.token_draw.v1":
        return (
            payoff and isinstance(trigger, dict)
            and trigger.get("event") == "token_enters"
            and trigger.get("subject") in {"creature_token", "tokens"}
        )
    if rule_id == "trigger.spells.v1":
        return (
            payoff and isinstance(trigger, dict)
            and trigger.get("event") == "spell_cast"
            and trigger.get("subject") == "instant_or_sorcery"
        )
    if rule_id == "cost.sacrifice_draw.v1":
        return payoff and any(
            isinstance(cost, dict)
            and cost.get("kind") == "sacrifice"
            and cost.get("subject") == "another_creature"
            for cost in costs
        )
    return False


def _compatible(definition, enabler_card, enabler_feature, payoff_card, payoff_feature) -> bool:
    enabler_context = _reviewed_context(enabler_feature)
    payoff_context = _reviewed_context(payoff_feature)
    if (
        enabler_context is None
        or payoff_context is None
        or not _context_matches_rule(enabler_feature, enabler_context, payoff=False)
        or not _context_matches_rule(payoff_feature, payoff_context, payoff=True)
    ):
        return False
    if definition.label == "creature_token_entry":
        trigger = payoff_context["prerequisites"]["trigger"]
        if trigger.get("subject") == "creature_token":
            return enabler_feature["rule_id"] == "effect.token.v1"
        if trigger.get("subject") == "tokens":
            return enabler_feature["rule_id"] in {"effect.token.v1", "effect.noncreature_token.v1"}
        return False
    if definition.label == "plus1_counters":
        trigger = payoff_context["prerequisites"]["trigger"]
        effect = enabler_context["effect"]
        subject, target = trigger.get("subject"), effect.get("target")
        targets = {"self", "target_creature", "target_creature_you_control",
                   "another_target_creature", "another_target_creature_you_control"}
        if subject == "creature_you_control":
            return target in targets
        if subject == "self":
            if target == "self":
                return enabler_card["title_id"] == payoff_card["title_id"]
            if target in {"target_creature", "target_creature_you_control"}:
                return True
            if target in {"another_target_creature", "another_target_creature_you_control"}:
                return enabler_card["title_id"] != payoff_card["title_id"]
        return False
    return True


def _pairs(cards: list[dict], definition: DependencyDefinition) -> list[dict]:
    pairs = []
    for enabler_card in sorted(cards, key=lambda item: item["title_id"]):
        for enabler_feature in _matching_features(enabler_card, definition.enabler_rule_ids,
                                                  definition.enabler_relationship):
            for payoff_card in sorted(cards, key=lambda item: item["title_id"]):
                for payoff_feature in _matching_features(payoff_card, definition.payoff_rule_ids,
                                                          definition.payoff_relationship):
                    if not _compatible(definition, enabler_card, enabler_feature,
                                       payoff_card, payoff_feature):
                        continue
                    enabler_context = _reviewed_context(enabler_feature)
                    pairs.append({
                        "enabler_title_id": enabler_card["title_id"],
                        "payoff_title_id": payoff_card["title_id"],
                        "support": (
                            "unconditional"
                            if enabler_context["availability"] == "unconditional"
                            else "conditional"
                        ),
                        "enabler_evidence": enabler_feature,
                        "payoff_evidence": payoff_feature,
                    })
    return pairs


def _acceptable_enablers(definition: DependencyDefinition, payoff: dict) -> tuple[str, ...]:
    if definition.label != "creature_token_entry":
        return definition.enabler_rule_ids
    subjects = {
        context["prerequisites"]["trigger"]["subject"]
        for card in payoff["cards"]
        for feature in card["evidence"]
        if (context := _reviewed_context(feature)) is not None
        and _context_matches_rule(feature, context, payoff=True)
    }
    if not subjects:
        return ()
    return (("effect.token.v1",) if subjects and subjects <= {"creature_token"}
            else definition.enabler_rule_ids)


def dependency_findings(cards: list[dict]) -> list[dict]:
    """Return active, zone-local compatibility facts from reviewed evidence."""
    findings = []
    for definition in DEPENDENCIES:
        enabler = _side(cards, definition.enabler_rule_ids, definition.enabler_relationship)
        payoff = _side(cards, definition.payoff_rule_ids, definition.payoff_relationship)
        if not enabler["cards"] and not payoff["cards"]:
            continue
        pairs = _pairs(cards, definition)
        if pairs:
            state = ("supported" if any(pair["support"] == "unconditional" for pair in pairs)
                     else "conditionally_supported")
        elif payoff["cards"]:
            state = ("optional_support_absent" if definition.policy == "optional_support"
                     else "payoff_without_enabler" if not enabler["cards"]
                     else "payoff_without_compatible_enabler")
        else:
            state = "enabler_without_payoff"
        acceptable = _acceptable_enablers(definition, payoff)
        enabler["acceptable_feature_rule_ids"] = list(acceptable)
        enabler["feature_rule_ids"] = list(acceptable)
        findings.append({"dependency_id": definition.dependency_id,
                         "label": definition.label, "policy": definition.policy,
                         "state": state, "explanation": definition.explanation,
                         "enabler_side": enabler, "payoff_side": payoff,
                         "compatible_pairs": pairs})
    return findings


def interaction_findings(cards: list[dict]) -> list[dict]:
    """Project compatible reviewed pairs into established interactions."""
    result = []
    definitions = {item.interaction_family_id: item for item in DEPENDENCIES}
    for family in INTERACTION_FAMILIES:
        definition = definitions[family.family_id]
        for pair in _pairs(cards, definition):
            if pair["enabler_title_id"] == pair["payoff_title_id"]:
                continue
            result.append({"rule_id": family.family_id,
                           "source_title_id": pair["enabler_title_id"],
                           "target_title_id": pair["payoff_title_id"],
                           "explanation": family.explanation,
                           "evidence": [pair["enabler_evidence"], pair["payoff_evidence"]]})
    return sorted(result, key=lambda item: (
        item["rule_id"], item["source_title_id"], item["target_title_id"],
    ))
