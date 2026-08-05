"""Read Arena's local card database.

Arena ships a full SQLite card database with the client, so we don't need
Scryfall: no HTTP, no rate limit, no bulk download, and the contents are
already scoped to exactly the Arena-legal card pool.

Everything in the Cards table is stored as an id -- names and type words live
in Localizations_enUS, type/subtype ids in Enums -- so this module's job is
mostly resolving those joins into something usable.

Stdlib only. Usage:
    python mtga_cards.py --stats
    python mtga_cards.py --name "Lightning Bolt"
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path

# The client ships the db under its install; the hash in the filename changes
# with each set release, so always glob rather than hard-code.
_INSTALL_ROOTS = [
    Path(f"{d}:/Program Files/Wizards of the Coast/MTGA") for d in "CDEFG"
] + [
    Path(f"{d}:/Program Files (x86)/Steam/steamapps/common/MTGA") for d in "CDEFG"
]
_DB_GLOB = "MTGA_Data/Downloads/Raw/Raw_CardDatabase_*.mtga"

# Rarity is an int in the db; 1 is used for basic lands and tokens.
RARITY = {1: "basic", 2: "common", 3: "uncommon", 4: "rare", 5: "mythic"}

# Localizations_enUS.Formatted tags text variants. The main English strings
# -- card names and type words alike -- are at 1; 0 and 2 hold small sets of
# variant text (0 is almost entirely Alchemy "A-" rebalances). Verified by
# row counts, not documented anywhere.
_EN_TEXT = 1


def find_card_db() -> Path | None:
    """Locate the newest card database shipped with the client."""
    hits = [p for root in _INSTALL_ROOTS for p in root.glob(_DB_GLOB)]
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def _connect(path: Path) -> sqlite3.Connection:
    """Open the db read-only, without touching the user's install."""
    uri = "file:" + urllib.request.pathname2url(str(path)) + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


# ------------------------------------------------------------------- mana

_MANA_TOKEN = re.compile(r"o(\([^)]*\)|\d+|[A-Za-z])")


def parse_mana(old_school: str) -> list[str]:
    """Turn Arena's 'o2o(G/W)oU' into ['2', 'G/W', 'U']."""
    return [t.strip("()") for t in _MANA_TOKEN.findall(old_school or "")]


def mana_cost_string(symbols: list[str]) -> str:
    """Render symbols the way Magic writes them: {2}{G/W}{U}."""
    return "".join("{" + s + "}" for s in symbols)


def cmc(symbols: list[str]) -> int:
    """Converted mana cost. X counts 0; hybrid takes its larger half."""
    total = 0
    for s in symbols:
        if s.isdigit():
            total += int(s)
        elif "/" in s:
            # {2/R} costs 2, {G/W} costs 1, {B/P} (phyrexian) costs 1.
            parts = [p for p in s.split("/") if p.isdigit()]
            total += int(parts[0]) if parts else 1
        elif s.upper() == "X":
            continue
        else:
            total += 1
    return total


# ------------------------------------------------------------------ cards


@dataclass
class Card:
    """One Arena card, with ids already resolved to text."""

    grp_id: int
    name: str
    mana_cost: str
    cmc: int
    types: str
    rarity: str
    set_code: str
    power: str = ""
    toughness: str = ""
    colors: str = ""
    color_identity: str = ""

    def __str__(self) -> str:
        pt = f" {self.power}/{self.toughness}" if self.power else ""
        return (
            f"{self.name} {self.mana_cost} - {self.types}{pt} "
            f"[{self.rarity} {self.set_code}]"
        )


class CardDB:
    """Query interface over Arena's shipped card database."""

    def __init__(self, path: Path | None = None):
        self.path = path or find_card_db()
        if self.path is None:
            raise FileNotFoundError(
                "No Arena card database found. Is MTGA installed?"
            )
        self.con = _connect(self.path)
        self._types = self._load_enum("CardType")

    def _load_enum(self, kind: str) -> dict[int, str]:
        q = """
            select e.Value, l.Loc
            from Enums e
            join Localizations_enUS l on l.LocId = e.LocId and l.Formatted = ?
            where e.Type = ?
        """
        return {r[0]: r[1] for r in self.con.execute(q, (_EN_TEXT, kind))}

    def _row_to_card(self, r: sqlite3.Row) -> Card:
        symbols = parse_mana(r["OldSchoolManaText"])
        type_ids = [int(t) for t in (r["Types"] or "").split(",") if t.strip()]
        return Card(
            grp_id=r["GrpId"],
            name=r["name"],
            mana_cost=mana_cost_string(symbols),
            cmc=cmc(symbols),
            types=" ".join(self._types.get(t, f"?{t}") for t in type_ids),
            rarity=RARITY.get(r["Rarity"], str(r["Rarity"])),
            set_code=r["ExpansionCode"] or "",
            power=r["Power"] or "",
            toughness=r["Toughness"] or "",
            colors=r["Colors"] or "",
            color_identity=r["ColorIdentity"] or "",
        )

    _SELECT = """
        select c.GrpId, l.Loc as name, c.OldSchoolManaText, c.Types, c.Rarity,
               c.ExpansionCode, c.Power, c.Toughness, c.Colors, c.ColorIdentity
        from Cards c
        join Localizations_enUS l on l.LocId = c.TitleId and l.Formatted = ?
        where c.IsPrimaryCard = 1 and c.IsToken = 0
    """

    def by_grp_id(self, grp_id: int) -> Card | None:
        """Look up the card a collection entry refers to."""
        r = self.con.execute(
            self._SELECT + " and c.GrpId = ?", (_EN_TEXT, grp_id)
        ).fetchone()
        return self._row_to_card(r) if r else None

    def by_name(self, name: str) -> list[Card]:
        """All printings matching a name, exact and case-insensitive."""
        rows = self.con.execute(
            self._SELECT + " and lower(l.Loc) = lower(?)", (_EN_TEXT, name)
        ).fetchall()
        return [self._row_to_card(r) for r in rows]

    def all_cards(self) -> list[Card]:
        rows = self.con.execute(self._SELECT, (_EN_TEXT,)).fetchall()
        return [self._row_to_card(r) for r in rows]

    def count(self) -> int:
        q = "select count(*) from Cards where IsPrimaryCard=1 and IsToken=0"
        return self.con.execute(q).fetchone()[0]


# -------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--name", help="look up a card by name")
    ap.add_argument("--grp-id", type=int, help="look up a card by Arena id")
    ap.add_argument("--stats", action="store_true", help="summarize the db")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args(argv)

    try:
        db = CardDB()
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 1

    if args.stats:
        cards = db.all_cards()
        print(f"database  {db.path.name}")
        print(f"cards     {len(cards):,} primary, non-token")
        by_rarity: dict[str, int] = {}
        for c in cards:
            by_rarity[c.rarity] = by_rarity.get(c.rarity, 0) + 1
        for k in ("basic", "common", "uncommon", "rare", "mythic"):
            if k in by_rarity:
                print(f"  {k:9} {by_rarity[k]:>6,}")
        sets = {c.set_code for c in cards}
        print(f"sets      {len(sets)}")
        return 0

    found: list[Card] = []
    if args.name:
        found = db.by_name(args.name)
    elif args.grp_id:
        c = db.by_grp_id(args.grp_id)
        found = [c] if c else []
    else:
        ap.print_help()
        return 0

    if not found:
        print("No match.", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([asdict(c) for c in found], indent=2))
    else:
        for c in found:
            print(f"  {c}  (grpId {c.grp_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
