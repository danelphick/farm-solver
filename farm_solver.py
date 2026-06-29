"""Core solver logic for Farm Solver.

Farm Solver works out the cheapest way to spend resources (Arnold Palmers and
Large Nets) to reach the mastery levels needed to climb the tower in FarmRPG.

This module holds the data model and the solver entry points. The functions are
currently skeletons: they parse/validate their inputs and raise
``NotImplementedError`` where the real algorithm still needs to be filled in.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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

    items: dict[str, InventoryItem] = field(default_factory=dict)

    def amount(self, item: str) -> int:
        entry = self.items.get(item)
        return entry.quantity if entry is not None else 0


@dataclass
class GameState:
    """Snapshot of the player's masteries and inventory."""

    masteries: dict[str, Mastery] = field(default_factory=dict)
    inventory: Inventory = field(default_factory=Inventory)


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


@dataclass
class ItemNeed:
    """How an item stands against a goal: how many are held vs. wanted."""

    item: str
    have: int
    target: int

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


# Built-in tower requirements. Populate this with the real FarmRPG values.
DEFAULT_TOWER_REQUIREMENTS = TowerRequirements(levels={})


def parse_tower_requirements(text: str) -> TowerRequirements:
    """Parse a tower-requirements file into a TowerRequirements."""
    raise NotImplementedError("parse_tower_requirements is not implemented yet")


def parse_item_amounts(spec: str) -> dict[str, int]:
    """Parse a ``"item:amount,item:amount"`` spec into a mapping.

    Used by both the ``-i`` (acquire) and ``-m`` (master) options.
    """
    result: dict[str, int] = {}
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        item, sep, amount = chunk.rpartition(":")
        if not sep:
            raise ValueError(f"expected 'item:amount', got {chunk!r}")
        try:
            result[item.strip()] = int(amount)
        except ValueError as exc:
            raise ValueError(f"invalid amount in {chunk!r}") from exc
    return result


def solve_tower(
    state: GameState,
    requirements: TowerRequirements,
    target_level: int | None = None,
) -> Solution:
    """Solve for the resources needed to reach ``target_level`` on the tower.

    If ``target_level`` is ``None`` the highest reachable level is targeted.
    """
    raise NotImplementedError("solve_tower is not implemented yet")


def solve_mastery(state: GameState, goals: dict[str, int]) -> Solution:
    """Solve for mastering each item in ``goals`` to the given amount."""
    raise NotImplementedError("solve_mastery is not implemented yet")


def solve_acquire(state: GameState, goals: dict[str, int]) -> Solution:
    """Solve for holding the given number of each item (counting inventory).

    For now this only works out how many extra of each item are still needed,
    given what is already in the inventory. Turning that shortfall into the
    Arnold Palmers and Large Nets required is still to come.
    """
    needs = [
        ItemNeed(item=item, have=state.inventory.amount(item), target=target)
        for item, target in goals.items()
    ]
    return Solution(needs=needs)
