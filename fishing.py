"""Fishing model for Farm Solver.

Fishing consumes Large Nets, each of which catches a number of fish from a
fishing zone. A zone gives each of its items a drop rate -- the chance that any
one fish caught there is that item -- so a net that catches ``N`` fish yields
``drop_rate(item) * N`` of each item. Player perks change how many fish a single
net catches; see :class:`FishingModifiers`.

This module only models the structures; populating them from data is done
elsewhere (parsing).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field, replace
from pathlib import Path

# The bundled zone data, used unless another path is given.
DEFAULT_FISHING_CSV = Path(__file__).with_name("fishing.csv")

# How many fish a single Large Net catches before any modifiers.
LARGE_NET_BASE_CATCH = 250
# Reinforced netting raises the base catch outright.
REINFORCED_NETTING_CATCH = 400
# The fishing trawl adds a flat number of fish per net.
FISHING_TRAWL_BONUS = 100
# The sea pincher multiplies the catch.
SEA_PINCHER_MULTIPLIER = 1.1

# The Sinking Swamp is locked behind holding the Swamp Puzzle: without it the
# zone cannot be fished at all, so it is dropped outright (see
# :func:`fishing_from_config`). Its exploring counterpart is gated the same way
# -- see ``exploring.SWAMP_PUZZLE_LOCATION``.
SWAMP_PUZZLE_ZONE = "Sinking Swamp"

# A CSV row tagged with a flag in the "Flags" column is an alternate for its
# zone, used only when that flag is active instead of the zone's unflagged rows
# (see FishingZone.with_flags), like exploring's flagged tables. Throughout
# October some zones swap to this table, which adds an October-only catch
# (Lake Minerva's Purple Ghost Crab, Pirate's Cove's Mega Lamprey Shark) and
# shifts the zone's other rates. Whether it is in force is resolved by the
# caller from the ``[fishing] october_fishing`` mode (see
# ``main.resolve_october_fishing``).
OCTOBER_FLAG = "october"


@dataclass
class FishingModifiers:
    """Player perks that change how many fish a single Large Net catches.

    The base catch is :data:`LARGE_NET_BASE_CATCH`, or
    :data:`REINFORCED_NETTING_CATCH` with reinforced netting. The trawl then
    adds :data:`FISHING_TRAWL_BONUS` fish, and the sea pincher finally scales
    the total by :data:`SEA_PINCHER_MULTIPLIER` (the flat additions apply before
    the percentage).
    """

    have_reinforced_netting: bool = False
    have_fishing_trawl: bool = False
    use_sea_pincher: bool = False

    def net_catch(self) -> int:
        """Fish caught by one Large Net after applying the modifiers."""
        catch = (
            REINFORCED_NETTING_CATCH
            if self.have_reinforced_netting
            else LARGE_NET_BASE_CATCH
        )
        if self.have_fishing_trawl:
            catch += FISHING_TRAWL_BONUS
        if self.use_sea_pincher:
            catch *= SEA_PINCHER_MULTIPLIER
        return round(catch)


@dataclass
class FishingZone:
    """A fishing zone: its name and the drop rate of each item per Large Net.

    ``drop_rates`` maps the item's display name to the probability that any one
    fish caught in the zone is that item. The rates need not sum to one -- some
    of what a net catches may be nothing of interest. ``flagged_drop_rates``
    holds alternate tables read from CSV rows tagged in the ``Flags`` column,
    keyed by flag name -- each replaces ``drop_rates`` outright when that flag is
    active (see :meth:`with_flags`).
    """

    name: str
    drop_rates: dict[str, float] = field(default_factory=dict)
    flagged_drop_rates: dict[str, dict[str, float]] = field(default_factory=dict)

    def catch(self, net_catch: int) -> dict[str, float]:
        """Expected count of each item from a net catching ``net_catch`` fish."""
        return {item: rate * net_catch for item, rate in self.drop_rates.items()}

    def with_flags(self, active: set[str]) -> FishingZone:
        """A copy with ``drop_rates`` swapped for an active flag's table, if any."""
        drop_rates = self.drop_rates
        for flag in sorted(active):
            if flag in self.flagged_drop_rates:
                drop_rates = self.flagged_drop_rates[flag]
        return replace(self, drop_rates=dict(drop_rates))


@dataclass
class Fishing:
    """The fishing model: the player's modifiers and the known zones.

    Zones are keyed by their lower-cased name for case-insensitive lookup.
    """

    modifiers: FishingModifiers = field(default_factory=FishingModifiers)
    zones: dict[str, FishingZone] = field(default_factory=dict)

    def zone(self, name: str) -> FishingZone | None:
        return self.zones.get(name.lower())


def read_zones(path: Path) -> dict[str, FishingZone]:
    """Read zones from a ``Zone,Item,Drop Rate,Flags`` CSV, keyed lower-cased.

    A zone's rows are gathered into one :class:`FishingZone`; each item is keyed
    lower-cased (like the rest of the model) for case-insensitive matching. A
    row's ``Flags`` cell, if non-empty, files its rate under
    ``flagged_drop_rates[flag]`` instead of the base ``drop_rates``; a missing
    column or blank cell is the always-on case. A missing file yields no zones.
    """
    zones: dict[str, FishingZone] = {}
    if not path.exists():
        return zones
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            name = row["Zone"].strip()
            zone = zones.setdefault(name.lower(), FishingZone(name=name))
            item = row["Item"].strip().lower()
            rate = float(row["Drop Rate"])
            flag = (row.get("Flags") or "").strip().lower()
            if flag:
                zone.flagged_drop_rates.setdefault(flag, {})[item] = rate
            else:
                zone.drop_rates[item] = rate
    return zones


def load_fishing(
    path: Path | None = None, modifiers: FishingModifiers | None = None
) -> Fishing:
    """Build a :class:`Fishing` from the zone CSV and the player's modifiers."""
    return Fishing(
        modifiers=modifiers or FishingModifiers(),
        zones=read_zones(path or DEFAULT_FISHING_CSV),
    )


def fishing_from_config(
    config_section: dict,
    path: Path | None = None,
    have_swamp_puzzle: bool = True,
    october_fishing: bool = False,
) -> Fishing:
    """Build a :class:`Fishing` from a ``[fishing]`` config section.

    The section's boolean flags set the catch modifiers (see
    :class:`FishingModifiers`); zone data comes from the CSV.
    ``have_swamp_puzzle`` (resolved by the caller -- see
    ``main.resolve_swamp_puzzle``) is False when the player does not hold the
    Swamp Puzzle, which drops :data:`SWAMP_PUZZLE_ZONE` entirely: an un-unlocked
    zone cannot be fished, so it must not source anything.
    ``october_fishing`` (see ``main.resolve_october_fishing``) swaps in the
    CSV's :data:`OCTOBER_FLAG` tables (each zone's October catch).
    """
    section = config_section or {}
    modifiers = FishingModifiers(
        have_reinforced_netting=bool(section.get("have_reinforced_netting")),
        have_fishing_trawl=bool(section.get("have_fishing_trawl")),
        use_sea_pincher=bool(section.get("use_sea_pincher")),
    )
    fishing = load_fishing(path, modifiers)
    if not have_swamp_puzzle:
        fishing.zones.pop(SWAMP_PUZZLE_ZONE.lower(), None)
    if october_fishing:
        fishing.zones = {
            key: zone.with_flags({OCTOBER_FLAG})
            for key, zone in fishing.zones.items()
        }
    return fishing


def flag_only_items(
    path: Path | None = None, flag: str = OCTOBER_FLAG
) -> dict[str, dict[str, str]]:
    """``{zone: {lower-cased item: item}}`` -- what only a zone's ``flag`` table drops.

    Read from the CSV (not a built :class:`Fishing`, whose zones have already
    swapped tables), keeping each item as the CSV spells it for display; zones
    with no such item are left out.
    """
    path = path or DEFAULT_FISHING_CSV
    if not path.exists():
        return {}
    usual: dict[str, set[str]] = {}
    flagged: dict[str, dict[str, str]] = {}
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            zone, item = row["Zone"].strip(), row["Item"].strip()
            row_flag = (row.get("Flags") or "").strip().lower()
            if not row_flag:
                usual.setdefault(zone, set()).add(item.lower())
            elif row_flag == flag:
                flagged.setdefault(zone, {})[item.lower()] = item
    found = {}
    for zone, items in flagged.items():
        extra = {k: v for k, v in items.items() if k not in usual.get(zone, set())}
        if extra:
            found[zone] = extra
    return found
