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

# Materials bought from the Country Store for Silver. The solver treats them as
# free like IGNORED_MATERIALS (never gathered or expanded, and Silver is not on
# the gold axis), but unlike those they are real purchases, so the plan report
# lists how many to buy (see :func:`store_purchases`). Keys are lower-cased.
STORE_MATERIALS = frozenset({"corn seeds", "sunflower seeds"})

# Every material the solver's recipe graph leaves out.
FREE_MATERIALS = IGNORED_MATERIALS | STORE_MATERIALS


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
            if material.lower() in FREE_MATERIALS:
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
        if item.lower() in FREE_MATERIALS:
            return  # supplied automatically or bought at the store; never gathered
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

    Auto-supplied materials (iron, nails) and Country Store purchases (see
    :data:`STORE_MATERIALS`) are stripped, since they are never gathered. This
    is the crafting the sourcing optimiser folds into its LP (see
    :mod:`optimizer`), so it can choose to craft an item instead of gathering it.
    """
    flows: dict[str, dict[str, float]] = {}
    for key, recipe in workshop.recipes.items():
        flows[key] = {
            material.lower(): float(quantity)
            for material, quantity in recipe.materials.items()
            if material.lower() not in FREE_MATERIALS
        }
    return flows


def store_purchases(
    crafts: dict[str, float], workshop: Workshop
) -> dict[str, float]:
    """The :data:`STORE_MATERIALS` a plan's ``crafts`` consume, lower-cased.

    ``crafts`` is ``{lower-cased item: craft count}`` (``SourcingPlan.crafts``).
    :func:`recipe_flows` strips these materials, so the LP never sees them; this
    puts back what the crafts it chose actually use, for the report.
    """
    totals: dict[str, float] = {}
    for item, count in crafts.items():
        recipe = workshop.get(item)
        if recipe is None:
            continue
        for material, quantity in recipe.materials.items():
            key = material.lower()
            if key in STORE_MATERIALS:
                totals[key] = totals.get(key, 0.0) + count * quantity
    return totals


def top_level_demand(
    needs: list[ItemNeed], mastery_bonus: float = DEFAULT_MASTERY_BONUS
) -> dict[str, float]:
    """The directly needed items and how many of each, for the optimiser.

    A mastery shortfall of ``n`` needs ``n / mastery_bonus`` items found, since
    each found item grants the mastery bonus; a plain item-count shortfall
    (``-i``) needs ``n`` held. Both measure the same underlying action --
    finding or crafting a copy of the item -- so once converted to that common
    "items produced" unit, every need for one item (mastery or plain) takes the
    *largest*, never a sum: a found item both counts toward mastery and sits in
    inventory, so producing enough for the bigger of two combined goals (see
    ``main.run``'s ``--target`` combining) automatically clears the smaller one
    as a side effect -- a lower ``-m`` target than the tower's own is absorbed
    (mastery is cumulative), and an ``-i`` floor below what a mastery goal
    already produces needs nothing extra, but a floor *above* it takes over.
    Matched case-insensitively, keeping each item's first-seen display name so
    the two kinds land on one entry rather than splitting by casing. The
    optimiser then works out all the crafting and gathering beneath these.
    """
    names: dict[str, str] = {}
    best: dict[str, float] = {}
    for need in needs:
        if need.extra <= 0:
            continue
        key = need.item.lower()
        names.setdefault(key, need.item)
        amount = need.extra / mastery_bonus if need.is_mastery else float(need.extra)
        best[key] = max(best.get(key, 0.0), amount)
    return {names[key]: amount for key, amount in best.items()}


def non_mastery_goal_items(needs: list[ItemNeed]) -> frozenset[str]:
    """Lower-cased items demanded **only** as a plain ``-i`` count (never ``-m``).

    A trade (or any buy) may satisfy a plain ``-i`` count goal but never a mastery
    goal -- traded items earn no mastery. An item with *any* mastery need is
    excluded even if it also has a plain count need: its full demand must be
    produced (the optimiser's ``buy <= derived`` floor stays tight), which
    over-covers the count as a side effect. See :func:`optimizer.minimize`'s floor
    relaxation, which is gated on membership here.
    """
    mastery: set[str] = set()
    plain: set[str] = set()
    for need in needs:
        if need.extra <= 0:
            continue
        (mastery if need.is_mastery else plain).add(need.item.lower())
    return frozenset(plain - mastery)

