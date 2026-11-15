"""Run 25 experimental contract and integration regressions; no paid services."""
import copy
import json
import unittest
from collections import Counter
from dataclasses import replace
from mtgadb.model import Card
from services.intelligence import classify_card
from services.abilities import decompose_abilities
from services.dependencies import dependency_findings
from services.evidence_scope import semantic_feature, require_dependency_vocabulary
from services import quality_deck_generator as q
from services.spell_cast_boost import (EXPERIMENTAL as P, DEFAULT, CONTRACT, DEPENDENCY,
    ENABLER, PAYOFF, EFFECT, validate_feature)

SLICK = 'Whenever you cast a noncreature spell, this creature gets +2/+0 until end of turn.'


def face(tid):
    return dict(GrpId=tid, TitleId=tid, LinkedFaceGrpIds='', LinkedFaceType=0, IsPrimaryCard=1)


def card(tid, text='', types='Creature', name=None):
    return Card(tid, name or f'Card {tid}', mana_cost='{R}', cmc=1, types=types,
                colors='R', color_identity='R', power='2', toughness='2', rules_text=text)


def row(c, profile=P, cast=None):
    return dict(classify_card(c, profile=profile, cast_evidence=face(c.title_id) if cast is None else cast), quantity=1)


def boost(*cards, profile=P):
    return [x for x in dependency_findings(list(cards), profile=profile) if x['dependency_id']==DEPENDENCY]


def decorated(c):
    from dataclasses import asdict
    return q.decorate(dict(asdict(c), basic=False, creature='Creature' in c.types.split(), tokens=['R']),
                      profile=P, cast_evidence=face(c.title_id))


class BoostTests(unittest.TestCase):
    def test_resource_gate_does_not_suppress_boost(self):
        spell=decorated(card(1,'Shock deals 2 damage to any target.','Instant'))
        payoff=decorated(card(2,'Prowess\n{T}: Draw a card.'))
        self.assertTrue(payoff['resource_evidence'])
        pool=[spell,payoff]; graph=q.relationships(pool,profile=P)
        self.assertTrue(graph[1])
        _, evidence=q.quality(spell,Counter({2:4}),{c['title_id']:c for c in pool},graph)
        self.assertTrue(any(x['rule_identifier']=='coherence.compatible_dependencies' for x in evidence))

    def test_positive_spell_types(self):
        for typ in ['Instant','Sorcery','Artifact','Enchantment','Planeswalker','Battle']:
            with self.subTest(typ=typ):
                f=boost(row(card(1,types=typ)),row(card(2,SLICK)))
                self.assertEqual(len(f[0]['compatible_pairs']),1)
                self.assertEqual(f[0]['state'],'conditionally_supported')
                self.assertEqual(f[0]['boost_contract'],CONTRACT)

    def test_creatures_and_lands_excluded(self):
        for typ in ['Creature','Artifact Creature','Enchantment Creature','Land','Artifact Land']:
            with self.subTest(typ=typ):
                self.assertFalse(boost(row(card(1,types=typ)),row(card(2,'Prowess')))[0]['compatible_pairs'])

    def test_prowess_is_triggered_with_exact_deltas(self):
        for text,p,t,origin in [('Prowess','1','1','prowess'),(SLICK,'2','0','explicit_trigger')]:
            r=row(card(2,text));a=r['abilities'][0]
            self.assertEqual(a['kind'],'triggered');self.assertEqual(a['trigger']['event'],'spell_cast')
            self.assertEqual(a['effects'][0]['kind'],EFFECT)
            qs={x['kind']:x['value'] for x in a['effects'][0]['qualifiers']}
            self.assertEqual(qs,dict(power_delta=p,toughness_delta=t,duration='until_end_of_turn',mechanic_origin=origin))
            self.assertFalse(any(f['rule_id']=='effect.counter.v1' for f in r['features']))

    def test_exact_self_name_and_reminder(self):
        for text in [SLICK.replace('this creature','Card 2'),
                     'Prowess (Whenever you cast a noncreature spell, this creature gets +1/+1 until end of turn.)']:
            self.assertTrue(any(f['rule_id']==PAYOFF for f in row(card(2,text))['features']))

    def test_non_cast_and_ambiguous_payoffs_fail_closed(self):
        for text in [SLICK.replace('you cast','an opponent casts'),SLICK.replace('cast','copy'),
                     SLICK.replace('you cast a noncreature spell','a noncreature spell resolves'),
                     SLICK.replace('you cast a noncreature spell','a creature enters'),
                     SLICK.replace('this creature','target creature'), SLICK.replace('this creature','another creature'),
                     SLICK+' If you control a Mouse, draw a card.', SLICK.replace('until end of turn.','permanently.'),
                     SLICK.replace('+2/+0','+X/+X'), 'Creatures you control have prowess.',
                     'Whenever you cast a noncreature spell, put a +1/+1 counter on this creature.']:
            with self.subTest(text=text):
                self.assertFalse(any(f['rule_id']==PAYOFF for f in row(card(2,text))['features']))

    def test_faces_missing_linked_and_nonprimary_fail_closed(self):
        for data in [{},dict(face(1),LinkedFaceGrpIds='2'),dict(face(1),LinkedFaceType=2),
                     dict(face(1),IsPrimaryCard=0),dict(face(1),TitleId=999)]:
            with self.subTest(data=data):
                self.assertFalse(any(f['rule_id']==ENABLER for f in row(card(1,types='Instant'),cast=data)['features']))
        r=classify_card(card(2,'Prowess'),profile=P)
        self.assertFalse(any(f['rule_id']==PAYOFF for f in r['features']))
        self.assertEqual(r['abilities'][0]['parse_status'],'unsupported')

    def test_anomalous_and_modal_payoffs_fail_closed(self):
        for c in [replace(card(2,'Prowess'),anomaly_count=1),card(2,'Choose one —\n'+SLICK)]:
            self.assertFalse(any(f['rule_id']==PAYOFF for f in row(c)['features']))

    def test_cast_and_resolution_scope_without_game_state_claim(self):
        f=boost(row(card(1,'Destroy target creature.','Instant')),row(card(2,'Prowess')))[0]
        text=f['explanation']
        self.assertIn('Requires a legal cast',text)
        self.assertIn('No legal-target availability',text)
        self.assertIn('later target illegality does not undo',text)
        self.assertIn('copy not cast creates no cast event',text)
        self.assertEqual(f['compatible_pairs'][0]['support'],'conditional')
        # No target/stack state is consumed: static support does not promise a legal action.
        self.assertNotIn('can_cast',f)

    def test_might_mouse_condition_is_not_a_cast_condition(self):
        text='Target creature gains trample until end of turn. It also gets +1/+0 until end of turn if you control a Mouse.\nDraw a card.'
        r=row(card(1,text,'Instant'));f=boost(r,row(card(2,'Prowess')))[0]
        self.assertTrue(f['compatible_pairs'])
        en=f['compatible_pairs'][0]['enabler_evidence']
        self.assertEqual(en['dependency_context']['origin']['kind'],'card_type')
        self.assertNotIn('Mouse',en['evidence'])
        self.assertFalse(any(e['kind']==EFFECT for a in r['abilities'] for e in a['effects']))

    def test_default_unchanged_even_with_face_metadata(self):
        for c in [card(1,SLICK),card(2,'Prowess'),card(3,types='Artifact')]:
            self.assertEqual(classify_card(c),classify_card(c,profile=DEFAULT,cast_evidence=face(c.title_id)))
            self.assertFalse(boost(row(c),profile=DEFAULT))

    def test_roundtrip_and_legacy_contract_rejection(self):
        for r in [row(card(1,types='Instant')),row(card(2,'Prowess'))]:
            f=next(f for f in r['features'] if f['rule_id'] in (ENABLER,PAYOFF))
            self.assertEqual(validate_feature(f,profile=P),validate_feature(json.loads(json.dumps(f)),profile=P))
            with self.assertRaises(ValueError):validate_feature(f,profile=DEFAULT)
            with self.assertRaises(ValueError):semantic_feature(f)
            for version in ("3", "4"):
                with self.assertRaises(ValueError):require_dependency_vocabulary(f,version)

    def test_tampered_evidence_fails_closed(self):
        source=row(card(2,'Prowess'));base=next(f for f in source['features'] if f['rule_id']==PAYOFF)
        for mutate in [lambda f:f['boost_contract'].update(evidence_version='wrong'),
                       lambda f:f['dependency_context']['effect']['qualifiers'][0].update(value='2'),
                       lambda f:f['dependency_context']['origin']['trigger'].update(controller='opponent'),
                       lambda f:f['dependency_context']['effect'].update(target='target_creature'),
                       lambda f:f['cast_evidence'].update(LinkedFaceType=2),
                       lambda f:f.update(source_types='Enchantment')]:
            bad=copy.deepcopy(base);mutate(bad)
            with self.assertRaises(ValueError):validate_feature(bad,profile=P)
            self.assertFalse(boost(row(card(1,types='Instant')),dict(source,features=[bad]))[0]['compatible_pairs'])

    def test_missing_duplicate_and_self_pairs(self):
        en=row(card(1,types='Instant'));pay=row(card(2,'Prowess'))
        self.assertFalse(boost(pay)[0]['compatible_pairs'])
        pay['features']+=copy.deepcopy(pay['features'])
        self.assertEqual(len(boost(en,pay)[0]['compatible_pairs']),1)
        self.assertFalse(boost(en,dict(pay,title_id=1))[0]['compatible_pairs'])

    def test_graph_scoring_and_bounds(self):
        en=decorated(card(1,types='Instant'));pay=decorated(card(2,'Prowess'));pool=[en,pay]
        graph=q.relationships(pool,profile=P);cards={x['title_id']:x for x in pool}
        self.assertTrue(graph[1]);self.assertFalse(q.relationships(pool))
        for count,expected in [(0,0),(1,2),(4,8),(20,8)]:
            _,ev=q.quality(en,Counter({2:count}),cards,graph)
            self.assertEqual(sum(e['numeric_contribution'] for e in ev if e['rule_identifier']=='coherence.compatible_dependencies'),expected)

    def test_old_draw_dependency_still_resource_gated(self):
        en=decorated(card(1,types='Instant'))
        pay=decorated(card(2,'Whenever you cast an instant or sorcery spell, draw a card.'))
        pool=[en,pay];g=q.relationships(pool,profile=P)
        self.assertTrue(g[1])
        _,ev=q.quality(en,Counter({2:4}),{x['title_id']:x for x in pool},g)
        self.assertFalse(any(e['rule_identifier']=='coherence.compatible_dependencies' for e in ev))

    def test_experimental_profile_changes_actual_synthetic_selection(self):
        from unittest.mock import patch
        from tests.test_quality_generator import fixture
        f=fixture()
        candidates=[c for c in f['cards'].values() if c['colors']=='R' and c['types']=='Creature' and c['cmc']==1]
        payoff=candidates[-1];payoff.update(rules_text='Prowess',power='1',toughness='1')
        with patch('services.quality_deck_generator.read_facts',return_value=f), \
             patch('services.generated_deck_validator.read_facts',return_value=f), \
             patch('services.spell_cast_boost.load_cast_evidence',return_value={t:face(t) for t in f['cards']}):
            default,_=q.generate({}, {}, 'std-bo1-br')
            experimental,raw=q.generate({'arena':'unused'}, {}, 'std-bo1-br',profile=P)
        report=json.loads(raw)
        self.assertNotEqual(default,experimental)
        self.assertTrue(report['recognition']['selected_main_boost_pairs'])
        self.assertTrue(report['winning_trials'])
        self.assertIn(payoff['name'],experimental)
        self.assertEqual(report['checks']['legality']['status'],'PASS')

    def test_gate_report_requires_only_applicable_color_pair_checks(self):
        from scripts.evaluate_run25 import checks_pass
        checks={k:{'status':'PASS'} for k in ('legality','template','color_pair')}
        self.assertTrue(checks_pass('std-bo1-wu',checks))
        checks['color_pair']['status']='not applicable'
        self.assertTrue(checks_pass('std-bo1',checks))
        self.assertTrue(checks_pass('std-bo3',checks))
        self.assertFalse(checks_pass('std-bo1-wu',checks))
        checks['legality']['status']='FAIL'
        self.assertFalse(checks_pass('std-bo1',checks))

    def test_unknown_profile_rejected(self):
        with self.assertRaises(ValueError):classify_card(card(1),profile='typo')
        with self.assertRaises(ValueError):dependency_findings([],profile='typo')


if __name__=='__main__':unittest.main()
