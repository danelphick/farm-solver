from datetime import timedelta

import pytest

from crops import (
    Crop,
    Farm,
    farm_from_config,
    load_farm,
    per_seed_rate,
    read_crops,
    read_drops,
)


def make_farm(**kwargs):
    crops = {
        "corn": Crop(name="Corn", growth=timedelta(minutes=10), drops={"corn": 1}),
        "carrot": Crop(
            name="Carrot",
            growth=timedelta(hours=1),
            drops={"carrot": 4, "runestone 01": 0.02, "gold carrot": 0.01},
        ),
    }
    return Farm(crops=crops, **kwargs)


@pytest.mark.parametrize(
    "metric, value, expected",
    [
        # 47.55 carrots over 12 rows is 3.9625 a seed.
        ("Crops/harvest all", 47.55, 47.55 / 12),
        # One runestone every 3.34 harvests over 12 rows: 1 / (3.34 * 12) a seed.
        ("Harvest alls/crop", 3.34, 1 / (3.34 * 12)),
        # Case and spacing around the metric are ignored.
        ("  crops/harvest all ", 48, 4),
    ],
)
def test_per_seed_rate_converts_the_in_game_metrics(metric, value, expected):
    assert per_seed_rate(metric, value, rows=12) == pytest.approx(expected)


def test_per_seed_rate_rejects_unknown_metric():
    with pytest.raises(ValueError):
        per_seed_rate("Widgets/day", 5, rows=12)


def test_carrot_seed_drops_sum_to_the_total_harvest():
    # The example: the three per-seed rates add up to 4 an seed (48 at 12 rows).
    total = (
        per_seed_rate("Crops/harvest all", 47.55, 12)
        + per_seed_rate("Harvest alls/crop", 3.34, 12)
        + per_seed_rate("Harvest alls/crop", 6.71, 12)
    )
    assert total == pytest.approx(4, abs=1e-3)


def test_per_harvest_defaults_to_the_primary_crop():
    farm = make_farm(rows=12)
    assert farm.per_harvest("Carrot") == pytest.approx(48)


def test_per_harvest_of_a_secondary_drop():
    farm = make_farm(rows=12)
    # A rare drop at 0.02 a seed over 12 rows averages 0.24 a harvest.
    assert farm.per_harvest("Carrot", "Runestone 01") == pytest.approx(0.24)


def test_per_harvest_unknown_drop_is_none():
    assert make_farm(rows=12).per_harvest("Carrot", "Diamond") is None


def test_double_chance_scales_every_drop():
    farm = make_farm(rows=12, double_chance=0.25)
    # A 25% double chance lifts each drop's expectation by a quarter.
    assert farm.per_harvest("Carrot") == pytest.approx(60)
    assert farm.per_harvest("Carrot", "Runestone 01") == pytest.approx(0.3)


def test_grow_time_is_the_base_when_unmodified():
    assert make_farm().grow_time("Carrot") == timedelta(hours=1)


def test_speed_factor_shortens_every_crop():
    farm = make_farm(speed=0.5)
    assert farm.grow_time("Carrot") == timedelta(minutes=30)
    assert farm.grow_time("Corn") == timedelta(minutes=5)


def test_corn_speed_only_affects_corn():
    farm = make_farm(speed=0.5, corn_speed=0.5)
    # Corn gets both factors (10min * 0.5 * 0.5 = 2.5min); carrot only the global.
    assert farm.grow_time("Corn") == timedelta(minutes=2, seconds=30)
    assert farm.grow_time("Carrot") == timedelta(minutes=30)


def test_time_to_grow_rounds_up_to_whole_plantings():
    farm = make_farm(rows=12)
    # 100 carrots at 48 a harvest needs 3 harvests (144), so 3 hours.
    assert farm.time_to_grow("Carrot", 100) == timedelta(hours=3)


def test_time_to_grow_targets_a_secondary_drop():
    farm = make_farm(rows=12)
    # One runestone at 0.24 a harvest needs 5 harvests (ceil(1/0.24)), so 5 hours.
    assert farm.time_to_grow("Carrot", 1, "Runestone 01") == timedelta(hours=5)


def test_grape_juice_adds_free_daily_harvests():
    # A crop that grows once a day, one per harvest, with 11 grape juices a day
    # yields 1 (natural) + 11 (instant) = 12 harvests a day, so 12 take one day.
    farm = Farm(
        crops={"slow": Crop("Slow", timedelta(days=1), {"slow": 1})},
        rows=1,
        grape_juices_per_day=11,
    )
    assert farm.time_to_grow("Slow", 12) == timedelta(days=1)


def test_no_grape_juice_leaves_the_grow_time_unchanged():
    # Without grape juice the wait is simply plantings * grow time.
    farm = Farm(
        crops={"slow": Crop("Slow", timedelta(days=1), {"slow": 1})},
        rows=1,
    )
    assert farm.time_to_grow("Slow", 12) == timedelta(days=12)


def test_time_to_grow_unknown_crop_is_none():
    assert make_farm(rows=12).time_to_grow("Turnip", 10) is None


def test_time_to_grow_without_rows_is_none():
    assert make_farm(rows=0).time_to_grow("Carrot", 10) is None


def test_lookup_is_case_insensitive():
    assert make_farm().crop("CARROT") is not None
    assert make_farm().crop("nope") is None


def test_growers_lists_crops_that_drop_the_item():
    farm = make_farm()
    assert {c.name for c in farm.growers("Runestone 01")} == {"Carrot"}
    assert {c.name for c in farm.growers("Corn")} == {"Corn"}
    assert farm.growers("Diamond") == []


def test_growable_items_collects_every_drop():
    items = make_farm().growable_items()
    assert {"corn", "carrot", "runestone 01", "gold carrot"} <= items


def test_best_source_finds_the_grower_of_a_rare_drop():
    crop, time = make_farm(rows=13).best_source("Runestone 01", 5)
    assert crop == "Carrot"
    assert time is not None


def test_best_source_prefers_the_faster_crop():
    # Cotton (4/seed) and Mega Cotton (40/seed) grow Cotton in the same time; a
    # large amount needs fewer mega plantings, so the mega seed wins.
    farm = Farm(
        crops={
            "cotton": Crop("Cotton", timedelta(days=4), {"cotton": 4}),
            "mega cotton": Crop(
                "Mega Cotton", timedelta(days=4), {"cotton": 40}, primary="Cotton"
            ),
        },
        rows=13,
    )
    crop, _ = farm.best_source("Cotton", 1000)
    assert crop == "Mega Cotton"


def test_best_source_unknown_item_is_none():
    assert make_farm(rows=13).best_source("Diamond", 5) is None


def test_best_source_without_rows_is_none():
    assert make_farm(rows=0).best_source("Corn", 5) is None


def test_read_crops_parses_durations_and_keeps_display_names(tmp_path):
    path = tmp_path / "crops.csv"
    path.write_text("Crop,Growth\nCorn,10m\nSugar Cane,1h30m\n")
    crops = read_crops(path)
    assert crops["corn"].growth == timedelta(minutes=10)
    assert crops["sugar cane"].name == "Sugar Cane"
    # Primary defaults to the crop's own name.
    assert crops["corn"].primary == "Corn"


def test_read_crops_uses_the_primary_column(tmp_path):
    path = tmp_path / "crops.csv"
    path.write_text(
        "Crop,Growth,Primary\n"
        "Cotton,4d,\n"
        "Mega Cotton,4d,Cotton\n"
    )
    crops = read_crops(path)
    # A blank Primary falls back to the name; a mega seed names its base crop.
    assert crops["cotton"].primary == "Cotton"
    assert crops["mega cotton"].primary == "Cotton"


def test_mega_style_row_targets_its_base_crop(tmp_path):
    # A mega seed is just data: its name differs from the item it produces, and
    # per_harvest without an item follows its primary.
    crops_path = tmp_path / "crops.csv"
    crops_path.write_text(
        "Crop,Growth,Primary\nCotton,4d,\nMega Cotton,4d,Cotton\n"
    )
    drops_path = tmp_path / "seed_drops.csv"
    drops_path.write_text(
        "Crop,Item,Metric,Value\n"
        "Cotton,Cotton,Crops/harvest all,48\n"
        "Mega Cotton,Cotton,Crops/harvest all,480\n"
    )
    farm = load_farm(crops_path, drops_path, rows=13)
    # per_seed = value / 12 capture rows; per_harvest = 13 farm rows * per_seed.
    assert farm.per_harvest("Mega Cotton") == pytest.approx(13 * 40)
    assert farm.per_harvest("Cotton") == pytest.approx(13 * 4)


def test_read_drops_normalises_the_in_game_metrics(tmp_path):
    path = tmp_path / "seed_drops.csv"
    path.write_text(
        "Crop,Item,Metric,Value\n"
        "Carrot,Carrot,Crops/harvest all,48\n"
        "Carrot,Runestone 01,Harvest alls/crop,4\n"
        "Corn,Corn,Crops/harvest all,24\n"
    )
    drops = read_drops(path)
    # Figures are captured at CAPTURE_ROWS (12): 48/12 = 4, 1/(4*12), 24/12 = 2.
    assert drops["carrot"]["carrot"] == pytest.approx(4)
    assert drops["carrot"]["runestone 01"] == pytest.approx(1 / 48)
    assert drops["corn"] == {"corn": pytest.approx(2)}


def test_read_drops_missing_file_is_empty(tmp_path):
    assert read_drops(tmp_path / "absent.csv") == {}


def test_farm_from_config_converts_percentages_to_factors(tmp_path):
    crops_path = tmp_path / "crops.csv"
    crops_path.write_text("Crop,Growth\nCorn,4h\nCarrot,2m\n")
    config = {
        "rows": 13,
        "double_crop_chance": 25,
        "speed_reduction": 90,
        "corn_speed_reduction": 20,
    }
    farm = farm_from_config(config, crops_path, tmp_path / "absent.csv")
    assert farm.rows == 13
    assert farm.double_chance == pytest.approx(0.25)
    # 90% reduction leaves a 0.1 factor; corn's extra 20% leaves 0.8.
    assert farm.speed == pytest.approx(0.1)
    assert farm.corn_speed == pytest.approx(0.8)
    # Corn's 4h base becomes 24m globally, then 19m12s with corn's reduction.
    assert farm.grow_time("Corn") == timedelta(minutes=19, seconds=12)
    assert farm.grow_time("Carrot") == timedelta(seconds=12)


def test_farm_from_config_reads_grape_juices_per_day(tmp_path):
    crops_path = tmp_path / "crops.csv"
    crops_path.write_text("Crop,Growth\nCorn,4h\n")
    farm = farm_from_config(
        {"grape_juices_per_day": 14}, crops_path, tmp_path / "absent.csv"
    )
    assert farm.grape_juices_per_day == 14


def test_farm_from_config_defaults_grape_juices_to_zero(tmp_path):
    crops_path = tmp_path / "crops.csv"
    crops_path.write_text("Crop,Growth\nCorn,4h\n")
    farm = farm_from_config({}, crops_path, tmp_path / "absent.csv")
    assert farm.grape_juices_per_day == 0


def test_farm_from_config_defaults_leave_crops_unmodified(tmp_path):
    crops_path = tmp_path / "crops.csv"
    crops_path.write_text("Crop,Growth\nCorn,4h\n")
    farm = farm_from_config({}, crops_path, tmp_path / "absent.csv")
    assert farm.rows == 0
    assert farm.speed == 1
    assert farm.corn_speed == 1
    assert farm.grow_time("Corn") == timedelta(hours=4)


def test_load_farm_merges_growth_and_drops(tmp_path):
    crops_path = tmp_path / "crops.csv"
    crops_path.write_text("Crop,Growth\nCarrot,1h\n")
    drops_path = tmp_path / "seed_drops.csv"
    drops_path.write_text(
        "Crop,Item,Metric,Value\n"
        "Carrot,Carrot,Crops/harvest all,48\n"
        "Carrot,Gold Carrot,Crops/harvest all,1.2\n"
    )
    farm = load_farm(crops_path, drops_path, rows=12, double_chance=0.25, speed=0.5)
    assert farm.grow_time("Carrot") == timedelta(minutes=30)
    # per_seed carrot = 48/12 = 4; per_harvest = 12 rows * 4 * 1.25 = 60.
    assert farm.per_harvest("Carrot") == pytest.approx(60)
    # per_seed gold = 1.2/12 = 0.1; per_harvest = 12 * 0.1 * 1.25 = 1.5.
    assert farm.per_harvest("Carrot", "Gold Carrot") == pytest.approx(1.5)
