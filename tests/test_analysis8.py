"""Synthetic-only tests for explicit v8 isolation and bounded partial records."""
from contextlib import closing
from dataclasses import asdict
import hashlib
import unittest

from mtgadb.structural_facts import readonly
from services.abilities import decompose_abilities, _parse_line
from services.analysis8 import decompose_abilities8, parse_entry8, recognition_record, analyze_deck8
from services.analysis6 import analyze_deck6
from services.analysis7 import analyze_deck7
from services.intelligence import analyze_deck
from services.evidence_scope import encoded, require_dependency_versions, context_from_ability
from tests import test_analysis7 as fixtures


class Analysis8Tests(unittest.TestCase):
    def parse(self, text):
        return decompose_abilities8(text, card_name="Synthetic", card_types="Creature Artifact Enchantment")

    def test_flash_partial(self):
        a = self.parse("Flash")[0]
        self.assertEqual((a.kind, a.parse_status, a.keywords[0].name), ("keyword_ability", "partial", "flash"))
        self.assertTrue(a.unsupported_remainder)
        self.assertFalse(a.effects)

    def test_enchant_partial(self):
        a = self.parse("Enchant creature")[0]
        self.assertEqual((a.parse_status, a.keywords[0].value), ("partial", "creature"))
        self.assertFalse(a.effects)

    def test_equip_cost(self):
        for cost in ("{0}", "{2}", "{W}", "{X}", "{W/U}", "{2/R}"):
            with self.subTest(cost=cost):
                a = self.parse("Equip " + cost)[0]
                self.assertEqual(a.parse_status, "partial")
                self.assertEqual((a.costs[0].kind, a.costs[0].evidence), ("mana", cost))
                self.assertEqual(recognition_record(a)["recognition_detail"]["mana_cost"], cost)

    def test_crew_integer(self):
        for n in ("0", "3", "12", "003"):
            with self.subTest(n=n):
                a = self.parse("Crew " + n)[0]
                self.assertEqual((a.parse_status, a.keywords[0].value), ("partial", n))
                self.assertEqual(recognition_record(a)["recognition_detail"]["literal_integer"], int(n))
                self.assertFalse(a.effects)

    def test_excluded_shapes_exactly_unchanged(self):
        texts = ["Creature spells you cast have flash.", "You may cast Flash now.",
                 "Enchant creature you control", "Enchant land", "Enchant permanent",
                 "Equip legendary creature {1}", "Equip—Pay 3 life.", "Equip {2}{R}",
                 "Equip {W/P}", "Equip {T}", "Equip {Q}", "Equip {E}", "Crew X",
                 "Crew 3. Activate only once each turn.", "This creature has Crew 3.",
                 "Flash (You may cast this spell any time.)", "Flash, flying", "flash",
                 "FLASH", "enchant creature", "Equip {r}", "Crew -1", "Crew 1.5",
                 "Equip {2} (Reminder text.)", "Crew 2 (Reminder text.)"]
        for text in texts:
            with self.subTest(text=text):
                self.assertEqual(self.parse(text), decompose_abilities(text, card_name="Synthetic", card_types="Creature Artifact Enchantment"))

    def test_modal_guard_preserved(self):
        text = "Choose one —\nFlash\nCrew 3"
        self.assertEqual(self.parse(text), decompose_abilities(text, card_name="Synthetic", card_types="Creature Artifact Enchantment"))

    def test_anomaly_guard_preserved(self):
        for text in ("Flash", "Enchant creature", "Equip {2}", "Crew 3"):
            self.assertEqual(decompose_abilities8(text, canonical_anomaly=True), decompose_abilities(text, canonical_anomaly=True))

    def test_default_frozen_synthetic_bytes(self):
        hashes = {
            "Flash": "2db8bf2969296429296d8d7f51ac9f2623a60e587b652d9bafa5a56cd1a4b70e",
            "Enchant creature": "bc21529c94c107f29f5cfd42ee41f13b532ba9f9be296c9b44b200881c5d9b3d",
            "Equip {2}": "3f7a2e0220bb21fb246937eff6d45ff46d34ee30944b97347614bdfc67afbd70",
            "Crew 3": "1eb626e781f0215a69757865cea6b8c8ae7fb9d4c4d91d4ff6ed21f1cbcc9eda",
            "Flying": "88ef6910ef9bc82924f8379b92005b02bd4cfe7a68c2f113be9ed6fe3ee3e87f",
            "Deathtouch": "79e8341acd21a7a2e5f9ddcb1cd6e7b6a32ba8e846d70c444b0dd1f10853ad5f",
        }
        for text, expected in hashes.items():
            old = decompose_abilities(text, card_name="Synthetic", card_types="Creature Artifact Enchantment")
            self.assertEqual(hashlib.sha256(encoded([asdict(a) for a in old])).hexdigest(), expected)

    def test_direct_entry_default_unchanged(self):
        kwargs = dict(ability_id="ability.001", source_index=1, card_name="Synthetic", card_types=frozenset({"Creature"}))
        old = _parse_line("Flash", **kwargs)
        self.assertEqual(old.parse_status, "unsupported")
        self.assertEqual(parse_entry8("Flash", **kwargs).parse_status, "partial")
        self.assertEqual(old, _parse_line("Flash", **kwargs))

    def test_invalid_version_fails_closed_even_empty(self):
        for version in (None, 8, "4", "9"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                decompose_abilities("", grammar_version=version)

    def test_existing_evidence_triples_only(self):
        for dependency in ("3", "4"):
            identity = dict(analysis_version="5", dependency_model_version=dependency, needs_model_version="3")
            self.assertEqual(require_dependency_versions(identity), dependency)
            with self.assertRaises(ValueError):
                require_dependency_versions(dict(identity, analysis_version="8"))

    def test_new_recognition_not_persistable_context(self):
        with self.assertRaises(ValueError):
            context_from_ability(self.parse("Equip {2}")[0])

    def test_historical_and_downstream_isolation(self):
        f = fixtures.LinkedFaceTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        with closing(readonly(f.fixture.candidate)) as db:
            evidence = f.evidence()
            deck = f.fixture.deck
            before = (analyze_deck(deck, db), analyze_deck6(deck, db, evidence.base), analyze_deck7(deck, db, evidence))
            new = analyze_deck8(deck, db)
            layer = new.pop("recognition_analysis")
            self.assertEqual(new, before[0])
            self.assertEqual(layer["grammar_version"], "8")
            after = (analyze_deck(deck, db), analyze_deck6(deck, db, evidence.base), analyze_deck7(deck, db, evidence))
            self.assertEqual(encoded(before), encoded(after))


if __name__ == "__main__":
    unittest.main()
