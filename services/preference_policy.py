"""Explicit strategic preference policy without candidate ordering.

The model records which reviewed upstream criteria a future ordering engine may
use.  Criteria remain neutral; direction belongs only to an explicit policy.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from services.packages import FUNCTIONAL_PACKAGES


STRATEGIC_PREFERENCE_POLICY_MODEL_VERSION = "2"
PREFERENCE_RULE_REGISTRY_VERSION = "2"

_POLICY_SOURCE_KINDS = frozenset({"explicit_user", "explicit_operator_profile"})
_UNKNOWN_BEHAVIORS = frozenset({"indeterminate", "equal_for_this_rule"})
_ZONES = ("main", "sideboard", "commander")
_PACKAGE_IDS = frozenset(item.package_id for item in FUNCTIONAL_PACKAGES)
_FORBIDDEN_KEYS = frozenset({
    "weight", "weights", "score", "scores", "utility", "points", "rank",
    "ranking", "tier", "tiers", "bonus", "penalty", "recommendation",
    "replacement", "deck_mutation", "candidate", "candidates", "candidate_id",
    "title_id", "ordered_candidates", "winner", "selection", "quantity",
})
_FORBIDDEN_EXPLANATION_TERMS = (
    "score", "rank", "recommend", "replacement", "deck mutation", "winner",
    "best candidate", "choose candidate", "select candidate", "tie-break",
    "add card", "remove card", "add copies", "remove copies", "quantity suggestion",
)


@dataclass(frozen=True, slots=True)
class PreferenceCriterion:
    criterion_id: str
    source_model: str
    source_version: str
    source_locator_kind: str
    source_locator: str
    value_type: str
    allowed_directions: tuple[str, ...]
    prerequisite_kind: str
    explanation: str

    def source(self) -> dict:
        result = {"model": self.source_model, "version": self.source_version}
        result[self.source_locator_kind] = self.source_locator
        return result


PREFERENCE_CRITERIA = (
    PreferenceCriterion(
        "criterion.need.observed_match_count.v1",
        "candidate_comparison", "2", "field_path",
        "title_index.need_coverage.matched_need_count", "integer",
        ("prefer_lower", "prefer_higher"), "none",
        "The number of structured observed needs matched by a title.",
    ),
    PreferenceCriterion(
        "criterion.mana.value.v1",
        "candidate_comparison", "2", "field_path",
        "title_index.canonical_facts.mana_value", "number",
        ("prefer_lower", "prefer_higher"), "none",
        "The canonical mana value reported by Candidate Comparison Model Version 2.",
    ),
    PreferenceCriterion(
        "criterion.package.named_presence.v1",
        "candidate_comparison", "2", "field_path",
        "title_index.package_ids", "named_package_presence",
        ("prefer_present", "prefer_absent"), "package_id",
        "Whether a specifically named reviewed functional package is present.",
    ),
    PreferenceCriterion(
        "criterion.package.count.v1",
        "candidate_comparison", "2", "field_path",
        "title_index.package_ids", "integer",
        ("prefer_lower", "prefer_higher"), "none",
        "The number of distinct reviewed functional-package IDs on a title.",
    ),
    PreferenceCriterion(
        "criterion.support.unconditional_presence.v1",
        "strategic_fit", "2", "signal_id", "fit.support.unconditional.v1",
        "signal_presence", ("prefer_present", "prefer_absent"), "none",
        "Presence of an unconditional reviewed support path for the current need.",
    ),
    PreferenceCriterion(
        "criterion.support.prerequisites_presence.v1",
        "strategic_fit", "2", "signal_id", "fit.support.prerequisites_present.v1",
        "signal_presence", ("prefer_present", "prefer_absent"), "none",
        "Presence of explicit prerequisites on reviewed support for the current need.",
    ),
    PreferenceCriterion(
        "criterion.support.conditional_presence.v1",
        "strategic_fit", "2", "signal_id", "fit.support.conditional.v1",
        "signal_presence", ("prefer_present", "prefer_absent"), "none",
        "Presence of a conditional reviewed support path for the current need.",
    ),
    PreferenceCriterion(
        "criterion.support.triggered_presence.v1",
        "strategic_fit", "2", "signal_id", "fit.support.triggered.v1",
        "signal_presence", ("prefer_present", "prefer_absent"), "none",
        "Presence of a triggered reviewed support path for the current need.",
    ),
    PreferenceCriterion(
        "criterion.support.activated_presence.v1",
        "strategic_fit", "2", "signal_id", "fit.support.activated.v1",
        "signal_presence", ("prefer_present", "prefer_absent"), "none",
        "Presence of an activated reviewed support path for the current need.",
    ),
    PreferenceCriterion(
        "criterion.support.partial_presence.v1",
        "strategic_fit", "2", "signal_id", "fit.support.partial.v1",
        "signal_presence", ("prefer_present", "prefer_absent"), "none",
        "Presence of a partially represented reviewed support path.",
    ),
    PreferenceCriterion(
        "criterion.support.unsupported_remainder_presence.v1",
        "strategic_fit", "2", "signal_id",
        "fit.support.unsupported_remainder_present.v1", "signal_presence",
        ("prefer_present", "prefer_absent"), "none",
        "Presence of an unsupported remainder on reviewed support evidence.",
    ),
    PreferenceCriterion(
        "criterion.eligibility.unresolved_presence.v1",
        "strategic_fit", "2", "signal_id",
        "fit.eligibility.unresolved_dimensions_present.v1", "signal_presence",
        ("prefer_present", "prefer_absent"), "none",
        "Presence of explicitly tracked unresolved eligibility dimensions.",
    ),
    PreferenceCriterion(
        "criterion.ownership.known_presence.v1",
        "strategic_fit", "2", "signal_id", "fit.ownership.known.v1",
        "signal_presence", ("prefer_present", "prefer_absent"), "none",
        "Presence of a known recorded ownership count.",
    ),
    PreferenceCriterion(
        "criterion.copy.finite_capacity_presence.v1",
        "strategic_fit", "2", "signal_id",
        "fit.copy.remaining_capacity_positive.v1", "signal_presence",
        ("prefer_present", "prefer_absent"), "none",
        "Presence of positive finite remaining copy capacity.",
    ),
    PreferenceCriterion(
        "criterion.copy.unlimited_presence.v1",
        "strategic_fit", "2", "signal_id", "fit.copy.unlimited.v1",
        "signal_presence", ("prefer_present", "prefer_absent"), "none",
        "Presence of unlimited recorded copy capacity.",
    ),
)
_CRITERIA_BY_ID = {item.criterion_id: item for item in PREFERENCE_CRITERIA}


def _mapping(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return value


def _nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _reject_forbidden_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("policy keys must be strings")
            if key.casefold() in _FORBIDDEN_KEYS:
                raise ValueError(f"policy field {key!r} is not supported")
            _reject_forbidden_keys(item)
    elif isinstance(value, list):
        for item in value:
            _reject_forbidden_keys(item)


def _provenance(value: Any, label: str) -> dict:
    provenance = _mapping(value, label)
    if set(provenance) != {"reference"}:
        raise ValueError(f"{label} must contain only a reference")
    _nonempty(provenance["reference"], f"{label} reference")
    return {"reference": provenance["reference"]}


def _need_key(value: Any) -> dict:
    item = _mapping(value, "structured need key")
    if set(item) != {"zone", "finding_id", "dependency_id"}:
        raise ValueError("structured need key has unexpected fields")
    if item["zone"] not in _ZONES:
        raise ValueError("structured need key has an unsupported zone")
    return {
        "zone": item["zone"],
        "finding_id": _nonempty(item["finding_id"], "finding ID"),
        "dependency_id": _nonempty(item["dependency_id"], "dependency ID"),
    }


def _normalize_scope(value: Any) -> dict:
    scope = _mapping(value, "policy scope")
    if set(scope) != {"zones", "need_keys"}:
        raise ValueError("policy scope must contain zones and need_keys")
    zones = _list(scope["zones"], "policy zones")
    if any(zone not in _ZONES for zone in zones) or len(zones) != len(set(zones)):
        raise ValueError("policy zones are malformed or duplicated")
    zone_set = set(zones)
    normalized_zones = [zone for zone in _ZONES if zone in zone_set]

    need_keys = [_need_key(item) for item in _list(scope["need_keys"], "policy need keys")]
    identities = [
        (item["zone"], item["finding_id"], item["dependency_id"])
        for item in need_keys
    ]
    if len(identities) != len(set(identities)):
        raise ValueError("policy need keys are duplicated")
    if normalized_zones and any(item["zone"] not in zone_set for item in need_keys):
        raise ValueError("policy need key lies outside the explicit zone scope")
    need_keys.sort(key=lambda item: (
        _ZONES.index(item["zone"]), item["finding_id"], item["dependency_id"],
    ))
    return {"zones": normalized_zones, "need_keys": need_keys}


def _normalize_eligibility(value: Any) -> dict:
    handling = _mapping(value, "eligibility handling")
    expected = {"deterministic_ineligibility", "unresolved_eligibility"}
    if set(handling) != expected:
        raise ValueError("eligibility handling has unexpected fields")
    if handling["deterministic_ineligibility"] != "reject_malformed_input":
        raise ValueError("deterministic ineligibility must fail closed downstream")
    if handling["unresolved_eligibility"] != "preserve_unresolved":
        raise ValueError("unresolved eligibility must remain explicit")
    return deepcopy(handling)


def _normalize_serialization(value: Any) -> dict:
    serialization = _mapping(value, "serialization policy")
    if set(serialization) != {"method", "non_strategic"}:
        raise ValueError("serialization policy has unexpected fields")
    if (
        serialization["method"] != "casefolded_name_then_title_id"
        or serialization["non_strategic"] is not True
    ):
        raise ValueError("serialization must be canonical and non-strategic")
    return deepcopy(serialization)


def _normalize_prerequisite(value: Any, criterion: PreferenceCriterion) -> dict:
    prerequisite = _mapping(value, "rule prerequisite")
    if criterion.prerequisite_kind == "none":
        if prerequisite:
            raise ValueError("criterion does not accept a prerequisite")
        return {}
    if criterion.prerequisite_kind == "package_id":
        if set(prerequisite) != {"package_id"} or prerequisite["package_id"] not in _PACKAGE_IDS:
            raise ValueError("named-package criterion requires a reviewed package ID")
        return {"package_id": prerequisite["package_id"]}
    raise ValueError("criterion prerequisite contract is unsupported")


def _normalize_rule(value: Any) -> dict:
    rule = _mapping(value, "preference rule")
    required = {
        "policy_rule_id", "criterion_id", "criterion_source", "prerequisite",
        "behavior", "unknown_behavior", "explanation", "source_provenance",
    }
    if set(rule) != required:
        raise ValueError("preference rule has missing or unexpected fields")
    rule_id = _nonempty(rule["policy_rule_id"], "policy rule ID")
    criterion_id = _nonempty(rule["criterion_id"], "criterion ID")
    if criterion_id not in _CRITERIA_BY_ID:
        raise ValueError("preference rule references an unknown criterion")
    criterion = _CRITERIA_BY_ID[criterion_id]
    source = _mapping(rule["criterion_source"], "criterion source")
    if source != criterion.source():
        raise ValueError("criterion source is not owned by the reviewed registry")
    prerequisite = _normalize_prerequisite(rule["prerequisite"], criterion)

    behavior = _mapping(rule["behavior"], "rule behavior")
    if set(behavior) != {"kind", "direction"}:
        raise ValueError("rule behavior must contain kind and direction")
    if behavior["kind"] != "lexicographic_order":
        raise ValueError("only lexicographic ordering behavior is supported")
    if behavior["direction"] not in criterion.allowed_directions:
        raise ValueError("rule direction is unsupported for its criterion")

    unknown = rule["unknown_behavior"]
    if unknown not in _UNKNOWN_BEHAVIORS:
        raise ValueError("rule unknown behavior is unsupported")
    explanation = _nonempty(rule["explanation"], "rule explanation")
    lowered = explanation.casefold()
    if any(term in lowered for term in _FORBIDDEN_EXPLANATION_TERMS):
        raise ValueError("rule explanation contains unsupported decision semantics")

    return {
        "policy_rule_id": rule_id,
        "criterion_id": criterion_id,
        "criterion_source": criterion.source(),
        "prerequisite": prerequisite,
        "behavior": deepcopy(behavior),
        "unknown_behavior": unknown,
        "explanation": explanation,
        "source_provenance": _provenance(
            rule["source_provenance"], "rule source provenance"
        ),
    }


def build_preference_policy(policy_spec: dict) -> dict:
    """Validate and normalize an explicit policy without ordering candidates."""
    source = _mapping(policy_spec, "preference policy")
    _reject_forbidden_keys(source)
    required = {
        "policy_id", "policy_source", "scope", "eligibility_handling", "rules",
        "serialization",
    }
    if set(source) != required:
        raise ValueError("preference policy has missing or unexpected fields")

    policy_id = _nonempty(source["policy_id"], "policy ID")
    policy_source = _mapping(source["policy_source"], "policy source")
    if set(policy_source) != {"kind", "provenance"}:
        raise ValueError("policy source must contain kind and provenance")
    if policy_source["kind"] not in _POLICY_SOURCE_KINDS:
        raise ValueError("policy source kind is unsupported")
    normalized_source = {
        "kind": policy_source["kind"],
        "provenance": _provenance(policy_source["provenance"], "policy provenance"),
    }

    rules = [_normalize_rule(item) for item in _list(source["rules"], "preference rules")]
    rule_ids = [item["policy_rule_id"] for item in rules]
    if len(rule_ids) != len(set(rule_ids)):
        raise ValueError("policy rule ID is duplicated")
    definitions = [
        (
            item["criterion_id"], item["prerequisite"], item["behavior"],
            item["unknown_behavior"],
        )
        for item in rules
    ]
    if len({repr(item) for item in definitions}) != len(definitions):
        raise ValueError("policy contains a duplicated rule definition")
    criterion_targets = [
        (item["criterion_id"], repr(item["prerequisite"])) for item in rules
    ]
    if len(criterion_targets) != len(set(criterion_targets)):
        raise ValueError("policy contains conflicting rules for one criterion target")

    return {
        "strategic_preference_policy_model_version":
            STRATEGIC_PREFERENCE_POLICY_MODEL_VERSION,
        "preference_rule_registry_version": PREFERENCE_RULE_REGISTRY_VERSION,
        "policy_id": policy_id,
        "policy_source": normalized_source,
        "scope": _normalize_scope(source["scope"]),
        "eligibility_handling": _normalize_eligibility(source["eligibility_handling"]),
        "rules": rules,
        "serialization": _normalize_serialization(source["serialization"]),
        "limitations": [
            "The policy declares explicit preferences but does not order candidates.",
            "Rule sequence is future lexicographic precedence and carries no numeric utility.",
            "An empty rule list means no strategic preference has been declared.",
            "Eligibility handling remains separate from strategic preference rules.",
            "No objective, preference, or missing fact is inferred.",
        ],
    }
