"""Explicit authority for one narrow future deck-edit proposal shape.

This model validates a declaration. It does not evaluate recommendation context,
construct a Deck, or decide whether a concrete addition is valid.
"""
from __future__ import annotations

from typing import Any


PROPOSAL_POLICY_MODEL_VERSION = "1"
_SOURCE_KINDS = frozenset({"explicit_user", "explicit_operator_profile"})
_ZONES = frozenset({"main", "sideboard", "commander"})
_SPEC_FIELDS = frozenset({
    "policy_id", "policy_source", "need_key", "recommendation_requirement",
    "candidate_pool_requirement", "eligibility_requirement",
    "printing_requirement", "operation", "quantity", "target_zone",
    "resource_mode",
})


def _mapping(value: Any, label: str, fields: frozenset[str]) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{label} has missing or unsupported fields")
    return value


def _text(value: Any, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _fixed(value: Any, expected: str, label: str) -> str:
    if type(value) is not str or value != expected:
        raise ValueError(f"{label} is unsupported by Proposal Policy v1")
    return value


def _zone(value: Any, label: str) -> str:
    if type(value) is not str or value not in _ZONES:
        raise ValueError(f"{label} is not a supported Deck zone")
    return value


def build_proposal_policy(policy_spec: dict) -> dict:
    """Normalize one explicit V1 proposal-shape policy; grant no deck validity."""
    spec = _mapping(policy_spec, "proposal policy", _SPEC_FIELDS)
    source = _mapping(
        spec["policy_source"], "proposal policy source",
        frozenset({"kind", "provenance"}),
    )
    if type(source["kind"]) is not str or source["kind"] not in _SOURCE_KINDS:
        raise ValueError("proposal authority requires an explicit source")
    provenance = _mapping(
        source["provenance"], "proposal policy provenance",
        frozenset({"reference"}),
    )
    need = _mapping(
        spec["need_key"], "proposal structured need key",
        frozenset({"zone", "finding_id", "dependency_id"}),
    )
    if type(spec["quantity"]) is not int or spec["quantity"] != 1:
        raise ValueError("Proposal Policy v1 permits exactly one added copy")

    return {
        "proposal_policy_model_version": PROPOSAL_POLICY_MODEL_VERSION,
        "required_recommendation_context_model_version": "1",
        "policy_id": _text(spec["policy_id"], "proposal policy ID"),
        "policy_source": {
            "kind": source["kind"],
            "provenance": {
                "reference": _text(provenance["reference"], "provenance reference"),
            },
        },
        "need_key": {
            "zone": _zone(need["zone"], "structured need zone"),
            "finding_id": _text(need["finding_id"], "finding ID"),
            "dependency_id": _text(need["dependency_id"], "dependency ID"),
        },
        "recommendation_requirement": _fixed(
            spec["recommendation_requirement"], "recommendable", "recommendation requirement",
        ),
        "candidate_pool_requirement": _fixed(
            spec["candidate_pool_requirement"], "complete_only", "candidate-pool requirement",
        ),
        "eligibility_requirement": _fixed(
            spec["eligibility_requirement"], "resolved", "eligibility requirement",
        ),
        "printing_requirement": _fixed(
            spec["printing_requirement"], "single_eligible_printing", "printing requirement",
        ),
        "operation": _fixed(spec["operation"], "add_only", "proposal operation"),
        "quantity": spec["quantity"],
        "target_zone": _zone(spec["target_zone"], "target zone"),
        "resource_mode": _fixed(spec["resource_mode"], "unlimited", "resource mode"),
        "limitations": [
            "The declaration applies to exactly one explicitly named structured need.",
            "Only a positive, untruncated, resolved recommendation with one eligible printing may proceed.",
            "The target zone is explicitly declared, not inferred from the source need.",
            "A future builder must check the immutable baseline Deck and validate any concrete proposal.",
            "No printing choice, Deck construction, validation, crafting, removal, or mutation occurs here.",
        ],
    }
