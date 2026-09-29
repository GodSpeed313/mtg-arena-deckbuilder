from contextlib import closing
from copy import deepcopy
import sqlite3
import unittest
from unittest.mock import patch

from mtgadb import managed_deck_store as store
from mtgadb.model import Deck
from tests import test_managed_deck_store as storage_tests


class ManagedStoreMigrationTests(unittest.TestCase):
    def setUp(self):
        self.fx = storage_tests.ManagedDeckStoreTests(methodName="runTest")
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.path = self.fx.path
        self.live = self.fx.create()
        self.deleted = store.create_managed_deck(self.path, Deck(main={1: 1}), "Deleted", None)
        store.conditional_delete(self.path, self.deleted["record_id"], self.deleted)

    def rows(self):
        with closing(sqlite3.connect(self.path)) as con:
            return ([tuple(r) for r in con.execute("SELECT * FROM managed_decks ORDER BY record_id")],
                    [tuple(r) for r in con.execute("SELECT * FROM managed_deck_cards ORDER BY record_id,zone,arena_id")])

    def error(self, code, action):
        self.fx.error(code, action)

    def test_exact_transition_preserves_all_rows_and_identities(self):
        before = self.rows()
        old = store.open_store(self.path)
        result = store.migrate_store_v1_to_v2(self.path)
        self.assertEqual(result, {**old, "schema_version": "2"})
        self.assertEqual(self.rows(), before)
        self.assertEqual(store.read_destination_state(self.path, self.live["record_id"]), self.live)
        self.error("deleted_record", lambda: store.read_destination_state(self.path, self.deleted["record_id"]))
        with closing(sqlite3.connect(self.path)) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM managed_application_operations").fetchone()[0], 0)

    def test_initialize_and_open_do_not_migrate(self):
        self.assertEqual(store.open_store(self.path)["schema_version"], "1")
        self.assertEqual(store.initialize_store(self.path)["schema_version"], "1")
        with closing(sqlite3.connect(self.path)) as con:
            self.assertIsNone(con.execute("SELECT name FROM sqlite_master WHERE name='managed_application_operations'").fetchone())
        store.migrate_store_v1_to_v2(self.path)
        self.assertEqual(store.initialize_store(self.path)["schema_version"], "2")

    def test_repeat_migration_is_rejected_without_changes(self):
        store.migrate_store_v1_to_v2(self.path)
        before = self.path.read_bytes()
        self.error("unsupported_schema", lambda: store.migrate_store_v1_to_v2(self.path))
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_and_unknown_schema_rejected(self):
        missing = self.fx.root / "absent.db"
        self.error("missing_store", lambda: store.migrate_store_v1_to_v2(missing))
        self.assertFalse(missing.exists())
        self.fx.sql("UPDATE managed_store_meta SET schema_version='999'")
        self.error("unsupported_schema", lambda: store.migrate_store_v1_to_v2(self.path))

    def test_malformed_v1_schema_and_orphans_rejected(self):
        self.fx.sql("CREATE TABLE unexpected(x)")
        self.error("malformed_store", lambda: store.migrate_store_v1_to_v2(self.path))
        self.fx.sql("DROP TABLE unexpected")
        self.fx.sql("INSERT INTO managed_deck_cards VALUES ('missing','main',3,1)")
        self.error("malformed_store", lambda: store.migrate_store_v1_to_v2(self.path))

    def test_all_records_validated_including_tombstones(self):
        for record in (self.live, self.deleted):
            self.fx.sql("UPDATE managed_deck_cards SET quantity=0 WHERE record_id=?", (record["record_id"],), ignore_checks=True)
            self.error("malformed_record", lambda: store.migrate_store_v1_to_v2(self.path))
            self.assertEqual(store.open_store(self.path)["schema_version"], "1")
            self.fx.sql("UPDATE managed_deck_cards SET quantity=1 WHERE record_id=?", (record["record_id"],))

    def test_result_verification_failure_rolls_back_ddl_and_version(self):
        original = store._migration_state
        calls = []
        def changed(*args):
            result = original(*args)
            calls.append(True)
            if len(calls) == 2:
                result["headers"] = []
            return result
        before = self.rows()
        with patch.object(store, "_migration_state", side_effect=changed):
            self.error("result_mismatch", lambda: store.migrate_store_v1_to_v2(self.path))
        self.assertEqual(store.open_store(self.path)["schema_version"], "1")
        self.assertEqual(self.rows(), before)

    def test_statement_failure_rolls_back_new_table(self):
        original = sqlite3.connect
        class Failing(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql.startswith("UPDATE managed_store_meta SET schema_version"):
                    raise sqlite3.OperationalError("private state")
                return super().execute(sql, parameters)
        with patch.object(store.sqlite3, "connect", side_effect=lambda *a, **kw: original(*a, factory=Failing, **kw)):
            self.error("transaction_failed", lambda: store.migrate_store_v1_to_v2(self.path))
        self.assertEqual(store.open_store(self.path)["schema_version"], "1")

    def test_commit_failure_rolls_back_migration(self):
        original = sqlite3.connect
        class Failing(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError("failure")
        with patch.object(store.sqlite3, "connect", side_effect=lambda *a, **kw: original(*a, factory=Failing, **kw)):
            self.error("commit_failed", lambda: store.migrate_store_v1_to_v2(self.path))
        self.assertEqual(store.open_store(self.path)["schema_version"], "1")

    def test_schema_v2_closed_and_cannot_be_enabled_by_version_alone(self):
        self.fx.sql("UPDATE managed_store_meta SET schema_version='2'")
        self.error("malformed_store", lambda: store.open_store(self.path))
        self.fx.sql("UPDATE managed_store_meta SET schema_version='1'")
        store.migrate_store_v1_to_v2(self.path)
        self.fx.sql("ALTER TABLE managed_application_operations ADD COLUMN extra TEXT")
        self.error("malformed_store", lambda: store.open_store(self.path))

    def test_v2_supports_unchanged_store_mutation_contracts(self):
        store.migrate_store_v1_to_v2(self.path)
        result = store.conditional_replace(self.path, self.live["record_id"], self.live,
                                           self.live["deck"], name=self.live["metadata"]["name"], format_label="Standard")
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(result["destination_state"], self.live)
        store.conditional_delete(self.path, self.live["record_id"], self.live)
        self.error("deleted_record", lambda: store.read_destination_state(self.path, self.live["record_id"]))

    def test_preserves_extreme_revision_and_unicode_nullable_metadata(self):
        self.fx.sql("UPDATE managed_decks SET revision=?,name=?,format_label=NULL", (store.MAX_INTEGER, "Ω"))
        before = self.rows()
        store.migrate_store_v1_to_v2(self.path)
        self.assertEqual(self.rows(), before)

    def test_commit_then_error_resolved_by_inspecting_schema_not_repeating_migration(self):
        before = self.rows()
        old = store.open_store(self.path)
        original = sqlite3.connect
        class After(sqlite3.Connection):
            def commit(self):
                super().commit()
                raise sqlite3.OperationalError("acknowledgment lost")
        with patch.object(store.sqlite3, "connect", side_effect=lambda *a, **kw: original(*a, factory=After, **kw)):
            self.error("commit_failed", lambda: store.migrate_store_v1_to_v2(self.path))
        self.assertEqual(store.open_store(self.path), {**old, "schema_version": "2"})
        self.assertEqual(self.rows(), before)
        self.error("unsupported_schema", lambda: store.migrate_store_v1_to_v2(self.path))

    def test_v2_schema_rejects_extra_trigger_and_mismatched_version(self):
        store.migrate_store_v1_to_v2(self.path)
        self.fx.sql("CREATE TRIGGER unexpected AFTER INSERT ON managed_application_operations BEGIN SELECT 1; END")
        self.error("malformed_store", lambda: store.open_store(self.path))
        self.fx.sql("DROP TRIGGER unexpected")
        self.fx.sql("UPDATE managed_store_meta SET schema_version='1'")
        self.error("malformed_store", lambda: store.open_store(self.path))


if __name__ == "__main__":
    unittest.main()
