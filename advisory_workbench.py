"""Optional fake-only advisory CLI. Existing workbench commands are unchanged."""
import argparse
import json
import sys
from pathlib import Path

from services.advisory import AdvisoryCoordinator, FakeProvider
from services.advisory_contract import AdvisoryError, export_evidence, make_request
from services.advisory_store import AdvisoryStore
from services import advisory_location
from services.advisory_operations import error_code


def display(value):
    # JSON escapes terminal control characters; never print arbitrary exceptions.
    from services.advisory_contract import encoded, safe_bytes
    safe_bytes(encoded(value))
    print(json.dumps(value, indent=2, ensure_ascii=True))


def confirm(phrase):
    if not sys.stdin.isatty():
        raise AdvisoryError("confirmation_required")
    if input("Type " + phrase + ": ") != phrase:
        raise AdvisoryError("confirmation_required")


def management(argv):
    parser = argparse.ArgumentParser(description="Local advisory management; never dispatches a provider.")
    groups = parser.add_subparsers(dest="group", required=True)
    loc = groups.add_parser("location").add_subparsers(dest="action", required=True)
    for name in ("inspect", "attest"):
        loc.add_parser(name).add_argument("--cache-root", required=True)
    loc.add_parser("revoke").add_argument("--attestation-id", required=True)
    cache = groups.add_parser("cache").add_subparsers(dest="action", required=True)
    for name in ("status", "show", "attempts", "verdict", "delete-review"):
        command = cache.add_parser(name)
        command.add_argument("--key", required=True)
        if name == "verdict":
            command.add_argument("--value", choices=("accepted", "rejected"), required=True)
    args = parser.parse_args(argv)
    if args.group == "location":
        if args.action == "revoke":
            preview = advisory_location.revocation_preview(args.attestation_id)
            display(preview)
            confirm("REVOKE " + args.attestation_id + " " + preview["confirmation_digest"])
            advisory_location.revoke(args.attestation_id, confirmation=preview["confirmation_digest"])
            display({"state": "revoked"})
        else:
            preview = advisory_location.inspect(args.cache_root)
            display(preview)
            if args.action == "attest":
                confirm("ATTEST " + preview["snapshot"]["cache_path"] + " " + preview["confirmation_digest"])
                display(advisory_location.attest(args.cache_root, confirmation=preview["confirmation_digest"]))
        return 0
    store = AdvisoryStore()
    if args.action == "status":
        display(store.inspection(args.key))
    elif args.action == "show":
        display(store.show(args.key))
    elif args.action == "attempts":
        display({"attempts": store.attempts(args.key)})
    else:
        preview = store.mutation_preview(args.key)
        display(preview)
        if args.action == "verdict":
            display(store.show(args.key))  # Must validate before asking for verdict.
            confirm(args.value.upper() + " " + args.key + " " + preview["confirmation_digest"])
            store.confirm_verdict(args.key, args.value, confirmation=preview["confirmation_digest"])
        else:
            confirm("DELETE REVIEW " + args.key + " " + preview["confirmation_digest"])
            store.delete_review(args.key, confirmation=preview["confirmation_digest"])
        display({"state": "completed", "operation": args.action})
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("location", "cache"):
        try:
            return management(argv)
        except (Exception, KeyboardInterrupt) as error:
            display({"operational_error_version": "1", "code": error_code(error), "automatic_retry": False})
            return 2
    parser = argparse.ArgumentParser(description="Offline advisory foundation: fake provider only, no paid calls.")
    parser.add_argument("deck", type=Path)
    parser.add_argument("--database", type=Path, default=Path("current.db"))
    parser.add_argument("--fake-response", type=Path, help="Local synthetic provider-response JSON")
    parser.add_argument("--review", action="store_true", help="Preview and ask for explicit consent on a miss")
    args = parser.parse_args(argv)
    try:
        request = make_request(export_evidence(args.deck.read_text(encoding="utf-8-sig"), args.database))
        provider = FakeProvider(args.fake_response.read_bytes()) if args.fake_response else None
        coordinator = AdvisoryCoordinator(AdvisoryStore(), provider)
        preview = coordinator.preview(request)
        display(preview)
        if not args.review:
            return 0
        consent = False
        acknowledgment = False
        if not preview["cache_hit"]:
            if provider is None:
                print("Unavailable: real provider is disabled in Phases 0–2.")
                return 2
            consent = input("Run this fake review at the displayed $0.00 cost? Type yes: ") == "yes"
            if preview["previous_uncertain_or_failed_attempts"]:
                acknowledgment = input("Earlier outcome may be charged. Authorize a new attempt? Type yes: ") == "yes"
        result = coordinator.run(request, preview=preview, authorize=consent,
                                 acknowledge_uncertainty=acknowledgment)
        display(result)
        return 0 if result["state"] in ("cached_valid", "fresh_valid") else 2
    except (Exception, KeyboardInterrupt) as error:
        display({"operational_error_version": "1", "code": error_code(error), "automatic_retry": False})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
