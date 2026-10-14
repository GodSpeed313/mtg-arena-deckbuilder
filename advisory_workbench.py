"""Optional fake-only advisory CLI. Existing workbench commands are unchanged."""
import argparse
import json
from pathlib import Path

from services.advisory import AdvisoryCoordinator, FakeProvider
from services.advisory_contract import AdvisoryError, export_evidence, make_request
from services.advisory_store import AdvisoryStore


def main(argv=None):
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
        print(json.dumps(preview, indent=2))
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
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["state"] in ("cached_valid", "fresh_valid") else 2
    except (Exception, KeyboardInterrupt):
        print("Advisory unavailable: input, storage, or location safety could not be established.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
