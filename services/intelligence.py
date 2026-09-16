"""Conservative, read-only deck analysis. No legality or resource decisions."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re
import sqlite3

from mtgadb.model import Card, Deck
from mtgadb.query import CardQueryEngine

VERSION = "1"
ROLES = ("removal", "card_draw", "card_selection", "ramp", "mana_fixing",
         "counterspell", "protection", "recursion", "threat")
THEMES = ("tokens", "counters", "sacrifice", "graveyard", "typal", "spells",
          "artifacts", "enchantments", "lands")


@dataclass(frozen=True)
class InteractionFamily:
    """One directional relationship between two classified feature rules."""

    family_id: str
    source_feature_rule_id: str
    beneficiary_feature_rule_id: str
    explanation: str


INTERACTION_FAMILIES = (
    InteractionFamily(
        "interaction.token_draw.v1", "effect.token.v1", "trigger.token_draw.v1",
        "Creating this creature token can trigger the other card's draw ability "
        "while that payoff is on the battlefield.",
    ),
    InteractionFamily(
        "interaction.spell_draw.v1", "type.spells.v1", "trigger.spells.v1",
        "Casting this instant/sorcery can trigger the other card's draw ability "
        "while that payoff is on the battlefield.",
    ),
    InteractionFamily(
        "interaction.token_sacrifice.v1", "effect.token.v1", "cost.sacrifice_draw.v1",
        "The created creature token can pay the other permanent's sacrifice cost "
        "to draw a card; the token is consumed.",
    ),
)

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


def classify_card(card: Card) -> dict:
    features = []
    types = set(card.types.split())
    permanent = bool(types & {"Creature", "Artifact", "Enchantment", "Land", "Planeswalker"})
    text_lines = [line.strip() for line in card.rules_text.splitlines() if line.strip()]

    def add(rule_id, dimension, label, relationship, evidence, explanation):
        features.append(dict(rule_id=rule_id, dimension=dimension, label=label,
                             relationship=relationship, evidence=evidence, explanation=explanation))

    for type_name, theme in (("Artifact", "artifacts"), ("Enchantment", "enchantments"), ("Land", "lands")):
        if type_name in types:
            add("type." + theme + ".v1", "theme", theme, "member", card.types,
                "Card has the " + type_name + " type; no payoff is inferred.")
    if types & {"Instant", "Sorcery"}:
        add("type.spells.v1", "theme", "spells", "enabler", card.types,
            "Casting this instant/sorcery can enable a matching cast trigger.")
    cannot_attack = any(
        line.casefold() == "defender"
        or line.casefold() == "this creature can't attack."
        or line.casefold() == f"{card.name} can't attack.".casefold()
        for line in text_lines
    )
    if "Creature" in types and card.power.isdigit() and int(card.power) > 0 and not cannot_attack:
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
    text_supported = not anomalous and not contextual
    for line in text_lines:
        matched = False
        for rule_id, pattern, outputs, explanation in RULES:
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
        if not matched:
            unsupported.append(line)
    features.sort(key=lambda f: (f["rule_id"], f["dimension"], f["label"], f["evidence"]))
    return dict(title_id=card.title_id, name=card.name, features=features,
                status="unclassified" if not features else "partial" if unsupported else "classified",
                unsupported_text=sorted(set(unsupported)),
                text_status="anomalous" if anomalous else "no_text" if not card.rules_text else
                            "unsupported" if unsupported else "supported",
                unclassified_dimensions=[dim for dim in ("role", "theme")
                                         if not any(f["dimension"] == dim for f in features)])


def interactions(cards: list[dict]) -> list[dict]:
    result = []
    # Exact feature pairs, never a shared-theme join. All require distinct titles.
    for source in cards:
        for beneficiary in cards:
            if source["title_id"] == beneficiary["title_id"]:
                continue
            for family in INTERACTION_FAMILIES:
                a = next((f for f in source["features"]
                          if f["rule_id"] == family.source_feature_rule_id), None)
                b = next((f for f in beneficiary["features"]
                          if f["rule_id"] == family.beneficiary_feature_rule_id), None)
                if a and b:
                    result.append(dict(
                        rule_id=family.family_id,
                        source_title_id=source["title_id"],
                        target_title_id=beneficiary["title_id"],
                        explanation=family.explanation,
                        evidence=[a, b],
                    ))
    return sorted(result, key=lambda x: (x["rule_id"], x["source_title_id"], x["target_title_id"]))


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
        roles, themes = Counter(), Counter()
        lands = 0
        for title_id in sorted(catalog):
            card, quantity = catalog[title_id], quantities[title_id]
            row = classify_card(card)
            row["quantity"] = quantity
            rows.append(row)
            if "Land" in card.types.split():
                lands += quantity
            else:
                curve[card.cmc] += quantity
            for dim, counts in (("role", roles), ("theme", themes)):
                # Count a card's copies once per label, not once per matched rule.
                for label in {f["label"] for f in row["features"] if f["dimension"] == dim}:
                    counts[label] += quantity
        zones[zone_name] = dict(total_count=sum(zone.values()), resolved_count=sum(quantities.values()),
                                land_count=lands, nonland_mana_curve={str(k): curve[k] for k in sorted(curve)},
                                role_counts={k: roles[k] for k in ROLES}, theme_counts={k: themes[k] for k in THEMES},
                                cards=rows, diagnostics=diagnostics,
                                coverage="partial" if diagnostics else "resolved")
    return dict(analysis_version=VERSION, legality="not_evaluated", zones=zones,
                interactions=interactions(zones["main"]["cards"]),
                limitations=["Only whole supported ability lines are interpreted; other text remains unsupported.",
                             "Counts describe recognized features, not deck quality or complete role coverage.",
                             "Curve uses canonical mana value, not alternative costs or mana-source probabilities.",
                             "Interactions are conditional possibilities, not combo or legality proofs."])
