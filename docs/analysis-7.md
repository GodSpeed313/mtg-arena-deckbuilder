# Analysis 7: opt-in reviewed linked components

Analysis 7 is Analysis 6 **unchanged**, plus the sibling `linked_face_analysis`
with `contract_version: "7"`. All original Analysis 5 fields, its version "5",
the Analysis 6 structural layer/version "6", Dependencies 4, Needs 3, deck
identity, quantities, scores and default entry points remain unchanged. Existing
code does not import this module. ED-1–ED-4 remain NON_SCORED.

## Verification and entry point

`services.analysis7.LinkedEvidence` invokes the existing Run 16
`StructuralEvidence` loader with explicit artifact, output-manifest, raw-source
and source-manifest paths. Independent payload/provenance pins and supporting
whole-file hash checks are unchanged. It then checks the verified whole-file
hash before and after reading components and links. All SQLite reads use URI
mode=ro and query_only=ON verified 1. Verification failure produces no analysis.
No development/repeat/newest snapshot or name lookup is permitted.

`analyze_deck7(deck, con, evidence)` is the library entry point.
`analyze_linked_faces.py` requires `--artifact`, `--output-manifest`,
`--raw-source`, `--source-manifest`, `--candidate` and `--deck`. Deck JSON uses
main/sideboard/commander exact-GrpId quantity maps and an optional name. Successful
output is ASCII-escaped JSON on stdout, preserving Unicode values across Windows
pipes. Failure exits 1 with a JSON diagnostic on stderr and no partial output.

## Reviewed relationships only

Only one-hop primary-to-secondary relationships attach. A transform must have
flags/codes `(1,2,0,1)` and an omen `(1,18,0,17)`. Both directed relationship rows
must be recovered with the same reviewed kind; each direction must have exactly
one link; target IDs must be reciprocal; raw Cards.LinkedFaceGrpIds must agree.
Names never select identities. Reversed secondary roots do not attach a primary.

Unknown numeric codes, inconsistent primary flags, unsupported signatures,
nonreciprocal/multiple links, unrecovered relationship rows, missing components
and missing visible text are explicit exclusions. Codes 3–16 and 19–20 and all
296 source flag-conflicting slots remain excluded. No inference is made from
their names or familiar card layouts. A missing face name, unavailable type
localization, empty/missing visible text, no visible entries or invalid ordered
positions yields `missing_text`; it is never silently analyzed from generic text.

An attached component carries exact face GrpId, kind, both raw layout codes,
localized raw name observation, raw Types/Subtypes, their localization
observations, raw Colors, IndicatorColors and ColorIdentity, and the existing
gameplay-color status. Empty colors remain empty, notably Roku and Kyoshi.

Roots retain their original quantity. Components add zero owned copies, deck
slots or cards. A component is not evidence that a face is on the battlefield.
No transformation condition/timing, Omen casting permission, active-face state
or gameplay legality is inferred. Omen is never relabeled as transform.

## Ordered visible abilities and unchanged grammar

Each component ability is one exact visible structural entry, with face ID,
Arena ability ID, zero-based source position, card-local and generic IDs/text,
raw LoyaltyCost and chapter index. Hidden/referenced source fragments are not
spliced, promoted, parsed or included in visible-entry denominators.

The existing `mtgadb.text.normalize_text` normalizes card-local text, then the
unchanged `services.abilities._parse_line` is invoked once per source entry.
Phase 0 verified that this directly callable helper returns the same `Ability`
dataclass used by Analysis 5. Serialization uses existing evidence_scope.normalize
and dataclasses.asdict. Face-specific localized name and type set provide grammar
context only; they never establish identity. Generic text remains separate and
is never parsed.

This direct helper call deliberately preserves source entry boundaries. It does
not run the canonical loader or Analysis 5's whole-text newline decomposition and
does not manufacture new canonical rows. A multiline source entry is supplied
whole to the existing single-entry parser; unsupported semantics stay unsupported.
No new parsing rules, decomposition repairs or canonical-anomaly decisions are
introduced. This is a new population with an explicitly recorded entry point,
not a replacement classification for any root or prior ability.

An entry with non-null loyalty cost receives `loyalty_ability` and its exact raw
string (`0`, `-X`, etc. included). A chapter receives `saga_chapter` and its exact
index. These are labeled `source-native`, never `bound`, because the source
entry is the analyzed unit. Null cost alone conveys no structure. Structural
labels never upgrade parse_status. No legality, triggering, timing or resolution
claim follows from these fields.

## Populations and denominators

Each zone has root records, attached components and excluded directed slots.
`linked_face_effects` reports supported/partial/unsupported from the unchanged
grammar. Its denominator is newly visible source entries, separate from both
existing effects and Analysis 6's binding counts. Source-native entry, loyalty
and chapter counts are separate measures; an entry with both structures counts
once in source-native entries and once in each applicable family.

Distinct summaries count exact root/component/source-entry identities within a
zone. Copy-weighted summaries multiply component facts by root quantity only for
measurement, with zero ownership/deck slots added. `root_records` is the number
of root records even in the weighted summary; it is not an owned-copy count.
Aggregate benchmark distinct results deduplicate root printing IDs across decks;
weighted results sum all original root quantities. These units must not be merged
with Analysis 6 bound entries or old effect denominators.

The benchmark target is 12 components (7 transform, 5 omen) and the 13 previously
unreachable structural entries: three Oko loyalty entries and 3 Bahamut, 3 Ifrit,
4 Esper Terra chapters. Every target is reported as recovered or inaccessible
with reason; no match is forced. Root/visible-face classifications are compared
byte-for-byte with pre-implementation Analysis 6 snapshots. Analysis 5,
Dependencies 4 and Needs 3 are included in that comparison.

Full-candidate measurement covers **every candidate printing** with an outbound
source link, including nonrepresentative printings. It reports exact printing
roots, distinct canonical titles, attached components, skipped directed slots,
and newly visible entries with their own denominators. A representative-only
subset is also reported for comparison with Run 16. Unknown and conflicting
source slots outside the candidate subset remain excluded globally. Missing-text
and zero-category exclusions are reported explicitly in the report. These
measurements are not whole-Arena gameplay understanding.

Life of Toshiro Umezawa (79533), abilities 149695, 147902, 147760, remains unbound
in the original Analysis 6 layer due to sequence_length_mismatch. Analysis 7 does
not repair it. A separately attached reviewed component does not change those
root-entry findings.

## Measurement artifacts and limits

`scripts/measure_analysis7.py --output-dir <existing run directory>` writes only
under `C:\MTG-Source-Preservation\analysis-7\` and refuses existing output files.
It requires preflight and pre-implementation baseline snapshots in that directory.
No source, existing code/test, README, audit file or frozen artifact is changed.
All source reads are offline. Synthetic tests depend only on temporary fixtures.

The unknown-code appendix lists up to five literal examples for each code 3–16
and 19–20: source/target IDs, localized names/types and outbound shape. Shape
describes only observed directed degree, not a semantic mapping or layout claim.
These examples never feed analysis or the relationship whitelist.

Downstream integration, further code mappings, effect grammar expansion,
canonical consolidation, default changes and publication require separate
operator authorization. This contract adopts none of those decisions.
