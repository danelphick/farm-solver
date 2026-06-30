from fishing import Fishing, FishingModifiers, FishingZone


def test_default_net_catch_is_base():
    assert FishingModifiers().net_catch() == 250


def test_reinforced_netting_raises_the_base():
    assert FishingModifiers(have_reinforced_netting=True).net_catch() == 400


def test_fishing_trawl_adds_a_flat_hundred():
    assert FishingModifiers(have_fishing_trawl=True).net_catch() == 350


def test_sea_pincher_adds_ten_percent():
    assert FishingModifiers(use_sea_pincher=True).net_catch() == 275


def test_modifiers_combine_additions_before_the_percentage():
    # (400 + 100) * 1.1 = 550.
    mods = FishingModifiers(
        have_reinforced_netting=True,
        have_fishing_trawl=True,
        use_sea_pincher=True,
    )
    assert mods.net_catch() == 550


def test_zone_catch_scales_drop_rates_by_the_net_catch():
    zone = FishingZone(name="Lake", drop_rates={"Trout": 0.4, "Minnow": 0.1})
    assert zone.catch(250) == {"Trout": 100.0, "Minnow": 25.0}


def test_fishing_looks_up_zones_case_insensitively():
    lake = FishingZone(name="Lake")
    fishing = Fishing(zones={"lake": lake})
    assert fishing.zone("LAKE") is lake
    assert fishing.zone("ocean") is None
