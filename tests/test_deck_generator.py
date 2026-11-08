from copy import deepcopy
import json
import unittest
import hashlib
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

from services.deterministic_deck_generator import generate, prepare, measure, template_check, SLOTS
from services.generated_deck_validator import validate_text


def fixture():
    cards, prints, titles = {}, {}, {}
    tid = 0
    for bucket, creature, needed in SLOTS:
        for _ in range(needed // 4 + 1):
            tid += 1
            cards[tid] = dict(title_id=tid, name=f'Card {tid}', types='Creature' if creature else 'Sorcery',
                              mana_cost=('{'+str(bucket-1)+'}' if bucket > 1 else '')+'{W}', cmc=bucket,
                              colors='W', color_identity='W', rules_text='Flying' if creature else 'Draw a card.', anomaly_count=0)
    cards[100] = dict(title_id=100, name='Plains', types='Land', mana_cost='', cmc=0, colors='', color_identity='W', rules_text='')
    for k in cards:
        prints[k] = dict(arena_id=k, title_id=k, set_code='TST', collector_number=str(k), rarity='common')
        titles[str(k)] = dict(queues={q: dict(status='legal') for q in ['BO1', 'BO3']})
    return dict(cards=cards, printings=prints, basic={100}, snapshot=dict(snapshot_version='fixture',payload=dict(titles=titles)), provenance={})


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture()
        for name in ['services.deterministic_deck_generator.read_facts', 'services.generated_deck_validator.read_facts']:
            p = patch(name, side_effect=lambda *a: deepcopy(self.f))
            p.start()
            self.addCleanup(p.stop)

    def gen(self, queue='BO1'):
        return generate({}, {}, queue)

    def validate(self, text, queue='BO1'):
        return validate_text(text, {}, {}, queue)

    def test_both_queues_validate_and_reproduce(self):
        for q in ['BO1', 'BO3']:
            a = self.gen(q)
            self.assertEqual(a, self.gen(q))
            report = json.loads(a[1])
            self.assertEqual(report['legality_validation']['status'], 'PASS')
            self.assertEqual(report['structural_template_compliance']['status'], 'PASS')
            self.assertEqual(report['arena_compatibility'], 'pending operator testing')
            self.assertEqual(report['legality_validation']['sideboard_count'], 15 if q == 'BO3' else 0)
            self.assertEqual('Sideboard' in a[0], q == 'BO3')

    def test_unknown_and_illegal_are_excluded(self):
        for state in ['unknown', 'illegal']:
            self.f['snapshot']['payload']['titles']['1']['queues']['BO1']['status'] = state
            text, report = self.gen()
            self.assertNotIn(' Card 1 (', text)
            self.assertIn('legality_'+state, json.loads(report)['exclusion_counts'])

    def test_missing_essential_fields_excluded(self):
        for field in ['name', 'types', 'mana_cost', 'cmc', 'colors']:
            with self.subTest(field=field):
                f = fixture()
                f['cards'][1][field] = None
                pool, _, exclusions = prepare(f, 'BO1')
                self.assertNotIn(1, {x['title_id'] for x in pool})
                self.assertTrue(next(x for x in exclusions if x['title_id']==1)['reasons'])

    def test_missing_rarity_unknown(self):
        for p in self.f['printings'].values():
            p['rarity'] = None
        self.assertEqual(json.loads(self.gen()[1])['crafting_summary']['quantities_by_rarity'], {'unknown':60})

    def test_empty_colors_do_not_hide_colored_cost(self):
        self.f['cards'][1]['colors']=''
        text,report=self.gen()
        self.assertNotIn(' Card 1 (',text)
        self.assertIn('colors_cost_disagreement',json.loads(report)['exclusion_counts'])

    def test_special_text_excluded_with_marker(self):
        self.f['cards'][1]['rules_text'] = 'A deck can have any number of cards named Card 1.'
        text, report = self.gen()
        self.assertNotIn(' Card 1 (', text)
        self.assertIn('special_text:a deck can have', json.loads(report)['exclusion_counts'])

    def test_insufficient_pool_fails(self):
        for t in self.f['snapshot']['payload']['titles'].values():
            t['queues']['BO1']['status']='unknown'
        with self.assertRaisesRegex(ValueError,'insufficient'):
            self.gen()

    def test_sufficiency_caps_at_one(self):
        pool, basics, _ = prepare(self.f,'BO1')
        a = measure(pool,'W',basics)
        b = measure(pool*20,'W',basics)
        self.assertEqual(a['primary'],b['primary'])
        self.assertEqual(a['secondary'],b['secondary'])

    def test_template_deviations(self):
        pool, basics, _ = prepare(self.f,'BO1')
        r=template_check({100:60},{x['title_id']:x for x in pool+basics})
        self.assertEqual(r['status'],'FAIL')
        self.assertTrue(r['deviations'])

    def test_independent_validator_negative_cases(self):
        text,_=self.gen()
        cases = [text.replace('4 Card 1 (TST) 1','5 Card 1 (TST) 1'),
                 text.replace('24 Plains','23 Plains'), text+'nonsense\n',
                 text.replace('Card 1 (TST) 1','Wrong (TST) 1'),
                 text.replace('Card 1 (TST) 1','Card 1 (TST) missing')]
        for bad in cases:
            self.assertEqual(self.validate(bad)['status'],'FAIL')
        self.f['snapshot']['payload']['titles']['1']['queues']['BO1']['status']='unknown'
        self.assertEqual(self.validate(text)['status'],'FAIL')
        self.f['snapshot']['payload']['titles']['1']['queues']['BO1']['status']='legal'
        self.f['cards'][1]['rules_text']='Companion — your starting deck contains only creatures.'
        self.assertEqual(self.validate(text)['status'],'FAIL')

    def test_copy_limit_across_zones(self):
        text,_=self.gen()
        self.assertEqual(self.validate(text+'\nSideboard\n1 Card 1 (TST) 1\n','BO3')['status'],'FAIL')

    def test_basic_name_not_exemption(self):
        text,_=self.gen()
        self.f['basic']=set()
        r=self.validate(text)
        self.assertTrue(any(e['code']=='copy_limit' and e['title_id']==100 for e in r['violations']))

    def test_bo1_sideboard_and_oversize_bo3_rejected(self):
        text,_=self.gen()
        self.assertEqual(self.validate(text+'\nSideboard\n1 Plains (TST) 100\n')['status'],'FAIL')
        self.assertEqual(self.validate(text+'\nSideboard\n16 Plains (TST) 100\n','BO3')['status'],'FAIL')

    def test_validator_has_no_generator_dependency(self):
        import inspect
        import services.generated_deck_validator as module
        source=inspect.getsource(module)
        self.assertNotIn('deterministic_deck_generator',source)
        self.assertNotIn('template_check',source)

    def test_sideboard_shortfall_is_disclosed(self):
        for bucket, creature, needed in SLOTS:
            ids=[k for k,c in self.f['cards'].items() if c['cmc']==bucket and ('Creature' in c['types'])==creature and k!=100]
            for tid in ids[needed//4:]:
                self.f['snapshot']['payload']['titles'][str(tid)]['queues']['BO3']['status']='unknown'
        _,report=self.gen('BO3')
        r=json.loads(report)
        self.assertEqual(r['legality_validation']['sideboard_count'],0)
        self.assertTrue(r['sideboard_shortfall_reason'])

    def test_authoritative_reader_basic_type_evidence_and_hashes(self):
        from services.deck_construction_data import read_facts
        from mtgadb.standard_legality import bindings, DETERMINATION, FINAL_AUTHORITY, encoded
        from mtgadb import canonical as db
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            paths=dict(database=root/'cards.db',arena=root/'arena.db',snapshot=root/'snapshot.json')
            c=sqlite3.connect(paths['database'])
            try:
                c.executescript(db.SCHEMA)
                c.execute("INSERT INTO cards(title_id,name,types) VALUES(1,'Plains','Land'),(2,'Plains','Creature')")
                c.execute("INSERT INTO printings(arena_id,title_id,set_code,collector_number) VALUES(11,1,'T','1'),(12,2,'T','2')")
                c.commit()
            finally:c.close()
            c=sqlite3.connect(paths['arena'])
            try:
                c.executescript("CREATE TABLE Cards(GrpId,TitleId,Types,Supertypes); CREATE TABLE Enums(Type,Value,LocId); CREATE TABLE Localizations_enUS(LocId,Formatted,Loc); INSERT INTO Cards VALUES(11,1,'1','1'),(12,2,'2',''); INSERT INTO Enums VALUES('CardType',1,1),('SuperType',1,2),('CardType',2,3); INSERT INTO Localizations_enUS VALUES(1,1,'Land'),(2,1,'Basic'),(3,1,'Creature');")
                c.commit()
            finally:c.close()
            p=dict(version=1,final_authority=FINAL_AUTHORITY,determination_verbatim=DETERMINATION,queues={'BO1':dict(provenance=bindings()['BO1'])})
            digest=hashlib.sha256(encoded(p).encode()).hexdigest()
            paths['snapshot'].write_text(encoded(dict(payload=p,payload_sha256=digest,snapshot_version='standard-legality-v1-'+digest)),encoding='utf-8')
            pins={k:hashlib.sha256(v.read_bytes()).hexdigest() for k,v in paths.items()}
            facts=read_facts(paths,pins,'BO1')
            self.assertEqual(facts['basic'],{11})
            self.assertEqual(pins,{k:hashlib.sha256(v.read_bytes()).hexdigest() for k,v in paths.items()})
            with self.assertRaises(ValueError):read_facts(paths,dict(pins,arena='0'*64),'BO1')


if __name__=='__main__':
    unittest.main()
