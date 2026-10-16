"""Local advisory artifacts, separate from managed decks. No network or retries."""
from __future__ import annotations

import os
import base64
import hashlib
from pathlib import Path
import re
from datetime import datetime, timezone
from uuid import uuid4

from services.advisory_contract import (AdvisoryError, CHECKER_VERSION, digest,
                                       encoded, safe_bytes, strict_load, validate_review, require_request)
from services import advisory_location
from services.advisory_operations import GATE, error_code


def safe_root(path=None):
    return advisory_location.admit(path)


def key_check(key):
    if type(key) is not str or not re.fullmatch(r"[0-9a-f]{64}", key):
        raise AdvisoryError("invalid_cache_key")


class AdvisoryStore:
    def __init__(self, root=None):
        self.root = advisory_location.admit(root, store_root=True)  # Never creates a directory.

    def _path(self, key):
        key_check(key)
        safe_root(self.root)
        path = self.root / key
        safe_root(path)
        return path

    def _write(self, path, value):
        safe_root(path)
        safe_root(path.parent)
        raw = safe_bytes(encoded(value))
        path.parent.mkdir(parents=True, exist_ok=True)
        safe_root(path.parent)
        temp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        try:
            with temp.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink()

    def _read(self, path):
        safe_root(path)
        return strict_load(path.read_bytes())

    def attempts(self, key):
        result = []
        try:
            folder = self._path(key)
            names = sorted(p for p in folder.iterdir() if p.name.startswith("attempt-") and p.suffix == ".json") if folder.exists() else []
            for p in names:
                safe_root(p)
                raw = p.read_bytes()
                fingerprint = hashlib.sha256(raw).hexdigest()
                try:
                    a = strict_load(raw)
                    if (type(a) is not dict or a.get("key") != key or
                            type(a.get("attempt_id")) is not str or
                            not re.fullmatch(r"[a-f0-9]{32}", a["attempt_id"]) or
                            p.name != "attempt-" + a["attempt_id"] + ".json" or
                            a.get("state") not in ("pending", "complete", "failed")):
                        raise ValueError
                    required = {"attempt_id", "key", "state", "started_at_utc", "preview_id", "approved_cap_usd", "charge"}
                    if a["state"] != "pending":
                        required.add("finished_at_utc")
                    if set(a) != required or a["charge"] != "uncertain_if_interrupted":
                        raise ValueError
                    if type(a["approved_cap_usd"]) is not str or not re.fullmatch(r"[0-9]+\.[0-9]{2}", a["approved_cap_usd"]):
                        raise ValueError
                    datetime.fromisoformat(a["started_at_utc"])
                    if "finished_at_utc" in a:
                        datetime.fromisoformat(a["finished_at_utc"])
                    # Only known fields leave this reader; provider bodies never do.
                    result.append({"attempt_id": a["attempt_id"], "state": a["state"],
                                   "started_at_utc": a["started_at_utc"], "approved_cap_usd": a["approved_cap_usd"],
                                   "fingerprint": fingerprint})
                except (ValueError, KeyError, TypeError):
                    result.append({"attempt_id": "unknown-" + digest([p.name, fingerprint]),
                                   "state": "unknown", "fingerprint": fingerprint})
            after = sorted(p for p in folder.iterdir() if p.name.startswith("attempt-") and p.suffix == ".json") if folder.exists() else []
            if names != after:
                raise OSError
            return result
        except OSError:
            raise AdvisoryError("attempt_history_unreadable") from None

    def begin(self, key, preview):
        attempt = {"attempt_id": uuid4().hex, "key": key, "state": "pending",
                   "started_at_utc": datetime.now(timezone.utc).isoformat(),
                   "preview_id": preview["preview_id"], "approved_cap_usd": preview["maximum_usd"],
                   "charge": "uncertain_if_interrupted"}
        self._write(self._path(key) / ("attempt-" + attempt["attempt_id"] + ".json"), attempt)
        return attempt

    def finish(self, attempt, state):
        record = {**attempt, "state": state, "finished_at_utc": datetime.now(timezone.utc).isoformat()}
        self._write(self._path(attempt["key"]) / ("attempt-" + attempt["attempt_id"] + ".json"), record)

    def diagnostic(self, attempt, raw, validation):
        # Not the valid cache. Invalid responses are retained separately.
        safe_bytes(raw)
        self._write(self._path(attempt["key"]) / ("diagnostic-" + attempt["attempt_id"] + ".json"),
                    {"raw_base64": base64.b64encode(raw).decode("ascii"),
                     "raw_sha256": hashlib.sha256(raw).hexdigest(), "validation": validation})

    def publish(self, request, raw, review, validation, attempt):
        if not validation["structural_valid"] or not validation["evidence_valid"]:
            raise AdvisoryError("invalid_review_not_cacheable")
        entry = {"cache_version": "1", "key": request["key"], "request": request,
                 "raw": raw.decode("utf-8"), "review": review, "validation": validation,
                 "raw_sha256": hashlib.sha256(raw).hexdigest(),
                 "completed_at_utc": datetime.now(timezone.utc).isoformat(),
                 "cost": {"provider": "fake", "actual_usd": "0.00", "basis": "No external request"},
                 "checker_version": CHECKER_VERSION, "lifecycle": "complete",
                 "attempt_id": attempt["attempt_id"]}
        self._write(self._path(request["key"]) / "review.json", {"entry": entry, "checksum": digest(entry)})

    def cached(self, request):
        request = require_request(request)
        path = self._path(request["key"]) / "review.json"
        if not path.exists():
            if (path.parent / "verdict.json").exists():
                raise AdvisoryError("review_orphan_verdict")
            return None
        try:
            saved = self._read(path)
            entry = saved["entry"]
            if saved["checksum"] != digest(entry) or entry["request"] != request or entry["lifecycle"] != "complete":
                raise AdvisoryError("cache_integrity_failed")
            response, review = parse_response(entry["raw"].encode())
            if entry["raw_sha256"] != hashlib.sha256(entry["raw"].encode()).hexdigest():
                raise AdvisoryError("cache_integrity_failed")
            validation = validate_review(review, request["body"]["input"])
            if review != entry["review"] or not validation["evidence_valid"]:
                raise AdvisoryError("cache_validation_failed")
            attempts = self.attempts(request["key"])
            if not any(a["attempt_id"] == entry["attempt_id"] and a["state"] == "complete" for a in attempts):
                raise AdvisoryError("cache_incomplete_attempt")
            return {**entry, "validation": validation, "thinking": [b for b in response["content"] if b["type"] != "text"],
                    "operator_verdict": self.verdict(request["key"], saved["checksum"])
                    if entry["checker_version"] == CHECKER_VERSION else "not_reviewed"}
        except AdvisoryError:
            raise
        except (KeyError, TypeError, ValueError):
            raise AdvisoryError("cache_integrity_failed") from None

    def verdict(self, key, checksum):
        path = self._path(key) / "verdict.json"
        if not path.exists():
            return "not_reviewed"
        record = self._read(path)
        if record.get("entry_checksum") != checksum or record.get("verdict") not in ("accepted", "rejected"):
            raise AdvisoryError("verdict_integrity_failed")
        return record["verdict"]

    def record_verdict(self, request, verdict, *, explicit_operator=False):
        if explicit_operator is not True or verdict not in ("accepted", "rejected"):
            raise AdvisoryError("operator_verdict_required")
        cached = self.cached(request)
        if cached is None:
            raise AdvisoryError("validated_review_required")
        saved = self._read(self._path(request["key"]) / "review.json")
        self._write(self._path(request["key"]) / "verdict.json",
                    {"entry_checksum": saved["checksum"], "verdict": verdict,
                     "source": "explicit_operator_assertion_not_authentication"})

    def inspection(self, key):
        folder = self._path(key)
        attempts = self.attempts(key)
        result = {"inspection_version": "1", "key": key, "review_state": "absent",
                  "evidence_status": "unknown", "operator_verdict": "not_reviewed",
                  "attempts": attempts, "uncertain_charge_required": any(a["state"] != "complete" for a in attempts)}
        try:
            if (folder / "review.json").exists():
                entry = self._read(folder / "review.json")["entry"]
                request = require_request(entry["request"])
                if request["key"] != key:
                    raise AdvisoryError("cache_integrity_failed")
                cached = self.cached(request)
                result.update(review_state="valid", evidence_status="valid",
                              operator_verdict=cached["operator_verdict"])
            elif (folder / "verdict.json").exists():
                raise AdvisoryError("review_orphan_verdict")
        except (ValueError, KeyError, TypeError) as error:
            result.update(review_state="unusable", error_code=error_code(error))
        return result

    def show(self, key):
        saved = self._read(self._path(key) / "review.json")
        request = require_request(saved["entry"]["request"])
        if request["key"] != key:
            raise AdvisoryError("cache_integrity_failed")
        cached = self.cached(request)
        return {"review": cached["review"], "validation": cached["validation"],
                "operator_verdict": cached["operator_verdict"], "thinking": cached["thinking"]}

    def mutation_preview(self, key, *, operation, verdict=None):
        if operation not in ("verdict", "delete_review"):
            raise AdvisoryError("confirmation_required")
        if operation == "verdict":
            if verdict not in ("accepted", "rejected"):
                raise AdvisoryError("operator_verdict_required")
            self.show(key)  # Verdict confirmation is available only for a validated review.
        elif verdict is not None:
            raise AdvisoryError("confirmation_required")
        folder = self._path(key)
        hashes = {}
        for name in ("verdict.json", "review.json"):
            path = folder / name
            safe_root(path)
            try:
                hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            except FileNotFoundError:
                hashes[name] = None
        value = {"key": key, "operation": operation, "verdict": verdict,
                 "hashes": hashes, "attempts": self.attempts(key)}
        if operation == "verdict":
            value["description"] = "Record the explicit operator verdict; evidence validation stays separate."
        else:
            value.update(description="Delete only the verdict and review, in that order.",
                         remove_only=["verdict.json", "review.json"],
                         preserve="attempts, diagnostics, unrelated files and key directory")
        return {**value, "confirmation_digest": digest(value)}

    def confirm_verdict(self, key, verdict, *, confirmation=None):
        if not GATE.acquire(blocking=False):
            raise AdvisoryError("busy")
        try:
            fresh = self.mutation_preview(key, operation="verdict", verdict=verdict)
            if confirmation != fresh["confirmation_digest"]:
                raise AdvisoryError("confirmation_required")
            saved = self._read(self._path(key) / "review.json")
            request = require_request(saved["entry"]["request"])
            if request["key"] != key:
                raise AdvisoryError("cache_integrity_failed")
            self.record_verdict(request, verdict, explicit_operator=True)
        finally:
            GATE.release()

    def history(self, current_key):
        key_check(current_key)
        safe_root(self.root)
        result = []
        for folder in sorted(self.root.glob("*")):
            if folder.is_dir() and re.fullmatch(r"[0-9a-f]{64}", folder.name):
                self._path(folder.name)
                result.append({"key": folder.name, "freshness": "current_input_match" if folder.name == current_key else "historical_stale"})
        return result

    def delete(self, key, *, explicit_operator=False):
        raise AdvisoryError("delete_deprecated")

    def delete_review(self, key, *, confirmation=None):
        if not GATE.acquire(blocking=False):
            raise AdvisoryError("busy")
        try:
            fresh = self.mutation_preview(key, operation="delete_review")
            if confirmation != fresh["confirmation_digest"]:
                raise AdvisoryError("confirmation_required")
            folder = self._path(key)
            # Mandatory order. Interrupted deletion can never CREATE an orphan verdict.
            for name in ("verdict.json", "review.json"):
                path = folder / name
                safe_root(path)
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
        finally:
            GATE.release()


def parse_response(raw):
    response = strict_load(raw)
    if type(response) is not dict or response.get("stop_reason") != "end_turn":
        raise AdvisoryError("incomplete_response")
    content = response.get("content")
    if type(content) is not list or any(type(b) is not dict or b.get("type") not in ("text", "thinking", "redacted_thinking") for b in content):
        raise AdvisoryError("unexpected_response_blocks")
    texts = [b.get("text") for b in content if b["type"] == "text"]
    if len(texts) != 1 or type(texts[0]) is not str:
        raise AdvisoryError("invalid_final_text")
    return response, strict_load(texts[0].encode())
