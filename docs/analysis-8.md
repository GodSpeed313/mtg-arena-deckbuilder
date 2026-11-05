# Analysis 8: opt-in, partial keyword recognition

Analysis 5 remains the default everywhere. Analysis 6/7 retain Analysis 5 and
their original parser behavior. No downstream consumer, evidence acceptance,
default version or persisted identity changes in this run.

## Explicit selection and isolation

Use `services.analysis8.decompose_abilities8` for whole-card recognition or
`parse_entry8` for one already-normalized source entry. They invoke the existing
shared parser with `grammar_version="8"`; its default is still `"5"`. Other
versions fail closed. The grammar is not copied. Whole-card modal and canonical
anomaly guards run before new recognition and remain intact.

`analyze_deck8(deck, readonly_connection)` returns the unchanged Analysis 5 result
plus `recognition_analysis`, with `grammar_version: "8"`. That layer contains
per-title entries and distinct/copy-weighted recognition coverage for each zone.
The top-level `analysis_version` stays "5" because it identifies the unchanged
base evidence, not the new recognition layer. The layer states its persistence
limit and downstream provenance explicitly. It is not an Analysis 8 persisted
evidence identity and must not be relabeled as one.

Dependencies 4 and Needs 3 remain derived from Analysis 5. No new keyword feature
projection is sent to them. Needs includes coverage in its evidence boundaries;
feeding it the new coverage would alter historical evidence. This is deliberately
a recognition/measurement capability, not a downstream migration or evidence
export. Every base field must be byte-identical after removing only the new layer.
No downstream version-identity exceptions are needed.

## Status standard and representation

Existing `services.abilities._ability` calls `_status`: no recognized component
is unsupported; recognized component(s) plus an explicit unsupported remainder
is partial; recognized component(s) without a remainder is supported. Existing
Flying/Deathtouch use exact intrinsic self-keyword records and no remainder.
README's ability discussion also distinguishes structural recognition from full
understanding. Those precedents do not establish complete recognition of the
new families. None of these four is assigned supported.

All new records use `kind="keyword_ability"`, a recognition-only kind, an exact
self-subject Keyword, and a whole-line UnsupportedRemainder with scope "ability"
and reason `analysis8_unmodeled_<family>`. This preserves incompleteness rather
than presenting Equip/Crew as static effects. There are no new effects, trigger
claims, battlefield states or simulation rules. The accurate bounded component
and retained unmodeled semantics meet the existing partial standard.

| Exact case-sensitive shape | Recorded component | Status and unmodeled behavior |
|---|---|---|
| `Flash` | Keyword flash, subject self | partial; casting timing/application unmodeled |
| `Enchant creature` | Keyword enchant, value creature | partial; attachment restrictions/legality unmodeled |
| `Equip {symbol}` | Keyword equip with exact cost; Cost mana/self/activated with exact evidence | partial; attachment effect, target, restrictions and timing unmodeled |
| `Crew <digits>` | Keyword crew with literal decimal spelling; recognition detail includes integer | partial; tapping requirement/activation and resulting effect unmodeled |

Equip accepts exactly one braced symbol: digits, W/U/B/R/G/C/X/Y/Z, colored hybrid
W/U-style, or 2/W-style. The literal full regex is in the version-8 parser gate.
Phyrexian symbols, tap/untap/energy, nonmana costs and qualified Equip are excluded.
The 292 Run 18 Equip candidates mean the single-symbol `Equip {M}` template.
Another 28 observed multi-symbol mana-only costs are deliberately deferred;
they remain unchanged despite belonging to the broader mana-only family.

Crew accepts ASCII digits only, including zero and leading zeros; it retains the
original spelling and reports the integer separately. Neither the number nor the
keyword implies a legal activation or available creatures. No card type is
inferred from the line. Recognition is literal whole-line shape only.

## Explicit exclusions

Lowercase/capitalization variants, reminder suffixes, comma-separated keyword
lists, granted keywords, larger sentences, qualified or other Enchant objects,
multi-symbol Equip, nonmana/qualified Equip, Crew X, signed/decimal Crew values,
and qualified Crew sentences retain the existing grammar result. Surrounding
line whitespace is handled by the unchanged whole-card decomposer; no new
normalization or fragment splicing occurs. Generic source text is never used
instead of the linked face's card-local text.

## Historical evidence and validation

The historical 4-to-5 transition at bc47274 changed the global version and
introduced strict evidence scope; it was not a selectable historical grammar
API. Current accepted dependency evidence triples remain (5,3,3) and (5,4,3).
No (8,*,*) triple is added. New keyword_ability kinds/remainder reasons are not
accepted as persisted Analysis 5 evidence. Existing evidence validators and
historical consumer call sites are untouched.

Full-pool validation compares all serialized default classify_card fields with
pre-implementation snapshots, not just statuses. Analysis 5/6/7 benchmark bytes
are compared with frozen pre-implementation outputs. The opt-in diff must affect
only the four selected shapes, and each transition is unsupported to partial.
Linked-face comparisons use the representative-root P2 population (735 entries)
for measurement only; Analysis 7 is not rebased. Exact excluded-shape counts are
in the Phase 0 report. Synthetic tests cover positives, boundaries, guards,
version rejection, historical bytes and persistence rejection.

Publication, a default migration, downstream consumption, Analysis 6/7 rebasing,
evidence contract expansion and excluded variants require separate authorization.
