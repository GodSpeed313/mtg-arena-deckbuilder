"""#6R contract acceptance, independent of the historical audit outputs."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from mtgadb.model import Card, Deck
from mtgadb.deck_identity import build_deck_snapshot_identity
from services.intelligence import classify_card, analyze_deck
from services.dependencies import dependency_findings, interaction_findings
from services.needs import needs_findings
from services.candidates import discover_candidates
from services.candidate_facts import derive_candidate_facts
from services.candidate_comparison import build_candidate_comparisons, _support_context
from services.strategic_fit import build_strategic_fit_signals
from services.evidence_scope import (
    assessment, context_from_ability, require_context, require_source_need,
    encoded, normalize, source_need, structural_context, semantic_feature,
)
from services.abilities import Ability, Effect, Trigger, Cost, Condition, Qualifier, TokenSpec, UnsupportedRemainder
from services.proposal_policy import build_proposal_policy, build_proposal_policy_v3
from services.proposal import build_scoped_proposal
from services.proposal_presentation import (
    build_proposal_presentation_v2, require_proposal_presentation_v2,
    require_proposal_presentation, _identity,
)
from services.human_proposal_decision import build_human_proposal_decision, require_human_proposal_decision
from tests import test_candidate_comparison as comparison_tests
from tests import test_candidate_facts as facts_tests
from tests import test_deck_review as review_tests
from tests import test_pre_execution_revalidation as revalidation_tests
from tests import test_local_deck_application_intent as intent_tests
from tests import test_local_deck_application as application_tests
from tests import test_local_deck_application_outcome as outcome_tests
from tests.test_human_proposal_decision import decision_spec


def replay(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def row(title, text, types="Sorcery"):
    result = classify_card(Card(title, f"Control {title}", types=types, rules_text=text))
    result["quantity"] = 1
    return result


def life_context(**kwargs):
    return comparison_tests.context(**kwargs)


def inject_presentation(value, presentation):
    """Place the actual new artifact at the proper nested human boundary."""
    if isinstance(value, dict):
        if "human_proposal_decision_model_version" in value:
            value["source_proposal_presentation_model_version"] = "2"
            value["source_presentation"] = deepcopy(presentation)
            value["proposal_identity"] = deepcopy(presentation["proposal_identity"])
            value["presentation_identity"] = deepcopy(presentation["presentation_identity"])
        for item in value.values():
            inject_presentation(item, presentation)
    elif isinstance(value, list):
        for item in value:
            inject_presentation(item, presentation)


class EvidenceScopeTests(unittest.TestCase):
    def fixture(self, cls):
        fixture = cls(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def scoped_presentation(self):
        fixture = self.fixture(review_tests.DeckReviewTests)
        report = fixture.run_review()
        self.assertEqual(report["run_status"], "completed")
        return fixture, report["artifacts"]["presentation"], report

    def test_all_fourteen_affected_records_keep_recognized_effects(self):
        records = json.loads((Path(__file__).parent / "fixtures/evidence_scope_6r.json").read_text(encoding="utf-8"))
        self.assertEqual(len(records), 14)
        self.assertEqual(len({item["title_id"] for item in records}), 11)
        for record in records:
            with self.subTest(card=record["name"], effect=record["dependency_context"]["effect"]["effect_id"]):
                classified = classify_card(Card(record["title_id"], record["name"], types=record["canonical_types"], rules_text=record["canonical_rules_text"]))
                effect_id = record["dependency_context"]["effect"]["effect_id"]
                feature = next(item for item in classified["features"] if item["rule_id"] == record["rule_id"] and
                               (item.get("dependency_context", {}).get("effect") or {}).get("effect_id") == effect_id)
                context = feature["dependency_context"]
                self.assertEqual(context["effect"]["kind"], record["dependency_context"]["effect"]["kind"])
                self.assertEqual(context["origin"]["ability_kind"], "unsupported")
                self.assertEqual(assessment(context)["availability"], "unestablished")
                self.assertIn("unsupported_ability_kind", assessment(context)["reasons"])
        knights = next(item for item in records if item["name"] == "Summon: Knights of Round")
        classified = classify_card(Card(knights["title_id"], knights["name"], types=knights["canonical_types"], rules_text=knights["canonical_rules_text"]))
        routes = [item for item in classified["features"] if item["rule_id"] == "effect.token.v1"]
        self.assertEqual(len(routes), 4)
        self.assertEqual([item["dependency_context"]["origin"]["source_index"] for item in routes], [1, 2, 3, 4])

    def test_a_true_unconditional_and_structural_control(self):
        self.assertEqual(assessment(life_context())["availability"], "unconditional")
        self.assertEqual(assessment(structural_context(["Instant"]))["availability"], "unconditional")
        feature = {"rule_id": "type.spells.v1", "dimension": "theme", "label": "spells", "relationship": "enabler", "evidence": "Instant", "dependency_context": structural_context(["Creature"])}
        with self.assertRaises(ValueError):
            semantic_feature(feature)

    def test_amendment_partial_activated_preserves_effect_and_unparsed_cost(self):
        classified = row(1, "{4}: Put a +1/+1 counter on this creature.", "Creature")
        feature = next(item for item in classified["features"] if item["rule_id"] == "effect.counter.v1")
        context = feature["dependency_context"]
        self.assertEqual(context["origin"]["ability_kind"], "activated")
        self.assertEqual(context["origin"]["parse_status"], "partial")
        self.assertEqual(context["origin"]["costs"], [])
        self.assertEqual(context["origin"]["unsupported_remainder"],
                         [{"text": "{4}", "scope": "cost", "reason": "unsupported_cost"}])
        self.assertEqual(context["effect"]["kind"], "put_counter")
        self.assertEqual(assessment(context)["establishment"], "unestablished")
        self.assertEqual(assessment(context)["availability"], "unestablished")
        self.assertEqual(context, require_context(replay(context)))

    def test_amendment_unrelated_remainder_cannot_explain_missing_cost(self):
        for scope, reason in (("effect", "unsupported_clause"), ("ability", "unsupported_cost"),
                              ("cost", "unsupported_clause")):
            with self.subTest(scope=scope, reason=reason):
                with self.assertRaises(ValueError):
                    life_context(ability_kind="activated", parse_status="partial",
                                 unsupported=[{"text": "Unmodeled source", "scope": scope, "reason": reason}])

    def test_amendment_missing_explanation_and_false_status_rejected(self):
        for status, remainder in (("supported", []), ("partial", []),
                                  ("unsupported", [{"text": "{4}", "scope": "cost", "reason": "unsupported_cost"}])):
            with self.subTest(status=status):
                with self.assertRaises(ValueError):
                    life_context(ability_kind="activated", parse_status=status, unsupported=remainder)
        valid = life_context(ability_kind="activated", parse_status="partial",
                             unsupported=[{"text": "{4}", "scope": "cost", "reason": "unsupported_cost"}])
        valid["effect"]["effect_id"] = "ability.002.effect.001"
        with self.assertRaises(ValueError):
            require_context(valid)

    def test_amendment_established_activated_control_remains_conditional(self):
        context = life_context(ability_kind="activated", costs=[{"kind": "tap", "subject": "self", "timing": "activated"}])
        self.assertEqual(assessment(context)["establishment"], "established")
        self.assertEqual(assessment(context)["availability"], "conditional")

    def test_bcd_conditional_structure_does_not_evaluate_satisfaction(self):
        context = life_context(ability_kind="activated", costs=[{"kind": "tap", "subject": "self", "timing": "activated"}])
        for described_state in ("satisfied", "unsatisfied", "unresolved"):
            with self.subTest(live_state=described_state):
                self.assertEqual(assessment(context)["availability"], "conditional")
                self.assertNotIn("satisfied", assessment(context))
        partial = life_context(ability_kind="activated", parse_status="partial", costs=[{"kind": "tap", "subject": "self", "timing": "activated"}], unsupported=[{"text": "Unknown cost", "scope": "cost", "reason": "unsupported_cost"}])
        self.assertEqual(assessment(partial)["availability"], "unestablished")
        self.assertTrue(assessment(partial)["prerequisites"]["costs"])

    def test_ef_recognized_effect_survives_unsupported_origin(self):
        context = life_context(ability_kind="unsupported")
        self.assertEqual(context["effect"]["kind"], "gain_life")
        self.assertEqual(assessment(context)["reasons"], ["unsupported_ability_kind"])
        unknown = row(1, "Unmodeled instruction.", "Enchantment")
        self.assertFalse(any(item["rule_id"] == "effect.lifegain.v1" for item in unknown["features"]))

    def test_g_reviewed_absence_preserves_scope(self):
        analysis = comparison_tests.originating_analysis()
        finding = next(item for item in analysis["zones"]["main"]["needs"] if item["finding_id"] == "need.lifegain.enabler.v1")
        snapshot = source_need(analysis, "main", finding)
        self.assertEqual(snapshot["missing_side"]["cards"], [])
        self.assertEqual(snapshot["evidence_boundary"]["claim_scope"], "reviewed_features_only")
        self.assertNotIn("rules_text_coverage", snapshot["source_identity"])

    def test_h_mixed_valid_route_is_not_vetoed(self):
        listener = row(3, "Whenever you gain life, draw a card.", "Enchantment")
        uncertain = row(2, "You gain 3 life.", "Planeswalker")
        valid = row(1, "You gain 1 life.")
        only_uncertain = next(item for item in dependency_findings([uncertain, listener]) if item["label"] == "lifegain")
        self.assertEqual(only_uncertain["state"], "support_scope_unestablished")
        self.assertEqual(needs_findings([only_uncertain], {"claim_scope": "reviewed_features_only"})[0]["finding_type"], "unestablished_support_observation")
        self.assertEqual(interaction_findings([uncertain, listener]), [])
        mixed = next(item for item in dependency_findings([valid, uncertain, listener]) if item["label"] == "lifegain")
        self.assertEqual(mixed["state"], "supported")
        self.assertEqual({item["support"] for item in mixed["compatible_pairs"]}, {"unconditional", "unestablished"})

    def test_i_multiple_conditional_routes_remain_plural(self):
        cards = [row(1, "{T}: You gain 1 life.", "Creature"), row(2, "Whenever you cast an instant or sorcery spell, you gain 2 life.", "Creature"), row(3, "Whenever you gain life, draw a card.", "Enchantment")]
        finding = next(item for item in dependency_findings(cards) if item["label"] == "lifegain")
        self.assertEqual(finding["state"], "conditionally_supported")
        self.assertEqual(len(finding["compatible_pairs"]), 2)
        self.assertTrue(all(item["support"] == "conditional" for item in finding["compatible_pairs"]))
        prerequisites = [assessment(item["enabler_evidence"]["dependency_context"])["prerequisites"] for item in finding["compatible_pairs"]]
        self.assertTrue(any(item["costs"] for item in prerequisites))
        self.assertTrue(any(item["trigger"] for item in prerequisites))

    def test_primeval_bounty_and_kraven_controls(self):
        bounty = row(1, "Whenever you cast a creature spell, create a 3/3 green Beast creature token.\nWhenever you cast a noncreature spell, put three +1/+1 counters on target creature you control.\nWhenever a land enters the battlefield under your control, you gain 3 life.", "Enchantment")
        self.assertFalse(any(item["relationship"] == "payoff" for item in bounty["features"]))
        spell = row(2, "Unmodeled instruction.")
        findings = needs_findings(dependency_findings([bounty, spell]), {"claim_scope": "reviewed_features_only"})
        self.assertTrue(any(item["finding_id"] == "opportunity.spell_cast.payoff.v1" for item in findings))
        self.assertFalse(any(item["finding_type"] == "support_need" for item in findings))
        # The exact canonical control is pinned with the affected-card fixture below.
        controls = json.loads((Path(__file__).parent / "fixtures/evidence_controls_6r.json").read_text(encoding="utf-8"))
        kraven = classify_card(Card(**controls["kraven"]))
        recursion = [item for item in kraven["features"] if item["rule_id"] == "effect.recursion.v1"]
        self.assertEqual(len(recursion), 2)
        self.assertTrue(all("dependency_context" not in item for item in recursion))
        self.assertFalse(any(item["dependency_id"].startswith("dependency.recursion") for item in dependency_findings([{**kraven, "quantity": 1}])))

    def test_nested_normalization_detached_idempotent_and_round_trip(self):
        condition = Condition("sacrificed_quality", "sacrificed_creature", "suspected", "If suspected")
        qualifier = Qualifier("once_each_turn", "once", "Only once each turn")
        token = TokenSpec(2, "creature", 1, 1, ("white",), ("Soldier",), False, keywords=("vigilance",))
        effect = Effect("ability.001.effect.001", "create_creature_token", "Create tokens.", token=token, conditions=(condition,), qualifiers=(qualifier,))
        ability = Ability("ability.001", 1, "triggered", "Source", "supported", trigger=Trigger("spell_cast", "instant_or_sorcery", "you", True, "When you cast", (condition,)), qualifiers=(qualifier,))
        context = context_from_ability(ability, effect)
        self.assertEqual(context, require_context(replay(context)))
        self.assertEqual(context, normalize(context))
        self.assertEqual(assessment(context), assessment(replay(context)))
        copy = require_context(context)
        copy["effect"]["token"]["colors"].append("blue")
        self.assertEqual(context["effect"]["token"]["colors"], ["white"])
        tuple_input = deepcopy(context)
        tuple_input["effect"]["token"]["colors"] = ("white",)
        self.assertEqual(require_context(tuple_input), context)

    def test_invalid_origin_records_fail_closed(self):
        mutations = [
            lambda v: v["origin"].update(availability="unconditional"),
            lambda v: v["origin"].update(ability_kind="invented"),
            lambda v: v["origin"].update(parse_status="partial"),
            lambda v: v["origin"].update(source_index=True),
            lambda v: v["origin"].pop("trigger"),
            lambda v: v["origin"].update(costs=None),
            lambda v: v["effect"].update(amount=True),
            lambda v: v["effect"].update(amount=float("nan")),
            lambda v: v["effect"].update(friendly=1),
            lambda v: v["effect"].update(effect_id="ability.002.effect.001"),
            lambda v: v["effect"].update(conditions=set()),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                value = life_context()
                mutate(value)
                with self.assertRaises(ValueError):
                    require_context(value)
        value = life_context(ability_kind="unsupported", parse_status="unsupported", unsupported=[{"text": "Source", "scope": "ability", "reason": "unsupported_clause"}])
        self.assertEqual(assessment(value)["reasons"], ["unsupported_ability_kind", "unsupported_parse_status", "unsupported_remainder_present"])

    def test_canonical_source_snapshots_reject_missing_duplicate_and_bool_counts(self):
        source = deepcopy(comparison_tests.candidate_facts()["per_need"][0]["source_need"])
        self.assertEqual(source, require_source_need(replay(source)))
        mutations = [lambda v: v.pop("evidence_boundary"), lambda v: v.update(required_feature_rule_ids=["effect.lifegain.v1"]), lambda v: v["missing_side"].update(acceptable_feature_rule_ids=["effect.lifegain.v1"] * 2), lambda v: v["existing_side"].update(copy_count=True), lambda v: v["evidence_boundary"].update(unsupported_text_card_copies=True)]
        for mutate in mutations:
            value = deepcopy(source); mutate(value)
            with self.assertRaises(ValueError):
                require_source_need(value)

    def test_facts_replays_candidate_and_separate_analysis(self):
        fixture = self.fixture(facts_tests.CandidateFactsTests)
        analysis, pools = fixture.candidates()
        fresh = derive_candidate_facts(pools, fixture.con, source_analysis=analysis)
        reloaded = derive_candidate_facts(replay(pools), fixture.con, source_analysis=replay(analysis))
        self.assertEqual(fresh, reloaded)
        self.assertEqual(build_candidate_comparisons(fresh), build_candidate_comparisons(replay(fresh)))
        self.assertEqual(analysis, replay(analysis))
        tuples = deepcopy(pools)
        for pool in tuples["pools"]:
            for candidate in pool["candidates"]:
                for feature in candidate["matching_feature_evidence"]:
                    context = feature.get("dependency_context")
                    if context and context["origin"]["kind"] == "ability":
                        context["origin"]["costs"] = tuple(context["origin"]["costs"])
        self.assertEqual(fresh, derive_candidate_facts(tuples, fixture.con, source_analysis=analysis))
        false_count = deepcopy(pools)
        false_count["pools"][0]["candidates"][0]["source_need"]["existing_side"]["copy_count"] = True
        with self.assertRaises(ValueError):
            derive_candidate_facts(false_count, fixture.con, source_analysis=analysis)

    def test_source_mismatch_matrix_against_separate_analysis(self):
        fixture = self.fixture(facts_tests.CandidateFactsTests)
        analysis, pools = fixture.candidates()
        mutations = [
            lambda s: s.update(zone="sideboard"), lambda s: s.update(finding_id="need.invalid.v1"),
            lambda s: s.update(dependency_id="dependency.spell_cast.v1"),
            lambda s: s.update(dependency_state="supported"), lambda s: s.update(dependency_policy="optional_support"),
            lambda s: s["source_identity"].update(analysis_version="4"),
            lambda s: s["source_identity"].update(analyzed_deck_identity=build_deck_snapshot_identity(Deck(main={101: 2}))),
            lambda s: s["evidence_boundary"].update(unsupported_text_card_copies=s["evidence_boundary"]["unsupported_text_card_copies"] + 1),
            lambda s: s["missing_side"].update(acceptable_feature_rule_ids=["type.spells.v1"]),
            lambda s: s.update(existing_side=deepcopy(s["missing_side"])),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                bad = deepcopy(pools); mutate(bad["pools"][0]["source_need"])
                for candidate in bad["pools"][0]["candidates"]:
                    candidate["source_need"] = deepcopy(bad["pools"][0]["source_need"])
                with self.assertRaises(ValueError):
                    derive_candidate_facts(bad, fixture.con, source_analysis=analysis)
        wrong = deepcopy(pools)
        wrong["analyzed_deck_identity"] = build_deck_snapshot_identity(Deck(main={101: 2}))
        with self.assertRaises(ValueError):
            derive_candidate_facts(wrong, fixture.con, source_analysis=analysis)

    def test_comparison_and_context_reject_inconsistent_scope_and_assessment(self):
        facts = comparison_tests.candidate_facts()
        bad = deepcopy(facts)
        bad["per_need"][0]["candidates"][0]["source_need"]["evidence_boundary"]["unsupported_text_card_copies"] += 1
        with self.assertRaises(ValueError):
            build_candidate_comparisons(bad)
        comparison = build_candidate_comparisons(facts)
        comparison["need_matrices"][0]["candidates"][0]["support_context"]["entries"][0]["assessment"]["availability"] = "conditional"
        with self.assertRaises(ValueError):
            build_strategic_fit_signals(comparison)

    def test_unknown_source_positive_progression_and_presentation_round_trip(self):
        fixture, presentation, report = self.scoped_presentation()
        comparison = report["artifacts"]["comparison"]
        self.assertEqual(comparison["need_matrices"][0]["evidence_completeness"]["status"], "unknown")
        self.assertEqual(report["artifacts"]["recommendation"]["decisions"][0]["outcome"], "recommendable")
        self.assertEqual(report["artifacts"]["proposal_result_projection"]["status"], "accepted")
        self.assertEqual(presentation["proposal_presentation_model_version"], "2")
        self.assertEqual(presentation, require_proposal_presentation_v2(replay(presentation)))
        self.assertEqual(set(presentation["review_artifact"]["evidence_context"]), {"source_need", "candidate_title_id", "matching_feature_evidence", "support_context", "source_evidence_completeness"})

    def test_uncertain_selected_candidate_and_mixed_routes_still_progress(self):
        fixture = self.fixture(review_tests.DeckReviewTests)
        for text, expected in (("You gain 1 life. Unmodeled rider.", ["unestablished"]),
                               ("You gain 1 life.\nYou gain 2 life. Unmodeled rider.", ["unconditional", "unestablished"])):
            with self.subTest(text=text):
                fixture.sql("UPDATE cards SET rules_text=? WHERE title_id=2", (text,))
                report = fixture.run_review()
                self.assertEqual(report["artifacts"]["recommendation"]["decisions"][0]["outcome"], "recommendable")
                presentation = report["artifacts"]["presentation"]
                self.assertEqual(require_proposal_presentation_v2(presentation), presentation)
                support = presentation["review_artifact"]["evidence_context"]["support_context"]
                self.assertEqual([entry["assessment"]["availability"] for entry in support["entries"]], expected)
                self.assertEqual(len(support["unresolved"]), 1)

    def test_context_round_trip_and_policy_change_changes_action_identity(self):
        fixture = self.fixture(review_tests.DeckReviewTests)
        captured = []
        original = review_tests.review.build_scoped_proposal
        def capture(*args, **kwargs):
            result = original(*args, **kwargs)
            captured.append(result)
            return result
        with patch.object(review_tests.review, "build_scoped_proposal", side_effect=capture):
            report = fixture.run_review()
        context = report["artifacts"]["recommendation_context"]
        from services.recommendation_context import build_recommendation_context
        self.assertEqual(context, build_recommendation_context(replay(report["artifacts"]["recommendation"]), replay(report["artifacts"]["comparison"])))
        result = captured[0]
        first = build_proposal_presentation_v2(result)
        changed = deepcopy(result)
        changed["source_proposal_policy"]["policy_id"] += ".explicit-second-declaration"
        second = build_proposal_presentation_v2(changed)
        self.assertEqual(require_proposal_presentation_v2(second), second)
        self.assertNotEqual(first["proposal_identity"]["digest"], second["proposal_identity"]["digest"])
        self.assertEqual(first["review_artifact"]["proposal"], second["review_artifact"]["proposal"])

    def test_scoped_producer_preserves_baseline_context_and_validator_gates(self):
        from tests import test_proposal as proposal_tests
        from tests import test_recommendation_context as context_tests
        from services.recommendation_context import build_recommendation_context
        from tests.test_proposal_policy import policy
        fixture = self.fixture(proposal_tests.ProposalTests)
        facts = comparison_tests.candidate_facts()
        context_tests.set_eligible_ids(facts, 1, [101])
        from tests.test_preference_policy import rule
        decisions, comparison = context_tests.models([rule()], facts=facts)
        context = build_recommendation_context(decisions, comparison)
        spec = policy()
        spec["target_zone"] = "main"
        declaration = build_proposal_policy_v3(spec)
        def produce(ctx=context, deck=fixture.deck, rules=fixture.rules, format=fixture.format):
            return build_scoped_proposal(ctx, declaration, deck, fixture.con, format=format, rules=rules)
        self.assertEqual(produce()["reason"], "validated")
        self.assertEqual(produce(deck=Deck(main={201: 2}, sideboard={301: 1}))["reason"], "baseline_snapshot_mismatch")
        bad = deepcopy(context)
        bad["contexts"][0]["returned_candidate_facts"][0]["eligibility"]["playset"]["current_deck_copies"] = 1
        bad["contexts"][0]["returned_candidate_facts"][0]["eligibility"]["playset"]["remaining_capacity"] = 3
        self.assertEqual(produce(ctx=bad)["reason"], "baseline_context_mismatch")
        bad = deepcopy(context)
        bad["contexts"][0]["returned_candidate_facts"][0]["support_context"]["entries"][0]["assessment"]["availability"] = "conditional"
        self.assertEqual(produce(ctx=bad)["reason"], "malformed_input")
        self.assertEqual(produce(format=replace(fixture.format, max_deck_size=1), rules=replace(fixture.rules, max_main=1))["reason"], "validation_failed")

    def test_scope_only_change_binds_presentation_not_action_identity(self):
        fixture, presentation, report = self.scoped_presentation()
        changed = deepcopy(presentation)
        evidence = changed["review_artifact"]["evidence_context"]
        evidence["source_need"]["evidence_boundary"]["unsupported_text_card_copies"] += 1
        evidence["source_evidence_completeness"]["source_evidence_boundary"] = deepcopy(evidence["source_need"]["evidence_boundary"])
        changed["presentation_identity"] = _identity("presentation_identity_version", "1", {"proposal_identity": changed["proposal_identity"], "review_artifact": changed["review_artifact"]})
        self.assertEqual(require_proposal_presentation_v2(changed), changed)
        self.assertEqual(changed["proposal_identity"], presentation["proposal_identity"])
        self.assertNotEqual(changed["presentation_identity"]["digest"], presentation["presentation_identity"]["digest"])
        self.assertEqual(changed["review_artifact"]["source_baseline_deck_identity"], presentation["review_artifact"]["source_baseline_deck_identity"])
        self.assertEqual(set(presentation["presentation_identity"]["canonical_payload"]), {"proposal_identity", "review_artifact"})
        self.assertNotIn("evidence_context", presentation["proposal_identity"]["canonical_payload"])

    def test_recomputed_inconsistent_scope_and_routes_are_rejected(self):
        fixture, presentation, report = self.scoped_presentation()
        for field in ("boundary", "assessment", "title"):
            bad = deepcopy(presentation); evidence = bad["review_artifact"]["evidence_context"]
            if field == "boundary": evidence["source_need"]["evidence_boundary"]["unsupported_text_card_copies"] += 1
            elif field == "assessment": evidence["support_context"]["entries"][0]["assessment"]["availability"] = "conditional"
            else: evidence["candidate_title_id"] += 1
            bad["presentation_identity"] = _identity("presentation_identity_version", "1", {"proposal_identity": bad["proposal_identity"], "review_artifact": bad["review_artifact"]})
            with self.assertRaises(ValueError):
                require_proposal_presentation_v2(bad)

    def test_policy_2_stays_context_2_and_policy_3_requires_context_3(self):
        from tests.test_proposal_policy import policy
        self.assertEqual(build_proposal_policy(policy())["required_recommendation_context_model_version"], "2")
        self.assertEqual(build_proposal_policy_v3(policy())["required_recommendation_context_model_version"], "3")
        _, presentation, _ = self.scoped_presentation()
        self.assertEqual(presentation["proposal_identity"]["canonical_payload"]["source_proposal_policy"]["proposal_policy_model_version"], "3")

    def test_actual_presentation_2_rejected_by_legacy_human_boundary(self):
        _, presentation, _ = self.scoped_presentation()
        with self.assertRaises(ValueError): require_proposal_presentation(presentation)
        with self.assertRaises(ValueError): build_human_proposal_decision(presentation, decision_spec())
        fixture = self.fixture(revalidation_tests.PreExecutionRevalidationTests)
        self.assertEqual(require_proposal_presentation(fixture.presentation), fixture.presentation)
        bad = deepcopy(fixture.decision); inject_presentation(bad, presentation)
        with self.assertRaises(ValueError): require_human_proposal_decision(bad)
        result = fixture.evaluate(human_decision=bad)
        self.assertEqual((result["status"], result["reason"]), ("rejected", "unsupported_version"))
        self.assertIsNone(result["pre_execution_revalidation_identity"])

    def test_nested_new_presentation_cannot_build_intent_or_mutate_store(self):
        _, presentation, _ = self.scoped_presentation()
        fixture = self.fixture(intent_tests.LocalDeckApplicationIntentTests)
        bad = deepcopy(fixture.revalidation); inject_presentation(bad, presentation)
        before = hashlib.sha256(fixture.path.read_bytes()).hexdigest()
        with self.assertRaises(ValueError): fixture.build(revalidation=bad)
        self.assertEqual(hashlib.sha256(fixture.path.read_bytes()).hexdigest(), before)
        fixture = self.fixture(application_tests.LocalDeckApplicationTests)
        bad = deepcopy(fixture.intent); inject_presentation(bad, presentation)
        before = hashlib.sha256(fixture.path.read_bytes()).hexdigest()
        from services.local_execution_validation import LocalDeckApplicationError
        with self.assertRaises(LocalDeckApplicationError): fixture.apply(intent=bad)
        self.assertEqual(hashlib.sha256(fixture.path.read_bytes()).hexdigest(), before)
        self.assertEqual(fixture.read()["revision"], 1)

    def test_nested_new_presentation_cannot_prepare_receipt_or_recovery(self):
        _, presentation, _ = self.scoped_presentation()
        fixture = self.fixture(outcome_tests.LocalDeckApplicationOutcomeTests)
        bad = deepcopy(fixture.intent); inject_presentation(bad, presentation)
        before = hashlib.sha256(fixture.path.read_bytes()).hexdigest()
        from services.local_execution_validation import LocalDeckApplicationError
        with self.assertRaises(LocalDeckApplicationError): fixture.prepare(intent=bad)
        from services import local_deck_application_outcome as outcome
        with self.assertRaises(LocalDeckApplicationError): outcome.recover_local_deck_application(bad, store_path=fixture.path)
        self.assertEqual(hashlib.sha256(fixture.path.read_bytes()).hexdigest(), before)
        operation = fixture.prepare()
        inject_presentation(operation, presentation)
        before = hashlib.sha256(fixture.path.read_bytes()).hexdigest()
        with self.assertRaises(outcome.LocalDeckApplicationOutcomeError): fixture.execute(operation)
        self.assertEqual(hashlib.sha256(fixture.path.read_bytes()).hexdigest(), before)
