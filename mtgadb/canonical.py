"""The canonical database: one SQLite file every feature queries.

Arena already has this data, so why copy it? Because Arena's schema needs a
localization join and an enum lookup for *every* field, its filename hash
changes each set release, and it holds no ownership. This is a materialized
projection -- rebuilt when the source fingerprint changes, and the only shape
the rest of the app ever sees.

Builds are written as dated snapshots so collections can be diffed over time,
Arena format changes can be detected, and a bug can be reproduced against the
exact data that caused it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

from mtgadb.model import Card, CardPrinting, Collection, Deck, Format, Inventory

SCHEMA_VERSION = "2"

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);

-- What you build decks with. One row per distinct card.
CREATE TABLE cards (
    title_id       INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    mana_cost      TEXT,
    cmc            INTEGER,
    types          TEXT,
    subtypes       TEXT,
    colors         TEXT,
    color_identity TEXT,
    power          TEXT,
    toughness      TEXT,
    rules_text     TEXT,
    source_printing_id INTEGER,
    resolution     TEXT,
    anomaly_count  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_cards_name ON cards(name);
CREATE INDEX idx_cards_cmc  ON cards(cmc);
CREATE INDEX idx_cards_ci   ON cards(color_identity);

-- What you actually own. One row per Arena grpId.
CREATE TABLE printings (
    arena_id         INTEGER PRIMARY KEY,
    title_id         INTEGER NOT NULL REFERENCES cards(title_id),
    set_code         TEXT,
    collector_number TEXT,
    rarity           TEXT,
    raw_rules_text   TEXT
);
CREATE INDEX idx_printings_title ON printings(title_id);
CREATE INDEX idx_printings_set   ON printings(set_code);

CREATE TABLE ownership (
    arena_id INTEGER PRIMARY KEY REFERENCES printings(arena_id),
    quantity INTEGER NOT NULL CHECK (quantity >= 0)
);

CREATE TABLE wildcards (rarity TEXT PRIMARY KEY, count INTEGER NOT NULL);
CREATE TABLE inventory (key TEXT PRIMARY KEY, value INTEGER NOT NULL);

CREATE TABLE formats (
    name            TEXT PRIMARY KEY,
    min_deck_size   INTEGER,
    max_deck_size   INTEGER,
    max_sideboard   INTEGER,
    min_command_zone INTEGER,
    max_command_zone INTEGER,
    uses_rebalanced INTEGER NOT NULL DEFAULT 0,
    uses_rebalanced_present INTEGER NOT NULL DEFAULT 0,
    format_type_internal INTEGER,
    card_count_restriction_internal INTEGER,
    sideboard_behavior_internal INTEGER,
    color_restrictions_internal TEXT
);
CREATE TABLE format_sets (
    format_name TEXT REFERENCES formats(name),
    set_code    TEXT,
    PRIMARY KEY (format_name, set_code)
);
CREATE TABLE format_filter_sets (
    format_name TEXT REFERENCES formats(name),
    set_code    TEXT,
    PRIMARY KEY (format_name, set_code)
);
-- Bans and allow-lists are keyed by title_id, not arena_id: Arena states
-- legality per card, never per printing.
CREATE TABLE format_title_rules (
    format_name TEXT REFERENCES formats(name),
    title_id    INTEGER,
    rule        TEXT,          -- banned | allowed | suppressed
    PRIMARY KEY (format_name, title_id, rule)
);
CREATE TABLE format_card_quotas (
    format_name TEXT REFERENCES formats(name),
    title_id    INTEGER,
    max_copies  INTEGER NOT NULL,
    PRIMARY KEY (format_name, title_id)
);
CREATE TABLE format_commander_allowlist (
    format_name TEXT REFERENCES formats(name),
    title_id    INTEGER,
    PRIMARY KEY (format_name, title_id)
);
CREATE TABLE format_rarity_quotas (
    format_name TEXT REFERENCES formats(name),
    rarity_internal INTEGER,
    max_copies  INTEGER,
    PRIMARY KEY (format_name, rarity_internal)
);

CREATE TABLE decks (
    deck_id TEXT PRIMARY KEY,
    name    TEXT,
    source  TEXT
);
CREATE TABLE deck_cards (
    deck_id  TEXT REFERENCES decks(deck_id),
    arena_id INTEGER,
    zone     TEXT,             -- main | sideboard | command
    quantity INTEGER NOT NULL,
    PRIMARY KEY (deck_id, arena_id, zone)
);
"""


def snapshot_path(root: Path, when: date | None = None) -> Path:
    """snapshots/snapshot_YYYY-MM-DD.db"""
    when = when or date.today()
    return root / "snapshots" / f"snapshot_{when.isoformat()}.db"


def create(path: Path) -> sqlite3.Connection:
    """Create a fresh canonical database, replacing any file at `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def open_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def open_writable(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def set_meta(con: sqlite3.Connection, **values: str) -> None:
    con.executemany(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        [(k, str(v)) for k, v in values.items()],
    )


def get_meta(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def migrate(con: sqlite3.Connection) -> None:
    """Upgrade an existing canonical database without rebuilding card data."""
    version = get_meta(con, "schema_version")
    if version == SCHEMA_VERSION:
        return
    if version != "1":
        raise ValueError(f"cannot migrate schema {version!r} to {SCHEMA_VERSION}")
    columns = {r[1] for r in con.execute("PRAGMA table_info(formats)")}
    additions = {
        "min_command_zone": "INTEGER",
        "uses_rebalanced_present": "INTEGER NOT NULL DEFAULT 0",
        "format_type_internal": "INTEGER",
        "card_count_restriction_internal": "INTEGER",
        "sideboard_behavior_internal": "INTEGER",
        "color_restrictions_internal": "TEXT",
    }
    for name, definition in additions.items():
        if name not in columns:
            con.execute(f"ALTER TABLE formats ADD COLUMN {name} {definition}")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS format_filter_sets (
            format_name TEXT REFERENCES formats(name), set_code TEXT,
            PRIMARY KEY (format_name, set_code));
        CREATE TABLE IF NOT EXISTS format_card_quotas (
            format_name TEXT REFERENCES formats(name), title_id INTEGER,
            max_copies INTEGER NOT NULL, PRIMARY KEY (format_name, title_id));
        CREATE TABLE IF NOT EXISTS format_commander_allowlist (
            format_name TEXT REFERENCES formats(name), title_id INTEGER,
            PRIMARY KEY (format_name, title_id));
        CREATE TABLE IF NOT EXISTS format_rarity_quotas (
            format_name TEXT REFERENCES formats(name), rarity_internal INTEGER,
            max_copies INTEGER,
            PRIMARY KEY (format_name, rarity_internal));
    """)
    set_meta(con, schema_version=SCHEMA_VERSION)


# ------------------------------------------------------------- population


def load_cards(con: sqlite3.Connection, cards: Iterable[Card]) -> int:
    rows = [
        (
            c.title_id, c.name, c.mana_cost, c.cmc, c.types, c.subtypes,
            c.colors, c.color_identity, c.power, c.toughness, c.rules_text,
            c.source_printing_id, c.resolution.value, c.anomaly_count,
        )
        for c in cards
    ]
    con.executemany(
        "INSERT OR REPLACE INTO cards VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    return len(rows)


def load_printings(con: sqlite3.Connection, printings: Iterable[CardPrinting]) -> int:
    rows = [
        (p.arena_id, p.title_id, p.set_code, p.collector_number, p.rarity,
         p.raw_rules_text)
        for p in printings
    ]
    con.executemany("INSERT OR REPLACE INTO printings VALUES (?,?,?,?,?,?)", rows)
    return len(rows)


def load_ownership(con: sqlite3.Connection, collection: Collection) -> int:
    """Only printings we know about; an unknown id means a stale card db."""
    known = {r[0] for r in con.execute("SELECT arena_id FROM printings")}
    rows = [(a, q) for a, q in collection.cards.items() if a in known]
    con.execute("DELETE FROM ownership")
    con.executemany("INSERT INTO ownership VALUES (?,?)", rows)
    return len(rows)


def load_inventory(con: sqlite3.Connection, inv: Inventory) -> None:
    con.execute("DELETE FROM wildcards")
    con.executemany(
        "INSERT INTO wildcards VALUES (?,?)", list(inv.wildcards.items())
    )
    con.execute("DELETE FROM inventory")
    con.executemany(
        "INSERT INTO inventory VALUES (?,?)",
        [("gold", inv.gold), ("gems", inv.gems),
         ("vault_progress", inv.vault_progress)],
    )


def load_formats(con: sqlite3.Connection, formats: dict[str, Format]) -> int:
    for table in (
        "format_sets", "format_filter_sets", "format_title_rules",
        "format_card_quotas", "format_commander_allowlist",
        "format_rarity_quotas", "formats",
    ):
        con.execute(f"DELETE FROM {table}")
    for f in formats.values():
        con.execute(
            "INSERT OR REPLACE INTO formats "
            "(name, min_deck_size, max_deck_size, max_sideboard, "
            "min_command_zone, max_command_zone, uses_rebalanced, "
            "uses_rebalanced_present, format_type_internal, "
            "card_count_restriction_internal, sideboard_behavior_internal, "
            "color_restrictions_internal) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f.name, f.min_deck_size, f.max_deck_size, f.max_sideboard,
             f.min_command_zone, f.max_command_zone,
             int(bool(f.uses_rebalanced_cards)),
             int(f.uses_rebalanced_cards is not None),
             f.format_type_internal, f.card_count_restriction_internal,
             f.sideboard_behavior_internal,
             json.dumps([sorted(v) for v in f.color_restrictions_internal])),
        )
        con.executemany(
            "INSERT OR REPLACE INTO format_sets VALUES (?,?)",
            [(f.name, s) for s in f.legal_sets],
        )
        con.executemany(
            "INSERT OR REPLACE INTO format_filter_sets VALUES (?,?)",
            [(f.name, s) for s in f.filter_sets],
        )
        rules = [(f.name, t, "banned") for t in f.banned_title_ids]
        rules += [(f.name, t, "suppressed") for t in f.suppressed_title_ids]
        rules += [(f.name, t, "suspended") for t in f.suspended_title_ids]
        if f.allowed_title_ids is not None:
            rules += [(f.name, t, "allowed") for t in f.allowed_title_ids]
        con.executemany(
            "INSERT OR REPLACE INTO format_title_rules VALUES (?,?,?)", rules
        )
        con.executemany(
            "INSERT OR REPLACE INTO format_card_quotas VALUES (?,?,?)",
            [(f.name, title_id, limit)
             for title_id, limit in f.individual_card_quotas.items()],
        )
        if f.allowed_commander_title_ids is not None:
            con.executemany(
                "INSERT OR REPLACE INTO format_commander_allowlist VALUES (?,?)",
                [(f.name, title_id) for title_id in f.allowed_commander_title_ids],
            )
        con.executemany(
            "INSERT OR REPLACE INTO format_rarity_quotas VALUES (?,?,?)",
            [(f.name, rarity, limit)
             for rarity, limit in f.rarity_card_quotas.items()],
        )
    return len(formats)


def get_format(con: sqlite3.Connection, name: str) -> Format | None:
    """Reconstruct one format from the canonical schema."""
    row = con.execute(
        "SELECT * FROM formats WHERE LOWER(name) = LOWER(?)", (name,)
    ).fetchone()
    if row is None:
        return None

    def title_rules(rule: str) -> frozenset[int]:
        return frozenset(r[0] for r in con.execute(
            "SELECT title_id FROM format_title_rules "
            "WHERE format_name = ? AND rule = ?", (row["name"], rule)
        ))

    allowed = title_rules("allowed")
    commanders = frozenset(r[0] for r in con.execute(
        "SELECT title_id FROM format_commander_allowlist WHERE format_name = ?",
        (row["name"],),
    ))
    colors = json.loads(row["color_restrictions_internal"] or "[]")
    return Format(
        name=row["name"],
        legal_sets=frozenset(r[0] for r in con.execute(
            "SELECT set_code FROM format_sets WHERE format_name = ?", (row["name"],)
        )),
        filter_sets=frozenset(r[0] for r in con.execute(
            "SELECT set_code FROM format_filter_sets WHERE format_name = ?",
            (row["name"],),
        )),
        banned_title_ids=title_rules("banned"),
        allowed_title_ids=allowed if allowed else None,
        suppressed_title_ids=title_rules("suppressed"),
        suspended_title_ids=title_rules("suspended"),
        allowed_commander_title_ids=commanders if commanders else None,
        individual_card_quotas={r[0]: r[1] for r in con.execute(
            "SELECT title_id, max_copies FROM format_card_quotas "
            "WHERE format_name = ?", (row["name"],)
        )},
        rarity_card_quotas={r[0]: r[1] for r in con.execute(
            "SELECT rarity_internal, max_copies FROM format_rarity_quotas "
            "WHERE format_name = ?", (row["name"],)
        )},
        min_deck_size=row["min_deck_size"],
        max_deck_size=row["max_deck_size"],
        max_sideboard=row["max_sideboard"],
        min_command_zone=row["min_command_zone"],
        max_command_zone=row["max_command_zone"],
        uses_rebalanced_cards=(
            bool(row["uses_rebalanced"])
            if row["uses_rebalanced_present"] else None
        ),
        format_type_internal=row["format_type_internal"],
        card_count_restriction_internal=row["card_count_restriction_internal"],
        sideboard_behavior_internal=row["sideboard_behavior_internal"],
        color_restrictions_internal=tuple(frozenset(v) for v in colors),
    )


def load_decks(con: sqlite3.Connection, decks: Iterable[Deck], source: str) -> int:
    n = 0
    for d in decks:
        con.execute(
            "INSERT OR REPLACE INTO decks VALUES (?,?,?)", (d.deck_id, d.name, source)
        )
        rows = []
        for zone, cards in (
            ("main", d.main), ("sideboard", d.sideboard), ("command", d.commander)
        ):
            rows += [(d.deck_id, a, zone, q) for a, q in cards.items()]
        con.executemany("INSERT OR REPLACE INTO deck_cards VALUES (?,?,?,?)", rows)
        n += 1
    return n


def stamp(con: sqlite3.Connection, source_fingerprint: str) -> None:
    set_meta(
        con,
        schema_version=SCHEMA_VERSION,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        arena_db_fingerprint=source_fingerprint,
    )
