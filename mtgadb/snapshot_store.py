"""Immutable Arena observations in a separate, privately held SQLite archive.

No account inference, current-state projection, deletion, or canonical writes.
Only save_snapshot writes observations; each call generates a new opaque ID.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from mtgadb.providers.arena_log import ArenaLogProvider

STORE_VERSION = "1"
PAYLOAD_VERSION = 1
CAPABILITIES = ("inventory", "decks", "collection")
ZONES = ("main", "sideboard", "commander", "companions")
RARITIES = ("common", "uncommon", "rare", "mythic")
BALANCES = ("gold", "gems", "vault_progress", "wildcard_track_position")
ATTRIBUTES = frozenset({
    "Format", "Version", "TileID", "LastPlayed", "LastUpdated", "IsFavorite",
    "favorite", "lastPlayed",
})
RECORD_FIELDS = frozenset({"MainDeck", "Sideboard", "CommandZone", "Companions", "CardSkins"})
EVIDENCE_FIELDS = frozenset({
    "source", "source_event", "authority", "observed_at", "timestamp_source",
    "availability", "completeness", "selection", "start_hook_responses", "event_line",
    "timestamp_issue", "error", "reason", "missing_fields", "sequence_id", "vault_units",
    "completeness_scope", "printing_ids_checked", "unknown_printing_ids",
    "missing_summary_count", "missing_record_count",
})
CACHE_FIELDS = ("DeckSummariesCacheVersion", "DecksCacheVersion")

SCHEMA = (
    "CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE arena_snapshots (
        sequence INTEGER PRIMARY KEY,
        snapshot_id TEXT NOT NULL UNIQUE,
        observed_at TEXT,
        persisted_at TEXT NOT NULL,
        source TEXT NOT NULL CHECK(source = 'Arena Player.log'),
        source_event TEXT NOT NULL CHECK(source_event = 'StartHook'),
        payload_version INTEGER NOT NULL,
        content_hash TEXT NOT NULL,
        account_key TEXT CHECK(account_key IS NULL)
    )""",
    """CREATE TABLE arena_snapshot_capabilities (
        snapshot_id TEXT NOT NULL REFERENCES arena_snapshots(snapshot_id),
        capability TEXT NOT NULL CHECK(capability IN ('inventory','decks','collection')),
        status TEXT NOT NULL CHECK(status IN ('ok','not_found','unsupported','error')),
        completeness TEXT NOT NULL CHECK(completeness IN ('complete','partial','unknown')),
        data_json TEXT,
        evidence_json TEXT NOT NULL,
        PRIMARY KEY(snapshot_id, capability),
        CHECK((status = 'ok' AND data_json IS NOT NULL) OR
              (status != 'ok' AND data_json IS NULL)),
        CHECK(capability != 'collection' OR (status = 'unsupported' AND data_json IS NULL))
    )""",
)


class SnapshotStoreError(ValueError):
    """Safe archive error: never include a raw payload or account identifiers."""


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise SnapshotStoreError("Snapshot is not safely serializable") from None


def _number(value):
    if value is not None and (type(value) is not int or value < 0):
        raise SnapshotStoreError("Snapshot counts must be nonnegative integers or null")
    return value


def _time(value, *, nullable=True):
    if value is None and nullable:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
        return parsed
    except (TypeError, ValueError):
        raise SnapshotStoreError("Invalid snapshot timestamp") from None


def _inventory(raw):
    if not isinstance(raw, dict) or set(raw) != {"wildcards", *BALANCES}:
        raise SnapshotStoreError("Invalid inventory snapshot fields")
    wildcards = raw["wildcards"]
    if not isinstance(wildcards, dict) or set(wildcards) != set(RARITIES):
        raise SnapshotStoreError("Invalid wildcard snapshot fields")
    return {"wildcards": {k: _number(wildcards[k]) for k in RARITIES},
            **{k: _number(raw[k]) for k in BALANCES}}


def _attributes(raw):
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise SnapshotStoreError("Invalid deck attributes")
    result = []
    for attr in raw:
        if not isinstance(attr, dict) or not isinstance(attr.get("name"), str):
            raise SnapshotStoreError("Invalid deck attribute")
        if attr["name"] not in ATTRIBUTES:
            continue
        kept = {"name": attr["name"]}
        if "value" in attr:
            if not isinstance(attr["value"], str):
                raise SnapshotStoreError("Invalid deck attribute value")
            kept["value"] = attr["value"]
        result.append(kept)
    return result


def _deck(raw):
    if not isinstance(raw, dict):
        raise SnapshotStoreError("Invalid saved deck")
    result = {}
    for field in ("deck_id", "name", "format"):
        value = raw[field]
        if value is not None and not isinstance(value, str):
            raise SnapshotStoreError("Invalid deck metadata")
        result[field] = value
    if not result["deck_id"]:
        raise SnapshotStoreError("Missing deck identity")
    result["attributes"] = _attributes(raw["attributes"])
    summary = raw["summary"]
    if summary is not None and not isinstance(summary, dict):
        raise SnapshotStoreError("Invalid deck summary")
    # Preserve only reviewed summary fields, not arbitrary provider metadata.
    result["summary"] = None if summary is None else {
        k: summary[k] for k in ("DeckIdInternal", "Name", "DeckTileId", "DeckArtId")
        if k in summary
    }
    if summary is not None:
        for value in result["summary"].values():
            if value is not None and type(value) not in (str, int):
                raise SnapshotStoreError("Invalid summary scalar")
        if "Attributes" in summary:
            result["summary"]["Attributes"] = _attributes(summary["Attributes"])
    for zone in ZONES:
        entries = raw[zone]
        if entries is None:
            result[zone] = None
            continue
        if not isinstance(entries, list):
            raise SnapshotStoreError("Invalid deck zone")
        result[zone] = []
        for entry in entries:
            if not isinstance(entry, dict) or not {"cardId", "quantity"} <= entry.keys():
                raise SnapshotStoreError("Invalid deck zone entry")
            card_id, qty = _number(entry["cardId"]), _number(entry["quantity"])
            if not card_id or qty is None:
                raise SnapshotStoreError("Invalid card ID or quantity")
            result[zone].append({"cardId": card_id, "quantity": qty})
    skins = raw["card_skins"]
    result["card_skins"] = None
    if skins is not None:
        if not isinstance(skins, list):
            raise SnapshotStoreError("Invalid card skins")
        result["card_skins"] = []
        for skin in skins:
            if not isinstance(skin, dict):
                raise SnapshotStoreError("Invalid card skin")
            kept = {k: skin[k] for k in ("GrpId", "CCV") if k in skin}
            if any(type(v) not in (str, int) for v in kept.values()):
                raise SnapshotStoreError("Invalid cosmetic scalar")
            result["card_skins"].append(kept)
    if type(raw["record_present"]) is not bool:
        raise SnapshotStoreError("Invalid record-presence marker")
    result["record_present"] = raw["record_present"]
    if not isinstance(raw["record_fields"], (list, tuple)):
        raise SnapshotStoreError("Invalid record fields")
    result["record_fields"] = [k for k in raw["record_fields"] if k in RECORD_FIELDS]
    return result


def _evidence(raw):
    if not isinstance(raw, dict):
        raise SnapshotStoreError("Invalid capability evidence")
    result = {k: raw[k] for k in EVIDENCE_FIELDS if k in raw}
    # Evidence comes from provider-generated diagnostics; never save raw responses.
    if "cache_markers" in raw:
        markers = raw["cache_markers"]
        if not isinstance(markers, dict):
            raise SnapshotStoreError("Invalid cache markers")
        result["cache_markers"] = {k: markers[k] for k in CACHE_FIELDS if k in markers}
    return result


def _validate(document):
    """Validate both prepared bundles and records read back from the archive."""
    try:
        _time(document["observed_at"])
        caps = document["capabilities"]
        if set(caps) != set(CAPABILITIES):
            raise SnapshotStoreError("Snapshot must contain all three capabilities")
        for name in CAPABILITIES:
            cap = caps[name]
            status, completeness, data = cap["status"], cap["completeness"], cap["data"]
            if status not in ("ok", "not_found", "unsupported", "error"):
                raise SnapshotStoreError("Invalid capability status")
            if completeness not in ("complete", "partial", "unknown"):
                raise SnapshotStoreError("Invalid capability completeness")
            evidence = cap["evidence"]
            if evidence.get("source") != "Arena Player.log" or evidence.get("source_event") != "StartHook":
                raise SnapshotStoreError("Invalid capability source")
            if evidence.get("completeness") != completeness:
                raise SnapshotStoreError("Inconsistent capability completeness")
            if name != "collection" and evidence.get("observed_at") != document["observed_at"]:
                raise SnapshotStoreError("Capabilities must share an observation timestamp")
            if status == "ok":
                if data is None or completeness == "unknown":
                    raise SnapshotStoreError("Missing successful capability data")
                if name == "inventory":
                    if _inventory(data) != data:
                        raise SnapshotStoreError("Invalid inventory data")
                    known = all(v is not None for v in data["wildcards"].values()) and all(
                        data[k] is not None for k in BALANCES)
                    if completeness == "complete" and not known:
                        raise SnapshotStoreError("Unknown inventory cannot be marked complete")
                elif name == "decks":
                    if not isinstance(data, list) or [_deck(d) for d in data] != data:
                        raise SnapshotStoreError("Invalid deck data")
                    if len({d["deck_id"] for d in data}) != len(data):
                        raise SnapshotStoreError("Duplicate saved deck identity")
                    known = all(d["summary"] is not None and d["name"] is not None
                                and d["record_present"] and all(d[z] is not None for z in ZONES)
                                for d in data)
                    if completeness == "complete" and not known:
                        raise SnapshotStoreError("Unknown deck fields cannot be marked complete")
            elif data is not None or completeness != "unknown":
                raise SnapshotStoreError("Failed capability cannot contain confirmed data")
            if name == "collection" and (status != "unsupported" or data is not None):
                raise SnapshotStoreError("Collection ownership is unsupported")
        _json(document)
    except (KeyError, TypeError, AttributeError, RecursionError):
        raise SnapshotStoreError("Malformed snapshot bundle") from None


def _prepare(provider: ArenaLogProvider):
    # Callers supply the provider, not independently assembled capability results.
    bundle = provider.get_snapshot()
    document = {"observed_at": bundle.observed_at, "capabilities": {}}
    for name in CAPABILITIES:
        result = getattr(bundle, name)
        data = result.data
        if result.ok and name == "inventory":
            data = _inventory({k: getattr(data, k) for k in ("wildcards", *BALANCES)})
        elif result.ok and name == "decks":
            data = [_deck({k: getattr(d, k) for k in (
                "deck_id", "name", "summary", "attributes", "format", *ZONES,
                "card_skins", "record_present", "record_fields",
            )}) for d in data]
        document["capabilities"][name] = {
            "status": result.diagnostics.status.value,
            "completeness": result.diagnostics.evidence["completeness"],
            "data": data, "evidence": _evidence(result.diagnostics.evidence),
        }
    _validate(document)
    # Freeze the validated value into plain JSON types before opening a writer.
    encoded = _json(document)
    return json.loads(encoded), hashlib.sha256(encoded.encode()).hexdigest()


def _check_schema(con, *, create=False):
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not tables and create:
        for statement in SCHEMA:
            con.execute(statement)
        con.execute("INSERT INTO store_meta VALUES ('schema_version', ?)", (STORE_VERSION,))
        return
    if not {"store_meta", "arena_snapshots", "arena_snapshot_capabilities"} <= tables:
        raise SnapshotStoreError("Not an Arena snapshot archive")
    row = con.execute("SELECT value FROM store_meta WHERE key='schema_version'").fetchone()
    if row is None or row[0] != STORE_VERSION:
        raise SnapshotStoreError("Unsupported snapshot-store schema version")


@contextmanager
def _connection(path: Path, *, write=False):
    con = None
    try:
        con = sqlite3.connect(path.resolve().as_uri() + ("?mode=rwc" if write else "?mode=ro"),
                              uri=True, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        if write:
            con.execute("BEGIN IMMEDIATE")
        _check_schema(con, create=write)
        yield con
        if write:
            con.commit()
    except sqlite3.Error:
        raise SnapshotStoreError("Snapshot archive operation failed; no success confirmed") from None
    finally:
        if con is not None:
            try:
                if con.in_transaction:
                    con.rollback()
            finally:
                con.close()


def create_store(path: Path):
    """Create an empty versioned archive, or verify an existing one; never erase."""
    with _connection(path, write=True):
        pass


def save_snapshot(path: Path, provider: ArenaLogProvider):
    """Append exactly one observation; repeated saves intentionally remain separate."""
    document, digest = _prepare(provider)
    snapshot_id = str(uuid4())
    persisted_at = datetime.now(timezone.utc).isoformat()
    with _connection(path, write=True) as con:
        cursor = con.execute(
            "INSERT INTO arena_snapshots (snapshot_id, observed_at, persisted_at, source, "
            "source_event, payload_version, content_hash, account_key) VALUES (?,?,?,?,?,?,?,NULL)",
            (snapshot_id, document["observed_at"], persisted_at, "Arena Player.log", "StartHook",
             PAYLOAD_VERSION, digest),
        )
        sequence = cursor.lastrowid
        for name in CAPABILITIES:
            cap = document["capabilities"][name]
            con.execute("INSERT INTO arena_snapshot_capabilities VALUES (?,?,?,?,?,?)", (
                snapshot_id, name, cap["status"], cap["completeness"],
                None if cap["data"] is None else _json(cap["data"]), _json(cap["evidence"]),
            ))
    return {"snapshot_id": snapshot_id, "sequence": sequence, "persisted_at": persisted_at,
            "source": "Arena Player.log", "source_event": "StartHook", "account_key": None,
            "payload_version": PAYLOAD_VERSION, "content_hash": digest, **document}


def _read(con, snapshot_id):
    row = con.execute("SELECT * FROM arena_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
    if row is None:
        raise SnapshotStoreError("Snapshot not found")
    if row["payload_version"] != PAYLOAD_VERSION:
        raise SnapshotStoreError("Unsupported snapshot payload version")
    if row["account_key"] is not None or row["source"] != "Arena Player.log" or row["source_event"] != "StartHook":
        raise SnapshotStoreError("Invalid snapshot metadata")
    _time(row["persisted_at"], nullable=False)
    caps = {}
    try:
        for cap in con.execute("SELECT * FROM arena_snapshot_capabilities WHERE snapshot_id=?", (snapshot_id,)):
            caps[cap["capability"]] = {
                "status": cap["status"], "completeness": cap["completeness"],
                "data": None if cap["data_json"] is None else json.loads(cap["data_json"]),
                "evidence": json.loads(cap["evidence_json"]),
            }
    except (ValueError, RecursionError):
        raise SnapshotStoreError("Invalid archived JSON") from None
    document = {"observed_at": row["observed_at"], "capabilities": caps}
    _validate(document)
    if hashlib.sha256(_json(document).encode()).hexdigest() != row["content_hash"]:
        raise SnapshotStoreError("Snapshot content hash mismatch")
    return {**dict(row), "capabilities": caps}


def get_snapshot(path: Path, snapshot_id: str):
    """Read one explicit observation; no Arena or canonical database access."""
    with _connection(path) as con:
        return _read(con, snapshot_id)


def list_snapshots(path: Path):
    """List sanitized observations in local insertion order, never account order."""
    with _connection(path) as con:
        ids = [r[0] for r in con.execute("SELECT snapshot_id FROM arena_snapshots ORDER BY sequence")]
        return [summarize(_read(con, sid)) for sid in ids]


def summarize(snapshot, *, details=False):
    """Whitelist-only CLI output: never emit account/deck/request identifiers or names."""
    observed = _time(snapshot["observed_at"])
    age = None if observed is None else (datetime.now(timezone.utc) - observed).total_seconds()
    result = {k: snapshot[k] for k in ("snapshot_id", "sequence", "observed_at", "persisted_at")}
    result.update(observation_age_seconds=None if age is None else max(0, age),
                  time_state="unknown" if age is None else "future_timestamp" if age < 0 else "age_only",
                  account_identity="unknown", source="Arena Player.log", source_event="StartHook",
                  capabilities={name: {k: snapshot["capabilities"][name][k]
                                      for k in ("status", "completeness")} for name in CAPABILITIES})
    if details:
        result["inventory"] = snapshot["capabilities"]["inventory"]["data"]
        decks = snapshot["capabilities"]["decks"]["data"]
        result["saved_deck_count"] = None if decks is None else len(decks)
        result["zone_counts"] = None if decks is None else {
            z: {"observed": sum(d[z] is not None for d in decks),
                "nonempty": sum(bool(d[z]) for d in decks)} for z in ZONES}
    return result
