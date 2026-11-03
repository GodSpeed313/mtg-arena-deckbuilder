"""Arena structural facts v1. No canonical/recognition imports or ownership model."""
from __future__ import annotations

from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time

VERSION = 1
TABLE_KEYS = {
    'Cards': ('GrpId',), 'Abilities': ('Id',),
    'Localizations_enUS': ('LocId', 'Formatted'), 'Enums': ('Type', 'Value'),
    'AltPrintings': ('AltGrpId',), 'AltToBasePrintings': ('BaseGrpId', 'AltGrpId'),
    'Versions': ('Type',),
}
REQUIRED = {
    'Cards': 'GrpId TitleId IsPrimaryCard LinkedFaceGrpIds LinkedFaceType Types Subtypes Colors IndicatorColors ColorIdentity AbilityIds HiddenAbilityIds IsRebalanced RebalancedCardGrpId'.split(),
    'Abilities': 'Id TextId LoyaltyCost BaseId BaseIdNumeral'.split(),
    'Localizations_enUS': ['LocId', 'Formatted', 'Loc'],
    'Enums': ['Type', 'Value', 'LocId'],
    'AltPrintings': ['AltGrpId'], 'AltToBasePrintings': ['BaseGrpId', 'AltGrpId'],
    'Versions': ['Type', 'Version'],
}
FACT_TABLES = ('source_records', 'components', 'links', 'entries', 'diagnostics')


class ExtractionError(ValueError):
    """Invalid input or destination; no validated partial snapshot is published."""

    def __init__(self, message, measurements=None):
        super().__init__(message)
        self.measurements = measurements


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def sha256(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def readonly(path):
    con = sqlite3.connect(Path(path).resolve(strict=True).as_uri() + '?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA query_only=ON')
    if con.execute('PRAGMA query_only').fetchone()[0] != 1:
        con.close()
        raise ExtractionError('query_only enforcement failed')
    return con


def payload_digest(con):
    digest = hashlib.sha256()
    for table in FACT_TABLES:
        for row in con.execute(f'SELECT * FROM {table} ORDER BY 1,2'):
            digest.update((encoded([table, list(row)]) + '\n').encode('utf-8'))
    return digest.hexdigest()


def validate(path):
    """Read-only integrity, provenance and deterministic payload verification."""
    with closing(readonly(path)) as con:
        if con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ExtractionError('snapshot integrity check failed')
        meta = {r[0]: json.loads(r[1]) for r in con.execute('SELECT * FROM metadata')}
        if (con.execute('PRAGMA user_version').fetchone()[0] != VERSION
                or meta.get('schema_version') != VERSION
                or meta.get('contract_version') != VERSION
                or meta.get('state') != 'complete'):
            raise ExtractionError('incomplete or incompatible snapshot')
        for key in ('implementation_sha256', 'source_sha256', 'manifest_sha256',
                    'arena_version', 'extracted_at_utc'):
            if not meta.get(key):
                raise ExtractionError('missing provenance: ' + key)
        if payload_digest(con) != meta['payload_sha256']:
            raise ExtractionError('factual payload digest mismatch')
        counts = {t: con.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in FACT_TABLES}
        if counts != meta['table_counts']:
            raise ExtractionError('snapshot count mismatch')
        return meta


def _tokens(value):
    if value is None or value == '':
        return []
    return str(value).split(',')


def _integer(value):
    return int(value) if re.fullmatch(r'[0-9]+', str(value)) else None


def _cycle_nodes(graph):
    # For each edge, seek a return path with at least one intermediate node.
    # Suppressing only the immediate reciprocal return admits ordinary pairs
    # and reciprocal trees, while still finding cycles across DFS cross edges.
    result = {node for node, targets in graph.items() if node in targets}
    for root, targets in graph.items():
        for first in targets:
            if first not in graph or first == root:
                continue
            parents, pending = {first: None}, [first]
            found = False
            while pending and not found:
                node = pending.pop()
                for target in graph[node]:
                    if target == root and node != first:
                        result.add(root)
                        while node is not None:
                            result.add(node)
                            node = parents[node]
                        found = True
                        break
                    if target != root and target in graph and target not in parents:
                        parents[target] = node
                        pending.append(target)
    return result


def _family(statuses, unit, unknown_eligibility=0):
    counts = Counter(statuses)
    total = len(statuses)
    recovered = counts.get('recovered', 0)
    unsupported = counts.get('unsupported_layout', 0)
    missing = sum(n for reason, n in counts.items() if reason.startswith(('missing', 'malformed', 'invalid')))
    return {'unit': unit, 'eligible': total, 'denominator': total,
            'recovered': recovered, 'unresolved': total - recovered,
            'unsupported': unsupported, 'missing_or_invalid': missing,
            'unresolved_by_reason': {k: v for k, v in sorted(counts.items()) if k != 'recovered'},
            'unknown_eligibility': unknown_eligibility,
            'recovery_percent': 100 * recovered / total if total and not unknown_eligibility else None,
            'measurement': 'NOT MEASURABLE' if unknown_eligibility else 'measured'}


def _extract(source, dest):
    for table, columns in REQUIRED.items():
        actual = {r[1] for r in source.execute(f'PRAGMA table_info("{table}")')}
        if not set(columns) <= actual:
            raise ExtractionError(f'{table}: missing required columns {sorted(set(columns)-actual)}')
    dest.executescript('''
        PRAGMA user_version=1;
        CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE source_records(table_name TEXT, source_key TEXT, raw_json TEXT NOT NULL,
                                    PRIMARY KEY(table_name,source_key));
        CREATE TABLE components(grp_id INTEGER PRIMARY KEY, facts_json TEXT NOT NULL);
        CREATE TABLE links(grp_id INTEGER, position INTEGER, target_id INTEGER,
                           kind TEXT, status TEXT, facts_json TEXT NOT NULL, PRIMARY KEY(grp_id,position));
        CREATE TABLE entries(grp_id INTEGER, position INTEGER, ability_id INTEGER,
                             chapter_index INTEGER, loyalty_cost TEXT, status TEXT,
                             facts_json TEXT NOT NULL, PRIMARY KEY(grp_id,position));
        CREATE TABLE diagnostics(scope TEXT, source_key TEXT, reason TEXT,
                                 detail_json TEXT NOT NULL);
    ''')
    rows = {}
    scanned = {}
    for table, keys in TABLE_KEYS.items():
        data = {}
        for row in source.execute(f'SELECT * FROM "{table}" ORDER BY ' + ','.join('"'+k+'"' for k in keys)):
            raw = dict(row)
            key = tuple(raw[k] for k in keys)
            if key in data or any(k is None for k in key):
                raise ExtractionError(f'{table}: duplicate/null identity {key}')
            data[key] = raw
            dest.execute('INSERT INTO source_records VALUES(?,?,?)', (table, encoded(key), encoded(raw)))
        rows[table] = data
        scanned[table] = len(data)
    cards = {k[0]: v for k, v in rows['Cards'].items()}
    abilities = {k[0]: v for k, v in rows['Abilities'].items()}
    locs = rows['Localizations_enUS']

    def loc(text_id):
        # Exact formatted source row, no text rewrite/fallback to generic text.
        row = locs.get((text_id, 1))
        return {'id': text_id, 'present': row is not None, 'text': row['Loc'] if row else None}

    def diagnostic(scope, key, reason, detail=None):
        dest.execute('INSERT INTO diagnostics VALUES(?,?,?,?)', (scope, str(key), reason, encoded(detail)))

    def enum_values(kind, raw):
        result = []
        for token in _tokens(raw):
            number = _integer(token)
            enum = rows['Enums'].get((kind, number))
            result.append({'raw': token, 'value': number, 'localization': loc(enum['LocId']) if enum else None})
        return result

    base = abilities.get(166)
    if not base or base['TextId'] != 242813 or loc(242813)['text'] != 'SagaChapter':
        raise ExtractionError('reviewed Saga base identity 166/242813/SagaChapter is not established')
    graph = {gid: [_integer(v) for v in _tokens(c['LinkedFaceGrpIds'])] for gid, c in cards.items()}
    cycles = _cycle_nodes(graph)
    link_statuses, loyalty_statuses, chapter_statuses = [], [], []
    kinds, raw_codes = Counter(), Counter()
    loyalty_faces, chapter_faces = set(), set()
    text_differences = empty_colors = entry_count = unknown_eligibility = 0
    primary = Counter()
    for gid, card in cards.items():
        primary[str(card['IsPrimaryCard'])] += 1
        color_unresolved = card['Colors'] == '' and card['ColorIdentity'] not in (None, '')
        empty_colors += color_unresolved
        component = {'source': card, 'name': loc(card['TitleId']),
                     'types': enum_values('CardType', card['Types']),
                     'subtypes': enum_values('SubType', card['Subtypes']),
                     'gameplay_color': 'unresolved' if color_unresolved else 'not_interpreted'}
        dest.execute('INSERT INTO components VALUES(?,?)', (gid, encoded(component)))
        if not component['name']['present']:
            diagnostic('component_name', gid, 'unavailable_component_name', {'TitleId':card['TitleId']})
        for field in ('types', 'subtypes'):
            if any(not e['localization'] or not e['localization']['present'] for e in component[field]):
                diagnostic('component_'+field, gid, 'unavailable_enum_localization', component[field])
        if color_unresolved:
            diagnostic('component', gid, 'empty_colors_populated_identity')
        tokens = _tokens(card['LinkedFaceGrpIds'])
        if not tokens and card['LinkedFaceType'] not in (0, None):
            # A layout-bearing face with no target remains in the denominator.
            tokens = [None]
        for position, token in enumerate(tokens):
            target_id = _integer(token)
            target = cards.get(target_id)
            kind, status = 'unknown', 'recovered'
            if token is None:
                status = 'missing_link_reference'
            elif target_id is None:
                status = 'malformed_link'
            elif target is None:
                status = 'missing_target'
            elif gid in cycles:
                status = 'invalid_cycle'
            elif gid not in graph[target_id]:
                status = 'nonreciprocal_link'
            elif len(graph[gid]) != 1 or len(graph[target_id]) != 1:
                status = 'unsupported_layout'
            else:
                signature = (card['IsPrimaryCard'], card['LinkedFaceType'], target['IsPrimaryCard'], target['LinkedFaceType'])
                if signature in ((1, 2, 0, 1), (0, 1, 1, 2)):
                    kind = 'transform'
                elif signature in ((1, 18, 0, 17), (0, 17, 1, 18)):
                    kind = 'omen'
                elif card['LinkedFaceType'] in (1, 2, 17, 18) and target['LinkedFaceType'] in (1, 2, 17, 18):
                    status = 'conflicting_layout'
                else:
                    status = 'unsupported_layout'
            fact = {'raw_target': token, 'source_code': card['LinkedFaceType'],
                    'target_code': target['LinkedFaceType'] if target else None,
                    'target_present': target is not None}
            dest.execute('INSERT INTO links VALUES(?,?,?,?,?,?)', (gid, position, target_id, kind, status, encoded(fact)))
            link_statuses.append(status)
            kinds[kind] += 1
            raw_codes[str(card['LinkedFaceType'])] += 1
            if status != 'recovered':
                diagnostic('link', f'{gid}:{position}', status, fact)
        for position, token in enumerate(_tokens(card['AbilityIds'])):
            entry_count += 1
            parts = token.split(':')
            aid, tid = (_integer(parts[0]), _integer(parts[1])) if len(parts) == 2 else (None, None)
            ability = abilities.get(aid)
            status = 'recovered'
            if aid is None or tid is None:
                status = 'malformed_ability_reference'
                unknown_eligibility += 1
            elif ability is None:
                status = 'missing_ability'
                unknown_eligibility += 1
            local = loc(tid)
            generic = loc(ability['TextId']) if ability else None
            if status == 'recovered' and (not local['present'] or not generic['present']):
                status = 'missing_localization'
            loyalty = ability['LoyaltyCost'] if ability else None
            chapter = ability['BaseId'] == 166 if ability else False
            chapter_index = ability['BaseIdNumeral'] if chapter else None
            if chapter and (type(chapter_index) is not int or chapter_index < 1):
                status = 'invalid_chapter_index'
            # Variable costs (e.g. -X) are source facts too, not numeric failures.
            if loyalty is not None and not isinstance(loyalty, str):
                status = 'invalid_loyalty_cost'
            if loyalty is not None:
                loyalty_statuses.append(status)
                loyalty_faces.add(gid)
            if chapter:
                chapter_statuses.append(status)
                chapter_faces.add(gid)
            if local['present'] and generic and generic['present'] and local['text'] != generic['text']:
                text_differences += 1
            fact = {'raw_pair': token, 'card_local': local, 'generic': generic,
                    'ability_present': ability is not None, 'ability': ability,
                    'is_chapter': chapter, 'chapter_index': chapter_index}
            dest.execute('INSERT INTO entries VALUES(?,?,?,?,?,?,?)',
                         (gid, position, aid, chapter_index, loyalty, status, encoded(fact)))
            if status != 'recovered':
                diagnostic('entry', f'{gid}:{position}', status, {'raw_pair': token})
    families = {'links': _family(link_statuses, 'directed link slots (including missing-reference sentinel)'),
                'loyalty': _family(loyalty_statuses, 'visible ability entries with non-null LoyaltyCost', unknown_eligibility),
                'chapters': _family(chapter_statuses, 'visible ability entries with BaseId=166', unknown_eligibility)}
    reason_counts = Counter(r[0] for r in dest.execute('SELECT reason FROM diagnostics'))
    link_records = len(link_statuses)
    unknown_codes = sorted({c['LinkedFaceType'] for c in cards.values()
                            if c['LinkedFaceType'] not in (0, 1, 2, 17, 18)}, key=str)
    # Rates never mix cards, directed link slots and ability entries.
    unresolved_components = len({r[0] for r in dest.execute(
        "SELECT source_key FROM diagnostics WHERE scope LIKE 'component%'")})
    unresolved_entries = sum(v for k,v in Counter(r[0] for r in dest.execute('SELECT status FROM entries')).items()
                             if k != 'recovered')
    return {'source_rows_scanned': scanned, 'cards': len(cards), 'primary_flags': dict(primary),
            'raw_records_retained': sum(scanned.values()),
            'derived_fact_rows': {'components':len(cards), 'directed_link_slots':link_records, 'visible_entries':entry_count},
            'unresolved_by_unit': {
                'components': {'count':unresolved_components,'denominator':len(cards)},
                'directed_link_slots': {'count':families['links']['unresolved'],'denominator':link_records},
                'visible_entries': {'count':unresolved_entries,'denominator':entry_count}},
            'unknown_layout_codes': unknown_codes,
            'link_diagnostics': {k:reason_counts[k] for k in (
                'missing_target','missing_link_reference','nonreciprocal_link','malformed_link',
                'invalid_cycle','conflicting_layout','unsupported_layout')},
            'linked_distinct_source_records': len({r[0] for r in dest.execute('SELECT grp_id FROM links')}),
            'reviewed_undirected_pairs': sum(1 for r in dest.execute(
                "SELECT grp_id,target_id FROM links WHERE status='recovered'") if r[0]<r[1]),
            'linked_slots_by_kind': dict(sorted(kinds.items())),
            'linked_slots_by_raw_code': dict(sorted(raw_codes.items())),
            'card_records_by_raw_code': dict(sorted(Counter(str(c['LinkedFaceType']) for c in cards.values()).items())),
            'ability_references': entry_count, 'loyalty_entries': len(loyalty_statuses),
            'loyalty_faces': len(loyalty_faces), 'chapter_entries': len(chapter_statuses),
            'chapter_faces': len(chapter_faces), 'empty_colors_populated_identity': empty_colors,
            'card_local_generic_differences': text_differences, 'families': families,
            'unprocessed_source_records': 0, 'extraction_errors': [],
            'entry_statuses': dict(Counter(r[0] for r in dest.execute('SELECT status FROM entries'))),
            'diagnostics_by_reason': dict(reason_counts)}


def extract(source, output, manifest):
    """Extract all records to a new explicit external file, never overwriting.

    Schema/provenance corruption is fatal. Unresolvable references remain raw
    and diagnosed in a complete export; complete does not mean interpreted.
    """
    started = time.perf_counter()
    source, manifest = Path(source).resolve(strict=True), Path(manifest).resolve(strict=True)
    output = Path(output).absolute()
    parent = output.parent.resolve(strict=True)
    output = parent / output.name
    summary_path = output.with_suffix(output.suffix + '.summary.json')
    repo = Path(__file__).resolve().parents[1]
    if output.is_relative_to(repo) or output.is_relative_to(manifest.parent) or output.is_relative_to(source.parent):
        raise ExtractionError('output must be outside repository and preserved source directories')
    if os.path.lexists(output) or os.path.lexists(summary_path):
        raise ExtractionError('output or summary already exists')
    source_hash, manifest_hash = sha256(source), sha256(manifest)
    document = json.loads(manifest.read_text(encoding='utf-8'))
    matches = [f for f in document.get('files', [])
               if (manifest.parent / f.get('relative_path', '')).resolve() == source]
    if len(matches) != 1 or matches[0].get('sha256') != source_hash or not document.get('arena_version'):
        raise ExtractionError('manifest does not establish source hash and Arena version')
    fd, staged = tempfile.mkstemp(prefix='.structural-', suffix='.sqlite', dir=parent)
    os.close(fd)
    staged = Path(staged)
    published = []
    try:
        with closing(readonly(source)) as incoming, closing(sqlite3.connect(staged)) as dest:
            incoming.execute('BEGIN')
            summary = _extract(incoming, dest)
            fatal = {reason: count for reason, count in summary['diagnostics_by_reason'].items()
                     if reason.startswith(('missing', 'malformed')) or reason in
                     ('invalid_chapter_index', 'invalid_loyalty_cost')}
            if fatal:
                raise ExtractionError('incomplete source facts: ' + encoded(fatal), summary)
            meta = {'contract_version': VERSION, 'schema_version': VERSION,
                    'implementation_sha256': sha256(__file__), 'source_sha256': source_hash,
                    'manifest_sha256': manifest_hash, 'arena_version': document['arena_version'],
                    'extracted_at_utc': datetime.now(timezone.utc).isoformat(), 'state': 'complete',
                    'read_only_enforcement': 'SQLite URI mode=ro; PRAGMA query_only=ON verified 1',
                    'source_path': str(source), 'payload_sha256': payload_digest(dest),
                    'table_counts': {t: dest.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in FACT_TABLES},
                    'measurements': summary}
            dest.executemany('INSERT INTO metadata VALUES(?,?)', ((k, encoded(v)) for k, v in sorted(meta.items())))
            dest.commit()
        validate(staged)
        if sha256(source) != source_hash or sha256(manifest) != manifest_hash:
            raise ExtractionError('input changed during extraction')
        summary = dict(meta, runtime_seconds=time.perf_counter()-started, output_bytes=staged.stat().st_size)
        # Windows rename refuses an existing destination; POSIX rename does not.
        # Use each platform's non-replacing atomic publication primitive.
        if os.name == 'nt':
            os.rename(staged, output)
        else:
            os.link(staged, output)
        published.append(output)
        with summary_path.open('x', encoding='utf-8') as stream:
            published.append(summary_path)
            json.dump(summary, stream, indent=2, ensure_ascii=False, sort_keys=True)
            stream.write('\n')
        return summary
    except Exception:
        for path in reversed(published):
            path.unlink()
        raise
    finally:
        staged.unlink(missing_ok=True)
