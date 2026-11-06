# Opt-in dated Standard legality reference (v1)

This module does not change any analysis, recommendation, validator, or CLI default.
It classifies canonical Arena titles for a bounded historical reference pool. It
does not generate decks, validate a complete deck, or establish current legality.

The BO1 -> `Standard` and BO3 -> `TraditionalStandard` bindings are an
**operator determination dated November 6, 2026**, not independently verified
from Arena data. The full determination, its three evidence items, scope, and
label `overlay_binding: operator_determination` are stored per queue. The cited
Wizards article was not fetched or independently checked during this offline run.

Arena remains the final authority for deck import and queue eligibility. Successful import alone does not establish BO1 or BO3 legality.

## Build and query

```powershell
python -B standard_legality.py build --queue BO1 --queue BO3 --output C:\MTG-Source-Preservation\legality\my-new-version
python -B standard_legality.py query --snapshot C:\MTG-Source-Preservation\legality\my-new-version\snapshot.json --queue BO1 --title-id 812770
python -B standard_legality.py query --snapshot C:\MTG-Source-Preservation\legality\my-new-version\snapshot.json --queue BO3 --arena-id 92227
```

The build CLI accepts only the four pinned Run 20D input hashes, even if their
paths are overridden. It requires a new subdirectory below the external legality
root. It never overwrites a store or writes generated data inside the repository.
Missing/ambiguous queue provenance or unsupported overlay restriction semantics
fail closed. A single-queue snapshot cannot be queried as the other queue.

Python interfaces in `mtgadb.standard_legality`:

- `assess(paths, expected, queue_bindings, queues)` reads inputs and returns title
  evidence without writing. Explicit expected hashes allow synthetic fixtures;
  the public CLI uses the fixed production pins.
- `build_snapshot(..., output, generated_at=None)` publishes a checksummed JSON
  snapshot in a new external directory after assessment and a second input-hash
  check. An interrupted/incomplete file fails checksum/contract validation.
- `query(path, queue, title_id=... | arena_id=...)` reads the snapshot and returns
  legal/illegal/unknown, reason, source restrictions, source conflicts, provenance,
  version, and staleness/final-authority statements. Unknown IDs remain unknown.
- `load_snapshot(path)` checks the payload hash, contract version and binding
  provenance. The checksum detects corruption; it is not a digital signature or
  proof that an untrusted third-party file has trustworthy contents.

## Classification

Arena `GrpId`/canonical `arena_id` is printing identity; canonical `title_id` is
card identity. Exact Scryfall Arena IDs supply base legality. Conflicting duplicate
IDs, title/oracle cardinality, missing identity, missing Arena availability, or
disagreement across a title's exact matches produce unknown. No secondary or
name-only matching is admitted. Face records and `digital` flags do not create
additional printings, owned cards, or established gameplay states.

Arena Land + Basic type/supertype evidence supplies the approved basic exception,
including snow and Wastes. Reference identity conflicts remain recorded rather
than repaired. This explicit exception is not derived from Scryfall names.

Overlay handling is generic: `banned`, `suspended`, and `suppressed` rules keep
their original kinds and source format. Recorded legal-set membership is evaluated
against all canonical printings of the title, with recorded `allowed` exceptions.
Basic eligibility follows the explicit operator exception. Unknown base identity
remains unknown; it is not relabeled illegal by an overlay. Unsupported quota,
rebalanced, color or internal restriction semantics block the build rather than
being silently ignored. Deck-size/sideboard fields are retained as context; this
interface classifies cards, not complete deck construction.

## Dated coverage

The real pinned run yields:

| Queue | Legal | Illegal | Unknown |
|---|---:|---:|---:|
| BO1 | 4,891 | 10,398 | 851 |
| BO3 | 4,892 | 10,397 | 851 |

Both queues retain 851 unknown titles (848 no exact matches and three conflicts).
The 269 Standard-legal Scryfall oracle identities found only among Arena-listed
no-ID records are separately disclosed; these are not 269 known illegal Arena
titles. Twelve Arena-basic titles are eligible despite retained reference conflicts.
Nine otherwise base-legal titles are excluded in both queues by the historical
legal-set lists; their per-title source restrictions explain those exclusions.

The overlay is dated September 12, 2026. Scryfall's November 5 22:06:50 export
label is filename-derived, not API `updated_at`; the log date is November 6.
The respective gaps are 54 and 55 calendar days. Warnings are always returned:
changes within those gaps cannot be detected offline. No current-legality claim
or numeric refresh-cadence guarantee is made.

The payload digest and classifications are deterministic for identical inputs,
bindings, and implementation. Generation time is outside the payload digest;
complete file bytes also reproduce if the same explicit generation time is used.
Refresh requires new source pins, per-queue assessment, tests and a new directory.
Retain the prior valid snapshot after failed refresh. Do not silently refresh or
modify the authoritative databases. No redistribution license is inferred from
local access to reference data; generated source-derived stores stay out of Git.

## Validation

`python -B -m unittest tests.test_standard_legality` covers the fifteen requested
Run 20B/20D cases plus source-write prevention, deterministic regeneration,
availability, basic type evidence, generic set restrictions, snapshot corruption,
unknown IDs and face/digital handling. Existing suites remain independent.
