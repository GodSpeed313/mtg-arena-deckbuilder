from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from services import quality_deck_generator as b
from services import deterministic_deck_generator as a
from services.generated_deck_validator import validate_text
from services.evaluation_v2_worksheets import create_packet
from services.evaluation_requests_v2 import allowed, check_pair
from tests.test_deck_generator import fixture as a_fixture


def fixture():
    cards={};prints={};titles={};tid=0
    for color in 'WUBRG':
        for bucket,creature,needed in b.SLOTS:
            for _ in range(needed//2+1):
                tid+=1
                text='Flying' if creature else 'Destroy target creature.'
                cards[tid]=dict(title_id=tid,name=f'Fixture {tid}',types='Creature' if creature else 'Sorcery',mana_cost=('{'+str(bucket-1)+'}' if bucket>1 else '')+'{'+color+'}',cmc=bucket,colors=color,color_identity=color,rules_text=text,power='2',toughness='2',anomaly_count=0)
        tid+=1;cards[tid]=dict(title_id=tid,name=f'Basic {color}',types='Land',mana_cost='',cmc=0,colors='',color_identity=color,rules_text='',anomaly_count=0)
    for tid,c in cards.items():
        prints[tid]=dict(arena_id=tid,title_id=tid,set_code='TST',collector_number=str(tid),rarity='common')
        titles[str(tid)]=dict(queues={q:dict(status='legal') for q in ['BO1','BO3']})
    return dict(cards=cards,printings=prints,basic={t for t,c in cards.items() if c['types']=='Land'},snapshot=dict(snapshot_version='synthetic',payload=dict(titles=titles)),provenance={})


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture()
        for path in ['services.quality_deck_generator.read_facts','services.generated_deck_validator.read_facts']:
            p=patch(path,side_effect=lambda *args:deepcopy(self.f));p.start();self.addCleanup(p.stop)

    def gen(self,req='std-bo1'):return b.generate({}, {}, req)

    def card(self,tid,text,**kw):
        c=dict(self.f['cards'][1],title_id=tid,name='Synthetic '+str(tid),rules_text=text,**kw)
        c.update(basic=False,creature='Creature' in c['types'],tokens=['W'],printing=self.f['printings'][1])
        return b.decorate(c)

    def test_all_requests_validate_and_repeat(self):
        for req in b.REQUESTS:
            x=self.gen(req);self.assertEqual(x,self.gen(req))
            r=json.loads(x[1]);self.assertEqual(r['checks']['legality']['status'],'PASS');self.assertEqual(r['checks']['template']['status'],'PASS')
            if b.REQUESTS[req]['color_pair']:self.assertEqual(r['checks']['color_pair']['status'],'PASS')

    def test_unknown_illegal_and_offcolor_never_admitted(self):
        self.f['snapshot']['payload']['titles']['1']['queues']['BO1']['status']='illegal'
        self.f['snapshot']['payload']['titles']['2']['queues']['BO1']['status']='unknown'
        self.f['cards'][3]['rules_text']='{B}: Draw a card.'
        text,_=self.gen('std-bo1-wu')
        for tid in [1,2,3]:self.assertNotIn(f' Fixture {tid} (',text)

    def test_impossible_pair_fails(self):
        for c in self.f['cards'].values():
            if c['colors']=='U':c['color_identity']='B'
        with self.assertRaisesRegex(ValueError,'failed without relaxation'):self.gen('std-bo1-wu')

    def test_legendary_limit_main_and_side(self):
        for c in self.f['cards'].values():
            if c['types']=='Creature':c['types']='Legendary Creature'
        text,_=self.gen('std-bo3')
        for line in text.splitlines():
            if 'Fixture' in line:
                tid=int(line.rsplit(' ',1)[1]);
                if 'Legendary' in self.f['cards'][tid]['types']:self.assertLessEqual(int(line.split()[0]),2)

    def test_resource_context_gate_and_unknown_neutral(self):
        for text in ['{T}: Add {W}.','Draw a card.']:
            card=self.card(999,text,types='Artifact',cmc=3)
            payoff=self.card(998,'Flying',cmc=6)
            cards={999:card,998:payoff}
            _,low=b.quality(card,Counter(),cards,{})
            _,high=b.quality(card,Counter({998:6}),cards,{})
            self.assertEqual(next(e['numeric_contribution'] for e in low if e['rule_identifier']=='resources.context_gate'),-3)
            self.assertEqual(next(e['numeric_contribution'] for e in high if e['rule_identifier']=='resources.context_gate'),2)
        c=self.card(999,'Mana draw token counter unknown words.',types='Artifact')
        _,con=b.quality(c,Counter(),{999:c},{})
        self.assertTrue(all(e['numeric_contribution']<=0 for e in con))
        self.assertFalse(any(e['evidence_source']=='recognized card text' for e in con))

    def test_dependency_coherence_and_hybrid_shared_participant(self):
        producer=self.card(901,'Create a 1/1 white Soldier creature token.\nYou gain 3 life.',types='Sorcery')
        payoff=self.card(902,'Whenever a creature token enters the battlefield under your control, draw a card.\nWhenever you gain life, draw a card.',types='Enchantment')
        high=self.card(903,'Flying',cmc=6)
        pool=[producer,payoff,high];graph=b.relationships(pool);cards={c['title_id']:c for c in pool}
        selected=Counter({902:2,903:6})
        score,ev=b.quality(producer,selected,cards,graph)
        empty,_=b.quality(producer,Counter(),cards,graph)
        self.assertGreater(score,empty)
        self.assertTrue(any(e['rule_identifier']=='hybrid.shared_enabler_or_payoff' for e in ev))

    def test_name_invariance(self):
        first=self.gen('std-bo1-wu'); ids=lambda text:[(line.split()[0],line.rsplit(' ',1)[1]) for line in text.splitlines() if '(TST)' in line]
        for c in self.f['cards'].values():c['name']='Renamed '+str(c['title_id'])
        second=self.gen('std-bo1-wu')
        self.assertEqual(ids(first[0]),ids(second[0]))
        l=json.loads(first[1])['contributions'];r=json.loads(second[1])['contributions']
        self.assertEqual({k:v['score'] for k,v in l.items()},{k:v['score'] for k,v in r.items()})

    def test_all_contributions_typed(self):
        _,report=self.gen()
        for card in json.loads(report)['contributions'].values():
            for e in card['contributions']:
                self.assertTrue(e['rule_identifier']);self.assertIsInstance(e['numeric_contribution'],(int,float));self.assertTrue(e['evidence'])
                self.assertIn(e['evidence_source'],['recognized card text','inferred relationship','general heuristic'])

    def test_validator_rejects_illegal_and_malformed(self):
        text,_=self.gen();self.assertEqual(validate_text(text+'garbage\n',{}, {},'BO1')['status'],'FAIL')
        tid=int(next(l.rsplit(' ',1)[1] for l in text.splitlines() if 'Fixture' in l))
        self.f['snapshot']['payload']['titles'][str(tid)]['queues']['BO1']['status']='illegal'
        self.assertEqual(validate_text(text,{}, {},'BO1')['status'],'FAIL')

    def test_generator_a_unchanged_before_after_b(self):
        af=a_fixture()
        with patch('services.deterministic_deck_generator.read_facts',return_value=af),patch('services.generated_deck_validator.read_facts',return_value=af):
            before=a.generate({}, {},'BO1')
        self.gen()
        with patch('services.deterministic_deck_generator.read_facts',return_value=af),patch('services.generated_deck_validator.read_facts',return_value=af):
            self.assertEqual(before,a.generate({}, {},'BO1'))

    def test_uniform_worksheets_no_origin_or_explanations(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);text,_=self.gen();p=root/'deck.txt';p.write_text(text,encoding='utf-8')
            entries=[dict(opaque_id=str(i),origin='PRIVATE_ORIGIN_'+str(i),path=str(p),sha256=hashlib.sha256(p.read_bytes()).hexdigest(),request_id=req) for i,req in enumerate(['std-bo1','std-bo1','std-bo3','std-bo3','std-bo1-wu','std-bo1-br','std-bo1-gw'])]
            salt=b'x'*32;key=create_packet(entries,self.f,root/'packet',salt)
            key2=create_packet(entries,self.f,root/'repeat',salt)
            self.assertEqual(key,key2)
            fields=None
            for path in (root/'packet').glob('*.json'):
                txt=path.read_text();w=json.loads(txt)
                for banned in ['PRIVATE_ORIGIN','opaque_id','source_path','contributions','selection_score','timestamp'] :self.assertNotIn(banned,txt)
                if fields is None:fields=set(w)
                self.assertEqual(set(w),fields)
                self.assertEqual(path.read_bytes(),(root/'repeat'/path.name).read_bytes())


if __name__=='__main__':unittest.main()
