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
from dataclasses import dataclass, field, replace
from pathlib import Path

# The bundled location data, used unless another path is given.
DEFAULT_EXPLORING_CSV = Path(__file__).with_name("exploring.csv")

# How many items a single Arnold Palmer yields before any modifiers.
ARNOLD_PALMER_BASE_YIELD = 200
# The lemon squeezer raises the base yield outright.
LEMON_SQUEEZER_YIELD = 500
# Quandary chowder multiplies the yield.
QUANDARY_CHOWDER_MULTIPLIER = 1.1

# Stamina an Apple Cider explore saves per Wanderer level (a fraction of the
# stamina one explore would otherwise cost), indexed by level 0..4.
WANDERER_STAMINA_REDUCTION = {0: 0.0, 1: 0.04, 2: 0.11, 3: 0.20, 4: 0.33}
# The Neigh meal cuts cider stamina 20%, multiplicatively with Wanderer. It is
# the only Neigh reduction -- there is no separate perk stacking on top of it.
NEIGH_STAMINA_REDUCTION = 0.20

# A CSV row tagged with a flag in the "Flags" column is an alternate for its
# location, used only when that flag is active instead of the location's
# unflagged rows (see ExploringLocation.with_flags). Holding the "Fungus
# Processing and You" artifact activates this one, swapping Gary's Crushroom's
# drop table (the Mushroom crushes into Mushroom Paste, plus rarer byproducts).
FUNGUS_PROCESSING_FLAG = "fungus_processing"

# Eating an Acorn Pie makes exploring drop Hide (in place of some of the zone's
# primary drop) for the next 450 Arnold Palmers -- or 450 Apple Ciders -- spent
# there. Unlike Fungus Processing (a permanent artifact that swaps the base
# table), the pie is a consumable held in inventory, so it is modelled not as a
# base-table swap but as *extra* offers alongside the plain ones, each burning
# 1/450 of a pie per explore (see :func:`sources.exploring_offers`). The two
# flagged tables give the pie-boosted drops without and with Fungus Processing
# also active (Hide displacing Mushroom / Mushroom Paste respectively).
ACORN_PIE_FLAG = "acorn_pie"
FUNGUS_ACORN_PIE_FLAG = "fungus_processing+acorn_pie"
# Explores one Acorn Pie covers, whether spent as Arnold Palmers or Apple Ciders.
ACORN_PIE_EXPLORES = 450

# The Sinking Swamp is locked behind holding the Swamp Puzzle: without it the
# location cannot be explored at all, so it is dropped outright rather than given
# an alternate drop table (see :func:`exploring_from_config`). Its fishing
# counterpart is gated the same way -- see ``fishing.SWAMP_PUZZLE_ZONE``.
SWAMP_PUZZLE_LOCATION = "Sinking Swamp"

# The Haunted House is a Halloween event location, open only while the event
# runs (``events.halloween_window``) and only to a player holding both of
# HAUNTED_HOUSE_ITEMS. Like the swamp it is dropped outright when unavailable
# (see :func:`exploring_from_config`); whether it is available is resolved by
# the caller from the ``[exploring] explore_haunted_house`` mode (see
# ``main.resolve_haunted_house``).
HAUNTED_HOUSE_LOCATION = "Haunted House"
HAUNTED_HOUSE_ITEMS = ("QED Pack", "Jack-o-key")


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
class CiderModifiers:
    """Player perks and meals that change how Apple Cider spends stamina.

    An Apple Cider does ``explores_per_cider`` explores (a function of the zone's
    Exploring Effectiveness, and larger with Cinnamon Sticks), each costing
    ``stamina_per_explore`` stamina -- one before reductions, cut multiplicatively
    by Wanderer and the Neigh meal. Cider always sets the zone drop rate to
    :data:`~sources.CIDER_DROP_RATE`, so what each explore finds comes from the
    zone's own drop-rate distribution.
    """

    have_cinnamon_sticks: bool = False
    wanderer_level: int = 0
    use_neigh: bool = False

    def explores_per_cider(self, effectiveness: int) -> int:
        """Explores one Apple Cider does at the given Exploring Effectiveness.

        Each point of effectiveness adds 10 explores (12.5 with Cinnamon Sticks)
        over a base of 1010 (1263 with Cinnamon Sticks) -- matching the in-game
        table (e.g. effectiveness 136 gives 2,360, or 2,950 with Cinnamon Sticks).
        """
        if self.have_cinnamon_sticks:
            # Round half up (like the in-game table), not banker's rounding.
            return int(1250 + 12.5 * effectiveness + 0.5)
        return 1000 + 10 * effectiveness

    def stamina_per_explore(self) -> float:
        """Stamina one cider explore costs after Wanderer / Neigh reductions."""
        stamina = 1.0 - WANDERER_STAMINA_REDUCTION.get(self.wanderer_level, 0.0)
        if self.use_neigh:
            stamina *= 1.0 - NEIGH_STAMINA_REDUCTION
        return stamina


@dataclass
class ExploringLocation:
    """An exploring location: its name and the drop rate of each item per palmer.

    ``drop_rates`` maps the item's display name to the probability that any one
    item found at the location is that item. The rates need not sum to one --
    some of what a palmer yields may be nothing of interest. ``flagged_drop_rates``
    holds alternate tables read from CSV rows tagged in the ``Flags`` column,
    keyed by flag name -- each replaces ``drop_rates`` outright when that flag is
    active (see :meth:`with_flags`), rather than adding to it.
    """

    name: str
    drop_rates: dict[str, float] = field(default_factory=dict)
    flagged_drop_rates: dict[str, dict[str, float]] = field(default_factory=dict)

    def explore(self, palmer_yield: int) -> dict[str, float]:
        """Expected count of each item from a palmer yielding ``palmer_yield``."""
        return {item: rate * palmer_yield for item, rate in self.drop_rates.items()}

    def with_flags(self, active: set[str]) -> ExploringLocation:
        """A copy with ``drop_rates`` swapped for an active flag's table, if any.

        Later flags in ``active`` win if more than one applies to this location
        (an arbitrary but deterministic tie-break; no location currently has more
        than one flagged table).
        """
        drop_rates = self.drop_rates
        for flag in active:
            if flag in self.flagged_drop_rates:
                drop_rates = self.flagged_drop_rates[flag]
        return replace(self, drop_rates=dict(drop_rates))


@dataclass
class Exploring:
    """The exploring model: the player's modifiers and the known locations.

    Locations are keyed by their lower-cased name for case-insensitive lookup.
    """

    modifiers: ExploringModifiers = field(default_factory=ExploringModifiers)
    locations: dict[str, ExploringLocation] = field(default_factory=dict)
    cider: CiderModifiers = field(default_factory=CiderModifiers)
    # Exploring Effectiveness per zone, keyed lower-cased -- drives how many
    # explores an Apple Cider does there (see :func:`sources.cider_offers`).
    effectiveness: dict[str, int] = field(default_factory=dict)
    # Which flagged drop table an Acorn Pie activates, given the rest of the
    # player's perks: FUNGUS_ACORN_PIE_FLAG when Fungus Processing is also active
    # (Hide displaces Mushroom Paste), else ACORN_PIE_FLAG (Hide displaces
    # Mushroom). None disables the pie channel. Consumed by the offer builders.
    acorn_flag: str | None = None

    def location(self, name: str) -> ExploringLocation | None:
        return self.locations.get(name.lower())

    def acorn_table(self, location: ExploringLocation) -> dict[str, float] | None:
        """The pie-boosted drop table for ``location``, or None if it has none.

        A location has a pie table only where the CSV tags one under the active
        :attr:`acorn_flag` (currently just Gary's Crushroom).
        """
        if self.acorn_flag is None:
            return None
        return location.flagged_drop_rates.get(self.acorn_flag)


def read_locations(path: Path) -> dict[str, ExploringLocation]:
    """Read locations from a ``Location,Item,Drop Rate,Flags`` CSV, keyed lower-cased.

    A location's rows are gathered into one :class:`ExploringLocation`; each item
    is keyed lower-cased (like the rest of the model) for case-insensitive
    matching. A row's ``Flags`` cell, if non-empty, files its rate under
    ``flagged_drop_rates[flag]`` instead of the location's base ``drop_rates`` --
    an alternate used only when that flag is active (see
    :meth:`ExploringLocation.with_flags`). A missing ``Flags`` column (or a blank
    cell) is the unflagged, always-on case. A missing file yields no locations.
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
            item = row["Item"].strip().lower()
            rate = float(row["Drop Rate"])
            flag = (row.get("Flags") or "").strip().lower()
            if flag:
                location.flagged_drop_rates.setdefault(flag, {})[item] = rate
            else:
                location.drop_rates[item] = rate
    return locations


def load_exploring(
    path: Path | None = None,
    modifiers: ExploringModifiers | None = None,
    cider: CiderModifiers | None = None,
    effectiveness: dict[str, int] | None = None,
) -> Exploring:
    """Build an :class:`Exploring` from the location CSV and the player's perks."""
    return Exploring(
        modifiers=modifiers or ExploringModifiers(),
        locations=read_locations(path or DEFAULT_EXPLORING_CSV),
        cider=cider or CiderModifiers(),
        effectiveness=effectiveness or {},
    )


def exploring_from_config(
    config_section: dict,
    path: Path | None = None,
    have_fungus_processing: bool = False,
    have_swamp_puzzle: bool = True,
    have_haunted_house: bool = False,
) -> Exploring:
    """Build an :class:`Exploring` from an ``[exploring]`` config section.

    The section's boolean flags set the Arnold Palmer yield modifiers (see
    :class:`ExploringModifiers`) and the Apple Cider perks/meals (see
    :class:`CiderModifiers`); its ``[exploring.effectiveness]`` subtable maps each
    zone to its Exploring Effectiveness. Location drop rates come from the CSV.
    ``have_fungus_processing`` (resolved by the caller -- see
    ``main.resolve_fungus_processing``) activates the CSV's
    :data:`FUNGUS_PROCESSING_FLAG` rows, swapping Gary's Crushroom's drop table.
    ``have_swamp_puzzle`` (likewise resolved by the caller -- see
    ``main.resolve_swamp_puzzle``) is False when the player does not hold the
    Swamp Puzzle, which drops :data:`SWAMP_PUZZLE_LOCATION` entirely: an
    un-unlocked location cannot be explored, so it must not source anything.
    ``have_haunted_house`` (see ``main.resolve_haunted_house``) likewise keeps
    the :data:`HAUNTED_HOUSE_LOCATION` event location, dropped by default.
    """
    section = config_section or {}
    modifiers = ExploringModifiers(
        have_lemon_squeezer=bool(section.get("have_lemon_squeezer")),
        use_quandary_chowder=bool(section.get("use_quandary_chowder")),
    )
    cider = CiderModifiers(
        have_cinnamon_sticks=bool(section.get("have_cinnamon_sticks")),
        wanderer_level=int(section.get("wanderer_level", 0)),
        use_neigh=bool(section.get("use_neigh")),
    )
    effectiveness = {
        zone.lower(): int(value)
        for zone, value in (section.get("effectiveness") or {}).items()
    }
    exploring = load_exploring(path, modifiers, cider, effectiveness)
    if not have_swamp_puzzle:
        exploring.locations.pop(SWAMP_PUZZLE_LOCATION.lower(), None)
    if not have_haunted_house:
        exploring.locations.pop(HAUNTED_HOUSE_LOCATION.lower(), None)
    active_flags = {FUNGUS_PROCESSING_FLAG} if have_fungus_processing else set()
    if active_flags:
        exploring.locations = {
            key: location.with_flags(active_flags)
            for key, location in exploring.locations.items()
        }
    # An Acorn Pie boosts whichever base table is in force, so which flagged pie
    # table applies depends on Fungus Processing (with_flags preserves both).
    exploring.acorn_flag = (
        FUNGUS_ACORN_PIE_FLAG if have_fungus_processing else ACORN_PIE_FLAG
    )
    return exploring
