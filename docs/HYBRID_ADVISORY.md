# Hybrid advisory foundation — Phases 0–2

This is an optional, fake-provider-only foundation. Existing offline commands,
legacy reports, managed deck storage, and execution contracts are unchanged.
There is no Anthropic transport, paid dispatch, automatic retry, proposal bridge,
or executable AI edit. Evidence validation is not strategic acceptance.

## Authority and scope

The operator authorized Phases 0–2 of
`Hybrid-Advisory-Integration-Design-Review-REVISED-r2.md` on the dedicated
`feature/hybrid-advisory-phase-0-2` branch. Design SHA-256:
`e3c1cd88deb965561ba14c1aa422a605e274b004566c9af7bcc6210cebb06fb3`.
The design remains external and unchanged. Decisions 1–8 are reflected here:
advisory-only, preserved deterministic output, separate human acceptance,
add-one-only existing proposals, strict citations, explicit review consent,
non-OneDrive local cache, and an engineering-owned acceptance rubric.

Phase 0 supplies this contract/rubric and verification plan. Phase 1 supplies
the detached evidence exporter, strict validator, separately versioned prompt,
and additive hybrid view. Phase 2 supplies the single-process coordinator,
fake adapter, consent preview, attempt records and validated atomic cache.
Phase 3 representative reviews and operator acceptance have not been performed.

## Local use

The normal `workbench.py` interface does not import the advisory subsystem.
The optional entry point is:

```powershell
python -B advisory_workbench.py examples/sample_deck.txt --database current.db
python -B advisory_workbench.py examples/sample_deck.txt --database current.db --fake-response synthetic-response.json --review
```

The first command only previews; it neither writes the cache nor dispatches.
The second asks for explicit consent on a miss and reads a local synthetic API
response envelope. It does not call a remote model. Fixture content must match
the exported input references; `tests/test_advisory.py` demonstrates a complete
synthetic envelope. No live or private grading fixture is bundled.

Programmatic composition:

```python
evidence = export_evidence(arena_text, database_path)
request = make_request(evidence)
coordinator = AdvisoryCoordinator(AdvisoryStore(), FakeProvider(response_bytes))
preview = coordinator.preview(request)
# Present preview, obtain an explicit decision for this exact request.
result = coordinator.run(request, preview=preview, authorize=operator_opt_in)
view = hybrid_view(original_offline_report, result, evidence=evidence)
```

Imports come from `services.advisory_contract`, `services.advisory`, and
`services.advisory_store`. No credential is required. `authorize` must be the
boolean `True`; a changed request or cost preview cannot reuse that consent.
The CLI displays provider output as JSON, with control characters escaped.
Neither API nor CLI follows returned links or executes returned content.

## Contracts and uncertainty

`AdvisoryInput v1` binds the existing gameplay identity to printing/quantity/zone
rows, canonical source text IDs, text hashes, completeness labels, captured card
facts, counts and explicitly unknown context. One read-only SQLite transaction
is closed before any provider step. Personal deck names and database paths are
not exported. Main/sideboard/commander are preserved. Separate card faces and
companion semantics are unavailable in the current canonical model; the export
says so. Canonical text is not certified as current Arena/Oracle text. Anomalous
text cannot support an established quotation.

`AdvisoryRequest v1` hashes the full exported input, prompt, schema, model/epoch,
sampling and output configuration. Unknown versions and inconsistent gameplay
counts/identity are rejected. The fake input count is UTF-8 bytes, explicitly
not an Anthropic token count. The preview's $0.00 bound is the actual fake cost;
it is not a claim about future paid pricing. A real adapter would require
separate authorization and current model, count, pricing and budget checks.

The new product prompt requires quotations for every referenced card relied on
by an ESTABLISHED claim. General caveats belong in `material_assumptions`; real
gaps remain in `missing_evidence`. This does not alter the frozen experiment.
The local schema requires four claims and a `game_plan`, closed object fields,
valid classifications and exact references. Only Unicode whitespace is
normalized in quotations: case and punctuation remain significant. Duplicate
JSON keys, nonfinite numbers, tool blocks, refusals and truncations fail.

Claim assessments contain exact checker errors, claim/source hashes and separate
`factual_conflict: not_assessed` and `semantic_review: not_reviewed` states.
The checker cannot discover every implicit card reliance or certify an
interaction's truth. No claim is promoted into deterministic needs or proposals.
The hybrid view carries captured facts separately, retains the original report,
labels heuristic limitations, and presents advisory interpretations separately.
Failure to recognize lifegain is not evidence that lifegain is absent.
Hypothetical swap text never becomes an executable action.

## Cache and request lifecycle

The default is `%LOCALAPPDATA%\MTGDeckbuilder\AdvisoryCache`, separate from all
deck databases and the repository. Runtime checks require agreement between the
Windows known-folder registry and environment, enumerate configured OneDrive
account/environment roots, reject overlap and reparse-point ancestors, and
fail closed on missing or inaccessible location evidence. No Documents fallback
exists. This is not a guarantee against every independent backup agent or a
hostile process changing filesystem mappings concurrently.

One process-wide gate rejects a second request while busy. There is no queue,
cross-process lock or account-wide budget subsystem. Consent is single-use and
bound to a preview. A pending attempt is durably written before the one fake
dispatch. Failures never trigger an automatic retry. A later independently
requested attempt needs a fresh preview and explicit acknowledgment of any
previous uncertain/failed outcome. Pending markers survive restart; possible
charges are not silently reported as zero in uncertainty messages.

Valid results retain the original UTF-8 response, its SHA-256, separate thinking
blocks, immutable review, usage metadata in the raw envelope, validation, fake
cost, and completion timestamp. Temporary-file write, flush, fsync and atomic
replacement publish each record. Both a valid review and matching completed
attempt are required on read. A failure between those publications is not a
valid cache hit. Reads recheck checksums and run the current validator.
Corrupt or incomplete entries disable reuse and do not trigger redispatch.

Invalid responses are separate diagnostic records, never valid cached reviews.
Their `raw_base64` and `raw_sha256` preserve malformed/non-UTF-8 bytes without
repair. Known credential contamination is the exception: contaminated bodies
are discarded, leaving only a safe attempt marker and generic failure. Errors
never print provider exception text. No credential is loaded for dispatch,
logged, or stored. The application checks known Anthropic environment secrets
and recognizable key strings; it cannot identify arbitrary unknown secrets
deliberately supplied as ordinary card text.

Exact-input hits need no new dispatch consent. Relevant input/model/prompt/schema
changes produce a different key; cosmetic deck-name changes do not. Old keys
remain historical. Current checker revalidation is mandatory; a checker-version
change removes current operator acceptance until re-reviewed. Checksums detect
accidental corruption, not authenticated provenance against a malicious editor.

`record_verdict(request, "accepted" or "rejected", explicit_operator=True)`
requires a valid completed review and records a separate explicit operator
assertion. It is not authentication, semantic certification or edit permission.
`history(current_key)` labels other keys historical/stale. Nothing auto-refreshes
or uploads. Reviews, summaries and invalid diagnostics remain until
`delete(key, explicit_operator=True)` removes that key's local files. It does
not erase independent backups/exports. Filesystem errors fail safely; the normal
offline workbench continues to be available separately.

## Engineering-owned acceptance rubric and evidence plan

Engineering supplies the evidence; the operator supplies strategic acceptance
or rejection. There is no invented numerical threshold or required deck count.

| Dimension | Engineering checks and review material | Operator judgment reserved |
|---|---|---|
| Factual consistency | Printing/text/count/source fidelity, exact references, unknown context, conflict status | Whether strategic conclusions follow from those facts |
| Game-plan explanation | Nonempty structured review with game-plan category, full unchanged claims and summaries | Whether the explanation is useful and specific |
| Uncertainty | Missing text/ownership/legality explicit; conditional claims and caveats retained | Whether assumptions and uncertainties are reasonable |
| Complete citations | Exact per-claim quotation results and errors; failures remain failures | Semantic correctness beyond literal quotation matching |
| Heuristic labels | Legacy output preserved; narrow recognition never presented as exhaustive absence | Whether presentation is clear and not misleading |
| Non-executable advice | No bridge, authority token, tool execution, swap action or deck mutation | Whether hypothetical advice merits future consideration |

The engineering test set uses synthetic isolated databases and a fake provider
for consent, errors, interruption, atomic publication, credential isolation,
cache identity/retention, unknown evidence and separate acceptance states.
The full existing regression suite protects legacy offline and store behavior.
An offline replay of the ten preserved experiment reviews checks unchanged
quotation semantics; no private grading answer key enters code or fixtures.

For a separately authorized Phase 3, engineering would select representative
game plans plus invalid and uncertain cases, disclose selection limitations,
and present unchanged reviews, per-claim failures and separate summaries. The
operator would read and accept/reject. This document neither performs that
exercise nor declares player usefulness or product acceptance.

## Remaining limits

Real provider compatibility, live pricing, billing, tokenization and usefulness
are unverified by this fake-only implementation. Single-process coordination
does not protect against other clients. Windows cache-location checks may make
advisory unavailable in redirected or inaccessible profiles. Separate faces,
companion semantics, current legality and inventory remain bounded by existing
data. There are no schema migrations, production prompt experiments, strategic
opportunity bridges, executable swaps, automatic deck applications or Phase 3
work in this change.
