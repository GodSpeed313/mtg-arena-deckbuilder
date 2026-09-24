"""Deterministic, non-actionable presentation of one validated proposal.

Proposal identity binds gameplay and validation semantics. Presentation
identity separately binds the exact review artifact a future human may see.
Neither identity is approval, authorization, authentication, or a signature.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any

from mtgadb.deck_identity import (
    build_deck_snapshot_identity,
    require_deck_snapshot_identity,
)
from mtgadb.model import Deck, Format
from services.proposal_policy import build_proposal_policy
from services.validator import DeckRules


PROPOSAL_PRESENTATION_MODEL_VERSION = "1"
PROPOSAL_IDENTITY_VERSION = "1"
PRESENTATION_IDENTITY_VERSION = "1"
IDENTITY_DIGEST_ALGORITHM = "sha256"

_PROPOSAL_FIELDS = frozenset({
    "proposal_model_version", "status", "reason", "evidence",
    "source_proposal_policy", "source_recommendation_context", "proposal",
    "validation", "validation_inputs",
})
_POLICY_FIELDS = (
    "policy_id", "policy_source", "need_key", "recommendation_requirement",
    "candidate_pool_requirement", "eligibility_requirement",
    "printing_requirement", "operation", "quantity", "target_zone",
    "resource_mode",
)
_DELTA_FIELDS = frozenset({
    "operation", "arena_id", "zone", "quantity", "title_id", "name", "need_key",
})
_ZONES = ("main", "sideboard", "commander")


def _mapping(value: Any, label: str, fields: frozenset[str] | None = None) -> dict:
    if not isinstance(value, dict) or (fields is not None and set(value) != fields):
        raise ValueError(f"{label} is malformed")
    return value


def _text(value: Any, label: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _integer(value: Any, label: str, *, minimum: int | None = None) -> int:
    if type(value) is not int or (minimum is not None and value < minimum):
        raise ValueError(f"{label} must be an integer")
    return value


def _need_key(value: Any) -> dict:
    item = _mapping(
        value, "structured need key",
        frozenset({"zone", "finding_id", "dependency_id"}),
    )
    if item["zone"] not in _ZONES:
        raise ValueError("structured need zone is unsupported")
    return {
        "zone": item["zone"],
        "finding_id": _text(item["finding_id"], "finding ID"),
        "dependency_id": _text(item["dependency_id"], "dependency ID"),
    }


def _encoded(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("identity payload is not canonical JSON") from exc


def _identity(version_field: str, version: str, payload: dict) -> dict:
    encoded = _encoded(payload)
    return {
        version_field: version,
        "digest_algorithm": IDENTITY_DIGEST_ALGORITHM,
        "canonical_payload": deepcopy(payload),
        "digest": hashlib.sha256(encoded).hexdigest(),
    }


def _string_set(value: Any, label: str) -> list[str]:
    if not isinstance(value, frozenset) or any(type(item) is not str for item in value):
        raise ValueError(f"{label} must be a frozen set of strings")
    return sorted(value)


def _integer_set(value: Any, label: str) -> list[int]:
    if not isinstance(value, frozenset) or any(type(item) is not int for item in value):
        raise ValueError(f"{label} must be a frozen set of integers")
    return sorted(value)


def _optional_integer_set(value: Any, label: str) -> list[int] | None:
    return None if value is None else _integer_set(value, label)


def _quota_map(value: Any, label: str, *, nullable: bool = False) -> list[list[int | None]]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    result = []
    for key, quantity in value.items():
        _integer(key, f"{label} key", minimum=1)
        if quantity is None and nullable:
            result.append([key, None])
            continue
        _integer(quantity, f"{label} quantity", minimum=0)
        result.append([key, quantity])
    return sorted(result, key=lambda item: item[0])


def _optional_integer(value: Any, label: str) -> int | None:
    if value is None:
        return None
    return _integer(value, label)


def _canonical_format(value: Any) -> dict:
    if not isinstance(value, Format):
        raise ValueError("captured validation format is unavailable")
    uses_rebalanced = value.uses_rebalanced_cards
    if uses_rebalanced is not None and type(uses_rebalanced) is not bool:
        raise ValueError("format rebalanced-card flag is malformed")
    restrictions = value.color_restrictions_internal
    if not isinstance(restrictions, tuple):
        raise ValueError("format color restrictions are malformed")
    canonical_restrictions = [
        _integer_set(item, "format color restriction") for item in restrictions
    ]
    return {
        "name": _text(value.name, "format name"),
        "legal_sets": _string_set(value.legal_sets, "format legal sets"),
        "filter_sets": _string_set(value.filter_sets, "format filter sets"),
        "banned_title_ids": _integer_set(value.banned_title_ids, "format banned titles"),
        "allowed_title_ids": _optional_integer_set(
            value.allowed_title_ids, "format allowed titles",
        ),
        "suppressed_title_ids": _integer_set(
            value.suppressed_title_ids, "format suppressed titles",
        ),
        "suspended_title_ids": _integer_set(
            value.suspended_title_ids, "format suspended titles",
        ),
        "allowed_commander_title_ids": _optional_integer_set(
            value.allowed_commander_title_ids, "format allowed commanders",
        ),
        "individual_card_quotas": _quota_map(
            value.individual_card_quotas, "format individual-card quotas",
        ),
        "rarity_card_quotas": _quota_map(
            value.rarity_card_quotas, "format rarity quotas", nullable=True,
        ),
        "min_deck_size": _integer(value.min_deck_size, "format minimum deck size", minimum=0),
        "max_deck_size": _integer(value.max_deck_size, "format maximum deck size", minimum=0),
        "max_sideboard": _integer(value.max_sideboard, "format maximum sideboard", minimum=0),
        "min_command_zone": _integer(
            value.min_command_zone, "format minimum command zone", minimum=0,
        ),
        "max_command_zone": _integer(
            value.max_command_zone, "format maximum command zone", minimum=0,
        ),
        "uses_rebalanced_cards": uses_rebalanced,
        "format_type_internal": _optional_integer(
            value.format_type_internal, "format internal type",
        ),
        "card_count_restriction_internal": _optional_integer(
            value.card_count_restriction_internal, "format internal card-count restriction",
        ),
        "sideboard_behavior_internal": _optional_integer(
            value.sideboard_behavior_internal, "format internal sideboard behavior",
        ),
        "color_restrictions_internal": canonical_restrictions,
    }


def _canonical_rules(value: Any) -> dict:
    if not isinstance(value, DeckRules):
        raise ValueError("captured validation rules are unavailable")
    return {
        "min_main": _integer(value.min_main, "minimum main size", minimum=0),
        "max_main": _integer(value.max_main, "maximum main size", minimum=0),
        "max_sideboard": _integer(value.max_sideboard, "maximum sideboard size", minimum=0),
        "min_commanders": _integer(value.min_commanders, "minimum commanders", minimum=0),
        "max_commanders": _integer(value.max_commanders, "maximum commanders", minimum=0),
        "copy_limit": _integer(value.copy_limit, "copy limit", minimum=1),
        "allowed_colors": (
            None if value.allowed_colors is None
            else _string_set(value.allowed_colors, "allowed colors")
        ),
    }


def _canonical_validation_inputs(value: Any) -> dict:
    inputs = _mapping(
        value, "captured validation inputs", frozenset({"mode", "format", "rules"}),
    )
    if inputs["mode"] != "unlimited":
        raise ValueError("Proposal Model v2 requires unlimited validation mode")
    result = {
        "mode": "unlimited",
        "format": _canonical_format(inputs["format"]),
        "rules": _canonical_rules(inputs["rules"]),
    }
    if result["format"]["min_deck_size"] > result["format"]["max_deck_size"] or (
        result["format"]["min_command_zone"] > result["format"]["max_command_zone"]
    ) or result["rules"]["min_main"] > result["rules"]["max_main"] or (
        result["rules"]["min_commanders"] > result["rules"]["max_commanders"]
    ):
        raise ValueError("captured validation ranges are contradictory")
    return result


def _validation_evidence(value: Any) -> dict:
    evidence = _mapping(
        value, "validation evidence",
        frozenset({"valid", "errors", "warnings", "wildcard_cost"}),
    )
    if evidence["valid"] is not True or evidence["errors"] != []:
        raise ValueError("presentation requires successful validation evidence")
    warnings = evidence["warnings"]
    if not isinstance(warnings, list):
        raise ValueError("validation warnings are malformed")
    normalized_warnings = []
    issue_fields = frozenset({"code", "message", "zone", "title_id"})
    for warning in warnings:
        warning = _mapping(warning, "validation warning", issue_fields)
        code = _text(warning["code"], "validation warning code")
        message = _text(warning["message"], "validation warning message")
        zone = warning["zone"]
        if zone is not None and zone not in _ZONES:
            raise ValueError("validation warning zone is malformed")
        title_id = warning["title_id"]
        if title_id is not None:
            _integer(title_id, "validation warning title ID", minimum=1)
        normalized_warnings.append({
            "code": code, "message": message, "zone": zone, "title_id": title_id,
        })
    wildcard_cost = evidence["wildcard_cost"]
    if not isinstance(wildcard_cost, dict) or any(
        type(rarity) is not str or not rarity
        or type(quantity) is not int or quantity < 0
        for rarity, quantity in wildcard_cost.items()
    ):
        raise ValueError("validation wildcard cost is malformed")
    return {
        "valid": True,
        "errors": [],
        "warnings": normalized_warnings,
        "wildcard_cost": {key: wildcard_cost[key] for key in sorted(wildcard_cost)},
    }


def _policy(value: Any) -> dict:
    policy = _mapping(value, "source proposal policy")
    if policy.get("proposal_policy_model_version") != "2" or (
        policy.get("required_recommendation_context_model_version") != "2"
    ):
        raise ValueError("Proposal Policy Model Version 2 is required")
    try:
        normalized = build_proposal_policy({field: policy[field] for field in _POLICY_FIELDS})
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("source proposal policy is malformed") from exc
    if normalized != policy:
        raise ValueError("source proposal policy is not normalized")
    return normalized


def _recommendation_context(value: Any, policy: dict, delta: dict) -> tuple[dict, dict]:
    row = _mapping(value, "source recommendation context")
    required = {
        "need_key", "decision", "source_need", "source_pool_summary",
        "source_evidence_completeness", "pool_context_conditions",
        "returned_candidate_facts", "source_context",
    }
    if set(row) != required:
        raise ValueError("source recommendation context is malformed")
    need = _need_key(row["need_key"])
    if need != policy["need_key"] or need != delta["need_key"]:
        raise ValueError("proposal need provenance contradicts the delta")
    source_need = _mapping(row["source_need"], "source need")
    if _need_key({field: source_need.get(field) for field in need}) != need:
        raise ValueError("source need provenance contradicts the proposal")

    decision = _mapping(row["decision"], "recommendation decision")
    if _need_key(decision.get("need_key")) != need or (
        decision.get("outcome") != "recommendable"
        or decision.get("reason") != "unique_first_under_explicit_policy"
        or decision.get("ordering_model_version") != "2"
        or decision.get("blocked_candidate") is not None
        or decision.get("unresolved_conditions") != []
    ):
        raise ValueError("recommendation decision contradicts the accepted proposal")
    policy_id = _text(decision.get("policy_id"), "recommendation policy ID")
    policy_source = _mapping(
        decision.get("policy_source"), "recommendation policy source",
        frozenset({"kind", "provenance"}),
    )
    if policy_source["kind"] not in {"explicit_user", "explicit_operator_profile"}:
        raise ValueError("recommendation policy provenance is unsupported")
    provenance = _mapping(
        policy_source["provenance"], "recommendation policy provenance",
        frozenset({"reference"}),
    )
    _text(provenance["reference"], "recommendation policy reference")
    expected_reference = (
        f"need_matrices[need_key={need['zone']}|{need['finding_id']}|"
        f"{need['dependency_id']}].source_need"
    )
    if decision.get("source_need_reference") != expected_reference:
        raise ValueError("recommendation source-need reference is contradictory")

    subject = _mapping(decision.get("candidate"), "recommended candidate")
    title_id = _integer(subject.get("title_id"), "recommended title ID", minimum=1)
    name = _text(subject.get("name"), "recommended card name")
    if title_id != delta["title_id"] or name != delta["name"] or (
        subject.get("eligibility_status") != "eligible"
        or subject.get("unresolved_eligibility") != []
    ):
        raise ValueError("recommended candidate contradicts the delta")

    summary = _mapping(row["source_pool_summary"], "source pool summary")
    candidates = row["returned_candidate_facts"]
    if not isinstance(candidates, list) or type(summary.get("returned")) is not int or (
        summary["returned"] != len(candidates) or summary.get("truncated") is not False
        or decision.get("considered_candidate_count") != len(candidates)
    ):
        raise ValueError("source candidate pool is incomplete or contradictory")
    candidate_ids = [item.get("title_id") for item in candidates if isinstance(item, dict)]
    if len(candidate_ids) != len(candidates) or any(
        type(item) is not int for item in candidate_ids
    ) or len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("source candidate identities are malformed or duplicated")
    pool_conditions = row["pool_context_conditions"]
    if not isinstance(pool_conditions, list) or any(
        not isinstance(item, dict) for item in pool_conditions
    ) or any(item.get("condition_id") == "candidate_pool_truncated"
             for item in pool_conditions):
        raise ValueError("source pool conditions contradict the complete pool")
    matches = [item for item in candidates if isinstance(item, dict)
               and item.get("title_id") == title_id]
    if len(matches) != 1:
        raise ValueError("recommended candidate is absent or ambiguous")
    candidate = matches[0]
    if candidate.get("name") != name or candidate.get("eligibility_status") != "eligible" or (
        candidate.get("unresolved_eligibility") != []
    ):
        raise ValueError("candidate context contradicts the delta")
    eligible_ids = candidate.get("eligible_printing_ids")
    if eligible_ids != [delta["arena_id"]]:
        raise ValueError("eligible printing evidence contradicts the delta")
    expected_candidate_reference = (
        f"need_matrices[need_key={need['zone']}|{need['finding_id']}|"
        f"{need['dependency_id']}].candidates[title_id={title_id}]"
    )
    if candidate.get("source_candidate_reference") != expected_candidate_reference:
        raise ValueError("candidate source reference is contradictory")
    context_conditions = candidate.get("context_conditions")
    if not isinstance(context_conditions, list) or any(
        not isinstance(item, dict) for item in context_conditions
    ) or any(item.get("condition_id") == "multiple_eligible_printings"
             for item in context_conditions):
        raise ValueError("candidate context conditions contradict the singleton printing")
    required_feature = _mapping(
        candidate.get("required_feature_eligibility"), "required feature eligibility",
    )
    if required_feature.get("status") != "matched" or not isinstance(
        candidate.get("matching_feature_evidence"), list
    ) or not candidate["matching_feature_evidence"]:
        raise ValueError("candidate lacks reviewed feature-match evidence")
    _mapping(candidate.get("support_context"), "candidate support context")
    eligibility = _mapping(candidate.get("eligibility"), "candidate eligibility")
    format_fact = _mapping(eligibility.get("format_legality"), "format eligibility")
    if format_fact.get("status") != "legal" or format_fact.get(
        "eligible_printing_ids"
    ) != eligible_ids:
        raise ValueError("candidate format evidence contradicts the delta")
    known = candidate.get("known_printings")
    if not isinstance(known, list) or sum(
        isinstance(item, dict) and item.get("arena_id") == delta["arena_id"] for item in known
    ) != 1:
        raise ValueError("candidate printing provenance contradicts the delta")

    source_context = _mapping(row["source_context"], "recommendation source context")
    baseline_identity = require_deck_snapshot_identity(
        source_context.get("analyzed_deck_identity")
    )
    recommendation_provenance = {
        "policy_id": policy_id,
        "policy_source": deepcopy(policy_source),
        "ordering_model_version": "2",
        "source_need_reference": expected_reference,
    }
    return baseline_identity, {
        "row": row,
        "candidate": candidate,
        "format_fact": format_fact,
        "known_printings": known,
        "source_context": source_context,
        "recommendation_provenance": recommendation_provenance,
    }


def _delta(value: Any) -> dict:
    delta = _mapping(value, "proposal delta", _DELTA_FIELDS)
    if delta["operation"] != "add" or delta["zone"] not in _ZONES:
        raise ValueError("proposal delta operation or zone is unsupported")
    return {
        "operation": "add",
        "arena_id": _integer(delta["arena_id"], "delta Arena ID", minimum=1),
        "zone": delta["zone"],
        "quantity": _integer(delta["quantity"], "delta quantity", minimum=1),
        "title_id": _integer(delta["title_id"], "delta title ID", minimum=1),
        "name": _text(delta["name"], "delta card name"),
        "need_key": _need_key(delta["need_key"]),
    }


def _expected_result_identity(baseline_identity: dict, delta: dict) -> dict:
    payload = deepcopy(baseline_identity["canonical_payload"])
    zone = {arena_id: quantity for arena_id, quantity in payload[delta["zone"]]}
    zone[delta["arena_id"]] = zone.get(delta["arena_id"], 0) + delta["quantity"]
    payload[delta["zone"]] = [[arena_id, zone[arena_id]] for arena_id in sorted(zone)]
    return build_deck_snapshot_identity(Deck(
        main=dict(payload["main"]),
        sideboard=dict(payload["sideboard"]),
        commander=dict(payload["commander"]),
    ))


def build_proposal_presentation(proposal_result: dict) -> dict:
    """Create one deterministic review artifact; grant no approval or action."""
    source = _mapping(proposal_result, "proposal result", _PROPOSAL_FIELDS)
    if source["proposal_model_version"] != "2" or source["status"] != "accepted" or (
        source["reason"] != "validated" or source["evidence"] is not None
    ):
        raise ValueError("an accepted and validated Proposal Model Version 2 is required")

    policy = _policy(source["source_proposal_policy"])
    proposal = _mapping(
        source["proposal"], "accepted proposal", frozenset({"delta", "proposed_deck"}),
    )
    delta = _delta(proposal["delta"])
    if policy["operation"] != "add_only" or policy["quantity"] != delta["quantity"] or (
        policy["target_zone"] != delta["zone"] or policy["resource_mode"] != "unlimited"
    ):
        raise ValueError("proposal policy contradicts the concrete delta")
    baseline_identity, context = _recommendation_context(
        source["source_recommendation_context"], policy, delta,
    )

    proposed_deck = proposal["proposed_deck"]
    if not isinstance(proposed_deck, Deck):
        raise ValueError("accepted proposal lacks a proposed Deck")
    result_identity = build_deck_snapshot_identity(proposed_deck)
    expected_identity = _expected_result_identity(baseline_identity, delta)
    if result_identity != expected_identity:
        raise ValueError("proposed gameplay state is not exactly baseline plus the delta")

    validation = _validation_evidence(source["validation"])
    validation_semantics = _canonical_validation_inputs(source["validation_inputs"])
    if context["source_context"].get("format_context") != validation_semantics["format"]["name"] or (
        context["format_fact"].get("format") != validation_semantics["format"]["name"]
    ):
        raise ValueError("captured format contradicts recommendation provenance")
    source_colors = context["source_context"].get("color_identity_context")
    rule_colors = validation_semantics["rules"]["allowed_colors"]
    expected_colors = None if rule_colors is None else "".join(rule_colors)
    if source_colors != expected_colors:
        raise ValueError("captured color rules contradict recommendation provenance")

    known_ids = []
    for printing in context["known_printings"]:
        printing = _mapping(printing, "known printing")
        known_ids.append(_integer(printing.get("arena_id"), "known printing ID", minimum=1))
    if len(known_ids) != len(set(known_ids)):
        raise ValueError("known printing IDs are duplicated")
    baseline_payload = baseline_identity["canonical_payload"]
    baseline_counts = {}
    for zone in _ZONES:
        for arena_id, quantity in baseline_payload[zone]:
            baseline_counts[arena_id] = baseline_counts.get(arena_id, 0) + quantity
    observed_copies = sum(baseline_counts.get(arena_id, 0) for arena_id in known_ids)
    playset = _mapping(context["candidate"].get("eligibility", {}).get("playset"),
                       "candidate playset evidence")
    if type(playset.get("current_deck_copies")) is not int or (
        playset["current_deck_copies"] != observed_copies
    ):
        raise ValueError("candidate copy evidence contradicts the baseline identity")
    if playset.get("status") == "capacity_available":
        limit, remaining = playset.get("copy_limit"), playset.get("remaining_capacity")
        if type(limit) is not int or limit <= 0 or type(remaining) is not int or (
            remaining < delta["quantity"]
            or remaining != limit - playset["current_deck_copies"]
        ):
            raise ValueError("candidate finite copy capacity is contradictory")
    elif playset.get("status") == "unlimited":
        if playset.get("copy_limit") is not None or playset.get("remaining_capacity") is not None:
            raise ValueError("candidate unlimited copy capacity is contradictory")
    else:
        raise ValueError("candidate copy-capacity status is unsupported")

    semantic_delta = {key: deepcopy(delta[key]) for key in (
        "operation", "arena_id", "zone", "quantity", "title_id", "need_key",
    )}
    semantic_policy = {key: deepcopy(policy[key]) for key in (
        "proposal_policy_model_version", "required_recommendation_context_model_version",
        "policy_id", "policy_source", "need_key", "recommendation_requirement",
        "candidate_pool_requirement", "eligibility_requirement", "printing_requirement",
        "operation", "quantity", "target_zone", "resource_mode",
    )}
    semantic_payload = {
        "source_proposal_model_version": "2",
        "source_proposal_policy": semantic_policy,
        "recommendation_provenance": context["recommendation_provenance"],
        "source_baseline_deck_identity": baseline_identity,
        "delta": semantic_delta,
        "resulting_deck_identity": result_identity,
        "captured_validation_semantics": validation_semantics,
    }
    proposal_identity = _identity(
        "proposal_identity_version", PROPOSAL_IDENTITY_VERSION, semantic_payload,
    )

    review_artifact = {
        "boundary_semantics": [
            "validator-accepted", "not-user-approved", "not-applied",
            "not-resource-authorized",
        ],
        "proposal": {
            "operation": delta["operation"],
            "quantity": delta["quantity"],
            "target_zone": delta["zone"],
            "card": {
                "title_id": delta["title_id"],
                "name": delta["name"],
                "arena_id": delta["arena_id"],
            },
            "need_key": deepcopy(delta["need_key"]),
        },
        "source_baseline_deck_identity": baseline_identity,
        "resulting_deck_identity": result_identity,
        "proposal_policy": semantic_policy,
        "recommendation_provenance": context["recommendation_provenance"],
        "validation": {
            "status": "validator-accepted",
            "evidence": validation,
            "captured_semantics": validation_semantics,
            "resource_mode_disclosure": "unlimited; ownership and wildcard resources not authorized",
            "freshness_disclosure": "captured validation is not a claim of current validity",
        },
        "limitations": [
            "This artifact presents one validator-accepted proposal and records no human approval.",
            "It does not mutate, persist, export, apply, or authorize the proposed change.",
            "Execution requires a fresh baseline identity check and fresh validation.",
            "Unlimited validation does not establish ownership, affordability, or spending authority.",
            "Identity digests detect mismatches; they are not signatures or authentication.",
        ],
    }
    presentation_identity = _identity(
        "presentation_identity_version", PRESENTATION_IDENTITY_VERSION,
        {"proposal_identity": proposal_identity, "review_artifact": review_artifact},
    )
    return {
        "proposal_presentation_model_version": PROPOSAL_PRESENTATION_MODEL_VERSION,
        "source_proposal_model_version": "2",
        "status": "presentable",
        "reason": "validator_accepted",
        "proposal_identity": proposal_identity,
        "presentation_identity": presentation_identity,
        "review_artifact": review_artifact,
    }
