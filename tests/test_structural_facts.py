"""Portable synthetic source fixtures; never require preserved Arena data."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from mtgadb.structural_facts import ExtractionError, extract, readonly, sha256, validate


class StructuralFactsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.inputs = self.root / 'inputs'
        self.inputs.mkdir()
        self.source = self.inputs / 'source.sqlite'
        self.manifest = self.inputs / 'manifest.json'
        self.output = self.root / 'facts.sqlite'
        self.db = sqlite3.connect(self.source)
        self.addCleanup(self.db.close)
        self.db.executescript('''
            CREATE TABLE Cards(GrpId INTEGER PRIMARY KEY, TitleId INTEGER, IsPrimaryCard INTEGER,
                LinkedFaceGrpIds TEXT, LinkedFaceType INTEGER, Types TEXT, Subtypes TEXT,
                Colors TEXT, IndicatorColors TEXT, ColorIdentity TEXT, AbilityIds TEXT,
                HiddenAbilityIds TEXT, IsRebalanced INTEGER, RebalancedCardGrpId INTEGER);
            CREATE TABLE Abilities(Id INTEGER PRIMARY KEY, TextId INTEGER, LoyaltyCost TEXT,
                BaseId INTEGER, BaseIdNumeral INTEGER, HiddenAbilityIds TEXT,
                ReferencedAbilityIds TEXT, IsIntrinsicAbility INTEGER);
            CREATE TABLE Localizations_enUS(LocId INTEGER, Formatted INTEGER, Loc TEXT,
                PRIMARY KEY(LocId,Formatted));
            CREATE TABLE Enums(Type TEXT, Value INTEGER, LocId INTEGER);
            CREATE TABLE AltPrintings(AltGrpId INTEGER, SkinCode TEXT);
            CREATE TABLE AltToBasePrintings(BaseGrpId INTEGER, AltGrpId INTEGER);
            CREATE TABLE Versions(Type TEXT, Version TEXT);
            INSERT INTO Abilities VALUES(166,242813,NULL,0,0,NULL,NULL,0);
            INSERT INTO Localizations_enUS VALUES(242813,1,'SagaChapter');
            INSERT INTO Versions VALUES('Data','test');
        ''')
        self.card(1)

    def card(self, gid, primary=1, code=0, link='', abilities='', **fields):
        self.db.execute('INSERT INTO Cards VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (gid, gid, primary, link, code, '', '', '', '', '', abilities, '', 0, 0))
        self.local(gid, f'Card {gid}')
        for key, value in fields.items():
            self.db.execute(f'UPDATE Cards SET {key}=? WHERE GrpId=?', (value, gid))

    def local(self, tid, value):
        self.db.execute('INSERT OR REPLACE INTO Localizations_enUS VALUES(?,1,?)', (tid, value))

    def ability(self, aid, cost=None, chapter=None, text='CARDNAME generic'):
        self.db.execute('INSERT INTO Abilities VALUES(?,?,?,?,?,NULL,NULL,0)',
                        (aid, aid+1000, cost, 166 if chapter is not None else 0, chapter))
        self.local(aid+1000, text)
        self.local(aid+2000, 'Card-local text')
        return f'{aid}:{aid+2000}'

    def pair(self, codes=(2,1)):
        self.db.execute('UPDATE Cards SET LinkedFaceType=?,LinkedFaceGrpIds=? WHERE GrpId=1', (codes[0], '2'))
        self.card(2, primary=0, code=codes[1], link='1')

    def run_extract(self, output=None):
        self.db.commit()
        self.manifest.write_text(json.dumps({'arena_version': 'synthetic', 'files': [
            {'relative_path': 'source.sqlite', 'sha256': sha256(self.source)}]}), encoding='utf-8')
        return extract(self.source, output or self.output, self.manifest)

    def rows(self, table):
        with closing(readonly(self.output)) as db:
            return [dict(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY 1,2')]

    def test_transform_and_valid_reciprocal_not_cycle(self):
        self.pair()
        self.run_extract()
        self.assertEqual([(r['kind'],r['status']) for r in self.rows('links')], [('transform','recovered')]*2)

    def test_omen_not_transform(self):
        self.pair((18,17))
        self.run_extract()
        self.assertEqual([r['kind'] for r in self.rows('links')], ['omen']*2)

    def test_unknown_layout_retained(self):
        self.pair((90,91))
        result=self.run_extract()
        self.assertEqual(result['measurements']['families']['links']['recovered'],0)
        self.assertEqual(json.loads(self.rows('links')[0]['facts_json'])['source_code'],90)

    def test_missing_target(self):
        self.db.execute("UPDATE Cards SET LinkedFaceGrpIds='99',LinkedFaceType=2")
        with self.assertRaisesRegex(ExtractionError,'missing_target'):
            self.run_extract()
        self.assertFalse(self.output.exists())

    def test_malformed_link_kept_with_diagnostic(self):
        self.db.execute("UPDATE Cards SET LinkedFaceGrpIds='not-an-id'")
        with self.assertRaisesRegex(ExtractionError,'malformed_link'):
            self.run_extract()
        self.assertFalse(self.output.exists())

    def test_nonreciprocal_link(self):
        self.pair()
        self.db.execute("UPDATE Cards SET LinkedFaceGrpIds='',LinkedFaceType=0 WHERE GrpId=2")
        self.run_extract()
        self.assertEqual(self.rows('links')[0]['status'],'nonreciprocal_link')

    def test_conflicting_reviewed_layout(self):
        self.pair((2,17))
        self.run_extract()
        self.assertEqual(self.rows('links')[0]['status'],'conflicting_layout')

    def test_cycle_longer_than_two(self):
        self.pair()
        self.db.execute("UPDATE Cards SET LinkedFaceGrpIds='3' WHERE GrpId=2")
        self.card(3,primary=0,code=1,link='1')
        self.run_extract()
        self.assertEqual([r['status'] for r in self.rows('links')],['invalid_cycle']*3)

    def test_self_cycle(self):
        self.db.execute("UPDATE Cards SET LinkedFaceGrpIds='1'")
        self.run_extract()
        self.assertEqual(self.rows('links')[0]['status'],'invalid_cycle')

    def test_cycle_with_cross_edges(self):
        self.pair()
        self.db.execute("UPDATE Cards SET LinkedFaceGrpIds='2,3' WHERE GrpId=1")
        self.card(3,primary=0,code=1,link='2')
        self.run_extract()
        self.assertEqual({r['status'] for r in self.rows('links')},{'invalid_cycle'})

    def test_multiple_components_retained_as_unsupported(self):
        self.pair()
        self.db.execute("UPDATE Cards SET LinkedFaceGrpIds='2,3' WHERE GrpId=1")
        self.card(3,primary=0,code=1,link='1')
        self.run_extract()
        self.assertEqual(len(self.rows('links')),4)
        self.assertEqual({r['status'] for r in self.rows('links')},{'unsupported_layout'})

    def test_secondary_has_no_ownership(self):
        self.pair()
        owned={1:3}
        self.run_extract()
        self.assertEqual(owned,{1:3})
        with closing(readonly(self.output)) as db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse({'ownership','deck','collection'} & tables)
        self.assertEqual(len(self.rows('components')),2)

    def test_signed_null_and_variable_costs_exact_binding(self):
        self.pair()
        pairs=[self.ability(10+i,cost=c) for i,c in enumerate(['+2','0','-6',None,'-X'])]
        self.db.execute('UPDATE Cards SET AbilityIds=? WHERE GrpId=2',(','.join(pairs),))
        self.run_extract()
        self.assertEqual([(r['grp_id'],r['ability_id'],r['loyalty_cost']) for r in self.rows('entries')],
                         [(2,10+i,c) for i,c in enumerate(['+2','0','-6',None,'-X'])])

    def test_chapter_order_repeated_text_and_nonchapters(self):
        pairs=[self.ability(20+i,chapter=i+1) for i in range(4)]
        pairs.extend([self.ability(30,text='Reach'),self.ability(31,text='Trample')])
        self.db.execute('UPDATE Cards SET AbilityIds=?',(','.join(pairs),))
        self.run_extract()
        entries=self.rows('entries')
        self.assertEqual([e['chapter_index'] for e in entries],[1,2,3,4,None,None])
        self.assertEqual([e['position'] for e in entries],list(range(6)))
        self.assertEqual([json.loads(e['facts_json'])['card_local']['text'] for e in entries[:4]],['Card-local text']*4)

    def test_text_resembling_chapter_not_classified(self):
        pair=self.ability(20,text='I, II, III — Saga chapter')
        self.db.execute('UPDATE Cards SET AbilityIds=?',(pair,))
        self.run_extract()
        self.assertIsNone(self.rows('entries')[0]['chapter_index'])

    def test_local_generic_and_hidden_not_spliced(self):
        pair=self.ability(20)
        self.ability(21,text='Hidden delayed trigger')
        self.db.execute("UPDATE Abilities SET HiddenAbilityIds='21',ReferencedAbilityIds='21' WHERE Id=20")
        self.db.execute("UPDATE Cards SET AbilityIds=?,HiddenAbilityIds='21'",(pair,))
        self.run_extract()
        entries=self.rows('entries')
        self.assertEqual(len(entries),1)
        fact=json.loads(entries[0]['facts_json'])
        self.assertEqual(fact['generic']['text'],'CARDNAME generic')
        self.assertEqual(fact['card_local']['text'],'Card-local text')
        self.assertEqual(fact['ability']['HiddenAbilityIds'],'21')

    def test_null_empty_zero_absent_and_colors(self):
        self.db.execute("UPDATE Cards SET ColorIdentity='4',IsRebalanced=0,RebalancedCardGrpId=NULL")
        self.run_extract()
        fact=json.loads(self.rows('components')[0]['facts_json'])
        self.assertEqual(fact['source']['Colors'],'')
        self.assertEqual(fact['source']['IndicatorColors'],'')
        self.assertEqual(fact['gameplay_color'],'unresolved')
        self.assertEqual(fact['source']['IsRebalanced'],0)
        self.assertIsNone(fact['source']['RebalancedCardGrpId'])
        self.assertNotIn('DefunctRebalancedCardGrpId',fact['source'])

    def test_variant_conflicts_retained(self):
        self.db.execute('UPDATE Cards SET IsRebalanced=1,RebalancedCardGrpId=999')
        self.db.execute("INSERT INTO AltPrintings VALUES(1,'skin')")
        self.db.executemany('INSERT INTO AltToBasePrintings VALUES(?,1)',[(77,),(88,)])
        self.run_extract()
        evidence=[json.loads(r['raw_json']) for r in self.rows('source_records') if r['table_name']=='AltToBasePrintings']
        self.assertEqual(len(evidence),2)
        self.assertEqual(json.loads(self.rows('components')[0]['facts_json'])['source']['RebalancedCardGrpId'],999)

    def test_provenance_and_source_readonly(self):
        result=self.run_extract()
        self.assertEqual(result['schema_version'],1)
        self.assertEqual(result['contract_version'],1)
        self.assertEqual(result['source_sha256'],sha256(self.source))
        self.assertEqual(result['manifest_sha256'],sha256(self.manifest))
        self.assertIn('+00:00',result['extracted_at_utc'])
        with closing(readonly(self.source)) as db:
            self.assertEqual(db.execute('PRAGMA query_only').fetchone()[0],1)
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('DELETE FROM Cards')

    def test_destination_exclusive(self):
        self.run_extract()
        before=sha256(self.output)
        with self.assertRaisesRegex(ExtractionError,'already exists'):
            self.run_extract()
        self.assertEqual(sha256(self.output),before)
        self.assertEqual({p.name for p in self.root.iterdir()},{'inputs','facts.sqlite','facts.sqlite.summary.json'})

    def test_source_directory_destination_refused(self):
        with self.assertRaisesRegex(ExtractionError,'outside'):
            self.run_extract(self.inputs/'output.sqlite')

    def test_incomplete_schema_no_partial_artifact(self):
        self.db.execute('DROP TABLE Abilities')
        with self.assertRaisesRegex(ExtractionError,'missing required columns'):
            self.run_extract()
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob('.structural-*')),[])

    def test_wrong_saga_base_no_artifact(self):
        self.db.execute("UPDATE Localizations_enUS SET Loc='Wrong' WHERE LocId=242813")
        with self.assertRaisesRegex(ExtractionError,'Saga base'):
            self.run_extract()
        self.assertFalse(self.output.exists())

    def test_invalid_chapter_index_no_artifact(self):
        pair=self.ability(20,chapter=0)
        self.db.execute('UPDATE Cards SET AbilityIds=?',(pair,))
        with self.assertRaisesRegex(ExtractionError,'invalid_chapter_index'):
            self.run_extract()
        self.assertFalse(self.output.exists())

    def test_missing_localization_no_artifact(self):
        pair=self.ability(20)
        self.db.execute('UPDATE Cards SET AbilityIds=?',(pair,))
        self.db.execute('DELETE FROM Localizations_enUS WHERE LocId=2020')
        with self.assertRaisesRegex(ExtractionError,'missing_localization'):
            self.run_extract()
        self.assertFalse(self.output.exists())

    def test_nameless_source_sentinel_is_diagnosed(self):
        self.db.execute('UPDATE Cards SET TitleId=0')
        result=self.run_extract()
        self.assertEqual(result['measurements']['diagnostics_by_reason']['unavailable_component_name'],1)
        self.assertFalse(json.loads(self.rows('components')[0]['facts_json'])['name']['present'])

    def test_repository_destination_refused(self):
        with self.assertRaisesRegex(ExtractionError,'outside'):
            self.run_extract(Path(__file__).resolve().parents[1]/'must-not-exist.sqlite')

    def test_bad_manifest_no_artifact(self):
        self.db.commit()
        self.manifest.write_text('{"arena_version":"test","files":[]}',encoding='utf-8')
        with self.assertRaisesRegex(ExtractionError,'manifest'):
            extract(self.source,self.output,self.manifest)
        self.assertFalse(self.output.exists())

    def test_missing_ability_denominator_not_measurable(self):
        self.db.execute("UPDATE Cards SET AbilityIds='999:1000,broken'")
        with self.assertRaises(ExtractionError) as caught:
            self.run_extract()
        measurements=caught.exception.measurements
        for family in ('chapters','loyalty'):
            self.assertEqual(measurements['families'][family]['measurement'],'NOT MEASURABLE')
            self.assertEqual(measurements['families'][family]['unknown_eligibility'],2)
            self.assertIsNone(measurements['families'][family]['recovery_percent'])
        self.assertFalse(self.output.exists())

    def test_repeatability_and_validation_detects_tamper(self):
        self.pair()
        first=self.run_extract()
        second=self.run_extract(self.root/'second.sqlite')
        self.assertEqual(first['payload_sha256'],second['payload_sha256'])
        self.assertEqual(validate(self.output)['payload_sha256'],first['payload_sha256'])
        with closing(sqlite3.connect(self.output)) as db:
            db.execute("UPDATE links SET kind='wrong'")
            db.commit()
        with self.assertRaisesRegex(ExtractionError,'digest mismatch'):
            validate(self.output)


if __name__ == '__main__':
    unittest.main()
