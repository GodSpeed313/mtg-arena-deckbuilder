"""Application-managed local deck storage and conditional storage mutations.

In-file identities cannot detect external copying, rollback, or coherent
tampering. Observations are historical values, not credentials or authority.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from uuid import UUID, uuid4

from mtgadb.deck_identity import build_deck_snapshot_identity, require_deck_snapshot_identity
from mtgadb.model import Deck


STORE_KIND = "local_managed_decks"
SCHEMA_VERSION = "1"
DESTINATION_STATE_VERSION = "1"
MAX_INTEGER = 2**63 - 1
_ZONES = ("main", "sideboard", "commander")
_ERRORS = frozenset({
    "missing_store", "unsupported_schema", "malformed_store", "unavailable_store",
    "missing_record", "deleted_record", "malformed_record", "invalid_input",
    "malformed_expected", "malformed_replacement", "store_identity_mismatch",
    "store_generation_mismatch", "record_identity_mismatch", "revision_mismatch",
    "baseline_mismatch", "metadata_mismatch", "revision_exhausted",
    "storage_busy", "transaction_failed", "commit_failed", "result_mismatch",
})
_SCHEMA = (
    """CREATE TABLE managed_store_meta (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        store_kind TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        store_id TEXT NOT NULL,
        store_generation TEXT NOT NULL
    )""",
    """CREATE TABLE managed_decks (
        record_id TEXT PRIMARY KEY NOT NULL,
        revision INTEGER NOT NULL CHECK(typeof(revision) = 'integer' AND revision > 0),
        lifecycle TEXT NOT NULL CHECK(lifecycle IN ('live', 'deleted')),
        name TEXT NOT NULL,
        format_label TEXT
    )""",
    """CREATE TABLE managed_deck_cards (
        record_id TEXT NOT NULL REFERENCES managed_decks(record_id),
        zone TEXT NOT NULL CHECK(zone IN ('main', 'sideboard', 'commander')),
        arena_id INTEGER NOT NULL CHECK(typeof(arena_id) = 'integer' AND arena_id > 0),
        quantity INTEGER NOT NULL CHECK(typeof(quantity) = 'integer' AND quantity > 0),
        PRIMARY KEY(record_id, zone, arena_id)
    )""",
)
_STATE_FIELDS = {
    "destination_state_version", "store_id", "store_generation", "record_id",
    "revision", "deck", "metadata", "gameplay_snapshot_identity",
}


class ManagedDeckStoreError(ValueError):
    """Closed error code; messages never contain stored deck data."""

    def __init__(self, code: str):
        if code not in _ERRORS:
            raise ValueError("Unsupported managed-store error code")
        self.code = code
        super().__init__(code)


def _fail(code):
    raise ManagedDeckStoreError(code)


def _uuid(value, code):
    try:
        if type(value) is not str or str(UUID(value)) != value or UUID(value).version != 4:
            _fail(code)
    except (ValueError, AttributeError):
        _fail(code)
    return value


def _integer(value, code):
    if type(value) is not int or not 1 <= value <= MAX_INTEGER:
        _fail(code)
    return value


def _metadata(name, format_label, code):
    if type(name) is not str or (format_label is not None and type(format_label) is not str):
        _fail(code)
    try:
        name.encode("utf-8")
        if format_label is not None:
            format_label.encode("utf-8")
    except UnicodeError:
        _fail(code)
    return {"name": name, "format_label": format_label}


def _deck(value, name, code):
    if type(value) is not Deck or type(value.deck_id) is not str or type(value.name) is not str:
        _fail(code)
    try:
        build_deck_snapshot_identity(value)
        for zone in _ZONES:
            for arena_id, quantity in getattr(value, zone).items():
                _integer(arena_id, code)
                _integer(quantity, code)
    except ValueError:
        _fail(code)
    return Deck(name=name, **{zone: deepcopy(getattr(value, zone)) for zone in _ZONES})


def require_destination_state(value: dict) -> dict:
    """Verify native or serialized historical observation; never read a store.

    Returns a detached native observation. This proves neither currentness nor
    provenance. Future writers must reread authoritative state in a transaction.
    """
    try:
        if type(value) is not dict or set(value) != _STATE_FIELDS:
            _fail("invalid_input")
        if value["destination_state_version"] != DESTINATION_STATE_VERSION:
            _fail("invalid_input")
        for key in ("store_id", "store_generation", "record_id"):
            _uuid(value[key], "invalid_input")
        _integer(value["revision"], "invalid_input")
        metadata = value["metadata"]
        if type(metadata) is not dict or set(metadata) != {"name", "format_label"}:
            _fail("invalid_input")
        _metadata(**metadata, code="invalid_input")
        raw = value["deck"]
        if type(raw) is dict:
            if set(raw) != {"deck_id", "name", *_ZONES}:
                _fail("invalid_input")
            zones = {}
            for zone in _ZONES:
                pairs = raw[zone]
                if type(pairs) is not list:
                    _fail("invalid_input")
                result = {}
                for pair in pairs:
                    if type(pair) is not list or len(pair) != 2:
                        _fail("invalid_input")
                    arena_id, quantity = pair
                    _integer(arena_id, "invalid_input")
                    _integer(quantity, "invalid_input")
                    if arena_id in result:
                        _fail("invalid_input")
                    result[arena_id] = quantity
                if pairs != [[key, result[key]] for key in sorted(result)]:
                    _fail("invalid_input")
                zones[zone] = result
            raw = Deck(deck_id=raw["deck_id"], name=raw["name"], **zones)
        deck = _deck(raw, metadata["name"], "invalid_input")
        if raw.deck_id != "" or raw.name != metadata["name"]:
            _fail("invalid_input")
        identity = require_deck_snapshot_identity(value["gameplay_snapshot_identity"])
        expected = build_deck_snapshot_identity(deck)
        if json.dumps(identity, sort_keys=True) != json.dumps(expected, sort_keys=True):
            _fail("invalid_input")
        return {**deepcopy(value), "deck": deck, "gameplay_snapshot_identity": expected}
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        _fail("invalid_input")


def serialize_destination_state(value: dict) -> dict:
    """Return the closed JSON representation (zones are sorted integer pairs)."""
    result = require_destination_state(value)
    result["deck"] = {
        "deck_id": "", "name": result["metadata"]["name"],
        **deepcopy(result["gameplay_snapshot_identity"]["canonical_payload"]),
    }
    return result


def _schema(con):
    objects = con.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE substr(name,1,7) != 'sqlite_'"
    ).fetchall()
    expected = {statement.split()[2]: statement for statement in _SCHEMA}
    if {row["name"] for row in objects} != set(expected):
        _fail("malformed_store")
    # A closed schema rejects missing constraints, extra columns, views and
    # triggers, not just familiar table names. Whitespace is non-semantic.
    for row in objects:
        if row["type"] != "table" or " ".join(row["sql"].split()) != " ".join(expected[row["name"]].split()):
            _fail("malformed_store")
    rows = con.execute("SELECT * FROM managed_store_meta").fetchall()
    if len(rows) != 1:
        _fail("malformed_store")
    meta = dict(rows[0])
    if type(meta["singleton"]) is not int or meta["singleton"] != 1 or meta["store_kind"] != STORE_KIND:
        _fail("malformed_store")
    if type(meta["schema_version"]) is not str:
        _fail("malformed_store")
    if meta["schema_version"] != SCHEMA_VERSION:
        _fail("unsupported_schema")
    _uuid(meta["store_id"], "malformed_store")
    _uuid(meta["store_generation"], "malformed_store")
    if con.execute("PRAGMA foreign_key_check").fetchone() is not None:
        _fail("malformed_store")
    return {key: meta[key] for key in ("store_kind", "schema_version", "store_id", "store_generation")}


@contextmanager
def _connection(path, *, write=False, initialize=False, mutation=False):
    con = None
    phase = "open"
    if not isinstance(path, (str, Path)) or not str(path):
        _fail("invalid_input")
    try:
        path = Path(path).resolve()
        if not initialize and not path.exists():
            _fail("missing_store")
        mode = "rwc" if initialize else "rw" if write else "ro"
        con = sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        if con.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            _fail("unavailable_store")
        if con.execute("PRAGMA journal_mode").fetchone()[0] not in ("delete", "truncate", "persist", "wal"):
            _fail("unavailable_store")
        if write:
            con.execute("PRAGMA synchronous=FULL")
        con.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        phase = "transaction"
        yield con
        phase = "commit"
        con.commit()
    except sqlite3.Error as exc:
        code = getattr(exc, "sqlite_errorcode", 0) & 255
        if mutation and code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            _fail("storage_busy")
        if mutation and code not in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
            if phase == "commit":
                _fail("commit_failed")
            if phase == "transaction":
                _fail("transaction_failed")
        _fail("malformed_store" if code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB) else "unavailable_store")
    except (OSError, TypeError):
        _fail("unavailable_store")
    finally:
        if con is not None:
            try:
                if con.in_transaction:
                    con.rollback()
            finally:
                con.close()


def initialize_store(path) -> dict:
    """Explicitly initialize an empty store, or validate an existing store."""
    with _connection(path, write=True, initialize=True) as con:
        if not con.execute("SELECT 1 FROM sqlite_master").fetchone():
            for statement in _SCHEMA:
                con.execute(statement)
            con.execute("INSERT INTO managed_store_meta VALUES (1,?,?,?,?)", (
                STORE_KIND, SCHEMA_VERSION, str(uuid4()), str(uuid4()),
            ))
        result = _schema(con)
    return result


def open_store(path) -> dict:
    """Validate an existing store and return detached metadata, not a connection."""
    with _connection(path) as con:
        return _schema(con)


def _read(con, meta, record_id, *, include_deleted=False):
    row = con.execute("SELECT * FROM managed_decks WHERE record_id=?", (record_id,)).fetchone()
    if row is None:
        _fail("missing_record")
    _uuid(row["record_id"], "malformed_record")
    _integer(row["revision"], "malformed_record")
    metadata = _metadata(row["name"], row["format_label"], "malformed_record")
    if row["lifecycle"] not in ("live", "deleted"):
        _fail("malformed_record")
    zones = {zone: {} for zone in _ZONES}
    for card in con.execute("SELECT * FROM managed_deck_cards WHERE record_id=?", (record_id,)):
        zone = card["zone"]
        if zone not in zones:
            _fail("malformed_record")
        arena_id = _integer(card["arena_id"], "malformed_record")
        quantity = _integer(card["quantity"], "malformed_record")
        if arena_id in zones[zone]:
            _fail("malformed_record")
        zones[zone][arena_id] = quantity
    if row["lifecycle"] == "deleted":
        if include_deleted:
            return None
        _fail("deleted_record")
    deck = Deck(name=metadata["name"], **zones)
    return {
        "destination_state_version": DESTINATION_STATE_VERSION,
        "store_id": meta["store_id"], "store_generation": meta["store_generation"],
        "record_id": record_id, "revision": row["revision"], "deck": deck,
        "metadata": metadata, "gameplay_snapshot_identity": build_deck_snapshot_identity(deck),
    }


def create_managed_deck(path, deck: Deck, name: str, format_label: str | None = None) -> dict:
    """Create a new identity at revision 1; never overwrite an existing record."""
    metadata = _metadata(name, format_label, "invalid_input")
    frozen = _deck(deck, name, "invalid_input")
    record_id = str(uuid4())
    with _connection(path, write=True) as con:
        meta = _schema(con)
        con.execute("INSERT INTO managed_decks VALUES (?,1,'live',?,?)", (
            record_id, metadata["name"], metadata["format_label"],
        ))
        con.executemany("INSERT INTO managed_deck_cards VALUES (?,?,?,?)", [
            (record_id, zone, arena_id, quantity)
            for zone in _ZONES for arena_id, quantity in getattr(frozen, zone).items()
        ])
        result = _read(con, meta, record_id)
    return result


def read_destination_state(path, record_id: str) -> dict:
    _uuid(record_id, "invalid_input")
    with _connection(path) as con:
        return _read(con, _schema(con), record_id)


def list_destinations(path) -> list[dict]:
    """List live summaries by UUID; each selection still requires a fresh read."""
    with _connection(path) as con:
        meta = _schema(con)
        result = []
        for row in con.execute("SELECT record_id FROM managed_decks ORDER BY record_id").fetchall():
            state = _read(con, meta, row[0], include_deleted=True)
            if state is not None:
                result.append({key: state[key] for key in (
                    "destination_state_version", "store_id", "store_generation",
                    "record_id", "revision", "metadata",
                )})
        return result


def _expected(record_id, value):
    _uuid(record_id, "invalid_input")
    try:
        expected = require_destination_state(value)
    except ManagedDeckStoreError:
        _fail("malformed_expected")
    if record_id != expected["record_id"]:
        _fail("record_identity_mismatch")
    return expected


def _current_for_mutation(con, expected):
    meta = _schema(con)
    if meta["store_id"] != expected["store_id"]:
        _fail("store_identity_mismatch")
    if meta["store_generation"] != expected["store_generation"]:
        _fail("store_generation_mismatch")
    current = _read(con, meta, expected["record_id"])
    if current["revision"] != expected["revision"]:
        _fail("revision_mismatch")
    if current["gameplay_snapshot_identity"] != expected["gameplay_snapshot_identity"]:
        _fail("baseline_mismatch")
    if current["metadata"] != expected["metadata"]:
        _fail("metadata_mismatch")
    return meta, current


def _next_revision(current):
    if current["revision"] == MAX_INTEGER:
        _fail("revision_exhausted")
    return current["revision"] + 1


def conditional_replace(path, record_id: str, expected_destination_state: dict,
                        replacement_deck: Deck, *, name: str,
                        format_label: str | None) -> dict:
    """Replace exact contents/metadata under CAS; grants no application authority.

    Both metadata arguments are required; None explicitly clears format_label.
    Returns {status: changed|unchanged, destination_state: fresh observation}.
    """
    expected = _expected(record_id, expected_destination_state)
    with _connection(path, write=True, mutation=True) as con:
        meta, current = _current_for_mutation(con, expected)
        metadata = _metadata(name, format_label, "malformed_replacement")
        replacement = _deck(replacement_deck, name, "malformed_replacement")
        identity = build_deck_snapshot_identity(replacement)
        changed = metadata != current["metadata"] or identity != current["gameplay_snapshot_identity"]
        revision = _next_revision(current) if changed else current["revision"]
        if changed:
            con.execute("UPDATE managed_decks SET revision=?, name=?, format_label=? WHERE record_id=?",
                        (revision, name, format_label, record_id))
            con.execute("DELETE FROM managed_deck_cards WHERE record_id=?", (record_id,))
            con.executemany("INSERT INTO managed_deck_cards VALUES (?,?,?,?)", [
                (record_id, zone, arena_id, quantity)
                for zone in _ZONES for arena_id, quantity in getattr(replacement, zone).items()
            ])
        result = _read(con, meta, record_id)
        wanted = {**current, "revision": revision, "deck": replacement,
                  "metadata": metadata, "gameplay_snapshot_identity": identity}
        if serialize_destination_state(result) != serialize_destination_state(wanted):
            _fail("result_mismatch")
    return {"status": "changed" if changed else "unchanged", "destination_state": result}


def conditional_delete(path, record_id: str, expected_destination_state: dict) -> dict:
    """Tombstone a matching live record, retaining its UUID and last contents.

    The returned acknowledgment is not a durable receipt or a live observation.
    """
    expected = _expected(record_id, expected_destination_state)
    with _connection(path, write=True, mutation=True) as con:
        meta, current = _current_for_mutation(con, expected)
        revision = _next_revision(current)
        con.execute("UPDATE managed_decks SET lifecycle='deleted', revision=? WHERE record_id=?",
                    (revision, record_id))
        header = con.execute("SELECT * FROM managed_decks WHERE record_id=?", (record_id,)).fetchone()
        rows = con.execute("SELECT zone, arena_id, quantity FROM managed_deck_cards WHERE record_id=?",
                           (record_id,)).fetchall()
        wanted_rows = sorted((zone, arena_id, quantity) for zone in _ZONES
                             for arena_id, quantity in getattr(current["deck"], zone).items())
        if dict(header) != {"record_id": record_id, "revision": revision, "lifecycle": "deleted",
                            **current["metadata"]} or sorted(tuple(row) for row in rows) != wanted_rows:
            _fail("result_mismatch")
        result = {"status": "changed", "lifecycle": "deleted", "store_id": meta["store_id"],
                  "store_generation": meta["store_generation"], "record_id": record_id,
                  "previous_revision": current["revision"], "revision": revision}
    return result
