# Run 25 experimental cast-boost profile

Opt-in only: `generate_quality_deck.py --profile experimental-spell-cast-boost-v1`
with the existing registered format/queue/color-pair and new output directory.
Omitting the option retains the default classifier, dependency vocabulary, needs,
and deck-building behavior. This is an experiment, not evidence of stronger play.

## Scope and contract

`dependency.noncreature_cast_boost.v1` connects `type.noncreature_spell.v1` to
`trigger.noncreature_cast_boost.v1`. It is separate from the unchanged
`dependency.spell_cast.v1` instant/sorcery-to-draw family. Evidence uses the
explicit `run25-boost.1` contract, experimental dependency model 5 with base model
4. General analysis/needs still use their historical versions; v3/v4 consumers
reject experimental vocabulary rather than silently relabeling it.

The experimental parser recognizes exact self-directed noncreature-cast triggers
with positive integral power and nonnegative integral toughness changes until
end of turn. Prowess is normalized to a triggered +1/+1 ability, preserving the
printed keyword as origin evidence. Explicit +2/+0 remains distinct from prowess.
`temporary_self_pt_boost` stores power, toughness, duration and mechanic origin
in strictly reconciled qualifiers. Controller, subject, self target, source index,
ability/effect identifiers and exact text remain structured evidence.

Only the normal printed cast route is considered. Enablers require ordinary
single-face primary-printing evidence from the pinned Arena database, a supported
spell type, and neither Creature nor Land. Missing, ambiguous, linked or secondary
face evidence yields no positive new relationship. An alternative route (plot,
flashback, disguise, etc.) is never evaluated or asserted available. In particular,
Slickshot's normal creature characteristics and cast-trigger payoff can be
recognized without understanding Plot. This is not a general cast legality test.

Compatible pairs are potential/conditional support. The engine does not assert
that a legal target exists, that a cast took place, or that a spell/trigger resolved.
A copy not cast is not a cast event; a target becoming illegal after a legal cast
does not undo that cast event. Might of the Meek's Mouse-dependent effect is not
modeled and cannot become a target restriction or prerequisite for cast support.

No valiant, counters-from-temporary-boosts, power-scaled damage, last-known
information, intracard state dependencies, conditional draw replacements,
nonbasic-land selection, typed-land mana dependencies, or combat simulation was
added.

## Selection and scoring

The existing pair builder, relationship graph, eight-seed bound and score weights
are reused. New pair evidence is deduplicated; self-pairs are excluded. Existing
per-partner and total coherence caps remain unchanged. The regression with a
prowess creature plus `{T}: Draw a card.` demonstrated that the old resource gate
suppressed a valid new edge. Only the boost family bypasses that edge gate;
resource penalties and all old-family gates remain unchanged.

Experimental reports contain pool family counts, boost participant identities,
main/sideboard selected pairs, considered seeds, completed trial scores, matching
winning trials, and per-card score contributions. A coherence contribution is a
bounded total across partners; do not sum or attribute it independently to each
family when partners overlap. Seed eligibility, score contribution, and a changed
final deck are different observations.

## Reproduction and validation

Historical reproduction must run the original `274bf78febd7384be420b2b641ec3c5cfeba859b`
implementation in a separate checkout. Use its `deck_construction_data` paths and
SHA-256 pins, its registered v2 inputs and default deterministic settings. Save
`generate()` deck text directly as UTF-8 with LF, without normalization. Compare
the raw bytes against the frozen Run 23 B decks used by Run 24. Modified code that
happens to emit the same deck does not establish historical reproduction.

`scripts/evaluate_run25.py --output <new-directory> --frozen <frozen-comparator-inputs.json>`
records default/experimental comparisons for all five requests. It verifies frozen
baseline hashes and never writes to their paths. Generated evidence is separate
from historical artifacts. No grading or AI calls are involved.

Run `python -B -m unittest discover -s tests -v` and `python -B test_mtgadb.py`
in the normal Windows environment. `tests/test_spell_cast_boost.py` covers the new
contract, fail-closed counterexamples, historical vocabulary rejection, resource
gating and actual synthetic selection changes. The real five-request evaluation
must be reported even if no deck changes.
