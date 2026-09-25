"""Record one explicit human decision about one verified review artifact.

This boundary records a caller-supplied decision. It does not authenticate the
caller, establish consent, authorize execution or resources, or perform work.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any

from services.proposal_presentation import require_proposal_presentation


HUMAN_PROPOSAL_DECISION_MODEL_VERSION = "1"
HUMAN_PROPOSAL_DECISION_IDENTITY_VERSION = "1"
IDENTITY_DIGEST_ALGORITHM = "sha256"

_DECISIONS = frozenset({"approved", "declined"})
_SOURCE_KINDS = frozenset({"explicit_user", "explicit_operator"})
_DECISION_FIELDS = frozenset({"decision", "decision_source"})
_OUTPUT_FIELDS = frozenset({
    "human_proposal_decision_model_version",
    "source_proposal_presentation_model_version", "decision", "decision_source",
    "proposal_identity", "presentation_identity", "source_presentation",
    "human_proposal_decision_identity", "limitations",
})
_IDENTITY_PAYLOAD_FIELDS = frozenset({
    "human_proposal_decision_model_version",
    "source_proposal_presentation_model_version", "decision", "decision_source",
    "proposal_identity", "presentation_identity",
})
_LIMITATIONS = [
    "This record captures a caller-asserted explicit human decision; it does not authenticate the human or prove legal identity, consent, or nonrepudiation.",
    "Approval is not execution authorization and does not establish that captured validation remains current.",
    "Neither decision grants ownership, wildcard, affordability, or resource-spending authority.",
    "This boundary does not mutate, persist, export, apply, or execute the proposal.",
    "Identity digests provide deterministic mismatch detection; they are not signatures, credentials, or authorization tokens.",
]


def _mapping(value: Any, label: str, fields: frozenset[str]) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{label} has missing or unsupported fields")
    return value


def _encoded(payload: dict) -> bytes:
    try:
        return json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("decision identity payload is not canonical JSON") from exc


def _require_identity(value: Any) -> dict:
    identity = _mapping(
        value, "human proposal decision identity",
        frozenset({
            "human_proposal_decision_identity_version", "digest_algorithm",
            "canonical_payload", "digest",
        }),
    )
    if identity["human_proposal_decision_identity_version"] != (
        HUMAN_PROPOSAL_DECISION_IDENTITY_VERSION
    ) or identity["digest_algorithm"] != IDENTITY_DIGEST_ALGORITHM or (
        not isinstance(identity["canonical_payload"], dict)
        or type(identity["digest"]) is not str
    ):
        raise ValueError("Human Proposal Decision Identity Version 1 is required")
    expected = hashlib.sha256(_encoded(identity["canonical_payload"])).hexdigest()
    if identity["digest"] != expected:
        raise ValueError("human proposal decision identity digest is contradictory")
    return deepcopy(identity)


def _decision_source(value: Any) -> dict:
    source = _mapping(
        value, "decision source", frozenset({"kind", "provenance"}),
    )
    if type(source["kind"]) is not str or source["kind"] not in _SOURCE_KINDS:
        raise ValueError("human decision requires an explicit supported source")
    provenance = _mapping(
        source["provenance"], "decision provenance", frozenset({"reference"}),
    )
    reference = provenance["reference"]
    if type(reference) is not str or not reference.strip():
        raise ValueError("decision provenance reference must be non-empty")
    return {
        "kind": source["kind"],
        "provenance": {"reference": reference.strip()},
    }


def require_human_proposal_decision(value: dict) -> dict:
    """Require one complete, internally consistent Human Decision Model v1."""
    decision_record = _mapping(value, "human proposal decision", _OUTPUT_FIELDS)
    if decision_record["human_proposal_decision_model_version"] != (
        HUMAN_PROPOSAL_DECISION_MODEL_VERSION
    ) or decision_record["source_proposal_presentation_model_version"] != "1":
        raise ValueError("Human Proposal Decision Model Version 1 is required")
    decision = decision_record["decision"]
    if type(decision) is not str or decision not in _DECISIONS:
        raise ValueError("human proposal decision must be approved or declined")
    source = _decision_source(decision_record["decision_source"])
    if source != decision_record["decision_source"]:
        raise ValueError("human proposal decision source is not normalized")
    presentation = require_proposal_presentation(decision_record["source_presentation"])
    if _encoded(decision_record["proposal_identity"]) != _encoded(presentation["proposal_identity"]) or (
        _encoded(decision_record["presentation_identity"]) != _encoded(presentation["presentation_identity"])
    ):
        raise ValueError("human decision identities contradict the source presentation")
    identity = _require_identity(decision_record["human_proposal_decision_identity"])
    payload = _mapping(
        identity["canonical_payload"], "human decision identity payload",
        _IDENTITY_PAYLOAD_FIELDS,
    )
    expected_payload = {
        "human_proposal_decision_model_version": HUMAN_PROPOSAL_DECISION_MODEL_VERSION,
        "source_proposal_presentation_model_version": "1",
        "decision": decision,
        "decision_source": source,
        "proposal_identity": presentation["proposal_identity"],
        "presentation_identity": presentation["presentation_identity"],
    }
    if _encoded(payload) != _encoded(expected_payload):
        raise ValueError("human decision identity payload contradicts the decision")
    if decision_record["limitations"] != _LIMITATIONS:
        raise ValueError("human proposal decision limitations are unsupported")
    return deepcopy(decision_record)


def build_human_proposal_decision(
    proposal_presentation: dict, decision_spec: dict,
) -> dict:
    """Bind an explicit approved/declined decision to one verified #6J artifact."""
    source_presentation = require_proposal_presentation(proposal_presentation)
    spec = _mapping(decision_spec, "human proposal decision", _DECISION_FIELDS)
    decision = spec["decision"]
    if type(decision) is not str or decision not in _DECISIONS:
        raise ValueError("human proposal decision must be approved or declined")
    decision_source = _decision_source(spec["decision_source"])

    identity_payload = {
        "human_proposal_decision_model_version": HUMAN_PROPOSAL_DECISION_MODEL_VERSION,
        "source_proposal_presentation_model_version": (
            source_presentation["proposal_presentation_model_version"]
        ),
        "decision": decision,
        "decision_source": decision_source,
        "proposal_identity": deepcopy(source_presentation["proposal_identity"]),
        "presentation_identity": deepcopy(source_presentation["presentation_identity"]),
    }
    digest = hashlib.sha256(_encoded(identity_payload)).hexdigest()
    decision_identity = {
        "human_proposal_decision_identity_version": (
            HUMAN_PROPOSAL_DECISION_IDENTITY_VERSION
        ),
        "digest_algorithm": IDENTITY_DIGEST_ALGORITHM,
        "canonical_payload": deepcopy(identity_payload),
        "digest": digest,
    }
    return {
        "human_proposal_decision_model_version": HUMAN_PROPOSAL_DECISION_MODEL_VERSION,
        "source_proposal_presentation_model_version": (
            source_presentation["proposal_presentation_model_version"]
        ),
        "decision": decision,
        "decision_source": decision_source,
        "proposal_identity": deepcopy(source_presentation["proposal_identity"]),
        "presentation_identity": deepcopy(source_presentation["presentation_identity"]),
        "source_presentation": source_presentation,
        "human_proposal_decision_identity": decision_identity,
        "limitations": deepcopy(_LIMITATIONS),
    }
