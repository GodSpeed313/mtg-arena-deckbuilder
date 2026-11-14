"""Run 24 preparation and free token counting. Contains no generation endpoint."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import urllib.error
import urllib.request

from services.ai_deck_generator import build_request, compact, legal_pool, ROOT
from services.deck_construction_data import SNAPSHOT, DATABASE, ARENA, PINS, read_facts
from services.evaluation_requests_v2 import REQUESTS

PATHS = dict(snapshot=SNAPSHOT, database=DATABASE, arena=ARENA)
OUTPUT_ROOT = Path('C:/MTG-Source-Preservation/evaluations')


def write(path, value):
    with path.open('x', encoding='utf-8', newline='\n') as f:
        f.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(output):
    output.mkdir(exist_ok=False)
    (output / 'requests').mkdir()
    facts = read_facts(PATHS, PINS, 'BO1')
    records = {}
    for rid in REQUESTS:
        body = build_request(facts, rid)
        path = output / 'requests' / (rid + '.json')
        write(path, body)
        records[rid] = dict(pool_count=len(legal_pool(facts, rid)), sha256=sha(path),
                            utf8_request_bytes=len(compact(body).encode('utf-8')),
                            max_tokens=body['max_tokens'])
    frozen = {}
    for name in ('run24_prompt.txt', 'run24_grading_protocol.md'):
        path = output / name
        with path.open('xb') as f:
            f.write((ROOT / 'docs' / name).read_bytes())
        frozen[name] = sha(path)
    write(output / 'pricing.json', json.loads((ROOT / 'docs/run24_pricing.example.json').read_text()))
    write(output / 'preparation.json', dict(requests=records, frozen=frozen, input_pins=PINS,
                                          prepared_at=datetime.now(timezone.utc).isoformat(),
                                          phase1_authorized=False))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('redirect refused')


def count(output):
    """Only the documented-free count_tokens endpoint, never a retry."""
    key = os.environ.get('ANTHROPIC_API_KEY')
    if not key:
        raise ValueError('ANTHROPIC_API_KEY absent')
    preparation = json.loads((output / 'preparation.json').read_text(encoding='utf-8'))
    for rid in REQUESTS:
        target = output / (rid + '-token-count.json')
        if target.exists():
            raise ValueError('token-count evidence already exists; refusing overwrite')
        path = output / 'requests' / (rid + '.json')
        if sha(path) != preparation['requests'][rid]['sha256']:
            raise ValueError('prepared request changed')
        body = json.loads(path.read_text(encoding='utf-8'))
        body.pop('max_tokens')  # Not an input field for token counting.
        request = urllib.request.Request('https://api.anthropic.com/v1/messages/count_tokens',
            data=compact(body).encode('utf-8'), method='POST',
            headers={'x-api-key': key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'})
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(request, timeout=60) as response:
                data = json.loads(response.read())
                evidence = dict(status=response.status, response=data,
                                request_id=response.headers.get('request-id'))
        except urllib.error.HTTPError as exc:
            # Do not persist arbitrary provider error bodies or credential-bearing objects.
            evidence = dict(status=exc.code, error='provider rejected token-count request')
        except Exception:
            evidence = dict(status=None, error='token-count transport failed; no retry')
        write(target, dict(evidence, model=body['model'], request_sha256=sha(path),
                           counted_at=datetime.now(timezone.utc).isoformat(),
                           free_endpoint_documentation='https://platform.claude.com/docs/en/build-with-claude/token-counting#pricing-and-rate-limits'))
        print(rid, evidence, flush=True)
        if evidence['status'] != 200:
            raise ValueError('token counting failed; stop for review')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'count'))
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    if OUTPUT_ROOT.resolve() not in output.parents:
        parser.error('output must be under the external evaluations directory')
    {'prepare': prepare, 'count': count}[args.action](output)


if __name__ == '__main__':
    main()
