"""Load an extracted Arena Formats JSON array into the canonical database."""

from __future__ import annotations

import argparse
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from mtgadb import canonical
from mtgadb.providers.formats_json import JSONFormatProvider


ROOT = Path(__file__).parent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("formats", type=Path, help="extracted Formats JSON array")
    ap.add_argument("--database", type=Path, default=ROOT / "current.db")
    args = ap.parse_args(argv)

    result = JSONFormatProvider(args.formats).get_formats()
    if not result.ok:
        print(result.diagnostics.render())
        return 1
    assert result.data is not None

    digest = hashlib.sha256(args.formats.read_bytes()).hexdigest()
    con = canonical.open_writable(args.database)
    try:
        with con:
            canonical.migrate(con)
            count = canonical.load_formats(con, result.data)
            canonical.set_meta(
                con,
                formats_source=args.formats.name,
                formats_sha256=digest,
                formats_loaded_at=datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
                formats_count=str(count),
            )
    finally:
        con.close()

    print(f"formats     {count}")
    print(f"database    {args.database}")
    print(f"sha256      {digest}")
    print(f"unknown     {result.diagnostics.evidence['unknown_fields']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

