"""Unified production offers for Farm Solver.

The solver can obtain an item several ways -- passive production, fishing,
exploring, and farming -- and many of those produce *several* items at once:
one Large Net at a zone catches a whole bundle of fish, one Arnold Palmer at a
location yields a bundle of finds, one harvest of a crop gives its primary crop
plus rarer drops. To let the optimiser credit a single action against *every*
need it happens to satisfy, each way of producing items is expressed as an
:class:`Offer` carrying its full bundle.

An offer performed once yields ``yields`` (a bundle of items), costs ``ap``
Arnold Palmers and ``ln`` Large Nets, and takes ``time_per_unit`` on a named time
``track``. Scaling to ``N`` units scales the bundle and every cost linearly (see
:class:`Draw`). Joint production *within* one offer is handled by driving it at
the ``units`` its most-demanded wanted item needs -- that one action covers the
rest of the bundle for free.

Time combines **sum within a track** and **max across tracks**: farming grows one
crop at a time so crop offers on the ``farming`` track add up, while farming and
each passive producer run concurrently so the whole plan's time is the longest
track (see :func:`aggregate`). Fishing and exploring are effectively instant, so
their tracks cost no time -- they trade Arnold Palmers and Large Nets for speed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from crops import Farm
from exploring import Exploring
from fishing import Fishing
from locksmith import Locksmith
from passive import PassiveProduction

# Time-track names. Crops share one track (grown one at a time, so their times
# add up); fishing and exploring are instant, so their tracks never cost time.
# All passive buildings (and pets) share one track: they run concurrently with
# each other, which one combined offer captures as the max over its items (see
# :func:`passive_offers`).
FARMING_TRACK = "farming"
FISHING_TRACK = "fishing"
EXPLORING_TRACK = "exploring"
PASSIVE_TRACK = "passive"


@dataclass
class Offer:
    """One joint-production activity and what performing it once costs and yields.

    ``yields`` maps each lower-cased item the activity produces to how many one
    unit of it yields (the bundle). ``ap`` and ``ln`` are the Arnold Palmers and
    Large Nets one unit consumes, ``time_per_unit`` the time one unit takes on
    its ``track``. Everything scales linearly with the number of units used.
    """

    name: str
    track: str
    yields: dict[str, float] = field(default_factory=dict)
    ap: float = 0.0
    ln: float = 0.0
    time_per_unit: timedelta = timedelta(0)

    def yield_of(self, item: str) -> float:
        """How many of ``item`` one unit yields (0 if the bundle lacks it)."""
        return self.yields.get(item.lower(), 0.0)

    def units_for(self, needs: dict[str, int]) -> float:
        """Units so every wanted item this offer yields is covered.

        Only items the bundle actually produces count; the binding one (needing
        the most units) drives the total, and the rest of the bundle comes along
        for free. Returns 0 when the offer yields none of ``needs``.
        """
        units = 0.0
        for item, quantity in needs.items():
            rate = self.yield_of(item)
            if rate:
                units = max(units, quantity / rate)
        return units


@dataclass
class Draw:
    """An offer used at a chosen scale: the whole bundle it makes, and the cost."""

    offer: Offer
    units: float

    @property
    def produced(self) -> dict[str, float]:
        """The full bundle this draw yields (every item, not just wanted ones)."""
        return {item: rate * self.units for item, rate in self.offer.yields.items()}

    @property
    def ap(self) -> float:
        return self.offer.ap * self.units

    @property
    def ln(self) -> float:
        return self.offer.ln * self.units

    @property
    def time(self) -> timedelta:
        return self.offer.time_per_unit * self.units


@dataclass
class OpenFlow:
    """Opening one openable: what it consumes and the average bundle it yields.

    Opening a chest or bag at the Locksmith consumes one of the openable itself
    (and a key, for chests) and adds its *average* contents to the inventory, at
    no time, Arnold Palmer or Large Net cost of its own -- the cost is having
    gathered the openable and key in the first place. So the optimiser opens an
    openable whenever acquiring-and-opening its contents beats gathering them
    directly, and chains naturally (a chest that yields a bag, a bag that yields
    keys). Unlike a craft, one open yields a whole *bundle* at once, so it is its
    own multi-output flow rather than a recipe (see :mod:`optimizer`).

    ``consumes`` and ``yields`` both map a lower-cased item to how many one open
    uses or produces.
    """

    name: str
    consumes: dict[str, float] = field(default_factory=dict)
    yields: dict[str, float] = field(default_factory=dict)


def aggregate(draws: list[Draw]) -> tuple[timedelta, float, float]:
    """Combine draws into a total ``(time, arnold_palmers, large_nets)``.

    Times add up within a track and the plan's time is the longest track, since
    tracks run concurrently. Consumables simply sum across every draw.
    """
    track_time: dict[str, timedelta] = {}
    arnold_palmers = large_nets = 0.0
    for draw in draws:
        track_time[draw.offer.track] = (
            track_time.get(draw.offer.track, timedelta(0)) + draw.time
        )
        arnold_palmers += draw.ap
        large_nets += draw.ln
    total_time = max(track_time.values(), default=timedelta(0))
    return total_time, arnold_palmers, large_nets


def fishing_offers(fishing: Fishing) -> list[Offer]:
    """An offer per fishing zone: one Large Net catches the zone's bundle."""
    catch = fishing.modifiers.net_catch()
    return [
        Offer(
            name=f"fish {zone.name}",
            track=FISHING_TRACK,
            yields=zone.catch(catch),
            ln=1,
        )
        for zone in fishing.zones.values()
    ]


def exploring_offers(exploring: Exploring) -> list[Offer]:
    """An offer per location: one Arnold Palmer yields the location's bundle."""
    palmer_yield = exploring.modifiers.palmer_yield()
    return [
        Offer(
            name=f"explore {location.name}",
            track=EXPLORING_TRACK,
            yields=location.explore(palmer_yield),
            ap=1,
        )
        for location in exploring.locations.values()
    ]


def passive_offers(production: PassiveProduction) -> list[Offer]:
    """One offer covering all passive production (buildings *and* pets).

    Every passive building runs concurrently, so the whole economy is modelled as
    a single offer whose bundle is each item's combined per-minute rate (summed
    across producers, storehouse-capped -- see :meth:`PassiveProduction.
    rate_per_minute`). One "unit" is a minute of the economy running; driving the
    offer by the binding item (via :meth:`Offer.units_for`) makes its time the
    longest single item's wait, which is exactly the concurrency we want: waiting
    for the slowest item also yields all the faster ones for free.

    Pets are included as ordinary (slow) producers rather than split out: their
    low rates make any item that *only* a pet supplies expensive in time, so the
    optimiser leaves pets as a last resort on its own, without a hand-coded rule.

    Compromise: unlike :meth:`PassiveProduction.time_to_produce`, which rounds a
    single producer's wait up to a whole cycle (you cannot collect part of a
    ten-minute deposit), this uses a continuous per-minute rate and never rounds.
    The optimiser needs smooth costs to compare channels, the error is at most one
    cycle, and it matches what ``time_to_produce`` already does whenever several
    producers share an item. Final reporting can round to whole cycles again.
    """
    items: set[str] = set()
    for producer in production.producers:
        items |= set(producer.outputs)
    if not items:
        return []
    yields = {item: production.rate_per_minute(item) for item in items}
    return [
        Offer(
            name="passive",
            track=PASSIVE_TRACK,
            yields=yields,
            time_per_unit=timedelta(minutes=1),
        )
    ]


def farming_offers(farm: Farm) -> list[Offer]:
    """An offer per crop: one full-field planting yields the crop's drop bundle.

    ``time_per_unit`` is the effective time for one full-field planting, which
    folds in the daily Grape Juice allowance the same way :meth:`Farm.time_to_grow`
    does: a day yields its natural harvests plus the instant grows, so one
    planting effectively takes ``day / harvests_per_day``.
    """
    day = timedelta(days=1)
    offers: list[Offer] = []
    for crop in farm.crops.values():
        grow_time = farm.grow_time(crop.name)
        if grow_time is None:
            continue
        harvests_per_day = day / grow_time + farm.grape_juices_per_day
        bundle = {item: farm.per_harvest(crop.name, item) for item in crop.drops}
        offers.append(
            Offer(
                name=f"grow {crop.name}",
                track=FARMING_TRACK,
                yields=bundle,
                time_per_unit=day / harvests_per_day,
            )
        )
    return offers


def open_flows(locksmith: Locksmith) -> list[OpenFlow]:
    """A flow per openable: consume it (and its key) to yield its average bundle.

    An openable with no contents (nothing to yield) is skipped. Chests also
    consume one key of their tier; bags consume only themselves.
    """
    flows: list[OpenFlow] = []
    for openable in locksmith.openables.values():
        yields = openable.average_output()
        if not yields:
            continue
        consumes = {openable.name.lower(): 1.0}
        if openable.key is not None:
            key = openable.key.lower()
            consumes[key] = consumes.get(key, 0.0) + 1.0
        flows.append(OpenFlow(name=openable.name, consumes=consumes, yields=yields))
    return flows
