"""Exploring model for Farm Solver.

Exploring consumes Arnold Palmers, each of which explores a location and yields a
number of items from it. A location gives each of its items a drop rate -- the
chance that any one item found there is that item -- so a palmer yielding ``N``
items gives ``drop_rate(item) * N`` of each. Player perks change how many items a
single Arnold Palmer yields; see :class:`ExploringModifiers`.

This is the exploring counterpart of :mod:`fishing` (Large Nets), and models the
structures only; populating them from data is done elsewhere (parsing).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

# The bundled location data, used unless another path is given.
DEFAULT_EXPLORING_CSV = Path(__file__).with_name("exploring.csv")

# How many items a single Arnold Palmer yields before any modifiers.
ARNOLD_PALMER_BASE_YIELD = 200
# The lemon squeezer raises the base yield outright.
LEMON_SQUEEZER_YIELD = 500
# Quandary chowder multiplies the yield.
QUANDARY_CHOWDER_MULTIPLIER = 1.1


@dataclass
class ExploringModifiers:
    """Player perks that change how many items a single Arnold Palmer yields.

    The base yield is :data:`ARNOLD_PALMER_BASE_YIELD`, or
    :data:`LEMON_SQUEEZER_YIELD` with the lemon squeezer. Quandary chowder then
    scales the total by :data:`QUANDARY_CHOWDER_MULTIPLIER` (so with the lemon
    squeezer 500 becomes 550).
    """

    have_lemon_squeezer: bool = False
    use_quandary_chowder: bool = False

    def palmer_yield(self) -> int:
        """Items yielded by one Arnold Palmer after applying the modifiers."""
        items = (
            LEMON_SQUEEZER_YIELD
            if self.have_lemon_squeezer
            else ARNOLD_PALMER_BASE_YIELD
        )
        if self.use_quandary_chowder:
            items *= QUANDARY_CHOWDER_MULTIPLIER
        return round(items)


@dataclass
class ExploringLocation:
    """An exploring location: its name and the drop rate of each item per palmer.

    ``drop_rates`` maps the item's display name to the probability that any one
    item found at the location is that item. The rates need not sum to one --
    some of what a palmer yields may be nothing of interest.
    """

    name: str
    drop_rates: dict[str, float] = field(default_factory=dict)

    def explore(self, palmer_yield: int) -> dict[str, float]:
        """Expected count of each item from a palmer yielding ``palmer_yield``."""
        return {item: rate * palmer_yield for item, rate in self.drop_rates.items()}


@dataclass
class Exploring:
    """The exploring model: the player's modifiers and the known locations.

    Locations are keyed by their lower-cased name for case-insensitive lookup.
    """

    modifiers: ExploringModifiers = field(default_factory=ExploringModifiers)
    locations: dict[str, ExploringLocation] = field(default_factory=dict)

    def location(self, name: str) -> ExploringLocation | None:
        return self.locations.get(name.lower())


def read_locations(path: Path) -> dict[str, ExploringLocation]:
    """Read locations from a ``Location,Item,Drop Rate`` CSV, keyed lower-cased.

    A location's rows are gathered into one :class:`ExploringLocation`; each item
    is keyed lower-cased (like the rest of the model) for case-insensitive
    matching. A missing file yields no locations.
    """
    locations: dict[str, ExploringLocation] = {}
    if not path.exists():
        return locations
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            name = row["Location"].strip()
            location = locations.setdefault(
                name.lower(), ExploringLocation(name=name)
            )
            location.drop_rates[row["Item"].strip().lower()] = float(row["Drop Rate"])
    return locations


def load_exploring(
    path: Path | None = None, modifiers: ExploringModifiers | None = None
) -> Exploring:
    """Build an :class:`Exploring` from the location CSV and the player's perks."""
    return Exploring(
        modifiers=modifiers or ExploringModifiers(),
        locations=read_locations(path or DEFAULT_EXPLORING_CSV),
    )


def exploring_from_config(
    config_section: dict, path: Path | None = None
) -> Exploring:
    """Build an :class:`Exploring` from an ``[exploring]`` config section.

    The section's boolean flags set the yield modifiers (see
    :class:`ExploringModifiers`); location data comes from the CSV.
    """
    section = config_section or {}
    modifiers = ExploringModifiers(
        have_lemon_squeezer=bool(section.get("have_lemon_squeezer")),
        use_quandary_chowder=bool(section.get("use_quandary_chowder")),
    )
    return load_exploring(path, modifiers)
