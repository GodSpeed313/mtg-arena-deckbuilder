from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from mtgadb import managed_deck_store as store
from mtgadb.deck_identity import build_deck_snapshot_identity
from mtgadb.model import Collection, Deck
from mtgadb.modes import OperatingMode
from services import local_deck_application_intent as intent
from services.human_proposal_decision import build_human_proposal_decision
from services.proposal_presentation import build_proposal_presentation
from services.validator import ValidationIssue, ValidationReport
from tests import test_pre_execution_revalidation as revalidation_tests
from tests.test_human_proposal_decision import decision_spec


ID = "local_deck_application_intent_identity"
SERVICE = "services.local_deck_application_intent."
SELECTION = ("store_id", "store_generation", "record_id", "revision", "gameplay_snapshot_identity")


def rehash(identity):
    identity["digest"] = hashlib.sha256(json.dumps(identity["canonical_payload"],
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class LocalDeckApplicationIntentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = revalidation_tests.PreExecutionRevalidationTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "managed.db"
        store.initialize_store(self.path)
        self.destination = store.create_managed_deck(self.path, self.fixture.deck, "Selected", "Test")
        self.revalidation = self.fixture.evaluate(mode=OperatingMode.FULL_COLLECTION, collection=self.fixture.owned)
        self.request = self.request_for(self.destination)

    def request_for(self, destination):
        return {"request": "apply", "request_source": {"kind": "explicit_user",
                "provenance": {"reference": "local explicit request #1"}},
                "selected_destination": {key: deepcopy(destination[key]) for key in SELECTION}}

    def build(self, revalidation=None, destination=None, request=None):
        return intent.build_local_deck_application_intent(
            self.revalidation if revalidation is None else revalidation,
            self.destination if destination is None else destination,
            self.request if request is None else request)

    def reject_build(self, **kwargs):
        with self.assertRaises(ValueError):
            self.build(**kwargs)

    def reject(self, artifact):
        with self.assertRaises(ValueError):
            intent.require_local_deck_application_intent(artifact)

    def rebind(self, artifact):
        payload = artifact[ID]["canonical_payload"]
        for key in payload:
            if key == "pre_execution_revalidation_identity":
                payload[key] = deepcopy(artifact["source_pre_execution_revalidation"][key])
            elif key == "destination_state":
                payload[key] = deepcopy(artifact["source_destination_state"])
            else:
                payload[key] = deepcopy(artifact[key])
        rehash(artifact[ID])

    def test_full_collection_positive_and_exact_action_result(self):
        value = self.build()
        self.assertEqual(value["status"], "intent_recorded")
        self.assertEqual(value["derived_action"], self.revalidation["proposal_identity"]["canonical_payload"]["delta"])
        self.assertEqual(value["expected_result_deck_identity"], self.revalidation["reconstructed_result_deck_identity"])
        self.assertEqual(intent.require_local_deck_application_intent(value), value)

    def test_wildcard_zero_cost_positive(self):
        value = self.build(revalidation=self.fixture.budget())
        self.assertEqual(intent.require_local_deck_application_intent(value), value)

    def test_unlimited_and_affordable_spending_rejected(self):
        self.reject_build(revalidation=self.fixture.evaluate())
        self.reject_build(revalidation=self.fixture.budget(collection=Collection({})))

    def test_nonzero_full_collection_cost_rejected_despite_positive_6l(self):
        with patch("services.pre_execution_revalidation.validate_deck",
                   return_value=ValidationReport(wildcard_cost={"common": 1})):
            value = self.fixture.evaluate(mode=OperatingMode.FULL_COLLECTION, collection=self.fixture.owned)
        self.assertEqual(value["status"], "revalidated")
        revalidation_tests.require_pre_execution_revalidation(value)
        self.reject_build(revalidation=value)

    def test_resource_tampering_and_types(self):
        for cost in ({"common": True}, {"common": -1}, {"common": "0"}, {"common": 0.0}, []):
            value = deepcopy(self.revalidation)
            value["resource_assessment"]["wildcard_cost"] = cost
            value["fresh_validation"]["wildcard_cost"] = cost
            payload = value["pre_execution_revalidation_identity"]["canonical_payload"]
            payload["resource_assessment"] = deepcopy(value["resource_assessment"])
            payload["fresh_validation"] = deepcopy(value["fresh_validation"])
            rehash(value["pre_execution_revalidation_identity"])
            self.reject_build(revalidation=value)
        for key, replacement in (("spending_authorized", True), ("wildcard_cost", {"common": 0}),
                                 ("resource_mode", "wildcard_budget")):
            value = deepcopy(self.revalidation)
            value["resource_assessment"][key] = replacement
            self.reject_build(revalidation=value)

    def test_explicit_sources_and_provenance_normalization(self):
        for kind in ("explicit_user", "explicit_operator"):
            request = deepcopy(self.request)
            request["request_source"] = {"kind": kind, "provenance": {"reference": "  explicit instruction  "}}
            value = self.build(request=request)
            self.assertEqual(value["application_request"]["request_source"]["provenance"]["reference"], "explicit instruction")
            self.assertEqual(intent.require_local_deck_application_intent(value), value)

    def test_invalid_provenance_sources_and_requests(self):
        for reference in ("", " \t ", None, True, 1):
            request = deepcopy(self.request)
            request["request_source"]["provenance"]["reference"] = reference
            self.reject_build(request=request)
        for kind in ("explicit_operator_profile", "inferred", None, []):
            request = deepcopy(self.request)
            request["request_source"]["kind"] = kind
            self.reject_build(request=request)
        for request in ({}, {k: v for k, v in self.request.items() if k != "request"},
                        {**self.request, "request": "default"}, {**self.request, "request": True}):
            self.reject_build(request=request)

    def test_every_selection_dimension_checked_type_sensitively(self):
        for key, replacement in (("store_id", str(uuid4())), ("store_generation", str(uuid4())),
                                 ("record_id", str(uuid4())), ("revision", 2), ("revision", True),
                                 ("revision", 1.0), ("revision", "1"),
                                 ("gameplay_snapshot_identity", build_deck_snapshot_identity(Deck()))):
            request = deepcopy(self.request)
            request["selected_destination"][key] = replacement
            self.reject_build(request=request)

    def test_same_gameplay_and_name_other_unselected_record_rejected(self):
        other = store.create_managed_deck(self.path, self.fixture.deck, "Selected", "Test")
        self.reject_build(destination=other)
        value = self.build(destination=other, request=self.request_for(other))
        self.assertNotEqual(value[ID], self.build()[ID])

    def test_baseline_printing_quantity_and_zone_mismatches(self):
        for deck in (Deck(main={102: 1}), Deck(main={201: 2}, sideboard={301: 1}),
                     Deck(sideboard={201: 1, 301: 1})):
            other = store.create_managed_deck(self.path, deck, "Selected")
            self.reject_build(destination=other, request=self.request_for(other))

    def test_caller_cannot_override_action(self):
        for field in ("card", "arena_id", "zone", "quantity", "derived_action"):
            with self.assertRaises(TypeError):
                intent.build_local_deck_application_intent(self.revalidation, self.destination, self.request,
                                                          **{field: 1})
        for field, replacement in (("arena_id", 102), ("title_id", 2), ("zone", "sideboard"),
                                   ("quantity", 2), ("operation", "remove")):
            artifact = self.build()
            artifact["derived_action"][field] = replacement
            self.rebind(artifact)
            self.reject(artifact)

    def test_result_identity_tampering_even_rehashed(self):
        artifact = self.build()
        artifact["expected_result_deck_identity"] = build_deck_snapshot_identity(Deck())
        self.rebind(artifact)
        self.reject(artifact)

    def test_metadata_preservation_and_scope(self):
        artifact = self.build()
        self.assertEqual(artifact["source_destination_state"]["metadata"], self.destination["metadata"])
        for key in ("destination_kind", "resource_policy", "metadata_policy"):
            changed = deepcopy(artifact)
            changed["application_scope"][key] = "other"
            self.rebind(changed)
            self.reject(changed)

    def test_identity_deterministic_and_serialization_invariant(self):
        artifact = self.build()
        self.assertEqual(self.build(), artifact)
        self.assertEqual(self.build(destination=store.serialize_destination_state(self.destination)), artifact)
        self.assertEqual(intent.require_local_deck_application_intent(json.loads(json.dumps(artifact))), artifact)

    def test_dictionary_order_invariance(self):
        def reverse(value):
            if type(value) is dict:
                return {key: reverse(item) for key, item in reversed(list(value.items()))}
            if type(value) is list:
                return [reverse(item) for item in value]
            return value
        artifact = self.build()
        self.assertEqual(intent.require_local_deck_application_intent(reverse(artifact)), artifact)
        self.assertEqual(self.build(revalidation=reverse(self.revalidation), request=reverse(self.request)), artifact)

    def test_request_changes_change_identity(self):
        original = self.build()[ID]
        for source in ({"kind": "explicit_operator", "provenance": {"reference": "local explicit request #1"}},
                       {"kind": "explicit_user", "provenance": {"reference": "another instruction"}}):
            self.assertNotEqual(self.build(request={**self.request, "request_source": source})[ID], original)

    def test_destination_revision_generation_and_metadata_change_identity(self):
        original = self.build()[ID]
        for key, value in (("revision", 2), ("store_generation", str(uuid4())), ("store_id", str(uuid4()))):
            destination = deepcopy(self.destination)
            destination[key] = value
            self.assertNotEqual(self.build(destination=destination, request=self.request_for(destination))[ID], original)
        changed = store.conditional_replace(self.path, self.destination["record_id"], self.destination,
                                            self.destination["deck"], name="Renamed", format_label=None)["destination_state"]
        self.assertNotEqual(self.build(destination=changed, request=self.request_for(changed))[ID], original)

    def test_resource_evidence_change_changes_identity(self):
        self.assertNotEqual(self.build()[ID], self.build(revalidation=self.fixture.budget())[ID])

    def test_alternate_zones_change_action_result_and_identity(self):
        original = self.build()
        for zone in ("sideboard", "commander"):
            proposal = self.fixture.fixture.accepted(target=zone)
            decision = build_human_proposal_decision(build_proposal_presentation(proposal), decision_spec())
            evidence = self.fixture.evaluate(human_decision=decision, mode=OperatingMode.FULL_COLLECTION,
                                             collection=self.fixture.owned)
            artifact = self.build(revalidation=evidence)
            self.assertEqual(artifact["derived_action"]["zone"], zone)
            self.assertNotEqual(artifact["expected_result_deck_identity"], original["expected_result_deck_identity"])
            self.assertNotEqual(artifact[ID], original[ID])
            self.assertEqual(intent.require_local_deck_application_intent(artifact), artifact)

    def test_prospective_signed_64_bit_overflow(self):
        baseline = deepcopy(self.fixture.deck)
        baseline.main[101] = store.MAX_INTEGER
        result = deepcopy(baseline)
        result.main[101] += 1
        old_baseline = self.revalidation["current_baseline_deck_identity"]
        old_result = self.revalidation["reconstructed_result_deck_identity"]

        # Coherently rewritten historical evidence is not authenticated. Reach
        # the actual storage boundary without allocating MAX_INTEGER copies.
        def rewrite(value):
            if type(value) is dict:
                if value == old_baseline:
                    return build_deck_snapshot_identity(baseline)
                if value == old_result:
                    return build_deck_snapshot_identity(result)
                changed = {key: rewrite(item) for key, item in value.items()}
                if "canonical_payload" in changed and "digest" in changed:
                    rehash(changed)
                return changed
            if type(value) is list:
                return [rewrite(item) for item in value]
            return value

        evidence = rewrite(self.revalidation)
        revalidation_tests.require_pre_execution_revalidation(evidence)
        destination = store.create_managed_deck(self.path, baseline, "At storage limit")
        with self.assertRaisesRegex(ValueError, "integer bounds"):
            self.build(revalidation=evidence, destination=destination, request=self.request_for(destination))

    def test_verifier_requires_normalized_request_even_rehashed(self):
        artifact = self.build()
        artifact["application_request"]["request_source"]["provenance"]["reference"] = " padded "
        self.rebind(artifact)
        self.reject(artifact)

    def test_coherent_request_rewrite_is_not_authentication(self):
        artifact = self.build()
        artifact["application_request"]["request_source"]["provenance"]["reference"] = "another declared request"
        self.rebind(artifact)
        self.assertEqual(intent.require_local_deck_application_intent(artifact), artifact)

    def test_tampered_upstream_and_destination(self):
        for field in ("proposal_identity", "human_proposal_decision_identity", "pre_execution_revalidation_identity"):
            value = deepcopy(self.revalidation)
            value[field]["digest"] = "0"*64
            self.reject_build(revalidation=value)
        destination = deepcopy(self.destination)
        destination["deck"].main[201] = 4
        self.reject_build(destination=destination)

    def test_declined_and_negative_relabeling_rejected(self):
        decision = build_human_proposal_decision(self.fixture.presentation, decision_spec("declined"))
        value = self.fixture.evaluate(human_decision=decision)
        self.reject_build(revalidation=value)
        value["status"], value["reason"] = "revalidated", "fresh_validation_passed"
        self.reject_build(revalidation=value)

    def test_identity_versions_algorithm_digest_and_payload(self):
        for key, replacement in (("local_deck_application_intent_identity_version", "2"),
                                 ("digest_algorithm", "sha512"), ("digest", "0"*64),
                                 ("canonical_payload", {})):
            artifact = self.build()
            artifact[ID][key] = replacement
            self.reject(artifact)
        for key, value in (("local_deck_application_intent_model_version", "2"),
                           ("status", "applied"), ("reason", "executed")):
            artifact = self.build()
            artifact[key] = value
            self.rebind(artifact)
            self.reject(artifact)

    def test_unknown_and_missing_fields(self):
        original = self.build()
        for path in ((), ("application_request",), ("application_request", "request_source"),
                     ("application_request", "request_source", "provenance"),
                     ("application_request", "selected_destination"), ("source_destination_state",),
                     ("application_scope",), ("derived_action",), (ID,), (ID, "canonical_payload")):
            artifact = deepcopy(original)
            target = artifact
            for key in path:
                target = target[key]
            target["unexpected"] = True
            self.reject(artifact)
        for key in original:
            artifact = deepcopy(original)
            del artifact[key]
            self.reject(artifact)

    def test_type_confusion_and_python_only_shapes(self):
        for replacement in (True, 1.0, "1"):
            artifact = self.build()
            artifact["derived_action"]["quantity"] = replacement
            self.rebind(artifact)
            self.reject(artifact)
        artifact = self.build()
        artifact["limitations"] = tuple(artifact["limitations"])
        self.reject(artifact)

    def test_detached_inputs_and_outputs(self):
        originals = deepcopy((self.revalidation, self.destination, self.request))
        artifact = self.build()
        verified = intent.require_local_deck_application_intent(artifact)
        verified["source_destination_state"]["metadata"]["name"] = "mutated"
        self.assertEqual((self.revalidation, self.destination, self.request), originals)
        self.assertEqual(artifact, self.build())

    def test_no_io_validator_or_mutation_and_no_path(self):
        artifact = self.build()
        before = self.path.read_bytes()
        with ExitStack() as stack:
            for name in ("sqlite3.connect", "builtins.open", "io.open", "socket.socket",
                         "mtgadb.managed_deck_store.read_destination_state", "mtgadb.managed_deck_store.open_store",
                         "mtgadb.managed_deck_store.conditional_replace", "mtgadb.managed_deck_store.conditional_delete",
                         "services.validator.validate_deck", "services.pre_execution_revalidation.validate_deck",
                         "services.exporter.export_arena_deck", "mtgadb.snapshot_store.save_snapshot"):
                stack.enter_context(patch(name, side_effect=AssertionError(name)))
            self.assertEqual(self.build(), artifact)
            self.assertEqual(intent.require_local_deck_application_intent(artifact), artifact)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(store.read_destination_state(self.path, self.destination["record_id"])["revision"], 1)
        self.assertNotIn(str(self.path), json.dumps(artifact))

    def test_owning_verifiers_are_called(self):
        with patch(SERVICE + "require_pre_execution_revalidation", wraps=intent.require_pre_execution_revalidation) as upstream, \
             patch(SERVICE + "require_destination_state", wraps=intent.require_destination_state) as destination:
            self.build()
        upstream.assert_called_once_with(self.revalidation)
        destination.assert_called_once_with(self.destination)

    def test_stale_artifact_still_historically_verifies(self):
        artifact = self.build()
        store.conditional_delete(self.path, self.destination["record_id"], self.destination)
        self.assertEqual(intent.require_local_deck_application_intent(artifact), artifact)

    def test_limitations_fixed_and_no_invented_completeness(self):
        artifact = self.build()
        self.assertTrue(any("discarded original pool evidence" in text for text in artifact["limitations"]))
        self.assertNotIn("candidate_completeness", artifact)
        artifact["limitations"] = []
        self.rebind(artifact)
        self.reject(artifact)

    def test_warnings_preserved(self):
        with patch("services.pre_execution_revalidation.validate_deck",
                   return_value=ValidationReport(warnings=(ValidationIssue("notice", "Review"),))):
            value = self.fixture.evaluate(mode=OperatingMode.FULL_COLLECTION, collection=self.fixture.owned)
        artifact = self.build(revalidation=value)
        self.assertEqual(artifact["source_pre_execution_revalidation"]["fresh_validation"]["warnings"], value["fresh_validation"]["warnings"])


if __name__ == "__main__":
    unittest.main()
