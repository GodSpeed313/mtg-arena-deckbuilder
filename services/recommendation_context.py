"""Read-only bridge from recommendation decisions to reviewed comparison facts."""
from __future__ import annotations

from copy import deepcopy
from typing import Any


RECOMMENDATION_CONTEXT_MODEL_VERSION = "1"
_ZONES = {"main": 0, "sideboard": 1, "commander": 2}
_OUTCOMES = frozenset({
    "recommendable", "top_tie", "indeterminate_ordering",
    "inconsistent_ordering", "no_declared_preference", "policy_not_applicable",
    "no_candidates", "single_candidate_no_preference", "unresolved_eligibility",
})


def _mapping(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return value


def _key(value: Any) -> tuple[str, str, str]:
    item = _mapping(value, "structured need key")
    if set(item) != {"zone", "finding_id", "dependency_id"} or item["zone"] not in _ZONES:
        raise ValueError("structured need key is malformed")
    if any(not isinstance(item[field], str) or not item[field]
           for field in ("finding_id", "dependency_id")):
        raise ValueError("structured need key is malformed")
    return item["zone"], item["finding_id"], item["dependency_id"]


def _sort_key(key: tuple[str, str, str]) -> tuple[int, str, str]:
    return _ZONES[key[0]], key[1], key[2]


def _identity(value: Any, label: str) -> tuple[int, str]:
    item = _mapping(value, label)
    title_id, name = item.get("title_id"), item.get("name")
    if type(title_id) is not int or not isinstance(name, str) or not name:
        raise ValueError(f"{label} identity is malformed")
    return title_id, name


def _condition(condition_id: str, path: str, evidence: Any) -> dict:
    return {"condition_id": condition_id, "source_path": path,
            "evidence": deepcopy(evidence)}


def _candidate_context(title: dict, occurrence: dict, key: tuple[str, str, str]) -> dict:
    title_id, name = _identity(title, "comparison title")
    if occurrence.get("title_id") != title_id:
        raise ValueError("candidate occurrence contradicts canonical title identity")
    eligibility = _mapping(title.get("eligibility"), "candidate eligibility")
    format_fact = _mapping(eligibility.get("format_legality"), "format eligibility")
    ownership = _mapping(eligibility.get("ownership"), "ownership")
    playset = _mapping(eligibility.get("playset"), "copy capacity")
    unresolved = _list(title.get("unresolved_eligibility"), "unresolved eligibility")
    if any(not isinstance(item, str) or not item for item in unresolved):
        raise ValueError("unresolved eligibility dimension is malformed")
    expected_status = "eligibility_unknown" if unresolved else "eligible"
    if title.get("eligibility_status") != expected_status:
        raise ValueError("candidate eligibility status contradicts unresolved dimensions")
    if format_fact.get("status") not in {"legal", "unknown"}:
        raise ValueError("returned candidate has unsupported format eligibility")
    eligible_ids = _list(format_fact.get("eligible_printing_ids"), "eligible printing IDs")
    if (any(type(item) is not int for item in eligible_ids)
        or len(eligible_ids) != len(set(eligible_ids))):
        raise ValueError("eligible printing IDs are malformed")
    printings = _list(title.get("known_printings"), "known printings")
    known_ids = set()
    for printing in printings:
        printing = _mapping(printing, "known printing")
        arena_id = printing.get("arena_id")
        if type(arena_id) is not int or arena_id in known_ids:
            raise ValueError("known printing ID is malformed or duplicated")
        known_ids.add(arena_id)
    if not set(eligible_ids) <= known_ids or (
        format_fact["status"] == "unknown" and eligible_ids
    ):
        raise ValueError("eligible printing IDs contradict known printings or format state")
    owned = ownership.get("owned_copies")
    if ownership.get("status") == "known":
        if type(owned) is not int or owned < 0:
            raise ValueError("known ownership requires a nonnegative copy count")
    elif ownership.get("status") == "unknown":
        if owned is not None:
            raise ValueError("unknown ownership cannot carry a count")
    else:
        raise ValueError("ownership status is unsupported")
    current = playset.get("current_deck_copies")
    if type(current) is not int or current < 0:
        raise ValueError("current deck copy count is malformed")
    if playset.get("status") == "capacity_available":
        limit, remaining = playset.get("copy_limit"), playset.get("remaining_capacity")
        if (type(limit) is not int or limit <= 0 or type(remaining) is not int
            or remaining <= 0 or remaining != limit - current):
            raise ValueError("finite candidate copy capacity is contradictory")
    elif playset.get("status") == "unlimited":
        if playset.get("copy_limit") is not None or playset.get("remaining_capacity") is not None:
            raise ValueError("unlimited capacity cannot carry finite limits")
    else:
        raise ValueError("copy-capacity status is unsupported")
    if occurrence.get("required_feature_eligibility", {}).get("status") != "matched":
        raise ValueError("candidate occurrence lacks required feature match")
    evidence = _list(occurrence.get("matching_feature_evidence"), "matching feature evidence")
    if not evidence:
        raise ValueError("candidate occurrence lacks exact matching feature evidence")
    conditions = []
    base = f"title_index[title_id={title_id}]"
    if len(eligible_ids) > 1:
        conditions.append(_condition(
            "multiple_eligible_printings", f"{base}.eligibility.format_legality.eligible_printing_ids",
            eligible_ids,
        ))
    if ownership["status"] == "unknown":
        conditions.append(_condition("ownership_unknown", f"{base}.eligibility.ownership", ownership))
    elif owned == 0:
        conditions.append(_condition("ownership_known_zero", f"{base}.eligibility.ownership", ownership))
    if unresolved:
        conditions.append(_condition(
            "unresolved_eligibility", f"{base}.unresolved_eligibility", unresolved,
        ))
    return {
        "title_id": title_id, "name": name,
        "eligible_printing_ids": deepcopy(eligible_ids),
        "known_printings": deepcopy(printings),
        "eligibility_status": expected_status,
        "eligibility": deepcopy(eligibility),
        "unresolved_eligibility": deepcopy(unresolved),
        "matching_feature_evidence": deepcopy(evidence),
        "required_feature_eligibility": deepcopy(occurrence["required_feature_eligibility"]),
        "support_context": deepcopy(_mapping(occurrence.get("support_context"), "support context")),
        "source_candidate_reference": (
            f"need_matrices[need_key={key[0]}|{key[1]}|{key[2]}]"
            f".candidates[title_id={title_id}]"
        ),
        "context_conditions": conditions,
    }


def _validate_decision(row: dict, matrix: dict, titles: dict[int, dict],
                       policy_id: str, key: tuple[str, str, str]) -> None:
    if _key(row.get("need_key")) != key or row.get("policy_id") != policy_id:
        raise ValueError("decision need or policy identity contradicts comparison")
    if row.get("outcome") not in _OUTCOMES or row.get("reason") is None:
        raise ValueError("recommendation decision outcome is malformed")
    expected_reason = ("unique_first_under_explicit_policy" if row["outcome"] == "recommendable"
                       else row["outcome"])
    if row["reason"] != expected_reason:
        raise ValueError("recommendation reason contradicts outcome")
    members = {_mapping(item, "comparison candidate").get("title_id")
               for item in _list(matrix.get("candidates"), "comparison candidates")}
    if type(row.get("considered_candidate_count")) is not int or (
        row["considered_candidate_count"] != len(members)
    ):
        raise ValueError("decision candidate count contradicts comparison pool")
    for field in ("candidate", "blocked_candidate"):
        subject = row.get(field)
        if subject is None:
            continue
        title_id, name = _identity(subject, field)
        if title_id not in members or title_id not in titles or name != titles[title_id]["name"]:
            raise ValueError("decision candidate identity contradicts comparison pool")
        if subject.get("eligibility_status") != titles[title_id].get("eligibility_status") or (
            subject.get("unresolved_eligibility") != titles[title_id].get("unresolved_eligibility")
        ):
            raise ValueError("decision candidate eligibility contradicts comparison facts")
    if row["outcome"] == "recommendable":
        if row.get("candidate") is None or row.get("blocked_candidate") is not None:
            raise ValueError("positive decision lacks one recommended candidate")
    elif row.get("candidate") is not None:
        raise ValueError("negative decision cannot contain a recommended candidate")
    if row["outcome"] == "unresolved_eligibility":
        if row.get("blocked_candidate") is None or row.get("unresolved_conditions") != (
            row["blocked_candidate"].get("unresolved_eligibility")
        ):
            raise ValueError("blocked decision lacks unresolved eligibility evidence")
    elif row.get("blocked_candidate") is not None:
        raise ValueError("non-eligibility outcome cannot carry a blocked candidate")
    expected_ref = f"need_matrices[need_key={key[0]}|{key[1]}|{key[2]}].source_need"
    if row.get("source_need_reference") != expected_ref:
        raise ValueError("decision source need reference contradicts comparison")
    for relation in _list(row.get("relation_evidence"), "decision relation evidence"):
        relation = _mapping(relation, "decision pair relation")
        if _key(relation.get("need_key")) != key or relation.get("policy_id") != policy_id:
            raise ValueError("decision pair relation has contradictory provenance")
        for field in ("left", "right"):
            title_id, name = _identity(relation.get(field), "relation candidate")
            if title_id not in members or name != titles[title_id]["name"]:
                raise ValueError("decision relation references a different candidate pool")


def build_recommendation_context(decisions: dict, comparison: dict) -> dict:
    """Attach #6A facts to #6E decisions without changing their outcomes."""
    decisions = _mapping(decisions, "recommendation decisions")
    comparison = _mapping(comparison, "candidate comparison")
    if decisions.get("recommendation_decision_model_version") != "1":
        raise ValueError("Recommendation Decision Model Version 1 is required")
    if comparison.get("candidate_comparison_model_version") != "1":
        raise ValueError("Candidate Comparison Model Version 1 is required")
    for field in ("source_candidate_ordering_model_version",
                  "source_candidate_comparison_model_version",
                  "source_strategic_fit_model_version",
                  "source_strategic_preference_policy_model_version"):
        if decisions.get(field) != "1":
            raise ValueError("recommendation source model version is unsupported")
    for field, expected in (("source_candidate_facts_model_version", "2"),
                            ("source_candidate_model_version", "2"),
                            ("functional_package_model_version", "1")):
        if comparison.get(field) != expected:
            raise ValueError("comparison source model version is unsupported")
    policy_id = decisions.get("policy_id")
    if not isinstance(policy_id, str) or not policy_id:
        raise ValueError("recommendation policy ID is malformed")

    titles = {}
    for title in _list(comparison.get("title_index"), "comparison title index"):
        title_id, name = _identity(title, "comparison title")
        if title_id in titles:
            raise ValueError("comparison title identity is duplicated")
        canonical = _mapping(title.get("canonical_facts"), "canonical facts")
        if canonical.get("title_id") != title_id or canonical.get("name") != name:
            raise ValueError("comparison title identity contradicts canonical facts")
        titles[title_id] = title
    if comparison.get("candidate_title_count") != len(titles):
        raise ValueError("comparison title count contradicts title index")

    matrices = {}
    for matrix in _list(comparison.get("need_matrices"), "comparison need matrices"):
        matrix = _mapping(matrix, "comparison need matrix")
        key = _key(matrix.get("need_key"))
        if key in matrices:
            raise ValueError("comparison structured need is duplicated")
        source_need = _mapping(matrix.get("source_need"), "source need")
        if _key({field: source_need.get(field) for field in ("zone", "finding_id", "dependency_id")}) != key:
            raise ValueError("source need identity contradicts comparison matrix")
        matrices[key] = matrix
    decision_rows = {}
    for row in _list(decisions.get("decisions"), "recommendation decisions"):
        row = _mapping(row, "recommendation decision")
        key = _key(row.get("need_key"))
        if key in decision_rows:
            raise ValueError("recommendation structured need is duplicated")
        decision_rows[key] = row
    if set(decision_rows) != set(matrices):
        raise ValueError("recommendation and comparison need identities differ")

    contexts = []
    for key in sorted(matrices, key=_sort_key):
        matrix, decision = matrices[key], decision_rows[key]
        candidates = _list(matrix.get("candidates"), "comparison candidates")
        if matrix.get("candidate_count") != len(candidates):
            raise ValueError("comparison candidate count contradicts pool")
        summary = _mapping(matrix.get("source_pool_summary"), "source pool summary")
        if type(summary.get("returned")) is not int or summary["returned"] != len(candidates) or (
            type(summary.get("truncated")) is not bool
        ):
            raise ValueError("source pool summary is malformed")
        by_id = {}
        for occurrence in candidates:
            occurrence = _mapping(occurrence, "comparison candidate")
            title_id = occurrence.get("title_id")
            if type(title_id) is not int or title_id in by_id or title_id not in titles:
                raise ValueError("comparison candidate identity is absent or duplicated")
            by_id[title_id] = occurrence
        _validate_decision(decision, matrix, titles, policy_id, key)
        candidate_rows = [_candidate_context(titles[title_id], by_id[title_id], key)
                          for title_id in sorted(by_id, key=lambda item: (
                              titles[item]["name"].casefold(), item,
                          ))]
        pool_conditions = []
        if summary["truncated"]:
            pool_conditions.append(_condition(
                "candidate_pool_truncated",
                f"need_matrices[need_key={key[0]}|{key[1]}|{key[2]}].source_pool_summary.truncated",
                True,
            ))
        contexts.append({
            "need_key": deepcopy(matrix["need_key"]),
            "decision": deepcopy(decision),
            "source_need": deepcopy(matrix["source_need"]),
            "source_pool_summary": deepcopy(summary),
            "source_evidence_completeness": deepcopy(matrix.get("evidence_completeness")),
            "pool_context_conditions": pool_conditions,
            "returned_candidate_facts": candidate_rows,
            "source_context": deepcopy(_mapping(comparison.get("source_context"), "source context")),
        })
    return {
        "recommendation_context_model_version": RECOMMENDATION_CONTEXT_MODEL_VERSION,
        "source_recommendation_decision_model_version": "1",
        "source_candidate_ordering_model_version": "1",
        "source_strategic_fit_model_version": "1",
        "source_strategic_preference_policy_model_version": "1",
        "source_candidate_comparison_model_version": "1",
        "source_candidate_facts_model_version": "2",
        "source_candidate_model_version": "2",
        "functional_package_model_version": "1",
        "policy_id": policy_id,
        "contexts": contexts,
        "limitations": [
            "A positive decision applies only to the returned candidate pool.",
            "Truncation is preserved and does not establish exhaustive coverage.",
            "Context conditions are descriptive and do not select an action.",
            "No printing, quantity, removal, crafting, deck change, or legality is selected.",
        ],
    }
