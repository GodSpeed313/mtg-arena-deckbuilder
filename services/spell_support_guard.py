"""Frozen Run 27 heuristic; ordinary casts, not a game-state simulator."""
from fractions import Fraction
import re

VERSION = 'run27-support.1'
WEIGHTS = {'Proactive': Fraction(1), 'Conditional': Fraction(2, 5),
           'Unknown': Fraction(2, 5), 'Unsupported': Fraction(0)}


def cost_factor(mana_value):
    return Fraction(1) if mana_value <= 2 else Fraction(3, 4) if mana_value == 3 else Fraction(1, 2)


def bonus(score):
    """Exact nonnegative half-up rounding, never binary float or round()."""
    value = 12 * Fraction(score) / 11 + Fraction(1, 2)
    return min(12, value.numerator // value.denominator)


def casting_category(card):
    """Closed whole-text grammar. Unrecognized prerequisites stay Unknown.

    Removal is Conditional by registration, even when targeting one's own
    creature would technically be possible. Conditional discounts are ignored.
    Creature pump/protection may use the deployed payoff itself as its target.
    """
    text = (card.get('rules_text') or '').strip()
    types = set(card.get('types', '').split())
    if types & {'Creature', 'Land'}:
        return 'Unsupported'
    # These positive patterns describe the entire card, not a substring that
    # could hide an additional casting/target prerequisite.
    proactive = (
        r'Draw (?:a card|two cards|three cards)\.',
        r'Scry [1-3]\.',
        r'Target creature(?: you control)? gets \+\d+/\+\d+ until end of turn\.',
        r'Target creature you control gains (?:hexproof|indestructible) until end of turn\.',
    )
    if types & {'Instant', 'Sorcery'} and any(re.fullmatch(p, text) for p in proactive):
        return 'Proactive'
    if types & {'Artifact', 'Enchantment', 'Planeswalker', 'Battle'} and 'Aura' not in card.get('subtypes', '').split():
        if not text:
            return 'Proactive'
        # A bounded ETB draw plus ordinary mana activation has no extra cast
        # requirement; neither ability is itself counted as a spell cast.
        if re.fullmatch(r'When this (?:artifact|enchantment) enters, draw a card\.\n\{1\}, \{T\}: Add one mana of any color\.', text):
            return 'Proactive'
    if types & {'Instant', 'Sorcery'}:
        # Target/stack restrictions remain Conditional even when the rest of
        # the card is not understood; they can never establish Proactive.
        if re.search(r'\b(?:Counter target .*spell|(?:Destroy|Exile) target (?:\w+ )*(?:creature|artifact|enchantment))\.', text):
            return 'Conditional'
        if re.search(r'\btarget (?:tapped|attacking|blocking) creature\b', text, re.I):
            return 'Conditional'
    return 'Unknown'


def classify_support(card, *, qualifies, zone='main', event='cast'):
    if not qualifies or zone != 'main' or event != 'cast':
        return 'Unsupported'
    return card.get('support_category') or casting_category(card)


def assess(selected, cards, qualifying_ids):
    """Assess exact compatible main-deck support; report raw and capped counts."""
    counts = dict.fromkeys(WEIGHTS, 0)
    capped = dict.fromkeys(WEIGHTS, 0)
    score = Fraction(0)
    details = []
    for tid, quantity in sorted(selected.items()):
        if not quantity:
            continue
        card = cards[tid]
        category = classify_support(card, qualifies=tid in qualifying_ids)
        copies = min(quantity, 4)
        counts[category] += quantity
        capped[category] += copies
        factor = cost_factor(card['cmc'])
        contribution = copies * WEIGHTS[category] * factor
        score += contribution
        details.append(dict(title_id=tid, name=card['name'], copies=quantity,
                            capped_copies=copies, category=category,
                            mana_value=card['cmc'], weight=str(WEIGHTS[category]),
                            cost_factor=str(factor), support=str(contribution)))
    return dict(version=VERSION, threshold=11, S=str(score), bonus=bonus(score),
                counts=counts, capped_counts=capped, cards=details,
                scope='Selected main-deck ordinary casts; availability not established')
