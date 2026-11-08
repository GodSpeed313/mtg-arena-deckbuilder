"""Opt-in, dated Arena Standard reference snapshots. No engine defaults change."""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3

VERSION = 1
SET_DISAGREEMENT = 'source disagreement: Scryfall (2026-11-05) base-legal vs Arena format legal-set list (2026-09-12)'
FINAL_AUTHORITY = "Arena remains the final authority for deck import and queue eligibility. Successful import alone does not establish BO1 or BO3 legality."
DETERMINATION = """## Operator determination (recorded as such, not as data-verified evidence)

I determine:
- **BO1 (Arena Standard, Best-of-One)** uses the current.db format record `Standard`.
- **BO3 (Arena Traditional Standard, Best-of-Three)** uses the current.db format record `TraditionalStandard`.

Evidence relied on:
1. Wizards of the Coast, "MTG Arena Banned and Restricted Announcement," October 22, 2024 (magic.wizards.com), which names "Traditional Standard" as the Best-of-Three format and distinguishes it from Arena Standard Best-of-One.
2. current.db (SHA-256 9f8761632230a93f58aa47d77d120cda2c5be76354d787045202ed0e01e7f3ef) contains exactly one Standard record and one TraditionalStandard record, as of September 12, 2026; their banned-title lists differ by one title.
3. Run 20C's preserved client log shows separate events `Play` and `Constructed_BestOf3`.

**Scope of the determination:** it establishes queue-to-format binding for the purpose of Run 20B gate criterion 1 only, for both queues. It does not establish current legality, resolve card identity conflicts, override or add restrictions, or waive criteria 2–5.

It must be recorded in the snapshot's provenance as `overlay_binding: operator_determination`, with the three evidence items above, the determination date, and the statement that the binding is not independently verified from Arena data. Do not describe it anywhere as verified."""
EVIDENCE = [
    'Operator-cited Wizards of the Coast, "MTG Arena Banned and Restricted Announcement," October 22, 2024 (magic.wizards.com); not fetched or independently checked in this run.',
    'Pinned current.db, September 12, 2026: exactly one Standard and one TraditionalStandard record; ban lists differ by one title.',
    'Run 20C preserved client log: separate Play and Constructed_BestOf3 events; no independently established queue binding.',
]
PINS = {
    'scryfall': 'f14ba1be53563ee231f9c13f83edf656027dcf331028ebf78149c92fbc44a021',
    'candidate': '88d9555f6d159be8e589b6bba181dbaed5480b05ebf13f04bb5c603ebaf679c6',
    'arena': '1be0105411ad989ca60e233f1341eaaa9276e3a679c89f1ef6f07e07ede10d25',
    'formats': '9f8761632230a93f58aa47d77d120cda2c5be76354d787045202ed0e01e7f3ef',
}
AS_OF = {'scryfall': '2026-11-05 22:06:50 (filename-derived; timezone and API updated_at not independently established)',
         'candidate': '2026-10-24 preserved inventory', 'arena': '2026-10-24 preserved inventory',
         'formats': '2026-09-12'}


class LegalityError(ValueError):
    pass


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def sha256(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def readonly(path):
    con = sqlite3.connect(Path(path).resolve(strict=True).as_uri() + '?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA query_only=1')
    if con.execute('PRAGMA query_only').fetchone()[0] != 1:
        con.close()
        raise LegalityError('query_only enforcement failed')
    return con


def bindings():
    return {q: {'format': name, 'overlay_binding': 'operator_determination',
                'determination_date': '2026-11-06', 'determination_verbatim': DETERMINATION,
                'evidence': list(EVIDENCE),
                'statement': 'The binding is not independently verified from Arena data.'}
            for q, name in [('BO1', 'Standard'), ('BO3', 'TraditionalStandard')]}


def verify_inputs(paths, expected):
    if set(paths) != set(PINS) or set(expected) != set(PINS):
        raise LegalityError('all four input pins are required')
    actual = {k: sha256(v) for k, v in paths.items()}
    if actual != expected:
        raise LegalityError('input hash mismatch')
    return actual


def _overlay(con, name):
    rows = list(con.execute('SELECT * FROM formats WHERE name=?', (name,)))
    if len(rows) != 1:
        raise LegalityError('missing or ambiguous format record')
    record = dict(rows[0])
    # Unknown restriction semantics cannot be silently ignored.
    for key in ('uses_rebalanced', 'card_count_restriction_internal',
                'sideboard_behavior_internal'):
        if record.get(key) not in (None, 0):
            raise LegalityError('unsupported format restriction: ' + key)
    if record.get('color_restrictions_internal') not in (None, '', '[]'):
        raise LegalityError('unsupported color restriction')
    rules = [dict(r) for r in con.execute(
        'SELECT title_id,rule FROM format_title_rules WHERE format_name=? ORDER BY title_id,rule', (name,))]
    if any(r['rule'] not in ('banned', 'suspended', 'suppressed', 'allowed') for r in rules):
        raise LegalityError('unsupported title restriction')
    for table in ('format_card_quotas', 'format_rarity_quotas', 'format_commander_allowlist'):
        if con.execute('SELECT count(*) FROM ' + table + ' WHERE format_name=?', (name,)).fetchone()[0]:
            raise LegalityError('unsupported quota/commander restriction')
    sets = sorted(r[0] for r in con.execute('SELECT set_code FROM format_sets WHERE format_name=?', (name,)))
    if not sets:
        raise LegalityError('missing set eligibility evidence')
    return {'record': record, 'rules': rules, 'legal_sets': sets}


def assess(paths, expected, queue_bindings, queues):
    """Read pinned inputs; classify all titles. No writes, secondary joins or ownership."""
    hashes = verify_inputs(paths, expected)
    queues = sorted(set(queues))
    authorized = bindings()
    if not queues or any(q not in authorized or queue_bindings.get(q) != authorized[q] for q in queues):
        raise LegalityError('missing, ambiguous or unsupported queue binding')
    with closing(readonly(paths['candidate'])) as con:
        cards = {r['title_id']: dict(r) for r in con.execute('SELECT title_id,name FROM cards')}
        printings = {r['arena_id']: dict(r) for r in con.execute('SELECT arena_id,title_id,set_code FROM printings')}
    by_title = defaultdict(list)
    for aid, p in printings.items():
        if p['title_id'] not in cards:
            raise LegalityError('orphan printing')
        by_title[p['title_id']].append(aid)
    with closing(readonly(paths['arena'])) as con:
        raw = {r['GrpId']: dict(r) for r in con.execute('SELECT GrpId,TitleId,Types,Supertypes,IsPrimaryCard,IsToken,IsRebalanced FROM Cards')}
        loc = {r['LocId']: r['Loc'] for r in con.execute('SELECT LocId,Loc FROM Localizations_enUS WHERE Formatted=1')}
        enums = {(r['Type'], r['Value']): (r['LocId'], loc.get(r['LocId'])) for r in con.execute('SELECT * FROM Enums')}
    def typed(kind, text):
        try:
            return [dict(value=int(v), loc_id=enums.get((kind, int(v)), (None, None))[0],
                         text=enums.get((kind, int(v)), (None, None))[1]) for v in (text or '').split(',') if v]
        except ValueError:
            return []
    basics = {}
    for tid, ids in by_title.items():
        facts = []
        for aid in sorted(ids):
            r = raw.get(aid, {})
            types, supers = typed('CardType', r.get('Types')), typed('SuperType', r.get('Supertypes'))
            if r.get('TitleId') != tid or 'Land' not in [v['text'] for v in types] or 'Basic' not in [v['text'] for v in supers]:
                break
            facts.append(dict(arena_id=aid, types=types, supertypes=supers))
        if facts and len(facts) == len(ids):
            basics[tid] = facts
    with closing(readonly(paths['formats'])) as con:
        overlays = {q: _overlay(con, queue_bindings[q]['format']) for q in queues}
        format_meta = {r[0]: r[1] for r in con.execute("SELECT key,value FROM meta WHERE key LIKE 'formats%'")}
    exact = defaultdict(list)
    arena_oracles, no_id_oracles = set(), set()
    no_id_rows = []
    seen_ids = set()
    with gzip.open(paths['scryfall'], 'rt', encoding='utf-8') as stream:
        for line in stream:
            x = json.loads(line)
            if not isinstance(x, dict) or not isinstance(x.get('id'), str) or x['id'] in seen_ids:
                raise LegalityError('malformed or duplicate Scryfall record id')
            seen_ids.add(x['id'])
            aid, oid = x.get('arena_id'), x.get('oracle_id')
            if aid is not None and type(aid) is not int:
                raise LegalityError('malformed Arena id')
            state = x.get('legalities', {}).get('standard')
            if state == 'legal' and oid:
                if aid is not None:
                    arena_oracles.add(oid)
                elif 'arena' in x.get('games', []):
                    no_id_oracles.add(oid)
                    no_id_rows.append({'id': x['id'], 'oracle_id': oid, 'status': 'unknown', 'reason': 'no_arena_id'})
            if aid in printings:
                exact[aid].append(dict(id=x['id'], oracle_id=oid, arena_id=aid,
                                       name=x.get('name'), standard=state, games=x.get('games', [])))
    title_oracles, oracle_titles = defaultdict(set), defaultdict(set)
    for aid, xs in exact.items():
        tid = printings[aid]['title_id']
        for x in xs:
            if x['oracle_id']:
                title_oracles[tid].add(x['oracle_id'])
                oracle_titles[x['oracle_id']].add(tid)
    conflict_titles = {tid for tid, os in title_oracles.items() if len(os) > 1}
    for tids in oracle_titles.values():
        if len(tids) > 1:
            conflict_titles.update(tids)
    titles = {}
    for tid, card in sorted(cards.items()):
        ids = sorted(by_title[tid])
        matches = sorted((x for aid in ids for x in exact[aid]), key=lambda x: x['id'])
        conflicts = []
        if tid in conflict_titles:
            conflicts.append('identity_cardinality_conflict')
        for aid in ids:
            if len({(x['oracle_id'], x['standard'], x['name']) for x in exact[aid]}) > 1:
                conflicts.append('duplicate_id_conflict:' + str(aid))
        if not ids or any(raw.get(aid, {}).get('TitleId') != tid for aid in ids):
            conflicts.append('arena_identity_unresolved')
        states = {x['standard'] for x in matches}
        if len(states) > 1:
            conflicts.append('exact_match_legality_disagreement')
        if not matches:
            conflicts.append('no_exact_match')
        if states and not states <= {'legal', 'not_legal', 'banned', 'restricted'}:
            conflicts.append('missing_or_unrecognized_legality')
        if any(not x['oracle_id'] for x in matches):
            conflicts.append('missing_reference_card_identity')
        if any('arena' not in x['games'] for x in matches):
            conflicts.append('arena_availability_unestablished')
        basic = tid in basics
        if basic:
            state, reason = 'legal', 'arena_verified_basic_land'
        elif conflicts:
            state, reason = 'unknown', 'no_exact_match' if conflicts == ['no_exact_match'] else 'conflict'
        elif states == {'legal'}:
            state, reason = 'legal', 'clean_exact_base_legal'
        elif states and states <= {'not_legal', 'banned', 'restricted'}:
            state, reason = 'illegal', 'clean_exact_base_illegality'
        else:
            state, reason = 'unknown', 'no_exact_match'
        per_queue = {}
        for q in queues:
            overlay = overlays[q]
            restrictions = [dict(format=queue_bindings[q]['format'], rule=r['rule'], title_id=tid,
                                 source='formats', as_of=AS_OF['formats']) for r in overlay['rules']
                            if r['title_id'] == tid and r['rule'] in ('banned', 'suspended', 'suppressed')]
            allowed = any(r['title_id'] == tid and r['rule'] == 'allowed' for r in overlay['rules'])
            title_sets = sorted({printings[aid]['set_code'] for aid in ids})
            disagreements = []
            if state == 'legal' and not basic and not allowed and not set(title_sets).intersection(overlay['legal_sets']):
                # A dated set-list omission is not an affirmative title restriction.
                # This schema supplies neither exhaustive-set semantics nor evidence
                # that the omission applies across the sources' different dates.
                # Preserve the competing claims without manufacturing either fact.
                disagreements.append(dict(
                    reason=SET_DISAGREEMENT,
                    base_claim=dict(source='scryfall', as_of=AS_OF['scryfall'], status='legal',
                                    exact_record_ids=[x['id'] for x in matches]),
                    overlay_claim=dict(source='formats', format=queue_bindings[q]['format'],
                                       as_of=AS_OF['formats'], title_sets=title_sets,
                                       legal_sets=overlay['legal_sets'], observation='no canonical printing set in recorded list'),
                    restrictive_semantics='unestablished', applicability='unestablished'))
            effective = state
            why = reason
            if state != 'unknown' and not basic and restrictions:
                effective, why = 'illegal', 'overlay_restriction'
            elif disagreements:
                effective, why = 'unknown', SET_DISAGREEMENT
            per_queue[q] = dict(status=effective, reason=why, source_restrictions=restrictions,
                                source_disagreements=disagreements)
        titles[str(tid)] = dict(name=card['name'], arena_ids=ids, base_state=state, base_reason=reason,
                               basic=basic, basic_evidence=basics.get(tid, []), source_conflicts=sorted(set(conflicts)),
                               exact_matches=matches, queues=per_queue)
    verify_inputs(paths, hashes)
    return dict(titles=titles, printing_to_title={str(k): v['title_id'] for k, v in sorted(printings.items())},
                queues={q: dict(provenance=queue_bindings[q], overlay=overlays[q],
                               counts=dict(sorted(Counter(t['queues'][q]['status'] for t in titles.values()).items()))) for q in queues},
                coverage=dict(canonical_titles=len(cards), canonical_printings=len(printings),
                              base_unknown_titles=sum(t['base_state'] == 'unknown' for t in titles.values()),
                              standard_legal_arena_id_oracles=len(arena_oracles),
                              standard_legal_no_arena_id_only_oracles=len(no_id_oracles - arena_oracles),
                              no_arena_id_evidence=sorted(no_id_rows, key=lambda x: x['id']),
                              secondary_matching='not performed or admitted; see Run 20B assessment'),
                format_source_metadata=format_meta)


def build_snapshot(paths, expected, queue_bindings, queues, output, *, generated_at=None):
    """Publish one immutable JSON file in a new external directory after complete assessment."""
    output = Path(output).resolve()
    repo = Path(__file__).resolve().parents[1]
    if output == repo or repo in output.parents or output.exists():
        raise LegalityError('output must be a new directory outside the repository')
    content = assess(paths, expected, queue_bindings, queues)
    payload = dict(version=VERSION, sources={k: dict(sha256=expected[k], as_of=AS_OF[k]) for k in sorted(expected)},
                   final_authority=FINAL_AUTHORITY, determination_verbatim=DETERMINATION,
                   scope='Dated, bounded reference pool; not current queue verification or a deck validator.',
                   staleness_policy=dict(warn_always=True, overlay_as_of='2026-09-12',
                       overlay_scryfall_gap_days=54, overlay_log_gap_days=55,
                       message='Mixed dated sources; intervening restriction changes cannot be detected offline. No refresh cadence or current-legality guarantee.',
                       refresh='Re-pin sources, re-assess each queue, validate, publish a new directory; retain prior valid snapshot on refresh failure.'),
                   **content)
    body = encoded(payload)
    digest = hashlib.sha256(body.encode('utf-8')).hexdigest()
    document = dict(generated_at_utc=generated_at or datetime.now(timezone.utc).isoformat(),
                    snapshot_version='standard-legality-v1-' + digest, payload_sha256=digest, payload=payload)
    verify_inputs(paths, expected)
    output.mkdir(parents=True, exist_ok=False)
    path = output / 'snapshot.json'
    with path.open('x', encoding='utf-8', newline='\n') as f:
        f.write(encoded(document) + '\n')
    return path


def load_snapshot(path):
    """Read-only checksum/contract validation; no source DB or network access."""
    with Path(path).open(encoding='utf-8') as f:
        doc = json.load(f)
    payload = doc.get('payload', {})
    digest = hashlib.sha256(encoded(payload).encode('utf-8')).hexdigest()
    if (payload.get('version') != VERSION or digest != doc.get('payload_sha256')
            or doc.get('snapshot_version') != 'standard-legality-v1-' + digest
            or payload.get('final_authority') != FINAL_AUTHORITY
            or payload.get('determination_verbatim') != DETERMINATION):
        raise LegalityError('invalid or incompatible snapshot')
    if not payload.get('queues') or any(q not in bindings() or v.get('provenance') != bindings()[q]
                                      for q, v in payload['queues'].items()):
        raise LegalityError('invalid queue provenance')
    return doc


def query(path, queue, *, title_id=None, arena_id=None):
    doc = load_snapshot(path)
    p = doc['payload']
    if queue not in p['queues']:
        raise LegalityError('queue not present in this snapshot')
    if (title_id is None) == (arena_id is None) or any(type(v) is not int for v in (title_id, arena_id) if v is not None):
        raise LegalityError('supply exactly one integer title_id or arena_id')
    if arena_id is not None:
        title_id = p['printing_to_title'].get(str(arena_id))
    t = p['titles'].get(str(title_id))
    result = dict(t['queues'][queue]) if t else dict(status='unknown', reason='identity_not_in_snapshot', source_restrictions=[])
    return dict(queue=queue, title_id=title_id, arena_id=arena_id, **result,
                source_conflicts=t['source_conflicts'] if t else [],
                provenance=p['queues'][queue]['provenance'], sources=p['sources'],
                snapshot_version=doc['snapshot_version'], staleness_policy=p['staleness_policy'],
                final_authority=FINAL_AUTHORITY)
