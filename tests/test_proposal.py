from __future__ import annotations

from copy import deepcopy
import sqlite3
from unittest.mock import patch
import unittest

from mtgadb import canonical
from mtgadb.deck_identity import build_deck_snapshot_identity
from mtgadb.model import Card, CardPrinting, Deck, Format
from services.proposal import PROPOSAL_MODEL_VERSION, build_proposal
from services.proposal_policy import build_proposal_policy
from services.recommendation_context import build_recommendation_context
from services.validator import DeckRules, ValidationReport, validate_deck
from tests.test_candidate_comparison import candidate_facts
from tests.test_preference_policy import rule
from tests.test_proposal_policy import policy as policy_spec
from tests.test_recommendation_context import models, set_eligible_ids


def inputs(*, target="main", eligible_ids=None):
    facts = candidate_facts()
    set_eligible_ids(facts, 1, [101] if eligible_ids is None else eligible_ids)
    decisions, comparison = models([rule()], facts=facts)
    context = build_recommendation_context(decisions, comparison)
    spec = policy_spec()
    spec["target_zone"] = target
    return context, build_proposal_policy(spec)


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(canonical.SCHEMA)
        canonical.load_cards(self.con, [
            Card(1, "Alpha", color_identity="W", rules_text="You gain 1 life."),
            Card(2, "Beta", color_identity="W", rules_text="You gain 2 life."),
            Card(3, "Gamma", color_identity="W", rules_text="You gain 3 life."),
        ])
        canonical.load_printings(self.con, [
            CardPrinting(101, 1, "AAA", "1", "common"),
            CardPrinting(102, 1, "BBB", "2", "rare"),
            CardPrinting(201, 2, "AAA", "3", "common"),
            CardPrinting(301, 3, "AAA", "4", "common"),
        ])
        self.format = Format("Test", legal_sets=frozenset({"AAA"}),
                             min_deck_size=0, max_deck_size=250,
                             max_sideboard=15, max_command_zone=1)
        self.rules = DeckRules.from_format(self.format)
        self.deck = Deck(deck_id="deck.test", name="Test", main={201: 1},
                         sideboard={301: 1})
        self.addCleanup(self.con.close)

    def run_proposal(self, context=None, policy=None, deck=None, *, format=None,
                     rules=None, con=None):
        if context is None or policy is None:
            source, declaration = inputs()
            context = source if context is None else context
            policy = declaration if policy is None else policy
        return build_proposal(
            context, policy, self.deck if deck is None else deck,
            self.con if con is None else con,
            format=self.format if format is None else format,
            rules=self.rules if rules is None else rules,
        )

    def test_valid_add_one_new_printing_and_full_provenance(self):
        context, policy = inputs()
        before = deepcopy(self.deck)
        self.assertEqual(
            context["analyzed_deck_identity"],
            build_deck_snapshot_identity(self.deck),
        )
        result = self.run_proposal(context, policy)
        self.assertEqual(PROPOSAL_MODEL_VERSION, "2")
        self.assertEqual((result["status"], result["reason"]), ("accepted", "validated"))
        self.assertEqual(result["proposal"]["delta"], {
            "operation": "add", "arena_id": 101, "zone": "main", "quantity": 1,
            "title_id": 1, "name": "Alpha", "need_key": policy["need_key"],
        })
        self.assertEqual(result["proposal"]["proposed_deck"].main, {201: 1, 101: 1})
        self.assertEqual(result["proposal"]["proposed_deck"].sideboard, {301: 1})
        self.assertEqual(result["proposal"]["proposed_deck"].commander, {})
        self.assertEqual(result["proposal"]["proposed_deck"].deck_id, self.deck.deck_id)
        self.assertIsNot(result["proposal"]["proposed_deck"], self.deck)
        self.assertEqual(self.deck, before)
        self.assertEqual(result["source_proposal_policy"], policy)
        self.assertEqual(result["source_recommendation_context"], context["contexts"][0])
        self.assertTrue(result["validation"]["valid"])
        self.assertEqual(result["validation"]["errors"], [])
        self.assertEqual(result["validation_inputs"], {
            "mode": "unlimited", "format": self.format, "rules": self.rules,
        })

    def test_increment_existing_printing_without_cross_zone_changes(self):
        context, policy = inputs(target="sideboard")
        deck = Deck(main={201: 1}, sideboard={101: 1, 301: 1}, commander={})
        identity = build_deck_snapshot_identity(deck)
        context["analyzed_deck_identity"] = identity
        context["contexts"][0]["source_context"]["analyzed_deck_identity"] = identity
        context["contexts"][0]["returned_candidate_facts"][0]["eligibility"]["playset"] = {
            "status": "capacity_available", "current_deck_copies": 1,
            "copy_limit": 4, "remaining_capacity": 3,
        }
        before = deepcopy(deck)
        result = self.run_proposal(context, policy, deck)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["proposal"]["proposed_deck"].sideboard, {101: 2, 301: 1})
        self.assertEqual(result["proposal"]["proposed_deck"].main, {201: 1})
        self.assertEqual(deck, before)

    def test_all_three_explicit_target_zones_and_no_zone_inference(self):
        for zone in ("main", "sideboard", "commander"):
            with self.subTest(zone=zone):
                context, policy = inputs(target=zone)
                self.assertEqual(policy["need_key"]["zone"], "main")
                result = self.run_proposal(context, policy)
                self.assertEqual(result["status"], "accepted")
                self.assertEqual(result["proposal"]["delta"]["zone"], zone)
                proposed = result["proposal"]["proposed_deck"]
                self.assertEqual(getattr(proposed, zone).get(101), 1)
                for other in {"main", "sideboard", "commander"} - {zone}:
                    self.assertEqual(getattr(proposed, other), getattr(self.deck, other))

    def test_need_mismatch_and_positive_identity_contradiction(self):
        context, policy = inputs()
        bad = deepcopy(policy)
        bad["need_key"]["finding_id"] = "need.other.v1"
        self.assertEqual(self.run_proposal(context, bad)["reason"], "policy_context_mismatch")
        spec = policy_spec()
        spec["need_key"]["finding_id"] = "need.other.v1"
        other_policy = build_proposal_policy(spec)
        self.assertEqual(self.run_proposal(context, other_policy)["reason"],
                         "policy_context_mismatch")
        bad = deepcopy(context)
        bad["contexts"][0]["decision"]["candidate"]["title_id"] = 999
        self.assertEqual(self.run_proposal(bad, policy)["reason"],
                         "policy_context_mismatch")

    def test_every_negative_recommendation_is_non_actionable(self):
        for outcome in (
            "top_tie", "indeterminate_ordering", "inconsistent_ordering",
            "no_declared_preference", "policy_not_applicable", "no_candidates",
            "single_candidate_no_preference", "unresolved_eligibility",
        ):
            with self.subTest(outcome=outcome):
                context, policy = inputs()
                row = context["contexts"][0]["decision"]
                row["outcome"] = outcome
                row["candidate"] = None
                result = self.run_proposal(context, policy)
                self.assertEqual((result["status"], result["reason"]),
                                 ("abstained", "recommendation_not_positive"))
                self.assertIsNone(result["proposal"])

    def test_truncation_unresolved_and_printing_cardinality_abstain(self):
        context, policy = inputs()
        context["contexts"][0]["source_pool_summary"]["truncated"] = True
        context["contexts"][0]["pool_context_conditions"] = [{
            "condition_id": "candidate_pool_truncated",
            "source_path": "need_matrices[need_key=main|need.lifegain.enabler.v1|dependency.lifegain.v1].source_pool_summary.truncated",
            "evidence": True,
        }]
        result = self.run_proposal(context, policy)
        self.assertEqual(result["reason"], "candidate_pool_truncated")
        context, policy = inputs()
        candidate = context["contexts"][0]["returned_candidate_facts"][0]
        candidate["unresolved_eligibility"] = ["format_legality"]
        candidate["eligibility_status"] = "eligibility_unknown"
        self.assertEqual(self.run_proposal(context, policy)["reason"],
                         "unresolved_eligibility")
        for ids in ([], [101, 102]):
            with self.subTest(ids=ids):
                context, policy = inputs(eligible_ids=ids)
                result = self.run_proposal(context, policy)
                self.assertEqual(result["reason"], "printing_cardinality_mismatch")
                self.assertIsNone(result["proposal"])

    def test_unsupported_policy_operations_quantities_and_modes_reject(self):
        context, policy = inputs()
        for field, value, reason in (
            ("operation", "swap", "unsupported_operation"),
            ("quantity", 2, "unsupported_quantity"),
            ("resource_mode", "wildcard_budget", "unsupported_resource_mode"),
            ("resource_mode", "full_collection", "unsupported_resource_mode"),
        ):
            with self.subTest(field=field, value=value):
                bad = deepcopy(policy)
                bad[field] = value
                result = self.run_proposal(context, bad)
                self.assertEqual(result["reason"], reason)
                self.assertIsNone(result["proposal"])

    def test_deck_size_failure_retains_evidence_and_never_repairs(self):
        context, policy = inputs()
        restrictive = DeckRules(min_main=0, max_main=1, max_sideboard=15,
                                max_commanders=1)
        before = deepcopy(self.deck)
        with patch("services.proposal.validate_deck", wraps=validate_deck) as validate:
            result = self.run_proposal(context, policy, rules=restrictive)
        self.assertEqual(validate.call_count, 1)
        self.assertEqual((result["status"], result["reason"]),
                         ("abstained", "validation_failed"))
        self.assertIsNone(result["proposal"])
        self.assertFalse(result["validation"]["valid"])
        self.assertEqual(result["validation_inputs"]["rules"], restrictive)
        self.assertIn("main_size", {item["code"] for item in result["validation"]["errors"]})
        self.assertEqual(result["evidence"]["operation"], "add")
        self.assertEqual(self.deck, before)

    def test_copy_limit_failure_does_not_change_quantity_or_printing(self):
        context, policy = inputs()
        deck = Deck(main={101: 3, 201: 1})
        identity = build_deck_snapshot_identity(deck)
        context["analyzed_deck_identity"] = identity
        context["contexts"][0]["source_context"]["analyzed_deck_identity"] = identity
        context["contexts"][0]["returned_candidate_facts"][0]["eligibility"]["playset"] = {
            "status": "capacity_available", "current_deck_copies": 3,
            "copy_limit": 4, "remaining_capacity": 1,
        }
        restrictive = DeckRules(min_main=0, max_main=250, max_sideboard=15,
                                max_commanders=1, copy_limit=3)
        before = deepcopy(deck)
        result = self.run_proposal(context, policy, deck, rules=restrictive)
        self.assertEqual(result["reason"], "validation_failed")
        self.assertIn("copy_limit", {item["code"] for item in result["validation"]["errors"]})
        self.assertEqual(result["evidence"]["arena_id"], 101)
        self.assertEqual(result["evidence"]["quantity"], 1)
        self.assertIsNone(result["proposal"])
        self.assertEqual(deck, before)

    def test_malformed_baseline_and_stale_snapshot_reject(self):
        context, policy = inputs()
        malformed = Deck(main={201: -1})
        before = deepcopy(malformed)
        result = self.run_proposal(context, policy, malformed)
        self.assertEqual(result["reason"], "malformed_baseline")
        self.assertEqual(malformed, before)
        stale = Deck(main={301: 1}, sideboard={201: 1})
        candidate = context["contexts"][0]["returned_candidate_facts"][0]
        known_ids = {item["arena_id"] for item in candidate["known_printings"]}
        self.assertEqual(candidate["eligibility"]["playset"]["current_deck_copies"], 0)
        self.assertEqual(sum(stale.main.get(item, 0) + stale.sideboard.get(item, 0)
                             + stale.commander.get(item, 0) for item in known_ids), 0)
        result = self.run_proposal(context, policy, stale)
        self.assertEqual((result["status"], result["reason"]),
                         ("abstained", "baseline_snapshot_mismatch"))

    def test_snapshot_identity_provenance_fails_closed(self):
        context, policy = inputs()
        missing = deepcopy(context)
        del missing["analyzed_deck_identity"]
        self.assertEqual(self.run_proposal(missing, policy)["reason"], "malformed_input")

        contradictory = deepcopy(context)
        contradictory["contexts"][0]["source_context"]["analyzed_deck_identity"] = (
            build_deck_snapshot_identity(Deck(main={999: 1}))
        )
        result = self.run_proposal(contradictory, policy)
        self.assertEqual((result["status"], result["reason"]),
                         ("rejected", "malformed_input"))

    def test_metadata_only_baseline_change_remains_a_matching_snapshot(self):
        context, policy = inputs()
        renamed = Deck(
            deck_id="another-id",
            name="Renamed",
            main=deepcopy(self.deck.main),
            sideboard=deepcopy(self.deck.sideboard),
            commander=deepcopy(self.deck.commander),
        )
        result = self.run_proposal(context, policy, renamed)
        self.assertEqual((result["status"], result["reason"]), ("accepted", "validated"))
        self.assertEqual(result["proposal"]["proposed_deck"].deck_id, "another-id")
        self.assertEqual(result["proposal"]["proposed_deck"].name, "Renamed")

    def test_format_rules_and_connection_are_explicit(self):
        context, policy = inputs()
        mismatch = Format("Other", min_deck_size=0)
        self.assertEqual(self.run_proposal(context, policy, format=mismatch)["reason"],
                         "policy_context_mismatch")
        result = build_proposal(context, policy, self.deck, None,
                                format=self.format, rules=self.rules)
        self.assertEqual(result["reason"], "validation_unavailable")
        result = build_proposal(context, policy, self.deck, self.con,
                                format=None, rules=self.rules)
        self.assertEqual(result["reason"], "validation_unavailable")
        result = build_proposal(context, policy, self.deck, self.con,
                                format=self.format, rules=None)
        self.assertEqual(result["reason"], "validation_unavailable")
        closed = sqlite3.connect(":memory:")
        closed.close()
        result = build_proposal(context, policy, self.deck, closed,
                                format=self.format, rules=self.rules)
        self.assertEqual(result["reason"], "validation_unavailable")

    def test_copy_capacity_and_context_conditions_fail_closed(self):
        context, policy = inputs()
        bad = deepcopy(context)
        bad["contexts"][0]["returned_candidate_facts"][0]["eligibility"]["playset"][
            "remaining_capacity"] = 0
        self.assertEqual(self.run_proposal(bad, policy)["reason"], "malformed_input")
        bad = deepcopy(context)
        bad["contexts"][0]["pool_context_conditions"] = [{
            "condition_id": "candidate_pool_truncated", "source_path": "wrong", "evidence": True,
        }]
        self.assertEqual(self.run_proposal(bad, policy)["reason"], "malformed_input")
        bad = deepcopy(context)
        bad["contexts"][0]["returned_candidate_facts"][0][
            "source_candidate_reference"] = "wrong"
        self.assertEqual(self.run_proposal(bad, policy)["reason"],
                         "policy_context_mismatch")

    def test_no_database_query_outside_validator(self):
        context, policy = inputs()
        statements = []
        self.con.set_trace_callback(statements.append)
        try:
            with patch("services.proposal.validate_deck",
                       return_value=ValidationReport()) as validate:
                result = self.run_proposal(context, policy)
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(validate.call_count, 1)
            self.assertEqual(statements, [])
        finally:
            self.con.set_trace_callback(None)

    def test_versions_provenance_and_pool_contradictions_fail_closed(self):
        context, policy = inputs()
        for field in ("recommendation_context_model_version",
                      "source_candidate_ordering_model_version"):
            bad = deepcopy(context)
            bad[field] = "1"
            self.assertEqual(self.run_proposal(bad, policy)["reason"], "malformed_input")
        bad = deepcopy(policy)
        bad["proposal_policy_model_version"] = "1"
        self.assertEqual(self.run_proposal(context, bad)["reason"], "malformed_input")
        bad = deepcopy(context)
        bad["contexts"][0]["decision"]["policy_source"] = None
        self.assertEqual(self.run_proposal(bad, policy)["reason"], "policy_context_mismatch")
        bad = deepcopy(context)
        bad["contexts"][0]["source_pool_summary"]["returned"] = 99
        self.assertEqual(self.run_proposal(bad, policy)["reason"], "malformed_input")

    def test_deterministic_inputs_unchanged_and_no_other_engine_calls(self):
        context, policy = inputs()
        before = deepcopy((context, policy, self.deck))
        with patch("services.candidates.discover_candidates",
                   side_effect=AssertionError("rediscovery")), \
             patch("services.candidate_ordering.build_candidate_ordering",
                   side_effect=AssertionError("reranking")), \
             patch("services.exporter.export_arena_deck",
                   side_effect=AssertionError("export")):
            first = self.run_proposal(context, policy)
            self.assertEqual(first, self.run_proposal(context, policy))
        self.assertEqual((context, policy, self.deck), before)
        self.assertTrue(set(first["proposal"]).isdisjoint({
            "remove", "replacement", "crafting", "wildcards", "saved_deck",
        }))


if __name__ == "__main__":
    unittest.main()
