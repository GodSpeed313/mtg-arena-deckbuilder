"""Deterministic fact aggregation for returned candidate-pool titles.

This layer describes established canonical, reviewed-feature, package, and
eligibility facts.  It does not search for candidates, reinterpret eligibility,
compare cards, score them, or recommend deck changes.
"""
from __future__ import annotations

from copy import deepcopy
import sqlite3

from mtgadb.query import CardQueryEngine
from services.candidates import CANDIDATE_MODEL_VERSION
from services.intelligence import classify_card
from services.packages import PACKAGE_MODEL_VERSION


CANDIDATE_FACTS_MODEL_VERSION = "1"

_ELIGIBILITY_STATUSES = frozenset({"eligible", "eligibility_unknown"})


def _require_mapping(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _source_need(value) -> dict:
    source = _require_mapping(value, "source need")
    required = (
        "zone",
        "finding_id",
        "dependency_id",
        "dependency_label",
        "missing_side_name",
        "missing_side",
        "required_feature_rule_ids",
        "required_relationship",
    )
    if any(key not in source for key in required):
        raise ValueError("source need is missing required provenance")
    if source["missing_side_name"] != "enabler":
        raise ValueError("candidate facts require a missing enabler side")
    if source["zone"] not in {"main", "sideboard", "commander"}:
        raise ValueError("source need has an unsupported zone")
    rule_ids = source["required_feature_rule_ids"]
    if (
        not isinstance(rule_ids, list)
        or not rule_ids
        or any(not isinstance(rule_id, str) or not rule_id for rule_id in rule_ids)
        or not isinstance(source["required_relationship"], str)
        or not source["required_relationship"]
    ):
        raise ValueError("source need has malformed feature requirements")
    missing = _require_mapping(source["missing_side"], "missing side")
    if (
        missing.get("feature_rule_ids") != source["required_feature_rule_ids"]
        or missing.get("relationship") != source["required_relationship"]
    ):
        raise ValueError("source need feature requirements are contradictory")
    return source


def _canonical_facts(card) -> dict:
    return {
        "title_id": card.title_id,
        "name": card.name,
        "mana_cost": card.mana_cost,
        "mana_value": card.cmc,
        "types": card.types,
        "subtypes": card.subtypes,
        "colors": card.colors,
        "color_identity": card.color_identity,
        "power": card.power,
        "toughness": card.toughness,
    }


def _candidate_invariants(candidate: dict) -> dict:
    eligibility = candidate["eligibility"]
    return {
        "name": candidate["name"],
        "known_printings": candidate["known_printings"],
        "eligibility_status": candidate["eligibility_status"],
        "eligibility": {
            key: eligibility[key]
            for key in (
                "format_legality",
                "color_identity",
                "playset",
                "ownership",
                "crafting",
            )
        },
        "unresolved_eligibility": candidate["unresolved_eligibility"],
    }


def _validate_candidate(
    candidate, source: dict, card, classification: dict,
) -> dict:
    item = _require_mapping(candidate, "candidate")
    required = (
        "title_id",
        "name",
        "matching_feature_evidence",
        "source_need",
        "known_printings",
        "eligibility_status",
        "eligibility",
        "unresolved_eligibility",
    )
    if any(key not in item for key in required):
        raise ValueError("candidate is missing required facts")
    if item["source_need"] != source:
        raise ValueError("candidate source need contradicts its pool")
    if item["title_id"] != card.title_id or item["name"] != card.name:
        raise ValueError("candidate identity contradicts canonical data")
    if item["eligibility_status"] not in _ELIGIBILITY_STATUSES:
        raise ValueError("unsupported candidate eligibility status")
    if not isinstance(item["known_printings"], list):
        raise ValueError("candidate printing facts must be a list")
    eligibility = _require_mapping(item["eligibility"], "candidate eligibility")
    if any(key not in eligibility for key in (
        "required_feature",
        "format_legality",
        "color_identity",
        "playset",
        "ownership",
        "crafting",
    )):
        raise ValueError("candidate eligibility is missing required facts")
    required_feature = _require_mapping(
        eligibility["required_feature"], "required-feature eligibility"
    )
    if (
        required_feature.get("status") != "matched"
        or required_feature.get("feature_rule_ids")
        != source["required_feature_rule_ids"]
        or required_feature.get("relationship") != source["required_relationship"]
    ):
        raise ValueError("candidate eligibility contradicts source requirements")
    if not isinstance(item["unresolved_eligibility"], list):
        raise ValueError("unresolved eligibility must be a list")

    evidence = item["matching_feature_evidence"]
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("candidate requires exact need-match evidence")
    required_ids = set(source["required_feature_rule_ids"])
    relationship = source["required_relationship"]
    reviewed = classification["features"]
    if any(
        not isinstance(feature, dict)
        or feature.get("rule_id") not in required_ids
        or feature.get("relationship") != relationship
        or feature not in reviewed
        for feature in evidence
    ):
        raise ValueError("candidate need-match evidence is not reviewed classifier evidence")
    return item


def derive_candidate_facts(
    candidate_pools: dict, con: sqlite3.Connection,
) -> dict:
    """Enrich returned Version 1 candidates without rediscovery or mutation."""
    source_output = _require_mapping(candidate_pools, "candidate pool")
    if source_output.get("candidate_model_version") != CANDIDATE_MODEL_VERSION:
        raise ValueError("candidate facts require candidate model version 1")
    if any(key not in source_output for key in (
        "trigger_finding_type",
        "format_context",
        "color_identity_context",
        "ownership_context",
        "ignored_non_trigger_findings",
        "pools",
        "ordering",
        "limitations",
    )):
        raise ValueError("candidate pool is missing Version 1 context")
    if source_output.get("trigger_finding_type") != "support_need":
        raise ValueError("unsupported candidate-pool trigger semantics")
    pools = source_output.get("pools")
    if not isinstance(pools, list):
        raise ValueError("candidate pools must be a list")

    title_ids = set()
    for pool in pools:
        pool = _require_mapping(pool, "candidate pool entry")
        if not isinstance(pool.get("candidates"), list):
            raise ValueError("candidate pool entry requires a candidate list")
        summary = _require_mapping(pool.get("summary"), "candidate pool summary")
        if (
            type(summary.get("returned")) is not int
            or summary["returned"] != len(pool["candidates"])
            or type(summary.get("truncated")) is not bool
        ):
            raise ValueError("candidate pool summary contradicts returned candidates")
        _source_need(pool.get("source_need"))
        for candidate in pool["candidates"]:
            candidate = _require_mapping(candidate, "candidate")
            title_id = candidate.get("title_id")
            if type(title_id) is not int:
                raise ValueError("candidate title ID must be an integer")
            title_ids.add(title_id)

    engine = CardQueryEngine(con)
    cards = (
        engine.find(
            title_id__in=sorted(title_ids),
            order_by="LOWER(c.name), c.title_id",
        )
        if title_ids else []
    )
    cards_by_id = {card.title_id: card for card in cards}
    if set(cards_by_id) != title_ids:
        raise ValueError("candidate title is missing from canonical data")
    classifications = {
        card.title_id: classify_card(card)
        for card in cards
    }

    per_need = []
    aggregate: dict[int, dict] = {}
    invariants: dict[int, dict] = {}
    for pool in pools:
        source = _source_need(pool["source_need"])
        seen_in_pool = set()
        facts = []
        for raw_candidate in pool["candidates"]:
            title_id = raw_candidate["title_id"]
            if title_id in seen_in_pool:
                raise ValueError("candidate title is duplicated within one need pool")
            seen_in_pool.add(title_id)
            card = cards_by_id[title_id]
            classification = classifications[title_id]
            candidate = _validate_candidate(raw_candidate, source, card, classification)
            invariant = _candidate_invariants(candidate)
            if title_id in invariants and invariants[title_id] != invariant:
                raise ValueError("candidate facts contradict across need pools")
            invariants[title_id] = deepcopy(invariant)

            candidate_fact = {
                "title_id": title_id,
                "name": card.name,
                "canonical_facts": _canonical_facts(card),
                "source_need": deepcopy(source),
                "matching_feature_evidence": deepcopy(
                    candidate["matching_feature_evidence"]
                ),
                "reviewed_features": deepcopy(classification["features"]),
                "functional_packages": deepcopy(
                    classification["functional_packages"]
                ),
                "known_printings": deepcopy(candidate["known_printings"]),
                "eligibility_status": candidate["eligibility_status"],
                "eligibility": deepcopy(candidate["eligibility"]),
                "unresolved_eligibility": deepcopy(
                    candidate["unresolved_eligibility"]
                ),
            }
            facts.append(candidate_fact)

            if title_id not in aggregate:
                aggregate[title_id] = {
                    "title_id": title_id,
                    "name": card.name,
                    "canonical_facts": _canonical_facts(card),
                    "reviewed_features": deepcopy(classification["features"]),
                    "functional_packages": deepcopy(
                        classification["functional_packages"]
                    ),
                    "known_printings": deepcopy(candidate["known_printings"]),
                    "eligibility_status": candidate["eligibility_status"],
                    "eligibility": deepcopy(invariant["eligibility"]),
                    "unresolved_eligibility": deepcopy(
                        candidate["unresolved_eligibility"]
                    ),
                    "matched_need_ids": [],
                    "matched_dependency_ids": [],
                    "per_need_matches": [],
                }
            combined = aggregate[title_id]
            combined["matched_need_ids"].append(source["finding_id"])
            combined["matched_dependency_ids"].append(source["dependency_id"])
            combined["per_need_matches"].append({
                "source_need": deepcopy(source),
                "matching_feature_evidence": deepcopy(
                    candidate["matching_feature_evidence"]
                ),
            })

        facts.sort(key=lambda item: (item["name"].casefold(), item["title_id"]))
        per_need.append({
            "source_need": deepcopy(source),
            "source_pool_summary": deepcopy(pool["summary"]),
            "candidates": facts,
        })

    by_title = sorted(
        aggregate.values(),
        key=lambda item: (item["name"].casefold(), item["title_id"]),
    )
    for item in by_title:
        item["matched_need_ids"] = sorted(set(item["matched_need_ids"]))
        item["matched_dependency_ids"] = sorted(
            set(item["matched_dependency_ids"])
        )
        item["per_need_matches"].sort(key=lambda match: (
            match["source_need"]["finding_id"],
            match["source_need"]["dependency_id"],
            match["source_need"]["zone"],
        ))

    return {
        "candidate_facts_model_version": CANDIDATE_FACTS_MODEL_VERSION,
        "source_candidate_model_version": CANDIDATE_MODEL_VERSION,
        "functional_package_model_version": PACKAGE_MODEL_VERSION,
        "candidate_title_count": len(by_title),
        "source_context": {
            "trigger_finding_type": source_output.get("trigger_finding_type"),
            "format_context": source_output.get("format_context"),
            "color_identity_context": source_output.get("color_identity_context"),
            "ownership_context": source_output.get("ownership_context"),
            "ignored_non_trigger_findings": source_output.get(
                "ignored_non_trigger_findings"
            ),
            "source_ordering": source_output.get("ordering"),
            "source_limitations": deepcopy(source_output.get("limitations")),
        },
        "per_need": per_need,
        "candidate_facts_by_title": by_title,
        "ordering": "casefolded_card_name_then_title_id_non_ranking",
        "limitations": [
            "Facts include only capabilities recognized by the reviewed classifier.",
            "Eligibility and ownership facts are carried forward without reinterpretation.",
            "Multi-need and multi-package overlap do not assign strategic value.",
            "Candidate facts are not comparison, ranking, scoring, or recommendation.",
        ],
    }
