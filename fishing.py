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

from dataclasses import dataclass, field

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
