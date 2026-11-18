"""Run 27: frozen-deck rescoring first, then deterministic new generation."""
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from services.deck_construction_data import SNAPSHOT, DATABASE, ARENA, PINS, read_facts
from services.quality_deck_generator import generate, decorate, relationships
from services.spell_cast_boost import EXPERIMENTAL, DEPENDENCY, PAYOFF, load_cast_evidence
from services.spell_support_guard import assess
from services.evaluation_requests_v2 import REQUESTS


def write(path, value):
    with path.open('x',encoding='utf-8',newline='\n') as f:
        json.dump(value,f,indent=2,ensure_ascii=False);f.write('\n')


def parse(text, facts, faces):
    cards={};zones={'main':Counter(),'sideboard':Counter()};zone='main'
    for line in text.splitlines():
        if line=='Sideboard':zone='sideboard';continue
        if not line or line=='Deck':continue
        m=re.fullmatch(r'(\d+) (.+) \(([^)]+)\) (\S+)',line)
        if not m:raise ValueError('unrecognized deck row')
        quantity,name,set_code,number=m.groups()
        matches=[p for p in facts['printings'].values() if p['set_code']==set_code and p['collector_number']==number and facts['cards'][p['title_id']]['name']==name]
        if len(matches)!=1:raise ValueError('ambiguous printing')
        p=matches[0];tid=p['title_id'];c=facts['cards'][tid]
        row=dict(c,basic=p['arena_id'] in facts['basic'],creature='Creature' in c['types'].split())
        cards[tid]=decorate(row,profile=EXPERIMENTAL,cast_evidence=faces.get(p['arena_id']))
        zones[zone][tid]+=int(quantity)
    return cards,zones


def measurements(text,facts,faces,old=None):
    cards,zones=parse(text,facts,faces);graph=relationships(list(cards.values()),profile=EXPERIMENTAL)
    rows=[]
    for zone,counts in zones.items():
        for tid,n in sorted(counts.items()):
            if not any(f['rule_id']==PAYOFF for f in cards[tid]['analysis']['features']):continue
            ids={e['cards'][0] for e in graph.get(tid,[]) if e['rule_id']==DEPENDENCY and e['cards'][1]==tid}
            row=dict(title_id=tid,name=cards[tid]['name'],zone=zone,copies=n,
                     support=assess(zones['main'],cards,ids),
                     note='Sideboard payoff, if present, assessed against main only; no post-board plan.' if zone=='sideboard' else 'Main deck')
            if old is not None:
                previous=old['contributions'][str(tid)]['contributions']
                coherence=[e for e in previous if e['rule_identifier']=='coherence.compatible_dependencies']
                if any(edge['rule_id']!=DEPENDENCY for e in coherence for edge in e['evidence']):
                    raise ValueError('old bonus has mixed families; attribution unavailable')
                row['old_bonus']=sum(e['numeric_contribution'] for e in coherence)
            rows.append(row)
    return rows,zones


def differences(a,b,cards):
    return {zone:[dict(title_id=t,name=cards[t]['name'],old=a[zone].get(t,0),new=b[zone].get(t,0))
                  for t in sorted(set(a[zone])|set(b[zone])) if a[zone].get(t,0)!=b[zone].get(t,0)] for zone in a}


def evaluate(output):
    root=Path(__file__).resolve().parents[1];frozen=root/'run25-artifacts'/'evaluation-final'
    manifest=json.loads((frozen/'manifest.json').read_text(encoding='utf-8'))
    texts={};old_reports={}
    for rid in REQUESTS:
        for profile in ['default','experimental']:
            name=f'{rid}-{profile}.txt';raw=(frozen/name).read_bytes()
            if hashlib.sha256(raw).hexdigest()!=manifest[name]:raise ValueError('frozen hash mismatch: '+name)
            texts[rid,profile]=raw.decode('utf-8')
        name=rid+'-experimental.json';raw=(frozen/name).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=manifest[name]:raise ValueError('frozen report hash mismatch: '+name)
        old_reports[rid]=json.loads(raw)
    paths=dict(snapshot=SNAPSHOT,database=DATABASE,arena=ARENA)
    facts=read_facts(paths,PINS,'BO1');faces=load_cast_evidence(ARENA)
    output.mkdir(exist_ok=False)
    step1=[]
    for rid in REQUESTS:
        rows,_=measurements(texts[rid,'experimental'],facts,faces,old_reports[rid])
        step1.append(dict(request_id=rid,payoffs=rows))
    # All five frozen decks are rescored and persisted before any generation.
    write(output/'step1-frozen-rescore.json',step1)
    print('Step 1 complete: frozen rescoring persisted',flush=True)
    step2=[]
    for rid in REQUESTS:
        text,report=generate(paths,PINS,rid,profile=EXPERIMENTAL)
        repeated=generate(paths,PINS,rid,profile=EXPERIMENTAL)
        if (text,report)!=repeated:raise ValueError('nondeterministic generation: '+rid)
        data=json.loads(report)
        if data['checks']['legality']['status']!='PASS' or data['checks']['template']['status']!='PASS':
            raise ValueError('validation failed: '+rid)
        if REQUESTS[rid]['color_pair'] and data['checks']['color_pair']['status']!='PASS':
            raise ValueError('color-pair check failed: '+rid)
        (output/(rid+'-experimental.txt')).write_bytes(text.encode('utf-8'))
        (output/(rid+'-experimental.json')).write_bytes(report.encode('utf-8'))
        rows,zones=measurements(text,facts,faces)
        _,default=parse(texts[rid,'default'],facts,faces)
        _,previous=parse(texts[rid,'experimental'],facts,faces)
        step2.append(dict(request_id=rid,payoffs=rows,
                          payoff_copies_by_zone={z:sum(r['copies'] for r in rows if r['zone']==z) for z in zones},
                          differences_from_default=differences(default,zones,facts['cards']),
                          differences_from_run25=differences(previous,zones,facts['cards']),
                          deterministic=True,checks=data['checks']))
        print(rid,'Step 2 generated and repeated identically',flush=True)
    write(output/'step2-new-decks.json',step2)
    write(output/'manifest.json',{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(output.iterdir()) if p.is_file()})


if __name__=='__main__':
    evaluate(Path(__file__).resolve().parents[1]/'run27-artifacts')
