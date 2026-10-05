"""Read-only composition of the accepted deck-review services (#6Q).

No policy is inferred and no owner model is repaired. One read transaction
covers every database-dependent stage. The report is not an execution input.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
from time import perf_counter_ns

from mtgadb import canonical
from mtgadb.deck_identity import build_deck_snapshot_identity
from services.exporter import import_arena_deck
from services.intelligence import analyze_deck
from services.diagnosis import diagnose_analysis
from services.candidates import discover_candidates
from services.candidate_facts import derive_candidate_facts
from services.candidate_comparison import build_candidate_comparisons
from services.strategic_fit import build_strategic_fit_signals
from services.preference_policy import build_preference_policy
from services.candidate_ordering import build_candidate_ordering
from services.recommendation import build_recommendation_decisions
from services.recommendation_context import build_recommendation_context
from services.proposal_policy import build_proposal_policy_v3
from services.proposal import build_scoped_proposal
from services.proposal_presentation import build_proposal_presentation_v2, require_proposal_presentation_v2
from services.validator import DeckRules
from services.deck_review_report import REPORT_VERSION, LIMITATIONS, project, findings

_STAGES = (
    "inputs", "preference_policy", "proposal_policy", "database", "format_context",
    "import", "baseline", "analysis", "diagnosis", "candidates", "candidate_facts",
    "comparison", "strategic_fit", "ordering", "recommendation", "recommendation_context",
    "proposal", "presentation", "report",
)
_VERSIONS = {
    "analysis": ("analysis_version", "5"), "diagnosis": ("diagnosis_version", "1"),
    "candidates": ("candidate_model_version", "4"), "candidate_facts": ("candidate_facts_model_version", "4"),
    "comparison": ("candidate_comparison_model_version", "3"), "strategic_fit": ("strategic_fit_model_version", "3"),
    "preference_policy": ("strategic_preference_policy_model_version", "2"),
    "proposal_policy": ("proposal_policy_model_version", "3"),
    "ordering": ("candidate_ordering_model_version", "2"), "recommendation": ("recommendation_decision_model_version", "2"),
    "recommendation_context": ("recommendation_context_model_version", "3"),
    "proposal": ("proposal_model_version", "2"), "presentation": ("proposal_presentation_model_version", "2"),
}
_PROPOSAL_REASONS = {
    "accepted": {"validated"},
    "abstained": {"policy_context_mismatch", "recommendation_not_positive", "candidate_pool_truncated",
                  "unresolved_eligibility", "printing_cardinality_mismatch", "baseline_snapshot_mismatch",
                  "baseline_context_mismatch", "validation_failed"},
    "rejected": {"malformed_input", "unsupported_operation", "unsupported_quantity", "unsupported_resource_mode",
                 "policy_context_mismatch", "malformed_baseline", "validation_unavailable"},
}
_DECISIONS = {"recommendable", "no_candidates", "no_declared_preference", "policy_not_applicable",
              "single_candidate_no_preference", "top_tie", "indeterminate_ordering", "inconsistent_ordering",
              "unresolved_eligibility"}
_FAILURES = (ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError, RecursionError)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def read_policy(path):
    """Read a declaration once; reject duplicate keys and non-JSON numbers."""
    def nonfinite(_):
        raise ValueError("non-finite JSON number")
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"),
                       object_pairs_hook=_object, parse_constant=nonfinite)
    # Also rejects overflow such as 1e999, which parse_constant does not see.
    json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if type(value) is not dict:
        raise ValueError("policy declaration must be an object")
    return value


class _Run:
    def __init__(self, context, timings):
        self.started = perf_counter_ns() if timings else None
        self.report = {
            "deck_review_report_version": REPORT_VERSION, "scope": "read_only_integrated_deck_review",
            "run_status": "completed", "input_context": context, "baseline": None,
            "stages": [{"name": name, "state": "skipped", "artifact_ref": None,
                        "diagnostic": {"code": "dependency_unavailable", "message": "A required earlier stage did not complete."}}
                       for name in _STAGES],
            "artifacts": {}, "findings": [], "limitations": deepcopy(LIMITATIONS),
            "observations": {"clock": "perf_counter_ns", "stage_elapsed_ns": {}, "workload": {},
                             "review_elapsed_ns": None} if timings else None,
        }

    def stage(self, name):
        return next(row for row in self.report["stages"] if row["name"] == name)

    def skip(self, name, code, message):
        self.stage(name).update(state="skipped", diagnostic={"code": code, "message": message})

    def error(self, name, code, message, *, input_error=False):
        self.stage(name).update(state="error", artifact_ref=None, diagnostic={"code": code, "message": message})
        if self.report["run_status"] != "stage_error":
            self.report["run_status"] = "input_error" if input_error else "stage_error"

    def call(self, name, function, *, artifact=None, input_error=False):
        start = perf_counter_ns() if self.started is not None else None
        try:
            value = function()
            if name in _VERSIONS:
                field, version = _VERSIONS[name]
                if type(value) is not dict or value.get(field) != version:
                    raise ValueError("unsupported owner version")
            if name == "proposal" and value.get("reason") not in _PROPOSAL_REASONS.get(value.get("status"), set()):
                raise ValueError("unsupported proposal outcome")
            if name == "recommendation" and any(item["outcome"] not in _DECISIONS for item in value["decisions"]):
                raise ValueError("unsupported recommendation outcome")
            if artifact:
                # Existing CLI JSON serializes the analyzer's tuples as arrays.
                # Retain the exact owner object by value here; do not tag or
                # rewrite it. Only the typed proposal result is a projection.
                encoded = project(value) if name == "proposal" else deepcopy(value)
                json.dumps(encoded, allow_nan=False)
                self.report["artifacts"][artifact] = deepcopy(encoded)
            self.stage(name).update(state="completed", diagnostic=None,
                                    artifact_ref=f"artifacts.{artifact}" if artifact else None)
            return value
        except (OSError, UnicodeError):
            self.error(name, "input_unavailable" if input_error else "stage_unavailable",
                       "The requested input could not be read." if input_error else "The stage could not access its data.", input_error=input_error)
        except sqlite3.Error:
            self.error(name, "database_failure", "Database access failed; no retry was attempted.")
        except _FAILURES as exc:
            if name == "presentation":
                self.error(name, "presentation_verification_failed",
                           "Presentation withheld: " + str(exc) + ". Policy sources were not changed.")
            else:
                self.error(name, "invalid_input" if input_error else "owner_contract_failure",
                           str(exc) if input_error else "The owner output or required contract could not be established.", input_error=input_error)
        finally:
            if start is not None:
                self.report["observations"]["stage_elapsed_ns"][name] = perf_counter_ns() - start
        return None

    def finish(self):
        observed = None if self.stage("report")["state"] == "error" else self.call("report", lambda: findings(self.report["artifacts"]))
        if observed is not None:
            self.report["findings"] = observed
        if self.started is not None:
            artifacts = self.report["artifacts"]
            pools = artifacts.get("candidates", {}).get("pools", [])
            self.report["observations"]["workload"] = {
                "deck_copies": sum(row["total_count"] for row in artifacts.get("analysis", {}).get("zones", {}).values()),
                "need_pools": len(pools),
                "candidate_pools": [deepcopy(pool["summary"]) for pool in pools],
                "materialized_pairs": sum(len(row["pairwise_results"]) for row in artifacts.get("ordering", {}).get("need_orderings", [])),
            }
            self.report["observations"]["review_elapsed_ns"] = perf_counter_ns() - self.started
        return self.report


def _baseline(deck, con):
    zones = {}
    for name in ("main", "sideboard", "commander"):
        zones[name] = []
        for arena_id, quantity in sorted(getattr(deck, name).items()):
            row = con.execute("SELECT p.title_id,c.name,p.set_code,p.collector_number FROM printings p "
                              "JOIN cards c ON c.title_id=p.title_id WHERE p.arena_id=?", (arena_id,)).fetchone()
            if row is None:
                raise ValueError("imported printing is unavailable")
            zones[name].append({"arena_id": arena_id, "quantity": quantity, **dict(row)})
    return {"name": deck.name, "deck_id": deck.deck_id, "zones": zones,
            "gameplay_identity": build_deck_snapshot_identity(deck)}


def review_deck(deck_path, *, database_path, format_name=None, colors=None,
                preference_policy_path=None, proposal_policy_path=None,
                candidate_limit=50, observe_timings=False):
    """Read files once and return a detached Deck Review Report v1.

    Policy paths contain declarations, not previously normalized artifacts.
    None candidate_limit explicitly requests all matches. No resource inputs,
    callbacks, existing artifacts, or writable connections are accepted.
    """
    # Invalid Python API arguments must not make the error report unserializable.
    run = _Run({"format_name": format_name if type(format_name) is str else None,
                "allowed_colors": colors if type(colors) is str else None,
                "candidate_limit_per_need": candidate_limit if type(candidate_limit) is int else None, "ownership": "unknown",
                "proposal_resource_mode": "unlimited", "validation_context_projection": None}, observe_timings is True)

    def inputs():
        if type(observe_timings) is not bool or (candidate_limit is not None and
            (type(candidate_limit) is not int or candidate_limit <= 0)):
            raise ValueError("candidate limit must be a positive integer or all; timings must be boolean")
        if format_name is not None and (type(format_name) is not str or not format_name.strip()):
            raise ValueError("format name must be nonempty")
        if colors is not None and (type(colors) is not str or any(c not in "WUBRG" for c in colors) or len(set(colors)) != len(colors)):
            raise ValueError("colors must contain distinct uppercase WUBRG letters (or an explicit empty string)")
        return Path(deck_path).read_text(encoding="utf-8-sig")

    text = run.call("inputs", inputs, input_error=True)
    if text is None:
        return run.finish()
    if proposal_policy_path is None:
        run.skip("proposal", "proposal_not_requested", "No explicit proposal policy supplied.")
    policies = {}
    for name, path, builder in (("preference_policy", preference_policy_path, build_preference_policy),
                                ("proposal_policy", proposal_policy_path, build_proposal_policy_v3)):
        if path is None:
            run.skip(name, "not_supplied", "No explicit declaration supplied; none is inferred.")
        else:
            policies[name] = run.call(name, lambda p=path, b=builder: b(read_policy(p)), artifact=name, input_error=True)

    def connect():
        con = sqlite3.connect(Path(database_path).resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only=ON")
            con.execute("BEGIN")
            # Establish the snapshot now, including when no format is requested.
            con.execute("SELECT count(*) FROM cards").fetchone()
            return con
        except BaseException:
            con.close()
            raise

    con = run.call("database", connect)
    if con is None:
        return run.finish()
    try:
        allowed_colors = None if colors is None else frozenset(colors)
        format = rules = None
        if format_name is None:
            run.skip("format_context", "not_supplied", "Format eligibility is unknown; proposal construction requires a format.")
        else:
            def load_format():
                value = canonical.get_format(con, format_name)
                if value is None:
                    raise ValueError("requested format is not loaded")
                return value
            format = run.call("format_context", load_format, input_error=True)
            if format is not None:
                rules = DeckRules.from_format(format, allowed_colors=allowed_colors)
                run.report["input_context"]["validation_context_projection"] = project({"format": format, "rules": rules, "mode": "unlimited"})
        parsed = run.call("import", lambda: import_arena_deck(text, con))
        if parsed is None:
            return run.report
        run.report["artifacts"]["import_issues"] = [asdict(issue) for issue in parsed.issues]
        run.stage("import")["artifact_ref"] = "artifacts.import_issues"
        if not parsed.ok:
            run.error("import", "import_error", "Deck import failed; partial deck analysis is withheld.", input_error=True)
            run.stage("import")["artifact_ref"] = "artifacts.import_issues"
            return run.report
        deck = parsed.deck
        run.report["baseline"] = run.call("baseline", lambda: _baseline(deck, con))
        if run.report["baseline"] is None:
            return run.report
        run.stage("baseline")["artifact_ref"] = "baseline"
        analysis = run.call("analysis", lambda: analyze_deck(deck, con), artifact="analysis")
        if analysis is None:
            return run.report
        run.call("diagnosis", lambda: diagnose_analysis(analysis), artifact="diagnosis")
        if format_name is not None and format is None:
            return run.report  # Never substitute unknown context for an invalid requested format.
        candidates = run.call("candidates", lambda: discover_candidates(analysis, deck, con,
            format_name=None if format is None else format.name, allowed_colors=allowed_colors,
            limit_per_need=candidate_limit), artifact="candidates")
        if candidates is None:
            return run.report
        facts = run.call("candidate_facts", lambda: derive_candidate_facts(candidates, con, source_analysis=analysis), artifact="candidate_facts")
        if facts is None:
            return run.report
        comparison = run.call("comparison", lambda: build_candidate_comparisons(facts), artifact="comparison")
        if comparison is None:
            return run.report
        fit = run.call("strategic_fit", lambda: build_strategic_fit_signals(comparison), artifact="strategic_fit")
        if fit is None:
            return run.report
        preference = policies.get("preference_policy")
        if preference is None:
            if preference_policy_path is None:
                for name in ("ordering", "recommendation", "recommendation_context"):
                    run.skip(name, "preference_not_supplied", "Explicit preference policy not supplied; no preference is fabricated.")
            return run.report
        ordering = run.call("ordering", lambda: build_candidate_ordering(comparison, fit, preference), artifact="ordering")
        if ordering is None:
            return run.report
        decisions = run.call("recommendation", lambda: build_recommendation_decisions(ordering), artifact="recommendation")
        if decisions is None:
            return run.report
        context = run.call("recommendation_context", lambda: build_recommendation_context(decisions, comparison), artifact="recommendation_context")
        if context is None:
            return run.report
        declaration = policies.get("proposal_policy")
        if declaration is None:
            if proposal_policy_path is None:
                run.skip("proposal", "proposal_not_requested", "No explicit proposal policy supplied.")
            return run.report
        if format is None:
            run.skip("proposal", "format_not_supplied", "A concrete proposal requires explicit format context.")
            return run.report
        proposal = run.call("proposal", lambda: build_scoped_proposal(context, declaration, deck, con, format=format, rules=rules),
                            artifact="proposal_result_projection")
        if proposal is not None and proposal["status"] == "accepted":
            # Do not expose the builder result until the owner verifier succeeds.
            run.call("presentation", lambda: require_proposal_presentation_v2(build_proposal_presentation_v2(proposal)), artifact="presentation")
        elif proposal is not None:
            run.skip("presentation", "proposal_not_accepted", "The owner did not accept a proposal; no presentation is constructed.")
    except sqlite3.Error:
        run.error("database", "database_failure", "Database access failed; no retry was attempted.")
    except _FAILURES:
        run.error("report", "composition_failure", "The review composition could not be established.")
    finally:
        try:
            con.rollback()
        except sqlite3.Error:
            run.error("database", "database_cleanup_failure", "Read transaction cleanup failed.")
        finally:
            try:
                con.close()
            except sqlite3.Error:
                run.error("database", "database_cleanup_failure", "Read connection cleanup failed.")
        run.finish()
    return run.report


def exit_code(report):
    return {"completed": 0, "input_error": 2, "stage_error": 1}[report["run_status"]]
