from __future__ import annotations

from copy import deepcopy
import unittest

from services.candidate_comparison import (
    CANDIDATE_COMPARISON_MODEL_VERSION,
    build_candidate_comparisons,
    compare_candidate_pair,
)


def context(
    availability="unconditional", ability_kind="spell_effect", *,
    trigger=None, costs=None, parse_status="supported", unsupported=None,
):
    return {
        "ability_id": "ability.001",
        "ability_kind": ability_kind,
        "availability": availability,
        "prerequisites": {
            "trigger": deepcopy(trigger),
            "costs": deepcopy(costs or []),
            "conditions": [],
            "qualifiers": [],
        },
        "effect": {"kind": "gain_life", "amount": 1},
        "parse_status": parse_status,
        "unsupported_remainder": deepcopy(unsupported or []),
    }


def feature(rule_id, evidence, dependency_context, relationship="producer"):
    return {
        "rule_id": rule_id,
        "dimension": "theme",
        "label": "lifegain" if "lifegain" in rule_id else "spells",
        "relationship": relationship,
        "evidence": evidence,
        "explanation": "Reviewed feature evidence.",
        "dependency_context": deepcopy(dependency_context),
    }


def package(package_id, evidence):
    return {
        "package_id": package_id,
        "label": package_id.split(".")[1],
        "explanation": "Reviewed functional package.",
        "evidence": [deepcopy(evidence)],
    }


def need(zone, finding_id, dependency_id, rule_ids, relationship="producer"):
    return {
        "zone": zone,
        "finding_id": finding_id,
        "dependency_id": dependency_id,
        "dependency_label": dependency_id.split(".")[1],
        "missing_side_name": "enabler",
        "missing_side": {
            "relationship": relationship,
            "acceptable_feature_rule_ids": list(rule_ids),
            "matching_semantics": "any",
            "feature_rule_ids": list(rule_ids),
            "copy_count": 0,
            "cards": [],
        },
        "required_feature_rule_ids": list(rule_ids),
        "feature_matching_semantics": "any",
        "required_relationship": relationship,
    }


def eligibility(ownership):
    return {
        "format_legality": {
            "status": "legal", "format": "Test",
            "eligible_printing_ids": [], "reason": "validator_backed_format_rules",
        },
        "color_identity": {
            "status": "not_applicable", "allowed_colors": None,
            "card_color_identity": "W",
        },
        "playset": {
            "status": "capacity_available", "current_deck_copies": 0,
            "copy_limit": 4, "remaining_capacity": 4,
        },
        "ownership": deepcopy(ownership),
        "crafting": {"status": "not_evaluated"},
    }


def title(
    title_id, name, mana_value, features, package_ids, ownership, printings,
):
    packages = [package(package_id, features[0]) for package_id in sorted(package_ids)]
    return {
        "title_id": title_id,
        "name": name,
        "canonical_facts": {
            "title_id": title_id,
            "name": name,
            "mana_cost": f"{{{mana_value}}}{{W}}",
            "mana_value": mana_value,
            "types": "Sorcery",
            "subtypes": "",
            "colors": "",
            "color_identity": "W",
            "power": "",
            "toughness": "",
        },
        "reviewed_features": deepcopy(features),
        "functional_packages": packages,
        "known_printings": deepcopy(printings),
        "eligibility_status": "eligible",
        "eligibility": eligibility(ownership),
        "unresolved_eligibility": [],
        "matched_need_ids": [],
        "matched_dependency_ids": [],
        "per_need_matches": [],
    }


def candidate(title_row, source_need, evidence):
    required = {
        "status": "matched",
        "feature_rule_ids": deepcopy(source_need["required_feature_rule_ids"]),
        "matching_semantics": "any",
        "relationship": source_need["required_relationship"],
    }
    return {
        "title_id": title_row["title_id"],
        "name": title_row["name"],
        "canonical_facts": deepcopy(title_row["canonical_facts"]),
        "source_need": deepcopy(source_need),
        "matching_feature_evidence": deepcopy(evidence),
        "reviewed_features": deepcopy(title_row["reviewed_features"]),
        "functional_packages": deepcopy(title_row["functional_packages"]),
        "known_printings": deepcopy(title_row["known_printings"]),
        "eligibility_status": title_row["eligibility_status"],
        "eligibility": {"required_feature": required, **deepcopy(title_row["eligibility"])},
        "unresolved_eligibility": deepcopy(title_row["unresolved_eligibility"]),
    }


def add_pool(facts, source_need, rows, *, truncated=False):
    candidates = [candidate(row, source_need, evidence) for row, evidence in rows]
    pool = {
        "source_need": deepcopy(source_need),
        "source_pool_summary": {
            "canonical_cards_classified": len(facts["candidate_facts_by_title"]),
            "required_feature_not_matched": 0,
            "feature_matches": len(candidates),
            "included": len(candidates),
            "returned": len(candidates),
            "excluded": 0,
            "excluded_reasons": {},
            "unresolved_reasons": {},
            "truncated": truncated,
        },
        "candidates": candidates,
    }
    facts["per_need"].append(pool)
    for row, evidence in rows:
        row["matched_need_ids"].append(source_need["finding_id"])
        row["matched_dependency_ids"].append(source_need["dependency_id"])
        row["per_need_matches"].append({
            "source_need": deepcopy(source_need),
            "matching_feature_evidence": deepcopy(evidence),
        })


def candidate_facts():
    unconditional = feature(
        "effect.lifegain.v1", "You gain 1 life.", context(),
    )
    triggered = feature(
        "effect.lifegain.v1", "Whenever you cast a spell, you gain 1 life.",
        context(
            "conditional", "triggered",
            trigger={"event": "spell_cast", "subject": "spell", "conditions": []},
        ),
    )
    activated = feature(
        "effect.lifegain.v1", "{T}: You gain 1 life.",
        context(
            "conditional", "activated",
            costs=[{"kind": "tap", "subject": "self", "timing": "activated"}],
        ),
    )
    partial = feature(
        "effect.lifegain.v1", "You gain 1 life. Unmodeled rider.",
        context(
            "partially_reviewed", "triggered", parse_status="partial",
            trigger={"event": "spell_cast", "subject": "spell", "conditions": []},
            unsupported=[{"text": "Unmodeled rider.", "reason": "unsupported_clause"}],
        ),
    )
    spell = feature(
        "type.spells.v1", "Sorcery",
        {
            "ability_id": None,
            "ability_kind": "card_type",
            "availability": "unconditional",
            "prerequisites": {
                "trigger": None, "costs": [], "conditions": [], "qualifiers": [],
            },
            "effect": None,
            "parse_status": "supported",
            "unsupported_remainder": [],
        },
        "enabler",
    )
    alpha = title(
        1, "Alpha", 2, [unconditional, spell],
        ["package.card_advantage.v1", "package.lifegain.v1", "package.spell_matters.v1"],
        {"status": "unknown", "owned_copies": None},
        [
            {"arena_id": 101, "set_code": "AAA", "collector_number": "1", "rarity": "common"},
            {"arena_id": 102, "set_code": "BBB", "collector_number": "2", "rarity": "rare"},
        ],
    )
    beta = title(
        2, "Beta", 4, [triggered], ["package.lifegain.v1"],
        {"status": "known", "owned_copies": 0},
        [{"arena_id": 201, "set_code": "AAA", "collector_number": "2", "rarity": "common"}],
    )
    gamma = title(
        3, "Gamma", 3, [activated, partial], ["package.lifegain.v1"],
        {"status": "known", "owned_copies": 2},
        [{"arena_id": 301, "set_code": "AAA", "collector_number": "3", "rarity": "common"}],
    )
    facts = {
        "candidate_facts_model_version": "2",
        "source_candidate_model_version": "2",
        "functional_package_model_version": "1",
        "candidate_title_count": 3,
        "source_context": {
            "trigger_finding_type": "support_need",
            "format_context": "Test",
            "color_identity_context": None,
            "ownership_context": "unknown",
            "ignored_non_trigger_findings": 0,
            "source_ordering": "casefolded_card_name_then_title_id_non_ranking",
            "source_limitations": [],
        },
        "per_need": [],
        "candidate_facts_by_title": [alpha, beta, gamma],
        "ordering": "casefolded_card_name_then_title_id_non_ranking",
        "limitations": [],
    }
    life_main = need(
        "main", "need.lifegain.enabler.v1", "dependency.lifegain.v1",
        ["effect.lifegain.v1"],
    )
    spell_main = need(
        "main", "need.spell_cast.enabler.v1", "dependency.spell_cast.v1",
        ["type.spells.v1"], "enabler",
    )
    life_side = need(
        "sideboard", "need.lifegain.enabler.v1", "dependency.lifegain.v1",
        ["effect.lifegain.v1"],
    )
    add_pool(facts, life_main, [
        (alpha, [unconditional]), (beta, [triggered]), (gamma, [activated, partial]),
    ])
    add_pool(facts, spell_main, [(alpha, [spell])], truncated=True)
    add_pool(facts, life_side, [(alpha, [unconditional])])
    for row in facts["candidate_facts_by_title"]:
        row["matched_need_ids"] = sorted(set(row["matched_need_ids"]))
        row["matched_dependency_ids"] = sorted(set(row["matched_dependency_ids"]))
        row["per_need_matches"].sort(key=lambda item: (
            item["source_need"]["finding_id"],
            item["source_need"]["dependency_id"],
            item["source_need"]["zone"],
        ))
    return facts


def matrix(result, zone="main", finding="need.lifegain.enabler.v1"):
    return next(item for item in result["need_matrices"]
                if item["need_key"]["zone"] == zone
                and item["need_key"]["finding_id"] == finding)


def dimension(pool, dimension_id):
    return next(item for item in pool["dimension_comparisons"]
                if item["dimension_id"] == dimension_id)


class CandidateComparisonTests(unittest.TestCase):
    def test_version_scalar_statuses_and_blank_canonical_values(self):
        result = build_candidate_comparisons(candidate_facts())
        pool = matrix(result)
        self.assertEqual(CANDIDATE_COMPARISON_MODEL_VERSION, "1")
        self.assertEqual(result["candidate_comparison_model_version"], "1")
        self.assertEqual(dimension(pool, "card.types")["status"], "same")
        self.assertEqual(dimension(pool, "mana.value")["status"], "different")
        self.assertEqual(dimension(pool, "eligibility.ownership")["status"], "unknown")
        self.assertEqual(
            dimension(pool, "eligibility.color_identity")["status"],
            "not_applicable",
        )
        subtypes = dimension(pool, "card.subtypes")
        self.assertEqual(subtypes["status"], "unknown")
        self.assertTrue(all(cell["fact_status"] == "unknown" for cell in subtypes["values"]))

    def test_need_coverage_uses_zone_aware_identity(self):
        result = build_candidate_comparisons(candidate_facts())
        alpha = next(item for item in result["title_index"] if item["title_id"] == 1)
        self.assertEqual(alpha["need_coverage"]["matched_need_count"], 3)
        self.assertEqual(
            {(row["zone"], row["finding_id"]) for row in alpha["need_coverage"]["needs"]},
            {
                ("main", "need.lifegain.enabler.v1"),
                ("main", "need.spell_cast.enabler.v1"),
                ("sideboard", "need.lifegain.enabler.v1"),
            },
        )

    def test_package_intersection_and_pool_local_exclusivity(self):
        packages = matrix(build_candidate_comparisons(candidate_facts()))["package_comparison"]
        self.assertEqual(packages["shared_by_all_package_ids"], ["package.lifegain.v1"])
        alpha = next(row for row in packages["pool_local_exclusive_package_ids_by_title"]
                     if row["title_id"] == 1)
        self.assertEqual(alpha["package_ids"], [
            "package.card_advantage.v1", "package.spell_matters.v1",
        ])

    def test_support_context_is_plural_and_preserves_prerequisites(self):
        pool = matrix(build_candidate_comparisons(candidate_facts()))
        by_title = {row["title_id"]: row["support_context"] for row in pool["candidates"]}
        self.assertEqual(by_title[1]["entries"][0]["dependency_context"]["availability"],
                         "unconditional")
        self.assertEqual(by_title[2]["entries"][0]["dependency_context"]["ability_kind"],
                         "triggered")
        self.assertEqual(
            by_title[2]["entries"][0]["dependency_context"]["prerequisites"]["trigger"]["event"],
            "spell_cast",
        )
        self.assertEqual(len(by_title[3]["entries"]), 2)
        kinds = {entry["dependency_context"]["ability_kind"] for entry in by_title[3]["entries"]}
        availability = {entry["dependency_context"]["availability"]
                        for entry in by_title[3]["entries"]}
        self.assertEqual(kinds, {"activated", "triggered"})
        self.assertEqual(availability, {"conditional", "partially_reviewed"})
        activated_entry = next(
            entry for entry in by_title[3]["entries"]
            if entry["dependency_context"]["ability_kind"] == "activated"
        )
        self.assertEqual(
            activated_entry["dependency_context"]["prerequisites"]["costs"][0]["kind"],
            "tap",
        )

    def test_alternative_rule_ids_remain_explicit(self):
        facts = candidate_facts()
        pool = facts["per_need"][0]
        alternatives = ["effect.lifegain.v1", "type.spells.v1"]
        pool["source_need"]["required_feature_rule_ids"] = alternatives
        pool["source_need"]["missing_side"]["acceptable_feature_rule_ids"] = alternatives
        pool["source_need"]["missing_side"]["feature_rule_ids"] = alternatives
        for candidate_row in pool["candidates"]:
            candidate_row["source_need"] = deepcopy(pool["source_need"])
            candidate_row["eligibility"]["required_feature"]["feature_rule_ids"] = alternatives
        for title_row in facts["candidate_facts_by_title"]:
            for match in title_row["per_need_matches"]:
                if match["source_need"]["zone"] == "main" and match["source_need"]["finding_id"] == pool["source_need"]["finding_id"]:
                    match["source_need"] = deepcopy(pool["source_need"])
        result = build_candidate_comparisons(facts)
        support = matrix(result)["candidates"][0]["support_context"]
        self.assertEqual(support["matching_semantics"], "any")
        self.assertEqual(support["acceptable_feature_rule_ids"], alternatives)

    def test_printings_ownership_truncation_and_evidence_completeness(self):
        result = build_candidate_comparisons(candidate_facts())
        alpha = next(item for item in result["title_index"] if item["title_id"] == 1)
        beta = next(item for item in result["title_index"] if item["title_id"] == 2)
        self.assertEqual(len(alpha["known_printings"]), 2)
        self.assertEqual(sum(item["title_id"] == 1 for item in result["title_index"]), 1)
        self.assertEqual(alpha["eligibility"]["ownership"], {
            "status": "unknown", "owned_copies": None,
        })
        self.assertEqual(beta["eligibility"]["ownership"], {
            "status": "known", "owned_copies": 0,
        })
        spell = matrix(result, finding="need.spell_cast.enabler.v1")
        self.assertTrue(spell["source_pool_summary"]["truncated"])
        self.assertTrue(all(row["status"] == "not_applicable"
                            and row["reason"] == "insufficient_candidates"
                            for row in spell["dimension_comparisons"]))
        completeness = matrix(result)["evidence_completeness"]
        self.assertEqual(completeness["status"], "unknown")
        self.assertEqual(
            completeness["reason"],
            "source_evidence_boundary_not_present_in_candidate_facts_v2",
        )
        self.assertTrue(completeness["feature_level_unsupported_remainders"])

    def test_output_is_deterministic_under_input_reordering(self):
        first_input = candidate_facts()
        second_input = deepcopy(first_input)
        second_input["candidate_facts_by_title"].reverse()
        second_input["per_need"].reverse()
        for pool in second_input["per_need"]:
            pool["candidates"].reverse()
        self.assertEqual(
            build_candidate_comparisons(first_input),
            build_candidate_comparisons(second_input),
        )

    def test_wrong_versions_and_title_count_fail_closed(self):
        for key, value in (
            ("candidate_facts_model_version", "1"),
            ("source_candidate_model_version", "1"),
            ("functional_package_model_version", "2"),
        ):
            facts = candidate_facts()
            facts[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                build_candidate_comparisons(facts)
        facts = candidate_facts()
        facts["candidate_title_count"] = 99
        with self.assertRaises(ValueError):
            build_candidate_comparisons(facts)

    def test_duplicate_title_within_need_fails_closed(self):
        facts = candidate_facts()
        facts["per_need"][0]["candidates"].append(
            deepcopy(facts["per_need"][0]["candidates"][0])
        )
        facts["per_need"][0]["source_pool_summary"]["returned"] += 1
        with self.assertRaises(ValueError):
            build_candidate_comparisons(facts)

    def test_contradictory_title_facts_fail_closed(self):
        facts = candidate_facts()
        facts["per_need"][0]["candidates"][0]["canonical_facts"]["name"] = "Other"
        with self.assertRaises(ValueError):
            build_candidate_comparisons(facts)

    def test_contradictory_eligibility_and_ownership_fail_closed(self):
        for path, value in (
            (("playset", "copy_limit"), 99),
            (("ownership", "owned_copies"), 42),
        ):
            facts = candidate_facts()
            facts["per_need"][0]["candidates"][0]["eligibility"][path[0]][path[1]] = value
            with self.subTest(path=path), self.assertRaises(ValueError):
                build_candidate_comparisons(facts)

    def test_contradictory_printings_fail_closed(self):
        facts = candidate_facts()
        facts["per_need"][0]["candidates"][0]["known_printings"][0]["rarity"] = "mythic"
        with self.assertRaises(ValueError):
            build_candidate_comparisons(facts)

    def test_malformed_support_context_and_evidence_fail_closed(self):
        facts = candidate_facts()
        facts["per_need"][0]["candidates"][0]["matching_feature_evidence"][0][
            "dependency_context"
        ]["availability"] = "often"
        with self.assertRaises(ValueError):
            build_candidate_comparisons(facts)

    def test_reference_provenance_alternatives_and_summary_fail_closed(self):
        mutations = []

        absent_title = candidate_facts()
        absent_title["per_need"][0]["candidates"][0]["title_id"] = 999
        mutations.append(absent_title)

        conflicting_need = candidate_facts()
        conflicting_need["per_need"][0]["candidates"][0]["source_need"]["zone"] = "sideboard"
        mutations.append(conflicting_need)

        malformed_alternative = candidate_facts()
        malformed_alternative["per_need"][0]["source_need"]["feature_matching_semantics"] = "all"
        mutations.append(malformed_alternative)

        contradictory_summary = candidate_facts()
        contradictory_summary["per_need"][0]["source_pool_summary"]["returned"] = 99
        mutations.append(contradictory_summary)

        for index, facts in enumerate(mutations):
            with self.subTest(case=index), self.assertRaises(ValueError):
                build_candidate_comparisons(facts)

        facts = candidate_facts()
        facts["per_need"][0]["candidates"][0]["matching_feature_evidence"][0][
            "relationship"
        ] = "payoff"
        with self.assertRaises(ValueError):
            build_candidate_comparisons(facts)

    def test_large_pool_is_linear_and_contains_no_pair_collection(self):
        facts = candidate_facts()
        base_title = facts["candidate_facts_by_title"][1]
        base_candidate = facts["per_need"][0]["candidates"][1]
        for index in range(4, 104):
            new_title = deepcopy(base_title)
            new_title["title_id"] = index
            new_title["name"] = f"Synthetic {index:03d}"
            new_title["canonical_facts"]["title_id"] = index
            new_title["canonical_facts"]["name"] = new_title["name"]
            new_title["known_printings"][0]["arena_id"] = index * 100
            new_title["matched_need_ids"] = ["need.lifegain.enabler.v1"]
            new_title["matched_dependency_ids"] = ["dependency.lifegain.v1"]
            new_title["per_need_matches"] = [{
                "source_need": deepcopy(facts["per_need"][0]["source_need"]),
                "matching_feature_evidence": deepcopy(
                    base_candidate["matching_feature_evidence"]
                ),
            }]
            facts["candidate_facts_by_title"].append(new_title)
            new_candidate = candidate(
                new_title, facts["per_need"][0]["source_need"],
                base_candidate["matching_feature_evidence"],
            )
            facts["per_need"][0]["candidates"].append(new_candidate)
        facts["candidate_title_count"] = len(facts["candidate_facts_by_title"])
        facts["per_need"][0]["source_pool_summary"]["returned"] = len(
            facts["per_need"][0]["candidates"]
        )
        result = build_candidate_comparisons(facts)
        pool = matrix(result)
        self.assertEqual(pool["candidate_count"], 103)
        self.assertNotIn("pairs", result)
        self.assertNotIn("pairs", pool)

    def test_on_demand_pair_returns_only_requested_pair(self):
        result = build_candidate_comparisons(candidate_facts())
        key = ("main", "need.lifegain.enabler.v1", "dependency.lifegain.v1")
        pair = compare_candidate_pair(result, key, 1, 2)
        self.assertEqual({pair["candidate_a"]["title_id"], pair["candidate_b"]["title_id"]},
                         {1, 2})
        self.assertEqual(pair["package_comparison"], {
            "shared_package_ids": ["package.lifegain.v1"],
            "candidate_a_only_package_ids": [
                "package.card_advantage.v1", "package.spell_matters.v1",
            ],
            "candidate_b_only_package_ids": [],
        })
        self.assertNotIn("title_index", pair)
        with self.assertRaises(ValueError):
            compare_candidate_pair(result, key, 1, 999)
        spell_key = (
            "main", "need.spell_cast.enabler.v1", "dependency.spell_cast.v1",
        )
        with self.assertRaises(ValueError):
            compare_candidate_pair(result, spell_key, 1, 2)

    def test_input_is_unchanged_and_derived_contract_has_no_evaluative_keys(self):
        facts = candidate_facts()
        before = deepcopy(facts)
        result = build_candidate_comparisons(facts)
        self.assertEqual(facts, before)
        forbidden = {
            "score", "ranking", "tier", "recommendation", "best", "preferred",
            "efficiency", "quality", "synergy_score", "fit_score", "value_score",
        }

        def keys(value):
            if isinstance(value, dict):
                return set(value) | set().union(*(keys(item) for item in value.values()))
            if isinstance(value, list):
                return set().union(*(keys(item) for item in value), set())
            return set()

        self.assertTrue(forbidden.isdisjoint(keys(result)))


if __name__ == "__main__":
    unittest.main()
