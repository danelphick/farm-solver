"""Parsers for text pasted out of FarmRPG.

FarmRPG lets you copy your mastery and inventory screens as plain text. Both are
laid out as blocks of lines separated by blank lines. This module turns those
pastes into the data model defined in :mod:`farm_solver`.
"""

from __future__ import annotations

import re

from farm_solver import Inventory, InventoryItem, Mastery


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


# --- Masteries -------------------------------------------------------------

# Matches a progress line such as "976,311 / 1,000,000 Progress" or the fully
# mastered form "832,681,556 / ∞ Progress".
_PROGRESS_RE = re.compile(r"^([\d,]+)\s*/\s*([\d,]+|∞)\s+Progress$")


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

    Entries are keyed by the lower-cased item name so they can be matched
    against tower requirements regardless of capitalisation.
    """
    masteries: dict[str, Mastery] = {}
    for block in _split_blocks(text):
        mastery = _parse_mastery_block(block)
        if mastery is not None:
            masteries[mastery.item.lower()] = mastery
    return masteries


# --- Inventory -------------------------------------------------------------

# A category header always ends in " chevron_down", e.g. "Items chevron_down".
_CATEGORY_SUFFIX = " chevron_down"

# A quantity line is just digits, optionally with thousands separators.
_QUANTITY_RE = re.compile(r"^[\d,]+$")


def _parse_inventory_block(block: list[str], category: str | None) -> InventoryItem:
    """Parse an item block into an InventoryItem.

    The first line is the item name and the last line is the quantity; every
    line in between (description, MAX ON HAND, mastery status, ...) is ignored.
    """
    name = block[0]
    quantity_line = block[-1]
    if not _QUANTITY_RE.match(quantity_line):
        raise ValueError(f"could not find quantity in block: {block!r}")
    quantity = int(quantity_line.replace(",", ""))
    return InventoryItem(name=name, quantity=quantity, category=category)


def parse_inventory(text: str) -> Inventory:
    """Parse an inventory pasted from FarmRPG into an Inventory.

    Items are blank-line separated blocks. A category header (``"<name>
    chevron_down"``) either stands alone on its own block or trails the last
    item of the previous block; in both cases it names the category for the
    items that *follow* it, not the item it shares a block with.

    Entries are keyed by the lower-cased item name; values keep the original.
    """
    inventory = Inventory()
    category: str | None = None

    for block in _split_blocks(text):
        next_category: str | None = None
        if block[-1].endswith(_CATEGORY_SUFFIX):
            marker = block.pop()
            next_category = marker[: -len(_CATEGORY_SUFFIX)].strip()

        if block:
            item = _parse_inventory_block(block, category)
            inventory.items[item.name.lower()] = item

        if next_category is not None:
            category = next_category

    return inventory
