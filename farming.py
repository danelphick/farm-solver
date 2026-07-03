"""Farmed items for Farm Solver.

Most farmed items are modelled by :mod:`crops` (growth times and per-seed drops)
and sourced through the optimiser like any other channel. A small fixed set (the
flowers) is not in the crop model but is still farmed; :data:`FARMING_ITEMS` and
:func:`is_farmed` record those so they still count as obtainable (see
``main.obtainable_items``).
"""

from __future__ import annotations

# Materials grown on the farm (keys are lower-cased for case-insensitive
# matching), including flowers the crop model does not otherwise cover.
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
