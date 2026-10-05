from crafting import Workshop, WorkshopRecipe
from main import unobtainable_materials

WORKSHOP = Workshop(
    recipes={
        # Chair is craftable from Board (craftable) and Painite (no source).
        "chair": WorkshopRecipe(name="Chair", materials={"Board": 4, "Painite": 1}),
        "board": WorkshopRecipe(name="Board", materials={"Wood": 5}),
    }
)


def test_lists_materials_with_no_source():
    # Wood is obtainable; Painite is not, and Board is craftable (excluded).
    obtainable = {"wood"}
    assert unobtainable_materials(WORKSHOP, obtainable) == ["Painite"]


def test_craftable_materials_are_never_unobtainable():
    # Even with nothing obtainable, Board is craftable so it is excluded.
    assert unobtainable_materials(WORKSHOP, set()) == ["Painite", "Wood"]


def test_nothing_unobtainable_when_all_sources_present():
    assert unobtainable_materials(WORKSHOP, {"wood", "painite"}) == []


def test_matching_is_case_insensitive():
    # A mixed-case material name is matched against the lower-cased obtainable
    # set, so it is not reported as unobtainable.
    workshop = Workshop(
        recipes={"box": WorkshopRecipe(name="Box", materials={"PAINITE": 1})}
    )
    assert unobtainable_materials(workshop, {"painite"}) == []


def test_obtainable_items_include_pet_output():
    # Pets are passive producers, so their items count as obtainable. A level-8
    # Cat has all three tiers unlocked, so its tier-3 item is obtainable.
    from config import passive_production
    from main import obtainable_items

    obtainable = obtainable_items(passive_production({"pets": {"Cat": 8}}))
    assert "shinefish" in obtainable  # Cat tier 3


def test_obtainable_items_exclude_locked_pet_tiers():
    # A level-2 Cat only reaches tier 1, so its tier-3 items are not obtainable.
    from config import passive_production
    from main import obtainable_items

    obtainable = obtainable_items(passive_production({"pets": {"Cat": 2}}))
    assert "drum" in obtainable  # Cat tier 1
    assert "shinefish" not in obtainable  # Cat tier 3, still locked


def test_obtainable_items_include_crop_drops_from_the_farm():
    # A crop's drops -- primary and rare -- count as obtainable via farming.
    from datetime import timedelta

    from config import passive_production
    from crops import Crop, Farm
    from main import obtainable_items

    farm = Farm(
        crops={
            "carrot": Crop(
                "Carrot", timedelta(hours=1), {"carrot": 4, "runestone 01": 0.02}
            )
        }
    )
    obtainable = obtainable_items(passive_production({}), farm)
    assert "carrot" in obtainable
    assert "runestone 01" in obtainable


def test_materials_with_an_unmodelled_source_are_flagged():
    # A material unmodelled.csv names is flagged with its real source; one it
    # does not name stays bare.
    reasons = {"painite": "Mining not modelled"}
    assert unobtainable_materials(WORKSHOP, set(), reasons) == [
        "Painite (Mining not modelled)",
        "Wood",
    ]
