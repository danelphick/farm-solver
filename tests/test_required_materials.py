from farm_solver import (
    ItemNeed,
    Workshop,
    WorkshopRecipe,
    required_materials,
)

WORKSHOP = Workshop(
    recipes={
        "board": WorkshopRecipe(name="Board", materials={"Wood": 5}),
        "bucket": WorkshopRecipe(
            name="Bucket", materials={"Board": 5, "Iron": 1}
        ),
    }
)


def test_scales_materials_by_shortfall():
    needs = [ItemNeed(item="Board", have=0, target=10)]
    assert required_materials(needs, WORKSHOP, crafting_bonus=1.0) == {"Wood": 50}


def test_sums_materials_across_items():
    needs = [
        ItemNeed(item="Board", have=0, target=2),
        ItemNeed(item="Bucket", have=0, target=3),
    ]
    # Board: 2*5 Wood; Bucket: 3*(5 Board, 1 Iron).
    assert required_materials(needs, WORKSHOP, crafting_bonus=1.0) == {
        "Wood": 10,
        "Board": 15,
        "Iron": 3,
    }


def test_uncraftable_items_are_skipped():
    needs = [ItemNeed(item="Wood", have=0, target=100)]  # no recipe
    assert required_materials(needs, WORKSHOP, crafting_bonus=1.0) == {}


def test_item_at_multiple_levels_counted_once_at_largest():
    # The same item required at two tower levels must not double-count; crafting
    # is cumulative, so only the largest shortfall drives the materials.
    needs = [
        ItemNeed(item="Board", have=0, target=100, level=305),
        ItemNeed(item="Board", have=0, target=1000, level=320),
    ]
    assert required_materials(needs, WORKSHOP, crafting_bonus=1.0) == {"Wood": 5000}


def test_lookup_is_case_insensitive():
    needs = [ItemNeed(item="BOARD", have=0, target=4)]
    assert required_materials(needs, WORKSHOP, crafting_bonus=1.0) == {"Wood": 20}


def test_no_needs_yields_no_materials():
    assert required_materials([], WORKSHOP, crafting_bonus=1.0) == {}


def test_crafting_bonus_reduces_crafts():
    # 10 / 1.45 = 6.9 -> 7 crafts, each using 5 Wood.
    needs = [ItemNeed(item="Board", have=0, target=10)]
    assert required_materials(needs, WORKSHOP, crafting_bonus=1.45) == {"Wood": 35}


def test_crafting_bonus_rounds_crafts_to_nearest():
    # 1 / 2 = 0.5 -> rounds up to 1 craft -> 5 Wood.
    needs = [ItemNeed(item="Board", have=0, target=1)]
    assert required_materials(needs, WORKSHOP, crafting_bonus=2.0) == {"Wood": 5}


def test_default_crafting_bonus_is_1_45():
    # 145 / 1.45 = 100 crafts -> 500 Wood, when no bonus is passed.
    needs = [ItemNeed(item="Board", have=0, target=145)]
    assert required_materials(needs, WORKSHOP, mastery_bonus=1.0) == {"Wood": 500}


def test_mastery_shortfall_applies_mastery_bonus():
    # 1000 / 1.45 / 1.1 = 626.96 -> 627 crafts -> 3135 Wood.
    needs = [ItemNeed(item="Board", have=0, target=1000, is_mastery=True)]
    assert required_materials(
        needs, WORKSHOP, crafting_bonus=1.45, mastery_bonus=1.1
    ) == {"Wood": 3135}


def test_default_mastery_bonus_is_1_1():
    # 1595 / 1.0 / 1.1 = 1450 crafts -> 7250 Wood, when no mastery bonus passed.
    needs = [ItemNeed(item="Board", have=0, target=1595, is_mastery=True)]
    assert required_materials(needs, WORKSHOP, crafting_bonus=1.0) == {"Wood": 7250}


def test_acquire_shortfall_ignores_mastery_bonus():
    # is_mastery=False, so only the crafting bonus applies: 10 / 1.0 = 10.
    needs = [ItemNeed(item="Board", have=0, target=10, is_mastery=False)]
    assert required_materials(
        needs, WORKSHOP, crafting_bonus=1.0, mastery_bonus=2.0
    ) == {"Wood": 50}
