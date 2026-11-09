"""Versioned, origin-masked human evaluation. Signals are never quality verdicts."""
from collections import Counter, defaultdict
from datetime import date
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re

from services.abilities import decompose_abilities
from services.deck_construction_data import ARENA, DATABASE, SNAPSHOT, PINS, read_facts

VERSIONS = dict(evaluation_set_version=1, rubric_version=1, protocol_version=1, flag_version=1)
REQUESTS = {'std-bo1': {'format': 'Standard', 'queue': 'BO1'},
            'std-bo3': {'format': 'Standard', 'queue': 'BO3'}}
ANCHORS = {
    'Card quality': {'0': 'Selections offer no credible utility for the stated or inferred plan.',
                     '3': 'Most selections serve the plan, with some questionable choices.',
                     '5': 'Selections consistently serve the plan with convincing individual utility.'},
    'Synergy': {'0': 'Cards obstruct one another or lack a discernible shared plan.',
                '3': 'There is a coherent plan with some mutually supporting cards.',
                '5': 'Cards consistently reinforce one another and the plan.'},
    'Interaction': {'0': 'The deck has no credible response to relevant opposing threats.',
                    '3': 'The deck can answer several relevant threats but has notable gaps.',
                    '5': 'The deck has a convincing range of timely responses for its plan.'},
    'Mana efficiency': {'0': 'Curve, acceleration, and mana sources prevent reliable execution.',
                        '3': 'Mana supports the plan with some inefficiencies or risks.',
                        '5': 'Curve, acceleration, and mana sources convincingly support timely execution.'},
    'Playability': {'0': 'I would not take this deck into a match in its assessed format.',
                    '3': 'I would try this deck with reservations or targeted changes.',
                    '5': 'I would willingly take this deck into a match as presented.'},
}
RATIONALES = {
    'curve': 'Main-deck nonland mana values and highest spell versus lands describe curve context; alternate costs, a slower plan or additional mana sources may make high costs acceptable.',
    'acceleration': 'Nonland text matching Add followed by mana symbols, any color, or any type is a mana-production mention lower bound, not executable recognition; low top-end may be acceptable for repeated abilities, fixing, or multiple spells per turn.',
    'composition': 'Main-deck creature, other-nonland and land counts describe composition; few creatures may be acceptable in a control, spell, or token plan.',
    'interaction': 'Grammar-5 recognized targeted destroy/exile effects are a lower bound limited by grammar coverage; few recognized effects may be acceptable with unrecognized answers or a proactive plan.',
    'understanding': 'Copy-weighted nonland recognition measures parser coverage, not card quality; unsupported cards may be essential to a coherent plan.',
    'split_printings': 'Multiple printings of one title are cosmetic; different artwork or owned printings can fully explain the split.',
    'mana_balance': 'Main-deck colored symbol mentions versus verified basic sources omit nonbasic production, alternate costs and sequencing; those factors may make an uneven basic split acceptable.',
}
POLICY = dict(
    **VERSIONS, requests=REQUESTS, rubric=ANCHORS, flag_definitions=RATIONALES,
    baseline_inputs='Standard and BO1/BO3 only; output path is storage configuration; forced colors unsupported.',
    masking='Sort SHA-256 of run22-origin-mask-v1: plus deck ID, tie by deck ID; assign Deck A onward. Origin-masked, not guaranteed blind: familiarity bias remains.',
    comparison='Pair by request ID and identical evaluation-set, rubric, protocol and flag versions and frozen policy hash; references never paired. Require both complete request sets. Human totals and flags stay separate.',
    anti_overfitting='Judge all requests and all five dimensions. Card-specific exclusions do not count as improved selection and must be disclosed by future generators.',
    freeze='Any change requires a new version and baseline regrading before comparison; never change scoring after seeing candidate results.',
    scoring='Human integers 0-5 plus nonempty why for each dimension; sum five dimensions to 25. No automatic quality scores. Synergy is human-only.',
)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n'


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_new(path, value):
    with Path(path).open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(canonical(value))


def pinned_read(entry):
    if file_hash(entry['source_path']) != entry['sha256']:
        raise ValueError('deck hash mismatch: ' + entry['id'])
    return Path(entry['source_path']).read_text(encoding='utf-8-sig')


def resolve(text, facts, encoding='arena_text'):
    """Strict identity resolution, no fuzzy/name-only matching or dropped zones."""
    rows = []
    if encoding == 'arena_json':
        data = json.loads(text)
        for zone, values in data.items():
            if zone not in ('main', 'sideboard', 'commander', 'companions'):
                raise ValueError('unknown zone')
            for row in values or []:
                rows.append((zone, row['cardId'], row['quantity']))
    else:
        index = defaultdict(list)
        for aid, p in facts['printings'].items():
            index[(p['set_code'], p['collector_number'])].append(aid)
        zone = None
        for line in text.splitlines():
            if not line.strip():
                continue
            if line in ('Deck', 'Sideboard', 'Commander', 'Companion'):
                zone = {'Deck': 'main', 'Sideboard': 'sideboard', 'Commander': 'commander', 'Companion': 'companions'}[line]
                continue
            m = re.fullmatch(r'([1-9][0-9]*) (.+) \(([^()]+)\) (\S+)', line)
            if zone is None or not m:
                raise ValueError('unresolvable deck line: ' + line)
            n, name, code, number = m.groups()
            ids = [aid for aid in index[(code, number)]
                   if facts['cards'][facts['printings'][aid]['title_id']]['name'] == name]
            if len(ids) != 1:
                raise ValueError('ambiguous or missing printing: ' + line)
            aid = ids[0]
            rows.append((zone, aid, int(n)))
    result = []
    for zone, aid, n in rows:
        if type(n) is not int or n <= 0 or aid not in facts['printings']:
            raise ValueError('invalid quantity or unknown Arena identity')
        p = facts['printings'][aid]
        c = facts['cards'].get(p['title_id'])
        if not c:
            raise ValueError('missing card identity')
        result.append(dict(zone=zone, arena_id=aid, quantity=n, title_id=p['title_id'], card=c, printing=p, basic=aid in facts['basic']))
    return result


def deck_text(rows):
    lines = []
    for zone, label in [('main', 'Deck'), ('sideboard', 'Sideboard'), ('commander', 'Commander'), ('companions', 'Companion')]:
        selected = [r for r in rows if r['zone'] == zone]
        if selected:
            if lines:
                lines.append('')
            lines.append(label)
            for r in selected:
                lines.append(f"{r['quantity']} {r['card']['name']} ({r['printing']['set_code']}) {r['printing']['collector_number']}")
    return '\n'.join(lines) + '\n'


def legality(rows, facts, queue):
    """Report per-title source status, separately from construction diagnostics."""
    counts, title_counts, details = Counter(), Counter(), {}
    violations = []
    for r in rows:
        tid = r['title_id']; n = r['quantity']
        evidence = facts['snapshot']['payload']['titles'].get(str(tid), {}).get('queues', {}).get(queue, {'status': 'unknown'})
        counts[evidence['status']] += n
        details[str(tid)] = dict(name=r['card']['name'], evidence=evidence)
        title_counts[tid] += n
    for tid, n in title_counts.items():
        if n > 4 and any(not r['basic'] for r in rows if r['title_id'] == tid):
            violations.append(f'copy limit: title {tid}, {n} copies')
    zones = Counter()
    for r in rows:
        zones[r['zone']] += r['quantity']
    if zones['main'] < 60:
        violations.append('Standard main deck fewer than 60')
    if zones['sideboard'] > (0 if queue == 'BO1' else 15):
        violations.append('sideboard outside assessed queue convention')
    if zones['commander'] or zones['companions']:
        violations.append('commander/companion construction not validated')
    status = 'illegal' if counts['illegal'] else 'unknown' if counts['unknown'] else 'legal'
    return dict(queue=queue, card_legality=status, copy_weighted_counts=dict(counts), titles=details,
                construction_diagnostics=violations, fully_standard_legal=status == 'legal' and not violations,
                scope='Pinned Run 20E evidence; not live Arena queue verification. BO1 no-sideboard convention follows Run 21.')


def automatic_flags(rows):
    main = [r for r in rows if r['zone'] == 'main']
    curve, composition, understanding, demand, lands = Counter(), Counter(), Counter(), Counter(), Counter()
    acceleration, interaction, unknown_cost = 0, 0, 0
    printing_sets = defaultdict(set)
    for r in rows:
        printing_sets[r['title_id']].add(r['arena_id'])
    for r in main:
        c = r['card']; n = r['quantity']; types = c.get('types')
        if not types:
            composition['unknown_type'] += n
            continue
        land = 'Land' in types.split()
        composition['lands' if land else 'creatures' if 'Creature' in types.split() else 'other_nonlands'] += n
        if land:
            if r['basic']:
                lands[c.get('color_identity') or 'unknown'] += n
            continue
        mv = c.get('cmc')
        curve['unknown' if mv is None else str(mv)] += n
        text = c.get('rules_text') or ''
        abilities = decompose_abilities(text, card_name=c['name'], card_types=types, canonical_anomaly=bool(c.get('anomaly_count')), grammar_version='5')
        statuses = [a.parse_status for a in abilities]
        level = 'fully' if statuses and all(s == 'supported' for s in statuses) else 'partially' if any(s in ('supported', 'partial') for s in statuses) else 'not_understood'
        understanding[level] += n
        if any(e.kind in ('destroy', 'exile') and e.target and e.target.startswith('target_') for a in abilities for e in a.effects):
            interaction += n
        # Lexical signal only; cannot infer executable mana production or net ramp.
        if re.search(r'\badd\s+(?:\{[WUBRGC0-9]+\}|[^.\n]{0,80}\bmana\b)', text, re.I):
            acceleration += n
        if not c.get('mana_cost'):
            unknown_cost += n
        for token in re.findall(r'\{([^{}]+)\}', c.get('mana_cost') or ''):
            for color in set(token.split('/')) & set('WUBRG'):
                demand[color] += n
    denominator = sum(understanding.values())
    values = {
        'curve': dict(counts=dict(sorted(curve.items())), highest_known_mana_value=max((float(x) for x in curve if x != 'unknown'), default=None), lands=composition['lands']),
        'acceleration': dict(mana_production_mentions=acceleration, highest_known_mana_value=max((float(x) for x in curve if x != 'unknown'), default=None), not_net_ramp=True),
        'composition': dict(composition),
        'interaction': dict(recognized_targeted_removal_copies=interaction, coverage='lower bound limited by grammar 5'),
        'understanding': dict(copy_counts={k:understanding[k] for k in ('fully','partially','not_understood')}, denominator=denominator, shares={k:str(Fraction(understanding[k],denominator or 1)) for k in ('fully','partially','not_understood')}, scope='main nonlands with known type'),
        'split_printings': dict(title_count=sum(len(v)>1 for v in printing_sets.values()), scope='all zones; cosmetic'),
        'mana_balance': dict(colored_symbol_mentions=dict(sorted(demand.items())), verified_basic_copies=dict(sorted(lands.items())), missing_cost_copies=unknown_cost, hybrid='each alternative color counted; not a payment prescription'),
    }
    return {key:dict(label='for human review', measurements=value, rationale=RATIONALES[key]) for key,value in values.items()}


def verify_manifest(folder):
    folder = Path(folder)
    manifest = read_json(folder/'manifest.json')
    seal = read_json(folder/'freeze.json')
    if file_hash(folder/'manifest.json') != seal['manifest_sha256'] or file_hash(folder/'policy.json') != seal['policy_sha256']:
        raise ValueError('frozen manifest/policy hash mismatch')
    if read_json(folder/'policy.json') != POLICY or any(manifest[k] != v for k,v in VERSIONS.items()):
        raise ValueError('unsupported frozen version; baseline regrading required')
    for entry in manifest['decks']:
        pinned_read(entry)
        for e in entry.get('evidence', []):
            if file_hash(e['source_path']) != e['sha256']:
                raise ValueError('generation evidence hash mismatch')
    return manifest, seal


def packet(entries, facts, output, manifest_hash):
    output = Path(output); output.mkdir(exist_ok=False)
    key, flags, legal = {}, {}, {}
    ordered = sorted(entries, key=lambda e:(hashlib.sha256(('run22-origin-mask-v1:'+e['id']).encode()).hexdigest(),e['id']))
    for i,e in enumerate(ordered):
        label = 'Deck '+chr(65+i) if i < 26 else 'Deck '+str(i+1)
        rows = resolve(pinned_read(e),facts,e.get('encoding','arena_text'))
        signals = automatic_flags(rows)
        key[label] = dict(id=e['id'], sha256=e['sha256'], request_id=e['request_id'], origin=e['origin'])
        flags[e['id']] = signals
        legal[e['id']] = {q:legality(rows,facts,q) for q in e['queues']}
        # No IDs, paths, origin or generator provenance in the grader-facing file.
        sheet = dict(label=label, **VERSIONS, deck_list=deck_text(rows), automatic_flags=signals,
                     legality=legal[e['id']], rubric=ANCHORS,
                     scores={k:dict(score=None,why='') for k in ANCHORS}, cards_i_would_cut='', cards_i_would_add='',
                     notice='Origin-masked, not guaranteed blind; familiarity bias remains. Grade selection, not explanations.')
        write_new(output/(label.replace(' ','-')+'.json'),sheet)
        key[label]['worksheet_content_sha256'] = digest({k:v for k,v in sheet.items() if k not in ('scores','cards_i_would_cut','cards_i_would_add')})
    write_new(output/'key.json',dict(manifest_sha256=manifest_hash, entries=key))
    write_new(output/'flags.json',flags)
    write_new(output/'legality.json',legal)


def build(entries, facts, output):
    output = Path(output)
    if len({e['id'] for e in entries}) != len(entries):
        raise ValueError('duplicate deck ID')
    for e in entries:
        pinned_read(e)
    manifest = dict(**VERSIONS, requests=REQUESTS, decks=entries, facts_provenance=facts.get('provenance',{}))
    output.mkdir(exist_ok=False)
    write_new(output/'manifest.json',manifest); write_new(output/'policy.json',POLICY)
    seal = dict(manifest_sha256=file_hash(output/'manifest.json'), policy_sha256=file_hash(output/'policy.json'))
    write_new(output/'freeze.json',seal)
    verify_manifest(output)
    packet(entries,facts,output/'worksheets',seal['manifest_sha256'])


def register_candidate(folder, source, request_id, provenance, output):
    manifest, seal = verify_manifest(folder)
    if request_id not in REQUESTS or not all(provenance.get(k) for k in ('generator','version','commit','inputs','card_specific_exclusions')):
        raise ValueError('request and full generator provenance required; disclose card-specific exclusions or none')
    h = file_hash(source)
    record = dict(id='candidate-'+digest(dict(sha256=h,request_id=request_id,provenance=provenance)), request_id=request_id,
                  source_path=str(Path(source).resolve()), sha256=h, queues=[REQUESTS[request_id]['queue']], origin='candidate', encoding='arena_text',
                  provenance=provenance, manifest_sha256=seal['manifest_sha256'], **VERSIONS)
    write_new(output,record)
    return record


def entries_with_candidates(folder, candidates):
    manifest,seal = verify_manifest(folder)
    entries = list(manifest['decks'])
    for path in candidates:
        c = read_json(path)
        if c['manifest_sha256'] != seal['manifest_sha256'] or any(c[k]!=v for k,v in VERSIONS.items()) or c['request_id'] not in REQUESTS or c['origin']!='candidate':
            raise ValueError('candidate version/request mismatch')
        if c['queues'] != [REQUESTS[c['request_id']]['queue']]:
            raise ValueError('candidate queue mismatch')
        pinned_read(c); entries.append(c)
    if len({e['id'] for e in entries}) != len(entries):
        raise ValueError('duplicate IDs')
    return entries,seal


def ingest(folder, worksheet_dir, candidates, grader, graded_on, output):
    if not grader.strip() or date.fromisoformat(graded_on).isoformat()!=graded_on:
        raise ValueError('grader and ISO date required')
    entries,seal = entries_with_candidates(folder,candidates)
    allowed = {e['id']:e for e in entries}
    worksheet_dir = Path(worksheet_dir); key = read_json(worksheet_dir/'key.json')
    if key['manifest_sha256'] != seal['manifest_sha256']:
        raise ValueError('worksheet manifest mismatch')
    scores = []; seen = set()
    sheets = sorted(worksheet_dir.glob('Deck-*.json'))
    if len(sheets)!=len(key['entries']):
        raise ValueError('incomplete worksheets')
    for path in sheets:
        w = read_json(path); link = key['entries'].get(w.get('label'))
        if not link or link['id'] not in allowed or link['id'] in seen:
            raise ValueError('unknown/duplicate worksheet deck')
        e = allowed[link['id']]; seen.add(e['id'])
        if any(link[k]!=e[k] for k in ('sha256','request_id','origin')) or any(w[k]!=v for k,v in VERSIONS.items()):
            raise ValueError('worksheet identity/version mismatch')
        immutable = {k:v for k,v in w.items() if k not in ('scores','cards_i_would_cut','cards_i_would_add')}
        if digest(immutable)!=link['worksheet_content_sha256']:
            raise ValueError('worksheet content changed outside grading fields')
        if set(w['scores'])!=set(ANCHORS):
            raise ValueError('all rubric dimensions required')
        for score in w['scores'].values():
            if type(score['score']) is not int or not 0<=score['score']<=5 or not isinstance(score.get('why'),str) or not score['why'].strip():
                raise ValueError('score must be integer 0-5 with nonempty why')
        if any(not isinstance(w.get(k),str) for k in ('cards_i_would_cut','cards_i_would_add')):
            raise ValueError('cut/add sections must be text')
        scores.append(dict(id=e['id'],request_id=e['request_id'],origin=e['origin'],sha256=e['sha256'],scores=w['scores'],total=sum(s['score'] for s in w['scores'].values()),cards_i_would_cut=w['cards_i_would_cut'],cards_i_would_add=w['cards_i_would_add']))
    # Flags live in a separate artifact and are never added to human totals.
    flags_path=worksheet_dir/'flags.json'
    flags=read_json(flags_path)
    for path in sheets:
        w=read_json(path); e=key['entries'][w['label']]
        if flags.get(e['id'])!=w['automatic_flags']:
            raise ValueError('flags artifact mismatch')
    result=dict(**VERSIONS,grader=grader,date=graded_on,**seal,human_scores=scores,
                automatic_flags_artifact=dict(path=str(flags_path.resolve()),sha256=file_hash(flags_path)))
    write_new(output,result)
    return result


def compare(left, right):
    for k in (*VERSIONS,'manifest_sha256','policy_sha256'):
        if left[k]!=right[k]:
            raise ValueError('comparison version/frozen-set mismatch: '+k)
    tables=[]; references=[]; sets=[]; flags=[]
    for result in (left,right):
        ref=result['automatic_flags_artifact']
        if file_hash(ref['path'])!=ref['sha256']:
            raise ValueError('flags hash mismatch')
        flags.append(read_json(ref['path']))
        indexed={}
        for e in result['human_scores']:
            if e['request_id']=='reference':
                references.append(e); continue
            req=e['request_id']
            if req not in REQUESTS or req in indexed:
                raise ValueError('unknown or duplicate request in comparison arm')
            indexed[req]=e
        if set(indexed)!=set(REQUESTS):
            raise ValueError('comparison requires all requests; no selective comparison')
        sets.append(indexed)
    for req in sorted(REQUESTS):
        a,b=(s[req] for s in sets)
        tables.append(dict(request_id=req,left_id=a['id'],right_id=b['id'],dimensions={d:dict(left=a['scores'][d]['score'],right=b['scores'][d]['score']) for d in ANCHORS},human_totals=dict(left=a['total'],right=b['total']),flag_differences={k:dict(left=flags[0][a['id']][k],right=flags[1][b['id']][k]) for k in RATIONALES if flags[0][a['id']][k]!=flags[1][b['id']][k]}))
    return dict(**VERSIONS,paired_requests=tables,calibration_reference_points=references,verdict=None,notice='Human scores and automatic review signals are separate; no automatic good/bad verdict.')


def real_facts():
    return read_facts(dict(snapshot=SNAPSHOT,database=DATABASE,arena=ARENA),PINS,'BO1')
