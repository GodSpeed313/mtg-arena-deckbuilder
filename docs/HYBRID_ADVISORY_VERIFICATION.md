# Phases 0–2 implementation verification — 2026-10-14

The following original Phase 0–2 record is retained as historical evidence.
See the operational-readiness addendum at the end for the October 16 changes.

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

## Operational-readiness implementation addendum — 2026-10-16

Authority: final consolidated operational-readiness authorization; amendment
SHA-256 `040fb72751b194dd92ee3488f30dc1ec926a789a4d4423c251c2158d6f97352c`.
The complete amendment was read and its hash verified before edits. Required
branch `feature/hybrid-advisory-phase-0-2`, starting HEAD
`8ea935b2fe2fb80cce50f1fe090235cc0d9de2c8`, root and tracked-clean condition all
matched. Existing `?? audit/` was preserved. No fetch or branch change occurred.

Implemented narrowly scoped missing-UserFolder attestation, strict local control
records, exact evidence invalidation, revocation followed by fresh attestation,
safe cache inspection, verdict-first review deletion, orphan-verdict recovery,
deprecated non-mutating broad deletion, conservative malformed-history handling,
and optional management CLI commands. Two small helpers isolate Windows location
authority and the shared gate/error allowlist. Model-facing contracts, prompt,
schema, checker, gameplay identity and legacy workbench are unchanged.

No replacement attestation, general policy override, deletion marker, recovery
generation, cache-version-2, preview/consent-v2 recovery system, reset, history
purge, live adapter or dependency framework was introduced. The existing local
preview adds a history fingerprint to prevent consent reuse after history changes.

### Focused test evidence

Command for each focused run: `python -B -m unittest tests.test_advisory -q`.
Runs passed 46 tests (7.573s), then 49 (8.581s), 49 (12.089s), and final 49
(6.854s). Repetitions followed added adversarial coverage and path/gate fixes.
No failed or interrupted test execution occurred in this amendment. Runs used
normal Windows temporary-directory permissions; no sandbox-denied test run was
attempted here. Prior Phase 0–2 sandbox failures above remain historical.

Full regression command: `python -B -m unittest discover -q`. The first run
passed **958 tests in 207.776s**. It was repeated after the lexical-path reparse
safeguard and shared mutation gate were finalized. The final run passed
**958 tests in 168.939s**, including all **49 advisory tests** (13 more than the
accepted Phase 0–2 baseline). Neither full run failed or was interrupted.

| Required test group | Implemented evidence |
|---|---|
| 1 Missing metadata | `test_location_only_missing_value_is_attestable`: value absence versus read/open/enumeration errors and invalid roots |
| 2 Exact invalidation | `test_attestation_independent_storage_and_exact_invalidation`: additions/removals/same-count replacement, roots, profile, paths, view and policy |
| 3 Unsafe paths | Existing reparse regression plus `test_attestation_does_not_override_unsafe_paths` and `test_control_file_reparse_and_late_evidence_change` |
| 4 Independent control storage | Outside-cache control path, no startup creation, state-bound confirmation, corruption rejection |
| 5 Attestation interruption/revocation | `test_changed_confirmation_and_interrupted_attestation`, `test_revocation_and_corrupt_record_fail_closed_preserve_cache` |
| 6–8 Ordered/interrupted deletion and accepted review cycle | `test_review_delete_order_and_accepted_interruption_full_cycle`: accepted before deletion, cached_valid/not_reviewed after interruption with no provider call, repeated deletion, preserved history, fresh explicitly authorized publication not_reviewed |
| 9 Orphan verdict | `test_orphan_verdict_recovery_no_authority`: no acceptance/dispatch, confirmed narrow removal |
| 10 Charge uncertainty | `test_deletion_preserves_uncertainty_and_binds_state`, `test_unreadable_attempt_and_changed_history_block_dispatch`: malformed/pending/failed/unknown/interrupted evidence retained; fresh acknowledgment required |
| 11 CLI safety | `test_management_cli_no_provider_and_safe_confirmation`, `test_cli_confirmed_management_and_location_lifecycle`, `test_confirmed_verdict_requires_current_valid_review` |
| 12 Legacy/deprecation | Authorized update to `test_explicit_delete_not_automatic_invalidation` verifies fixed non-mutating error and preserves review/verdict/attempt/diagnostic bytes; legacy rendering test retained |

All fake execution occurred only in isolated automated fixtures. No actual
provider request, paid inference or standalone fake review experiment occurred.
Management tests explicitly prohibit provider construction. No operator verdict
or attestation was created against the real application cache.

### Integrity inventory and operational limits

The historical 404-member baseline was checked before edits and again after
implementation, with zero mismatches. Its actual scope is **87 audit files,
115 other repository files, one canonical database, and 201 external frozen
files**. It predates the seven Phase 0–2 additions; it is not interchangeable
with the 209-member repository/audit inventory at this starting HEAD.

The starting tracked/untracked scope is **122 tracked files + 87 audit files =
209**. Six existing tracked advisory files are intentionally modified; two
helpers are newly added. The original 115 tracked files, unchanged advisory
contract and 87 audit files remain unchanged. Final evidence exports record the
actual changed-file list and per-member hashes. They exclude audit material.

`python -B test_mtgadb.py`: **50/50 passed**. SQLite opened with
`mode=ro&immutable=1`: integrity `ok`, no foreign-key violations, no journal/WAL/
SHM sidecars. Database SHA-256 before/after remains
`9f8761632230a93f58aa47d77d120cda2c5be76354d787045202ed0e01e7f3ef`.

A read-only real-profile location probe succeeded: two missing UserFolder
entries, three known-root provenance records, no existing attestation. This is
not operator approval of those paths. Actual cache use still requires the
operator's explicit path/evidence confirmation. No real profile/cache/control
record or registry value was created or changed.

Remaining limits: single-process coordination; no account-wide spending guarantee;
attestation does not prove absence of every backup/synchronization agent; corrupt
control records require separately reviewed recovery; deliberate external history
erasure is not prevented. Independent patch inspection is the next authorization
gate. No push, merge, Phase 3 or Next Recognition Pass is included.
