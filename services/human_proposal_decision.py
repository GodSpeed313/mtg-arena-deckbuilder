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
