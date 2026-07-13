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

from crops import PLOTS_PER_ROW, Farm
from exploring import ACORN_PIE_EXPLORES, Exploring
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
# Topping up stamina by drinking Orange Juice or eating an Apple is instant, like
# fishing/exploring, so this track never costs time (see :func:`stamina_offers`).
DRINKING_TRACK = "drinking"

# Stamina restored by consuming one of each item, for the Apple Cider channel.
ORANGE_JUICE_STAMINA = 100.0
APPLE_STAMINA = 15.0

# The drop rate Apple Cider forces in every zone: 0.40 items discovered per
# explore (one stamina). The zone's own drop-rate distribution then says which
# item each discovery is (see :func:`cider_offers`).
CIDER_DROP_RATE = 0.40

# The consumable an Acorn Pie explore burns, and how much of one each explore
# costs (one pie covers ACORN_PIE_EXPLORES explores; see :mod:`exploring`).
ACORN_PIE = "acorn pie"
ACORN_PIE_PER_EXPLORE = 1.0 / ACORN_PIE_EXPLORES

# The Hickory Omelette: a held/bought meal that lasts one hour and boosts the
# sawmill's output by HICKORY_OMELETTE_FRACTION while active. The boost is a
# grounded activity on its own concurrent track (see :func:`sawmill_boost_offers`
# and :data:`config.HICKORY_MODES`): one unit is one boosted hour, yielding that
# fraction of the sawmill's hourly output and eating one omelette.
HICKORY_OMELETTE = "hickory omelette"
HICKORY_OMELETTE_FRACTION = 0.2
# The boost runs concurrently with the base sawmill (same wall clock), so it gets
# its own track: a unit costs one hour there, and the LP can run it for at most
# the makespan-worth of hours (past that it would become the binding track and
# only lengthen the plan), which bounds the omelettes to the plan's span.
SAWMILL_BOOST_TRACK = "sawmill boost"


@dataclass
class Offer:
    """One joint-production activity and what performing it once costs and yields.

    ``yields`` maps each lower-cased item the activity produces to how many one
    unit of it yields (the bundle). ``consumes`` maps each lower-cased item one
    unit *uses up* to how many (an input the plan must itself source -- e.g. in
    the inventory plan mode an exploring unit consumes one Arnold Palmer, a cider
    explore consumes stamina and part of an Apple Cider); it is empty in the
    unlimited mode, where consumables are the abstract costs below instead.
    ``ap``, ``ln``, ``cookies`` and ``gj`` are the Arnold Palmers, Large Nets,
    cookie sets and Grape Juices one unit costs (the priced/reported tally),
    ``time_per_unit`` the time one unit takes on its ``track``. Everything scales
    linearly with the number of units used.
    """

    name: str
    track: str
    yields: dict[str, float] = field(default_factory=dict)
    consumes: dict[str, float] = field(default_factory=dict)
    ap: float = 0.0
    ln: float = 0.0
    cookies: float = 0.0
    gj: float = 0.0
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
    def cookies(self) -> float:
        return self.offer.cookies * self.units

    @property
    def gj(self) -> float:
        return self.offer.gj * self.units

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


def fishing_offers(fishing: Fishing, ground: bool = False) -> list[Offer]:
    """An offer per fishing zone: one Large Net catches the zone's bundle.

    With ``ground`` (the inventory plan mode) each net is also a real item the
    plan must craft or hold (``consumes``), grounding fishing in passive
    stone/wood/straw/antlers; otherwise the Large Net is an abstract cost.
    """
    catch = fishing.modifiers.net_catch()
    return [
        Offer(
            name=f"fish {zone.name}",
            track=FISHING_TRACK,
            yields=zone.catch(catch),
            consumes={"large net": 1.0} if ground else {},
            ln=1,
        )
        for zone in fishing.zones.values()
    ]


def exploring_offers(exploring: Exploring, ground: bool = False) -> list[Offer]:
    """An offer per location: one Arnold Palmer yields the location's bundle.

    With ``ground`` (the inventory plan mode) each Arnold Palmer is also a real
    item the plan must craft or hold (``consumes``), grounding exploring in the
    Lemonade/Iced Tea chain; otherwise the Arnold Palmer is an abstract cost.

    A location with an Acorn Pie table (see :meth:`Exploring.acorn_table`) also
    gets a pie-boosted variant that drops Hide: same Arnold Palmer cost plus
    ``1/450`` of a held Acorn Pie per explore, so the LP takes it only when it
    wants the Hide and only up to the pies on hand.
    """
    palmer_yield = exploring.modifiers.palmer_yield()
    offers: list[Offer] = []
    for location in exploring.locations.values():
        offers.append(
            Offer(
                name=f"explore {location.name}",
                track=EXPLORING_TRACK,
                yields=location.explore(palmer_yield),
                consumes={"arnold palmer": 1.0} if ground else {},
                ap=1,
            )
        )
        acorn_table = exploring.acorn_table(location)
        if acorn_table:
            consumes = {ACORN_PIE: ACORN_PIE_PER_EXPLORE}
            if ground:
                consumes["arnold palmer"] = 1.0
            offers.append(
                Offer(
                    name=f"explore {location.name} with acorn pie",
                    track=EXPLORING_TRACK,
                    yields={item: rate * palmer_yield for item, rate in acorn_table.items()},
                    consumes=consumes,
                    ap=1,
                )
            )
    return offers


def cider_offers(exploring: Exploring) -> list[Offer]:
    """An Apple Cider exploring offer per zone that has an Effectiveness set.

    One unit is a single *explore* (one stamina before reductions): it finds
    :data:`CIDER_DROP_RATE` items on average, split across the zone's items by
    their drop rates, and spends ``stamina_per_explore`` stamina and ``1/E`` of an
    Apple Cider (``E`` = explores one cider does here). Driving the offer in
    explores keeps stamina, cider count and yield all linear in the usage level.
    Zones with no configured Exploring Effectiveness are skipped.
    """
    stamina = exploring.cider.stamina_per_explore()
    offers: list[Offer] = []
    for location in exploring.locations.values():
        effectiveness = exploring.effectiveness.get(location.name.lower())
        if not effectiveness:
            continue
        per_cider = exploring.cider.explores_per_cider(effectiveness)
        if per_cider <= 0:
            continue
        cider_per_explore = 1.0 / per_cider
        offers.append(
            Offer(
                name=f"cider {location.name}",
                track=EXPLORING_TRACK,
                yields={
                    item: CIDER_DROP_RATE * rate
                    for item, rate in location.drop_rates.items()
                },
                consumes={"stamina": stamina, "apple cider": cider_per_explore},
            )
        )
        acorn_table = exploring.acorn_table(location)
        if acorn_table:
            # One pie covers 450 Apple Ciders, i.e. 450*per_cider explores, so a
            # single explore burns cider_per_explore/450 of a pie.
            offers.append(
                Offer(
                    name=f"cider {location.name} with acorn pie",
                    track=EXPLORING_TRACK,
                    yields={
                        item: CIDER_DROP_RATE * rate
                        for item, rate in acorn_table.items()
                    },
                    consumes={
                        "stamina": stamina,
                        "apple cider": cider_per_explore,
                        ACORN_PIE: cider_per_explore * ACORN_PIE_PER_EXPLORE,
                    },
                )
            )
    return offers


def stamina_offers() -> list[Offer]:
    """The instant item-to-stamina flows that fuel Apple Cider explores.

    Two ways to top up stamina at no time cost: drinking one Orange Juice yields
    100 stamina, eating one Apple yields 15. Each consumes a real item the plan
    must source itself -- Orange Juice crafted from Orchard Oranges, Apples from
    the orchard (or thrown off as a cider-explore byproduct) -- so stamina is
    bounded by those, not free. Modelled as offers that consume the item and yield
    stamina; the LP picks whichever is cheaper at the margin.
    """
    return [
        Offer(
            name="drink orange juice",
            track=DRINKING_TRACK,
            yields={"stamina": ORANGE_JUICE_STAMINA},
            consumes={"orange juice": 1.0},
        ),
        Offer(
            name="eat apple",
            track=DRINKING_TRACK,
            yields={"stamina": APPLE_STAMINA},
            consumes={"apple": 1.0},
        ),
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

    Compromise: production really arrives in whole deposits (you cannot collect
    part of a ten-minute cycle), but this offer uses a continuous per-minute rate
    and never rounds -- the optimiser needs smooth costs to compare channels, and
    the error is at most one cycle per item. The rounding happens *after* the LP
    instead: plan times, the frontier's breakpoint caps and every report wait are
    lifted to the real deposit schedule via
    :meth:`PassiveProduction.quantized_wait` (see
    :func:`optimizer._quantize_plan_time`, :func:`report._passive_wait`), so the
    smooth rate is purely the LP's internal view.
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


def sawmill_boost_offers(
    outputs: dict[str, float], mode: str
) -> list[Offer]:
    """The Hickory Omelette sawmill boost, as a grounded offer (or none).

    ``outputs`` are the sawmill's base hourly outputs (see
    :func:`config.sawmill_outputs`); ``mode`` is one of :data:`config.HICKORY_MODES`.

    One unit is a single boosted **hour**: it yields :data:`HICKORY_OMELETTE_FRACTION`
    of the boosted outputs and eats one :data:`HICKORY_OMELETTE` (which the plan
    must hold or buy). It lives on its own :data:`SAWMILL_BOOST_TRACK`, so it runs
    concurrently with the base sawmill yet still costs an hour of makespan per
    omelette -- which stops the LP boosting for more hours than the plan actually
    spans (past that the boost track becomes the binding one, only lengthening the
    plan). At the fast end that recovers the same +20% throughput as baking the
    boost into the sawmill's rate; nearer the cheap end the LP can boost fewer
    hours, trading omelette gold against makespan like any other consumable.

    * ``"all"``      boosts every output (oak, board, wood).
    * ``"only_oak"`` boosts oak alone -- board and wood are left out entirely, so
      an omelette is never spent to source them (they are so much more plentiful
      per hour than oak that the boost's only real value is oak anyway).
    * ``"none"``     produces no offer.
    """
    if mode == "none":
        return []
    boosted = dict(outputs)
    if mode == "only_oak":
        boosted = {"oak": outputs["oak"]} if "oak" in outputs else {}
    if not boosted:
        return []
    return [
        Offer(
            name="hickory omelette boost",
            track=SAWMILL_BOOST_TRACK,
            yields={item: HICKORY_OMELETTE_FRACTION * qty for item, qty in boosted.items()},
            consumes={HICKORY_OMELETTE: 1.0},
            time_per_unit=timedelta(hours=1),
        )
    ]


def farming_offers(farm: Farm, ground: bool = False) -> list[Offer]:
    """Offers for growing each crop, trading farming time against Grape Juice.

    Each crop that grows Grape Juice gets two ways to farm it so the optimiser can
    choose whether to spend the scarce daily allowance:

    * ``grow X`` folds the Grape Juice in the way :meth:`Farm.time_to_grow` does --
      a day yields its natural harvests plus the instant grows, so one planting
      effectively takes ``day / harvests_per_day`` and costs its share
      ``grape_juices / harvests_per_day`` of a Grape Juice. It is the faster way.
    * ``grow X without grape juice`` waits out natural growth alone: one planting
      takes the full grow time and costs no Grape Juice. It is the slower way.

    A near-instant crop grows faster than a wait, so it never gets Grape Juice
    (:meth:`Farm.grape_juices_for` is zero) -- just the one plain ``grow X`` at its
    grow time.

    A qualifying crop (slow enough -- see :meth:`Farm.takes_cookies`) also gets a
    cookie-day offer: one unit is a single day of farming that crop under a full
    cookie set, whose five-minute window boosts the harvests taken in it --
    ninefold (:data:`crops.COOKIE_SET_MULTIPLIER`) on the timed harvest and the
    day's Grape Juice grows (see :meth:`Farm.cookie_day_yield`). It takes a real
    day on the farming track and costs one cookie set and that day's Grape Juice.
    Modelling a whole day (natural harvests included) rather than only the boost
    keeps it a standalone action, needing no coupling back to the plain offers.

    With the Scythe of Dewstar (``farm.dewstar``), every harvest also throws in
    bonus crops of other, no-slower items (:meth:`Farm.dewstar_bonus`), folded into
    the same bundles; the cookie-day bundle instead uses
    :meth:`Farm.dewstar_cookie_bonus`, which only carries the Cookie multiplier
    onto the bonus crops when Dewstar's Journal is held.

    A seed-limited crop's (a Mega crop's or Spring's) grounded offers also consume
    its seeds (:meth:`crops.Crop.seed_item` -- a full field per planting, one seed
    per plot: ``PLOTS_PER_ROW * rows``), so the LP is bounded by the seeds held in
    inventory: a limited seed cannot be readily replaced, and with none held the
    offer is unusable. A normal crop's seeds are freely purchasable, so they are
    not modelled.
    """
    day = timedelta(days=1)
    offers: list[Offer] = []
    for crop in farm.crops.values():
        grow_time = farm.grow_time(crop.name)
        if grow_time is None:
            continue
        juices = farm.grape_juices_for(crop.name)
        harvests_per_day = day / grow_time + juices
        bundle = {item: farm.per_harvest(crop.name, item) for item in crop.drops}
        for item, rate in farm.dewstar_bonus(crop.name).items():
            bundle[item] = bundle.get(item, 0.0) + rate
        gj_per_unit = juices / (day / grow_time + juices)
        # One unit of a plain grow offer is one full-field planting, sowing a
        # seed in every plot -- a limited-seed crop's spend per unit.
        seed = crop.seed_item if ground else None
        seeds_per_planting = float(PLOTS_PER_ROW * farm.rows)
        consumes: dict[str, float] = {}
        if ground and gj_per_unit:
            consumes["grape juice"] = gj_per_unit
        if seed:
            consumes[seed] = seeds_per_planting
        offers.append(
            Offer(
                name=f"grow {crop.name}",
                track=FARMING_TRACK,
                yields=bundle,
                consumes=consumes,
                gj=gj_per_unit,
                time_per_unit=day / harvests_per_day,
            )
        )
        if juices:
            # The Grape-Juice-free alternative: slower, but spends no allowance.
            offers.append(
                Offer(
                    name=f"grow {crop.name} without grape juice",
                    track=FARMING_TRACK,
                    yields=bundle,
                    consumes={seed: seeds_per_planting} if seed else {},
                    time_per_unit=grow_time,
                )
            )
        if farm.takes_cookies(crop.name):
            cookie_bundle = {
                item: farm.cookie_day_yield(crop.name, item) for item in crop.drops
            }
            for item, rate in farm.dewstar_cookie_bonus(crop.name).items():
                cookie_bundle[item] = cookie_bundle.get(item, 0.0) + rate
            cookie_consumes: dict[str, float] = {}
            if ground and juices:
                cookie_consumes["grape juice"] = juices
            if seed:
                # A cookie day's plantings are the day's natural harvests plus the
                # Grape Juice grows -- the window boosts yields, not plantings.
                cookie_consumes[seed] = seeds_per_planting * harvests_per_day
            offers.append(
                Offer(
                    name=f"grow {crop.name} with cookies",
                    track=FARMING_TRACK,
                    yields=cookie_bundle,
                    consumes=cookie_consumes,
                    cookies=1,
                    gj=juices,
                    time_per_unit=day,
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
