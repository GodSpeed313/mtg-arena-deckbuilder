"""Parse and render MTG Arena's text deck format.

Import is intentionally strict about quantities and card names, but tolerant
of blank lines, localized section headings used by the English client, and
the optional ``(SET) collector`` suffix Arena includes on export.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from mtgadb.model import Deck


_CARD_LINE = re.compile(
    r"^\s*(?P<quantity>\d+)\s+(?P<name>.+?)"
    r"(?:\s+\((?P<set>[^()]+)\)\s+(?P<number>\S+))?\s*$"
)
_SECTIONS = {
    "deck": "main",
    "main": "main",
    "sideboard": "sideboard",
    "commander": "commander",
    "commanders": "commander",
}


@dataclass(frozen=True)
class ImportIssue:
    line: int
    code: str
    message: str
    text: str = ""


@dataclass(frozen=True)
class DeckImportResult:
    deck: Deck
    issues: tuple[ImportIssue, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.issues


def _resolve_printing(
    con: sqlite3.Connection,
    name: str,
    set_code: str | None,
    collector_number: str | None,
) -> tuple[int | None, str | None]:
    """Resolve an Arena line to one printing without fuzzy guessing."""
    rows = con.execute(
        "SELECT p.arena_id, p.set_code, p.collector_number, "
        "c.source_printing_id FROM cards c JOIN printings p "
        "ON p.title_id = c.title_id WHERE LOWER(c.name) = LOWER(?) "
        "ORDER BY p.arena_id",
        (name,),
    ).fetchall()
    if not rows:
        return None, "unknown_card"

    if set_code is not None:
        exact = [
            r for r in rows
            if (r[1] or "").casefold() == set_code.casefold()
            and str(r[2] or "").casefold() == str(collector_number or "").casefold()
        ]
        if not exact:
            return None, "unknown_printing"
        return exact[0][0], None

    preferred = next((r[0] for r in rows if r[0] == r[3]), None)
    return preferred if preferred is not None else rows[0][0], None


def import_arena_deck(
    text: str,
    con: sqlite3.Connection,
    *,
    name: str = "Imported Deck",
    deck_id: str = "",
) -> DeckImportResult:
    """Parse an Arena decklist and resolve every card against ``con``."""
    zones: dict[str, dict[int, int]] = {
        "main": {}, "sideboard": {}, "commander": {}
    }
    issues: list[ImportIssue] = []
    zone = "main"

    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip().lstrip("\ufeff")
        if not line:
            continue
        section = _SECTIONS.get(line.casefold())
        if section:
            zone = section
            continue

        match = _CARD_LINE.match(line)
        if not match or int(match.group("quantity")) <= 0:
            issues.append(ImportIssue(
                line_number, "invalid_line", "Expected a positive quantity and card name.", raw
            ))
            continue

        quantity = int(match.group("quantity"))
        card_name = match.group("name")
        arena_id, error = _resolve_printing(
            con, card_name, match.group("set"), match.group("number")
        )
        if error:
            detail = (
                f"Unknown card: {card_name}"
                if error == "unknown_card"
                else f"Card exists, but printing ({match.group('set')}) "
                     f"{match.group('number')} was not found: {card_name}"
            )
            issues.append(ImportIssue(line_number, error, detail, raw))
            continue
        assert arena_id is not None
        zones[zone][arena_id] = zones[zone].get(arena_id, 0) + quantity

    deck = Deck(
        deck_id=deck_id,
        name=name,
        main=zones["main"],
        sideboard=zones["sideboard"],
        commander=zones["commander"],
    )
    return DeckImportResult(deck, tuple(issues))


def export_arena_deck(deck: Deck, con: sqlite3.Connection) -> str:
    """Render a resolved deck in the format accepted by MTG Arena."""
    sections = (
        ("Deck", deck.main),
        ("Sideboard", deck.sideboard),
        ("Commander", deck.commander),
    )
    output: list[str] = []
    for heading, cards in sections:
        if not cards and heading != "Deck":
            continue
        if output:
            output.append("")
        output.append(heading)
        for arena_id, quantity in cards.items():
            row = con.execute(
                "SELECT c.name, p.set_code, p.collector_number "
                "FROM printings p JOIN cards c ON c.title_id = p.title_id "
                "WHERE p.arena_id = ?",
                (arena_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown Arena printing id: {arena_id}")
            suffix = f" ({row[1]}) {row[2]}" if row[1] and row[2] else ""
            output.append(f"{quantity} {row[0]}{suffix}")
    return "\n".join(output) + "\n"

