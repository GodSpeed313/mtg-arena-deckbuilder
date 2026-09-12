"""Command-line entry point for the offline deck workbench."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mtgadb import canonical
from mtgadb.model import Collection, Inventory
from mtgadb.modes import OperatingMode
from services.exporter import export_arena_deck, import_arena_deck
from services.validator import DeckRules, validate_deck


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
        rules = DeckRules(
            min_main=args.min_main,
            max_main=args.max_main,
            max_sideboard=args.max_sideboard,
            max_commanders=args.max_commanders,
            allowed_colors=(frozenset(args.colors.upper()) if args.colors else None),
        )
        report = validate_deck(
            parsed.deck,
            con,
            mode=OperatingMode(args.mode),
            rules=rules,
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
    validate.add_argument("--min-main", type=int, default=60)
    validate.add_argument("--max-main", type=int, default=250)
    validate.add_argument("--max-sideboard", type=int, default=15)
    validate.add_argument("--max-commanders", type=int, default=0)
    validate.set_defaults(func=validate_command)

    normalize = sub.add_parser(
        "normalize", help="resolve and render a canonical Arena decklist"
    )
    normalize.add_argument("deck", type=Path)
    normalize.add_argument("--output", "-o", type=Path)
    normalize.set_defaults(func=normalize_command)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not args.database.exists():
        print(f"database not found: {args.database}", file=sys.stderr)
        return 2
    try:
        return args.func(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

