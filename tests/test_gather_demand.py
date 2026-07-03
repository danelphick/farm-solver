from crafting import Workshop, WorkshopRecipe, gather_demand, required_materials
from farm_solver import ItemNeed

WORKSHOP = Workshop(
    recipes={
        "board": WorkshopRecipe(name="Board", materials={"Wood": 5}),
        "bucket": WorkshopRecipe(name="Bucket", materials={"Board": 5, "Iron": 2}),
    }
)


def test_without_gatherables_matches_recursive_required_materials():
    needs = [ItemNeed(item="Bucket", have=0, target=10)]
    # Nothing gatherable: expand all the way to base Wood, like recursive.
    assert gather_demand(needs, WORKSHOP, crafting_bonus=1.0) == required_materials(
        needs, WORKSHOP, crafting_bonus=1.0, recursive=True
    )


def test_stops_at_a_gatherable_intermediate():
    needs = [ItemNeed(item="Bucket", have=0, target=10)]
    # Board is gatherable, so it is a leaf to source, not crafted from Wood.
    demand = gather_demand(
        needs, WORKSHOP, crafting_bonus=1.0, gatherable=frozenset({"board"})
    )
    assert demand == {"Board": 50}


def test_gatherable_leaf_uses_the_recipe_display_name():
    needs = [ItemNeed(item="bucket", have=0, target=1)]
    demand = gather_demand(
        needs, WORKSHOP, crafting_bonus=1.0, gatherable=frozenset({"board"})
    )
    # Display name comes from the recipe even though the need was lower-cased.
    assert set(demand) == {"Board"}


def test_a_directly_needed_gatherable_item_is_its_own_leaf():
    needs = [ItemNeed(item="Board", have=0, target=8)]
    demand = gather_demand(
        needs, WORKSHOP, crafting_bonus=1.0, gatherable=frozenset({"board"})
    )
    assert demand == {"Board": 8}


def test_base_material_goal_is_recorded_as_a_leaf():
    # Wood has no recipe; it is a leaf whether or not it is gatherable.
    needs = [ItemNeed(item="Wood", have=0, target=30)]
    assert gather_demand(needs, WORKSHOP, crafting_bonus=1.0) == {"Wood": 30}


def test_iron_and_nails_are_dropped():
    needs = [ItemNeed(item="Bucket", have=0, target=10)]
    # Bucket's recipe also needs Iron, which is auto-supplied and never gathered.
    demand = gather_demand(needs, WORKSHOP, crafting_bonus=1.0)
    assert "Iron" not in demand


def test_mastery_bonus_reduces_only_the_top_level_shortfall():
    needs = [ItemNeed(item="Board", have=0, target=100, is_mastery=True)]
    # 100 mastery / 1.1 bonus / 1.0 craft = ~90.9 Boards -> 5 Wood each.
    demand = gather_demand(needs, WORKSHOP, crafting_bonus=1.0, mastery_bonus=1.1)
    assert demand == {"Wood": round(100 / 1.1 * 5)}
