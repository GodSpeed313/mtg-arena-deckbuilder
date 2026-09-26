from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from mtgadb import canonical, managed_deck_store as store, snapshot_store
from mtgadb.deck_identity import build_deck_snapshot_identity
from mtgadb.model import Deck


class ManagedDeckStoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "managed.db"
        self.meta = store.initialize_store(self.path)
        self.deck = Deck(deck_id="external-id", name="source", main={901: 2},
                         sideboard={902: 3}, commander={903: 1})

    def create(self):
        return store.create_managed_deck(self.path, self.deck, "My deck", "Standard")

    def error(self, code, action):
        with self.assertRaises(store.ManagedDeckStoreError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def sql(self, statement, args=(), *, ignore_checks=False):
        with closing(sqlite3.connect(self.path)) as con, con:
            if ignore_checks:
                con.execute("PRAGMA ignore_check_constraints=ON")
            con.execute(statement, args)

    def test_initialize_reopen_and_new_store_identities(self):
        self.assertEqual(store.open_store(self.path), self.meta)
        self.assertEqual(store.initialize_store(self.path), self.meta)
        other = store.initialize_store(self.root / "other.db")
        for field in ("store_id", "store_generation"):
            self.assertNotEqual(other[field], self.meta[field])
            self.assertEqual(len(self.meta[field]), 36)

    def test_missing_store_operations_do_not_create(self):
        path = self.root / "missing.db"
        for action in (lambda: store.open_store(path), lambda: store.list_destinations(path),
                       lambda: store.read_destination_state(path, str(uuid4())),
                       lambda: store.create_managed_deck(path, self.deck, "name")):
            self.error("missing_store", action)
            self.assertFalse(path.exists())

    def test_wrong_database_kinds_rejected_without_changes(self):
        for filename, creator in (("canonical.db", canonical.create),
                                  ("archive.db", snapshot_store.create_store)):
            path = self.root / filename
            result = creator(path)
            if isinstance(result, sqlite3.Connection):
                result.close()
            before = path.read_bytes()
            self.error("malformed_store", lambda: store.open_store(path))
            self.error("malformed_store", lambda: store.initialize_store(path))
            self.assertEqual(path.read_bytes(), before)

    def test_arbitrary_sqlite_and_non_sqlite_rejected(self):
        path = self.root / "arbitrary.db"
        with closing(sqlite3.connect(path)) as con, con:
            con.execute("CREATE TABLE other(x)")
        self.error("malformed_store", lambda: store.open_store(path))
        path.write_bytes(b"not a database" * 100)
        self.error("malformed_store", lambda: store.open_store(path))

    def test_unsupported_version(self):
        self.sql("UPDATE managed_store_meta SET schema_version='2'")
        before = self.path.read_bytes()
        self.error("unsupported_schema", lambda: store.open_store(self.path))
        self.error("unsupported_schema", lambda: self.create())
        self.assertEqual(self.path.read_bytes(), before)

    def test_malformed_store_metadata(self):
        for field, value in (("store_id", "bad"), ("store_generation", "bad"),
                             ("store_kind", "other")):
            original = self.meta[field]
            self.sql(f"UPDATE managed_store_meta SET {field}=?", (value,))
            self.error("malformed_store", lambda: store.open_store(self.path))
            self.sql(f"UPDATE managed_store_meta SET {field}=?", (original,))
        self.sql("DELETE FROM managed_store_meta")
        self.error("malformed_store", lambda: store.open_store(self.path))

    def test_closed_schema_rejects_extra_column_table_and_trigger(self):
        self.sql("ALTER TABLE managed_decks ADD COLUMN surprise TEXT")
        self.error("malformed_store", lambda: store.open_store(self.path))

    def test_create_identity_revision_metadata_and_all_zones(self):
        before = deepcopy(self.deck)
        state = self.create()
        self.assertEqual(self.deck, before)
        self.assertEqual(state["revision"], 1)
        self.assertNotEqual(state["record_id"], self.deck.deck_id)
        self.assertEqual(state["deck"].deck_id, "")
        self.assertEqual(state["metadata"], {"name": "My deck", "format_label": "Standard"})
        self.assertEqual(state["deck"].name, "My deck")
        for zone in ("main", "sideboard", "commander"):
            self.assertEqual(getattr(state["deck"], zone), getattr(self.deck, zone))
        self.assertEqual(state["gameplay_snapshot_identity"], build_deck_snapshot_identity(self.deck))
        self.assertEqual(store.read_destination_state(self.path, state["record_id"]), state)

    def test_each_zone_and_empty_deck(self):
        for zone in ("main", "sideboard", "commander"):
            deck = Deck(**{zone: {999999999: 1}})
            state = store.create_managed_deck(self.path, deck, "")
            self.assertEqual(state["deck"], deck)
        self.assertEqual(store.create_managed_deck(self.path, Deck(), "")["deck"], Deck())

    def test_identical_contents_and_names_are_distinct(self):
        a, b = self.create(), self.create()
        self.assertNotEqual(a["record_id"], b["record_id"])
        self.assertEqual(a["gameplay_snapshot_identity"], b["gameplay_snapshot_identity"])
        self.assertEqual(len(store.list_destinations(self.path)), 2)

    def test_caller_cannot_choose_identity(self):
        with self.assertRaises(TypeError):
            store.create_managed_deck(self.path, self.deck, "name", record_id=str(uuid4()))

    def test_invalid_quantities_and_ids_before_persistence(self):
        for bad in (0, -1, True, 1.0, "1", 2**63):
            for deck in (Deck(main={1: bad}), Deck(main={bad: 1})):
                self.error("invalid_input", lambda: store.create_managed_deck(self.path, deck, "name"))
        self.assertEqual(store.list_destinations(self.path), [])

    def test_invalid_metadata_and_deck(self):
        for action in (lambda: store.create_managed_deck(self.path, {}, "name"),
                       lambda: store.create_managed_deck(self.path, self.deck, True),
                       lambda: store.create_managed_deck(self.path, self.deck, "name", {}),
                       lambda: store.create_managed_deck(self.path, self.deck, "\ud800"),
                       lambda: store.create_managed_deck(self.path, Deck(main=[]), "name")):
            self.error("invalid_input", action)

    def test_revision_validation_on_read(self):
        state = self.create()
        for value in (0, -1, 1.5, "bad", float(2**63)):
            self.sql("UPDATE managed_decks SET revision=?", (value,), ignore_checks=True)
            self.error("malformed_record", lambda: store.read_destination_state(self.path, state["record_id"]))
        self.sql("UPDATE managed_decks SET revision=?", (2**63-1,))
        self.assertEqual(store.read_destination_state(self.path, state["record_id"])["revision"], 2**63-1)

    def test_missing_deleted_and_invalid_record_id(self):
        self.error("missing_record", lambda: store.read_destination_state(self.path, str(uuid4())))
        self.error("invalid_input", lambda: store.read_destination_state(self.path, "bad"))
        state = self.create()
        self.sql("UPDATE managed_decks SET lifecycle='deleted'")
        self.error("deleted_record", lambda: store.read_destination_state(self.path, state["record_id"]))
        self.assertEqual(store.list_destinations(self.path), [])

    def test_malformed_stored_uuid_detected_by_listing(self):
        self.create()
        self.sql("DELETE FROM managed_deck_cards")
        self.sql("UPDATE managed_decks SET record_id='bad'")
        self.error("malformed_record", lambda: store.list_destinations(self.path))

    def test_malformed_stored_rows(self):
        state = self.create()
        for column, value, original in (("zone", "unsupported", "main"),
                                        ("quantity", 0, 2), ("arena_id", -1, 901)):
            self.sql(f"UPDATE managed_deck_cards SET {column}=? WHERE zone='main'", (value,), ignore_checks=True)
            self.error("malformed_record", lambda: store.read_destination_state(self.path, state["record_id"]))
            self.sql(f"UPDATE managed_deck_cards SET {column}=? WHERE {column}=?", (original, value))

    def test_unique_rows_and_adapter_foreign_keys(self):
        state = self.create()
        with store._connection(self.path, write=True) as con:
            self.assertEqual(con.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("INSERT INTO managed_deck_cards VALUES (?,?,?,?)", (state["record_id"], "main", 901, 2))
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("INSERT INTO managed_deck_cards VALUES (?,?,?,?)", (str(uuid4()), "main", 1, 1))

    def test_external_orphan_rejected(self):
        self.sql("INSERT INTO managed_deck_cards VALUES (?,?,?,?)", (str(uuid4()), "main", 1, 1))
        self.error("malformed_store", lambda: store.open_store(self.path))

    def test_listing_sorted_summaries_and_stale_observation(self):
        a, b = self.create(), self.create()
        listing = store.list_destinations(self.path)
        self.assertEqual([x["record_id"] for x in listing], sorted([a["record_id"], b["record_id"]]))
        self.assertTrue(all("deck" not in x and "gameplay_snapshot_identity" not in x for x in listing))
        self.sql("UPDATE managed_decks SET name='New', revision=2")
        self.assertEqual(listing[0]["revision"], 1)
        self.assertEqual(store.read_destination_state(self.path, a["record_id"])["revision"], 2)

    def test_detached_values_and_read_only_storage(self):
        state = self.create()
        before = self.path.read_bytes()
        state["deck"].main[901] = 99
        state["metadata"]["name"] = "changed"
        read = store.read_destination_state(self.path, state["record_id"])
        self.assertEqual(read["deck"].main, {901: 2})
        store.list_destinations(self.path)
        store.open_store(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_failed_creation_rolls_back_header_and_cards(self):
        with patch.object(store, "_read", side_effect=store.ManagedDeckStoreError("malformed_record")):
            self.error("malformed_record", self.create)
        with closing(sqlite3.connect(self.path)) as con, con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM managed_decks").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM managed_deck_cards").fetchone()[0], 0)

    def test_failed_initialization_rolls_back_schema(self):
        path = self.root / "failed.db"
        with patch.object(store, "_SCHEMA", (*store._SCHEMA, "INVALID SQL")):
            self.error("unavailable_store", lambda: store.initialize_store(path))
        with closing(sqlite3.connect(path)) as con, con:
            self.assertEqual(con.execute("SELECT name FROM sqlite_master").fetchall(), [])

    def test_canonical_rebuild_and_archive_are_independent(self):
        state = self.create()
        path = self.root / "canonical.db"
        canonical.create(path).close()
        canonical.create(path).close()
        snapshot_store.create_store(self.root / "archive.db")
        self.assertEqual(store.read_destination_state(self.path, state["record_id"]), state)
        with self.assertRaises(snapshot_store.SnapshotStoreError):
            snapshot_store.create_store(self.path)

    def test_public_open_exposes_only_metadata(self):
        self.assertEqual(set(store.open_store(self.path)), {"store_kind", "schema_version", "store_id", "store_generation"})

    def test_verifier_native_serialized_detached_and_order(self):
        state = self.create()
        frozen = deepcopy(state)
        verified = store.require_destination_state(state)
        wire = store.serialize_destination_state(state)
        self.assertEqual(store.require_destination_state(json.loads(json.dumps(wire))), state)
        self.assertEqual(store.require_destination_state(dict(reversed(list(wire.items())))), state)
        verified["deck"].main.clear()
        self.assertEqual(state, frozen)
        reverse = Deck(main={2: 1, 1: 2})
        a = store.create_managed_deck(self.path, reverse, "name")
        b = store.create_managed_deck(self.path, Deck(main={1: 2, 2: 1}), "name")
        self.assertEqual(a["gameplay_snapshot_identity"], b["gameplay_snapshot_identity"])

    def test_verifier_rejects_identity_and_type_confusion(self):
        wire = store.serialize_destination_state(self.create())
        for key, value in (("revision", True), ("revision", 1.0), ("revision", "1"),
                           ("revision", 2**63), ("record_id", "bad"),
                           ("destination_state_version", 1), ("surprise", True)):
            changed = deepcopy(wire)
            changed[key] = value
            self.error("invalid_input", lambda: store.require_destination_state(changed))
        for path, value in ((("deck", "main"), [[901, True]]),
                            (("deck", "unexpected"), []),
                            (("deck", "deck_id"), wire["record_id"]),
                            (("gameplay_snapshot_identity", "digest"), "0"*64),
                            (("metadata", "name"), "different")):
            changed = deepcopy(wire)
            changed[path[0]][path[1]] = value
            self.error("invalid_input", lambda: store.require_destination_state(changed))

    def test_verifier_is_historical_and_no_io(self):
        state = self.create()
        self.sql("UPDATE managed_decks SET revision=2")
        with patch("sqlite3.connect", side_effect=AssertionError("database access")):
            self.assertEqual(store.require_destination_state(state), state)

    def test_unavailable_store(self):
        self.error("unavailable_store", lambda: store.open_store(self.root))

    def test_coherent_read_during_concurrent_write(self):
        state = self.create()
        self.sql("PRAGMA journal_mode=WAL")
        original = store._schema

        def change_after_snapshot(con):
            meta = original(con)
            with closing(sqlite3.connect(self.path)) as writer, writer:
                writer.execute("UPDATE managed_decks SET name='Later', revision=2")
                writer.execute("UPDATE managed_deck_cards SET quantity=9")
            return meta

        with patch.object(store, "_schema", side_effect=change_after_snapshot):
            observed = store.read_destination_state(self.path, state["record_id"])
        self.assertEqual(observed, state)
        current = store.read_destination_state(self.path, state["record_id"])
        self.assertEqual(current["revision"], 2)
        self.assertEqual(current["deck"].main, {901: 9})

    def test_commit_failure_never_reports_success(self):
        original = sqlite3.connect

        class FailingCommit(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError("synthetic commit failure")

        with patch.object(store.sqlite3, "connect", side_effect=lambda *a, **kw:
                          original(*a, factory=FailingCommit, **kw)):
            self.error("unavailable_store", self.create)
        self.assertEqual(store.list_destinations(self.path), [])

    def test_extra_trigger_and_table_rejected(self):
        for statement in ("CREATE TABLE extra(x)", "CREATE TABLE sqliteXextra(x)",
                          "CREATE TRIGGER extra AFTER INSERT ON managed_decks BEGIN SELECT 1; END"):
            path = self.root / (str(uuid4()) + ".db")
            store.initialize_store(path)
            with closing(sqlite3.connect(path)) as con, con:
                con.execute(statement)
            self.error("malformed_store", lambda: store.open_store(path))

    def test_malformed_lifecycle_and_metadata(self):
        state = self.create()
        self.sql("UPDATE managed_decks SET lifecycle='other'", ignore_checks=True)
        self.error("malformed_record", lambda: store.read_destination_state(self.path, state["record_id"]))
        self.sql("UPDATE managed_decks SET lifecycle='live', name=?", (b"blob",))
        self.error("malformed_record", lambda: store.read_destination_state(self.path, state["record_id"]))

    def test_invalid_paths(self):
        for path in (None, {}, ""):
            self.error("invalid_input", lambda: store.open_store(path))

    def test_verifier_closed_shapes_and_recomputed_contents(self):
        state = store.serialize_destination_state(self.create())
        for key in state:
            changed = deepcopy(state)
            del changed[key]
            self.error("invalid_input", lambda: store.require_destination_state(changed))
        changed = deepcopy(state)
        changed["deck"]["main"] = [[901, 8]]
        self.error("invalid_input", lambda: store.require_destination_state(changed))
        changed = deepcopy(state)
        changed["deck"]["main"] *= 2
        self.error("invalid_input", lambda: store.require_destination_state(changed))


if __name__ == "__main__":
    unittest.main()
