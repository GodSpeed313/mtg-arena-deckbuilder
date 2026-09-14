from __future__ import annotations

import sqlite3
import unittest

from mtgadb import canonical
from mtgadb.model import Card, CardPrinting, Collection, Format, Inventory
from mtgadb.modes import OperatingMode
from mtgadb.query import CardQueryEngine
from services.exporter import export_arena_deck, import_arena_deck
from services.validator import DeckRules, validate_deck


class WorkbenchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(canonical.SCHEMA)
        canonical.load_cards(self.con, [
            Card(1, "Test Bolt", "{R}", 1, "Instant", colors="R",
                 color_identity="R", source_printing_id=101),
            Card(2, "Forest", types="Basic Land", subtypes="Forest",
                 source_printing_id=201),
            Card(3, "Blue Spell", "{U}", 1, "Sorcery", colors="U",
                 color_identity="U", source_printing_id=301),
            Card(4, "Endless Rats", "{1}{B}", 2, "Creature", subtypes="Rat",
                 color_identity="B",
                 rules_text="A deck can have any number of cards named Endless Rats.",
                 source_printing_id=401),
            Card(5, "Nine Riders", "{1}{B}", 2, "Creature", color_identity="B",
                 rules_text="A deck can have up to nine cards named Nine Riders.",
                 source_printing_id=501),
        ])
        canonical.load_printings(self.con, [
            CardPrinting(101, 1, "TST", "1", "common"),
            CardPrinting(102, 1, "OLD", "9", "common"),
            CardPrinting(201, 2, "TST", "200", "basic"),
            CardPrinting(301, 3, "OLD", "3", "rare"),
            CardPrinting(401, 4, "TST", "4", "uncommon"),
            CardPrinting(501, 5, "TST", "5", "uncommon"),
        ])

    def tearDown(self) -> None:
        self.con.close()

    def test_import_export_round_trip(self) -> None:
        source = """Deck
4 Test Bolt (TST) 1
56 Forest (TST) 200

Sideboard
1 Blue Spell (OLD) 3
"""
        parsed = import_arena_deck(source, self.con, name="Round Trip")
        self.assertTrue(parsed.ok)
        self.assertEqual(parsed.deck.size(), 60)
        self.assertEqual(export_arena_deck(parsed.deck, self.con), source)

    def test_import_reports_unknown_card_without_guessing(self) -> None:
        parsed = import_arena_deck("Deck\n4 Imaginary Card\n", self.con)
        self.assertFalse(parsed.ok)
        self.assertEqual(parsed.issues[0].code, "unknown_card")
        self.assertEqual(parsed.deck.main, {})

    def test_import_reports_unknown_printing(self) -> None:
        parsed = import_arena_deck("1 Test Bolt (NOPE) 404", self.con)
        self.assertEqual(parsed.issues[0].code, "unknown_printing")

    def test_duplicate_lines_are_aggregated(self) -> None:
        parsed = import_arena_deck("2 Test Bolt\n2 Test Bolt", self.con)
        self.assertEqual(parsed.deck.main, {101: 4})

    def test_copy_limit_aggregates_across_printings(self) -> None:
        deck = import_arena_deck(
            "3 Test Bolt (TST) 1\n2 Test Bolt (OLD) 9\n55 Forest", self.con
        ).deck
        report = validate_deck(deck, self.con)
        self.assertIn("copy_limit", {e.code for e in report.errors})

    def test_basic_lands_ignore_copy_limit(self) -> None:
        deck = import_arena_deck("60 Forest", self.con).deck
        self.assertTrue(validate_deck(deck, self.con).valid)

    def test_basic_lands_require_no_ownership_or_wildcards(self) -> None:
        deck = import_arena_deck("60 Forest", self.con).deck
        owned = validate_deck(
            deck, self.con, mode=OperatingMode.FULL_COLLECTION,
            collection=Collection(),
        )
        crafted = validate_deck(
            deck, self.con, mode=OperatingMode.WILDCARD_BUDGET,
            collection=Collection(), inventory=Inventory(),
        )
        self.assertTrue(owned.valid)
        self.assertTrue(crafted.valid)
        self.assertEqual(crafted.wildcard_cost, {})

    def test_card_text_can_override_copy_limit(self) -> None:
        unlimited = import_arena_deck("60 Endless Rats", self.con).deck
        nine = import_arena_deck("9 Nine Riders\n51 Forest", self.con).deck
        ten = import_arena_deck("10 Nine Riders\n50 Forest", self.con).deck
        self.assertTrue(validate_deck(unlimited, self.con).valid)
        self.assertTrue(validate_deck(nine, self.con).valid)
        self.assertIn("copy_limit", {e.code for e in validate_deck(ten, self.con).errors})

    def test_structural_limits(self) -> None:
        deck = import_arena_deck("4 Test Bolt\n55 Forest", self.con).deck
        report = validate_deck(deck, self.con)
        self.assertEqual([e.code for e in report.errors], ["main_size"])

    def test_color_identity(self) -> None:
        deck = import_arena_deck("4 Blue Spell\n56 Forest", self.con).deck
        rules = DeckRules(allowed_colors=frozenset("G"))
        report = validate_deck(deck, self.con, rules=rules)
        self.assertIn("color_identity", {e.code for e in report.errors})

    def test_format_legality_uses_any_legal_printing(self) -> None:
        deck = import_arena_deck("4 Test Bolt (OLD) 9\n56 Forest", self.con).deck
        format = Format("Test", legal_sets=frozenset({"TST"}))
        report = validate_deck(deck, self.con, format=format)
        self.assertNotIn("format_illegal_set", {e.code for e in report.errors})

    def test_allowed_title_is_exception_not_exclusive_allowlist(self) -> None:
        deck = import_arena_deck("4 Blue Spell\n56 Forest", self.con).deck
        format = Format(
            "Test", legal_sets=frozenset({"TST"}),
            allowed_title_ids=frozenset({3}),
        )
        report = validate_deck(deck, self.con, format=format)
        self.assertNotIn("format_illegal_set", {e.code for e in report.errors})
        canonical.load_formats(self.con, {format.name: format})
        names = {card.name for card in CardQueryEngine(self.con).find(format="Test")}
        self.assertIn("Blue Spell", names)
        self.assertIn("Forest", names)

    def test_format_ban(self) -> None:
        deck = import_arena_deck("4 Test Bolt\n56 Forest", self.con).deck
        format = Format("Test", banned_title_ids=frozenset({1}))
        report = validate_deck(deck, self.con, format=format)
        self.assertIn("format_banned", {e.code for e in report.errors})

    def test_format_quota_and_suspension(self) -> None:
        deck = import_arena_deck("4 Test Bolt\n4 Blue Spell\n52 Forest", self.con).deck
        format = Format(
            "Test", individual_card_quotas={1: 1},
            suspended_title_ids=frozenset({3}),
        )
        codes = {e.code for e in validate_deck(deck, self.con, format=format).errors}
        self.assertIn("copy_limit", codes)
        self.assertIn("format_suspended", codes)

    def test_commander_minimum_and_allowlist(self) -> None:
        deck = import_arena_deck("59 Forest\n\nCommander\n1 Blue Spell", self.con).deck
        format = Format(
            "Brawl", min_deck_size=59, max_deck_size=59,
            min_command_zone=1, max_command_zone=1,
            allowed_commander_title_ids=frozenset({1}),
        )
        report = validate_deck(deck, self.con, format=format)
        self.assertIn("commander_not_allowed", {e.code for e in report.errors})
        self.assertNotIn("commander_size", {e.code for e in report.errors})

    def test_full_collection_requires_data(self) -> None:
        deck = import_arena_deck("4 Test Bolt\n56 Forest", self.con).deck
        report = validate_deck(deck, self.con, mode=OperatingMode.FULL_COLLECTION)
        self.assertIn("collection_required", {e.code for e in report.errors})

    def test_full_collection_reports_shortage(self) -> None:
        deck = import_arena_deck("4 Test Bolt\n56 Forest", self.con).deck
        collection = Collection({101: 3, 201: 56})
        report = validate_deck(
            deck, self.con, mode=OperatingMode.FULL_COLLECTION,
            collection=collection,
        )
        self.assertIn("not_owned", {e.code for e in report.errors})

    def test_wildcard_cost_and_shortage(self) -> None:
        deck = import_arena_deck("4 Test Bolt\n4 Blue Spell\n52 Forest", self.con).deck
        report = validate_deck(
            deck, self.con, mode=OperatingMode.WILDCARD_BUDGET,
            collection=Collection({101: 2, 201: 52}),
            inventory=Inventory({"common": 2, "rare": 3}),
        )
        self.assertEqual(report.wildcard_cost, {"common": 2, "rare": 4})
        self.assertEqual(
            [e.code for e in report.errors].count("wildcard_shortage"), 1
        )

    def test_wildcard_budget_does_not_assume_missing_ownership_is_zero(self) -> None:
        deck = import_arena_deck("4 Test Bolt\n56 Forest", self.con).deck
        report = validate_deck(
            deck, self.con, mode=OperatingMode.WILDCARD_BUDGET,
            inventory=Inventory({"common": 100}),
        )
        self.assertFalse(report.valid)
        self.assertIn("collection_required", {e.code for e in report.errors})
        self.assertEqual(report.wildcard_cost, {})

    def test_wildcard_budget_accepts_explicit_known_empty_collection(self) -> None:
        deck = import_arena_deck("4 Test Bolt\n56 Forest", self.con).deck
        report = validate_deck(
            deck, self.con, mode=OperatingMode.WILDCARD_BUDGET,
            collection=Collection(), inventory=Inventory({"common": 4}),
        )
        self.assertTrue(report.valid)
        self.assertEqual(report.wildcard_cost, {"common": 4})


    def test_confirmed_zero_wildcards_produce_shortage(self) -> None:
        deck = import_arena_deck("4 Test Bolt\n56 Forest", self.con).deck
        report = validate_deck(
            deck, self.con, mode=OperatingMode.WILDCARD_BUDGET,
            collection=Collection(), inventory=Inventory({"common": 0}),
        )
        self.assertFalse(report.valid)
        self.assertEqual([e.code for e in report.errors], ["wildcard_shortage"])
        self.assertEqual(report.wildcard_cost, {"common": 4})

    def test_missing_wildcard_rarity_is_unknown_not_zero(self) -> None:
        deck = import_arena_deck("4 Test Bolt\n56 Forest", self.con).deck
        report = validate_deck(
            deck, self.con, mode=OperatingMode.WILDCARD_BUDGET,
            collection=Collection(), inventory=Inventory({"rare": 10}),
        )
        self.assertFalse(report.valid)
        self.assertEqual([e.code for e in report.errors], ["wildcard_inventory_incomplete"])
        self.assertIn("common", report.errors[0].message)
        self.assertEqual(report.wildcard_cost, {"common": 4})

    def test_unavailable_inventory_does_not_produce_affordability(self) -> None:
        deck = import_arena_deck("4 Test Bolt\n56 Forest", self.con).deck
        report = validate_deck(
            deck, self.con, mode=OperatingMode.WILDCARD_BUDGET,
            collection=Collection(), inventory=None,
        )
        self.assertFalse(report.valid)
        self.assertEqual([e.code for e in report.errors], ["inventory_required"])
        self.assertEqual(report.wildcard_cost, {})


if __name__ == "__main__":
    unittest.main()
