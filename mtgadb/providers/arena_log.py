"""Read-only, event-scoped Arena snapshots; no ownership inference or persistence.

Snapshot records deliberately differ from Inventory/Deck: unknown balances and
omitted zones cannot safely enter their zero/empty-default consumer contracts.
ProviderResult and Diagnostics retain the existing capability-result convention.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
from typing import Any

from arena_log import iter_json_objects
from mtgadb.model import Collection, Diagnostics, ProviderResult, Status


@dataclass(frozen=True)
class ArenaInventorySnapshot:
    wildcards: dict[str, int | None]
    gold: int | None
    gems: int | None
    vault_progress: int | None  # Raw TotalVaultProgress, not a percentage.
    wildcard_track_position: int | None


@dataclass(frozen=True)
class ArenaDeckSnapshot:
    deck_id: str
    name: str | None
    summary: dict[str, Any] | None
    attributes: list[dict[str, str]] | None
    format: str | None
    main: list[dict[str, int]] | None
    sideboard: list[dict[str, int]] | None
    commander: list[dict[str, int]] | None
    companions: list[dict[str, int]] | None
    card_skins: list[dict[str, Any]] | None
    record_present: bool
    # {} with record_present=True is distinct from a missing record.
    record_fields: tuple[str, ...]


_WILDCARDS = {
    "WildCardCommons": "common", "WildCardUnCommons": "uncommon",
    "WildCardRares": "rare", "WildCardMythics": "mythic",
}
_BALANCES = {
    "Gold": "gold", "Gems": "gems", "TotalVaultProgress": "vault_progress",
    "WcTrackPosition": "wildcard_track_position",
}
_ZONES = {
    "MainDeck": "main", "Sideboard": "sideboard",
    "CommandZone": "commander", "Companions": "companions",
}
# Anchor to a received event, never a JSON key or an outgoing request string.
_START = re.compile(
    r"^(?:\[UnityCrossThreadLogger\])?[ \t]*<==[ \t]+StartHook"
    r"\([^\r\n)]*\)[ \t]*", re.MULTILINE,
)
_BOUNDARY = re.compile(r"^(?:\[UnityCrossThreadLogger\]|[ \t]*(?:<==|==>))", re.MULTILINE)


def _integer(obj: dict, key: str) -> int | None:
    if key not in obj:
        return None
    value = obj[key]
    if type(value) is not int or value < 0:
        raise ValueError(f"{key} must be a nonnegative integer when present")
    return value


def _zone(raw: dict, key: str) -> list[dict[str, int]] | None:
    if key not in raw:
        return None
    if not isinstance(raw[key], list):
        raise ValueError(f"{key} must be an array")
    for entry in raw[key]:
        if not isinstance(entry, dict):
            raise ValueError(f"{key} entries must be objects")
        card_id = _integer(entry, "cardId")
        quantity = _integer(entry, "quantity")
        if card_id is None or card_id == 0 or quantity is None:
            raise ValueError(f"{key} requires positive cardId and nonnegative quantity")
    # Repeated IDs are observed in real snapshots. Keep every row and its order;
    # aggregation is a later consumer policy, not evidence from the response.
    return deepcopy(raw[key])


class ArenaLogProvider:
    """One captured log read, with independently inspectable capabilities.

    Use from_text for fixtures. Optional database lookup only SELECTs printing
    IDs. Last received StartHook wins by file order, even if malformed: never
    silently reuse an older state or combine capabilities across responses.
    """

    def __init__(self, path: Path, *, database: sqlite3.Connection | None = None):
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            self._initialize("", database)
            self._failure = "log_read_failed"
            self._evidence["availability"] = "error"
        else:
            self._initialize(text, database)

    @classmethod
    def from_text(cls, text: str, *, database: sqlite3.Connection | None = None):
        instance = cls.__new__(cls)
        instance._initialize(text, database)
        return instance

    def _initialize(self, text: str, database: sqlite3.Connection | None):
        self._database = database
        self._state: dict[str, Any] | None = None
        self._failure: str | None = None
        self._evidence: dict[str, Any] = {
            "source": "Arena Player.log", "source_event": "StartHook",
            "authority": "server_response_snapshot", "observed_at": None,
            "timestamp_source": None, "availability": "unavailable",
            "completeness": "unknown", "selection": "last_received_in_file_order",
        }
        matches = list(_START.finditer(text))
        self._evidence["start_hook_responses"] = len(matches)
        if not matches:
            return
        match = matches[-1]
        self._evidence["event_line"] = text.count("\n", 0, match.start()) + 1
        end = _BOUNDARY.search(text, match.end())
        body = text[match.end():end.start() if end else len(text)].lstrip()
        try:
            # Reuse the root scanner, but require the actual top-level object.
            # Its general recovery of nested objects must not rescue a broken envelope.
            parsed = next(iter_json_objects(body), None)
        except (ValueError, RecursionError):
            parsed = None
        if parsed is None or parsed[1] != 0:
            self._failure = "malformed_StartHook_JSON"
            return
        self._state = parsed[0]
        self._evidence["availability"] = "available"
        timestamp = self._state.get("ServerTimeInternal")
        if timestamp is not None:
            try:
                observed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if observed.tzinfo is None:
                    raise ValueError("timezone required")
                self._evidence["observed_at"] = observed.astimezone(timezone.utc).isoformat()
                self._evidence["timestamp_source"] = "ServerTimeInternal"
            except (AttributeError, TypeError, ValueError, OverflowError):
                self._evidence["timestamp_issue"] = "invalid_ServerTimeInternal"

    def _result(self, data, status: Status, **evidence):
        detail = deepcopy(self._evidence)
        detail.update(evidence)
        if status is not Status.OK:
            detail["availability"] = "error" if status is Status.ERROR else "unavailable"
            detail["completeness"] = "unknown"
        return ProviderResult(data, Diagnostics("Arena Player.log", status, detail))

    def _unavailable(self):
        if self._failure:
            return self._result(None, Status.ERROR, error=self._failure)
        return self._result(None, Status.NOT_FOUND, reason="StartHook_not_found")

    def get_collection(self) -> ProviderResult[Collection]:
        return self._result(
            None, Status.UNSUPPORTED,
            reason="No confirmed ownership payload is supported; deck lists are not ownership.",
            authority=None, observed_at=None, timestamp_source=None,
        )

    def get_inventory(self) -> ProviderResult[ArenaInventorySnapshot]:
        if self._state is None:
            return self._unavailable()
        if "InventoryInfo" not in self._state:
            return self._result(None, Status.NOT_FOUND, reason="InventoryInfo_absent")
        raw = self._state["InventoryInfo"]
        try:
            if not isinstance(raw, dict):
                raise ValueError("InventoryInfo must be an object")
            wildcards = {rarity: _integer(raw, field) for field, rarity in _WILDCARDS.items()}
            balances = {dest: _integer(raw, field) for field, dest in _BALANCES.items()}
            sequence = _integer(raw, "SeqId")
        except ValueError as exc:
            return self._result(None, Status.ERROR, error=str(exc))
        missing = [key for key in (*_WILDCARDS, *_BALANCES) if key not in raw]
        return self._result(
            ArenaInventorySnapshot(wildcards, **balances), Status.OK,
            completeness="partial" if missing else "complete", missing_fields=missing,
            sequence_id=sequence, vault_units="raw_Arena_value",
        )

    def get_decks(self) -> ProviderResult[list[ArenaDeckSnapshot]]:
        if self._state is None:
            return self._unavailable()
        if not any(k in self._state for k in ("DeckSummaries", "DecksInternal")):
            return self._result(None, Status.NOT_FOUND, reason="saved_deck_fields_absent")
        try:
            summaries = self._state.get("DeckSummaries", [])
            records = self._state.get("DecksInternal", {})
            if not isinstance(summaries, list) or not isinstance(records, dict):
                raise ValueError("DeckSummaries must be an array and DecksInternal an object")
            by_id = {}
            for summary in summaries:
                if not isinstance(summary, dict):
                    raise ValueError("deck summary must be an object")
                deck_id = summary.get("DeckIdInternal")
                if not isinstance(deck_id, str) or not deck_id:
                    raise ValueError("summary requires a nonempty DeckIdInternal")
                if deck_id in by_id:
                    raise ValueError("duplicate DeckIdInternal")
                by_id[deck_id] = summary
            if any(not isinstance(k, str) or not k for k in records):
                raise ValueError("DecksInternal keys must be nonempty strings")
            decks = []
            for deck_id in dict.fromkeys([*by_id, *records]):
                summary = by_id.get(deck_id)
                raw = records.get(deck_id, {})
                if not isinstance(raw, dict):
                    raise ValueError("deck record must be an object")
                name = summary.get("Name") if summary else None
                if name is not None and not isinstance(name, str):
                    raise ValueError("deck Name must be a string")
                attrs = summary.get("Attributes") if summary else None
                if attrs is not None and (
                    not isinstance(attrs, list) or any(
                        not isinstance(a, dict) or not isinstance(a.get("name"), str)
                        or ("value" in a and not isinstance(a["value"], str)) for a in attrs
                    )
                ):
                    raise ValueError("Attributes require string names and string values when present")
                formats = [a.get("value") for a in attrs or [] if a["name"] == "Format"]
                if len(formats) > 1:
                    raise ValueError("duplicate Format attributes")
                skins = raw.get("CardSkins")
                if "CardSkins" in raw and (
                    not isinstance(skins, list) or any(not isinstance(s, dict) for s in skins)
                ):
                    raise ValueError("CardSkins must be an array of objects")
                decks.append(ArenaDeckSnapshot(
                    deck_id=deck_id, name=name, summary=deepcopy(summary),
                    attributes=deepcopy(attrs), format=formats[0] if formats else None,
                    **{dest: _zone(raw, src) for src, dest in _ZONES.items()},
                    card_skins=deepcopy(skins), record_present=deck_id in records,
                    record_fields=tuple(raw),
                ))
            markers = {}
            for key in ("DeckSummariesCacheVersion", "DecksCacheVersion"):
                if key in self._state and type(self._state[key]) is not int:
                    raise ValueError(f"{key} must be an integer")
                markers[key] = self._state.get(key)
            ids = {entry["cardId"] for d in decks for zone in _ZONES.values()
                   for entry in (getattr(d, zone) or [])}
            unknown = None
            if self._database is not None:
                known = {r[0] for r in self._database.execute("SELECT arena_id FROM printings")}
                unknown = sorted(ids - known)
        except RecursionError:
            return self._result(None, Status.ERROR, error="deck_metadata_nesting_too_deep")
        except (ValueError, sqlite3.Error) as exc:
            message = str(exc) if isinstance(exc, ValueError) else "printing_database_lookup_failed"
            return self._result(None, Status.ERROR, error=message)
        complete = (
            "DeckSummaries" in self._state and "DecksInternal" in self._state
            and all(d.summary is not None and d.name is not None and d.record_present
                    and all(getattr(d, z) is not None for z in _ZONES.values()) for d in decks)
        )
        return self._result(
            decks, Status.OK, completeness="complete" if complete else "partial",
            completeness_scope="summary/name and explicit card zones, not a server completeness guarantee",
            cache_markers=markers, printing_ids_checked=unknown is not None,
            unknown_printing_ids=unknown,
            missing_summary_count=len(set(records) - set(by_id)),
            missing_record_count=len(set(by_id) - set(records)),
        )
