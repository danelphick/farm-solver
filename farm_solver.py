"""Core solver logic for Farm Solver.

Farm Solver works out the cheapest way to spend resources (Arnold Palmers and
Large Nets) to reach the mastery levels needed to climb the tower in FarmRPG.

This module holds the data model and the solver entry points. The functions are
currently skeletons: they parse/validate their inputs and raise
``NotImplementedError`` where the real algorithm still needs to be filled in.
"""

from __future__ import annotations

import re
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
class Inventory:
    """Quantities of items currently held."""

    items: dict[str, int] = field(default_factory=dict)

    def amount(self, item: str) -> int:
        return self.items.get(item, 0)


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
class Solution:
    """The result of a solve: the resources required to hit the goal."""

    arnold_palmers: int = 0
    large_nets: int = 0
    notes: list[str] = field(default_factory=list)


# Built-in tower requirements. Populate this with the real FarmRPG values.
DEFAULT_TOWER_REQUIREMENTS = TowerRequirements(levels={})


# Matches a progress line such as "976,311 / 1,000,000 Progress" or the fully
# mastered form "832,681,556 / ∞ Progress".
_PROGRESS_RE = re.compile(r"^([\d,]+)\s*/\s*([\d,]+|∞)\s+Progress$")


def _split_blocks(text: str) -> list[list[str]]:
    """Split pasted text into blocks of consecutive non-blank lines."""
    blocks: list[list[str]] = []
    current: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line:
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _parse_mastery_block(block: list[str]) -> Mastery | None:
    """Parse a single block into a Mastery, or None if it is the header.

    A block is the item name, a progress line, a percentage/status line, and
    optionally a trailing tier-header line which is ignored.
    """
    # The header block starts with "Mastery In-Progress" and has no progress
    # line; skip it rather than treating it as an item.
    if block[0].startswith("Mastery In-Progress"):
        return None

    if len(block) < 2:
        raise ValueError(f"mastery block too short: {block!r}")

    match = _PROGRESS_RE.match(block[1])
    if match is None:
        raise ValueError(f"could not parse progress line: {block[1]!r}")

    progress = int(match.group(1).replace(",", ""))
    denominator = match.group(2)
    required = None if denominator == "∞" else int(denominator.replace(",", ""))

    return Mastery(item=block[0], progress=progress, required=required)


def parse_masteries(text: str) -> dict[str, Mastery]:
    """Parse masteries pasted from FarmRPG into a mapping of item -> Mastery.

    The pasted text is a header followed by blank-line separated blocks. Each
    block holds an item name, a ``"<progress> / <required> Progress"`` line, a
    percentage line, and optionally a trailing tier header which is ignored.
    """
    masteries: dict[str, Mastery] = {}
    for block in _split_blocks(text):
        mastery = _parse_mastery_block(block)
        if mastery is not None:
            masteries[mastery.item] = mastery
    return masteries


def parse_inventory(text: str) -> Inventory:
    """Parse an inventory pasted from FarmRPG into an Inventory."""
    raise NotImplementedError("parse_inventory is not implemented yet")


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
    """Solve for holding the given number of each item (counting inventory)."""
    raise NotImplementedError("solve_acquire is not implemented yet")
