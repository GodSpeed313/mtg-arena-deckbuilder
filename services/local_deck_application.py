"""Atomic one-copy, no-spend application to a managed local deck (#6O v1)."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from uuid import UUID

from mtgadb import managed_deck_store as store
from mtgadb.deck_identity import build_deck_snapshot_identity
from mtgadb.model import Collection, Inventory, Deck
from mtgadb.modes import OperatingMode
from services.local_deck_application_intent import (
    require_local_deck_application_intent, require_local_deck_application_intent_v2,
)
from services.local_execution_validation import (
    LocalDeckApplicationError, LocalExecutionValidationAuthorityV1,
    _encoded, _fail, _resource_inputs,
)
from services.pre_execution_revalidation import (
    build_pre_execution_revalidation, require_pre_execution_revalidation,
    build_pre_execution_revalidation_v2, require_pre_execution_revalidation_v2,
)


LOCAL_DECK_APPLICATION_RESULT_MODEL_VERSION = "1"
LOCAL_DECK_APPLICATION_RESULT_IDENTITY_VERSION = "1"
_ID = "local_deck_application_result_identity"
_INTENT_ID = "local_deck_application_intent_identity"
_SCOPE = {"destination_kind": "managed_local_deck", "operation": "add", "quantity": 1,
          "resource_policy": "no_spend_only", "metadata_policy": "preserve_destination_metadata"}
_LIMITATIONS = [
    "Execution used the explicitly designated local authority, not proof of live Arena ownership, wildcard balances, account state, or synchronization with Arena at commit time.",
    "The guard protects participating publication to this process-local authority only, not independent authorities or nonparticipating writers.",
    "This returned historical result is not a durable receipt; loss of acknowledgment or commit failure can leave the caller uncertain whether application committed.",
    "The digest detects historical mismatches, not authentication, capability authority, non-repudiation, external rollback, or exactly-once execution.",
    "Historical verification cannot prove execution occurred, recheck the original card database from its digest, or authenticate resource inputs; coherent rewrites may verify.",
    "In-file store identities cannot detect arbitrary coherent copying, replacement, rollback, or tampering.",
    "No crafting, spending, Arena IO, export/import, upstream artifact mutation, or automatic acceptance is performed.",
    "The upstream candidate-pool construction guarantee is retained; discarded original completeness evidence cannot be independently reconstructed.",
]
_V2_LIMITATIONS = [*_LIMITATIONS,
    "Decision 2 is not destination-bound. Intent 2 selects one destination for this application.",
    "Approval is not globally consumed and is not globally single-use. Same-destination replay fails through current revision/baseline checks; a separate application request against another matching destination may succeed.",
    "Scoped evidence is historical digest-bound provenance. Execution does not rerun scoped analysis or prove that the need was resolved or improved.",
    "Result 2 is a non-durable acknowledgment, not a durable receipt or recovery record; no Operation 2, Receipt 2, Recovery 2 or prepared-execution Model 2 exists in #6T.",
]
_FIELDS = {"local_deck_application_result_model_version", "status", "reason",
           "source_local_deck_application_intent", "source_local_deck_application_intent_identity",
           "previous_destination_state", "execution_pre_execution_revalidation", "execution_context",
           "applied_action", "resulting_destination_state", "resulting_deck_identity",
           "previous_revision", "new_revision", "execution_scope", "limitations", _ID}


def _same(left, right):
    return _encoded(left) == _encoded(right)


def _reconstruct(intent, current):
    action = intent["derived_action"]
    if action["operation"] != "add" or type(action["quantity"]) is not int or action["quantity"] != 1 or (
        action["zone"] not in ("main", "sideboard", "commander")
    ):
        _fail("invalid_intent")
    zones = {zone: deepcopy(getattr(current["deck"], zone)) for zone in ("main", "sideboard", "commander")}
    target = zones[action["zone"]]
    target[action["arena_id"]] = target.get(action["arena_id"], 0) + 1
    replacement = store._deck(Deck(name=current["metadata"]["name"], **zones),
                              current["metadata"]["name"], "malformed_replacement")
    identity = build_deck_snapshot_identity(replacement)
    if not _same(identity, intent["expected_result_deck_identity"]) or _same(
        identity, current["gameplay_snapshot_identity"]
    ):
        _fail("expected_result_mismatch")
    return replacement


def _fresh(value, intent):
    return _fresh_with_verifier(value, intent, require_pre_execution_revalidation)


def _fresh_v2(value, intent):
    return _fresh_with_verifier(value, intent, require_pre_execution_revalidation_v2)


def _fresh_with_verifier(value, intent, verifier):
    try:
        value = verifier(value)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        _fail("validation_unavailable")
    historical = intent["source_pre_execution_revalidation"]
    if not _same(value["source_human_proposal_decision"], historical["source_human_proposal_decision"]):
        _fail("invalid_intent")
    if not _same(value["current_validation_context"], historical["current_validation_context"]):
        _fail("execution_context_conflict")
    if not _same(value["reconstructed_result_deck_identity"], intent["expected_result_deck_identity"]):
        _fail("expected_result_mismatch")
    if not _same(value["approved_delta"], intent["derived_action"]) or not _same(
        value["current_baseline_deck_identity"], intent["source_destination_state"]["gameplay_snapshot_identity"]
    ):
        _fail("expected_result_mismatch")
    if value["reason"] == "validation_unavailable":
        _fail("validation_unavailable")
    validation = value["fresh_validation"]
    if validation is None:
        _fail("fresh_validation_failed")
    cost = validation["wildcard_cost"]
    if type(cost) is not dict or any(type(v) is not int or v < 0 for v in cost.values()):
        _fail("validation_unavailable")
    if sum(cost.values()) != 0:
        _fail("current_spend_required")
    resource = value["resource_assessment"]
    if value["status"] != "revalidated" or value["reason"] != "fresh_validation_passed" or (
        validation["valid"] is not True or validation["errors"] or resource["spending_authorized"] is not False
    ) or (resource["resource_mode"], resource["resource_status"]) not in (
        ("full_collection", "owned_no_crafting_required"), ("wildcard_budget", "no_spend_required")
    ) or not _same(cost, resource["wildcard_cost"]) or resource["resource_mode"] != value["current_validation_context"]["mode"]:
        _fail("fresh_validation_failed")
    return value


def _context(value, mode):
    if type(value) is not dict or set(value) != {
        "context_version", "authority_id", "generation", "card_database_identity", "collection", "inventory"
    } or value["context_version"] != "1":
        raise ValueError("invalid context")
    identifier = value["authority_id"]
    if type(identifier) is not str or str(UUID(identifier)) != identifier or UUID(identifier).version != 4:
        raise ValueError("invalid authority identity")
    if type(value["generation"]) is not int or value["generation"] < 1:
        raise ValueError("invalid generation")
    identity = value["card_database_identity"]
    if type(identity) is not dict or set(identity) != {"projection_version", "digest_algorithm", "digest"} or (
        identity["projection_version"] != "1" or identity["digest_algorithm"] != "sha256"
    ) or type(identity["digest"]) is not str or len(identity["digest"]) != 64 or any(
        c not in "0123456789abcdef" for c in identity["digest"]
    ):
        raise ValueError("invalid card projection identity")
    pairs = value["collection"]
    if type(pairs) is not list or any(type(p) is not list or len(p) != 2 for p in pairs):
        raise ValueError("invalid collection")
    collection = Collection(dict(pairs))
    inventory = value["inventory"]
    if inventory is not None:
        if type(inventory) is not dict or set(inventory) != {"wildcards", "gold", "gems", "vault_progress"}:
            raise ValueError("invalid inventory")
        inventory = Inventory(**inventory)
    _resource_inputs(collection, inventory, OperatingMode(mode))
    if not _same(pairs, [[k, collection.cards[k]] for k in sorted(collection.cards)]):
        raise ValueError("noncanonical collection")
    return deepcopy(value)


def _result(intent, previous, fresh, context, resulting):
    return _result_for_model(intent, previous, fresh, context, resulting, model_version="1")


def _result_v2(intent, previous, fresh, context, resulting):
    return _result_for_model(intent, previous, fresh, context, resulting, model_version="2")


def _result_for_model(intent, previous, fresh, context, resulting, *, model_version):
    # Called before commit; never returned to the caller until commit succeeds.
    artifact = {
        "local_deck_application_result_model_version": model_version, "status": "applied",
        "reason": "exact_local_add_committed", "source_local_deck_application_intent": deepcopy(intent),
        "source_local_deck_application_intent_identity": deepcopy(intent[_INTENT_ID]),
        "previous_destination_state": store.serialize_destination_state(previous),
        "execution_pre_execution_revalidation": deepcopy(fresh), "execution_context": deepcopy(context),
        "applied_action": deepcopy(intent["derived_action"]),
        "resulting_destination_state": store.serialize_destination_state(resulting),
        "resulting_deck_identity": deepcopy(resulting["gameplay_snapshot_identity"]),
        "previous_revision": previous["revision"], "new_revision": resulting["revision"],
        "execution_scope": deepcopy(_SCOPE), "limitations": deepcopy(
            _LIMITATIONS if model_version == "1" else _V2_LIMITATIONS),
    }
    payload = deepcopy(artifact)
    artifact[_ID] = {"local_deck_application_result_identity_version": "1",
                     "digest_algorithm": "sha256", "canonical_payload": payload,
                     "digest": hashlib.sha256(_encoded(payload)).hexdigest()}
    return artifact


def require_local_deck_application_result(value: dict) -> dict:
    """Strict Result 1 owner retained for durable v1 callers."""
    return _require_result(value,
        lambda artifact: require_local_deck_application_intent(artifact),
        lambda evidence, intent: _fresh(evidence, intent),
        lambda *args: _result(*args))


def require_local_deck_application_result_v2(value: dict) -> dict:
    """Strict historical Result 2 consistency, not durable proof of execution."""
    return _require_result(value, require_local_deck_application_intent_v2, _fresh_v2, _result_v2)


def _require_result(value, intent_verifier, fresh_verifier, result_builder) -> dict:
    """Historical consistency only, not proof of commit, freshness or authority."""
    try:
        if type(value) is not dict or set(value) != _FIELDS:
            raise ValueError("invalid shape")
        _encoded(value)
        intent = intent_verifier(value["source_local_deck_application_intent"])
        previous = store.require_destination_state(value["previous_destination_state"])
        if not _same(store.serialize_destination_state(previous), intent["source_destination_state"]):
            raise ValueError("different baseline")
        fresh = fresh_verifier(value["execution_pre_execution_revalidation"], intent)
        context = _context(value["execution_context"], fresh["current_validation_context"]["mode"])
        resulting = store.require_destination_state(value["resulting_destination_state"])
        if not _same(resulting["gameplay_snapshot_identity"], intent["expected_result_deck_identity"]):
            raise ValueError("different expected result")
        replacement = _reconstruct(intent, previous)
        wanted = {**previous, "deck": replacement, "revision": store._next_revision(previous),
                  "gameplay_snapshot_identity": build_deck_snapshot_identity(replacement)}
        if not _same(store.serialize_destination_state(resulting), store.serialize_destination_state(wanted)):
            raise ValueError("different result")
        expected = result_builder(intent, previous, fresh, context, resulting)
        if not _same(expected, value):
            raise ValueError("inconsistent artifact")
        return expected
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError, OverflowError):
        raise ValueError("invalid local deck application result") from None


def apply_local_deck_application(intent: dict, *, store_path,
                                 validation_authority: LocalExecutionValidationAuthorityV1) -> dict:
    """Execute one intent. Lock order is authority -> managed-store writer.

    No public replacement call, caller Deck, validation flags, or action overrides.
    An uncertain commit is an error, never inferred success or an automatic retry.
    """
    # Defer legacy global lookup until the original call positions in _apply.
    return _apply(intent, store_path=store_path, validation_authority=validation_authority,
        intent_verifier=lambda artifact: require_local_deck_application_intent(artifact),
        revalidation_builder=lambda *args, **kwargs: build_pre_execution_revalidation(*args, **kwargs),
        fresh_verifier=lambda evidence, verified: _fresh(evidence, verified),
        result_builder=lambda *args: _result(*args),
        result_verifier=lambda artifact: require_local_deck_application_result(artifact))


def apply_local_deck_application_v2(intent: dict, *, store_path,
                                    validation_authority: LocalExecutionValidationAuthorityV1) -> dict:
    """Apply one scoped intent atomically; acknowledgment is non-durable."""
    return _apply(intent, store_path=store_path, validation_authority=validation_authority,
        intent_verifier=require_local_deck_application_intent_v2,
        revalidation_builder=build_pre_execution_revalidation_v2, fresh_verifier=_fresh_v2,
        result_builder=_result_v2, result_verifier=require_local_deck_application_result_v2)


def _apply(intent, *, store_path, validation_authority, intent_verifier,
           revalidation_builder, fresh_verifier, result_builder, result_verifier):
    # Only fixed owners above supply these callables. Artifact data never dispatches.
    # One shared mutation sequence: authority guard -> writer -> checks -> commit.
    try:
        verified = intent_verifier(intent)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError):
        raise LocalDeckApplicationError("invalid_intent") from None
    if type(validation_authority) is not LocalExecutionValidationAuthorityV1:
        _fail("invalid_execution_context")
    with validation_authority._guarded() as view:
        with store._connection(store_path, write=True, mutation=True) as con:
            expected = store.require_destination_state(verified["source_destination_state"])
            meta, current = store._current_for_mutation(con, expected)
            if not _same(view["policy"], verified["source_pre_execution_revalidation"]["current_validation_context"]):
                _fail("execution_context_conflict")
            revision = store._next_revision(current)
            replacement = _reconstruct(verified, current)
            try:
                evidence = revalidation_builder(
                    verified["source_pre_execution_revalidation"]["source_human_proposal_decision"],
                    current["deck"], view["con"], format=view["format"], rules=view["rules"],
                    mode=view["mode"], collection=view["collection"], inventory=view["inventory"],
                )
            except (ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError, OverflowError):
                raise LocalDeckApplicationError("validation_unavailable") from None
            fresh = fresh_verifier(evidence, verified)
            resulting = store._replace_in_transaction(con, meta, current, replacement, current["metadata"], revision)
            try:
                result = result_verifier(
                    result_builder(verified, current, fresh, view["context"], resulting))
            except ValueError:
                raise store.ManagedDeckStoreError("result_mismatch") from None
        # Connection context commits before the authority guard is released.
    return result
