"""Fixed transparent template, pinned inputs, no competitive-strength claims."""
from collections import Counter, defaultdict
from fractions import Fraction
from itertools import combinations
import hashlib
import json
from pathlib import Path

from services.abilities import decompose_abilities
from services.deck_construction_data import COLORS, read_facts, special, symbols
from services.generated_deck_validator import validate_text

# (mana-value bucket, creature flag, copies). 24 creatures, 12 other nonlands.
SLOTS = ((1, True, 8), (2, True, 8), (3, True, 4), (3, False, 4),
         (4, True, 4), (4, False, 4), (5, False, 4))
STATEMENT = 'This deck is legal and structurally coherent by the generator\'s documented rules. It has not been evaluated for competitive strength.'


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n'


def revision():
    root = Path(__file__).resolve().parents[1]
    files = ['services/deck_construction_data.py', 'services/generated_deck_validator.py',
             'services/deterministic_deck_generator.py', 'generate_standard_deck.py']
    return {f: hashlib.sha256((root / f).read_bytes()).hexdigest() for f in files}


def prepare(facts, queue):
    eligible, basics, excluded = [], [], []
    export_keys = Counter((p['set_code'], p['collector_number']) for p in facts['printings'].values())
    by_title = defaultdict(list)
    for aid, p in facts['printings'].items():
        by_title[p['title_id']].append((aid, p))
    for tid, c in sorted(facts['cards'].items()):
        reasons = []
        state = facts['snapshot']['payload']['titles'].get(str(tid), {}).get('queues', {}).get(queue, {}).get('status', 'unknown')
        if state != 'legal':
            reasons.append('legality_' + state)
        markers = special(c.get('rules_text'))
        reasons.extend('special_text:' + m for m in markers)
        if not c.get('name') or not c.get('types') or not isinstance(c.get('title_id'), int):
            reasons.append('missing_identity_name_or_type')
        ps = [(aid, p) for aid, p in by_title[tid] if p.get('set_code') and p.get('collector_number')
              and export_keys[(p['set_code'], p['collector_number'])] == 1]
        ps.sort(key=lambda x: (x[1]['set_code'], x[1]['collector_number'], x[0], tid))
        is_land = 'Land' in (c.get('types') or '').split()
        if is_land:
            ps = [(aid, p) for aid, p in ps if aid in facts['basic']]
            if not ps:
                reasons.append('nonbasic_land_deferred_or_unverified')
        if not ps:
            reasons.append('missing_unambiguous_export_printing')
        if c.get('colors') is None or any(x not in COLORS for x in (c.get('colors') or '')):
            reasons.append('missing_or_invalid_colors')
        if c.get('cmc') is None or type(c.get('cmc')) not in (int, float) or c.get('cmc', -1) < 0:
            reasons.append('missing_or_invalid_mana_value')
        tokens = []
        if not is_land:
            try:
                tokens = symbols(c.get('mana_cost'))
                if not {t for t in tokens if t in COLORS} <= set(c.get('colors') or ''):
                    reasons.append('colors_cost_disagreement')
                if 'C' in tokens:
                    reasons.append('colorless_required_mana_source_deferred')
                actual = sum(int(t) if t.isdigit() else 1 for t in tokens)
                if actual != c.get('cmc'):
                    reasons.append('mana_value_cost_disagreement')
                if actual < 1:
                    reasons.append('outside_template_zero_mana')
            except ValueError:
                reasons.append('missing_or_unsupported_mana_cost')
        if reasons:
            excluded.append(dict(title_id=tid, name=c.get('name'), reasons=sorted(set(reasons))))
            continue
        aid, printing = ps[0]
        row = dict(c, arena_id=aid, printing=printing, basic=is_land, tokens=tokens,
                   required_colors=sorted(set(c['colors']) | {t for t in tokens if t in COLORS}),
                   creature='Creature' in c['types'].split(), bucket=min(5, int(c['cmc'])))
        if is_land:
            # Mono-color basic choice from authoritative identity and canonical color identity.
            identity = c.get('color_identity')
            if not isinstance(identity, str) or identity not in COLORS or len(identity) != 1:
                excluded.append(dict(title_id=tid, name=c['name'], reasons=['basic_not_single_color_source']))
                continue
            row['land_color'] = identity
            basics.append(row)
        else:
            abilities = decompose_abilities(c.get('rules_text') or '', card_name=c['name'], card_types=c['types'],
                                           canonical_anomaly=bool(c.get('anomaly_count')), grammar_version='5')
            statuses = [a.parse_status for a in abilities]
            row['understanding'] = 0 if statuses and all(s == 'supported' for s in statuses) else 1 if any(s in ('supported', 'partial') for s in statuses) else 2
            row['removal'] = any(e.kind in ('destroy', 'exile') and e.target and e.target.startswith('target_') for a in abilities for e in a.effects)
            eligible.append(row)
    return eligible, basics, excluded


def measure(pool, colors, basics):
    available = [c for c in pool if set(c['required_colors']) <= set(colors)]
    fills = [min(Fraction(sum(4 for c in available if c['bucket'] == b and c['creature'] == creature), n), 1)
             for b, creature, n in SLOTS]
    creature_fill = min(Fraction(sum(4 for c in available if c['creature']), 24), 1)
    primary = (sum(fills) + creature_fill) / (len(fills) + 1)
    share = Fraction(sum(c['understanding'] < 2 for c in available), len(available) or 1)
    removal = sum(c['removal'] for c in available)
    secondary = Fraction(3, 4) * share + Fraction(1, 4) * min(Fraction(removal, 4), 1)
    lands_ok = all(any(c['land_color'] == color for c in basics) for color in colors)
    return dict(colors=colors, eligible_titles=len(available), slot_fractions=[str(x) for x in fills],
                creature_fraction=str(creature_fill), primary=str(primary), understood_share=str(share),
                recognized_removal_lower_bound=removal, secondary=str(secondary),
                feasible=all(x == 1 for x in fills) and lands_ok, basic_sources_available=lands_ok)


def rank(card):
    return card['understanding'], card['cmc'], card['title_id']


def template_check(main, cards):
    lands = sum(n for tid, n in main.items() if cards[tid]['basic'])
    creatures = sum(n for tid, n in main.items() if not cards[tid]['basic'] and cards[tid]['creature'])
    actual = Counter()
    for tid, n in main.items():
        c = cards[tid]
        if not c['basic']:
            actual[(c['bucket'], c['creature'])] += n
    expected = {(b, c): n for b, c, n in SLOTS}
    deviations = []
    for field, got, want in [('total', sum(main.values()), 60), ('lands', lands, 24), ('creatures', creatures, 24)]:
        if got != want:
            deviations.append(dict(field=field, actual=got, expected=want))
    for key in sorted(set(actual) | set(expected)):
        if actual[key] != expected.get(key, 0):
            deviations.append(dict(field='slot', mana_bucket=key[0], creature=key[1], actual=actual[key], expected=expected.get(key, 0)))
    return dict(status='FAIL' if deviations else 'PASS', deviations=deviations, land_count=lands,
                creature_count=creatures, mana_distribution={str(b): sum(n for (bucket, _), n in actual.items() if bucket == b) for b in range(1, 6)})


def generate(paths, pins, queue):
    facts = read_facts(paths, pins, queue)
    pool, basics, excluded = prepare(facts, queue)
    measurements = [measure(pool, ''.join(cs), basics) for n in (1, 2) for cs in combinations(COLORS, n)]
    feasible = [m for m in measurements if m['feasible']]
    if not feasible:
        raise ValueError('insufficient legal complete-data pool for fixed template: ' + canonical(measurements))
    winner = min(feasible, key=lambda m: (-Fraction(m['primary']), -Fraction(m['secondary']), len(m['colors']), tuple(COLORS.index(x) for x in m['colors'])))
    colors = winner['colors']
    available = [c for c in pool if set(c['required_colors']) <= set(colors)]
    cards = {c['title_id']: c for c in pool + basics}
    main, side = Counter(), Counter()
    for bucket, creature, needed in SLOTS:
        for c in sorted((c for c in available if c['bucket'] == bucket and c['creature'] == creature), key=rank):
            n = min(4, needed)
            main[c['title_id']] += n
            needed -= n
            if not needed:
                break
        if needed:
            raise ValueError('slot filling failed; no relaxation')
    pips = Counter()
    for tid, n in main.items():
        for token in cards[tid]['tokens']:
            if token in COLORS:
                pips[token] += n
    if not sum(pips.values()):
        raise ValueError('no colored demand; mana-base policy undefined')
    quotas = {c: Fraction(24 * pips[c], sum(pips.values())) for c in colors}
    lands = {c: int(v) for c, v in quotas.items()}
    for color in sorted(colors, key=lambda c: (-(quotas[c] - lands[c]), COLORS.index(c)))[:24 - sum(lands.values())]:
        lands[color] += 1
    for color, n in lands.items():
        if n:
            land = min((c for c in basics if c['land_color'] == color), key=lambda c: c['title_id'])
            main[land['title_id']] = n
    if queue == 'BO3':
        needed = 15
        for c in sorted(available, key=lambda c: (not c['removal'], *rank(c))):
            n = min(4 - main[c['title_id']], needed)
            if n:
                side[c['title_id']] += n
                needed -= n
            if not needed:
                break
    lines = []
    for section, counts in [('Deck', main), ('Sideboard', side)]:
        if section == 'Sideboard' and queue == 'BO1':
            continue
        if lines:
            lines.append('')
        lines.append(section)
        for tid, n in sorted(counts.items()):
            if n:
                c = cards[tid]
                lines.append(f"{n} {c['name']} ({c['printing']['set_code']}) {c['printing']['collector_number']}")
    text = '\n'.join(lines) + '\n'
    compliance = template_check(main, cards)
    validation = validate_text(text, paths, pins, queue)
    if validation['status'] != 'PASS' or compliance['status'] != 'PASS':
        raise ValueError('generated deck failed checks: ' + canonical([validation, compliance]))
    rarity = Counter()
    for tid, n in (main + side).items():
        rarity[cards[tid]['printing'].get('rarity') or 'unknown'] += n
    understood = sum(n for tid, n in main.items() if not cards[tid]['basic'] and cards[tid]['understanding'] < 2)
    report = dict(format='Standard', queue=queue, chosen_colors=colors,
                  selection_reason='Feasible template; lexicographic primary then secondary, fewer colors then WUBRG order.',
                  combinations=measurements, template_slots=[dict(mana_bucket=b, creature=c, copies=n) for b, c, n in SLOTS],
                  mana_base=dict(colored_pips=dict(sorted(pips.items())), lands=lands, rounding='largest remainder; WUBRG tie-break', nonbasic_lands='deferred'),
                  legality_validation=validation, structural_template_compliance=compliance,
                  arena_compatibility='pending operator testing', statement=STATEMENT,
                  recognized_removal_lower_bound=sum(n for tid, n in main.items() if not cards[tid]['basic'] and cards[tid]['removal']),
                  understood_ability_share=str(Fraction(understood, 36)),
                  exclusions=excluded, exclusion_counts=dict(sorted(Counter(r for x in excluded for r in x['reasons']).items())),
                  crafting_summary=dict(quantities_by_rarity=dict(sorted(rarity.items())), owned_cards='not known; no missing-cards claim',
                                        basis='Full main plus sideboard quantities from nothing; basic lands require no wildcards',
                                        wildcard_quantities_by_rarity=dict(sorted(Counter({r: sum(n for tid, n in (main + side).items() if not cards[tid]['basic'] and (cards[tid]['printing'].get('rarity') or 'unknown') == r) for r in rarity}).items()))),
                  sideboard_shortfall_reason='insufficient remaining eligible copy capacity' if queue == 'BO3' and sum(side.values()) < 15 else None,
                  provenance=facts['provenance'], snapshot_version=facts['snapshot']['snapshot_version'],
                  generator_source_revision=revision())
    return text, canonical(report)
