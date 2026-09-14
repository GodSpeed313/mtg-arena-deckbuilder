from __future__ import annotations

from contextlib import closing, redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from arena_log import iter_json_objects
from mtgadb.model import Status
from mtgadb.providers.arena_log import ArenaLogProvider
from workbench import main

FIXTURE = Path(__file__).parent / "fixtures" / "arena_log_redacted.log"
TEXT = FIXTURE.read_text(encoding="utf-8")
STATE = next(obj for obj, _ in iter_json_objects(TEXT) if "InventoryInfo" in obj)


def response(state):
    return "[UnityCrossThreadLogger]9/13/2026 8:08:29 PM\n<== StartHook(fake-request)\n" + json.dumps(state) + "\n"


class ArenaLogTests(unittest.TestCase):
    def setUp(self):
        self.provider = ArenaLogProvider.from_text(TEXT)
        self.state = deepcopy(STATE)

    def changed(self):
        return ArenaLogProvider.from_text(response(self.state))

    def test_wildcards(self):
        result = self.provider.get_inventory()
        self.assertTrue(result.ok)
        self.assertEqual(result.data.wildcards, {"common": 29, "uncommon": 29, "rare": 16, "mythic": 17})

    def test_currencies(self):
        inv = self.provider.get_inventory().data
        self.assertEqual((inv.gold, inv.gems), (2400, 250))

    def test_raw_vault(self):
        result = self.provider.get_inventory()
        self.assertEqual(result.data.vault_progress, 621)
        self.assertEqual(result.diagnostics.evidence["vault_units"], "raw_Arena_value")

    def test_wildcard_track(self):
        self.assertEqual(self.provider.get_inventory().data.wildcard_track_position, 1)

    def test_server_timestamp(self):
        for result in (self.provider.get_inventory(), self.provider.get_decks()):
            self.assertEqual(result.diagnostics.evidence["observed_at"], "2026-09-14T00:08:29.703840+00:00")
            self.assertEqual(result.diagnostics.evidence["timestamp_source"], "ServerTimeInternal")

    def test_invalid_or_missing_timestamp_stays_unknown(self):
        for value in (None, "not-a-date", "2026-09-14T00:08:29", [], 123):
            with self.subTest(value=value):
                self.state["ServerTimeInternal"] = value
                result = self.changed().get_inventory()
                self.assertTrue(result.ok)
                self.assertIsNone(result.diagnostics.evidence["observed_at"])
                self.assertIsNone(result.diagnostics.evidence["timestamp_source"])
        del self.state["ServerTimeInternal"]
        self.assertIsNone(self.changed().get_inventory().diagnostics.evidence["observed_at"])

    def test_timezone_is_normalized(self):
        self.state["ServerTimeInternal"] = "2026-09-13T20:08:29-04:00"
        self.assertEqual(self.changed().get_inventory().diagnostics.evidence["observed_at"], "2026-09-14T00:08:29+00:00")

    def test_summary_metadata_preserved(self):
        deck = self.provider.get_decks().data[0]
        self.assertEqual(deck.deck_id, "fake-deck-alpha")
        self.assertEqual(deck.name, "Synthetic Alpha")
        self.assertEqual(deck.format, "Standard")
        self.assertEqual(deck.attributes, STATE["DeckSummaries"][0]["Attributes"])
        self.assertEqual(deck.summary, STATE["DeckSummaries"][0])

    def test_correlation_uses_ids_not_array_order(self):
        self.state["DeckSummaries"].reverse()
        decks = {d.deck_id: d for d in self.changed().get_decks().data}
        self.assertEqual(decks["fake-deck-alpha"].main, [{"cardId": 101, "quantity": 4}, {"cardId": 201, "quantity": 56}])
        self.assertIsNone(decks["fake-deck-empty"].main)

    def test_main_deck(self):
        self.assertEqual(self.provider.get_decks().data[0].main, [{"cardId": 101, "quantity": 4}, {"cardId": 201, "quantity": 56}])

    def test_sideboard(self):
        self.assertEqual(self.provider.get_decks().data[0].sideboard, [{"cardId": 301, "quantity": 2}])

    def test_command_zone(self):
        self.assertEqual(self.provider.get_decks().data[0].commander, [{"cardId": 401, "quantity": 1}])

    def test_companions_remain_separate(self):
        deck = self.provider.get_decks().data[0]
        self.assertEqual(deck.companions, [{"cardId": 501, "quantity": 1}])
        self.assertNotIn(501, [entry["cardId"] for entry in deck.sideboard])

    def test_missing_zones_are_unknown(self):
        del self.state["DecksInternal"]["fake-deck-alpha"]["Sideboard"]
        self.assertIsNone(self.changed().get_decks().data[0].sideboard)

    def test_explicit_empty_zones_are_known_empty(self):
        deck = self.provider.get_decks().data[2]
        self.assertEqual([deck.main, deck.sideboard, deck.commander, deck.companions], [[], [], [], []])
        self.assertEqual(deck.card_skins, [])

    def test_empty_record_not_invented(self):
        deck = self.provider.get_decks().data[1]
        self.assertTrue(deck.record_present)
        self.assertEqual(deck.record_fields, ())
        self.assertEqual([deck.main, deck.sideboard, deck.commander, deck.companions, deck.card_skins], [None] * 5)
        self.assertEqual(self.provider.get_decks().diagnostics.evidence["completeness"], "partial")

    def test_missing_record_distinct_from_empty_record(self):
        del self.state["DecksInternal"]["fake-deck-empty"]
        result = self.changed().get_decks()
        self.assertFalse(result.data[1].record_present)
        self.assertEqual(result.diagnostics.evidence["missing_record_count"], 1)

    def test_missing_summary_does_not_invent_name(self):
        self.state["DeckSummaries"] = self.state["DeckSummaries"][1:]
        result = self.changed().get_decks()
        deck = next(d for d in result.data if d.deck_id == "fake-deck-alpha")
        self.assertIsNone(deck.name)
        self.assertIsNone(deck.summary)
        self.assertIsNone(deck.attributes)
        self.assertIsNone(deck.format)
        self.assertEqual(deck.main, [{"cardId": 101, "quantity": 4}, {"cardId": 201, "quantity": 56}])
        self.assertEqual(result.diagnostics.evidence["missing_summary_count"], 1)

    def test_skins_preserved_but_not_cards(self):
        deck = self.provider.get_decks().data[0]
        self.assertEqual(deck.card_skins, [{"GrpId": "999999", "CCV": "DA"}])
        self.assertNotIn(999999, [entry["cardId"] for entry in deck.main])
        self.assertIsNone(self.provider.get_collection().data)

    def test_precon_course_and_request_updates_excluded(self):
        decks = self.provider.get_decks().data
        self.assertEqual(len(decks), 3)
        self.assertEqual(decks[0].main, [{"cardId": 101, "quantity": 4}, {"cardId": 201, "quantity": 56}])
        self.assertEqual(decks[0].name, "Synthetic Alpha")
        self.assertIsNone(self.provider.get_collection().data)

    def test_ownership_unavailable_never_empty_collection(self):
        result = self.provider.get_collection()
        self.assertFalse(result.ok)
        self.assertIs(result.diagnostics.status, Status.UNSUPPORTED)
        self.assertIsNone(result.data)
        self.assertEqual(result.diagnostics.evidence["availability"], "unavailable")
        self.assertIsNone(result.diagnostics.evidence["observed_at"])

    def test_numeric_quantity_maps_do_not_establish_ownership(self):
        self.state["UnconfirmedCards"] = {str(i): i % 4 + 1 for i in range(10000, 11000)}
        self.assertIsNone(self.changed().get_collection().data)

    def test_missing_inventory_fields_not_zero(self):
        for key in ("Gold", "Gems", "TotalVaultProgress", "WcTrackPosition", "WildCardRares"):
            del self.state["InventoryInfo"][key]
        result = self.changed().get_inventory()
        self.assertTrue(result.ok)
        self.assertIsNone(result.data.gold)
        self.assertIsNone(result.data.gems)
        self.assertIsNone(result.data.vault_progress)
        self.assertIsNone(result.data.wildcard_track_position)
        self.assertIsNone(result.data.wildcards["rare"])
        self.assertEqual(result.diagnostics.evidence["completeness"], "partial")
        self.assertIn("Gold", result.diagnostics.evidence["missing_fields"])

    def test_known_zero_preserved(self):
        for key in self.state["InventoryInfo"]:
            if key != "Cosmetics":
                self.state["InventoryInfo"][key] = 0
        result = self.changed().get_inventory()
        self.assertEqual(result.data.gold, 0)
        self.assertEqual(result.data.wildcards["rare"], 0)
        self.assertEqual(result.data.wildcard_track_position, 0)
        self.assertEqual(result.diagnostics.evidence["sequence_id"], 0)
        self.assertEqual(result.diagnostics.evidence["completeness"], "complete")

    def test_absent_inventory_is_unavailable(self):
        del self.state["InventoryInfo"]
        result = self.changed().get_inventory()
        self.assertIs(result.diagnostics.status, Status.NOT_FOUND)
        self.assertIsNone(result.data)
        self.assertTrue(self.changed().get_decks().ok)

    def test_empty_inventory_is_present_but_all_fields_unknown(self):
        self.state["InventoryInfo"] = {}
        result = self.changed().get_inventory()
        self.assertTrue(result.ok)
        self.assertIsNone(result.data.gold)
        self.assertTrue(all(v is None for v in result.data.wildcards.values()))

    def test_evidence_and_markers(self):
        inv = self.provider.get_inventory().diagnostics
        self.assertEqual(inv.source, "Arena Player.log")
        self.assertEqual(inv.evidence["source_event"], "StartHook")
        self.assertEqual(inv.evidence["authority"], "server_response_snapshot")
        self.assertEqual(inv.evidence["sequence_id"], 1)
        self.assertEqual(inv.evidence["availability"], "available")
        decks = self.provider.get_decks().diagnostics.evidence
        self.assertEqual(decks["cache_markers"], {"DeckSummariesCacheVersion": -1756407444, "DecksCacheVersion": 796858945})
        self.assertFalse(decks["printing_ids_checked"])
        self.assertIsNone(decks["unknown_printing_ids"])

    def test_invalid_inventory_does_not_break_decks(self):
        for value in (None, True, -1, 1.5, "12", []):
            with self.subTest(value=value):
                self.state["InventoryInfo"]["Gold"] = value
                provider = self.changed()
                result = provider.get_inventory()
                self.assertIs(result.diagnostics.status, Status.ERROR)
                self.assertIsNone(result.data)
                self.assertTrue(provider.get_decks().ok)

    def test_malformed_top_level_fields_diagnosed(self):
        for key in ("InventoryInfo", "DeckSummaries", "DecksInternal"):
            for value in (None, 12, "bad"):
                with self.subTest(key=key, value=value):
                    state = deepcopy(STATE)
                    state[key] = value
                    provider = ArenaLogProvider.from_text(response(state))
                    result = provider.get_inventory() if key == "InventoryInfo" else provider.get_decks()
                    self.assertIs(result.diagnostics.status, Status.ERROR)

    def test_malformed_deck_does_not_break_inventory(self):
        for value in (None, {}, [None], [{"cardId": True, "quantity": 1}],
                      [{"cardId": 101}], [{"cardId": 101, "quantity": -1}]):
            with self.subTest(value=value):
                self.state["DecksInternal"]["fake-deck-alpha"]["MainDeck"] = value
                provider = self.changed()
                self.assertIs(provider.get_decks().diagnostics.status, Status.ERROR)
                self.assertTrue(provider.get_inventory().ok)

    def test_duplicate_card_ids_preserved_without_merging(self):
        self.state["DecksInternal"]["fake-deck-alpha"]["MainDeck"] *= 2
        result = self.changed().get_decks()
        self.assertTrue(result.ok)
        self.assertEqual(result.data[0].main, self.state["DecksInternal"]["fake-deck-alpha"]["MainDeck"])
        self.assertEqual(len(result.data[0].main), 4)

    def test_malformed_summary_and_attributes_diagnosed(self):
        for update in ({"DeckIdInternal": None}, {"Name": 123}, {"Attributes": {}},
                       {"Attributes": [{"name": "Format", "value": 1}]}):
            with self.subTest(update=update):
                self.state = deepcopy(STATE)
                self.state["DeckSummaries"][0].update(update)
                self.assertIs(self.changed().get_decks().diagnostics.status, Status.ERROR)

    def test_attribute_with_missing_value_preserves_unknown_format(self):
        self.state["DeckSummaries"][0]["Attributes"] = [{"name": "Format"}]
        result = self.changed().get_decks()
        self.assertTrue(result.ok)
        self.assertIsNone(result.data[0].format)
        self.assertEqual(result.data[0].attributes, [{"name": "Format"}])

    def test_deep_metadata_returns_error_without_losing_inventory(self):
        self.state["DeckSummaries"][0]["Extra"] = "nested-placeholder"
        text = response(self.state).replace(
            '"nested-placeholder"', "[" * 600 + "0" + "]" * 600
        )
        provider = ArenaLogProvider.from_text(text)
        decks = provider.get_decks()
        self.assertIs(decks.diagnostics.status, Status.ERROR)
        self.assertIsNone(decks.data)
        self.assertEqual(decks.diagnostics.evidence["error"], "deck_metadata_nesting_too_deep")
        self.assertEqual(decks.diagnostics.evidence["completeness"], "unknown")
        inventory = provider.get_inventory()
        self.assertTrue(inventory.ok)
        self.assertEqual(inventory.data.gold, 2400)

    def test_duplicate_summary_ids_diagnosed(self):
        self.state["DeckSummaries"].append(self.state["DeckSummaries"][0])
        self.assertIs(self.changed().get_decks().diagnostics.status, Status.ERROR)

    def test_missing_deck_fields_vs_explicit_empty_snapshot(self):
        for key in ("DeckSummaries", "DecksInternal"):
            del self.state[key]
        result = self.changed().get_decks()
        self.assertIsNone(result.data)
        self.assertIs(result.diagnostics.status, Status.NOT_FOUND)
        self.state.update(DeckSummaries=[], DecksInternal={})
        result = self.changed().get_decks()
        self.assertTrue(result.ok)
        self.assertEqual(result.data, [])
        self.assertEqual(result.diagnostics.evidence["completeness"], "complete")

    def test_unrelated_json_is_not_start_hook(self):
        text = "noise\n" + json.dumps(STATE) + "\n==> StartHook(fake-request)\n" + json.dumps(STATE)
        provider = ArenaLogProvider.from_text(text)
        self.assertIs(provider.get_inventory().diagnostics.status, Status.NOT_FOUND)
        self.assertIs(provider.get_decks().diagnostics.status, Status.NOT_FOUND)

    def test_last_response_wins_without_merging_old_capabilities(self):
        newer = {"InventoryInfo": {"Gold": 0}}
        provider = ArenaLogProvider.from_text(response(STATE) + response(newer))
        self.assertEqual(provider.get_inventory().data.gold, 0)
        self.assertIsNone(provider.get_inventory().data.gems)
        self.assertIs(provider.get_decks().diagnostics.status, Status.NOT_FOUND)
        self.assertEqual(provider.get_inventory().diagnostics.evidence["start_hook_responses"], 2)

    def test_truncated_latest_response_does_not_fall_back(self):
        for broken in ('{"InventoryInfo":', '{"broken":, "nested": {"Gold": 1}}', '[{"InventoryInfo": {}}]'):
            with self.subTest(broken=broken):
                provider = ArenaLogProvider.from_text(response(STATE) + "<== StartHook(fake-broken)\n" + broken)
                self.assertIs(provider.get_inventory().diagnostics.status, Status.ERROR)
                self.assertIsNone(provider.get_inventory().data)

    def test_event_boundary_does_not_capture_unrelated_payload(self):
        text = "<== StartHook(fake-empty)\n[UnityCrossThreadLogger]\n<== Other(fake-other)\n" + json.dumps(STATE)
        self.assertIs(ArenaLogProvider.from_text(text).get_inventory().diagnostics.status, Status.ERROR)

    def test_unrelated_malformed_json_harmless(self):
        provider = ArenaLogProvider.from_text("unrelated {broken\n" + TEXT + "\nnoise {bad")
        self.assertTrue(provider.get_inventory().ok)
        self.assertTrue(provider.get_decks().ok)

    def test_validates_printing_ids_without_rewriting_them(self):
        with closing(sqlite3.connect(":memory:")) as con:
            con.execute("CREATE TABLE printings(arena_id INTEGER, title_id INTEGER)")
            con.executemany("INSERT INTO printings VALUES (?, 1)", [(i,) for i in (101, 201, 301, 401, 501)])
            before = con.total_changes
            result = ArenaLogProvider.from_text(TEXT, database=con).get_decks()
            self.assertTrue(result.ok)
            self.assertEqual(result.data[0].main, [{"cardId": 101, "quantity": 4}, {"cardId": 201, "quantity": 56}])
            self.assertEqual(result.diagnostics.evidence["unknown_printing_ids"], [])
            self.assertEqual(con.total_changes, before)
            con.execute("DELETE FROM printings WHERE arena_id=501")
            result = ArenaLogProvider.from_text(TEXT, database=con).get_decks()
            self.assertEqual(result.diagnostics.evidence["unknown_printing_ids"], [501])
            self.assertEqual(result.data[0].companions, [{"cardId": 501, "quantity": 1}])

    def test_invalid_database_diagnosed_without_losing_inventory(self):
        with closing(sqlite3.connect(":memory:")) as con:
            provider = ArenaLogProvider.from_text(TEXT, database=con)
            self.assertIs(provider.get_decks().diagnostics.status, Status.ERROR)
            self.assertTrue(provider.get_inventory().ok)

    def test_result_mutation_does_not_change_provider_state(self):
        result = self.provider.get_decks()
        result.data[0].summary["Name"] = "changed"
        result.data[0].attributes.clear()
        result.diagnostics.evidence["observed_at"] = "changed"
        again = self.provider.get_decks()
        self.assertEqual(again.data[0].name, "Synthetic Alpha")
        self.assertEqual(again.data[0].summary["Name"], "Synthetic Alpha")
        self.assertTrue(again.data[0].attributes)
        self.assertNotEqual(again.diagnostics.evidence["observed_at"], "changed")

    def test_read_failure_returns_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp:
            provider = ArenaLogProvider(Path(temp) / "missing.log")
            self.assertIs(provider.get_inventory().diagnostics.status, Status.ERROR)
            self.assertIs(provider.get_decks().diagnostics.status, Status.ERROR)
            self.assertIsNone(provider.get_collection().data)

    def test_reads_once_without_mutating_log(self):
        before = FIXTURE.read_bytes()
        provider = ArenaLogProvider(FIXTURE)
        self.assertTrue(provider.get_inventory().ok)
        self.assertTrue(provider.get_decks().ok)
        self.assertEqual(FIXTURE.read_bytes(), before)

    def test_inspection_cli_is_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "test.db"
            with closing(sqlite3.connect(database)) as con:
                con.execute("CREATE TABLE printings(arena_id INTEGER)")
                con.executemany("INSERT INTO printings VALUES (?)", [(i,) for i in (101, 201, 301, 401, 501)])
                con.commit()
            before = hashlib.sha256(database.read_bytes()).hexdigest()
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["--database", str(database), "inspect-arena", str(FIXTURE)])
            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertIsNone(payload["collection"]["data"])
            self.assertEqual(payload["collection"]["diagnostics"]["status"], "unsupported")
            self.assertIsNone(payload["decks"]["data"][1]["main"])
            self.assertEqual(payload["inventory"]["data"]["gold"], 2400)
            self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), before)
            self.assertEqual(sorted(p.name for p in Path(temp).iterdir()), ["test.db"])


if __name__ == "__main__":
    unittest.main()
