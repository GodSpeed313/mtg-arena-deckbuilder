"""Opt-in deterministic Standard construction with separate text validation."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from services.deck_construction_data import SNAPSHOT, DATABASE, ARENA, PINS
from services.deterministic_deck_generator import generate


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--format', choices=['Standard'], required=True)
    p.add_argument('--queue', choices=['BO1', 'BO3'], required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args(argv)
    root = Path('C:/MTG-Source-Preservation/decks').resolve()
    output = a.output.resolve()
    if root not in output.parents or output.exists():
        p.error('output must be a new directory below the external decks root')
    text, report = generate(dict(snapshot=SNAPSHOT, database=DATABASE, arena=ARENA), PINS, a.queue)
    output.mkdir(parents=True, exist_ok=False)
    (output / 'deck.txt').write_text(text, encoding='utf-8', newline='\n')
    (output / 'report.json').write_text(report, encoding='utf-8', newline='\n')
    (output / 'run-metadata.json').write_text(json.dumps(dict(created_at_utc=datetime.now(timezone.utc).isoformat(), output=str(output), final_commit=None), indent=2), encoding='utf-8')
    print(str(output))


if __name__ == '__main__':
    main()
