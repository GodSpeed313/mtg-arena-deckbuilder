from __future__ import annotations

from copy import deepcopy
import unittest

from mtgadb.deck_identity import (
    build_deck_snapshot_identity, require_deck_snapshot_identity,
)
from mtgadb.model import Deck


class DeckSnapshotIdentityTests(unittest.TestCase):
    def setUp(self):
        self.deck = Deck(
            deck_id="deck.one",
            name="One",
            main={202: 1, 101: 2},
            sideboard={303: 1},
            commander={404: 1},
        )

    def test_equivalent_gameplay_state_is_deterministic(self):
        reordered = Deck(
            deck_id="deck.two",
            name="Renamed",
            main={101: 2, 202: 1},
            sideboard={303: 1},
            commander={404: 1},
        )
        first = build_deck_snapshot_identity(self.deck)
        self.assertEqual(first, build_deck_snapshot_identity(self.deck))
        self.assertEqual(first, build_deck_snapshot_identity(reordered))
        self.assertEqual(first["canonical_payload"]["main"], [[101, 2], [202, 1]])

    def test_each_gameplay_zone_changes_identity(self):
        baseline = build_deck_snapshot_identity(self.deck)
        for zone, arena_id in (("main", 505), ("sideboard", 606), ("commander", 707)):
            changed = deepcopy(self.deck)
            getattr(changed, zone)[arena_id] = 1
            with self.subTest(zone=zone):
                self.assertNotEqual(build_deck_snapshot_identity(changed), baseline)

    def test_printing_and_quantity_changes_change_identity(self):
        baseline = build_deck_snapshot_identity(self.deck)
        printing_changed = deepcopy(self.deck)
        del printing_changed.main[101]
        printing_changed.main[102] = 2
        quantity_changed = deepcopy(self.deck)
        quantity_changed.main[101] = 3
        self.assertNotEqual(build_deck_snapshot_identity(printing_changed), baseline)
        self.assertNotEqual(build_deck_snapshot_identity(quantity_changed), baseline)

    def test_metadata_is_excluded_from_gameplay_identity(self):
        changed = Deck(
            deck_id="different-private-id",
            name="Entirely Different Name",
            main=deepcopy(self.deck.main),
            sideboard=deepcopy(self.deck.sideboard),
            commander=deepcopy(self.deck.commander),
        )
        self.assertEqual(
            build_deck_snapshot_identity(changed),
            build_deck_snapshot_identity(self.deck),
        )

    def test_malformed_deck_state_fails_closed(self):
        malformed = (
            Deck(main={0: 1}),
            Deck(main={"101": 1}),
            Deck(main={101: 0}),
            Deck(main={101: True}),
            Deck(main={101: 1.0}),
            Deck(main=None),
        )
        for deck in malformed:
            with self.subTest(deck=deck), self.assertRaises(ValueError):
                build_deck_snapshot_identity(deck)
        with self.assertRaises(ValueError):
            build_deck_snapshot_identity(object())

    def test_identity_record_tampering_fails_closed(self):
        identity = build_deck_snapshot_identity(self.deck)
        tampered = deepcopy(identity)
        tampered["canonical_payload"]["main"][0][1] += 1
        with self.assertRaises(ValueError):
            require_deck_snapshot_identity(tampered)
        malformed = deepcopy(identity)
        malformed["canonical_payload"]["main"].reverse()
        with self.assertRaises(ValueError):
            require_deck_snapshot_identity(malformed)


if __name__ == "__main__":
    unittest.main()
