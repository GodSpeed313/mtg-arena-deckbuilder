"""Core data model.

Two-level card identity, per DESIGN.md section 3. The split is not stylistic:
ownership in Arena is per *printing* (grpId), while the four-copy deck limit
and every ban list are per *card* (titleId). A one-level model gets deck
legality wrong, independent of any data-quality concern.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Generic, TypeVar

T = TypeVar("T")


# --------------------------------------------------------------- identity


class Resolution(str, Enum):
    """How a card's canonical rules text was chosen among its printings."""

    MAJORITY = "majority"
    SELF_REFERENCE = "self_reference"
    SINGLE = "single"  # only one printing; nothing to reconcile
    ANOMALY = "anomaly"  # printings disagree and no rule settled it


@dataclass(frozen=True)
class CardPrinting:
    """One Arena grpId. This is the thing you own."""

    arena_id: int
    title_id: int
    set_code: str = ""
    collector_number: str = ""
    rarity: str = ""
    raw_rules_text: str = ""


@dataclass(frozen=True)
class Card:
    """One distinct card. This is the thing you build decks with."""

    title_id: int
    name: str
    mana_cost: str = ""
    cmc: int = 0
    types: str = ""
    subtypes: str = ""
    colors: str = ""
    color_identity: str = ""
    power: str = ""
    toughness: str = ""
    rules_text: str = ""
    source_printing_id: int | None = None
    resolution: Resolution = Resolution.SINGLE
    anomaly_count: int = 0

    def __str__(self) -> str:
        pt = f" {self.power}/{self.toughness}" if self.power else ""
        cost = f" {self.mana_cost}" if self.mana_cost else ""
        return f"{self.name}{cost} - {self.types}{pt}"


# ------------------------------------------------------------- collection


@dataclass(frozen=True)
class Collection:
    """Owned quantities, keyed by printing because that is how Arena counts."""

    cards: dict[int, int] = field(default_factory=dict)
    source: str = ""
    observed_at: datetime | None = None

    def total_copies(self) -> int:
        return sum(self.cards.values())


@dataclass(frozen=True)
class Inventory:
    """Craft budget: wildcards and currency."""

    wildcards: dict[str, int] = field(default_factory=dict)
    gold: int = 0
    gems: int = 0
    vault_progress: int = 0


@dataclass(frozen=True)
class Deck:
    """A decklist. Zones are kept separate; quantities are per printing."""

    deck_id: str = ""
    name: str = ""
    main: dict[int, int] = field(default_factory=dict)
    sideboard: dict[int, int] = field(default_factory=dict)
    commander: dict[int, int] = field(default_factory=dict)

    def size(self) -> int:
        return sum(self.main.values())


@dataclass(frozen=True)
class Format:
    """A constructed format's legality rules, as Arena states them."""

    name: str
    legal_sets: frozenset[str] = frozenset()
    filter_sets: frozenset[str] = frozenset()
    banned_title_ids: frozenset[int] = frozenset()
    # Explicit legality exceptions for titles outside legal_sets; not an
    # exclusive allowlist (Timeless has only a handful of these).
    allowed_title_ids: frozenset[int] | None = None
    suppressed_title_ids: frozenset[int] = frozenset()
    suspended_title_ids: frozenset[int] = frozenset()
    allowed_commander_title_ids: frozenset[int] | None = None
    individual_card_quotas: dict[int, int] = field(default_factory=dict)
    rarity_card_quotas: dict[int, int | None] = field(default_factory=dict)
    min_deck_size: int = 60
    max_deck_size: int = 250
    max_sideboard: int = 15
    min_command_zone: int = 0
    max_command_zone: int = 0
    uses_rebalanced_cards: bool | None = None
    format_type_internal: int | None = None
    card_count_restriction_internal: int | None = None
    sideboard_behavior_internal: int | None = None
    color_restrictions_internal: tuple[frozenset[int], ...] = ()


# ------------------------------------------------------------ diagnostics


class Status(str, Enum):
    OK = "ok"
    NOT_FOUND = "not_found"  # source worked, data absent
    UNSUPPORTED = "unsupported"  # this source cannot supply this capability
    ERROR = "error"  # source failed


@dataclass(frozen=True)
class Diagnostics:
    """Why a provider returned what it did.

    Wizards will break us eventually. When that happens the output should be a
    report -- what was searched, what was found, what to try next -- rather
    than a stack trace. Every provider returns this whether it succeeded or
    not, so failure paths are as inspectable as success paths.
    """

    source: str
    status: Status
    evidence: dict[str, Any] = field(default_factory=dict)
    next_steps: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"source : {self.source}", f"status : {self.status.value}"]
        for k, v in self.evidence.items():
            lines.append(f"  {k:.<24} {v}")
        for step in self.next_steps:
            lines.append(f"  -> {step}")
        return "\n".join(lines)


@dataclass(frozen=True)
class ProviderResult(Generic[T]):
    """Data plus the story of how it was obtained. `data` is None unless OK."""

    data: T | None
    diagnostics: Diagnostics

    @property
    def ok(self) -> bool:
        return self.diagnostics.status is Status.OK and self.data is not None
