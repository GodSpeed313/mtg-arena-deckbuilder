import copy
import json
from pathlib import Path
import unittest

from mtgadb.model import Card
from services.abilities import decompose_abilities
from services.intelligence import classify_card, interactions
from services.dependencies import dependency_findings
from services.evidence_scope import semantic_feature
from services.needs import needs_findings


DATA = json.loads((Path(__file__).parent / 'fixtures/fynn_run11.json').read_text())['card']
TEXT = DATA['rules_text']
INTRINSIC, TRIGGER = TEXT.splitlines()


def row(text=TEXT, ident=477565, types='Creature', quantity=1):
    result = classify_card(Card(ident, DATA['name'] if ident == 477565 else 'Fixture',
                                rules_text=text, types=types))
    result['quantity'] = quantity
    return result


def combat(rows):
    return [f for f in dependency_findings(rows) if f['label'] == 'deathtouch_combat_poison']


class QualifiedCombatTests(unittest.TestCase):
    def test_database_fynn_source_trigger_recipient_and_quantity(self):
        self.assertEqual(DATA['title_id'], 477565)
        a = decompose_abilities(TEXT, card_name=DATA['name'], card_types=DATA['types'])[1]
        self.assertEqual(a.parse_status, 'supported')
        self.assertEqual(a.trigger.subject, 'creature_you_control')
        self.assertEqual(a.trigger.conditions[0].subject, 'damage_source')
        self.assertEqual(a.trigger.conditions[0].value, 'deathtouch')
        e = a.effects[0]
        self.assertEqual((e.kind, e.target, e.amount, e.counter_type, e.source),
                         ('put_player_poison_counters', 'damaged_player', 2, 'poison', 'ability_source'))
        self.assertEqual(e.conditions[0].value, 'combat_damage_to_player')
        self.assertEqual(a.raw_text, TRIGGER)

    def test_other_keyword_poison_has_no_dependency(self):
        for keyword in ('flying', 'first strike', 'toxic'):
            text = TRIGGER.replace('deathtouch', keyword)
            r = row(text)
            self.assertEqual(r['abilities'][0]['parse_status'], 'supported')
            self.assertEqual(combat([r]), [])
            self.assertEqual(interactions([r]), [])
            for f in r['features']:
                semantic_feature(f)

    def test_numeric_and_supported_word_quantities(self):
        for word, number in [('one',1),('three',3),('7',7)]:
            r = row(TRIGGER.replace('two',word))
            self.assertEqual(r['abilities'][0]['effects'][0]['amount'],number)

    def test_qualified_trigger_negatives(self):
        for old,new in [('you control','an opponent controls'),('combat damage','damage'),
                        ('a player','a planeswalker'),('a player','each opponent'),
                        ('deathtouch','deathtouch and flying'),('deathtouch','deathtouch that attacked'),
                        ('deathtouch','counters')]:
            with self.subTest(new=new):
                a = decompose_abilities(TRIGGER.replace(old,new),card_types='Creature')[0]
                self.assertEqual(a.parse_status,'unsupported')
                self.assertIsNone(a.trigger)
                self.assertFalse(a.effects)

    def test_mixed_and_unreviewed_effects_remain_partial(self):
        unknown = ' Draw a card.'
        r=row(TRIGGER+unknown)
        self.assertEqual(r['abilities'][0]['parse_status'],'partial')
        self.assertEqual(r['unsupported_text'],[unknown.strip()])
        self.assertEqual(len(r['abilities'][0]['effects']),1)
        self.assertEqual(combat([r])[0]['state'],'payoff_without_enabler')
        for tail in ('that player gets an additional poison counter.', 'that player gets X poison counters.'):
            text=TRIGGER.split(', ',1)[0]+', '+tail
            a=decompose_abilities(text,card_types='Creature')[0]
            self.assertEqual(a.parse_status,'partial')
            self.assertFalse(a.effects)

    def test_fynn_alone_is_conditional_self_enabler(self):
        r=row(); f=combat([r])[0]
        self.assertEqual(f['state'],'conditionally_supported')
        self.assertEqual(f['enabler_side']['copy_count'],1)
        self.assertEqual(len(f['compatible_pairs']),1)
        p=f['compatible_pairs'][0]
        self.assertEqual(p['enabler_title_id'],p['payoff_title_id'])
        self.assertEqual(p['support'],'conditional')
        self.assertEqual(needs_findings([f],{}),[])
        for feature in r['features']: semantic_feature(feature)

    def test_intrinsic_creatures_only_and_deduplication(self):
        fynn=row(); fynn['features'] += copy.deepcopy(fynn['features'])
        rows=[fynn,row(INTRINSIC,2,quantity=3),
              row('Other creatures you control have deathtouch.',3),
              row(INTRINSIC,4,types='Enchantment')]
        f=combat(rows)[0]
        self.assertEqual(f['enabler_side']['copy_count'],4)
        self.assertEqual({x['title_id'] for x in f['enabler_side']['cards']},{477565,2})
        self.assertEqual(len(f['compatible_pairs']),2)

    def test_zero_intrinsic_enablers_is_unmet(self):
        f=combat([row(TRIGGER,9),row('Target creature gains deathtouch until end of turn.',10)])[0]
        self.assertEqual(f['state'],'payoff_without_enabler')
        self.assertEqual(f['enabler_side']['copy_count'],0)
        self.assertFalse(f['compatible_pairs'])

    def test_bad_intrinsic_evidence_fails_closed(self):
        r=row()
        for f in r['features']:
            if f['rule_id']=='intrinsic.creature_deathtouch.v1':
                f['dependency_context']['origin']['types']=['Enchantment']
                with self.assertRaises(ValueError): semantic_feature(f)
        self.assertEqual(combat([r])[0]['state'],'payoff_without_enabler')

    def test_no_outcome_fields_or_new_interaction(self):
        r=row(); data={'card':r,'dependency':combat([r])}
        def keys(v):
            if isinstance(v,dict):
                for k,x in v.items(): yield k; yield from keys(x)
            elif isinstance(v,list):
                for x in v: yield from keys(x)
        self.assertFalse(set(keys(data)) & {'combat_occurred','trigger_resolved','poison_received','poison_total','victory'})
        self.assertEqual(interactions([r]),[])
