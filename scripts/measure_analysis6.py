"""Offline Run 16 measurements; writes only to an explicit external directory."""
import argparse
from collections import Counter
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mtgadb.model import Deck
from mtgadb.structural_evidence import StructuralEvidence
from mtgadb.structural_facts import readonly, sha256
from services.analysis6 import analyze_deck6, structural_counts
from services.intelligence import analyze_deck

ROOT = Path('C:/MTG-Source-Preservation')
RUN15 = ROOT/'structural-facts/run15-20261102T174626Z'
RUN14 = ROOT/'pilots/run14/assessment-20261102T171540Z-f81c04dd'
CANDIDATE = ROOT/'candidates/2026-10-24-r1/canonical-candidate.db'
RAW = ROOT/'2026-10-24-r1/arena/Raw_CardDatabase_c344d0dccdfb898a5e3fd4101eedbd48.mtga'
SOURCE_MANIFEST = ROOT/'2026-10-24-r1/manifest.json'
EXPECTED_EFFECTS = {'Earth Danger':[25,2,55],'Dragon Love':[19,20,54],'Crimson Forest':[15,5,78]}


def serialized(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode('utf-8')


def require(condition,message):
    if not condition:
        raise ValueError(message)


def validate_addition(base, added):
    old={k:v for k,v in added.items() if k!='structural_analysis'}
    require(serialized(old)==serialized(base),'changed Analysis 5 field')
    violations=[]
    for zone_name,zone in added['structural_analysis']['zones'].items():
        rows={r['title_id']:r for r in base['zones'][zone_name]['cards']}
        for card in zone['cards']:
            for r in card['entries']:
                entry=r['entry']
                require(entry['loyalty_cost'] is not None or entry['chapter_index'] is not None,'ineligible structure')
                if r['binding']=='bound':
                    require(r['evidence_path']=='path2','unestablished binding path')
                    original=rows[card['title_id']]['abilities'][entry['position']]
                    require(serialized(original)==serialized(r['effect_classification']),'changed effect classification')
                    require(entry['grp_id']==card['grp_id'],'cross-printing binding')
                else:
                    require(not r['structures'] and r['effect_classification'] is None,'credit without binding')
    return violations


def run(output):
    start=time.perf_counter()
    output=Path(output).resolve(strict=True)
    require(output.is_relative_to((ROOT/'analysis-6').resolve()),'output must be under authorized analysis-6 root')
    artifact=RUN15/'structural-facts-v1.sqlite'
    protected=[artifact,RUN15/'output-manifest.json',RAW,SOURCE_MANIFEST,CANDIDATE,Path('current.db')]
    before={str(p.resolve()):sha256(p) for p in protected}
    require(sha256(CANDIDATE)=='88d9555f6d159be8e589b6bba181dbaed5480b05ebf13f04bb5c603ebaf679c6','candidate hash')
    require(sha256('current.db')=='9f8761632230a93f58aa47d77d120cda2c5be76354d787045202ed0e01e7f3ef','current hash')
    evidence=StructuralEvidence(artifact,RUN15/'output-manifest.json',RAW,SOURCE_MANIFEST)
    require(sha256(RUN14/'benchmark-inputs.json')=='84d2570031e8522b8cbd7455271472f752889621a1d0283d8c9580e2ce44d3bc','benchmark identity evidence hash')
    require(sha256(RUN14/'structural-comparison.json')=='c0ed055efb2ea236290a10708058aa58b229f0dfa3fd9f01747934cf3bdc6f05','historical denominator hash')
    inputs=json.loads((RUN14/'benchmark-inputs.json').read_text(encoding='utf-8'))
    historical=json.loads((RUN14/'structural-comparison.json').read_text(encoding='utf-8'))['metrics']
    report={'evidence':evidence.proof,'benchmarks':{},'named_cards':{},'contract_violations':0,'diagnostics':'NOT RUN'}
    artifacts={}
    all_bound=set();all_expected=set();all_gaps={};weighted=Counter()
    with closing(readonly(CANDIDATE)) as con:
        for name,data in inputs.items():
            require(sha256(data['path'])==data['sha256'],'benchmark input bytes changed')
            deck=Deck(name=name,**{z:{int(gid):q for gid,q in zone.items()} for z,zone in data['zones'].items()})
            base=analyze_deck(deck,con);added=analyze_deck6(deck,con,evidence)
            validate_addition(base,added)
            baseline_path=output/('analysis5-base-'+name.replace(' ','-')+'.json')
            require(baseline_path.read_bytes()==serialized(base),'Analysis 5 differs from pre-implementation base output')
            require(all(base['zones'][z][k]==added['zones'][z][k] for z in base['zones'] for k in ('dependencies','needs')),
                    'Dependencies/Needs changed')
            coverage=added['zones']['main']['rules_text_coverage']
            effects=[coverage[k+'_ability_count'] for k in ('supported','partial','unsupported')]
            require(effects==EXPECTED_EFFECTS[name],'benchmark effect regression')
            cards=added['structural_analysis']['zones']['main']['cards']
            actual={(r['entry']['grp_id'],r['entry']['ability_id']):r for card in cards for r in card['entries']}
            categories={};gaps=[]
            for family,metric in [('loyalty','loyalty_cost'),('chapters','chapter_number')]:
                expected=historical[name][metric]['records']
                counts=Counter(eligible=0,bound=0,unbound=0,ambiguous=0,kind_corrections=0)
                copies=Counter(counts);reasons=Counter();copy_reasons=Counter()
                for item in expected:
                    key=(item['face_id'],item['ability_id']);all_expected.add((family,*key))
                    counts['eligible']+=1;copies['eligible']+=item['quantity']
                    row=actual.get(key)
                    if row is None:
                        state='unbound';reason='face_not_in_deck_linked_components_excluded'
                        gaps.append(dict(item,family=family,reason=reason))
                        all_gaps[(family,*key)]=reason
                    else:
                        state=row['binding'];reason=row['reason']
                    counts[state]+=1;copies[state]+=item['quantity']
                    if state=='bound':
                        all_bound.add((family,*key))
                        counts['kind_corrections']+=bool(row['kind_corrections'])
                        copies['kind_corrections']+=item['quantity']*bool(row['kind_corrections'])
                    else:
                        reasons[reason]+=1;copy_reasons[reason]+=item['quantity']
                    weighted[family+'_eligible']+=item['quantity']
                    weighted[family+'_'+state]+=item['quantity']
                categories[family]={'distinct':dict(counts,denominator=counts['eligible']),
                                    'copy_weighted':dict(copies,denominator=copies['eligible']),
                                    'unbound_by_reason':dict(reasons),'copy_weighted_unbound_by_reason':dict(copy_reasons)}
            distinct_effects=Counter(a['parse_status'] for r in base['zones']['main']['cards'] for a in r['abilities'])
            report['benchmarks'][name]={'structural':categories,'historical_unbound_entries':gaps,
                'effect_axis_copy_weighted':dict(zip(('supported','partial','unsupported'),effects),denominator=coverage['ability_count']),
                'effect_axis_distinct':dict(distinct_effects,denominator=sum(distinct_effects.values())),
                'base_bytes_identical':True,'dependencies4_unchanged':True,'needs3_unchanged':True,
                'contract_violations':0,'text_source_differences':'NOT RUN',
                'analysis5_sha256':hashlib.sha256(serialized(base)).hexdigest()}
            artifacts['analysis6-'+name.replace(' ','-')+'.json']=added
        for gid in (98378,98379,75245,93947,96072):
            deck=Deck(name='named exact printing',main={gid:1})
            base=analyze_deck(deck,con);added=analyze_deck6(deck,con,evidence);validate_addition(base,added)
            card=added['structural_analysis']['zones']['main']['cards'][0]
            structure={r['entry']['position']:r for r in card['entries']}
            rows=base['zones']['main']['cards']
            abilities=rows[0]['abilities'] if rows else []
            report['named_cards'][str(gid)]={'name':rows[0]['name'] if rows else 'unavailable in canonical candidate',
                'canonical_status':'available' if rows else 'missing_canonical_card',
                'binding':card['binding'],'reason':card['reason'],
                'abilities':[{'analysis5':a,'analysis6_effect':deepcopy(a),'structural':structure.get(i)} for i,a in enumerate(abilities)],
                'unbound_source_entries':card['entries'] if not rows else []}
        eligible=set(evidence.structural_printings())
        representatives=[dict(r) for r in con.execute('SELECT title_id,source_printing_id,name FROM cards ORDER BY title_id')
                         if r['source_printing_id'] in eligible]
        full_cards=[];full_rows=[]
        for row in representatives:
            gid=row['source_printing_id'];deck=Deck(main={gid:1})
            base=analyze_deck(deck,con);added=analyze_deck6(deck,con,evidence);validate_addition(base,added)
            card=added['structural_analysis']['zones']['main']['cards'][0]
            full_cards.append(card)
            full_rows.append(dict(row,binding=card['binding'],reason=card['reason'],entries=card['entries']))
        report['full_candidate']={'representative_cards':len(representatives),
            'distinct_entries':structural_counts(full_cards),'contract_violations':0,
            'cards_by_binding':dict(Counter(c['binding'] for c in full_cards)),
            'scope':'exact representative printing only; no linked-face traversal; one copy per representative for analysis, reported as distinct entries'}
        artifacts['full-candidate-entries.json']=full_rows
    report['aggregate_benchmarks']={family:{'eligible':sum(k[0]==family for k in all_expected),
        'bound':sum(k[0]==family for k in all_bound),'unbound':sum(k[0]==family for k in all_expected-all_bound),
        'copy_weighted':{k[len(family)+1:]:v for k,v in weighted.items() if k.startswith(family+'_')}}
        for family in ('loyalty','chapters')}
    require(report['aggregate_benchmarks']['loyalty']['eligible']==11 and report['aggregate_benchmarks']['chapters']['eligible']==40,'historical distinct denominators')
    require(weighted['loyalty_eligible']==14 and weighted['chapters_eligible']==53,'historical weighted denominators')
    after={p:sha256(p) for p in before}
    require(before==after,'protected inputs changed')
    report.update(protected_before=before,protected_after=after,runtime_seconds=time.perf_counter()-start)
    artifacts['measurements.json']=report
    for name in artifacts:require(not (output/name).exists(),'refusing existing output '+name)
    for name,value in artifacts.items():
        with (output/name).open('x',encoding='utf-8') as stream:json.dump(value,stream,indent=2,sort_keys=True,ensure_ascii=False)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args()
    try:result=run(args.output_dir)
    except Exception as error:
        print(json.dumps({'status':'FAIL','diagnostic':str(error)}));return 1
    print(json.dumps({'status':'PASS','aggregate_benchmarks':result['aggregate_benchmarks'],
                      'full_candidate':result['full_candidate'],'runtime_seconds':result['runtime_seconds']}));return 0


if __name__=='__main__':
    raise SystemExit(main())
