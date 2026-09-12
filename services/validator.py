"""Deterministic deck validation.

No model or heuristic participates here. Every error is tied to a concrete
deck, format, collection, or inventory rule and can be reproduced offline.
"""

from __future__ import annotations

import collections
import re
import sqlite3
from dataclasses import dataclass, field

from mtgadb.model import Collection, Deck, Format, Inventory
from mtgadb.modes import OperatingMode


_BASIC_LAND_NAMES = frozenset({
    "Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes",
    "Snow-Covered Plains", "Snow-Covered Island", "Snow-Covered Swamp",
    "Snow-Covered Mountain", "Snow-Covered Forest",
})
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


@dataclass(frozen=True)
class DeckRules:
    min_main: int = 60
    max_main: int = 250
    max_sideboard: int = 15
    min_commanders: int = 0
    max_commanders: int = 0
    copy_limit: int = 4
    allowed_colors: frozenset[str] | None = None

    @classmethod
    def from_format(
        cls, format: Format, *, allowed_colors: frozenset[str] | None = None
    ) -> "DeckRules":
        return cls(
            min_main=format.min_deck_size,
            max_main=format.max_deck_size,
            max_sideboard=format.max_sideboard,
            min_commanders=format.min_command_zone,
            max_commanders=format.max_command_zone,
            allowed_colors=allowed_colors,
        )


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    zone: str | None = None
    title_id: int | None = None


@dataclass(frozen=True)
class ValidationReport:
    errors: tuple[ValidationIssue, ...] = field(default_factory=tuple)
    warnings: tuple[ValidationIssue, ...] = field(default_factory=tuple)
    wildcard_cost: dict[str, int] = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return not self.errors


def _printing_rows(con: sqlite3.Connection, deck: Deck) -> dict[int, sqlite3.Row]:
    ids = set(deck.main) | set(deck.sideboard) | set(deck.commander)
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = con.execute(
        f"SELECT p.arena_id, p.title_id, p.set_code, p.rarity, "
        f"c.name, c.types, c.color_identity, c.rules_text FROM printings p "
        f"JOIN cards c ON c.title_id = p.title_id "
        f"WHERE p.arena_id IN ({marks})",
        tuple(ids),
    ).fetchall()
    return {r["arena_id"]: r for r in rows}


def _is_basic_land(row: sqlite3.Row) -> bool:
    """Arena's card projection omits the Basic supertype."""
    return row["name"] in _BASIC_LAND_NAMES


def _copy_limit(row: sqlite3.Row, default: int) -> int | None:
    if _is_basic_land(row):
        return None
    text = row["rules_text"] or ""
    if re.search(r"deck can have any number of cards named", text, re.I):
        return None
    match = re.search(r"deck can have up to (\w+) cards named", text, re.I)
    if match:
        token = match.group(1).casefold()
        if token.isdigit():
            return int(token)
        if token in _NUMBER_WORDS:
            return _NUMBER_WORDS[token]
    return default


def validate_deck(
    deck: Deck,
    con: sqlite3.Connection,
    *,
    mode: OperatingMode = OperatingMode.UNLIMITED,
    rules: DeckRules | None = None,
    format: Format | None = None,
    collection: Collection | None = None,
    inventory: Inventory | None = None,
) -> ValidationReport:
    """Validate structure, copies, legality, ownership, and craft budget."""
    rules = rules or (DeckRules.from_format(format) if format else DeckRules())
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []
    rows = _printing_rows(con, deck)

    for zone_name, zone in (
        ("main", deck.main), ("sideboard", deck.sideboard),
        ("commander", deck.commander),
    ):
        for arena_id, quantity in zone.items():
            if quantity <= 0:
                errors.append(ValidationIssue(
                    "invalid_quantity", f"Printing {arena_id} has quantity {quantity}.", zone_name
                ))
            if arena_id not in rows:
                errors.append(ValidationIssue(
                    "unknown_printing", f"Unknown Arena printing id: {arena_id}.", zone_name
                ))

    sizes = {
        "main": sum(deck.main.values()),
        "sideboard": sum(deck.sideboard.values()),
        "commander": sum(deck.commander.values()),
    }
    if not rules.min_main <= sizes["main"] <= rules.max_main:
        errors.append(ValidationIssue(
            "main_size",
            f"Main deck has {sizes['main']} cards; expected {rules.min_main}-{rules.max_main}.",
            "main",
        ))
    if sizes["sideboard"] > rules.max_sideboard:
        errors.append(ValidationIssue(
            "sideboard_size",
            f"Sideboard has {sizes['sideboard']} cards; maximum is {rules.max_sideboard}.",
            "sideboard",
        ))
    if not rules.min_commanders <= sizes["commander"] <= rules.max_commanders:
        errors.append(ValidationIssue(
            "commander_size",
            f"Command zone has {sizes['commander']} cards; expected "
            f"{rules.min_commanders}-{rules.max_commanders}.",
            "commander",
        ))

    by_title: collections.Counter[int] = collections.Counter()
    for zone in (deck.main, deck.sideboard, deck.commander):
        for arena_id, quantity in zone.items():
            if arena_id in rows and quantity > 0:
                by_title[rows[arena_id]["title_id"]] += quantity

    for title_id, quantity in by_title.items():
        row = next(r for r in rows.values() if r["title_id"] == title_id)
        limit = _copy_limit(row, rules.copy_limit)
        format_limit = format.individual_card_quotas.get(title_id) if format else None
        if format_limit is not None:
            limit = format_limit if limit is None else min(limit, format_limit)
        if limit is not None and quantity > limit:
            errors.append(ValidationIssue(
                "copy_limit",
                f"{row['name']} has {quantity} copies; maximum is {limit}.",
                title_id=title_id,
            ))

        if rules.allowed_colors is not None:
            identity = set(row["color_identity"] or "")
            if not identity <= set(rules.allowed_colors):
                errors.append(ValidationIssue(
                    "color_identity",
                    f"{row['name']} has color identity {row['color_identity'] or 'colorless'}, "
                    f"outside {''.join(sorted(rules.allowed_colors)) or 'colorless'}.",
                    title_id=title_id,
                ))

        if format:
            if title_id in format.banned_title_ids or title_id in format.suppressed_title_ids:
                errors.append(ValidationIssue(
                    "format_banned", f"{row['name']} is banned or suppressed in {format.name}.",
                    title_id=title_id,
                ))
            if title_id in format.suspended_title_ids:
                errors.append(ValidationIssue(
                    "format_suspended", f"{row['name']} is suspended in {format.name}.",
                    title_id=title_id,
                ))
            if format.legal_sets:
                legal_printing = con.execute(
                    "SELECT 1 FROM printings WHERE title_id = ? AND set_code IN ("
                    + ",".join("?" for _ in format.legal_sets) + ") LIMIT 1",
                    (title_id, *format.legal_sets),
                ).fetchone()
                explicitly_allowed = (
                    format.allowed_title_ids is not None
                    and title_id in format.allowed_title_ids
                )
                if not legal_printing and not explicitly_allowed:
                    errors.append(ValidationIssue(
                        "format_illegal_set",
                        f"{row['name']} has no printing legal in {format.name}.",
                        title_id=title_id,
                    ))

    if format and format.allowed_commander_title_ids is not None:
        for arena_id in deck.commander:
            if arena_id not in rows:
                continue
            title_id = rows[arena_id]["title_id"]
            if title_id not in format.allowed_commander_title_ids:
                errors.append(ValidationIssue(
                    "commander_not_allowed",
                    f"{rows[arena_id]['name']} is not an allowed commander in {format.name}.",
                    "commander", title_id,
                ))

    owned = collection.cards if collection else {}
    required_by_printing: collections.Counter[int] = collections.Counter()
    for zone in (deck.main, deck.sideboard, deck.commander):
        required_by_printing.update({a: q for a, q in zone.items() if q > 0})
    shortages = {
        arena_id: max(0, required - owned.get(arena_id, 0))
        for arena_id, required in required_by_printing.items()
        if arena_id not in rows or not _is_basic_land(rows[arena_id])
    }

    wildcard_cost: collections.Counter[str] = collections.Counter()
    if mode is OperatingMode.FULL_COLLECTION:
        if collection is None:
            errors.append(ValidationIssue(
                "collection_required", "Full-collection mode requires collection data."
            ))
        else:
            for arena_id, shortage in shortages.items():
                if shortage and arena_id in rows:
                    errors.append(ValidationIssue(
                        "not_owned",
                        f"Need {shortage} more copie(s) of {rows[arena_id]['name']}.",
                        title_id=rows[arena_id]["title_id"],
                    ))
    elif mode is OperatingMode.WILDCARD_BUDGET:
        if inventory is None:
            errors.append(ValidationIssue(
                "inventory_required", "Wildcard-budget mode requires wildcard inventory."
            ))
        else:
            for arena_id, shortage in shortages.items():
                if shortage and arena_id in rows:
                    wildcard_cost[rows[arena_id]["rarity"] or "unknown"] += shortage
            for rarity, needed in wildcard_cost.items():
                available = inventory.wildcards.get(rarity, 0)
                if needed > available:
                    errors.append(ValidationIssue(
                        "wildcard_shortage",
                        f"Need {needed} {rarity} wildcards; only {available} available.",
                    ))

    return ValidationReport(tuple(errors), tuple(warnings), dict(wildcard_cost))
