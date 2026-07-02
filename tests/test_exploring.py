from exploring import (
    DEFAULT_EXPLORING_CSV,
    Exploring,
    ExploringLocation,
    ExploringModifiers,
    exploring_from_config,
    read_locations,
)


def test_default_palmer_yield_is_base():
    assert ExploringModifiers().palmer_yield() == 200


def test_lemon_squeezer_raises_the_base():
    assert ExploringModifiers(have_lemon_squeezer=True).palmer_yield() == 500


def test_quandary_chowder_adds_ten_percent():
    assert ExploringModifiers(use_quandary_chowder=True).palmer_yield() == 220


def test_modifiers_combine_to_the_full_yield():
    # 500 * 1.1 = 550.
    mods = ExploringModifiers(have_lemon_squeezer=True, use_quandary_chowder=True)
    assert mods.palmer_yield() == 550


def test_location_explore_scales_drop_rates_by_the_yield():
    location = ExploringLocation(name="Cave", drop_rates={"Wood": 0.4, "Stone": 0.1})
    assert location.explore(200) == {"Wood": 80.0, "Stone": 20.0}


def test_exploring_looks_up_locations_case_insensitively():
    cave = ExploringLocation(name="Cave")
    exploring = Exploring(locations={"cave": cave})
    assert exploring.location("CAVE") is cave
    assert exploring.location("forest") is None


def test_read_locations_gathers_rows_and_lowercases_items(tmp_path):
    path = tmp_path / "exploring.csv"
    path.write_text(
        "Location,Item,Drop Rate\n"
        "Small Cave,Wood,0.3872\n"
        "Small Cave,Skeleton Key,0.0003\n"
        "Small Spring,Feathers,0.25\n"
    )
    locations = read_locations(path)
    assert set(locations) == {"small cave", "small spring"}
    # Display name is preserved; item keys are lower-cased.
    assert locations["small cave"].name == "Small Cave"
    assert locations["small cave"].drop_rates == {"wood": 0.3872, "skeleton key": 0.0003}


def test_read_locations_missing_file_is_empty(tmp_path):
    assert read_locations(tmp_path / "absent.csv") == {}


def test_exploring_from_config_sets_modifiers_and_loads_locations():
    exploring = exploring_from_config(
        {"have_lemon_squeezer": True, "use_quandary_chowder": True}
    )
    assert exploring.modifiers.have_lemon_squeezer
    assert exploring.modifiers.use_quandary_chowder
    # The bundled CSV populates real locations.
    assert exploring.locations


def test_the_bundled_exploring_csv_parses():
    assert read_locations(DEFAULT_EXPLORING_CSV)
