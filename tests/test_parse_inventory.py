import textwrap

import pytest

from farm_solver import InventoryItem
from parsing import inventory_overflows, parse_inventory

# Mirrors the FarmRPG paste: a leading category, items with multi-line
# descriptions and mastery-status lines, a category that trails an item block,
# and comma-grouped quantities.
SAMPLE = textwrap.dedent(
    """\

    Meals chevron_down

    Bone Broth
    Useful to cook other things
    Grand Mastered
    23,805

    Gravy Boat
    Toot toot! Here comes the gravy boat!
    26

    Fish and Chips
    Greatly increases XP earned
    from each cooking action
    22
    Items chevron_down

    Board
    The foundation of any good farm
    MAX ON HAND
    Mega Mastered
    49,420

    Iron
    A pressing need
    1,234
    """
)


def test_parses_all_items():
    inv = parse_inventory(SAMPLE)
    # Entries are keyed by the lower-cased item name.
    assert set(inv.items) == {
        "bone broth",
        "gravy boat",
        "fish and chips",
        "board",
        "iron",
    }


def test_quantity_strips_comma_grouping():
    inv = parse_inventory(SAMPLE)
    assert inv.amount("Bone Broth") == 23_805
    assert inv.amount("Board") == 49_420
    assert inv.amount("Iron") == 1_234


def test_simple_two_line_item():
    inv = parse_inventory(SAMPLE)
    assert inv.items["gravy boat"] == InventoryItem(
        name="Gravy Boat", quantity=26, category="Meals"
    )


def test_ignores_lines_between_name_and_quantity():
    # "Board" has a description, MAX ON HAND and a mastery-status line that must
    # all be skipped, leaving just name + quantity.
    inv = parse_inventory(SAMPLE)
    assert inv.items["board"] == InventoryItem(
        name="Board", quantity=49_420, category="Items"
    )


def test_leading_category_assigned():
    inv = parse_inventory(SAMPLE)
    assert inv.items["bone broth"].category == "Meals"
    assert inv.items["fish and chips"].category == "Meals"


def test_trailing_category_applies_to_following_items_not_its_own_block():
    # "Items chevron_down" trails Fish and Chips' block but belongs to the items
    # after it (Board, Iron), not to Fish and Chips.
    inv = parse_inventory(SAMPLE)
    assert inv.items["fish and chips"].category == "Meals"
    assert inv.items["board"].category == "Items"
    assert inv.items["iron"].category == "Items"


def test_category_marker_is_not_an_item():
    inv = parse_inventory(SAMPLE)
    assert "Meals chevron_down" not in inv.items
    assert "Items chevron_down" not in inv.items
    assert "Meals" not in inv.items


def test_empty_input():
    assert parse_inventory("").items == {}


def test_item_before_any_category_has_none():
    text = "Mystery\nsome flavour text\n7\n"
    inv = parse_inventory(text)
    assert inv.items["mystery"] == InventoryItem(
        name="Mystery", quantity=7, category=None
    )


def test_missing_quantity_raises():
    text = "Broken Item\nno number here\nstill nothing\n"
    with pytest.raises(ValueError, match="could not find quantity"):
        parse_inventory(text)


def test_amount_of_unknown_item_is_zero():
    inv = parse_inventory(SAMPLE)
    assert inv.amount("Nonexistent") == 0


def test_lookup_is_case_insensitive():
    inv = parse_inventory(SAMPLE)
    assert inv.amount("BONE BROTH") == 23_805
    assert inv.get("fish AND chips").name == "Fish and Chips"


def test_inventory_overflows_lists_only_items_past_the_maximum():
    inv = parse_inventory(SAMPLE)
    # Board sits exactly at the maximum (MAX ON HAND), which is not an overflow.
    assert inventory_overflows(inv, 49_420) == []
    assert [item.name for item in inventory_overflows(inv, 1_000)] == [
        "Bone Broth", "Board", "Iron",
    ]
