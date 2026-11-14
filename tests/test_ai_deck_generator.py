from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from services.ai_deck_generator import (assess, build_request, citation_flags,
                                       fake_generate, legal_pool, parse_response,
                                       encode_pool, decode_pool, FIELDS, assess_message)
from tests.test_deck_generator import fixture


class Fake:
    is_fake = True

    def __init__(self, response):
        self.response, self.calls = response, []

    def __call__(self, request):
        self.calls.append(deepcopy(request))
        return self.response


class GeneratorCTests(unittest.TestCase):
    def setUp(self):
        self.facts = fixture()
        self.data = dict(main=[dict(name='Plains', quantity=60)], sideboard=[], interactions=[])
        self.patch = patch('services.generated_deck_validator.read_facts', return_value=self.facts)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def assess(self, data=None, request='std-bo1'):
        return assess(json.dumps(data or self.data), self.facts, {}, {}, request)

    def test_full_pool_retains_b_exclusions(self):
        c = self.facts['cards'][1]
        c.update(mana_cost='{W/U}', cmc=0, rules_text='A deck can have any number of cards named Card 1.')
        self.assertIn(1, {c['title_id'] for c in legal_pool(self.facts, 'std-bo1')})
        c.update(types='Land', color_identity='')
        self.assertIn(1, {c['title_id'] for c in legal_pool(self.facts, 'std-bo1')})
        self.facts['snapshot']['payload']['titles']['1']['queues']['BO1']['status'] = 'illegal'
        self.assertNotIn(1, {c['title_id'] for c in legal_pool(self.facts, 'std-bo1')})
        self.assertIn(1, {c['title_id'] for c in legal_pool(self.facts, 'std-bo3')})

    def test_missing_metadata_stops_instead_of_filtering(self):
        self.facts['cards'][1]['rules_text'] = None
        with self.assertRaises(ValueError):
            legal_pool(self.facts, 'std-bo1')

    def test_pair_filter_and_full_text_preserved(self):
        self.facts['cards'][1].update(colors='R', color_identity='R')
        body = build_request(self.facts, 'std-bo1-wu')
        header, encoded = body['messages'][0]['content'].split('\n', 1)
        payload = decode_pool(encoded)
        names = [r['name'] for r in payload]
        self.assertNotIn('Card 1', names)
        self.assertEqual(body['model'], 'claude-opus-5-5')
        self.assertEqual(len(payload), len(self.facts['cards']) - 1)

    def test_encoding_exact_round_trip_all_fields_and_duplicates(self):
        pool = legal_pool(self.facts, 'std-bo1')
        pool[0].update(name='Name\twith\\slashes', rules_text='Écho\n\r\t\\N literal \\n {W/U}',
                       power='*', toughness='1+*', resolution=None, cmc=1.5)
        pool.append(deepcopy(pool[0]))
        encoded = encode_pool(pool)
        expected = [{k: c.get(k) for k in FIELDS} for c in pool]
        self.assertEqual(decode_pool(encoded), expected)
        self.assertEqual(len(encoded.split('\n')), len(pool))
        self.assertEqual(encode_pool(decode_pool(encoded)), encoded)
        self.assertEqual(decode_pool(''), [])

    def test_empty_null_and_literal_null_remain_distinct(self):
        pool = [{k: None for k in FIELDS} for _ in range(3)]
        for c, text in zip(pool, ['', None, r'\N']):
            c['rules_text'] = text
        self.assertEqual(decode_pool(encode_pool(pool)), pool)

    def test_valid_output_uses_independent_validator_and_export(self):
        result = self.assess()
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['independent_validator']['status'], 'PASS')
        self.assertEqual(result['export_text'], 'Deck\n60 Plains (TST) 100\n')

    def test_wrong_count_preserved(self):
        self.data['main'][0]['quantity'] = 59
        before = deepcopy(self.data)
        result = self.assess()
        self.assertEqual(result['status'], 'FAIL')
        self.assertIn('59 Plains', result['export_text'])
        self.assertEqual(before, self.data)

    def test_exact_names_no_case_whitespace_alias_repair(self):
        for name in ('plains', 'Plains ', 'Plain', 'Unknown'):
            self.data['main'][0]['name'] = name
            result = self.assess()
            self.assertEqual(result['status'], 'FAIL')
            self.assertNotIn('export_text', result)

    def test_illegal_card_is_failure_not_replaced(self):
        self.facts['snapshot']['payload']['titles']['100']['queues']['BO1']['status'] = 'illegal'
        self.assertEqual(self.assess()['status'], 'FAIL')

    def test_sideboard_exact_fifteen_and_no_completion(self):
        self.assertEqual(self.assess(request='std-bo3')['status'], 'FAIL')
        self.data['sideboard'] = [dict(name='Plains', quantity=15)]
        self.assertEqual(self.assess(request='std-bo3')['status'], 'PASS')
        self.assertEqual(self.assess()['status'], 'FAIL')

    def test_copy_limit_and_pair_quota(self):
        self.data['main'] = [dict(name='Plains', quantity=55), dict(name='Card 1', quantity=5)]
        self.assertEqual(self.assess()['status'], 'FAIL')
        self.data['main'] = [dict(name='Plains', quantity=60)]
        self.assertEqual(self.assess(request='std-bo1-wu')['status'], 'FAIL')

    def test_strict_parser(self):
        valid = json.dumps(self.data)
        bad = ['```json\n' + valid + '\n```', valid + valid,
               '{"main":[],"main":[],"sideboard":[],"interactions":[]}',
               valid.replace('60', 'true'), valid.replace('60', '60.0'),
               valid.replace('60', 'NaN'), valid.replace('60', '-1')]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_response(raw)

    def test_citations_flag_only_exact_spans(self):
        self.data['interactions'] = [dict(claim='A claimed interaction.', citations=[
            dict(name='Plains', quote='Add {W}.')])]
        result = self.assess()
        self.assertEqual(result['status'], 'PASS')
        self.assertTrue(result['citation_check']['flags'])
        self.data['main'] = [dict(name='Card 1', quantity=1)]
        self.data['interactions'][0]['citations'] = [dict(name='Card 1', quote='Flying')]
        self.assertFalse(citation_flags(self.data, self.facts)['flags'])
        for quote in ('flying', 'Flying ', ''):
            self.data['interactions'][0]['citations'][0]['quote'] = quote
            self.assertTrue(citation_flags(self.data, self.facts)['flags'])

    def test_one_call_even_for_failure_no_real_provider(self):
        fake = Fake('not JSON')
        result = fake_generate(fake, self.facts, {}, {}, 'std-bo1')
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(len(fake.calls), 1)

    def test_cutoff_valid_json_still_fails_and_thinking_is_not_parsed(self):
        message = dict(model='claude-opus-5-5', stop_reason='end_turn', content=[
            dict(type='thinking', thinking='not JSON'), dict(type='text', text=json.dumps(self.data))])
        self.assertEqual(assess_message(message, self.facts, {}, {}, 'std-bo1')['status'], 'PASS')
        for reason in ('max_tokens', 'pause_turn', 'refusal', None):
            message['stop_reason'] = reason
            fake = Fake(message)
            self.assertEqual(fake_generate(fake, self.facts, {}, {}, 'std-bo1')['status'], 'FAIL')
            self.assertEqual(len(fake.calls), 1)

    def test_transport_cutoff_is_not_retried(self):
        class Broken(Fake):
            def __call__(self, request):
                self.calls.append(request)
                raise TimeoutError('incomplete transport')
        fake = Broken(None)
        self.assertEqual(fake_generate(fake, self.facts, {}, {}, 'std-bo1')['status'], 'FAIL')
        self.assertEqual(len(fake.calls), 1)
        fake.is_fake = False
        with self.assertRaises(ValueError):
            fake_generate(fake, self.facts, {}, {}, 'std-bo1')
        self.assertEqual(len(fake.calls), 1)


if __name__ == '__main__':
    unittest.main()
