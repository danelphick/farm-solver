import pytest

from crafting import non_mastery_goal_items, top_level_demand
from farm_solver import ItemNeed


def test_non_mastery_goal_items_are_the_plain_i_goals():
    needs = [
        ItemNeed(item="Red Dye", have=0, target=5000, is_mastery=False),  # plain -i
        ItemNeed(item="Board", have=0, target=100, is_mastery=True),  # mastery
    ]
    assert non_mastery_goal_items(needs) == frozenset({"red dye"})


def test_an_item_with_any_mastery_need_is_excluded():
    # Both a plain count and a mastery need on one item -> must be produced, so it
    # is NOT trade-satisfiable (excluded from the set).
    needs = [
        ItemNeed(item="Board", have=0, target=100, is_mastery=False),
        ItemNeed(item="Board", have=0, target=500, is_mastery=True),
    ]
    assert non_mastery_goal_items(needs) == frozenset()


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


def test_item_goal_below_a_mastery_goal_is_absorbed():
    # A found item counts toward mastery *and* sits in inventory, so the 100
    # items the mastery goal already produces (mastery_bonus=1.0) clear the
    # smaller 50-item -i floor as a side effect -- no extra demand.
    needs = [
        ItemNeed(item="Steel Wire", have=0, target=100, is_mastery=True),
        ItemNeed(item="Steel Wire", have=0, target=50, is_mastery=False),
    ]
    assert top_level_demand(needs, mastery_bonus=1.0) == {"Steel Wire": 100.0}


def test_item_goal_above_a_mastery_goal_takes_over():
    # The -i floor (200) now needs more items than the mastery goal (100) --
    # it takes over, and the mastery goal is cleared as a side effect.
    needs = [
        ItemNeed(item="Steel Wire", have=0, target=100, is_mastery=True),
        ItemNeed(item="Steel Wire", have=0, target=200, is_mastery=False),
    ]
    assert top_level_demand(needs, mastery_bonus=1.0) == {"Steel Wire": 200.0}


def test_mastery_goal_converted_by_bonus_before_comparing_to_item_goal():
    # mastery_bonus=1.1: the 11001 mastery shortfall needs 11001/1.1 = 10001
    # items found, just over the 10000-item -i floor, so mastery takes over.
    needs = [
        ItemNeed(item="Board", have=0, target=11001, is_mastery=True),
        ItemNeed(item="Board", have=0, target=10000, is_mastery=False),
    ]
    assert top_level_demand(needs, mastery_bonus=1.1)["Board"] == pytest.approx(
        10000.909090909091
    )


def test_item_goal_above_the_converted_mastery_goal_takes_over():
    # mastery_bonus=1.1: a 5000 mastery shortfall needs only 5000/1.1 = 4545.45
    # items found, well under the 10000-item -i floor, so the floor takes over
    # and the mastery goal is cleared as a by-product.
    needs = [
        ItemNeed(item="Board", have=0, target=5000, is_mastery=True),
        ItemNeed(item="Board", have=0, target=10000, is_mastery=False),
    ]
    assert top_level_demand(needs, mastery_bonus=1.1) == {"Board": 10000.0}


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
    assert result == {"steel wire": 100.0}


def test_needs_sorted_by_level_still_merge_the_same_item():
    needs = [
        ItemNeed(item="Board", have=0, target=100, level=201, is_mastery=True),
        ItemNeed(item="Bucket", have=0, target=3, is_mastery=False),
    ]
    assert top_level_demand(needs, mastery_bonus=1.0) == {
        "Board": 100.0,
        "Bucket": 3.0,
    }
