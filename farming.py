"""Farming source for Farm Solver.

Some materials are grown on the farm rather than gathered, crafted, or produced
passively. The :mod:`crops` model works out which crop grows a needed item and
how long it takes; :func:`plan_farming` uses it to pull farmable materials out of
the totals and into their own report section, like passive production. A small
fixed set of farmed items (the flowers) is not modelled by :mod:`crops`, so it is
still recognised here and reported without a time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from crops import Farm

# Materials grown on the farm (keys are lower-cased for case-insensitive
# matching).
FARMING_ITEMS = frozenset(
    {
        "beet",
        "carrot",
        "corn",
        "cotton",
        "cucumber",
        "eggplant",
        "peas",
        "peppers",
        "potato",
        "sunflower",
        "tomato",
        "watermelon",
        "pine tree",
        "blue flowers",
        "orange flowers",
        "red flowers",
        "white flowers",
        "yellow flowers",
    }
)


def is_farmed(item: str) -> bool:
    """Whether ``item`` is one of the fixed farmed items (the flowers)."""
    return item.lower() in FARMING_ITEMS


@dataclass
class FarmUse:
    """A material obtained by farming, and how long that takes.

    ``source`` is the crop to plant -- which may differ from ``item`` for a rare
    drop (``Runestone 11`` via ``Corn``) or a bulk mega seed -- and ``time`` the
    wait to grow ``quantity`` of it, harvesting and replanting with no rest. Both
    are ``None`` for a farmed item the crop model does not cover (the flowers),
    which is still grown but has no modelled time.
    """

    item: str
    quantity: int
    source: str | None = None
    time: timedelta | None = None


def plan_farming(
    materials: dict[str, int], farm: Farm
) -> tuple[list[FarmUse], dict[str, int]]:
    """Split ``materials`` into what to farm and what remains.

    An item a crop can grow becomes a :class:`FarmUse` with the fastest crop and
    its grow time; a farmed item outside the crop model (the flowers) becomes a
    timeless :class:`FarmUse`; anything else stays in the returned remainder.
    """
    uses: list[FarmUse] = []
    rest: dict[str, int] = {}
    for item, quantity in materials.items():
        best = farm.best_source(item, quantity)
        if best is not None:
            source, time = best
            uses.append(FarmUse(item, quantity, source, time))
        elif is_farmed(item):
            uses.append(FarmUse(item, quantity))
        else:
            rest[item] = quantity
    return uses, rest
