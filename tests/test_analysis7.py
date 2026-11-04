"""Synthetic linked components; reuse Run 16 fixture setup without its tests."""
from contextlib import closing
from dataclasses import replace
import json
import sqlite3
import unittest
from unittest.mock import patch

from tests import test_analysis6 as fixtures
from mtgadb import structural_evidence as se
from mtgadb.structural_facts import encoded, payload_digest, readonly, FACT_TABLES
from services import analysis7
from services.analysis6 import analyze_deck6
from services.intelligence import analyze_deck


class LinkedFaceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.Analysis6Tests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.face_entries = [replace(e,grp_id=90000) for e in f.entries]
        with closing(sqlite3.connect(f.artifact)) as db:
            for gid, primary, code, target in ((75245,1,2,90000),(90000,0,1,75245)):
                component = {'source':{'GrpId':gid,'TitleId':gid,'IsPrimaryCard':primary,
                    'LinkedFaceType':code,'LinkedFaceGrpIds':str(target),'Types':'2,5','Subtypes':'',
                    'Colors':'','IndicatorColors':'','ColorIdentity':'4','HiddenAbilityIds':'666'},
                    'name':{'id':gid,'text':'Identical name','present':True},
                    'types':[{'localization':{'text':t,'present':True}} for t in ('Creature','Enchantment')],
                    'subtypes':[],'gameplay_color':'unresolved'}
                db.execute('INSERT INTO components VALUES(?,?)',(gid,encoded(component)))
                db.execute('INSERT INTO links VALUES(?,0,?,?,?,?)',(gid,target,'transform','recovered','{}'))
            for e in self.face_entries:
                fact={'ability_present':True,'card_local':{'id':e.local_text_id,'text':e.local_text,'present':True},
                      'generic':{'id':e.generic_text_id,'text':'Scry 2.','present':True}}
                db.execute('INSERT INTO entries VALUES(?,?,?,?,?,?,?)',(e.grp_id,e.position,e.ability_id,
                           e.chapter_index,e.loyalty_cost,'recovered',encoded(fact)))
            db.execute('INSERT INTO source_records VALUES(?,?,?)',('Abilities','[666]',encoded({'Id':666,'text':'Hidden trigger fragment'})))
            db.commit()
        self.seal()

    def seal(self):
        f=self.fixture
        with closing(sqlite3.connect(f.artifact)) as db:
            digest=payload_digest(db)
            db.execute('UPDATE metadata SET value=? WHERE key=?',(encoded(digest),'payload_sha256'))
            db.execute('UPDATE metadata SET value=? WHERE key=?',
                       (encoded({t:db.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in FACT_TABLES}),'table_counts'))
            db.commit()
        f.refresh_manifest()
        p=patch.object(se,'PAYLOAD',digest);p.start();self.addCleanup(p.stop)

    def modify_card(self,gid,**values):
        with closing(sqlite3.connect(self.fixture.artifact)) as db:
            data=json.loads(db.execute('SELECT facts_json FROM components WHERE grp_id=?',(gid,)).fetchone()[0])
            data['source'].update(values)
            db.execute('UPDATE components SET facts_json=? WHERE grp_id=?',(encoded(data),gid));db.commit()
        self.seal()

    def evidence(self):
        f=self.fixture
        return analysis7.LinkedEvidence(f.artifact,f.manifest,f.raw,f.source_manifest)

    def output(self):
        with closing(readonly(self.fixture.candidate)) as db:
            return analysis7.analyze_deck7(self.fixture.deck,db,self.evidence())

    def component(self):
        return self.output()['linked_face_analysis']['zones']['main']['roots'][0]['components'][0]

    def test_reviewed_transform_attaches_exact_id(self):
        c=self.component()
        self.assertEqual((c['grp_id'],c['relationship'],c['root_linked_face_type'],c['face_linked_face_type']),
                         (90000,'transform',2,1))

    def test_reviewed_omen_has_only_omen_relationship(self):
        self.modify_card(75245,LinkedFaceType=18);self.modify_card(90000,LinkedFaceType=17)
        with closing(sqlite3.connect(self.fixture.artifact)) as db:
            db.execute("UPDATE links SET kind='omen'");db.commit()
        self.seal();c=self.component()
        self.assertEqual(c['relationship'],'omen')
        self.assertNotIn('transform_condition',c)
        self.assertNotIn('casting',c)

    def test_unknown_codes_never_attach(self):
        for code in list(range(3,17))+[19,20]:
            with self.subTest(code=code):
                self.modify_card(75245,LinkedFaceType=code)
                attached,skipped=self.evidence().components(75245)
                self.assertEqual(attached,[]);self.assertEqual(skipped[0]['reason'],'unknown_code')

    def test_flag_conflict_never_attaches(self):
        self.modify_card(75245,IsPrimaryCard=0)
        a,s=self.evidence().components(75245)
        self.assertFalse(a);self.assertEqual(s[0]['reason'],'flag_conflict')

    def test_nonreciprocal_link_never_attaches(self):
        self.modify_card(90000,LinkedFaceGrpIds='999')
        a,s=self.evidence().components(75245)
        self.assertFalse(a);self.assertEqual(s[0]['reason'],'nonreciprocal_or_multiple_links')

    def test_nonreciprocal_relationship_rows_never_attach(self):
        with closing(sqlite3.connect(self.fixture.artifact)) as db:
            db.execute('UPDATE links SET target_id=999 WHERE grp_id=90000');db.commit()
        self.seal();self.assertFalse(self.evidence().components(75245)[0])

    def test_conflicting_reviewed_codes_excluded(self):
        self.modify_card(90000,LinkedFaceType=17)
        a,s=self.evidence().components(75245)
        self.assertFalse(a);self.assertEqual(s[0]['reason'],'unreviewed_signature')

    def test_stored_unknown_kind_not_promoted_from_codes(self):
        with closing(sqlite3.connect(self.fixture.artifact)) as db:
            db.execute("UPDATE links SET kind='unknown'");db.commit()
        self.seal();self.assertFalse(self.evidence().components(75245)[0])

    def test_missing_text_is_reported_without_attachment(self):
        with closing(sqlite3.connect(self.fixture.artifact)) as db:
            db.execute('DELETE FROM entries WHERE grp_id=90000');db.commit()
        self.seal();a,s=self.evidence().components(75245)
        self.assertFalse(a);self.assertEqual(s[0]['reason'],'missing_text')

    def test_no_name_lookup_or_reverse_face_following(self):
        evidence=self.evidence()
        self.assertEqual(evidence.components(12345),([],[]))
        with self.assertRaises(se.EvidenceError):evidence.components('Identical name')
        self.assertFalse(evidence.components(90000)[0])

    def test_ownership_and_prior_outputs_unchanged(self):
        evidence=self.evidence();deck=self.fixture.deck
        before_deck=dict(deck.main)
        with closing(readonly(self.fixture.candidate)) as db:
            old5=analyze_deck(deck,db);old6=analyze_deck6(deck,db,evidence.base)
            new=analysis7.analyze_deck7(deck,db,evidence)
            after5=analyze_deck(deck,db);after6=analyze_deck6(deck,db,evidence.base)
        layer=new.pop('linked_face_analysis')
        self.assertEqual(fixtures.serialized(new),fixtures.serialized(old6))
        self.assertEqual(fixtures.serialized(old6),fixtures.serialized(after6))
        self.assertEqual(fixtures.serialized(old5),fixtures.serialized(after5))
        self.assertEqual(deck.main,before_deck);self.assertEqual(deck.size(),2)
        self.assertEqual(layer['zones']['main']['copy_weighted']['ownership_or_deck_slots_added'],0)

    def test_costs_chapters_source_native_not_bound(self):
        c=self.component();entries=c['abilities']
        self.assertEqual([e['structures'][0]['raw_cost'] for e in entries[:4]],['+2','0','-6','-X'])
        self.assertEqual([e['structures'][0]['chapter_index'] for e in entries[5:9]],[1,2,3,4])
        for e in entries:
            self.assertNotIn('binding',e)
            if e['structures']:self.assertEqual(e['structural_evidence'],'source-native')
        self.assertIsNone(entries[4]['source_entry']['loyalty_cost'])
        self.assertEqual(entries[4]['structures'],[])

    def test_population_separate_from_analysis6(self):
        out=self.output();old=out['structural_analysis']['zones']['main']['structural_axis']['copy_weighted']
        new=out['linked_face_analysis']['zones']['main']['copy_weighted']
        self.assertEqual(old['bound'],16)
        self.assertEqual(new['source_native_entries'],16)
        self.assertNotIn('bound',new)

    def test_generic_not_parsed_hidden_not_spliced(self):
        with patch.object(analysis7,'_parse_line',wraps=analysis7._parse_line) as grammar:
            c=self.component()
        self.assertEqual(grammar.call_count,len(self.face_entries))
        self.assertEqual([call.args[0] for call in grammar.call_args_list],[e.local_text for e in self.face_entries])
        self.assertEqual(len(c['abilities']),11)
        self.assertEqual(c['abilities'][2]['source_entry']['generic_text'],'Scry 2.')
        self.assertEqual(c['abilities'][2]['linked_face_effect']['parse_status'],'unsupported')

    def test_face_specific_empty_colors_preserved(self):
        c=self.component()
        self.assertEqual((c['Colors'],c['IndicatorColors'],c['ColorIdentity']),('','','4'))
        self.assertEqual(c['gameplay_color'],'unresolved')

    def test_no_game_state_claims(self):
        c=self.component()
        self.assertFalse({'in_play','battlefield','transform_conditions','casting_permission','activation_legal'} & c.keys())

    def test_fail_closed_independent_payload_and_provenance(self):
        for key in ('PAYLOAD','SOURCE','SOURCE_MANIFEST','ARENA_VERSION'):
            with self.subTest(pin=key),patch.object(se,key,'wrong'):
                with self.assertRaises(se.EvidenceError):self.evidence()

    def test_fail_closed_wrong_file_hash(self):
        f=self.fixture;f.manifest.write_text(json.dumps({'files':{f.artifact.name:{'sha256':'wrong'}}}))
        with self.assertRaises(se.EvidenceError):self.evidence()

    def test_fail_closed_missing_artifact(self):
        f=self.fixture
        with self.assertRaises(se.EvidenceError):analysis7.LinkedEvidence(f.root/'missing.sqlite',f.manifest,f.raw,f.source_manifest)

    def test_fail_closed_before_new_parser_output(self):
        with patch.object(se,'PAYLOAD','wrong'),patch.object(analysis7,'_parse_line') as grammar:
            with self.assertRaises(se.EvidenceError):self.output()
            grammar.assert_not_called()


if __name__=='__main__':
    unittest.main()
