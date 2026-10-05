"""Historical caller-asserted review of one verified scoped Presentation 2.

No authentication, evidence promotion, freshness check, IO or execution.
Legacy human-decision and execution authorities remain separate and v1-only.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from services.proposal_presentation import require_proposal_presentation_v2


HUMAN_PROPOSAL_DECISION_MODEL_VERSION = "2"
HUMAN_PROPOSAL_DECISION_IDENTITY_VERSION = "1"
IDENTITY_DIGEST_ALGORITHM = "sha256"

_DECISIONS = frozenset({"approved", "declined"})
_SOURCE_KINDS = frozenset({"explicit_user", "explicit_operator"})
_SPEC_FIELDS = frozenset({"decision", "decision_source"})
_OUTPUT_FIELDS = frozenset({
    "human_proposal_decision_model_version",
    "source_proposal_presentation_model_version", "decision", "decision_source",
    "proposal_identity", "presentation_identity", "source_presentation",
    "human_proposal_decision_identity", "limitations",
})
_LIMITATIONS = [
    "This records a caller-asserted explicit review, acknowledgment of displayed evidence and limitations, and consent to the exact captured proposal when approved; it does not authenticate a human, prove actual display or reading, legal consent, or nonrepudiation.",
    "Neither decision certifies exhaustive evidence or external/Oracle truth, establishes uncertain routes, or asserts prerequisite satisfaction.",
    "Approval does not establish current legality, deck freshness, ownership, collection state, affordability, resources, or captured validation currentness.",
    "Neither decision grants spending, application, or execution authority; this historical record is only potential input to a separately designed compatible future revalidation authority.",
    "Current legacy human-decision, revalidation, and execution authorities reject this scoped model.",
    "This boundary does not query, mutate, persist, export, apply, execute, or introduce expiry, revocation, supersession, or latest-review selection.",
    "Identity digests detect deterministic mismatches; they are not authentication, signatures, credentials, authorization tokens, or proof of who made a decision.",
]


def _closed(value, fields, label):
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"{label} has missing or unsupported fields")
    return value


def _json_value(value):
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError("decision object keys must be strings")
        for item in value.values():
            _json_value(item)
    elif type(value) is list:
        for item in value:
            _json_value(item)
    elif value is not None and type(value) not in (str, int, float, bool):
        raise ValueError("decision artifact must contain JSON values")


def _encoded(value):
    _json_value(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _source(value):
    source = _closed(value, {"kind", "provenance"}, "decision source")
    if type(source["kind"]) is not str or source["kind"] not in _SOURCE_KINDS:
        raise ValueError("human decision requires an explicit supported source")
    provenance = _closed(source["provenance"], {"reference"}, "decision provenance")
    reference = provenance["reference"]
    if type(reference) is not str or not reference.strip():
        raise ValueError("decision provenance reference must be non-empty")
    return {"kind": source["kind"], "provenance": {"reference": reference.strip()}}


def _build(proposal_presentation, decision_spec):
    _encoded(proposal_presentation)
    _encoded(decision_spec)
    presentation = require_proposal_presentation_v2(proposal_presentation)
    spec = _closed(decision_spec, _SPEC_FIELDS, "scoped human decision")
    decision = spec["decision"]
    if type(decision) is not str or decision not in _DECISIONS:
        raise ValueError("human proposal decision must be approved or declined")
    source = _source(spec["decision_source"])
    # Retain Identity 1's exact six-field canonical payload contract.
    payload = {
        "human_proposal_decision_model_version": "2",
        "source_proposal_presentation_model_version": "2",
        "decision": decision, "decision_source": source,
        "proposal_identity": deepcopy(presentation["proposal_identity"]),
        "presentation_identity": deepcopy(presentation["presentation_identity"]),
    }
    return {
        **deepcopy(payload), "source_presentation": presentation,
        "human_proposal_decision_identity": {
            "human_proposal_decision_identity_version": "1",
            "digest_algorithm": "sha256", "canonical_payload": deepcopy(payload),
            "digest": hashlib.sha256(_encoded(payload)).hexdigest(),
        },
        "limitations": deepcopy(_LIMITATIONS),
    }


def build_human_proposal_decision_v2(proposal_presentation: dict, decision_spec: dict) -> dict:
    """Record an explicit approved/declined assertion; grant no action authority."""
    try:
        return _build(proposal_presentation, decision_spec)
    except (TypeError, KeyError, AttributeError, IndexError, RecursionError, OverflowError) as exc:
        raise ValueError("scoped human decision inputs are malformed") from exc


def require_human_proposal_decision_v2(value: dict) -> dict:
    """Verify historical scoped review consistency, never freshness or identity of a human."""
    try:
        record = _closed(value, _OUTPUT_FIELDS, "scoped human proposal decision")
        _encoded(record)
        if record["human_proposal_decision_model_version"] != "2" or (
            record["source_proposal_presentation_model_version"] != "2"
        ):
            raise ValueError("Human Proposal Decision Model 2 / Presentation 2 is required")
        expected = _build(record["source_presentation"], {
            "decision": record["decision"], "decision_source": record["decision_source"],
        })
        # Rebuilding checks the complete identity envelope/payload/digest,
        # normalized provenance, repeated identities and fixed limitations.
        # Canonical bytes distinguish JSON bool/int/float and preserve arrays.
        if _encoded(record) != _encoded(expected):
            raise ValueError("scoped human decision contradicts its verified presentation or identity")
        return expected
    except (TypeError, KeyError, AttributeError, IndexError, RecursionError, OverflowError) as exc:
        raise ValueError("scoped human decision artifact is malformed") from exc
