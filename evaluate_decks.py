"""Offline evaluation harness. Every output is new and outside the repository."""
import argparse
from pathlib import Path
from services import deck_evaluation as ev


def external(path):
    p=Path(path).resolve()
    root=Path('C:/MTG-Source-Preservation/evaluations').resolve()
    if root not in p.parents or p.exists():
        raise ValueError('output must be a new path below the external evaluations root')
    return p


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    b=sub.add_parser('build'); b.add_argument('--output',required=True)
    c=sub.add_parser('candidate')
    c.add_argument('--set',required=True); c.add_argument('--deck',required=True)
    c.add_argument('--request',choices=ev.REQUESTS,required=True)
    c.add_argument('--provenance',required=True); c.add_argument('--output',required=True)
    p=sub.add_parser('worksheets'); p.add_argument('--set',required=True)
    p.add_argument('--candidate',action='append',required=True); p.add_argument('--output',required=True)
    i=sub.add_parser('ingest'); i.add_argument('--set',required=True)
    i.add_argument('--worksheets',required=True); i.add_argument('--candidate',action='append',default=[])
    i.add_argument('--grader',required=True); i.add_argument('--date',required=True); i.add_argument('--output',required=True)
    x=sub.add_parser('compare'); x.add_argument('--left',required=True); x.add_argument('--right',required=True); x.add_argument('--output',required=True)
    a=parser.parse_args(argv); out=external(a.output)
    if a.command=='build':
        entries=ev.read_json(Path(__file__).parent/'docs/deck_evaluation_v1_inputs.json')
        ev.build(entries,ev.real_facts(),out)
    elif a.command=='candidate':
        ev.register_candidate(a.set,a.deck,a.request,ev.read_json(a.provenance),out)
    elif a.command=='worksheets':
        entries,seal=ev.entries_with_candidates(a.set,a.candidate)
        ev.packet([e for e in entries if e['origin']=='candidate'],ev.real_facts(),out,seal['manifest_sha256'])
    elif a.command=='ingest':
        ev.ingest(a.set,a.worksheets,a.candidate,a.grader,a.date,out)
    else:
        ev.write_new(out,ev.compare(ev.read_json(a.left),ev.read_json(a.right)))
    print(out)


if __name__=='__main__':
    main()
