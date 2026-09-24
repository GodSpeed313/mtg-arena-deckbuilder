from __future__ import annotations

from copy import deepcopy
import sqlite3
from unittest.mock import patch
import unittest

from mtgadb import canonical
from mtgadb.deck_identity import build_deck_snapshot_identity
from mtgadb.model import Card, CardPrinting, Deck, Format
from services.proposal import build_proposal
from services.proposal_presentation import (
    PRESENTATION_IDENTITY_VERSION,
    PROPOSAL_IDENTITY_VERSION,
    PROPOSAL_PRESENTATION_MODEL_VERSION,
    build_proposal_presentation,
    require_proposal_presentation,
)
from services.proposal_policy import build_proposal_policy
from services.validator import DeckRules
from tests.test_proposal import inputs
from tests.test_proposal_policy import policy as policy_spec


class ProposalPresentationTests(unittest.TestCase):
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
        self.format = Format(
            "Test", legal_sets=frozenset({"AAA"}), min_deck_size=0,
            max_deck_size=250, max_sideboard=15, max_command_zone=1,
        )
        self.rules = DeckRules.from_format(self.format)
        self.deck = Deck(
            deck_id="deck.test", name="Test", main={201: 1}, sideboard={301: 1},
        )
        self.addCleanup(self.con.close)

    def accepted(self, *, target="main", deck=None, format=None, rules=None,
                 policy_id="proposal.test.v1", policy_reference="test declaration"):
        context, policy = inputs(target=target)
        baseline = self.deck if deck is None else deck
        if baseline != self.deck:
            identity = build_deck_snapshot_identity(baseline)
            context["analyzed_deck_identity"] = identity
            row = context["contexts"][0]
            row["source_context"]["analyzed_deck_identity"] = identity
            known_ids = {
                item["arena_id"]
                for item in row["returned_candidate_facts"][0]["known_printings"]
            }
            copies = sum(
                zone.get(arena_id, 0)
                for zone in (baseline.main, baseline.sideboard, baseline.commander)
                for arena_id in known_ids
            )
            playset = row["returned_candidate_facts"][0]["eligibility"]["playset"]
            playset["current_deck_copies"] = copies
            if playset["status"] == "capacity_available":
                playset["remaining_capacity"] = playset["copy_limit"] - copies
        if policy_id != "proposal.test.v1" or policy_reference != "test declaration":
            spec = policy_spec()
            spec["target_zone"] = target
            spec["policy_id"] = policy_id
            spec["policy_source"]["provenance"]["reference"] = policy_reference
            policy = build_proposal_policy(spec)
        result = build_proposal(
            context, policy, baseline, self.con,
            format=self.format if format is None else format,
            rules=self.rules if rules is None else rules,
        )
        self.assertEqual((result["status"], result["reason"]), ("accepted", "validated"))
        return result

    def test_deterministic_separate_identities_and_explicit_non_action_semantics(self):
        source = self.accepted()
        first = build_proposal_presentation(source)
        second = build_proposal_presentation(source)
        self.assertEqual(first, second)
        self.assertEqual(PROPOSAL_PRESENTATION_MODEL_VERSION, "1")
        self.assertEqual(first["proposal_presentation_model_version"], "1")
        self.assertEqual(first["proposal_identity"]["proposal_identity_version"],
                         PROPOSAL_IDENTITY_VERSION)
        self.assertEqual(first["presentation_identity"]["presentation_identity_version"],
                         PRESENTATION_IDENTITY_VERSION)
        self.assertNotEqual(first["proposal_identity"]["digest"],
                            first["presentation_identity"]["digest"])
        self.assertEqual(first["review_artifact"]["boundary_semantics"], [
            "validator-accepted", "not-user-approved", "not-applied",
            "not-resource-authorized",
        ])
        self.assertEqual(first["review_artifact"]["validation"][
            "captured_semantics"]["mode"], "unlimited")
        self.assertIn("ownership and wildcard resources not authorized", first[
            "review_artifact"]["validation"]["resource_mode_disclosure"])
        forbidden = {"approval", "authorization", "execution_authorization"}

        def keys(value):
            if isinstance(value, dict):
                return set(value).union(*(keys(item) for item in value.values()))
            if isinstance(value, list):
                return set().union(*(keys(item) for item in value)) if value else set()
            return set()

        self.assertTrue(forbidden.isdisjoint(keys(first)))

    def test_insertion_order_and_deck_metadata_do_not_change_either_identity(self):
        source = self.accepted()
        changed = deepcopy(source)
        deck = changed["proposal"]["proposed_deck"]
        changed["proposal"]["proposed_deck"] = Deck(
            deck_id="other-id", name="Renamed",
            main=dict(reversed(list(deck.main.items()))),
            sideboard=dict(reversed(list(deck.sideboard.items()))),
            commander=dict(reversed(list(deck.commander.items()))),
        )
        original = build_proposal_presentation(source)
        reordered = build_proposal_presentation(changed)
        self.assertEqual(original["proposal_identity"], reordered["proposal_identity"])
        self.assertEqual(original["presentation_identity"], reordered["presentation_identity"])

    def test_validation_mapping_insertion_order_is_not_semantic(self):
        first_format = Format(
            "Test", legal_sets=frozenset({"AAA"}),
            individual_card_quotas={1: 4, 2: 4},
            min_deck_size=0, max_deck_size=250, max_sideboard=15,
            max_command_zone=1,
        )
        second_format = Format(
            "Test", legal_sets=frozenset({"AAA"}),
            individual_card_quotas={2: 4, 1: 4},
            min_deck_size=0, max_deck_size=250, max_sideboard=15,
            max_command_zone=1,
        )
        first = build_proposal_presentation(self.accepted(format=first_format))
        second = build_proposal_presentation(self.accepted(format=second_format))
        self.assertEqual(first["proposal_identity"], second["proposal_identity"])
        self.assertEqual(first["presentation_identity"], second["presentation_identity"])

    def test_semantic_identity_changes_for_baseline_zone_policy_and_validation_context(self):
        baseline = build_proposal_presentation(self.accepted())
        different_deck = Deck(main={201: 2}, sideboard={301: 1})
        changed_baseline = build_proposal_presentation(self.accepted(deck=different_deck))
        changed_zone = build_proposal_presentation(self.accepted(target="sideboard"))
        changed_policy = build_proposal_presentation(self.accepted(
            policy_id="proposal.other.v1", policy_reference="other declaration",
        ))
        changed_rules = build_proposal_presentation(self.accepted(
            rules=DeckRules(
                min_main=0, max_main=249, max_sideboard=15,
                min_commanders=0, max_commanders=1,
            ),
        ))
        digests = {
            item["proposal_identity"]["digest"] for item in (
                baseline, changed_baseline, changed_zone, changed_policy, changed_rules,
            )
        }
        self.assertEqual(len(digests), 5)

    def test_semantic_identity_changes_for_printing_title_need_and_result_state(self):
        source = self.accepted()
        baseline = build_proposal_presentation(source)

        printing = deepcopy(source)
        printing["proposal"]["delta"]["arena_id"] = 102
        proposed = printing["proposal"]["proposed_deck"]
        main = deepcopy(proposed.main)
        del main[101]
        main[102] = 1
        printing["proposal"]["proposed_deck"] = Deck(
            deck_id=proposed.deck_id, name=proposed.name, main=main,
            sideboard=deepcopy(proposed.sideboard), commander=deepcopy(proposed.commander),
        )
        candidate = printing["source_recommendation_context"][
            "returned_candidate_facts"][0]
        candidate["eligible_printing_ids"] = [102]
        candidate["eligibility"]["format_legality"]["eligible_printing_ids"] = [102]

        title = deepcopy(source)
        title["proposal"]["delta"]["title_id"] = 99
        row = title["source_recommendation_context"]
        row["decision"]["candidate"]["title_id"] = 99
        row["returned_candidate_facts"][0]["title_id"] = 99
        row["returned_candidate_facts"][0]["source_candidate_reference"] = (
            "need_matrices[need_key=main|need.lifegain.enabler.v1|"
            "dependency.lifegain.v1].candidates[title_id=99]"
        )

        need = deepcopy(source)
        row = need["source_recommendation_context"]
        for value in (
            need["source_proposal_policy"]["need_key"],
            need["proposal"]["delta"]["need_key"],
            row["need_key"], row["decision"]["need_key"],
        ):
            value["finding_id"] = "need.changed.v1"
        row["source_need"]["finding_id"] = "need.changed.v1"
        row["decision"]["source_need_reference"] = (
            "need_matrices[need_key=main|need.changed.v1|dependency.lifegain.v1].source_need"
        )
        row["returned_candidate_facts"][0]["source_candidate_reference"] = (
            "need_matrices[need_key=main|need.changed.v1|dependency.lifegain.v1]"
            ".candidates[title_id=1]"
        )

        variants = [
            build_proposal_presentation(item) for item in (printing, title, need)
        ]
        self.assertEqual(len({
            baseline["proposal_identity"]["digest"],
            *(item["proposal_identity"]["digest"] for item in variants),
        }), 4)

    def test_display_change_changes_presentation_but_not_semantic_identity(self):
        source = self.accepted()
        changed = deepcopy(source)
        changed["proposal"]["delta"]["name"] = "Displayed Alpha"
        row = changed["source_recommendation_context"]
        row["decision"]["candidate"]["name"] = "Displayed Alpha"
        row["returned_candidate_facts"][0]["name"] = "Displayed Alpha"
        original = build_proposal_presentation(source)
        displayed = build_proposal_presentation(changed)
        self.assertEqual(original["proposal_identity"], displayed["proposal_identity"])
        self.assertNotEqual(original["presentation_identity"],
                            displayed["presentation_identity"])

    def test_warning_is_preserved_and_bound_only_to_presentation(self):
        source = self.accepted()
        warned = deepcopy(source)
        warned["validation"]["warnings"] = [{
            "code": "review_notice", "message": "Review this interaction.",
            "zone": "main", "title_id": 1,
        }]
        plain = build_proposal_presentation(source)
        result = build_proposal_presentation(warned)
        self.assertEqual(result["review_artifact"]["validation"]["evidence"]["warnings"],
                         warned["validation"]["warnings"])
        self.assertEqual(plain["proposal_identity"], result["proposal_identity"])
        self.assertNotEqual(plain["presentation_identity"],
                            result["presentation_identity"])

    def test_exact_result_verification_rejects_unrelated_change(self):
        source = self.accepted()
        changed = deepcopy(source)
        proposed = changed["proposal"]["proposed_deck"]
        changed["proposal"]["proposed_deck"] = Deck(
            deck_id=proposed.deck_id, name=proposed.name,
            main={**proposed.main, 301: 1}, sideboard=deepcopy(proposed.sideboard),
            commander=deepcopy(proposed.commander),
        )
        with self.assertRaisesRegex(ValueError, "exactly baseline plus"):
            build_proposal_presentation(changed)

    def test_tampered_identity_delta_operation_quantity_title_and_need_fail_closed(self):
        source = self.accepted()
        mutations = []
        tampered_identity = deepcopy(source)
        tampered_identity["source_recommendation_context"]["source_context"][
            "analyzed_deck_identity"]["digest"] = "0" * 64
        mutations.append(tampered_identity)
        for field, value in (("operation", "remove"), ("quantity", 2), ("title_id", 99)):
            changed = deepcopy(source)
            changed["proposal"]["delta"][field] = value
            mutations.append(changed)
        wrong_need = deepcopy(source)
        wrong_need["proposal"]["delta"]["need_key"]["finding_id"] = "need.other.v1"
        mutations.append(wrong_need)
        for changed in mutations:
            with self.subTest(changed=changed["proposal"]["delta"]), \
                    self.assertRaises(ValueError):
                build_proposal_presentation(changed)

    def test_contradictory_policy_candidate_and_validation_provenance_fail_closed(self):
        source = self.accepted()
        cases = []
        wrong_policy = deepcopy(source)
        wrong_policy["source_proposal_policy"]["target_zone"] = "sideboard"
        cases.append(wrong_policy)
        wrong_candidate = deepcopy(source)
        wrong_candidate["source_recommendation_context"]["decision"][
            "candidate"]["title_id"] = 99
        cases.append(wrong_candidate)
        wrong_format = deepcopy(source)
        wrong_format["validation_inputs"]["format"] = Format(
            "Other", min_deck_size=0, max_command_zone=1,
        )
        cases.append(wrong_format)
        invalid_evidence = deepcopy(source)
        invalid_evidence["validation"]["valid"] = False
        cases.append(invalid_evidence)
        for changed in cases:
            with self.assertRaises(ValueError):
                build_proposal_presentation(changed)

    def test_contradictory_pool_feature_and_copy_context_fail_closed(self):
        source = self.accepted()
        cases = []
        duplicate = deepcopy(source)
        duplicate["source_recommendation_context"]["returned_candidate_facts"].append(
            deepcopy(duplicate["source_recommendation_context"][
                "returned_candidate_facts"][0])
        )
        duplicate["source_recommendation_context"]["source_pool_summary"]["returned"] += 1
        duplicate["source_recommendation_context"]["decision"][
            "considered_candidate_count"] += 1
        cases.append(duplicate)
        truncated = deepcopy(source)
        truncated["source_recommendation_context"]["pool_context_conditions"] = [{
            "condition_id": "candidate_pool_truncated", "source_path": "tampered",
            "evidence": True,
        }]
        cases.append(truncated)
        feature = deepcopy(source)
        feature["source_recommendation_context"]["returned_candidate_facts"][0][
            "required_feature_eligibility"]["status"] = "not_matched"
        cases.append(feature)
        capacity = deepcopy(source)
        capacity["source_recommendation_context"]["returned_candidate_facts"][0][
            "eligibility"]["playset"]["remaining_capacity"] = 2
        cases.append(capacity)
        for changed in cases:
            with self.assertRaises(ValueError):
                build_proposal_presentation(changed)

    def test_input_unchanged_and_no_external_or_action_calls(self):
        source = self.accepted()
        before = deepcopy(source)
        with patch("services.validator.validate_deck",
                   side_effect=AssertionError("validator")), \
             patch("services.exporter.export_arena_deck",
                   side_effect=AssertionError("export")), \
             patch("mtgadb.snapshot_store.save_snapshot",
                   side_effect=AssertionError("snapshot persistence")), \
             patch("sqlite3.connect", side_effect=AssertionError("database")), \
             patch("builtins.open", side_effect=AssertionError("filesystem")):
            build_proposal_presentation(source)
        self.assertEqual(source, before)

    def test_nonaccepted_and_unexpected_fields_fail_closed(self):
        for status, reason in (("abstained", "validation_failed"),
                               ("rejected", "malformed_input")):
            source = self.accepted()
            source["status"], source["reason"] = status, reason
            source["proposal"] = None
            with self.assertRaises(ValueError):
                build_proposal_presentation(source)
        source = self.accepted()
        source["approval"] = True
        with self.assertRaises(ValueError):
            build_proposal_presentation(source)

    def test_public_verifier_accepts_only_complete_unchanged_v1_artifact(self):
        artifact = build_proposal_presentation(self.accepted())
        before = deepcopy(artifact)
        verified = require_proposal_presentation(artifact)
        self.assertEqual(verified, artifact)
        self.assertIsNot(verified, artifact)
        self.assertEqual(artifact, before)

        for field, value in (
            ("proposal_presentation_model_version", "2"),
            ("source_proposal_model_version", "3"),
            ("status", "accepted"),
            ("reason", "validated"),
        ):
            changed = deepcopy(artifact)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                require_proposal_presentation(changed)

    def test_public_verifier_rejects_identity_and_display_tampering(self):
        artifact = build_proposal_presentation(self.accepted())
        mutations = []
        for path, value in (
            (("proposal_identity", "digest"), "0" * 64),
            (("presentation_identity", "digest"), "0" * 64),
            (("proposal_identity", "proposal_identity_version"), "2"),
            (("presentation_identity", "presentation_identity_version"), "2"),
            (("proposal_identity", "digest_algorithm"), "sha512"),
            (("presentation_identity", "digest_algorithm"), "sha512"),
            (("review_artifact", "proposal", "target_zone"), "sideboard"),
            (("review_artifact", "proposal", "card", "name"), "Altered display"),
            (("review_artifact", "validation", "evidence", "warnings"), [{
                "code": "altered", "message": "Changed warning.",
                "zone": None, "title_id": None,
            }]),
            (("review_artifact", "validation", "resource_mode_disclosure"), "unlimited"),
            (("review_artifact", "limitations"), []),
        ):
            changed = deepcopy(artifact)
            target = changed
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            mutations.append((path, changed))
        for path, changed in mutations:
            with self.subTest(path=path), self.assertRaises(ValueError):
                require_proposal_presentation(changed)

    def test_public_verifier_rejects_rehashed_unrelated_result_and_provenance(self):
        artifact = build_proposal_presentation(self.accepted())

        def rehash(identity):
            import hashlib
            import json
            identity["digest"] = hashlib.sha256(json.dumps(
                identity["canonical_payload"], sort_keys=True,
                separators=(",", ":"), ensure_ascii=False, allow_nan=False,
            ).encode("utf-8")).hexdigest()

        changed_result = deepcopy(artifact)
        semantic = changed_result["proposal_identity"]["canonical_payload"]
        result = semantic["resulting_deck_identity"]
        result["canonical_payload"]["main"].append([301, 1])
        rehash(result)
        rehash(changed_result["proposal_identity"])

        changed_baseline = deepcopy(artifact)
        semantic = changed_baseline["proposal_identity"]["canonical_payload"]
        baseline = semantic["source_baseline_deck_identity"]
        baseline["canonical_payload"]["main"][0][1] += 1
        rehash(baseline)
        rehash(changed_baseline["proposal_identity"])

        changed_policy = deepcopy(artifact)
        semantic = changed_policy["proposal_identity"]["canonical_payload"]
        semantic["recommendation_provenance"]["policy_id"] = "proposal.other.v1"
        rehash(changed_policy["proposal_identity"])

        changed_validation = deepcopy(artifact)
        semantic = changed_validation["proposal_identity"]["canonical_payload"]
        semantic["captured_validation_semantics"]["rules"]["max_main"] = 249
        rehash(changed_validation["proposal_identity"])

        for changed in (
            changed_result, changed_baseline, changed_policy, changed_validation,
        ):
            with self.assertRaises(ValueError):
                require_proposal_presentation(changed)

    def test_public_verifier_rejects_top_level_nested_identity_swap(self):
        artifact = build_proposal_presentation(self.accepted())
        other = build_proposal_presentation(self.accepted(
            policy_id="proposal.other.v1", policy_reference="test declaration",
        ))
        artifact["proposal_identity"] = other["proposal_identity"]
        with self.assertRaises(ValueError):
            require_proposal_presentation(artifact)

    def test_public_verifier_rejects_removed_bound_warning(self):
        source = self.accepted()
        source["validation"]["warnings"] = [{
            "code": "review_notice", "message": "Review this interaction.",
            "zone": "main", "title_id": 1,
        }]
        artifact = build_proposal_presentation(source)
        artifact["review_artifact"]["validation"]["evidence"]["warnings"] = []
        with self.assertRaises(ValueError):
            require_proposal_presentation(artifact)

    def test_public_verifier_rejects_proposal_v2_directly(self):
        with self.assertRaises(ValueError):
            require_proposal_presentation(self.accepted())


if __name__ == "__main__":
    unittest.main()
