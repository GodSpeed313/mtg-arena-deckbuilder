"""Symbol and markup normalization.

This is the *only* place raw Arena text is cleaned. Query predicates, deck
logic, export and UI all consume normalized text and never re-parse. Doing it
anywhere else is how you end up with `{oG}`, `{G}` and `green` all meaning the
same thing in different modules.
"""

from __future__ import annotations

import re

# Arena encodes mana symbols with an 'o' prefix, in two contexts:
#   costs:      'o4oG'          (bare, from Cards.OldSchoolManaText)
#   rules text: '{oT}: Add {oG}.'  (brace-wrapped, and a single pair of
#                                   braces may hold several symbols: '{oBoB}')
_MANA_TOKEN = re.compile(r"o(\([^)]*\)|\d+|[A-Za-z])")
_BRACED = re.compile(r"\{([^}]*)\}")

# UI markup: <nobr>, <i>, <b>, <s>, <sup>, <cspace>, <sprite .../>. Tags are
# stripped and inner content kept. <sprite name="arena_a"> is the Alchemy
# rebalance badge and holds no text -- dropping it loses nothing, since
# Cards.IsRebalanced already carries that fact.
_TAG = re.compile(r"<[^>]+>")

# Arena uses a private-use glyph before attribution lines in flavor text.
_PUA = re.compile(r"[-]")


def parse_mana(old_school: str) -> list[str]:
    """'o2o(G/W)oU' -> ['2', 'G/W', 'U']."""
    return [t.strip("()") for t in _MANA_TOKEN.findall(old_school or "")]


def mana_cost_string(symbols: list[str]) -> str:
    """['2', 'G/W'] -> '{2}{G/W}'."""
    return "".join("{" + s + "}" for s in symbols)


def cmc(symbols: list[str]) -> int:
    """Converted mana cost. X counts 0; hybrid takes its larger half."""
    total = 0
    for s in symbols:
        if s.isdigit():
            total += int(s)
        elif "/" in s:
            # {2/R} costs 2; {G/W} and phyrexian {B/P} cost 1.
            numeric = [p for p in s.split("/") if p.isdigit()]
            total += int(numeric[0]) if numeric else 1
        elif s.upper() == "X":
            continue
        else:
            total += 1
    return total


def normalize_cost(old_school: str) -> str:
    """'o4oG' -> '{4}{G}'."""
    return mana_cost_string(parse_mana(old_school))


def _expand_braced(inner: str) -> str:
    """'oBoB' -> '{B}{B}'; 'oT' -> '{T}'; anything else passes through."""
    symbols = parse_mana(inner)
    if not symbols:
        return "{" + inner + "}"
    return mana_cost_string(symbols)


# Invisible formatting characters: zero-width spaces/joiners, bidi marks, BOM.
# These are not textual differences, but they defeat equality comparison. Two
# printings of Smuggler's Copter differ only by a stray U+200E, which would
# otherwise be reported as a genuine rules-text conflict.
_INVISIBLE = re.compile("[​-‏⁠﻿]")


def strip_markup(text: str) -> str:
    """Remove UI tags, keep their content, tidy the leftovers."""
    if not text:
        return ""
    out = _TAG.sub("", text)
    out = _PUA.sub("", out)
    out = _INVISIBLE.sub("", out)
    # Tag removal can leave doubled spaces mid-sentence.
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def normalize_text(text: str) -> str:
    """Full cleanup for rules text: markup stripped, symbols normalized."""
    if not text:
        return ""
    return strip_markup(_BRACED.sub(lambda m: _expand_braced(m.group(1)), text))


# Arena stores colours as comma-separated enum ids, not letters, and does not
# order them canonically: Boros Charm is colors='1,4' but identity='4,1'.
# Decoding to a WUBRG-ordered string makes equality and LIKE both meaningful.
_COLOR_ENUM = {1: "W", 2: "U", 3: "B", 4: "R", 5: "G"}
WUBRG = "WUBRG"


def normalize_colors(raw: str | None) -> str:
    """'4,1' -> 'WR'. Empty stays empty, which is how colorless is stored."""
    if not raw:
        return ""
    letters = {
        _COLOR_ENUM[int(tok)]
        for tok in str(raw).split(",")
        if tok.strip().isdigit() and int(tok) in _COLOR_ENUM
    }
    return "".join(c for c in WUBRG if c in letters)


def color_key(colors: list[str]) -> str:
    """Canonical WUBRG-ordered key for a requested colour set."""
    wanted = {c.upper() for c in colors}
    return "".join(c for c in WUBRG if c in wanted)


def self_reference_names(name: str) -> list[str]:
    """Names a card might use to refer to itself, most specific first.

    Arena shortens self-references unpredictably: 'Sheoldred, the Apocalypse'
    becomes 'Sheoldred' (comma), but 'Loran of the Third Path' becomes 'Loran'
    (no comma to split on). Splitting only on the comma misses the second form,
    so offer the full name, the pre-comma portion, and the first word, and let
    the caller take the longest that matches.
    """
    clean = strip_markup(name)
    candidates = [clean, clean.split(",")[0].strip(), clean.split(" ")[0].strip()]
    seen: list[str] = []
    for c in candidates:
        if c and c not in seen:
            seen.append(c)
    return seen


def short_name(name: str) -> str:
    """The most specific self-reference candidate. See self_reference_names."""
    return self_reference_names(name)[0]
