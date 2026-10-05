"""Non-authoritative Deck Review Report v1 projections and rendering.

Typed projections are report data, never a replacement wire contract for an
owner artifact. In particular they must not be passed to proposal services.
"""
from __future__ import annotations

from dataclasses import fields
import json
import math

from mtgadb.model import Deck, Format
from services.validator import DeckRules

REPORT_VERSION = "1"
LIMITATIONS = [
    "Read-only review: no approval, application intent, execution, recovery, or deck export.",
    "This report is not authoritative, has no identity, and establishes no post-run freshness.",
    "Ownership is unknown; crafting and spending are not evaluated or authorized.",
    "Validation means only the captured existing validator rules, not complete live Arena legality.",
    "Complete discovery means untruncated reviewed matches, not exhaustive Magic understanding.",
    "Names and IDs provide stable display order, never strategic preference or tie-breaking.",
    "Embedded identities retain their historical meanings and are not authentication or authority.",
]


def project(value):
    """Lossless, deterministic projection of the three supported owner types.

    JSON owner artifacts stay unchanged. Only Deck/Format/DeckRules projections
    may contain tagged integer maps, tuples and frozensets.
    """
    if type(value) in (Deck, Format, DeckRules):
        return {"report_type": type(value).__name__, "fields": {
            field.name: project(getattr(value, field.name)) for field in fields(value)
        }}
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is list:
        return [project(item) for item in value]
    if type(value) is tuple:
        return {"report_type": "tuple", "items": [project(item) for item in value]}
    if type(value) is frozenset:
        return {"report_type": "frozenset", "items": [project(item) for item in sorted(value)]}
    if type(value) is dict:
        if all(type(key) is str for key in value):
            return {key: project(item) for key, item in value.items()}
        if all(type(key) is int or key is None for key in value):
            return {"report_type": "integer_map", "entries": [
                [key, project(value[key])] for key in sorted(value, key=lambda k: (k is not None, k or 0))
            ]}
    raise ValueError("unsupported report projection value")


def render_json(report):
    return json.dumps(report, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2)


_EXPLANATIONS = {
    "no_candidates": "A supported need exists, but no reviewed candidate survived discovery.",
    "no_declared_preference": "The explicit policy declares no strategic preference.",
    "policy_not_applicable": "The explicit policy does not apply to this need.",
    "single_candidate_no_preference": "One candidate alone establishes no comparative preference.",
    "recommendable": "Uniquely preferred under the explicit policy within this returned pool only.",
    "top_tie": "The top candidates are tied; names and IDs do not break the tie.",
    "indeterminate_ordering": "Unknown comparison evidence prevents a unique preferred candidate.",
    "inconsistent_ordering": "The pair relationships establish no consistent unique first candidate.",
    "unresolved_eligibility": "Eligibility remains unresolved; this is not proof of legality.",
    "candidate_pool_truncated": "The proposal requires a complete pool; the returned pool was truncated.",
    "printing_cardinality_mismatch": "The proposal requires exactly one eligible printing; no printing is selected.",
    "recommendation_not_positive": "The recommendation outcome does not permit this proposal.",
    "policy_context_mismatch": "The declared need or policy context does not match the available evidence.",
    "baseline_snapshot_mismatch": "The baseline does not match the analyzed gameplay snapshot.",
    "baseline_context_mismatch": "The baseline contradicts the captured candidate copy evidence.",
    "validation_failed": "The add-one result failed the captured validator rules; no alternative edit is attempted.",
    "validated": "One addition passed the captured rules; this grants no approval, ownership claim, or execution authority.",
    "validation_unavailable": "Proposal validation could not be established.",
    "malformed_input": "The owner rejected malformed or unsupported input.",
    "malformed_baseline": "The owner rejected the baseline shape.",
    "unsupported_operation": "Only the existing add-only proposal operation is supported.",
    "unsupported_quantity": "The existing proposal requires exactly one added copy.",
    "unsupported_resource_mode": "The existing proposal supports unlimited validation only.",
}


def findings(artifacts):
    """Presentation observations only; never modify owner outputs or outcomes."""
    output = []

    def add(code, message, source):
        output.append({"code": code, "message": message, "source": source})

    analysis = artifacts.get("analysis")
    if analysis:
        for zone, data in analysis["zones"].items():
            coverage = data["rules_text_coverage"]
            if coverage["partial_ability_count"] or coverage["unsupported_ability_count"] or coverage["anomalous_ability_count"]:
                add("limited_rules_coverage", "Unsupported or partial rules text remains; absence of reviewed evidence is not absence of a mechanic.",
                    f"artifacts.analysis.zones.{zone}.rules_text_coverage")
    pools = artifacts.get("candidates")
    if pools is not None:
        if not pools["pools"]:
            add("no_supported_need", "No reviewed support need triggered discovery; zero candidate pools is a useful review outcome.",
                "artifacts.candidates.pools")
        for i, pool in enumerate(pools["pools"]):
            summary = pool["summary"]
            path = f"artifacts.candidates.pools[{i}].summary"
            if summary["truncated"]:
                add("candidate_pool_truncated", _EXPLANATIONS["candidate_pool_truncated"], path)
            if not summary["returned"]:
                add("no_candidates", _EXPLANATIONS["no_candidates"], path)
    decisions = artifacts.get("recommendation", {}).get("decisions", [])
    for i, decision in enumerate(decisions):
        code = decision["outcome"]
        add(code, _EXPLANATIONS[code], f"artifacts.recommendation.decisions[{i}]")
    proposal = artifacts.get("proposal_result_projection")
    if proposal:
        code = proposal["reason"]
        add(code, _EXPLANATIONS[code], "artifacts.proposal_result_projection")
        for i, issue in enumerate((proposal["validation"] or {}).get("errors", [])):
            add(issue["code"], issue["message"], f"artifacts.proposal_result_projection.validation.errors[{i}]")
    return output


def render_text(report):
    """A summary with source locators; JSON retains all evidence and traces."""
    lines = ["Deck Review v1 — READ ONLY", f"Run: {report['run_status']}"]
    context = report["input_context"]
    limit = context["candidate_limit_per_need"]
    input_failed = next(row for row in report["stages"] if row["name"] == "inputs")["state"] == "error"
    limit_label = "not established (invalid inputs)" if input_failed else "all (explicit)" if limit is None else str(limit)
    lines += [f"Candidate limit per need: {limit_label}",
              f"Format: {context['format_name'] or 'not supplied — eligibility unknown'}",
              "Ownership: unknown. Proposal validation: unlimited; no resource assessment."]
    if report["baseline"]:
        baseline = report["baseline"]
        lines.append(f"Baseline gameplay identity: {baseline['gameplay_identity']['digest']}")
        for zone, cards in baseline["zones"].items():
            lines.append(f"{zone}: " + ", ".join(f"{row['quantity']} {row['name']} [Arena {row['arena_id']}]" for row in cards))
    for stage in report["stages"]:
        diagnostic = stage["diagnostic"]
        lines.append(f"{stage['name']}: {stage['state']}" + (f" — {diagnostic['code']}: {diagnostic['message']}" if diagnostic else ""))
    artifacts = report["artifacts"]
    for issue in artifacts.get("import_issues", []):
        lines.append(f"Import line {issue['line']}: {issue['code']} - {issue['message']}")
    analysis = artifacts.get("analysis")
    if analysis:
        for zone, data in analysis["zones"].items():
            lines.append(f"{zone} coverage: {json.dumps(data['rules_text_coverage'], sort_keys=True)}")
            lines.append(f"{zone} packages: {json.dumps(data['functional_package_counts'], sort_keys=True)}")
            for dependency in data["dependencies"]:
                lines.append(f"{zone} dependency: {dependency['dependency_id']} - {dependency['state']}")
                if dependency["state"] == "support_scope_unestablished":
                    lines.append("  Recognized routes remain, but their originating support scope is unestablished.")
            for need in data["needs"]:
                lines.append(f"{zone} need: {need['finding_id']} ({need['finding_type']}) - {need['explanation']}")
            for card in data["cards"]:
                if card["unsupported_text"]:
                    lines.append(f"{zone} unsupported text for {card['name']}: {json.dumps(card['unsupported_text'], ensure_ascii=False)}")
    if "diagnosis" in artifacts:
        diagnosis = artifacts["diagnosis"]
        lines.append(f"Diagnosis: {diagnosis['status']} — {diagnosis['message']}")
        for item in diagnosis["unknowns"]:
            lines.append(f"Unknown: {item['message']} - {item['reason']}")
    for name in ("preference_policy", "proposal_policy"):
        if name in artifacts:
            lines.append(f"Explicit {name}: {json.dumps(artifacts[name], sort_keys=True, ensure_ascii=False)}")
    for pool in artifacts.get("candidates", {}).get("pools", []):
        lines.append(f"Pool {pool['source_need']['zone']}/{pool['source_need']['finding_id']}: {json.dumps(pool['summary'], sort_keys=True)}")
        lines.append("  Source scope: reviewed features only; empty reviewed sides do not establish actual absence. " +
                     json.dumps(pool["source_need"]["evidence_boundary"], sort_keys=True))
        for candidate in pool["candidates"]:
            lines.append(f"  {candidate['name']} [title {candidate['title_id']}]: {candidate['eligibility_status']}; eligible printings {candidate['eligibility']['format_legality']['eligible_printing_ids']}")
    for item in report["findings"]:
        lines.append(f"{item['code']}: {item['message']} [{item['source']}]")
    for row in artifacts.get("ordering", {}).get("need_orderings", []):
        lines.append(f"Ordering {row['need_key']}: {row['status']}; {len(row['pairwise_results'])} pairs evaluated.")
        for pair in row["pairwise_results"][:8]:
            lines.append(f"  Pair {pair['left']['title_id']}/{pair['right']['title_id']}: {pair['status']}; trace: " +
                         json.dumps(pair["rule_trace"], sort_keys=True, ensure_ascii=False))
        if len(row["pairwise_results"]) > 8:
            lines.append("  Further pairs omitted from this summary; all evidence is in artifacts.ordering (--json).")
    if "comparison" in artifacts:
        lines.append(f"Comparison: {len(artifacts['comparison']['need_matrices'])} need matrices; full facts at artifacts.comparison.")
    if "strategic_fit" in artifacts:
        for row in artifacts["strategic_fit"]["need_signal_sets"]:
            lines.append(f"Fit evidence {row['need_key']}: " + "; ".join(
                f"title {candidate['title_id']}: " + ", ".join(signal['signal_id'] for signal in candidate['signals'])
                for candidate in row['candidates']))
    if "presentation" in artifacts:
        lines.append("Verified detached proposal presentation (review only):")
        lines.append(json.dumps(artifacts["presentation"]["review_artifact"], sort_keys=True, ensure_ascii=False, indent=2))
    if report["observations"] is not None:
        lines.append("Performance observations: " + json.dumps(report["observations"], sort_keys=True))
    lines += report["limitations"]
    lines.append("This is a summary. Use --json for full owner evidence, comparisons, fit signals, policy traces, and limitations.")
    return "\n".join(lines)
