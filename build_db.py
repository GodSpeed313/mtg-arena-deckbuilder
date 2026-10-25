"""Build the canonical database from available providers.

    python build_db.py            # build today's snapshot
    python build_db.py --stats    # summarize the current snapshot
"""

from __future__ import annotations

import argparse
from contextlib import closing
import os
import shutil
import sqlite3
import sys
from pathlib import Path

from mtgadb import canonical
from mtgadb.providers.arena_cards import ArenaSQLiteCardProvider
from mtgadb.query import CardQueryEngine

ROOT = Path(__file__).parent
CURRENT = ROOT / "current.db"


def explicit_paths(source: Path, output: Path) -> tuple[Path, Path]:
    """Resolve aliases before checking boundaries; never discover a source."""
    source = source.resolve(strict=True)
    output_exists = os.path.lexists(output)
    output = output.resolve()
    if output == CURRENT.resolve():
        raise ValueError("output resolves to current.db")
    if output_exists or output.exists():
        raise ValueError("output already exists")
    if output.is_relative_to(ROOT.resolve()):
        raise ValueError("output is inside the repository")
    if output.is_relative_to(source.parent) or source.parent.is_relative_to(output):
        raise ValueError("output overlaps the source directory")
    if not source.is_file():
        raise ValueError("source must be a file")
    return source, output


def build_explicit(source: Path, output: Path) -> int:
    provider = None
    try:
        source, output = explicit_paths(source, output)
        provider = ArenaSQLiteCardProvider(source)
        # Stage in memory: canonical.create() intentionally replaces files and
        # therefore must never be used for an explicit destination.
        with closing(sqlite3.connect(":memory:")) as staged:
            staged.executescript(canonical.SCHEMA)
            n_print = canonical.load_printings(staged, provider.iter_printings())
            n_cards = canonical.load_cards(staged, provider.iter_cards())
            canonical.stamp(staged, provider.fingerprint())
            staged.commit()
            source, output = explicit_paths(source, output)
            # Parent must already exist. Exclusive creation also catches a file
            # created since validation, without truncating its contents.
            with output.open("xb"):
                pass
            target = sqlite3.connect(output.as_uri() + "?mode=rw", uri=True)
            try:
                staged.backup(target)
            finally:
                target.close()
        print(f"output      {output}")
        print(f"cards       {n_cards:,}")
        print(f"printings   {n_print:,}")
        return 0
    except (OSError, ValueError, sqlite3.Error) as e:
        print(f"Explicit build failed: {e}", file=sys.stderr)
        return 1
    finally:
        if provider is not None:
            provider.con.close()


def build() -> int:
    try:
        cards_provider = ArenaSQLiteCardProvider()
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 1

    out = canonical.snapshot_path(ROOT)
    print(f"source      {cards_provider.path.name}")
    print(f"fingerprint {cards_provider.fingerprint()}")

    con = canonical.create(out)
    with con:
        n_print = canonical.load_printings(con, cards_provider.iter_printings())
        n_cards = canonical.load_cards(con, cards_provider.iter_cards())
        canonical.stamp(con, cards_provider.fingerprint())
    con.close()

    shutil.copyfile(out, CURRENT)
    print(f"snapshot    {out.relative_to(ROOT)}")
    print(f"current     {CURRENT.name}")
    print(f"cards       {n_cards:,}")
    print(f"printings   {n_print:,}")
    return 0


def stats() -> int:
    if not CURRENT.exists():
        print("No current.db -- run `python build_db.py` first.", file=sys.stderr)
        return 1
    con = canonical.open_db(CURRENT)
    q = CardQueryEngine(con)
    print(f"schema      {canonical.get_meta(con, 'schema_version')}")
    print(f"built       {canonical.get_meta(con, 'created_at')}")
    print(f"fingerprint {canonical.get_meta(con, 'arena_db_fingerprint')}")
    for table in ("cards", "printings", "ownership", "formats", "decks"):
        print(f"  {table:<11} {q.count(table):>7,}")
    rows = con.execute(
        "SELECT resolution, COUNT(*) FROM cards GROUP BY resolution ORDER BY 2 DESC"
    ).fetchall()
    print("  resolution:")
    for name, n in rows:
        print(f"    {name:<16} {n:>7,}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stats", action="store_true", help="summarize current.db")
    ap.add_argument("--source", type=Path, help="explicit Arena .mtga source")
    ap.add_argument("--output", type=Path, help="new database outside the repository")
    args = ap.parse_args(argv)
    if (args.source is None) != (args.output is None):
        ap.error("--source and --output must be supplied together")
    if args.source is not None:
        if args.stats:
            ap.error("--stats cannot be combined with --source/--output")
        return build_explicit(args.source, args.output)
    return stats() if args.stats else build()


if __name__ == "__main__":
    raise SystemExit(main())
