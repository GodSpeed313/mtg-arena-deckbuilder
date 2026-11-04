"""Verified immutable view of the explicitly pinned Run 15 visible entries."""
from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
from types import MappingProxyType

from mtgadb.structural_facts import sha256, readonly, validate

PAYLOAD = 'f880d3d7c694cdff60331b3456b9598e8216a1fa69822ce43337fd270ce495a3'
SOURCE = '1be0105411ad989ca60e233f1341eaaa9276e3a679c89f1ef6f07e07ede10d25'
SOURCE_MANIFEST = '8d60c34505f5aeb21c7aa5209bc0f9e3414765b95beafd8a83f0cb808d32d8c9'
ARENA_VERSION = '0.1.14517.1329972'
ARTIFACT_NAME = 'structural-facts-v1.sqlite'


class EvidenceError(ValueError):
    """Fail closed: no evidence object or partial analysis may be returned."""


@dataclass(frozen=True)
class Entry:
    grp_id: int
    position: int
    ability_id: int
    local_text_id: int
    local_text: str
    generic_text_id: int
    generic_text: str
    loyalty_cost: str | None
    chapter_index: int | None

    @property
    def eligible(self):
        return self.loyalty_cost is not None or self.chapter_index is not None


class StructuralEvidence:
    """All verification finishes before any entry becomes accessible.

    Paths are explicit. Independently pinned constants cannot be overridden via
    the public API/CLI. No newest-file search, name lookup or fallback exists.
    The mandatory Run 15 payload validator hashes links but this reader never
    queries, exposes or follows them. After verification only entries are read.
    """

    def __init__(self, artifact, output_manifest, raw_source, source_manifest):
        try:
            artifact = Path(artifact).resolve(strict=True)
            if artifact.name != ARTIFACT_NAME:
                raise EvidenceError('only the pinned artifact filename is accepted')
            actual_file = sha256(artifact)
            supporting = json.loads(Path(output_manifest).read_text(encoding='utf-8'))
            expected_file = supporting['files'][ARTIFACT_NAME]['sha256']
            if actual_file != expected_file:
                raise EvidenceError('artifact whole-file hash mismatch')
            meta = validate(artifact)  # Recomputes factual hash using Run 15 code.
            expected = {'payload_sha256': PAYLOAD, 'source_sha256': SOURCE,
                        'manifest_sha256': SOURCE_MANIFEST, 'arena_version': ARENA_VERSION,
                        'contract_version': 1, 'schema_version': 1}
            for key, value in expected.items():
                if meta.get(key) != value:
                    raise EvidenceError('independent pinned provenance mismatch: ' + key)
            if sha256(raw_source) != SOURCE or sha256(source_manifest) != SOURCE_MANIFEST:
                raise EvidenceError('actual preserved source hash mismatch')
            records = {}
            with closing(readonly(artifact)) as db:
                for row in db.execute('SELECT * FROM entries ORDER BY grp_id,position'):
                    fact = json.loads(row['facts_json'])
                    if row['status'] != 'recovered' or not fact['ability_present']:
                        raise EvidenceError('incomplete visible entry in pinned artifact')
                    local, generic = fact['card_local'], fact['generic']
                    if not local['present'] or not generic['present']:
                        raise EvidenceError('missing visible-entry text')
                    entry = Entry(row['grp_id'], row['position'], row['ability_id'],
                                  local['id'], local['text'], generic['id'], generic['text'],
                                  row['loyalty_cost'], row['chapter_index'])
                    records.setdefault(entry.grp_id, []).append(entry)
            if sha256(artifact) != actual_file:
                raise EvidenceError('artifact changed during verification')
            proof = dict(expected, whole_file_sha256=actual_file,
                         verification='Run15 validate + independent pins + actual sources + output manifest',
                         read_only='URI mode=ro; query_only=1 verified')
        except EvidenceError:
            raise
        except Exception as error:
            raise EvidenceError('structural evidence verification failed: ' + str(error)) from error
        self._entries = MappingProxyType({k: tuple(v) for k, v in records.items()})
        self._proof = MappingProxyType(proof)

    @property
    def proof(self):
        return dict(self._proof)

    def entries(self, grp_id):
        if type(grp_id) is not int:
            raise EvidenceError('exact integer Arena GrpId required')
        return self._entries.get(grp_id, ())

    def structural_printings(self):
        return tuple(k for k, rows in self._entries.items() if any(e.eligible for e in rows))
