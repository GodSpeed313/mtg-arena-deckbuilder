"""End-to-end #6Q controls. Normal paths never inject intermediate artifacts."""
from contextlib import closing, ExitStack, redirect_stdout, redirect_stderr
from copy import deepcopy
from dataclasses import replace
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from mtgadb import canonical
from mtgadb.model import Card, CardPrinting, Deck, Format
from services import deck_review as review
from services.deck_review_report import project, render_json, render_text, findings
from services.proposal_presentation import require_proposal_presentation
from services.validator import DeckRules
import workbench

FIXTURE = Path(__file__).parent / "fixtures" / "deck_review_v1.json"


class DeckReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "cards.db"
        self.deck = self.root / "deck.txt"
        self.pref = self.root / "preference.json"
        self.prop = self.root / "proposal.json"
        self.fx = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.format = Format("Fixture", legal_sets=frozenset({"FIX"}), max_command_zone=1)
        with closing(sqlite3.connect(self.db)) as con:
            con.executescript(canonical.SCHEMA)
            canonical.load_cards(con, [Card(**row) for row in self.fx["cards"]])
            canonical.load_printings(con, [CardPrinting(row["title_id"] * 100 + 1,
                row["title_id"], "FIX", str(row["title_id"]), "common") for row in self.fx["cards"]])
            canonical.load_formats(con, {"Fixture": self.format})
            con.commit()
        self.write(self.pref, self.fx["preference"])
        self.write(self.prop, self.fx["proposal"])
        self.theme("life")

    def write(self, path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def theme(self, name):
        self.deck.write_text(self.fx["main_base"] + self.fx["themes"][name], encoding="utf-8")

    def sql(self, sql, params=()):
        with closing(sqlite3.connect(self.db)) as con:
            con.execute(sql, params)
            con.commit()

    def run_review(self, **kwargs):
        options = dict(database_path=self.db, format_name="Fixture",
                       preference_policy_path=self.pref, proposal_policy_path=self.prop)
        options.update(kwargs)
        return review.review_deck(self.deck, **options)

    def stage(self, report, name):
        return next(row for row in report["stages"] if row["name"] == name)

    def outcomes(self, report):
        return [row["outcome"] for row in report["artifacts"].get("recommendation", {}).get("decisions", [])]

    def reason(self, report):
        return report["artifacts"]["proposal_result_projection"]["reason"]

    def test_positive_deck_text_to_verified_presentation(self):
        report = self.run_review()
        self.assertEqual(report["run_status"], "completed", render_text(report))
        self.assertEqual(self.outcomes(report), ["recommendable"])
        self.assertEqual(self.reason(report), "validated")
        presentation = report["artifacts"]["presentation"]
        self.assertEqual(require_proposal_presentation(presentation), presentation)
        proposed = report["artifacts"]["proposal_result_projection"]["proposal"]
        self.assertEqual(proposed["delta"]["arena_id"], 201)
        self.assertEqual(proposed["proposed_deck"]["report_type"], "Deck")
        self.assertEqual(review.exit_code(report), 0)

    def test_token_family_positive(self):
        self.theme("token")
        spec = deepcopy(self.fx["proposal"])
        spec["need_key"].update(finding_id="need.creature_token_entry.enabler.v2", dependency_id="dependency.creature_token_entry.v2")
        self.write(self.prop, spec)
        report = self.run_review()
        self.assertEqual(self.outcomes(report), ["recommendable"])
        self.assertIn("presentation", report["artifacts"], render_text(report))

    def test_counter_family_positive(self):
        self.theme("counter")
        spec = deepcopy(self.fx["proposal"])
        spec["need_key"].update(finding_id="need.plus1_counters.enabler.v2", dependency_id="dependency.plus1_counters.v2")
        self.write(self.prop, spec)
        report = self.run_review()
        self.assertEqual(self.outcomes(report), ["recommendable"])
        self.assertIn("presentation", report["artifacts"], render_text(report))

    def test_no_supported_need_is_completed_review(self):
        self.theme("no_need")
        report = self.run_review(proposal_policy_path=None)
        self.assertEqual(report["run_status"], "completed")
        self.assertEqual(report["artifacts"]["candidates"]["pools"], [])
        self.assertIn("no_supported_need", [row["code"] for row in report["findings"]])

    def test_need_with_zero_candidates_preserves_exclusion_counts(self):
        for title in (2, 3, 12):
            self.sql("INSERT INTO format_title_rules VALUES ('Fixture',?,'banned')", (title,))
        report = self.run_review()
        self.assertEqual(self.outcomes(report), ["no_candidates"])
        self.assertEqual(report["artifacts"]["candidates"]["pools"][0]["summary"]["excluded"], 3)
        self.assertEqual(self.reason(report), "recommendation_not_positive")
        self.assertEqual(review.exit_code(report), 0)

    def test_tie_does_not_use_name_or_id(self):
        self.sql("UPDATE cards SET cmc=1 WHERE title_id IN (3,12)")
        report = self.run_review()
        self.assertEqual(self.outcomes(report), ["top_tie"])
        self.assertNotIn("presentation", report["artifacts"])

    def test_single_candidate_not_promoted(self):
        report = self.run_review(candidate_limit=1)
        self.assertEqual(self.outcomes(report), ["single_candidate_no_preference"])
        self.assertTrue(report["artifacts"]["candidates"]["pools"][0]["summary"]["truncated"])

    def test_missing_preference_not_fabricated(self):
        with patch.object(review, "build_candidate_ordering", side_effect=AssertionError("must not order")):
            report = self.run_review(preference_policy_path=None)
        self.assertIn("strategic_fit", report["artifacts"])
        self.assertNotIn("preference_policy", report["artifacts"])
        self.assertEqual(self.stage(report, "ordering")["diagnostic"]["code"], "preference_not_supplied")

    def test_explicit_empty_preference_preserved(self):
        spec = deepcopy(self.fx["preference"]); spec["rules"] = []
        self.write(self.pref, spec)
        self.assertEqual(self.outcomes(self.run_review()), ["no_declared_preference"])

    def test_out_of_scope_preference(self):
        spec = deepcopy(self.fx["preference"]); spec["scope"]["zones"] = ["sideboard"]
        self.write(self.pref, spec)
        self.assertEqual(self.outcomes(self.run_review()), ["policy_not_applicable"])

    def test_unsupported_rules_remain_visible(self):
        self.theme("unsupported")
        report = self.run_review(proposal_policy_path=None)
        self.assertEqual(report["run_status"], "completed")
        self.assertIn("limited_rules_coverage", [row["code"] for row in report["findings"]])
        self.assertIn("Unreviewed Engine", render_json(report))

    def test_missing_format_unresolved_not_legal(self):
        report = self.run_review(format_name=None)
        self.assertEqual(self.outcomes(report), ["unresolved_eligibility"])
        self.assertEqual(self.stage(report, "proposal")["diagnostic"]["code"], "format_not_supplied")

    def test_truncated_recommendation_cannot_become_proposal(self):
        report = self.run_review(candidate_limit=2)
        self.assertEqual(self.outcomes(report), ["recommendable"])
        self.assertEqual(self.reason(report), "candidate_pool_truncated")
        self.assertNotIn("presentation", report["artifacts"])
        self.assertIn("returned pool only", render_text(report))

    def test_explicit_all_returns_complete_pool(self):
        report = self.run_review(candidate_limit=None)
        self.assertFalse(report["artifacts"]["candidates"]["pools"][0]["summary"]["truncated"])
        self.assertIn("presentation", report["artifacts"])
        self.assertIn("all (explicit)", render_text(report))

    def test_multiple_printings_abstains(self):
        self.sql("INSERT INTO printings(arena_id,title_id,set_code,collector_number,rarity) VALUES (202,2,'FIX','alt','common')")
        report = self.run_review()
        self.assertEqual(self.reason(report), "printing_cardinality_mismatch")
        self.assertNotIn("presentation", report["artifacts"])

    def test_exact_size_add_one_fails(self):
        self.sql("UPDATE formats SET max_deck_size=60")
        report = self.run_review()
        self.assertEqual(self.reason(report), "validation_failed")
        self.assertIn("main_size", render_text(report))
        self.assertNotIn("presentation", report["artifacts"])

    def test_absent_need_not_reselected(self):
        spec = deepcopy(self.fx["proposal"]); spec["need_key"]["finding_id"] = "absent"
        self.write(self.prop, spec)
        self.assertEqual(self.reason(self.run_review()), "policy_context_mismatch")

    def test_invalid_proposal_policy_does_not_destroy_independent_analysis(self):
        spec = deepcopy(self.fx["proposal"]); spec["quantity"] = 2
        self.write(self.prop, spec)
        report = self.run_review()
        self.assertEqual(report["run_status"], "input_error")
        self.assertEqual(self.outcomes(report), ["recommendable"])
        self.assertEqual(self.stage(report, "proposal_policy")["state"], "error")
        self.assertNotIn("proposal_result_projection", report["artifacts"])

    def test_provenance_mismatch_withholds_presentation_without_rewrite(self):
        spec = deepcopy(self.fx["proposal"]); spec["policy_source"]["provenance"]["reference"] = "different declaration"
        self.write(self.prop, spec)
        report = self.run_review()
        self.assertEqual(self.reason(report), "validated")
        self.assertEqual(self.stage(report, "presentation")["state"], "error")
        self.assertEqual(review.exit_code(report), 1)
        self.assertNotIn("presentation", report["artifacts"])
        self.assertEqual(report["artifacts"]["proposal_policy"]["policy_source"], spec["policy_source"])
        self.assertEqual(report["artifacts"]["preference_policy"]["policy_source"], self.fx["preference"]["policy_source"])

    def test_failed_import_withholds_partial_deck(self):
        self.deck.write_text("Deck\n1 Plains\n1 Missing Card", encoding="utf-8")
        report = self.run_review()
        self.assertIsNone(report["baseline"])
        self.assertNotIn("analysis", report["artifacts"])
        self.assertEqual(review.exit_code(report), 2)
        self.assertEqual(report["artifacts"]["import_issues"][0]["code"], "unknown_card")

    def test_unknown_printing_is_import_error(self):
        self.deck.write_text("1 Life Payoff (FIX) 999", encoding="utf-8")
        report = self.run_review()
        self.assertEqual(report["artifacts"]["import_issues"][0]["code"], "unknown_printing")

    def test_unknown_requested_format_not_silently_omitted(self):
        report = self.run_review(format_name="not loaded")
        self.assertEqual(review.exit_code(report), 2)
        self.assertIn("analysis", report["artifacts"])
        self.assertNotIn("candidates", report["artifacts"])

    def test_duplicate_keys_nonfinite_and_wrong_shape_policies(self):
        for text in ('{"rules":[],"rules":[]}', '{"nested":{"a":1,"a":2}}',
                     '{"value":NaN}', '{"value":Infinity}', '{"value":1e999}', '[]', '{'):
            with self.subTest(text=text):
                self.pref.write_text(text, encoding="utf-8")
                report = self.run_review()
                self.assertEqual(self.stage(report, "preference_policy")["state"], "error")
                self.assertNotIn("ordering", report["artifacts"])
                render_json(report)

    def test_invalid_limits_colors_and_bool_quantities_rejected(self):
        for options in ({"candidate_limit": True}, {"candidate_limit": 0}, {"candidate_limit": 1.5}, {"colors": "WW"}, {"colors": "blue"}):
            self.assertEqual(review.exit_code(self.run_review(**options)), 2)
        spec = deepcopy(self.fx["proposal"]); spec["quantity"] = True
        self.write(self.prop, spec)
        self.assertEqual(review.exit_code(self.run_review()), 2)

    def test_input_and_database_failures_no_creation(self):
        missing = self.root / "missing.db"
        report = self.run_review(database_path=missing)
        self.assertEqual(review.exit_code(report), 1)
        self.assertFalse(missing.exists())
        self.deck.unlink()
        self.assertEqual(review.exit_code(self.run_review()), 2)

    def test_stage_failure_blocks_dependents_but_retains_analysis(self):
        with patch.object(review, "discover_candidates", side_effect=sqlite3.OperationalError("private detail")):
            report = self.run_review()
        self.assertEqual(review.exit_code(report), 1)
        self.assertIn("analysis", report["artifacts"])
        self.assertNotIn("private detail", render_json(report))
        self.assertEqual(self.stage(report, "candidate_facts")["state"], "skipped")

    def test_unsupported_owner_version_is_not_promoted(self):
        with patch.object(review, "analyze_deck", return_value={"analysis_version": "999"}):
            report = self.run_review()
        self.assertEqual(review.exit_code(report), 1)
        self.assertNotIn("analysis", report["artifacts"])

    def test_presentation_verifier_called_and_failure_withheld(self):
        with patch.object(review, "require_proposal_presentation", side_effect=ValueError("bad identity")) as verify:
            report = self.run_review()
        verify.assert_called_once()
        self.assertNotIn("presentation", report["artifacts"])
        self.assertEqual(self.stage(report, "presentation")["state"], "error")

    def test_exact_owner_artifacts_and_inputs_preserved(self):
        captured = {}
        names = {"analyze_deck": "analysis", "diagnose_analysis": "diagnosis", "discover_candidates": "candidates",
                 "derive_candidate_facts": "candidate_facts", "build_candidate_comparisons": "comparison",
                 "build_strategic_fit_signals": "strategic_fit", "build_preference_policy": "preference_policy",
                 "build_proposal_policy": "proposal_policy", "build_candidate_ordering": "ordering",
                 "build_recommendation_decisions": "recommendation", "build_recommendation_context": "recommendation_context",
                 "build_proposal": "proposal_result_projection", "require_proposal_presentation": "presentation"}
        with ExitStack() as stack:
            for function, artifact in names.items():
                original = getattr(review, function)
                def capture(*args, _original=original, _artifact=artifact, **kwargs):
                    result = _original(*args, **kwargs)
                    captured[_artifact] = deepcopy(result)
                    return result
                stack.enter_context(patch.object(review, function, side_effect=capture))
            report = self.run_review()
        self.assertIn("presentation", captured)
        for key, value in captured.items():
            expected = project(value) if key == "proposal_result_projection" else value
            self.assertEqual(report["artifacts"][key], expected, key)
            self.assertEqual(json.loads(render_json(report))["artifacts"][key], json.loads(json.dumps(expected)), key)
        report["artifacts"]["analysis"]["zones"].clear()
        self.assertTrue(captured["analysis"]["zones"])

    def test_default_determinism_and_timing_semantics(self):
        first, second = self.run_review(), self.run_review()
        self.assertEqual(render_json(first), render_json(second))
        timed = self.run_review(observe_timings=True)
        observations = timed.pop("observations")
        first.pop("observations")
        self.assertEqual(timed, first)
        self.assertGreater(observations["review_elapsed_ns"], 0)
        self.assertEqual(observations["workload"]["materialized_pairs"], 3)

    def test_default_limit_truncates_large_pool_without_relaxing_proposal(self):
        with closing(sqlite3.connect(self.db)) as con:
            cards = [Card(i, f"Extra Life {i}", types="Sorcery", cmc=i,
                          rules_text="You gain 1 life.") for i in range(100, 150)]
            canonical.load_cards(con, cards)
            canonical.load_printings(con, [CardPrinting(c.title_id * 100 + 1, c.title_id,
                "FIX", str(c.title_id), "common") for c in cards])
            con.commit()
        report = self.run_review(observe_timings=True)
        summary = report["artifacts"]["candidates"]["pools"][0]["summary"]
        self.assertEqual((summary["included"], summary["returned"], summary["truncated"]), (53, 50, True))
        self.assertEqual(self.reason(report), "candidate_pool_truncated")
        self.assertEqual(report["observations"]["workload"]["materialized_pairs"], 1225)
        self.assertIn("Further pairs omitted", render_text(report))

    def test_files_are_read_once_and_report_detached_from_later_edits(self):
        original = Path.read_text
        counts = {}
        def read(path, *args, **kwargs):
            counts[path] = counts.get(path, 0) + 1
            return original(path, *args, **kwargs)
        with patch.object(Path, "read_text", read):
            report = self.run_review()
        self.assertEqual(counts, {self.deck: 1, self.pref: 1, self.prop: 1})
        before = render_json(report)
        self.pref.write_text('{}', encoding="utf-8")
        self.deck.write_text('changed', encoding="utf-8")
        self.assertEqual(before, render_json(report))

    def test_cleanup_failure_is_not_success_or_overwritten_by_report_stage(self):
        original = sqlite3.connect
        class CleanupFailure(sqlite3.Connection):
            def rollback(self):
                super().rollback()
                raise sqlite3.OperationalError("cleanup failed")
        with patch.object(review.sqlite3, "connect", side_effect=lambda *a, **kw: original(*a, factory=CleanupFailure, **kw)):
            report = self.run_review()
        self.assertEqual(review.exit_code(report), 1)
        self.assertEqual(self.stage(report, "database")["diagnostic"]["code"], "database_cleanup_failure")

    def test_report_failure_remains_explicit(self):
        with patch.object(review, "findings", side_effect=ValueError("projection failed")):
            report = self.run_review()
        self.assertEqual(self.stage(report, "report")["state"], "error")
        self.assertEqual(review.exit_code(report), 1)

    def test_strict_policy_unknown_fields_utf8_and_missing_file(self):
        for content in (b'\xff', b'{"policy_id":"\\ud800"}', b'{"unexpected":true}'):
            self.pref.write_bytes(content)
            report = self.run_review()
            self.assertEqual(review.exit_code(report), 2)
            self.assertNotIn("ordering", report["artifacts"])
            render_json(report)
        self.pref.unlink()
        self.assertEqual(review.exit_code(self.run_review()), 2)

    def test_unserializable_python_input_is_rejected_with_renderable_error(self):
        for kwargs in ({"candidate_limit": float('inf')}, {"colors": object()}, {"format_name": object()}):
            report = self.run_review(**kwargs)
            self.assertEqual(review.exit_code(report), 2)
            render_json(report)
            self.assertIn("invalid inputs", render_text(report))

    def test_read_only_single_connection_and_no_account_reads(self):
        original = sqlite3.connect
        connections, statements = [], []
        def connect(*args, **kwargs):
            self.assertIn("?mode=ro", args[0])
            con = original(*args, **kwargs)
            connections.append(con)
            con.set_trace_callback(statements.append)
            return con
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        with patch.object(review.sqlite3, "connect", side_effect=connect):
            report = self.run_review()
        self.assertEqual(report["run_status"], "completed")
        self.assertEqual(len(connections), 1)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        sql = "\n".join(statements).lower()
        for forbidden in ("insert ", "update ", "delete ", "ownership", "wildcards", "inventory", "managed_"):
            self.assertNotIn(forbidden, sql)
        self.assertEqual(statements.count("BEGIN"), 1)
        self.assertEqual(statements.count("ROLLBACK"), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            connections[0].execute("SELECT 1")

    def test_wal_writer_cannot_change_review_snapshot(self):
        self.sql("PRAGMA journal_mode=WAL")
        original = review.analyze_deck
        def concurrent(deck, con):
            result = original(deck, con)
            self.sql("UPDATE cards SET cmc=9 WHERE title_id=2")
            return result
        with patch.object(review, "analyze_deck", side_effect=concurrent):
            old = self.run_review()
        new = self.run_review()
        self.assertEqual(old["artifacts"]["proposal_result_projection"]["proposal"]["delta"]["arena_id"], 201)
        self.assertEqual(new["artifacts"]["proposal_result_projection"]["proposal"]["delta"]["arena_id"], 301)

    def test_rollback_journal_writer_cannot_commit_during_review(self):
        original = review.analyze_deck
        def concurrent(deck, con):
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("UPDATE cards SET cmc=99")
            with closing(sqlite3.connect(self.db, timeout=0)) as writer:
                writer.execute("UPDATE cards SET cmc=9 WHERE title_id=2")
                with self.assertRaises(sqlite3.OperationalError):
                    writer.commit()
                writer.rollback()
            return original(deck, con)
        with patch.object(review, "analyze_deck", side_effect=concurrent):
            report = self.run_review()
        self.assertIn("presentation", report["artifacts"])

    def test_forbidden_capabilities_never_invoked(self):
        forbidden = [
            "services.human_proposal_decision.build_human_proposal_decision",
            "services.local_deck_application_intent.build_local_deck_application_intent",
            "services.local_deck_application.apply_local_deck_application",
            "services.local_deck_application_outcome.prepare_local_deck_application",
            "services.local_deck_application_outcome.execute_prepared_local_deck_application",
            "services.local_deck_application_outcome.recover_local_deck_application",
            "services.local_execution_validation.LocalExecutionValidationAuthorityV1.publish",
            "mtgadb.managed_deck_store._connection", "mtgadb.snapshot_store.save_snapshot",
            "mtgadb.canonical.open_writable", "mtgadb.canonical.load_ownership",
            "mtgadb.canonical.load_inventory", "mtgadb.query.CardQueryEngine.owned_count",
            "services.exporter.export_arena_deck", "workbench._collection", "workbench._wildcards",
        ]
        with ExitStack() as stack:
            for name in forbidden:
                stack.enter_context(patch(name, side_effect=AssertionError(name)))
            self.assertIn("presentation", self.run_review()["artifacts"])

    def test_zone_preservation_and_explicit_target(self):
        self.deck.write_text(self.fx["main_base"] + self.fx["themes"]["no_need"] +
                             "Sideboard\n1 Life Payoff (FIX) 1\nCommander\n1 Unreviewed Engine (FIX) 11", encoding="utf-8")
        spec = deepcopy(self.fx["proposal"]); spec["need_key"]["zone"] = "sideboard"
        self.write(self.prop, spec)
        report = self.run_review()
        self.assertEqual(report["artifacts"]["proposal_result_projection"]["proposal"]["delta"]["zone"], "main")
        self.assertEqual(len(report["baseline"]["zones"]["commander"]), 1)

    def test_projection_preserves_format_maps_sets_null_and_unicode(self):
        format = replace(self.format, name="Fó", rarity_card_quotas={1: None},
                         individual_card_quotas={2: 1}, color_restrictions_internal=(frozenset({1, 2}),))
        value = project({"format": format, "deck": Deck(name="Déck", main={201: 1}), "rules": DeckRules(allowed_colors=frozenset("WU"))})
        fields = value["format"]["fields"]
        self.assertEqual(fields["rarity_card_quotas"]["entries"], [[1, None]])
        self.assertEqual(fields["color_restrictions_internal"]["report_type"], "tuple")
        self.assertEqual(json.loads(json.dumps(value)), value)
        for invalid in (object(), float("nan"), float("inf")):
            with self.assertRaises(ValueError): project(invalid)

    def test_cli_json_text_timings_and_default_limit(self):
        base = ["--database", str(self.db), "review-deck", str(self.deck), "--format", "Fixture",
                "--preference-policy", str(self.pref), "--proposal-policy", str(self.prop)]
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = workbench.main(base + ["--json", "--timings"])
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["input_context"]["candidate_limit_per_need"], 50)
        self.assertGreater(json.loads(errors.getvalue())["render_elapsed_ns"], 0)
        output = io.StringIO()
        with redirect_stdout(output): code = workbench.main(base + ["--candidate-limit", "all"])
        self.assertEqual(code, 0)
        self.assertIn("Verified detached", output.getvalue())
        self.assertIn("all (explicit)", output.getvalue())

    def test_outcome_explanations_include_all_closed_decisions(self):
        # Adapter-only coverage for outcomes not naturally produced by fixtures.
        for outcome in review._DECISIONS:
            result = findings({"recommendation": {"decisions": [{"outcome": outcome}]}})
            self.assertEqual(result[0]["code"], outcome)
        for status, reasons in review._PROPOSAL_REASONS.items():
            for reason in reasons:
                result = findings({"proposal_result_projection": {"status": status, "reason": reason, "validation": None}})
                self.assertEqual(result[0]["code"], reason)


if __name__ == "__main__":
    unittest.main()
