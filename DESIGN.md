# MTG Arena Deck Builder — Design

Status: **active implementation.** The canonical card/query foundation and
Phase 1 Offline Deck Workbench are implemented. Current limitations and the
next build boundary are tracked in `README.md`.

---

## 1. What the reverse engineering established

Findings are separated by how strongly the evidence supports them, because
the difference changes what we build.

### Proven

**Arena's 2026 client does not write the player's card collection to
Player.log.** Evidence:

- Detailed Logs confirmed `ENABLED`; scenes visited include `DeckBuilder`,
  which cannot render without ownership data.
- 691 card-id/quantity lists exist in one session's log. Largest: **190
  distinct cards** — all precon decks, set pools, booster pools. A real
  collection is thousands.
- A shape-agnostic scan for *any* container ≥400 entries found only
  `Formats.allowedTitleIds` lists, which resolve to 0/300 real cards.
- Opening `DeckBuilder` produced **53,271 bytes of log and zero
  request/response pairs.** The last server message predates the scene change.

The last point is the decisive one: the collection was already client-side.
It arrives in an **unlogged login payload and is memory-resident**.

**Card definitions are available offline.** Arena ships
`MTGA_Data/Downloads/Raw/Raw_CardDatabase_*.mtga` — SQLite, 19,683 primary
non-token cards, 209 sets. Verified by resolving a real 60-card deck out of
the log into names, costs, types, and a mana curve.

### Scoped to evidence, not proven

**No ownership cache has been found after inspecting the expected
locations** — `AppData\Local`, `LocalLow`, `Roaming`, and the install tree,
plus a check for files modified during collection browsing.

This is *not* proof that no cache exists. Not yet searched: LevelDB/RocksDB
style stores, Unity `PlayerPrefs`/registry persistence, and any binary blob
whose contents were never decoded (the search matched JSON structure and
filenames only). If ownership extraction becomes critical, those are the
remaining stones to turn over.

### Ruled out on principle

Memory inspection and TLS interception would both work. Both are against
Arena's terms and would break on every patch. Not pursuing either.

### Oracle text — resolved, with a trap

**Oracle text is reconstructable.** `Cards.AbilityIds` holds `abilityId:textId`
pairs; each `textId` resolves through `Localizations_enUS` at `Formatted=1`.
Verified against ground truth — Sheoldred returns exactly
`Deathtouch` / `Whenever you draw a card, you gain 2 life.` /
`Whenever an opponent draws a card, they lose 2 life.`

Two consequences found while verifying:

**Ability text is per-printing, and printings disagree.** Measured across the
whole database:

| | count |
|---|---|
| distinct card titles | 15,813 |
| titles with >1 printing | 1,951 |
| titles whose printings **disagree** on text | **20** (1.0% of multi-printing) |
| of those: clear majority exists | 5 |
| of those: **exact tie, `mode()` cannot pick** | **15** |

Rare, but `mode()` alone resolves only a quarter of it. Inspecting all 20:
the dominant cause is **Universes Beyond reskins sharing a `TitleId` with the
original**. `Lightning Bolt`/`Thrum of the Vestige`, `Venser`/`Master Xande`,
`Purphoros`/`Kefka Palazzo`, `Ragavan`/`Zidane Tribal`, `Lorthos`/`Doc Ock`.
Sets `FCA` and `MAR` account for most.

That suggests preferring text that **self-references the card's own name**,
which correctly rejects every reskin. But it has a genuine counterexample:
**Terramorphic Expanse** — three printings read *"Sacrifice this land"*
(current Oracle templating) and one reads *"Sacrifice Terramorphic Expanse"*
(older wording). Self-reference picks the wrong one there.

**No single rule wins.** Resolution order, with the loser recorded either way:

1. Majority text among printings (`mode`), when one exists.
2. Otherwise prefer text that self-references the title's short name, treating
   generic self-reference (*"this land"*, *"this Vehicle"*) as valid.
3. Otherwise flag `anomaly` and do not guess.

Not "newest printing" — recent sets deliberately change wording (templating
updates, UB flavor renames), so recency is anti-correlated with correctness.
Because n=20, the unresolved set is small enough to carry as an explicit
reviewed list rather than a silent heuristic.

**Mana symbols inside text use the same `o` encoding as costs** — Llanowar
Elves returns `{oT}: Add {oG}.` Normalization belongs in the projection layer,
once, not at every call site.

---

## 2. Operating modes

The application must be useful even if ownership extraction is never solved.
Modes are a first-class concept, not a flag.

| Mode | Ownership input | Question it answers |
|---|---|---|
| `FULL_COLLECTION` | exact per-card quantities | "Best deck from cards I own." |
| `WILDCARD_BUDGET` | wildcard + currency counts | "Best deck I can afford to craft." |
| `UNLIMITED` | none | "Strongest deck in the format, ignore ownership." |

`UNLIMITED` and `WILDCARD_BUDGET` both work **today** — wildcards and
currency *are* logged (26/25/14/16, 2350 gold, 250 gems). `FULL_COLLECTION`
waits on an ownership provider. This is why ownership must not be a hard
dependency anywhere below the service layer.

---

## 3. Core data model

Immutable dataclasses. No ORM.

**A card name is not a card identity.** The printing measurement forces a
two-level model: collapsing printings early destroys the evidence that the
source data disagreed.

```python
CardPrinting:      # one row per Arena grpId -- what you actually own
    arena_id, title_id, set_code, collector_number,
    rarity, raw_rules_text

CardCanonical:     # one row per distinct card -- what you build decks with
    title_id, name, mana_cost, cmc, types, subtypes,
    colors, color_identity, power, toughness,
    canonical_rules_text,
    source_printing_id,       # which printing the text came from
    resolution: MAJORITY | SELF_REFERENCE | ANOMALY
    anomaly_count             # printings disagreeing with canonical

Collection:  cards: dict[int, int]        # arena_id -> quantity owned
             source: str, observed_at: datetime
Inventory:   wildcards: dict[str, int]    # common/uncommon/rare/mythic
             gold: int, gems: int, vault_progress: int
Deck:        deck_id, name, main: dict[int,int], sideboard: dict[int,int],
             commander: dict[int,int]
```

The query engine reads `CardCanonical`. Diagnostics read `CardPrinting`.
Provenance (`source_printing_id`, `resolution`) is stored, never inferred.

This split is also required by a hard rule of the game, independent of the
text problem: **ownership is per printing, the 4-copy limit is per name.**
Four Lightning Bolts across `STA` and `J21` are four distinct `arena_id`s but
still a single playset. A one-level model gets deck legality wrong.

### Symbol normalization happens exactly once

Mana symbols arrive `o`-encoded in both costs and rules text (`{oT}: Add
{oG}.`). Normalization belongs in the projection layer that writes
`CardCanonical` — never in query predicates, deck logic, or export. The
failure mode of getting this wrong is `{oG}`, `{G}`, and `green` all meaning
the same thing in different modules six months from now.

The same layer strips **UI markup**, which is pervasive: 924 card names and
20,657 localization strings carry tags — `<nobr>` (`<nobr>Cori-Steel</nobr>
Cutter`), `<i>` around flavor and reminder text, plus `<sprite>`, `<b>`,
`<s>`, `<sup>`, `<cspace>`. Strip the tags, keep the inner content.
`<sprite name="arena_a">` is the Alchemy-rebalance badge and carries no text —
dropping it loses nothing, since `Cards.IsRebalanced` already states it.

**No `oracle_id`.** It is a Scryfall concept; Arena exposes `GrpId`/`TitleId`
and no oracle UUID. Adding it means a deliberate Scryfall name-join with its
own network dependency — a decision to make explicitly, not a NULL column
that looks populated.

---

## 4. Provider interfaces

Interfaces describe **what the app needs**. Implementations describe **where
it came from**. Adding a source must never touch a consumer.

```python
class CardDatabaseProvider(Protocol):
    def iter_cards(self) -> Iterable[Card]: ...
    def fingerprint(self) -> str          # detects Arena content updates

class OwnershipProvider(Protocol):
    def get_collection(self) -> ProviderResult[Collection]: ...

class InventoryProvider(Protocol):
    def get_inventory(self) -> ProviderResult[Inventory]: ...

class DeckProvider(Protocol):
    def get_decks(self) -> ProviderResult[list[Deck]]: ...
```

Note these are **four capabilities, not one**. `Player.log` implements
inventory and decks but *not* ownership — a single `LogProvider` would have
forced it to fail wholesale at something it partly does well.

### Failures return diagnostics, never exceptions

```python
@dataclass
class Diagnostics:
    source: str                    # "Player.log"
    status: Status                 # OK | NOT_FOUND | UNSUPPORTED | ERROR
    evidence: dict[str, Any]       # blobs_scanned, largest_card_list,
                                   # scenes_visited, detailed_logs
    next_steps: list[str]          # actionable, ordered

@dataclass
class ProviderResult(Generic[T]):
    data: T | None
    diagnostics: Diagnostics
```

Wizards will break us. When they do the output is a report, not a stack
trace — evidence counts, what was searched, which provider to try next.

### Planned implementations

| Class | Capability | State |
|---|---|---|
| `ArenaSQLiteCardProvider` | cards | works |
| `PlayerLogInventoryProvider` | inventory | works |
| `PlayerLogDeckProvider` | decks | works |
| `PlayerLogOwnershipProvider` | ownership | returns `NOT_FOUND` + evidence |
| `ManualImportOwnershipProvider` | ownership | planned |
| `UntappedOwnershipProvider` | ownership | speculative — verify export exists |

`PlayerLogOwnershipProvider` is kept deliberately. It encodes §1 as running
code and will start working by itself if Wizards restores the payload.

---

## 5. Canonical database

One SQLite file. Every feature becomes SQL.

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
-- schema_version, created_at, arena_db_fingerprint, provider provenance

-- What you build decks with. One row per distinct card.
CREATE TABLE cards (
    title_id       INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    mana_cost      TEXT,          -- normalized '{2}{G/W}{U}'
    cmc            INTEGER,
    types          TEXT,          -- 'Creature'
    subtypes       TEXT,
    colors         TEXT,
    color_identity TEXT,
    power          TEXT,          -- text: '*' is legal
    toughness      TEXT,
    rules_text     TEXT,          -- canonical, symbols normalized
    source_printing_id INTEGER,   -- which printing the text came from
    resolution     TEXT,          -- majority | self_reference | anomaly
    anomaly_count  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_cards_name ON cards(name);
CREATE INDEX idx_cards_cmc  ON cards(cmc);
CREATE INDEX idx_cards_ci   ON cards(color_identity);

-- What you actually own. One row per Arena grpId.
CREATE TABLE printings (
    arena_id         INTEGER PRIMARY KEY,
    title_id         INTEGER NOT NULL REFERENCES cards(title_id),
    set_code         TEXT,
    collector_number TEXT,
    rarity           TEXT,        -- basic|common|uncommon|rare|mythic
    raw_rules_text   TEXT         -- pre-canonicalization, for diagnostics
);
CREATE INDEX idx_printings_title ON printings(title_id);

CREATE TABLE ownership (
    arena_id INTEGER PRIMARY KEY REFERENCES printings(arena_id),
    quantity INTEGER NOT NULL CHECK (quantity >= 0)
);
-- Playset limits aggregate through printings.title_id, never arena_id:
-- 4 Lightning Bolts across STA and J21 are 4 arena_ids but one playset.

CREATE TABLE wildcards (rarity TEXT PRIMARY KEY, count INTEGER NOT NULL);
CREATE TABLE inventory (key TEXT PRIMARY KEY, value INTEGER NOT NULL);

CREATE TABLE decks (
    deck_id TEXT PRIMARY KEY, name TEXT, source TEXT
);
CREATE TABLE deck_cards (
    deck_id  TEXT REFERENCES decks(deck_id),
    arena_id INTEGER REFERENCES cards(arena_id),
    zone     TEXT,            -- main | sideboard | command
    quantity INTEGER NOT NULL,
    PRIMARY KEY (deck_id, arena_id, zone)
);
```

`cards` duplicates data Arena already has. That is deliberate: Arena's schema
needs a localization join and enum lookup for *every* field, and its filename
hash changes each set release. This is a **materialized projection**, rebuilt
when `arena_db_fingerprint` changes.

### Snapshots

Each build writes `snapshots/snapshot_YYYY-MM-DD.db`; `current.db` points at
the newest. Enables diffing collections over time, detecting Arena format
changes, and reproducing bugs against the exact data that caused them.

---

## 6. Module boundaries

```
mtgadb/
  model.py              Card, Collection, Inventory, Deck, Diagnostics
  providers/
    base.py             the four Protocols + ProviderResult
    arena_cards.py      ArenaSQLiteCardProvider
    player_log.py       inventory / decks / ownership-not-found
    manual_import.py    ManualImportOwnershipProvider
  canonical.py          schema, build, refresh, snapshots
  query.py              CardQueryEngine
  modes.py              OperatingMode
services/
  exporter.py           MTGA import/export format
  validator.py          deterministic deck rules and resource checks
workbench.py             offline command-line interface
```

Dependencies point downward only. `query.py` never imports a provider;
`services/` never touches Arena's files.

### Query engine comes before the deck builder

Every future feature asks the same question in different clothes:

```python
engine.find(colors=["G"], cmc__lte=3, types="Creature",
            format="Standard", owned_only=True)
```

Deck Builder, Draft Helper, Collection Tracker, Wildcard Advisor and Meta
Analyzer are all clients of this. Building it first means the deck builder is
a thin consumer instead of the thing that owns all the logic — which is
precisely how the reference repo went wrong.

---

## 7. Deterministic vs LLM

The reference repo asked one LLM call to pick ratios, select cards, validate
legality, *and* self-review. That is why it produced schema mismatches.

**Python owns — LLM never decides:**
format legality · ownership and quantities · playset caps · color identity ·
mana curve arithmetic · wildcard cost · deck size validation · export format

**LLM owns:**
archetype and game plan · synergy reasoning · preference *among cards already
proven legal and owned* · sideboard rationale · natural-language explanation

**The contract:** the LLM receives a pre-filtered candidate pool and returns
choices. It never sees a card it cannot legally play. Its output is validated
by the deterministic layer and repaired or rejected on violation — never
trusted directly.

Claude should never need to wonder whether you own a card.

---

## 8. Build order

1. [x] `model.py` + `providers/base.py`
2. [x] `canonical.py` + `ArenaSQLiteCardProvider` → populated `cards` table
3. [x] **Verify the oracle-text unknown (§1)** — gates §5 text predicates
4. [x] `query.py` — the foundation
5. [x] `modes.py` + deck import/export + deterministic validation
6. [ ] `player_log.py` providers → inventory, decks, format diagnostics
7. [ ] Manual ownership import workflow
8. [ ] `services/deckbuilder.py` — first LLM call

Steps 1–6 involve no LLM and no network.

---

## 9. Open questions

**Closed:** oracle text *is* reconstructable (§1) — this was the gating
assumption for the query engine, and it held.

**Closed: format legality is fully derivable offline.** The `Formats` payload
carries 138 formats, each with `legalSets`, `mainDeckQuota`, `sideBoardQuota`,
`commandZoneQuota`, and — as *optional* keys present only where they apply —
`bannedTitleIds`, `allowedTitleIds`, `supressedTitleIds` (Wizards' spelling),
`individualCardQuotas`, `AllowedCommanderTitleIds`, `useRebalancedCards`.
Verified by resolving ban lists to names: Standard 11 bans (Cori-Steel Cutter,
Heartfire Hero, Monstrous Rage, Vivi Ornitier…), Historic 77 (Brainstorm,
Blood Moon, Ancient Tomb, Chrome Mox…).

Two consequences: **bans are keyed by `TitleId`**, independently confirming the
two-level identity model of §3; and this payload lives in `Player.log`, so a
`FormatProvider` is a fifth capability sourced from the log — not from the card
database.

1. **Ownership ingestion.** Does any tracker still export collections?
   Determines whether `FULL_COLLECTION` is ever reachable. This is now the
   single gating problem; everything else has a path.
2. **Scryfall dependency.** Accept it for `oracle_id`, pricing and meta data,
   or stay fully offline? Currently offline and nothing requires breaking that.
4. **The 20 anomalies.** Carry as a reviewed constant, or re-derive on each
   rebuild and fail loudly when the count changes? Leaning re-derive — a
   change in that number is a signal Wizards altered something.
5. **Recommendation scoring.** Synergy evaluation is the genuinely hard
   problem and is deliberately unspecified here. Keyword matching is a
   placeholder, not a design.
