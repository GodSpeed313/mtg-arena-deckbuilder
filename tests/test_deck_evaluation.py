"""Synthetic evaluation protocol checks; no real grades are created."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from services import deck_evaluation as ev


def fixture():
    cards={1:dict(title_id=1,name='Forest',types='Basic Land',color_identity='G',cmc=0,rules_text='',mana_cost=None)}
    cards[2]=dict(title_id=2,name='Ramp',types='Artifact',cmc=3,rules_text='{T}: Add {G}.',mana_cost='{3}')
    cards[3]=dict(title_id=3,name='Creature',types='Creature',cmc=4,rules_text='Flying',mana_cost='{3}{G}')
    printings={i:dict(title_id=i,set_code='TST',collector_number=str(i)) for i in cards}
    return dict(cards=cards,printings=printings,basic={1},snapshot={'payload':{'titles':{str(i):{'queues':{q:{'status':'legal'} for q in ('BO1','BO3')}} for i in cards}}})


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.facts=fixture()
        self.source=self.root/'source.txt'; self.source.write_text('Deck\n24 Forest (TST) 1\n4 Ramp (TST) 2\n32 Creature (TST) 3\n',encoding='utf-8')
        self.entries=[dict(id='baseline-'+q,request_id='std-'+q,source_path=str(self.source),sha256=ev.file_hash(self.source),queues=[q.upper()],origin='secret baseline origin') for q in ('bo1','bo3')]
        self.folder=self.root/'set'; ev.build(self.entries,self.facts,self.folder)

    def filled(self):
        for p in (self.folder/'worksheets').glob('Deck-*.json'):
            w=ev.read_json(p)
            w['scores']={k:dict(score=3,why='Synthetic fixture rationale') for k in ev.ANCHORS}
            p.write_text(ev.canonical(w),encoding='utf-8')

    def ingest(self,name='result.json'):
        return ev.ingest(self.folder,self.folder/'worksheets',[],'operator','2026-11-09',self.root/name)

    def test_flags_deterministic_and_contextual(self):
        rows=ev.resolve(self.source.read_text(),self.facts)
        flags=ev.automatic_flags(rows)
        self.assertEqual(flags,ev.automatic_flags(rows))
        self.assertEqual(set(flags),set(ev.RATIONALES))
        for flag in flags.values():
            self.assertEqual(flag['label'],'for human review')
            self.assertIn('acceptable',flag['rationale']) if flag is not flags['understanding'] and flag is not flags['split_printings'] else self.assertTrue(flag['rationale'])
            self.assertNotIn('bad',flag['rationale'])
        self.assertEqual(flags['acceleration']['measurements']['mana_production_mentions'],4)
        self.assertNotIn('synergy',flags)

    def test_masking_and_separate_key(self):
        key=ev.read_json(self.folder/'worksheets/key.json')
        self.assertEqual(len(key['entries']),2)
        for p in (self.folder/'worksheets').glob('Deck-*.json'):
            text=p.read_text()
            self.assertNotIn('secret baseline origin',text)
            self.assertNotIn(str(self.source),text)
            self.assertNotIn('baseline-bo',text)
            self.assertIsNone(ev.read_json(p)['scores']['Synergy']['score'])

    def test_valid_ingestion_separates_scores_flags(self):
        self.filled(); result=self.ingest()
        self.assertEqual(result['human_scores'][0]['total'],15)
        self.assertNotIn('automatic_flags',result['human_scores'][0])
        self.assertIn('automatic_flags_artifact',result)

    def test_invalid_scores_and_why(self):
        for score,why in [(6,'why'),(-1,'why'),(2.5,'why'),('3','why'),(True,'why'),(3,''),(3,'   '),(3,None)]:
            with self.subTest(score=score,why=why):
                self.filled(); p=next((self.folder/'worksheets').glob('Deck-*.json'))
                w=ev.read_json(p); w['scores']['Synergy']=dict(score=score,why=why)
                p.write_text(ev.canonical(w),encoding='utf-8')
                with self.assertRaises(ValueError):self.ingest()

    def test_unknown_worksheet_deck(self):
        self.filled(); p=self.folder/'worksheets/key.json'; key=ev.read_json(p)
        next(iter(key['entries'].values()))['id']='intruder'
        p.write_text(ev.canonical(key),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'unknown'):self.ingest()

    def test_changed_worksheet_content_rejected(self):
        self.filled(); p=next((self.folder/'worksheets').glob('Deck-*.json')); w=ev.read_json(p)
        w['deck_list']='tampered'; p.write_text(ev.canonical(w),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'content changed'):self.ingest()

    def test_deck_hash_fails_closed(self):
        self.source.write_text('changed')
        with self.assertRaisesRegex(ValueError,'hash mismatch'):ev.verify_manifest(self.folder)

    def test_policy_and_manifest_frozen(self):
        for file in ['policy.json','manifest.json']:
            p=self.folder/file; original=p.read_bytes(); p.write_bytes(original+b' ')
            with self.assertRaisesRegex(ValueError,'hash mismatch'):ev.verify_manifest(self.folder)
            p.write_bytes(original)

    def test_candidate_does_not_mutate_manifest(self):
        before=(self.folder/'manifest.json').read_bytes()
        c=self.root/'candidate.json'
        prov=dict(generator='synthetic',version='1',commit='abc',inputs={'format':'Standard','queue':'BO1'},card_specific_exclusions='none')
        record=ev.register_candidate(self.folder,self.source,'std-bo1',prov,c)
        self.assertEqual(before,(self.folder/'manifest.json').read_bytes())
        self.assertEqual(record['provenance'],prov)
        entries,seal=ev.entries_with_candidates(self.folder,[c])
        packet=self.root/'candidate-sheets'
        ev.packet([entries[-1]],self.facts,packet,seal['manifest_sha256'])
        for p in packet.glob('Deck-*.json'):
            w=ev.read_json(p); w['scores']={d:dict(score=3,why='synthetic') for d in ev.ANCHORS}; p.write_text(ev.canonical(w))
        with self.assertRaises(ValueError):ev.ingest(self.folder,packet,[],'operator','2026-11-09',self.root/'bad.json')
        r=ev.ingest(self.folder,packet,[c],'operator','2026-11-09',self.root/'good.json')
        self.assertEqual(r['human_scores'][0]['id'],record['id'])

    def test_comparison_pairs_by_request_not_order(self):
        self.filled(); left=self.ingest(); right=copy.deepcopy(left)
        right['human_scores'].reverse()
        for e in right['human_scores']:
            e['scores']['Synergy']['score']=4 if e['request_id']=='std-bo1' else 2
            e['total']=sum(s['score'] for s in e['scores'].values())
        result=ev.compare(left,right)
        self.assertEqual([r['request_id'] for r in result['paired_requests']],['std-bo1','std-bo3'])
        self.assertEqual(result['paired_requests'][0]['dimensions']['Synergy']['right'],4)
        self.assertIsNone(result['verdict'])

    def test_comparison_cli_synthetic_only(self):
        import evaluate_decks
        self.filled(); self.ingest('left.json'); self.ingest('right.json')
        output=self.root/'comparison.json'
        with patch.object(evaluate_decks,'external',return_value=output):
            evaluate_decks.main(['compare','--left',str(self.root/'left.json'),'--right',str(self.root/'right.json'),'--output',str(output)])
        self.assertEqual(len(ev.read_json(output)['paired_requests']),2)

    def test_comparison_rejects_versions(self):
        self.filled(); left=self.ingest()
        for key in ev.VERSIONS:
            right=copy.deepcopy(left); right[key]+=1
            with self.assertRaisesRegex(ValueError,'version'):ev.compare(left,right)

    def test_comparison_requires_all_requests(self):
        self.filled(); left=self.ingest(); right=copy.deepcopy(left); right['human_scores'].pop()
        with self.assertRaisesRegex(ValueError,'all requests'):ev.compare(left,right)

    def test_reference_not_paired(self):
        self.filled(); left=self.ingest(); right=copy.deepcopy(left)
        ref=copy.deepcopy(left['human_scores'][0]); ref['request_id']='reference'; ref['id']='calibration'
        left['human_scores'].insert(0,ref)
        result=ev.compare(left,right)
        self.assertEqual(len(result['paired_requests']),2)
        self.assertEqual(len(result['calibration_reference_points']),1)

    def test_split_printings_and_unknown_data(self):
        self.facts['printings'][4]=dict(title_id=3,set_code='ALT',collector_number='1')
        text='Deck\n4 Creature (TST) 3\n4 Creature (ALT) 1\n'
        self.facts['cards'][3]['cmc']=None; self.facts['cards'][3]['rules_text']=''
        flags=ev.automatic_flags(ev.resolve(text,self.facts))
        self.assertEqual(flags['split_printings']['measurements']['title_count'],1)
        self.assertEqual(flags['curve']['measurements']['counts'],{'unknown':8})
        self.assertEqual(flags['understanding']['measurements']['copy_counts']['not_understood'],8)

    def test_legality_independent_queues_and_copy_diagnostic(self):
        self.facts['snapshot']['payload']['titles']['3']['queues']['BO1']={'status':'illegal','reason':'fixture ban'}
        rows=ev.resolve(self.source.read_text(),self.facts)
        self.assertEqual(ev.legality(rows,self.facts,'BO1')['card_legality'],'illegal')
        self.assertEqual(ev.legality(rows,self.facts,'BO3')['card_legality'],'legal')
        self.assertFalse(ev.legality(rows,self.facts,'BO3')['fully_standard_legal'])

    def test_exact_name_disambiguates_set_collector_collision(self):
        self.facts['printings'][9]=dict(title_id=2,set_code='TST',collector_number='3')
        rows=ev.resolve('Deck\n1 Creature (TST) 3\n',self.facts)
        self.assertEqual(rows[0]['arena_id'],3)
        self.facts['printings'][10]=dict(title_id=3,set_code='TST',collector_number='3')
        with self.assertRaisesRegex(ValueError,'ambiguous'):ev.resolve('Deck\n1 Creature (TST) 3\n',self.facts)


if __name__=='__main__':
    unittest.main()
