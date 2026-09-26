from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
from threading import Barrier
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


class ManagedDeckMutationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ManagedDeckStoreTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.path = self.fixture.path
        self.state = self.fixture.create()
        self.record_id = self.state["record_id"]

    def replace(self, expected=None, deck=None, **metadata):
        return store.conditional_replace(
            self.path, self.record_id, self.state if expected is None else expected,
            Deck(main={1001: 4}) if deck is None else deck,
            **({**self.state["metadata"], **metadata}))

    def delete(self, expected=None):
        return store.conditional_delete(self.path, self.record_id,
                                        self.state if expected is None else expected)

    def error(self, code, action):
        self.fixture.error(code, action)

    def read(self):
        return store.read_destination_state(self.path, self.record_id)

    def test_exact_replacement_all_zones_and_removal(self):
        replacement = Deck(main={10: 2}, sideboard={20: 3}, commander={30: 1})
        result = self.replace(deck=replacement)
        self.assertEqual(result["status"], "changed")
        state = result["destination_state"]
        self.assertEqual(state["revision"], 2)
        self.assertEqual(state["gameplay_snapshot_identity"], build_deck_snapshot_identity(replacement))
        self.assertEqual(store.require_destination_state(state), self.read())
        empty = self.replace(expected=state, deck=Deck())["destination_state"]
        self.assertEqual(empty["revision"], 3)
        self.assertEqual(empty["deck"].size(), 0)
        with closing(sqlite3.connect(self.path)) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM managed_deck_cards").fetchone()[0], 0)

    def test_individual_zone_replacement(self):
        current = self.state
        for zone in ("main", "sideboard", "commander"):
            replacement = Deck(**{zone: {77: 1}})
            current = self.replace(expected=current, deck=replacement)["destination_state"]
            self.assertEqual(current["gameplay_snapshot_identity"], build_deck_snapshot_identity(replacement))

    def test_metadata_changes_increment_and_null_clears(self):
        renamed = self.replace(deck=self.state["deck"], name="Renamed")["destination_state"]
        self.assertEqual(renamed["revision"], 2)
        cleared = self.replace(expected=renamed, deck=renamed["deck"], name="Renamed", format_label=None)["destination_state"]
        self.assertEqual(cleared["revision"], 3)
        self.assertIsNone(cleared["metadata"]["format_label"])
        self.assertEqual(cleared["gameplay_snapshot_identity"], self.state["gameplay_snapshot_identity"])

    def test_metadata_arguments_are_required(self):
        with self.assertRaises(TypeError):
            store.conditional_replace(self.path, self.record_id, self.state, Deck(), name="x")

    def test_noop_preserves_revision_and_storage(self):
        before = self.path.read_bytes()
        result = self.replace(deck=self.state["deck"])
        self.assertEqual(result, {"status": "unchanged", "destination_state": self.state})
        self.assertEqual(self.path.read_bytes(), before)

    def test_stale_noop_cannot_refresh_state(self):
        changed = self.replace()["destination_state"]
        self.error("revision_mismatch", lambda: self.replace(deck=changed["deck"]))
        self.error("revision_mismatch", self.delete)
        self.assertEqual(self.read(), changed)

    def test_changed_then_restored_rejects_old_revision(self):
        changed = self.replace()["destination_state"]
        restored = self.replace(expected=changed, deck=self.state["deck"])["destination_state"]
        self.assertEqual(restored["revision"], 3)
        self.assertEqual(restored["gameplay_snapshot_identity"], self.state["gameplay_snapshot_identity"])
        self.error("revision_mismatch", lambda: self.replace(deck=self.state["deck"]))

    def test_store_identity_and_generation_checked_for_both_operations(self):
        for key, code in (("store_id", "store_identity_mismatch"),
                          ("store_generation", "store_generation_mismatch")):
            expected = deepcopy(self.state)
            expected[key] = str(uuid4())
            self.error(code, lambda: self.replace(expected=expected))
            self.error(code, lambda: self.delete(expected=expected))
        self.assertEqual(self.read(), self.state)

    def test_other_record_same_gameplay_not_substituted(self):
        other = self.fixture.create()
        self.error("record_identity_mismatch", lambda: self.replace(expected=other))
        self.error("record_identity_mismatch", lambda: self.delete(expected=other))
        self.error("record_identity_mismatch", lambda: store.conditional_delete(self.path, str(uuid4()), self.state))

    def test_matching_missing_record_id_is_missing(self):
        expected = deepcopy(self.state)
        expected["record_id"] = str(uuid4())
        self.error("missing_record", lambda: store.conditional_delete(self.path, expected["record_id"], expected))

    def test_baseline_double_check_for_both_operations(self):
        self.fixture.sql("UPDATE managed_deck_cards SET quantity=8")
        before = self.read()
        self.error("baseline_mismatch", self.replace)
        self.error("baseline_mismatch", self.delete)
        self.assertEqual(self.read(), before)

    def test_metadata_double_check(self):
        self.fixture.sql("UPDATE managed_decks SET name='untracked change'")
        self.error("metadata_mismatch", self.replace)
        self.error("metadata_mismatch", self.delete)

    def test_malformed_and_tampered_expected(self):
        for key, value in (("revision", True), ("revision", 1.0), ("revision", "1"),
                           ("deck", Deck()), ("surprise", 1)):
            expected = deepcopy(self.state)
            expected[key] = value
            self.error("malformed_expected", lambda: self.replace(expected=expected))
            self.error("malformed_expected", lambda: self.delete(expected=expected))
        self.assertEqual(self.read(), self.state)

    def test_malformed_replacement_is_not_persisted(self):
        for deck in ({}, Deck(main={1: True}), Deck(main={True: 1}), Deck(main={1: 0}),
                     Deck(main={1: "1"}), Deck(main={1: 1.0})):
            self.error("malformed_replacement", lambda: self.replace(deck=deck))
        self.error("malformed_replacement", lambda: self.replace(name=True))
        self.error("malformed_replacement", lambda: self.replace(format_label={}))
        self.assertEqual(self.read(), self.state)

    def test_revision_exhaustion_change_delete_but_not_noop(self):
        self.fixture.sql("UPDATE managed_decks SET revision=?", (store.MAX_INTEGER,))
        state = self.read()
        self.error("revision_exhausted", lambda: self.replace(expected=state))
        self.error("revision_exhausted", lambda: self.delete(expected=state))
        self.assertEqual(self.replace(expected=state, deck=state["deck"])["status"], "unchanged")
        self.assertEqual(self.read(), state)

    def test_tombstone_retains_rows_and_identity_reservation(self):
        result = self.delete()
        self.assertEqual(result, {"status": "changed", "lifecycle": "deleted",
            "store_id": self.state["store_id"], "store_generation": self.state["store_generation"],
            "record_id": self.record_id, "previous_revision": 1, "revision": 2})
        self.error("deleted_record", self.read)
        self.error("deleted_record", self.replace)
        self.error("deleted_record", self.delete)
        self.assertEqual(store.list_destinations(self.path), [])
        with closing(sqlite3.connect(self.path)) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM managed_deck_cards").fetchone()[0], 3)
        with patch.object(store, "uuid4", return_value=self.record_id):
            self.error("unavailable_store", self.fixture.create)
        self.assertNotEqual(self.fixture.create()["record_id"], self.record_id)
        self.assertFalse(hasattr(store, "undelete"))

    def test_serialized_expected_and_detached_results(self):
        before = deepcopy(self.state)
        deck = Deck(deck_id="unrelated", main={6: 1})
        frozen = deepcopy(deck)
        result = self.replace(expected=store.serialize_destination_state(self.state), deck=deck)
        self.assertEqual(deck, frozen)
        self.assertEqual(self.state, before)
        result["destination_state"]["deck"].main.clear()
        self.assertEqual(self.read()["deck"].main, {6: 1})

    def test_store_generation_and_other_stores_unchanged(self):
        canonical_path = self.fixture.root / "canonical.db"
        archive_path = self.fixture.root / "archive.db"
        canonical.create(canonical_path).close()
        snapshot_store.create_store(archive_path)
        before = (canonical_path.read_bytes(), archive_path.read_bytes())
        state = self.replace()["destination_state"]
        self.delete(expected=state)
        self.assertEqual(store.open_store(self.path), self.fixture.meta)
        self.assertEqual((canonical_path.read_bytes(), archive_path.read_bytes()), before)

    def test_malformed_stored_state_fails_closed(self):
        self.fixture.sql("UPDATE managed_deck_cards SET quantity=0", ignore_checks=True)
        before = self.path.read_bytes()
        self.error("malformed_record", self.replace)
        self.error("malformed_record", self.delete)
        self.assertEqual(self.path.read_bytes(), before)

    def test_reread_failure_rolls_back_complete_replacement(self):
        original = store._read
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise sqlite3.OperationalError("synthetic read failure")
            return original(*args, **kwargs)

        with patch.object(store, "_read", side_effect=fail_second):
            self.error("transaction_failed", self.replace)
        self.assertEqual(self.read(), self.state)

    def test_result_mismatch_rolls_back(self):
        original = store._read
        calls = 0

        def wrong_result(*args, **kwargs):
            nonlocal calls
            calls += 1
            value = original(*args, **kwargs)
            if calls == 2:
                value["revision"] += 1
            return value

        with patch.object(store, "_read", side_effect=wrong_result):
            self.error("result_mismatch", self.replace)
        self.assertEqual(self.read(), self.state)

    def test_commit_failure_both_operations_roll_back(self):
        original = sqlite3.connect

        class FailingCommit(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError("synthetic failure")

        for operation in (self.replace, self.delete):
            with patch.object(store.sqlite3, "connect", side_effect=lambda *a, **kw:
                              original(*a, factory=FailingCommit, **kw)):
                self.error("commit_failed", operation)
            self.assertEqual(self.read(), self.state)

    def test_actual_concurrent_writers_only_one_succeeds(self):
        for index, journal in enumerate(("DELETE", "WAL")):
            self.fixture.sql("PRAGMA journal_mode=" + journal)
            expected = self.read()
            gate = Barrier(2)

            def writer(number):
                gate.wait(timeout=10)
                try:
                    return self.replace(expected=expected, deck=Deck(main={number: 1}))["status"]
                except store.ManagedDeckStoreError as exc:
                    return exc.code

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(writer, n + index * 100) for n in (40, 50)]
                outcomes = [future.result(timeout=15) for future in futures]
            self.assertCountEqual(outcomes, ["changed", "revision_mismatch"])
            self.assertEqual(self.read()["revision"], expected["revision"] + 1)

    def test_busy_storage_no_retry_or_partial_update(self):
        original = sqlite3.connect
        with closing(original(self.path, isolation_level=None)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            with patch.object(store.sqlite3, "connect", side_effect=lambda *a, **kw:
                              original(*a, timeout=0, **kw)):
                self.error("storage_busy", self.replace)
                self.error("storage_busy", self.delete)
            writer.rollback()
        self.assertEqual(self.read(), self.state)

    def test_foreign_keys_enabled_during_mutation(self):
        original = store._current_for_mutation

        def check(con, expected):
            self.assertEqual(con.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            return original(con, expected)

        with patch.object(store, "_current_for_mutation", side_effect=check):
            state = self.replace()["destination_state"]
            self.delete(expected=state)

    def test_partial_insert_failure_rolls_back_header_and_rows(self):
        original = sqlite3.connect

        class PartialInsert(sqlite3.Connection):
            def executemany(self, sql, parameters):
                rows = list(parameters)
                super().execute(sql, rows[0])
                raise sqlite3.OperationalError("synthetic partial insert")

        with patch.object(store.sqlite3, "connect", side_effect=lambda *a, **kw:
                          original(*a, factory=PartialInsert, **kw)):
            self.error("transaction_failed", lambda: self.replace(deck=Deck(main={5: 2, 6: 1}), name="New"))
        self.assertEqual(self.read(), self.state)

    def test_delete_verification_failure_rolls_back_tombstone(self):
        original = sqlite3.connect

        class DeleteReadFailure(sqlite3.Connection):
            deleted = False

            def execute(self, sql, parameters=()):
                if self.deleted and sql.startswith("SELECT"):
                    raise sqlite3.OperationalError("synthetic post-delete failure")
                result = super().execute(sql, parameters)
                if sql.startswith("UPDATE managed_decks SET lifecycle"):
                    self.deleted = True
                return result

        with patch.object(store.sqlite3, "connect", side_effect=lambda *a, **kw:
                          original(*a, factory=DeleteReadFailure, **kw)):
            self.error("transaction_failed", self.delete)
        self.assertEqual(self.read(), self.state)


if __name__ == "__main__":
    unittest.main()
