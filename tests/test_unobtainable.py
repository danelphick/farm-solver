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
