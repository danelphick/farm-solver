"""Passive production for Farm Solver.

Some FarmRPG buildings generate items on a fixed cadence (every ten minutes,
hourly, or daily) without being crafted. This module models those producers so
the solver can work out how long passive generation would take to supply a
material instead of crafting it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import timedelta

# Cadences at which the game tops up passive producers.
MINUTE = timedelta(minutes=1)
TEN_MINUTES = timedelta(minutes=10)
HOURLY = timedelta(hours=1)
DAILY = timedelta(days=1)


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

    def rate_per_minute(self, item: str) -> float:
        """How many of ``item`` are produced per minute across all producers."""
        key = item.lower()
        total = 0.0
        for producer in self.producers:
            quantity = producer.outputs.get(key)
            if quantity:
                per_cycle = quantity
                if self.inventory_size is not None:
                    per_cycle = min(quantity, self.inventory_size)
                total += per_cycle / (producer.period.total_seconds() / 60)
        return total

    def produces(self, item: str) -> bool:
        """Whether anything generates ``item`` passively."""
        return self.rate_per_minute(item) > 0

    def time_to_produce(self, item: str, quantity: int) -> timedelta | None:
        """Time to passively generate ``quantity`` of ``item``.

        With a single producer the wait is rounded up to a whole number of its
        periods, since production arrives in whole cycles and you cannot collect
        part of one. When several producers make the item (e.g. the main run
        plus an offset bonus deposit like the raptor snare or orchard shaker),
        the wait falls back to their combined rate and is *not* clamped to a
        whole cycle -- so such items can report a fraction of a period.
        Returns ``None`` when nothing produces the item.
        """
        key = item.lower()
        sources = [
            producer for producer in self.producers if producer.outputs.get(key)
        ]
        if not sources:
            return None
        if len(sources) == 1:
            producer = sources[0]
            per_cycle = producer.outputs[key]
            if self.inventory_size is not None:
                per_cycle = min(per_cycle, self.inventory_size)
            if per_cycle <= 0:
                return None
            return math.ceil(quantity / per_cycle) * producer.period
        # Several producers make it, possibly on different cadences; fall back
        # to the combined rate. There is no single period to round up to here,
        # so unlike the single-producer case the result is not clamped to a
        # whole cycle and may show a fraction of a period.
        rate = self.rate_per_minute(item)
        if rate <= 0:
            return None
        return timedelta(minutes=quantity / rate)


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


_DURATION_RE = re.compile(r"(\d+)\s*([dhm])")


def parse_duration(text: str) -> timedelta:
    """Parse a duration like ``"5d"``, ``"3h"``, ``"90m"`` or ``"1d6h30m"``.

    Units are ``d`` (days), ``h`` (hours) and ``m`` (minutes).
    """
    cleaned = text.strip().lower()
    matches = _DURATION_RE.findall(cleaned)
    # Reject anything with leftover characters (e.g. "5x" or "abc").
    if not matches or _DURATION_RE.sub("", cleaned).strip():
        raise ValueError(f"invalid duration: {text!r}")

    days = hours = minutes = 0
    for value, unit in matches:
        if unit == "d":
            days += int(value)
        elif unit == "h":
            hours += int(value)
        else:
            minutes += int(value)
    return timedelta(days=days, hours=hours, minutes=minutes)


def format_duration(duration: timedelta) -> str:
    """Render a duration as ``"5d 3h 20m"``, dropping zero components."""
    minutes_total = int(duration.total_seconds() + 30) // 60  # round to minutes
    days, rem = divmod(minutes_total, 24 * 60)
    hours, minutes = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)
