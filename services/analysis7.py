"""Opt-in reviewed linked components; prior analyses remain unchanged."""
from collections import Counter
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
import json

from mtgadb.structural_evidence import StructuralEvidence, EvidenceError
from mtgadb.structural_facts import readonly, sha256
from mtgadb.text import normalize_text
from services.abilities import _parse_line
from services.evidence_scope import normalize
from services.analysis6 import analyze_deck6


class LinkedEvidence:
    """Reuse Run 16's full verification before reading component/link facts."""

    def __init__(self, artifact, output_manifest, raw_source, source_manifest):
        self.base = StructuralEvidence(artifact, output_manifest, raw_source, source_manifest)
        expected = self.base.proof['whole_file_sha256']
        if sha256(artifact) != expected:
            raise EvidenceError('artifact changed before linked evidence read')
        with closing(readonly(artifact)) as db:
            cards = {r[0]: json.loads(r[1]) for r in db.execute('SELECT * FROM components')}
            links = {}
            for row in db.execute('SELECT * FROM links ORDER BY grp_id,position'):
                links.setdefault(row['grp_id'], []).append(dict(row))
        if sha256(artifact) != expected:
            raise EvidenceError('artifact changed during linked evidence read')
        self._cards, self._links = cards, links

    def linked_roots(self):
        return tuple(self._links)

    def components(self, root_id):
        """Only exact IDs, one hop, reviewed primary -> secondary signatures."""
        if type(root_id) is not int:
            raise EvidenceError('exact integer Arena GrpId required')
        attached, skipped = [], []
        root = self._cards.get(root_id)
        links = self._links.get(root_id, [])
        for link in links:
            target_id = link['target_id']
            target = self._cards.get(target_id)
            reason = None
            a = root['source'] if root else {}
            b = target['source'] if target else {}
            codes = (a.get('LinkedFaceType'), b.get('LinkedFaceType'))
            kind = {(2, 1): 'transform', (18, 17): 'omen'}.get(codes)
            if not root or not target:
                reason = 'missing_component'
            elif codes[0] not in (1, 2, 17, 18) or codes[1] not in (1, 2, 17, 18):
                reason = 'unknown_code'
            elif a.get('IsPrimaryCard') != 1 or b.get('IsPrimaryCard') != 0:
                reason = 'flag_conflict'
            elif kind is None:
                reason = 'unreviewed_signature'
            else:
                reverse = self._links.get(target_id, [])
                reciprocal = (len(links) == 1 and len(reverse) == 1
                              and reverse[0]['target_id'] == root_id
                              and a.get('LinkedFaceGrpIds') == str(target_id)
                              and b.get('LinkedFaceGrpIds') == str(root_id))
                if not reciprocal:
                    reason = 'nonreciprocal_or_multiple_links'
                elif any(r['status'] != 'recovered' or r['kind'] != kind for r in (link, reverse[0])):
                    reason = 'unreviewed_stored_relationship'
            entries = self.base.entries(target_id) if reason is None else ()
            if reason is None:
                name = target['name']
                types = target['types']
                if (not name['present'] or not isinstance(name['text'], str)
                        or any(not t['localization'] or not t['localization']['present'] for t in types)
                        or not entries or any(not isinstance(e.local_text, str) or not normalize_text(e.local_text) for e in entries)
                        or [e.position for e in entries] != list(range(len(entries)))):
                    reason = 'missing_text'
            if reason:
                skipped.append({'root_grp_id': root_id, 'target_grp_id': target_id,
                                'position': link['position'], 'raw_codes': list(codes), 'reason': reason})
                continue
            attached.append({'grp_id': target_id, 'relationship': kind,
                             'root_linked_face_type': codes[0], 'face_linked_face_type': codes[1],
                             'face': deepcopy(target), 'entries': entries})
        return attached, skipped


def analyze_component(component):
    face = component['face']
    raw = face['source']
    types = frozenset(t['localization']['text'] for t in face['types'])
    abilities = []
    for entry in component['entries']:
        # Direct single-entry invocation: never split/concatenate source entries.
        # _parse_line is the unchanged grammar used by Analysis 5's decomposer.
        parsed = _parse_line(normalize_text(entry.local_text),
                             ability_id=f'ability.{entry.position+1:03d}', source_index=entry.position+1,
                             card_name=face['name']['text'], card_types=types)
        structures = []
        if entry.loyalty_cost is not None:
            structures.append({'structure': 'loyalty_ability', 'raw_cost': entry.loyalty_cost})
        if entry.chapter_index is not None:
            structures.append({'structure': 'saga_chapter', 'chapter_index': entry.chapter_index})
        abilities.append({'source_entry': asdict(entry), 'structures': structures,
                          'structural_evidence': 'source-native' if structures else None,
                          'linked_face_effect': normalize(asdict(parsed))})
    return {'grp_id': component['grp_id'], 'relationship': component['relationship'],
            'root_linked_face_type': component['root_linked_face_type'],
            'face_linked_face_type': component['face_linked_face_type'],
            'face_name': deepcopy(face['name']), 'raw_name': face['name']['text'],
            'raw_types': raw['Types'], 'raw_subtypes': raw['Subtypes'],
            'types': deepcopy(face['types']), 'subtypes': deepcopy(face['subtypes']),
            'Colors': raw['Colors'], 'IndicatorColors': raw['IndicatorColors'],
            'ColorIdentity': raw['ColorIdentity'], 'gameplay_color': face['gameplay_color'],
            'abilities': abilities}


def population(roots, weighted=False):
    counts = Counter(root_records=len(roots), linked_faces=0, visible_entries=0,
                     source_native_entries=0, loyalty_entries=0, chapter_entries=0)
    kinds, effects = Counter(), Counter(supported=0, partial=0, unsupported=0)
    for root in roots:
        weight = root['root_quantity'] if weighted else 1
        for component in root['components']:
            counts['linked_faces'] += weight
            kinds[component['relationship']] += weight
            for entry in component['abilities']:
                counts['visible_entries'] += weight
                effects[entry['linked_face_effect']['parse_status']] += weight
                counts['source_native_entries'] += weight * bool(entry['structures'])
                counts['loyalty_entries'] += weight * (entry['source_entry']['loyalty_cost'] is not None)
                counts['chapter_entries'] += weight * (entry['source_entry']['chapter_index'] is not None)
    return dict(counts, face_denominator=counts['linked_faces'], entry_denominator=counts['visible_entries'],
                linked_faces_by_kind=dict(kinds), linked_face_effects=dict(effects),
                weighting='root_multiplicity_for_measurement_only' if weighted else 'root_component_source_entry',
                ownership_or_deck_slots_added=0)


def analyze_deck7(deck, con, evidence):
    if not isinstance(evidence, LinkedEvidence):
        raise TypeError('verified LinkedEvidence required')
    prior = analyze_deck6(deck, con, evidence.base)
    result = deepcopy(prior)
    zones = {}
    for zone in ('main', 'sideboard', 'commander'):
        roots, skipped = [], []
        for gid, quantity in sorted(getattr(deck, zone).items()):
            components, exclusions = evidence.components(gid)
            roots.append({'root_grp_id': gid, 'root_quantity': quantity,
                          'components': [analyze_component(c) for c in components]})
            skipped.extend(exclusions)
        zones[zone] = {'roots': roots, 'skipped_links': skipped,
                       'distinct': population(roots), 'copy_weighted': population(roots, True)}
    result['linked_face_analysis'] = {'contract_version': '7', 'evidence': evidence.base.proof,
                                    'grammar_entrypoint': 'services.abilities._parse_line',
                                    'population': 'linked_face_effects', 'zones': zones}
    return result
