"""Farming source for Farm Solver.

Some materials are grown on the farm rather than gathered, crafted, or produced
passively. For now this is just a fixed set of farmed items; when a craft needs
one it is reported in its own section, like passive production.
"""

from __future__ import annotations

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
    """Whether ``item`` is grown on the farm."""
    return item.lower() in FARMING_ITEMS


def split_farmed(materials: dict[str, int]) -> tuple[dict[str, int], dict[str, int]]:
    """Partition ``materials`` into (non-farmed, farmed) by item name."""
    rest: dict[str, int] = {}
    farmed: dict[str, int] = {}
    for material, quantity in materials.items():
        (farmed if is_farmed(material) else rest)[material] = quantity
    return rest, farmed
