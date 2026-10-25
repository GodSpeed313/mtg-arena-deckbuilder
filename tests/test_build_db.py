"""Builder isolation tests: all databases and destination paths are temporary."""
import contextlib
import io
import sqlite3
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build_db


class ExplicitBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        self.source = self.inputs / "cards.mtga"
        self.output = self.root / "candidate.db"
        self.current = self.repo / "current.db"
        self.current.write_bytes(b"frozen sentinel")
        for name, value in (("ROOT", self.repo), ("CURRENT", self.current)):
            p = patch.object(build_db, name, value)
            p.start()
            self.addCleanup(p.stop)
        with contextlib.closing(sqlite3.connect(self.source)) as c:
            c.executescript("""
                CREATE TABLE Localizations_enUS (LocId, Loc, Formatted);
                INSERT INTO Localizations_enUS VALUES
                    (10, 'Example', 1), (20, 'Example Rebalanced', 1),
                    (100, 'Draw one card.', 1), (200, 'Draw two cards.', 1);
                CREATE TABLE Enums (Value, LocId, Type);
                CREATE TABLE Cards (GrpId, TitleId, ExpansionCode,
                    CollectorNumber, Rarity, AbilityIds, OldSchoolManaText,
                    Types, Subtypes, Power, Toughness, Colors, ColorIdentity,
                    IsPrimaryCard, IsToken, IsRebalanced);
                INSERT INTO Cards VALUES
                    (101,10,'TST','007a',3,'1:100','','','','','','','',1,0,0),
                    (202,20,'TST','007a',3,'2:200','','','','','','','',1,0,1);
            """)

    def invoke(self, source=None, output=None):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return build_db.main(["--source", str(source or self.source),
                                  "--output", str(output or self.output)])

    def test_current_refused(self):
        self.assertEqual(self.invoke(output=self.current), 1)
        self.assertEqual(self.current.read_bytes(), b"frozen sentinel")

    def test_existing_refused(self):
        self.output.write_bytes(b"keep")
        self.assertEqual(self.invoke(), 1)
        self.assertEqual(self.output.read_bytes(), b"keep")

    def test_repository_refused(self):
        self.assertEqual(self.invoke(output=self.repo / "new.db"), 1)
        self.assertFalse((self.repo / "new.db").exists())

    def test_source_and_directory_overlap_refused(self):
        before = self.source.read_bytes()
        for output in (self.source, self.inputs, self.inputs / "new.db",
                       self.inputs / "nested" / "new.db", self.root):
            with self.subTest(output=output):
                self.assertEqual(self.invoke(output=output), 1)
        self.assertEqual(self.source.read_bytes(), before)

    def test_alias_to_current_refused(self):
        alias = self.directory_alias()
        self.assertEqual(self.invoke(output=alias / 'current.db'), 1)
        self.assertEqual(self.current.read_bytes(), b"frozen sentinel")

    def test_directory_alias_to_repo_refused(self):
        alias = self.directory_alias()
        self.assertEqual(self.invoke(output=alias / "new.db"), 1)

    def test_dangling_alias_is_existing_destination(self):
        target = self.root / 'vanished'
        target.mkdir()
        alias = self.directory_alias(target)
        target.rmdir()
        self.assertEqual(self.invoke(output=alias), 1)
        self.assertFalse(target.exists())

    def directory_alias(self, target=None):
        target = target or self.repo
        alias = self.root / "alias"
        if os.name == 'nt':
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(alias), str(target)],
                           check=True, capture_output=True)
        else:
            alias.symlink_to(target, target_is_directory=True)
        return alias

    def test_arguments_must_be_paired(self):
        for args in (["--source", str(self.source)], ["--output", str(self.output)]):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    build_db.main(args)
                self.assertEqual(raised.exception.code, 2)

    def test_no_discovery_even_when_source_missing(self):
        with patch('mtgadb.providers.arena_cards.find_card_db', side_effect=AssertionError('discovery')):
            self.assertEqual(self.invoke(source=self.inputs / 'missing.mtga'), 1)
            self.assertEqual(self.invoke(), 0)

    def test_variant_text_and_printing_identity(self):
        self.assertEqual(self.invoke(), 0)
        with contextlib.closing(sqlite3.connect(self.output)) as c:
            self.assertEqual(c.execute('SELECT title_id,rules_text FROM cards ORDER BY title_id').fetchall(),
                             [(10, 'Draw one card.'), (20, 'Draw two cards.')])
            self.assertEqual(c.execute('SELECT arena_id,title_id,set_code,collector_number FROM printings ORDER BY arena_id').fetchall(),
                             [(101,10,'TST','007a'), (202,20,'TST','007a')])

    def test_exclusive_creation_race(self):
        original = Path.open
        def race(path, mode='r', *args, **kwargs):
            if path == self.output and mode == 'xb':
                with original(path, 'wb') as f:
                    f.write(b'competing file')
            return original(path, mode, *args, **kwargs)
        with patch.object(Path, 'open', race):
            self.assertEqual(self.invoke(), 1)
        self.assertEqual(self.output.read_bytes(), b'competing file')

    def test_legacy_build_and_dispatch_unchanged(self):
        from mtgadb.providers.arena_cards import ArenaSQLiteCardProvider
        provider = ArenaSQLiteCardProvider(self.source)
        self.addCleanup(provider.con.close)
        with patch.object(build_db, 'ArenaSQLiteCardProvider', return_value=provider) as factory:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(build_db.main([]), 0)
            factory.assert_called_once_with()
        self.assertEqual(self.current.read_bytes(), next((self.repo / 'snapshots').glob('*.db')).read_bytes())
        with patch.object(build_db, 'stats', return_value=0) as stats:
            self.assertEqual(build_db.main(['--stats']), 0)
            stats.assert_called_once_with()

    def test_stats_rejects_explicit_arguments(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_db.main(['--stats', '--source', str(self.source), '--output', str(self.output)])
