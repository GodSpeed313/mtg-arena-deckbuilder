"""Conservative, read-only deck analysis. No legality or resource decisions."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict
import re
import sqlite3

from mtgadb.model import Card, Deck
from mtgadb.query import CardQueryEngine
from services.abilities import Ability, decompose_abilities
from services.dependencies import (
    DEPENDENCY_MODEL_VERSION,
    INTERACTION_FAMILIES,
    dependency_findings,
    interaction_findings,
)
from services.needs import NEEDS_MODEL_VERSION, needs_findings
from services.packages import (
    FUNCTIONAL_PACKAGES, PACKAGE_MODEL_VERSION, functional_package_contributions,
)

VERSION = "3"
ROLES = ("removal", "card_draw", "card_selection", "ramp", "mana_fixing",
         "counterspell", "protection", "recursion", "threat")
THEMES = ("tokens", "counters", "lifegain", "sacrifice", "graveyard", "typal", "spells",
          "artifacts", "enchantments", "lands")


ABILITY_PROJECTED_RULE_IDS = frozenset({
    "effect.token.v1",
    "effect.noncreature_token.v1",
    "effect.lifegain.v1",
    "effect.counter.v1",
    "trigger.lifegain.v1",
    "trigger.counter.v1",
    "trigger.spells.v1",
    "trigger.token_draw.v1",
    "cost.sacrifice_draw.v1",
    "effect.recursion.v1",
})

# Whole ability lines only. No substring/keyword classification: conditional,
# modal, opponent-directed and unfamiliar variants deliberately remain unknown.
# (stable ID, full pattern, [(dimension, label, relationship)], explanation)
RULES = (
    ("effect.destroy.v1", r"Destroy target (?:creature|artifact|enchantment)\.",
     (("role", "removal", "effect"),), "Explicit targeted permanent destruction."),
    ("effect.draw.v1", r"Draw (?:a card|two cards|three cards)\.",
     (("role", "card_draw", "effect"),), "Explicit instruction to draw cards."),
    ("effect.scry.v1", r"Scry [1-3]\.",
     (("role", "card_selection", "effect"),), "Scry selects future draws; it does not draw cards."),
    ("effect.counterspell.v1", r"Counter target spell\.",
     (("role", "counterspell", "effect"),), "Explicit spell countering, not a counter on a permanent."),
    ("effect.protection.v1", r"Target creature you control gains (?:hexproof|indestructible) until end of turn\.",
     (("role", "protection", "effect"),), "Temporary protection for a creature you control."),
    ("effect.recursion.v1", r"Return target creature card from your graveyard to your hand\.",
     (("role", "recursion", "effect"), ("theme", "graveyard", "consumer")),
     "Returns a creature card from your graveyard to hand, not the battlefield."),
    ("effect.land_ramp.v1", r"Search your library for a basic land card, put it onto the battlefield tapped, then shuffle\.",
     (("role", "ramp", "effect"), ("role", "mana_fixing", "effect"),
      ("theme", "lands", "producer")), "Adds a tapped basic land; color choice depends on available basics."),
    ("effect.token.v1", r"Create a 1/1 white Soldier creature token\.",
     (("theme", "tokens", "producer"),), "Creates one creature token; no payoff is inferred."),
    ("trigger.token_draw.v1", r"Whenever a creature token enters the battlefield under your control, draw a card\.",
     (("role", "card_draw", "conditional"), ("theme", "tokens", "payoff")),
     "Draw requires a creature token entering under your control."),
    ("effect.typal.v1", r"Other Soldiers you control get \+1/\+1\.",
     (("theme", "typal", "payoff"),), "Benefits other Soldiers you control, not all creatures."),
    ("effect.counter.v1", r"Put a \+1/\+1 counter on target creature you control\.",
     (("theme", "counters", "producer"),), "Places a +1/+1 counter on your creature."),
    ("trigger.spells.v1", r"Whenever you cast an instant or sorcery spell, draw a card\.",
     (("role", "card_draw", "conditional"), ("theme", "spells", "payoff")),
     "Draw requires casting an instant or sorcery."),
    ("cost.sacrifice_draw.v1", r"Sacrifice another creature: Draw a card\.",
     (("role", "card_draw", "conditional"), ("theme", "sacrifice", "consumer")),
     "Repeatable sacrifice outlet on a permanent; each activation needs another creature."),
    ("cost.self_sacrifice.v1", r"Sacrifice this creature: Draw a card\.",
     (("role", "card_draw", "conditional"), ("theme", "sacrifice", "cost")),
     "Sacrifices itself; not a reusable outlet."),
)


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _ability_coverage(abilities: tuple[Ability, ...]) -> dict:
    total = len(abilities)
    structural = sum(ability.kind != "unsupported" for ability in abilities)
    meaningful = sum(
        ability.parse_status in {"supported", "partial"} for ability in abilities
    )
    return {
        "ability_count": total,
        "structurally_recognized_ability_count": structural,
        "meaningfully_understood_ability_count": meaningful,
        "structural_recognition": _ratio(structural, total),
        "meaningful_understanding": _ratio(meaningful, total),
        "supported_ability_count": sum(
            ability.parse_status == "supported" for ability in abilities
        ),
        "partial_ability_count": sum(
            ability.parse_status == "partial" for ability in abilities
        ),
        "unsupported_ability_count": sum(
            ability.parse_status == "unsupported" for ability in abilities
        ),
        "anomalous_ability_count": sum(
            ability.parse_status == "anomalous" for ability in abilities
        ),
    }


def _project_ability_features(
    abilities: tuple[Ability, ...], *, permanent: bool
) -> list[dict]:
    """Project reviewed ability semantics into the stable Pass #1 feature schema."""
    projected = []

    def context(ability, effect=None):
        prerequisites = {
            "trigger": asdict(ability.trigger) if ability.trigger else None,
            "costs": [asdict(cost) for cost in ability.costs],
            "conditions": [asdict(item) for item in (
                effect.conditions if effect is not None else ()
            )],
            "qualifiers": [asdict(item) for item in (
                *ability.qualifiers,
                *(effect.qualifiers if effect is not None else ()),
            )],
        }
        availability = (
            "partially_reviewed"
            if ability.unsupported_remainder
            else "unconditional" if not any(prerequisites.values())
            else "conditional"
        )
        return {
            "ability_id": ability.ability_id,
            "ability_kind": ability.kind,
            "availability": availability,
            "prerequisites": prerequisites,
            "effect": asdict(effect) if effect is not None else None,
            "parse_status": ability.parse_status,
            "unsupported_remainder": [
                asdict(item) for item in ability.unsupported_remainder
            ],
        }

    def feature(
        rule_id, dimension, label, relationship, evidence, explanation,
        dependency_context=None,
    ):
        item = dict(
            rule_id=rule_id,
            dimension=dimension,
            label=label,
            relationship=relationship,
            evidence=evidence,
            explanation=explanation,
        )
        if dependency_context is not None:
            item["dependency_context"] = dependency_context
        projected.append(item)

    for ability in abilities:
        if ability.parse_status not in {"supported", "partial"}:
            continue
        for keyword in ability.keywords:
            feature(
                f"ability.keyword.{keyword.name}.v1",
                "ability",
                keyword.name,
                "intrinsic",
                keyword.evidence,
                "Exact whole-line intrinsic keyword; no strategic value is inferred.",
            )
        draws = [effect for effect in ability.effects if effect.kind == "draw"]
        for effect in ability.effects:
            friendly_trigger = ability.trigger is None or ability.trigger.friendly is True
            capability_effects = {
                ("exile", "target_creature"): (
                    "effect.exile.creature.v1", "exile_creature",
                ),
                ("exile", "target_permanent"): (
                    "effect.exile.permanent.v1", "exile_permanent",
                ),
                ("exile", "target_artifact"): (
                    "effect.exile.artifact.v1", "exile_artifact",
                ),
                ("exile", "target_enchantment"): (
                    "effect.exile.enchantment.v1", "exile_enchantment",
                ),
                ("exile", "card"): (
                    "effect.exile.graveyard_card.v1", "exile_graveyard_card",
                ),
                ("return_to_hand", "target_creature"): (
                    "effect.bounce.creature.v1", "bounce_creature",
                ),
                ("return_to_hand", "target_permanent"): (
                    "effect.bounce.permanent.v1", "bounce_permanent",
                ),
                ("discard", "target_opponent"): (
                    "effect.discard.opponent.v1", "discard_opponent",
                ),
                ("force_sacrifice", "creature"): (
                    "effect.force_sacrifice.creature.v1", "force_sacrifice_creature",
                ),
                ("force_sacrifice", "permanent"): (
                    "effect.force_sacrifice.permanent.v1", "force_sacrifice_permanent",
                ),
            }
            capability = capability_effects.get((effect.kind, effect.target))
            if capability is not None:
                rule_id, label = capability
                feature(
                    rule_id,
                    "ability",
                    label,
                    "capability",
                    effect.evidence,
                    "Exact reviewed interaction capability; no strategic role, "
                    "quality, or recommendation is inferred.",
                )
            if effect.kind == "return_from_graveyard":
                feature(
                    "effect.recursion.v1", "role", "recursion", "effect",
                    effect.evidence,
                    "Returns a creature card from your graveyard to hand, not the battlefield.",
                )
                feature(
                    "effect.recursion.v1", "theme", "graveyard", "consumer",
                    effect.evidence,
                    "Returns a creature card from your graveyard to hand, not the battlefield.",
                )
            damage_targets = {
                "target_creature": ("creature", "damage_creature"),
                "target_player": ("player", "damage_player"),
                "target_opponent": ("opponent", "damage_opponent"),
                "any_target": ("any_target", "damage_any_target"),
                "target_planeswalker": ("planeswalker", "damage_planeswalker"),
                "target_battle": ("battle", "damage_battle"),
                "each_opponent": ("each_opponent", "damage_each_opponent"),
            }
            if effect.kind == "damage" and effect.target in damage_targets:
                target_id, label = damage_targets[effect.target]
                relationship = {
                    "activated": "activated",
                    "triggered": "triggered",
                }.get(ability.kind, "one_shot")
                feature(
                    f"effect.damage.{target_id}.v1",
                    "ability",
                    label,
                    relationship,
                    effect.evidence,
                    "Explicit direct-damage capability with retained amount and "
                    "target; no removal, burn, or effectiveness is inferred.",
                )
            if (
                effect.kind == "create_creature_token"
                and effect.friendly is True
                and friendly_trigger
            ):
                feature(
                    "effect.token.v1", "theme", "tokens", "producer",
                    effect.evidence,
                    "Creates a friendly creature token; no payoff is inferred.",
                    context(ability, effect),
                )
            if (
                effect.kind == "create_noncreature_token"
                and effect.friendly is True
                and friendly_trigger
            ):
                feature(
                    "effect.noncreature_token.v1", "theme", "tokens", "producer",
                    effect.evidence,
                    "Creates a reviewed named noncreature token; no use or payoff is inferred.",
                    context(ability, effect),
                )
            if (
                effect.kind == "gain_life"
                and effect.friendly is True
                and friendly_trigger
            ):
                feature(
                    "effect.lifegain.v1", "theme", "lifegain", "producer",
                    effect.evidence,
                    "Explicitly instructs you to gain life; no strategic value is inferred.",
                    context(ability, effect),
                )
            if (
                effect.kind == "put_counter"
                and effect.counter_type == "+1/+1"
                and effect.friendly is True
                and friendly_trigger
            ):
                feature(
                    "effect.counter.v1", "theme", "counters", "producer",
                    effect.evidence,
                    "Explicitly places +1/+1 counters; no archetype is inferred.",
                    context(ability, effect),
                )
        if (
            permanent
            and ability.kind == "activated"
            and any(effect.kind == "damage" for effect in ability.effects)
        ):
            feature(
                "ability.activated_damage.v1",
                "ability",
                "activated_damage",
                "activated",
                ability.raw_text,
                "Explicit activated damage capability; activation frequency is not inferred.",
            )
        if (
            permanent
            and ability.trigger is not None
            and ability.trigger.event == "life_gained"
            and ability.trigger.friendly is True
        ):
            feature(
                "trigger.lifegain.v1", "theme", "lifegain", "payoff",
                ability.raw_text,
                "Listens for you gaining life; it does not itself gain life.",
                context(ability),
            )
        if (
            permanent
            and ability.trigger is not None
            and ability.trigger.event == "counter_placed"
            and ability.trigger.friendly is True
        ):
            feature(
                "trigger.counter.v1", "theme", "counters", "payoff",
                ability.raw_text,
                "Listens for +1/+1 counter placement; it does not itself place counters.",
                context(ability),
            )
        if (
            ability.trigger is not None
            and ability.trigger.event == "spell_cast"
            and ability.trigger.friendly is True
            and draws
        ):
            feature(
                "trigger.spells.v1", "role", "card_draw", "conditional",
                ability.raw_text,
                "Draw requires you to cast an instant or sorcery spell.",
                context(ability),
            )
            feature(
                "trigger.spells.v1", "theme", "spells", "payoff",
                ability.raw_text,
                "Draw requires you to cast an instant or sorcery spell.",
                context(ability),
            )
        if (
            ability.trigger is not None
            and ability.trigger.event == "token_enters"
            and ability.trigger.friendly is True
            and draws
        ):
            feature(
                "trigger.token_draw.v1", "role", "card_draw", "conditional",
                ability.raw_text,
                "Draw requires a token to enter under your control.",
                context(ability),
            )
            feature(
                "trigger.token_draw.v1", "theme", "tokens", "payoff",
                ability.raw_text,
                "Draw requires a token to enter under your control.",
                context(ability),
            )
        sacrifice_another = any(
            cost.kind == "sacrifice"
            and cost.subject == "another_creature"
            and cost.timing == "activated"
            for cost in ability.costs
        )
        if permanent and ability.kind == "activated" and sacrifice_another and draws:
            feature(
                "cost.sacrifice_draw.v1", "role", "card_draw", "conditional",
                ability.raw_text,
                "Activated permanent ability sacrifices another creature to draw.",
                context(ability),
            )
            feature(
                "cost.sacrifice_draw.v1", "theme", "sacrifice", "consumer",
                ability.raw_text,
                "Activated permanent ability consumes another creature as its cost.",
                context(ability),
            )
    return projected


def classify_card(card: Card) -> dict:
    features = []
    types = set(card.types.split())
    permanent = bool(types & {"Creature", "Artifact", "Enchantment", "Land", "Planeswalker"})
    text_lines = [line.strip() for line in card.rules_text.splitlines() if line.strip()]

    def add(
        rule_id, dimension, label, relationship, evidence, explanation,
        dependency_context=None,
    ):
        item = dict(rule_id=rule_id, dimension=dimension, label=label,
                    relationship=relationship, evidence=evidence, explanation=explanation)
        if dependency_context is not None:
            item["dependency_context"] = dependency_context
        features.append(item)

    for type_name, theme in (("Artifact", "artifacts"), ("Enchantment", "enchantments"), ("Land", "lands")):
        if type_name in types:
            add("type." + theme + ".v1", "theme", theme, "member", card.types,
                "Card has the " + type_name + " type; no payoff is inferred.")
    if types & {"Instant", "Sorcery"}:
        add("type.spells.v1", "theme", "spells", "enabler", card.types,
            "Casting this instant/sorcery can enable a matching cast trigger.",
            {
                "ability_id": None,
                "ability_kind": "card_type",
                "availability": "unconditional",
                "prerequisites": {
                    "trigger": None, "costs": [], "conditions": [], "qualifiers": [],
                },
                "effect": None,
                "parse_status": "supported",
                "unsupported_remainder": [],
            })
    cannot_attack = any(
        line.casefold() == "defender"
        or line.casefold() == "this creature can't attack."
        or line.casefold() == f"{card.name} can't attack.".casefold()
        for line in text_lines
    )
    self_names = {card.name, card.name.split(",", 1)[0]}
    self_name_pattern = "|".join(
        re.escape(name) for name in sorted(self_names, key=len, reverse=True)
    )
    conditional_creature_line = next((
        line for line in text_lines
        if re.fullmatch(
            rf"As long as .+, (?:{self_name_pattern}) isn't a creature\.",
            line,
            re.I,
        )
    ), None)
    if "Creature" in types and card.power.isdigit() and int(card.power) > 0 and not cannot_attack:
        if conditional_creature_line:
            add(
                "type.conditional_threat.v1", "role", "threat", "conditional",
                conditional_creature_line,
                "Printed positive-power creature is a combat threat only while its explicit creature-state condition is satisfied.",
            )
        else:
            add("type.threat.v1", "role", "threat", "potential", card.types + " " + card.power + "/" + card.toughness,
                "Positive-power creature: potential combat threat, not an effectiveness rating.")

    unsupported = []
    # An anomalous canonical choice cannot safely ground ability classification.
    contextual = bool(re.search(
        r"^(?:Choose |As an additional cost|If |Unless |Instead|You may |Until |For each |Whenever |When |At )",
        card.rules_text, re.M)) and any(
            not any(re.fullmatch(pattern, line.strip(), re.I) for _, pattern, _, _ in RULES)
            for line in text_lines)
    anomalous = card.resolution.value == "anomaly"
    abilities = decompose_abilities(
        card.rules_text,
        card_name=card.name,
        card_types=card.types,
        canonical_anomaly=anomalous,
    )
    projected_features = _project_ability_features(abilities, permanent=permanent)
    features.extend(projected_features)
    text_supported = not anomalous and not contextual
    matched_lines = set()
    for line in text_lines:
        matched = False
        for rule_id, pattern, outputs, explanation in RULES:
            if rule_id in ABILITY_PROJECTED_RULE_IDS:
                continue
            if not text_supported or not re.fullmatch(pattern, line, re.I):
                continue
            if rule_id.startswith(("cost.", "trigger.")) and not permanent:
                continue
            for dimension, label, relationship in outputs:
                add(rule_id, dimension, label, relationship, line, explanation)
            matched = True
        mana = re.fullmatch(r"\{T\}: Add \{([WUBRG])\}\.", line)
        if text_supported and permanent and "Land" not in types and mana:
            add("ability.mana_ramp.v1", "role", "ramp", "producer", line,
                "Nonland permanent produces mana by tapping; timing and summoning restrictions are not modeled.")
            matched = True
        if text_supported and permanent and line == "{T}: Add one mana of any color.":
            add("ability.fixing.v1", "role", "mana_fixing", "producer", line,
                "Produces a chosen color; availability and activation timing are not modeled.")
            if "Land" not in types:
                add("ability.any_ramp.v1", "role", "ramp", "producer", line,
                    "Nonland permanent is an additional mana source.")
            matched = True
        if matched:
            matched_lines.add(line)
    for ability in abilities:
        if ability.raw_text in matched_lines or ability.parse_status == "supported":
            continue
        if ability.parse_status == "partial":
            unsupported.extend(
                remainder.text for remainder in ability.unsupported_remainder
            )
        else:
            unsupported.append(ability.raw_text)
    features.sort(key=lambda f: (f["rule_id"], f["dimension"], f["label"], f["evidence"]))
    functional_packages = functional_package_contributions(features)
    return dict(title_id=card.title_id, name=card.name, features=features,
                functional_packages=functional_packages,
                status="unclassified" if not features else "partial" if unsupported else "classified",
                unsupported_text=sorted(set(unsupported)),
                text_status="anomalous" if anomalous else "no_text" if not card.rules_text else
                            "unsupported" if unsupported else "supported",
                abilities=[asdict(ability) for ability in abilities],
                ability_coverage=_ability_coverage(abilities),
                unclassified_dimensions=[dim for dim in ("role", "theme")
                                         if not any(f["dimension"] == dim for f in features)])


def interactions(cards: list[dict]) -> list[dict]:
    return interaction_findings(cards)


def analyze_deck(deck: Deck, con: sqlite3.Connection) -> dict:
    """Analyze zones independently; interactions/primary totals concern main only."""
    engine = CardQueryEngine(con)
    zones = {}
    for zone_name in ("main", "sideboard", "commander"):
        zone = getattr(deck, zone_name)
        quantities = Counter()
        catalog = {}
        diagnostics = []
        for arena_id, quantity in sorted(zone.items()):
            if type(quantity) is not int or quantity <= 0:
                raise ValueError("Analysis requires positive integer quantities")
            card = engine.by_arena_id(arena_id)
            if card is None:
                diagnostics.append(dict(code="unknown_printing", arena_id=arena_id, quantity=quantity))
                continue
            quantities[card.title_id] += quantity
            catalog[card.title_id] = card
        rows, curve = [], Counter()
        roles, themes, package_counts = Counter(), Counter(), Counter()
        lands = 0
        ability_totals = Counter()
        for title_id in sorted(catalog):
            card, quantity = catalog[title_id], quantities[title_id]
            row = classify_card(card)
            row["quantity"] = quantity
            rows.append(row)
            coverage = row["ability_coverage"]
            for key in (
                "ability_count",
                "structurally_recognized_ability_count",
                "meaningfully_understood_ability_count",
                "supported_ability_count",
                "partial_ability_count",
                "unsupported_ability_count",
                "anomalous_ability_count",
            ):
                ability_totals[key] += coverage[key] * quantity
            if "Land" in card.types.split():
                lands += quantity
            else:
                curve[card.cmc] += quantity
            for dim, counts in (("role", roles), ("theme", themes)):
                # Count a card's copies once per label, not once per matched rule.
                for label in {f["label"] for f in row["features"] if f["dimension"] == dim}:
                    counts[label] += quantity
            for package in row["functional_packages"]:
                package_counts[package["label"]] += quantity
        ability_count = ability_totals["ability_count"]
        rules_text_coverage = {
            **{key: ability_totals[key] for key in (
                "ability_count",
                "structurally_recognized_ability_count",
                "meaningfully_understood_ability_count",
                "supported_ability_count",
                "partial_ability_count",
                "unsupported_ability_count",
                "anomalous_ability_count",
            )},
            "structural_recognition": _ratio(
                ability_totals["structurally_recognized_ability_count"], ability_count,
            ),
            "meaningful_understanding": _ratio(
                ability_totals["meaningfully_understood_ability_count"], ability_count,
            ),
            "weighting": "card_copy_times_ability",
        }
        total_count = sum(zone.values())
        resolved_count = sum(quantities.values())
        zone_coverage = "partial" if diagnostics else "resolved"
        dependencies = dependency_findings(rows)
        evidence_boundary = {
            "claim_scope": "reviewed_features_only",
            "zone_resolution": zone_coverage,
            "unresolved_printing_copies": total_count - resolved_count,
            "unclassified_card_copies": sum(
                row["quantity"] for row in rows if row["status"] == "unclassified"
            ),
            "partially_classified_card_copies": sum(
                row["quantity"] for row in rows if row["status"] == "partial"
            ),
            "unsupported_text_card_copies": sum(
                row["quantity"] for row in rows if row["unsupported_text"]
            ),
            "rules_text_coverage": rules_text_coverage,
        }
        zones[zone_name] = dict(total_count=total_count, resolved_count=resolved_count,
                                land_count=lands, nonland_mana_curve={str(k): curve[k] for k in sorted(curve)},
                                role_counts={k: roles[k] for k in ROLES}, theme_counts={k: themes[k] for k in THEMES},
                                functional_package_counts={
                                    package.label: package_counts[package.label]
                                    for package in FUNCTIONAL_PACKAGES
                                },
                                dependencies=dependencies,
                                needs=needs_findings(dependencies, evidence_boundary),
                                cards=rows, diagnostics=diagnostics,
                                rules_text_coverage=rules_text_coverage,
                                coverage=zone_coverage)
    return dict(analysis_version=VERSION,
                functional_package_model_version=PACKAGE_MODEL_VERSION,
                dependency_model_version=DEPENDENCY_MODEL_VERSION,
                needs_model_version=NEEDS_MODEL_VERSION,
                legality="not_evaluated", zones=zones,
                interactions=interactions(zones["main"]["cards"]),
                limitations=["Only reviewed decomposed ability shapes are interpreted; other text remains unsupported.",
                             "Counts describe recognized features, not deck quality or complete role coverage.",
                             "Functional packages describe evidenced card jobs, not deck needs, archetypes, or recommendations.",
                             "Dependency findings describe zone-local structural support, not sufficiency, quality, or recommendations.",
                             "Needs identify missing reviewed dependency counterparts; they do not prove unsupported text lacks support or prescribe changes.",
                             "Curve uses canonical mana value, not alternative costs or mana-source probabilities.",
                             "Interactions are conditional possibilities, not combo or legality proofs."])
