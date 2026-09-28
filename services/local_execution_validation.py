"""Process-local, explicitly designated validation authority for #6O only.

The current state is the last successfully published complete context, not the
source file or the live Arena account. All participating updates use publish().
An authority owns its card projection and nested inputs; it exposes neither a
connection nor a public transaction callback. Restart creates a new identity.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import Lock
from uuid import uuid4

from mtgadb.model import Collection, Inventory, Format
from mtgadb.modes import OperatingMode
from services.proposal_presentation import canonical_validation_context
from services.validator import DeckRules


LOCAL_EXECUTION_VALIDATION_CONTEXT_VERSION = "1"
_ERRORS = frozenset({
    "invalid_intent", "invalid_execution_context", "execution_context_unavailable",
    "execution_context_conflict", "validation_unavailable", "fresh_validation_failed",
    "current_spend_required", "expected_result_mismatch",
})


class LocalDeckApplicationError(ValueError):
    """Closed execution failures; never include input, deck or SQL contents."""

    def __init__(self, code):
        if code not in _ERRORS:
            raise ValueError("Unsupported local application error code")
        self.code = code
        super().__init__(code)


def _fail(code):
    raise LocalDeckApplicationError(code) from None


def _encoded(value):
    def check(item):
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise ValueError("Non-string JSON key")
            for child in item.values():
                check(child)
        elif type(item) is list:
            for child in item:
                check(child)
        elif item is not None and type(item) not in (str, int, bool, float):
            raise ValueError("Non-JSON value")
    check(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _resource_inputs(collection, inventory, mode):
    if type(collection) is not Collection or type(collection.cards) is not dict or any(
        type(key) is not int or key <= 0 or type(count) is not int or count < 0
        for key, count in collection.cards.items()
    ):
        _fail("invalid_execution_context")
    if mode not in (OperatingMode.FULL_COLLECTION, OperatingMode.WILDCARD_BUDGET) or type(mode) is not OperatingMode:
        _fail("invalid_execution_context")
    if mode is OperatingMode.WILDCARD_BUDGET and inventory is None:
        _fail("invalid_execution_context")
    if inventory is not None and (type(inventory) is not Inventory or
        type(inventory.wildcards) is not dict or any(
            type(key) is not str or not key or type(count) is not int or count < 0
            for key, count in inventory.wildcards.items()
        ) or any(type(n) is not int or n < 0 for n in (
            inventory.gold, inventory.gems, inventory.vault_progress))):
        _fail("invalid_execution_context")
    # Source timestamps/references are not freshness evidence or authority.
    return Collection(deepcopy(collection.cards)), deepcopy(inventory)


def _load_cards(path):
    """Read a fresh coherent source transaction; own just validator columns."""
    if not isinstance(path, (str, Path)) or not str(path):
        _fail("invalid_execution_context")
    source = target = None
    try:
        source = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True,
                                 isolation_level=None)
        source.execute("BEGIN")
        cards = source.execute(
            "SELECT title_id,name,types,color_identity,rules_text FROM cards ORDER BY title_id"
        ).fetchall()
        printings = source.execute(
            "SELECT arena_id,title_id,set_code,collector_number,rarity FROM printings ORDER BY arena_id"
        ).fetchall()
        titles = set()
        for title_id, name, *texts in cards:
            if type(title_id) is not int or title_id <= 0 or title_id in titles or (
                type(name) is not str or not name
            ) or any(v is not None and type(v) is not str for v in texts):
                _fail("invalid_execution_context")
            titles.add(title_id)
        ids = set()
        for arena_id, title_id, *texts in printings:
            if type(arena_id) is not int or arena_id <= 0 or arena_id in ids or (
                type(title_id) is not int or title_id not in titles
            ) or any(v is not None and type(v) is not str for v in texts):
                _fail("invalid_execution_context")
            ids.add(arena_id)
        payload = {"cards": [list(row) for row in cards],
                   "printings": [list(row) for row in printings]}
        digest = hashlib.sha256(_encoded(payload)).hexdigest()
        target = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        target.row_factory = sqlite3.Row
        target.executescript(
            "CREATE TABLE cards(title_id INTEGER PRIMARY KEY,name TEXT,types TEXT,color_identity TEXT,rules_text TEXT);"
            "CREATE TABLE printings(arena_id INTEGER PRIMARY KEY,title_id INTEGER,set_code TEXT,collector_number TEXT,rarity TEXT);"
        )
        target.executemany("INSERT INTO cards VALUES (?,?,?,?,?)", cards)
        target.executemany("INSERT INTO printings VALUES (?,?,?,?,?)", printings)
        target.execute("PRAGMA query_only=ON")
        result, target = target, None
        return result, {"projection_version": "1", "digest_algorithm": "sha256", "digest": digest}
    except (sqlite3.Error, OSError):
        _fail("execution_context_unavailable")
    finally:
        if source is not None:
            source.close()
        if target is not None:
            target.close()


class LocalExecutionValidationAuthorityV1:
    """Designate one owned local state; publish complete replacements atomically.

    Publication inputs must be exclusively owned by the trusted publisher for
    the duration of publish(). Their origin/completeness cannot be authenticated
    here. Once published, caller aliases and source-file updates have no effect.
    A new publication is the only participating update, including card refresh.
    This authority is process-local; it never coordinates independent authorities.
    """

    def __init__(self, *, card_database_path, collection, format, rules, mode, inventory=None):
        self._lock = Lock()
        self._authority_id = str(uuid4())
        self._generation = 0
        self._state = None
        self._closed = False
        self.publish(card_database_path=card_database_path, collection=collection,
                     inventory=inventory, format=format, rules=rules, mode=mode)

    def publish(self, *, card_database_path, collection, format, rules, mode, inventory=None):
        """Replace all local authority inputs, incrementing generation once.

        Blocks behind executions through their managed-store commit. Failed
        publication leaves both the previous complete state and generation intact.
        Does not write the source card database or any managed deck.
        """
        with self._lock:
            if self._closed:
                _fail("execution_context_unavailable")
            con = None
            try:
                collection, inventory = _resource_inputs(collection, inventory, mode)
                if type(format) is not Format or type(rules) is not DeckRules:
                    _fail("invalid_execution_context")
                format, rules = deepcopy(format), deepcopy(rules)
                policy = canonical_validation_context(format=format, rules=rules, mode=mode)
                _encoded(policy)
                resources = {"collection": [[k, collection.cards[k]] for k in sorted(collection.cards)],
                             "inventory": None if inventory is None else asdict(inventory)}
                _encoded(resources)
                con, card_identity = _load_cards(card_database_path)
                state = {"con": con, "collection": collection, "inventory": inventory,
                         "format": format, "rules": rules, "mode": mode, "policy": policy,
                         "resources": resources, "card_database_identity": card_identity}
            except LocalDeckApplicationError:
                raise
            except (ValueError, TypeError, AttributeError, RecursionError, OverflowError):
                _fail("invalid_execution_context")
            old = self._state
            self._state = state
            self._generation += 1
            if old is not None:
                old["con"].close()
            return {"context_version": "1", "authority_id": self._authority_id,
                    "generation": self._generation}

    @contextmanager
    def _guarded(self):
        # Global order: authority lock -> managed-store BEGIN IMMEDIATE.
        # No user callback and no public connection/guard API.
        with self._lock:
            if self._closed or self._state is None:
                _fail("execution_context_unavailable")
            state = self._state
            context = {"context_version": "1", "authority_id": self._authority_id,
                       "generation": self._generation,
                       "card_database_identity": deepcopy(state["card_database_identity"]),
                       **deepcopy(state["resources"])}
            yield {"con": state["con"], "format": deepcopy(state["format"]),
                   "rules": deepcopy(state["rules"]), "mode": state["mode"],
                   "collection": deepcopy(state["collection"]),
                   "inventory": deepcopy(state["inventory"]),
                   "policy": deepcopy(state["policy"]), "context": context}

    def close(self):
        """Wait for any execution, then retire this authority permanently."""
        with self._lock:
            if not self._closed:
                self._closed = True
                if self._state is not None:
                    self._state["con"].close()
                    self._state = None
