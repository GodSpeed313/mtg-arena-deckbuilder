"""Pinned read-only facts and conservative construction exclusions."""
from contextlib import closing
from pathlib import Path
import re

from mtgadb.standard_legality import load_snapshot, readonly, sha256

SNAPSHOT = 'C:/MTG-Source-Preservation/legality/run20e-20261107-final/snapshot.json'
DATABASE = 'C:/MTG-Source-Preservation/candidates/2026-10-24-r1/canonical-candidate.db'
ARENA = 'C:/MTG-Source-Preservation/2026-10-24-r1/arena/Raw_CardDatabase_c344d0dccdfb898a5e3fd4101eedbd48.mtga'
PINS = {'snapshot': 'dbe99b964ecfde659978e38f77f7ade5a97e42c442ee245b020413f96ea300d4',
        'database': '88d9555f6d159be8e589b6bba181dbaed5480b05ebf13f04bb5c603ebaf679c6',
        'arena': '1be0105411ad989ca60e233f1341eaaa9276e3a679c89f1ef6f07e07ede10d25'}
MARKERS = ('a deck can have', 'your deck can have', 'companion', 'starting deck',
           'deck construction', 'deckbuilding', 'deck-building', 'build your deck',
           'building your deck', 'deck must contain', 'deck may contain', 'deck can contain')
COLORS = 'WUBRG'


def special(text):
    return [m for m in MARKERS if m in (text or '').casefold()]


def read_facts(paths, pins, queue):
    if set(paths) != set(PINS) or set(pins) != set(PINS):
        raise ValueError('all three pinned sources required')
    hashes = {k: sha256(v) for k, v in paths.items()}
    if hashes != pins:
        raise ValueError('input hash mismatch')
    snapshot = load_snapshot(paths['snapshot'])
    if queue not in snapshot['payload']['queues']:
        raise ValueError('queue absent from snapshot')
    with closing(readonly(paths['database'])) as c:
        cards = {r['title_id']: dict(r) for r in c.execute('SELECT * FROM cards')}
        printings = {r['arena_id']: dict(r) for r in c.execute('SELECT * FROM printings')}
    with closing(readonly(paths['arena'])) as c:
        loc = {r['LocId']: r['Loc'] for r in c.execute('SELECT * FROM Localizations_enUS WHERE Formatted=1')}
        enums = {(r['Type'], r['Value']): loc.get(r['LocId']) for r in c.execute('SELECT * FROM Enums')}
        raw = {r['GrpId']: dict(r) for r in c.execute('SELECT GrpId,TitleId,Types,Supertypes FROM Cards')}
    basic = set()
    for aid, r in raw.items():
        try:
            types = {enums.get(('CardType', int(v))) for v in (r['Types'] or '').split(',') if v}
            supers = {enums.get(('SuperType', int(v))) for v in (r['Supertypes'] or '').split(',') if v}
        except ValueError:
            continue
        if 'Land' in types and 'Basic' in supers and aid in printings and printings[aid]['title_id'] == r['TitleId']:
            basic.add(aid)
    if {k: sha256(v) for k, v in paths.items()} != hashes:
        raise ValueError('inputs changed during read')
    return dict(cards=cards, printings=printings, basic=basic, snapshot=snapshot,
                provenance={k: dict(path=str(paths[k]), sha256=hashes[k]) for k in sorted(paths)})


def symbols(cost):
    tokens = re.findall(r'\{([^{}]+)\}', cost or '')
    if not tokens or ''.join('{' + t + '}' for t in tokens) != cost:
        raise ValueError('missing or unsupported mana cost')
    if any(not (t.isdigit() or t in COLORS + 'C') for t in tokens):
        raise ValueError('unsupported mana symbols')
    return tokens
