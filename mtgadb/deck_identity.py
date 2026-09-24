"""Canonical gameplay-state fingerprint for a complete Deck value."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from mtgadb.model import Deck


DECK_SNAPSHOT_IDENTITY_VERSION = "1"
DECK_SNAPSHOT_DIGEST_ALGORITHM = "sha256"
_ZONES = ("main", "sideboard", "commander")


def _encoded(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def canonical_deck_payload(deck: Deck) -> dict:
    """Return sorted printing/quantity pairs; metadata and format are excluded."""
    if not isinstance(deck, Deck):
        raise ValueError("Deck snapshot identity requires a Deck")
    payload = {}
    for name in _ZONES:
        zone = getattr(deck, name)
        if not isinstance(zone, dict) or any(
            type(arena_id) is not int or arena_id <= 0
            or type(quantity) is not int or quantity <= 0
            for arena_id, quantity in zone.items()
        ):
            raise ValueError("Deck snapshot zones require positive integer printing IDs and quantities")
        payload[name] = [[arena_id, zone[arena_id]] for arena_id in sorted(zone)]
    return payload


def build_deck_snapshot_identity(deck: Deck) -> dict:
    payload = canonical_deck_payload(deck)
    return {
        "deck_snapshot_identity_version": DECK_SNAPSHOT_IDENTITY_VERSION,
        "digest_algorithm": DECK_SNAPSHOT_DIGEST_ALGORITHM,
        "canonical_payload": payload,
        "digest": hashlib.sha256(_encoded(payload)).hexdigest(),
    }


def require_deck_snapshot_identity(value: dict) -> dict:
    """Reject stale, malformed, noncanonical, or unsupported identity records."""
    if not isinstance(value, dict) or set(value) != {
        "deck_snapshot_identity_version", "digest_algorithm", "canonical_payload", "digest",
    } or value["deck_snapshot_identity_version"] != DECK_SNAPSHOT_IDENTITY_VERSION or (
        value["digest_algorithm"] != DECK_SNAPSHOT_DIGEST_ALGORITHM
    ):
        raise ValueError("Deck Snapshot Identity Version 1 is required")
    payload = value["canonical_payload"]
    if not isinstance(payload, dict) or set(payload) != set(_ZONES):
        raise ValueError("Deck snapshot payload is malformed")
    for name in _ZONES:
        entries = payload[name]
        if not isinstance(entries, list) or any(
            not isinstance(entry, list) or len(entry) != 2
            or type(entry[0]) is not int or entry[0] <= 0
            or type(entry[1]) is not int or entry[1] <= 0
            for entry in entries
        ) or entries != sorted(entries, key=lambda entry: entry[0]) or (
            len({entry[0] for entry in entries}) != len(entries)
        ):
            raise ValueError("Deck snapshot payload is not canonical")
    if type(value["digest"]) is not str or value["digest"] != hashlib.sha256(
        _encoded(payload)
    ).hexdigest():
        raise ValueError("Deck snapshot digest contradicts its payload")
    return deepcopy(value)
