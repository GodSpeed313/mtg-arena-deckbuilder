"""Explicit local location authority. No registry writes or provider access."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import stat
import subprocess
from uuid import uuid4

from services.advisory_contract import AdvisoryError, digest, encoded, safe_bytes, strict_load
from services.advisory_operations import GATE

POLICY = "1"
ACCOUNTS = r"Software\Microsoft\OneDrive\Accounts"


def _profile():
    # Windows built-in, read-only current token identity; never log its output.
    result = subprocess.run([str(Path(os.environ["SystemRoot"]) / "System32" / "whoami.exe"),
                             "/user", "/fo", "csv", "/nh"],
                            check=True, capture_output=True, text=True)
    sid = list(csv.reader(result.stdout.splitlines()))[0][-1]
    if not re.fullmatch(r"S-1-[0-9-]+", sid):
        raise AdvisoryError("location_evidence_unreadable")
    return sid


def absolute(value):
    if type(value) is not str or not value or ".." in Path(value).parts:
        raise AdvisoryError("location_invalid_path")
    path = Path(value)
    if not path.is_absolute():
        raise AdvisoryError("location_invalid_path")
    return path


def reject_reparse(path):
    # Inspect the lexical path before resolve() can hide a redirected ancestor.
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise AdvisoryError("location_reparse_point")


def collect():
    """Only a missing VALUE on an opened account key is attestable."""
    if os.name != "nt":
        raise AdvisoryError("location_evidence_unreadable")
    import winreg
    access = winreg.KEY_READ | winreg.KEY_WOW64_64KEY
    def open_key(parent, name):
        return winreg.OpenKey(parent, name, 0, access)
    def string(value):
        raw, kind = value
        if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
            raise AdvisoryError("location_evidence_unreadable")
        return str(absolute(os.path.expandvars(raw)).resolve())
    try:
        with open_key(winreg.HKEY_CURRENT_USER,
                      r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
            local_value = winreg.QueryValueEx(key, "Local AppData")
            local = string(local_value)
        reject_reparse(absolute(os.path.expandvars(local_value[0])))
        env_local = absolute(os.environ.get("LOCALAPPDATA", ""))
        reject_reparse(env_local)
        if env_local.resolve() != Path(local):
            raise AdvisoryError("location_known_folder_mismatch")
        roots, missing = [], []
        for key, value in sorted(os.environ.items()):
            if key.lower().startswith("onedrive"):
                roots.append({"source": "env:" + key, "path": str(absolute(value).resolve())})
        try:
            accounts = open_key(winreg.HKEY_CURRENT_USER, ACCOUNTS)
        except FileNotFoundError:
            accounts = None
        if accounts is not None:
            with accounts:
                names = [winreg.EnumKey(accounts, i) for i in range(winreg.QueryInfoKey(accounts)[0])]
                for name in names:
                    # Failure to OPEN (including disappearance) must escape.
                    with open_key(accounts, name) as account:
                        source = "HKCU\\" + ACCOUNTS + "\\" + name
                        try:
                            value = winreg.QueryValueEx(account, "UserFolder")
                        except FileNotFoundError:
                            missing.append(source)
                        else:
                            roots.append({"source": source + "\\UserFolder", "path": string(value)})
                again = [winreg.EnumKey(accounts, i) for i in range(winreg.QueryInfoKey(accounts)[0])]
                if names != again:
                    raise AdvisoryError("location_evidence_unreadable")
        result = {"policy_version": POLICY, "profile": _profile(), "local_appdata": local,
                  "registry_view": "HKCU-64", "missing": sorted(missing),
                  "roots": sorted(roots, key=lambda r: (r["source"], r["path"]))}
        validate_evidence(result)
        return result
    except AdvisoryError:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError, subprocess.SubprocessError):
        raise AdvisoryError("location_evidence_unreadable") from None


def validate_evidence(e):
    try:
        if set(e) != {"policy_version", "profile", "local_appdata", "registry_view", "missing", "roots"}:
            raise ValueError
        if any(type(e[k]) is not str or not e[k] for k in ("policy_version", "profile", "registry_view")):
            raise ValueError
        absolute(e["local_appdata"])
        if type(e["missing"]) is not list or any(type(v) is not str or not v for v in e["missing"]):
            raise ValueError
        if e["missing"] != sorted(set(e["missing"])) or type(e["roots"]) is not list:
            raise ValueError
        for root in e["roots"]:
            if set(root) != {"source", "path"} or type(root["source"]) is not str or not root["source"]:
                raise ValueError
            absolute(root["path"])
        safe_bytes(encoded(e))
    except (ValueError, KeyError, TypeError):
        raise AdvisoryError("location_evidence_unreadable") from None


def unconditional(path, e):
    """No attestation is consulted to establish structural storage safety."""
    validate_evidence(e)
    path = absolute(str(path))
    local = absolute(e["local_appdata"])
    try:
        reject_reparse(path)
        resolved = path.resolve()
        if not resolved.is_relative_to(local.resolve()) or resolved == local.resolve():
            raise AdvisoryError("cache_outside_application_data")
        if any(part.casefold().startswith("onedrive") for part in resolved.parts):
            raise AdvisoryError("location_positive_sync_overlap")
        for root in e["roots"]:
            synced = absolute(root["path"]).resolve()
            if resolved.is_relative_to(synced) or synced.is_relative_to(resolved):
                raise AdvisoryError("location_positive_sync_overlap")
        return resolved
    except OSError:
        raise AdvisoryError("location_evidence_unreadable") from None


def control_path(e):
    return unconditional(Path(e["local_appdata"]) / "MTGDeckbuilder" / "AdvisoryControl" / "location.json", e)


def _read(e):
    path = control_path(e)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise AdvisoryError("location_evidence_unreadable") from None
    try:
        envelope = strict_load(raw)
        if set(envelope) != {"record", "checksum"} or digest(envelope["record"]) != envelope["checksum"]:
            raise ValueError
        r = envelope["record"]
        if set(r) != {"version", "id", "state", "snapshot", "confirmed_at_utc"}:
            raise ValueError
        if r["version"] != "1" or r["state"] not in ("active", "revoked") or not re.fullmatch(r"[a-f0-9]{32}", r["id"]):
            raise ValueError
        datetime.fromisoformat(r["confirmed_at_utc"])
        s = r["snapshot"]
        if set(s) != {"evidence", "cache_path", "control_path"}:
            raise ValueError
        validate_evidence(s["evidence"])
        absolute(s["cache_path"]); absolute(s["control_path"])
        return r
    except (ValueError, KeyError, TypeError):
        raise AdvisoryError("location_attestation_corrupt") from None


def _snapshot(path, e):
    root, control = unconditional(path, e), control_path(e)
    if root.is_relative_to(control.parent) or control.parent.is_relative_to(root):
        raise AdvisoryError("location_control_overlap")
    return {"evidence": e, "cache_path": str(root), "control_path": str(control)}


def inspect(path):
    e = collect()
    snapshot = _snapshot(path, e)
    current = _read(e)
    result = {"snapshot": snapshot, "current": current}
    return {**result, "confirmation_digest": digest(result)}


def _write(record, e):
    path = control_path(e)
    raw = safe_bytes(encoded({"record": record, "checksum": digest(record)}))
    path.parent.mkdir(parents=True, exist_ok=True)
    unconditional(path, e)
    temp = path.with_name("location-" + uuid4().hex + ".tmp")
    try:
        with temp.open("xb") as f:
            f.write(raw); f.flush(); os.fsync(f.fileno())
        if collect() != e:
            raise AdvisoryError("location_attestation_stale")
        unconditional(path, e)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def attest(path, *, confirmation=None):
    if not GATE.acquire(blocking=False):
        raise AdvisoryError("busy")
    try:
        fresh = inspect(path)  # Reread immediately before publication.
        if confirmation != fresh["confirmation_digest"]:
            raise AdvisoryError("confirmation_required")
        if fresh["current"] and fresh["current"]["state"] != "revoked":
            raise AdvisoryError("location_revoke_required")
        record = {"version": "1", "id": uuid4().hex, "state": "active", "snapshot": fresh["snapshot"],
                  "confirmed_at_utc": datetime.now(timezone.utc).isoformat()}
        _write(record, fresh["snapshot"]["evidence"])
        return record
    finally:
        GATE.release()


def revoke(attestation_id, *, confirmation=None):
    if not GATE.acquire(blocking=False):
        raise AdvisoryError("busy")
    try:
        e = collect()
        record = _read(e)
        if record is None or record["id"] != attestation_id:
            raise AdvisoryError("location_attestation_missing")
        if confirmation != digest(record):
            raise AdvisoryError("confirmation_required")
        _write({**record, "state": "revoked"}, e)
    finally:
        GATE.release()


def revocation_preview(attestation_id):
    record = _read(collect())
    if record is None or record["id"] != attestation_id:
        raise AdvisoryError("location_attestation_missing")
    return {"record": record, "confirmation_digest": digest(record)}


def admit(path=None, *, store_root=False):
    e = collect()
    record = _read(e)
    default = Path(e["local_appdata"]) / "MTGDeckbuilder" / "AdvisoryCache"
    root = path if path is not None else record["snapshot"]["cache_path"] if record else default
    target = unconditional(root, e)
    if record:
        designated = _snapshot(record["snapshot"]["cache_path"], e)
        if record["state"] != "active" or record["snapshot"] != designated:
            raise AdvisoryError("location_attestation_stale")
        if not target.is_relative_to(Path(designated["cache_path"])):
            raise AdvisoryError("location_attestation_stale")
        if store_root and target != Path(designated["cache_path"]):
            raise AdvisoryError("location_attestation_stale")
    elif e["missing"]:
        raise AdvisoryError("location_missing_userfolder_attestation_required")
    return target
