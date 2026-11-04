# Opt-in Analysis 6: structural evidence

Analysis 6 preserves **every Analysis 5 field unchanged**, including
`analysis_version: "5"`, and adds one sibling key, `structural_analysis`, whose
`contract_version` is `"6"`. This avoids overwriting the original contract/version
field. Analysis 5 remains the default. No existing caller or module imports
Analysis 6. Dependencies 4, Needs 3, discovery, comparison, fit and recommendation
code receive no structural input. ED-1–ED-4 remain NON_SCORED.

## Explicit entry point

`services.analysis6.analyze_deck6(deck, con, evidence)` requires a verified
`StructuralEvidence` object and a read-only canonical connection with
`query_only=1`. `analyze_structural.py` is the opt-in CLI. It requires explicit
`--artifact`, `--output-manifest`, `--raw-source`, `--source-manifest`, `--candidate`
and `--deck` paths. Deck JSON has optional name and main/sideboard/commander maps
from exact Arena GrpId to positive quantity. Output is JSON on stdout; errors
produce a diagnostic on stderr and exit 1, without partial analysis output.
The CLI has no default artifact or output file and no network capabilities.

## Verification and trust

`mtgadb.structural_evidence.StructuralEvidence` accepts explicit paths and only
the pinned `structural-facts-v1.sqlite` filename. It verifies the supporting Run 15
output-manifest whole-file hash, invokes Run 15 `validate()` (which independently
recomputes the factual payload), checks all independently pinned values below,
checks actual preserved source and source-manifest bytes, and checks the artifact
again after reading. Any failure raises EvidenceError before exposing entries.

* Factual payload: `f880d3d7c694cdff60331b3456b9598e8216a1fa69822ce43337fd270ce495a3`
* Raw source: `1be0105411ad989ca60e233f1341eaaa9276e3a679c89f1ef6f07e07ede10d25`
* Source manifest: `8d60c34505f5aeb21c7aa5209bc0f9e3414765b95beafd8a83f0cb808d32d8c9`
* Arena version: `0.1.14517.1329972`; structural contract/schema: 1.

Pins cannot be overridden by public API/CLI arguments. Synthetic tests patch
module constants only in their isolated fixture contexts. Matching manifest and
artifact hashes never replace the independent pins. No newest lookup, development
snapshot, repeat snapshot or fallback is accepted. All SQLite inputs are opened
using URI `mode=ro`, with `query_only=ON` verified as 1.

After verification, immutable visible-entry records are kept in memory, keyed
only by exact GrpId. They retain zero-based position, Arena ability ID, card-local
text ID/text, generic text ID/text, raw LoyaltyCost (including `0`, `-X`, null),
and chapter index. No names or linked-component lookup API are exposed.
The required Run 15 whole-payload validation necessarily hashes the links table;
that integrity operation is the only link access. Evidence queries afterward
read only entries. Analysis 6 never traverses or uses links.

## Binding established by Phase 0

Path 1 is unavailable: Analysis 5 card rows carry canonical title IDs, and its
ability IDs are generated `ability.NNN`, not Arena ability/localization IDs.
Its source_index is a one-based normalized text-line position. Deck inputs retain
exact Arena printing IDs. Therefore v6 implements only Path 2.

For each deck printing and zone, the canonical printings table maps the exact
GrpId to the corresponding Analysis 5 title row. This lookup does not select a
different printing as structural evidence. The entire visible sequence for that
exact GrpId is compared with the entire Analysis 5 ability sequence:

1. Entry counts must agree, including nonstructural abilities.
2. Source positions must be exactly 0..N-1; Analysis 5 source_index exactly 1..N.
3. Analysis 5 generated ability IDs must be unique.
4. Each card-local text, passed through existing `mtgadb.text.normalize_text`,
   must equal the corresponding Analysis 5 raw_text exactly.
5. Every source entry must belong to the requested exact GrpId.

No split, combined, omitted, duplicated or reordered sequence receives credit.
Repeated identical text can bind only through the complete uniquely positioned
sequence, never by searching for matching fragments. Positions are independently
supplied by the artifact and Analysis 5; no positions are invented for missing
entries. If multiple exact deck printings collapse into the same Analysis 5 title
row, the card is ambiguous: multiple structural entries cannot share one Analysis
5 counterpart. No representative substitution or cross-face binding occurs.

States are `bound`, `unbound` or `ambiguous`, with reason. Missing canonical card,
exact-printing mismatch, length mismatch or text mismatch is unbound. Duplicate/
invalid positions and merged deck printings are ambiguous. All structural entries
on an affected card receive no credit; a partial match is never accepted.

For full-candidate measurement, the explicitly supplied representative GrpId is
itself the exact identity, with the same rules. Tyvar 75245 can bind independently
of representative 93292 because its entire ordered sequence agrees; the reader
still retrieves only 75245 entries.

## Additive structure and unchanged effects

Each eligible visible entry has non-null LoyaltyCost or chapter_index. Bound
entries receive `structures` containing `loyalty_ability` with `raw_cost`, or
`saga_chapter` with `chapter_index`. Null costs produce no loyalty structure;
Reach, Trample, ordinary colon abilities and other nonchapter entries receive
no structure solely because of card type or wording.

The original Analysis 5 kind is copied into `analysis5_kind`. Differences are
listed in `kind_corrections` with old kind and structural kind. The original field
is never replaced. `effect_classification` is a deep copy of the **entire** original
Analysis 5 ability result. It is not reparsed, upgraded, or merged with source text.
Unbound/ambiguous entries have empty structures/corrections and null counterpart
effect_classification; their unchanged original effect results remain in Analysis
5's original fields. No nonexistent counterpart is fabricated.

No diagnostic parsing is implemented: `diagnostic_axis.status = not_run`.
Card-local/generic source text is factual evidence only. No new legality, timing,
turn-frequency, triggering, chapter-resolution or gameplay-effect claims are
introduced. Existing Analysis 5 trigger/cost/effect fields remain untouched.

## Axes, denominators and scope

Per zone, structural_axis reports distinct exact-printing entries and separately
copy-weighted entries: eligible denominator, bound by path, unbound, ambiguous,
unbound/ambiguous reasons, structurally recognized, and entries with kind
corrections. Each eligible entry counts once even if it carries both structural
families. Both structures are retained. A kind correction is counted once per
entry for summary purposes. effect_axis copies the original Analysis 5 coverage,
including its separate card-copy-times-ability denominator. There is no combined
supported percentage across these axes. Distinct entries in different zones
remain zone-specific; aggregation must explicitly deduplicate as appropriate.

Deck analysis uses only explicitly supplied printing faces. Run 15's frozen
benchmark denominators additionally include linked back-face entries. The
measurement script retains the original 11/40 distinct and 14/53 copy-weighted
loyalty/chapter denominators, reporting absent faces as unbound with reason
`face_not_in_deck_linked_components_excluded`. It reads the frozen denominator
inventory for reporting, not the artifact links and not to feed those faces into
Analysis 6. Named-card measurements may analyze 98379 as an explicitly requested
identity; if absent from the canonical candidate it remains unbound with no
invented Analysis 5 ability/effect result.

`scripts/measure_analysis6.py --output-dir <existing unique authorized directory>`
measures the three decks, five named IDs, and all candidate representative cards
whose exact printing has structural entries. It checks base-output bytes using
pre-implementation snapshots, checks copied effects and original fields, and
records structural gaps separately from contract violations. Full-candidate
counts are distinct entries, not benchmark copy counts. Factual extraction is not
effect understanding. Benchmark success is not whole-database completeness.

All durable outputs for Run 16 belong under
`C:\MTG-Source-Preservation\analysis-6\` in its unique run directory. No existing
file, schema, test, default, evidence version, frozen expectation or README is
changed. Any future linked-face use, card-local parsing policy, effect grammar,
canonical consolidation, or Dependencies/Needs integration requires separate
operator authorization. Publication requires the operator's normal-Windows test
run; the local commit alone is not publication readiness.
