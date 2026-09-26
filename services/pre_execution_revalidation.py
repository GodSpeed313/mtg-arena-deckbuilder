"""Fresh, non-executable revalidation of one explicitly approved proposal."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, fields
import hashlib
import json
import sqlite3
from typing import Any

from mtgadb.deck_identity import build_deck_snapshot_identity, require_deck_snapshot_identity
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


def _closed(value: Any, expected: set[str], label: str) -> dict:
    if type(value) is not dict or set(value) != expected:
        raise ValueError(f"{label} has missing or unsupported fields")
    return value


def _same(left: Any, right: Any) -> bool:
    return _encoded(left) == _encoded(right)


def _require_json(value: Any) -> None:
    # Reject Python-only representations (including tuple/list substitutions)
    # before canonical JSON comparisons erase those distinctions.
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("revalidation object keys must be strings")
            _require_json(item)
    elif type(value) is list:
        for item in value:
            _require_json(item)
    elif value is not None and type(value) not in (str, int, float, bool):
        raise ValueError("revalidation artifact must contain JSON values")


def _require_current_context(value: Any) -> dict:
    context = _closed(value, {"mode", "format", "rules"}, "current validation context")
    format_args = deepcopy(_closed(
        context["format"], {field.name for field in fields(Format)}, "current format",
    ))
    rules_args = deepcopy(_closed(
        context["rules"], {field.name for field in fields(DeckRules)}, "current rules",
    ))

    def frozen(raw: Any) -> frozenset:
        if type(raw) is not list:
            raise ValueError("canonical set must be a list")
        return frozenset(raw)

    for key in ("legal_sets", "filter_sets", "banned_title_ids",
                "suppressed_title_ids", "suspended_title_ids"):
        format_args[key] = frozen(format_args[key])
    for key in ("allowed_title_ids", "allowed_commander_title_ids"):
        if format_args[key] is not None:
            format_args[key] = frozen(format_args[key])
    for key in ("individual_card_quotas", "rarity_card_quotas"):
        raw = format_args[key]
        if type(raw) is not list or any(type(pair) is not list or len(pair) != 2 for pair in raw):
            raise ValueError("canonical quota map must contain pairs")
        format_args[key] = dict(raw)
    restrictions = format_args["color_restrictions_internal"]
    if type(restrictions) is not list:
        raise ValueError("canonical color restrictions must be a list")
    format_args["color_restrictions_internal"] = tuple(frozen(item) for item in restrictions)
    if rules_args["allowed_colors"] is not None:
        rules_args["allowed_colors"] = frozen(rules_args["allowed_colors"])
    normalized = canonical_validation_context(
        format=Format(**format_args), rules=DeckRules(**rules_args),
        mode=OperatingMode(context["mode"]),
    )
    if not _same(value, normalized):
        raise ValueError("current validation context is not canonical")
    return normalized


def _require_fresh_validation(value: Any) -> dict:
    report = _closed(value, {"valid", "errors", "warnings", "wildcard_cost"}, "fresh validation")
    for key in ("errors", "warnings"):
        if type(report[key]) is not list:
            raise ValueError("validation issues must be lists")
        for issue in report[key]:
            _closed(issue, {"code", "message", "zone", "title_id"}, "validation issue")
            if any(type(issue[field]) is not str for field in ("code", "message")) or (
                issue["zone"] is not None and issue["zone"] not in _ZONES
            ) or (issue["title_id"] is not None and type(issue["title_id"]) is not int):
                raise ValueError("validation issue is malformed")
        if not _same(report[key], sorted(report[key], key=_encoded)):
            raise ValueError("validation issues are not canonical")
    if type(report["valid"]) is not bool or report["valid"] != (not report["errors"]):
        raise ValueError("validation validity contradicts errors")
    cost = report["wildcard_cost"]
    if type(cost) is not dict or any(
        not rarity or type(quantity) is not int or quantity < 0
        for rarity, quantity in cost.items()
    ):
        raise ValueError("validation wildcard cost is malformed")
    return deepcopy(report)


def require_pre_execution_revalidation(value: dict) -> dict:
    """Verify historical v1 evidence and return a detached copy.

    This does not consult current state, rerun validation, authenticate the
    evidence, or grant any authority. Negative results retain null identities.
    """
    try:
        _require_json(value)
        _encoded(value)  # Also reject non-finite numbers.
        return _require_revalidation(value)
    except (TypeError, KeyError, IndexError, RecursionError) as exc:
        raise ValueError("pre-execution revalidation artifact is malformed") from exc


def _require_revalidation(value: dict) -> dict:
    _closed(value, set(_result("", "")), "pre-execution revalidation")
    reason = value["reason"]
    statuses = {
        "fresh_validation_passed": "revalidated",
        "decision_declined": "not_ready",
        "baseline_snapshot_mismatch": "not_ready",
        "current_printing_unavailable": "not_ready",
        "current_printing_identity_mismatch": "not_ready",
        "current_validation_failed": "not_ready",
        "resource_authorization_required": "not_ready",
        "malformed_input": "rejected", "unsupported_version": "rejected",
        "identity_mismatch": "rejected", "result_identity_mismatch": "rejected",
        "validation_unavailable": "rejected",
    }
    if type(reason) is not str or reason not in statuses or value["status"] != statuses[reason]:
        raise ValueError("revalidation status/reason is unsupported or contradictory")
    args = {}

    def finish() -> dict:
        expected = _result(statuses[reason], reason, **args)
        if not _same(value, expected):
            raise ValueError("revalidation artifact contradicts its verified evidence or stage")
        return deepcopy(expected)

    def diagnostic() -> None:
        if type(value["evidence"]) is not str:
            raise ValueError("rejected outcome requires diagnostic text")
        args["evidence"] = value["evidence"]

    if value["source_human_proposal_decision"] is None:
        if reason not in ("malformed_input", "unsupported_version", "identity_mismatch"):
            raise ValueError("outcome requires a verified human decision")
        diagnostic()
        return finish()
    decision = require_human_proposal_decision(value["source_human_proposal_decision"])
    args["decision"] = decision
    if decision["decision"] == "declined":
        if reason != "decision_declined":
            raise ValueError("declined decision cannot proceed to revalidation")
        return finish()
    proposal = decision["proposal_identity"]["canonical_payload"]
    delta = proposal["delta"]
    args["delta"] = delta
    if reason == "malformed_input" and value["current_baseline_deck_identity"] is None:
        diagnostic()
        return finish()
    baseline = require_deck_snapshot_identity(value["current_baseline_deck_identity"])
    args["baseline_identity"] = baseline
    if not _same(baseline, proposal["source_baseline_deck_identity"]):
        if reason != "baseline_snapshot_mismatch":
            raise ValueError("current baseline contradicts reviewed baseline")
        args["evidence"] = {
            "approved_digest": proposal["source_baseline_deck_identity"]["digest"],
            "current_digest": baseline["digest"],
        }
        return finish()
    zones = {zone: dict(baseline["canonical_payload"][zone]) for zone in _ZONES}
    zone = zones[delta["zone"]]
    zone[delta["arena_id"]] = zone.get(delta["arena_id"], 0) + delta["quantity"]
    reconstructed = build_deck_snapshot_identity(Deck(**zones))
    if not _same(reconstructed, proposal["resulting_deck_identity"]):
        raise ValueError("approved result contradicts independently reconstructed delta")
    result = require_deck_snapshot_identity(value["reconstructed_result_deck_identity"])
    args["result_identity"] = result
    if reason == "result_identity_mismatch":
        # This diagnostic reports a failed reconstruction; it does not endorse
        # the reported result as the correct application of the approved delta.
        if _same(result, reconstructed):
            raise ValueError("result mismatch diagnostic reports a matching result")
        args["evidence"] = {
            "approved_digest": reconstructed["digest"], "reconstructed_digest": result["digest"],
        }
        return finish()
    if not _same(result, reconstructed):
        raise ValueError("result contradicts independently reconstructed delta")
    if reason == "malformed_input":
        diagnostic()
        return finish()
    context = _require_current_context(value["current_validation_context"])
    args["validation_context"] = context
    printing = value["current_printing_fact"]
    if printing is None:
        if reason == "validation_unavailable":
            diagnostic()
        elif reason != "current_printing_unavailable":
            raise ValueError("outcome requires current printing evidence")
        return finish()
    _closed(printing, {"arena_id", "title_id", "name", "set_code", "collector_number", "rarity"}, "printing fact")
    if not _same(printing["arena_id"], delta["arena_id"]) or any(
        type(printing[key]) is not str for key in ("name", "set_code", "collector_number", "rarity")
    ) or not printing["name"]:
        raise ValueError("current printing fact is malformed or contradicts approved printing")
    args["printing_fact"] = printing
    if not _same(printing["title_id"], delta["title_id"]):
        if reason != "current_printing_identity_mismatch":
            raise ValueError("current printing title contradicts approved title")
        args["evidence"] = {"approved_title_id": delta["title_id"], "current_title_id": printing["title_id"]}
        return finish()
    if reason == "validation_unavailable":
        diagnostic()
        return finish()
    validation = _require_fresh_validation(value["fresh_validation"])
    resource = _resource_assessment(OperatingMode(context["mode"]), validation)
    args.update(validation=validation, resource_assessment=resource)
    expected_reason = (
        "current_validation_failed" if not validation["valid"] else
        "resource_authorization_required" if resource["resource_status"] == "affordable_spend_not_authorized"
        else "fresh_validation_passed"
    )
    if reason != expected_reason:
        raise ValueError("outcome contradicts validation or resource assessment")
    if reason == "fresh_validation_passed":
        expected = _result("revalidated", reason, **args)
        payload_fields = {
            "pre_execution_revalidation_model_version", "status", "reason",
            "human_proposal_decision_identity", "proposal_identity", "presentation_identity",
            "approved_delta", "current_baseline_deck_identity", "reconstructed_result_deck_identity",
            "current_validation_context", "current_printing_fact", "fresh_validation",
            "resource_assessment", "destination_assessment",
        }
        args["revalidation_identity"] = _identity({key: expected[key] for key in payload_fields})
    return finish()


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
