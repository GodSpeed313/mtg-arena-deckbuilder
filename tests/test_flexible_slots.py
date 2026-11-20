from collections import Counter
from copy import deepcopy
import hashlib
import inspect
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from services import quality_deck_generator as q
from services.flexible_slots import (PROFILE, POLICY, SENSITIVITY_LABEL,
                                    decision_record, enabler_credit, validate_template)
from services.spell_cast_boost import EXPERIMENTAL
from tests.test_quality_generator import fixture


BASE = '124ea40e7edb9c7102f8e794fc0a2e57a2ed52df'


def face(tid):
    return dict(GrpId=tid, TitleId=tid, LinkedFaceGrpIds='', LinkedFaceType=0, IsPrimaryCard=1)


def candidate(tid, bucket, creature, *, score_body='2', interaction=False):
    raw=dict(title_id=tid, name=f'Card {tid}', arena_id=tid,
             types='Creature' if creature else 'Sorcery',
             mana_cost='{W}' if bucket==1 else '{'+str(bucket-1)+'}{W}',
             cmc=bucket, colors='W', color_identity='W',
             rules_text='Destroy target creature.' if interaction else '',
             power=score_body, toughness=score_body, anomaly_count=0,
             basic=False, creature=creature, tokens=['W'],
             printing=dict(set_code='TST',collector_number=str(tid)))
    return q.decorate(raw,profile=EXPERIMENTAL,cast_evidence=face(tid))


def pool():
    result=[];tid=0
    for b,c,n in q.SLOTS:
        for _ in range(n//4+1):
            tid+=1;result.append(candidate(tid,b,c,interaction=not c))
    return result


def add_basics(main, cards):
    cards[9999]=dict(basic=True,creature=False,limit=1000)
    main[9999]=24


class FlexibleSlotsTests(unittest.TestCase):
    def run_build(self, p=None, seed=(), pair=None, extra=()):
        p=p or pool();record={}
        main,target=q.build_main(p,{},pair,seed,flexible_slots=True,
                                 flex_only=extra,flex_record=record)
        return main,target,record,{c['title_id']:c for c in [*p,*extra]}

    def test_four_buckets_last_general_decision_and_native_capacity(self):
        extra=[candidate(1000,1,False,interaction=True)]
        main,target,r,cards=self.run_build(extra=extra)
        self.assertEqual([d['bucket'] for d in r['decisions']],[1,2,3,4])
        self.assertEqual(r['forfeited_by_bucket'],dict.fromkeys(['1','2','3','4'],0))
        self.assertEqual(len(r['assignments']),4)
        for d in r['decisions']:
            b=d['bucket']; prior=d['selected_before']
            count=sum(n for t,n in prior.items() if cards[t]['creature'] and cards[t]['slot'][0]==b)
            self.assertEqual(count,dict(POLICY['creature_slots'])[b]-1)
            self.assertEqual(d['winner']['card_type'],'noncreature')
            self.assertEqual(d['best_alternative_other_type']['card_type'],'creature')
        add_basics(main,cards)
        result=validate_template(main,Counter(),cards,target,r['assignments'],profile=PROFILE)
        self.assertEqual(result['status'],'PASS',result)
        self.assertEqual((result['creatures'],result['noncreatures']),(20,16))
        self.assertTrue(all(main[t]<=c['limit'] for t,c in cards.items()))

    def test_same_bucket_excludes_stronger_other_bucket(self):
        extra=[candidate(1000,1,False,interaction=True),candidate(1001,2,False,interaction=True)]
        _,_,r,cards=self.run_build(extra=extra)
        for d in r['decisions']:
            self.assertEqual(cards[d['winner']['title_id']]['slot'][0],d['bucket'])
            a=d['best_alternative_other_type']
            if a:self.assertEqual(cards[a['title_id']]['slot'][0],d['bucket'])

    def test_forfeiture_when_seeds_fill_bucket_never_moves(self):
        p=pool();tid=next(c['title_id'] for c in p if c['slot']==(1,True))
        _,_,r,_=self.run_build(p,seed=(tid,)*4)
        self.assertEqual(r['forfeited_by_bucket']['1'],1)
        self.assertEqual([d['bucket'] for d in r['decisions']],[2,3,4])

    def test_color_minimum_can_forfeit_bucket_and_stays_original(self):
        p=pool()
        original=subprocess.check_output(['git','show',BASE+':services/quality_deck_generator.py'],text=True)
        namespace={'__file__':q.__file__}
        exec(compile(original,'run27-baseline-test','exec'),namespace)
        def early_calls(fn,**kwargs):
            seen=[]
            def trace(frame,event,arg):
                if (event=='call' and frame.f_code.co_name=='put'
                        and frame.f_back.f_code is fn.__code__
                        and 'bucket' not in frame.f_back.f_locals):
                    seen.append(frame.f_locals['c']['title_id'])
                return trace if frame.f_code is fn.__code__ else None
            sys.settrace(trace)
            try:fn(p,{},'W',(),**kwargs)
            finally:sys.settrace(None)
            return seen
        r={}
        self.assertEqual(early_calls(namespace['build_main']),
                         early_calls(q.build_main,flexible_slots=True,flex_record=r,
                                     flex_only=[candidate(1000,1,False,interaction=True)]))
        self.assertEqual(r['forfeited_by_bucket']['1'],1)
        self.assertEqual(len(r['assignments']),sum(d['winner']['card_type']=='noncreature' for d in r['decisions']))
        self.assertEqual(sum(r['forfeited_by_bucket'].values())+len(r['decisions']),4)

    def test_copy_capacity_is_shared_across_base_and_flex(self):
        main,_,r,cards=self.run_build()
        self.assertTrue(r['assignments'])
        for a in r['assignments']:
            self.assertLessEqual(main[a['title_id']],cards[a['title_id']]['limit'])
        add_basics(main,cards)
        a=r['assignments'][0];tid=a['title_id']
        side=Counter({tid:cards[tid]['limit']-main[tid]+1})
        self.assertEqual(validate_template(main,side,cards,0,r['assignments'],profile=PROFILE)['status'],'FAIL')

    def test_existing_tie_order_not_creature_first(self):
        # At MV1: body zero -> score -1, spell -> -1. Smaller spell ID wins.
        p=pool()
        p=[candidate(c['title_id']+100,1,True,score_body='0') if c['slot']==(1,True) else c for c in p]
        extra=[candidate(90,1,False)]
        _,_,r,_=self.run_build(p,extra=extra)
        d=r['decisions'][0]
        self.assertEqual(d['score_margin'],0)
        self.assertEqual(d['winner']['title_id'],90)
        # At bucket4+, actual MV precedes ID when score ties.
        c=candidate(2,4,True,score_body='1');s=candidate(1,5,False,interaction=True)
        c['interaction']=False
        # Scores -3 and -1 are not a tie; use a controlled quality result only
        # to exercise the unchanged selector's tuple via the real build path.
        p=pool();p=[x for x in p if x['slot']!=(4,True)]+[c,candidate(3,4,True,score_body='1')]
        def equal_quality(card,*args):return (0,[])
        with patch.object(q,'quality',side_effect=equal_quality):
            _,_,r,_=self.run_build(p,extra=[s])
        self.assertEqual(r['decisions'][-1]['winner']['actual_mana_value'],4)

    def test_no_quota_and_no_other_type_measurement_is_null(self):
        p=pool()
        p=[candidate(c['title_id'],c['slot'][0],c['creature'],score_body='8') if c['creature'] else
           candidate(c['title_id'],c['slot'][0],False) for c in p]
        _,_,r,_=self.run_build(p)
        self.assertEqual(r['assignments'],[])
        self.assertEqual(r['decisions'][0]['best_alternative_other_type'],None)
        self.assertEqual(r['decisions'][0]['score_margin'],None)
        self.assertIsNone(r['decisions'][0]['sensitivity']['outcome_reverses'])

    def test_validator_accepts_entire_declared_range(self):
        original=pool();main,target=q.build_main(original,{},None)
        cards={c['title_id']:c for c in original};add_basics(main,cards)
        assignments=[]
        for conversions in range(5):
            result=validate_template(main,Counter(),cards,target,assignments,profile=PROFILE)
            self.assertEqual(result['status'],'PASS',result)
            self.assertEqual(result['creatures'],24-conversions)
            if conversions==4:break
            bucket=conversions+1
            tid=next(t for t,n in main.items() if n and not cards[t]['basic'] and cards[t]['slot']==(bucket,True))
            main[tid]-=1
            spell=2000+bucket;cards[spell]=candidate(spell,bucket,False)
            main[spell]=1;assignments.append(dict(bucket=bucket,title_id=spell,quantity=1))
        # A fifth conversion cannot be admitted by fabricated output bounds.
        tid=next(t for t,n in main.items() if n and not cards[t]['basic'] and cards[t]['slot']==(2,True))
        main[tid]-=1;main[2002]+=1;assignments.append(dict(bucket=2,title_id=2002,quantity=1))
        self.assertEqual(validate_template(main,Counter(),cards,target,assignments,profile=PROFILE)['status'],'FAIL')
        with self.assertRaises(ValueError):validate_template(main,Counter(),cards,target,assignments,profile={'creature_range':[19,24]})

    def test_default_rejects_23_and_flex_rejects_bucket_and_base_corruption(self):
        original=pool();main,target=q.build_main(original,{},None)
        cards={c['title_id']:c for c in original};add_basics(main,cards)
        tid=next(t for t,n in main.items() if n and not cards[t]['basic'] and cards[t]['slot']==(1,True))
        main[tid]-=1;cards[2001]=candidate(2001,1,False);main[2001]=1
        assignments=[dict(bucket=1,title_id=2001,quantity=1)]
        self.assertEqual(q.template(main,Counter(),cards,target)['status'],'FAIL')
        self.assertEqual(validate_template(main,Counter(),cards,target,assignments,profile=PROFILE)['status'],'PASS')
        assignments[0]['bucket']=2
        self.assertEqual(validate_template(main,Counter(),cards,target,assignments,profile=PROFILE)['status'],'FAIL')
        assignments[0]['bucket']=1
        t=next(t for t,n in main.items() if n and not cards[t]['basic'] and cards[t]['slot']==(2,False))
        main[t]-=1;main[2001]+=1
        self.assertEqual(validate_template(main,Counter(),cards,target,assignments,profile=PROFILE)['status'],'FAIL')

    def test_enabler_sensitivity_preserves_shared_family_credit(self):
        c=candidate(1,1,False)
        evidence=[dict(rule_identifier='coherence.compatible_dependencies',numeric_contribution=12,
                       evidence=[dict(rule_id='dependency.noncreature_cast_boost.v1',cards=[1,2]),
                                 dict(rule_id='other',cards=[1,2]),dict(rule_id='other',cards=[1,3])])]
        self.assertEqual(enabler_credit(c,{2:4,3:2},evidence),0)
        evidence[0]['evidence']=evidence[0]['evidence'][:1]
        evidence[0]['numeric_contribution']=8
        self.assertEqual(enabler_credit(c,{2:4},evidence),8)

    def test_sensitivity_uses_original_tie_key_and_never_changes_scores(self):
        s=candidate(1,1,False);c=candidate(2,1,True)
        evidence=[dict(rule_identifier='coherence.compatible_dependencies',numeric_contribution=8,
                       evidence=[dict(rule_id='dependency.noncreature_cast_boost.v1',cards=[1,2])])]
        def scorer(card,*args):return (7,evidence) if card['title_id']==1 else (1,[])
        r=decision_record(1,s,c,{2:4},{1:s,2:c},{},scorer)
        self.assertEqual(r['sensitivity']['label'],SENSITIVITY_LABEL)
        self.assertTrue(r['sensitivity']['outcome_reverses'])
        self.assertEqual(r['winner']['score'],7)
        self.assertEqual(r['winner']['arithmetic_score'],-1)
        self.assertEqual(r['winner']['roles'],['unknown'])

    def test_scoring_function_source_unchanged_from_run27(self):
        import ast
        original=subprocess.check_output(['git','show',BASE+':services/quality_deck_generator.py'],text=True)
        def fn(text):
            tree=ast.parse(text)
            return ast.dump(next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='quality'))
        self.assertEqual(fn(original),fn(inspect.getsource(q)))

    def test_flag_rejects_default_and_non_boolean(self):
        with self.assertRaises(ValueError):q.generate({}, {},'std-bo1',flexible_slots=True)
        with self.assertRaises(ValueError):q.generate({}, {},'std-bo1',profile=EXPERIMENTAL,flexible_slots=1)

    def test_synthetic_all_requests_validate_and_repeat(self):
        f=fixture();tid=max(f['cards'])+1
        for color in 'WUBRG':
            f['cards'][tid]=dict(title_id=tid,name=f'Cheap {color}',types='Sorcery',mana_cost='{'+color+'}',cmc=1,colors=color,color_identity=color,rules_text='Destroy target creature.',anomaly_count=0)
            f['printings'][tid]=dict(arena_id=tid,title_id=tid,set_code='TST',collector_number=str(tid),rarity='common')
            f['snapshot']['payload']['titles'][str(tid)]=dict(queues={q:dict(status='legal') for q in ['BO1','BO3']});tid+=1
        with patch('services.quality_deck_generator.read_facts',side_effect=lambda *a:deepcopy(f)),patch('services.generated_deck_validator.read_facts',side_effect=lambda *a:deepcopy(f)),patch('services.spell_cast_boost.load_cast_evidence',return_value={t:face(t) for t in f['cards']}):
            for rid in q.REQUESTS:
                args=({'arena':'unused'},{},rid)
                text,raw=q.generate(*args,profile=EXPERIMENTAL,flexible_slots=True)
                self.assertEqual((text,raw),q.generate(*args,profile=EXPERIMENTAL,flexible_slots=True))
                report=json.loads(raw)
                self.assertEqual(report['checks']['legality']['status'],'PASS')
                self.assertEqual(report['checks']['template']['status'],'PASS')
                self.assertEqual(sum(report['flexible_slots']['forfeited_by_bucket'].values())+len(report['flexible_slots']['decisions']),4)

    def test_all_frozen_default_and_run27_baseline_equivalence(self):
        from services.deck_construction_data import SNAPSHOT,DATABASE,ARENA,PINS
        root=Path(__file__).resolve().parents[1]
        paths=dict(snapshot=SNAPSHOT,database=DATABASE,arena=ARENA)
        for profile,folder,label in [('default',root/'run25-artifacts/evaluation-final','default'),(EXPERIMENTAL,root/'run27-artifacts','experimental')]:
            manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
            for rid in q.REQUESTS:
                text,report=q.generate(paths,PINS,rid,profile=profile)
                for extension in ('txt','json'):
                    name=f'{rid}-{label}.{extension}'
                    self.assertEqual(hashlib.sha256((folder/name).read_bytes()).hexdigest(),manifest[name])
                self.assertEqual(text.encode('utf-8'),(folder/f'{rid}-{label}.txt').read_bytes())
                old=json.loads((folder/f'{rid}-{label}.json').read_text(encoding='utf-8'));new=json.loads(report)
                path='services/quality_deck_generator.py'
                del old['source_revision'][path];del new['source_revision'][path]
                self.assertEqual(new,old)


if __name__=='__main__':unittest.main()
