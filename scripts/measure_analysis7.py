"""Offline Analysis 7 coverage, preservation, and uninterpreted layout samples."""
import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mtgadb.model import Deck
from mtgadb.structural_facts import readonly,sha256
from services.analysis6 import analyze_deck6
from services.analysis7 import LinkedEvidence,analyze_deck7,population
from scripts.measure_analysis6 import ROOT,RUN15,RUN14,CANDIDATE,RAW,SOURCE_MANIFEST,serialized,require

RUN16=ROOT/'analysis-6/run16-20261103-structural'
TARGETS={(98379,a) for a in (193887,193888,193890)} | {(95870,a) for a in (189068,189069,189070)} | {
    (95998,a) for a in (189265,189266,189267)} | {(96122,a) for a in (189456,189457,189458,189459)}


def compare(prior,current,deck):
    require(serialized(prior)==serialized({k:v for k,v in current.items() if k!='linked_face_analysis'}),'prior analysis changed')
    for z in ('main','sideboard','commander'):
        layer=current['linked_face_analysis']['zones'][z]
        require(sum(r['root_quantity'] for r in layer['roots'])==sum(getattr(deck,z).values()),'root quantities changed')
        for root in layer['roots']:
            for face in root['components']:
                require((face['relationship'],face['root_linked_face_type'],face['face_linked_face_type']) in
                        (('transform',2,1),('omen',18,17)),'unreviewed signature attached')
                for ability in face['abilities']:
                    require(ability['source_entry']['grp_id']==face['grp_id'],'cross-face entry')
                    require('binding' not in ability,'source-native mislabeled binding')
                    require(bool(ability['structures'])==(ability['structural_evidence']=='source-native'),'structural label')


def run(output):
    started=time.perf_counter();output=Path(output).resolve(strict=True)
    require(output.is_relative_to((ROOT/'analysis-7').resolve()),'output outside authorized root')
    pre=json.loads((output/'preflight.json').read_text(encoding='utf-8'))
    require(all(sha256(p)==h for p,h in pre['protected_hashes'].items()),'protected input changed')
    require(sha256(CANDIDATE)=='88d9555f6d159be8e589b6bba181dbaed5480b05ebf13f04bb5c603ebaf679c6','candidate pin')
    evidence=LinkedEvidence(RUN15/'structural-facts-v1.sqlite',RUN15/'output-manifest.json',RAW,SOURCE_MANIFEST)
    inputs=json.loads((output/'benchmark-inputs.json').read_text())
    report={'evidence':evidence.base.proof,'benchmarks':{},'contract_violations':0,'target_outcomes':[]}
    outputs={};unique_roots={};all_roots=[];recovered={};existing=set()
    with closing(readonly(CANDIDATE)) as con:
        for name,data in inputs.items():
            require(sha256(data['path'])==data['sha256'],'benchmark bytes')
            deck=Deck(name=name,**{z:{int(k):v for k,v in q.items()} for z,q in data['zones'].items()})
            prior=analyze_deck6(deck,con,evidence.base);current=analyze_deck7(deck,con,evidence);compare(prior,current,deck)
            require(serialized(prior)==(output/('analysis6-base-'+name.replace(' ','-')+'.json')).read_bytes(),'base Analysis 6 bytes changed')
            layer=current['linked_face_analysis']['zones']['main'];roots=layer['roots']
            for root in roots:
                unique_roots[root['root_grp_id']]=root;all_roots.append(root)
                for face in root['components']:
                    for entry in face['abilities']:
                        key=(face['grp_id'],entry['source_entry']['ability_id'])
                        if entry['structures']:recovered[key]=entry
            for card in prior['structural_analysis']['zones']['main']['cards']:
                for entry in card['entries']:
                    if entry['binding']=='bound':existing.add((entry['entry']['grp_id'],entry['entry']['ability_id']))
            report['benchmarks'][name]={'before_linked_faces_analyzed':0,'after':layer['distinct'],
                'copy_weighted':layer['copy_weighted'],'skipped':layer['skipped_links'],
                'analysis5_effects_unchanged':prior['zones']['main']['rules_text_coverage'],
                'analysis6_binding_unchanged':prior['structural_analysis']['zones']['main']['structural_axis'],
                'prior_bytes_identical':True,'contract_violations':0}
            outputs['analysis7-'+name.replace(' ','-')+'.json']=current
        report['aggregate_benchmarks']={'distinct':population(list(unique_roots.values())),
                                       'copy_weighted':population(all_roots,True),
                                       'existing_analysis6_bound_distinct':len(existing)}
        require(len(existing)==38,'Analysis 6 visible-face binding changed')
        require(report['aggregate_benchmarks']['distinct']['linked_faces']==12,'benchmark face denominator')
        require(report['aggregate_benchmarks']['distinct']['linked_faces_by_kind']=={'omen':5,'transform':7},'benchmark kinds')
        for gid,aid in sorted(TARGETS):
            entry=recovered.get((gid,aid))
            report['target_outcomes'].append({'grp_id':gid,'ability_id':aid,'status':'recovered' if entry else 'inaccessible',
                'structural_evidence':'source-native' if entry else None,'structures':entry['structures'] if entry else [],
                'reason':None if entry else 'no_reviewed_visible_component'})
        require(set(recovered)==TARGETS,'13-entry source-native target set differs')
        # Every candidate printing with outbound source links, not only title
        # representatives. Report canonical-title and printing counts separately.
        linked_ids=set(evidence.linked_roots())
        candidates=[dict(r) for r in con.execute('SELECT arena_id,title_id FROM printings ORDER BY arena_id') if r['arena_id'] in linked_ids]
        representative_ids={r[0] for r in con.execute('SELECT source_printing_id FROM cards')}
        full_roots=[];skipped=[];representative_roots=[]
        for row in candidates:
            deck=Deck(main={row['arena_id']:1})
            prior=analyze_deck6(deck,con,evidence.base);current=analyze_deck7(deck,con,evidence);compare(prior,current,deck)
            layer=current['linked_face_analysis']['zones']['main'];full_roots.extend(layer['roots']);skipped.extend(layer['skipped_links'])
            if row['arena_id'] in representative_ids:representative_roots.extend(layer['roots'])
        attached=sum(len(r['components']) for r in full_roots)
        report['full_candidate']={'scope':'all candidate printing GrpIds with outbound source links; includes nonrepresentative printings',
            'candidate_printing_roots_with_links':len(candidates),'distinct_canonical_titles':len({r['title_id'] for r in candidates}),
            'roots_attached':sum(bool(r['components']) for r in full_roots),'roots_without_attachment':sum(not r['components'] for r in full_roots),
            'outbound_link_slot_denominator':attached+len(skipped),'attached_component_slots':attached,
            'skipped_link_slots':len(skipped),'skipped_by_reason':dict(Counter(s['reason'] for s in skipped)),
            'population':population(full_roots),'representative_only_population':population(representative_roots),
            'representative_roots_with_links':len(representative_roots),'contract_violations':0}
        outputs['full-candidate-linked-faces.json']={'roots':full_roots,'skipped':skipped}
        toshiro=analyze_deck7(Deck(main={79533:1}),con,evidence)
        unchanged=toshiro['structural_analysis']['zones']['main']['cards'][0]['entries']
        require([(r['entry']['ability_id'],r['reason']) for r in unchanged]==
                [(149695,'sequence_length_mismatch'),(147902,'sequence_length_mismatch'),(147760,'sequence_length_mismatch')],'Toshiro exception changed')
        report['toshiro_exception']={'grp_id':79533,'unchanged_unbound_entries':unchanged,'out_of_scope_for_repair':True}
    # Appendix is literal source inventory only, never an inferred code mapping.
    samples={}
    with closing(readonly(RUN15/'structural-facts-v1.sqlite')) as db:
        cards={r[0]:json.loads(r[1]) for r in db.execute('SELECT * FROM components')}
        links={}
        for r in db.execute('SELECT * FROM links ORDER BY grp_id,position'):links.setdefault(r['grp_id'],[]).append(dict(r))
        for code in list(range(3,17))+[19,20]:
            examples=[]
            for gid,card in sorted(cards.items()):
                if card['source']['LinkedFaceType']!=code:continue
                outgoing=links.get(gid,[])
                for link in outgoing:
                    target=cards.get(link['target_id'])
                    examples.append({'source_grp_id':gid,'source_name':card['name'],'source_types':card['types'],
                        'target_grp_id':link['target_id'],'target_name':target['name'] if target else None,
                        'target_types':target['types'] if target else None,
                        'directed_shape':'one-to-many' if len(outgoing)>1 else 'one-to-one',
                        'outgoing_count':len(outgoing)})
                    if len(examples)==5:break
                if len(examples)==5:break
            samples[str(code)]=examples
        report['source_exclusions']={'flag_conflicting_directed_slots':db.execute("SELECT count(*) FROM links WHERE status='conflicting_layout'").fetchone()[0],
                                     'unknown_codes_sampled':list(samples)}
    outputs['unknown-code-samples.json']=samples
    require(all(sha256(p)==h for p,h in pre['protected_hashes'].items()),'post-measurement protected input changed')
    report['protected_hashes_unchanged']=len(pre['protected_hashes']);report['runtime_seconds']=time.perf_counter()-started
    outputs['measurements.json']=report
    for name in outputs:require(not (output/name).exists(),'refusing existing '+name)
    for name,value in outputs.items():
        with (output/name).open('x',encoding='utf-8') as f:json.dump(value,f,indent=2,sort_keys=True,ensure_ascii=False)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args()
    try:result=run(args.output_dir)
    except Exception as error:
        print(json.dumps({'status':'FAIL','diagnostic':str(error)}));return 1
    print(json.dumps({'status':'PASS','benchmarks':result['aggregate_benchmarks'],'full_candidate':result['full_candidate'],
                      'runtime_seconds':result['runtime_seconds']}));return 0


if __name__=='__main__':
    raise SystemExit(main())
