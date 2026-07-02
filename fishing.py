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
from dataclasses import dataclass, field
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
    of what a net catches may be nothing of interest.
    """

    name: str
    drop_rates: dict[str, float] = field(default_factory=dict)

    def catch(self, net_catch: int) -> dict[str, float]:
        """Expected count of each item from a net catching ``net_catch`` fish."""
        return {item: rate * net_catch for item, rate in self.drop_rates.items()}


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
    """Read zones from a ``Zone,Item,Drop Rate`` CSV, keyed by lower-cased name.

    A zone's rows are gathered into one :class:`FishingZone`; each item is keyed
    lower-cased (like the rest of the model) for case-insensitive matching. A
    missing file yields no zones.
    """
    zones: dict[str, FishingZone] = {}
    if not path.exists():
        return zones
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            name = row["Zone"].strip()
            zone = zones.setdefault(name.lower(), FishingZone(name=name))
            zone.drop_rates[row["Item"].strip().lower()] = float(row["Drop Rate"])
    return zones


def load_fishing(
    path: Path | None = None, modifiers: FishingModifiers | None = None
) -> Fishing:
    """Build a :class:`Fishing` from the zone CSV and the player's modifiers."""
    return Fishing(
        modifiers=modifiers or FishingModifiers(),
        zones=read_zones(path or DEFAULT_FISHING_CSV),
    )


def fishing_from_config(config_section: dict, path: Path | None = None) -> Fishing:
    """Build a :class:`Fishing` from a ``[fishing]`` config section.

    The section's boolean flags set the catch modifiers (see
    :class:`FishingModifiers`); zone data comes from the CSV.
    """
    section = config_section or {}
    modifiers = FishingModifiers(
        have_reinforced_netting=bool(section.get("have_reinforced_netting")),
        have_fishing_trawl=bool(section.get("have_fishing_trawl")),
        use_sea_pincher=bool(section.get("use_sea_pincher")),
    )
    return load_fishing(path, modifiers)
