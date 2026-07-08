import pytest

from crafting import top_level_demand
from farm_solver import ItemNeed


def test_mastery_shortfall_divides_by_mastery_bonus():
    needs = [ItemNeed(item="Board", have=0, target=110, is_mastery=True)]
    assert top_level_demand(needs, mastery_bonus=1.1)["Board"] == pytest.approx(100.0)


def test_plain_shortfall_ignores_mastery_bonus():
    needs = [ItemNeed(item="Board", have=0, target=10, is_mastery=False)]
    assert top_level_demand(needs, mastery_bonus=1.1) == {"Board": 10.0}


def test_item_at_multiple_tower_levels_takes_the_largest():
    needs = [
        ItemNeed(item="Board", have=0, target=100, level=201, is_mastery=True),
        ItemNeed(item="Board", have=0, target=1000, level=210, is_mastery=True),
    ]
    assert top_level_demand(needs, mastery_bonus=1.0) == {"Board": 1000.0}


def test_completed_items_are_dropped():
    needs = [ItemNeed(item="Board", have=100, target=100, is_mastery=True)]
    assert top_level_demand(needs) == {}


def test_extra_mastery_goal_lower_than_tower_is_absorbed():
    # An extra -m goal weaker than the tower's own requirement for the same
    # item changes nothing -- mastery is cumulative, so only the larger wins.
    needs = [
        ItemNeed(item="Grand Piano", have=0, target=1_000_000, is_mastery=True),
        ItemNeed(item="Grand Piano", have=0, target=500_000, is_mastery=True),
    ]
    assert top_level_demand(needs, mastery_bonus=1.0) == {"Grand Piano": 1_000_000.0}


def test_extra_mastery_goal_higher_than_tower_wins():
    needs = [
        ItemNeed(item="Grand Piano", have=0, target=500_000, is_mastery=True),
        ItemNeed(item="Grand Piano", have=0, target=1_000_000, is_mastery=True),
    ]
    assert top_level_demand(needs, mastery_bonus=1.0) == {"Grand Piano": 1_000_000.0}


def test_item_goal_adds_on_top_of_a_mastery_goal_for_the_same_item():
    # An -i floor is a different ask (held count) from a mastery shortfall
    # (items found), so the two add rather than max -- 100 mastery-driven items
    # plus 50 held on top.
    needs = [
        ItemNeed(item="Steel Wire", have=0, target=100, is_mastery=True),
        ItemNeed(item="Steel Wire", have=0, target=50, is_mastery=False),
    ]
    assert top_level_demand(needs, mastery_bonus=1.0) == {"Steel Wire": 150.0}


def test_item_goal_alone_needs_no_recipe_demand():
    needs = [ItemNeed(item="Steel Wire", have=10, target=50, is_mastery=False)]
    assert top_level_demand(needs) == {"Steel Wire": 40.0}


def test_combining_keeps_one_entry_despite_differing_casing():
    # The tower's own requirement might resolve to a different display case
    # than a user-typed -i goal for the same item; both must land on one entry
    # (the first-seen display name), not silently collide or double-count.
    needs = [
        ItemNeed(item="steel wire", have=0, target=100, is_mastery=True),
        ItemNeed(item="Steel Wire", have=0, target=50, is_mastery=False),
    ]
    result = top_level_demand(needs, mastery_bonus=1.0)
    assert result == {"steel wire": 150.0}


def test_needs_sorted_by_level_still_merge_the_same_item():
    needs = [
        ItemNeed(item="Board", have=0, target=100, level=201, is_mastery=True),
        ItemNeed(item="Bucket", have=0, target=3, is_mastery=False),
    ]
    assert top_level_demand(needs, mastery_bonus=1.0) == {
        "Board": 100.0,
        "Bucket": 3.0,
    }
