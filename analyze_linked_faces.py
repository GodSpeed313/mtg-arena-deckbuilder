"""Opt-in Analysis 7 CLI with explicit verified inputs and JSON stdout."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sys

from mtgadb.model import Deck
from mtgadb.structural_facts import readonly
from services.analysis7 import LinkedEvidence, analyze_deck7


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('artifact','output-manifest','raw-source','source-manifest','candidate','deck'):
        parser.add_argument('--'+key, type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        evidence = LinkedEvidence(args.artifact,args.output_manifest,args.raw_source,args.source_manifest)
        data = json.loads(args.deck.read_text(encoding='utf-8'))
        deck = Deck(name=data.get('name',''), **{z:{int(k):v for k,v in data.get(z,{}).items()}
                                               for z in ('main','sideboard','commander')})
        with closing(readonly(args.candidate)) as con:
            result = analyze_deck7(deck,con,evidence)
    except Exception as error:
        print(json.dumps({'status':'failed','diagnostic':str(error)}),file=sys.stderr)
        return 1
    print(json.dumps(result,sort_keys=True,separators=(',',':'),ensure_ascii=True))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
