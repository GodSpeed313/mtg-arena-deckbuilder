"""Durable prepare/execute/recover for local applications (#6P v1).

This is a separate path. The public receipt-free #6O executor is never called,
changed or retroactively assigned a receipt. A supplied artifact is not proof
of persistence; recovery reads the exact managed store under a writer barrier.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from uuid import UUID, uuid4

from mtgadb import managed_deck_store as store
from services import local_deck_application as application
from services.local_deck_application_intent import (
    require_local_deck_application_intent, require_local_deck_application_intent_v2,
)
from services.local_execution_validation import (
    LocalDeckApplicationError, LocalExecutionValidationAuthorityV1, _encoded,
)


LOCAL_DECK_APPLICATION_OPERATION_MODEL_VERSION = "1"
LOCAL_DECK_APPLICATION_RECEIPT_MODEL_VERSION = "1"
LOCAL_DECK_APPLICATION_RECEIPT_IDENTITY_VERSION = "1"
LOCAL_DECK_APPLICATION_RECOVERY_MODEL_VERSION = "1"
_INTENT_ID = "local_deck_application_intent_identity"
_RECEIPT_ID = "local_deck_application_receipt_identity"
_OP_FIELDS = {"local_deck_application_operation_model_version", "operation_id", "store_id",
              "store_generation", "record_id", "expected_revision", "source_intent", "source_intent_identity"}
_RECEIPT_FIELDS = {"local_deck_application_receipt_model_version", "status", "reason", "operation_id",
                   "store_id", "store_generation", "source_intent_identity", "application_result",
                   "limitations", _RECEIPT_ID}
_ERRORS = frozenset({"invalid_operation", "operation_not_found", "operation_binding_conflict",
                     "operation_state_conflict", "malformed_operation", "receipt_mismatch"})
_LIMITATIONS = [
    "This is durable local evidence only when recovered from the identified managed store generation, conditional on store continuity; a supplied artifact alone is not durable proof.",
    "A committed receipt describes a historical application, not the current contents, metadata or lifecycle of its destination.",
    "No exactly-once delivery, automatic retry, live Arena ownership, live wildcard/account state, or synchronization with Arena is established.",
    "Digests provide mismatch detection, not authenticated human identity, approval authority, non-repudiation, or protection against coherent external rollback, copying or tampering.",
    "The original local-authority and nonparticipating-writer limitations remain in force; receipt-free #6O executions cannot be retrospectively recovered through receipts.",
]
_RECOVERY_LIMITATIONS = [
    *_LIMITATIONS,
    "Prepared and not_found are observations at this transaction boundary, not terminal cancellation, proof of no future execution, or permission to retry automatically.",
]


class LocalDeckApplicationOutcomeError(ValueError):
    """Closed operation/receipt failures without stored data in messages."""

    def __init__(self, code):
        if code not in _ERRORS:
            raise ValueError("Unsupported application outcome error code")
        self.code = code
        super().__init__(code)


def _fail(code):
    raise LocalDeckApplicationOutcomeError(code) from None


def _uuid(value):
    if type(value) is not str or str(UUID(value)) != value or UUID(value).version != 4:
        raise ValueError("invalid UUID")
    return value


def _same(left, right):
    return _encoded(left) == _encoded(right)


def _intent(value):
    return _verify_intent(value, lambda artifact: require_local_deck_application_intent(artifact))


def _verify_intent(value, verifier):
    try:
        return verifier(value)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError, OverflowError):
        raise LocalDeckApplicationError("invalid_intent") from None


def _operation(intent, operation_id):
    return _build_operation(intent, operation_id, model_version="1")


def _build_operation(intent, operation_id, *, model_version):
    destination = intent["source_destination_state"]
    return {
        "local_deck_application_operation_model_version": model_version, "operation_id": _uuid(operation_id),
        "store_id": destination["store_id"], "store_generation": destination["store_generation"],
        "record_id": destination["record_id"], "expected_revision": destination["revision"],
        "source_intent": deepcopy(intent), "source_intent_identity": deepcopy(intent[_INTENT_ID]),
    }


def _require_operation(value):
    return _verify_operation(value, _v1_owners())


def _verify_operation(value, owners):
    try:
        if type(value) is not dict or set(value) != _OP_FIELDS:
            raise ValueError("invalid shape")
        _encoded(value)
        intent = owners["intent_verifier"](value["source_intent"])
        expected = owners["operation_builder"](intent, value["operation_id"])
        if not _same(expected, value):
            raise ValueError("invalid binding")
        return expected
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError, OverflowError):
        _fail("invalid_operation")


def _receipt(operation, result):
    return _build_receipt(operation, result, model_version="1", limitations=_LIMITATIONS)


def _build_receipt(operation, result, *, model_version, limitations):
    value = {
        "local_deck_application_receipt_model_version": model_version, "status": "committed",
        "reason": "deck_and_receipt_committed_atomically", "operation_id": operation["operation_id"],
        "store_id": operation["store_id"], "store_generation": operation["store_generation"],
        "source_intent_identity": deepcopy(operation["source_intent_identity"]),
        "application_result": deepcopy(result), "limitations": deepcopy(limitations),
    }
    payload = deepcopy(value)
    value[_RECEIPT_ID] = {"local_deck_application_receipt_identity_version": "1",
                          "digest_algorithm": "sha256", "canonical_payload": payload,
                          "digest": hashlib.sha256(_encoded(payload)).hexdigest()}
    return value


def require_local_deck_application_receipt(value: dict) -> dict:
    """Detached historical consistency; no IO or proof of durable storage."""
    return _verify_receipt(value, _v1_owners())


def _verify_receipt(value, owners):
    try:
        if type(value) is not dict or set(value) != _RECEIPT_FIELDS:
            raise ValueError("invalid shape")
        _encoded(value)
        result = owners["result_verifier"](value["application_result"])
        operation = owners["operation_builder"](result["source_local_deck_application_intent"], value["operation_id"])
        expected = owners["receipt_builder"](operation, result)
        if not _same(value, expected):
            raise ValueError("invalid binding")
        return expected
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError, OverflowError):
        raise ValueError("invalid local deck application receipt") from None


def _store(con, intent):
    meta = store._schema(con)
    if meta["schema_version"] != store.OUTCOME_SCHEMA_VERSION:
        raise store.ManagedDeckStoreError("unsupported_schema")
    expected = intent["source_destination_state"]
    if meta["store_id"] != expected["store_id"]:
        raise store.ManagedDeckStoreError("store_identity_mismatch")
    if meta["store_generation"] != expected["store_generation"]:
        raise store.ManagedDeckStoreError("store_generation_mismatch")
    return meta


def _loads(raw):
    # Re-encoding also rejects noncanonical JSON, duplicate keys, NaN, etc.
    if type(raw) is not str:
        raise ValueError("invalid persisted JSON")
    value = json.loads(raw)
    if _encoded(value).decode("utf-8") != raw:
        raise ValueError("noncanonical persisted JSON")
    return value


def _read_operation(con, meta, *, operation_id=None, intent_digest=None):
    return _read_expected_operation(con, meta, operation_id=operation_id, intent_digest=intent_digest, owners=_v1_owners())


def _read_expected_operation(con, meta, *, operation_id=None, intent_digest=None, owners):
    if operation_id is not None:
        row = con.execute("SELECT * FROM managed_application_operations WHERE operation_id=?", (operation_id,)).fetchone()
    else:
        row = con.execute("SELECT * FROM managed_application_operations WHERE store_generation=? AND intent_digest=?",
                          (meta["store_generation"], intent_digest)).fetchone()
    if row is None:
        return None
    try:
        intent = owners["intent_verifier"](_loads(row["intent_json"]))
        operation = owners["operation_builder"](intent, row["operation_id"])
        if operation["store_id"] != meta["store_id"] or operation["store_generation"] != meta["store_generation"] or (
            not _same([row["store_generation"], row["intent_digest"], row["record_id"], row["expected_revision"]],
                      [operation["store_generation"], intent[_INTENT_ID]["digest"], operation["record_id"], operation["expected_revision"]])
        ) or row["state"] not in ("prepared", "committed"):
            raise ValueError("invalid stored binding")
        if row["state"] == "prepared":
            if row["receipt_json"] is not None:
                raise ValueError("prepared receipt")
            return {"operation": operation, "state": "prepared", "receipt": None}
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError, OverflowError):
        _fail("malformed_operation")
    try:
        receipt = owners["receipt_verifier"](_loads(row["receipt_json"]))
        if not _same(receipt, owners["receipt_builder"](operation, receipt["application_result"])) or not _same(
            receipt["application_result"]["source_local_deck_application_intent"], operation["source_intent"]
        ):
            raise ValueError("receipt does not match operation")
        return {"operation": operation, "state": "committed", "receipt": receipt}
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError, OverflowError):
        _fail("receipt_mismatch")


def _match_intent(stored, intent):
    if not _same(stored["operation"]["source_intent"], intent):
        _fail("operation_binding_conflict")


def prepare_local_deck_application(intent: dict, *, store_path) -> dict:
    """Register one intent without resource validation or deck changes.

    Returns the same immutable handle for a fully matching existing registration,
    even if since committed. The handle is not a current lifecycle observation.
    New registration requires the exact live destination still to match #6N.
    """
    return _prepare(intent, store_path=store_path, owners=_v1_owners())


def _prepare(intent, *, store_path, owners):
    verified = owners["intent"](intent)  # Before IO.
    with store._connection(store_path, write=True, mutation=True) as con:
        meta = _store(con, verified)
        existing = owners["reader"](con, meta, intent_digest=verified[_INTENT_ID]["digest"])
        if existing is not None:
            _match_intent(existing, verified)
            operation = existing["operation"]
        else:
            store._current_for_mutation(con, store.require_destination_state(verified["source_destination_state"]))
            operation = owners["operation_builder"](verified, str(uuid4()))
            if owners["reader"](con, meta, operation_id=operation["operation_id"]) is not None:
                _fail("operation_binding_conflict")
            con.execute("INSERT INTO managed_application_operations VALUES (?,?,?,?,?,?,'prepared',NULL)", (
                operation["operation_id"], operation["store_generation"], verified[_INTENT_ID]["digest"],
                operation["record_id"], operation["expected_revision"], _encoded(verified).decode("utf-8"),
            ))
            observed = owners["reader"](con, meta, operation_id=operation["operation_id"])
            if observed is None or observed["state"] != "prepared" or not _same(observed["operation"], operation):
                _fail("operation_binding_conflict")
    return operation


def execute_prepared_local_deck_application(operation: dict, *, store_path,
                                            validation_authority: LocalExecutionValidationAuthorityV1) -> dict:
    """New #6P path; deck and receipt share one transaction and commit.

    Lock order: authority -> managed-store BEGIN IMMEDIATE. No nested public
    executor, automatic retry or historical-success shortcut is used.
    """
    return _execute_prepared(operation, store_path=store_path, validation_authority=validation_authority, owners=_v1_owners())


def _execute_prepared(operation, *, store_path, validation_authority, owners):
    operation = owners["operation_verifier"](operation)  # Includes owning #6N verification before IO.
    verified = operation["source_intent"]
    if type(validation_authority) is not LocalExecutionValidationAuthorityV1:
        raise LocalDeckApplicationError("invalid_execution_context")
    with validation_authority._guarded() as view:
        with store._connection(store_path, write=True, mutation=True) as con:
            meta = _store(con, verified)
            existing = owners["reader"](con, meta, operation_id=operation["operation_id"])
            if existing is None:
                _fail("operation_not_found")
            if not _same(existing["operation"], operation):
                _fail("operation_binding_conflict")
            if existing["state"] != "prepared":
                _fail("operation_state_conflict")
            expected = store.require_destination_state(verified["source_destination_state"])
            meta, current = store._current_for_mutation(con, expected)
            if not _same(view["policy"], verified["source_pre_execution_revalidation"]["current_validation_context"]):
                raise LocalDeckApplicationError("execution_context_conflict")
            revision = store._next_revision(current)
            replacement = application._reconstruct(verified, current)
            try:
                evidence = owners["revalidation_builder"](
                    verified["source_pre_execution_revalidation"]["source_human_proposal_decision"],
                    current["deck"], view["con"], format=view["format"], rules=view["rules"],
                    mode=view["mode"], collection=view["collection"], inventory=view["inventory"],
                )
            except (ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError, OverflowError):
                raise LocalDeckApplicationError("validation_unavailable") from None
            fresh = owners["fresh_verifier"](evidence, verified)
            resulting = store._replace_in_transaction(con, meta, current, replacement, current["metadata"], revision)
            try:
                result = owners["result_verifier"](
                    owners["result_builder"](verified, current, fresh, view["context"], resulting))
            except ValueError:
                raise store.ManagedDeckStoreError("result_mismatch") from None
            try:
                receipt = owners["receipt_verifier"](owners["receipt_builder"](operation, result))
            except ValueError:
                _fail("receipt_mismatch")
            changed = con.execute(
                "UPDATE managed_application_operations SET state='committed',receipt_json=? "
                "WHERE operation_id=? AND state='prepared' AND receipt_json IS NULL",
                (_encoded(receipt).decode("utf-8"), operation["operation_id"]),
            )
            if changed.rowcount != 1:
                _fail("operation_state_conflict")
            observed = owners["reader"](con, meta, operation_id=operation["operation_id"])
            if observed is None or observed["state"] != "committed" or not _same(observed["operation"], operation) or (
                not _same(observed["receipt"], receipt)
            ):
                _fail("receipt_mismatch")
        # Commit and connection cleanup finish while the authority is guarded.
    return receipt


def recover_local_deck_application(intent: dict, *, store_path) -> dict:
    """Observe outcome behind a writer barrier, with no logical state changes.

    Prepared/not_found are not permission to retry or proof of no future commit.
    Do not read current deck contents: later changes/deletion preserve receipts.
    """
    return _recover(intent, store_path=store_path, owners=_v1_owners())


def _recover(intent, *, store_path, owners):
    verified = owners["intent"](intent)
    with store._connection(store_path, write=True, mutation=True) as con:
        meta = _store(con, verified)
        observed = owners["reader"](con, meta, intent_digest=verified[_INTENT_ID]["digest"])
        if observed is not None:
            _match_intent(observed, verified)
        status = "not_found" if observed is None else observed["state"]
        result = {
            "local_deck_application_recovery_model_version": owners["model_version"], "status": status,
            "reason": {"not_found": "no_registration_observed", "prepared": "no_committed_receipt_observed",
                       "committed": "matching_durable_receipt_observed"}[status],
            "store_id": meta["store_id"], "store_generation": meta["store_generation"],
            "source_intent_identity": deepcopy(verified[_INTENT_ID]),
            "operation": None if observed is None else observed["operation"],
            "receipt": None if observed is None else observed["receipt"],
            "limitations": deepcopy(owners["recovery_limitations"]()),
        }
    return result


def _v1_owners():
    """Trusted Model 1 expectation; late adapters preserve existing patch points.

    No stored discriminator selects an owner. Each attribute is resolved only
    when the shared engine reaches its original verification/call boundary.
    """
    return {
        "model_version": "1",
        "intent": lambda value: _intent(value),
        "intent_verifier": lambda value: require_local_deck_application_intent(value),
        "operation_builder": lambda *args: _operation(*args),
        "operation_verifier": lambda value: _require_operation(value),
        "receipt_builder": lambda *args: _receipt(*args),
        "receipt_verifier": lambda value: require_local_deck_application_receipt(value),
        "reader": lambda *args, **kwargs: _read_operation(*args, **kwargs),
        "revalidation_builder": lambda *args, **kwargs: application.build_pre_execution_revalidation(*args, **kwargs),
        "fresh_verifier": lambda *args: application._fresh(*args),
        "result_builder": lambda *args: application._result(*args),
        "result_verifier": lambda value: application.require_local_deck_application_result(value),
        "recovery_limitations": lambda: _RECOVERY_LIMITATIONS,
    }


_V2_LIMITATIONS = [*_LIMITATIONS,
    "Scoped evidence is historical digest-bound provenance; durable execution does not rerun analysis or establish that the scoped need was resolved or improved.",
    "Decision 2 is not destination-bound, is not globally consumed, and is not globally single-use. Intent 2 selects one destination for this application; another explicitly requested matching destination may succeed.",
    "This separate Model 2 durable receipt does not change #6T's receipt-free application or its nested Intent/Result 2 contracts. An embedded Result 2 alone remains a non-durable acknowledgment; receipt-free writes are not retroactively recoverable here.",
]
_V2_RECOVERY_LIMITATIONS = [*_V2_LIMITATIONS,
    "Prepared and not_found are observations at this transaction boundary, not terminal cancellation, proof of no future execution, or permission to retry automatically.",
]


def _intent_v2(value):
    return _verify_intent(value, lambda artifact: require_local_deck_application_intent_v2(artifact))


def _operation_v2(intent, operation_id):
    return _build_operation(intent, operation_id, model_version="2")


def _require_operation_v2(value):
    return _verify_operation(value, _v2_owners())


def _receipt_v2(operation, result):
    return _build_receipt(operation, result, model_version="2", limitations=_V2_LIMITATIONS)


def require_local_deck_application_receipt_v2(value: dict) -> dict:
    """Verify strict Receipt 2 historical consistency, not proof of persistence."""
    return _verify_receipt(value, _v2_owners())


def _read_operation_v2(con, meta, *, operation_id=None, intent_digest=None):
    """Caller-selected Model 2 expectation; stored models never choose owners."""
    return _read_expected_operation(con, meta, operation_id=operation_id,
                                    intent_digest=intent_digest, owners=_v2_owners())


def prepare_local_deck_application_v2(intent: dict, *, store_path) -> dict:
    """Register Intent 2 only; do not validate resources or mutate the deck."""
    return _prepare(intent, store_path=store_path, owners=_v2_owners())


def execute_prepared_local_deck_application_v2(operation: dict, *, store_path,
        validation_authority: LocalExecutionValidationAuthorityV1) -> dict:
    """Atomically commit the exact scoped add and Receipt 2 in one writer transaction."""
    return _execute_prepared(operation, store_path=store_path,
                             validation_authority=validation_authority, owners=_v2_owners())


def recover_local_deck_application_v2(intent: dict, *, store_path) -> dict:
    """Observe verified Model 2 durable state; no execution, revalidation or retry."""
    return _recover(intent, store_path=store_path, owners=_v2_owners())


def _v2_owners():
    """Trusted Model 2 expectation, independent of all artifact discriminators."""
    return {
        "model_version": "2",
        "intent": lambda value: _intent_v2(value),
        "intent_verifier": lambda value: require_local_deck_application_intent_v2(value),
        "operation_builder": lambda *args: _operation_v2(*args),
        "operation_verifier": lambda value: _require_operation_v2(value),
        "receipt_builder": lambda *args: _receipt_v2(*args),
        "receipt_verifier": lambda value: require_local_deck_application_receipt_v2(value),
        "reader": lambda *args, **kwargs: _read_operation_v2(*args, **kwargs),
        "revalidation_builder": lambda *args, **kwargs: application.build_pre_execution_revalidation_v2(*args, **kwargs),
        "fresh_verifier": lambda *args: application._fresh_v2(*args),
        "result_builder": lambda *args: application._result_v2(*args),
        "result_verifier": lambda value: application.require_local_deck_application_result_v2(value),
        "recovery_limitations": lambda: _V2_RECOVERY_LIMITATIONS,
    }
