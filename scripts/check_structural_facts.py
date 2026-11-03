"""Offline real-source acceptance; missing external evidence is an explicit skip.

Run from any directory with --snapshot. --require-inputs turns a portable skip
into failure. This script never writes databases or evidence.
"""
from collections import Counter
from contextlib import closing
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mtgadb.structural_facts import readonly, sha256, validate
from mtgadb.model import Deck
from services.intelligence import analyze_deck

SOURCE = Path('C:/MTG-Source-Preservation/2026-10-24-r1/arena/Raw_CardDatabase_c344d0dccdfb898a5e3fd4101eedbd48.mtga')
CANDIDATE = Path('C:/MTG-Source-Preservation/candidates/2026-10-24-r1/canonical-candidate.db')
EVIDENCE = Path('C:/MTG-Source-Preservation/pilots/run14/assessment-20261102T171540Z-f81c04dd')
PINS = {
    'benchmark-inputs.json': '84d2570031e8522b8cbd7455271472f752889621a1d0283d8c9580e2ce44d3bc',
    'structural-comparison.json': 'c0ed055efb2ea236290a10708058aa58b229f0dfa3fd9f01747934cf3bdc6f05',
    'arena-evidence.json': '58683ba4c9ac363901920d82b40b3c92a971fd11658cc2e3141816e733fe9f89',
}
SOURCE_HASH = '1be0105411ad989ca60e233f1341eaaa9276e3a679c89f1ef6f07e07ede10d25'
CANDIDATE_HASH = '88d9555f6d159be8e589b6bba181dbaed5480b05ebf13f04bb5c603ebaf679c6'
LINKS = {95869:(95870,'transform'),97421:(97422,'transform'),96121:(96122,'transform'),
         95997:(95998,'transform'),97463:(97464,'transform'),98378:(98379,'transform'),
         98520:(98521,'transform'),95598:(95599,'omen'),95536:(95537,'omen'),
         95701:(95702,'omen'),95679:(95680,'omen'),95644:(95645,'omen')}
RECOGNITION = {'Earth Danger':[25,2,55], 'Dragon Love':[19,20,54], 'Crimson Forest':[15,5,78]}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def check(snapshot, source=SOURCE, candidate=CANDIDATE, evidence=EVIDENCE):
    paths = [source, candidate] + [evidence/name for name in PINS]
    before = {str(p):sha256(p) for p in paths}
    require(before[str(source)] == SOURCE_HASH, 'raw source hash mismatch')
    require(before[str(candidate)] == CANDIDATE_HASH, 'candidate hash mismatch')
    for name, expected in PINS.items():
        require(before[str(evidence/name)] == expected, 'Run 14 evidence hash mismatch: '+name)
    inputs = json.loads((evidence/'benchmark-inputs.json').read_text(encoding='utf-8'))
    historical = json.loads((evidence/'arena-evidence.json').read_text(encoding='utf-8'))
    expected_metrics = json.loads((evidence/'structural-comparison.json').read_text(encoding='utf-8'))['metrics']
    for deck in inputs.values():
        require(sha256(deck['path']) == deck['sha256'], 'benchmark input hash mismatch')
        before[deck['path']] = deck['sha256']
    meta = validate(snapshot)
    require(meta['source_sha256'] == SOURCE_HASH, 'snapshot source provenance mismatch')
    roots = {int(gid) for d in inputs.values() for zone in d['zones'].values() for gid in zone}
    require(len(roots) == 92, '92 printing denominator mismatch')
    quantities = Counter()
    for d in inputs.values():
        for zone in d['zones'].values():
            quantities.update({int(k):v for k,v in zone.items()})
    require(sum(quantities.values()) == 180, '180 deck-copy denominator mismatch')
    with closing(readonly(snapshot)) as facts, closing(readonly(source)) as raw, closing(readonly(candidate)) as canonical:
        components = {r[0]:json.loads(r[1]) for r in facts.execute('SELECT * FROM components')}
        require(roots <= components.keys(), 'missing root printing identities')
        require(len({components[i]['source']['TitleId'] for i in roots}) == 89, '89 title denominator mismatch')
        links = [dict(r) for r in facts.execute('SELECT * FROM links') if r['grp_id'] in roots]
        require({r['grp_id']:(r['target_id'],r['kind']) for r in links} == LINKS and len(links)==12,
                'benchmark link set differs')
        require(all(r['status']=='recovered' for r in links), 'unresolved benchmark relationship')
        faces = roots | {v[0] for v in LINKS.values()}
        entries = [dict(r) for r in facts.execute('SELECT * FROM entries ORDER BY grp_id,position') if r['grp_id'] in faces]
        require(all(r['status']=='recovered' for r in entries), 'unresolved benchmark ability')
        # Independent raw row and preserved Run 14 comparisons verify exact source
        # binding, ordering, local text, generic text and variant fields.
        for gid in sorted(faces):
            row = dict(raw.execute('SELECT * FROM Cards WHERE GrpId=?',(gid,)).fetchone())
            require(components[gid]['source']==row==historical['cards'][str(gid)]['raw'], f'card source mismatch {gid}')
            expected = [e for e in historical['cards'][str(gid)]['abilities'] if e['field']=='AbilityIds']
            actual = [e for e in entries if e['grp_id']==gid]
            require(len(expected)==len(actual), f'ability cardinality mismatch {gid}')
            for want, got in zip(expected,actual):
                f=json.loads(got['facts_json'])
                require((got['position'],got['ability_id'],f['card_local']['id']) ==
                        (want['index'],want['ability_id'],want['card_local_text_id']), f'entry binding mismatch {gid}')
                local = next(x['Loc'] for x in want['card_local_text'] if x['Formatted']==1)
                require(f['card_local']['text']==local, f'local text mismatch {gid}')
                ability=dict(raw.execute('SELECT * FROM Abilities WHERE Id=?',(got['ability_id'],)).fetchone())
                require(f['ability']==ability, f'raw ability mismatch {gid}')
                generic=raw.execute('SELECT Loc FROM Localizations_enUS WHERE LocId=? AND Formatted=1',(ability['TextId'],)).fetchone()[0]
                require(f['generic']['id']==ability['TextId'] and f['generic']['text']==generic, f'generic mismatch {gid}')
        aggregate=expected_metrics['aggregate_unique_roots_copy_weighted_across_decks']
        for field, metric, expected_count in [('loyalty_cost','loyalty_cost',11),('chapter_index','chapter_number',40)]:
            found={(r['grp_id'],r['ability_id'],r[field]) for r in entries if r[field] is not None}
            wanted={(r['face_id'],r['ability_id'],r['value']) for r in aggregate[metric]['records']}
            require(found==wanted and len(found)==expected_count,f'{field} exact expected set mismatch')
        for gid in (97422,97464):
            c=components[gid]
            require(c['source']['Colors']==c['source']['IndicatorColors']=='' and c['gameplay_color']=='unresolved',
                    f'empty source colors changed {gid}')
        oko=next(json.loads(e['facts_json']) for e in entries if e['grp_id']==98378 and e['ability_id']==193884)
        require(oko['card_local']['id']==1105438 and oko['generic']['id']==1078715 and
                oko['card_local']['text']!=oko['generic']['text'], 'Oko local reminder lost')
        for gid in (95870,95998,96122):
            require(any('front face up' in json.loads(e['facts_json'])['card_local']['text'].lower()
                        for e in entries if e['grp_id']==gid), f'front-face-up wording lost {gid}')
        per_deck={}
        weighted=Counter()
        for name,d in inputs.items():
            # Frozen Run 14 zones already use Arena printing IDs. No name match,
            # import fallback, representative printing or added face quantities.
            zones={z:{int(k):v for k,v in q.items()} for z,q in d['zones'].items()}
            deck=Deck(name=name,**zones)
            before_zones=json.dumps(zones,sort_keys=True)
            analysis=analyze_deck(deck,canonical)
            require([analysis['analysis_version'],analysis['dependency_model_version'],analysis['needs_model_version']]==['5','4','3'],
                    'evidence version boundary changed')
            coverage=analysis['zones']['main']['rules_text_coverage']
            recognition=[coverage[k+'_ability_count'] for k in ('supported','partial','unsupported')]
            require(recognition==RECOGNITION[name], 'recognition regression: '+name)
            counts=Counter()
            for zone in zones.values():
                for gid,quantity in zone.items():
                    face_ids={gid}
                    if gid in LINKS:
                        counts['links']+=quantity
                        face_ids.add(LINKS[gid][0])
                    for e in entries:
                        if e['grp_id'] in face_ids:
                            counts['loyalty']+=quantity*(e['loyalty_cost'] is not None)
                            counts['chapters']+=quantity*(e['chapter_index'] is not None)
            require(counts['links']==expected_metrics[name]['links']['copy_weighted_links'], 'weighted links mismatch')
            require(counts['loyalty']==expected_metrics[name]['loyalty_cost']['copy_weighted_fact_instances'], 'weighted loyalty mismatch')
            require(counts['chapters']==expected_metrics[name]['chapter_number']['copy_weighted_fact_instances'], 'weighted chapters mismatch')
            require(json.dumps({z:getattr(deck,z) for z in zones},sort_keys=True)==before_zones, 'printing zones/quantities changed')
            per_deck[name]={'preserved_zones':zones,'recognition':recognition,'copy_weighted':dict(counts),
                            'copy_slots':sum(sum(q.values()) for q in zones.values())}
            weighted.update(counts)
        require(dict(weighted)=={'links':14,'loyalty':14,'chapters':53},'aggregate weighted mismatch')
        source_counts={t:raw.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
                       for t in meta['measurements']['source_rows_scanned']}
        require(source_counts==meta['measurements']['source_rows_scanned'], 'full-source scan counts mismatch')
        require(len(components)==source_counts['Cards'], 'full-source components incomplete')
    after={p:sha256(p) for p in before}
    require(before==after,'protected input changed during acceptance')
    measures={k:{'expected':v,'recovered':v,'missing':0,'conflicting':0,'unresolved':0,'denominator':v}
              for k,v in {'printing_ids':92,'titles':89,'deck_copies':180,'links':12,'loyalty':11,'chapters':40,
                          'weighted_links':14,'weighted_loyalty':14,'weighted_chapters':53}.items()}
    return {'status':'PASS','recognition_gain':0,'ED_1_to_ED_4':'NON_SCORED',
            'measurements':measures,'per_deck':per_deck,'protected_before':before,'protected_after':after,
            'color_interpretation_unresolved_faces':[97422,97464],
            'text_and_binding_checks':'all benchmark face entries compared exactly with source and frozen Run 14 evidence',
            'full_source_counts':source_counts,'snapshot_payload_sha256':meta['payload_sha256']}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot',type=Path,required=True)
    parser.add_argument('--source',type=Path,default=SOURCE)
    parser.add_argument('--candidate',type=Path,default=CANDIDATE)
    parser.add_argument('--evidence',type=Path,default=EVIDENCE)
    parser.add_argument('--require-inputs',action='store_true')
    args=parser.parse_args(argv)
    unavailable=[str(p) for p in (args.source,args.candidate,args.evidence) if not p.exists()]
    if unavailable:
        print(json.dumps({'status':'FAIL' if args.require_inputs else 'SKIP','unavailable':unavailable}))
        return 1 if args.require_inputs else 0
    try:
        result=check(args.snapshot,args.source,args.candidate,args.evidence)
    except (ValueError,OSError,KeyError,TypeError) as error:
        print(json.dumps({'status':'FAIL','diagnostic':str(error)}))
        return 1
    print(json.dumps(result,indent=2,sort_keys=True))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
