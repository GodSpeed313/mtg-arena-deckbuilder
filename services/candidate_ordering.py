"""Apply an explicit #6C policy to candidates within each structured need.

Pair results are the authority. Ordered groups are emitted only when those
results form a consistent total preorder; canonical serialization never
supplies strategic precedence.
"""
from __future__ import annotations

from copy import deepcopy
from itertools import combinations
from math import isfinite
from typing import Any

from services.preference_policy import PREFERENCE_CRITERIA, build_preference_policy
from services.strategic_fit import build_strategic_fit_signals


CANDIDATE_ORDERING_MODEL_VERSION = "1"
_CRITERIA = {item.criterion_id: item for item in PREFERENCE_CRITERIA}
_ZONES = {"main": 0, "sideboard": 1, "commander": 2}


def _need_key(value: Any) -> tuple[str, str, str]:
    if not isinstance(value, dict) or set(value) != {"zone", "finding_id", "dependency_id"}:
        raise ValueError("structured need key is malformed")
    if value["zone"] not in _ZONES or any(
        not isinstance(value[field], str) or not value[field]
        for field in ("finding_id", "dependency_id")
    ):
        raise ValueError("structured need key is malformed")
    return value["zone"], value["finding_id"], value["dependency_id"]


def _serialization_key(title: dict) -> tuple[str, int]:
    return title["name"].casefold(), title["title_id"]


def _policy_applies(policy: dict, key: tuple[str, str, str]) -> bool:
    scope = policy["scope"]
    return (not scope["zones"] or key[0] in scope["zones"]) and (
        not scope["need_keys"] or key in {_need_key(item) for item in scope["need_keys"]}
    )


def _fact(criterion, rule: dict, title: dict, signals: dict) -> dict:
    title_id = title["title_id"]
    path = criterion.source_locator
    if criterion.source_model == "strategic_fit":
        signal_id = path
        if signal_id.startswith(("fit.support.",)):
            record = signals["candidate"]
            source_path = "need_signal_sets.candidates.signals"
        else:
            record = signals["title"]
            source_path = "title_signal_index.signals"
        matching = [item for item in record if item["signal_id"] == signal_id]
        value = bool(matching)
        return {
            "fact_status": "known", "value": value,
            "source_path": f"{source_path}[title_id={title_id}][signal_id={signal_id}]",
            "source_evidence": deepcopy(matching),
        }

    if path == "title_index.need_coverage.matched_need_count":
        value = title["need_coverage"]["matched_need_count"]
    elif path == "title_index.canonical_facts.mana_value":
        value = title["canonical_facts"].get("mana_value")
        if value is None or value == "":
            return {
                "fact_status": "unknown", "value": deepcopy(value),
                "source_path": f"title_index[title_id={title_id}].canonical_facts.mana_value",
                "source_evidence": [],
            }
        if type(value) not in (int, float) or not isfinite(value):
            raise ValueError("mana value must be numeric or unknown")
    elif path == "title_index.package_ids":
        package_ids = title["package_ids"]
        value = (rule["prerequisite"]["package_id"] in package_ids
                 if criterion.value_type == "named_package_presence" else len(package_ids))
    else:
        raise ValueError("criterion has an unsupported source locator")
    return {
        "fact_status": "known", "value": value,
        "source_path": f"title_index[title_id={title_id}].{path.removeprefix('title_index.')}",
        "source_evidence": deepcopy(title["package_ids"] if path == "title_index.package_ids" else value),
    }


def _compare_pair(key: dict, left: dict, right: dict, policy: dict,
                  signals_by_title: dict, signals_by_need: dict) -> dict:
    trace = []
    result = "tie"
    preceding = None
    decisive = None
    for rule in policy["rules"]:
        criterion = _CRITERIA[rule["criterion_id"]]
        a = _fact(criterion, rule, left, {
            "title": signals_by_title[left["title_id"]],
            "candidate": signals_by_need[left["title_id"]],
        })
        b = _fact(criterion, rule, right, {
            "title": signals_by_title[right["title_id"]],
            "candidate": signals_by_need[right["title_id"]],
        })
        step = {
            "policy_rule_id": rule["policy_rule_id"],
            "criterion_id": rule["criterion_id"],
            "criterion_source": deepcopy(rule["criterion_source"]),
            "prerequisite": deepcopy(rule["prerequisite"]),
            "direction": rule["behavior"]["direction"],
            "unknown_behavior": rule["unknown_behavior"],
            "source_provenance": deepcopy(rule["source_provenance"]),
            "left_fact": a, "right_fact": b,
        }
        if "unknown" in (a["fact_status"], b["fact_status"]):
            if rule["unknown_behavior"] == "indeterminate":
                step["outcome"] = "indeterminate"
                result = "indeterminate"
                decisive = rule["policy_rule_id"]
            else:
                step["outcome"] = "equal_for_this_rule"
        elif a["value"] == b["value"]:
            step["outcome"] = "equal"
        else:
            direction = rule["behavior"]["direction"]
            if direction == "prefer_lower":
                prefer_left = a["value"] < b["value"]
            elif direction == "prefer_higher":
                prefer_left = a["value"] > b["value"]
            elif direction == "prefer_present":
                prefer_left = a["value"] is True
            elif direction == "prefer_absent":
                prefer_left = a["value"] is False
            else:
                raise ValueError("policy direction is unsupported")
            preceding = left["title_id"] if prefer_left else right["title_id"]
            result = "precedes"
            decisive = rule["policy_rule_id"]
            step["outcome"] = "left_precedes" if prefer_left else "right_precedes"
        trace.append(step)
        if result != "tie":
            break
    return {
        "need_key": deepcopy(key), "policy_id": policy["policy_id"],
        "left": {"title_id": left["title_id"], "name": left["name"]},
        "right": {"title_id": right["title_id"], "name": right["name"]},
        "status": result, "preceding_title_id": preceding,
        "decisive_policy_rule_id": decisive, "rule_trace": trace,
    }


def _ordered_groups(candidates: list[dict], pairs: list[dict]) -> list[list[int]] | None:
    """Return groups only for a consistent, fully comparable total preorder."""
    ids = [item["title_id"] for item in candidates]
    if any(pair["status"] == "indeterminate" for pair in pairs):
        return None
    tied = {title_id: {title_id} for title_id in ids}
    for pair in pairs:
        if pair["status"] == "tie":
            a, b = pair["left"]["title_id"], pair["right"]["title_id"]
            tied[a].add(b)
            tied[b].add(a)
    # Equality must be transitive. A skipped unknown can violate this.
    if any(tied[a] != tied[b] for a in ids for b in tied[a]):
        return None
    groups = []
    seen = set()
    for title_id in ids:
        if title_id not in seen:
            group = [item for item in ids if item in tied[title_id]]
            groups.append(group)
            seen.update(group)
    group_of = {title_id: i for i, group in enumerate(groups) for title_id in group}
    before = set()
    for pair in pairs:
        if pair["status"] != "precedes":
            continue
        winner = pair["preceding_title_id"]
        loser = (pair["right"]["title_id"] if winner == pair["left"]["title_id"]
                 else pair["left"]["title_id"])
        if group_of[winner] == group_of[loser]:
            return None
        before.add((group_of[winner], group_of[loser]))
    if any((b, a) in before for a, b in before):
        return None
    ordered = sorted(range(len(groups)), key=lambda i: -sum(a == i for a, _ in before))
    if any((ordered[i], ordered[j]) not in before
           for i in range(len(ordered)) for j in range(i + 1, len(ordered))):
        return None
    return [groups[i] for i in ordered]


def build_candidate_ordering(candidate_comparison: dict, strategic_fit: dict,
                             preference_policy: dict) -> dict:
    """Order only same-need candidates under a validated explicit policy."""
    if not isinstance(candidate_comparison, dict) or not isinstance(strategic_fit, dict):
        raise ValueError("complete comparison and strategic fit models are required")
    if candidate_comparison.get("candidate_comparison_model_version") != "1":
        raise ValueError("Candidate Comparison Model Version 1 is required")
    if strategic_fit.get("strategic_fit_model_version") != "1":
        raise ValueError("Strategic Fit Model Version 1 is required")
    if strategic_fit != build_strategic_fit_signals(candidate_comparison):
        raise ValueError("strategic fit contradicts candidate comparison")
    if not isinstance(preference_policy, dict) or (
        preference_policy.get("strategic_preference_policy_model_version") != "1"
        or preference_policy.get("preference_rule_registry_version") != "1"
    ):
        raise ValueError("Strategic Preference Policy Model Version 1 is required")
    policy_fields = ("policy_id", "policy_source", "scope", "eligibility_handling",
                     "rules", "serialization")
    if any(field not in preference_policy for field in policy_fields):
        raise ValueError("complete preference policy is required")
    policy = build_preference_policy({field: preference_policy[field] for field in policy_fields})
    if preference_policy != policy:
        raise ValueError("preference policy is not a normalized Version 1 model")

    titles = {item["title_id"]: item for item in candidate_comparison["title_index"]}
    title_signals = {item["title_id"]: item["signals"]
                     for item in strategic_fit["title_signal_index"]}
    need_signals = {_need_key(item["need_key"]): item
                    for item in strategic_fit["need_signal_sets"]}
    output = []
    for matrix in sorted(candidate_comparison["need_matrices"], key=lambda item: (
        _ZONES[_need_key(item["need_key"])[0]], *_need_key(item["need_key"])[1:],
    )):
        key = _need_key(matrix["need_key"])
        fit_need = need_signals[key]
        by_id = {item["title_id"]: item["signals"] for item in fit_need["candidates"]}
        candidates = sorted((titles[item["title_id"]] for item in matrix["candidates"]),
                            key=_serialization_key)
        serialized = [{"title_id": item["title_id"], "name": item["name"],
                       "eligibility_status": item["eligibility_status"],
                       "unresolved_eligibility": deepcopy(item["unresolved_eligibility"])}
                      for item in candidates]
        applicable = _policy_applies(policy, key)
        pairs = ([_compare_pair(matrix["need_key"], a, b, policy, title_signals, by_id)
                  for a, b in combinations(candidates, 2)]
                 if applicable and policy["rules"] else [])
        groups = _ordered_groups(candidates, pairs) if pairs else None
        if not applicable:
            status = "policy_not_applicable"
        elif not policy["rules"]:
            status = "no_declared_preference"
        elif len(candidates) < 2:
            status = "insufficient_candidates"
        elif any(pair["status"] == "indeterminate" for pair in pairs) or (
            pairs and groups is None
        ):
            status = "indeterminate"
        elif any(pair["status"] == "precedes" for pair in pairs):
            status = "strategically_ordered"
        else:
            status = "tied"
        output.append({
            "need_key": deepcopy(matrix["need_key"]),
            "policy_id": policy["policy_id"], "status": status,
            "serialized_candidates": serialized, "pairwise_results": pairs,
            "ordered_groups": groups if status == "strategically_ordered" else None,
        })
    return {
        "candidate_ordering_model_version": CANDIDATE_ORDERING_MODEL_VERSION,
        "source_candidate_comparison_model_version": "1",
        "source_strategic_fit_model_version": "1",
        "source_strategic_preference_policy_model_version": "1",
        "policy_id": policy["policy_id"],
        "policy_source": deepcopy(policy["policy_source"]),
        "serialization": deepcopy(policy["serialization"]),
        "need_orderings": output,
        "limitations": [
            "Pair results apply only within one structured need and returned pool.",
            "Serialized candidate order has no strategic meaning.",
            "Ordered groups are absent when pair results are indeterminate or inconsistent.",
            "No deck changes or recommendations are produced.",
        ],
    }
