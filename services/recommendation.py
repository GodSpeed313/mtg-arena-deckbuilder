"""Decide whether #6D establishes one recommendable candidate per need.

This layer consumes ordering authority and never evaluates preference rules.
"""
from __future__ import annotations

from copy import deepcopy
from itertools import combinations
from math import isfinite
from typing import Any


RECOMMENDATION_DECISION_MODEL_VERSION = "1"
_ORDERING_VERSION = "1"
_ZONES = {"main": 0, "sideboard": 1, "commander": 2}
_POOL_STATUSES = frozenset({
    "policy_not_applicable", "no_declared_preference", "insufficient_candidates",
    "tied", "strategically_ordered", "indeterminate",
})


def _need_key(value: Any) -> tuple[str, str, str]:
    if not isinstance(value, dict) or set(value) != {"zone", "finding_id", "dependency_id"}:
        raise ValueError("structured need key is malformed")
    if value["zone"] not in _ZONES or any(
        not isinstance(value[field], str) or not value[field]
        for field in ("finding_id", "dependency_id")
    ):
        raise ValueError("structured need key is malformed")
    return value["zone"], value["finding_id"], value["dependency_id"]


def _identity(value: Any) -> tuple[int, str]:
    if not isinstance(value, dict) or not {"title_id", "name"} <= set(value):
        raise ValueError("candidate identity must be an object")
    title_id, name = value.get("title_id"), value.get("name")
    if type(title_id) is not int or not isinstance(name, str) or not name:
        raise ValueError("candidate identity is malformed")
    return title_id, name


def _validate_candidate(value: Any) -> dict:
    title_id, name = _identity(value)
    if set(value) != {
        "title_id", "name", "eligibility_status", "unresolved_eligibility",
    }:
        raise ValueError("serialized candidate has unexpected fields")
    unresolved = value["unresolved_eligibility"]
    if (not isinstance(unresolved, list) or any(
        not isinstance(item, str) or not item for item in unresolved
    ) or len(unresolved) != len(set(unresolved))):
        raise ValueError("candidate unresolved eligibility is malformed")
    expected = "eligibility_unknown" if unresolved else "eligible"
    if value["eligibility_status"] != expected:
        raise ValueError("candidate eligibility status contradicts unresolved dimensions")
    return {"title_id": title_id, "name": name,
            "eligibility_status": expected, "unresolved_eligibility": deepcopy(unresolved)}


def _validate_trace(pair: dict) -> None:
    trace = pair.get("rule_trace")
    if not isinstance(trace, list) or not trace:
        raise ValueError("pair result requires evaluated policy-rule evidence")
    rule_ids = []
    for index, step in enumerate(trace):
        if not isinstance(step, dict) or set(step) != {
            "policy_rule_id", "criterion_id", "criterion_source", "prerequisite",
            "direction", "unknown_behavior", "source_provenance",
            "left_fact", "right_fact", "outcome",
        }:
            raise ValueError("policy-rule trace entry is malformed")
        rule_id = step.get("policy_rule_id")
        if not isinstance(rule_id, str) or not rule_id:
            raise ValueError("policy-rule trace is missing its rule ID")
        rule_ids.append(rule_id)
        if not isinstance(step.get("criterion_id"), str) or not step["criterion_id"]:
            raise ValueError("policy-rule trace is missing its criterion ID")
        if not isinstance(step.get("criterion_source"), dict) or not isinstance(
            step.get("source_provenance"), dict
        ) or not isinstance(step.get("prerequisite"), dict):
            raise ValueError("policy-rule trace is missing source provenance")
        source = step["criterion_source"]
        expected_locator = ("field_path" if source.get("model") == "candidate_comparison"
                            else "signal_id")
        if (source.get("model") not in {"candidate_comparison", "strategic_fit"}
            or source.get("version") != "1"
            or set(source) != {"model", "version", expected_locator}
            or not isinstance(source[expected_locator], str)
            or not source[expected_locator]):
            raise ValueError("policy-rule source reference is malformed")
        if (set(step["source_provenance"]) != {"reference"} or not isinstance(
            step["source_provenance"]["reference"], str
        ) or not step["source_provenance"]["reference"]):
            raise ValueError("policy-rule declaration provenance is malformed")
        if step.get("direction") not in {
            "prefer_lower", "prefer_higher", "prefer_present", "prefer_absent",
        } or step.get("unknown_behavior") not in {
            "indeterminate", "equal_for_this_rule",
        }:
            raise ValueError("policy-rule trace has invalid behavior")
        facts = []
        for field in ("left_fact", "right_fact"):
            fact = step.get(field)
            if not isinstance(fact, dict) or set(fact) != {
                "fact_status", "value", "source_path", "source_evidence",
            } or fact.get("fact_status") not in {
                "known", "unknown",
            } or not isinstance(fact.get("source_path"), str) or not fact["source_path"]:
                raise ValueError("policy-rule fact is malformed")
            facts.append(fact)
        outcome = step.get("outcome")
        unknown = any(fact["fact_status"] == "unknown" for fact in facts)
        if outcome == "equal":
            valid = not unknown and facts[0].get("value") == facts[1].get("value")
        elif outcome == "equal_for_this_rule":
            valid = unknown and step["unknown_behavior"] == "equal_for_this_rule"
        elif outcome == "indeterminate":
            valid = unknown and step["unknown_behavior"] == "indeterminate"
        elif outcome in {"left_precedes", "right_precedes"}:
            a, b = facts[0].get("value"), facts[1].get("value")
            direction = step["direction"]
            if direction in {"prefer_lower", "prefer_higher"}:
                comparable = all(type(value) in (int, float) and isfinite(value)
                                 for value in (a, b))
                left_precedes = (a < b if direction == "prefer_lower" else a > b) if comparable else False
            else:
                comparable = type(a) is bool and type(b) is bool
                left_precedes = a is True if direction == "prefer_present" else a is False
            valid = (not unknown and comparable and a != b and
                     outcome == ("left_precedes" if left_precedes else "right_precedes"))
        else:
            valid = False
        if not valid or (index < len(trace) - 1 and outcome not in {
            "equal", "equal_for_this_rule",
        }):
            raise ValueError("policy-rule trace contradicts its outcome")
    if len(rule_ids) != len(set(rule_ids)):
        raise ValueError("policy-rule trace repeats a rule")
    final = trace[-1]
    status = pair.get("status")
    if status == "tie":
        if final["outcome"] not in {"equal", "equal_for_this_rule"} or (
            pair.get("preceding_title_id") is not None
            or pair.get("decisive_policy_rule_id") is not None
        ):
            raise ValueError("tie contradicts its rule trace")
    elif status == "indeterminate":
        if final["outcome"] != "indeterminate" or (
            pair.get("preceding_title_id") is not None
            or pair.get("decisive_policy_rule_id") != final["policy_rule_id"]
        ):
            raise ValueError("indeterminate relation contradicts its rule trace")
    elif status == "precedes":
        expected = (pair["left"]["title_id"] if final["outcome"] == "left_precedes"
                    else pair["right"]["title_id"] if final["outcome"] == "right_precedes"
                    else None)
        if expected is None or pair.get("preceding_title_id") != expected or (
            pair.get("decisive_policy_rule_id") != final["policy_rule_id"]
        ):
            raise ValueError("precedence contradicts its rule trace")
    else:
        raise ValueError("pair relation status is unsupported")


def _validate_pool(pool: Any, policy_id: str) -> tuple[list[dict], list[dict]]:
    if not isinstance(pool, dict):
        raise ValueError("need ordering must be an object")
    key = _need_key(pool.get("need_key"))
    if pool.get("policy_id") != policy_id or pool.get("status") not in _POOL_STATUSES:
        raise ValueError("need ordering has contradictory policy or status")
    raw_candidates = pool.get("serialized_candidates")
    if not isinstance(raw_candidates, list):
        raise ValueError("serialized candidates must be a list")
    candidates = [_validate_candidate(item) for item in raw_candidates]
    ids = [item["title_id"] for item in candidates]
    if len(ids) != len(set(ids)) or candidates != sorted(
        candidates, key=lambda item: (item["name"].casefold(), item["title_id"])
    ):
        raise ValueError("candidate identities or canonical serialization are malformed")
    raw_pairs = pool.get("pairwise_results")
    if not isinstance(raw_pairs, list):
        raise ValueError("pairwise results must be a list")
    pairs = []
    expected = {(a["title_id"], b["title_id"])
                for a, b in combinations(candidates, 2)}
    seen = set()
    by_id = {item["title_id"]: item for item in candidates}
    for pair in raw_pairs:
        if not isinstance(pair, dict) or set(pair) != {
            "need_key", "policy_id", "left", "right", "status",
            "preceding_title_id", "decisive_policy_rule_id", "rule_trace",
        } or pair.get("need_key") != pool["need_key"] or (
            pair.get("policy_id") != policy_id
        ):
            raise ValueError("pair provenance contradicts its need or policy")
        left_id, left_name = _identity(pair.get("left"))
        right_id, right_name = _identity(pair.get("right"))
        if set(pair["left"]) != {"title_id", "name"} or set(pair["right"]) != {
            "title_id", "name",
        }:
            raise ValueError("pair candidate identity has unexpected fields")
        identity = (left_id, right_id)
        if (identity not in expected or identity in seen or
            by_id[left_id]["name"] != left_name or by_id[right_id]["name"] != right_name):
            raise ValueError("pair candidate identity is absent, reversed, or duplicated")
        seen.add(identity)
        _validate_trace(pair)
        pairs.append(pair)
    status = pool["status"]
    inactive = {"policy_not_applicable", "no_declared_preference", "insufficient_candidates"}
    if status in inactive:
        if pairs or pool.get("ordered_groups") is not None or (
            status == "insufficient_candidates" and len(candidates) >= 2
        ):
            raise ValueError("inactive need ordering carries strategic comparisons")
    elif seen != expected:
        raise ValueError("active need ordering lacks an exact candidate pair")
    if status == "tied" and (len(candidates) < 2 or any(
        pair["status"] != "tie" for pair in pairs
    ) or pool.get("ordered_groups") is not None):
        raise ValueError("tied need ordering contradicts its pairs")
    if status == "indeterminate" and (len(candidates) < 2 or
                                           pool.get("ordered_groups") is not None):
        raise ValueError("indeterminate need ordering has invalid groups")
    if status == "strategically_ordered":
        groups = pool.get("ordered_groups")
        if (not isinstance(groups, list) or len(groups) < 2 or any(
            not isinstance(group, list) or not group for group in groups
        )):
            raise ValueError("ordered groups are malformed")
        flattened = [title_id for group in groups for title_id in group]
        if len(flattened) != len(ids) or set(flattened) != set(ids):
            raise ValueError("ordered groups duplicate or omit a candidate")
        position_by_id = {item["title_id"]: index for index, item in enumerate(candidates)}
        if any(group != sorted(group, key=position_by_id.__getitem__) for group in groups):
            raise ValueError("tied group must use canonical serialization")
        positions = {title_id: index for index, group in enumerate(groups)
                     for title_id in group}
        for pair in pairs:
            left, right = pair["left"]["title_id"], pair["right"]["title_id"]
            if positions[left] == positions[right]:
                if pair["status"] != "tie":
                    raise ValueError("ordered-group tie contradicts pair relation")
            elif pair["status"] != "precedes" or pair["preceding_title_id"] != (
                left if positions[left] < positions[right] else right
            ):
                raise ValueError("ordered-group precedence contradicts pair relation")
    return candidates, pairs


def _decision(pool: dict, candidates: list[dict], pairs: list[dict],
              model: dict) -> dict:
    ids = [item["title_id"] for item in candidates]
    by_id = {item["title_id"]: item for item in candidates}
    pair_by_ids = {frozenset((item["left"]["title_id"], item["right"]["title_id"])): item
                   for item in pairs}
    first = []
    if len(ids) >= 2 and pairs:
        for candidate_id in ids:
            if all(pair_by_ids[frozenset((candidate_id, other))]["status"] == "precedes"
                   and pair_by_ids[frozenset((candidate_id, other))]["preceding_title_id"]
                   == candidate_id for other in ids if other != candidate_id):
                first.append(candidate_id)
    if len(first) > 1:
        raise ValueError("two candidates cannot both precede every other candidate")
    unbeaten = [candidate_id for candidate_id in ids if not any(
        item["status"] == "precedes" and item["preceding_title_id"] != candidate_id
        and candidate_id in {item["left"]["title_id"], item["right"]["title_id"]}
        for item in pairs
    )]
    top_tied = bool(pairs) and len(unbeaten) > 1 and all(
        pair_by_ids[frozenset((a, b))]["status"] == "tie"
        for a, b in combinations(unbeaten, 2)
    ) and all(
        pair_by_ids[frozenset((a, other))]["status"] == "precedes"
        and pair_by_ids[frozenset((a, other))]["preceding_title_id"] == a
        for a in unbeaten for other in ids if other not in unbeaten
    )

    status = pool["status"]
    if not candidates:
        outcome = "no_candidates"
    elif status == "no_declared_preference":
        outcome = "no_declared_preference"
    elif status == "policy_not_applicable":
        outcome = "policy_not_applicable"
    elif len(candidates) == 1:
        outcome = "single_candidate_no_preference"
    elif first:
        outcome = ("recommendable" if by_id[first[0]]["eligibility_status"] == "eligible"
                   else "unresolved_eligibility")
    elif top_tied:
        outcome = "top_tie"
    elif any(item["status"] == "indeterminate" for item in pairs):
        outcome = "indeterminate_ordering"
    else:
        outcome = "inconsistent_ordering"

    first_id = first[0] if first else None
    evidence = [deepcopy(item) for item in pairs if first_id in {
        item["left"]["title_id"], item["right"]["title_id"]
    }] if first_id is not None else [deepcopy(item) for item in pairs if (
        item["status"] != "precedes" or outcome == "top_tie"
    )]
    return {
        "need_key": deepcopy(pool["need_key"]),
        "source_need_reference": (
            f"need_matrices[need_key={pool['need_key']['zone']}|"
            f"{pool['need_key']['finding_id']}|{pool['need_key']['dependency_id']}].source_need"
        ),
        "policy_id": model["policy_id"],
        "policy_source": deepcopy(model["policy_source"]),
        "ordering_model_version": _ORDERING_VERSION,
        "ordering_status": status,
        "outcome": outcome,
        "reason": ("unique_first_under_explicit_policy" if outcome == "recommendable"
                   else outcome),
        "candidate": (deepcopy(by_id[first_id]) if outcome == "recommendable" else None),
        "blocked_candidate": (deepcopy(by_id[first_id])
                              if outcome == "unresolved_eligibility" else None),
        "ordered_first_group": (deepcopy(pool["ordered_groups"][0])
                                if pool.get("ordered_groups") else None),
        "relation_evidence": evidence,
        "unresolved_conditions": (deepcopy(by_id[first_id]["unresolved_eligibility"])
                                  if outcome == "unresolved_eligibility" else []),
        "considered_candidate_count": len(candidates),
    }


def build_recommendation_decisions(ordering: dict) -> dict:
    """Decide unique-first status from a complete Candidate Ordering v1 model."""
    if not isinstance(ordering, dict) or ordering.get("candidate_ordering_model_version") != "1":
        raise ValueError("Candidate Ordering Model Version 1 is required")
    for field in ("source_candidate_comparison_model_version",
                  "source_strategic_fit_model_version",
                  "source_strategic_preference_policy_model_version"):
        if ordering.get(field) != "1":
            raise ValueError("ordering source model versions are unsupported")
    policy_id = ordering.get("policy_id")
    if not isinstance(policy_id, str) or not policy_id:
        raise ValueError("ordering policy ID is malformed")
    policy_source = ordering.get("policy_source")
    if not isinstance(policy_source, dict) or policy_source.get("kind") not in {
        "explicit_user", "explicit_operator_profile",
    } or not isinstance(policy_source.get("provenance"), dict) or not isinstance(
        policy_source["provenance"].get("reference"), str
    ) or not policy_source["provenance"]["reference"]:
        raise ValueError("explicit policy provenance is required")
    if ordering.get("serialization") != {
        "method": "casefolded_name_then_title_id", "non_strategic": True,
    }:
        raise ValueError("canonical non-strategic serialization is required")
    pools = ordering.get("need_orderings")
    if not isinstance(pools, list):
        raise ValueError("need orderings must be a list")
    seen = set()
    decisions = []
    for pool in pools:
        key = _need_key(pool.get("need_key") if isinstance(pool, dict) else None)
        if key in seen:
            raise ValueError("structured need is duplicated")
        seen.add(key)
        candidates, pairs = _validate_pool(pool, policy_id)
        decisions.append(_decision(pool, candidates, pairs, ordering))
    decisions.sort(key=lambda item: (
        _ZONES[item["need_key"]["zone"]], item["need_key"]["finding_id"],
        item["need_key"]["dependency_id"],
    ))
    return {
        "recommendation_decision_model_version": RECOMMENDATION_DECISION_MODEL_VERSION,
        "source_candidate_ordering_model_version": _ORDERING_VERSION,
        "source_candidate_comparison_model_version": "1",
        "source_strategic_fit_model_version": "1",
        "source_strategic_preference_policy_model_version": "1",
        "policy_id": policy_id,
        "decisions": decisions,
        "limitations": [
            "Decisions apply only to candidates considered for each structured need.",
            "A recommendation decision does not direct a deck change.",
            "No replacement or quantity is selected.",
        ],
    }
