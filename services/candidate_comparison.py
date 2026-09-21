"""Deterministic comparison facts derived only from Candidate Facts Version 2.

The normalized model stores title facts once and need-specific evidence per
candidate occurrence. Pairwise projections are generated only when requested.
No candidates, eligibility facts, needs, or card semantics are rediscovered.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from typing import Any, Callable


CANDIDATE_COMPARISON_MODEL_VERSION = "1"
_CANDIDATE_FACTS_VERSION = "2"
_CANDIDATE_VERSION = "2"
_PACKAGE_VERSION = "1"
_ZONE_ORDER = {"main": 0, "sideboard": 1, "commander": 2}
_FACT_STATES = frozenset({"known", "unknown", "not_applicable"})
_COMPARISON_STATES = frozenset({"same", "different", "unknown", "not_applicable"})


def _mapping(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return value


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _need_key(source: dict) -> tuple[str, str, str]:
    zone = source.get("zone")
    if zone not in _ZONE_ORDER:
        raise ValueError("source need has an unsupported zone")
    return (
        zone,
        _nonempty_string(source.get("finding_id"), "finding ID"),
        _nonempty_string(source.get("dependency_id"), "dependency ID"),
    )


def _need_key_object(key: tuple[str, str, str]) -> dict:
    return {"zone": key[0], "finding_id": key[1], "dependency_id": key[2]}


def _need_sort_key(key: tuple[str, str, str]) -> tuple[int, str, str]:
    return (_ZONE_ORDER[key[0]], key[1], key[2])


def _title_sort_key(item: dict) -> tuple[str, int]:
    return (item["name"].casefold(), item["title_id"])


def _feature_sort_key(feature: dict) -> tuple[str, str, str]:
    return (
        str(feature.get("rule_id", "")),
        str(feature.get("relationship", "")),
        str(feature.get("evidence", "")),
    )


def _validate_source_need(value: Any) -> dict:
    source = _mapping(value, "source need")
    _need_key(source)
    required = (
        "dependency_label", "missing_side_name", "missing_side",
        "required_feature_rule_ids", "feature_matching_semantics",
        "required_relationship",
    )
    if any(key not in source for key in required):
        raise ValueError("source need is missing required provenance")
    if source["missing_side_name"] != "enabler":
        raise ValueError("comparison requires a missing enabler side")
    rule_ids = source["required_feature_rule_ids"]
    if (
        not isinstance(rule_ids, list)
        or not rule_ids
        or any(not isinstance(rule_id, str) or not rule_id for rule_id in rule_ids)
        or len(rule_ids) != len(set(rule_ids))
        or source["feature_matching_semantics"] != "any"
        or not isinstance(source["required_relationship"], str)
        or not source["required_relationship"]
    ):
        raise ValueError("source need has malformed feature requirements")
    missing = _mapping(source["missing_side"], "missing side")
    if (
        missing.get("acceptable_feature_rule_ids") != rule_ids
        or missing.get("feature_rule_ids") != rule_ids
        or missing.get("matching_semantics") != "any"
        or missing.get("relationship") != source["required_relationship"]
    ):
        raise ValueError("source need feature requirements are contradictory")
    return source


def _validate_dependency_context(value: Any) -> dict:
    context = _mapping(value, "dependency context")
    prerequisites = _mapping(context.get("prerequisites"), "dependency prerequisites")
    trigger = prerequisites.get("trigger")
    if trigger is not None and not isinstance(trigger, dict):
        raise ValueError("dependency trigger must be an object or null")
    for key in ("costs", "conditions", "qualifiers"):
        _list(prerequisites.get(key), f"dependency prerequisite {key}")
    if context.get("availability") not in {
        "unconditional", "conditional", "partially_reviewed",
    }:
        raise ValueError("dependency availability is malformed")
    if context.get("parse_status") not in {"supported", "partial"}:
        raise ValueError("dependency parse status is malformed")
    _list(context.get("unsupported_remainder"), "unsupported remainder")
    if context.get("effect") is not None and not isinstance(context.get("effect"), dict):
        raise ValueError("dependency effect must be an object or null")
    ability_kind = context.get("ability_kind")
    if ability_kind is not None and not isinstance(ability_kind, str):
        raise ValueError("dependency ability kind is malformed")
    return context


def _validate_matching_evidence(value: Any, source: dict, reviewed: list) -> list[dict]:
    evidence = _list(value, "matching feature evidence")
    if not evidence:
        raise ValueError("candidate requires matching feature evidence")
    required_ids = set(source["required_feature_rule_ids"])
    relationship = source["required_relationship"]
    result = []
    for raw_feature in evidence:
        feature = _mapping(raw_feature, "matching feature")
        if (
            feature.get("rule_id") not in required_ids
            or feature.get("relationship") != relationship
            or feature not in reviewed
        ):
            raise ValueError("matching evidence contradicts source requirements")
        _validate_dependency_context(feature.get("dependency_context"))
        result.append(deepcopy(feature))
    return sorted(result, key=_feature_sort_key)


def _validate_packages(value: Any) -> list[dict]:
    packages = _list(value, "functional packages")
    seen = set()
    result = []
    for raw_package in packages:
        package = _mapping(raw_package, "functional package")
        package_id = _nonempty_string(package.get("package_id"), "package ID")
        if package_id in seen:
            raise ValueError("functional package is duplicated")
        seen.add(package_id)
        _list(package.get("evidence"), "functional package evidence")
        result.append(deepcopy(package))
    return sorted(result, key=lambda item: item["package_id"])


def _validate_printings(value: Any) -> list[dict]:
    printings = _list(value, "known printings")
    seen = set()
    result = []
    for raw_printing in printings:
        printing = _mapping(raw_printing, "printing fact")
        arena_id = printing.get("arena_id")
        if type(arena_id) is not int or arena_id in seen:
            raise ValueError("printing facts require unique integer Arena IDs")
        seen.add(arena_id)
        result.append(deepcopy(printing))
    return sorted(result, key=lambda item: item["arena_id"])


def _validate_eligibility(value: Any, *, per_need: bool) -> dict:
    eligibility = _mapping(value, "eligibility")
    required = {
        "format_legality", "color_identity", "playset", "ownership", "crafting",
    }
    if per_need:
        required.add("required_feature")
    if not required <= set(eligibility):
        raise ValueError("eligibility is missing required facts")
    for key in required:
        _mapping(eligibility[key], f"eligibility {key}")
    return eligibility


def _invariant_eligibility(per_need: dict) -> dict:
    return {
        key: deepcopy(per_need[key])
        for key in ("format_legality", "color_identity", "playset", "ownership", "crafting")
    }


def _canonical_fact_state(value: Any) -> str:
    return "unknown" if value is None or value == "" else "known"


def _eligibility_fact_state(value: dict) -> str:
    status = value.get("status")
    if status == "not_applicable":
        return "not_applicable"
    if status in {"unknown", "not_evaluated"} or status is None:
        return "unknown"
    return "known"


def _cell(title_id: int, state: str, value: Any, path: str) -> dict:
    if state not in _FACT_STATES:
        raise ValueError("unsupported fact state")
    return {
        "title_id": title_id,
        "fact_status": state,
        "value": deepcopy(value),
        "provenance_path": path,
    }


def _comparison_status(cells: list[dict]) -> tuple[str, str | None]:
    if len(cells) < 2:
        return "not_applicable", "insufficient_candidates"
    states = [cell["fact_status"] for cell in cells]
    if any(state == "unknown" for state in states):
        return "unknown", "one_or_more_facts_unknown"
    if all(state == "not_applicable" for state in states):
        return "not_applicable", "all_facts_not_applicable"
    values = [(cell["fact_status"], cell["value"]) for cell in cells]
    return ("same", None) if all(value == values[0] for value in values[1:]) else ("different", None)


def _dimension(dimension_id: str, cells: list[dict]) -> dict:
    status, reason = _comparison_status(cells)
    if status not in _COMPARISON_STATES:
        raise ValueError("unsupported comparison status")
    result = {"dimension_id": dimension_id, "status": status, "values": cells}
    if reason is not None:
        result["reason"] = reason
    return result


def _canonical_extractor(field: str) -> Callable[[dict, dict], dict]:
    def extract(title: dict, occurrence: dict) -> dict:
        value = title["canonical_facts"].get(field)
        return _cell(
            title["title_id"], _canonical_fact_state(value), value,
            f"candidate_facts_by_title.canonical_facts.{field}",
        )
    return extract


def _eligibility_extractor(field: str) -> Callable[[dict, dict], dict]:
    def extract(title: dict, occurrence: dict) -> dict:
        value = title["eligibility"][field]
        return _cell(
            title["title_id"], _eligibility_fact_state(value), value,
            f"candidate_facts_by_title.eligibility.{field}",
        )
    return extract


def _overall_eligibility(title: dict, occurrence: dict) -> dict:
    value = title["eligibility_status"]
    state = "unknown" if value == "eligibility_unknown" else "known"
    return _cell(title["title_id"], state, value, "candidate_facts_by_title.eligibility_status")


def _printings(title: dict, occurrence: dict) -> dict:
    value = title["known_printings"]
    state = "known" if value else "unknown"
    return _cell(title["title_id"], state, value, "candidate_facts_by_title.known_printings")


def _unresolved(title: dict, occurrence: dict) -> dict:
    return _cell(
        title["title_id"], "known", title["unresolved_eligibility"],
        "candidate_facts_by_title.unresolved_eligibility",
    )


def _support_values(field: str) -> Callable[[dict, dict], dict]:
    def extract(title: dict, occurrence: dict) -> dict:
        entries = occurrence["support_context"]["entries"]
        if field == "prerequisites":
            values = [entry["dependency_context"]["prerequisites"] for entry in entries]
        elif field == "alternative_rule_ids":
            values = occurrence["support_context"]["acceptable_feature_rule_ids"]
        elif field == "unsupported_remainder":
            values = [entry["dependency_context"]["unsupported_remainder"] for entry in entries]
        else:
            values = [entry["dependency_context"].get(field) for entry in entries]
        return _cell(
            title["title_id"], "known", values,
            f"per_need.matching_feature_evidence.dependency_context.{field}",
        )
    return extract


_DIMENSIONS: tuple[tuple[str, Callable[[dict, dict], dict]], ...] = (
    ("mana.cost", _canonical_extractor("mana_cost")),
    ("mana.value", _canonical_extractor("mana_value")),
    ("card.colors", _canonical_extractor("colors")),
    ("card.color_identity", _canonical_extractor("color_identity")),
    ("card.types", _canonical_extractor("types")),
    ("card.subtypes", _canonical_extractor("subtypes")),
    ("card.power", _canonical_extractor("power")),
    ("card.toughness", _canonical_extractor("toughness")),
    ("printing.facts", _printings),
    ("eligibility.overall_status", _overall_eligibility),
    ("eligibility.format_legality", _eligibility_extractor("format_legality")),
    ("eligibility.color_identity", _eligibility_extractor("color_identity")),
    ("eligibility.playset", _eligibility_extractor("playset")),
    ("eligibility.ownership", _eligibility_extractor("ownership")),
    ("eligibility.crafting", _eligibility_extractor("crafting")),
    ("eligibility.unresolved_dimensions", _unresolved),
    ("support.availability", _support_values("availability")),
    ("support.ability_kind", _support_values("ability_kind")),
    ("support.parse_status", _support_values("parse_status")),
    ("support.prerequisites", _support_values("prerequisites")),
    ("support.alternative_rule_ids", _support_values("alternative_rule_ids")),
    ("evidence.unsupported_remainder", _support_values("unsupported_remainder")),
)


def _support_context(evidence: list[dict], source: dict) -> dict:
    entries = []
    for feature in evidence:
        context = _validate_dependency_context(feature["dependency_context"])
        entries.append({
            "rule_id": feature["rule_id"],
            "relationship": feature["relationship"],
            "evidence": feature.get("evidence"),
            "feature_evidence": deepcopy(feature),
            "dependency_context": deepcopy(context),
        })
    return {
        "matching_semantics": source["feature_matching_semantics"],
        "acceptable_feature_rule_ids": deepcopy(source["required_feature_rule_ids"]),
        "matched_feature_rule_ids": sorted({entry["rule_id"] for entry in entries}),
        "entries": entries,
        "unresolved": [],
    }


def _package_comparison(candidates: list[dict], titles: dict[int, dict]) -> dict:
    by_title = []
    counts = Counter()
    for candidate in candidates:
        title_id = candidate["title_id"]
        package_ids = [item["package_id"] for item in titles[title_id]["functional_packages"]]
        by_title.append({"title_id": title_id, "package_ids": package_ids})
        counts.update(package_ids)
    shared = sorted(package_id for package_id, count in counts.items()
                    if count == len(candidates) and candidates)
    exclusive = [
        {"title_id": row["title_id"],
         "package_ids": sorted(package_id for package_id in row["package_ids"]
                               if counts[package_id] == 1)}
        for row in by_title
    ]
    return {
        "shared_by_all_package_ids": shared,
        "package_ids_by_title": by_title,
        "pool_local_exclusive_package_ids_by_title": exclusive,
    }


def build_candidate_comparisons(candidate_facts: dict) -> dict:
    """Build normalized, non-evaluative comparison facts without database access."""
    source = _mapping(candidate_facts, "candidate facts")
    if source.get("candidate_facts_model_version") != _CANDIDATE_FACTS_VERSION:
        raise ValueError("candidate comparisons require Candidate Facts Model Version 2")
    if source.get("source_candidate_model_version") != _CANDIDATE_VERSION:
        raise ValueError("candidate comparisons require Candidate Model Version 2")
    if source.get("functional_package_model_version") != _PACKAGE_VERSION:
        raise ValueError("candidate comparisons require Functional Package Model Version 1")

    raw_titles = _list(source.get("candidate_facts_by_title"), "candidate title facts")
    if type(source.get("candidate_title_count")) is not int:
        raise ValueError("candidate title count must be an integer")
    titles: dict[int, dict] = {}
    for raw_title in raw_titles:
        title = _mapping(raw_title, "candidate title")
        title_id = title.get("title_id")
        name = title.get("name")
        if type(title_id) is not int or not isinstance(name, str) or not name:
            raise ValueError("candidate title identity is malformed")
        if title_id in titles:
            raise ValueError("candidate title ID is duplicated")
        canonical = _mapping(title.get("canonical_facts"), "canonical facts")
        if canonical.get("title_id") != title_id or canonical.get("name") != name:
            raise ValueError("candidate title contradicts canonical facts")
        reviewed = _list(title.get("reviewed_features"), "reviewed features")
        packages = _validate_packages(title.get("functional_packages"))
        printings = _validate_printings(title.get("known_printings"))
        eligibility = _validate_eligibility(title.get("eligibility"), per_need=False)
        unresolved = _list(title.get("unresolved_eligibility"), "unresolved eligibility")
        if title.get("eligibility_status") not in {"eligible", "eligibility_unknown"}:
            raise ValueError("candidate eligibility status is malformed")
        titles[title_id] = {
            "title_id": title_id,
            "name": name,
            "canonical_facts": deepcopy(canonical),
            "reviewed_features": deepcopy(reviewed),
            "functional_packages": packages,
            "package_ids": [item["package_id"] for item in packages],
            "known_printings": printings,
            "eligibility_status": title["eligibility_status"],
            "eligibility": deepcopy(eligibility),
            "unresolved_eligibility": deepcopy(unresolved),
        }
    if source["candidate_title_count"] != len(titles):
        raise ValueError("candidate title count contradicts title facts")

    raw_pools = _list(source.get("per_need"), "per-need candidate facts")
    pools: list[tuple[tuple[str, str, str], dict]] = []
    seen_needs = set()
    occurrences: dict[int, list[dict]] = defaultdict(list)
    expected_matches: dict[int, list[dict]] = defaultdict(list)
    for raw_pool in raw_pools:
        pool = _mapping(raw_pool, "per-need pool")
        source_need = _validate_source_need(pool.get("source_need"))
        key = _need_key(source_need)
        if key in seen_needs:
            raise ValueError("source need is duplicated")
        seen_needs.add(key)
        summary = _mapping(pool.get("source_pool_summary"), "source pool summary")
        candidates = _list(pool.get("candidates"), "per-need candidates")
        if type(summary.get("returned")) is not int or summary["returned"] != len(candidates):
            raise ValueError("source pool summary contradicts returned candidates")
        if type(summary.get("truncated")) is not bool:
            raise ValueError("source pool truncation fact is malformed")

        normalized_candidates = []
        seen_titles = set()
        for raw_candidate in candidates:
            candidate = _mapping(raw_candidate, "per-need candidate")
            title_id = candidate.get("title_id")
            if title_id not in titles:
                raise ValueError("candidate references a title absent from the title index")
            if title_id in seen_titles:
                raise ValueError("candidate title is duplicated within one need pool")
            seen_titles.add(title_id)
            title = titles[title_id]
            if candidate.get("name") != title["name"]:
                raise ValueError("candidate name contradicts title facts")
            if candidate.get("canonical_facts") != title["canonical_facts"]:
                raise ValueError("candidate canonical facts contradict title facts")
            if candidate.get("reviewed_features") != title["reviewed_features"]:
                raise ValueError("candidate reviewed features contradict title facts")
            if _validate_packages(candidate.get("functional_packages")) != title["functional_packages"]:
                raise ValueError("candidate package facts contradict title facts")
            if _validate_printings(candidate.get("known_printings")) != title["known_printings"]:
                raise ValueError("candidate printing facts contradict title facts")
            if candidate.get("eligibility_status") != title["eligibility_status"]:
                raise ValueError("candidate eligibility status contradicts title facts")
            candidate_eligibility = _validate_eligibility(candidate.get("eligibility"), per_need=True)
            if _invariant_eligibility(candidate_eligibility) != title["eligibility"]:
                raise ValueError("candidate eligibility contradicts title facts")
            if candidate.get("unresolved_eligibility") != title["unresolved_eligibility"]:
                raise ValueError("candidate unresolved eligibility contradicts title facts")
            if candidate.get("source_need") != source_need:
                raise ValueError("candidate source need contradicts its pool")
            required_feature = candidate_eligibility["required_feature"]
            if (
                required_feature.get("status") != "matched"
                or required_feature.get("feature_rule_ids") != source_need["required_feature_rule_ids"]
                or required_feature.get("matching_semantics") != "any"
                or required_feature.get("relationship") != source_need["required_relationship"]
            ):
                raise ValueError("required-feature eligibility contradicts source need")
            evidence = _validate_matching_evidence(
                candidate.get("matching_feature_evidence"), source_need,
                title["reviewed_features"],
            )
            occurrence = {
                "title_id": title_id,
                "matching_feature_evidence": evidence,
                "required_feature_eligibility": deepcopy(required_feature),
                "support_context": _support_context(evidence, source_need),
            }
            normalized_candidates.append(occurrence)
            match = {
                "source_need": deepcopy(source_need),
                "matching_feature_evidence": deepcopy(evidence),
            }
            expected_matches[title_id].append(match)
            occurrences[title_id].append(_need_key_object(key))

        normalized_candidates.sort(key=lambda item: _title_sort_key(titles[item["title_id"]]))
        pools.append((key, {
            "need_key": _need_key_object(key),
            "source_need": deepcopy(source_need),
            "source_pool_summary": deepcopy(summary),
            "candidate_count": len(normalized_candidates),
            "candidates": normalized_candidates,
        }))

    # Validate #5E's aggregate provenance against the normalized per-need records.
    raw_title_by_id = {item["title_id"]: item for item in raw_titles}
    for title_id, title in titles.items():
        raw_title = raw_title_by_id[title_id]
        actual_matches = _list(raw_title.get("per_need_matches"), "title per-need matches")
        sort_match = lambda item: _need_sort_key(_need_key(item["source_need"]))
        normalized_actual_matches = []
        for raw_match in actual_matches:
            match = _mapping(raw_match, "title per-need match")
            match_source = _validate_source_need(match.get("source_need"))
            normalized_actual_matches.append({
                "source_need": deepcopy(match_source),
                "matching_feature_evidence": _validate_matching_evidence(
                    match.get("matching_feature_evidence"), match_source,
                    title["reviewed_features"],
                ),
            })
        if (
            sorted(normalized_actual_matches, key=sort_match)
            != sorted(expected_matches[title_id], key=sort_match)
        ):
            raise ValueError("title per-need provenance contradicts need pools")
        if sorted(set(raw_title.get("matched_need_ids") or [])) != sorted({
            match["source_need"]["finding_id"] for match in expected_matches[title_id]
        }):
            raise ValueError("matched need IDs contradict per-need provenance")
        if sorted(set(raw_title.get("matched_dependency_ids") or [])) != sorted({
            match["source_need"]["dependency_id"] for match in expected_matches[title_id]
        }):
            raise ValueError("matched dependency IDs contradict per-need provenance")

    pools.sort(key=lambda item: _need_sort_key(item[0]))
    need_matrices = []
    for key, pool in pools:
        candidates = pool["candidates"]
        dimension_rows = []
        for dimension_id, extractor in _DIMENSIONS:
            cells = [
                extractor(titles[candidate["title_id"]], candidate)
                for candidate in candidates
            ]
            dimension_rows.append(_dimension(dimension_id, cells))
        unsupported = [
            {
                "title_id": candidate["title_id"],
                "rule_id": entry["rule_id"],
                "unsupported_remainder": deepcopy(
                    entry["dependency_context"]["unsupported_remainder"]
                ),
            }
            for candidate in candidates
            for entry in candidate["support_context"]["entries"]
            if entry["dependency_context"]["unsupported_remainder"]
        ]
        need_matrices.append({
            **pool,
            "dimension_comparisons": dimension_rows,
            "package_comparison": _package_comparison(candidates, titles),
            "evidence_completeness": {
                "status": "unknown",
                "reason": "source_evidence_boundary_not_present_in_candidate_facts_v2",
                "feature_level_unsupported_remainders": unsupported,
            },
        })

    title_index = []
    for title in sorted(titles.values(), key=_title_sort_key):
        need_rows = sorted(
            occurrences[title["title_id"]],
            key=lambda item: _need_sort_key((
                item["zone"], item["finding_id"], item["dependency_id"],
            )),
        )
        title_index.append({
            **deepcopy(title),
            "need_coverage": {
                "matched_need_count": len(need_rows),
                "needs": need_rows,
            },
        })

    return {
        "candidate_comparison_model_version": CANDIDATE_COMPARISON_MODEL_VERSION,
        "source_candidate_facts_model_version": _CANDIDATE_FACTS_VERSION,
        "source_candidate_model_version": _CANDIDATE_VERSION,
        "functional_package_model_version": _PACKAGE_VERSION,
        "source_context": deepcopy(_mapping(source.get("source_context"), "source context")),
        "candidate_title_count": len(title_index),
        "title_index": title_index,
        "need_matrices": need_matrices,
        "ordering": {
            "needs": "zone_then_finding_id_then_dependency_id",
            "titles": "casefolded_name_then_title_id_non_ranking",
            "packages": "package_id",
            "printings": "arena_id",
            "dimensions": "fixed_registry_order",
        },
        "limitations": [
            "Comparisons describe returned Candidate Facts Version 2 records only.",
            "Pool-local package exclusivity does not establish global uniqueness.",
            "Unknown and not-applicable facts remain distinct.",
            "Support context describes reviewed prerequisites without estimating occurrence.",
        ],
    }


def _coerce_need_key(value: Any) -> tuple[str, str, str]:
    if isinstance(value, dict):
        return _need_key(value)
    if (
        isinstance(value, (tuple, list))
        and len(value) == 3
        and value[0] in _ZONE_ORDER
        and all(isinstance(item, str) and item for item in value)
    ):
        return tuple(value)
    raise ValueError("need key must be (zone, finding_id, dependency_id)")


def compare_candidate_pair(
    comparison: dict, need_key: Any, title_a: int, title_b: int,
) -> dict:
    """Project exactly one requested pair from one normalized need matrix."""
    model = _mapping(comparison, "candidate comparison")
    if model.get("candidate_comparison_model_version") != CANDIDATE_COMPARISON_MODEL_VERSION:
        raise ValueError("unsupported candidate comparison model version")
    key = _coerce_need_key(need_key)
    matches = [
        matrix for matrix in _list(model.get("need_matrices"), "need matrices")
        if _coerce_need_key(matrix.get("need_key")) == key
    ]
    if len(matches) != 1:
        raise ValueError("requested need is not uniquely present")
    if type(title_a) is not int or type(title_b) is not int or title_a == title_b:
        raise ValueError("pair comparison requires two distinct integer title IDs")
    matrix = matches[0]
    occurrences = {item["title_id"]: item for item in matrix["candidates"]}
    if title_a not in occurrences or title_b not in occurrences:
        raise ValueError("both candidates must belong to the requested need")
    titles = {item["title_id"]: item for item in model["title_index"]}
    if title_a not in titles or title_b not in titles:
        raise ValueError("candidate title is absent from the title index")

    dimensions = []
    for row in matrix["dimension_comparisons"]:
        cells_by_title = {cell["title_id"]: cell for cell in row["values"]}
        dimensions.append(_dimension(
            row["dimension_id"],
            [deepcopy(cells_by_title[title_a]), deepcopy(cells_by_title[title_b])],
        ))
    packages_a = set(titles[title_a]["package_ids"])
    packages_b = set(titles[title_b]["package_ids"])
    return {
        "candidate_comparison_model_version": CANDIDATE_COMPARISON_MODEL_VERSION,
        "need_key": _need_key_object(key),
        "candidate_a": {
            "title_id": title_a,
            "name": titles[title_a]["name"],
            "support_context": deepcopy(occurrences[title_a]["support_context"]),
        },
        "candidate_b": {
            "title_id": title_b,
            "name": titles[title_b]["name"],
            "support_context": deepcopy(occurrences[title_b]["support_context"]),
        },
        "dimension_comparisons": dimensions,
        "package_comparison": {
            "shared_package_ids": sorted(packages_a & packages_b),
            "candidate_a_only_package_ids": sorted(packages_a - packages_b),
            "candidate_b_only_package_ids": sorted(packages_b - packages_a),
        },
        "ordering": "requested_pair_only",
        "limitations": [
            "This projection contains only the explicitly requested pair.",
            "Package differences are factual within this pair.",
        ],
    }
