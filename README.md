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

## Deterministic deck analysis (intelligence pass #1)

```powershell
python workbench.py analyze-deck examples/sample_deck.txt
```

This read-only command returns versioned JSON with separate main, sideboard and
commander reports: land count, quantity-weighted nonland mana curve, card features,
role/theme copy counts, unsupported text and main-deck interactions. Import errors
return exit code 2 without analyzing a silently truncated deck. Successful analysis
returns 0 and `legality: not_evaluated`; use `validate` separately for legality.

Each feature carries a stable rule ID, exact evidence, relationship and explanation.
Counts count each card copy once per label even when multiple rules support it;
labels overlap, so counts must not be summed to obtain deck size. Unknown printing
IDs produce partial coverage and resolved-card-only counts in the service API.

The initial rules recognize narrow whole-line forms of targeted destruction,
draw, scry, counterspells, temporary protection, graveyard-to-hand recursion,
basic-land ramp/fixing, tapping nonlands for mana, and flexible mana production.
Positive-power creatures are labeled potential threats unless supported text is
exactly `Defender` or explicitly says that card/creature can't attack; this is
not a general combat evaluator.
Themes distinguish an exact Soldier-token producer from a token-entry draw payoff,
+1/+1 counter placement, a Soldier typal bonus, instant/sorcery cast payoffs,
reusable sacrifice outlets and self-sacrifice costs. Artifact/enchantment/land
types establish membership only. This is not general rules-text parsing.

Only three interactions are recognized: the supported creature-token producer
with the token-entry draw payoff; that producer with the reusable sacrifice/draw
outlet; and an instant/sorcery with the supported spell-cast draw payoff. Every
interaction includes both classified features and states its prerequisites.
Shared themes alone never produce interactions. There is no numeric synergy score.

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
