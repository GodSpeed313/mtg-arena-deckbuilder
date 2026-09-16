"""Bounded, deterministic decomposition of canonical card ability text.

The parser deliberately understands only a small reviewed grammar.  It keeps
unsupported text beside any supported components and performs no game-state or
strategic inference.  This module has no database or model dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True, slots=True)
class Condition:
    kind: str
    subject: str
    value: str
    evidence: str


@dataclass(frozen=True, slots=True)
class Qualifier:
    kind: str
    value: str
    evidence: str


@dataclass(frozen=True, slots=True)
class Trigger:
    event: str
    subject: str
    controller: str
    friendly: bool | None
    evidence: str
    conditions: tuple[Condition, ...] = ()


@dataclass(frozen=True, slots=True)
class Cost:
    kind: str
    subject: str
    timing: str
    evidence: str


@dataclass(frozen=True, slots=True)
class Keyword:
    name: str
    subject: str
    evidence: str
    value: str | None = None


@dataclass(frozen=True, slots=True)
class TokenSpec:
    quantity: int | str
    kind: str
    power: int | None
    toughness: int | None
    colors: tuple[str, ...]
    subtypes: tuple[str, ...]
    artifact: bool
    name: str | None = None
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Effect:
    effect_id: str
    kind: str
    evidence: str
    amount: int | str | None = None
    target: str | None = None
    counter_type: str | None = None
    controller: str = "you"
    friendly: bool | None = True
    token: TokenSpec | None = None
    conditions: tuple[Condition, ...] = ()
    qualifiers: tuple[Qualifier, ...] = ()


@dataclass(frozen=True, slots=True)
class UnsupportedRemainder:
    text: str
    scope: str
    reason: str


@dataclass(frozen=True, slots=True)
class Ability:
    ability_id: str
    source_index: int
    kind: str
    raw_text: str
    parse_status: str
    trigger: Trigger | None = None
    costs: tuple[Cost, ...] = ()
    effects: tuple[Effect, ...] = ()
    qualifiers: tuple[Qualifier, ...] = ()
    keywords: tuple[Keyword, ...] = ()
    unsupported_remainder: tuple[UnsupportedRemainder, ...] = ()


_COUNT_WORDS = {"a": 1, "one": 1, "two": 2, "three": 3}
_COLORS = frozenset({"white", "blue", "black", "red", "green", "colorless"})
_INTRINSIC_KEYWORDS = frozenset({
    "flying", "vigilance", "trample", "deathtouch", "lifelink", "haste",
    "reach", "menace", "defender", "first_strike", "double_strike",
    "hexproof", "indestructible",
})
_NAMED_NONCREATURE_TOKENS = {
    "treasure": "Treasure",
    "clue": "Clue",
    "food": "Food",
    "blood": "Blood",
    "map": "Map",
}
_SENTENCE_SPLIT = re.compile(r"(?<=\.)\s+")


def _status(has_supported: bool, unsupported: list[UnsupportedRemainder]) -> str:
    if not has_supported:
        return "unsupported"
    return "partial" if unsupported else "supported"


def _count(value: str) -> int | str:
    lowered = value.casefold()
    if lowered in _COUNT_WORDS:
        return _COUNT_WORDS[lowered]
    if value.isdigit():
        return int(value)
    return value.upper()


def _parse_trigger(text: str, *, card_name: str) -> Trigger | None:
    value = text.casefold()
    if value == "you gain life":
        return Trigger("life_gained", "you", "you", True, text)

    counter_subjects = {
        "one or more +1/+1 counters are put on a creature you control": (
            "creature_you_control", "you", True,
        ),
    }
    if card_name:
        counter_subjects[
            f"one or more +1/+1 counters are put on {card_name}".casefold()
        ] = ("self", "you", True)
    if value in counter_subjects:
        subject, controller, friendly = counter_subjects[value]
        return Trigger("counter_placed", subject, controller, friendly, text)

    spell_subjects = {
        "you cast an instant or sorcery spell": ("instant_or_sorcery", "you", True),
        "an opponent casts an instant or sorcery spell": (
            "instant_or_sorcery", "opponent", False,
        ),
        "an opponent cast an instant or sorcery spell": (
            "instant_or_sorcery", "opponent", False,
        ),
    }
    if value in spell_subjects:
        subject, controller, friendly = spell_subjects[value]
        return Trigger("spell_cast", subject, controller, friendly, text)

    token_subjects = {
        "a creature token enters the battlefield under your control": (
            "creature_token", "you", True,
        ),
        "one or more tokens you control enter": ("tokens", "you", True),
        "a token an opponent controls enters": ("token", "opponent", False),
    }
    if value in token_subjects:
        subject, controller, friendly = token_subjects[value]
        return Trigger("token_enters", subject, controller, friendly, text)

    death_subjects = {
        "one or more other creatures die": ("other_creatures", "any", None),
        "one or more creatures die": ("creatures", "any", None),
        "a creature dies": ("creature", "any", None),
        "a creature you control dies": ("creature", "you", True),
        "another creature you control dies": ("another_creature", "you", True),
        "a creature an opponent controls dies": ("creature", "opponent", False),
    }
    if value in death_subjects:
        subject, controller, friendly = death_subjects[value]
        return Trigger("creature_dies", subject, controller, friendly, text)
    return None


def _keyword_name(text: str) -> str:
    return text.casefold().replace(" ", "_")


def _parse_keyword_list(text: str) -> tuple[str, ...] | None:
    parts = tuple(
        _keyword_name(part.strip())
        for part in re.split(r", | and ", text)
        if part.strip()
    )
    if not parts or any(part not in _INTRINSIC_KEYWORDS for part in parts):
        return None
    return parts


def _token_spec(text: str) -> tuple[TokenSpec, str] | None:
    named = re.fullmatch(
        r"(?P<count>a|one|two|three|\d+|X) "
        r"(?P<name>Treasure|Clue|Food|Blood|Map) tokens?",
        text,
        re.I,
    )
    if named:
        name = _NAMED_NONCREATURE_TOKENS[named.group("name").casefold()]
        return TokenSpec(
            quantity=_count(named.group("count")),
            kind="named_noncreature",
            power=None,
            toughness=None,
            colors=(),
            subtypes=(),
            artifact=False,
            name=name,
        ), named.group(0)

    match = re.fullmatch(
        r"(?P<count>a|one|two|three|\d+|X) "
        r"(?P<power>\d+)/(?P<toughness>\d+) "
        r"(?P<description>.+?) creature tokens?"
        r"(?: with (?P<keywords>.+))?",
        text,
        re.I,
    )
    if not match:
        return None
    keywords = ()
    if match.group("keywords"):
        parsed_keywords = _parse_keyword_list(match.group("keywords"))
        if parsed_keywords is None:
            return None
        keywords = parsed_keywords
    words = match.group("description").split()
    colors = tuple(word.casefold() for word in words if word.casefold() in _COLORS)
    artifact = any(word.casefold() == "artifact" for word in words)
    subtypes = tuple(
        word for word in words
        if word.casefold() not in _COLORS and word.casefold() != "artifact"
    )
    return TokenSpec(
        quantity=_count(match.group("count")),
        kind="creature",
        power=int(match.group("power")),
        toughness=int(match.group("toughness")),
        colors=colors,
        subtypes=subtypes,
        artifact=artifact,
        keywords=keywords,
    ), match.group(0)


def _parse_effects(
    text: str, *, ability_id: str, card_name: str
) -> tuple[list[Effect], list[Qualifier], list[UnsupportedRemainder]]:
    effects: list[Effect] = []
    qualifiers: list[Qualifier] = []
    unsupported: list[UnsupportedRemainder] = []

    def append_token_effect(sentence: str) -> bool:
        token_controller = None
        token_text = None
        friendly = None
        imperative = re.fullmatch(r"Create (?P<token>.+)\.", sentence, re.I)
        opponent = re.fullmatch(
            r"Target opponent creates (?P<token>.+)\.", sentence, re.I
        )
        if imperative:
            token_controller, friendly = "you", True
            token_text = imperative.group("token")
        elif opponent:
            token_controller, friendly = "opponent", False
            token_text = opponent.group("token")
        if token_text is None:
            return False
        parsed_token = _token_spec(token_text)
        if parsed_token:
            token, _ = parsed_token
            effects.append(Effect(
                f"{ability_id}.effect.{len(effects) + 1:03d}",
                (
                    "create_creature_token"
                    if token.kind == "creature"
                    else "create_noncreature_token"
                ),
                sentence,
                amount=token.quantity,
                controller=token_controller,
                friendly=friendly,
                token=token,
            ))
            return True
        unsupported.append(UnsupportedRemainder(
            sentence,
            "effect",
            (
                "unsupported_noncreature_token"
                if re.search(r"\b(?:Treasure|Food|Clue|Blood|Map) tokens?\b", token_text, re.I)
                else "unsupported_token"
            ),
        ))
        return True

    for sentence in (part.strip() for part in _SENTENCE_SPLIT.split(text) if part.strip()):
        if sentence.casefold() == "this ability triggers only once each turn.":
            qualifiers.append(Qualifier("once_each_turn", "one", sentence))
            continue

        conditional_draw = re.fullmatch(
            r"If the sacrificed creature was (?P<property>[A-Za-z]+), "
            r"draw (?P<count>a|one|two|three) cards?(?P<instead> instead)?\.",
            sentence,
            re.I,
        )
        if conditional_draw:
            condition = Condition(
                "sacrificed_object_property",
                "sacrificed_creature",
                conditional_draw.group("property").casefold(),
                sentence[:sentence.index(",")],
            )
            effect_qualifiers = ()
            if conditional_draw.group("instead"):
                effect_qualifiers = (Qualifier("instead", "replace_base_effect", "instead"),)
            effects.append(Effect(
                f"{ability_id}.effect.{len(effects) + 1:03d}",
                "draw",
                sentence,
                amount=_count(conditional_draw.group("count")),
                conditions=(condition,),
                qualifiers=effect_qualifiers,
            ))
            continue

        draw = re.fullmatch(
            r"Draw (?P<count>a|one|two|three) cards?(?P<tail> and .+)?\.",
            sentence,
            re.I,
        )
        if draw:
            evidence = sentence if not draw.group("tail") else sentence[:draw.start("tail")] + "."
            effects.append(Effect(
                f"{ability_id}.effect.{len(effects) + 1:03d}",
                "draw",
                evidence,
                amount=_count(draw.group("count")),
            ))
            if draw.group("tail"):
                remainder = draw.group("tail")[5:] + "."
                if not append_token_effect(remainder):
                    unsupported.append(UnsupportedRemainder(
                        remainder, "effect", "unsupported_clause"
                    ))
            continue

        gain_life = re.fullmatch(
            r"(?:(?P<you>You) gain|(?P<opponent>Target opponent) gains) "
            r"(?P<count>one|two|three|\d+|X) life\.",
            sentence,
            re.I,
        )
        if gain_life:
            friendly = gain_life.group("you") is not None
            effects.append(Effect(
                f"{ability_id}.effect.{len(effects) + 1:03d}",
                "gain_life",
                sentence,
                amount=_count(gain_life.group("count")),
                controller="you" if friendly else "target_opponent",
                friendly=friendly,
            ))
            continue

        put_counter = re.fullmatch(
            r"Put (?P<count>a|one|two|three|\d+) \+1/\+1 counters? on "
            r"(?P<target>.+)\.",
            sentence,
            re.I,
        )
        if put_counter:
            target_text = put_counter.group("target")
            targets = {
                "this creature": "self",
                "target creature": "target_creature",
                "target creature you control": "target_creature_you_control",
                "another target creature": "another_target_creature",
                "another target creature you control": (
                    "another_target_creature_you_control"
                ),
            }
            if card_name:
                targets[card_name.casefold()] = "self"
            target = targets.get(target_text.casefold())
            if target is not None:
                effects.append(Effect(
                    f"{ability_id}.effect.{len(effects) + 1:03d}",
                    "put_counter",
                    sentence,
                    amount=_count(put_counter.group("count")),
                    target=target,
                    counter_type="+1/+1",
                ))
                continue

        damage_subjects = ["This spell"]
        if card_name:
            damage_subjects.insert(0, re.escape(card_name))
        damage = re.fullmatch(
            rf"(?:{'|'.join(damage_subjects)}) deals (?P<amount>\d+) damage to "
            r"(?P<target>any target|target creature|target opponent|each opponent)\.",
            sentence,
            re.I,
        )
        if damage:
            target = damage.group("target").casefold().replace(" ", "_")
            effects.append(Effect(
                f"{ability_id}.effect.{len(effects) + 1:03d}",
                "damage",
                sentence,
                amount=int(damage.group("amount")),
                target=target,
            ))
            continue

        if append_token_effect(sentence):
            continue

        unsupported.append(UnsupportedRemainder(sentence, "effect", "unsupported_clause"))
    return effects, qualifiers, unsupported


def _parse_sacrifice_cost(text: str, *, timing: str) -> Cost | None:
    subjects = {
        "sacrifice this creature": "self_creature",
        "sacrifice another creature": "another_creature",
        "sacrifice a creature": "creature",
        "sacrifice an artifact or creature": "artifact_or_creature",
    }
    subject = subjects.get(text.strip().casefold())
    return Cost("sacrifice", subject, timing, text.strip()) if subject else None


def _ability(
    ability_id: str,
    source_index: int,
    raw_text: str,
    kind: str,
    *,
    trigger: Trigger | None = None,
    costs: list[Cost] | None = None,
    effects: list[Effect] | None = None,
    qualifiers: list[Qualifier] | None = None,
    keywords: list[Keyword] | None = None,
    unsupported: list[UnsupportedRemainder] | None = None,
) -> Ability:
    costs = costs or []
    effects = effects or []
    qualifiers = qualifiers or []
    keywords = keywords or []
    unsupported = unsupported or []
    has_supported = bool(trigger or costs or effects or qualifiers or keywords)
    return Ability(
        ability_id=ability_id,
        source_index=source_index,
        # Preserve a recognized syntactic shape even when none of its semantic
        # components are currently understood.  parse_status carries that
        # distinction for coverage reporting.
        kind=kind,
        raw_text=raw_text,
        parse_status=_status(has_supported, unsupported),
        trigger=trigger,
        costs=tuple(costs),
        effects=tuple(effects),
        qualifiers=tuple(qualifiers),
        keywords=tuple(keywords),
        unsupported_remainder=tuple(unsupported),
    )


def _parse_line(
    raw_text: str,
    *,
    ability_id: str,
    source_index: int,
    card_name: str,
    card_types: frozenset[str],
) -> Ability:
    permanent = bool(card_types & {
        "Creature", "Artifact", "Enchantment", "Land", "Planeswalker",
    })
    intrinsic = _keyword_name(raw_text)
    if permanent and intrinsic in _INTRINSIC_KEYWORDS:
        return _ability(
            ability_id,
            source_index,
            raw_text,
            "static_keyword",
            keywords=[Keyword(intrinsic, "self", raw_text)],
        )

    ward = re.fullmatch(r"Ward (?P<cost>(?:\{[0-9WUBRGCXYZ/]+\})+)", raw_text, re.I)
    if permanent and ward:
        return _ability(
            ability_id,
            source_index,
            raw_text,
            "static_keyword",
            keywords=[Keyword("ward", "self", raw_text, ward.group("cost"))],
        )

    additional = re.fullmatch(
        r"As an additional cost to cast this spell, (?P<cost>sacrifice .+)\.",
        raw_text,
        re.I,
    )
    if additional:
        cost = _parse_sacrifice_cost(additional.group("cost"), timing="additional_spell")
        unsupported = [] if cost else [
            UnsupportedRemainder(additional.group("cost"), "cost", "unsupported_cost")
        ]
        return _ability(
            ability_id, source_index, raw_text, "spell_effect",
            costs=[cost] if cost else [], unsupported=unsupported,
        )

    triggered = re.fullmatch(r"(?:When|Whenever) (?P<trigger>.+?), (?P<effect>.+)", raw_text, re.I)
    if triggered:
        trigger = _parse_trigger(triggered.group("trigger"), card_name=card_name)
        if trigger is None:
            return _ability(
                ability_id, source_index, raw_text, "unsupported",
                unsupported=[UnsupportedRemainder(raw_text, "trigger", "unsupported_trigger")],
            )
        effects, qualifiers, unsupported = _parse_effects(
            triggered.group("effect"), ability_id=ability_id, card_name=card_name
        )
        return _ability(
            ability_id, source_index, raw_text, "triggered",
            trigger=trigger, effects=effects, qualifiers=qualifiers,
            unsupported=unsupported,
        )

    if ":" in raw_text:
        cost_text, effect_text = raw_text.split(":", 1)
        costs: list[Cost] = []
        unsupported: list[UnsupportedRemainder] = []
        for part in (piece.strip() for piece in cost_text.split(",") if piece.strip()):
            cost = _parse_sacrifice_cost(part, timing="activated")
            if cost:
                costs.append(cost)
            else:
                unsupported.append(UnsupportedRemainder(part, "cost", "unsupported_cost"))
        effects, qualifiers, effect_unsupported = _parse_effects(
            effect_text.strip(), ability_id=ability_id, card_name=card_name
        )
        return _ability(
            ability_id, source_index, raw_text, "activated",
            costs=costs, effects=effects, qualifiers=qualifiers,
            unsupported=[*unsupported, *effect_unsupported],
        )

    effects, qualifiers, unsupported = _parse_effects(
        raw_text, ability_id=ability_id, card_name=card_name
    )
    kind = "spell_effect" if card_types & {"Instant", "Sorcery"} else "unsupported"
    return _ability(
        ability_id, source_index, raw_text, kind,
        effects=effects, qualifiers=qualifiers, unsupported=unsupported,
    )


def decompose_abilities(
    rules_text: str,
    *,
    card_name: str = "",
    card_types: str = "",
    canonical_anomaly: bool = False,
) -> tuple[Ability, ...]:
    """Return source-ordered ability records for one canonical card text."""
    lines = tuple(line.strip() for line in rules_text.splitlines() if line.strip())
    types = frozenset(card_types.split())
    result = []
    unsupported_modal = any(
        re.match(r"^Choose (?:one|two|one or more)\b", line, re.I) for line in lines
    )
    for index, line in enumerate(lines, start=1):
        ability_id = f"ability.{index:03d}"
        if canonical_anomaly or unsupported_modal:
            reason = "canonical_anomaly" if canonical_anomaly else "unsupported_modal"
            status = "anomalous" if canonical_anomaly else "unsupported"
            result.append(Ability(
                ability_id=ability_id,
                source_index=index,
                kind="unsupported",
                raw_text=line,
                parse_status=status,
                unsupported_remainder=(UnsupportedRemainder(
                    line, "ability", reason
                ),),
            ))
            continue
        result.append(_parse_line(
            line,
            ability_id=ability_id,
            source_index=index,
            card_name=card_name,
            card_types=types,
        ))
    return tuple(result)
