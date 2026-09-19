"""Deterministic candidate retrieval for reviewed structural support needs.

Candidate presence is not ranking, comparison, or a recommendation.  Matching
uses only existing classified features; eligibility uses established canonical,
format, color-identity, copy-limit, and optional collection facts.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import sqlite3

from mtgadb import canonical
from mtgadb.model import Collection, Deck
from mtgadb.query import CardQueryEngine
from services.intelligence import classify_card
from services.validator import card_copy_limit


CANDIDATE_MODEL_VERSION = "1"


def _output(
    pools: list[dict],
    *,
    format_name: str | None,
    allowed_colors: frozenset[str] | None,
    collection: Collection | None,
    ignored: int,
) -> dict:
    return {
        "candidate_model_version": CANDIDATE_MODEL_VERSION,
        "trigger_finding_type": "support_need",
        "format_context": format_name,
        "color_identity_context": (
            "".join(sorted(allowed_colors)) if allowed_colors is not None else None
        ),
        "ownership_context": "known" if collection is not None else "unknown",
        "ignored_non_trigger_findings": ignored,
        "pools": pools,
        "ordering": "casefolded_card_name_then_title_id_non_ranking",
        "limitations": [
            "Only cards whose required feature is recognized by the reviewed classifier can match.",
            "Unknown format context never establishes format legality.",
            "Ownership does not filter the general pool and crafting is not evaluated.",
            "Candidate presence is deterministic retrieval, not ranking, comparison, or recommendation.",
        ],
    }


def _printing_facts(con: sqlite3.Connection) -> dict[int, list[dict]]:
    result: dict[int, list[dict]] = defaultdict(list)
    for row in con.execute(
        "SELECT arena_id, title_id, set_code, collector_number, rarity "
        "FROM printings ORDER BY title_id, arena_id"
    ):
        result[row["title_id"]].append({
            "arena_id": row["arena_id"],
            "set_code": row["set_code"] or "",
            "collector_number": row["collector_number"] or "",
            "rarity": row["rarity"] or "",
        })
    return dict(result)


def _deck_title_counts(con: sqlite3.Connection, deck: Deck) -> Counter:
    quantities: Counter[int] = Counter()
    combined: Counter[int] = Counter()
    for zone in (deck.main, deck.sideboard, deck.commander):
        combined.update({arena_id: quantity for arena_id, quantity in zone.items()
                         if quantity > 0})
    if not combined:
        return quantities
    marks = ",".join("?" for _ in combined)
    title_by_printing = {
        row["arena_id"]: row["title_id"] for row in con.execute(
            f"SELECT arena_id, title_id FROM printings WHERE arena_id IN ({marks})",
            tuple(combined),
        )
    }
    for arena_id, quantity in combined.items():
        if arena_id in title_by_printing:
            quantities[title_by_printing[arena_id]] += quantity
    return quantities


def _source_needs(analysis: dict) -> tuple[list[dict], int]:
    sources = []
    ignored = 0
    zones = analysis.get("zones") or {}
    for zone_name in ("main", "sideboard", "commander"):
        for finding in (zones.get(zone_name) or {}).get("needs", []):
            finding_type = finding.get("finding_type")
            if finding_type == "unused_support_opportunity":
                ignored += 1
                continue
            if finding_type != "support_need":
                raise ValueError("unsupported needs finding type")
            if finding.get("missing_side_name") != "enabler":
                raise ValueError("support need must identify a missing enabler side")
            missing = finding.get("missing_side") or {}
            rule_ids = tuple(missing.get("feature_rule_ids") or ())
            relationship = missing.get("relationship")
            if not rule_ids or not relationship:
                raise ValueError("support need is missing reviewed feature requirements")
            sources.append({
                "zone": zone_name,
                "finding_id": finding["finding_id"],
                "dependency_id": finding["dependency_id"],
                "dependency_label": finding["dependency_label"],
                "missing_side_name": "enabler",
                "missing_side": deepcopy(missing),
                "required_feature_rule_ids": list(rule_ids),
                "required_relationship": relationship,
            })
    return sources, ignored


def discover_candidates(
    analysis: dict,
    deck: Deck,
    con: sqlite3.Connection,
    *,
    format_name: str | None = None,
    allowed_colors: frozenset[str] | None = None,
    collection: Collection | None = None,
    limit_per_need: int | None = None,
) -> dict:
    """Return neutral per-need candidate pools without mutating inputs or data."""
    if analysis.get("needs_model_version") != "1":
        raise ValueError("candidate discovery requires needs model version 1")
    if limit_per_need is not None and (
        type(limit_per_need) is not int or limit_per_need <= 0
    ):
        raise ValueError("candidate limit must be a positive integer")

    sources, ignored = _source_needs(analysis)
    format = canonical.get_format(con, format_name) if format_name else None
    if format_name and format is None:
        raise ValueError(f"format {format_name!r} is not loaded")
    if not sources:
        return _output(
            [],
            format_name=format.name if format else None,
            allowed_colors=allowed_colors,
            collection=collection,
            ignored=ignored,
        )

    engine = CardQueryEngine(con)
    legal_title_ids = (
        {card.title_id for card in engine.find(
            format=format.name, order_by="LOWER(c.name), c.title_id",
        )}
        if format else None
    )
    cards = engine.find(order_by="LOWER(c.name), c.title_id")
    classified = {card.title_id: classify_card(card) for card in cards}
    printings = _printing_facts(con)
    current_counts = _deck_title_counts(con, deck)

    pools = []
    for source in sources:
        required_ids = set(source["required_feature_rule_ids"])
        relationship = source["required_relationship"]
        matches = []
        for card in cards:
            evidence = [
                feature for feature in classified[card.title_id]["features"]
                if feature["rule_id"] in required_ids
                and feature["relationship"] == relationship
            ]
            if evidence:
                matches.append((card, evidence))

        excluded = Counter()
        unresolved = Counter()
        included = []
        for card, evidence in matches:
            reasons = []
            known_printings = printings.get(card.title_id, [])

            if format is None:
                format_fact = {
                    "status": "unknown",
                    "format": None,
                    "eligible_printing_ids": [],
                    "reason": "no_format_context",
                }
            elif card.title_id not in legal_title_ids:
                reasons.append("format_illegal")
                format_fact = None
            else:
                explicitly_allowed = (
                    format.allowed_title_ids is not None
                    and card.title_id in format.allowed_title_ids
                )
                eligible_printings = [
                    printing["arena_id"] for printing in known_printings
                    if not format.legal_sets
                    or explicitly_allowed
                    or printing["set_code"] in format.legal_sets
                ]
                format_fact = {
                    "status": "legal",
                    "format": format.name,
                    "eligible_printing_ids": eligible_printings,
                    "reason": "validator_backed_format_rules",
                }

            if allowed_colors is None:
                color_fact = {
                    "status": "not_applicable",
                    "allowed_colors": None,
                    "card_color_identity": card.color_identity,
                }
            elif set(card.color_identity) <= set(allowed_colors):
                color_fact = {
                    "status": "compatible",
                    "allowed_colors": "".join(sorted(allowed_colors)),
                    "card_color_identity": card.color_identity,
                }
            else:
                reasons.append("color_identity_incompatible")
                color_fact = None

            limit = card_copy_limit(card.name, card.rules_text)
            if format is not None and card.title_id in format.individual_card_quotas:
                format_limit = format.individual_card_quotas[card.title_id]
                limit = format_limit if limit is None else min(limit, format_limit)
            current = current_counts[card.title_id]
            if limit is not None and current >= limit:
                reasons.append("playset_cap_reached")
                playset_fact = None
            else:
                playset_fact = {
                    "status": "unlimited" if limit is None else "capacity_available",
                    "current_deck_copies": current,
                    "copy_limit": limit,
                    "remaining_capacity": None if limit is None else limit - current,
                }

            if (
                format is not None
                and source["zone"] == "commander"
                and format.allowed_commander_title_ids is not None
                and card.title_id not in format.allowed_commander_title_ids
            ):
                reasons.append("commander_not_allowed")

            if reasons:
                excluded.update(set(reasons))
                continue

            if collection is None:
                ownership_fact = {"status": "unknown", "owned_copies": None}
            else:
                ownership_fact = {
                    "status": "known",
                    "owned_copies": sum(
                        collection.cards.get(printing["arena_id"], 0)
                        for printing in known_printings
                    ),
                }
            unresolved_dimensions = []
            if format is None:
                unresolved_dimensions.append("format_legality")
                unresolved["format_legality_unknown"] += 1
            if not known_printings:
                unresolved_dimensions.append("printing_availability")
                unresolved["printing_availability_unknown"] += 1
            included.append({
                "title_id": card.title_id,
                "name": card.name,
                "matching_feature_evidence": deepcopy(evidence),
                "source_need": deepcopy(source),
                "known_printings": deepcopy(known_printings),
                "eligibility_status": (
                    "eligibility_unknown" if unresolved_dimensions else "eligible"
                ),
                "eligibility": {
                    "required_feature": {
                        "status": "matched",
                        "feature_rule_ids": source["required_feature_rule_ids"],
                        "relationship": relationship,
                    },
                    "format_legality": format_fact,
                    "color_identity": color_fact,
                    "playset": playset_fact,
                    "ownership": ownership_fact,
                    "crafting": {"status": "not_evaluated"},
                },
                "unresolved_eligibility": unresolved_dimensions,
            })

        included.sort(key=lambda item: (item["name"].casefold(), item["title_id"]))
        matched_included = len(included)
        returned = included[:limit_per_need] if limit_per_need else included
        pools.append({
            "source_need": deepcopy(source),
            "candidates": returned,
            "summary": {
                "canonical_cards_classified": len(cards),
                "required_feature_not_matched": len(cards) - len(matches),
                "feature_matches": len(matches),
                "included": matched_included,
                "returned": len(returned),
                "excluded": len(matches) - matched_included,
                "excluded_reasons": dict(sorted(excluded.items())),
                "unresolved_reasons": dict(sorted(unresolved.items())),
                "truncated": len(returned) < matched_included,
            },
        })

    return _output(
        pools,
        format_name=format.name if format else None,
        allowed_colors=allowed_colors,
        collection=collection,
        ignored=ignored,
    )
