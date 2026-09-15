from __future__ import annotations

from contextlib import closing, redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID

from mtgadb import canonical, snapshot_store as store
from mtgadb.providers.arena_log import ArenaLogProvider
from tests.test_arena_log import STATE, TEXT, response
from workbench import main


class SnapshotStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "arena_snapshots.db"

    def save(self, state=None, *, text=None):
        source = text if text is not None else response(STATE if state is None else state)
        return store.save_snapshot(self.path, ArenaLogProvider.from_text(source))

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_create_and_version_store(self):
        store.create_store(self.path)
        with closing(sqlite3.connect(self.path)) as con:
            self.assertEqual(con.execute("SELECT value FROM store_meta WHERE key='schema_version'").fetchone()[0], "1")
        self.assertEqual(store.list_snapshots(self.path), [])
        before = self.path.read_bytes()
        store.create_store(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_generated_identity_and_exactly_three_capabilities(self):
        saved = self.save()
        self.assertEqual(str(UUID(saved["snapshot_id"])), saved["snapshot_id"])
        self.assertEqual(saved["sequence"], 1)
        self.assertIsNone(saved["account_key"])
        with closing(sqlite3.connect(self.path)) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM arena_snapshot_capabilities").fetchone()[0], 3)
            self.assertIsNone(con.execute("SELECT data_json FROM arena_snapshot_capabilities WHERE capability='collection'").fetchone()[0])
        self.assertEqual(store.get_snapshot(self.path, saved["snapshot_id"]), saved)

    def test_repeated_saves_have_distinct_ids_and_same_content_hash(self):
        first, second = self.save(), self.save()
        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(first["content_hash"], second["content_hash"])
        self.assertEqual([s["sequence"] for s in store.list_snapshots(self.path)], [1, 2])
        self.assertEqual(store.get_snapshot(self.path, first["snapshot_id"]), first)

    def test_overlapping_deck_ids_never_group_accounts(self):
        first = self.save()
        another = deepcopy(STATE)
        another["AccountId"] = "fake-other-account"
        another["DeckSummaries"][0]["Name"] = "Synthetic Other Account"
        another["InventoryInfo"]["Gold"] = 0
        second = self.save(another)
        self.assertEqual(first["capabilities"]["decks"]["data"][0]["deck_id"], second["capabilities"]["decks"]["data"][0]["deck_id"])
        self.assertIsNone(second["account_key"])
        self.assertEqual(store.get_snapshot(self.path, first["snapshot_id"])["capabilities"]["inventory"]["data"]["gold"], 2400)
        self.assertEqual(store.get_snapshot(self.path, second["snapshot_id"])["capabilities"]["inventory"]["data"]["gold"], 0)

    def test_complete_empty_snapshot_does_not_delete_older_decks(self):
        first = self.save()
        newer = deepcopy(STATE)
        newer.update(DeckSummaries=[], DecksInternal={})
        second = self.save(newer)
        self.assertEqual(second["capabilities"]["decks"]["completeness"], "complete")
        self.assertEqual(second["capabilities"]["decks"]["data"], [])
        self.assertEqual(len(store.get_snapshot(self.path, first["snapshot_id"])["capabilities"]["decks"]["data"]), 3)

    def test_partial_snapshot_never_fills_holes(self):
        first = self.save()
        second = self.save({"InventoryInfo": {"Gold": 0}, "DeckSummaries": []})
        self.assertIsNone(second["capabilities"]["inventory"]["data"]["gems"])
        self.assertEqual(second["capabilities"]["decks"]["completeness"], "partial")
        self.assertEqual(second["capabilities"]["decks"]["data"], [])
        self.assertEqual(store.get_snapshot(self.path, first["snapshot_id"]), first)

    def test_null_zero_and_positive_inventory_round_trip(self):
        state = deepcopy(STATE)
        state["InventoryInfo"] = {"Gold": 0, "WildCardCommons": 0, "Gems": 7}
        saved = self.save(state)
        data = store.get_snapshot(self.path, saved["snapshot_id"])["capabilities"]["inventory"]["data"]
        self.assertEqual(data["gold"], 0)
        self.assertEqual(data["gems"], 7)
        self.assertEqual(data["wildcards"]["common"], 0)
        self.assertTrue(all(data["wildcards"][k] is None for k in ("rare", "uncommon", "mythic")))
        self.assertIsNone(data["vault_progress"])
        self.assertIsNone(data["wildcard_track_position"])

    def test_all_inventory_fields_round_trip(self):
        saved = self.save()
        inv = saved["capabilities"]["inventory"]["data"]
        self.assertEqual(inv["wildcards"], {"common":29,"uncommon":29,"rare":16,"mythic":17})
        self.assertEqual([inv[k] for k in store.BALANCES], [2400,250,621,1])

    def test_missing_and_empty_zones_and_empty_record(self):
        saved = self.save()
        decks = store.get_snapshot(self.path, saved["snapshot_id"])["capabilities"]["decks"]["data"]
        for zone in store.ZONES:
            self.assertIsNone(decks[1][zone])
            self.assertEqual(decks[2][zone], [])
        self.assertTrue(decks[1]["record_present"])
        self.assertEqual(decks[1]["record_fields"], [])

    def test_absent_record_remains_absent(self):
        state = deepcopy(STATE)
        del state["DecksInternal"]["fake-deck-empty"]
        saved = self.save(state)
        self.assertFalse(saved["capabilities"]["decks"]["data"][1]["record_present"])

    def test_ordered_duplicate_card_entries_preserved(self):
        state = deepcopy(STATE)
        rows = state["DecksInternal"]["fake-deck-alpha"]["MainDeck"]
        rows.append(deepcopy(rows[0]))
        saved = self.save(state)
        self.assertEqual(store.get_snapshot(self.path,saved["snapshot_id"])["capabilities"]["decks"]["data"][0]["main"], rows)

    def test_collection_unsupported_null_round_trip(self):
        saved = self.save()
        coll = store.get_snapshot(self.path,saved["snapshot_id"])["capabilities"]["collection"]
        self.assertEqual(coll["status"], "unsupported")
        self.assertEqual(coll["completeness"], "unknown")
        self.assertIsNone(coll["data"])
        self.assertEqual(coll["evidence"]["availability"], "unavailable")

    def test_valid_inventory_and_deck_error_commit_together(self):
        state = deepcopy(STATE)
        state["DecksInternal"] = None
        saved = self.save(state)
        read = store.get_snapshot(self.path,saved["snapshot_id"])
        self.assertEqual(read["capabilities"]["inventory"]["status"], "ok")
        self.assertEqual(read["capabilities"]["decks"]["status"], "error")
        self.assertIsNone(read["capabilities"]["decks"]["data"])

    def test_deep_metadata_error_archived_without_raw_metadata(self):
        state = deepcopy(STATE)
        state["DeckSummaries"][0]["Extra"] = "placeholder"
        source = response(state).replace('"placeholder"', '['*600+'0'+']'*600)
        saved = self.save(text=source)
        self.assertEqual(saved["capabilities"]["decks"]["status"], "error")
        self.assertEqual(saved["capabilities"]["inventory"]["status"], "ok")

    def test_unavailable_capabilities_are_not_previous_successes(self):
        first = self.save()
        newer = self.save({})
        for name in ("inventory", "decks"):
            self.assertEqual(newer["capabilities"][name]["status"], "not_found")
            self.assertIsNone(newer["capabilities"][name]["data"])
        self.assertEqual(store.get_snapshot(self.path, first["snapshot_id"]), first)

    def test_malformed_selected_response_can_archive_errors(self):
        saved = self.save(text='<== StartHook(fake)\n{"InventoryInfo":')
        self.assertIsNone(saved["observed_at"])
        self.assertTrue(all(saved["capabilities"][k]["status"] == "error" for k in ("inventory", "decks")))

    def test_no_selected_observation_does_not_create_store(self):
        with self.assertRaises(ValueError):
            self.save(text="unrelated log")
        self.assertFalse(self.path.exists())

    def test_no_cross_start_hook_merging(self):
        newer = {"InventoryInfo": {"Gold": 0}}
        saved = self.save(text=response(STATE)+response(newer))
        self.assertEqual(saved["capabilities"]["inventory"]["data"]["gold"], 0)
        self.assertIsNone(saved["capabilities"]["inventory"]["data"]["gems"])
        self.assertEqual(saved["capabilities"]["decks"]["status"], "not_found")

    def test_older_arrival_not_promoted_or_reordered(self):
        first = self.save()
        state = deepcopy(STATE)
        state["ServerTimeInternal"] = "2000-01-01T00:00:00Z"
        second = self.save(state)
        listing = store.list_snapshots(self.path)
        self.assertEqual([r["snapshot_id"] for r in listing], [first["snapshot_id"],second["snapshot_id"]])
        self.assertEqual(listing[1]["observed_at"], "2000-01-01T00:00:00+00:00")
        self.assertTrue(all("current" not in r for r in listing))

    def test_observation_and_persistence_time_are_distinct(self):
        saved = self.save()
        self.assertEqual(saved["observed_at"], "2026-09-14T00:08:29.703840+00:00")
        self.assertNotEqual(saved["observed_at"], saved["persisted_at"])

    def test_unknown_and_future_observation_time(self):
        state = deepcopy(STATE)
        state.pop("ServerTimeInternal")
        unknown = store.summarize(self.save(state))
        self.assertIsNone(unknown["observed_at"])
        self.assertIsNone(unknown["observation_age_seconds"])
        self.assertEqual(unknown["time_state"], "unknown")
        state["ServerTimeInternal"] = "2999-01-01T00:00:00Z"
        self.assertEqual(store.summarize(self.save(state))["time_state"], "future_timestamp")

    def test_transaction_failure_rolls_back_entire_new_observation(self):
        first = self.save()
        with closing(sqlite3.connect(self.path)) as con:
            con.execute("""CREATE TRIGGER fail_decks BEFORE INSERT ON arena_snapshot_capabilities
                WHEN NEW.capability='decks' BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END""")
            con.commit()
        before = self.path.read_bytes()
        with self.assertRaises(store.SnapshotStoreError):
            self.save()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(store.get_snapshot(self.path,first["snapshot_id"]), first)
        self.assertEqual(len(store.list_snapshots(self.path)), 1)

    def test_failed_schema_creation_rolls_back(self):
        with patch.object(store, "SCHEMA", (*store.SCHEMA, "INVALID SQL")):
            with self.assertRaises(store.SnapshotStoreError):
                self.save()
        with closing(sqlite3.connect(self.path)) as con:
            self.assertEqual(con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [])

    def test_future_store_version_rejected_without_changes(self):
        self.save()
        with closing(sqlite3.connect(self.path)) as con:
            con.execute("UPDATE store_meta SET value='999' WHERE key='schema_version'")
            con.commit()
        before = self.path.read_bytes()
        for operation in (lambda:store.list_snapshots(self.path),lambda:self.save(),lambda:store.create_store(self.path)):
            with self.assertRaisesRegex(store.SnapshotStoreError, "Unsupported"):
                operation()
        self.assertEqual(self.path.read_bytes(),before)

    def test_future_payload_version_rejected(self):
        saved = self.save()
        with closing(sqlite3.connect(self.path)) as con:
            con.execute("UPDATE arena_snapshots SET payload_version=999")
            con.commit()
        with self.assertRaisesRegex(store.SnapshotStoreError,"Unsupported"):
            store.get_snapshot(self.path,saved["snapshot_id"])

    def test_reading_missing_store_never_creates_it(self):
        for operation in (lambda:store.list_snapshots(self.path),lambda:store.get_snapshot(self.path,"fake-id")):
            with self.assertRaises(store.SnapshotStoreError):
                operation()
        self.assertFalse(self.path.exists())

    def test_unknown_snapshot_id_returns_safe_error(self):
        self.save()
        with self.assertRaisesRegex(store.SnapshotStoreError,"Snapshot not found"):
            store.get_snapshot(self.path,"fake-private-deck-id")

    def test_malformed_bundle_rejected_before_opening_database(self):
        provider = ArenaLogProvider.from_text(response({"InventoryInfo":{"Gold":0}}))
        bundle = provider.get_snapshot()
        bundle.inventory.diagnostics.evidence["completeness"] = "complete"
        with patch.object(provider,"get_snapshot",return_value=bundle):
            with self.assertRaises(store.SnapshotStoreError):
                store.save_snapshot(self.path,provider)
        self.assertFalse(self.path.exists())

    def test_mismatched_capability_times_rejected(self):
        provider = ArenaLogProvider.from_text(TEXT)
        bundle = provider.get_snapshot()
        bundle.decks.diagnostics.evidence["observed_at"] = "2000-01-01T00:00:00+00:00"
        with patch.object(provider,"get_snapshot",return_value=bundle):
            with self.assertRaises(store.SnapshotStoreError):
                store.save_snapshot(self.path,provider)
        self.assertFalse(self.path.exists())

    def test_content_tampering_detected(self):
        saved = self.save()
        with closing(sqlite3.connect(self.path)) as con:
            raw = con.execute("SELECT data_json FROM arena_snapshot_capabilities WHERE capability='inventory'").fetchone()[0]
            data = json.loads(raw);data["gold"] = 999
            con.execute("UPDATE arena_snapshot_capabilities SET data_json=? WHERE capability='inventory'",(json.dumps(data),))
            con.commit()
        with self.assertRaisesRegex(store.SnapshotStoreError,"hash mismatch"):
            store.get_snapshot(self.path,saved["snapshot_id"])

    def test_no_raw_log_or_unreviewed_identity_fields_persisted(self):
        state = deepcopy(STATE)
        secret = "fake-sensitive-value-not-for-archive"
        state["AccountId"] = secret
        state["Token"] = secret
        state["DeckSummaries"][0]["AccountId"] = secret
        state["DeckSummaries"][0]["Attributes"].append({"name":"AccountId","value":secret})
        state["DecksInternal"]["fake-deck-alpha"]["MainDeck"][0]["RequestId"] = secret
        saved = self.save(state)
        encoded = json.dumps(store.get_snapshot(self.path,saved["snapshot_id"]))
        self.assertNotIn(secret,encoded)
        self.assertNotIn(secret.encode(),self.path.read_bytes())
        self.assertNotIn("fake-request",encoded)
        self.assertIn("fake-deck-alpha",encoded)  # Necessary data, not CLI output.

    def test_returned_data_mutation_does_not_change_archive(self):
        saved = self.save()
        read = store.get_snapshot(self.path,saved["snapshot_id"])
        read["capabilities"]["decks"]["data"].clear()
        self.assertEqual(store.get_snapshot(self.path,saved["snapshot_id"]),saved)

    def test_cli_save_show_list_without_source_or_canonical_database(self):
        log = self.root / "synthetic.log"
        log.write_text(TEXT,encoding="utf-8")
        missing_db = self.root / "absent-card-db.db"
        args = ["--database",str(missing_db)]
        code,out,err = self.cli(*args,"save-arena",str(log),"--store",str(self.path))
        self.assertEqual((code,err),(0,""))
        saved = json.loads(out)
        self.assertTrue(saved["persisted"])
        self.assertEqual(saved["saved_deck_count"],3)
        log.unlink()
        before = self.path.read_bytes()
        code,show,err = self.cli(*args,"show-arena-snapshot",saved["snapshot_id"],"--store",str(self.path))
        self.assertEqual((code,err),(0,""))
        self.assertEqual(json.loads(show)["inventory"]["gold"],2400)
        code,listing,err = self.cli(*args,"list-arena-snapshots","--store",str(self.path))
        self.assertEqual((code,err),(0,""))
        self.assertEqual(len(json.loads(listing)["snapshots"]),1)
        for text in (out,show,listing):
            self.assertNotIn("fake-deck",text)
            self.assertNotIn("Synthetic Alpha",text)
            self.assertNotIn("fake-request",text)
            self.assertNotIn("account_key",text)
        self.assertEqual(self.path.read_bytes(),before)
        self.assertFalse(missing_db.exists())

    def test_cli_missing_archive_reports_no_snapshots_without_creating(self):
        code,out,err = self.cli("list-arena-snapshots","--store",str(self.path))
        self.assertEqual((code,err),(0,""))
        self.assertEqual(json.loads(out),{"store_exists":False,"snapshots":[]})
        self.assertFalse(self.path.exists())

    def test_cli_saved_capability_error_is_honest(self):
        log = self.root / "synthetic.log"
        log.write_text(response({"InventoryInfo":{"Gold":0},"DecksInternal":None}),encoding="utf-8")
        code,out,err = self.cli("save-arena",str(log),"--store",str(self.path))
        self.assertEqual((code,err),(0,""))
        result=json.loads(out)
        self.assertTrue(result["persisted"])
        self.assertEqual(result["capabilities"]["decks"]["status"],"error")
        self.assertEqual(result["capabilities"]["inventory"]["completeness"],"partial")
        self.assertIsNone(result["saved_deck_count"])

    def test_cli_failure_does_not_report_success(self):
        code,out,err = self.cli("save-arena",str(self.root/"missing.log"),"--store",str(self.path))
        self.assertEqual(code,2)
        self.assertEqual(out,"")
        self.assertTrue(err)
        self.assertFalse(self.path.exists())

    def test_canonical_rebuild_does_not_change_archive(self):
        saved=self.save(); before=self.path.read_bytes()
        canonical_path=self.root/"current.db"
        for _ in range(2):
            canonical.create(canonical_path).close()
        self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(store.get_snapshot(self.path,saved["snapshot_id"]),saved)

    def test_archive_writer_rejects_canonical_database(self):
        canonical.create(self.path).close()
        before=self.path.read_bytes()
        with self.assertRaises(store.SnapshotStoreError):
            self.save()
        self.assertEqual(self.path.read_bytes(),before)


if __name__ == "__main__":
    unittest.main()
