"""Crop growing for Farm Solver.

Farming is another way to obtain items: the player plants seeds in a fixed
number of crop rows and harvests them. A seed has a primary crop but each seed
also has a small chance to drop other items instead, so one seed's harvest is a
*distribution*: mostly the primary crop, occasionally a rarer item. The
distribution is described per seed by an in-game drop table, e.g. Carrot Seeds
at 12 rows::

    Carrot        Crops/harvest all   47.55
    Runestone 01  Harvest alls/crop    3.34
    Gold Carrot   Harvest alls/crop    6.71

The two metrics are inverses: "Crops/harvest all" is how many of that item a
full harvest of every row yields, while "Harvest alls/crop" is how many full
harvests it takes to get one (used for the rare drops). Both scale with the row
count, so the row-independent figure we keep is the **per-row** expected count
(see :func:`per_row_rate`). A row holds :data:`PLOTS_PER_ROW` (four) seeds, so
the per-row figure is four times the true per-seed yield -- a normal crop's ~4
per row is ~1 per seed, a Mega/Spring seed's 40 is the in-game "10x per seed".
These raw figures include no yield bonuses.

A few seeds are bulkier: Mushroom, and the "Mega" seed versions of some crops
(Cotton, Sunflower and Beet), yield ten times as many items per seed as a normal
crop. Their in-game drop tables already reflect that, so they are stored like any
other crop -- a mega seed is simply another data row whose figures are ten times
its base crop's and whose ``primary`` names the base crop it grows.

A differing ``primary`` also marks the seed as **limited**: unlike a normal
crop's seeds (bought from the store in any quantity), a Mega or Spring seed
cannot be readily replaced, so plantings are bounded by the seeds held in
inventory (see :meth:`Crop.seed_item` and :func:`sources.farming_offers`).
Spring Seeds each yield ten flowers, one of the five colours at random, so the
drop table carries an expected two per seed of each (Red/Blue/White/Orange/
Yellow Flowers) and the usual row/yield multipliers apply on top; the crop's
``primary`` is the seed description's collective "Mixed Flowers", not itself a
real item (and so excluded from the Dewstar pool, see :data:`DEWSTAR_EXCLUDED`).

Two independent kinds of modifier then change a harvest:

* **speed** modifiers shorten the growth time by a factor -- one applies to
  every crop, and corn has a further one of its own (a factor below one grows
  faster, e.g. 0.5 halves the time);
* the **yield** modifier is a chance that each seed drops *two* items instead of
  one (typically 0%, 15% or 25%), raising every drop's expectation to
  ``1 + chance`` times its bonus-free rate.

A :class:`Farm` combines the crop data with the player's row count and modifiers
to work out, for a given crop and target item, how many are harvested per
planting and how long a quantity takes to grow. Crops are grown one at a time
(the rows are devoted to a single crop per planting), so each crop's grow time
is modelled as if it had every row to itself and several crops' times add up.

Growth can also be sped up with Grape Juice, which grows the whole field
instantly but only a limited number of times a day. Because crops are grown one
at a time, whichever crop is being farmed gets the full daily allowance, adding
that many free harvests a day on top of natural growth.

This module only models the structures; the seed data lives in the CSVs and the
row count and modifiers come from config.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from passive import parse_duration

# The bundled data, used unless another path is given.
DEFAULT_CROPS_CSV = Path(__file__).with_name("crops.csv")
DEFAULT_DROPS_CSV = Path(__file__).with_name("seed_drops.csv")

# The one crop with a growth-speed modifier of its own, beyond the global one.
CORN = "corn"

# A crop whose *base* growth is at most this grows almost instantly with full
# perks, so its real cost is the few seconds it takes to click a harvest rather
# than any wait (see :attr:`Farm.instant_grow_time`). Eggplant, at five minutes,
# is the slowest such crop.
INSTANT_GROW_MAX_BASE = timedelta(minutes=5)

# A full cookie set (one Happy, one Spooky and one Lovely Cookie active at once)
# multiplies the crop yield of any harvest taken while it lasts by this. One
# cookie triples the yield, so three stacked give nine times (see :meth:`Farm.
# cookie_day_yield`).
COOKIE_SET_MULTIPLIER = 9

# How long one cookie set's effect lasts -- the window during which harvests are
# boosted. You time it around a natural harvest and burn your Grape Juice inside
# it, so it bounds how many boosted harvests one set buys.
COOKIE_WINDOW = timedelta(minutes=5)

# Veggie Juice: drinking one cuts the time left on every growing crop by 10%, so
# several compound (``0.9 ** k``) -- but never below a minute. A planting can take
# as many as it likes; past the count that reaches the floor they gain nothing.
VEGGIE_JUICE = "veggie juice"
VEGGIE_JUICE_FACTOR = 0.9
VEGGIE_JUICE_FLOOR = timedelta(minutes=1)
# The juice counts per planting offered to the LP (see Farm.veggie_juice_steps):
# every count up to this, then each next ~VEGGIE_JUICE_STRIDE times the last.
# Grow time is convex in the count, so the LP's mix of two neighbouring counts is
# a slight over-estimate of the time in between -- never an under-estimate.
VEGGIE_JUICE_DENSE = 3
VEGGIE_JUICE_STRIDE = 1.6

# The inventory item a crop's seeds are held as: the crop's name plus this,
# lower-cased like every inventory key ("Mega Cotton" -> "mega cotton seeds").
# Only a limited seed's stock is modelled (see :meth:`Crop.seed_item`).
SEED_ITEM_SUFFIX = " seeds"

# Excluded from the Scythe of Dewstar's bonus-crop pool (see Farm.dewstar_bonus):
# Mushroom, and Spring -- whose "primary" (Mixed Flowers, the seed description's
# collective name) is not a real item; its actual drops are the five flower
# colours, which a Dewstar pick never gives.
DEWSTAR_EXCLUDED = frozenset({"mushroom", "spring"})

# A farm row has four plots, each planted with one seed -- so a full-field
# planting sows ``PLOTS_PER_ROW * rows`` seeds (what a limited-seed crop's offer
# consumes, see :func:`sources.farming_offers`), and the Scythe of Dewstar rolls
# one bonus crop per plot (see :meth:`Farm.dewstar_bonus`).
PLOTS_PER_ROW = 4

# Config gives modifiers as whole-number percentages.
PERCENT = 100

# The two metrics an in-game seed drop table uses (lower-cased).
CROPS_PER_HARVEST = "crops/harvest all"  # items yielded per full harvest
HARVESTS_PER_CROP = "harvest alls/crop"  # full harvests needed for one item

# The row count the figures in seed_drops.csv were captured at; each is
# normalised from this to a row-independent per-row rate at load.
CAPTURE_ROWS = 12


def per_row_rate(metric: str, value: float, rows: int) -> float:
    """Convert an in-game drop figure at ``rows`` rows to a per-row rate.

    ``Crops/harvest all`` counts items across every row, so the per-row rate is
    ``value / rows``; ``Harvest alls/crop`` is the inverse (harvests per item),
    so the per-row rate is ``1 / (value * rows)``. A row is four seeds
    (:data:`PLOTS_PER_ROW`), so this is *not* a per-seed figure.
    """
    label = metric.strip().lower()
    if label == CROPS_PER_HARVEST:
        return value / rows
    if label == HARVESTS_PER_CROP:
        return 1 / (value * rows)
    raise ValueError(f"unknown drop metric {metric!r}")


@dataclass
class Crop:
    """A crop and its seed's growing facts.

    ``growth`` is the base time one planting takes to mature. ``drops`` maps each
    lower-cased item the seed can produce (its primary crop included) to the
    bonus-free expected count per planted row (see :func:`per_row_rate`).
    ``primary`` names the main item produced; it defaults to the crop's own name
    but differs for a mega seed (``Mega Cotton`` produces ``Cotton``).
    """

    name: str
    growth: timedelta
    drops: dict[str, float] = field(default_factory=dict)
    primary: str = ""

    def __post_init__(self) -> None:
        if not self.primary:
            self.primary = self.name

    @property
    def seed_item(self) -> str | None:
        """The held seed item one planting consumes -- for a limited seed only.

        A limited seed (its ``primary`` names another item -- the Mega seeds and
        Spring Seeds) cannot be readily replaced, so plantings are bounded by the
        seeds held in inventory; the item is the crop's name plus
        :data:`SEED_ITEM_SUFFIX`, lower-cased as inventory keys are (``Mega
        Cotton`` -> ``mega cotton seeds``). A normal crop's seeds are freely
        purchasable in any quantity, so their stock is not modelled and this is
        ``None``.
        """
        if self.primary.lower() == self.name.lower():
            return None
        return self.name.lower() + SEED_ITEM_SUFFIX


@dataclass
class Farm:
    """The player's crops, row count, and growth modifiers.

    ``rows`` is how many seeds can be planted (and so harvested) at once.
    ``double_chance`` is the probability (0-1) that a seed drops two items rather
    than one, raising each drop's expectation to ``1 + double_chance`` times its
    bonus-free rate. ``speed`` multiplies every crop's growth time (a factor
    below one grows faster); ``corn_speed`` is a further factor for corn alone.
    ``grape_juices_per_day`` is how many times a day a Grape Juice can be used to
    grow the whole field instantly, adding that many free harvests a day on top
    of natural growth (see :meth:`time_to_grow`).

    ``cookie_time_threshold`` gates which crops a cookie set may boost: only crops
    whose grow time is at least this benefit enough to be worth a scarce set (a
    fast crop's harvest is dominated by natural growth, which cookies do not
    touch). ``None`` disables cookies entirely (see :meth:`cookie_day_yield`).

    ``instant_grow_time`` is the near-instant effective grow time of the very fast
    crops (base growth at most :data:`INSTANT_GROW_MAX_BASE`), which with full
    perks grow quicker than you can click; when set it replaces the speed formula
    for those crops (see :meth:`grow_time`). ``None`` leaves them on the formula.

    ``dewstar`` is whether the Scythe of Dewstar's bonus-crop yield is active (see
    :meth:`dewstar_bonus`); ``dewstar_journal`` is whether Dewstar's Journal is
    also held, extending the Cookie bonus to those bonus crops (see
    :meth:`dewstar_cookie_bonus`).

    ``veggie_juice`` is whether plantings may be sped up by drinking Veggie Juice
    (see :meth:`veggie_juice_steps`).
    """

    crops: dict[str, Crop] = field(default_factory=dict)
    rows: int = 0
    double_chance: float = 0.0
    speed: float = 1.0
    corn_speed: float = 1.0
    grape_juices_per_day: float = 0.0
    cookie_time_threshold: timedelta | None = None
    instant_grow_time: timedelta | None = None
    dewstar: bool = False
    dewstar_journal: bool = False
    veggie_juice: bool = False

    def crop(self, name: str) -> Crop | None:
        return self.crops.get(name.lower())

    def is_instant(self, name: str) -> bool:
        """Whether crop ``name`` is grown near-instantly (see :attr:`instant_grow_time`).

        True only when an ``instant_grow_time`` is set and the crop's *base* growth
        is at most :data:`INSTANT_GROW_MAX_BASE`. Grape Juice is never spent on such
        a crop -- it already grows faster than a wait, so an instant grow would be
        wasted (see :meth:`grape_juices_for`).
        """
        crop = self.crop(name)
        if crop is None or self.instant_grow_time is None:
            return False
        return crop.growth <= INSTANT_GROW_MAX_BASE

    def grape_juices_for(self, name: str) -> float:
        """The daily Grape Juice allowance spent growing ``name`` -- zero if instant."""
        return 0.0 if self.is_instant(name) else self.grape_juices_per_day

    def grow_time(self, name: str) -> timedelta | None:
        """Time for one planting of ``name`` to mature, with speed modifiers.

        A very fast crop (base growth at most :data:`INSTANT_GROW_MAX_BASE`) grows
        almost instantly with full perks, so when ``instant_grow_time`` is set it
        takes that fixed time -- the harvest cadence, not the speed formula.
        """
        crop = self.crop(name)
        if crop is None:
            return None
        if self.is_instant(name):
            return self.instant_grow_time
        factor = self.speed
        if name.lower() == CORN:
            factor *= self.corn_speed
        return crop.growth * factor

    def veggie_juice_steps(self, name: str) -> list[tuple[int, timedelta]]:
        """``[(juices, grow time), ...]``: the Veggie Juice options for one planting.

        Each juice drunk cuts the time left by ``1 - VEGGIE_JUICE_FACTOR`` (10%),
        compounding, down to :data:`VEGGIE_JUICE_FLOOR`; so ``k`` juices grow a
        planting in ``max(floor, grow_time * 0.9 ** k)``. The counts are sampled
        (every one to :data:`VEGGIE_JUICE_DENSE`, then geometrically) and end at
        the first that reaches the floor, since more gain nothing. Empty when
        Veggie Juice is off, the crop is near-instant, or already at the floor.
        """
        grow_time = self.grow_time(name)
        if not self.veggie_juice or grow_time is None or self.is_instant(name):
            return []
        if grow_time <= VEGGIE_JUICE_FLOOR:
            return []
        steps: list[tuple[int, timedelta]] = []
        k = 1
        while True:
            time = max(VEGGIE_JUICE_FLOOR, grow_time * VEGGIE_JUICE_FACTOR**k)
            if time == VEGGIE_JUICE_FLOOR:
                # The fewest juices that reach the floor -- the last useful count.
                k = math.ceil(
                    math.log(VEGGIE_JUICE_FLOOR / grow_time)
                    / math.log(VEGGIE_JUICE_FACTOR)
                    - 1e-9
                )
                if not steps or steps[-1][0] < k:
                    steps.append((k, VEGGIE_JUICE_FLOOR))
                return steps
            steps.append((k, time))
            k = k + 1 if k < VEGGIE_JUICE_DENSE else math.ceil(k * VEGGIE_JUICE_STRIDE)

    def per_harvest(self, name: str, item: str | None = None) -> float | None:
        """Expected count of ``item`` from one full harvest of crop ``name``.

        ``item`` defaults to the crop's primary produce. The bonus-free per-row
        rate is scaled by the row count and the double-drop chance. Returns
        ``None`` if the crop is unknown or does not drop that item.
        """
        crop = self.crop(name)
        if crop is None:
            return None
        rate = crop.drops.get((item or crop.primary).lower())
        if rate is None:
            return None
        return self.rows * rate * (1 + self.double_chance)

    def takes_cookies(self, name: str) -> bool:
        """Whether crop ``name`` is slow enough for a cookie set to be worth it.

        A cookie set boosts a fixed few harvests (the one or handful taken in its
        five-minute window, plus the day's Grape Juice grows). On a slow crop each
        of those boosted harvests stands in for a long grow, so the set saves a lot
        of farming time; on a fast crop the same harvests are cheap to grow anyway,
        so a scarce set is wasted. A crop qualifies when its grow time reaches the
        farm's ``cookie_time_threshold``; with no threshold set, cookies are off.
        """
        threshold = self.cookie_time_threshold
        if threshold is None:
            return False
        grow_time = self.grow_time(name)
        return grow_time is not None and grow_time >= threshold

    def _cookie_boosted_harvests(self, name: str) -> float | None:
        """A cookie day's harvest count for ``name``, boosted-equivalent.

        The boost applies to whatever is *harvested* during the set's
        :data:`COOKIE_WINDOW`, at :data:`COOKIE_SET_MULTIPLIER` times the count. In
        that window you harvest the crop you timed to finish plus any that regrow
        naturally inside it (many for a fast crop, just the one for a slow crop),
        and burn the day's Grape Juice instant grows -- all boosted. The day's
        other natural harvests, outside the window, are not. So the day's
        boosted-equivalent harvest count is ``natural - in_window + multiplier *
        (in_window + grape_juices)``. Returns ``None`` for an unknown crop; shared
        by :meth:`cookie_day_yield` and :meth:`dewstar_cookie_bonus`.
        """
        grow_time = self.grow_time(name)
        if grow_time is None:
            return None
        natural = timedelta(days=1) / grow_time
        # Harvests boosted inside the window: the one timed to it, plus any natural
        # regrowth that fits (fast crops), capped at the day's natural harvests.
        in_window = min(natural, 1 + math.floor(COOKIE_WINDOW / grow_time))
        boosted = in_window + self.grape_juices_per_day
        return natural - in_window + COOKIE_SET_MULTIPLIER * boosted

    def cookie_day_yield(self, name: str, item: str | None = None) -> float | None:
        """Expected ``item`` from one day of farming ``name`` under a cookie set.

        See :meth:`_cookie_boosted_harvests` for the boosted-equivalent harvest
        count; the day yields that many harvests' worth of ``item``. Returns
        ``None`` when the crop does not qualify (see :meth:`takes_cookies`) or does
        not drop the item.
        """
        if not self.takes_cookies(name):
            return None
        per_harvest = self.per_harvest(name, item)
        harvests = self._cookie_boosted_harvests(name)
        if not per_harvest or harvests is None:
            return None
        return harvests * per_harvest

    def _cookie_burst_harvests(self, name: str) -> float | None:
        """Boosted-equivalent harvest count from a single once-per-day cookie burst.

        A cookie set's five-minute :data:`COOKIE_WINDOW` is used once a day and is
        the best home for that day's Grape Juice: everything harvested in it -- the
        one or few crops that mature inside the window plus every Grape Juice instant
        grow -- comes out at :data:`COOKIE_SET_MULTIPLIER`. This is that boost *only*
        (``multiplier * (in_window + grape_juices)``); the day's other natural
        harvests belong to the plain ``grow`` offer, so the burst is an effectively
        instant action rather than a whole day (contrast :meth:`_cookie_boosted_
        harvests`, the whole-day count this is the boosted slice of). ``None`` for an
        unknown crop.
        """
        grow_time = self.grow_time(name)
        if grow_time is None:
            return None
        natural = timedelta(days=1) / grow_time
        in_window = min(natural, 1 + math.floor(COOKIE_WINDOW / grow_time))
        return COOKIE_SET_MULTIPLIER * (in_window + self.grape_juices_per_day)

    def cookie_burst_yield(self, name: str, item: str | None = None) -> float | None:
        """Expected ``item`` from one once-per-day cookie burst on ``name``.

        See :meth:`_cookie_burst_harvests`; the burst yields that many harvests'
        worth of ``item``. ``None`` when the crop does not qualify for cookies (see
        :meth:`takes_cookies`) or does not drop the item.
        """
        if not self.takes_cookies(name):
            return None
        per_harvest = self.per_harvest(name, item)
        harvests = self._cookie_burst_harvests(name)
        if not per_harvest or harvests is None:
            return None
        return harvests * per_harvest

    def cookie_day_harvests(self, name: str) -> tuple[float, float] | None:
        """Harvest Alls in one cookie day of ``name``, as ``(natural, boosted)``.

        A cookie day is a *whole* day of farming, not just the boosted window: the
        crop keeps regrowing all day and every one of those harvests is taken. Only
        the one timed to the five-minute :data:`COOKIE_WINDOW` and that day's Grape
        Juice instant grows come out at :data:`COOKIE_SET_MULTIPLIER` times the
        yield (``boosted``, the same count as :meth:`cookie_burst_plantings`); the
        day's other natural regrowths (``natural``) are ordinary harvests. Their
        sum is the day's real Harvest All count -- contrast
        :meth:`_cookie_boosted_harvests`, the *yield* multiple those harvests come
        to. Reporting only; ``None`` when the crop does not qualify for cookies.
        """
        if not self.takes_cookies(name):
            return None
        grow_time = self.grow_time(name)
        if grow_time is None:
            return None
        natural = timedelta(days=1) / grow_time
        in_window = min(natural, 1 + math.floor(COOKIE_WINDOW / grow_time))
        return natural - in_window, in_window + self.grape_juices_per_day

    def cookie_burst_plantings(self, name: str) -> float:
        """Full-field plantings one cookie burst harvests (window crops + Grape Juice).

        One planting per boosted harvest -- the Cookie multiplier boosts the yield,
        not the number of plantings -- so this is a limited-seed crop's seed spend
        per burst. Zero when the crop does not qualify for cookies or is unknown.
        """
        if not self.takes_cookies(name):
            return 0.0
        grow_time = self.grow_time(name)
        if grow_time is None:
            return 0.0
        natural = timedelta(days=1) / grow_time
        in_window = min(natural, 1 + math.floor(COOKIE_WINDOW / grow_time))
        return in_window + self.grape_juices_per_day

    def dewstar_cookie_burst_bonus(self, name: str) -> dict[str, float]:
        """Dewstar bonus crops from one cookie burst on ``name``.

        Mirrors :meth:`dewstar_cookie_bonus` but over the burst's harvests only:
        boosted with Dewstar's Journal (:meth:`_cookie_burst_harvests`), the raw
        window plantings without it (:meth:`cookie_burst_plantings`). Empty when
        Dewstar is off or the crop does not qualify for cookies.
        """
        if not self.dewstar or not self.takes_cookies(name):
            return {}
        bonus = self.dewstar_bonus(name)
        if not bonus:
            return {}
        harvests = (
            self._cookie_burst_harvests(name)
            if self.dewstar_journal
            else self.cookie_burst_plantings(name)
        )
        if harvests is None:
            return {}
        return {item: rate * harvests for item, rate in bonus.items()}

    def _dewstar_pool(self, name: str) -> list[Crop]:
        """Crops the Scythe of Dewstar may pick as a bonus for growing ``name``.

        Every crop no slower than ``name`` (compared on raw seed :attr:`Crop.growth`,
        unmodified by speed perks -- so, e.g., Carrot's pool is Carrot and Peppers
        regardless of :attr:`speed`), :data:`DEWSTAR_EXCLUDED` (Mushroom, Spring)
        aside.
        """
        crop = self.crop(name)
        if crop is None:
            return []
        return [
            c for c in self.crops.values()
            if c.name.lower() not in DEWSTAR_EXCLUDED and c.growth <= crop.growth
        ]

    def dewstar_bonus(self, name: str) -> dict[str, float]:
        """Extra crops one harvest of ``name`` yields via the Scythe of Dewstar.

        One bonus crop is rolled per farm plot (:data:`PLOTS_PER_ROW` *
        ``rows``), each choosing uniformly among :meth:`_dewstar_pool` and giving
        exactly one of the chosen crop's *primary* item -- never a rarer drop, and
        never scaled by a picked Mega seed's own tenfold yield. Empty when
        ``dewstar`` is off or the crop is unknown.
        """
        if not self.dewstar:
            return {}
        pool = self._dewstar_pool(name)
        if not pool:
            return {}
        share = (PLOTS_PER_ROW * self.rows) / len(pool)
        bonus: dict[str, float] = {}
        for crop in pool:
            item = crop.primary.lower()
            bonus[item] = bonus.get(item, 0.0) + share
        return bonus

    def dewstar_cookie_bonus(self, name: str) -> dict[str, float]:
        """Dewstar bonus crops from one cookie-boosted day of farming ``name``.

        Without Dewstar's Journal the Cookie bonus does not touch these bonus
        crops -- every harvest in the day, in the window or out, gives the flat
        per-harvest :meth:`dewstar_bonus` count. With the journal they follow the
        crop's own boosted-equivalent harvest count (see
        :meth:`_cookie_boosted_harvests`), so windowed harvests give
        :data:`COOKIE_SET_MULTIPLIER` times as many too. Empty when ``dewstar`` is
        off or the crop does not qualify for cookies (see :meth:`takes_cookies`).
        """
        if not self.dewstar or not self.takes_cookies(name):
            return {}
        bonus = self.dewstar_bonus(name)
        if not bonus:
            return {}
        grow_time = self.grow_time(name)
        if grow_time is None:
            return {}
        if self.dewstar_journal:
            harvests = self._cookie_boosted_harvests(name)
        else:
            natural = timedelta(days=1) / grow_time
            harvests = natural + self.grape_juices_per_day
        if harvests is None:
            return {}
        return {item: rate * harvests for item, rate in bonus.items()}

    def time_to_grow(
        self, name: str, quantity: int, item: str | None = None
    ) -> timedelta | None:
        """Time to harvest ``quantity`` of ``item`` by growing crop ``name``.

        Harvests come a whole planting at a time, so the number of plantings
        rounds up. Each day the field yields its natural harvests (a day divided
        by the grow time) plus ``grape_juices_per_day`` free instant harvests, so
        the wait is the plantings divided by that daily rate. With no grape juice
        this reduces to ``plantings * grow_time``. Returns ``None`` when nothing
        can be grown (unknown crop or item, no rows, or a zero rate).
        """
        per_harvest = self.per_harvest(name, item)
        grow_time = self.grow_time(name)
        if not per_harvest or grow_time is None:
            return None
        plantings = math.ceil(quantity / per_harvest)
        day = timedelta(days=1)
        harvests_per_day = day / grow_time + self.grape_juices_for(name)
        return day * (plantings / harvests_per_day)

    def grape_juices_used(self, time: timedelta, name: str | None = None) -> float:
        """Grape juices spent farming ``name`` for ``time`` at the daily allowance.

        Whichever crop is being farmed gets the full daily allowance, so the count
        is the rate times the duration in days -- but an instant-grow crop gets
        none, since a Grape Juice would be wasted on it (see :meth:`is_instant`).
        """
        rate = self.grape_juices_for(name) if name is not None else self.grape_juices_per_day
        return rate * (time / timedelta(days=1))

    def growers(self, item: str) -> list[Crop]:
        """The crops whose seed can drop ``item`` (its primary or a rarer drop)."""
        key = item.lower()
        return [crop for crop in self.crops.values() if key in crop.drops]

    def growable_items(self) -> set[str]:
        """Every lower-cased item some crop can drop."""
        items: set[str] = set()
        for crop in self.crops.values():
            items |= set(crop.drops)
        return items

    def best_source(self, item: str, quantity: int) -> tuple[str, timedelta] | None:
        """The crop that grows ``quantity`` of ``item`` fastest, and that time.

        Every crop that drops ``item`` is considered, so a rare drop is grown via
        its crop and a bulk mega seed beats its base for large amounts. Returns
        the crop's name and the grow time, or ``None`` when nothing grows it (or
        the farm has no rows). Ties break on the crop name for a stable choice.
        """
        best: tuple[str, timedelta] | None = None
        for crop in sorted(self.growers(item), key=lambda c: c.name):
            time = self.time_to_grow(crop.name, quantity, item)
            if time is None:
                continue
            if best is None or time < best[1]:
                best = (crop.name, time)
        return best


def read_crops(path: Path) -> dict[str, Crop]:
    """Read crops from a ``Crop,Growth[,Primary]`` CSV, keyed by lower-cased name.

    ``Growth`` is a duration like ``"10m"`` or ``"1d6h"`` (see
    :func:`passive.parse_duration`). The optional ``Primary`` column names the
    main item a seed produces when it differs from the crop's own name (a ``Mega
    Cotton`` seed produces ``Cotton``); left blank it defaults to the crop name.
    The crops come back without drops; :func:`load_farm` attaches those.
    """
    crops: dict[str, Crop] = {}
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            name = row["Crop"].strip()
            crops[name.lower()] = Crop(
                name=name,
                growth=parse_duration(row["Growth"]),
                primary=(row.get("Primary") or "").strip(),
            )
    return crops


def read_drops(path: Path) -> dict[str, dict[str, float]]:
    """Read seed drops from a ``Crop,Item,Metric,Value`` CSV.

    Each row is an in-game drop figure (``Metric`` one of the two units, see
    :func:`per_row_rate`) captured at :data:`CAPTURE_ROWS` rows; it is
    normalised to a row-independent per-row rate. The result maps a crop's
    lower-cased name to its drops -- each dropped item (lower-cased) to its
    bonus-free per-row expected count. A missing file yields no drops, so crops
    fall back to growth times alone.
    """
    drops: dict[str, dict[str, float]] = {}
    if not path.exists():
        return drops
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            crop = row["Crop"].strip().lower()
            item = row["Item"].strip().lower()
            rate = per_row_rate(row["Metric"], float(row["Value"]), CAPTURE_ROWS)
            drops.setdefault(crop, {})[item] = rate
    return drops


def load_farm(
    crops_path: Path | None = None,
    drops_path: Path | None = None,
    rows: int = 0,
    double_chance: float = 0.0,
    speed: float = 1.0,
    corn_speed: float = 1.0,
    grape_juices_per_day: float = 0.0,
    cookie_time_threshold: timedelta | None = None,
    instant_grow_time: timedelta | None = None,
    dewstar: bool = False,
    dewstar_journal: bool = False,
    veggie_juice: bool = False,
) -> Farm:
    """Build a :class:`Farm` from the crop CSVs and the player's settings."""
    crops = read_crops(crops_path or DEFAULT_CROPS_CSV)
    drops = read_drops(drops_path or DEFAULT_DROPS_CSV)
    for key, crop in crops.items():
        crop.drops = drops.get(key, {})
    return Farm(
        crops=crops,
        rows=rows,
        double_chance=double_chance,
        speed=speed,
        corn_speed=corn_speed,
        grape_juices_per_day=grape_juices_per_day,
        cookie_time_threshold=cookie_time_threshold,
        instant_grow_time=instant_grow_time,
        dewstar=dewstar,
        dewstar_journal=dewstar_journal,
        veggie_juice=veggie_juice,
    )


def farm_from_config(
    config_section: dict,
    crops_path: Path | None = None,
    drops_path: Path | None = None,
    have_scythe_of_dewstar: bool = False,
    have_dewstars_journal: bool = False,
) -> Farm:
    """Build a :class:`Farm` from a ``[farming]`` config section.

    The section gives the player's ``rows`` and their modifiers as whole-number
    percentages: ``double_crop_chance`` (chance of a second drop),
    ``speed_reduction`` (how much faster every crop grows) and
    ``corn_speed_reduction`` (corn's further reduction). The reductions become
    growth-time factors of ``1 - reduction`` (a 90% reduction leaves a 0.1
    factor). ``grape_juices_per_day`` is a plain count of daily instant grows.
    ``cookies_grow_time_threshold`` is a duration (e.g. ``"18m"``) below which a
    crop is too fast to be worth a cookie set; omit it to disable cookies.
    ``instant_grow_time`` (e.g. ``"3s"``) is how long the very fast crops
    effectively take; omit it to leave them on the speed formula. Crop and drop
    data come from the CSVs. ``have_scythe_of_dewstar`` and
    ``have_dewstars_journal`` are resolved by the caller -- see
    ``main.resolve_scythe_of_dewstar`` / ``main.resolve_dewstars_journal``.
    ``use_veggie_juice`` lets plantings be sped up with Veggie Juice (see
    :meth:`Farm.veggie_juice_steps`).
    """
    rows = int(config_section.get("rows", 0))
    double_chance = float(config_section.get("double_crop_chance", 0)) / PERCENT
    reduction = float(config_section.get("speed_reduction", 0))
    if reduction >= PERCENT:
        # 100% would make every crop instant and divide grow-rate maths by
        # zero. Reachable by accident: the level-10 tower reward adds +10 in
        # ascension mode, so a config whose speed_reduction already bakes that
        # reward in (e.g. 90) overflows when climbing from below level 10.
        raise ValueError(
            f"[farming] speed_reduction must be below 100, not {reduction:g} "
            "(if climbing with --ascend from below level 10, the config value "
            "must exclude the level-10 reward's +10)"
        )
    speed = 1 - reduction / PERCENT
    corn_speed = 1 - float(config_section.get("corn_speed_reduction", 0)) / PERCENT
    grape_juices = float(config_section.get("grape_juices_per_day", 0))
    threshold_text = config_section.get("cookies_grow_time_threshold")
    threshold = parse_duration(threshold_text) if threshold_text else None
    instant_text = config_section.get("instant_grow_time")
    instant = parse_duration(instant_text) if instant_text else None
    return load_farm(
        crops_path, drops_path, rows, double_chance, speed, corn_speed, grape_juices,
        threshold, instant, have_scythe_of_dewstar, have_dewstars_journal,
        bool(config_section.get("use_veggie_juice")),
    )
