"""Card definitions from Arena's own SQLite database.

Arena ships a complete card database with the client, so no Scryfall call is
needed: no HTTP, no rate limit, and the contents are already scoped to exactly
the Arena-legal pool.

Everything in the Cards table is an id -- names and type words live in
Localizations_enUS, type ids in Enums, rules text via Cards.AbilityIds -- so
this module's real job is resolving those joins and reconciling the fact that
printings of the same card can disagree about their own rules text.
"""

from __future__ import annotations

import collections
import hashlib
import sqlite3
import urllib.request
from pathlib import Path
from typing import Iterable, Iterator

from mtgadb.model import Card, CardPrinting, Resolution
from mtgadb.text import (
    cmc,
    mana_cost_string,
    normalize_colors,
    normalize_text,
    parse_mana,
    self_reference_names,
)

# The filename hash changes with each set release, so always glob.
_INSTALL_ROOTS = [
    Path(f"{d}:/Program Files/Wizards of the Coast/MTGA") for d in "CDEFG"
] + [
    Path(f"{d}:/Program Files (x86)/Steam/steamapps/common/MTGA") for d in "CDEFG"
]
_DB_GLOB = "MTGA_Data/Downloads/Raw/Raw_CardDatabase_*.mtga"

RARITY = {1: "basic", 2: "common", 3: "uncommon", 4: "rare", 5: "mythic"}

# Localizations_enUS.Formatted tags text variants. The main English strings --
# names, type words and rules text alike -- are at 1. Formatted=0 holds ~924
# rows that are almost entirely Alchemy "A-" rebalances, and is a tempting
# false positive: joining on it silently yields only rebalanced cards.
_EN_TEXT = 1


def find_card_db() -> Path | None:
    """Locate the newest card database shipped with the client."""
    hits = [p for root in _INSTALL_ROOTS for p in root.glob(_DB_GLOB)]
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


class ArenaSQLiteCardProvider:
    """Implements CardDatabaseProvider against Arena's shipped database."""

    def __init__(self, path: Path | None = None):
        self.path = path or find_card_db()
        if self.path is None:
            raise FileNotFoundError("No Arena card database found. Is MTGA installed?")
        uri = "file:" + urllib.request.pathname2url(str(self.path)) + "?mode=ro"
        self.con = sqlite3.connect(uri, uri=True)
        self.con.row_factory = sqlite3.Row
        self._loc: dict[int, str] | None = None
        self._types: dict[int, str] | None = None
        self._subtypes: dict[int, str] | None = None

    # ------------------------------------------------------------ lookups

    def _localizations(self) -> dict[int, str]:
        if self._loc is None:
            q = "select LocId, Loc from Localizations_enUS where Formatted = ?"
            self._loc = {r[0]: r[1] for r in self.con.execute(q, (_EN_TEXT,))}
        return self._loc

    def _enum(self, kind: str) -> dict[int, str]:
        loc = self._localizations()
        q = "select Value, LocId from Enums where Type = ?"
        return {v: loc.get(l, "") for v, l in self.con.execute(q, (kind,))}

    def _card_types(self) -> dict[int, str]:
        if self._types is None:
            self._types = self._enum("CardType")
        return self._types

    def _card_subtypes(self) -> dict[int, str]:
        if self._subtypes is None:
            self._subtypes = self._enum("SubType")
        return self._subtypes

    def fingerprint(self) -> str:
        """Identify this exact database, so the projection is rebuilt only
        when Arena actually shipped new content."""
        st = self.path.stat()
        raw = f"{self.path.name}:{st.st_size}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    # -------------------------------------------------------------- rows

    _SELECT = """
        select c.GrpId, c.TitleId, c.ExpansionCode, c.CollectorNumber,
               c.Rarity, c.AbilityIds, c.OldSchoolManaText, c.Types,
               c.Subtypes, c.Power, c.Toughness, c.Colors, c.ColorIdentity
        from Cards c
        where c.IsPrimaryCard = 1 and c.IsToken = 0
    """

    def _rules_text(self, ability_ids: str | None) -> str:
        """Cards.AbilityIds is 'abilityId:textId,...'; the text ids resolve
        through the localization table. Verified against Sheoldred."""
        loc = self._localizations()
        parts = []
        for pair in (ability_ids or "").split(","):
            if ":" not in pair:
                continue
            text = loc.get(int(pair.split(":")[1]), "")
            if text:
                parts.append(normalize_text(text))
        return "\n".join(parts)

    def iter_printings(self) -> Iterator[CardPrinting]:
        for r in self.con.execute(self._SELECT):
            yield CardPrinting(
                arena_id=r["GrpId"],
                title_id=r["TitleId"],
                set_code=r["ExpansionCode"] or "",
                collector_number=str(r["CollectorNumber"] or ""),
                rarity=RARITY.get(r["Rarity"], str(r["Rarity"])),
                raw_rules_text=self._rules_text(r["AbilityIds"]),
            )

    # -------------------------------------------------- canonical resolve

    @staticmethod
    def _resolve_text(
        name: str, candidates: list[tuple[int, str]]
    ) -> tuple[str, int, Resolution, int]:
        """Pick one card's rules text from its printings.

        Returns (text, source_printing_id, resolution, anomaly_count).

        Measured on the shipped database: of 15,813 titles, 1,951 have several
        printings and only 20 disagree -- but 15 of those 20 are exact ties, so
        majority alone settles a minority of cases. Universes Beyond reskins
        are the dominant cause (Lightning Bolt / "Thrum of the Vestige").

        Order is majority, then self-reference, then give up and flag. Not
        "newest printing": recent sets deliberately change wording, so recency
        is anti-correlated with correctness.
        """
        if len(candidates) == 1:
            return candidates[0][1], candidates[0][0], Resolution.SINGLE, 0

        counts = collections.Counter(text for _, text in candidates)
        if len(counts) == 1:
            return candidates[0][1], candidates[0][0], Resolution.SINGLE, 0

        ranked = counts.most_common()
        anomalies = len(candidates) - ranked[0][1]

        # Clear majority.
        if len(ranked) == 1 or ranked[0][1] > ranked[1][1]:
            best = ranked[0][0]
            pid = next(p for p, t in candidates if t == best)
            return best, pid, Resolution.MAJORITY, anomalies

        # Tie: prefer text that names the card, which rejects reskins whose
        # text names a different character entirely. Try the most specific
        # self-reference form first so a first-word match cannot outrank a
        # full-name one.
        for needle in self_reference_names(name):
            for pid, text in candidates:
                if needle and needle in text:
                    others = sum(1 for _, t in candidates if t != text)
                    return text, pid, Resolution.SELF_REFERENCE, others

        pid, text = candidates[0]
        return text, pid, Resolution.ANOMALY, anomalies

    def iter_cards(self) -> Iterator[Card]:
        loc = self._localizations()
        types_by_id = self._card_types()
        subtypes_by_id = self._card_subtypes()

        grouped: dict[int, list[sqlite3.Row]] = collections.defaultdict(list)
        for r in self.con.execute(self._SELECT):
            grouped[r["TitleId"]].append(r)

        for title_id, rows in grouped.items():
            name = normalize_text(loc.get(title_id, ""))
            if not name:
                continue

            candidates = [
                (r["GrpId"], self._rules_text(r["AbilityIds"])) for r in rows
            ]
            text, pid, resolution, anomalies = self._resolve_text(name, candidates)

            # Non-text attributes come from the printing that supplied the
            # text, so a card's fields are internally consistent.
            src = next((r for r in rows if r["GrpId"] == pid), rows[0])
            symbols = parse_mana(src["OldSchoolManaText"])

            def _names(raw: str | None, table: dict[int, str]) -> str:
                ids = [int(t) for t in (raw or "").split(",") if t.strip()]
                return " ".join(filter(None, (table.get(i, "") for i in ids)))

            yield Card(
                title_id=title_id,
                name=name,
                mana_cost=mana_cost_string(symbols),
                cmc=cmc(symbols),
                types=_names(src["Types"], types_by_id),
                subtypes=_names(src["Subtypes"], subtypes_by_id),
                colors=normalize_colors(src["Colors"]),
                color_identity=normalize_colors(src["ColorIdentity"]),
                power=src["Power"] or "",
                toughness=src["Toughness"] or "",
                rules_text=text,
                source_printing_id=pid,
                resolution=resolution,
                anomaly_count=anomalies,
            )
