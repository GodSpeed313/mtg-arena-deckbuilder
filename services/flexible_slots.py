"""Frozen Run 28 r4 slot policy and validation; no scoring policy changes."""
from collections import Counter
from types import MappingProxyType

PROFILE = 'run28-flexible-slots-r4'
POLICY = MappingProxyType({
    'profile': PROFILE,
    'creature_range': (20, 24),
    'noncreature_range': (12, 16),
    'creature_slots': ((1, 4), (2, 8), (3, 8), (4, 4)),
    'noncreature_slots': ((2, 4), (3, 4), (4, 4)),
    'flex_allocation': ((1, 1), (2, 1), (3, 1), (4, 1)),
})
SENSITIVITY_LABEL = 'LOCAL ARITHMETIC SENSITIVITY (diagnostic only)'
SENSITIVITY_SCOPE = ('Local arithmetic sensitivity, not a counterfactual '
                     'deck-generation result; later selections and scores may change.')


def enabler_credit(card, selected, evidence):
    """Marginal boost-enabler portion of existing capped partner credit.

    Preserve credit justified by other families sharing the same partners/cap.
    Other terms (including hybrid credit) remain fixed in this local arithmetic.
    """
    tid = card['title_id']
    for term in evidence:
        if term['rule_identifier'] != 'coherence.compatible_dependencies':
            continue
        remaining = {next(t for t in edge['cards'] if t != tid)
                     for edge in term['evidence']
                     if not (edge['rule_id'] == 'dependency.noncreature_cast_boost.v1'
                             and edge['cards'][0] == tid)}
        return term['numeric_contribution'] - min(12, 2 * sum(min(selected[t], 4) for t in remaining))
    return 0


def decision_record(bucket, winner, alternative, selected, cards, graph, quality):
    def describe(card):
        if card is None:
            return None
        score, evidence = quality(card, selected, cards, graph)
        credit = enabler_credit(card, selected, evidence)
        return dict(title_id=card['title_id'], name=card['name'],
                    card_type='creature' if card['creature'] else 'noncreature',
                    actual_mana_value=card['cmc'], score=score,
                    enabler_credit=credit, arithmetic_score=score-credit,
                    guard_contribution=sum(e['numeric_contribution'] for e in evidence
                                           if e['rule_identifier']=='coherence.spell_support_guard'),
                    contributions=[dict(rule_identifier=e['rule_identifier'],
                                        numeric_contribution=e['numeric_contribution']) for e in evidence],
                    roles=sorted({f['label'] for f in card['analysis']['features']
                                  if f['dimension']=='role'}) or ['unknown'])
    win, other = describe(winner), describe(alternative)
    def arithmetic_key(row):
        return (-row['arithmetic_score'], row['actual_mana_value'], row['title_id'])
    return dict(bucket=bucket, winner=win, best_alternative_other_type=other,
                score_margin=win['score']-other['score'] if other else None,
                selected_before=dict(selected),
                sensitivity=dict(label=SENSITIVITY_LABEL, scope=SENSITIVITY_SCOPE,
                                 method='Subtract marginal boost-enabler credit from these two scores only; all other terms fixed; original tie key.',
                                 outcome_reverses=arithmetic_key(other)<arithmetic_key(win) if other else None,
                                 arithmetic_margin=win['arithmetic_score']-other['arithmetic_score'] if other else None,
                                 comparison_status='established for two recorded candidates' if other else 'no eligible other-type alternative'))


def validate_template(main, side, cards, target, assignments, *, profile):
    """Validate declared r4 bounds, never bounds supplied in generated output."""
    if profile != PROFILE:
        raise ValueError('unknown flexible-slot validation profile')
    errors = []
    tagged = Counter(); by_bucket = Counter()
    for a in assignments:
        if (not isinstance(a, dict) or set(a) != {'bucket','title_id','quantity'}
                or type(a['bucket']) is not int or type(a['title_id']) is not int
                or type(a['quantity']) is not int or a['quantity'] != 1):
            errors.append('malformed flex assignment'); continue
        bucket, tid = a['bucket'], a['title_id']
        c = cards.get(tid)
        if bucket not in dict(POLICY['flex_allocation']) or not c or c['basic'] or c['creature'] or c['slot'] != (bucket, False):
            errors.append('flex assignment type or bucket mismatch'); continue
        tagged[tid] += 1; by_bucket[bucket] += 1
    if any(n > dict(POLICY['flex_allocation'])[b] for b,n in by_bucket.items()):
        errors.append('flex allocation exceeded')
    if any(n > main.get(t,0) for t,n in tagged.items()):
        errors.append('flex assignment exceeds selected main copies')
    creatures = Counter(); spells = Counter(); lands = 0
    for tid,n in main.items():
        c = cards[tid]
        if type(n) is not int or n < 0:
            errors.append('invalid main quantity'); continue
        if c['basic']:
            lands += n
        elif c['creature']:
            creatures[c['slot'][0]] += n
        else:
            spells[c['slot'][0]] += n-tagged[tid]
    # Counter equality treats zero-count unused buckets as absent.
    expected = Counter(dict(POLICY['creature_slots']))
    if creatures + by_bucket != expected:
        errors.append('creatures plus flex spells differ from declared bucket counts')
    if spells != Counter(dict(POLICY['noncreature_slots'])):
        errors.append('original 12 noncreature slots disturbed')
    creature_count = sum(creatures.values())
    noncreature_count = sum(spells.values())+sum(tagged.values())
    if not POLICY['creature_range'][0] <= creature_count <= POLICY['creature_range'][1]:
        errors.append('creature count outside declared 20-24 range')
    if not POLICY['noncreature_range'][0] <= noncreature_count <= POLICY['noncreature_range'][1]:
        errors.append('noncreature count outside declared 12-16 range')
    if lands != 24 or sum(main.values()) != 60:
        errors.append('requires 60 main and 24 basics')
    if sum(n for t,n in main.items() if not cards[t]['basic'] and cards[t]['interaction']) < target:
        errors.append('recognized interaction minimum missed')
    if any(type(n) is not int or n < 0 for n in side.values()):
        errors.append('invalid sideboard quantity')
    if any(n>cards[t]['limit'] for t,n in (main+side).items() if not cards[t]['basic']):
        errors.append('legendary/general combined copy limit exceeded')
    return dict(status='FAIL' if errors else 'PASS', violations=errors,
                declared_profile=PROFILE, lands=lands, creatures=creature_count,
                noncreatures=noncreature_count, flex_by_bucket=dict(by_bucket),
                interaction_minimum=target,
                curve={str(b):creatures[b]+spells[b]+by_bucket[b] for b in range(1,5)})
