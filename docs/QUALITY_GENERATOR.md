# Generator B — technical implementation reference

Do not read implementation details or sealed reports before masked grading if
you want to minimize familiarity bias. The grading-safe report is the handoff.

Generator B is opt-in and separate. Generator A and the independent validator
remain unchanged. It uses the same pinned Run 20E legality snapshot, canonical
candidate database, raw Arena basic verification, and Run 21 `prepare` exclusions.
Every additional filter only removes candidates. Unknown/illegal cards never
enter selection. Missing identity fails closed. Nonbasic lands remain deferred;
unsupported mana symbols (including hybrid casting costs) remain deferred by A's
data rules even though v2 color accounting correctly understands hybrid colors.
The registration is docs/evaluation_requests_v2.json, committed before this code.

```powershell
python -B generate_quality_deck.py --format Standard --queue BO1 --output C:\MTG-Source-Preservation\evaluations\new-output
python -B generate_quality_deck.py --format Standard --queue BO1 --color-pair WU --output C:\MTG-Source-Preservation\evaluations\new-wu
```

Only the five registered requests are accepted. Colors WU, BR and GW are BO1-only.
For unrestricted requests, consider all mono/two-color sets in WUBRG order. Known
canonical identity, canonical colors and colored symbols in costs/rules must fit
the set. Requested pairs also enforce their registered main-deck eight-copy quota.

## Fixed template

24 verified basic lands, 24 creatures and 12 other nonlands, exactly 60 main.
Mana bucket 4 means 4+. BO1 has no sideboard. BO3 fills 15 where capacity permits.

| Bucket | Creatures | Other nonlands |
|---|---:|---:|
| 1 | 4 | 0 |
| 2 | 8 | 4 |
| 3 | 8 | 4 |
| 4+ | 4 | 4 |

For each color set, slot sufficiency is min(available copy capacity / demand, 1).
Primary sufficiency is the mean of seven fractions. Only fully sufficient sets
with verified basic sources proceed; excess pool size earns no primary credit.
Legendary permanents are capped at two copies across main and sideboard; other
nonbasics at four. This redundancy limit is a heuristic, not a Magic legality rule.

Minimum recognized interaction is four copies, or all template-compatible legal
copy capacity when fewer than four exist. Interaction is reviewed role removal or
counterspell, or reviewed targeted destroy/exile abilities. It is a lower bound,
not complete threat coverage. The template check enforces the selected minimum.

## Recognized evidence and scoring

Use unchanged Analysis 5 `classify_card` and Dependencies 4 `dependency_findings`.
Edges are existing compatible enabler/payoff pairs with unconditional or conditional
support; unestablished scope and self-pairs are excluded. No invented lexical theme
recognition. Known dependencies cover lifegain, counters, tokens, sacrifice, spells
and qualified deathtouch poison. Unrecognized typal/landfall relationships remain
neutral. Conditional edges represent potential support, never established gameplay.

Per-copy contributions are recalculated against the other selected cards:

- `curve.efficiency`: minus mana value; general heuristic.
- `body.printed_stats`: min(4, (printed numeric power + toughness)/2) for creatures,
  zero if stats are missing/non-numeric; general heuristic, not ability recognition.
- `interaction.reviewed`: +4 when reviewed interaction exists, with recognized text.
- `coherence.compatible_dependencies`: +2 per other participating copy, capped at
  +12 per evaluated card, deduplicating partners. Evidence includes involved IDs and
  existing dependency facts. No points for unsupported text.
- `hybrid.shared_enabler_or_payoff`: +2 when the evaluated card participates in at
  least two distinct active dependency families. Themes must share that card;
  unrelated themes merely coexisting receive no hybrid bonus.
- `resources.context_gate`: +2 only if other selected cards contain at least six
  copies of mana value >=5 creatures or recognized-ability payoffs, or four copies
  with recognized activated mana costs and effects; otherwise -3. Applies to
  reviewed mana-production/card-flow permanents. Sinks are potential uses, not
  proof of profitable activation. Resource-dependent coherence edges are also
  suppressed if either resource endpoint fails this gate. Independent body or
  interaction evidence may still have value; unsupported resource words do not.

Each contribution records rule identifier, numeric contribution, evidence source
(`recognized card text`, `inferred relationship`, or `general heuristic`) and
details. Recognized resource features carry exact reviewed rule/text evidence;
context weighting itself is a general heuristic. Unknown abilities get no positive
evidence or coverage bonus. General printed-stat and curve heuristics are separate.

Explore an empty seed and the first eight compatible pair seeds sorted by title
IDs, a bounded search rather than exhaustive strategy optimization. Reserve the
interaction minimum, then each requested color quota, then fill remaining slots
in table order. Every step selects highest current score, then lowest mana value,
then title_id. No names influence selection. Seeds that cannot complete fail;
no constraints relax. Choose the complete deck with highest sum of copy-weighted
scores, then fewer colors, WUBRG order, then expanded sorted title IDs. The search
can miss a feasible or better configuration; failure is explicit, not permission
to weaken the template. Quality improvement remains unproven until human grading.

Land allocation uses main casting colored pips, floors proportional shares of 24,
then largest remainder with WUBRG ties. It does not model activation affordability
or casting probabilities. Basic printing selection is inherited from A. BO3
sideboard prefers reviewed interaction, then context score against main, mana
value and title_id, enforcing combined limits. Shortfalls are explicit.

## Evidence, reproducibility and worksheets

Text-only independent validation, B template checking and registered color checking
must each pass. Same inputs/runtime bytes produce identical deck/report bytes.
Runtime hashes are in reports; final commit is separate run metadata. No competitive
strength or live Arena queue acceptance is claimed.

Worksheet presentation uses unchanged Run 22 flags and rubric 1. All seven have
the same fields and flag sets; no origin, source paths, contributions, timestamps
or generator explanations. Four labels use HMAC-SHA256 over opaque IDs with a
fixed private 32-byte salt recorded only in the sealed external key; sort digest,
then opaque ID, assign A-D. This is deterministic given the saved private salt.
Three open labels use request IDs. Masked files share one neutral folder, uniform
timestamps and neutral names. Their deck contents may be recognized: masking is
not blindness. The operator has already seen A's decks. This is a separate v2
grading session; frozen Run 22 v1 grades remain official and are never merged.

Version-1 ingestion/compare behavior is not changed. This run creates blank v2
worksheets only, not grades or results. A future authorized v2 ingestion must honor
the registered five-request set and unchanged rubric/protocol rather than passing
v2 worksheets into the v1-only interface. No origin key enters Git.
