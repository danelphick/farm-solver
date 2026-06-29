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
    assert required_materials(needs, WORKSHOP) == {"Wood": 50}


def test_sums_materials_across_items():
    needs = [
        ItemNeed(item="Board", have=0, target=2),
        ItemNeed(item="Bucket", have=0, target=3),
    ]
    # Board: 2*5 Wood; Bucket: 3*(5 Board, 1 Iron).
    assert required_materials(needs, WORKSHOP) == {
        "Wood": 10,
        "Board": 15,
        "Iron": 3,
    }


def test_uncraftable_items_are_skipped():
    needs = [ItemNeed(item="Wood", have=0, target=100)]  # no recipe
    assert required_materials(needs, WORKSHOP) == {}


def test_item_at_multiple_levels_counted_once_at_largest():
    # The same item required at two tower levels must not double-count; crafting
    # is cumulative, so only the largest shortfall drives the materials.
    needs = [
        ItemNeed(item="Board", have=0, target=100, level=305),
        ItemNeed(item="Board", have=0, target=1000, level=320),
    ]
    assert required_materials(needs, WORKSHOP) == {"Wood": 5000}


def test_lookup_is_case_insensitive():
    needs = [ItemNeed(item="BOARD", have=0, target=4)]
    assert required_materials(needs, WORKSHOP) == {"Wood": 20}


def test_no_needs_yields_no_materials():
    assert required_materials([], WORKSHOP) == {}
