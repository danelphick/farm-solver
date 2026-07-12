"""Passive production for Farm Solver.

Some FarmRPG buildings generate items on a fixed cadence (every ten minutes,
hourly, or daily) without being crafted. This module models those producers so
the solver can work out how long passive generation would take to supply a
material instead of crafting it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

# Absolute slack for "has this deposit reached the target yet" comparisons, so a
# quantity that is an exact multiple of a per-cycle amount is not pushed into an
# extra, unneeded cycle by float noise (e.g. a chain of divisions landing on
# 200.0000001 instead of 200). Comfortably below any real quantity this module
# deals in (storehouse caps and passive hauls are all whole items or cents of a
# rate), so it never masks a genuine shortfall.
_QUANTITY_EPSILON = 1e-6

# Cadences at which the game tops up passive producers.
MINUTE = timedelta(minutes=1)
TEN_MINUTES = timedelta(minutes=10)
HOURLY = timedelta(hours=1)
DAILY = timedelta(days=1)

# Items that are not held in the storehouse, so its per-cycle cap does not apply
# to them. Stamina is a separate stat (the Farmhouse tops it up daily, far above
# the storehouse size), not a stored good.
UNCAPPED_ITEMS = frozenset({"stamina"})


@dataclass
class PassiveProducer:
    """A building that yields items on a fixed cadence.

    ``outputs`` maps the lower-cased item name to the quantity produced each
    ``period``. Quantities are usually whole, but some sources (e.g. pets)
    produce a fractional average per cycle.
    """

    name: str
    period: timedelta
    outputs: dict[str, float] = field(default_factory=dict)


@dataclass
class PassiveProduction:
    """All passive producers, queried by item.

    ``inventory_size`` is the storehouse capacity. Each cycle the output is
    deposited into the storehouse, which holds at most ``inventory_size`` and
    voids the excess, and the player uses it before the next cycle. So the
    effective per-cycle output is ``min(production, inventory_size)``; it caps
    only the per-cycle amount, not the total obtainable over many cycles.
    """

    producers: list[PassiveProducer] = field(default_factory=list)
    inventory_size: int | None = None

    def per_cycle(self, producer: PassiveProducer, item: str) -> float:
        """``producer``'s effective per-deposit output of ``item``, storehouse-clamped.

        The single place the storehouse-cap rule (see the class docstring) is
        applied to a producer's raw ``outputs`` entry -- :meth:`producer_rates`
        divides this by the period to get a rate, and :meth:`quantized_wait` walks
        it directly as the size of each scheduled deposit. 0.0 when the producer
        does not make ``item`` at all (absent, zero, or a non-positive amount),
        never negative.
        """
        key = item.lower()
        quantity = producer.outputs.get(key)
        if not quantity or quantity <= 0:
            return 0.0
        if self.inventory_size is not None and key not in UNCAPPED_ITEMS:
            return min(quantity, self.inventory_size)
        return quantity

    def producer_rates(self, item: str) -> list[tuple[str, float]]:
        """Each producer's per-minute rate for ``item`` as ``(name, rate)``.

        Only producers that actually make the item are listed, at their
        storehouse-clamped :meth:`per_cycle` amount (the single place that rule
        lives). Callers wanting a total sum the rates, and callers wanting the
        per-building breakdown (see :func:`main._passive_sources`,
        :func:`main._pet_passive_rate`) read them directly.
        """
        key = item.lower()
        rates: list[tuple[str, float]] = []
        for producer in self.producers:
            per_cycle = self.per_cycle(producer, key)
            if per_cycle <= 0:
                continue
            rate = per_cycle / (producer.period.total_seconds() / 60)
            if rate > 0:
                rates.append((producer.name, rate))
        return rates

    def rate_per_minute(self, item: str) -> float:
        """How many of ``item`` are produced per minute across all producers."""
        return sum(rate for _, rate in self.producer_rates(item))

    def produces(self, item: str) -> bool:
        """Whether anything generates ``item`` passively."""
        return self.rate_per_minute(item) > 0

    def time_to_produce(self, item: str, quantity: int) -> timedelta | None:
        """Time to passively generate ``quantity`` of ``item``.

        Delegates to :meth:`quantized_wait` with no ``smeared`` predicate, so
        every producer -- including a pet's own averaged daily haul -- is treated
        as a real whole-cycle depositor. With a single producer this reproduces
        the old rounded-up-to-a-whole-period behaviour exactly. **Behaviour
        change:** an item made by several producers on the same period (the main
        raptor pen run plus its antler snare, say) used to fall back to their
        combined continuous rate and could report a fraction of a period; it now
        staggers the producers across the period instead (see
        :meth:`quantized_wait`), which is the real deposit schedule and only ever
        reports whole cycle-times. Returns ``None`` when nothing produces the
        item.
        """
        return self.quantized_wait(item, quantity)

    def quantized_wait(
        self,
        item: str,
        quantity: float,
        smeared: Callable[[str], bool] | None = None,
    ) -> timedelta | None:
        """Minimal time for whole passive deposits (plus smeared rates) to supply
        ``quantity`` of ``item``; ``None`` when nothing produces it.

        The real-world rule: a building deposits its storehouse-clamped
        :meth:`per_cycle` amount at its own scheduled times, and you hold only
        whatever has *landed* -- not a smooth fraction of the next deposit. A
        *smeared* producer -- one ``smeared`` flags true for, e.g. a pet's
        averaged daily haul or the Mechanical Heart's monthly grant, both of
        which are themselves already an average standing in for lumpy real
        events -- is instead treated as a continuous ramp, since quantising an
        average would be quantising a fiction. ``smeared`` defaults to "nothing
        is smeared" (every producer is a real depositor); it is injected rather
        than read off a name convention because this module must not import
        :mod:`config`, which owns that convention (see
        :func:`config.passive_quantizer`).

        Producers that share one ``period`` are evenly phase-staggered across it
        (deposits at ``period/N``, ``2*period/N``, ..., cycling) rather than
        landing together -- the raptor pen and its antler snare, both daily and
        both storehouse-clamped, so bank one storehouse-worth every twelve hours,
        not two at once every day. When their per-cycle amounts differ, the
        *order* they arrive within a cycle is unknown, so the k-th stagger slot
        is assumed to hold the k *smallest* deposits of the cycle (ascending,
        worst case) -- never optimistic, and by the end of a full period the
        whole cycle's total has landed regardless of the true order. Producers on
        different periods run independent schedules. A lone producer on a period
        is the ``N=1`` case of this same rule: its first deposit lands one full
        period out, exactly the old single-producer behaviour.

        The result is the minimal ``t`` at which the summed step functions (plus
        the smeared ramp) reach ``quantity`` -- never below the continuous
        ``quantity / total_rate`` estimate, and exact to the real deposit
        schedule rather than merely the combined rate. Small floating error
        (a quantity landing a hair past an exact multiple) is absorbed by
        :data:`_QUANTITY_EPSILON` rather than forced into an extra cycle.
        """
        key = item.lower()
        if quantity <= 0:
            return timedelta(0)

        smeared_rate = 0.0
        groups: dict[timedelta, list[float]] = {}
        for producer in self.producers:
            per_cycle = self.per_cycle(producer, key)
            if per_cycle <= 0:
                continue
            if smeared is not None and smeared(producer.name):
                smeared_rate += per_cycle / (producer.period.total_seconds() / 60)
            else:
                groups.setdefault(producer.period, []).append(per_cycle)
        real_rate = sum(
            sum(amounts) / (period.total_seconds() / 60)
            for period, amounts in groups.items()
        )
        total_rate = smeared_rate + real_rate
        if total_rate <= 0:
            return None
        t0 = timedelta(minutes=quantity / total_rate)
        if not groups:
            return t0  # purely smeared: the continuous estimate is already exact

        # The crossing can only sit between t0 (supply never beats the combined
        # continuous rate) and t0 + the longest real period (by then every real
        # producer has deposited at least `rate * t0`-worth -- see the
        # docstring). So rather than enumerate every deposit from the start of
        # the plan -- a per-minute mill days into a plan would mean thousands of
        # events for a crossing that only ever lands in the last couple -- sum
        # everything landed by the window's start in closed form and walk only
        # the deposits inside the window.
        for amounts in groups.values():
            amounts.sort()  # ascending -- the worst-case fill order
        prefix: dict[timedelta, list[float]] = {}
        for period, amounts in groups.items():
            sums = [0.0]
            for amount in amounts:
                sums.append(sums[-1] + amount)
            prefix[period] = sums

        def landed(period: timedelta, t: timedelta) -> float:
            """A group's whole deposits at or before ``t``, in closed form."""
            sums = prefix[period]
            n = len(sums) - 1
            # The +epsilon keeps a deposit landing exactly at ``t`` counted in
            # the face of the float division (t and slot are both timedeltas).
            k = int(t / (period / n) + 1e-9)
            return (k // n) * sums[n] + sums[k % n]

        # Start the walk at the last deposit at or before t0 (a real event time,
        # so an exact-multiple quantity still reports the deposit's own time).
        start = max(
            (period / len(amounts)) * int(t0 / (period / len(amounts)) + 1e-9)
            for period, amounts in groups.items()
        )
        window_end = t0 + max(groups)

        def supply_at(t: timedelta) -> float:
            ramp = smeared_rate * (t.total_seconds() / 60)
            return ramp + sum(landed(period, t) for period in groups)

        supply = supply_at(start)
        if supply >= quantity - _QUANTITY_EPSILON:
            return start
        events: set[timedelta] = set()
        for period, amounts in groups.items():
            slot = period / len(amounts)
            k = int(start / slot + 1e-9) + 1
            while (when := slot * k) <= window_end:
                events.add(when)
                k += 1
        t = start
        for when in sorted(events):
            if smeared_rate > 0:
                span = (when - t).total_seconds() / 60
                if supply + smeared_rate * span >= quantity - _QUANTITY_EPSILON:
                    short = (quantity - supply) / smeared_rate
                    return t + timedelta(minutes=short)
            reached = supply_at(when)
            if reached >= quantity - _QUANTITY_EPSILON:
                return when
            supply, t = reached, when
        return window_end  # defensive: the window bound above should preempt this


def time_to_produce_all(
    production: PassiveProduction, materials: dict[str, int]
) -> dict[str, timedelta]:
    """Map each passively-produced material to the time to generate its amount.

    Materials that nothing produces are omitted, so the result holds only those
    a craft could source by waiting rather than crafting.
    """
    times: dict[str, timedelta] = {}
    for item, quantity in materials.items():
        duration = production.time_to_produce(item, quantity)
        if duration is not None:
            times[item] = duration
    return times


@dataclass
class PassiveUse:
    """A quantity of an item sourced from passive production, and the wait.

    ``time`` is how long that ``quantity`` takes to generate; it never exceeds
    the passive limit, since any shortfall beyond the limit is crafted instead.
    """

    item: str
    quantity: int
    time: timedelta


def total_time(uses: list[PassiveUse]) -> timedelta:
    """The wait for a set of passive uses, which run concurrently (the longest)."""
    return max((use.time for use in uses), default=timedelta())


_DURATION_RE = re.compile(r"(\d+)\s*([ydhms])")


def parse_duration(text: str) -> timedelta:
    """Parse a duration like ``"5d"``, ``"3h"``, ``"90m"``, ``"3s"`` or ``"1d6h30m"``.

    Units are ``y`` (years, of :data:`DAYS_PER_YEAR` days), ``d`` (days), ``h``
    (hours), ``m`` (minutes) and ``s`` (seconds); years and days both feed the day
    count, matching what :func:`format_duration` prints.
    """
    cleaned = text.strip().lower()
    matches = _DURATION_RE.findall(cleaned)
    # Reject anything with leftover characters (e.g. "5x" or "abc").
    if not matches or _DURATION_RE.sub("", cleaned).strip():
        raise ValueError(f"invalid duration: {text!r}")

    days = hours = minutes = seconds = 0
    for value, unit in matches:
        if unit == "y":
            days += int(value) * DAYS_PER_YEAR
        elif unit == "d":
            days += int(value)
        elif unit == "h":
            hours += int(value)
        elif unit == "m":
            minutes += int(value)
        else:
            seconds += int(value)
    return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


DAYS_PER_YEAR = 365


def format_duration(duration: timedelta) -> str:
    """Render a duration as ``"5d 3h 20m"``, dropping zero components.

    A year or longer is shown as years and days (e.g. ``"2y 40d"``), dropping
    the finer hours and minutes since they are noise at that scale.
    """
    minutes_total = int(duration.total_seconds() + 30) // 60  # round to minutes
    days, rem = divmod(minutes_total, 24 * 60)
    hours, minutes = divmod(rem, 60)
    if days >= DAYS_PER_YEAR:
        years, days = divmod(days, DAYS_PER_YEAR)
        parts = [f"{years}y"]
        if days:
            parts.append(f"{days}d")
        return " ".join(parts)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)
