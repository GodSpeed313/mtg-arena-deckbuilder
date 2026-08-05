"""Build the canonical database from available providers.

    python build_db.py            # build today's snapshot
    python build_db.py --stats    # summarize the current snapshot
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from mtgadb import canonical
from mtgadb.providers.arena_cards import ArenaSQLiteCardProvider
from mtgadb.query import CardQueryEngine

ROOT = Path(__file__).parent
CURRENT = ROOT / "current.db"


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
    args = ap.parse_args(argv)
    return stats() if args.stats else build()


if __name__ == "__main__":
    raise SystemExit(main())
