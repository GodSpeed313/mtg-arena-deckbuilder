"""Deterministic structural needs projected from dependency findings.

This layer consumes Pass #5B output.  It does not inspect card text, rebuild
dependencies, assess quantity sufficiency, search candidates, or recommend
changes.
"""
from __future__ import annotations

from copy import deepcopy


NEEDS_MODEL_VERSION = "2"


def _finding_version(dependency: dict) -> str:
    return dependency["dependency_id"].rsplit(".v", 1)[-1]


def needs_findings(
    dependencies: list[dict], evidence_boundary: dict,
) -> list[dict]:
    """Project active dependency gaps into needs or neutral opportunities."""
    findings = []
    for dependency in dependencies:
        state = dependency.get("state")
        if state == "supported":
            continue
        version = _finding_version(dependency)
        if state in {"payoff_without_enabler", "payoff_without_compatible_enabler"}:
            finding_type = "support_need"
            identifier = f"need.{dependency['label']}.enabler.v{version}"
            existing_name, missing_name = "payoff", "enabler"
            explanation = (
                f"Reviewed {dependency['label']} payoff or consumer evidence is "
                "present, but no compatible reviewed matching enabler is present in this zone."
            )
        elif state == "enabler_without_payoff":
            finding_type = "unused_support_opportunity"
            identifier = f"opportunity.{dependency['label']}.payoff.v{version}"
            existing_name, missing_name = "enabler", "payoff"
            explanation = (
                f"Reviewed {dependency['label']} enabler evidence is present without "
                "a reviewed matching payoff or consumer in this zone; this is an "
                "opportunity observation, not a deck need."
            )
        elif state == "optional_support_absent":
            finding_type = "optional_support_observation"
            identifier = f"observation.{dependency['label']}.optional_enabler.v{version}"
            existing_name, missing_name = "payoff", "enabler"
            explanation = (
                f"Reviewed {dependency['label']} consumer evidence is present without this "
                "optional reviewed support route; this is not a deck need."
            )
        elif state == "conditionally_supported":
            finding_type = "conditional_support_observation"
            identifier = f"observation.{dependency['label']}.conditional_support.v{version}"
            existing_name, missing_name = "payoff", "enabler"
            explanation = (
                f"Reviewed {dependency['label']} support exists in this zone, but its "
                "reviewed producer has a trigger, cost, condition, or qualifier."
            )
        else:
            raise ValueError("unsupported dependency state")

        findings.append({
            "finding_id": identifier,
            "finding_type": finding_type,
            "dependency_id": dependency["dependency_id"],
            "dependency_label": dependency["label"],
            "dependency_state": state,
            "dependency_policy": dependency.get("policy", "strict"),
            "existing_side_name": existing_name,
            "missing_side_name": missing_name,
            "existing_side": deepcopy(dependency[f"{existing_name}_side"]),
            "missing_side": deepcopy(dependency[f"{missing_name}_side"]),
            "explanation": explanation,
            "evidence_boundary": deepcopy(evidence_boundary),
        })
    return findings
