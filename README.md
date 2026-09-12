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

Still planned:

- Player log providers for decks, wildcards, and currency
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
