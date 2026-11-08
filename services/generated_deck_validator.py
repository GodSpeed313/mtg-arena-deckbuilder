"""Independent text-only legality validator; no generator/template imports."""
from collections import Counter, defaultdict
import re
from services.deck_construction_data import read_facts, special


def validate_text(text, paths, pins, queue):
    facts = read_facts(paths, pins, queue)
    errors = []
    index = defaultdict(list)
    for aid, p in facts['printings'].items():
        index[(p['set_code'], p['collector_number'])].append(aid)
    counts, zones, nonbasic = Counter(), Counter(), set()
    section = None
    seen = set()
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if line in ('Deck', 'Sideboard'):
            if line in seen:
                errors.append(dict(line=number, code='duplicate_section'))
            if line == 'Sideboard' and 'Deck' not in seen:
                errors.append(dict(line=number, code='section_order'))
            seen.add(line)
            section = line
            if queue == 'BO1' and line == 'Sideboard':
                errors.append(dict(line=number, code='bo1_sideboard'))
            continue
        match = re.fullmatch(r'([1-9][0-9]*) (.+) \(([^()]+)\) (\S+)', line)
        if section is None or match is None:
            errors.append(dict(line=number, code='unresolvable_line'))
            continue
        qty, name, code, collector = match.groups()
        qty = int(qty)
        zones[section] += qty
        ids = index.get((code, collector), [])
        if len(ids) != 1:
            errors.append(dict(line=number, code='ambiguous_or_unknown_printing'))
            continue
        aid = ids[0]
        tid = facts['printings'][aid]['title_id']
        card = facts['cards'].get(tid)
        if not card or card['name'] != name:
            errors.append(dict(line=number, code='name_mismatch'))
        counts[tid] += qty
        if aid not in facts['basic']:
            nonbasic.add(tid)
        evidence = facts['snapshot']['payload']['titles'].get(str(tid), {}).get('queues', {}).get(queue, {})
        if evidence.get('status') != 'legal':
            errors.append(dict(line=number, code='not_legal', title_id=tid, evidence_status=evidence.get('status', 'unknown')))
        markers = special(card.get('rules_text') if card else '')
        if markers:
            errors.append(dict(line=number, code='special_deckbuilding', markers=markers))
    if zones['Deck'] != 60:
        errors.append(dict(code='main_size', actual=zones['Deck'], expected=60))
    if zones['Sideboard'] > 15 or (queue == 'BO1' and zones['Sideboard']):
        errors.append(dict(code='sideboard_size', actual=zones['Sideboard']))
    for tid in sorted(nonbasic):
        if counts[tid] > 4:
            errors.append(dict(code='copy_limit', title_id=tid, quantity=counts[tid]))
    return dict(status='FAIL' if errors else 'PASS', violations=errors, main_count=zones['Deck'],
                sideboard_count=zones['Sideboard'], scope='legality validation only')
