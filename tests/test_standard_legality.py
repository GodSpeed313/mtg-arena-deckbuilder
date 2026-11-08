import gzip
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager

from mtgadb import canonical
from mtgadb.standard_legality import (DETERMINATION, FINAL_AUTHORITY, SET_DISAGREEMENT, LegalityError,
    assess, bindings, build_snapshot, load_snapshot, query, readonly, sha256)


@contextmanager
def fixture_db(path):
    con = sqlite3.connect(path)
    try:
        with con:
            yield con
    finally:
        con.close()


class StandardLegalityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = {k: self.root / (k + ('.jsonl.gz' if k == 'scryfall' else '.db'))
                      for k in ['candidate', 'arena', 'formats', 'scryfall']}
        for key in ['candidate', 'formats']:
            with fixture_db(self.paths[key]) as c:
                c.executescript(canonical.SCHEMA)
        with fixture_db(self.paths['candidate']) as c:
            for tid in range(1, 6):
                c.execute('INSERT INTO cards(title_id,name) VALUES (?,?)', (tid, 'Card ' + str(tid)))
                c.execute('INSERT INTO printings(arena_id,title_id,set_code) VALUES (?,?,?)', (100 + tid, tid, 'TST'))
        with fixture_db(self.paths['arena']) as c:
            c.executescript('''CREATE TABLE Cards(GrpId INTEGER PRIMARY KEY,TitleId INTEGER,Types TEXT,Supertypes TEXT,IsPrimaryCard INTEGER,IsToken INTEGER,IsRebalanced INTEGER);
            CREATE TABLE Localizations_enUS(LocId INTEGER,Formatted INTEGER,Loc TEXT);
            CREATE TABLE Enums(Type TEXT,Value INTEGER,LocId INTEGER);
            INSERT INTO Localizations_enUS VALUES(1,1,'Land'),(2,1,'Basic'),(3,1,'Creature');
            INSERT INTO Enums VALUES('CardType',1,1),('SuperType',1,2),('CardType',2,3);''')
            for tid in range(1, 6):
                c.execute('INSERT INTO Cards VALUES(?,?,?,?,1,0,0)', (100 + tid, tid, '1' if tid == 5 else '2', '1' if tid == 5 else ''))
        with fixture_db(self.paths['formats']) as c:
            for name in ['Standard', 'TraditionalStandard']:
                c.execute('INSERT INTO formats(name,min_deck_size,max_deck_size,max_sideboard) VALUES(?,60,250,15)', (name,))
                c.execute('INSERT INTO format_sets VALUES(?,?)', (name, 'TST'))
            c.execute("INSERT INTO meta VALUES('formats_loaded_at','2026-09-12T23:28:52+00:00')")
        self.rows = [self.row(t) for t in range(1, 6)]
        self.write_rows()

    def row(self, tid, **changes):
        row = dict(id='printing-' + str(tid), oracle_id='oracle-' + str(tid), arena_id=100 + tid,
                   name='Card ' + str(tid), games=['arena'], legalities={'standard': 'legal'})
        row.update(changes)
        return row

    def write_rows(self):
        with gzip.open(self.paths['scryfall'], 'wt', encoding='utf-8') as f:
            for row in self.rows:
                f.write(json.dumps(row) + '\n')

    def pins(self):
        return {k: sha256(p) for k, p in self.paths.items()}

    def result(self, **kw):
        return assess(self.paths, self.pins(), kw.get('binding', bindings()), kw.get('queues', ['BO1', 'BO3']))

    def state(self, result, tid=1, queue='BO1'):
        return result['titles'][str(tid)]['queues'][queue]['status']

    def ban(self, fmt, tid, rule='banned'):
        with fixture_db(self.paths['formats']) as c:
            c.execute('INSERT INTO format_title_rules VALUES(?,?,?)', (fmt, tid, rule))

    def test_01_clean_exact_match(self):
        self.assertEqual(self.state(self.result()), 'legal')

    def test_02_conflicting_matches(self):
        self.rows.append(self.row(1, id='other', legalities={'standard': 'banned'}))
        self.write_rows()
        self.assertEqual(self.state(self.result()), 'unknown')

    def test_03_duplicate_identity_conflict(self):
        self.rows.append(self.row(1, id='other', oracle_id='other-card'))
        self.write_rows()
        self.assertEqual(self.state(self.result()), 'unknown')

    def test_04_arena_basic_overrides_reference_conflict(self):
        self.rows.append(self.row(5, id='other', name='Wrong', oracle_id='wrong', legalities={'standard': 'banned'}))
        self.write_rows()
        result = self.result()
        for q in ['BO1', 'BO3']:
            self.assertEqual(self.state(result, 5, q), 'legal')
        self.assertTrue(result['titles']['5']['source_conflicts'])
        self.assertTrue(result['titles']['5']['basic_evidence'])

    def test_05_one_queue_restriction(self):
        self.ban('Standard', 1)
        result = self.result()
        self.assertEqual(self.state(result), 'illegal')
        self.assertEqual(self.state(result, queue='BO3'), 'legal')

    def test_06_undetermined_overlay_cannot_build(self):
        with self.assertRaises(LegalityError):
            self.result(binding={})
        with fixture_db(self.paths['formats']) as c:
            c.execute("DELETE FROM formats WHERE name='Standard'")
        with self.assertRaises(LegalityError):
            self.result()

    def test_07_no_arena_id_unknown_not_illegal(self):
        self.rows[0]['arena_id'] = None
        self.write_rows()
        result = self.result()
        self.assertEqual(self.state(result), 'unknown')
        self.assertEqual(result['coverage']['standard_legal_no_arena_id_only_oracles'], 1)

    def test_08_identity_cardinality_unknown(self):
        self.rows[1]['oracle_id'] = self.rows[0]['oracle_id']
        self.write_rows()
        result = self.result()
        self.assertEqual(self.state(result), 'unknown')
        self.assertEqual(self.state(result, 2), 'unknown')

    def test_09_secondary_match_not_admitted(self):
        self.rows[0].update(arena_id=9999, set='TST', collector_number='1')
        self.write_rows()
        self.assertEqual(self.state(self.result()), 'unknown')

    def build(self, name='store', queues=None, **kw):
        return build_snapshot(self.paths, self.pins(), bindings(), queues or ['BO1', 'BO3'],
                              self.root / name, **kw)

    def test_10_queue_isolation(self):
        path = self.build(queues=['BO1'])
        with self.assertRaises(LegalityError):
            query(path, 'BO3', title_id=1)
        self.assertEqual(query(path, 'BO1', arena_id=101)['status'], 'legal')

    def test_11_all_hash_mismatches_fail_closed(self):
        for key in self.paths:
            pins = self.pins()
            pins[key] = '0' * 64
            with self.assertRaises(LegalityError):
                assess(self.paths, pins, bindings(), ['BO1'])

    def test_12_provenance_dates_and_final_authority(self):
        p = load_snapshot(self.build())['payload']
        self.assertEqual(p['sources']['formats']['as_of'], '2026-09-12')
        self.assertEqual(p['final_authority'], FINAL_AUTHORITY)
        self.assertEqual(p['determination_verbatim'], DETERMINATION)

    def test_13_restricted_in_both_records(self):
        self.ban('Standard', 2)
        self.ban('TraditionalStandard', 2, 'suspended')
        result = self.result()
        for q, fmt, rule in [('BO1', 'Standard', 'banned'), ('BO3', 'TraditionalStandard', 'suspended')]:
            row = result['titles']['2']['queues'][q]
            self.assertEqual(row['status'], 'illegal')
            self.assertEqual(row['source_restrictions'][0]['format'], fmt)
            self.assertEqual(row['source_restrictions'][0]['rule'], rule)

    def test_14_only_one_record_preserves_source(self):
        self.ban('TraditionalStandard', 3, 'suppressed')
        result = self.result()
        self.assertEqual(self.state(result, 3), 'legal')
        row = result['titles']['3']['queues']['BO3']
        self.assertEqual(row['status'], 'illegal')
        self.assertEqual(row['source_restrictions'][0]['format'], 'TraditionalStandard')
        self.assertEqual(row['source_restrictions'][0]['rule'], 'suppressed')

    def test_15_both_queue_binding_provenances(self):
        for q in load_snapshot(self.build())['payload']['queues'].values():
            p = q['provenance']
            self.assertEqual(p['overlay_binding'], 'operator_determination')
            self.assertEqual(p['determination_date'], '2026-11-06')
            self.assertEqual(len(p['evidence']), 3)
            self.assertIn('not independently verified', p['statement'])

    def test_regeneration_and_input_protection(self):
        before = self.pins()
        a, b = self.build('a', generated_at='fixed'), self.build('b', generated_at='fixed')
        self.assertEqual(a.read_bytes(), b.read_bytes())
        self.assertEqual(before, self.pins())
        con = readonly(self.paths['candidate'])
        try:
            with self.assertRaises(sqlite3.OperationalError):
                con.execute('DELETE FROM cards')
        finally:
            con.close()

    def test_basic_name_is_not_type_evidence(self):
        self.rows[0]['name'] = 'Plains'
        self.rows[0]['legalities']['standard'] = 'not_legal'
        self.write_rows()
        result = self.result()
        self.assertFalse(result['titles']['1']['basic'])
        self.assertEqual(self.state(result), 'illegal')

    def test_older_set_omission_is_unknown_without_semantics_and_applicability(self):
        with fixture_db(self.paths['candidate']) as c:
            c.execute("UPDATE printings SET set_code='OLD' WHERE title_id=1")
        result = self.result()
        for queue in ['BO1', 'BO3']:
            row = result['titles']['1']['queues'][queue]
            self.assertEqual(row['status'], 'unknown')
            self.assertEqual(row['reason'], SET_DISAGREEMENT)
            self.assertEqual(row['source_restrictions'], [])
            evidence = row['source_disagreements'][0]
            self.assertEqual(evidence['base_claim']['status'], 'legal')
            self.assertEqual(evidence['base_claim']['exact_record_ids'], ['printing-1'])
            self.assertEqual(evidence['overlay_claim']['as_of'], '2026-09-12')
            self.assertEqual(evidence['overlay_claim']['title_sets'], ['OLD'])
            self.assertEqual(evidence['restrictive_semantics'], 'unestablished')
            self.assertEqual(evidence['applicability'], 'unestablished')
        self.assertEqual(result['titles']['1']['base_state'], 'legal')
        self.ban('Standard', 1, 'allowed')
        self.assertEqual(self.state(self.result()), 'legal')

    def test_affirmative_restriction_survives_set_disagreement(self):
        with fixture_db(self.paths['candidate']) as c:
            c.execute("UPDATE printings SET set_code='LATER' WHERE title_id=1")
        self.ban('Standard', 1, 'banned')
        self.ban('TraditionalStandard', 1, 'suspended')
        result = self.result()
        for queue, fmt, kind in [('BO1', 'Standard', 'banned'), ('BO3', 'TraditionalStandard', 'suspended')]:
            row = result['titles']['1']['queues'][queue]
            self.assertEqual(row['status'], 'illegal')
            self.assertEqual(row['reason'], 'overlay_restriction')
            self.assertEqual(row['source_restrictions'][0]['format'], fmt)
            self.assertEqual(row['source_restrictions'][0]['rule'], kind)
            self.assertEqual(row['source_restrictions'][0]['title_id'], 1)
            self.assertTrue(row['source_disagreements'])

    def test_queue_queries_and_unknown_queue_fail_closed(self):
        self.ban('Standard', 1)
        path = self.build()
        self.assertEqual(query(path, 'BO1', title_id=1)['status'], 'illegal')
        self.assertEqual(query(path, 'BO3', title_id=1)['status'], 'legal')
        for queue in ['unknown', 'bo1', 'Standard', '']:
            with self.assertRaises(LegalityError):
                query(path, queue, title_id=1)

    def test_digital_and_faces_do_not_add_owned_cards(self):
        self.rows[0].update(digital=True, card_faces=[{'name': 'Front'}, {'name': 'Back'}])
        self.write_rows()
        result = self.result()
        self.assertEqual(self.state(result), 'legal')
        self.assertEqual(len(result['printing_to_title']), 5)

    def test_arena_availability_required(self):
        self.rows[0]['games'] = ['paper']
        self.write_rows()
        self.assertEqual(self.state(self.result()), 'unknown')

    def test_corrupt_snapshot_and_existing_directory_rejected(self):
        path = self.build()
        with self.assertRaises(LegalityError):
            self.build()
        doc = json.loads(path.read_text())
        doc['payload']['titles']['1']['queues']['BO1']['status'] = 'illegal'
        path.write_text(json.dumps(doc))
        with self.assertRaises(LegalityError):
            query(path, 'BO1', title_id=1)

    def test_missing_identity_not_illegal(self):
        self.assertEqual(query(self.build(), 'BO1', arena_id=999)['status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
