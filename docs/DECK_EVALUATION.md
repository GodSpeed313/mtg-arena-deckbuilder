# Deck evaluation harness, version 1

This opt-in, offline harness measures selection through human grading. Automatic
signals never judge a card or deck. Run 21 generation, legality, analyses and all
defaults remain unchanged. No AI, network or package dependencies are used.

## Frozen evaluation set

`deck_evaluation_v1_inputs.json` pins five source decks by SHA-256. Requests are
`std-bo1` (Standard/BO1) and `std-bo3` (Standard/BO3). Run 21 accepts format, queue,
and an output-directory setting; it cannot accept forced colors. Baseline reports
and metadata are pinned as generation/validation evidence. Earth Danger, Dragon
Love and Crimson Forest are calibration references. The operator requested both
queue assessments for all references; no historical queue is inferred. References
remain gradeable regardless of legality and never alter the legality store.

Build emits a manifest, policy and freeze receipt. Decks, generation evidence,
manifest and policy hashes are checked on later operations. Policy must also match
the version compiled into this module. Future changes require new versions and
baseline regrading before comparison; do not reinterpret a frozen version.
The freeze receipt is a local integrity record, not a cryptographic signature.

All source SQLite access uses the unchanged Run 21 pinned read-only reader. Text
entries resolve by the intersection of exact name, set and collector number;
one Arena printing must remain. No fuzzy or name-only identity matching occurs.
Arena JSON resolves its preserved card IDs directly. Unknown identity fails closed;
unknown legality does not suppress grading. Legal status includes title evidence
and separate construction diagnostics: Standard minimum 60 main, max four copies
unless verified basic, BO3 sideboard <=15. BO1 uses Run 21's no-sideboard convention,
not a claim that Arena disallows all BO1 wishboards. Commander/companion zones are
reported as unvalidated. No special deckbuilding exemption is interpreted.

## Version-1 signals

All seven signals always appear, carry `for human review`, and include a rationale
describing contexts in which the pattern is acceptable. Zero is a measurement,
not a criticism. Quantities are copy-weighted. Except cosmetic printing splits,
signals use the main deck; sideboards remain visible in the grading deck list.

1. **Curve:** count nonlands by canonical mana value, unknown bucket for missing
   values, highest known value and land count. Alternate costs, additional sources
   or a slower plan can explain high costs. No land-count threshold judges quality.
2. **Acceleration context:** count nonland copies whose rules mention `Add` followed
   by mana symbols or a bounded phrase containing `mana`, alongside highest known
   mana value. This lexical mention signal is not executable recognition, proven
   net ramp, or a guarantee that the card produces mana itself. Fixing, activated
   abilities and casting several spells may justify acceleration at low top-end.
3. **Composition:** lands, creatures and other nonlands, plus unknown type. Few
   creatures can fit control, spells or tokens. Creature lands count as lands.
4. **Interaction:** cards with grammar-5 destroy/exile effects targeting `target_*`;
   each card copy counted once. This narrow removal lower bound omits many kinds
   of interaction. Unrecognized answers or a proactive plan can explain a low value.
5. **Understanding:** main nonlands with known type, counted fully if nonempty
   abilities are all supported, partially if any are supported/partial, otherwise
   not understood. Counts, denominator and exact rational shares are explicit.
   Empty rules/classifications never mean supported. Coverage is not quality.
6. **Split printings:** number of titles appearing under multiple Arena printing
   IDs across all zones. This is cosmetic; artwork or ownership may explain it.
7. **Mana balance:** colored-symbol mentions and verified basic copies by color.
   Every alternative in hybrid symbols counts; this is not a prescribed payment.
   Missing costs are counted. Nonbasic production, sequencing and alternate costs
   are omitted and may explain a basic split that appears uneven.

Synergy has no automatic signal. Missing types are counted separately, not treated
as noncreatures; missing mana values are not zero. Front-face canonical data and
grammar 5 remain bounded and incomplete. No card-specific criticism/exclusion is
encoded in this harness.

## Human rubric and masking

Five dimensions, integer 0–5 each, total 25. Every score requires nonempty `why`.
Each worksheet also includes free-text `cards_i_would_cut` and `cards_i_would_add`.
Blank cut/add text is allowed when the grader has no proposed changes.

| Dimension | 0 | 3 | 5 |
|---|---|---|---|
| Card quality | Selections offer no credible utility for the stated or inferred plan. | Most selections serve the plan, with some questionable choices. | Selections consistently serve the plan with convincing individual utility. |
| Synergy | Cards obstruct one another or lack a discernible shared plan. | There is a coherent plan with some mutually supporting cards. | Cards consistently reinforce one another and the plan. |
| Interaction | The deck has no credible response to relevant opposing threats. | The deck can answer several relevant threats but has notable gaps. | The deck has a convincing range of timely responses for its plan. |
| Mana efficiency | Curve, acceleration, and mana sources prevent reliable execution. | Mana supports the plan with some inefficiencies or risks. | Curve, acceleration, and mana sources convincingly support timely execution. |
| Playability | I would not take this deck into a match in its assessed format. | I would try this deck with reservations or targeted changes. | I would willingly take this deck into a match as presented. |

Sort SHA-256 of UTF-8 `run22-origin-mask-v1:` plus deck ID, tie by deck ID, then
assign Deck A, B, C, etc. The key is a separate file. Worksheets omit origin,
source path, generator identity and request/deck IDs. They include lists, flags,
legality and anchors. Origin-masked is not guaranteed blind: the operator may
recognize decks; masking does not remove familiarity bias. Do not open the key,
manifest or origin-bearing report while grading if you wish to preserve masking.

## Commands

Use a new output path under `C:\MTG-Source-Preservation\evaluations\` for every
operation. Existing outputs are never overwritten. Parent directories must exist.

```powershell
python -B evaluate_decks.py build --output C:\MTG-Source-Preservation\evaluations\new-set
python -B evaluate_decks.py ingest --set C:\MTG-Source-Preservation\evaluations\new-set --worksheets C:\MTG-Source-Preservation\evaluations\new-set\worksheets --grader operator --date 2026-11-09 --output C:\MTG-Source-Preservation\evaluations\grades.json
```

The operator fills only scores/why and cut/add fields in Deck-*.json, preferably in
a copy of the entire worksheet directory to preserve the original blank packet.
Ingestion rejects missing sheets, unknown decks, changed display content, invalid
versions, invalid scores, empty why and altered flags. Results store human scores
and totals; automatic flags remain in a separately hashed file, never in the total.

Future candidates are registered separately; the manifest is never changed:

```powershell
python -B evaluate_decks.py candidate --set SET --deck DECK --request std-bo1 --provenance PROVENANCE.json --output CANDIDATE.json
python -B evaluate_decks.py worksheets --set SET --candidate BO1-CANDIDATE.json --candidate BO3-CANDIDATE.json --output NEW-PACKET
python -B evaluate_decks.py ingest --set SET --worksheets NEW-PACKET --candidate BO1-CANDIDATE.json --candidate BO3-CANDIDATE.json --grader operator --date YYYY-MM-DD --output NEW-RESULTS.json
python -B evaluate_decks.py compare --left BASELINE-RESULTS.json --right CANDIDATE-RESULTS.json --output COMPARISON.json
```

Provenance requires generator, version, commit, inputs and card_specific_exclusions
(explicit `none` when absent). Candidate records pin deck bytes and frozen set.
Comparison returns a structured side-by-side table per request/dimension plus
separate flag differences. It refuses differing rubric/protocol/set/flag versions
or freeze hashes, duplicate/unknown requests, or missing request coverage. It
pairs by request ID, never position. References appear alongside without pairing.
All requests and all dimensions must inform future judgment. Avoiding individual
previously criticized cards is not evidence of improved selection; disclose any
card-specific rules. No new generator is evaluated or real scores supplied here.
