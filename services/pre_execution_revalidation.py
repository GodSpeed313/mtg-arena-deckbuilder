"""Fresh, non-executable revalidation of one explicitly approved proposal."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import sqlite3
from typing import Any

from mtgadb.deck_identity import build_deck_snapshot_identity
from mtgadb.model import Collection, Deck, Format, Inventory
from mtgadb.modes import OperatingMode
from services.human_proposal_decision import require_human_proposal_decision
from services.proposal_presentation import canonical_validation_context
from services.validator import DeckRules, ValidationReport, validate_deck


PRE_EXECUTION_REVALIDATION_MODEL_VERSION = "1"
PRE_EXECUTION_REVALIDATION_IDENTITY_VERSION = "1"
IDENTITY_DIGEST_ALGORITHM = "sha256"

_ZONES = ("main", "sideboard", "commander")
_DESTINATION_ASSESSMENT = {
    "destination_status": "deferred",
    "destination_reason": "no_destination_contract",
}
_LIMITATIONS = [
    "A revalidated result is evidence of a current check, not execution authorization or a capability token.",
    "No Deck is mutated, persisted, exported, applied, or sent to Arena by this boundary.",
    "Resource feasibility does not authorize wildcard, currency, crafting, or other spending.",
    "Destination identity and saved-deck existence are not established here.",
    "Successful revalidation does not eliminate time-of-check/time-of-use risk; a future executor must independently verify authoritative current state immediately before mutation.",
    "Identity digests provide deterministic mismatch detection; they are not authentication, signatures, consent, or proof of human identity.",
]


def _encoded(value: dict) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("revalidation identity payload is not canonical JSON") from exc


def _identity(payload: dict) -> dict:
    return {
        "pre_execution_revalidation_identity_version": (
            PRE_EXECUTION_REVALIDATION_IDENTITY_VERSION
        ),
        "digest_algorithm": IDENTITY_DIGEST_ALGORITHM,
        "canonical_payload": deepcopy(payload),
        "digest": hashlib.sha256(_encoded(payload)).hexdigest(),
    }


def _result(status: str, reason: str, *, evidence: Any = None,
            decision: dict | None = None, delta: dict | None = None,
            baseline_identity: dict | None = None,
            result_identity: dict | None = None,
            validation_context: dict | None = None,
            printing_fact: dict | None = None,
            validation: dict | None = None,
            resource_assessment: dict | None = None,
            revalidation_identity: dict | None = None) -> dict:
    return {
        "pre_execution_revalidation_model_version": (
            PRE_EXECUTION_REVALIDATION_MODEL_VERSION
        ),
        "source_human_proposal_decision_model_version": (
            None if decision is None
            else decision["human_proposal_decision_model_version"]
        ),
        "status": status,
        "reason": reason,
        "evidence": deepcopy(evidence),
        "source_human_proposal_decision": deepcopy(decision),
        "human_proposal_decision_identity": (
            None if decision is None
            else deepcopy(decision["human_proposal_decision_identity"])
        ),
        "proposal_identity": (
            None if decision is None else deepcopy(decision["proposal_identity"])
        ),
        "presentation_identity": (
            None if decision is None else deepcopy(decision["presentation_identity"])
        ),
        "approved_delta": deepcopy(delta),
        "current_baseline_deck_identity": deepcopy(baseline_identity),
        "reconstructed_result_deck_identity": deepcopy(result_identity),
        "current_validation_context": deepcopy(validation_context),
        "current_printing_fact": deepcopy(printing_fact),
        "fresh_validation": deepcopy(validation),
        "resource_assessment": deepcopy(resource_assessment),
        "destination_assessment": deepcopy(_DESTINATION_ASSESSMENT),
        "pre_execution_revalidation_identity": deepcopy(revalidation_identity),
        "limitations": deepcopy(_LIMITATIONS),
    }


def _decision_failure_reason(exc: Exception) -> str:
    # Classify only after the owning verifier rejects the complete artifact.
    # Never inspect untrusted version values with hash-based membership tests.
    message = str(exc).casefold()
    if "version" in message or "algorithm" in message:
        return "unsupported_version"
    if any(word in message for word in ("identity", "digest", "contradict")):
        return "identity_mismatch"
    return "malformed_input"


def _well_formed_baseline(value: Any) -> bool:
    return isinstance(value, Deck) and type(value.deck_id) is str and (
        type(value.name) is str
    )


def _well_formed_collection(value: Any) -> bool:
    return isinstance(value, Collection) and isinstance(value.cards, dict) and all(
        type(arena_id) is int and arena_id > 0
        and type(quantity) is int and quantity >= 0
        for arena_id, quantity in value.cards.items()
    )


def _well_formed_inventory(value: Any) -> bool:
    return isinstance(value, Inventory) and isinstance(value.wildcards, dict) and all(
        type(rarity) is str and bool(rarity)
        and type(quantity) is int and quantity >= 0
        for rarity, quantity in value.wildcards.items()
    ) and all(type(quantity) is int and quantity >= 0 for quantity in (
        value.gold, value.gems, value.vault_progress,
    ))


def _printing_fact(row: sqlite3.Row) -> dict:
    fact = {
        "arena_id": row[0], "title_id": row[1], "name": row[2],
        "set_code": "" if row[3] is None else row[3],
        "collector_number": "" if row[4] is None else row[4],
        "rarity": "" if row[5] is None else row[5],
    }
    if any(type(fact[key]) is not str for key in (
        "name", "set_code", "collector_number", "rarity",
    )) or not fact["name"]:
        raise ValueError("current printing metadata is malformed")
    return fact


def _validation_evidence(report: ValidationReport) -> dict:
    return {
        "valid": report.valid,
        "errors": sorted((asdict(item) for item in report.errors), key=_encoded),
        "warnings": sorted((asdict(item) for item in report.warnings), key=_encoded),
        "wildcard_cost": {
            rarity: report.wildcard_cost[rarity]
            for rarity in sorted(report.wildcard_cost)
        },
    }


def _resource_assessment(mode: OperatingMode, validation: dict) -> dict:
    wildcard_cost = deepcopy(validation["wildcard_cost"])
    if mode is OperatingMode.UNLIMITED:
        status = "not_evaluated"
    elif not validation["valid"]:
        status = "unavailable"
    elif mode is OperatingMode.FULL_COLLECTION:
        status = "owned_no_crafting_required"
    elif sum(wildcard_cost.values()) == 0:
        status = "no_spend_required"
    else:
        status = "affordable_spend_not_authorized"
    return {
        "resource_mode": mode.value,
        "resource_status": status,
        "wildcard_cost": wildcard_cost,
        "spending_authorized": False,
    }


def build_pre_execution_revalidation(
    human_decision: dict,
    current_baseline_deck: Deck,
    con: sqlite3.Connection,
    *,
    format: Format,
    rules: DeckRules,
    mode: OperatingMode,
    collection: Collection | None = None,
    inventory: Inventory | None = None,
) -> dict:
    """Freshly check one approved delta without applying or authorizing it."""
    try:
        decision = require_human_proposal_decision(human_decision)
    except (TypeError, ValueError) as exc:
        reason = _decision_failure_reason(exc)
        return _result("rejected", reason, evidence=str(exc))

    if decision["decision"] == "declined":
        return _result("not_ready", "decision_declined", decision=decision)

    proposal_payload = decision["proposal_identity"]["canonical_payload"]
    delta = deepcopy(proposal_payload["delta"])
    expected_baseline = proposal_payload["source_baseline_deck_identity"]
    expected_result = proposal_payload["resulting_deck_identity"]

    if not _well_formed_baseline(current_baseline_deck):
        return _result(
            "rejected", "malformed_input", evidence="current baseline Deck is malformed",
            decision=decision, delta=delta,
        )
    try:
        baseline_identity = build_deck_snapshot_identity(current_baseline_deck)
    except ValueError as exc:
        return _result(
            "rejected", "malformed_input", evidence=str(exc),
            decision=decision, delta=delta,
        )
    if baseline_identity != expected_baseline:
        return _result(
            "not_ready", "baseline_snapshot_mismatch",
            evidence={
                "approved_digest": expected_baseline["digest"],
                "current_digest": baseline_identity["digest"],
            },
            decision=decision, delta=delta, baseline_identity=baseline_identity,
        )

    zones = {name: deepcopy(getattr(current_baseline_deck, name)) for name in _ZONES}
    zone = zones[delta["zone"]]
    zone[delta["arena_id"]] = zone.get(delta["arena_id"], 0) + delta["quantity"]
    working_deck = Deck(
        deck_id=current_baseline_deck.deck_id, name=current_baseline_deck.name,
        main=zones["main"], sideboard=zones["sideboard"],
        commander=zones["commander"],
    )
    result_identity = build_deck_snapshot_identity(working_deck)
    if result_identity != expected_result:
        return _result(
            "rejected", "result_identity_mismatch",
            evidence={
                "approved_digest": expected_result["digest"],
                "reconstructed_digest": result_identity["digest"],
            },
            decision=decision, delta=delta, baseline_identity=baseline_identity,
            result_identity=result_identity,
        )

    if not isinstance(format, Format) or not isinstance(rules, DeckRules) or (
        not isinstance(mode, OperatingMode)
    ):
        return _result(
            "rejected", "malformed_input",
            evidence="explicit Format, DeckRules, and OperatingMode are required",
            decision=decision, delta=delta, baseline_identity=baseline_identity,
            result_identity=result_identity,
        )
    if collection is not None and not _well_formed_collection(collection):
        return _result(
            "rejected", "malformed_input", evidence="collection is malformed",
            decision=decision, delta=delta, baseline_identity=baseline_identity,
            result_identity=result_identity,
        )
    if inventory is not None and not _well_formed_inventory(inventory):
        return _result(
            "rejected", "malformed_input", evidence="inventory is malformed",
            decision=decision, delta=delta, baseline_identity=baseline_identity,
            result_identity=result_identity,
        )
    try:
        validation_context = canonical_validation_context(
            format=format, rules=rules, mode=mode,
        )
    except ValueError as exc:
        return _result(
            "rejected", "malformed_input", evidence=str(exc),
            decision=decision, delta=delta, baseline_identity=baseline_identity,
            result_identity=result_identity,
        )
    if not isinstance(con, sqlite3.Connection):
        return _result(
            "rejected", "validation_unavailable",
            evidence="explicit SQLite connection is required", decision=decision,
            delta=delta, baseline_identity=baseline_identity,
            result_identity=result_identity, validation_context=validation_context,
        )

    try:
        row = con.execute(
            "SELECT p.arena_id, p.title_id, c.name, p.set_code, "
            "p.collector_number, p.rarity FROM printings p "
            "JOIN cards c ON c.title_id = p.title_id WHERE p.arena_id = ?",
            (delta["arena_id"],),
        ).fetchone()
        printing_fact = None if row is None else _printing_fact(row)
    except (sqlite3.Error, TypeError, ValueError, KeyError, IndexError) as exc:
        return _result(
            "rejected", "validation_unavailable", evidence=str(exc), decision=decision,
            delta=delta, baseline_identity=baseline_identity,
            result_identity=result_identity, validation_context=validation_context,
        )
    if printing_fact is None:
        return _result(
            "not_ready", "current_printing_unavailable", decision=decision,
            delta=delta, baseline_identity=baseline_identity,
            result_identity=result_identity, validation_context=validation_context,
        )
    if type(printing_fact["title_id"]) is not int or (
        printing_fact["title_id"] != delta["title_id"]
    ):
        return _result(
            "not_ready", "current_printing_identity_mismatch",
            evidence={
                "approved_title_id": delta["title_id"],
                "current_title_id": printing_fact["title_id"],
            },
            decision=decision, delta=delta, baseline_identity=baseline_identity,
            result_identity=result_identity, validation_context=validation_context,
            printing_fact=printing_fact,
        )

    try:
        report = validate_deck(
            working_deck, con, mode=mode, rules=rules, format=format,
            collection=collection, inventory=inventory,
        )
    except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        return _result(
            "rejected", "validation_unavailable", evidence=str(exc), decision=decision,
            delta=delta, baseline_identity=baseline_identity,
            result_identity=result_identity, validation_context=validation_context,
            printing_fact=printing_fact,
        )
    if not isinstance(report, ValidationReport):
        return _result(
            "rejected", "validation_unavailable",
            evidence="validator returned no ValidationReport", decision=decision,
            delta=delta, baseline_identity=baseline_identity,
            result_identity=result_identity, validation_context=validation_context,
            printing_fact=printing_fact,
        )
    validation = _validation_evidence(report)
    resource_assessment = _resource_assessment(mode, validation)
    if not validation["valid"]:
        return _result(
            "not_ready", "current_validation_failed", decision=decision, delta=delta,
            baseline_identity=baseline_identity, result_identity=result_identity,
            validation_context=validation_context, printing_fact=printing_fact,
            validation=validation, resource_assessment=resource_assessment,
        )
    if resource_assessment["resource_status"] == "affordable_spend_not_authorized":
        return _result(
            "not_ready", "resource_authorization_required", decision=decision,
            delta=delta, baseline_identity=baseline_identity,
            result_identity=result_identity, validation_context=validation_context,
            printing_fact=printing_fact, validation=validation,
            resource_assessment=resource_assessment,
        )

    semantic_payload = {
        "pre_execution_revalidation_model_version": (
            PRE_EXECUTION_REVALIDATION_MODEL_VERSION
        ),
        "status": "revalidated",
        "reason": "fresh_validation_passed",
        "human_proposal_decision_identity": deepcopy(
            decision["human_proposal_decision_identity"]
        ),
        "proposal_identity": deepcopy(decision["proposal_identity"]),
        "presentation_identity": deepcopy(decision["presentation_identity"]),
        "approved_delta": delta,
        "current_baseline_deck_identity": baseline_identity,
        "reconstructed_result_deck_identity": result_identity,
        "current_validation_context": validation_context,
        "current_printing_fact": printing_fact,
        "fresh_validation": validation,
        "resource_assessment": resource_assessment,
        "destination_assessment": deepcopy(_DESTINATION_ASSESSMENT),
    }
    revalidation_identity = _identity(semantic_payload)
    return _result(
        "revalidated", "fresh_validation_passed", decision=decision, delta=delta,
        baseline_identity=baseline_identity, result_identity=result_identity,
        validation_context=validation_context, printing_fact=printing_fact,
        validation=validation, resource_assessment=resource_assessment,
        revalidation_identity=revalidation_identity,
    )
