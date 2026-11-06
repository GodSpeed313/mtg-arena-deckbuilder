"""Build or query an opt-in pinned Standard legality reference snapshot."""
import argparse
import json
from pathlib import Path

from mtgadb.standard_legality import PINS, bindings, build_snapshot, query


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    build = commands.add_parser('build')
    build.add_argument('--queue', choices=['BO1', 'BO3'], action='append', required=True)
    build.add_argument('--output', type=Path, required=True)
    build.add_argument('--scryfall', type=Path, default=Path('C:/MTG-Source-Preservation/scryfall/2026-11-06/default-cards-20261105220650.jsonl.gz'))
    build.add_argument('--candidate', type=Path, default=Path('C:/MTG-Source-Preservation/candidates/2026-10-24-r1/canonical-candidate.db'))
    build.add_argument('--arena', type=Path, default=Path('C:/MTG-Source-Preservation/2026-10-24-r1/arena/Raw_CardDatabase_c344d0dccdfb898a5e3fd4101eedbd48.mtga'))
    build.add_argument('--formats', type=Path, default=Path(__file__).parent / 'current.db')
    read = commands.add_parser('query')
    read.add_argument('--snapshot', type=Path, required=True)
    read.add_argument('--queue', choices=['BO1', 'BO3'], required=True)
    identity = read.add_mutually_exclusive_group(required=True)
    identity.add_argument('--title-id', type=int)
    identity.add_argument('--arena-id', type=int)
    args = parser.parse_args(argv)
    try:
        if args.command == 'build':
            root = Path('C:/MTG-Source-Preservation/legality').resolve()
            if root not in args.output.resolve().parents:
                raise ValueError('output must be a new subdirectory under C:/MTG-Source-Preservation/legality')
            path = build_snapshot({k: getattr(args, k) for k in PINS}, PINS, bindings(), args.queue, args.output)
            print(json.dumps({'snapshot': str(path)}))
        else:
            print(json.dumps(query(args.snapshot, args.queue, title_id=args.title_id, arena_id=args.arena_id), indent=2))
    except (OSError, ValueError) as exc:
        parser.exit(1, str(exc) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
