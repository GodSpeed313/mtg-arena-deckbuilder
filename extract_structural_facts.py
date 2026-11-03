"""Explicit, offline entry point for a standalone structural-facts snapshot."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

from mtgadb.structural_facts import ExtractionError, extract


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = extract(args.source, args.output, args.manifest)
    except (ExtractionError, OSError, sqlite3.Error, ValueError) as error:
        print(json.dumps({'status': 'failed', 'diagnostic': str(error),
                          'measurements': getattr(error, 'measurements', None)}), file=sys.stderr)
        return 1
    print(json.dumps({'status': 'complete', 'output': str(args.output),
                      'payload_sha256': result['payload_sha256'],
                      'runtime_seconds': result['runtime_seconds']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
