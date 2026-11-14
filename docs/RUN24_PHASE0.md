# Run 24 Phase 0 implementation

Generator C is a separate adapter; Generator A, frozen B (274bf78), the v2
registration and the independent validator are unchanged. Its pool includes all
queue-legal canonical cards, restricted only by the registered color pair where
applicable. Encoding revision 2 is accepted and unchanged. Every original payload
field reconstructs exactly; no card rows are merged. The source's canonical text
and uncertainty metadata are retained; no new text or linked faces are inferred.

The operator's November 14, 2026 decision supersedes only the old $5 execution cap
and opt-in amount in the frozen grading protocol: the cap is now $10 cumulative,
and max_tokens is 32,000 per request, including adaptive thinking plus response.
Effort remains medium. The grading protocol bytes and hash remain unchanged:
07a835253efaa2eec908ba2534a8001b60d04a0840278648dbd9d76006a827d2.
The prompt is unchanged:
3489ebf66cded2059ba6659440da1f9eea37c2a4555ffeb2a2e12f49dc9346cb.

The strict response parser and shared text validator never repair decks. Citation
checks compare exact nonempty contiguous database text, without normalization;
their flags do not grade quality or prove interactions. A provider envelope must
end with end_turn; max_tokens, missing completion, refusal, transport cutoff and
other nonfinal stop reasons are failures even if text happens to parse. No retry.

## Budget guard

services/ai_budget.py computes costs from the operator's external pricing config,
using Decimal arithmetic and rounding reservations upward to integer nano-USD.
It reserves 110% of provider-counted input plus the full output maximum. The 10%
input margin is a conservative allowance, not a certified token-count bound.
The entire five-request plan must fit $10 before initialization. Context checks
include guarded input plus maximum output.

Explicit initialization creates a new SQLite ledger; existing ledgers are never
overwritten, and missing/corrupt ledgers fail closed on access. BEGIN IMMEDIATE
serializes reservation checks and writes. Each request must match its frozen hash
and registered order. Duplicate requests cannot be reserved again. Every normal
accept/refuse decision is committed to the ledger before dispatch. If storage
fails, dispatch must not occur. Full reservations remain charged locally after
success, failure, timeout, or restart; actual usage is recorded without refunds.
This deliberately sacrifices unused local budget to avoid ambiguous billing.

Phase 1 additionally requires accepted Phase 0 results, operator confirmation of
an account-level spending limit, and the exact new opt-in line:
"I authorize up to $10.00 in Anthropic API calls for Run 24 Phase 1."
No such authorization has been given. The ledger is initialized with zero
reservations. This code has no paid generation transport; fake providers alone
exercise the adapter in Phase 0. A future authorized caller must reserve before
dispatch and preserve raw request/response artifacts before assessing them.

## Preparation and validation

run24_phase0.py prepares external artifacts and optionally counts tokens using
only the documented-free Anthropic count_tokens endpoint. It has no generation
endpoint. The final prepared requests differ from encoding revision 2 only in
max_tokens, a field not supplied to count_tokens; previous counts remain valid
after exact equality checks of every counted parameter. No additional recount
is needed for this output-only change.

Run the focused tests with:
`python -B -m unittest tests.test_ai_budget tests.test_ai_deck_generator -v`.
Run the full Windows suite with `python -B -m unittest discover -v` and
`python -B test_mtgadb.py`. Evaluation outputs and the live ledger belong under
C:/MTG-Source-Preservation/evaluations/, never in the repository.
