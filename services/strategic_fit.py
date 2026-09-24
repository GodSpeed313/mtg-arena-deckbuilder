"""Deterministic strategic-fit signals derived only from Comparison Model v2.

Signals in this module describe explicit reviewed facts.  They do not assign
weights, choose candidates, or mutate deck state.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from typing import Any


STRATEGIC_FIT_MODEL_VERSION = "2"
STRATEGIC_SIGNAL_REGISTRY_VERSION = "1"
_COMPARISON_MODEL_VERSION = "2"
_ZONE_ORDER = {"main": 0, "sideboard": 1, "commander": 2}


_SIGNAL_SPECS = (
    ("fit.need.current_match.v1", "need", "candidate_need",
     "Reviewed evidence matches the current structured need."),
    ("fit.need.multiple_observed_needs.v1", "need", "title",
     "This title occurs under more than one observed structured need."),
    ("fit.need.single_observed_need.v1", "need", "title",
     "This title occurs under one observed structured need."),
    ("fit.package.multiple_contributions.v1", "package", "title",
     "This title contributes more than one reviewed functional package."),
    ("fit.package.shared_by_all_returned_candidates.v1", "package", "candidate_need",
     "This candidate contributes package IDs shared by every candidate in this returned pool."),
    ("fit.package.not_shared_by_all_returned_candidates.v1", "package", "candidate_need",
     "This candidate contributes package IDs not shared by every candidate in this returned pool."),
    ("fit.package.pool_local_exclusive_contribution.v1", "package", "candidate_need",
     "This candidate contributes package IDs exclusive to it within this returned pool."),
    ("fit.package.same_set_across_returned_pool.v1", "package", "candidate_need",
     "Every candidate in this returned pool has the same functional-package ID set."),
    ("fit.support.unconditional.v1", "support", "candidate_need",
     "At least one matching reviewed support entry is unconditional."),
    ("fit.support.prerequisites_present.v1", "support", "candidate_need",
     "At least one matching reviewed support entry has explicit prerequisites."),
    ("fit.support.conditional.v1", "support", "candidate_need",
     "At least one matching reviewed support entry is conditional."),
    ("fit.support.triggered.v1", "support", "candidate_need",
     "At least one matching reviewed support entry is triggered."),
    ("fit.support.activated.v1", "support", "candidate_need",
     "At least one matching reviewed support entry is activated."),
    ("fit.support.partial.v1", "support", "candidate_need",
     "At least one matching reviewed support entry is only partially represented."),
    ("fit.support.unsupported_remainder_present.v1", "support", "candidate_need",
     "At least one matching reviewed support entry preserves an unsupported remainder."),
    ("fit.support.alternative_rule_paths.v1", "support", "candidate_need",
     "The current need accepts more than one alternative reviewed rule ID."),
    ("fit.eligibility.unresolved_dimensions_present.v1", "eligibility", "title",
     "One or more explicitly tracked eligibility dimensions remain unresolved."),
    ("fit.eligibility.no_unresolved_dimensions.v1", "eligibility", "title",
     "No explicitly tracked eligibility dimension is unresolved."),
    ("fit.ownership.known.v1", "ownership", "title",
     "The recorded ownership count is known."),
    ("fit.ownership.unknown.v1", "ownership", "title",
     "The recorded ownership count is unknown."),
    ("fit.copy.remaining_capacity_positive.v1", "copy", "title",
     "The recorded finite remaining copy capacity is positive."),
    ("fit.copy.unlimited.v1", "copy", "title",
     "The recorded copy capacity is unlimited."),
    ("fit.source.pool_truncated.v1", "source", "need_pool",
     "The returned candidate pool was truncated by its source limit."),
    ("fit.evidence.zone_boundary_unavailable.v1", "evidence", "need_pool",
     "The source zone evidence boundary is unavailable in Candidate Facts Model Version 3."),
)
_SIGNAL_BY_ID = {
    signal_id: {"category": category, "scope": scope, "explanation": explanation}
    for signal_id, category, scope, explanation in _SIGNAL_SPECS
}
_SIGNAL_ORDER = {item[0]: index for index, item in enumerate(_SIGNAL_SPECS)}


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


def _need_key(value: Any) -> tuple[str, str, str]:
    item = _mapping(value, "need key")
    zone = item.get("zone")
    if zone not in _ZONE_ORDER:
        raise ValueError("need key has an unsupported zone")
    return (
        zone,
        _nonempty(item.get("finding_id"), "finding ID"),
        _nonempty(item.get("dependency_id"), "dependency ID"),
    )


def _need_key_object(key: tuple[str, str, str]) -> dict:
    return {"zone": key[0], "finding_id": key[1], "dependency_id": key[2]}


def _need_sort_key(key: tuple[str, str, str]) -> tuple[int, str, str]:
    return (_ZONE_ORDER[key[0]], key[1], key[2])


def _title_sort_key(title: dict) -> tuple[str, int]:
    return (title["name"].casefold(), title["title_id"])


def _title_path(title_id: int, suffix: str) -> str:
    return f"title_index[title_id={title_id}].{suffix}"


def _matrix_path(key: tuple[str, str, str], suffix: str) -> str:
    identity = f"{key[0]}|{key[1]}|{key[2]}"
    return f"need_matrices[need_key={identity}].{suffix}"


def _candidate_path(key: tuple[str, str, str], title_id: int, suffix: str) -> str:
    return _matrix_path(key, f"candidates[title_id={title_id}].{suffix}")


def _signal(signal_id: str, evidence: list[dict]) -> dict:
    spec = _SIGNAL_BY_ID[signal_id]
    if any(not isinstance(item, dict) or set(item) != {"source_path", "value"}
           for item in evidence):
        raise ValueError("signal evidence is malformed")
    paths = [item["source_path"] for item in evidence]
    return {
        "signal_id": signal_id,
        "category": spec["category"],
        "scope": spec["scope"],
        "state": "present",
        "source_paths": paths,
        "evidence": deepcopy(evidence),
        "explanation": spec["explanation"],
    }


def _fact(path: str, value: Any) -> dict:
    return {"source_path": path, "value": deepcopy(value)}


def _sort_signals(signals: list[dict]) -> list[dict]:
    if len({item["signal_id"] for item in signals}) != len(signals):
        raise ValueError("a signal ID was emitted more than once in one scope")
    return sorted(signals, key=lambda item: _SIGNAL_ORDER[item["signal_id"]])


def _validate_source_need(value: Any, expected_key: tuple[str, str, str]) -> dict:
    source = _mapping(value, "source need")
    if _need_key(source) != expected_key:
        raise ValueError("need key contradicts source need")
    required = source.get("required_feature_rule_ids")
    if (
        not isinstance(required, list)
        or not required
        or any(not isinstance(item, str) or not item for item in required)
        or len(required) != len(set(required))
        or source.get("feature_matching_semantics") != "any"
        or not isinstance(source.get("required_relationship"), str)
        or not source["required_relationship"]
        or source.get("missing_side_name") != "enabler"
    ):
        raise ValueError("source need has malformed feature requirements")
    missing = _mapping(source.get("missing_side"), "source missing side")
    if (
        missing.get("acceptable_feature_rule_ids") != required
        or missing.get("feature_rule_ids") != required
        or missing.get("matching_semantics") != "any"
        or missing.get("relationship") != source["required_relationship"]
    ):
        raise ValueError("source need feature requirements are contradictory")
    return source


def _validate_context(value: Any) -> dict:
    context = _mapping(value, "support dependency context")
    if context.get("availability") not in {
        "unconditional", "conditional", "partially_reviewed",
    }:
        raise ValueError("support availability is malformed")
    if context.get("parse_status") not in {"supported", "partial"}:
        raise ValueError("support parse status is malformed")
    prerequisites = _mapping(context.get("prerequisites"), "support prerequisites")
    trigger = prerequisites.get("trigger")
    if trigger is not None and not isinstance(trigger, dict):
        raise ValueError("support trigger prerequisite is malformed")
    for field in ("costs", "conditions", "qualifiers"):
        _list(prerequisites.get(field), f"support prerequisite {field}")
    _list(context.get("unsupported_remainder"), "support unsupported remainder")
    ability_kind = context.get("ability_kind")
    if ability_kind is not None and not isinstance(ability_kind, str):
        raise ValueError("support ability kind is malformed")
    effect = context.get("effect")
    if effect is not None and not isinstance(effect, dict):
        raise ValueError("support effect is malformed")
    return context


def _validate_eligibility(title: dict) -> None:
    eligibility = _mapping(title.get("eligibility"), "title eligibility")
    required = {"format_legality", "color_identity", "playset", "ownership", "crafting"}
    if not required <= set(eligibility):
        raise ValueError("title eligibility is missing required facts")
    for field in required:
        _mapping(eligibility[field], f"eligibility {field}")

    unresolved = _list(title.get("unresolved_eligibility"), "unresolved eligibility")
    if any(not isinstance(item, str) or not item for item in unresolved):
        raise ValueError("unresolved eligibility contains a malformed dimension")
    status = title.get("eligibility_status")
    expected_status = "eligibility_unknown" if unresolved else "eligible"
    if status != expected_status:
        raise ValueError("eligibility status contradicts unresolved dimensions")

    ownership = eligibility["ownership"]
    owned = ownership.get("owned_copies")
    if ownership.get("status") == "known":
        if type(owned) is not int or owned < 0:
            raise ValueError("known ownership requires nonnegative integer copies")
    elif ownership.get("status") == "unknown":
        if owned is not None:
            raise ValueError("unknown ownership cannot carry a copy count")
    else:
        raise ValueError("ownership status is malformed")

    playset = eligibility["playset"]
    current = playset.get("current_deck_copies")
    if type(current) is not int or current < 0:
        raise ValueError("current deck copies must be a nonnegative integer")
    if playset.get("status") == "capacity_available":
        limit = playset.get("copy_limit")
        remaining = playset.get("remaining_capacity")
        if (
            type(limit) is not int or limit <= 0
            or type(remaining) is not int or remaining <= 0
            or remaining != limit - current
        ):
            raise ValueError("finite returned-candidate capacity is contradictory")
    elif playset.get("status") == "unlimited":
        if playset.get("copy_limit") is not None or playset.get("remaining_capacity") is not None:
            raise ValueError("unlimited copy semantics cannot carry a finite capacity")
    else:
        raise ValueError("playset status is malformed for a returned candidate")


def _validate_support(
    value: Any, source_need: dict, key: tuple[str, str, str], title_id: int,
) -> dict:
    support = _mapping(value, "support context")
    accepted = support.get("acceptable_feature_rule_ids")
    matched = support.get("matched_feature_rule_ids")
    entries = _list(support.get("entries"), "support entries")
    if (
        support.get("matching_semantics") != "any"
        or accepted != source_need["required_feature_rule_ids"]
        or not isinstance(matched, list)
        or not matched
        or any(not isinstance(item, str) or not item for item in matched)
        or len(matched) != len(set(matched))
        or not set(matched) <= set(accepted)
        or not entries
        or support.get("unresolved") != []
    ):
        raise ValueError("support context contradicts source need")
    entry_rule_ids = []
    for entry in entries:
        entry = _mapping(entry, "support entry")
        rule_id = entry.get("rule_id")
        if (
            not isinstance(rule_id, str) or not rule_id
            or rule_id not in accepted
            or entry.get("relationship") != source_need["required_relationship"]
        ):
            raise ValueError("support entry has malformed rule identity")
        feature = _mapping(entry.get("feature_evidence"), "support feature evidence")
        if (
            feature.get("rule_id") != rule_id
            or feature.get("relationship") != entry["relationship"]
            or feature.get("evidence") != entry.get("evidence")
            or feature.get("dependency_context") != entry.get("dependency_context")
        ):
            raise ValueError("support entry contradicts feature evidence")
        _validate_context(entry.get("dependency_context"))
        entry_rule_ids.append(rule_id)
    if sorted(set(entry_rule_ids)) != sorted(matched):
        raise ValueError("matched rule IDs contradict support entries")
    return support


def _comparison_status(cells: list[dict]) -> tuple[str, str | None]:
    if len(cells) < 2:
        return "not_applicable", "insufficient_candidates"
    states = [item["fact_status"] for item in cells]
    if any(state == "unknown" for state in states):
        return "unknown", "one_or_more_facts_unknown"
    if all(state == "not_applicable" for state in states):
        return "not_applicable", "all_facts_not_applicable"
    values = [(item["fact_status"], item.get("value")) for item in cells]
    return ("same", None) if all(item == values[0] for item in values[1:]) else ("different", None)


def _validate_dimensions(value: Any, title_ids: set[int]) -> None:
    dimensions = _list(value, "dimension comparisons")
    seen = set()
    for dimension in dimensions:
        dimension = _mapping(dimension, "dimension comparison")
        dimension_id = _nonempty(dimension.get("dimension_id"), "dimension ID")
        if dimension_id in seen:
            raise ValueError("dimension comparison is duplicated")
        seen.add(dimension_id)
        cells = _list(dimension.get("values"), "dimension values")
        if {item.get("title_id") for item in cells if isinstance(item, dict)} != title_ids:
            raise ValueError("dimension values contradict returned candidates")
        if len(cells) != len(title_ids):
            raise ValueError("dimension values contain duplicate candidates")
        for cell in cells:
            cell = _mapping(cell, "dimension value")
            if cell.get("fact_status") not in {"known", "unknown", "not_applicable"}:
                raise ValueError("dimension fact status is malformed")
            _nonempty(cell.get("provenance_path"), "dimension provenance path")
        expected, reason = _comparison_status(cells)
        if dimension.get("status") != expected or dimension.get("reason") != reason:
            if reason is None and dimension.get("status") == expected and "reason" not in dimension:
                continue
            raise ValueError("dimension comparison status contradicts its values")


def _title_signals(title: dict) -> list[dict]:
    title_id = title["title_id"]
    signals = []
    coverage = title["need_coverage"]
    coverage_evidence = [_fact(_title_path(title_id, "need_coverage"), coverage)]
    if coverage["matched_need_count"] > 1:
        signals.append(_signal("fit.need.multiple_observed_needs.v1", coverage_evidence))
    elif coverage["matched_need_count"] == 1:
        signals.append(_signal("fit.need.single_observed_need.v1", coverage_evidence))

    if len(title["package_ids"]) > 1:
        signals.append(_signal(
            "fit.package.multiple_contributions.v1",
            [_fact(_title_path(title_id, "package_ids"), title["package_ids"])],
        ))

    unresolved = title["unresolved_eligibility"]
    eligibility_path = _title_path(title_id, "unresolved_eligibility")
    signals.append(_signal(
        "fit.eligibility.unresolved_dimensions_present.v1"
        if unresolved else "fit.eligibility.no_unresolved_dimensions.v1",
        [_fact(eligibility_path, unresolved)],
    ))

    ownership = title["eligibility"]["ownership"]
    signals.append(_signal(
        "fit.ownership.known.v1" if ownership["status"] == "known"
        else "fit.ownership.unknown.v1",
        [_fact(_title_path(title_id, "eligibility.ownership"), ownership)],
    ))

    playset = title["eligibility"]["playset"]
    signals.append(_signal(
        "fit.copy.remaining_capacity_positive.v1"
        if playset["status"] == "capacity_available" else "fit.copy.unlimited.v1",
        [_fact(_title_path(title_id, "eligibility.playset"), playset)],
    ))
    return _sort_signals(signals)


def _pool_signals(matrix: dict, key: tuple[str, str, str]) -> list[dict]:
    signals = []
    summary = matrix["source_pool_summary"]
    if summary["truncated"]:
        signals.append(_signal(
            "fit.source.pool_truncated.v1",
            [_fact(_matrix_path(key, "source_pool_summary.truncated"), True)],
        ))
    completeness = matrix["evidence_completeness"]
    if (
        completeness["status"] == "unknown"
        and completeness["reason"]
        == "source_evidence_boundary_not_present_in_candidate_facts_v3"
    ):
        signals.append(_signal(
            "fit.evidence.zone_boundary_unavailable.v1",
            [_fact(_matrix_path(key, "evidence_completeness"), completeness)],
        ))
    return _sort_signals(signals)


def _candidate_signals(
    candidate: dict, title: dict, matrix: dict, key: tuple[str, str, str],
) -> list[dict]:
    title_id = title["title_id"]
    signals = [_signal(
        "fit.need.current_match.v1",
        [
            _fact(_candidate_path(key, title_id, "required_feature_eligibility"),
                  candidate["required_feature_eligibility"]),
            _fact(_candidate_path(key, title_id, "matching_feature_evidence"),
                  candidate["matching_feature_evidence"]),
        ],
    )]

    package_data = matrix["package_comparison"]
    package_ids = set(title["package_ids"])
    shared = set(package_data["shared_by_all_package_ids"])
    exclusive_by_title = {
        item["title_id"]: item["package_ids"]
        for item in package_data["pool_local_exclusive_package_ids_by_title"]
    }
    package_path = _matrix_path(key, "package_comparison")
    if package_ids & shared:
        signals.append(_signal(
            "fit.package.shared_by_all_returned_candidates.v1",
            [_fact(package_path, sorted(package_ids & shared))],
        ))
    if package_ids - shared:
        signals.append(_signal(
            "fit.package.not_shared_by_all_returned_candidates.v1",
            [_fact(package_path, sorted(package_ids - shared))],
        ))
    if exclusive_by_title[title_id]:
        signals.append(_signal(
            "fit.package.pool_local_exclusive_contribution.v1",
            [_fact(package_path, exclusive_by_title[title_id])],
        ))
    package_sets = [frozenset(item["package_ids"])
                    for item in package_data["package_ids_by_title"]]
    if len(package_sets) >= 2 and all(item == package_sets[0] for item in package_sets[1:]):
        signals.append(_signal(
            "fit.package.same_set_across_returned_pool.v1",
            [_fact(package_path, package_data["package_ids_by_title"])],
        ))

    support = candidate["support_context"]
    entries = support["entries"]
    support_path = _candidate_path(key, title_id, "support_context")
    predicates = (
        ("fit.support.unconditional.v1",
         lambda entry: entry["dependency_context"]["availability"] == "unconditional"),
        ("fit.support.prerequisites_present.v1", lambda entry: bool(
            entry["dependency_context"]["prerequisites"]["trigger"] is not None
            or entry["dependency_context"]["prerequisites"]["costs"]
            or entry["dependency_context"]["prerequisites"]["conditions"]
            or entry["dependency_context"]["prerequisites"]["qualifiers"]
        )),
        ("fit.support.conditional.v1",
         lambda entry: entry["dependency_context"]["availability"] == "conditional"),
        ("fit.support.triggered.v1",
         lambda entry: entry["dependency_context"]["ability_kind"] == "triggered"),
        ("fit.support.activated.v1",
         lambda entry: entry["dependency_context"]["ability_kind"] == "activated"),
        ("fit.support.partial.v1", lambda entry: (
            entry["dependency_context"]["availability"] == "partially_reviewed"
            or entry["dependency_context"]["parse_status"] == "partial"
        )),
        ("fit.support.unsupported_remainder_present.v1",
         lambda entry: bool(entry["dependency_context"]["unsupported_remainder"])),
    )
    for signal_id, predicate in predicates:
        matching = [deepcopy(entry) for entry in entries if predicate(entry)]
        if matching:
            signals.append(_signal(signal_id, [_fact(support_path, matching)]))
    if support["matching_semantics"] == "any" and len(support["acceptable_feature_rule_ids"]) > 1:
        signals.append(_signal(
            "fit.support.alternative_rule_paths.v1",
            [_fact(support_path, {
                "matching_semantics": support["matching_semantics"],
                "acceptable_feature_rule_ids": support["acceptable_feature_rule_ids"],
                "matched_feature_rule_ids": support["matched_feature_rule_ids"],
            })],
        ))
    return _sort_signals(signals)


def build_strategic_fit_signals(candidate_comparison: dict) -> dict:
    """Build registry-driven descriptive signals from a complete #6A model."""
    source = _mapping(candidate_comparison, "candidate comparison")
    if source.get("candidate_comparison_model_version") != _COMPARISON_MODEL_VERSION:
        raise ValueError("strategic fit requires Candidate Comparison Model Version 2")
    required_complete = {
        "title_index", "need_matrices", "candidate_title_count", "ordering", "limitations",
    }
    if not required_complete <= set(source):
        raise ValueError("strategic fit requires the complete candidate comparison model")
    _mapping(source.get("ordering"), "comparison ordering")
    _list(source.get("limitations"), "comparison limitations")

    titles: dict[int, dict] = {}
    for raw_title in _list(source.get("title_index"), "title index"):
        title = _mapping(raw_title, "title index entry")
        title_id = title.get("title_id")
        name = title.get("name")
        if type(title_id) is not int or not isinstance(name, str) or not name:
            raise ValueError("title identity is malformed")
        if title_id in titles:
            raise ValueError("title ID is duplicated")
        package_ids = _list(title.get("package_ids"), "title package IDs")
        if (
            any(not isinstance(item, str) or not item for item in package_ids)
            or len(package_ids) != len(set(package_ids))
            or package_ids != sorted(package_ids)
        ):
            raise ValueError("title package IDs are malformed")
        packages = _list(title.get("functional_packages"), "title functional packages")
        if [item.get("package_id") for item in packages if isinstance(item, dict)] != package_ids:
            raise ValueError("title package IDs contradict functional packages")
        coverage = _mapping(title.get("need_coverage"), "title need coverage")
        needs = _list(coverage.get("needs"), "title covered needs")
        if type(coverage.get("matched_need_count")) is not int or coverage["matched_need_count"] != len(needs):
            raise ValueError("title need count contradicts structured needs")
        need_keys = [_need_key(item) for item in needs]
        if len(need_keys) != len(set(need_keys)):
            raise ValueError("title need coverage contains duplicates")
        _validate_eligibility(title)
        titles[title_id] = title
    if type(source.get("candidate_title_count")) is not int or source["candidate_title_count"] != len(titles):
        raise ValueError("candidate title count contradicts title index")

    matrices: list[tuple[tuple[str, str, str], dict]] = []
    seen_needs = set()
    occurrences: dict[int, list[tuple[str, str, str]]] = defaultdict(list)
    for raw_matrix in _list(source.get("need_matrices"), "need matrices"):
        matrix = _mapping(raw_matrix, "need matrix")
        key = _need_key(matrix.get("need_key"))
        if key in seen_needs:
            raise ValueError("need key is duplicated")
        seen_needs.add(key)
        source_need = _validate_source_need(matrix.get("source_need"), key)
        candidates = _list(matrix.get("candidates"), "need candidates")
        if type(matrix.get("candidate_count")) is not int or matrix["candidate_count"] != len(candidates):
            raise ValueError("candidate count contradicts need candidates")
        summary = _mapping(matrix.get("source_pool_summary"), "source pool summary")
        if (
            type(summary.get("returned")) is not int
            or summary["returned"] != len(candidates)
            or type(summary.get("truncated")) is not bool
        ):
            raise ValueError("source pool summary contradicts returned candidates")

        candidate_ids = set()
        for candidate in candidates:
            candidate = _mapping(candidate, "candidate occurrence")
            title_id = candidate.get("title_id")
            if title_id not in titles or title_id in candidate_ids:
                raise ValueError("candidate title reference is absent or duplicated")
            candidate_ids.add(title_id)
            title = titles[title_id]
            if candidate.get("name", title["name"]) != title["name"]:
                raise ValueError("candidate identity contradicts title index")
            required = _mapping(
                candidate.get("required_feature_eligibility"),
                "required feature eligibility",
            )
            if (
                required.get("status") != "matched"
                or required.get("feature_rule_ids") != source_need["required_feature_rule_ids"]
                or required.get("matching_semantics") != "any"
                or required.get("relationship") != source_need["required_relationship"]
            ):
                raise ValueError("required feature eligibility contradicts source need")
            evidence = _list(candidate.get("matching_feature_evidence"), "matching feature evidence")
            if not evidence:
                raise ValueError("current need match requires exact evidence")
            support = _validate_support(candidate.get("support_context"), source_need, key, title_id)
            expected_features = [entry["feature_evidence"] for entry in support["entries"]]
            if evidence != expected_features:
                raise ValueError("matching evidence contradicts support context")
            occurrences[title_id].append(key)

        package_data = _mapping(matrix.get("package_comparison"), "package comparison")
        by_title = _list(package_data.get("package_ids_by_title"), "package IDs by title")
        if len(by_title) != len(candidate_ids) or {item.get("title_id") for item in by_title if isinstance(item, dict)} != candidate_ids:
            raise ValueError("package comparison contradicts returned candidates")
        package_sets = {}
        for item in by_title:
            item = _mapping(item, "package IDs by title entry")
            package_ids = _list(item.get("package_ids"), "pool package IDs")
            if package_ids != titles[item["title_id"]]["package_ids"]:
                raise ValueError("package comparison contradicts title package facts")
            package_sets[item["title_id"]] = set(package_ids)
        expected_shared = sorted(set.intersection(*package_sets.values())) if package_sets else []
        if package_data.get("shared_by_all_package_ids") != expected_shared:
            raise ValueError("shared package facts contradict title package facts")
        counts = Counter(item for values in package_sets.values() for item in values)
        exclusive = _list(
            package_data.get("pool_local_exclusive_package_ids_by_title"),
            "pool-local exclusive packages",
        )
        if len(exclusive) != len(candidate_ids) or {item.get("title_id") for item in exclusive if isinstance(item, dict)} != candidate_ids:
            raise ValueError("pool-local exclusive facts contradict returned candidates")
        for item in exclusive:
            expected = sorted(value for value in package_sets[item["title_id"]] if counts[value] == 1)
            if item.get("package_ids") != expected:
                raise ValueError("pool-local exclusive packages contradict title package facts")

        _validate_dimensions(matrix.get("dimension_comparisons"), candidate_ids)
        completeness = _mapping(matrix.get("evidence_completeness"), "evidence completeness")
        if (
            completeness.get("status") != "unknown"
            or completeness.get("reason")
            != "source_evidence_boundary_not_present_in_candidate_facts_v3"
            or not isinstance(completeness.get("feature_level_unsupported_remainders"), list)
        ):
            raise ValueError("evidence completeness is malformed")
        matrices.append((key, matrix))

    for title_id, title in titles.items():
        expected = sorted(occurrences[title_id], key=_need_sort_key)
        actual = sorted((_need_key(item) for item in title["need_coverage"]["needs"]), key=_need_sort_key)
        if actual != expected or title["need_coverage"]["matched_need_count"] != len(expected):
            raise ValueError("title need coverage contradicts candidate occurrences")

    sorted_titles = sorted(titles.values(), key=_title_sort_key)
    title_output = [
        {
            "title_id": title["title_id"],
            "name": title["name"],
            "signals": _title_signals(title),
        }
        for title in sorted_titles
    ]
    title_signal_ids = {
        item["title_id"]: [signal["signal_id"] for signal in item["signals"]]
        for item in title_output
    }

    need_output = []
    for key, matrix in sorted(matrices, key=lambda item: _need_sort_key(item[0])):
        pool_signals = _pool_signals(matrix, key)
        pool_signal_ids = [item["signal_id"] for item in pool_signals]
        candidate_rows = []
        for candidate in sorted(
            matrix["candidates"], key=lambda item: _title_sort_key(titles[item["title_id"]]),
        ):
            title = titles[candidate["title_id"]]
            candidate_rows.append({
                "title_id": title["title_id"],
                "name": title["name"],
                "signals": _candidate_signals(candidate, title, matrix, key),
                "applicable_title_signal_ids": deepcopy(title_signal_ids[title["title_id"]]),
                "applicable_pool_signal_ids": deepcopy(pool_signal_ids),
            })
        need_output.append({
            "need_key": _need_key_object(key),
            "source_need": deepcopy(matrix["source_need"]),
            "candidate_count": len(candidate_rows),
            "pool_signals": pool_signals,
            "candidates": candidate_rows,
        })

    return {
        "strategic_fit_model_version": STRATEGIC_FIT_MODEL_VERSION,
        "signal_registry_version": STRATEGIC_SIGNAL_REGISTRY_VERSION,
        "source_candidate_comparison_model_version": _COMPARISON_MODEL_VERSION,
        "title_signal_index": title_output,
        "need_signal_sets": need_output,
        "ordering": {
            "titles": "casefolded_name_then_title_id",
            "needs": "zone_then_finding_id_then_dependency_id",
            "candidates": "casefolded_name_then_title_id",
            "signals": "signal_registry_sequence",
        },
        "limitations": [
            "Signals describe only facts established by Candidate Comparison Model Version 2.",
            "Signal absence means only that the exact registry predicate was not established.",
            "Pool-relative signals apply only to the returned candidates in their source need.",
            "Signals are not combined into an aggregate evaluation.",
        ],
    }
