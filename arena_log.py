"""Extract your MTG Arena card collection from Player.log.

Arena writes its client/server traffic into a Unity log when "Detailed Logs"
is enabled. Somewhere in that traffic is the response listing every card you
own, as {arena_card_id: quantity}. This module finds it and writes it out.

Design note: Wizards changes the log format between releases -- marker strings
like "PlayerInventory.GetPlayerCardsV3" have come and gone. So we do NOT match
on markers. We scan the log for every JSON object it contains and identify the
collection by its *shape*: a mapping of numeric card ids to small quantities.
That survives renames.

Stdlib only. Usage:
    python arena_log.py              # find log, write collection.json
    python arena_log.py --doctor     # diagnose why nothing was found
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

# ---------------------------------------------------------------- locating

# Arena writes the same traffic to two places: the Unity player log under
# LocalLow, and timestamped UTC_Log files next to the install. Which one is
# complete varies by version, so we read both and take whichever has more.
_USER = Path(os.environ.get("USERPROFILE", "~")).expanduser()

_LOG_DIRS = [
    _USER / "AppData/LocalLow/Wizards Of The Coast/MTGA",
    # Wine/Proton layout, for the Linux+Lutris crowd.
    Path.home()
    / ".wine/drive_c/users/steamuser/AppData/LocalLow/Wizards Of The Coast/MTGA",
]

# Arena rotates the current log to Player-prev.log on restart. The previous
# one often holds a *more complete* login burst than a log from a session
# that's still open, so we consider both and let the caller pick.
_LOG_NAMES = ["Player.log", "Player-prev.log"]

# The install can sit on any drive; these are the common roots.
_INSTALL_DIRS = [
    Path(f"{d}:/Program Files/Wizards of the Coast/MTGA/MTGA_Data/Logs/Logs")
    for d in "CDEFG"
] + [
    Path(f"{d}:/Program Files (x86)/Steam/steamapps/common/MTGA"
         "/MTGA_Data/Logs/Logs")
    for d in "CDEFG"
]


def find_logs() -> list[Path]:
    """Return every Arena log we can see, newest first."""
    found = [d / n for d in _LOG_DIRS for n in _LOG_NAMES if (d / n).is_file()]
    for d in _INSTALL_DIRS:
        if d.is_dir():
            found.extend(p for p in d.glob("UTC_Log*.log") if p.is_file())
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def detailed_logs_enabled(text: str) -> bool | None:
    """Read Arena's own statement about its logging level.

    Returns True/False, or None if the log never said (older clients).
    """
    m = re.search(r"DETAILED LOGS:\s*(ENABLED|DISABLED)", text, re.I)
    return None if not m else m.group(1).upper() == "ENABLED"


# ----------------------------------------------------------------- parsing

# Arena ids ("grpId") are 5-6 digits today; allow 4-7 for headroom.
_CARD_ID = re.compile(r"^\d{4,7}$")

_SCENE = re.compile(r'"toSceneName":"([A-Za-z_]+)"')

# Wildcard/currency fields, which travel in a different payload than the cards.
_INVENTORY_KEYS = {
    "wcCommon": "common_wildcards",
    "wcUncommon": "uncommon_wildcards",
    "wcRare": "rare_wildcards",
    "wcMythic": "mythic_wildcards",
    "gold": "gold",
    "gems": "gems",
    "vaultProgress": "vault_progress",
}


def iter_json_objects(text: str) -> Iterator[tuple[dict, int]]:
    """Yield every JSON object embedded in the log, with its offset.

    Log lines carry prefixes ([UnityCrossThreadLogger], timestamps, arrows)
    and payloads are sometimes pretty-printed across many lines, so we can't
    parse line-by-line. raw_decode lets us try a parse at each '{' and skip
    past whatever it successfully consumed.
    """
    decoder = json.JSONDecoder()
    i, n = 0, len(text)
    while i < n:
        start = text.find("{", i)
        if start == -1:
            return
        try:
            obj, end = decoder.raw_decode(text, start)
        except ValueError:
            i = start + 1
            continue
        if isinstance(obj, dict):
            yield obj, start
        i = max(end, start + 1)


def _is_qty(v: Any) -> bool:
    """A plausible card quantity: 1-4 normally, more for basics."""
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 1000


# Arena writes deck lists as [{"cardId": n, "quantity": n}], so the collection
# may well use the same shape rather than a plain mapping. Accept both.
_ID_FIELDS = ("cardId", "grpId", "GrpId", "CardId", "id")
_QTY_FIELDS = ("quantity", "Quantity", "count", "Count", "owned")


def _as_pairs(obj: Any) -> dict[str, int] | None:
    """Normalize either card-list shape into {card_id: quantity}, or None.

    The size threshold keeps us from latching onto a 60-card *deck* -- there
    are 185 of those in the log -- when we're after the whole collection.
    """
    if isinstance(obj, dict) and len(obj) >= 20:
        if all(_CARD_ID.match(str(k)) and _is_qty(v) for k, v in obj.items()):
            return {str(k): int(v) for k, v in obj.items()}
        return None

    if isinstance(obj, list) and len(obj) >= 20:
        out: dict[str, int] = {}
        for entry in obj:
            if not isinstance(entry, dict):
                return None
            cid = next((entry[f] for f in _ID_FIELDS if f in entry), None)
            qty = next((entry[f] for f in _QTY_FIELDS if f in entry), None)
            if cid is None or not _CARD_ID.match(str(cid)) or not _is_qty(qty):
                return None
            out[str(cid)] = int(qty)
        return out or None

    return None


# Card-id/quantity shape alone is far too weak. A single session's log holds
# ~700 lists of that shape -- every precon deck, set pool and booster pool --
# and the largest measured was 190 distinct cards. A real collection runs to
# thousands, so anything under this is a deck or a pool, not ownership.
_MIN_COLLECTION_CARDS = 400


def _looks_like_collection(obj: Any) -> bool:
    """True if a card listing is plausibly *ownership*, not a deck or pool.

    Two guards: collection scale, and quantity variance -- set and booster
    pools are long lists with every quantity pinned to 1, whereas a real
    collection mixes 1s through 4s.
    """
    pairs = _as_pairs(obj)
    if pairs is None or len(pairs) < _MIN_COLLECTION_CARDS:
        return False
    return len(set(pairs.values())) > 1


def _walk(obj: Any, depth: int = 0) -> Iterator[Any]:
    """Yield obj and everything nested in it, decoding JSON-in-a-string.

    Some client versions send the payload double-encoded, as a string field
    containing JSON, so a plain structural walk would miss it.
    """
    if depth > 12:
        return
    yield obj
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, depth + 1)
    elif isinstance(obj, str) and len(obj) > 40 and obj.lstrip()[:1] in "{[":
        try:
            yield from _walk(json.loads(obj), depth + 1)
        except ValueError:
            pass


@dataclass
class Extraction:
    """What we recovered from one log file."""

    log_path: Path
    detailed_logs: bool | None = None
    collection: dict[str, int] = field(default_factory=dict)
    inventory: dict[str, int] = field(default_factory=dict)
    json_objects_seen: int = 0
    collection_snapshots: int = 0
    # Arena logs Client.SceneChange as you move around the UI. Knowing where
    # you've been tells us whether a missing collection means "broken" or
    # just "never opened the screen that fetches it".
    scenes: list[str] = field(default_factory=list)

    @property
    def distinct_cards(self) -> int:
        return len(self.collection)

    @property
    def total_cards(self) -> int:
        return sum(self.collection.values())


def extract(log_path: Path) -> Extraction:
    """Pull the collection and wildcard counts out of one log file."""
    text = log_path.read_text(encoding="utf-8", errors="replace")
    result = Extraction(log_path=log_path, detailed_logs=detailed_logs_enabled(text))
    result.scenes = _SCENE.findall(text)

    for obj, _offset in iter_json_objects(text):
        result.json_objects_seen += 1
        for node in _walk(obj):
            if _looks_like_collection(node):
                pairs = _as_pairs(node) or {}
                # Keep the largest snapshot rather than the last: decks and
                # partial payloads share this shape, and the collection is
                # always the biggest card list in the log.
                if len(pairs) > len(result.collection):
                    result.collection = pairs
                result.collection_snapshots += 1
            elif isinstance(node, dict):
                hits = {
                    dest: node[src]
                    for src, dest in _INVENTORY_KEYS.items()
                    if isinstance(node.get(src), int)
                    and not isinstance(node.get(src), bool)
                }
                # Require a couple of fields so we don't match on a stray "gold".
                if len(hits) >= 3:
                    result.inventory = hits

    return result


# --------------------------------------------------------------------- cli


def _doctor(extractions: list[Extraction]) -> int:
    print("Arena log diagnostic")
    print("=" * 60)
    if not extractions:
        print("No Player.log found. Looked in:")
        for d in _LOG_DIRS:
            print(f"  {d}")
        print("\nIs Arena installed, and has it been run at least once?")
        return 1

    for e in extractions:
        size = e.log_path.stat().st_size
        print(f"\n{e.log_path}")
        print(f"  size              {size:,} bytes")
        state = {True: "ENABLED", False: "DISABLED", None: "not stated"}[
            e.detailed_logs
        ]
        print(f"  detailed logs     {state}")
        print(f"  JSON objects      {e.json_objects_seen:,}")
        print(f"  collection found  {'yes' if e.collection else 'no'}")
        if e.collection:
            print(f"    distinct cards  {e.distinct_cards:,}")
            print(f"    total copies    {e.total_cards:,}")
        if e.inventory:
            print(f"  wildcards/gold    {e.inventory}")

    if not any(e.collection for e in extractions):
        print("\n" + "=" * 60)
        # Diagnose from the log Arena is actually writing to -- a stale
        # rotated log will still say DISABLED and would mislead us.
        active = max(extractions, key=lambda e: e.json_objects_seen)
        if active.detailed_logs is False:
            print("Cause: Detailed Logs are OFF, so Arena never wrote your")
            print("collection to the log. Fix it in the client:")
            print("  Settings (gear) -> Account -> tick 'Detailed Logs (Plugin")
            print("  Support)', then fully restart Arena.")
        elif set(active.scenes) <= {"Home", "None"}:
            seen = " -> ".join(active.scenes) or "(none logged)"
            print("Detailed logs are on, but Arena has not left the home")
            print(f"screen this session: {seen}")
            print("It only fetches your cards when you open the screen that")
            print("shows them, so nothing has been sent yet. In the client:")
            print("  click Collection in the top nav, let the cards render.")
            print("Then re-run this. No restart needed.")
        else:
            seen = " -> ".join(active.scenes)
            print(f"Detailed logs are on and you visited: {seen}")
            print("but no collection payload appeared. This client version")
            print("may not log the card list at all -- worth checking the")
            print("raw log by hand before trusting this tool.")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--log", type=Path, help="path to a specific Player.log")
    ap.add_argument(
        "--out", type=Path, default=Path("collection.json"), help="output file"
    )
    ap.add_argument(
        "--doctor", action="store_true", help="diagnose instead of extracting"
    )
    args = ap.parse_args(argv)

    logs = [args.log] if args.log else find_logs()
    if args.log and not args.log.is_file():
        print(f"No such log: {args.log}", file=sys.stderr)
        return 1

    extractions = [extract(p) for p in logs]

    if args.doctor:
        return _doctor(extractions)

    best = max(extractions, key=lambda e: e.distinct_cards, default=None)
    if best is None or not best.collection:
        print("No collection found. Run with --doctor for why.", file=sys.stderr)
        return 1

    payload = {
        "source_log": str(best.log_path),
        "distinct_cards": best.distinct_cards,
        "total_copies": best.total_cards,
        "inventory": best.inventory,
        "collection": best.collection,
    }
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(
        f"Wrote {args.out}: {best.distinct_cards:,} distinct cards, "
        f"{best.total_cards:,} total copies"
    )
    if best.inventory:
        print(f"Inventory: {best.inventory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
