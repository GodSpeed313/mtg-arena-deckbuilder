"""Bind a caller-declared local application request without executing it."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from mtgadb.deck_identity import build_deck_snapshot_identity
from mtgadb.managed_deck_store import (
    MAX_INTEGER, require_destination_state, serialize_destination_state,
)
from mtgadb.model import Deck
from services.pre_execution_revalidation import require_pre_execution_revalidation


LOCAL_DECK_APPLICATION_INTENT_MODEL_VERSION = "1"
LOCAL_DECK_APPLICATION_INTENT_IDENTITY_VERSION = "1"
IDENTITY_DIGEST_ALGORITHM = "sha256"
_ZONES = ("main", "sideboard", "commander")
_SELECTION_FIELDS = {"store_id", "store_generation", "record_id", "revision", "gameplay_snapshot_identity"}
_SCOPE = {
    "destination_kind": "managed_local_deck",
    "resource_policy": "no_spend_only",
    "metadata_policy": "preserve_destination_metadata",
}
_LIMITATIONS = [
    "This records a caller-declared explicit application request; it does not authenticate a human or prove legal identity, consent, or non-repudiation.",
    "The identity provides deterministic mismatch detection, not a signature, bearer credential, capability token, execution authority, or resource-spending authority.",
    "Verification establishes historical internal consistency only, not currentness of the destination, validation, collection, wildcard balances, or account state.",
    "Historical no-spend evidence follows the retained validator rules; it does not independently prove ownership or present resource authority.",
    "This preserves the verified upstream construction guarantee against truncated candidate pools but cannot reconstruct or independently re-prove discarded original pool evidence.",
    "This boundary does not read storage, validate current state, mutate, advance revision, persist, export, craft, spend resources, or write to Arena.",
    "A trusted future invocation must resolve the store, reread authoritative state, revalidate the exact action and no-spend state, and preserve destination metadata before any mutation.",
    "Intent is not single-use, replay prevention, an operation receipt, or exactly-once protection; a coherently rewritten and rehashed artifact may remain internally valid.",
    "In-file identities cannot detect arbitrary external store copying, rollback, replacement, or coherent tampering; filesystem paths are not semantic destination identity.",
]
_FIELDS = {
    "local_deck_application_intent_model_version", "status", "reason",
    "source_pre_execution_revalidation", "source_destination_state", "application_request",
    "derived_action", "expected_result_deck_identity", "application_scope",
    "local_deck_application_intent_identity", "limitations",
}


def _closed(value, fields, label):
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"{label} has missing or unsupported fields")
    return value


def _json_value(value):
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("intent object keys must be strings")
            _json_value(item)
    elif type(value) is list:
        for item in value:
            _json_value(item)
    elif value is not None and type(value) not in (str, int, float, bool):
        raise ValueError("intent artifact must contain JSON values")


def _encoded(value):
    _json_value(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _equal(left, right, label):
    if _encoded(left) != _encoded(right):
        raise ValueError(f"{label} is contradictory")


def _request(value, destination):
    _closed(value, {"request", "request_source", "selected_destination"}, "application request")
    if type(value["request"]) is not str or value["request"] != "apply":
        raise ValueError("explicit apply request is required")
    source = _closed(value["request_source"], {"kind", "provenance"}, "request source")
    if type(source["kind"]) is not str or source["kind"] not in ("explicit_user", "explicit_operator"):
        raise ValueError("explicit supported request source is required")
    provenance = _closed(source["provenance"], {"reference"}, "request provenance")
    reference = provenance["reference"]
    if type(reference) is not str or not reference.strip():
        raise ValueError("request provenance reference must be non-empty")
    selection = _closed(value["selected_destination"], _SELECTION_FIELDS, "selected destination")
    expected_selection = {key: destination[key] for key in _SELECTION_FIELDS}
    _equal(selection, expected_selection, "explicit destination selection")
    return {
        "request": "apply",
        "request_source": {"kind": source["kind"], "provenance": {"reference": reference.strip()}},
        "selected_destination": deepcopy(expected_selection),
    }


def _build(pre_execution_revalidation, destination_state, request_spec):
    revalidation = require_pre_execution_revalidation(pre_execution_revalidation)
    if revalidation["status"] != "revalidated" or revalidation["reason"] != "fresh_validation_passed":
        raise ValueError("positive pre-execution revalidation is required")
    if revalidation["source_human_proposal_decision"]["decision"] != "approved" or (
        revalidation["pre_execution_revalidation_identity"] is None
        or revalidation["fresh_validation"]["valid"] is not True
    ):
        raise ValueError("approved successful revalidation is required")
    _equal(revalidation["destination_assessment"], {
        "destination_status": "deferred", "destination_reason": "no_destination_contract",
    }, "revalidation destination assessment")
    resource = revalidation["resource_assessment"]
    if (resource["resource_mode"], resource["resource_status"]) not in (
        ("full_collection", "owned_no_crafting_required"),
        ("wildcard_budget", "no_spend_required"),
    ) or resource["spending_authorized"] is not False:
        raise ValueError("eligible historical no-spend resource evidence is required")
    _equal(resource["resource_mode"], revalidation["current_validation_context"]["mode"], "resource mode")
    cost = resource["wildcard_cost"]
    _equal(cost, revalidation["fresh_validation"]["wildcard_cost"], "wildcard cost")
    if type(cost) is not dict or any(type(n) is not int or n < 0 for n in cost.values()) or sum(cost.values()) != 0:
        raise ValueError("exact nonnegative integer costs totaling zero are required")

    native = require_destination_state(destination_state)
    destination = serialize_destination_state(native)
    request = _request(request_spec, destination)
    proposal = revalidation["proposal_identity"]["canonical_payload"]
    action = deepcopy(proposal["delta"])
    if action["operation"] != "add" or type(action["quantity"]) is not int or action["quantity"] != 1 or action["zone"] not in _ZONES:
        raise ValueError("intent supports exactly one approved add operation")
    _equal(action, revalidation["approved_delta"], "approved action")
    baseline = destination["gameplay_snapshot_identity"]
    _equal(baseline, revalidation["current_baseline_deck_identity"], "destination/revalidation baseline")
    _equal(baseline, proposal["source_baseline_deck_identity"], "destination/proposal baseline")
    zones = {zone: deepcopy(getattr(native["deck"], zone)) for zone in _ZONES}
    target = zones[action["zone"]]
    target[action["arena_id"]] = target.get(action["arena_id"], 0) + action["quantity"]
    for zone in zones.values():
        if any(type(arena_id) is not int or not 1 <= arena_id <= MAX_INTEGER
               or type(quantity) is not int or not 1 <= quantity <= MAX_INTEGER
               for arena_id, quantity in zone.items()):
            raise ValueError("prospective result exceeds managed-store integer bounds")
    result = build_deck_snapshot_identity(Deck(name=native["metadata"]["name"], **zones))
    _equal(result, revalidation["reconstructed_result_deck_identity"], "prospective revalidation result")
    _equal(result, proposal["resulting_deck_identity"], "prospective proposal result")
    artifact = {
        "local_deck_application_intent_model_version": LOCAL_DECK_APPLICATION_INTENT_MODEL_VERSION,
        "status": "intent_recorded", "reason": "explicit_local_application_requested",
        "source_pre_execution_revalidation": revalidation,
        "source_destination_state": destination, "application_request": request,
        "derived_action": action, "expected_result_deck_identity": result,
        "application_scope": deepcopy(_SCOPE), "limitations": deepcopy(_LIMITATIONS),
    }
    payload = {key: deepcopy(artifact[key]) for key in (
        "local_deck_application_intent_model_version", "status", "reason", "application_request",
        "derived_action", "expected_result_deck_identity", "application_scope", "limitations",
    )}
    payload["pre_execution_revalidation_identity"] = deepcopy(revalidation["pre_execution_revalidation_identity"])
    payload["destination_state"] = deepcopy(destination)
    artifact["local_deck_application_intent_identity"] = {
        "local_deck_application_intent_identity_version": LOCAL_DECK_APPLICATION_INTENT_IDENTITY_VERSION,
        "digest_algorithm": IDENTITY_DIGEST_ALGORITHM, "canonical_payload": payload,
        "digest": hashlib.sha256(_encoded(payload)).hexdigest(),
    }
    return artifact


def build_local_deck_application_intent(pre_execution_revalidation, destination_state, request_spec) -> dict:
    """Record one explicit, positive historical intent; perform no IO or mutation."""
    try:
        return _build(pre_execution_revalidation, destination_state, request_spec)
    except (TypeError, KeyError, AttributeError, RecursionError, IndexError) as exc:
        raise ValueError("application intent inputs are malformed") from exc


def require_local_deck_application_intent(value: dict) -> dict:
    """Verify historical internal consistency only and return a detached artifact."""
    try:
        _closed(value, _FIELDS, "local deck application intent")
        _encoded(value)
        expected = _build(value["source_pre_execution_revalidation"],
                          value["source_destination_state"], value["application_request"])
        # Rebuilding verifies every version, identity field, digest, projection,
        # normalized request, scope and limitation, including unknown fields.
        _equal(value, expected, "local deck application intent")
        return expected
    except (TypeError, KeyError, AttributeError, RecursionError, IndexError) as exc:
        raise ValueError("application intent artifact is malformed") from exc
