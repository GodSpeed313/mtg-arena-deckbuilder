"""Generator B: bounded relational scoring, deterministic construction, no AI."""
from collections import Counter, defaultdict
from dataclasses import fields
from fractions import Fraction
from itertools import combinations
import hashlib
from pathlib import Path

from mtgadb.model import Card, Resolution
from services.intelligence import classify_card
from services.dependencies import dependency_findings
from services.deck_construction_data import COLORS, read_facts
from services.deterministic_deck_generator import prepare, canonical
from services.generated_deck_validator import validate_text
from services.evaluation_requests_v2 import REQUESTS, allowed, represented_colors, check_pair

SLOTS = ((1,True,4),(2,True,8),(3,True,8),(4,True,4),
         (2,False,4),(3,False,4),(4,False,4))
DEMAND = {(b,c):n for b,c,n in SLOTS}
PERMANENTS = {'Creature','Artifact','Enchantment','Planeswalker','Land','Battle'}


def decorate(card):
    c=dict(card)
    kw={f.name:c[f.name] for f in fields(Card) if f.name in c and c[f.name] is not None and f.name!='resolution'}
    kw['resolution']=Resolution.ANOMALY if c.get('anomaly_count') else Resolution.SINGLE
    analysis=classify_card(Card(**kw))
    c['analysis']=dict(analysis,quantity=1)
    c['slot']=(min(4,int(c['cmc'])),c['creature'])
    c['permanent']=bool(set(c['types'].split()) & PERMANENTS)
    c['limit']=2 if c['permanent'] and 'Legendary' in c['types'].split() else 4
    c['mechanical_colors']=sorted(represented_colors(c))
    c['recognized']=[a for a in analysis['abilities'] if a['parse_status'] in ('supported','partial')]
    c['interaction_evidence']=[f for f in analysis['features'] if f['dimension']=='role' and f['label'] in ('removal','counterspell')]
    c['interaction_evidence'] += [a for a in c['recognized'] if any(e['kind'] in ('destroy','exile') and (e.get('target') or '').startswith('target_') for e in a['effects'])]
    c['interaction']=bool(c['interaction_evidence'])
    c['resource_evidence']=[f for f in analysis['features'] if f['label'] in ('ramp','mana_fixing','card_draw','card_selection') and f['dimension']=='role'] if c['permanent'] else []
    c['resource_evidence'] += [a for a in c['recognized'] if any(e['kind']=='draw' for e in a['effects'])] if c['permanent'] else []
    c['sink']=any(a['kind']=='activated' and a['effects'] and any(x['kind']=='mana' for x in a['costs']) for a in c['recognized'])
    return c


def relationships(pool):
    graph=defaultdict(list)
    for finding in dependency_findings([c['analysis'] for c in pool]):
        for pair in finding['compatible_pairs']:
            a,b=pair['enabler_title_id'],pair['payoff_title_id']
            if a==b or pair['support']=='unestablished':
                continue
            edge=dict(rule_id=finding['dependency_id'],cards=[a,b],support=pair['support'],evidence=[pair['enabler_evidence'],pair['payoff_evidence']])
            if edge not in graph[a]:graph[a].append(edge)
            if edge not in graph[b]:graph[b].append(edge)
    return graph


def resource_context(card, selected, cards):
    high=[t for t,n in selected.items() if n and t!=card['title_id'] and cards[t]['cmc']>=5 and (cards[t]['creature'] or cards[t]['recognized'])]
    sinks=[t for t,n in selected.items() if n and t!=card['title_id'] and cards[t]['sink']]
    detail=dict(recognized_resource_abilities=card['resource_evidence'],higher_cost_payoff_cards=high,mana_sink_cards=sinks,higher_cost_copies=sum(selected[t] for t in high),sink_copies=sum(selected[t] for t in sinks),threshold='6 copies at MV>=5 or 4 copies with recognized activated mana costs')
    return detail['higher_cost_copies']>=6 or detail['sink_copies']>=4,detail


def quality(card, selected, cards, graph):
    """Per-copy score against other selected cards. No name-based ranking."""
    evidence=[]
    def add(rule,value,source,detail):
        evidence.append(dict(rule_identifier=rule,numeric_contribution=value,evidence_source=source,evidence=detail))
    add('curve.efficiency',-card['cmc'],'general heuristic',dict(mana_value=card['cmc'],target_slot=card['slot']))
    if card['creature']:
        # Printed stats are not a claim of recognized ability or strategic value.
        pt=[card.get(k) for k in ('power','toughness')]
        stat=min(4,sum(int(x) for x in pt)/2) if all(isinstance(x,str) and x.isdigit() for x in pt) else 0
        add('body.printed_stats',stat,'general heuristic',dict(power=pt[0],toughness=pt[1],cap=4))
    if card['interaction']:
        add('interaction.reviewed',4,'recognized card text',card['interaction_evidence'])
    active=[]; partners=defaultdict(set)
    for edge in graph.get(card['title_id'],[]):
        other=next(t for t in edge['cards'] if t!=card['title_id'])
        if selected.get(other,0):
            if any(cards[t]['resource_evidence'] and not resource_context(cards[t],selected,cards)[0] for t in edge['cards']):
                continue
            active.append(edge);partners[other].add(edge['rule_id'])
    if active:
        n=sum(min(selected[t],4) for t in partners)
        add('coherence.compatible_dependencies',min(12,2*n),'inferred relationship',active)
    # Multiple dependencies must actually share a participant, not merely coexist.
    if len({e['rule_id'] for e in active})>=2:
        add('hybrid.shared_enabler_or_payoff',2,'inferred relationship',dict(shared_title_id=card['title_id'],relationships=active))
    if card['resource_evidence']:
        usable,detail=resource_context(card,selected,cards)
        add('resources.context_gate',2 if usable else -3,'general heuristic',detail)
    return sum(x['numeric_contribution'] for x in evidence),evidence


def build_main(pool, graph, pair, seed=()):
    cards={c['title_id']:c for c in pool}; main=Counter(); remaining=dict(DEMAND)
    interaction_target=min(4,sum(c['limit'] for c in pool if c['interaction']))
    def choices(predicate=lambda c:True):
        return [c for c in pool if remaining.get(c['slot'],0)>0 and main[c['title_id']]<c['limit'] and predicate(c)]
    def put(c):
        main[c['title_id']]+=1;remaining[c['slot']]-=1
    def best(options):
        if not options:raise ValueError('no candidate can meet remaining registered template constraints')
        return min(options,key=lambda c:(-quality(c,main,cards,graph)[0],c['cmc'],c['title_id']))
    for tid in seed:
        c=cards.get(tid)
        if c and c in choices():put(c)
    while sum(n for t,n in main.items() if cards[t]['interaction'])<interaction_target:
        put(best(choices(lambda c:c['interaction'])))
    if pair:
        for color in pair:
            while sum(n for t,n in main.items() if color in cards[t]['mechanical_colors'])<8:
                put(best(choices(lambda c:color in c['mechanical_colors'])))
    for bucket,creature,needed in SLOTS:
        while remaining[(bucket,creature)]:
            put(best(choices(lambda c:c['slot']==(bucket,creature))))
    return main,interaction_target


def template(main, side, cards, target):
    violations=[]; actual=Counter(); lands=0; creatures=0
    for tid,n in main.items():
        c=cards[tid]
        if c['basic']:lands+=n
        else:
            actual[c['slot']]+=n;creatures+=n*int(c['creature'])
    if lands!=24 or sum(main.values())!=60 or creatures!=24:violations.append('requires 60 main, 24 basics, 24 creatures')
    if actual!=Counter(DEMAND):violations.append('curve/creature slots differ from target')
    if sum(n for t,n in main.items() if not cards[t]['basic'] and cards[t]['interaction'])<target:violations.append('recognized interaction minimum missed')
    if any(n>cards[t]['limit'] for t,n in (main+side).items() if not cards[t]['basic']):violations.append('legendary/general copy limit exceeded')
    return dict(status='FAIL' if violations else 'PASS',violations=violations,lands=lands,creatures=creatures,curve={str(b):sum(n for (v,c),n in actual.items() if v==b) for b in range(1,5)},interaction_minimum=target,interaction_scope='lower bound limited by grammar coverage; minimum 4 or available template-compatible capacity')


def generate(paths,pins,request_id):
    if request_id not in REQUESTS:raise ValueError('unregistered request')
    req=REQUESTS[request_id];pair=req['color_pair'];queue=req['queue']
    facts=read_facts(paths,pins,queue)
    raw,basics,excluded=prepare(facts,queue)
    pool=[]
    for c in raw:
        if not allowed(c,pair):
            excluded.append(dict(title_id=c['title_id'],reasons=['color_identity_or_pair_exclusion']));continue
        c=decorate(c)
        if c['slot'] not in DEMAND:
            excluded.append(dict(title_id=c['title_id'],reasons=['outside_generator_b_template']));continue
        pool.append(c)
    graph=relationships(pool)
    colorsets=[pair] if pair else [''.join(c) for n in (1,2) for c in combinations(COLORS,n)]
    trials=[];measurements=[]
    for colors in colorsets:
        available=[c for c in pool if set(c['mechanical_colors'])<=set(colors)]
        capacities={slot:sum(c['limit'] for c in available if c['slot']==slot) for slot in DEMAND}
        fills=[min(Fraction(capacities[s],n),1) for s,n in DEMAND.items()]
        measure=dict(colors=colors,slot_fractions=[str(x) for x in fills],primary_sufficiency=str(sum(fills)/len(fills)),failures=[])
        measurements.append(measure)
        if not all(x==1 for x in fills) or not all(any(b['land_color']==color for b in basics) for color in colors):
            measure['failures'].append('insufficient template or verified basic sources');continue
        ids={c['title_id'] for c in available}
        seeds={()}
        for tid in sorted(ids):
            for edge in graph.get(tid,[]):
                if set(edge['cards'])<=ids:seeds.add(tuple(edge['cards']))
        # Bounded deterministic seed exploration, never name-based.
        seedlist=[()]+sorted(s for s in seeds if s)[:8]
        cards={c['title_id']:c for c in available}
        for seed in seedlist:
            try:main,target=build_main(available,graph,pair,seed)
            except ValueError as e:
                measure['failures'].append(str(e));continue
            score=sum(n*quality(cards[t],main,cards,graph)[0] for t,n in main.items())
            trials.append((score,colors,tuple(t for t,n in sorted(main.items()) for _ in range(n)),main,target,available))
    if not trials:raise ValueError('request failed without relaxation: '+canonical(measurements))
    score,colors,_,main,target,available=min(trials,key=lambda x:(-x[0],len(x[1]),tuple(COLORS.index(c) for c in x[1]),x[2]))
    cards={c['title_id']:c for c in pool+basics};side=Counter();pips=Counter()
    for tid,n in main.items():
        for token in cards[tid]['tokens']:
            if token in COLORS:pips[token]+=n
    if not sum(pips.values()):raise ValueError('no colored casting demand; no silent mana fallback')
    quota={c:Fraction(24*pips[c],sum(pips.values())) for c in colors}; allocation={c:int(q) for c,q in quota.items()}
    for c in sorted(colors,key=lambda c:(-(quota[c]-allocation[c]),COLORS.index(c)))[:24-sum(allocation.values())]:allocation[c]+=1
    for color,n in allocation.items():
        if n:
            b=min((b for b in basics if b['land_color']==color),key=lambda b:b['title_id']);main[b['title_id']]=n
    nonland=Counter({t:n for t,n in main.items() if not cards[t]['basic']})
    if queue=='BO3':
        for c in sorted(available,key=lambda c:(not c['interaction'],-quality(c,nonland,cards,graph)[0],c['cmc'],c['title_id'])):
            n=min(c['limit']-main[c['title_id']],15-sum(side.values()))
            if n>0:side[c['title_id']]=n
            if sum(side.values())==15:break
    lines=[];rows=[]
    for zone,counts in [('main',main),('sideboard',side)]:
        if zone=='sideboard' and queue=='BO1':continue
        if lines:lines.append('')
        lines.append('Deck' if zone=='main' else 'Sideboard')
        for tid,n in sorted(counts.items()):
            if not n:continue
            c=cards[tid];p=c['printing'];lines.append(f"{n} {c['name']} ({p['set_code']}) {p['collector_number']}")
            rows.append(dict(card=c,quantity=n,zone=zone))
    text='\n'.join(lines)+'\n'
    checks=dict(legality=validate_text(text,paths,pins,queue),template=template(main,side,cards,target),color_pair=check_pair(rows,pair))
    if any(v['status']=='FAIL' for v in checks.values()):raise ValueError('validation failed: '+canonical(checks))
    contributions={}
    for tid,n in (main+side).items():
        if not n:continue
        c=cards[tid]
        if c['basic']:
            contributions[str(tid)]=dict(name=c['name'],quantity=n,score=0,contributions=[dict(rule_identifier='mana.basic_allocation',numeric_contribution=0,evidence_source='general heuristic',evidence=dict(land_color=c['land_color'],colored_pips=dict(pips),rounding='largest remainder, WUBRG tie'))])
        else:
            value,evidence=quality(c,nonland,cards,graph)
            contributions[str(tid)]=dict(name=c['name'],quantity=n,score=value,contributions=evidence)
    source_files=['services/quality_deck_generator.py','services/evaluation_requests_v2.py','docs/evaluation_requests_v2.json']
    root=Path(__file__).resolve().parents[1]
    return text,canonical(dict(generator='B',request_id=request_id,request=req,checks=checks,arena_compatibility='pending operator testing',contributions=contributions,selection_score=score,color_measurements=measurements,excluded=excluded,mana_allocation=allocation,nonbasic_lands='deferred',sideboard_shortfall_reason='insufficient remaining eligible copy capacity' if queue=='BO3' and sum(side.values())<15 else None,provenance=facts['provenance'],snapshot_version=facts['snapshot']['snapshot_version'],source_revision={f:hashlib.sha256((root/f).read_bytes()).hexdigest() for f in source_files},limitations='Heuristic quality is not demonstrated competitive strength. Unknown abilities add no positive recognition evidence. Conditional dependencies are potential, not established gameplay events.'))
