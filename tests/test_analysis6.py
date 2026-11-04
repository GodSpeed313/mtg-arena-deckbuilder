"""Synthetic-only evidence, identity, and additive-contract tests."""
from contextlib import closing, redirect_stdout
from dataclasses import replace
import json
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from mtgadb import canonical
from mtgadb.model import Card, CardPrinting, Deck
from mtgadb.structural_facts import FACT_TABLES, encoded, payload_digest, readonly, sha256
from mtgadb import structural_evidence as se
from services.analysis6 import analyze_deck6, bind_card, structural_counts
from services.intelligence import analyze_deck, classify_card


def serialized(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


class Analysis6Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.artifact = self.root / 'structural-facts-v1.sqlite'
        self.manifest = self.root / 'output-manifest.json'
        self.raw = self.root / 'raw.sqlite'
        self.source_manifest = self.root / 'source.json'
        with closing(sqlite3.connect(self.raw)) as db:
            db.execute('CREATE TABLE source(value)')
            db.commit()
        self.source_manifest.write_text('{}', encoding='utf-8')
        costs = ['+2', '0', '-6', '-X', None, None, None, None, None, None, None]
        texts = ['Draw two cards.', 'Scry 2.', 'Unknown effect.', 'Destroy target creature.',
                 '{T}: Add {B}.', 'Draw a card.', 'Draw a card.', 'Scry 2.',
                 'Unknown chapter.', 'Reach', 'Trample']
        self.entries = [se.Entry(75245, i, 100+i, 200+i, text, 300+i,
                                'CARDNAME generic different', costs[i], i-4 if 5 <= i <= 8 else None)
                        for i, text in enumerate(texts)]
        with closing(sqlite3.connect(self.artifact)) as db:
            db.executescript('''
                PRAGMA user_version=1;
                CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE source_records(table_name,source_key,raw_json);
                CREATE TABLE components(grp_id,facts_json);
                CREATE TABLE links(grp_id,position,target_id,kind,status,facts_json);
                CREATE TABLE diagnostics(scope,source_key,reason,detail_json);
                CREATE TABLE entries(grp_id,position,ability_id,chapter_index,loyalty_cost,status,facts_json);
            ''')
            for entry in self.entries:
                fact = {'ability_present': True, 'card_local': {'id':entry.local_text_id,'text':entry.local_text,'present':True},
                        'generic': {'id':entry.generic_text_id,'text':entry.generic_text,'present':True}}
                db.execute('INSERT INTO entries VALUES(?,?,?,?,?,?,?)',
                           (entry.grp_id,entry.position,entry.ability_id,entry.chapter_index,entry.loyalty_cost,'recovered',encoded(fact)))
            meta = {'state':'complete','contract_version':1,'schema_version':1,'arena_version':'synthetic',
                    'implementation_sha256':'synthetic','source_sha256':sha256(self.raw),
                    'manifest_sha256':sha256(self.source_manifest),'extracted_at_utc':'synthetic',
                    'payload_sha256':payload_digest(db),
                    'table_counts':{t:db.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in FACT_TABLES}}
            db.executemany('INSERT INTO metadata VALUES(?,?)',[(k,encoded(v)) for k,v in meta.items()])
            db.commit()
        self.refresh_manifest()
        pins = patch.multiple(se, PAYLOAD=meta['payload_sha256'], SOURCE=meta['source_sha256'],
                              SOURCE_MANIFEST=meta['manifest_sha256'], ARENA_VERSION='synthetic')
        pins.start()
        self.addCleanup(pins.stop)
        self.candidate = self.root/'candidate.sqlite'
        with closing(canonical.create(self.candidate)) as db:
            canonical.load_cards(db,[Card(1,'Synthetic',types='Planeswalker',source_printing_id=93292,
                                          rules_text='\n'.join(texts))])
            canonical.load_printings(db,[CardPrinting(75245,1),CardPrinting(93292,1)])
            db.commit()
        self.deck=Deck(main={75245:2})

    def refresh_manifest(self):
        self.manifest.write_text(json.dumps({'files':{self.artifact.name:{'sha256':sha256(self.artifact)}}}),encoding='utf-8')

    def load(self):
        return se.StructuralEvidence(self.artifact,self.manifest,self.raw,self.source_manifest)

    def outputs(self):
        evidence=self.load()
        with closing(readonly(self.candidate)) as con:
            before=analyze_deck(self.deck,con)
            after=analyze_deck6(self.deck,con,evidence)
            repeated=analyze_deck(self.deck,con)
        return before,after,repeated

    def abilities(self):
        with closing(readonly(self.candidate)) as con:
            return analyze_deck(self.deck,con)['zones']['main']['cards'][0]['abilities']

    def test_missing_artifact_refused(self):
        self.artifact.unlink()
        with self.assertRaisesRegex(se.EvidenceError,'verification failed'):self.load()

    def test_wrong_whole_file_refused(self):
        self.manifest.write_text(json.dumps({'files':{self.artifact.name:{'sha256':'wrong'}}}))
        with self.assertRaisesRegex(se.EvidenceError,'whole-file'):self.load()

    def test_wrong_independent_payload_despite_matching_manifest(self):
        with patch.object(se,'PAYLOAD','wrong'):
            with self.assertRaisesRegex(se.EvidenceError,'payload_sha256'):self.load()

    def test_correct_payload_wrong_embedded_provenance(self):
        with closing(sqlite3.connect(self.artifact)) as db:
            db.execute('UPDATE metadata SET value=? WHERE key=?',(encoded('wrong'),'source_sha256'));db.commit()
        self.refresh_manifest()
        with self.assertRaisesRegex(se.EvidenceError,'source_sha256'):self.load()

    def test_correct_payload_wrong_arena_version(self):
        with patch.object(se,'ARENA_VERSION','wrong'):
            with self.assertRaisesRegex(se.EvidenceError,'arena_version'):self.load()

    def test_actual_source_hash_required(self):
        self.source_manifest.write_text('{"changed":true}')
        with self.assertRaisesRegex(se.EvidenceError,'actual preserved source'):self.load()

    def test_repeat_snapshot_filename_refused(self):
        other=self.root/'structural-facts-v1-repeat.sqlite'
        other.write_bytes(self.artifact.read_bytes())
        with self.assertRaisesRegex(se.EvidenceError,'filename'):
            se.StructuralEvidence(other,self.manifest,self.raw,self.source_manifest)

    def test_verification_before_analysis_and_no_partial_object(self):
        with patch.object(se,'PAYLOAD','wrong'),patch('services.analysis6.analyze_deck') as analyze:
            with self.assertRaises(se.EvidenceError):
                evidence=self.load()
                with closing(readonly(self.candidate)) as con:analyze_deck6(self.deck,con,evidence)
            analyze.assert_not_called()

    def test_loyalty_costs_and_null(self):
        _,after,_=self.outputs()
        entries=after['structural_analysis']['zones']['main']['cards'][0]['entries']
        self.assertEqual([r['structures'][0]['raw_cost'] for r in entries[:4]],['+2','0','-6','-X'])
        self.assertIsNone(self.load().entries(75245)[4].loyalty_cost)
        self.assertNotIn(104,[r['entry']['ability_id'] for r in entries])

    def test_chapters_and_repeated_text_unique_positions(self):
        bound=bind_card(75245,self.abilities(),self.entries)
        chapters=[r for r in bound['entries'] if r['entry']['chapter_index'] is not None]
        self.assertEqual([r['structures'][0]['chapter_index'] for r in chapters],[1,2,3,4])
        self.assertNotEqual(chapters[0]['analysis5_ability_id'],chapters[1]['analysis5_ability_id'])
        self.assertEqual(chapters[0]['entry']['local_text'],chapters[1]['entry']['local_text'])

    def test_reach_trample_and_colon_not_eligible(self):
        bound=bind_card(75245,self.abilities(),self.entries)
        self.assertTrue({104,109,110}.isdisjoint(r['entry']['ability_id'] for r in bound['entries']))

    def test_every_analysis5_field_byte_identical(self):
        before,after,repeated=self.outputs()
        structural=after.pop('structural_analysis')
        self.assertEqual(serialized(before),serialized(after))
        self.assertEqual(serialized(before),serialized(repeated))
        self.assertEqual(structural['contract_version'],'6')
        self.assertEqual(after['analysis_version'],'5')

    def test_effects_are_exact_copies_even_when_generic_would_parse_differently(self):
        before,after,_=self.outputs()
        abilities=before['zones']['main']['cards'][0]['abilities']
        for row in after['structural_analysis']['zones']['main']['cards'][0]['entries']:
            self.assertEqual(serialized(row['effect_classification']),serialized(abilities[row['entry']['position']]))
        diagnostic=classify_card(Card(1,'Synthetic',types='Planeswalker',rules_text='CARDNAME generic different'))['abilities'][0]
        self.assertNotEqual(diagnostic['parse_status'],abilities[0]['parse_status'])
        self.assertEqual(after['structural_analysis']['diagnostic_axis'],{'status':'not_run'})

    def test_no_new_timing_trigger_or_legality_fields(self):
        bound=bind_card(75245,self.abilities(),self.entries)
        for row in bound['entries']:
            for structure in row['structures']:
                self.assertTrue(set(structure)<= {'structure','raw_cost','chapter_index'})
            self.assertFalse({'legality','timing','trigger','resolution','turn_frequency'} & row.keys())

    def test_sequence_text_mismatch_denies_entire_card(self):
        entries=list(self.entries);entries[-1]=replace(entries[-1],local_text='Different')
        result=bind_card(75245,self.abilities(),entries)
        self.assertEqual(result['reason'],'sequence_text_mismatch')
        self.assertTrue(all(not r['structures'] for r in result['entries']))

    def test_omission_split_combination_and_duplication_deny_binding(self):
        original=self.abilities()
        for abilities in (original[:-1],original+[original[-1]],original[:4]+original[5:],original+original[:1]):
            with self.subTest(length=len(abilities)):
                result=bind_card(75245,abilities,self.entries)
                self.assertEqual(result['binding'],'unbound')
                self.assertTrue(all(not r['structures'] for r in result['entries']))

    def test_reordered_text_denies_binding(self):
        abilities=self.abilities();abilities[0]['raw_text'],abilities[1]['raw_text']=abilities[1]['raw_text'],abilities[0]['raw_text']
        self.assertEqual(bind_card(75245,abilities,self.entries)['reason'],'sequence_text_mismatch')

    def test_repeated_text_without_unique_positions_is_ambiguous(self):
        entries=list(self.entries);entries[6]=replace(entries[6],position=5)
        result=bind_card(75245,self.abilities(),entries)
        self.assertEqual(result['binding'],'ambiguous')
        result['quantity']=2
        counts=structural_counts([result],True)
        self.assertEqual(counts['structurally_recognized'],0)
        self.assertEqual(counts['ambiguous'],16)

    def test_duplicate_analysis_positions_ambiguous(self):
        abilities=self.abilities();abilities[6]['source_index']=6
        self.assertEqual(bind_card(75245,abilities,self.entries)['binding'],'ambiguous')

    def test_exact_printing_never_replaced_by_representative(self):
        result=bind_card(75245,self.abilities(),[replace(e,grp_id=93292) for e in self.entries])
        self.assertEqual(result['reason'],'exact_printing_mismatch')
        _,after,_=self.outputs()
        rows=after['structural_analysis']['zones']['main']['cards'][0]['entries']
        self.assertTrue(all(r['entry']['grp_id']==75245 and r['binding']=='bound' for r in rows))

    def test_multiple_printings_collapsed_by_analysis5_ambiguous(self):
        self.deck=Deck(main={75245:1,93292:1})
        _,after,_=self.outputs()
        card=after['structural_analysis']['zones']['main']['cards'][0]
        self.assertEqual(card['reason'],'multiple_deck_printings_for_title')
        self.assertTrue(all(r['binding']=='ambiguous' for r in card['entries']))

    def test_missing_canonical_face_unbound(self):
        result=bind_card(75245,[],self.entries,unavailable='missing_canonical_card')
        self.assertEqual(result['binding'],'unbound')
        self.assertTrue(all(r['effect_classification'] is None for r in result['entries']))

    def test_linked_rows_never_exposed_or_followed(self):
        evidence=self.load()
        self.assertFalse(hasattr(evidence,'links'))
        self.assertEqual(evidence.entries(75246),())
        with closing(readonly(self.candidate)) as con:
            queried=[];con.set_trace_callback(queried.append)
            analyze_deck6(self.deck,con,evidence)
        self.assertFalse(any('linked' in q.lower() for q in queried))

    def test_reader_queries_only_entries_after_required_payload_verification(self):
        original=se.readonly;queries=[]
        def traced(path):
            con=original(path);con.set_trace_callback(queries.append);return con
        with patch.object(se,'readonly',traced):self.load()
        self.assertEqual(queries,['SELECT * FROM entries ORDER BY grp_id,position'])

    def test_kind_corrections_do_not_modify_analysis5(self):
        before,after,_=self.outputs()
        row=after['structural_analysis']['zones']['main']['cards'][0]['entries'][0]
        self.assertEqual(row['kind_corrections'][0]['structural_kind'],'loyalty_ability')
        self.assertEqual(row['analysis5_kind'],before['zones']['main']['cards'][0]['abilities'][0]['kind'])

    def test_unverified_evidence_rejected(self):
        with closing(readonly(self.candidate)) as con:
            with self.assertRaisesRegex(TypeError,'verified'):analyze_deck6(self.deck,con,object())

    def test_reader_entries_immutable(self):
        evidence=self.load()
        with self.assertRaises(AttributeError):evidence.entries(75245)[0].position=2
        proof=evidence.proof;proof['payload_sha256']='changed'
        self.assertNotEqual(evidence.proof['payload_sha256'],'changed')

    def test_cli_json_preserves_unicode_across_windows_pipes(self):
        from analyze_structural import main
        with closing(sqlite3.connect(self.candidate)) as con:
            con.execute('UPDATE cards SET name=?',('Synthetic \u2014 card',));con.commit()
        deck_path=self.root/'deck.json'
        deck_path.write_text(json.dumps({'main':{'75245':2}}),encoding='utf-8')
        stream=io.StringIO()
        with redirect_stdout(stream):
            status=main(['--artifact',str(self.artifact),'--output-manifest',str(self.manifest),
                         '--raw-source',str(self.raw),'--source-manifest',str(self.source_manifest),
                         '--candidate',str(self.candidate),'--deck',str(deck_path)])
        self.assertEqual(status,0)
        self.assertTrue(stream.getvalue().isascii())
        self.assertEqual(json.loads(stream.getvalue())['zones']['main']['cards'][0]['name'],'Synthetic \u2014 card')


if __name__=='__main__':
    unittest.main()
