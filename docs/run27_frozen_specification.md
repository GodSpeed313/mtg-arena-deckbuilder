# RUN 27 — Spell-Cast Support Scoring Guard

**FROZEN SPECIFICATION (design approved; NOT implementation authorization)**

## SCOPE
Opt-in change to the boost family (dependency.noncreature_cast_boost) inside
the experimental profile only. Default generator, other dependency families,
and the deck template (12 fixed noncreature slots) are unchanged. Flexible
slots are Run 28, a separate experiment.

## WHAT STAYS
- Final-deck partner presence check (already works; do not rebuild).
- Per-title copy cap of 4 when counting support.
- Bonus ceiling of +12 per payoff.

## WHAT CHANGES
Replace "2 × Σ min(copies, 4), capped at 12" with a support score that
depends on what the support spells are.

1. CLASSIFY each selected MAIN-DECK card against the payoff's exact trigger:
   - Unsupported (weight 0): does not satisfy the trigger — creature spells,
     lands, cycling/activated abilities, copies not cast, sideboard cards.
   - Proactive (weight 1.0): only when the card's casting and targeting
     prerequisites are demonstrably satisfiable through the player's own
     ordinary game actions — e.g., no target, or a target the player's own
     permanents or self can supply without further conditions (such as a
     required tapped state, a specific permanent type the deck may lack, or
     an opponent action). If that cannot be shown from implemented card
     understanding, classify as Conditional or Unknown, never Proactive.
   - Conditional (weight 0.4): needs an opponent-controlled object, a spell
     on the stack, or another game-state condition the player doesn't
     control (counterspells, removal, artifact-only removal).
   - Unknown (weight 0.4): classification not determinable from implemented
     card understanding. Receives conditional weight but is ALWAYS counted
     and reported separately from Conditional. Never treated as Proactive.

2. COST FACTOR by printed mana value (conditional discounts ignored):
   - MV 0–2: 1.0    - MV 3: 0.75    - MV 4+: 0.5

3. SUPPORT SCORE  S = Σ (min(copies,4) × category weight × cost factor)

4. BONUS = min(12, round_half_up(12 × S / T)),  T = 11
   round_half_up(x) for x ≥ 0 means floor(x + 0.5). Python's built-in
   round() (ties-to-even) must not be used. Implementation and tests use the
   same rule.

## REGISTERED ASSUMPTIONS (fixed now; not tuned from Run 26 results)
- Prowess-style payoffs reward casting a spell in the same turn as attacking,
  so cheap, player-timed spells are worth more than reactive ones.
- T = 11 is a registered starting heuristic, not a consistency guarantee.
  Basis: by turn 4 a player has seen about 10 cards on the play and 11 on
  the draw; 11 full-credit spells in 60 cards gives an expected value of
  about two qualifying spells among 11 cards seen. This is an average, not a
  high probability of drawing two.
- Weights 1.0 / 0.4 / 0 / 0.4 and cost factors 1.0 / 0.75 / 0.5 are
  registered judgments, not derived values. They may be revised only in a
  new, separately registered run.

## WORKED EXAMPLE (generic)
12 conditional spells, 4 each at MV 2/3/4+:
S = 4×0.4×1.0 + 4×0.4×0.75 + 4×0.4×0.5 = 3.6
Bonus = round_half_up(12 × 3.6 / 11) = round_half_up(3.927) = 4
(previously 12).

## REQUIRED TESTS
- No selected qualifying spell → bonus 0, even with many in the pool.
- Creature spells, cycling, uncast copies, wrong trigger → contribute 0.
- Sideboard-only support → contributes 0 to main-deck payoffs.
- Removing support from the final deck lowers recomputed credit.
- Equal counts, different category or cost → different bonus as specified.
- A self-targeting spell with an unmet extra prerequisite (e.g., requires a
  tapped creature) is not classified Proactive.
- Unknown cards appear in their own reported count, not merged into
  Conditional.
- Rounding: a constructed case at an exact .5 boundary rounds up.
- Proactive-heavy support reaches the +12 ceiling (positive control).
- Default generator: byte-identical outputs on all five frozen requests.
- Deterministic and independently legal outputs.

## REPORTING ORDER
1. Rescore the FROZEN Run 25 experimental decks with the new rule (no
   regeneration): per request, per payoff — old bonus, new bonus, S, and
   counts by category (Proactive / Conditional / Unknown / Unsupported).
   This isolates the scoring change from any deck-composition change.
2. Only then generate new experimental decks and report: payoff count, S,
   category counts, unknown count, and differences from both the default
   and the frozen Run 25 experimental decks.

## SUCCESS CRITERIA FOR RUN 27 (no grading round required)
- All tests pass on a normal-Windows run.
- Step 1 shows conditional-only support no longer saturates the bonus.
- If new experimental decks land at or near the default decks, that is the
  guard working, not a failure.
- A new grading round is justified only if new decks differ meaningfully
  from both the default and the frozen Run 25 experimental decks.

## AUTHORIZATION BOUNDARY
This document freezes design only. It does not authorize implementation,
branch creation, commits, or publication. Implementation requires a separate
go-ahead on a new local branch, with local commit only. Publication of the
two existing branches is a separate decision.
