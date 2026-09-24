"""Deterministic diagnosis over normalized Deck Intelligence output.

This module does not read card text, query a database, validate legality, or
recommend changes. Unknown evidence limits conclusions instead of counting
against a deck.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from services.intelligence import INTERACTION_FAMILIES

DIAGNOSIS_VERSION = "1"

PLAN_FAMILIES = {
    "interaction.token_draw.v1": "token_value",
    "interaction.token_sacrifice.v1": "token_sacrifice",
    "interaction.spell_draw.v1": "spells_matter",
}

STRATEGIC_RELATIONSHIPS = frozenset({"producer", "enabler", "consumer", "payoff"})


def normalize_analysis(analysis: dict) -> dict:
    """Return the flat-feature view shared by analysis Versions 1 through 4.

    Version 2 adds ability records and coverage fields; Version 3 adds
    compatibility-aware dependency and needs records. Both retain the Version 1
    zones, flat features and interaction rows. Diagnosis consumes only that
    compatibility surface and does not reinterpret ability text. Version 4
    adds a Deck identity that diagnosis does not use.
    """
    version = analysis.get("analysis_version")
    if version not in {"1", "2", "3", "4"}:
        raise ValueError("unsupported deck analysis version")
    return {
        "analysis_version": "1",
        "legality": analysis.get("legality"),
        "zones": analysis.get("zones"),
        "interactions": analysis.get("interactions"),
        "limitations": analysis.get("limitations", []),
    }


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _at_least(numerator: int, denominator: int, threshold_numerator: int,
              threshold_denominator: int) -> bool:
    """Compare a ratio exactly; rounded ratios are presentation values only."""
    return denominator > 0 and (
        numerator * threshold_denominator >= denominator * threshold_numerator
    )


def _at_most(numerator: int, denominator: int, threshold_numerator: int,
             threshold_denominator: int) -> bool:
    """Compare a ratio exactly; an empty population has zero observed share."""
    return denominator == 0 or (
        numerator * threshold_denominator <= denominator * threshold_numerator
    )


def _has_rule(card: dict, rule_id: str) -> bool:
    return any(feature["rule_id"] == rule_id for feature in card["features"])


def _is_land(card: dict) -> bool:
    return _has_rule(card, "type.lands.v1")


def _is_strategic(card: dict) -> bool:
    return any(
        feature["dimension"] == "role"
        or feature["relationship"] in STRATEGIC_RELATIONSHIPS
        for feature in card["features"]
    )


def _coverage(zone: dict) -> dict:
    cards = zone["cards"]
    total = zone["total_count"]
    resolved = zone["resolved_count"]
    nonland = sum(card["quantity"] for card in cards if not _is_land(card))
    strategic = sum(
        card["quantity"] for card in cards if not _is_land(card) and _is_strategic(card)
    )
    unsupported_nonland = sum(
        card["quantity"] for card in cards
        if not _is_land(card) and card["text_status"] == "unsupported"
    )
    return {
        "total_copies": total,
        "resolved_copies": resolved,
        "unresolved_copies": total - resolved,
        "resolution_coverage": _ratio(resolved, total),
        "resolved_nonland_copies": nonland,
        "strategically_classified_nonland_copies": strategic,
        "strategic_coverage": _ratio(strategic, nonland),
        "unsupported_nonland_copies": unsupported_nonland,
        "unsupported_nonland_coverage": _ratio(unsupported_nonland, nonland),
        "unclassified_nonland_copies": sum(
            card["quantity"] for card in cards
            if not _is_land(card) and card["status"] == "unclassified"
        ),
        "partially_classified_copies": sum(
            card["quantity"] for card in cards if card["status"] == "partial"
        ),
        "unsupported_text_copies": sum(
            card["quantity"] for card in cards if card["text_status"] == "unsupported"
        ),
        "canonical_anomaly_copies": sum(
            card["quantity"] for card in cards if card["text_status"] == "anomalous"
        ),
    }


def _curve_fact(zone: dict) -> dict:
    curve = {int(value): count for value, count in zone["nonland_mana_curve"].items()}
    total = sum(curve.values())
    median = None
    modes: list[int] = []
    if total:
        position = (total + 1) // 2
        seen = 0
        for value in sorted(curve):
            seen += curve[value]
            if seen >= position:
                median = value
                break
        peak = max(curve.values())
        modes = [value for value in sorted(curve) if curve[value] == peak]
    return {
        "id": "fact.nonland_curve.v1",
        "zone": "main",
        "nonland_copies": total,
        "mana_value_counts": {str(value): curve[value] for value in sorted(curve)},
        "weighted_median_mana_value": median,
        "modal_mana_values": modes,
        "bins": {
            "zero_to_two": sum(count for value, count in curve.items() if value <= 2),
            "three_to_four": sum(count for value, count in curve.items() if 3 <= value <= 4),
            "five_plus": sum(count for value, count in curve.items() if value >= 5),
        },
        "interpretation": "descriptive_only",
    }


def _family_support(main: dict, interaction_rows: list[dict], reference_size: int) -> list[dict]:
    cards = main["cards"]
    quantities = {card["title_id"]: card["quantity"] for card in cards}
    results = []
    for family in INTERACTION_FAMILIES:
        sources = sorted(
            card["title_id"] for card in cards
            if _has_rule(card, family.source_feature_rule_id)
        )
        beneficiaries = sorted(
            card["title_id"] for card in cards
            if _has_rule(card, family.beneficiary_feature_rule_id)
        )
        pairs = sorted(
            (row["source_title_id"], row["target_title_id"])
            for row in interaction_rows if row["rule_id"] == family.family_id
        )
        support_titles = sorted(set(sources) | set(beneficiaries))
        support_copies = sum(quantities[title_id] for title_id in support_titles)
        results.append({
            "family_id": family.family_id,
            "plan": PLAN_FAMILIES[family.family_id],
            "source_requirement": family.source_feature_rule_id,
            "beneficiary_requirement": family.beneficiary_feature_rule_id,
            "source_title_ids": sources,
            "beneficiary_title_ids": beneficiaries,
            "source_copies": sum(quantities[title_id] for title_id in sources),
            "beneficiary_copies": sum(quantities[title_id] for title_id in beneficiaries),
            "supporting_title_ids": support_titles,
            "supporting_title_count": len(support_titles),
            "supporting_copies": support_copies,
            "support_density": _ratio(support_copies, reference_size),
            "interaction_pairs": [
                {"source_title_id": source, "beneficiary_title_id": beneficiary}
                for source, beneficiary in pairs
            ],
            "interaction_pair_count": len(pairs),
        })
    return results


def _candidate(family: dict, coverage: dict, reference_size: int) -> dict:
    minimum_support = max(4, (reference_size + 9) // 10)
    failures = []
    if family["interaction_pair_count"] < 1:
        failures.append("no_supported_interaction")
    if family["supporting_title_count"] < 2:
        failures.append("fewer_than_two_supporting_titles")
    if family["source_copies"] < 1 or family["beneficiary_copies"] < 1:
        failures.append("missing_relationship_side")
    if family["supporting_copies"] < minimum_support:
        failures.append("support_below_scaled_minimum")
    if not _at_least(
        coverage["resolved_copies"], coverage["total_copies"], 95, 100
    ):
        failures.append("resolution_coverage_below_95_percent")
    if not _at_least(
        coverage["strategically_classified_nonland_copies"],
        coverage["resolved_nonland_copies"], 1, 2,
    ):
        failures.append("strategic_coverage_below_50_percent")

    passes = not failures
    high = (
        passes
        and _at_least(
            coverage["strategically_classified_nonland_copies"],
            coverage["resolved_nonland_copies"], 3, 4,
        )
        and _at_least(family["supporting_copies"], reference_size, 1, 5)
        and family["supporting_title_count"] >= 3
        and family["interaction_pair_count"] >= 2
        and _at_most(
            coverage["unsupported_nonland_copies"],
            coverage["resolved_nonland_copies"], 1, 4,
        )
    )
    return {
        **family,
        "minimum_supporting_copies": minimum_support,
        "confidence": "high" if high else "moderate" if passes else
                      "low" if family["interaction_pair_count"] else "not_assessed",
        "passes_plan_gate": passes,
        "gate_failures": failures,
    }


def _plan(candidates: list[dict], reference_size: int) -> tuple[str, dict, list[dict]]:
    evidence = [candidate for candidate in candidates if candidate["interaction_pair_count"]]
    passing = [candidate for candidate in evidence if candidate["passes_plan_gate"]]
    passing.sort(key=lambda item: (-item["supporting_copies"], item["plan"]))
    evidence.sort(key=lambda item: (-item["supporting_copies"], item["plan"]))

    if len(passing) >= 2:
        confidence = "high" if all(item["confidence"] == "high" for item in passing) else "moderate"
        return "diagnosed", {
            "state": "hybrid",
            "probable_plan": None,
            "probable_plans": sorted(item["plan"] for item in passing),
            "confidence": confidence,
            "evidence_family_ids": sorted(item["family_id"] for item in passing),
        }, []

    if len(passing) == 1:
        winner = passing[0]
        runner = next((item for item in evidence if item["plan"] != winner["plan"]), None)
        close = bool(
            runner
            and 20 * (winner["supporting_copies"] - runner["supporting_copies"])
                < reference_size
            and 2 * winner["supporting_copies"] < 3 * runner["supporting_copies"]
        )
        if not close:
            return "diagnosed", {
                "state": "named",
                "probable_plan": winner["plan"],
                "probable_plans": [winner["plan"]],
                "confidence": winner["confidence"],
                "evidence_family_ids": [winner["family_id"]],
            }, []
        unknown = {
            "id": "unknown.deck_plan_ambiguous.v1",
            "scope": "main",
            "message": "insufficient evidence to diagnose",
            "reason": "leading supported plan is not clearly separated from another candidate",
            "candidate_plans": [winner["plan"], runner["plan"]],
        }
        return "insufficient_evidence", {
            "state": "ambiguous", "probable_plan": None, "probable_plans": [],
            "confidence": "not_assessed", "evidence_family_ids": [],
        }, [unknown]

    unknown = {
        "id": "unknown.deck_plan.v1",
        "scope": "main",
        "message": "insufficient evidence to diagnose",
        "reason": "no candidate passed every evidence and coverage gate",
        "candidate_plans": [item["plan"] for item in evidence],
    }
    return "insufficient_evidence", {
        "state": "insufficient_evidence", "probable_plan": None,
        "probable_plans": [], "confidence": "not_assessed", "evidence_family_ids": [],
    }, [unknown]


def _package_warnings(main: dict, interactions: list[dict]) -> list[dict]:
    warnings = []
    cards = main["cards"]
    for family in INTERACTION_FAMILIES:
        sources = [card for card in cards if _has_rule(card, family.source_feature_rule_id)]
        beneficiaries = [
            card for card in cards if _has_rule(card, family.beneficiary_feature_rule_id)
        ]
        connected = {
            row["target_title_id"] for row in interactions if row["rule_id"] == family.family_id
        }
        for beneficiary in beneficiaries:
            evidence = [
                feature for feature in beneficiary["features"]
                if feature["rule_id"] == family.beneficiary_feature_rule_id
            ]
            if not sources:
                kind = (
                    "consumer" if any(feature["relationship"] == "consumer" for feature in evidence)
                    else "payoff"
                )
                warnings.append({
                    "id": f"warning.unsupported_{kind}.v1",
                    "scope": "main",
                    "family_id": family.family_id,
                    "title_id": beneficiary["title_id"],
                    "message": f"Recognized {kind} has no recognized matching source.",
                    "evidence": evidence,
                })
            elif beneficiary["title_id"] not in connected:
                warnings.append({
                    "id": "warning.disconnected_package.v1",
                    "scope": "main",
                    "family_id": family.family_id,
                    "title_id": beneficiary["title_id"],
                    "message": "Recognized package sides exist but no supported directional pair connects them.",
                    "evidence": evidence,
                })
    return sorted(warnings, key=lambda item: (item["id"], item["family_id"], item["title_id"]))


def _role_interpretations(main: dict) -> list[dict]:
    role_titles: dict[str, dict[int, int]] = defaultdict(dict)
    for card in main["cards"]:
        roles = {
            feature["label"] for feature in card["features"]
            if feature["dimension"] == "role"
        }
        for role in roles:
            role_titles[role][card["title_id"]] = card["quantity"]

    results = []
    for role in sorted(role_titles):
        titles = role_titles[role]
        identifier = "interpretation.role_redundancy.v1" if len(titles) > 1 else \
                     "interpretation.role_concentration.v1"
        results.append({
            "id": identifier,
            "scope": "main",
            "role": role,
            "title_ids": sorted(titles),
            "distinct_title_count": len(titles),
            "copy_count": sum(titles.values()),
            "message": (
                "Several recognized titles provide this role; this is redundancy, not a quality score."
                if len(titles) > 1 else
                "One recognized title provides this role; this is concentration, not a quality warning."
            ),
        })
    return results


def diagnose_analysis(analysis: dict, *, reference_main_size: int | None = None) -> dict:
    """Diagnose one normalized report without card-text or database access."""
    source_version = analysis.get("analysis_version")
    normalized = normalize_analysis(analysis)
    if normalized.get("legality") != "not_evaluated":
        raise ValueError("diagnosis requires analysis with legality not evaluated")
    zones = normalized["zones"]
    main = zones["main"]
    reference_size = main["total_count"] if reference_main_size is None else reference_main_size
    if type(reference_size) is not int or reference_size < 0:
        raise ValueError("reference main size must be a nonnegative integer")

    coverage = {zone: _coverage(zones[zone]) for zone in ("main", "sideboard", "commander")}
    families = _family_support(main, normalized["interactions"], reference_size)
    candidates = [_candidate(family, coverage["main"], reference_size) for family in families]
    status, plan, plan_unknowns = _plan(candidates, reference_size)

    unknowns = [
        {
            "id": "unknown.mana_source_adequacy.v1", "scope": "main",
            "message": "mana-source adequacy not assessed",
            "reason": "colored production, pip demand, conditional mana and draw probability are unavailable",
        },
        {
            "id": "unknown.mechanical_conflicts.v1", "scope": "main",
            "message": "mechanical conflicts not assessed",
            "reason": "no supported conflict vocabulary exists",
        },
        *plan_unknowns,
    ]
    if zones["sideboard"]["total_count"]:
        unknowns.append({
            "id": "unknown.sideboard_purpose.v1", "scope": "sideboard",
            "message": "sideboard purpose not assessed",
            "reason": "matchup and transformation intent are unavailable",
        })
    if zones["commander"]["total_count"]:
        unknowns.append({
            "id": "unknown.commander_integration.v1", "scope": "commander",
            "message": "commander-to-main integration not assessed",
            "reason": "compatible interactions are main-deck only",
        })

    facts = [
        {
            "id": "fact.coverage.v1",
            "zones": coverage,
            "reference_main_size": reference_size,
        },
        {
            "id": "fact.role_theme_counts.v1", "zone": "main",
            "role_counts": main["role_counts"], "theme_counts": main["theme_counts"],
        },
        _curve_fact(main),
        {
            "id": "fact.interaction_family_support.v1", "zone": "main",
            "families": families,
        },
    ]
    return {
        "diagnosis_version": DIAGNOSIS_VERSION,
        "analysis_version": source_version,
        "status": status,
        "message": "insufficient evidence to diagnose" if status == "insufficient_evidence" else
                   "deterministic diagnosis available",
        "plan": {**plan, "candidates": candidates},
        "coverage": coverage,
        "facts": facts,
        "interpretations": _role_interpretations(main),
        "warnings": _package_warnings(main, normalized["interactions"]),
        "unknowns": sorted(unknowns, key=lambda item: (item["scope"], item["id"])),
    }
