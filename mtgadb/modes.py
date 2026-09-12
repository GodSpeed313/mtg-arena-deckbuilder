"""Operating modes for building against ownership and craft constraints."""

from __future__ import annotations

from enum import Enum


class OperatingMode(str, Enum):
    """How strictly a deck is constrained by the player's collection."""

    FULL_COLLECTION = "full_collection"
    WILDCARD_BUDGET = "wildcard_budget"
    UNLIMITED = "unlimited"

