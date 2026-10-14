# Phases 0–2 implementation verification — 2026-10-14

## Baseline recovery

The interrupted retry's session could not be recovered (`Unknown process id`).
Its result was not assumed. Baseline verification was rerun before branch creation
or source edits. An explicitly authorized fresh `git fetch origin` succeeded.

- Base branch: `feature/offline-deck-workbench-phase-1`.
- Base HEAD: `3ed5dc8f9782e23b6d76974c8a11c7eb678cd941`.
- Upstream: `origin/feature/offline-deck-workbench-phase-1`; ahead/behind 0/0
  after fetch. Tracked worktree clean; existing untracked `audit/` preserved.
- Full baseline: `python -B -m unittest discover -q`: **909 passed**, 180.717s.
- Canonical checks: `python -B test_mtgadb.py`: **50/50 passed**.
- Baseline SQLite integrity `ok`; foreign-key violations none; no sidecars.

The first sandboxed baseline test invocation encountered Windows temporary-file
access/cleanup errors (WinError 5). The normal-permission rerun above passed.
This was an environment-dependent failure, not hidden or counted as a pass.
Final regression testing uses the same normal-permission environment.

## Implementation inventory

| File | Authorized phase and behavior |
|---|---|
| `docs/HYBRID_ADVISORY.md` | Phase 0 contracts, accepted boundaries, engineering rubric and evidence plan; usage and limitations |
| `services/advisory_contract.py` | Phase 1 detached read-only exporter, versioned prompt/schema, exact quote checks, source hashes and additive hybrid view |
| `services/advisory_store.py` | Phase 2 verified non-OneDrive cache location, pending attempts, atomic publication, current validation, separate operator verdict, retention/deletion |
| `services/advisory.py` | Phase 2 fake-only adapter/coordinator, one in-flight gate, exact consent binding, no automatic retries, safe errors |
| `advisory_workbench.py` | Separate optional fake-only CLI; existing workbench interface untouched |
| `tests/test_advisory.py` | Synthetic fixtures and targeted offline/control/integrity/adversarial tests |
| `docs/HYBRID_ADVISORY_VERIFICATION.md` | Baseline, verification evidence, operational limitation and completion report |

No pre-existing tracked file was edited. Existing parser/report schemas and
managed-store schema are unchanged. The implementation branch was created only
after baseline verification. No provider SDK or dependency was installed.

## Strict-checker replay

Final full regression command `python -B -m unittest discover -q` passed
**945 tests in 162.247s**, including **36 new advisory tests**. The suite covers
explicit consent, fake execution with network access blocked, missing provider,
failed evidence, unknown/interrupted outcomes, atomic cache publication,
reuse/invalidation, credential/error isolation, operator verdict separation,
malformed raw-byte preservation, hostile input/tool blocks, optional CLI use and
unchanged legacy rendering. No tests were skipped to obtain this result.

Executed locally against all ten preserved Gate 2 raw responses, reparsing each
and comparing to its original extracted review and validation record. All ten
structural results, evidence results and ordered error lists match. Four reviews
pass evidence validation; the same six fail. No repair, inference, rerun, prompt
tuning or semantic grading occurred. Synthetic unit tests are independently
portable; this additional replay requires the preserved external evidence.

Evidence locations (existing, read-only):

- `hybrid-strategy-v2-gate2-20261013T180530Z` under the operator's
  `Documents\MTG-Deckbuilder-Measurements` directory.
- Frozen input reference:
  `hybrid-strategy-v2-readiness-r1-20261012T171823Z\inputs`.

No independent grading expectations were read or copied into code or fixtures.

## Integrity and database checks

Rechecked all **404** files in the existing external
`read-only-integrity-baseline.json`: **zero mismatches**, including all **87**
recorded `audit/` files, original tracked files, database and frozen artifacts.
The r2 design still hashes to
`e3c1cd88deb965561ba14c1aa422a605e274b004566c9af7bcc6210cebb06fb3`.

`current.db` SHA-256 before and after read-only immutable checks:
`9f8761632230a93f58aa47d77d120cda2c5be76354d787045202ed0e01e7f3ef`.
`PRAGMA integrity_check` returned `ok`; `PRAGMA foreign_key_check` returned no
rows. No `-journal`, `-wal` or `-shm` sidecars were present. Post-implementation
canonical checks passed **50/50**. Tests use isolated stores/databases, not
mutations to this frozen database.

New product prompt UTF-8 SHA-256:
`8e9a982fa6e673d0337449768dd905ffc4cf1f7ec37c8a666becd207870128e1`.
New local schema canonical-JSON SHA-256:
`cb59e78348a104133c6e53ac302c6da626bfe41fb8f763e50b28b98ad7fc6341`.
These identify new product contracts, not replacements for frozen artifacts.

## Operational limitation found

The real Windows default-cache admission check returns
`cache_location_unestablished`, both inside and outside the sandbox. Read-only
inspection found that the Local AppData known-folder and environment agree, but
two of three OneDrive account registry entries lack `UserFolder`; the third has
an absolute root. Missing information is not treated as evidence of no sync.

Consequently the optional advisory CLI/cache is **unavailable on this profile**
until location safety can be established. No registry edits, fallback paths,
cache creation or weakened checks were made. Fake-provider integration tests
use isolated roots with explicit synthetic location evidence. They prove the
code path under those conditions, not operational cache availability here.
The existing offline workbench remains independent.

## Deviations and remaining boundaries

No intentional scope expansion from the approved design. Provider functionality
is deliberately fake-only under the implementation authorization: a $0 preview
and simulated byte count are not live pricing/token verification. A real
transport, live representative reviews and product acceptance are not delivered
or claimed. The default-cache environment limitation above remains unresolved.

Semantic/factual correctness beyond exact evidence is not certified. The
operator owns acceptance; claim evidence passage remains a separate state.
Single-process safeguards do not coordinate other processes or other clients.
Canonical face/companion and captured legality/ownership limitations remain.
No automatic upload, background refresh, executable swap, opportunity bridge,
database migration, real API request, push, merge or Phase 3 work occurred.
