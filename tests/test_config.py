import textwrap
from datetime import date, timedelta

import pytest

from config import load_config, passive_production, storehouse_size
from passive import DAILY, HOURLY, MINUTE


def test_load_config_reads_toml(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        textwrap.dedent(
            """
            [sawmill]
            wood = 46000
            board = 60000
            oak = 6000
            """
        )
    )
    config = load_config(path)
    assert config["sawmill"] == {"wood": 46000, "board": 60000, "oak": 6000}


def test_passive_production_reads_sawmill():
    config = {
        "sawmill": {
            "wood": 46000,
            "board": 60000,
            "oak": 6000,
            "have_sturdy_saw": True,
        }
    }
    production = passive_production(config)
    hourly = next(p for p in production.producers if p.period == HOURLY)
    assert hourly.name == "sawmill"
    assert hourly.outputs == {"wood": 46000, "board": 60000, "oak": 6000}
    # The built production answers timing queries.
    assert production.time_to_produce("board", 60000) == timedelta(hours=1)


def test_oak_needs_the_sturdy_saw():
    # Without the sturdy saw the sawmill makes no oak.
    production = passive_production(
        {"sawmill": {"wood": 46000, "oak": 6000}}
    )
    hourly = next(p for p in production.producers if p.period == HOURLY)
    assert hourly.outputs == {"wood": 46000}
    assert not production.produces("oak")


def test_sawmill_makes_pine_board_a_minute_at_a_time():
    production = passive_production({"sawmill": {"wood": 46000}})
    pine = next(p for p in production.producers if "pine board" in p.outputs)
    assert pine.period == MINUTE
    assert pine.outputs == {"pine board": 1}
    # The boost flag is not turned into an hourly output of its own.
    hourly = next(p for p in production.producers if p.period == HOURLY)
    assert hourly.outputs == {"wood": 46000}


def test_pine_boost_doubles_pine_board():
    production = passive_production(
        {"sawmill": {"wood": 46000, "have_pine_boost": True}}
    )
    pine = next(p for p in production.producers if "pine board" in p.outputs)
    assert pine.outputs == {"pine board": 2}
    hourly = next(p for p in production.producers if p.period == HOURLY)
    assert hourly.outputs == {"wood": 46000}  # boost flag excluded


def test_passive_production_lowercases_item_names():
    config = {"sawmill": {"Board": 60000}}
    production = passive_production(config)
    assert production.producers[0].outputs == {"board": 60000}


def test_passive_production_reads_chicken_coop():
    # produces eggs and feathers daily; gold feather at 5% when starmap is done.
    production = passive_production(
        {"chicken_coop": {"produces": 18271, "completed_starmap": True}}
    )
    coop = production.producers[0]
    assert coop.name == "chicken_coop"
    assert coop.period == DAILY
    assert coop.outputs == {
        "eggs": 18271,
        "feathers": 18271,
        "gold feather": 913,  # 18271 // 20
    }


def test_chicken_coop_without_starmap_has_no_gold_feather():
    production = passive_production(
        {"chicken_coop": {"produces": 18271, "completed_starmap": False}}
    )
    assert production.producers[0].outputs == {
        "eggs": 18271,
        "feathers": 18271,
    }


def test_passive_production_reads_worm_habitat():
    # Worms, gummy worms and mealworms hourly; underscores become spaces.
    production = passive_production(
        {
            "worm_habitat": {
                "worms": 10000,
                "gummy_worms": 4000,
                "mealworms": 4000,
            }
        }
    )
    habitat = production.producers[0]
    assert habitat.name == "worm_habitat"
    assert habitat.period == HOURLY
    assert habitat.outputs == {
        "worms": 10000,
        "gummy worms": 4000,
        "mealworms": 4000,
    }


def test_passive_production_reads_trout_bait_farm_with_two_cadences():
    # Grubs and minnows hourly, trout daily.
    production = passive_production(
        {"trout_bait_farm": {"trout": 25000, "grubs": 7000, "minnows": 6000}}
    )
    bait = next(p for p in production.producers if p.period == HOURLY)
    assert bait.outputs == {"grubs": 7000, "minnows": 6000}
    trout = next(p for p in production.producers if p.period == DAILY)
    assert trout.outputs == {"trout": 25000}


def test_passive_production_reads_orchard_with_bonus():
    # Each tree count is scaled by the multiplicative bonus and floored:
    # 6162 * 1.3 = 8010.6 -> 8010, etc.
    production = passive_production(
        {
            "orchard": {
                "apple_trees": 6162,
                "orange_trees": 6175,
                "lemon_trees": 6165,
                "bonus": 1.3,
            }
        }
    )
    orchard = production.producers[0]
    assert orchard.name == "orchard"
    assert orchard.period == DAILY
    assert orchard.outputs == {
        "apple": 8010,
        "orange": 8027,
        "lemon": 8014,
    }


def test_orchard_without_bonus_is_one_to_one():
    production = passive_production({"orchard": {"apple_trees": 100}})
    assert production.producers[0].outputs == {"apple": 100}


def test_tree_shaker_is_a_separate_offset_producer():
    # Like the antler snare, the shaker's 10% of each fruit (floored) is a
    # separate daily producer with its own storehouse-cap window.
    production = passive_production(
        {
            "orchard": {
                "apple_trees": 6162,
                "orange_trees": 6175,
                "lemon_trees": 6165,
                "bonus": 1.3,
                "have_tree_shaker": True,
            }
        }
    )
    main = next(p for p in production.producers if p.name == "orchard")
    shaker = next(p for p in production.producers if p.name == "orchard_shaker")
    assert main.outputs == {"apple": 8010, "orange": 8027, "lemon": 8014}
    assert shaker.period == DAILY
    assert shaker.outputs == {"apple": 801, "orange": 802, "lemon": 801}


def test_passive_production_reads_raptor_pen():
    production = passive_production({"raptor_pen": {"antlers": 2617810}})
    pen = production.producers[0]
    assert pen.name == "raptor_pen"
    assert pen.period == DAILY
    assert pen.outputs == {"antler": 2617810}


def test_antler_snare_is_a_separate_offset_producer():
    # The snare's 10% (2617810 // 10 = 261781) is a separate daily producer so
    # it gets its own storehouse-cap window twelve hours after the main run.
    production = passive_production(
        {"raptor_pen": {"antlers": 2617810, "have_antler_snare": True}}
    )
    main = next(p for p in production.producers if p.name == "raptor_pen")
    snare = next(p for p in production.producers if p.name == "raptor_pen_snare")
    assert main.period == DAILY
    assert main.outputs == {"antler": 2617810}
    assert snare.period == DAILY
    assert snare.outputs == {"antler": 261781}


def test_passive_production_reads_cow_pasture_milk_daily():
    production = passive_production({"cow_pasture": {"milk": 5000}})
    milk = production.producers[0]
    assert milk.name == "cow_pasture"
    assert milk.period == DAILY
    assert milk.outputs == {"milk": 5000}


def test_passive_production_reads_vineyard_grapes_daily():
    production = passive_production({"vineyard": {"grapes": 43000}})
    grapes = production.producers[0]
    assert grapes.name == "vineyard"
    assert grapes.period == DAILY
    assert grapes.outputs == {"grapes": 43000}


def test_flour_mill_produces_one_a_minute_by_default():
    production = passive_production({"flour_mill": {}})
    mill = production.producers[0]
    assert mill.name == "flour_mill"
    assert mill.period == MINUTE
    assert mill.outputs == {"flour": 1}


def test_flour_power_doubles_flour():
    production = passive_production({"flour_mill": {"have_flour_power": True}})
    assert production.producers[0].outputs == {"flour": 2}


def test_feed_mill_produces_one_a_minute_by_default():
    production = passive_production({"feed_mill": {}})
    mill = production.producers[0]
    assert mill.name == "feed_mill"
    assert mill.period == MINUTE
    assert mill.outputs == {"feed": 1}


def test_feed_boost_doubles_feed():
    production = passive_production({"feed_mill": {"have_feed_boost": True}})
    assert production.producers[0].outputs == {"feed": 2}


def test_sugar_cane_mill_produces_one_a_minute_by_default():
    production = passive_production({"sugar_cane_mill": {}})
    mill = production.producers[0]
    assert mill.name == "sugar_cane_mill"
    assert mill.period == MINUTE
    assert mill.outputs == {"sugar cane": 1}


def test_sugar_boost_i_doubles_to_two():
    production = passive_production(
        {"sugar_cane_mill": {"have_sugar_boost_i": True}}
    )
    assert production.producers[0].outputs == {"sugar cane": 2}


def test_sugar_boost_ii_quadruples_to_four():
    production = passive_production(
        {
            "sugar_cane_mill": {
                "have_sugar_boost_i": True,
                "have_sugar_boost_ii": True,
            }
        }
    )
    assert production.producers[0].outputs == {"sugar cane": 4}


def test_sugar_boost_ii_without_i_is_an_error():
    with pytest.raises(ValueError):
        passive_production(
            {"sugar_cane_mill": {"have_sugar_boost_ii": True}}
        )


def test_passive_production_reads_hay_field_as_straw():
    # The hay_field section reports straw produced every ten minutes.
    production = passive_production({"hay_field": {"hay": 30000}})
    assert production.produces("straw")
    assert not production.produces("hay")
    assert production.rate_per_minute("straw") == 3000  # 30000 / 10


def test_passive_production_reads_quarry_with_two_cadences():
    # Stone every ten minutes, coal every hour.
    production = passive_production({"quarry": {"stone": 8000, "coal": 8000}})
    assert production.rate_per_minute("stone") == 800  # 8000 / 10
    assert production.rate_per_minute("coal") == 8000 / 60  # hourly


def test_passive_production_reads_steelworks_with_derived_wire():
    # Steel hourly, steel wire at a third of that, rounded down.
    production = passive_production({"steelworks": {"steel": 100}})
    assert production.rate_per_minute("steel") == 100 / 60
    assert production.rate_per_minute("steel wire") == 33 / 60  # 100 // 3


def test_passive_production_attaches_storehouse_size():
    config = {
        "sawmill": {"board": 60000},
        "storehouse": {
            "inventory": 100,
            "updated_on": "2026-06-30",
            "growth": 20,
        },
    }
    production = passive_production(config, today=date(2026, 7, 2))
    assert production.inventory_size == 140


def test_passive_production_includes_pets():
    # A level-8 Skunk has all three tiers (12 items), fetching 8*24/12 = 16 of
    # each a day; Corn Oil is one of its tier-3 items.
    production = passive_production({"pets": {"Skunk": 8}})
    skunk = next(p for p in production.producers if p.name == "pet Skunk")
    assert skunk.period == DAILY
    assert skunk.outputs["corn oil"] == 16


def test_pets_crunchy_omelette_boosts_producer_output():
    production = passive_production(
        {"pets": {"Skunk": 8, "use_crunchy_omelette": True}}
    )
    skunk = next(p for p in production.producers if p.name == "pet Skunk")
    assert skunk.outputs["corn oil"] == 24  # 16 * 1.5


def test_seasonal_pet_output_is_scaled_to_annual_average():
    # With no plan span given, the Shark's June-July window is scaled to its
    # steady-state yearly share by days: 61 of 365. Level 8 full rate is 16 a day.
    production = passive_production({"pets": {"Shark": 8}})
    shark = next(p for p in production.producers if p.name == "pet Shark")
    assert shark.outputs["ghost jelly"] == pytest.approx(16 * 61 / 365)  # tier 3


def test_seasonal_pet_out_of_season_for_the_plan_is_dropped():
    # A 30-day plan in January never overlaps the Shark's June-July window, so it
    # contributes nothing and is not listed as a producer.
    production = passive_production(
        {"pets": {"Shark": 8}}, start=date(2026, 1, 1), horizon=timedelta(days=30)
    )
    assert not any(p.name == "pet Shark" for p in production.producers)


def test_seasonal_pet_in_season_for_the_whole_plan_is_full_rate():
    # A plan wholly inside June-July gets the Shark's full daily haul, not the
    # yearly average.
    production = passive_production(
        {"pets": {"Shark": 8}}, start=date(2026, 6, 1), horizon=timedelta(days=30)
    )
    shark = next(p for p in production.producers if p.name == "pet Shark")
    assert shark.outputs["ghost jelly"] == pytest.approx(16)  # tier 3, full daily


def test_pets_with_no_level_are_not_producers():
    # The boost flag alone leaves every pet at level 0, so none produce.
    production = passive_production({"pets": {"use_crunchy_omelette": True}})
    assert not any(p.name.startswith("pet ") for p in production.producers)


def test_passive_production_without_sawmill_is_empty():
    assert passive_production({}).producers == []


def test_storehouse_size_grows_each_day():
    config = {
        "storehouse": {
            "inventory": 49440,
            "updated_on": "2026-06-30",
            "growth": 20,
        }
    }
    # Five days on, the storehouse has grown by 5 * 20.
    assert storehouse_size(config, today=date(2026, 7, 5)) == 49540


def test_storehouse_size_on_updated_day_is_recorded_value():
    config = {"storehouse": {"inventory": 100, "updated_on": "2026-06-30"}}
    assert storehouse_size(config, today=date(2026, 6, 30)) == 100


def test_storehouse_size_ignores_dates_before_updated_on():
    config = {
        "storehouse": {
            "inventory": 100,
            "updated_on": "2026-06-30",
            "growth": 20,
        }
    }
    assert storehouse_size(config, today=date(2026, 6, 1)) == 100


def test_storehouse_size_parses_toml_date_value():
    # An unquoted TOML date is parsed as a date object, not a string.
    config = load_config_from_text(
        """
        [storehouse]
        inventory = 100
        updated_on = 2026-06-30
        growth = 20
        """
    )
    assert storehouse_size(config, today=date(2026, 7, 2)) == 140


def test_no_storehouse_section_is_none():
    assert storehouse_size({}) is None


def load_config_from_text(text: str) -> dict:
    import tomllib

    return tomllib.loads(textwrap.dedent(text))


# --- Farmhouse (daily stamina for Apple Cider) --------------------------------


def test_farmhouse_daily_stamina_is_eight_times_the_cap():
    production = passive_production(
        {"farmhouse": {"stamina_cap": 98387, "registered_on": "2026-07-05"}},
        today=date(2026, 7, 5),
    )
    house = next(p for p in production.producers if p.name == "farmhouse")
    assert house.period == DAILY
    # 8 * 98387 = 787096, under the 500k... no: default cap is 500k, so clamped.
    assert house.outputs == {"stamina": 500_000}


def test_farmhouse_apple_pie_enthusiast_raises_the_gain_cap_to_one_million():
    production = passive_production(
        {
            "farmhouse": {
                "stamina_cap": 98387,
                "registered_on": "2026-07-05",
                "have_apple_pie_enthusiast": True,
            }
        },
        today=date(2026, 7, 5),
    )
    house = next(p for p in production.producers if p.name == "farmhouse")
    # 8 * 98387 = 787096 < 1,000,000, so it is not clamped.
    assert house.outputs == {"stamina": 787_096}


def test_farmhouse_mattress_pad_grows_the_cap_two_a_day():
    # Ten days on, the cap has grown by 10 * 2; with Apple Pie the gain is uncapped.
    production = passive_production(
        {
            "farmhouse": {
                "stamina_cap": 1000,
                "registered_on": "2026-06-25",
                "have_mattress_pad": True,
                "have_apple_pie_enthusiast": True,
            }
        },
        today=date(2026, 7, 5),
    )
    house = next(p for p in production.producers if p.name == "farmhouse")
    assert house.outputs == {"stamina": 8 * (1000 + 20)}


def test_farmhouse_stamina_ignores_the_storehouse_cap():
    # Stamina is not stored in the storehouse, so its per-cycle rate is not clamped
    # to the (much smaller) storehouse size.
    production = passive_production(
        {
            "storehouse": {"inventory": 50_000},
            "farmhouse": {
                "stamina_cap": 98387,
                "registered_on": "2026-07-05",
                "have_apple_pie_enthusiast": True,
            },
        },
        today=date(2026, 7, 5),
    )
    # 787,096 a day, uncapped by the 50,000 storehouse: /1440 minutes.
    assert production.rate_per_minute("stamina") == pytest.approx(787_096 / 1440)


def test_no_farmhouse_section_produces_no_stamina():
    production = passive_production({"vineyard": {"grapes": 100}})
    assert production.rate_per_minute("stamina") == 0
