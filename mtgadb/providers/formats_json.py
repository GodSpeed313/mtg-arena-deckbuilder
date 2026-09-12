"""Arena format definitions extracted from a Player.log payload."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mtgadb.model import Diagnostics, Format, ProviderResult, Status


_KNOWN_FIELDS = frozenset({
    "name", "FormatTypeInternal", "mainDeckQuota", "sideBoardQuota",
    "commandZoneQuota", "legalSets", "filterSets", "bannedTitleIds",
    "allowedTitleIds", "supressedTitleIds", "suspendedTitleIds",
    "individualCardQuotas", "AllowedCommanderTitleIds",
    "RarityPerCardQuotasInternal", "useRebalancedCards",
    "CardCountRestrictionInternal", "SideboardBehaviorInternal",
    "RestrictToColorIdentityInternal",
})


def _ids(value: Any) -> frozenset[int]:
    return frozenset(int(v) for v in (value or []))


def _quota_map(value: Any, *, allow_empty: bool = False) -> dict[int, int | None]:
    if not value:
        return {}
    if not isinstance(value, dict):
        raise ValueError("quota map must be an object")
    result: dict[int, int | None] = {}
    for raw_key, raw_quota in value.items():
        if raw_quota == {} and allow_empty:
            result[int(raw_key)] = None
            continue
        if not isinstance(raw_quota, dict) or "max" not in raw_quota:
            raise ValueError(f"quota {raw_key!r} must contain integer max")
        result[int(raw_key)] = int(raw_quota["max"])
    return result


def _color_restrictions(value: Any) -> tuple[frozenset[int], ...]:
    if not value:
        return ()
    return tuple(frozenset(int(c) for c in item.get("Colors", [])) for item in value)


def parse_format(raw: dict[str, Any]) -> Format:
    """Translate explicit fields without interpreting Arena's numeric enums."""
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("format is missing a non-empty name")
    main = raw.get("mainDeckQuota") or {}
    side = raw.get("sideBoardQuota") or {}
    command = raw.get("commandZoneQuota") or {}
    rebalance = raw.get("useRebalancedCards") if "useRebalancedCards" in raw else None
    return Format(
        name=name,
        legal_sets=frozenset(str(v) for v in raw.get("legalSets", [])),
        filter_sets=frozenset(str(v) for v in raw.get("filterSets", [])),
        banned_title_ids=_ids(raw.get("bannedTitleIds")),
        allowed_title_ids=(
            _ids(raw["allowedTitleIds"]) if "allowedTitleIds" in raw else None
        ),
        suppressed_title_ids=_ids(raw.get("supressedTitleIds")),
        suspended_title_ids=_ids(raw.get("suspendedTitleIds")),
        allowed_commander_title_ids=(
            _ids(raw["AllowedCommanderTitleIds"])
            if "AllowedCommanderTitleIds" in raw else None
        ),
        individual_card_quotas=_quota_map(raw.get("individualCardQuotas")),
        rarity_card_quotas=_quota_map(
            raw.get("RarityPerCardQuotasInternal"), allow_empty=True
        ),
        min_deck_size=int(main.get("min", 0)),
        max_deck_size=int(main.get("max", 250)),
        max_sideboard=int(side.get("max", 0)),
        min_command_zone=int(command.get("min", 0)),
        max_command_zone=int(command.get("max", 0)),
        uses_rebalanced_cards=(bool(rebalance) if rebalance is not None else None),
        format_type_internal=raw.get("FormatTypeInternal"),
        card_count_restriction_internal=raw.get("CardCountRestrictionInternal"),
        sideboard_behavior_internal=raw.get("SideboardBehaviorInternal"),
        color_restrictions_internal=_color_restrictions(
            raw.get("RestrictToColorIdentityInternal")
        ),
    )


class JSONFormatProvider:
    """Load a Formats array extracted from Arena without reading the whole log."""

    def __init__(self, path: Path):
        self.path = path

    def get_formats(self) -> ProviderResult[dict[str, Format]]:
        evidence: dict[str, Any] = {"path": str(self.path)}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if not isinstance(raw, list):
                raise ValueError("top level must be the Formats array")
            evidence["objects"] = len(raw)
            formats: dict[str, Format] = {}
            unknown: set[str] = set()
            for item in raw:
                if not isinstance(item, dict):
                    raise ValueError("every format entry must be an object")
                unknown.update(set(item) - _KNOWN_FIELDS)
                parsed = parse_format(item)
                if parsed.name in formats:
                    raise ValueError(f"duplicate format name: {parsed.name}")
                formats[parsed.name] = parsed
            evidence["unknown_fields"] = sorted(unknown)
            return ProviderResult(
                formats,
                Diagnostics(str(self.path), Status.OK, evidence),
            )
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            evidence["error"] = str(exc)
            return ProviderResult(
                None,
                Diagnostics(
                    str(self.path), Status.ERROR, evidence,
                    ["Extract a fresh Formats array from the newest Arena Player.log."],
                ),
            )
