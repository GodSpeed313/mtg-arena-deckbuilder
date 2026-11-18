"""Frozen Run 27 controls, including five real default byte comparisons."""
import hashlib
import json
from collections import Counter
from fractions import Fraction
from pathlib import Path
import unittest
from unittest.mock import patch

from services import quality_deck_generator as q
from services.spell_support_guard import assess, bonus, classify_support, casting_category
from services.spell_cast_boost import EXPERIMENTAL, DEPENDENCY
from tests.test_spell_cast_boost import card, decorated, face


def support(tid, text='Draw a card.', mv=2):
    return dict(title_id=tid, name=f'Spell {tid}', types='Instant', rules_text=text, cmc=mv)


class SupportGuardTests(unittest.TestCase):
    def test_no_selected_support_even_with_pool_partners(self):
        spell=decorated(card(1,'Draw a card.','Instant'))
        payoff=decorated(card(2,'Prowess'))
        pool=[spell,payoff]; graph=q.relationships(pool,profile=EXPERIMENTAL)
        self.assertTrue(graph)
        _,ev=q.quality(payoff,Counter({2:4}),{c['title_id']:c for c in pool},graph)
        self.assertFalse(any(e['rule_identifier']=='coherence.spell_support_guard' for e in ev))

    def test_unsupported_events_types_and_trigger(self):
        for typ in ['Creature','Artifact Creature','Land']:
            self.assertEqual(classify_support(dict(support(1),types=typ),qualifies=True),'Unsupported')
        for event in ['cycling','activated','copy_not_cast']:
            self.assertEqual(classify_support(support(1),qualifies=True,event=event),'Unsupported')
        self.assertEqual(classify_support(support(1),qualifies=False),'Unsupported')
        en=decorated(card(1,'Draw a card.','Instant'))
        wrong=decorated(card(2,'Whenever an opponent casts a noncreature spell, this creature gets +1/+1 until end of turn.'))
        self.assertFalse(q.relationships([en,wrong],profile=EXPERIMENTAL))

    def test_sideboard_only_is_unsupported(self):
        self.assertEqual(classify_support(support(1),qualifies=True,zone='sideboard'),'Unsupported')
        self.assertEqual(assess({}, {1:support(1)}, {1})['bonus'],0)

    def test_removing_final_support_recomputes(self):
        cards={i:support(i) for i in range(1,4)}
        full=assess({1:4,2:4,3:4},cards,set(cards))
        reduced=assess({1:4},cards,set(cards))
        self.assertEqual(full['bonus'],12)
        self.assertEqual(reduced['bonus'],4)

    def test_categories_costs_worked_example_and_cap(self):
        cards={i:support(i,'Counter target spell.',i+1) for i in range(1,4)}
        result=assess(dict.fromkeys(cards,4),cards,set(cards))
        self.assertEqual(result['S'],'18/5');self.assertEqual(result['bonus'],4)
        self.assertEqual(result['counts']['Conditional'],12)
        self.assertEqual(assess({1:20},cards,{1})['S'],'8/5')
        self.assertEqual(assess({1:4},{1:support(1)}, {1})['bonus'],4)
        self.assertEqual(assess({1:4},{1:support(1,mv=4)}, {1})['bonus'],2)
        self.assertEqual(assess({1:4},cards,{1})['bonus'],2)

    def test_unmet_prerequisite_never_proactive(self):
        for text in ['Target tapped creature gets +2/+2 until end of turn.',
                     'As an additional cost to cast this spell, sacrifice a creature.\nDraw a card.',
                     'Target artifact you control gets +2/+2 until end of turn.',
                     'Cast this spell only if you control a Mouse.\nDraw a card.']:
            self.assertNotEqual(casting_category(support(1,text)),'Proactive')
        self.assertEqual(casting_category(support(1,'Target creature gets +4/+4 until end of turn.')),'Proactive')

    def test_unknown_reported_separately(self):
        cards={1:support(1,'Unrecognized mechanism.'),2:support(2,'Counter target spell.')}
        result=assess({1:3,2:2},cards,{1,2})
        self.assertEqual(result['counts'],dict(Proactive=0,Conditional=2,Unknown=3,Unsupported=0))
        self.assertEqual(result['S'],'2')

    def test_exact_half_up_boundary_and_positive_control(self):
        self.assertEqual(bonus(Fraction(11,24)),1)
        self.assertEqual(bonus(Fraction(55,24)),3)
        self.assertEqual(bonus(Fraction(11,24)-Fraction(1,10000)),0)
        cards={i:support(i) for i in range(3)}
        self.assertEqual(assess({0:4,1:4,2:4},cards,set(cards))['bonus'],12)

    def test_other_family_score_preserved_with_guard(self):
        en=decorated(card(1,'Draw a card.','Instant'));pay=decorated(card(2,'Prowess'))
        cards={1:en,2:pay};graph=q.relationships(list(cards.values()),profile=EXPERIMENTAL)
        old=dict(rule_id='existing.test.family',cards=[1,2],support='conditional',evidence=[])
        graph[2].append(old)
        _,ev=q.quality(pay,Counter({1:4}),cards,graph)
        self.assertEqual(next(e['numeric_contribution'] for e in ev if e['rule_identifier']=='coherence.compatible_dependencies'),8)
        self.assertEqual(next(e['numeric_contribution'] for e in ev if e['rule_identifier']=='coherence.spell_support_guard'),4)

    def test_synthetic_experimental_deterministic_and_independently_legal(self):
        from tests.test_quality_generator import fixture
        f=fixture()
        for c in f['cards'].values():
            if c['types']=='Creature':c['rules_text']='Prowess'
        with patch('services.quality_deck_generator.read_facts',return_value=f), patch('services.generated_deck_validator.read_facts',return_value=f), patch('services.spell_cast_boost.load_cast_evidence',return_value={t:face(t) for t in f['cards']}):
            for rid in q.REQUESTS:
                result=q.generate({'arena':'unused'},{},rid,profile=EXPERIMENTAL)
                self.assertEqual(result,q.generate({'arena':'unused'},{},rid,profile=EXPERIMENTAL))
                self.assertEqual(json.loads(result[1])['checks']['legality']['status'],'PASS')

    def test_five_frozen_default_decks_byte_identical(self):
        from services.deck_construction_data import SNAPSHOT,DATABASE,ARENA,PINS
        root=Path(__file__).resolve().parents[1]/'run25-artifacts'/'evaluation-final'
        manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
        for rid in q.REQUESTS:
            name=rid+'-default.txt';raw=(root/name).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(),manifest[name])
            text,report=q.generate(dict(snapshot=SNAPSHOT,database=DATABASE,arena=ARENA),PINS,rid)
            self.assertEqual(text.encode('utf-8'),raw)
            self.assertEqual(json.loads(report)['checks']['legality']['status'],'PASS')


if __name__=='__main__':unittest.main()
