from exploring import Exploring, ExploringLocation, ExploringModifiers


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
