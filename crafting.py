"""Crafting logic for Farm Solver.

Turns the item shortfalls found by the solver into the workshop recipes, crafts
and materials needed to cover them, applying the crafting and mastery bonuses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from farm_solver import ItemNeed


@dataclass
class WorkshopRecipe:
    """How to craft an item: the materials it consumes, keyed by material name."""

    name: str
    materials: dict[str, int] = field(default_factory=dict)


@dataclass
class Workshop:
    """Workshop recipes, keyed by the lower-cased item name."""

    recipes: dict[str, WorkshopRecipe] = field(default_factory=dict)

    def get(self, item: str) -> WorkshopRecipe | None:
        return self.recipes.get(item.lower())


# Bundled workshop file, used when the user does not supply their own.
DEFAULT_WORKSHOP_PATH = Path(__file__).with_name("workshop.txt")

DEFAULT_CRAFTING_BONUS = 1.45
DEFAULT_MASTERY_BONUS = 1.1

# Materials the game supplies automatically when a craft needs them, so they
# are never reported, gathered, or expanded (keys are lower-cased).
IGNORED_MATERIALS = frozenset({"iron", "nails"})


def load_workshop(path: Path | None = None) -> Workshop:
    """Load workshop recipes from ``path``, or the bundled file if None."""
    from parsing import parse_workshop

    source = path if path is not None else DEFAULT_WORKSHOP_PATH
    return parse_workshop(source.read_text())


def _round_to_nearest(value: float) -> int:
    """Round a non-negative value to the nearest integer (ties round up)."""
    return int(value + 0.5)


def _shortfalls(needs: list[ItemNeed]) -> dict[str, tuple[int, bool]]:
    """Each item's largest shortfall, and whether it is a mastery shortfall.

    An item required at several tower levels appears once, at its largest
    shortfall, since crafting is cumulative. Shared by :func:`required_materials`
    and :func:`top_level_demand`.
    """
    shortfalls: dict[str, tuple[int, bool]] = {}
    for need in needs:
        previous = shortfalls.get(need.item, (0, need.is_mastery))
        shortfalls[need.item] = (max(previous[0], need.extra), need.is_mastery)
    return shortfalls


def required_materials(
    needs: list[ItemNeed],
    workshop: Workshop,
    crafting_bonus: float = DEFAULT_CRAFTING_BONUS,
    mastery_bonus: float = DEFAULT_MASTERY_BONUS,
    recursive: bool = False,
) -> dict[str, int]:
    """Total materials to craft the shortfall of each needed, craftable item.

    The crafting bonus is the average output per craft. For a mastery shortfall
    each crafted item also grants the mastery bonus, so the items needed to cover
    a shortfall of ``n`` is ``n / mastery_bonus``, taking ``n / mastery_bonus /
    crafting_bonus`` crafts; a plain item-count shortfall (``-i``) needs ``n``
    items and ``n / crafting_bonus`` crafts. An item required at several tower
    levels is only counted once, at its largest shortfall, since crafting is
    cumulative. Items with no workshop recipe (gathered, not crafted) are skipped.

    With ``recursive`` set, a material that is itself craftable is expanded into
    its own materials, all the way down to the base (uncraftable) ones. The
    mastery bonus applies only to an item's own mastery shortfall, never to the
    quantity consumed as a material by another craft. Counts are kept as floats
    through the tree and only the final totals are rounded, so rounding errors do
    not accumulate. The non-recursive path rounds each item's crafts as before.
    """
    shortfalls = _shortfalls(needs)

    if recursive:
        return _required_materials_recursive(
            shortfalls, workshop, crafting_bonus, mastery_bonus
        )

    materials: dict[str, int] = {}
    for item, (count, is_mastery) in shortfalls.items():
        recipe = workshop.get(item)
        if recipe is None:
            continue
        divisor = crafting_bonus * mastery_bonus if is_mastery else crafting_bonus
        crafts = _round_to_nearest(count / divisor)
        for material, quantity in recipe.materials.items():
            if material.lower() in IGNORED_MATERIALS:
                continue
            materials[material] = materials.get(material, 0) + crafts * quantity
    return materials


def _required_materials_recursive(
    shortfalls: dict[str, tuple[int, bool]],
    workshop: Workshop,
    crafting_bonus: float,
    mastery_bonus: float,
) -> dict[str, int]:
    """Expand every craftable need down to its base materials (see above)."""
    totals: dict[str, float] = {}

    def expand(item: str, items_needed: float) -> None:
        if item.lower() in IGNORED_MATERIALS:
            return  # supplied automatically; never gathered
        recipe = workshop.get(item)
        if recipe is None:
            # A base material: nothing to craft, so record the quantity needed.
            totals[item] = totals.get(item, 0.0) + items_needed
            return
        crafts = items_needed / crafting_bonus
        for material, quantity in recipe.materials.items():
            # The quantity consumed as a material gets no mastery bonus.
            expand(material, crafts * quantity)

    for item, (count, is_mastery) in shortfalls.items():
        if workshop.get(item) is None:
            continue
        # The mastery bonus reduces only an item's own mastery shortfall.
        items_needed = count / mastery_bonus if is_mastery else count
        expand(item, float(items_needed))

    return {material: _round_to_nearest(qty) for material, qty in totals.items()}


def recipe_flows(workshop: Workshop) -> dict[str, dict[str, float]]:
    """Each recipe as ``lower-cased item -> {lower-cased material: quantity}``.

    Auto-supplied materials (iron, nails) are stripped, since they are never
    gathered or counted. This is the crafting the sourcing optimiser folds into
    its LP (see :mod:`optimizer`), so it can choose to craft an item instead of
    gathering it.
    """
    flows: dict[str, dict[str, float]] = {}
    for key, recipe in workshop.recipes.items():
        flows[key] = {
            material.lower(): float(quantity)
            for material, quantity in recipe.materials.items()
            if material.lower() not in IGNORED_MATERIALS
        }
    return flows


def top_level_demand(
    needs: list[ItemNeed], mastery_bonus: float = DEFAULT_MASTERY_BONUS
) -> dict[str, float]:
    """The directly needed items and how many of each, for the optimiser.

    A mastery shortfall of ``n`` needs ``n / mastery_bonus`` items found, since
    each found item grants the mastery bonus; a plain item-count shortfall
    (``-i``) needs ``n``. Same-kind shortfalls for one item are cumulative --
    crafting is cumulative, and a bigger ``-m`` mastery target simply subsumes a
    smaller one (e.g. the tower's own requirement), so only the largest wins.
    But a plain item-count floor is a *different* ask from a mastery shortfall
    (holding N in inventory vs. finding N for mastery credit), so where a goal
    combines both for the same item (see ``main.run``'s ``--target`` combining)
    they add: the mastery shortfall's items plus the extra held on top. Matched
    case-insensitively, keeping each item's first-seen display name so the two
    kinds land on one entry rather than splitting by casing. The optimiser then
    works out all the crafting and gathering beneath these.
    """
    names: dict[str, str] = {}
    extras: dict[tuple[str, bool], int] = {}
    for need in needs:
        key = need.item.lower()
        names.setdefault(key, need.item)
        kind = (key, need.is_mastery)
        extras[kind] = max(extras.get(kind, 0), need.extra)

    demand: dict[str, float] = {}
    for (key, is_mastery), extra in extras.items():
        if extra <= 0:
            continue
        amount = extra / mastery_bonus if is_mastery else float(extra)
        display = names[key]
        demand[display] = demand.get(display, 0.0) + amount
    return demand

