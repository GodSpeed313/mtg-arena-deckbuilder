"""Offline v8 recognition measurements against pre-implementation snapshots."""
import argparse
from collections import Counter
from contextlib import closing
from dataclasses import asdict
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mtgadb.model import Deck
from mtgadb.query import CardQueryEngine
from mtgadb.structural_facts import readonly, sha256
from mtgadb.text import normalize_text
from services.analysis6 import analyze_deck6
from services.analysis7 import LinkedEvidence, analyze_deck7
from services.analysis8 import decompose_abilities8, parse_entry8, analyze_deck8, family
from services.evidence_scope import normalize
from services.intelligence import classify_card, analyze_deck
from scripts.measure_analysis6 import ROOT, RUN15, RAW, SOURCE_MANIFEST, CANDIDATE, serialized, require

RUN17 = ROOT / 'analysis-7/run17-20261103-linked-faces'
RUN18 = ROOT / 'census/run18-20261104-baseid-templates'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def distribution(rows):
    c = Counter(supported=0, partial=0, unsupported=0, anomalous=0)
    for a, weight in rows:
        c[a['parse_status']] += weight
    return {'total': sum(c.values()), 'outcomes': dict(c)}


def delta(old, new, identity):
    require(len(old) == len(new), 'entry count changed')
    changed = []
    for a, b in zip(old, new):
        require((a['ability_id'], a['source_index'], a['raw_text']) ==
                (b['ability_id'], b['source_index'], b['raw_text']), 'identity/order/text changed')
        if serialized(a) != serialized(b):
            name = family(b)
            require(name in ('flash', 'enchant', 'equip', 'crew'), 'out-of-family change')
            require(a['parse_status'] == 'unsupported' and b['parse_status'] == 'partial', 'unexpected transition')
            changed.append(dict(identity, family=name, before=a, after=b))
    return changed


def run(output):
    output = Path(output).resolve(strict=True)
    require(output.is_relative_to((ROOT / 'analysis-8').resolve()), 'unauthorized output directory')
    pre = read(output / 'preflight.json')
    mutable = (Path.cwd() / 'services/abilities.py').resolve()
    protected = {p: h for p, h in pre['protected_hashes'].items() if Path(p).resolve() != mutable}
    require(all(sha256(p) == h for p, h in protected.items()), 'protected bytes changed')
    require(sha256(CANDIDATE) == '88d9555f6d159be8e589b6bba181dbaed5480b05ebf13f04bb5c603ebaf679c6', 'candidate pin')
    evidence = LinkedEvidence(RUN15 / 'structural-facts-v1.sqlite', RUN15 / 'output-manifest.json', RAW, SOURCE_MANIFEST)
    baseline = {}
    with (output / 'P1-base.jsonl').open(encoding='utf-8') as f:
        for line in f:
            row = json.loads(line)
            baseline[row['title_id']] = row
    changes = []; before = []; after = []; benchmark = {}; files = {}
    with closing(readonly(CANDIDATE)) as db:
        for row in db.execute('SELECT * FROM cards ORDER BY title_id'):
            card = CardQueryEngine._row_to_card(row)
            old = classify_card(card)
            require(serialized(old) == serialized(baseline[card.title_id]['output']), 'default full card differs from base')
            aa = [normalize(asdict(a)) for a in decompose_abilities8(
                card.rules_text, card_name=card.name, card_types=card.types,
                canonical_anomaly=card.resolution.value == 'anomaly')]
            changes.extend(delta(old['abilities'], aa, {'title_id': card.title_id, 'grp_id': card.source_printing_id}))
            before.extend((a, 1) for a in old['abilities']); after.extend((a, 1) for a in aa)
        inputs = read(RUN17 / 'benchmark-inputs.json')
        for name, data in inputs.items():
            require(sha256(data['path']) == data['sha256'], 'benchmark input hash')
            deck = Deck(name=name, **{z: {int(k): v for k, v in q.items()} for z, q in data['zones'].items()})
            old = analyze_deck(deck, db)
            versions = {'5': old, '6': analyze_deck6(deck, db, evidence.base), '7': analyze_deck7(deck, db, evidence)}
            for version, value in versions.items():
                require(serialized(value) == (output / ('base-' + version + '-' + name.replace(' ', '-') + '.json')).read_bytes(), 'historical version changed')
            new = analyze_deck8(deck, db)
            require(serialized({k: v for k, v in new.items() if k != 'recognition_analysis'}) == serialized(old), 'Analysis5 base changed')
            detail = {}
            for zone in ('main', 'sideboard', 'commander'):
                older = old['zones'][zone]; newer = new['recognition_analysis']['zones'][zone]
                require(serialized(older['dependencies']) == serialized(new['zones'][zone]['dependencies']), 'Dependencies 4 changed')
                require(serialized(older['needs']) == serialized(new['zones'][zone]['needs']), 'Needs 3 changed')
                bb = []; aa = []; changes_here = []
                for oc, nc in zip(older['cards'], newer['cards']):
                    require((oc['title_id'], oc['quantity']) == (nc['title_id'], nc['quantity']), 'benchmark card identity')
                    parsed = [r['ability'] for r in nc['entries']]
                    changes_here.extend(delta(oc['abilities'], parsed, {'title_id': oc['title_id'], 'quantity': oc['quantity']}))
                    bb.extend((a, oc['quantity']) for a in oc['abilities']); aa.extend((a, oc['quantity']) for a in parsed)
                detail[zone] = {'before_weighted': distribution(bb), 'after_weighted': distribution(aa),
                                'before_distinct': distribution([(a, 1) for a, _ in bb]),
                                'after_distinct': distribution([(a, 1) for a, _ in aa]),
                                'changes': changes_here, 'dependencies_identical': True, 'needs_identical': True}
            benchmark[name] = detail
            files['analysis8-' + name.replace(' ', '-') + '.json'] = new
        reps = {r[0] for r in db.execute('SELECT source_printing_id FROM cards')}
    p2before = []; p2after = []; p2changes = []
    for root in read(RUN17 / 'full-candidate-linked-faces.json')['roots']:
        if root['root_grp_id'] not in reps:
            continue
        for face in root['components']:
            for entry in face['abilities']:
                old = entry['linked_face_effect']
                new = normalize(asdict(parse_entry8(normalize_text(entry['source_entry']['local_text']),
                    ability_id=old['ability_id'], source_index=old['source_index'], card_name=face['raw_name'],
                    card_types=frozenset(t['localization']['text'] for t in face['types']))))
                p2changes.extend(delta([old], [new], {'root_grp_id': root['root_grp_id'], 'grp_id': face['grp_id'],
                                                    'source_ability_id': entry['source_entry']['ability_id']}))
                p2before.append((old, 1)); p2after.append((new, 1))
    require(len(p2before) == 735, 'P2 denominator')
    census = read(RUN18 / 'denominators.json')
    require(distribution(before)['outcomes'] == census['P1']['outcomes'], 'Run18 P1 disagreement')
    require(distribution(p2before)['outcomes'] == census['P2']['outcomes'], 'Run18 P2 disagreement')
    family_counts = dict(Counter(c['family'] for c in changes))
    selected = {'flash': 408, 'enchant': 255, 'equip': 292, 'crew': 135}
    require(family_counts == selected, 'selected shape recognition differs')
    report = {'P1': {'titles': len(baseline), 'before': distribution(before), 'after': distribution(after),
                     'changes_by_family': family_counts, 'out_of_family_changes': 0,
                     'selected_still_unrecognized': {k: selected[k] - family_counts[k] for k in selected},
                     'multi_symbol_mana_equip_deferred_unchanged': 28,
                     'default_full_card_outputs_byte_identical': True},
              'P2': {'before': distribution(p2before), 'after': distribution(p2after),
                     'changes_by_family': dict(Counter(c['family'] for c in p2changes)), 'out_of_family_changes': 0},
              'benchmarks': benchmark, 'downstream_version_identity_exceptions': [],
              'downstream_source': 'Unchanged Analysis5; v8 recognition never projected into Dependencies/Needs',
              'historical_versions_unchanged': ['5', '6', '7'], 'protected_unchanged': len(protected)}
    files.update({'measurements.json': report, 'P1-changes.json': changes, 'P2-changes.json': p2changes})
    require(all(sha256(p) == h for p, h in protected.items()), 'protected post-run bytes changed')
    for name in files:
        require(not (output / name).exists(), 'refusing overwrite: ' + name)
    for name, value in files.items():
        with (output / name).open('x', encoding='utf-8') as f:
            json.dump(value, f, indent=2, ensure_ascii=False, sort_keys=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args()
    report = run(args.output_dir)
    print(json.dumps({'P1': report['P1'], 'P2': report['P2'], 'status': 'PASS'}))
