from __future__ import annotations

from copy import deepcopy
import unittest

from services.candidate_comparison import build_candidate_comparisons, compare_candidate_pair
from services.strategic_fit import (
    STRATEGIC_FIT_MODEL_VERSION,
    STRATEGIC_SIGNAL_REGISTRY_VERSION,
    build_strategic_fit_signals,
)
from tests.test_candidate_comparison import candidate_facts


EXPECTED_SIGNAL_IDS = (
    "fit.need.current_match.v1",
    "fit.need.multiple_observed_needs.v1",
    "fit.need.single_observed_need.v1",
    "fit.package.multiple_contributions.v1",
    "fit.package.shared_by_all_returned_candidates.v1",
    "fit.package.not_shared_by_all_returned_candidates.v1",
    "fit.package.pool_local_exclusive_contribution.v1",
    "fit.package.same_set_across_returned_pool.v1",
    "fit.support.unconditional.v1",
    "fit.support.prerequisites_present.v1",
    "fit.support.conditional.v1",
    "fit.support.triggered.v1",
    "fit.support.activated.v1",
    "fit.support.partial.v1",
    "fit.support.unsupported_remainder_present.v1",
    "fit.support.alternative_rule_paths.v1",
    "fit.eligibility.unresolved_dimensions_present.v1",
    "fit.eligibility.no_unresolved_dimensions.v1",
    "fit.ownership.known.v1",
    "fit.ownership.unknown.v1",
    "fit.copy.remaining_capacity_positive.v1",
    "fit.copy.unlimited.v1",
    "fit.source.pool_truncated.v1",
    "fit.evidence.zone_boundary_unavailable.v1",
)


def comparison():
    return build_candidate_comparisons(candidate_facts())


def fit(source=None):
    return build_strategic_fit_signals(source or comparison())


def need_set(result, zone="main", finding="need.lifegain.enabler.v1"):
    return next(
        item for item in result["need_signal_sets"]
        if item["need_key"]["zone"] == zone
        and item["need_key"]["finding_id"] == finding
    )


def matrix(source, zone="main", finding="need.lifegain.enabler.v1"):
    return next(
        item for item in source["need_matrices"]
        if item["need_key"]["zone"] == zone
        and item["need_key"]["finding_id"] == finding
    )


def title_row(result, title_id):
    return next(item for item in result["title_signal_index"] if item["title_id"] == title_id)


def candidate_row(result, title_id, zone="main", finding="need.lifegain.enabler.v1"):
    return next(item for item in need_set(result, zone, finding)["candidates"]
                if item["title_id"] == title_id)


def ids(record):
    return [item["signal_id"] for item in record["signals"]]


def set_title_eligibility(facts, title_id, *, unresolved=None, playset=None):
    title = next(item for item in facts["candidate_facts_by_title"]
                 if item["title_id"] == title_id)
    if unresolved is not None:
        title["unresolved_eligibility"] = deepcopy(unresolved)
        title["eligibility_status"] = "eligibility_unknown" if unresolved else "eligible"
    if playset is not None:
        title["eligibility"]["playset"] = deepcopy(playset)
    for pool in facts["per_need"]:
        for candidate in pool["candidates"]:
            if candidate["title_id"] != title_id:
                continue
            if unresolved is not None:
                candidate["unresolved_eligibility"] = deepcopy(unresolved)
                candidate["eligibility_status"] = title["eligibility_status"]
            if playset is not None:
                candidate["eligibility"]["playset"] = deepcopy(playset)


def add_alternative_rule_path(facts):
    pool = facts["per_need"][0]
    alternatives = ["effect.lifegain.v1", "type.spells.v1"]
    pool["source_need"]["required_feature_rule_ids"] = alternatives
    pool["source_need"]["missing_side"]["acceptable_feature_rule_ids"] = alternatives
    pool["source_need"]["missing_side"]["feature_rule_ids"] = alternatives
    for candidate in pool["candidates"]:
        candidate["source_need"] = deepcopy(pool["source_need"])
        candidate["eligibility"]["required_feature"]["feature_rule_ids"] = alternatives
    for title in facts["candidate_facts_by_title"]:
        for match in title["per_need_matches"]:
            source = match["source_need"]
            if source["zone"] == "main" and source["finding_id"] == "need.lifegain.enabler.v1":
                match["source_need"] = deepcopy(pool["source_need"])


def make_same_packages(facts):
    for title in facts["candidate_facts_by_title"]:
        title["functional_packages"] = [
            item for item in title["functional_packages"]
            if item["package_id"] == "package.lifegain.v1"
        ]
    by_title = {item["title_id"]: item for item in facts["candidate_facts_by_title"]}
    for pool in facts["per_need"]:
        for candidate in pool["candidates"]:
            candidate["functional_packages"] = deepcopy(
                by_title[candidate["title_id"]]["functional_packages"]
            )


class StrategicFitTests(unittest.TestCase):
    def test_versions_shape_and_complete_registry_triggers(self):
        base = fit()
        self.assertEqual(STRATEGIC_FIT_MODEL_VERSION, "2")
        self.assertEqual(STRATEGIC_SIGNAL_REGISTRY_VERSION, "1")
        self.assertEqual(base["source_candidate_comparison_model_version"], "2")

        facts = candidate_facts()
        add_alternative_rule_path(facts)
        set_title_eligibility(facts, 2, unresolved=["format_legality"])
        set_title_eligibility(facts, 3, playset={
            "status": "unlimited", "current_deck_copies": 0,
            "copy_limit": None, "remaining_capacity": None,
        })
        variant = fit(build_candidate_comparisons(facts))

        same_facts = candidate_facts()
        make_same_packages(same_facts)
        same = fit(build_candidate_comparisons(same_facts))

        emitted = set()
        for result in (base, variant, same):
            for title in result["title_signal_index"]:
                emitted.update(ids(title))
            for pool in result["need_signal_sets"]:
                emitted.update(item["signal_id"] for item in pool["pool_signals"])
                for candidate in pool["candidates"]:
                    emitted.update(ids(candidate))
        self.assertEqual(emitted, set(EXPECTED_SIGNAL_IDS))

    def test_signal_records_have_exact_contract_fields_and_registry_order(self):
        result = fit()
        all_records = [signal for title in result["title_signal_index"]
                       for signal in title["signals"]]
        all_records += [signal for pool in result["need_signal_sets"]
                        for signal in pool["pool_signals"]]
        all_records += [signal for pool in result["need_signal_sets"]
                        for candidate in pool["candidates"]
                        for signal in candidate["signals"]]
        expected_fields = {
            "signal_id", "category", "scope", "state", "source_paths",
            "evidence", "explanation",
        }
        for signal in all_records:
            self.assertEqual(set(signal), expected_fields)
            self.assertEqual(signal["state"], "present")
            self.assertEqual(
                signal["source_paths"],
                [item["source_path"] for item in signal["evidence"]],
            )
        for record in result["title_signal_index"]:
            positions = [EXPECTED_SIGNAL_IDS.index(item["signal_id"])
                         for item in record["signals"]]
            self.assertEqual(positions, sorted(positions))

    def test_need_coverage_is_zone_aware_and_referenced_without_duplication(self):
        result = fit()
        alpha = title_row(result, 1)
        self.assertIn("fit.need.multiple_observed_needs.v1", ids(alpha))
        signal = next(item for item in alpha["signals"]
                      if item["signal_id"] == "fit.need.multiple_observed_needs.v1")
        needs = signal["evidence"][0]["value"]["needs"]
        self.assertEqual({(item["zone"], item["finding_id"]) for item in needs}, {
            ("main", "need.lifegain.enabler.v1"),
            ("main", "need.spell_cast.enabler.v1"),
            ("sideboard", "need.lifegain.enabler.v1"),
        })
        beta = title_row(result, 2)
        self.assertIn("fit.need.single_observed_need.v1", ids(beta))
        occurrence = candidate_row(result, 1)
        self.assertEqual(occurrence["applicable_title_signal_ids"], ids(alpha))
        self.assertNotIn("fit.need.multiple_observed_needs.v1", ids(occurrence))

    def test_same_title_has_need_specific_support_evidence(self):
        result = fit()
        life = candidate_row(result, 1)
        spell = candidate_row(result, 1, finding="need.spell_cast.enabler.v1")
        life_match = next(item for item in life["signals"]
                          if item["signal_id"] == "fit.need.current_match.v1")
        spell_match = next(item for item in spell["signals"]
                           if item["signal_id"] == "fit.need.current_match.v1")
        self.assertNotEqual(life_match["evidence"], spell_match["evidence"])
        self.assertIn("fit.support.unconditional.v1", ids(life))
        self.assertIn("fit.support.unconditional.v1", ids(spell))

    def test_plural_support_signals_and_exact_prerequisites(self):
        result = fit()
        gamma = candidate_row(result, 3)
        gamma_ids = ids(gamma)
        for signal_id in (
            "fit.support.prerequisites_present.v1",
            "fit.support.conditional.v1",
            "fit.support.triggered.v1",
            "fit.support.activated.v1",
            "fit.support.partial.v1",
            "fit.support.unsupported_remainder_present.v1",
        ):
            self.assertIn(signal_id, gamma_ids)
        prerequisite = next(item for item in gamma["signals"]
                            if item["signal_id"] == "fit.support.prerequisites_present.v1")
        contexts = [entry["dependency_context"]
                    for entry in prerequisite["evidence"][0]["value"]]
        self.assertTrue(any(context["prerequisites"]["costs"] for context in contexts))
        self.assertTrue(any(context["prerequisites"]["trigger"] for context in contexts))
        unsupported = next(item for item in gamma["signals"]
                           if item["signal_id"] == "fit.support.unsupported_remainder_present.v1")
        self.assertEqual(
            unsupported["evidence"][0]["value"][0]["dependency_context"]["unsupported_remainder"],
            [{"text": "Unmodeled rider.", "reason": "unsupported_clause"}],
        )

    def test_alternative_paths_describe_accepted_rules_not_all_matches(self):
        facts = candidate_facts()
        add_alternative_rule_path(facts)
        result = fit(build_candidate_comparisons(facts))
        alpha = candidate_row(result, 1)
        signal = next(item for item in alpha["signals"]
                      if item["signal_id"] == "fit.support.alternative_rule_paths.v1")
        evidence = signal["evidence"][0]["value"]
        self.assertEqual(evidence["matching_semantics"], "any")
        self.assertEqual(len(evidence["acceptable_feature_rule_ids"]), 2)
        self.assertEqual(evidence["matched_feature_rule_ids"], ["effect.lifegain.v1"])
        self.assertNotIn("fit.support.alternative_rule_paths.v1", ids(candidate_row(fit(), 1)))

    def test_title_eligibility_ownership_and_capacity_signals(self):
        result = fit()
        alpha = title_row(result, 1)
        beta = title_row(result, 2)
        self.assertIn("fit.ownership.unknown.v1", ids(alpha))
        self.assertIn("fit.ownership.known.v1", ids(beta))
        ownership = next(item for item in beta["signals"]
                         if item["signal_id"] == "fit.ownership.known.v1")
        self.assertEqual(ownership["evidence"][0]["value"]["owned_copies"], 0)
        self.assertIn("fit.copy.remaining_capacity_positive.v1", ids(beta))
        self.assertIn("fit.eligibility.no_unresolved_dimensions.v1", ids(beta))

        facts = candidate_facts()
        set_title_eligibility(facts, 2, unresolved=["format_legality"])
        set_title_eligibility(facts, 3, playset={
            "status": "unlimited", "current_deck_copies": 0,
            "copy_limit": None, "remaining_capacity": None,
        })
        variant = fit(build_candidate_comparisons(facts))
        self.assertIn("fit.eligibility.unresolved_dimensions_present.v1",
                      ids(title_row(variant, 2)))
        self.assertNotIn("fit.eligibility.no_unresolved_dimensions.v1",
                         ids(title_row(variant, 2)))
        self.assertIn("fit.copy.unlimited.v1", ids(title_row(variant, 3)))

    def test_package_signals_remain_returned_pool_local(self):
        result = fit()
        alpha = candidate_row(result, 1)
        self.assertIn("fit.package.shared_by_all_returned_candidates.v1", ids(alpha))
        self.assertIn("fit.package.not_shared_by_all_returned_candidates.v1", ids(alpha))
        exclusive = next(item for item in alpha["signals"]
                         if item["signal_id"] == "fit.package.pool_local_exclusive_contribution.v1")
        self.assertEqual(exclusive["evidence"][0]["value"], [
            "package.card_advantage.v1", "package.spell_matters.v1",
        ])
        self.assertIn("returned pool", exclusive["explanation"])
        self.assertNotIn("fit.package.same_set_across_returned_pool.v1", ids(alpha))

        facts = candidate_facts()
        make_same_packages(facts)
        same = fit(build_candidate_comparisons(facts))
        for candidate in need_set(same)["candidates"]:
            self.assertIn("fit.package.same_set_across_returned_pool.v1", ids(candidate))

    def test_pool_signals_have_pool_scope_and_candidate_references(self):
        result = fit()
        spell = need_set(result, finding="need.spell_cast.enabler.v1")
        self.assertEqual(
            {item["signal_id"] for item in spell["pool_signals"]},
            {
                "fit.source.pool_truncated.v1",
                "fit.evidence.zone_boundary_unavailable.v1",
            },
        )
        self.assertTrue(all(item["scope"] == "need_pool" for item in spell["pool_signals"]))
        self.assertEqual(
            spell["candidates"][0]["applicable_pool_signal_ids"],
            [item["signal_id"] for item in spell["pool_signals"]],
        )
        self.assertNotIn("fit.source.pool_truncated.v1", ids(spell["candidates"][0]))
        life = need_set(result)
        self.assertNotIn("fit.source.pool_truncated.v1",
                         [item["signal_id"] for item in life["pool_signals"]])
        self.assertIn("fit.evidence.zone_boundary_unavailable.v1",
                      [item["signal_id"] for item in life["pool_signals"]])

    def test_input_reordering_is_deterministic_and_input_unchanged(self):
        source = comparison()
        before = deepcopy(source)
        expected = fit(source)
        self.assertEqual(source, before)
        reordered = deepcopy(source)
        reordered["title_index"].reverse()
        reordered["need_matrices"].reverse()
        for item in reordered["need_matrices"]:
            item["candidates"].reverse()
            item["package_comparison"]["package_ids_by_title"].reverse()
            item["package_comparison"]["pool_local_exclusive_package_ids_by_title"].reverse()
            for dimension in item["dimension_comparisons"]:
                dimension["values"].reverse()
        self.assertEqual(fit(reordered), expected)

    def test_wrong_version_and_pair_projection_are_rejected(self):
        source = comparison()
        source["candidate_comparison_model_version"] = "1"
        with self.assertRaises(ValueError):
            fit(source)
        complete = comparison()
        pair = compare_candidate_pair(
            complete,
            ("main", "need.lifegain.enabler.v1", "dependency.lifegain.v1"),
            1, 2,
        )
        with self.assertRaises(ValueError):
            fit(pair)

    def test_zero_or_negative_remaining_capacity_is_rejected(self):
        for remaining in (0, -1):
            source = comparison()
            title = next(item for item in source["title_index"] if item["title_id"] == 1)
            title["eligibility"]["playset"]["remaining_capacity"] = remaining
            with self.subTest(remaining=remaining), self.assertRaises(ValueError):
                fit(source)

    def test_contradictory_counts_and_need_coverage_are_rejected(self):
        source = comparison()
        source["candidate_title_count"] += 1
        with self.assertRaises(ValueError):
            fit(source)
        source = comparison()
        matrix(source)["candidate_count"] += 1
        with self.assertRaises(ValueError):
            fit(source)
        source = comparison()
        source["title_index"][0]["need_coverage"]["matched_need_count"] += 1
        with self.assertRaises(ValueError):
            fit(source)

    def test_contradictory_need_package_and_support_facts_are_rejected(self):
        mutations = []
        source = comparison()
        matrix(source)["source_need"]["zone"] = "sideboard"
        mutations.append(source)
        source = comparison()
        matrix(source)["package_comparison"]["shared_by_all_package_ids"] = []
        mutations.append(source)
        source = comparison()
        matrix(source)["candidates"][0]["support_context"]["entries"][0][
            "dependency_context"
        ]["availability"] = "sometimes"
        mutations.append(source)
        source = comparison()
        matrix(source)["candidates"][0]["support_context"]["matched_feature_rule_ids"] = [
            "effect.other.v1"
        ]
        mutations.append(source)
        for index, item in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(ValueError):
                fit(item)

    def test_contradictory_eligibility_and_ownership_are_rejected(self):
        mutations = []
        source = comparison()
        title = next(item for item in source["title_index"] if item["title_id"] == 1)
        title["eligibility"]["ownership"] = {"status": "known", "owned_copies": None}
        mutations.append(source)
        source = comparison()
        title = next(item for item in source["title_index"] if item["title_id"] == 1)
        title["eligibility"]["ownership"] = {"status": "unknown", "owned_copies": 0}
        mutations.append(source)
        source = comparison()
        title = next(item for item in source["title_index"] if item["title_id"] == 1)
        title["eligibility_status"] = "eligibility_unknown"
        mutations.append(source)
        source = comparison()
        title = next(item for item in source["title_index"] if item["title_id"] == 1)
        title["eligibility"]["playset"] = {
            "status": "unlimited", "current_deck_copies": 0,
            "copy_limit": 4, "remaining_capacity": None,
        }
        mutations.append(source)
        for index, item in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(ValueError):
                fit(item)

    def test_trigger_absence_does_not_emit_neighboring_signals(self):
        result = fit()
        alpha = candidate_row(result, 1)
        beta = candidate_row(result, 2)
        self.assertIn("fit.support.unconditional.v1", ids(alpha))
        for signal_id in (
            "fit.support.prerequisites_present.v1",
            "fit.support.conditional.v1",
            "fit.support.triggered.v1",
            "fit.support.activated.v1",
            "fit.support.partial.v1",
            "fit.support.unsupported_remainder_present.v1",
            "fit.support.alternative_rule_paths.v1",
        ):
            self.assertNotIn(signal_id, ids(alpha))
        self.assertNotIn("fit.support.unconditional.v1", ids(beta))
        self.assertNotIn("fit.support.activated.v1", ids(beta))
        self.assertNotIn("fit.support.partial.v1", ids(beta))
        self.assertNotIn("fit.package.multiple_contributions.v1", ids(title_row(result, 2)))

    def test_authored_contract_vocabulary_is_non_evaluative(self):
        result = fit()
        authored = []
        authored.extend(result["ordering"].keys())
        authored.extend(result["ordering"].values())
        authored.extend(result["limitations"])
        records = [signal for title in result["title_signal_index"]
                   for signal in title["signals"]]
        records += [signal for pool in result["need_signal_sets"]
                    for signal in pool["pool_signals"]]
        records += [signal for pool in result["need_signal_sets"]
                    for candidate in pool["candidates"] for signal in candidate["signals"]]
        for signal in records:
            authored.extend([
                signal["signal_id"], signal["category"], signal["scope"],
                signal["state"], signal["explanation"],
            ])
        forbidden = (
            "score", "rank", "recommend", "preference", "preferred", "bonus",
            "penalty", "weight", "priority", "importance", "best", "better", "worse",
        )
        serialized = "\n".join(authored).casefold()
        self.assertTrue(all(word not in serialized for word in forbidden))


if __name__ == "__main__":
    unittest.main()
