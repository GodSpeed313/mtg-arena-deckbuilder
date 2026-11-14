# Run 24 grading protocol — version 1

Freeze this document's exact bytes and SHA-256 in Phase 0, before any Phase 1
output exists. No edits after freeze. The five requests, in registered order,
are std-bo1, std-bo3, std-bo1-wu, std-bo1-br, std-bo1-gw from evaluation set v2.
Generator B is frozen at 274bf78febd7384be420b2b641ec3c5cfeba859b; Generator A
is unchanged and is not a Run 24 comparator. Use B's five preserved Run 23 final
decks, with their hashes recorded before Phase 1, without regeneration.

## Graders and information

The operator, Copilot using a GPT-family model (record the exact selected model),
Gemini (record model), and Kimi (record model) grade the same packet in the same
session. No Claude model grades Run 24. Record grader identity/model, date, and
completion status; missing grades are missing, never zero or imputed.

Use the Run 23 worksheet structure and Run 22 rubric, anchors, automatic descriptive
signals, and scoring instructions from services/deck_evaluation.py (version 1).
Every dimension receives an integer 0–5 and a nonempty why: Card quality,
Synergy, Interaction, Mana efficiency, Playability. Sum to 25 for each grader.
Retain the cards_i_would_cut and cards_i_would_add fields. Automatic descriptive
signals are for human review, never quality grades. Apply identical presentation
to both generators. Show no generator identity, provenance, explanation,
interaction claim, citation-check flag, or validator flag. Do not add or disclose
the eight-copy pair validation result or any other validation diagnostic.

## Validation and masking

Revalidate both generators using the same independent validator and registered
request constraints (60 main; exactly 15 side for BO3, zero for BO1; registered
color-pair rules). Do not repair either generator. A failed deck is excluded from
grading and gets no quality score. One valid and one failed deck is a win for the
valid generator for that request, regardless of quality grades. If both fail,
report both failures and no winner, with no fabricated quality scores.

Re-grade all five valid B decks together with all valid C decks. Assign random
opaque IDs; sort HMAC-SHA256(private random 32-byte salt, opaque ID), tie by opaque
ID, then label Deck A onward. Shuffle all valid decks together, including the
color-pair requests. Keep the salt, IDs, request mapping and generator mapping
outside the repository in the sealed folder. Normalize worksheet timestamps.
Only the common worksheet fields, lists, descriptive signals and rubric go to
graders. Previous familiarity can limit masking; this is not guaranteed blinding.

Preserve raw C requests/responses and failures exactly. Seal explanations,
interaction claims, citation checks, validator results, budget logs, and the
masking key separately from the grader packet. Grades from every grader must be
complete and their files and manifest hashed before unmasking or reading sealed
explanations. No grading after exposure to the key or sealed material.

## Comparison after grade freeze

Pair by registered request, never by label/order. Report every dimension and
total separately for each grader, then the mean total over the same four graders
for each valid deck. Higher mean wins that request; equal means tie. Do not declare
an aggregate quality winner with missing grader data. Validation failure wins
remain explicit and independent. Report wins/ties/failures across all five requests;
do not select a subset. Never turn citation flags or validator flags into scores.

Report B's Run 24 masked re-grades alongside its frozen Run 23 grades as a separate
consistency check. Never modify, merge, or replace the Run 23 grades. Identify
differences in grader/model panels when interpreting the consistency check.

After unmasking, audit C's interaction claims against exact card text and rules:
claims supported, unsupported claims and conditions, and real interactions the
deterministic engine does not recognize. Exact text matches establish quotation
fidelity only, not mechanical correctness. List deterministic-engine candidates
without implementing them in this run. Report costs versus projections, validator
outcomes, and SHA-256 artifact manifests. The conclusion must state that this run
measures single-request, full-pool reasoning only, not the maximum strength of an
AI-assisted deck-building system.

## Execution boundary

Phase 0 permits fake generation tests and documented-free non-generation provider
calls only. Phase 1 requires accepted Phase 0 results, operator confirmation of an
account-level Anthropic spending limit, and the exact operator opt-in:
"I authorize up to $5.00 in Anthropic API calls for Run 24 Phase 1."
The $5.00 persisted local guard, one response per request, and no-repair rule remain
mandatory. A Phase 0 commit does not authorize paid calls, push, PR, or merge.
