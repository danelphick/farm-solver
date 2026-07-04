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
count, so the row-independent figure we keep is the **per-seed** expected count
(see :func:`per_seed_rate`). These raw figures include no yield bonuses.

A few seeds are bulkier: Mushroom, and the "Mega" seed versions of some crops
(Cotton, Sunflower and Beet), yield ten times as many items per seed as a normal
crop. Their in-game drop tables already reflect that, so they are stored like any
other crop -- a mega seed is simply another data row whose figures are ten times
its base crop's and whose ``primary`` names the base crop it grows.

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

# A full cookie set (one Happy, one Spooky and one Lovely Cookie active at once)
# multiplies the crop yield of any harvest taken while it lasts by this. One
# cookie triples the yield, so three stacked give nine times (see :meth:`Farm.
# cookie_day_yield`).
COOKIE_SET_MULTIPLIER = 9

# How long one cookie set's effect lasts -- the window during which harvests are
# boosted. You time it around a natural harvest and burn your Grape Juice inside
# it, so it bounds how many boosted harvests one set buys.
COOKIE_WINDOW = timedelta(minutes=5)

# Config gives modifiers as whole-number percentages.
PERCENT = 100

# The two metrics an in-game seed drop table uses (lower-cased).
CROPS_PER_HARVEST = "crops/harvest all"  # items yielded per full harvest
HARVESTS_PER_CROP = "harvest alls/crop"  # full harvests needed for one item

# The row count the figures in seed_drops.csv were captured at; each is
# normalised from this to a row-independent per-seed rate at load.
CAPTURE_ROWS = 12


def per_seed_rate(metric: str, value: float, rows: int) -> float:
    """Convert an in-game drop figure at ``rows`` rows to a per-seed rate.

    ``Crops/harvest all`` counts items across every row, so the per-seed rate is
    ``value / rows``; ``Harvest alls/crop`` is the inverse (harvests per item),
    so the per-seed rate is ``1 / (value * rows)``.
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
    bonus-free expected count per seed. ``primary`` names the main item produced;
    it defaults to the crop's own name but differs for a mega seed (``Mega
    Cotton`` produces ``Cotton``).
    """

    name: str
    growth: timedelta
    drops: dict[str, float] = field(default_factory=dict)
    primary: str = ""

    def __post_init__(self) -> None:
        if not self.primary:
            self.primary = self.name


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
    """

    crops: dict[str, Crop] = field(default_factory=dict)
    rows: int = 0
    double_chance: float = 0.0
    speed: float = 1.0
    corn_speed: float = 1.0
    grape_juices_per_day: float = 0.0
    cookie_time_threshold: timedelta | None = None

    def crop(self, name: str) -> Crop | None:
        return self.crops.get(name.lower())

    def grow_time(self, name: str) -> timedelta | None:
        """Time for one planting of ``name`` to mature, with speed modifiers."""
        crop = self.crop(name)
        if crop is None:
            return None
        factor = self.speed
        if name.lower() == CORN:
            factor *= self.corn_speed
        return crop.growth * factor

    def per_harvest(self, name: str, item: str | None = None) -> float | None:
        """Expected count of ``item`` from one full harvest of crop ``name``.

        ``item`` defaults to the crop's primary produce. The bonus-free per-seed
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

    def cookie_day_yield(self, name: str, item: str | None = None) -> float | None:
        """Expected ``item`` from one day of farming ``name`` under a cookie set.

        The boost applies to whatever is *harvested* during the set's
        :data:`COOKIE_WINDOW`, at :data:`COOKIE_SET_MULTIPLIER` times the yield. In
        that window you harvest the crop you timed to finish plus any that regrow
        naturally inside it (many for a fast crop, just the one for a slow crop),
        and burn the day's Grape Juice instant grows -- all boosted. The day's
        other natural harvests, outside the window, are not. So the day yields
        ``(natural - in_window + multiplier * (in_window + grape_juices)) *
        per_harvest``. Returns ``None`` when the crop does not qualify (see
        :meth:`takes_cookies`) or does not drop the item.
        """
        if not self.takes_cookies(name):
            return None
        per_harvest = self.per_harvest(name, item)
        grow_time = self.grow_time(name)
        if not per_harvest or grow_time is None:
            return None
        natural = timedelta(days=1) / grow_time
        # Harvests boosted inside the window: the one timed to it, plus any natural
        # regrowth that fits (fast crops), capped at the day's natural harvests.
        in_window = min(natural, 1 + math.floor(COOKIE_WINDOW / grow_time))
        boosted = in_window + self.grape_juices_per_day
        return (natural - in_window + COOKIE_SET_MULTIPLIER * boosted) * per_harvest

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
        harvests_per_day = day / grow_time + self.grape_juices_per_day
        return day * (plantings / harvests_per_day)

    def grape_juices_used(self, time: timedelta) -> float:
        """Grape juices spent farming for ``time`` at the daily allowance.

        Whichever crop is being farmed gets the full daily allowance, so the
        count is simply the rate times the duration in days, regardless of crop.
        """
        return self.grape_juices_per_day * (time / timedelta(days=1))

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
    :func:`per_seed_rate`) captured at :data:`CAPTURE_ROWS` rows; it is
    normalised to a row-independent per-seed rate. The result maps a crop's
    lower-cased name to its drops -- each dropped item (lower-cased) to its
    bonus-free per-seed expected count. A missing file yields no drops, so crops
    fall back to growth times alone.
    """
    drops: dict[str, dict[str, float]] = {}
    if not path.exists():
        return drops
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            crop = row["Crop"].strip().lower()
            item = row["Item"].strip().lower()
            rate = per_seed_rate(row["Metric"], float(row["Value"]), CAPTURE_ROWS)
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
    )


def farm_from_config(
    config_section: dict,
    crops_path: Path | None = None,
    drops_path: Path | None = None,
) -> Farm:
    """Build a :class:`Farm` from a ``[farming]`` config section.

    The section gives the player's ``rows`` and their modifiers as whole-number
    percentages: ``double_crop_chance`` (chance of a second drop),
    ``speed_reduction`` (how much faster every crop grows) and
    ``corn_speed_reduction`` (corn's further reduction). The reductions become
    growth-time factors of ``1 - reduction`` (a 90% reduction leaves a 0.1
    factor). ``grape_juices_per_day`` is a plain count of daily instant grows.
    ``cookies_grow_time_threshold`` is a duration (e.g. ``"18m"``) below which a
    crop is too fast to be worth a cookie set; omit it to disable cookies. Crop
    and drop data come from the CSVs.
    """
    rows = int(config_section.get("rows", 0))
    double_chance = float(config_section.get("double_crop_chance", 0)) / PERCENT
    speed = 1 - float(config_section.get("speed_reduction", 0)) / PERCENT
    corn_speed = 1 - float(config_section.get("corn_speed_reduction", 0)) / PERCENT
    grape_juices = float(config_section.get("grape_juices_per_day", 0))
    threshold_text = config_section.get("cookies_grow_time_threshold")
    threshold = parse_duration(threshold_text) if threshold_text else None
    return load_farm(
        crops_path, drops_path, rows, double_chance, speed, corn_speed, grape_juices,
        threshold,
    )
