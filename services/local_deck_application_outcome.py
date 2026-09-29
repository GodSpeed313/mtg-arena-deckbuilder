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
from services.local_deck_application_intent import require_local_deck_application_intent
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
    try:
        return require_local_deck_application_intent(value)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError, OverflowError):
        raise LocalDeckApplicationError("invalid_intent") from None


def _operation(intent, operation_id):
    destination = intent["source_destination_state"]
    return {
        "local_deck_application_operation_model_version": "1", "operation_id": _uuid(operation_id),
        "store_id": destination["store_id"], "store_generation": destination["store_generation"],
        "record_id": destination["record_id"], "expected_revision": destination["revision"],
        "source_intent": deepcopy(intent), "source_intent_identity": deepcopy(intent[_INTENT_ID]),
    }


def _require_operation(value):
    try:
        if type(value) is not dict or set(value) != _OP_FIELDS:
            raise ValueError("invalid shape")
        _encoded(value)
        intent = require_local_deck_application_intent(value["source_intent"])
        expected = _operation(intent, value["operation_id"])
        if not _same(expected, value):
            raise ValueError("invalid binding")
        return expected
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError, OverflowError):
        _fail("invalid_operation")


def _receipt(operation, result):
    value = {
        "local_deck_application_receipt_model_version": "1", "status": "committed",
        "reason": "deck_and_receipt_committed_atomically", "operation_id": operation["operation_id"],
        "store_id": operation["store_id"], "store_generation": operation["store_generation"],
        "source_intent_identity": deepcopy(operation["source_intent_identity"]),
        "application_result": deepcopy(result), "limitations": deepcopy(_LIMITATIONS),
    }
    payload = deepcopy(value)
    value[_RECEIPT_ID] = {"local_deck_application_receipt_identity_version": "1",
                          "digest_algorithm": "sha256", "canonical_payload": payload,
                          "digest": hashlib.sha256(_encoded(payload)).hexdigest()}
    return value


def require_local_deck_application_receipt(value: dict) -> dict:
    """Detached historical consistency; no IO or proof of durable storage."""
    try:
        if type(value) is not dict or set(value) != _RECEIPT_FIELDS:
            raise ValueError("invalid shape")
        _encoded(value)
        result = application.require_local_deck_application_result(value["application_result"])
        operation = _operation(result["source_local_deck_application_intent"], value["operation_id"])
        expected = _receipt(operation, result)
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
    if operation_id is not None:
        row = con.execute("SELECT * FROM managed_application_operations WHERE operation_id=?", (operation_id,)).fetchone()
    else:
        row = con.execute("SELECT * FROM managed_application_operations WHERE store_generation=? AND intent_digest=?",
                          (meta["store_generation"], intent_digest)).fetchone()
    if row is None:
        return None
    try:
        intent = require_local_deck_application_intent(_loads(row["intent_json"]))
        operation = _operation(intent, row["operation_id"])
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
        receipt = require_local_deck_application_receipt(_loads(row["receipt_json"]))
        if not _same(receipt, _receipt(operation, receipt["application_result"])) or not _same(
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
    verified = _intent(intent)  # Before IO.
    with store._connection(store_path, write=True, mutation=True) as con:
        meta = _store(con, verified)
        existing = _read_operation(con, meta, intent_digest=verified[_INTENT_ID]["digest"])
        if existing is not None:
            _match_intent(existing, verified)
            operation = existing["operation"]
        else:
            store._current_for_mutation(con, store.require_destination_state(verified["source_destination_state"]))
            operation = _operation(verified, str(uuid4()))
            if _read_operation(con, meta, operation_id=operation["operation_id"]) is not None:
                _fail("operation_binding_conflict")
            con.execute("INSERT INTO managed_application_operations VALUES (?,?,?,?,?,?,'prepared',NULL)", (
                operation["operation_id"], operation["store_generation"], verified[_INTENT_ID]["digest"],
                operation["record_id"], operation["expected_revision"], _encoded(verified).decode("utf-8"),
            ))
            observed = _read_operation(con, meta, operation_id=operation["operation_id"])
            if observed is None or observed["state"] != "prepared" or not _same(observed["operation"], operation):
                _fail("operation_binding_conflict")
    return operation


def execute_prepared_local_deck_application(operation: dict, *, store_path,
                                            validation_authority: LocalExecutionValidationAuthorityV1) -> dict:
    """New #6P path; deck and receipt share one transaction and commit.

    Lock order: authority -> managed-store BEGIN IMMEDIATE. No nested public
    executor, automatic retry or historical-success shortcut is used.
    """
    operation = _require_operation(operation)  # Includes owning #6N verification before IO.
    verified = operation["source_intent"]
    if type(validation_authority) is not LocalExecutionValidationAuthorityV1:
        raise LocalDeckApplicationError("invalid_execution_context")
    with validation_authority._guarded() as view:
        with store._connection(store_path, write=True, mutation=True) as con:
            meta = _store(con, verified)
            existing = _read_operation(con, meta, operation_id=operation["operation_id"])
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
                evidence = application.build_pre_execution_revalidation(
                    verified["source_pre_execution_revalidation"]["source_human_proposal_decision"],
                    current["deck"], view["con"], format=view["format"], rules=view["rules"],
                    mode=view["mode"], collection=view["collection"], inventory=view["inventory"],
                )
            except (ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError, OverflowError):
                raise LocalDeckApplicationError("validation_unavailable") from None
            fresh = application._fresh(evidence, verified)
            resulting = store._replace_in_transaction(con, meta, current, replacement, current["metadata"], revision)
            try:
                result = application.require_local_deck_application_result(
                    application._result(verified, current, fresh, view["context"], resulting))
            except ValueError:
                raise store.ManagedDeckStoreError("result_mismatch") from None
            try:
                receipt = require_local_deck_application_receipt(_receipt(operation, result))
            except ValueError:
                _fail("receipt_mismatch")
            changed = con.execute(
                "UPDATE managed_application_operations SET state='committed',receipt_json=? "
                "WHERE operation_id=? AND state='prepared' AND receipt_json IS NULL",
                (_encoded(receipt).decode("utf-8"), operation["operation_id"]),
            )
            if changed.rowcount != 1:
                _fail("operation_state_conflict")
            observed = _read_operation(con, meta, operation_id=operation["operation_id"])
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
    verified = _intent(intent)
    with store._connection(store_path, write=True, mutation=True) as con:
        meta = _store(con, verified)
        observed = _read_operation(con, meta, intent_digest=verified[_INTENT_ID]["digest"])
        if observed is not None:
            _match_intent(observed, verified)
        status = "not_found" if observed is None else observed["state"]
        result = {
            "local_deck_application_recovery_model_version": "1", "status": status,
            "reason": {"not_found": "no_registration_observed", "prepared": "no_committed_receipt_observed",
                       "committed": "matching_durable_receipt_observed"}[status],
            "store_id": meta["store_id"], "store_generation": meta["store_generation"],
            "source_intent_identity": deepcopy(verified[_INTENT_ID]),
            "operation": None if observed is None else observed["operation"],
            "receipt": None if observed is None else observed["receipt"],
            "limitations": deepcopy(_RECOVERY_LIMITATIONS),
        }
    return result
