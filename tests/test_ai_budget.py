from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from services.ai_budget import BudgetGuard, OPT_IN, plan, quote
from services.ai_deck_generator import build_request
from services.evaluation_requests_v2 import REQUESTS
from tests.test_deck_generator import fixture


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'budget.sqlite'
        self.pricing = dict(model='claude-opus-5-5', currency='USD', operator_confirmed=True,
                            input_usd_per_million='4.00', output_usd_per_million='20.00',
                            source_url='https://platform.claude.com/docs/en/about-claude/pricing',
                            verified_date='2026-11-13',
                            constraints=dict(fast_mode=False, batch=False, prompt_caching=False))
        self.requests = {rid: build_request(fixture(), rid) for rid in REQUESTS}
        self.counts = dict(zip(REQUESTS, [470716, 470829, 169672, 174244, 171369]))
        self.auth = dict(opt_in=OPT_IN, phase0_accepted=True, account_limit_confirmed=True)

    def initialize(self):
        return BudgetGuard.initialize(self.path, self.requests, self.counts, self.pricing)

    def test_projection_and_all_five_persisted(self):
        guard = self.initialize()
        self.assertEqual(plan(self.requests, self.counts, self.pricing)['projected_usd'], '9.027320')
        for rid in REQUESTS:
            BudgetGuard(self.path).reserve(rid, self.requests[rid], **self.auth)
        state = guard.snapshot()
        self.assertEqual(len(state['reservations']), 5)
        self.assertLessEqual(sum(n for _, n in state['reservations']), 10_000_000_000)
        self.assertGreater(Decimal(state['config']['reserved_usd']), Decimal('9.02732'))

    def test_no_authorization_no_reservation_and_durable_log(self):
        guard = self.initialize()
        for auth in ({}, dict(self.auth, opt_in='I authorize up to $5.00 in Anthropic API calls for Run 24 Phase 1.'),
                     dict(self.auth, account_limit_confirmed=False), dict(self.auth, phase0_accepted=False)):
            with self.assertRaisesRegex(ValueError, 'preconditions'):
                guard.reserve('std-bo1', self.requests['std-bo1'], **auth)
        self.assertEqual(guard.snapshot()['reservations'], [])
        self.assertEqual(len(guard.snapshot()['decisions']), 5)

    def test_order_changed_request_and_duplicate_refused(self):
        guard = self.initialize()
        with self.assertRaisesRegex(ValueError, 'order'):
            guard.reserve('std-bo3', self.requests['std-bo3'], **self.auth)
        for key, value in [('max_tokens', 32001), ('model', 'other'), ('system', 'changed')]:
            body = deepcopy(self.requests['std-bo1']); body[key] = value
            with self.assertRaisesRegex(ValueError, 'changed'):
                guard.reserve('std-bo1', body, **self.auth)
        guard.reserve('std-bo1', self.requests['std-bo1'], **self.auth)
        guard.record_outcome('std-bo1', 'transport_failure')
        with self.assertRaisesRegex(ValueError, 'no_retry'):
            BudgetGuard(self.path).reserve('std-bo1', self.requests['std-bo1'], **self.auth)
        self.assertEqual(len(guard.snapshot()['reservations']), 1)

    def test_concurrent_reservations_only_one_wins(self):
        self.initialize()
        def attempt(_):
            try:
                BudgetGuard(self.path).reserve('std-bo1', self.requests['std-bo1'], **self.auth)
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=2) as workers:
            self.assertEqual(sum(workers.map(attempt, range(2))), 1)

    def test_cap_refusal_logged_before_spending(self):
        guard = self.initialize()
        # Simulate a pre-existing charge from a prior in-flight operation.
        with closing(guard.connect()) as db, db:
            db.execute('INSERT INTO reservations VALUES (?,?)', ('prior-charge', 9_000_000_000))
        with self.assertRaisesRegex(ValueError, 'budget_exceeded'):
            guard.reserve('std-bo1', self.requests['std-bo1'], **self.auth)
        self.assertEqual(guard.snapshot()['decisions'][-1]['reason'], 'cumulative_budget_exceeded')

    def test_invalid_rates_counts_limits_and_context_fail_closed(self):
        for value in ('NaN', 'Infinity', '-1', '0'):
            with self.assertRaises(ValueError):
                quote(100, 32000, dict(self.pricing, input_usd_per_million=value))
        for value in (True, -1, 0, 1.5):
            with self.assertRaises(ValueError):
                quote(value, 32000, self.pricing)
        with self.assertRaises(ValueError):
            quote(100, 8192, self.pricing)
        with self.assertRaisesRegex(ValueError, 'context'):
            quote(999999, 32000, self.pricing)
        with self.assertRaisesRegex(ValueError, 'cap'):
            plan(self.requests, dict.fromkeys(REQUESTS, 500000), self.pricing)
        with self.assertRaises(ValueError):
            plan(self.requests, self.counts, dict(self.pricing, operator_confirmed=False))

    def test_missing_corrupt_and_existing_ledger_never_reset(self):
        with self.assertRaises(Exception):
            BudgetGuard(self.path).snapshot()
        self.assertFalse(self.path.exists())
        self.initialize()
        before = self.path.read_bytes()
        with self.assertRaises(FileExistsError):
            self.initialize()
        self.assertEqual(before, self.path.read_bytes())
        self.path.write_bytes(b'corrupt')
        with self.assertRaises(Exception):
            BudgetGuard(self.path).snapshot()
        self.assertEqual(self.path.read_bytes(), b'corrupt')
