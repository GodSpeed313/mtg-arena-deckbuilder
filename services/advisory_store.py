"""Local advisory artifacts, separate from managed decks. No network or retries."""
from __future__ import annotations

import os
import base64
import hashlib
from pathlib import Path
import re
import stat
from datetime import datetime, timezone
from uuid import uuid4

from services.advisory_contract import (AdvisoryError, CHECKER_VERSION, digest,
                                       encoded, safe_bytes, strict_load, validate_review)


def _location_evidence():
    """Enumerate Windows known-folder and OneDrive roots; errors fail closed."""
    if os.name != "nt":
        raise AdvisoryError("cache_location_unestablished")
    import winreg
    roots = [v for k, v in os.environ.items() if k.lower().startswith("onedrive") and v]
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
            local = os.path.expandvars(winreg.QueryValueEx(key, "Local AppData")[0])
        # Missing Accounts means no configured OneDrive accounts in this profile;
        # access-denied and incomplete enumeration are NOT treated as absence.
        try:
            accounts = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\OneDrive\Accounts")
        except FileNotFoundError:
            accounts = None
        if accounts is not None:
            with accounts:
                for i in range(winreg.QueryInfoKey(accounts)[0]):
                    with winreg.OpenKey(accounts, winreg.EnumKey(accounts, i)) as account:
                        roots.append(winreg.QueryValueEx(account, "UserFolder")[0])
    except OSError:
        raise AdvisoryError("cache_location_unestablished") from None
    env_local = os.environ.get("LOCALAPPDATA")
    if not env_local or Path(env_local).resolve() != Path(local).resolve():
        raise AdvisoryError("cache_location_unestablished")
    return Path(local), [Path(p) for p in roots]


def safe_root(path=None):
    local, synced = _location_evidence()
    root = Path(path) if path is not None else local / "MTGDeckbuilder" / "AdvisoryCache"
    if not local.is_absolute() or not root.is_absolute():
        raise AdvisoryError("cache_location_unestablished")
    # Fail on junctions, symlinks and other reparse points, including ancestors.
    for p in (root, *root.parents):
        if p.exists() or p.is_symlink():
            st = p.lstat()
            if p.is_symlink() or getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise AdvisoryError("cache_redirected")
    resolved = root.resolve()
    if not resolved.is_relative_to(local.resolve()) or resolved == local.resolve():
        raise AdvisoryError("cache_outside_application_data")
    if any(not p.is_absolute() for p in synced):
        raise AdvisoryError("cache_location_unestablished")
    if any(resolved.is_relative_to(p.resolve()) or p.resolve().is_relative_to(resolved) for p in synced):
        raise AdvisoryError("cache_synced")
    if any(part.casefold().startswith("onedrive") for part in resolved.parts):
        raise AdvisoryError("cache_synced")
    return resolved


def key_check(key):
    if type(key) is not str or not re.fullmatch(r"[0-9a-f]{64}", key):
        raise AdvisoryError("invalid_cache_key")


class AdvisoryStore:
    def __init__(self, root=None):
        self.root = safe_root(root)  # Constructor never creates a directory.

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
        return [self._read(p) for p in sorted(self._path(key).glob("attempt-*.json"))]

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
        path = self._path(request["key"]) / "review.json"
        if not path.exists():
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
        if explicit_operator is not True:
            raise AdvisoryError("explicit_deletion_required")
        folder = self._path(key)
        if folder.exists():
            # No recursive deletion, external paths, or linked files.
            files = list(folder.iterdir())
            for p in files:
                safe_root(p)
                if not p.is_file():
                    raise AdvisoryError("unexpected_cache_directory")
            for p in files:
                p.unlink()
            folder.rmdir()


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
