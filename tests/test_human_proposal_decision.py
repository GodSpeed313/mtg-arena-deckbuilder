from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from unittest.mock import patch
import unittest

from services.human_proposal_decision import (
    HUMAN_PROPOSAL_DECISION_IDENTITY_VERSION,
    HUMAN_PROPOSAL_DECISION_MODEL_VERSION,
    build_human_proposal_decision,
    require_human_proposal_decision,
)
from services.proposal_presentation import build_proposal_presentation
from tests import test_proposal_presentation as presentation_tests


def decision_spec(decision="approved", kind="explicit_user", reference="review 42"):
    return {
        "decision": decision,
        "decision_source": {
            "kind": kind,
            "provenance": {"reference": reference},
        },
    }


class HumanProposalDecisionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = presentation_tests.ProposalPresentationTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.proposal_v2 = self.fixture.accepted()
        self.presentation = build_proposal_presentation(self.proposal_v2)

    def test_approved_and_declined_are_complete_successful_decisions(self):
        approved = build_human_proposal_decision(
            self.presentation, decision_spec("approved"),
        )
        declined = build_human_proposal_decision(
            self.presentation, decision_spec("declined"),
        )
        expected_fields = {
            "human_proposal_decision_model_version",
            "source_proposal_presentation_model_version", "decision",
            "decision_source", "proposal_identity", "presentation_identity",
            "source_presentation", "human_proposal_decision_identity", "limitations",
        }
        self.assertEqual(set(approved), expected_fields)
        self.assertEqual(approved["decision"], "approved")
        self.assertEqual(declined["decision"], "declined")
        self.assertEqual(approved["human_proposal_decision_model_version"], "1")
        self.assertEqual(approved["source_proposal_presentation_model_version"], "1")
        self.assertEqual(HUMAN_PROPOSAL_DECISION_MODEL_VERSION, "1")
        self.assertEqual(
            approved["human_proposal_decision_identity"][
                "human_proposal_decision_identity_version"
            ], HUMAN_PROPOSAL_DECISION_IDENTITY_VERSION,
        )
        self.assertNotEqual(
            approved["human_proposal_decision_identity"]["digest"],
            declined["human_proposal_decision_identity"]["digest"],
        )

    def test_identity_is_deterministic_canonical_and_binds_complete_identities(self):
        spec = decision_spec(reference="  review 42  ")
        first = build_human_proposal_decision(self.presentation, spec)
        second = build_human_proposal_decision(self.presentation, deepcopy(spec))
        self.assertEqual(first, second)
        identity = first["human_proposal_decision_identity"]
        payload = identity["canonical_payload"]
        expected_digest = hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")).hexdigest()
        self.assertEqual(identity["digest"], expected_digest)
        self.assertEqual(payload["proposal_identity"], self.presentation["proposal_identity"])
        self.assertEqual(payload["presentation_identity"],
                         self.presentation["presentation_identity"])
        self.assertEqual(first["decision_source"]["provenance"]["reference"],
                         "review 42")

    def test_identity_ignores_dictionary_insertion_order(self):
        reordered_presentation = dict(reversed(list(self.presentation.items())))
        spec = decision_spec()
        reordered_spec = {
            "decision_source": {
                "provenance": {"reference": "review 42"},
                "kind": "explicit_user",
            },
            "decision": "approved",
        }
        first = build_human_proposal_decision(self.presentation, spec)
        second = build_human_proposal_decision(reordered_presentation, reordered_spec)
        self.assertEqual(
            first["human_proposal_decision_identity"],
            second["human_proposal_decision_identity"],
        )

    def test_decision_source_and_provenance_are_identity_semantics(self):
        variants = [
            build_human_proposal_decision(
                self.presentation, decision_spec(kind=kind, reference=reference),
            )
            for kind, reference in (
                ("explicit_user", "user review"),
                ("explicit_user", "other review"),
                ("explicit_operator", "operator review"),
            )
        ]
        self.assertEqual(len({
            item["human_proposal_decision_identity"]["digest"] for item in variants
        }), 3)

    def test_proposal_and_presentation_identity_changes_change_decision_identity(self):
        baseline = build_human_proposal_decision(self.presentation, decision_spec())
        other_proposal = build_proposal_presentation(self.fixture.accepted(
            policy_id="proposal.other.v1", policy_reference="test declaration",
        ))
        changed_display_source = deepcopy(self.proposal_v2)
        changed_display_source["proposal"]["delta"]["name"] = "Displayed Alpha"
        row = changed_display_source["source_recommendation_context"]
        row["decision"]["candidate"]["name"] = "Displayed Alpha"
        row["returned_candidate_facts"][0]["name"] = "Displayed Alpha"
        other_presentation = build_proposal_presentation(changed_display_source)
        results = [
            build_human_proposal_decision(value, decision_spec())
            for value in (other_proposal, other_presentation)
        ]
        self.assertEqual(len({
            baseline["human_proposal_decision_identity"]["digest"],
            *(item["human_proposal_decision_identity"]["digest"] for item in results),
        }), 3)

    def test_complete_source_presentation_is_retained_by_value(self):
        result = build_human_proposal_decision(self.presentation, decision_spec())
        self.assertEqual(result["source_presentation"], self.presentation)
        self.assertIsNot(result["source_presentation"], self.presentation)
        self.assertEqual(result["proposal_identity"], self.presentation["proposal_identity"])
        self.assertEqual(result["presentation_identity"],
                         self.presentation["presentation_identity"])

    def test_missing_inferred_unknown_or_profile_decisions_fail_closed(self):
        invalid = [
            {},
            {"decision": "approved"},
            decision_spec("accepted"),
            decision_spec("validator-accepted"),
            decision_spec("approved", "explicit_operator_profile"),
            decision_spec("approved", "implicit_user"),
            decision_spec("approved", reference="   "),
        ]
        for spec in invalid:
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                build_human_proposal_decision(self.presentation, spec)

    def test_direct_proposal_v2_and_tampered_or_future_presentations_fail_closed(self):
        artifacts = [self.proposal_v2]
        for status, reason in (("abstained", "validation_failed"),
                               ("rejected", "malformed_input")):
            proposal = deepcopy(self.proposal_v2)
            proposal["status"], proposal["reason"], proposal["proposal"] = status, reason, None
            artifacts.append(proposal)
        tampered = deepcopy(self.presentation)
        tampered["review_artifact"]["proposal"]["card"]["name"] = "Different"
        artifacts.append(tampered)
        future = deepcopy(self.presentation)
        future["proposal_presentation_model_version"] = "2"
        artifacts.append(future)
        stale = deepcopy(self.presentation)
        stale["proposal_identity"]["digest"] = "0" * 64
        artifacts.append(stale)
        for artifact in artifacts:
            with self.assertRaises(ValueError):
                build_human_proposal_decision(artifact, decision_spec())

    def test_inputs_unchanged_and_no_io_validation_persistence_or_upstream_calls(self):
        presentation = deepcopy(self.presentation)
        spec = decision_spec()
        before_presentation, before_spec = deepcopy(presentation), deepcopy(spec)
        with patch("services.proposal_presentation.build_proposal_presentation",
                   side_effect=AssertionError("upstream presentation")), \
             patch("services.validator.validate_deck",
                   side_effect=AssertionError("validator")), \
             patch("services.exporter.export_arena_deck",
                   side_effect=AssertionError("exporter")), \
             patch("mtgadb.snapshot_store.save_snapshot",
                   side_effect=AssertionError("snapshot persistence")), \
             patch("sqlite3.connect", side_effect=AssertionError("database")), \
             patch("time.time", side_effect=AssertionError("clock")), \
             patch("builtins.open", side_effect=AssertionError("filesystem")):
            build_human_proposal_decision(presentation, spec)
        self.assertEqual(presentation, before_presentation)
        self.assertEqual(spec, before_spec)

    def test_no_executable_authorization_field_and_limitations_are_explicit(self):
        result = build_human_proposal_decision(self.presentation, decision_spec())

        def keys(value):
            if isinstance(value, dict):
                return set(value).union(*(keys(item) for item in value.values()))
            if isinstance(value, list):
                return set().union(*(keys(item) for item in value)) if value else set()
            return set()

        self.assertTrue({
            "approval", "authorization", "execution_authorization", "apply",
            "execute",
        }.isdisjoint(keys(result)))
        limitations = " ".join(result["limitations"]).lower()
        for phrase in ("not execution authorization", "validation remains current",
                       "resource-spending authority", "does not mutate", "not signatures"):
            self.assertIn(phrase, limitations)

    def test_public_verifier_accepts_complete_decision_by_value(self):
        decision = build_human_proposal_decision(self.presentation, decision_spec())
        before = deepcopy(decision)
        verified = require_human_proposal_decision(decision)
        self.assertEqual(verified, decision)
        self.assertIsNot(verified, decision)
        self.assertEqual(decision, before)

    def test_public_verifier_rejects_versions_digests_and_cross_identity_tampering(self):
        decision = build_human_proposal_decision(self.presentation, decision_spec())
        mutations = []
        for path, value in (
            (("human_proposal_decision_model_version",), "2"),
            (("source_proposal_presentation_model_version",), "2"),
            (("human_proposal_decision_identity",
              "human_proposal_decision_identity_version"), "2"),
            (("human_proposal_decision_identity", "digest_algorithm"), "sha512"),
            (("human_proposal_decision_identity", "digest"), "0" * 64),
            (("decision_source", "provenance", "reference"), " altered "),
            (("limitations",), []),
        ):
            changed = deepcopy(decision)
            target = changed
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            mutations.append(changed)
        other = build_human_proposal_decision(
            build_proposal_presentation(self.fixture.accepted(
                policy_id="proposal.other.v1", policy_reference="test declaration",
            )),
            decision_spec(),
        )
        swapped = deepcopy(decision)
        swapped["proposal_identity"] = other["proposal_identity"]
        mutations.append(swapped)
        nested = deepcopy(decision)
        nested["source_presentation"] = other["source_presentation"]
        mutations.append(nested)
        for changed in mutations:
            with self.assertRaises(ValueError):
                require_human_proposal_decision(changed)

    def test_public_verifier_rejects_missing_and_extra_fields_at_owned_boundaries(self):
        decision = build_human_proposal_decision(self.presentation, decision_spec())
        for path in ((), ("human_proposal_decision_identity",),
                     ("human_proposal_decision_identity", "canonical_payload"),
                     ("decision_source",), ("decision_source", "provenance")):
            original = decision
            for key in path:
                original = original[key]
            for missing in [*original, None]:
                changed = deepcopy(decision)
                target = changed
                for key in path:
                    target = target[key]
                if missing is None:
                    target["unsupported"] = True
                else:
                    del target[missing]
                with self.subTest(path=path, missing=missing), self.assertRaises(ValueError):
                    require_human_proposal_decision(changed)

    def test_public_verifier_rejects_numeric_type_substitution_in_repeated_identity(self):
        for value in (True, 1.0):
            for field in ("proposal_identity", "human_proposal_decision_identity"):
                changed = build_human_proposal_decision(self.presentation, decision_spec())
                identity = changed[field]
                proposal_identity = identity if field == "proposal_identity" else (
                    identity["canonical_payload"]["proposal_identity"]
                )
                proposal_identity["canonical_payload"]["delta"]["quantity"] = value
                # Rehashing a contradictory Decision payload cannot hide type drift.
                identity["digest"] = hashlib.sha256(json.dumps(
                    identity["canonical_payload"], sort_keys=True, separators=(",", ":"),
                    ensure_ascii=False, allow_nan=False,
                ).encode("utf-8")).hexdigest()
                with self.subTest(value=value, field=field), self.assertRaises(ValueError):
                    require_human_proposal_decision(changed)


if __name__ == "__main__":
    unittest.main()
