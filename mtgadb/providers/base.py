"""Provider capability interfaces.

Interfaces describe *what the app needs*; implementations describe *where it
came from*. Adding a new source must never require touching a consumer.

These are deliberately five separate capabilities rather than one "provider".
Player.log supplies inventory, decks and formats but demonstrably not
ownership -- a single monolithic interface would force it to report total
failure at something it does three-quarters of well.
"""

from __future__ import annotations

from typing import Iterable, Protocol, runtime_checkable

from mtgadb.model import (
    Card,
    CardPrinting,
    Collection,
    Deck,
    Format,
    Inventory,
    ProviderResult,
)


@runtime_checkable
class CardDatabaseProvider(Protocol):
    """Card definitions: the printings and the cards they roll up into."""

    def iter_printings(self) -> Iterable[CardPrinting]: ...

    def iter_cards(self) -> Iterable[Card]: ...

    def fingerprint(self) -> str:
        """Stable id for the underlying data, so we can detect Arena updates
        and rebuild the projection only when it actually changed."""
        ...


@runtime_checkable
class OwnershipProvider(Protocol):
    """Which cards the player owns, and how many."""

    def get_collection(self) -> ProviderResult[Collection]: ...


@runtime_checkable
class InventoryProvider(Protocol):
    """Wildcards and currency -- the craft budget."""

    def get_inventory(self) -> ProviderResult[Inventory]: ...


@runtime_checkable
class DeckProvider(Protocol):
    """Decks already saved in the client."""

    def get_decks(self) -> ProviderResult[list[Deck]]: ...


@runtime_checkable
class FormatProvider(Protocol):
    """Format legality: legal sets, bans, allow-lists, deck quotas."""

    def get_formats(self) -> ProviderResult[dict[str, Format]]: ...
