from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
from threading import Event
import unittest

from mtgadb.model import Collection, Inventory
from mtgadb.modes import OperatingMode
from services import local_execution_validation as authority
from tests import test_pre_execution_revalidation as upstream


class AuthorityFixture:
    def __init__(self, test):
        self.upstream = upstream.PreExecutionRevalidationTests(methodName="runTest")
        self.upstream.setUp()
        test.addCleanup(self.upstream.doCleanups)
        temp = tempfile.TemporaryDirectory()
        test.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cards = self.root / "cards.db"
        self.upstream.con.commit()
        con = sqlite3.connect(self.cards)
        self.upstream.con.backup(con)
        con.close()
        self.args = dict(card_database_path=self.cards, collection=self.upstream.owned,
                         inventory=None, format=self.upstream.format, rules=self.upstream.rules,
                         mode=OperatingMode.FULL_COLLECTION)
        self.authority = authority.LocalExecutionValidationAuthorityV1(**self.args)
        test.addCleanup(self.authority.close)

    def publish(self, **overrides):
        return self.authority.publish(**{**self.args, **overrides})


class LocalExecutionValidationTests(unittest.TestCase):
    def setUp(self):
        self.fx = AuthorityFixture(self)
        self.authority = self.fx.authority

    def error(self, code, action):
        with self.assertRaises(authority.LocalDeckApplicationError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def test_owned_complete_context_identity_and_generation(self):
        with self.authority._guarded() as view:
            first = deepcopy(view["context"])
            self.assertEqual(first["generation"], 1)
            self.assertEqual(first["collection"], [[101, 1], [201, 1], [301, 1]])
        changed = self.fx.publish()
        self.assertEqual(changed["authority_id"], first["authority_id"])
        self.assertEqual(changed["generation"], 2)

    def test_nested_input_and_guarded_output_aliases_detached(self):
        self.fx.args["collection"].cards[101] = 0
        with self.authority._guarded() as view:
            self.assertEqual(view["collection"].cards[101], 1)
            view["collection"].cards[101] = 50
            view["context"]["collection"].clear()
            view["format"].individual_card_quotas[1] = 0
        with self.authority._guarded() as view:
            self.assertEqual(view["collection"].cards[101], 1)
            self.assertEqual(view["format"].individual_card_quotas, {})

    def test_card_projection_owned_read_only_and_source_refresh_explicit(self):
        with self.authority._guarded() as view:
            old = view["context"]["card_database_identity"]
            with self.assertRaises(sqlite3.OperationalError):
                view["con"].execute("DELETE FROM printings")
        with closing(sqlite3.connect(self.fx.cards)) as con, con:
            con.execute("UPDATE printings SET title_id=2 WHERE arena_id=101")
        with self.authority._guarded() as view:
            self.assertEqual(view["con"].execute("SELECT title_id FROM printings WHERE arena_id=101").fetchone()[0], 1)
        self.fx.publish()
        with self.authority._guarded() as view:
            self.assertEqual(view["con"].execute("SELECT title_id FROM printings WHERE arena_id=101").fetchone()[0], 2)
            self.assertNotEqual(view["context"]["card_database_identity"], old)

    def test_publication_waits_for_guard_and_is_all_or_nothing(self):
        started, finished = Event(), Event()
        def publish():
            started.set()
            result = self.fx.publish(collection=Collection({101: 7}))
            finished.set()
            return result
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.authority._guarded() as view:
                future = pool.submit(publish)
                self.assertTrue(started.wait(3))
                self.assertFalse(finished.wait(.05))
                self.assertEqual(view["context"]["generation"], 1)
            self.assertEqual(future.result(3)["generation"], 2)
        with self.authority._guarded() as view:
            self.assertEqual(view["collection"].cards, {101: 7})

    def test_failed_publication_preserves_generation_and_inputs(self):
        self.error("execution_context_unavailable", lambda: self.fx.publish(card_database_path=self.fx.root / "missing"))
        with self.authority._guarded() as view:
            self.assertEqual(view["context"]["generation"], 1)
            self.assertEqual(view["collection"], self.fx.upstream.owned)

    def test_invalid_collection_counts_and_mode(self):
        for value in (None, {}, Collection({True: 1}), Collection({101: True}),
                      Collection({101: -1}), Collection({101: 1.0}), Collection({0: 1})):
            self.error("invalid_execution_context", lambda: self.fx.publish(collection=value))
        for mode in (OperatingMode.UNLIMITED, "full_collection", [], None):
            self.error("invalid_execution_context", lambda: self.fx.publish(mode=mode))

    def test_budget_requires_inventory_and_strict_counts(self):
        self.error("invalid_execution_context", lambda: self.fx.publish(mode=OperatingMode.WILDCARD_BUDGET))
        for value in (Inventory({"common": True}), Inventory({"common": -1}),
                      Inventory({"": 1}), Inventory({}, gold=True), {}):
            self.error("invalid_execution_context", lambda: self.fx.publish(inventory=value))
        self.fx.publish(mode=OperatingMode.WILDCARD_BUDGET, inventory=Inventory({}))

    def test_invalid_rules_format_and_card_source(self):
        for args in ({"rules": None}, {"format": None}, {"rules": replace(self.fx.upstream.rules, min_main=True)},
                     {"format": replace(self.fx.upstream.format, max_sideboard=True)}, {"card_database_path": object()}):
            self.error("invalid_execution_context", lambda: self.fx.publish(**args))
        self.error("invalid_execution_context", lambda: self.fx.publish(card_database_path=self.fx.upstream.con))
        with closing(sqlite3.connect(self.fx.cards)) as con, con:
            con.execute("UPDATE printings SET title_id=999 WHERE arena_id=101")
        self.error("invalid_execution_context", self.fx.publish)

    def test_fresh_source_transaction_does_not_reuse_old_reader(self):
        with closing(sqlite3.connect(self.fx.cards)) as con, con:
            con.execute("PRAGMA journal_mode=WAL")
        reader = sqlite3.connect(self.fx.cards)
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM printings").fetchall()
        with closing(sqlite3.connect(self.fx.cards)) as con, con:
            con.execute("UPDATE printings SET rarity='rare' WHERE arena_id=101")
        self.fx.publish()
        with self.authority._guarded() as view:
            self.assertEqual(view["con"].execute("SELECT rarity FROM printings WHERE arena_id=101").fetchone()[0], "rare")
        self.assertEqual(reader.execute("SELECT rarity FROM printings WHERE arena_id=101").fetchone()[0], "common")

    def test_close_and_independent_authority_identity(self):
        other = authority.LocalExecutionValidationAuthorityV1(**self.fx.args)
        self.addCleanup(other.close)
        with other._guarded() as right, self.authority._guarded() as left:
            self.assertNotEqual(right["context"]["authority_id"], left["context"]["authority_id"])
        self.authority.close()
        self.error("execution_context_unavailable", self.fx.publish)
        def guard():
            with self.authority._guarded():
                self.fail("closed guard entered")
        self.error("execution_context_unavailable", guard)


if __name__ == "__main__":
    unittest.main()
