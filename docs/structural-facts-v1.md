# Arena structural facts v1

This is a standalone factual export, not a canonical database, ownership store,
rules interpreter, source-precedence policy, or recognition input. Analysis 5,
Dependencies 4 and Needs 3 remain unchanged. ED-1–ED-4 remain NON_SCORED.
The extractor imports no builder, canonical provider, analysis or scoring code.

## Running offline

Use Python's standard library. No network, packages or external services are used.

```powershell
python -B extract_structural_facts.py --source C:\path\arena\source.mtga --manifest C:\path\manifest.json --output C:\separate\unique-run\facts.sqlite
python -B -m unittest tests.test_structural_facts
python -B scripts/check_structural_facts.py --snapshot C:\separate\unique-run\facts.sqlite --require-inputs
```

All three extractor arguments are required. The parent output directory must
already exist. For Run 15, the operator-authorized root is
`C:\MTG-Source-Preservation\structural-facts\`, with a unique run subdirectory.
The reusable code also accepts other explicitly selected external destinations
for portable fixtures and later authorized use; this is not permission to write
outside the operator's authorized root. Repository and preserved input directories
are rejected after resolving parent aliases. Existing destinations, including
dangling aliases and existing companion summaries, are rejected.

The source manifest must have `arena_version` and exactly one matching `files`
entry with `relative_path` (relative to the manifest) and `sha256`. Its bytes and
the raw source are hashed before and after extraction. All source SQLite readers
use URI `mode=ro`, then `PRAGMA query_only=ON`, verified as 1. Extraction reads in
one transaction. No source connection is writable.

## Storage and provenance

SQLite `PRAGMA user_version=1`; structural contract version 1. Tables:

| Table | Key | Contents |
|---|---|---|
| metadata | key | JSON values, versions, implementation module SHA-256, manifest/raw SHA-256, Arena version, UTC extraction time, complete state, payload hash, row counts and measurements |
| source_records | table_name, source_key | Every original row of Cards, Abilities, Localizations_enUS, Enums, AltPrintings, AltToBasePrintings and Versions as canonical JSON |
| components | grp_id | Original card record, localized name observation, type/subtype enum observations and gameplay-color status |
| links | grp_id, position | One directed link slot, target ID, reviewed kind, status and original codes/token |
| entries | grp_id, position | One visible Cards.AbilityIds entry, exact ability ID, chapter index, raw loyalty cost, status and joined observations |
| diagnostics | scope, source_key (not unique) | Reason and source detail for unresolved observations |

The source-key JSON array follows the source table's primary key. All source
columns are retained, including optional columns unknown to this implementation.
Absent columns remain absent JSON keys, SQL null becomes JSON null, empty strings
and zero remain distinct. Required columns missing from a source schema are fatal.
Duplicate/null source identities are fatal. No record is filtered by primary,
token, rebalanced, set, benchmark membership or ability type.

The companion `<output>.summary.json` includes metadata plus runtime and output
bytes. The factual payload SHA-256 covers UTF-8 canonical JSON lines of
`[table_name, row_values]`, ordered by the first two columns, for source_records,
components, links, entries, diagnostics in that fixed order. Timestamp, paths,
runtime, provenance and summary are excluded from the factual payload hash.
`validate()` checks SQLite integrity, versions, complete state, required provenance,
table counts and recomputed payload hash. It validates export integrity, not
gameplay understanding or resolution of every diagnostic.

## Joins and exact values

Cards.GrpId is the face/component identity. Cards.TitleId joins an English
localization observation. Localized observations retain ID, presence and exact
`Formatted=1` text. All other formatted source rows remain in source_records;
there is no normalization or fallback. Types/Subtypes retain their raw tokens
and CardType/SubType enum observations. Missing names (including TitleId 0 source
sentinels) and missing enum labels are explicit unavailable diagnostics.

Each comma-delimited Cards.AbilityIds pair `ability_id:local_text_id` becomes one
zero-based ordered entry. Its Abilities row and generic TextId localization stay
separate from the card-local TextId/localization. Repeated IDs or repeated text
are not deduplicated. Hidden, referenced, modal, embedded and template relations
stay in the exact raw source fields of Cards/Abilities; all target ability rows
are retained independently. They are not spliced into entries, recursively
interpreted, or promoted to intrinsic abilities. There is no ownership quantity
column and no API that adds linked faces to decks.

LoyaltyCost remains its source string (`+2`, `0`, `-6`, `-X`, etc.) or null. A
variable cost is not a failed numeric parse: this contract does not evaluate it.
No activation legality, timing, frequency or effect is inferred.

Saga classification requires Abilities.BaseId=166 and verifies that source base
ability 166 has TextId 242813 whose formatted localization is exactly SagaChapter.
BaseIdNumeral is the chapter index; a chapter index must be a positive integer.
Reach, Trample and text that merely describes a chapter are not chapters. Chapter
entries retain exact position, face, ability, text IDs and both text observations.
No timing, merging or deduplication is performed.

## Relationships and unresolved facts

Only a single reciprocal link with signature
`(source IsPrimaryCard, source code, target IsPrimaryCard, target code)` matching
`(1,2,0,1)` or its inverse is transform; `(1,18,0,17)` or its inverse is omen.
An Omen spell is never a transformation. Both directions are retained, but a
reviewed reciprocal pair is counted once in the undirected-pair measurement.
Source components and directed relationship slots are separate units.

All other combinations stay `unknown`. Reviewed numeric codes with inconsistent
primary flags or conflicting reviewed partner codes get `conflicting_layout`;
unreviewed codes and multi-target configurations get `unsupported_layout`.
The raw layout is never rewritten. Unsupported layouts are not interpreted
successfully. Ordinary reciprocal pairs are not invalid cycles. Self-links and
cycles longer than two receive `invalid_cycle` under v1. This is a v1 diagnostic,
not a claim that Arena can never support a larger layout. Every link token and
all component rows remain retained; no universal two-face schema is assumed.

Diagnostics use this precedence for a link: missing slot, malformed token,
missing target, invalid cycle, nonreciprocal, unsupported cardinality, conflicting
or unsupported codes. The primary reason is exclusive; raw records retain all
evidence for later review. A layout-bearing face with no targets contributes a
missing-reference sentinel, so it cannot disappear from a denominator.

Colors and IndicatorColors are never filled from ColorIdentity. Empty Colors
with populated ColorIdentity gets unresolved gameplay-color status, including
Avatar Roku/Kyoshi. All raw variant flags, rebalanced pointers, alternate-printing
rows and conflicting variant references remain separate source evidence. No
reconciliation, name-only matching or source precedence is adopted.

## Failure behavior and measurements

Schema/provenance errors, malformed visible references, missing targets/visible
abilities/localizations and invalid chapter indices or loyalty value types fail
publication. An ExtractionError carries measurements when a full scan was possible;
the CLI emits a JSON diagnostic and exits 1. It never publishes a validated partial
database. Unknown/conflicting layouts, nonreciprocal/cyclic relationships, absent
component labels and unresolved colors are complete raw observations with explicit
diagnostics; these do not represent silently omitted source records.

A temporary file in the explicit output directory is validated before publication.
Windows uses non-replacing rename; POSIX uses non-replacing hard-link publication.
There is no overwriting fallback. Summary creation is exclusive. Exceptions remove
only artifacts created by this invocation. An abrupt process/OS failure can leave
a staging file or a database without a summary: such an incomplete artifact pair
must not be accepted or automatically overwritten. Prior snapshots are untouched.

Family denominators:

* Links: every directed linked token, plus missing-slot sentinels on nonzero layouts.
  Recovered means a reviewed relationship, not merely a retained raw token.
* Loyalty: every visible entry joined to a non-null raw LoyaltyCost.
* Chapters: every visible entry joined to BaseId=166, including invalid indices.

Each family reports eligible, recovered, unresolved, unsupported (a subset of
unresolved), missing/invalid references (also a subset), reason breakdown and
recovery percentage using the entire eligible denominator. Missing/malformed
ability IDs make loyalty/chapter eligibility NOT MEASURABLE; the known-eligible
count remains visible, unknown eligibility is reported, and percentages are null.
An empty denominator also yields a null percentage.

Full-source counts include all scanned rows, primary flags, card layout-code
counts, distinct linked faces, directed slots by kind/code, reviewed undirected
pairs, unknown codes, link diagnostics (including zeros), visible references,
loyalty/chapter entries and faces, empty-color observations and local/generic text
differences. Raw rows retained and derived rows are reported separately. Unresolved
counts/denominators are separate for components, directed link slots and visible
entries; they must not be summed into a purported card-coverage percentage.

Benchmark acceptance pins Run 14 evidence hashes, uses its exact Arena printing-ID
zones without name matching, compares source records and every visible benchmark
entry, verifies 12 links / 11 loyalty entries / 40 chapters and 14 / 14 / 53 weighted
facts, and reruns recognition against the read-only candidate. It preserves all
180 copy slots and 92 root IDs. Linked facts inherit root quantities only for
measurement. Benchmark success is not full-source completeness. Missing external
inputs yield a labeled portable SKIP; `--require-inputs` makes that failure, and
a skipped run never satisfies the Run 15 commit gate.

## Deferred operator decisions

Additional layout mappings, nonprimary variant pairing, source precedence,
canonical schema changes and recognition integration require separate review and
authorization. No extracted fact establishes an executable mechanic. Raw data
and snapshots must remain outside the public repository; this contract makes no
redistribution grant. Publication also requires the operator's clean full-suite
run in a normal Windows environment.
