"""Single-user fake-provider advisory coordinator. Real transport is disabled."""
from __future__ import annotations

from copy import deepcopy
from threading import Lock
from uuid import uuid4

from services.advisory_contract import (AdvisoryError, digest, encoded, require_request,
                                       safe_bytes, validate_review)
from services.advisory_store import parse_response

_IN_FLIGHT = Lock()  # One application process, not a cross-process budget lock.


class FakeProvider:
    """Explicit local response bytes; no key, HTTP library, hidden retry or tools."""
    def __init__(self, response: bytes):
        self.response = safe_bytes(response)
        self.calls = 0

    def count(self, request):
        # Conservative simulated byte count, NOT an Anthropic token count.
        return len(encoded(request["body"]))

    def review(self, request):
        self.calls += 1
        return self.response


class AdvisoryCoordinator:
    def __init__(self, store, provider=None):
        self.store = store
        self.provider = provider
        self._previews = {}

    def preview(self, request):
        request = require_request(request)
        if self.provider is not None and type(self.provider) is not FakeProvider:
            raise AdvisoryError("real_provider_disabled")
        attempts = self.store.attempts(request["key"])
        prior = [a["attempt_id"] for a in attempts if a["state"] != "complete"]
        cached = self.store.cached(request)
        preview = {"preview_id": uuid4().hex, "request_key": request["key"],
                   "provider": "fake", "paid_dispatch_enabled": False, "maximum_usd": "0.00",
                   "input_count": self.provider.count(request) if self.provider else None,
                   "count_basis": "simulated UTF-8 bytes; not provider tokens",
                   "max_output_tokens": request["body"]["max_output_tokens"],
                   "disclosure": "Frozen neutral deck and card text; hypothetical advisory only.",
                   "cache_hit": cached is not None, "previous_uncertain_or_failed_attempts": prior,
                   "warning": "Earlier requests may have been charged; no retry is automatic." if prior else None}
        self._previews[preview["preview_id"]] = digest(preview)
        return deepcopy(preview)

    def run(self, request, *, preview=None, authorize=False, acknowledge_uncertainty=False):
        """One explicit invocation. Typed safe errors never contain provider text."""
        if not _IN_FLIGHT.acquire(blocking=False):
            return {"state": "busy", "execution_authority": False}
        attempt = None
        try:
            request = require_request(request)
            cached = self.store.cached(request)
            if cached is not None:
                return {"state": "cached_valid", "review": cached["review"],
                        "validation": cached["validation"], "operator_verdict": cached["operator_verdict"],
                        "thinking": cached["thinking"], "execution_authority": False}
            if self.provider is None:
                return {"state": "unavailable", "reason": "real_provider_disabled", "execution_authority": False}
            if type(self.provider) is not FakeProvider:
                raise AdvisoryError("real_provider_disabled")
            if type(preview) is not dict or authorize is not True:
                raise AdvisoryError("explicit_authorization_required")
            pid = preview.get("preview_id")
            if (self._previews.get(pid) != digest(preview) or preview.get("request_key") != request["key"]
                    or preview.get("maximum_usd") != "0.00" or preview.get("cache_hit") is not False):
                raise AdvisoryError("preview_consent_mismatch")
            prior = [a["attempt_id"] for a in self.store.attempts(request["key"]) if a["state"] != "complete"]
            if prior != preview["previous_uncertain_or_failed_attempts"] or (prior and acknowledge_uncertainty is not True):
                raise AdvisoryError("uncertain_attempt_acknowledgment_required")
            del self._previews[pid]  # Consent cannot be replayed, even after failure.
            attempt = self.store.begin(request["key"], preview)
            raw = self.provider.review(deepcopy(request))  # Exactly one call.
            safe_bytes(raw)
            try:
                response, review = parse_response(raw)
                validation = validate_review(review, request["body"]["input"])
            except AdvisoryError as error:
                if str(error) == "credential_contamination":
                    raise  # Discard contaminated content, including escaped secrets.
                self.store.diagnostic(attempt, raw, {"evidence_valid": False, "reason": "invalid_response"})
                self.store.finish(attempt, "failed")
                return {"state": "failed", "reason": "invalid_response", "execution_authority": False}
            if not validation["evidence_valid"]:
                self.store.diagnostic(attempt, raw, validation)
                self.store.finish(attempt, "failed")
                return {"state": "failed", "reason": "evidence_validation", "validation": validation,
                        "operator_verdict": "not_reviewed", "execution_authority": False}
            self.store.publish(request, raw, review, validation, attempt)
            self.store.finish(attempt, "complete")
            return {"state": "fresh_valid", "review": review, "validation": validation,
                    "operator_verdict": "not_reviewed", "execution_authority": False,
                    "thinking": [b for b in response["content"] if b["type"] != "text"]}
        except (Exception, KeyboardInterrupt):
            # Deliberately omit arbitrary exception messages, types and raw bodies.
            # Pending marker remains; possibly billed/partially persisted != success.
            return {"state": "interrupted_or_outcome_unknown" if attempt else "unavailable",
                    "reason": "request_may_have_been_processed_and_charged" if attempt else "advisory_precondition_failed",
                    "execution_authority": False}
        finally:
            _IN_FLIGHT.release()
