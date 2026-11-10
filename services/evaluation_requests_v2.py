"""Representation and validation of preregistered v2 requests; no generation."""
import json
from pathlib import Path
import re

REGISTRATION = json.loads((Path(__file__).resolve().parents[1] / 'docs/evaluation_requests_v2.json').read_text(encoding='utf-8'))
REQUESTS = REGISTRATION['requests']


def represented_colors(card):
    identity = card.get('color_identity')
    colors = card.get('colors')
    if not isinstance(identity, str) or not isinstance(colors, str) or any(c not in 'WUBRG' for c in identity + colors):
        raise ValueError('missing or invalid canonical color identity/colors')
    result = set(identity + colors)
    for token in re.findall(r'\{([^{}]+)\}', (card.get('mana_cost') or '') + ' ' + (card.get('rules_text') or '')):
        result.update(set(token.split('/')) & set('WUBRG'))
    return result


def allowed(card, pair, already_eligible=True):
    if not already_eligible:
        return False
    try:
        colors = represented_colors(card)
    except ValueError:
        return False
    return colors <= set(pair) if pair else True


def check_pair(rows, pair):
    """Rows carry card, quantity and zone; caller independently validates legality."""
    if pair is None:
        return dict(status='not applicable', counts={}, violations=[])
    if pair not in ('WU', 'BR', 'GW'):
        raise ValueError('unregistered pair')
    counts = {c:0 for c in pair}; violations = []
    for row in rows:
        card = row['card']
        if not allowed(card,pair):
            violations.append(dict(title_id=card['title_id'],reason='outside pair or missing identity'))
        elif row['zone']=='main' and 'Land' not in (card.get('types') or '').split():
            for color in represented_colors(card):
                counts[color] += row['quantity']
    for color,n in counts.items():
        if n<8:
            violations.append(dict(color=color,actual=n,minimum=8))
    return dict(status='FAIL' if violations else 'PASS',counts=counts,violations=violations)
