"""Parsers for text pasted out of FarmRPG.

FarmRPG lets you copy your mastery and inventory screens as plain text. Both are
laid out as blocks of lines separated by blank lines. This module turns those
pastes (and the bundled tower CSV) into the data model defined in
:mod:`farm_solver`.
"""

from __future__ import annotations

import csv
import io
import re

from crafting import Workshop, WorkshopRecipe
from farm_solver import (
    Inventory,
    InventoryItem,
    Mastery,
    TowerRequirements,
)


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
    if block[0].lower().startswith("mastery in-progress"):
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


# --- Workshop --------------------------------------------------------------

# A material line looks like "5x Wood" or "200x Grapes".
_MATERIAL_RE = re.compile(r"^([\d,]+)x\s+(.+)$")

# The line after an item's name marks a real recipe; other blocks (the header,
# the "Nothing" level dividers) do not have it and are skipped.
_WORKSHOP_VISIBILITY = ("Visible in Workshop", "Hidden in Workshop")


def _parse_workshop_block(block: list[str]) -> WorkshopRecipe | None:
    """Parse a block into a WorkshopRecipe, or None if it isn't a recipe."""
    if len(block) < 2 or block[1] not in _WORKSHOP_VISIBILITY:
        return None

    materials: dict[str, int] = {}
    # Materials run from the visibility line up to the "<N> Silver" cost line.
    for line in block[2:]:
        match = _MATERIAL_RE.match(line)
        if match is None:
            break
        quantity = int(match.group(1).replace(",", ""))
        materials[match.group(2).strip()] = quantity

    return WorkshopRecipe(name=block[0], materials=materials)


def parse_workshop(text: str) -> Workshop:
    """Parse a workshop screen pasted from FarmRPG into a Workshop.

    Items are blank-line separated blocks. An optional header and the "Nothing"
    level dividers are skipped. For each item only the name and its materials
    are kept; the visibility, silver cost, XP and level lines are discarded.

    Recipes are keyed by the lower-cased item name; values keep the original.
    """
    workshop = Workshop()
    for block in _split_blocks(text):
        recipe = _parse_workshop_block(block)
        if recipe is not None:
            workshop.recipes[recipe.name.lower()] = recipe
    return workshop


# --- Tower -----------------------------------------------------------------


def parse_tower_requirements(text: str) -> TowerRequirements:
    """Parse a tower-requirements CSV into a TowerRequirements.

    Each row is ``level, item, mastery quantity``. The first line may be an
    optional header. Mastery requirements only start at level 201, so rows
    without an item and quantity (e.g. lower levels) are ignored.
    """
    rows = list(csv.reader(io.StringIO(text)))
    # Drop an optional header: a first row whose level column isn't a number.
    if rows and rows[0] and not rows[0][0].strip().isdigit():
        rows = rows[1:]

    levels: dict[int, dict[str, int]] = {}
    for row in rows:
        if not row or not row[0].strip():
            continue
        if len(row) < 3:
            continue  # no mastery requirement on this row
        item = row[1].strip()
        # A comma-grouped quantity (e.g. 1,000,000) is split across the trailing
        # columns by the CSV reader; rejoin them before stripping the commas.
        quantity_str = "".join(cell.strip() for cell in row[2:]).replace(",", "")
        if not item or not quantity_str:
            continue
        level = int(row[0].strip())
        quantity = int(quantity_str)
        levels.setdefault(level, {})[item] = quantity

    return TowerRequirements(levels=levels)


# --- Item specs ------------------------------------------------------------


# FarmRPG mastery-tier shorthands (case-insensitive standalone tokens):
# Mastery, Grand Mastery, Mega Mastery.
_MASTERY_TOKENS = {"M": 10_000, "GM": 100_000, "MM": 1_000_000}


def parse_amount(text: str) -> int:
    """Parse an amount, accepting FarmRPG shorthands (case-insensitively).

    Standalone tier tokens ``M``/``GM``/``MM`` mean 10K/100K/1M; a trailing
    ``k`` multiplies the leading number by 1,000 and ``m`` by 1,000,000. A plain
    number is taken as-is. Fractional prefixes are allowed (``1.5m`` -> 1,500,000).
    """
    text = text.strip()
    upper = text.upper()
    if upper in _MASTERY_TOKENS:
        return _MASTERY_TOKENS[upper]
    multiplier = 1
    if upper.endswith("K"):
        multiplier, text = 1_000, text[:-1]
    elif upper.endswith("M"):
        multiplier, text = 1_000_000, text[:-1]
    return round(float(text) * multiplier)


def parse_item_amounts(spec: str) -> dict[str, int]:
    """Parse a ``"item:amount,item:amount"`` spec into a mapping.

    Used by both the ``-i`` (acquire) and ``-m`` (master) options. Amounts accept
    the shorthands documented on :func:`parse_amount`.
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
            result[item.strip()] = parse_amount(amount)
        except ValueError as exc:
            raise ValueError(f"invalid amount in {chunk!r}") from exc
    return result
