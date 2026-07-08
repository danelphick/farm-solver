"""Core solver logic for Farm Solver.

Farm Solver works out the cheapest way to spend resources (Arnold Palmers and
Large Nets) to reach the mastery levels needed to climb the tower in FarmRPG.

This module holds the data model (masteries, inventory, tower requirements) and
the solver entry points. Text parsing lives in :mod:`parsing` and the crafting
calculations in :mod:`crafting`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Item names differ in capitalisation between FarmRPG screens and tower.csv
# (e.g. "Essence of Slime" vs "Essence Of Slime"). To match them reliably,
# masteries and inventory are always keyed by the lower-cased name, and lookups
# go through the helpers below which lower-case the query.


@dataclass
class Mastery:
    """A single item's mastery progress.

    ``progress`` is the current count towards the next mastery threshold and
    ``required`` is that threshold (the denominator shown in FarmRPG). When an
    item is fully mastered FarmRPG shows ``∞`` as the denominator, which we
    represent with ``required = None``.
    """

    item: str
    progress: int = 0
    required: int | None = None

    @property
    def mastered(self) -> bool:
        """True once the item has nothing left to master (``∞`` denominator)."""
        return self.required is None

    @property
    def tier(self) -> int:
        """Current mastery tier, derived from the threshold.

        FarmRPG thresholds are powers of ten: 10 (No Tier), 100 (Tier I),
        1,000 (II), 10,000 (III), 100,000 (IV), 1,000,000 (V). A fully
        mastered item (``∞``) sits one tier above the last, at 6.
        """
        if self.required is None:
            return 6
        return len(str(self.required)) - 2


@dataclass
class InventoryItem:
    """A single inventory entry: how many are held and its FarmRPG category."""

    name: str
    quantity: int = 0
    category: str | None = None


@dataclass
class Inventory:
    """Items currently held, keyed by item name."""

    # Keyed by the lower-cased item name; values keep the original-case name.
    items: dict[str, InventoryItem] = field(default_factory=dict)

    def get(self, item: str) -> InventoryItem | None:
        return self.items.get(item.lower())

    def amount(self, item: str) -> int:
        entry = self.get(item)
        return entry.quantity if entry is not None else 0


@dataclass
class GameState:
    """Snapshot of the player's masteries and inventory."""

    # ``masteries`` is keyed by the lower-cased item name; values keep the
    # original-case name.
    masteries: dict[str, Mastery] = field(default_factory=dict)
    inventory: Inventory = field(default_factory=Inventory)

    def mastery(self, item: str) -> Mastery | None:
        return self.masteries.get(item.lower())


@dataclass
class TowerRequirements:
    """Mastery levels required for each tower level.

    ``levels`` maps a tower level to the set of mastery requirements needed to
    climb to it.
    """

    levels: dict[int, dict[str, int]] = field(default_factory=dict)

    @property
    def max_level(self) -> int:
        return max(self.levels, default=0)

    def requirements_through(self, level: int) -> list[tuple[int, str, int]]:
        """Every ``(level, item, quantity)`` requirement up to ``level``.

        Returned in tower order (by level, then item). An item demanded at
        several levels appears once per level, so each distinct requirement is
        listed separately rather than collapsed into one.
        """
        result: list[tuple[int, str, int]] = []
        for lvl in sorted(self.levels):
            if lvl > level:
                continue
            for item in sorted(self.levels[lvl]):
                result.append((lvl, item, self.levels[lvl][item]))
        return result


@dataclass
class ItemNeed:
    """How an item stands against a goal: how many are held vs. wanted.

    ``level`` is the tower level that imposes the requirement, or None for the
    ``-m``/``-i`` goals which are not tied to a tower level. ``is_mastery`` marks
    whether the shortfall is measured in mastery (tower, ``-m``) rather than a
    plain item count (``-i``); only mastery shortfalls earn the mastery bonus.
    """

    item: str
    have: int
    target: int
    level: int | None = None
    is_mastery: bool = False

    @property
    def extra(self) -> int:
        """Additional units still needed to reach the target (never negative)."""
        return max(0, self.target - self.have)


@dataclass
class Solution:
    """The result of a solve: the resources required to hit the goal."""

    arnold_palmers: int = 0
    large_nets: int = 0
    needs: list[ItemNeed] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# Bundled tower file, used when the user does not supply their own.
DEFAULT_TOWER_PATH = Path(__file__).with_name("tower.csv")


def load_tower_requirements(path: Path | None = None) -> TowerRequirements:
    """Load tower requirements from ``path``, or the bundled file if None."""
    from parsing import parse_tower_requirements

    source = path if path is not None else DEFAULT_TOWER_PATH
    return parse_tower_requirements(source.read_text())


def solve_tower(
    state: GameState,
    requirements: TowerRequirements,
    target_level: int | None = None,
) -> Solution:
    """Solve for the resources needed to reach ``target_level`` on the tower.

    If ``target_level`` is ``None`` the highest level in ``requirements`` is
    targeted. For now this reports how much more mastery each item still needs,
    skipping items whose requirement is already met.
    """
    if target_level is None:
        target_level = requirements.max_level

    needs = []
    for level, item, quantity in requirements.requirements_through(target_level):
        mastery = state.mastery(item)
        have = mastery.progress if mastery is not None else 0
        name = mastery.item if mastery is not None else item
        need = ItemNeed(
            item=name, have=have, target=quantity, level=level, is_mastery=True
        )
        if need.extra > 0:
            needs.append(need)

    needs.sort(key=lambda need: (need.level, need.item))
    return Solution(needs=needs)


def has_tower_level(
    state: GameState, requirements: TowerRequirements, level: int
) -> bool:
    """Whether ``state`` already meets every mastery requirement through ``level``.

    There is no direct record of which tower level a player has reached, so this
    stands in for it: a shortfall anywhere through ``level`` (see
    :func:`solve_tower`) means it hasn't been cleared. Used to auto-detect
    perks/rewards a specific level grants, e.g. the Scythe of Dewstar at level
    250 (see ``main.resolve_scythe_of_dewstar``).
    """
    return not solve_tower(state, requirements, level).needs


def solve_mastery(state: GameState, goals: dict[str, int]) -> Solution:
    """Solve for mastering each item in ``goals`` to the given amount.

    For now this only works out how much more mastery progress each item still
    needs, given its current progress. Turning that shortfall into the Arnold
    Palmers and Large Nets required is still to come.
    """
    needs = []
    for item, target in goals.items():
        mastery = state.mastery(item)
        have = mastery.progress if mastery is not None else 0
        name = mastery.item if mastery is not None else item
        needs.append(
            ItemNeed(item=name, have=have, target=target, is_mastery=True)
        )
    return Solution(needs=needs)


def solve_goals(
    state: GameState,
    mastery_goals: dict[str, int] | None = None,
    item_goals: dict[str, int] | None = None,
) -> Solution:
    """Solve ``-m``/``-i`` goals together, with no tower goal at all.

    Used without ``--target`` (see ``main.run``): unlike before, ``-m`` and
    ``-i`` may now both be given at once, each simply contributing its own
    need list -- ``crafting.top_level_demand`` does the per-item combining
    (see :func:`solve_target`).
    """
    needs: list[ItemNeed] = []
    if mastery_goals:
        needs += solve_mastery(state, mastery_goals).needs
    if item_goals:
        needs += solve_acquire(state, item_goals).needs
    return Solution(needs=needs)


def solve_target(
    state: GameState,
    requirements: TowerRequirements,
    target_level: int | None = None,
    mastery_goals: dict[str, int] | None = None,
    item_goals: dict[str, int] | None = None,
) -> Solution:
    """Solve the tower goal, plus any extra ``-m``/``-i`` goals layered on top.

    Used for ``--target`` (see ``main.run``): the tower's own mastery needs
    (:func:`solve_tower`) are combined with :func:`solve_goals`'s optional
    extra ``-m``/``-i`` need list, simply by concatenation --
    ``crafting.top_level_demand`` is what reduces same-item needs to one
    demand figure, taking the largest (never adding): a found item both counts
    toward mastery and sits in inventory, so whichever goal for an item needs
    the most items produced clears the others as a side effect (a lower ``-m``
    target than the tower's own is absorbed; an ``-i`` floor only takes over
    once it demands more items than the mastery goal already produces).
    """
    needs = list(solve_tower(state, requirements, target_level).needs)
    needs += solve_goals(state, mastery_goals, item_goals).needs
    return Solution(needs=needs)


def solve_acquire(state: GameState, goals: dict[str, int]) -> Solution:
    """Solve for holding the given number of each item (counting inventory).

    For now this only works out how many extra of each item are still needed,
    given what is already in the inventory. Turning that shortfall into the
    Arnold Palmers and Large Nets required is still to come.
    """
    needs = []
    for item, target in goals.items():
        entry = state.inventory.get(item)
        have = entry.quantity if entry is not None else 0
        name = entry.name if entry is not None else item
        needs.append(ItemNeed(item=name, have=have, target=target))
    return Solution(needs=needs)
