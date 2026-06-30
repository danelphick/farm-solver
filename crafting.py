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


@dataclass
class CraftStep:
    """One craftable item's contribution to the crafting plan.

    ``crafts`` is how many times the item must be crafted and ``materials`` are
    the ``(name, quantity)`` pairs that those crafts consume. ``produced`` is how
    many of the item those crafts yield (crafts times the crafting bonus) and
    ``mastery_added`` is the mastery that yields (produced times the mastery
    bonus).
    """

    item: str
    crafts: int
    materials: list[tuple[str, int]] = field(default_factory=list)
    produced: int = 0
    mastery_added: int = 0


# Bundled workshop file, used when the user does not supply their own.
DEFAULT_WORKSHOP_PATH = Path(__file__).with_name("workshop.txt")

DEFAULT_CRAFTING_BONUS = 1.45
DEFAULT_MASTERY_BONUS = 1.1


def load_workshop(path: Path | None = None) -> Workshop:
    """Load workshop recipes from ``path``, or the bundled file if None."""
    from parsing import parse_workshop

    source = path if path is not None else DEFAULT_WORKSHOP_PATH
    return parse_workshop(source.read_text())


def _round_to_nearest(value: float) -> int:
    """Round a non-negative value to the nearest integer (ties round up)."""
    return int(value + 0.5)


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
    # Each item's largest shortfall, plus whether it is a mastery shortfall.
    shortfalls: dict[str, tuple[int, bool]] = {}
    for need in needs:
        previous = shortfalls.get(need.item, (0, need.is_mastery))
        shortfalls[need.item] = (max(previous[0], need.extra), need.is_mastery)

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


def craft_breakdown(
    needs: list[ItemNeed],
    workshop: Workshop,
    crafting_bonus: float = DEFAULT_CRAFTING_BONUS,
    mastery_bonus: float = DEFAULT_MASTERY_BONUS,
) -> list[CraftStep]:
    """Every craft needed to satisfy ``needs``, expanded through the whole tree.

    Each craftable item gets one :class:`CraftStep` giving the total number of
    crafts and the materials those crafts consume, with quantities aggregated
    across every place the item is used. Bonuses follow the same rules as
    :func:`required_materials`: the mastery bonus reduces only an item's own
    mastery shortfall, never the amount consumed as a material. Counts are kept
    as floats through the tree and only the displayed numbers are rounded. Steps
    are listed parents-before-children, with base (uncraftable) materials only
    appearing inside a step's material list, never as a step of their own.
    """
    # Each item's largest shortfall, plus whether it is a mastery shortfall.
    shortfalls: dict[str, tuple[int, bool]] = {}
    for need in needs:
        previous = shortfalls.get(need.item, (0, need.is_mastery))
        shortfalls[need.item] = (max(previous[0], need.extra), need.is_mastery)

    demand: dict[str, float] = {}  # lower-cased item -> total items needed
    names: dict[str, str] = {}  # lower-cased item -> display name
    order: list[str] = []  # craftable items, in first-seen (parents-first) order

    def expand(item: str, items_needed: float) -> None:
        recipe = workshop.get(item)
        if recipe is None:
            return  # base material: shown only where it is consumed
        key = item.lower()
        if key not in demand:
            names[key] = recipe.name
            order.append(key)
        demand[key] = demand.get(key, 0.0) + items_needed
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

    steps: list[CraftStep] = []
    for key in order:
        recipe = workshop.get(key)
        assert recipe is not None  # only craftable items are recorded
        crafts = demand[key] / crafting_bonus
        materials = [
            (material, _round_to_nearest(crafts * quantity))
            for material, quantity in recipe.materials.items()
        ]
        produced = crafts * crafting_bonus  # == demand[key]
        steps.append(
            CraftStep(
                item=names[key],
                crafts=_round_to_nearest(crafts),
                materials=materials,
                produced=_round_to_nearest(produced),
                mastery_added=_round_to_nearest(produced * mastery_bonus),
            )
        )
    return steps
