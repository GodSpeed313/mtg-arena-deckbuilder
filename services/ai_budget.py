"""Persistent conservative reservations; no provider/network access.

Reserve the complete worst-case cost before dispatch. Never refund a reservation,
including after timeout or failure. SQLite serializes concurrent attempts. Missing
or corrupt ledgers fail closed; initialization is explicit and never overwrites.
"""
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
from pathlib import Path
import sqlite3

from services.ai_deck_generator import MODEL, MAX_TOKENS, CONTEXT_TOKENS, compact
from services.evaluation_requests_v2 import REQUESTS

CAP_USD = Decimal('10.00')
OPT_IN = 'I authorize up to $10.00 in Anthropic API calls for Run 24 Phase 1.'


def digest(value):
    return hashlib.sha256(compact(value).encode('utf-8')).hexdigest()


def rates(config):
    if config.get('operator_confirmed') is not True or config.get('model') != MODEL or config.get('currency') != 'USD':
        raise ValueError('operator-confirmed USD pricing for exact model required')
    if not config.get('source_url') or not config.get('verified_date'):
        raise ValueError('pricing provenance required')
    if config.get('constraints') != dict(fast_mode=False, batch=False, prompt_caching=False):
        raise ValueError('standard-mode pricing constraints required')
    result = [Decimal(str(config[k])) for k in ('input_usd_per_million', 'output_usd_per_million')]
    if any(not x.is_finite() or x <= 0 for x in result):
        raise ValueError('rates must be finite and positive')
    return result


def quote(counted_input, output_limit, pricing):
    if type(counted_input) is not int or counted_input <= 0:
        raise ValueError('positive integer input count required')
    if type(output_limit) is not int or output_limit != MAX_TOKENS:
        raise ValueError('frozen output allowance required')
    inp, out = rates(pricing)
    # Provider counts are estimates: a conservative 10% input reserve is additional
    # to the full output maximum. This margin is not a certified tokenizer bound.
    guarded = (counted_input * 110 + 99) // 100
    if guarded + output_limit > CONTEXT_TOKENS:
        raise ValueError('guarded input plus output exceeds context')
    base = (counted_input * inp + output_limit * out) / 1_000_000
    worst = (guarded * inp + output_limit * out) / 1_000_000
    nano = int((worst * 1_000_000_000).to_integral_value(rounding=ROUND_CEILING))
    return dict(counted_input=counted_input, guarded_input=guarded, max_tokens=output_limit,
                projected_usd=str(base), reserved_usd=str(Decimal(nano) / 1_000_000_000), reserved_nano=nano)


def plan(requests, counts, pricing):
    if list(requests) != list(REQUESTS) or set(counts) != set(REQUESTS):
        raise ValueError('exact registered request inventory and order required')
    entries = []
    for rid, body in requests.items():
        if (set(body) != {'model', 'max_tokens', 'system', 'thinking', 'output_config', 'messages'}
                or body['model'] != MODEL or body['thinking'] != {'type': 'adaptive'}
                or body['output_config'] != {'effort': 'medium'}):
            raise ValueError('unfrozen or nonstandard request settings')
        entries.append(dict(request_id=rid, request_hash=digest(body),
                            **quote(counts[rid], body['max_tokens'], pricing)))
    total = sum(e['reserved_nano'] for e in entries)
    if total > int(CAP_USD * 1_000_000_000):
        raise ValueError('five-request worst-case projection exceeds cumulative cap')
    return dict(cap_nano=int(CAP_USD * 1_000_000_000), pricing=pricing, entries=entries,
                projected_usd=str(sum(Decimal(e['projected_usd']) for e in entries)),
                reserved_usd=str(Decimal(total) / 1_000_000_000))


class BudgetGuard:
    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        # mode=rw never silently recreates a missing ledger.
        db = sqlite3.connect(self.path.resolve().as_uri() + '?mode=rw', uri=True, timeout=30)
        try:
            db.execute('PRAGMA synchronous=FULL')
        except Exception:
            db.close()
            raise
        return db

    @classmethod
    def initialize(cls, path, requests, counts, pricing):
        config = plan(requests, counts, pricing)
        path = Path(path)
        with path.open('xb'):
            pass
        guard = cls(path)
        with closing(guard.connect()) as db, db:
            db.execute('CREATE TABLE config (value TEXT NOT NULL)')
            db.execute('INSERT INTO config VALUES (?)', (compact(config),))
            db.execute('CREATE TABLE reservations (request_id TEXT PRIMARY KEY, nano INTEGER NOT NULL)')
            db.execute('CREATE TABLE decisions (seq INTEGER PRIMARY KEY, at TEXT NOT NULL, detail TEXT NOT NULL)')
            guard.log(db, dict(decision='initialized', phase1_authorized=False, cap_usd=str(CAP_USD)))
        return guard

    @staticmethod
    def log(db, detail):
        db.execute('INSERT INTO decisions(at,detail) VALUES (?,?)',
                   (datetime.now(timezone.utc).isoformat(), compact(detail)))

    def reserve(self, request_id, request, *, opt_in='', phase0_accepted=False, account_limit_confirmed=False):
        reason = None
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                config = json.loads(db.execute('SELECT value FROM config').fetchone()[0])
                used = db.execute('SELECT COALESCE(SUM(nano),0) FROM reservations').fetchone()[0]
                attempted = {r[0] for r in db.execute('SELECT request_id FROM reservations')}
                entry = next((e for e in config['entries'] if e['request_id'] == request_id), None)
                remaining = [e['request_id'] for e in config['entries'] if e['request_id'] not in attempted]
                if opt_in != OPT_IN or phase0_accepted is not True or account_limit_confirmed is not True:
                    reason = 'phase1_preconditions_missing'
                elif entry is None or entry['request_hash'] != digest(request):
                    reason = 'unregistered_or_changed_request'
                elif request_id in attempted:
                    reason = 'request_already_reserved_no_retry'
                elif not remaining or request_id != remaining[0]:
                    reason = 'registered_order_required'
                elif used + entry['reserved_nano'] > config['cap_nano']:
                    reason = 'cumulative_budget_exceeded'
                if reason is None:
                    db.execute('INSERT INTO reservations VALUES (?,?)', (request_id, entry['reserved_nano']))
                self.log(db, dict(request_id=request_id, decision='refused' if reason else 'reserved',
                                  reason=reason, used_before_nano=used, remaining_before_nano=config['cap_nano']-used,
                                  worst_case_nano=entry['reserved_nano'] if entry else None))
                db.commit()
            except Exception:
                db.rollback()
                raise
        if reason:
            raise ValueError(reason)
        return entry

    def record_outcome(self, request_id, status, usage=None):
        if status not in ('PASS', 'FAIL', 'transport_failure', 'truncated'):
            raise ValueError('invalid outcome')
        if usage is not None and (set(usage) != {'input_tokens', 'output_tokens'} or
                any(type(n) is not int or n < 0 for n in usage.values())):
            raise ValueError('invalid usage')
        with closing(self.connect()) as db, db:
            if db.execute('SELECT 1 FROM reservations WHERE request_id=?', (request_id,)).fetchone() is None:
                raise ValueError('outcome without reservation')
            self.log(db, dict(request_id=request_id, decision='outcome', status=status, usage=usage,
                              reservation_retained=True))

    def snapshot(self):
        with closing(self.connect()) as db:
            return dict(config=json.loads(db.execute('SELECT value FROM config').fetchone()[0]),
                        reservations=list(db.execute('SELECT request_id,nano FROM reservations ORDER BY rowid')),
                        decisions=[json.loads(r[0]) for r in db.execute('SELECT detail FROM decisions ORDER BY seq')])
