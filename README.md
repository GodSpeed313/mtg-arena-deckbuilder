# MTG Arena Deck Architect

An offline-first foundation for importing, validating, querying, and exporting
MTG Arena decks. The project keeps factual game constraints deterministic so a
future AI deck architect can reason about strategy without inventing legality,
ownership, quantity, or crafting facts.

## Current milestone: Offline Deck Workbench

Implemented:

- Canonical SQLite projection of Arena's local card database
- Two-level card identity: canonical card (`title_id`) and printing (`arena_id`)
- Card search and rules-text normalization
- Arena text deck import and export
- Deck-size, sideboard, commander, copy-limit, and color-identity validation
- Basic-land and card-specific copy-limit exceptions
- Arena format-payload ingestion and schema-v2 format persistence
- Ban, suppression, suspension, explicit exception, legal-set, per-card quota,
  commander-allowlist, and command-zone validation
- `unlimited`, `full_collection`, and `wildcard_budget` modes
- Structured import and validation diagnostics
- Read-only `StartHook` inventory and saved-deck snapshot inspection

Still planned:

- Persistence and consumer conversion for Arena account snapshots
- Manual collection-import workflow
- Strategy and synergy recommendation layer
- Graphical user interface

## Requirements

Python 3.12 or newer. Runtime features use only the Python standard library.

The repository includes a generated `current.db` when exchanged as a project
archive. Git ignores generated databases because `python build_db.py` rebuilds
them from an installed MTG Arena client.

## Use the workbench

Export a deck from Arena and save it as `my_deck.txt`, then validate it:

```powershell
python workbench.py validate my_deck.txt
```

Normalize it to an Arena-importable file:

```powershell
python workbench.py normalize my_deck.txt -o normalized_deck.txt
```

Validate against a color identity:

```powershell
python workbench.py validate my_deck.txt --colors WU
```

Load a freshly extracted Arena `Formats` JSON array, then validate by format:

```powershell
python sync_formats.py arena-formats-2026-09-12.json
python workbench.py validate my_deck.txt --format Standard
```

Only extract the `Formats` array. Do not copy the complete `Player.log` into
the project because it can contain unrelated player data.

Check a wildcard budget:

```powershell
python workbench.py validate my_deck.txt --mode wildcard_budget `
  --collection collection.json `
  --wildcard common=20 --wildcard uncommon=15 `
  --wildcard rare=8 --wildcard mythic=3
```

Collection JSON is an object keyed by Arena printing ID:

```json
{
  "cards": {
    "96269": 4
  }
}
```

Use it with exact-collection validation:

```powershell
python workbench.py validate my_deck.txt `
  --mode full_collection --collection collection.json
```

## Refresh Arena card data

After Arena updates, close the game and run:

```powershell
python build_db.py
python test_mtgadb.py
python -m unittest discover -v
```

The build produces `current.db` and a dated database under `snapshots/`.
Re-run `sync_formats.py` after rebuilding because format rules originate in
`Player.log`, not Arena's card database.

## Architecture boundary

Python owns format legality, quantities, playset caps, color identity, deck
sizes, wildcard arithmetic, and export formatting. A future language-model
service will receive only a pre-filtered candidate pool and will own strategy,
synergy reasoning, and explanation. Its output must pass this deterministic
validator before it can be accepted or exported.

## Arena snapshot inspection (implementation pass 1)

Inspect a captured log using the current printing database:

```powershell
python workbench.py inspect-arena "C:\Users\User\AppData\LocalLow\Wizards Of The Coast\MTGA\Player.log"
```

`--database PATH` before the subcommand selects another existing database.
The command reads the log once, opens the database read-only, and prints JSON
containing `inventory`, `decks`, and `collection` capability results. It does
not import anything into database tables, calculate affordability, or change
Arena files. Output includes saved-deck identifiers and names; the checked-in
fixture uses only synthetic identifiers and names.

The provider is `mtgadb.providers.arena_log.ArenaLogProvider`. Each method uses
existing `ProviderResult` / `Diagnostics` conventions. Inventory and deck data
use small provider-local snapshot dataclasses rather than the existing
`Inventory` / `Deck` consumer models: those models default missing values to
zero or empty zones. These snapshots intentionally are not implementations of
the existing validation-ready `InventoryProvider` / `DeckProvider` contracts.
No model or database schema migration is needed for this inspection boundary.

### Capability and evidence semantics

- `ok` means the payload was parsed, not that every field is known or the deck
  is legal. `completeness` reports `complete` or `partial` independently.
- Confirmed zero stays `0`. An absent inventory field is `null`, including an
  absent wildcard rarity. An absent inventory object is `not_found` with null
  data; an explicit empty inventory object is a partial all-unknown snapshot.
- Ownership is always `unsupported` with null data and `availability` set to
  `unavailable`. No `Collection` is manufactured from decks or quantity shapes.
- A malformed relevant capability returns `error` with null data; the other
  capability can still succeed. A malformed response envelope invalidates both.
- Source/event, server-response authority, event line, observation time,
  sequence ID and cache markers accompany the supported results. Diagnostics
  do not echo raw payloads, request IDs, deck IDs, or file-system account names.
- A valid timezone-aware `ServerTimeInternal` is normalized to UTC. Invalid or
  missing time remains unknown; file modification time is not substituted.
  Python timestamps preserve microsecond precision.
- `TotalVaultProgress` remains the raw Arena integer; `WcTrackPosition` is
  preserved separately. Cache markers are retained without ordering assumptions.
- The last received `StartHook` in file order wins. There is no fallback to an
  older valid response and no merging across responses, even if a later response
  lacks one capability. This does not establish account identity or continuity.

Exit status is 0 when both supported capabilities parsed, 1 when one is absent,
and 2 for a supported capability error. Partial results and unknown printing IDs
remain inspectable in the JSON; exit status 0 is not a completeness certificate.

### Saved decks

`DeckSummaries[].DeckIdInternal` joins `DecksInternal` by ID. Summary metadata,
attributes and `Format` are retained. Observed attributes such as
`{"name":"Format"}` without a value remain intact with unknown format. `MainDeck`, `Sideboard`, `CommandZone`, and
`Companions` remain separate ordered lists of printing IDs and quantities.
Missing zones are null; explicit empty arrays stay empty arrays. An empty record `{}` is tracked
as present with unknown zones, separately from a missing record. Unpaired
summaries/records are retained with missing portions unknown. Repeated card
IDs within a zone are observed in the live log; every row and quantity is
preserved without aggregating or overwriting entries.

`CardSkins` is preserved as cosmetic metadata and never counted as cards.
Optional database checking reports unknown printing IDs without dropping or
converting them to title IDs. Deck completeness describes explicit summary/name
and all four card zones, not a guarantee that Arena sent every saved deck.

Preconstructed decks, course decks, and `DeckUpsertDeckV3` requests/responses
are excluded. The inspection is the StartHook baseline at its observation time;
later outgoing requests are not treated as confirmed server-side deck contents.

The root `arena_log.py` embedded-JSON scanner is reused with strict event and
top-level-object checks. Its legacy ownership heuristics and old inventory
aliases are not used by the new provider. The legacy CLI remains unchanged.

### Persistence and ownership limits

`canonical.load_decks()` still upserts deck/card rows without removing cards
or decks missing from later snapshots. Calling it repeatedly is not full
snapshot replacement. This pass does not call or change it. Companion handling,
omitted-zone policy, account isolation, freshness persistence, and removal
semantics require review before a persistence pass. The existing database
builder also rebuilds account tables rather than preserving imported state.

Wildcard-budget validation now requires explicit collection data before it
calculates costs. Missing ownership returns `collection_required` and no cost;
an explicitly supplied empty collection remains a known-empty input. This is a
capability guard on the existing validator, not new affordability functionality.
For a required rarity, an explicitly supplied zero can produce `wildcard_shortage`;
an absent count produces `wildcard_inventory_incomplete` instead. The known
card demand remains visible, but incomplete inventory never establishes affordability.

### Verification

```powershell
python -m unittest tests.test_arena_log -v
python -m unittest tests.test_workbench -v
python -m unittest discover -v
python test_mtgadb.py
python workbench.py validate examples/sample_deck.txt --format Timeless
```

The Arena tests use the privacy-safe `tests/fixtures/arena_log_redacted.log`.
Account/request/deck identifiers are synthetic/redacted and user-created deck
names are synthetic. Some non-identifying structural/sample values reflect
observed Arena payloads. Tests do not require a running Arena client or write
to a real log.

## Immutable Arena snapshot archive (pass #2)

The archive is separate from canonical schema version 2 and defaults to
`arena_snapshots.db`. Its own store schema and payload serialization versions
are both 1. Canonical rebuilds and ownership/deck loaders do not use this archive.

```powershell
python workbench.py save-arena "C:\Users\User\AppData\LocalLow\Wizards Of The Coast\MTGA\Player.log"
python workbench.py list-arena-snapshots
python workbench.py show-arena-snapshot <snapshot-id>
```

Each command accepts `--store <path>`. Saving generates an opaque ID internally;
repeated saves of the same observation create separate immutable records. Listing
uses local insertion order. Observation time and persistence time are separate;
unknown observation time remains null. Age alone is not proof of freshness.

One save archives inventory, decks, and unsupported collection together from the
provider's last received StartHook by file order. It never merges responses or
fills holes from earlier records. The reserved account key is always SQL NULL:
no account grouping, current-state promotion, replacement, or deletion occurs.
`complete` means observed fields satisfy the provider contract, not that Arena
sent the entire saved-deck roster. A deck absent later is not evidence of deletion.

Missing numeric fields remain JSON null, confirmed zero remains 0, absent zones
remain null, and explicit empty zones remain []. Card rows retain their order,
printing IDs, and duplicates. Error/unsupported capabilities have SQL NULL data.
A valid inventory and a deck error can be archived in the same observation.

Validation and serialization precede a single SQLite transaction containing the
header and all three capabilities. Success is reported only after commit. Save
exit status 0 means persisted, not complete: inspect the reported capability
statuses. Failures return exit status 2 without success output. A failed initial
creation can leave an empty file, but never a visible partial observation.
Unsupported store/payload versions fail explicitly; there is no automatic rebuild
or migration. The API provides no update/delete operation. Immutability is an
application contract, not protection against manually editing the SQLite file.

List/show open existing archives read-only and need neither Arena, Player.log,
nor the canonical database. Listing a missing archive reports no store without
creating it; showing a missing archive or ID fails. Output contains opaque archive
IDs, times, statuses, balances and zone counts, never saved-deck IDs or names.
Printing-ID database lookup is not performed by archive commands.

The private archive retains necessary deck IDs/names and reviewed deck fields
internally. It excludes raw logs, request/account IDs, and arbitrary StartHook or
summary metadata. Only reviewed attributes, cosmetic fields and provider evidence
are serialized. Do not publish the database: the default filename and SQLite
companion files are ignored by Git. A custom `--store` path should be outside the
repository or separately ignored. Content hashes detect accidental payload changes;
they do not authenticate the archive.

```powershell
python -m unittest tests.test_snapshot_store -v
```

Archive tests use temporary stores and the existing privacy-safe fixture. They do
not read the live log or retain real account/deck/request identifiers or names.

## Deterministic deck analysis

```powershell
python workbench.py analyze-deck examples/sample_deck.txt
```

This read-only command returns analysis Version 3 JSON with separate main, sideboard and
commander reports: land count, quantity-weighted nonland mana curve, card features,
role/theme copy counts, decomposed abilities, rules-text coverage, unsupported text
and main-deck interactions. Import errors
return exit code 2 without analyzing a silently truncated deck. Successful analysis
returns 0 and `legality: not_evaluated`; use `validate` separately for legality.

Each feature carries a stable rule ID, exact evidence, relationship and explanation.
Counts count each card copy once per label even when multiple rules support it;
labels overlap, so counts must not be summed to obtain deck size. Unknown printing
IDs produce partial coverage and resolved-card-only counts in the service API.

Functional-package model Version 1 derives reusable card jobs only from those
stable reviewed feature IDs. The current package vocabulary is threats,
interaction, card advantage, mana/ramp, protection, recursion, token production,
lifegain, +1/+1 counters, sacrifice, spell-matters, and graveyard interaction.
A card may contribute to several packages, and every contribution retains the
exact classified features that support it. Zone package counts count each card's
copies once per package even when several features support that same package.
Packages are functional descriptions, not archetype assignments: they do not
identify deck needs, create dependency edges, measure package sufficiency, score
synergy, compare candidates, or recommend cards. Those later reasoning layers
must consume the evidence rather than reinterpret card text.

The compatibility rules recognize narrow whole-line forms of targeted destruction,
draw, scry, counterspells, temporary protection, graveyard-to-hand recursion,
basic-land ramp/fixing, tapping nonlands for mana, and flexible mana production.
Positive-power creatures are labeled potential threats unless supported text is
exactly `Defender` or explicitly says that card/creature can't attack. A named
`As long as ..., <card> isn't a creature.` line instead produces a conditional
threat backed by that exact line (including Heliod-style devotion conditions);
the condition is retained but not evaluated. This is not a general combat evaluator.
Structured abilities conservatively separate reviewed triggers, costs, effects,
conditions, qualifiers and unsupported remainders. Reviewed creature-token forms
retain explicit quantity, power/toughness, color, creature type, artifact status,
and a bounded list of explicit token keywords. Treasure, Clue, Food, Blood, and
Map production is represented separately as named noncreature-token production;
it can satisfy a reviewed any-token-entry listener but never a creature-token-only
listener. No token use beyond these reviewed contracts is inferred. Exact whole-line
intrinsic flying, vigilance, trample, deathtouch, lifelink, haste, reach, menace,
defender, first strike, double strike, hexproof, and indestructible are recognized,
as is Ward with a braced mana cost. Granted, conditional, reminder-text, and other
qualified keyword forms remain unsupported, and a token's keyword is not assigned
to its source card. Exact `You gain N life` and `You gain X life` effects retain
their amount, while explicit opponent life gain remains structurally distinct and
does not become a friendly producer. Exact `Whenever you gain life` abilities are
listeners/payoffs, not gain-life events; lifelink remains an intrinsic keyword and
is not treated as guaranteed lifegain. Reviewed +1/+1 counter instructions retain
explicit quantity and distinguish the source permanent, a target creature, and an
explicitly different target creature. Exact self or controlled-creature +1/+1
counter-placement triggers are listeners/payoffs rather than producers.

Reviewed direct-damage forms retain an exact numeric or symbolic `X` amount,
distinguish the spell from the named source card, and separately represent target
creature, player, opponent, planeswalker, battle, each-opponent, and any-target
wording. Exact tap-activated damage is identified as an activated capability, but
activation frequency, available targets, and damage resolution are not evaluated.
These capability features do not imply removal, burn, threat quality, interaction
membership, or strategic value. Combat damage, prevention/replacement/redirection,
relative or divided damage, excess damage, multipliers, fight/bite mechanics, and
unreviewed trigger wording remain unsupported and visible.

Reviewed interaction forms also recognize exact targeted exile of a creature,
permanent, artifact, enchantment, or card from a graveyard; return of a target
creature or permanent to its owner's hand; target-opponent discard with an explicit
quantity; and target-opponent sacrifice of one creature or permanent. The structured
effects retain the reviewed target, quantity where present, controller information,
and explicit origin/destination zones. Graveyard-to-hand creature recursion keeps
its existing recursion and graveyard-consumer features, while temporary hexproof
and indestructible protection keep their existing protection behavior. The new
interaction capabilities do not imply removal quality, tempo, control, discard or
graveyard-hate archetypes, card advantage, strategic ranking, or recommendations,
and they add no interaction family.

Mass or delayed exile, blink/flicker, replacement exile, cards chosen from hand,
random or conditional discard, each-opponent wording, sacrifice costs and choice
edicts, mill, surveil, battlefield reanimation, flashback, escape, disturb, delve,
threshold, graveyard-size conditions, and unreviewed modal forms remain unsupported
and visible. This bounded vocabulary is not a general zone, modal, replacement-
effect, or Magic rules engine.

Structured abilities project the reviewed creature-token and named
noncreature-token producers, lifegain and +1/+1 counter producers/listeners,
token-entry draw payoff, instant/sorcery cast payoff and activated sacrifice/draw
outlet feature IDs. Themes also distinguish a Soldier typal bonus,
instant/sorcery cast payoffs, reusable sacrifice outlets and self-sacrifice costs.
Artifact/enchantment/land types establish membership only. Structural-recognition
coverage is separate from meaningful rules-text understanding. Replacement
effects, relative amounts such as `that much`, counter movement/removal,
proliferate, and other named counters remain unsupported. Conditions are retained,
not evaluated; no stateful
board evaluation, synergy score, or recommendation is produced. This is not a
general Magic rules engine.

Only three interaction families are recognized: a compatible reviewed token producer
with the token-entry draw payoff; a creature-token producer with the reusable sacrifice/draw
outlet; and an instant/sorcery with the supported spell-cast draw payoff. Every
interaction includes both classified features and states its prerequisites.
Shared themes alone never produce interactions. There is no numeric synergy score.

### Deterministic dependency support (Pass #5B)

Deck analysis includes dependency model Version 2. It derives zone-local support
facts from stable reviewed feature IDs, relationship values, and structured ability
context; it does not parse rules text again. The exact families are lifegain producer to
lifegain payoff, +1/+1 counter producer to counter payoff, compatible reviewed
token producer to token-entry payoff, creature-token producer to sacrifice consumer, and
instant/sorcery enabler to spell-cast payoff. The last three reuse the same exact
feature-side definitions as the existing interaction registry.

Strict families distinguish `supported`, `conditionally_supported`,
`payoff_without_enabler`, `payoff_without_compatible_enabler`, and
`enabler_without_payoff`; families with neither side are omitted. Main, sideboard,
and commander are calculated independently. Each side retains card identity, name,
quantity, qualifying feature IDs, and exact feature evidence. Copy totals count a
card once per side even if it has multiple qualifying feature rows. Dependency-relevant
evidence also retains structured triggers, costs, conditions, qualifiers, effect targets,
parse status, and unsupported remainder. A producer governed by a reviewed prerequisite
is conditional rather than silently equivalent to unconditional support.

Counter support is established only when the reviewed producer target can satisfy the
reviewed listener subject. Creature-token-only listeners accept only creature-token
production, while any-token listeners accept reviewed creature or named noncreature
token production. Missing-side alternatives use explicit `matching_semantics: any`.
Lifelink is not a lifegain producer and unsupported text creates no positive evidence.

Each dependency declares `strict` or `optional_support` policy. Creature-token
production remains a valid support/interaction route for a generic creature-sacrifice
outlet, but it is optional because ordinary creatures can also pay that cost. Its absence
is `optional_support_absent`, never a strict token-production need. No fodder count,
frequency, ratio, or other sufficiency heuristic is evaluated.

Dependency findings describe structural support among reviewed features. They do
not establish deck quality, package sufficiency, optimal ratios, archetype
correctness, or recommended changes. No scores, thresholds, candidate selection,
ownership filtering, or new rules-text interpretation are part of this model.

### Deterministic deck needs (Pass #5C)

Deck analysis includes needs model Version 2. It consumes the existing zone-local
dependency findings without rebuilding relationships or parsing rules text. A
`payoff_without_enabler` dependency produces a `support_need`: the reviewed payoff
or consumer has no reviewed matching enabler in that zone. An
`enabler_without_payoff` dependency produces an `unused_support_opportunity`, a
neutral observation that does not imply the deck needs a payoff. Supported
dependencies produce no needs finding. Conditional support produces a neutral
`conditional_support_observation`, and absent optional support produces an
`optional_support_observation`; neither is a strict need.

Each finding retains the source dependency identity and state, the existing and
missing sides, participating cards and quantities, exact qualifying feature
evidence, and an evidence boundary with zone resolution and rules-text coverage.
Main, sideboard, and commander remain independent. Imperfect coverage does not
suppress a deterministic finding and unsupported text is never guessed into a
relationship.

A support need means that a reviewed payoff or consumer lacks its reviewed
dependency counterpart in the analyzed zone. It does not prove that unsupported
card text cannot provide that support, and it does not identify which card should
be added. An enabler without a payoff is reported only as an opportunity
observation; it is not treated as evidence that the deck needs a payoff.

Version 2 adds no quantity-sufficiency or ratio analysis, generic package-count
heuristics, scores, candidate search, comparisons, or recommendations.

### Deterministic candidate pool (Pass #5D)

Candidate model Version 2 is an explicit, read-only service layered after deck
analysis. Call `services.candidates.discover_candidates(analysis, deck, con, ...)`
with an analysis containing Version 2 needs and the canonical database connection.
Database-wide discovery is not run by ordinary `analyze-deck`, so its cost is paid
only when candidate retrieval is requested.

Only `support_need` findings trigger discovery. An
`unused_support_opportunity`, optional-support observations, and conditional-support
observations never start a search, and unknown finding types fail
closed. The service takes the exact feature rule IDs and relationship from the
need's missing side, classifies each canonical card through the existing reviewed
classifier, and includes matches only when that exact classified evidence is
present. Explicit `any` matching allows any one reviewed alternative to satisfy a
missing-side contract; alternatives are not treated as conjunctive. It does not
parse or search rules text as semantic proof. Per-need pools
retain the source need and dependency, missing-side contract, and exact matching
feature evidence; the same canonical card may consequently appear in multiple
pools with separate provenance.

Candidates use canonical title identity and are ordered by normalized card name,
then title ID. Multiple printings do not create duplicate strategic candidates;
known printing IDs and the printing IDs eligible for an established format remain
visible as evidence. This ordering is neutral and is not a ranking. When an
explicit format is supplied, the existing format query and format rules establish
legal or illegal titles. With no format context, format eligibility remains
unknown and no candidate is described as format-legal. An explicit
`allowed_colors` context applies the existing color-identity subset rule; otherwise
color identity is marked not applicable rather than inferred from the deck.

Current quantities are counted across all deck zones. Cards already at their
existing deterministic title or format copy cap are excluded, while being below a
cap says nothing about how many copies should be added. Deck-size transactions are
not modeled. A commander-zone need also respects an established format's commander
allowlist, but the service never infers a commander or its color identity.

Ownership is `unknown` unless an explicit collection mapping is supplied. Known
zero ownership remains distinct from unknown ownership, and neither ownership
state filters the general pool. Saved decks are not treated as collection proof.
Wildcard and crafting eligibility are not evaluated. Missing format or printing
facts remain explicit unresolved eligibility dimensions; unknown is never reported
as a positive fact or silently treated as failure.

Candidate discovery is deterministic retrieval, not strategic selection. A
candidate is included because reviewed evidence matches a missing dependency
feature and applicable deterministic eligibility checks permit it. Candidate
presence does not mean the card is a good addition or should replace an existing
card. The model adds no score, tier, comparison, recommendation, replacement, or
deck mutation.

Cards whose relevant rules text is unsupported by the reviewed classifier may be
absent from the candidate pool even if a human Magic player would recognize them
as useful. The output therefore describes reviewed matches, not every possible
card that could support a need.

### Deterministic candidate facts (Pass #5E)

Candidate Facts Model Version 2 is the final deterministic evidence-aggregation
layer before future strategic judgment. Call
`services.candidate_facts.derive_candidate_facts(candidate_pools, con)` with Pass
#5D Version 2 output and the canonical database connection. It does not run during
ordinary `analyze-deck`, rediscover needs or candidates, or change eligibility.

The service classifies only the unique titles actually returned by #5D, rather
than reclassifying the full database. For every returned candidate it exposes
canonical title identity, mana cost and mana value, types and subtypes, colors and
color identity, power and toughness, and the printing-specific set, collector
number, and rarity facts already carried by #5D. Missing canonical values retain
their existing empty/null representation; no metadata parser is added.

Each per-need record preserves the analyzed zone, source need and dependency,
missing-side feature requirements, exact matching classifier evidence, complete
reviewed feature set, and every functional-package contribution with its exact
#5A evidence. Eligibility status, format and eligible-printing facts, explicit
color context, copy limit and current-deck quantity, ownership, crafting state,
and unresolved dimensions are copied from #5D without recalculation or semantic
change. Unknown ownership remains unknown, known zero remains known zero, and
crafting remains not evaluated when that is what #5D established.

A neutral canonical-title index also lists every matched need and dependency for
titles returned under multiple pools while retaining separate per-need evidence.
Duplicate printings never create duplicate title records. Source pool summaries,
including candidate limits and truncation, are preserved. Titles are ordered by
case-folded name and title ID; need and dependency IDs use stable lexical order.
Contradictory duplicate facts, malformed entries, unsupported trigger semantics,
and incompatible model versions fail clearly instead of being guessed into shape.

Candidate facts describe what is deterministically known about a candidate. They
do not assign strategic value to those facts. A candidate matching multiple needs
or contributing to multiple functional packages is not automatically better than
a candidate matching fewer. Strategic evaluation belongs to a later layer.

Version 2 adds no scoring, ranking, comparison, recommendation, replacement, deck
mutation, candidate rediscovery, eligibility reinterpretation, ownership or
crafting inference, diagnosis change, or new rules-text interpretation. Unsupported
capabilities remain absent even when a human player would recognize them.

Unrecognized wording, modal/conditional contexts outside the rules, and anomalous
canonical text are unsupported. A card may have recognized structural features
while its text or role remains unclassified. Empty text is reported explicitly.
Mana value is canonical printed-cost arithmetic, not casting feasibility; alternative
costs, card-face choices, source probabilities and game simulation are outside scope.
No ownership, crafting, archive conversion, automatic construction or replacement
is performed. Analysis never changes the database or validator rules.

```powershell
python -m unittest tests.test_intelligence -v
```

## Deterministic deck diagnosis (intelligence pass #2)

```powershell
python workbench.py diagnose-deck examples/sample_deck.txt
```

Diagnosis normalizes Versions 1, 2, and 3 analysis through their shared flat
feature and interaction contract. It does not parse card text,
validate legality, query ownership, or recommend changes. Output separates facts,
interpretations, warnings, and unknowns. Only the existing token-value,
token-sacrifice, and spells-matter interaction families can support a probable
plan. A plan is named only after its exact directional interaction, both
relationship sides, distinct-title support, scaled copy support, 95% resolution
coverage, and 50% strategic coverage all pass. Stronger evidence may raise the
confidence from moderate to high; close incomplete candidates remain ambiguous.

Unclassified and unresolved cards reduce confidence rather than count against a
deck. A recognized payoff or consumer with no matching source is reported as a
structural warning. Role redundancy and concentration are interpretations, not
quality warnings. Curve output is descriptive only. Mana-source adequacy and
mechanical conflicts are explicitly not assessed. When the gates do not support
a conclusion, the command returns `insufficient evidence to diagnose`.
