"""Construct and validate one explicitly authorized add-only Deck proposal."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import sqlite3
from typing import Any

from mtgadb.model import Deck, Format
from mtgadb.modes import OperatingMode
from services.proposal_policy import build_proposal_policy
from services.validator import DeckRules, ValidationReport, validate_deck


PROPOSAL_MODEL_VERSION = "1"
_POLICY_FIELDS = (
    "policy_id", "policy_source", "need_key", "recommendation_requirement",
    "candidate_pool_requirement", "eligibility_requirement",
    "printing_requirement", "operation", "quantity", "target_zone", "resource_mode",
)
_CONTEXT_VERSIONS = {
    "recommendation_context_model_version": "1",
    "source_recommendation_decision_model_version": "1",
    "source_candidate_ordering_model_version": "1",
    "source_strategic_fit_model_version": "1",
    "source_strategic_preference_policy_model_version": "1",
    "source_candidate_comparison_model_version": "1",
    "source_candidate_facts_model_version": "2",
    "source_candidate_model_version": "2",
    "functional_package_model_version": "1",
}
_NEGATIVE_OUTCOMES = frozenset({
    "top_tie", "indeterminate_ordering", "inconsistent_ordering",
    "no_declared_preference", "policy_not_applicable", "no_candidates",
    "single_candidate_no_preference", "unresolved_eligibility",
})


def _key(value: Any) -> tuple[str, str, str] | None:
    if not isinstance(value, dict) or set(value) != {
        "zone", "finding_id", "dependency_id",
    } or value.get("zone") not in {"main", "sideboard", "commander"}:
        return None
    if any(type(value.get(field)) is not str or not value[field]
           for field in ("finding_id", "dependency_id")):
        return None
    return value["zone"], value["finding_id"], value["dependency_id"]


def _result(status: str, reason: str, evidence: Any = None, *,
            policy: dict | None = None, source: dict | None = None,
            proposal: dict | None = None, validation: dict | None = None,
            validation_inputs: dict | None = None) -> dict:
    return {
        "proposal_model_version": PROPOSAL_MODEL_VERSION,
        "status": status,
        "reason": reason,
        "evidence": deepcopy(evidence),
        "source_proposal_policy": deepcopy(policy),
        "source_recommendation_context": deepcopy(source),
        "proposal": proposal,
        "validation": validation,
        "validation_inputs": deepcopy(validation_inputs),
    }


def _baseline_well_formed(deck: Any) -> bool:
    if not isinstance(deck, Deck) or type(deck.deck_id) is not str or type(deck.name) is not str:
        return False
    for zone in (deck.main, deck.sideboard, deck.commander):
        if not isinstance(zone, dict) or any(
            type(arena_id) is not int or arena_id <= 0
            or type(quantity) is not int or quantity <= 0
            for arena_id, quantity in zone.items()
        ):
            return False
    return True


def _validation_evidence(report: ValidationReport) -> dict:
    return {
        "valid": report.valid,
        "errors": [asdict(item) for item in report.errors],
        "warnings": [asdict(item) for item in report.warnings],
        "wildcard_cost": dict(sorted(report.wildcard_cost.items())),
    }


def build_proposal(
    recommendation_context: dict,
    proposal_policy: dict,
    baseline_deck: Deck,
    con: sqlite3.Connection,
    *,
    format: Format,
    rules: DeckRules,
) -> dict:
    """Instantiate one #6G delta and accept it only after one validator call."""
    if not isinstance(proposal_policy, dict):
        return _result("rejected", "malformed_input", "proposal policy is not an object")
    for field, reason, expected in (
        ("operation", "unsupported_operation", "add_only"),
        ("quantity", "unsupported_quantity", 1),
        ("resource_mode", "unsupported_resource_mode", "unlimited"),
    ):
        value = proposal_policy.get(field)
        if type(value) is not type(expected) or value != expected:
            return _result("rejected", reason, {"field": field, "value": value})
    if (proposal_policy.get("proposal_policy_model_version") != "1"
        or proposal_policy.get("required_recommendation_context_model_version") != "1"):
        return _result("rejected", "malformed_input", "unsupported proposal policy version")
    try:
        normalized = build_proposal_policy({field: proposal_policy[field]
                                            for field in _POLICY_FIELDS})
    except (KeyError, ValueError, TypeError) as exc:
        return _result("rejected", "malformed_input", str(exc))
    if proposal_policy != normalized:
        return _result("rejected", "malformed_input", "proposal policy is not normalized")

    if not isinstance(recommendation_context, dict) or any(
        recommendation_context.get(field) != version
        for field, version in _CONTEXT_VERSIONS.items()
    ):
        return _result("rejected", "malformed_input", "unsupported recommendation context version",
                       policy=normalized)
    rows = recommendation_context.get("contexts")
    key = _key(normalized["need_key"])
    if not isinstance(rows, list) or key is None or any(
        not isinstance(row, dict) or _key(row.get("need_key")) is None for row in rows
    ):
        return _result("rejected", "malformed_input", "recommendation contexts are malformed",
                       policy=normalized)
    identities = [_key(row["need_key"]) for row in rows]
    if len(identities) != len(set(identities)):
        return _result("rejected", "malformed_input", "structured need is duplicated",
                       policy=normalized)
    matching = [row for row in rows if _key(row["need_key"]) == key]
    if not matching:
        return _result("abstained", "policy_context_mismatch", "declared need is absent",
                       policy=normalized)
    row = matching[0]
    decision = row.get("decision")
    source_need = row.get("source_need")
    if not isinstance(decision, dict) or not isinstance(source_need, dict) or (
        _key(decision.get("need_key")) != key
        or tuple(source_need.get(field) for field in ("zone", "finding_id", "dependency_id")) != key
        or type(recommendation_context.get("policy_id")) is not str
        or not recommendation_context["policy_id"]
        or decision.get("policy_id") != recommendation_context.get("policy_id")
        or decision.get("ordering_model_version") != "1"
    ):
        return _result("rejected", "policy_context_mismatch", "source identities contradict",
                       policy=normalized, source=row)
    expected_ref = f"need_matrices[need_key={key[0]}|{key[1]}|{key[2]}].source_need"
    source_policy = decision.get("policy_source")
    if (decision.get("source_need_reference") != expected_ref
        or not isinstance(source_policy, dict)
        or source_policy.get("kind") not in {"explicit_user", "explicit_operator_profile"}
        or not isinstance(source_policy.get("provenance"), dict)
        or not isinstance(source_policy["provenance"].get("reference"), str)
        or not source_policy["provenance"]["reference"]):
        return _result("rejected", "policy_context_mismatch", "recommendation provenance is malformed",
                       policy=normalized, source=row)
    outcome = decision.get("outcome")
    if outcome in _NEGATIVE_OUTCOMES:
        if decision.get("candidate") is not None:
            return _result("rejected", "malformed_input", "negative decision carries a candidate",
                           policy=normalized, source=row)
        return _result("abstained", "recommendation_not_positive", {"outcome": outcome},
                       policy=normalized, source=row)
    if outcome != normalized["recommendation_requirement"] or (
        decision.get("reason") != "unique_first_under_explicit_policy"
    ):
        return _result("rejected", "malformed_input", {"outcome": outcome},
                       policy=normalized, source=row)

    summary = row.get("source_pool_summary")
    candidates = row.get("returned_candidate_facts")
    if not isinstance(summary, dict) or not isinstance(candidates, list) or (
        type(summary.get("truncated")) is not bool
        or type(summary.get("returned")) is not int
        or summary["returned"] != len(candidates)
        or decision.get("considered_candidate_count") != len(candidates)
    ):
        return _result("rejected", "malformed_input", "returned pool is malformed",
                       policy=normalized, source=row)
    conditions = row.get("pool_context_conditions")
    if not isinstance(conditions, list) or (
        any(not isinstance(item, dict) for item in conditions)
        or any(item.get("condition_id") == "candidate_pool_truncated"
               for item in conditions) != summary["truncated"]
    ):
        return _result("rejected", "malformed_input", "pool conditions contradict truncation",
                       policy=normalized, source=row)
    if summary["truncated"]:
        return _result("abstained", "candidate_pool_truncated", deepcopy(summary),
                       policy=normalized, source=row)
    subject = decision.get("candidate")
    if not isinstance(subject, dict) or type(subject.get("title_id")) is not int or (
        type(subject.get("name")) is not str or not subject["name"]
    ):
        return _result("rejected", "malformed_input", "positive decision lacks title identity",
                       policy=normalized, source=row)
    candidate_ids = [item.get("title_id") for item in candidates if isinstance(item, dict)]
    if len(candidate_ids) != len(candidates) or any(type(item) is not int for item in candidate_ids) or (
        len(candidate_ids) != len(set(candidate_ids))
    ):
        return _result("rejected", "malformed_input", "returned candidate identities are malformed",
                       policy=normalized, source=row)
    matching_candidates = [item for item in candidates if isinstance(item, dict)
                           and item.get("title_id") == subject["title_id"]]
    if len(matching_candidates) != 1 or matching_candidates[0].get("name") != subject["name"]:
        return _result("rejected", "policy_context_mismatch", "recommended title is absent or ambiguous",
                       policy=normalized, source=row)
    candidate = matching_candidates[0]
    expected_candidate_ref = (
        f"need_matrices[need_key={key[0]}|{key[1]}|{key[2]}]"
        f".candidates[title_id={subject['title_id']}]"
    )
    if candidate.get("source_candidate_reference") != expected_candidate_ref:
        return _result("rejected", "policy_context_mismatch", "candidate source reference differs",
                       policy=normalized, source=row)
    if (subject.get("eligibility_status") != "eligible"
        or subject.get("unresolved_eligibility") != []
        or candidate.get("eligibility_status") != "eligible"
        or candidate.get("unresolved_eligibility") != []):
        return _result("abstained", "unresolved_eligibility",
                       {"decision": subject.get("unresolved_eligibility"),
                        "candidate": candidate.get("unresolved_eligibility")},
                       policy=normalized, source=row)
    eligible_ids = candidate.get("eligible_printing_ids")
    if not isinstance(eligible_ids, list) or any(type(item) is not int or item <= 0
                                                  for item in eligible_ids):
        return _result("rejected", "malformed_input", "eligible printing IDs are malformed",
                       policy=normalized, source=row)
    if len(eligible_ids) != 1:
        return _result("abstained", "printing_cardinality_mismatch",
                       {"eligible_printing_ids": deepcopy(eligible_ids)},
                       policy=normalized, source=row)
    arena_id = eligible_ids[0]
    eligibility = candidate.get("eligibility")
    format_fact = eligibility.get("format_legality") if isinstance(eligibility, dict) else None
    known = candidate.get("known_printings")
    if (not isinstance(format_fact, dict) or format_fact.get("status") != "legal"
        or format_fact.get("eligible_printing_ids") != eligible_ids
        or not isinstance(known, list)
        or sum(isinstance(item, dict) and item.get("arena_id") == arena_id
               for item in known) != 1):
        return _result("rejected", "policy_context_mismatch", "printing evidence contradicts title facts",
                       policy=normalized, source=row)

    if not _baseline_well_formed(baseline_deck):
        return _result("rejected", "malformed_baseline", "baseline Deck shape is malformed",
                       policy=normalized, source=row)
    source_context = row.get("source_context")
    if not isinstance(con, sqlite3.Connection) or not isinstance(format, Format) or (
        not isinstance(rules, DeckRules)
    ):
        return _result("rejected", "validation_unavailable",
                       "explicit SQLite connection, Format, and DeckRules are required",
                       policy=normalized, source=row)
    if (not isinstance(source_context, dict)
        or source_context.get("format_context") != format.name
        or format_fact.get("format") != format.name):
        return _result("abstained", "policy_context_mismatch", "format identity differs",
                       policy=normalized, source=row)
    source_colors = source_context.get("color_identity_context")
    rule_colors = (None if rules.allowed_colors is None
                   else "".join(sorted(rules.allowed_colors)))
    if source_colors != rule_colors:
        return _result("abstained", "policy_context_mismatch", "color context differs",
                       policy=normalized, source=row)
    known_ids = [item.get("arena_id") for item in known if isinstance(item, dict)]
    if (len(known_ids) != len(known) or any(type(item) is not int for item in known_ids)
        or len(known_ids) != len(set(known_ids))):
        return _result("rejected", "malformed_input", "known printings are malformed",
                       policy=normalized, source=row)
    observed_copies = sum(zone.get(printing_id, 0)
                          for zone in (baseline_deck.main, baseline_deck.sideboard,
                                       baseline_deck.commander)
                          for printing_id in known_ids)
    playset = eligibility.get("playset")
    if not isinstance(playset, dict) or type(playset.get("current_deck_copies")) is not int:
        return _result("rejected", "malformed_input", "copy-capacity evidence is malformed",
                       policy=normalized, source=row)
    if playset.get("status") == "capacity_available":
        limit, remaining = playset.get("copy_limit"), playset.get("remaining_capacity")
        if (type(limit) is not int or type(remaining) is not int or remaining < 1
            or remaining != limit - playset["current_deck_copies"]):
            return _result("rejected", "malformed_input", "finite copy-capacity evidence is contradictory",
                           policy=normalized, source=row)
    elif playset.get("status") == "unlimited":
        if playset.get("copy_limit") is not None or playset.get("remaining_capacity") is not None:
            return _result("rejected", "malformed_input", "unlimited copy-capacity evidence is contradictory",
                           policy=normalized, source=row)
    else:
        return _result("rejected", "malformed_input", "copy-capacity status is unsupported",
                       policy=normalized, source=row)
    if observed_copies != playset["current_deck_copies"]:
        return _result("abstained", "baseline_context_mismatch",
                       {"context_title_copies": playset["current_deck_copies"],
                        "baseline_known_printing_copies": observed_copies},
                       policy=normalized, source=row)

    zones = {name: deepcopy(getattr(baseline_deck, name))
             for name in ("main", "sideboard", "commander")}
    target = normalized["target_zone"]
    zones[target][arena_id] = zones[target].get(arena_id, 0) + normalized["quantity"]
    proposed = Deck(deck_id=baseline_deck.deck_id, name=baseline_deck.name, **zones)
    delta = {
        "operation": "add", "arena_id": arena_id, "zone": target,
        "quantity": normalized["quantity"], "title_id": subject["title_id"],
        "name": subject["name"], "need_key": deepcopy(normalized["need_key"]),
    }
    validation_inputs = {
        "mode": OperatingMode.UNLIMITED.value,
        "format": deepcopy(format),
        "rules": deepcopy(rules),
    }
    try:
        report = validate_deck(
            proposed, con, mode=OperatingMode.UNLIMITED, rules=rules, format=format,
        )
    except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        return _result("rejected", "validation_unavailable", str(exc),
                       policy=normalized, source=row,
                       validation_inputs=validation_inputs)
    if not isinstance(report, ValidationReport):
        return _result("rejected", "validation_unavailable", "validator returned no report",
                       policy=normalized, source=row,
                       validation_inputs=validation_inputs)
    validation = _validation_evidence(report)
    if not report.valid:
        return _result("abstained", "validation_failed", deepcopy(delta),
                       policy=normalized, source=row, validation=validation,
                       validation_inputs=validation_inputs)
    return _result("accepted", "validated", None, policy=normalized, source=row,
                   proposal={"delta": delta, "proposed_deck": proposed},
                   validation=validation, validation_inputs=validation_inputs)
