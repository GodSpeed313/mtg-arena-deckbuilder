"""Command-line entry point for the offline deck workbench."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import sys
from pathlib import Path

from mtgadb import canonical, snapshot_store
from mtgadb.model import Collection, Inventory, Status
from mtgadb.modes import OperatingMode
from mtgadb.providers.arena_log import ArenaLogProvider
from services.exporter import export_arena_deck, import_arena_deck
from services.validator import DeckRules, validate_deck
from services.intelligence import analyze_deck
from services.diagnosis import diagnose_analysis


ROOT = Path(__file__).parent


def _collection(path: Path | None) -> Collection | None:
    if path is None:
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    cards = raw.get("cards", raw)
    if not isinstance(cards, dict):
        raise ValueError("collection JSON must be an object of arena_id: quantity")
    return Collection({int(k): int(v) for k, v in cards.items()}, source=str(path))


def _wildcards(values: list[str] | None) -> Inventory | None:
    if values is None:
        return None
    counts: dict[str, int] = {}
    for value in values:
        try:
            rarity, count = value.split("=", 1)
            counts[rarity.casefold()] = int(count)
        except ValueError as exc:
            raise ValueError(f"invalid wildcard value {value!r}; use rarity=count") from exc
    return Inventory(wildcards=counts)


def validate_command(args: argparse.Namespace) -> int:
    con = canonical.open_db(args.database)
    try:
        parsed = import_arena_deck(
            args.deck.read_text(encoding="utf-8-sig"), con, name=args.deck.stem
        )
        for issue in parsed.issues:
            print(f"IMPORT line {issue.line}: {issue.message}")
        format = canonical.get_format(con, args.format) if args.format else None
        if args.format and format is None:
            raise ValueError(
                f"format {args.format!r} is not loaded; run sync_formats.py first"
            )
        colors = frozenset(args.colors.upper()) if args.colors else None
        rules = (
            DeckRules.from_format(format, allowed_colors=colors)
            if format else DeckRules(
                min_main=args.min_main,
                max_main=args.max_main,
                max_sideboard=args.max_sideboard,
                min_commanders=args.min_commanders,
                max_commanders=args.max_commanders,
                allowed_colors=colors,
            )
        )
        report = validate_deck(
            parsed.deck,
            con,
            mode=OperatingMode(args.mode),
            rules=rules,
            format=format,
            collection=_collection(args.collection),
            inventory=_wildcards(args.wildcard),
        )
        for issue in report.errors:
            print(f"ERROR [{issue.code}] {issue.message}")
        for issue in report.warnings:
            print(f"WARN  [{issue.code}] {issue.message}")
        if report.wildcard_cost:
            cost = ", ".join(f"{k}={v}" for k, v in sorted(report.wildcard_cost.items()))
            print(f"wildcard cost: {cost}")
        ok = parsed.ok and report.valid
        print("VALID" if ok else "INVALID")
        return 0 if ok else 1
    finally:
        con.close()


def normalize_command(args: argparse.Namespace) -> int:
    con = canonical.open_db(args.database)
    try:
        parsed = import_arena_deck(
            args.deck.read_text(encoding="utf-8-sig"), con, name=args.deck.stem
        )
        if not parsed.ok:
            for issue in parsed.issues:
                print(f"line {issue.line}: {issue.message}", file=sys.stderr)
            return 1
        rendered = export_arena_deck(parsed.deck, con)
        if args.output:
            args.output.write_text(rendered, encoding="utf-8")
            print(args.output)
        else:
            print(rendered, end="")
        return 0
    finally:
        con.close()


def inspect_arena_command(args: argparse.Namespace) -> int:
    """Print source snapshots and diagnostics; never load account-state tables."""
    con = canonical.open_db(args.database)
    try:
        provider = ArenaLogProvider(args.log, database=con)
        results = {
            "inventory": provider.get_inventory(),
            "decks": provider.get_decks(),
            "collection": provider.get_collection(),
        }
        print(json.dumps({key: asdict(value) for key, value in results.items()}, indent=2))
        supported = (results["inventory"], results["decks"])
        return 0 if all(r.ok for r in supported) else (
            2 if any(r.diagnostics.status is Status.ERROR for r in supported) else 1
        )
    finally:
        con.close()


def save_arena_command(args: argparse.Namespace) -> int:
    provider = ArenaLogProvider(args.log)
    saved = snapshot_store.save_snapshot(args.store, provider)
    print(json.dumps({"persisted": True, **snapshot_store.summarize(saved, details=True)}, indent=2))
    return 0


def list_arena_snapshots_command(args: argparse.Namespace) -> int:
    exists = args.store.exists()
    snapshots = snapshot_store.list_snapshots(args.store) if exists else []
    print(json.dumps({"store_exists": exists, "snapshots": snapshots}, indent=2))
    return 0


def show_arena_snapshot_command(args: argparse.Namespace) -> int:
    saved = snapshot_store.get_snapshot(args.store, args.snapshot_id)
    print(json.dumps(snapshot_store.summarize(saved, details=True), indent=2))
    return 0


def analyze_deck_command(args: argparse.Namespace) -> int:
    con = canonical.open_db(args.database)
    try:
        parsed = import_arena_deck(args.deck.read_text(encoding="utf-8-sig"), con)
        if not parsed.ok:
            print(json.dumps({"status": "import_error", "issues": [asdict(i) for i in parsed.issues]}, indent=2))
            return 2
        print(json.dumps(analyze_deck(parsed.deck, con), indent=2))
        return 0
    finally:
        con.close()


def diagnose_deck_command(args: argparse.Namespace) -> int:
    con = canonical.open_db(args.database)
    try:
        parsed = import_arena_deck(args.deck.read_text(encoding="utf-8-sig"), con)
        if not parsed.ok:
            print(json.dumps({"status": "import_error", "issues": [asdict(i) for i in parsed.issues]}, indent=2))
            return 2
        print(json.dumps(diagnose_analysis(analyze_deck(parsed.deck, con)), indent=2))
        return 0
    finally:
        con.close()


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--database", type=Path, default=ROOT / "current.db",
        help="canonical database (default: current.db)",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="import and validate an Arena decklist")
    validate.add_argument("deck", type=Path)
    validate.add_argument(
        "--mode", choices=[m.value for m in OperatingMode],
        default=OperatingMode.UNLIMITED.value,
    )
    validate.add_argument("--collection", type=Path)
    validate.add_argument(
        "--wildcard", action="append",
        help="available wildcard as rarity=count; repeat for each rarity",
    )
    validate.add_argument("--colors", help="allowed WUBRG color identity")
    validate.add_argument("--format", help="name of a format loaded in current.db")
    validate.add_argument("--min-main", type=int, default=60)
    validate.add_argument("--max-main", type=int, default=250)
    validate.add_argument("--max-sideboard", type=int, default=15)
    validate.add_argument("--min-commanders", type=int, default=0)
    validate.add_argument("--max-commanders", type=int, default=0)
    validate.set_defaults(func=validate_command)

    inspect = sub.add_parser(
        "inspect-arena", help="inspect read-only StartHook snapshots and capability evidence"
    )
    inspect.add_argument("log", type=Path)
    inspect.set_defaults(func=inspect_arena_command)

    save = sub.add_parser("save-arena", help="archive one StartHook observation without merging")
    save.add_argument("log", type=Path)
    save.set_defaults(func=save_arena_command)
    listing = sub.add_parser("list-arena-snapshots", help="list archived observations")
    listing.set_defaults(func=list_arena_snapshots_command)
    show = sub.add_parser("show-arena-snapshot", help="inspect one archived observation")
    show.add_argument("snapshot_id", help="opaque archive-generated snapshot ID")
    show.set_defaults(func=show_arena_snapshot_command)
    for command in (save, listing, show):
        command.add_argument("--store", type=Path, default=ROOT / "arena_snapshots.db")

    analysis = sub.add_parser("analyze-deck", help="read-only deterministic deck analysis; not legality validation")
    analysis.add_argument("deck", type=Path)
    analysis.set_defaults(func=analyze_deck_command)

    diagnosis = sub.add_parser(
        "diagnose-deck", help="read-only deterministic diagnosis over deck analysis"
    )
    diagnosis.add_argument("deck", type=Path)
    diagnosis.set_defaults(func=diagnose_deck_command)

    normalize = sub.add_parser(
        "normalize", help="resolve and render a canonical Arena decklist"
    )
    normalize.add_argument("deck", type=Path)
    normalize.add_argument("--output", "-o", type=Path)
    normalize.set_defaults(func=normalize_command)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command not in {"save-arena", "list-arena-snapshots", "show-arena-snapshot"} and not args.database.exists():
        print(f"database not found: {args.database}", file=sys.stderr)
        return 2
    try:
        return args.func(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
