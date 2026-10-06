"""Frozen benchmark provenance and byte integrity; no engine or database reads."""
import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "tests" / "fixtures" / "benchmarks"


def canonical_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


class BenchmarkManifestTests(unittest.TestCase):
    def setUp(self):
        self.v1 = json.loads((BASE / "v1/manifest.json").read_bytes())
        self.v2 = json.loads((BASE / "v2/manifest.json").read_bytes())

    def test_manifests_are_canonical_json(self):
        for version, manifest in (("v1", self.v1), ("v2", self.v2)):
            self.assertEqual((BASE / version / "manifest.json").read_bytes(), canonical_bytes(manifest))
            self.assertEqual(manifest["canonical_identity"], "deferred")

    def test_every_payload_is_hashed_and_no_fixture_is_unlisted(self):
        listed = set()

        def check(value):
            if isinstance(value, dict):
                if "path" in value and "sha256" in value:
                    path = ROOT / value["path"]
                    self.assertTrue(path.resolve().is_relative_to(BASE.resolve()))
                    self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), value["sha256"])
                    listed.add(path.resolve())
                for child in value.values():
                    check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)

        check(self.v1)
        check(self.v2)
        actual = {p.resolve() for p in BASE.rglob("*") if p.is_file() and p.name != "manifest.json"}
        self.assertEqual(listed, actual)

    def test_v2_inherits_v1_without_copying_or_changing_payloads(self):
        self.assertEqual(self.v1["benchmark_version"], "1")
        self.assertIsNone(self.v1["parent"])
        self.assertEqual(self.v2["benchmark_version"], "2")
        self.assertEqual(self.v2["parent"], self.v1["benchmark_version"])
        self.assertEqual(ROOT / self.v2["parent_manifest"], BASE / "v1/manifest.json")
        original = {m["member_id"]: m for m in self.v1["members"]}
        extended = {m["member_id"]: m for m in self.v2["members"]}
        self.assertEqual(len(original), 11)
        self.assertEqual(len(self.v1["members"]), len(original))
        self.assertEqual(len(self.v2["members"]), len(extended))
        self.assertEqual(set(extended) - set(original), {"crimson_forest"})
        self.assertTrue(set(original) <= set(extended))
        for identity, member in original.items():
            inherited = extended[identity]
            self.assertEqual(inherited["inherited_from"], "1")
            self.assertEqual((inherited["path"], inherited["sha256"]), (member["path"], member["sha256"]))
        self.assertEqual({p.name for p in (BASE / "v2/decks").iterdir()}, {"crimson_forest.json"})

    def test_archive_evidence_and_attestations_have_explicit_provenance(self):
        def check(value):
            if isinstance(value, dict):
                if value.get("provenance") in ("historical_audit", "operator_attestation", "arena_log_observation"):
                    self.assertTrue(value.get("locator"))
                for attestation in value.get("attestations", []):
                    self.assertEqual(attestation["provenance"], "operator_attestation")
                for child in value.values():
                    check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)

        check(self.v1)
        check(self.v2)
        dragons = next(m for m in self.v1["members"] if m["member_id"] == "kevin_dragons")
        self.assertIsNone(dragons["arena_deck_id"])
        self.assertEqual(dragons["attestations"][0]["deck_name"], "Dragon Love")
        self.assertEqual(dragons["attestations"][0]["arena_deck_id"], "c672cfa1-ef01-4c61-aa35-ca612329e23d")
        crimson = self.v2["members"][-1]
        self.assertEqual(crimson["measurement_inclusion"]["provenance"], "operator_attestation")
        for member in self.v1["members"]:
            self.assertEqual(member["source"]["provenance"], "historical_audit")
            self.assertEqual(member["measurement_inclusion"]["provenance"], "historical_audit")
        for note in self.v1["historical_notes"]:
            self.assertEqual(note["reproducibility"], "not reproducible: stand-in database not preserved")

    def test_crimson_payload_preserves_comparison_serialization(self):
        member = self.v2["members"][-1]
        raw = (ROOT / member["path"]).read_bytes()
        payload = json.loads(raw)
        self.assertEqual(set(payload), {"main", "sideboard", "commander", "companions"})
        self.assertEqual(raw, json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        self.assertEqual(hashlib.sha256(raw).hexdigest(), "01c9d061f9a2a6f6ded26851390c8a3e929d3cd9ba890fce297ae540fa59c7fb")
        self.assertEqual(member["canonical_identity"], "deferred")


if __name__ == "__main__":
    unittest.main()
