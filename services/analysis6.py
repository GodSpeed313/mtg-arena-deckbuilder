"""Opt-in additive structural recognition. Analysis 5 remains the effect axis."""
from collections import Counter
from copy import deepcopy
from dataclasses import asdict

from mtgadb.structural_evidence import StructuralEvidence
from mtgadb.text import normalize_text
from services.intelligence import analyze_deck


def bind_card(grp_id, abilities, entries, *, unavailable=None, merged_printings=False):
    """Path 2 only: exact deck ID plus complete ordered positional correspondence.

    No isolated text matching and no representative-printing fallback. A merged
    Analysis 5 title with multiple exact deck printings cannot provide a unique
    counterpart and therefore receives no credit.
    """
    entries = tuple(entries)
    state, reason = 'bound', 'complete_ordered_correspondence'
    if unavailable:
        state, reason = 'unbound', unavailable
    elif merged_printings:
        state, reason = 'ambiguous', 'multiple_deck_printings_for_title'
    elif any(e.grp_id != grp_id for e in entries):
        state, reason = 'unbound', 'exact_printing_mismatch'
    elif len(entries) != len(abilities):
        state, reason = 'unbound', 'sequence_length_mismatch'
    elif ([e.position for e in entries] != list(range(len(entries)))
          or [a.get('source_index') for a in abilities] != list(range(1, len(abilities)+1))
          or len({a.get('ability_id') for a in abilities}) != len(abilities)):
        state, reason = 'ambiguous', 'nonunique_or_invalid_positions'
    elif any(normalize_text(e.local_text) != a['raw_text']
             for e, a in zip(entries, abilities)):
        state, reason = 'unbound', 'sequence_text_mismatch'
    result = []
    for entry in entries:
        if not entry.eligible:
            continue
        fact = asdict(entry)
        record = {'entry': fact, 'binding': state, 'reason': reason,
                  'evidence_path': 'path2' if state == 'bound' else None,
                  'analysis5_ability_id': None, 'analysis5_kind': None,
                  'effect_classification': None, 'structures': [], 'kind_corrections': []}
        if state == 'bound':
            ability = abilities[entry.position]
            record['analysis5_ability_id'] = ability['ability_id']
            record['analysis5_kind'] = ability['kind']
            # Entire original ability result, byte-equivalent under the same
            # serializer, not merely a supported/partial status copied by name.
            record['effect_classification'] = deepcopy(ability)
            if entry.loyalty_cost is not None:
                record['structures'].append({'structure': 'loyalty_ability', 'raw_cost': entry.loyalty_cost})
            if entry.chapter_index is not None:
                record['structures'].append({'structure': 'saga_chapter', 'chapter_index': entry.chapter_index})
            for structure in record['structures']:
                if ability['kind'] != structure['structure']:
                    record['kind_corrections'].append({'analysis5_kind': ability['kind'],
                                                       'structural_kind': structure['structure']})
        result.append(record)
    return {'grp_id': grp_id, 'binding': state, 'reason': reason,
            'analysis5_sequence_length': len(abilities), 'source_sequence_length': len(entries),
            'entries': result}


def structural_counts(cards, weighted=False):
    counts = Counter(eligible=0, bound=0, unbound=0, ambiguous=0,
                     structurally_recognized=0, kind_corrections=0)
    reasons, paths = Counter(), Counter()
    for card in cards:
        weight = card['quantity'] if weighted else 1
        for entry in card['entries']:
            counts['eligible'] += weight
            counts[entry['binding']] += weight
            if entry['binding'] == 'bound':
                paths[entry['evidence_path']] += weight
                counts['structurally_recognized'] += weight
                counts['kind_corrections'] += weight * bool(entry['kind_corrections'])
            else:
                reasons[entry['reason']] += weight
    return dict(counts, denominator=counts['eligible'], unbound_or_ambiguous_by_reason=dict(reasons),
                bound_by_path=dict(paths), unit='card_copy_times_structural_entry' if weighted else 'exact_printing_structural_entry')


def analyze_deck6(deck, con, evidence):
    if not isinstance(evidence, StructuralEvidence):
        raise TypeError('verified StructuralEvidence required before analysis')
    if con.execute('PRAGMA query_only').fetchone()[0] != 1:
        raise ValueError('read-only canonical connection required (query_only=1)')
    base = analyze_deck(deck, con)
    result = deepcopy(base)
    zones = {}
    for zone_name in ('main', 'sideboard', 'commander'):
        zone = getattr(deck, zone_name)
        identities = {}
        for gid in zone:
            row = con.execute('SELECT title_id FROM printings WHERE arena_id=?', (gid,)).fetchone()
            identities[gid] = row[0] if row else None
        multiplicity = Counter(t for t in identities.values() if t is not None)
        rows = {r['title_id']: r for r in base['zones'][zone_name]['cards']}
        cards = []
        for gid, quantity in sorted(zone.items()):
            title = identities[gid]
            row = rows.get(title)
            bound = bind_card(gid, row['abilities'] if row else [], evidence.entries(gid),
                              unavailable='missing_canonical_card' if row is None else None,
                              merged_printings=multiplicity[title] > 1)
            bound.update(quantity=quantity, title_id=title)
            cards.append(bound)
        zones[zone_name] = {'cards': cards, 'structural_axis': {
            'distinct': structural_counts(cards), 'copy_weighted': structural_counts(cards, True)},
            'effect_axis': deepcopy(base['zones'][zone_name]['rules_text_coverage'])}
    result['structural_analysis'] = {'contract_version': '6', 'evidence': evidence.proof,
                                    'zones': zones, 'diagnostic_axis': {'status': 'not_run'}}
    return result
