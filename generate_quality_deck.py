"""Opt-in Generator B for the preregistered Standard requests."""
import argparse
from pathlib import Path
from services.deck_construction_data import SNAPSHOT,DATABASE,ARENA,PINS
from services.evaluation_requests_v2 import REQUESTS
from services.quality_deck_generator import generate


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--format',choices=['Standard'],required=True)
    p.add_argument('--queue',choices=['BO1','BO3'],required=True)
    p.add_argument('--color-pair',choices=['WU','BR','GW'])
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args(argv)
    requests=[k for k,v in REQUESTS.items() if v==dict(format=a.format,queue=a.queue,color_pair=a.color_pair)]
    if len(requests)!=1:p.error('unregistered format/queue/color-pair request')
    out=a.output.resolve();root=Path('C:/MTG-Source-Preservation/evaluations').resolve()
    if root not in out.parents or out.exists():p.error('output must be new and outside repository under evaluations')
    text,report=generate(dict(snapshot=SNAPSHOT,database=DATABASE,arena=ARENA),PINS,requests[0])
    out.mkdir(parents=True,exist_ok=False)
    (out/'deck.txt').write_text(text,encoding='utf-8',newline='\n')
    (out/'report.json').write_text(report,encoding='utf-8',newline='\n')
    print(requests[0]+': generated and validated')


if __name__=='__main__':main()
