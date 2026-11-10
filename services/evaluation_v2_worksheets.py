"""Uniform v2 presentation; opaque private shuffle salt never enters worksheets."""
import hashlib
import hmac
import os
from pathlib import Path
from services.deck_evaluation import ANCHORS, automatic_flags, canonical, resolve, deck_text, write_new


def create_packet(entries,facts,output,private_salt):
    output=Path(output);output.mkdir(exist_ok=False)
    if len(private_salt)<32:raise ValueError('private shuffle salt must have at least 32 bytes')
    masked=[e for e in entries if e['request_id'] in ('std-bo1','std-bo3')]
    opened=[e for e in entries if e['request_id'] not in ('std-bo1','std-bo3')]
    if len(masked)!=4 or len(opened)!=3:raise ValueError('expected four masked and three open decks')
    if sorted(e['request_id'] for e in masked)!=['std-bo1','std-bo1','std-bo3','std-bo3']:raise ValueError('shared requests need two decks each')
    if {e['request_id'] for e in opened}!={'std-bo1-wu','std-bo1-br','std-bo1-gw'}:raise ValueError('open request inventory mismatch')
    order=sorted(masked,key=lambda e:(hmac.new(private_salt,e['opaque_id'].encode(),hashlib.sha256).hexdigest(),e['opaque_id']))
    records=[('Deck '+chr(65+i),e) for i,e in enumerate(order)]+[(e['request_id'],e) for e in sorted(opened,key=lambda e:e['request_id'])]
    key={}
    for label,e in records:
        path=Path(e['path'])
        if hashlib.sha256(path.read_bytes()).hexdigest()!=e['sha256']:raise ValueError('deck hash mismatch')
        rows=resolve(path.read_text(encoding='utf-8'),facts)
        sheet=dict(label=label,evaluation_set_version=2,rubric_version=1,protocol_version=1,flag_version=1,
                   deck_list=deck_text(rows),automatic_flags=automatic_flags(rows),rubric=ANCHORS,
                   scores={d:dict(score=None,why='') for d in ANCHORS},cards_i_would_cut='',cards_i_would_add='')
        filename=label.replace(' ','-')+'.json'
        write_new(output/filename,sheet)
        key[label]=dict(e,worksheet=filename,worksheet_sha256=hashlib.sha256((output/filename).read_bytes()).hexdigest())
    # Common filesystem timestamps, no generator/run timestamp in worksheet metadata.
    # Windows creation time is normalized by the run orchestrator before handoff.
    for path in output.glob('*.json'):os.utime(path,(1700000000,1700000000))
    return dict(algorithm='HMAC-SHA256(private 32-byte salt, opaque deck ID), sorted digest then opaque ID; A-D assigned in order',salt_hex=private_salt.hex(),entries=key)
