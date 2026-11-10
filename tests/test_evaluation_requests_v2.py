import unittest
from services.evaluation_requests_v2 import REQUESTS, REGISTRATION, allowed, check_pair


class RegistrationTests(unittest.TestCase):
    def card(self, **kw):
        return dict(dict(title_id=1,colors='W',color_identity='W',types='Creature',mana_cost='{W}',rules_text=''),**kw)

    def test_request_inventory_and_versions(self):
        self.assertEqual(set(REQUESTS),{'std-bo1','std-bo3','std-bo1-wu','std-bo1-br','std-bo1-gw'})
        self.assertEqual(REGISTRATION['rubric_version'],1)
        self.assertEqual(REGISTRATION['protocol_version'],1)
        self.assertTrue(all(r['queue']=='BO1' for r in REQUESTS.values() if r['color_pair']))

    def test_exclusion_includes_rules_identity_and_prior_eligibility(self):
        for c in [self.card(color_identity='WB'),self.card(rules_text='{B}: Draw a card.'),self.card(color_identity=None)]:
            self.assertFalse(allowed(c,'WU'))
        self.assertFalse(allowed(self.card(),'WU',False))
        self.assertTrue(allowed(self.card(colors='',color_identity='',mana_cost='{2}'),'WU'))

    def test_multicolor_and_hybrid_count_both(self):
        for cost in ['{W}{U}','{W/U}']:
            c=self.card(colors='WU',color_identity='WU',mana_cost=cost)
            result=check_pair([dict(card=c,quantity=8,zone='main')],'WU')
            self.assertEqual(result['counts'],{'W':8,'U':8})
            self.assertEqual(result['status'],'PASS')

    def test_minimum_excludes_lands_and_sideboard(self):
        rows=[dict(card=self.card(colors='WU',color_identity='WU'),quantity=7,zone='main'),dict(card=self.card(colors='WU',color_identity='WU'),quantity=4,zone='sideboard'),dict(card=self.card(types='Land',color_identity='WU'),quantity=24,zone='main')]
        self.assertEqual(check_pair(rows,'WU')['status'],'FAIL')


if __name__=='__main__':unittest.main()
