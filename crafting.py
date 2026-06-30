"""Crafting logic for Farm Solver.

Turns the item shortfalls found by the solver into the workshop recipes, crafts
and materials needed to cover them, applying the crafting and mastery bonuses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from farm_solver import ItemNeed
from passive import PassiveProduction, PassiveUse


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


@dataclass
class CraftPlan:
    """A passive-aware plan for covering the solver's needs.

    ``passive`` lists what is sourced from passive production (preferred over
    crafting); ``steps`` are the crafts for whatever passive could not cover.
    ``materials`` is the recursive base-material total (everything expanded down
    to uncraftable items) and ``shallow_materials`` the immediate materials of
    the directly requested items, both after passive sourcing.
    """

    steps: list[CraftStep] = field(default_factory=list)
    materials: dict[str, int] = field(default_factory=dict)
    shallow_materials: dict[str, int] = field(default_factory=dict)
    passive: list[PassiveUse] = field(default_factory=list)


def craft_plan(
    needs: list[ItemNeed],
    workshop: Workshop,
    production: PassiveProduction,
    crafting_bonus: float = DEFAULT_CRAFTING_BONUS,
    mastery_bonus: float = DEFAULT_MASTERY_BONUS,
    passive_limit: timedelta | None = None,
) -> CraftPlan:
    """Plan the crafts and passive production needed to satisfy ``needs``.

    Passive sourcing is preferred: any item a producer makes is taken from
    passive production rather than crafted. With a ``passive_limit`` only what a
    producer can make within that time is taken; the remainder is crafted (and
    its own materials expanded in turn). Without a limit, passively-produced
    items are taken entirely from production. The storehouse size limits the
    per-cycle production rate (see :class:`passive.PassiveProduction`) rather
    than the total obtainable.

    Demand is aggregated across the whole crafting tree before passive capacity
    is shared out, so a producer's limited output is never double-counted. The
    tree is walked parents-before-children; counts stay float until displayed.
    """
    # Each item's largest shortfall, plus whether it is a mastery shortfall.
    shortfalls: dict[str, tuple[int, bool]] = {}
    for need in needs:
        previous = shortfalls.get(need.item, (0, need.is_mastery))
        shortfalls[need.item] = (max(previous[0], need.extra), need.is_mastery)

    demand: dict[str, float] = {}  # lower-cased item -> total items needed
    names: dict[str, str] = {}  # lower-cased item -> display name
    top_level: set[str] = set()  # directly requested items
    for item, (count, is_mastery) in shortfalls.items():
        key = item.lower()
        items_needed = count / mastery_bonus if is_mastery else count
        demand[key] = demand.get(key, 0.0) + items_needed
        names.setdefault(key, item)
        top_level.add(key)

    # Topological order, parents before children, over every reachable item.
    visited: set[str] = set()
    finished: list[str] = []

    def visit(key: str) -> None:
        if key in visited:
            return
        visited.add(key)
        recipe = workshop.get(key)
        if recipe is not None:
            names[key] = recipe.name
            for material in recipe.materials:
                names.setdefault(material.lower(), material)
                visit(material.lower())
        finished.append(key)

    for key in list(demand):
        visit(key)
    order = list(reversed(finished))  # parents before children

    limit_minutes = (
        None if passive_limit is None else passive_limit.total_seconds() / 60
    )

    plan = CraftPlan()
    step_by_key: dict[str, CraftStep] = {}
    for key in order:
        needed = demand.get(key, 0.0)
        if needed <= 0:
            continue

        # Prefer passive: take what production can supply (within the limit).
        rate = production.rate_per_minute(key)
        if rate > 0:
            capacity = needed if limit_minutes is None else rate * limit_minutes
            from_passive = min(needed, capacity)
            if from_passive > 0:
                amount = _round_to_nearest(from_passive)
                time = production.time_to_produce(key, amount)
                assert time is not None  # rate > 0, so a producer makes it
                plan.passive.append(
                    PassiveUse(item=names[key], quantity=amount, time=time)
                )
        else:
            from_passive = 0.0
        to_make = needed - from_passive

        recipe = workshop.get(key)
        if recipe is None:
            # A base material: gather whatever passive could not cover.
            gathered = _round_to_nearest(to_make)
            if gathered > 0:
                plan.materials[names[key]] = (
                    plan.materials.get(names[key], 0) + gathered
                )
            continue

        if to_make <= 0:
            continue  # fully sourced from passive production
        crafts = to_make / crafting_bonus
        materials = [
            (material, _round_to_nearest(crafts * quantity))
            for material, quantity in recipe.materials.items()
        ]
        for material, quantity in recipe.materials.items():
            demand[material.lower()] = (
                demand.get(material.lower(), 0.0) + crafts * quantity
            )
        produced = to_make  # crafts * crafting_bonus
        step = CraftStep(
            item=names[key],
            crafts=_round_to_nearest(crafts),
            materials=materials,
            produced=_round_to_nearest(produced),
            mastery_added=_round_to_nearest(produced * mastery_bonus),
        )
        plan.steps.append(step)
        step_by_key[key] = step

    # Shallow materials: the immediate ingredients of the requested items.
    for key in top_level:
        step = step_by_key.get(key)
        if step is None:
            continue
        for material, quantity in step.materials:
            plan.shallow_materials[material] = (
                plan.shallow_materials.get(material, 0) + quantity
            )

    return plan
