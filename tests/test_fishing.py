from fishing import (
    DEFAULT_FISHING_CSV,
    Fishing,
    FishingModifiers,
    FishingZone,
    fishing_from_config,
    read_zones,
)


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


def test_read_zones_gathers_rows_and_lowercases_items(tmp_path):
    path = tmp_path / "fishing.csv"
    path.write_text(
        "Zone,Item,Drop Rate\n"
        "Small Pond,Drum,0.4758\n"
        "Small Pond,Yellow Perch,0.4\n"
        "Big Lake,Trout,0.3\n"
    )
    zones = read_zones(path)
    assert set(zones) == {"small pond", "big lake"}
    # Display name is preserved; item keys are lower-cased.
    assert zones["small pond"].name == "Small Pond"
    assert zones["small pond"].drop_rates == {"drum": 0.4758, "yellow perch": 0.4}


def test_read_zones_missing_file_is_empty(tmp_path):
    assert read_zones(tmp_path / "absent.csv") == {}


def test_fishing_from_config_sets_modifiers_and_loads_zones():
    fishing = fishing_from_config(
        {"have_reinforced_netting": True, "use_sea_pincher": True}
    )
    assert fishing.modifiers.have_reinforced_netting
    assert fishing.modifiers.use_sea_pincher
    assert not fishing.modifiers.have_fishing_trawl
    # The bundled CSV populates real zones.
    assert fishing.zones


def test_the_bundled_fishing_csv_parses():
    assert read_zones(DEFAULT_FISHING_CSV)
