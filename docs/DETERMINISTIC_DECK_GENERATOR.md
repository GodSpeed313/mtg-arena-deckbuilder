# Deterministic Standard deck generator, Milestone 1

Opt-in only. No existing defaults, analysis versions, legality interface, or tests
are changed. Required user decisions are Standard and BO1/BO3; output location is
storage configuration. No collection, playstyle, existing list, AI or strategy input.

```powershell
python -B generate_standard_deck.py --format Standard --queue BO1 --output C:\MTG-Source-Preservation\decks\new-bo1
python -B generate_standard_deck.py --format Standard --queue BO3 --output C:\MTG-Source-Preservation\decks\new-bo3
```

The CLI accepts only new external directories and pinned Run 20E snapshot/candidate
data. It also pins the preserved raw Arena database, necessary to independently
recheck basic-land supertypes. Hash mismatch fails. All SQLite access is mode=ro
with query_only enabled and verified by the unchanged legality reader.

## Fixed template and selection

Exactly 24 basic lands and 36 nonlands, including exactly 24 creatures:

| Mana value | Creatures | Other nonlands | Total |
|---|---:|---:|---:|
| 1 | 8 | 0 | 8 |
| 2 | 8 | 0 | 8 |
| 3 | 4 | 4 | 8 |
| 4 | 4 | 4 | 8 |
| 5+ | 0 | 4 | 4 |

Every mono-color and two-color combination is measured. A card's required colors
are the union of canonical colors and colored cost symbols. Colorless generic
costs are eligible; explicit {C}, hybrid/Phyrexian/snow/variable costs and zero-cost
nonlands are deferred and counted, not silently treated as ordinary mana.

Each of the seven slot sufficiencies is min(available legal copy capacity / slot
demand, 1). Creature sufficiency is min(creature copy capacity / 24, 1). Primary
score is their mean, with equal 1/8 weights. Secondary score is 3/4 understood-card
share + 1/4 min(recognized-removal title count / 4, 1). Exact rational arithmetic is
used. Scores are compared lexicographically, so secondary cannot compensate for
insufficiency. Only combinations with every slot filled and verified basic sources
are feasible. Final combination ties prefer fewer colors then WUBRG order. Larger
pools receive no primary credit past sufficiency and no removal credit past four.

Within each disjoint slot, candidates rank by understanding (all ability lines
supported; some supported/partial; otherwise not understood), then ascending mana
value for curve fit, then title_id. Take four copies unless the remaining slot
requires fewer. Recognition uses the existing grammar 5 parser, unchanged. No
ability text/empty classification counts as not understood. Recognized destroy or
targeted exile is a lower bound on removal, limited by grammar coverage; it is not
exhaustive strategic knowledge. BO3 sideboard ordering prefers this recognized
removal, then the same rank, filling up to 15 from remaining combined copy capacity.
Any shortfall is explicit. BO1 has no Sideboard section.

Printings must have an unambiguous exact set/collector key. Printing ties use set,
collector string, Arena ID, then title_id. Basic-card ties use title_id. The 24-land
allocation uses counted colored pips in selected main-deck nonlands, floors the
exact proportional allocations, then distributes residual lands by descending
fractional remainder, ties WUBRG. Only independently Arena-verified mono-color basic
sources are selected; nonbasics are deferred. This is a simple pip proportion, not
a casting-probability or competitive mana-base model.

## Evidence and exclusions

Only snapshot legal titles are considered. Unknown and illegal remain excluded.
Missing name, identity, type, colors, mana value, nonland cost or export printing
is counted. Empty canonical colors is an explicit colorless value only when present;
None is missing. A basic land has no casting cost by card type and is not assigned
a zero-cost nonland slot. Nonland mana value must agree with supported cost symbols.
Missing rarity stays unknown. Rarity totals include main/side quantities; basic
lands are separately exempt from wildcard quantities. Owned cards are not known.

Special deckbuilding text is excluded case-insensitively using the explicit markers
in deck_construction_data.MARKERS: a deck can have; your deck can have; companion;
starting deck; deck construction; deckbuilding; deck-building; build your deck;
building your deck; deck must contain; deck may contain; deck can contain. Each
matched marker is reported. This bounded safeguard does not interpret special
rules or claim to recognize every possible wording.

## Three separate results

The generator's template check measures lands, creatures, curve slots and main size.
The independent generated_deck_validator imports only authoritative data access,
not generator code, objects, rankings, template or results. It receives exported
text and independently resolves exact set/collector, checks the name, rereads the
pinned legality store and raw basic facts, counts all zones, applies four-copy
limits and special-text exclusions, and reports every observed violation.

Legality validation PASS is scoped to these pinned dated sources and construction
rules. Template compliance PASS is structural only. Arena compatibility remains
pending operator testing of both import and entry to the matching queue. Import
alone does not establish queue legality. Neither status establishes competitiveness.

This deck is legal and structurally coherent by the generator's documented rules. It has not been evaluated for competitive strength.

## Reproducibility and outputs

deck.txt and report.json are canonical UTF-8/LF content. Reports include every color
combination, exclusions, separate checks, source paths/hashes and a source revision
map (SHA-256 of the four runtime files). This revision does not change when the
tested files are committed. Timestamps, output path and final commit are recorded
separately in run-metadata.json after the authorized commit. Generation writes no
repository data. Replays must compare deck and report bytes independently.
