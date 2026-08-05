"""The card query engine.

Every feature eventually asks the same question in different clothes: "find
cards that...". Deck Builder, Draft Helper, Collection Tracker, Wildcard
Advisor and Meta Analyzer are all clients of this. Building it first is what
keeps the deck builder a thin consumer instead of the module that owns all the
logic -- which is exactly how the reference project went wrong.

This layer is deterministic and offline. It never calls an LLM, and it is the
component that guarantees a model is only ever shown cards that are legal and
(when asked) owned.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from mtgadb.model import Card, Resolution
from mtgadb.text import WUBRG, color_key

# Mapping from a predicate suffix to its SQL comparison.
_OPS = {
    "lte": "<=",
    "lt": "<",
    "gte": ">=",
    "gt": ">",
    "ne": "!=",
}


@dataclass(frozen=True)
class OwnedCard:
    """A card plus how many copies are owned, summed across printings."""

    card: Card
    owned: int


class CardQueryEngine:
    """Composable predicate search over the canonical database."""

    def __init__(self, con: sqlite3.Connection):
        self.con = con

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _row_to_card(r: sqlite3.Row) -> Card:
        return Card(
            title_id=r["title_id"],
            name=r["name"],
            mana_cost=r["mana_cost"] or "",
            cmc=r["cmc"] or 0,
            types=r["types"] or "",
            subtypes=r["subtypes"] or "",
            colors=r["colors"] or "",
            color_identity=r["color_identity"] or "",
            power=r["power"] or "",
            toughness=r["toughness"] or "",
            rules_text=r["rules_text"] or "",
            source_printing_id=r["source_printing_id"],
            resolution=Resolution(r["resolution"] or "single"),
            anomaly_count=r["anomaly_count"] or 0,
        )

    def _build_where(self, filters: dict[str, Any]) -> tuple[list[str], list[Any]]:
        where: list[str] = []
        params: list[Any] = []

        for key, value in filters.items():
            if value is None:
                continue
            field, _, suffix = key.partition("__")

            if suffix in _OPS:
                where.append(f"c.{field} {_OPS[suffix]} ?")
                params.append(value)
            elif suffix == "contains":
                where.append(f"LOWER(c.{field}) LIKE ?")
                params.append(f"%{str(value).lower()}%")
            elif suffix == "in":
                marks = ",".join("?" * len(value))
                where.append(f"c.{field} IN ({marks})")
                params.extend(value)
            else:
                where.append(f"c.{field} = ?")
                params.append(value)

        return where, params

    # -------------------------------------------------------------- colors

    @staticmethod
    def _color_clause(colors: list[str], mode: str) -> tuple[str, list[Any]]:
        """Colour identity is stored as a string like 'WU'.

        `exact`    identity is precisely these colours
        `subset`   identity fits inside these colours (deck-legal in them)
        `contains` identity includes all of these colours
        """
        wanted = color_key(colors)
        if mode == "exact":
            # color_identity is normalized to WUBRG order on load, so exact
            # match is plain equality -- no per-character gymnastics in SQL.
            return "c.color_identity = ?", [wanted]
        if mode == "contains":
            return (
                " AND ".join(["c.color_identity LIKE ?"] * len(colors)),
                [f"%{c.upper()}%" for c in colors],
            )
        # subset: every colour present must be one of the requested ones.
        excluded = [c for c in WUBRG if c not in wanted]
        if not excluded:
            return "1=1", []
        return (
            " AND ".join(["c.color_identity NOT LIKE ?"] * len(excluded)),
            [f"%{c}%" for c in excluded],
        )

    # --------------------------------------------------------------- find

    def find(
        self,
        *,
        colors: list[str] | None = None,
        color_mode: str = "subset",
        format: str | None = None,
        owned_only: bool = False,
        text: str | None = None,
        limit: int | None = None,
        order_by: str = "c.cmc, c.name",
        **filters: Any,
    ) -> list[Card]:
        """Find cards matching every supplied predicate.

        Field predicates accept Django-style suffixes::

            engine.find(colors=["G"], cmc__lte=3, types__contains="Creature")
        """
        where, params = self._build_where(filters)
        joins = ""

        if colors:
            clause, cparams = self._color_clause(colors, color_mode)
            where.append(f"({clause})")
            params.extend(cparams)

        if text:
            where.append("LOWER(c.rules_text) LIKE ?")
            params.append(f"%{text.lower()}%")

        if format:
            # A card is legal if one of its printings is in a legal set, it is
            # not banned, and -- where the format uses an allow-list -- it is
            # on it. All keyed by title_id, as Arena states legality.
            joins += """
                JOIN printings p ON p.title_id = c.title_id
                JOIN format_sets fs
                  ON fs.set_code = p.set_code AND fs.format_name = ?
            """
            params.insert(0, format)
            where.append(
                "NOT EXISTS (SELECT 1 FROM format_title_rules r "
                "WHERE r.format_name = ? AND r.title_id = c.title_id "
                "AND r.rule IN ('banned','suppressed'))"
            )
            params.append(format)
            where.append(
                "(NOT EXISTS (SELECT 1 FROM format_title_rules r2 "
                "WHERE r2.format_name = ? AND r2.rule = 'allowed') "
                "OR EXISTS (SELECT 1 FROM format_title_rules r3 "
                "WHERE r3.format_name = ? AND r3.rule = 'allowed' "
                "AND r3.title_id = c.title_id))"
            )
            params.extend([format, format])

        if owned_only:
            where.append(
                "EXISTS (SELECT 1 FROM ownership o JOIN printings p2 "
                "ON p2.arena_id = o.arena_id "
                "WHERE p2.title_id = c.title_id AND o.quantity > 0)"
            )

        sql = f"SELECT DISTINCT c.* FROM cards c {joins}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {order_by}"
        if limit:
            sql += f" LIMIT {int(limit)}"

        return [self._row_to_card(r) for r in self.con.execute(sql, params)]

    # -------------------------------------------------------- convenience

    def by_name(self, name: str) -> Card | None:
        r = self.con.execute(
            "SELECT * FROM cards WHERE LOWER(name) = LOWER(?)", (name,)
        ).fetchone()
        return self._row_to_card(r) if r else None

    def by_arena_id(self, arena_id: int) -> Card | None:
        r = self.con.execute(
            "SELECT c.* FROM cards c JOIN printings p ON p.title_id = c.title_id "
            "WHERE p.arena_id = ?",
            (arena_id,),
        ).fetchone()
        return self._row_to_card(r) if r else None

    def owned_count(self, title_id: int) -> int:
        """Copies owned across every printing -- the number the four-copy
        limit actually applies to."""
        r = self.con.execute(
            "SELECT COALESCE(SUM(o.quantity), 0) FROM ownership o "
            "JOIN printings p ON p.arena_id = o.arena_id WHERE p.title_id = ?",
            (title_id,),
        ).fetchone()
        return r[0] or 0

    def count(self, table: str = "cards") -> int:
        if table not in {"cards", "printings", "ownership", "formats", "decks"}:
            raise ValueError(f"unknown table {table!r}")
        return self.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
