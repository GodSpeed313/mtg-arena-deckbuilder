"""Run 24 single-response adapter. No live generation transport in Phase 0."""
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path

from services.deck_evaluation import deck_text
from services.evaluation_requests_v2 import REQUESTS, allowed, check_pair
from services.generated_deck_validator import validate_text

ROOT = Path(__file__).resolve().parents[1]
MODEL = 'claude-opus-5-5'
MAX_TOKENS = 32000  # Total output budget, including adaptive thinking.
CONTEXT_TOKENS = 1_000_000
FIELDS = ('name', 'mana_cost', 'types', 'subtypes', 'rules_text', 'cmc',
          'colors', 'color_identity', 'power', 'toughness', 'basic',
          'resolution', 'anomaly_count')


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def prompt():
    return (ROOT / 'docs/run24_prompt.txt').read_text(encoding='utf-8')


def legal_pool(facts, request_id):
    """Only queue legality and registered color constraints exclude cards."""
    request = REQUESTS[request_id]
    pool = []
    basic_titles = {p['title_id'] for aid, p in facts['printings'].items() if aid in facts['basic']}
    for tid, card in sorted(facts['cards'].items()):
        status = facts['snapshot']['payload']['titles'].get(str(tid), {}).get(
            'queues', {}).get(request['queue'], {}).get('status', 'unknown')
        if status != 'legal':
            continue
        if request['color_pair'] and not allowed(card, request['color_pair']):
            continue
        # Missing essential text is a whole-request stop, never a pool filter.
        if not all(isinstance(card.get(k), str) for k in ('name', 'mana_cost', 'types', 'rules_text')):
            raise ValueError('full-pool payload has missing essential metadata')
        basic = tid in basic_titles
        pool.append(dict(card, basic=basic))
    return pool


def encode_pool(pool):
    """One TSV line per card, retaining every original payload field and row."""
    lines = []
    for card in pool:
        cells = []
        for field in FIELDS:
            value = card.get(field)
            if value is None:
                cell = r'\N'
            elif isinstance(value, str):
                cell = value.replace('\\', '\\\\').replace('\t', r'\t').replace('\r', r'\r').replace('\n', r'\n')
            else:
                cell = compact(value)
            cells.append(cell)
        lines.append('\t'.join(cells))
    return '\n'.join(lines)


def decode_pool(text):
    """Inverse of encode_pool, used to prove exact field reconstruction."""
    if not text:
        return []
    rows = []
    for line in text.split('\n'):
        cells = line.split('\t')
        if len(cells) != len(FIELDS):
            raise ValueError('invalid compact card field count')
        row = {}
        for field, cell in zip(FIELDS, cells):
            if cell == r'\N':
                value = None
            elif field in ('cmc', 'basic', 'anomaly_count'):
                value = json.loads(cell)
            else:
                chars, i = [], 0
                escapes = {'\\': '\\', 't': '\t', 'r': '\r', 'n': '\n'}
                while i < len(cell):
                    if cell[i] == '\\':
                        i += 1
                        if i == len(cell) or cell[i] not in escapes:
                            raise ValueError('invalid compact escape')
                        chars.append(escapes[cell[i]])
                    else:
                        chars.append(cell[i])
                    i += 1
                value = ''.join(chars)
            row[field] = value
        rows.append(row)
    return rows


def build_request(facts, request_id):
    pool = legal_pool(facts, request_id)
    content = compact(dict(request_id=request_id, request=REQUESTS[request_id])) + '\n' + encode_pool(pool)
    return dict(model=MODEL, max_tokens=MAX_TOKENS, system=prompt(),
                thinking={'type': 'adaptive'}, output_config={'effort': 'medium'},
                messages=[dict(role='user', content=content)])


def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def parse_response(text):
    def invalid_constant(_):
        raise ValueError('nonfinite JSON constant')
    data = json.loads(text, object_pairs_hook=no_duplicates, parse_constant=invalid_constant)
    if not isinstance(data, dict) or set(data) != {'main', 'sideboard', 'interactions'}:
        raise ValueError('expected exactly main, sideboard, interactions')
    for zone in ('main', 'sideboard'):
        if not isinstance(data[zone], list):
            raise ValueError('zone must be an array')
        names = set()
        for row in data[zone]:
            if not isinstance(row, dict) or set(row) != {'name', 'quantity'}:
                raise ValueError('invalid deck row schema')
            if not isinstance(row['name'], str) or not row['name']:
                raise ValueError('invalid card name')
            if type(row['quantity']) is not int or row['quantity'] <= 0:
                raise ValueError('quantity must be a positive integer')
            if row['name'] in names:
                raise ValueError('duplicate name within zone')
            names.add(row['name'])
    if not isinstance(data['interactions'], list):
        raise ValueError('interactions must be an array')
    for claim in data['interactions']:
        if not isinstance(claim, dict) or set(claim) != {'claim', 'citations'}:
            raise ValueError('invalid interaction schema')
        if not isinstance(claim['claim'], str) or not claim['claim'].strip():
            raise ValueError('empty interaction claim')
        if not isinstance(claim['citations'], list) or not claim['citations']:
            raise ValueError('interaction requires citations')
        for citation in claim['citations']:
            if not isinstance(citation, dict) or set(citation) != {'name', 'quote'}:
                raise ValueError('invalid citation schema')
            if not all(isinstance(citation[k], str) for k in ('name', 'quote')):
                raise ValueError('citation name and quote must be strings')
    return data


def citation_flags(data, facts):
    by_name = defaultdict(list)
    for card in facts['cards'].values():
        by_name[card['name']].append(card)
    in_deck = {r['name'] for z in ('main', 'sideboard') for r in data[z]}
    flags = []
    for i, claim in enumerate(data['interactions']):
        for j, cite in enumerate(claim['citations']):
            cards = by_name[cite['name']]
            reasons = []
            if len(cards) != 1:
                reasons.append('unknown_or_ambiguous_exact_name')
            elif not cite['quote'] or cite['quote'] not in (cards[0].get('rules_text') or ''):
                reasons.append('quote_not_exact_nonempty_span')
            if cite['name'] not in in_deck:
                reasons.append('cited_card_not_in_deck')
            if reasons:
                flags.append(dict(interaction=i, citation=j, reasons=reasons))
    return dict(flags=flags, scope='Exact citation checks only; never quality grades or proof of an interaction.')


def assess(text, facts, paths, pins, request_id):
    """No normalization or repairs. Printing choice only encodes an exact name."""
    result = dict(status='FAIL', raw_response_sha256=hashlib.sha256(text.encode('utf-8')).hexdigest())
    try:
        data = parse_response(text)
    except (ValueError, TypeError) as exc:
        return dict(result, violations=[dict(code='unparseable_output', reason=str(exc))])
    result['citation_check'] = citation_flags(data, facts)
    result['interactions'] = data['interactions']
    by_name = defaultdict(list)
    export_keys = Counter((p['set_code'], p['collector_number']) for p in facts['printings'].values())
    for card in facts['cards'].values():
        by_name[card['name']].append(card)
    rows, errors = [], []
    for zone in ('main', 'sideboard'):
        for entry in data[zone]:
            matches = by_name[entry['name']]
            if len(matches) != 1:
                errors.append(dict(code='unknown_or_ambiguous_exact_name', zone=zone, name=entry['name']))
                continue
            card = matches[0]
            ps = [p for p in facts['printings'].values() if p['title_id'] == card['title_id']
                  and p.get('set_code') and p.get('collector_number')
                  and export_keys[(p['set_code'], p['collector_number'])] == 1]
            if not ps:
                errors.append(dict(code='no_unambiguous_export_printing', name=entry['name']))
                continue
            p = min(ps, key=lambda p: (p['set_code'], p['collector_number'], p['arena_id']))
            rows.append(dict(zone=zone, card=card, printing=p, quantity=entry['quantity']))
    if errors:
        return dict(result, violations=errors)  # Never export a partial resolved deck.
    exported = deck_text(rows)
    req = REQUESTS[request_id]
    validation = validate_text(exported, paths, pins, req['queue'])
    pair = check_pair(rows, req['color_pair'])
    errors = list(validation['violations'])
    expected_side = 15 if req['queue'] == 'BO3' else 0
    if sum(r['quantity'] for r in data['sideboard']) != expected_side:
        errors.append(dict(code='registered_sideboard_size', expected=expected_side))
    if pair['status'] == 'FAIL':
        errors.append(dict(code='registered_color_pair', details=pair))
    return dict(result, status='FAIL' if errors else 'PASS', violations=errors,
                independent_validator=validation, color_pair=pair, export_text=exported)


def fake_generate(provider, facts, paths, pins, request_id):
    """Test seam only: exactly one invocation; exceptions never cause a retry."""
    if not getattr(provider, 'is_fake', False):
        raise ValueError('Phase 0 permits only a fake generation provider')
    try:
        response = provider(build_request(facts, request_id))
    except Exception:
        return dict(status='FAIL', violations=[dict(code='provider_transport_failure')])
    if isinstance(response, dict):
        return assess_message(response, facts, paths, pins, request_id)
    return assess(response, facts, paths, pins, request_id)


def assess_message(message, facts, paths, pins, request_id):
    """A cut-off or nonfinal envelope fails even if its text happens to parse."""
    failure = dict(status='FAIL', violations=[dict(code='incomplete_or_invalid_provider_response')])
    if not isinstance(message, dict) or message.get('model') != MODEL:
        return failure
    if message.get('stop_reason') != 'end_turn':
        return dict(status='FAIL', violations=[dict(code='nonfinal_stop_reason',
                                                  stop_reason=message.get('stop_reason'))])
    content = message.get('content')
    if not isinstance(content, list) or any(not isinstance(b, dict) for b in content):
        return failure
    texts = [b.get('text') for b in content if b.get('type') == 'text']
    if len(texts) != 1 or not isinstance(texts[0], str):
        return failure
    if any(b.get('type') not in ('text', 'thinking', 'redacted_thinking') for b in content):
        return failure
    return assess(texts[0], facts, paths, pins, request_id)
