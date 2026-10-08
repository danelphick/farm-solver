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


def test_obtainable_items_include_every_flagged_exploring_table():
    # Painite drops only from Gary's Crushroom's Fungus Processing table, which
    # the --unobtainable report never switches on (it reads no inventory). The
    # report asks what can be sourced at all, so every flagged table counts --
    # alongside the base one, whose Mushroom the fungus table lacks.
    from config import passive_production
    from exploring import exploring_from_config
    from main import obtainable_items

    explore = exploring_from_config({}, have_fungus_processing=False)
    obtainable = obtainable_items(passive_production({}), explore=explore)
    assert "painite" in obtainable  # fungus_processing table only
    assert "hide" in obtainable  # acorn_pie table only
    assert "mushroom" in obtainable  # base table only


# --- --unobtainable scopes ------------------------------------------------------


RECIPES = {
    "chair": {"board": 4.0, "painite": 1.0},
    "board": {"wood": 5.0},
    "ring": {"gem": 1.0},
}
DISPLAY = {
    "chair": "Chair", "board": "Board", "painite": "Painite",
    "wood": "Wood", "ring": "Ring", "gem": "Gem", "frozen fish": "Frozen Fish",
}


def test_sourceable_from_game_data_follows_crafts_and_opens():
    from main import sourceable_from_game_data
    from sources import OpenFlow

    flows = [OpenFlow(name="Chest", consumes={"chest": 1.0}, yields={"gem": 2.0})]
    available = sourceable_from_game_data({"wood", "chest"}, RECIPES, flows)
    # Board is crafted from Wood; Gem comes out of the chest, so Ring crafts too.
    assert {"wood", "board", "chest", "gem", "ring"} <= available
    assert "chair" not in available  # Painite has no source


def test_unobtainable_items_lists_direct_and_indirect_blockers():
    from main import unobtainable_items

    universe = {"chair": "Chair", "board": "Board", "frozen fish": "Frozen Fish"}
    available = {"wood", "board"}
    found = unobtainable_items(
        universe, available, RECIPES, DISPLAY, {"frozen fish": "seasonal"}
    )
    assert found == [
        "Chair needs Painite (unobtainable)",  # indirectly, through a material
        "Frozen Fish (seasonal)",  # directly, with its reason
    ]


def test_load_item_catalogue_reads_items_csv(tmp_path):
    from main import load_item_catalogue

    path = tmp_path / "items.csv"
    path.write_text("# comment\nitem,event\nFrozen Fish,true\nBoard,\n")
    assert load_item_catalogue(path) == {"frozen fish": "Frozen Fish", "board": "Board"}
    assert load_item_catalogue(tmp_path / "missing.csv") == {}


def test_unobtainable_flag_takes_a_scope_defaulting_to_crafting():
    import pytest

    from main import build_parser

    parser = build_parser()
    assert parser.parse_args([]).unobtainable is None
    assert parser.parse_args(["--unobtainable"]).unobtainable == "crafting"
    assert parser.parse_args(["--unobtainable=masterable"]).unobtainable == "masterable"
    assert parser.parse_args(["--unobtainable", "all"]).unobtainable == "all"
    with pytest.raises(SystemExit):
        parser.parse_args(["--unobtainable=bogus"])


def test_each_scope_has_its_own_title(capsys):
    from report import MarkdownDoc, markdown_unobtainable, print_unobtainable

    print_unobtainable(["Chair needs Painite (unobtainable)"], "masterable")
    out = capsys.readouterr().out
    assert out.startswith("The following masterable items are unobtainable")
    doc = MarkdownDoc()
    markdown_unobtainable(doc, [], "all")
    assert "## Unobtainable items" in doc.render()


def test_masterable_scope_covers_items_no_recipe_uses(capsys):
    # The frozen fish are masterable but a material of nothing, so only the
    # masterable scope (not crafting) reports them.
    from main import build_parser, run

    run(build_parser().parse_args(["--unobtainable=masterable"]))
    masterable = capsys.readouterr().out
    assert "Frozen Angelfish (seasonal)" in masterable
    assert "Compass needs Magna Core" in masterable  # indirectly unobtainable

    run(build_parser().parse_args(["--unobtainable"]))
    assert "Frozen Angelfish" not in capsys.readouterr().out
