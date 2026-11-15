"""Local Run 25 acceptance evidence. No network, grading, or frozen-output writes."""
import argparse
import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import patch
from collections import Counter
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from services.deck_construction_data import SNAPSHOT,DATABASE,ARENA,PINS
from services.evaluation_requests_v2 import REQUESTS
from services.quality_deck_generator import generate, relationships
from services.spell_cast_boost import EXPERIMENTAL,DEPENDENCY


def sha(b):return hashlib.sha256(b).hexdigest()
def load(p):return json.loads(p.read_text(encoding='utf-8-sig'))
def write(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def zones(text):
    result={'main':{},'sideboard':{}};zone='main'
    for line in text.splitlines():
        if line=='Sideboard':zone='sideboard'
        elif line and line!='Deck':
            n,name=line.split(' ',1);result[zone][name]=int(n)
    return result

def families(report):
    found=set()
    for v in report['contributions'].values():
        for c in v['contributions']:
            if c['rule_identifier']=='coherence.compatible_dependencies':
                found.update(e['rule_id'] for e in c['evidence'])
    return sorted(found)

def checks_pass(request_id, checks):
    """Only registered unrestricted requests may have a non-applicable pair check."""
    expected_pair = 'PASS' if REQUESTS[request_id]['color_pair'] else 'not applicable'
    return (set(checks) == {'legality', 'template', 'color_pair'}
            and checks['legality']['status'] == 'PASS'
            and checks['template']['status'] == 'PASS'
            and checks['color_pair']['status'] == expected_pair)


def evaluate(output,frozen):
    output.mkdir(exist_ok=False)
    baseline=load(frozen)['decks']; rows=[]
    paths=dict(snapshot=SNAPSHOT,database=DATABASE,arena=ARENA)
    for rid in REQUESTS:
        observed={}
        def observe(pool, *, profile="default"):
            graph=relationships(pool,profile=profile)
            observed[profile]=sorted({(e['rule_id'], *e['cards']) for edges in graph.values() for e in edges})
            return graph
        b=baseline[rid];raw=Path(b['path']).read_bytes()
        if sha(raw)!=b['sha256']:raise ValueError('frozen baseline hash mismatch: '+rid)
        with patch("services.quality_deck_generator.relationships",side_effect=observe):
            text,report=generate(paths,PINS,rid)
        default=json.loads(report)
        (output/(rid+'-default.txt')).write_bytes(text.encode('utf-8'))
        (output/(rid+'-default.json')).write_bytes(report.encode('utf-8'))
        match=text.encode('utf-8')==raw
        print(rid,'default byte match',match,flush=True)
        with patch("services.quality_deck_generator.relationships",side_effect=observe):
            exptext,expreport=generate(paths,PINS,rid,profile=EXPERIMENTAL)
        experiment=json.loads(expreport)
        (output/(rid+'-experimental.txt')).write_bytes(exptext.encode('utf-8'))
        (output/(rid+'-experimental.json')).write_bytes(expreport.encode('utf-8'))
        dz,ez=zones(text),zones(exptext)
        differences={z:[dict(card=n,default=dz[z].get(n,0),experimental=ez[z].get(n,0))
            for n in sorted(set(dz[z])|set(ez[z])) if dz[z].get(n,0)!=ez[z].get(n,0)] for z in dz}
        scored=[]
        for tid,v in experiment['contributions'].items():
            for c in v['contributions']:
                if c['rule_identifier']=='coherence.compatible_dependencies' and any(e['rule_id']==DEPENDENCY for e in c['evidence']):
                    scored.append(dict(title_id=tid,name=v['name'],quantity=v['quantity'],
                        contribution=c['numeric_contribution'],note='bounded total across active partners; not additive per-family attribution',
                        boost_pairs=[e['cards'] for e in c['evidence'] if e['rule_id']==DEPENDENCY]))
        row=dict(request_id=rid,expected_sha256=b['sha256'],default_sha256=sha(text.encode('utf-8')),
            experimental_sha256=sha(exptext.encode('utf-8')),default_byte_match=match,differences=differences,
            checks=experiment['checks'],recognition=experiment['recognition'],scored_boost_participants=scored,
            default_scored_families=families(default),experimental_scored_families=families(experiment),
            displaced_scored_families=sorted(set(families(default))-set(families(experiment))),
            pool_pairs_removed=sorted(set(observed['default'])-set(observed[EXPERIMENTAL])),
            pool_pairs_added=sorted(set(observed[EXPERIMENTAL])-set(observed['default'])),
            default_pool_family_counts=dict(Counter(e[0] for e in observed['default'])),
            winning_trials=experiment['winning_trials'],default_score=default['selection_score'],experimental_score=experiment['selection_score'])
        rows.append(row);write(output/'comparison.json',rows)
        print(rid,'changed',bool(any(differences.values())),'boost pairs selected',len(experiment['recognition']['selected_main_boost_pairs']),flush=True)
    write(output/'gates.json',dict(B='PASS' if all(r['default_byte_match'] for r in rows) else 'FAIL',
        C='PASS' if all(checks_pass(r['request_id'],r['checks']) for r in rows) else 'FAIL'))
    write(output/'manifest.json',{p.name:sha(p.read_bytes()) for p in output.iterdir() if p.is_file()})

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--frozen',type=Path,required=True)
    a=p.parse_args();evaluate(a.output,a.frozen)
