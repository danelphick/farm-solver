from crafting import CraftStep, Workshop, WorkshopRecipe, craft_breakdown
from farm_solver import ItemNeed

WORKSHOP = Workshop(
    recipes={
        "hammer": WorkshopRecipe(
            name="Hammer",
            materials={"Board": 1, "Iron": 1, "Mushroom Paste": 1},
        ),
        "mushroom paste": WorkshopRecipe(
            name="Mushroom Paste", materials={"Mushroom": 3}
        ),
        "board": WorkshopRecipe(name="Board", materials={"Wood": 5}),
    }
)


def test_breaks_down_every_craft():
    # 5 Hammers -> 5 Board + 5 Iron + 5 Mushroom Paste; those craftable
    # materials each get their own step, base materials only appear inline.
    needs = [ItemNeed(item="Hammer", have=0, target=5)]
    steps = craft_breakdown(
        needs, WORKSHOP, crafting_bonus=1.0, mastery_bonus=1.0
    )
    assert steps == [
        CraftStep(
            item="Hammer",
            crafts=5,
            materials=[("Board", 5), ("Iron", 5), ("Mushroom Paste", 5)],
            produced=5,
            mastery_added=5,
        ),
        CraftStep(
            item="Board",
            crafts=5,
            materials=[("Wood", 25)],
            produced=5,
            mastery_added=5,
        ),
        CraftStep(
            item="Mushroom Paste",
            crafts=5,
            materials=[("Mushroom", 15)],
            produced=5,
            mastery_added=5,
        ),
    ]


def test_base_materials_are_not_their_own_step():
    needs = [ItemNeed(item="Hammer", have=0, target=5)]
    steps = craft_breakdown(
        needs, WORKSHOP, crafting_bonus=1.0, mastery_bonus=1.0
    )
    assert {step.item for step in steps} == {"Hammer", "Board", "Mushroom Paste"}


def test_crafting_bonus_distinguishes_crafts_from_consumption():
    # 8 Hammers / 2 = 4 crafts, consuming 4 Mushroom Paste; but those 4 paste
    # only take 4 / 2 = 2 crafts, so the step count differs from consumption.
    needs = [ItemNeed(item="Hammer", have=0, target=8)]
    steps = craft_breakdown(
        needs, WORKSHOP, crafting_bonus=2.0, mastery_bonus=1.0
    )
    assert steps == [
        CraftStep(
            item="Hammer",
            crafts=4,
            materials=[("Board", 4), ("Iron", 4), ("Mushroom Paste", 4)],
            produced=8,
            mastery_added=8,
        ),
        CraftStep(
            item="Board",
            crafts=2,
            materials=[("Wood", 10)],
            produced=4,
            mastery_added=4,
        ),
        CraftStep(
            item="Mushroom Paste",
            crafts=2,
            materials=[("Mushroom", 6)],
            produced=4,
            mastery_added=4,
        ),
    ]


def test_mastery_bonus_applies_only_to_own_mastery():
    # Board is both a Hammer material and a mastery goal. Its own mastery (8 ->
    # 4 items after the bonus) adds to the 4 consumed by the Hammers, for 8
    # crafts; the consumption under Hammer stays 4 (no bonus there).
    needs = [
        ItemNeed(item="Hammer", have=0, target=8, is_mastery=True),
        ItemNeed(item="Board", have=0, target=8, is_mastery=True),
    ]
    steps = craft_breakdown(
        needs, WORKSHOP, crafting_bonus=1.0, mastery_bonus=2.0
    )
    assert steps == [
        CraftStep(
            item="Hammer",
            crafts=4,
            materials=[("Board", 4), ("Iron", 4), ("Mushroom Paste", 4)],
            produced=4,
            mastery_added=8,
        ),
        CraftStep(
            item="Board",
            crafts=8,
            materials=[("Wood", 40)],
            produced=8,
            mastery_added=16,
        ),
        CraftStep(
            item="Mushroom Paste",
            crafts=4,
            materials=[("Mushroom", 12)],
            produced=4,
            mastery_added=8,
        ),
    ]


def test_reports_produced_and_mastery_added():
    # Mastery goal of 1595 -> 1595 / 1.1 = 1450 items needed -> 1450 / 1.45 =
    # 1000 crafts. Those 1000 crafts produce 1000 * 1.45 = 1450 Board, adding
    # 1450 * 1.1 = 1595 mastery (back to the original shortfall).
    needs = [ItemNeed(item="Board", have=0, target=1595, is_mastery=True)]
    steps = craft_breakdown(
        needs, WORKSHOP, crafting_bonus=1.45, mastery_bonus=1.1
    )
    assert steps == [
        CraftStep(
            item="Board",
            crafts=1000,
            materials=[("Wood", 5000)],
            produced=1450,
            mastery_added=1595,
        ),
    ]


def test_every_craft_reports_produced_and_mastery():
    # Intermediate materials get the same produced/mastery figures, not zero.
    needs = [ItemNeed(item="Hammer", have=0, target=5)]
    steps = craft_breakdown(
        needs, WORKSHOP, crafting_bonus=1.0, mastery_bonus=1.0
    )
    assert all(
        step.produced > 0 and step.mastery_added > 0 for step in steps
    )


def test_no_craftable_needs_yields_no_steps():
    needs = [ItemNeed(item="Wood", have=0, target=100)]  # no recipe
    assert craft_breakdown(needs, WORKSHOP) == []


def test_lookup_is_case_insensitive():
    needs = [ItemNeed(item="HAMMER", have=0, target=5)]
    steps = craft_breakdown(
        needs, WORKSHOP, crafting_bonus=1.0, mastery_bonus=1.0
    )
    assert steps[0].item == "Hammer"
