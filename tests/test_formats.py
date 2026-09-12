from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from mtgadb import canonical
from mtgadb.model import Status
from mtgadb.providers.formats_json import JSONFormatProvider


FIXTURE = Path(__file__).parent / "fixtures" / "formats_sample.json"


class FormatProviderTests(unittest.TestCase):
    def test_schema_one_migrates_without_card_rebuild(self) -> None:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO meta VALUES ('schema_version', '1');
            CREATE TABLE formats (
                name TEXT PRIMARY KEY, min_deck_size INTEGER,
                max_deck_size INTEGER, max_sideboard INTEGER,
                max_command_zone INTEGER,
                uses_rebalanced INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE format_sets (
                format_name TEXT, set_code TEXT,
                PRIMARY KEY (format_name, set_code));
            CREATE TABLE format_title_rules (
                format_name TEXT, title_id INTEGER, rule TEXT,
                PRIMARY KEY (format_name, title_id, rule));
        """)
        canonical.migrate(con)
        self.assertEqual(canonical.get_meta(con, "schema_version"), "2")
        columns = {r[1] for r in con.execute("PRAGMA table_info(formats)")}
        self.assertIn("min_command_zone", columns)
        self.assertIn("uses_rebalanced_present", columns)
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        self.assertIn("format_card_quotas", tables)
        self.assertIn("format_commander_allowlist", tables)
        con.close()

    def test_explicit_fields_are_preserved(self) -> None:
        result = JSONFormatProvider(FIXTURE).get_formats()
        self.assertTrue(result.ok)
        self.assertEqual(result.diagnostics.status, Status.OK)
        self.assertEqual(result.diagnostics.evidence["unknown_fields"], [])
        assert result.data is not None
        self.assertEqual(set(result.data), {"Standard", "Brawl", "Timeless"})

        brawl = result.data["Brawl"]
        self.assertEqual((brawl.min_deck_size, brawl.max_deck_size), (59, 59))
        self.assertEqual((brawl.min_command_zone, brawl.max_command_zone), (1, 1))
        self.assertEqual(brawl.allowed_commander_title_ids, frozenset({1}))
        self.assertIsNone(brawl.uses_rebalanced_cards)

        timeless = result.data["Timeless"]
        self.assertEqual(timeless.individual_card_quotas, {1: 1})
        self.assertEqual(timeless.rarity_card_quotas, {0: 4, 3: None})
        self.assertEqual(timeless.suppressed_title_ids, frozenset({2}))
        self.assertEqual(timeless.suspended_title_ids, frozenset({3}))
        self.assertEqual(timeless.color_restrictions_internal, (frozenset({1, 2}),))
        self.assertTrue(timeless.uses_rebalanced_cards)

    def test_missing_file_returns_diagnostics(self) -> None:
        result = JSONFormatProvider(FIXTURE.with_name("missing.json")).get_formats()
        self.assertFalse(result.ok)
        self.assertEqual(result.diagnostics.status, Status.ERROR)

    def test_database_round_trip(self) -> None:
        result = JSONFormatProvider(FIXTURE).get_formats()
        assert result.data is not None
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript(canonical.SCHEMA)
        canonical.load_formats(con, result.data)

        timeless = canonical.get_format(con, "timeless")
        self.assertIsNotNone(timeless)
        assert timeless is not None
        self.assertEqual(timeless.individual_card_quotas, {1: 1})
        self.assertEqual(timeless.rarity_card_quotas, {0: 4, 3: None})
        self.assertEqual(timeless.filter_sets, frozenset({"TST"}))
        self.assertEqual(timeless.suspended_title_ids, frozenset({3}))
        self.assertTrue(timeless.uses_rebalanced_cards)
        con.close()


if __name__ == "__main__":
    unittest.main()
