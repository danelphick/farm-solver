import textwrap
from datetime import date, timedelta

import pytest

import config
import crafting
from config import (
    MONTHLY_GRANT_PRODUCER,
    crafting_bonus_for,
    current_tower_level,
    effective_grape_juices,
    is_smeared_producer,
    load_config,
    passive_production,
    passive_quantizer,
    storehouse_size,
    sugar_cane_mill_rates,
    trading_prices,
)
from passive import DAILY, HOURLY, MINUTE, PassiveProducer, PassiveProduction


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
    config = {"sawmill": {"wood": 46000, "board": 60000, "oak": 6000}}
    production = passive_production(config)
    hourly = next(p for p in production.producers if p.period == HOURLY)
    assert hourly.name == "sawmill"
    assert hourly.outputs == {"wood": 46000, "board": 60000, "oak": 6000}
    # The built production answers timing queries.
    assert production.time_to_produce("board", 60000) == timedelta(hours=1)


def test_no_oak_without_an_oak_rate():
    # No Sturdy Saw is said by the quantity itself: a zero rate is no output at
    # all, not a producer that makes nothing.
    production = passive_production({"sawmill": {"wood": 46000, "oak": 0}})
    hourly = next(p for p in production.producers if p.period == HOURLY)
    assert hourly.outputs == {"wood": 46000}
    assert not production.produces("oak")
    assert not passive_production({"sawmill": {"wood": 46000}}).produces("oak")


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


def test_hickory_omelette_setting_does_not_boost_passive_production():
    # The Hickory Omelette boost is a grounded offer, not part of passive
    # production: the setting never touches the sawmill's free hourly rates.
    base = passive_production({"sawmill": {"board": 46000}})
    boosted = passive_production(
        {"sawmill": {"board": 46000, "hickory_omelette": "all"}}
    )
    assert base.rate_per_minute("board") == boosted.rate_per_minute("board")
    # The string setting is a config flag, never an output item.
    hourly = next(
        p for p in boosted.producers if p.name == "sawmill" and p.period == HOURLY
    )
    assert hourly.outputs == {"board": 46000}


def test_sawmill_outputs_drops_zero_rates_and_settings():
    cfg = {
        "sawmill": {
            "Wood": 60000,
            "board": 46000,
            "oak": 0,
            "hickory_omelette": "only_oak",
            "have_pine_boost": True,
        }
    }
    # A zero quantity is no output; string/bool settings are not outputs either.
    assert config.sawmill_outputs(cfg) == {"wood": 60000, "board": 46000}
    cfg["sawmill"]["oak"] = 6000
    assert config.sawmill_outputs(cfg) == {
        "wood": 60000,
        "board": 46000,
        "oak": 6000,
    }


def test_hickory_omelette_mode_reads_and_validates():
    assert config.hickory_omelette_mode({}) == "none"
    assert config.hickory_omelette_mode({"sawmill": {}}) == "none"
    assert (
        config.hickory_omelette_mode({"sawmill": {"hickory_omelette": "only_oak"}})
        == "only_oak"
    )
    with pytest.raises(ValueError, match="none.*all.*only_oak|only_oak"):
        config.hickory_omelette_mode({"sawmill": {"hickory_omelette": "some"}})
    # The retired boolean is rejected with a migration hint.
    with pytest.raises(ValueError, match="use_hickory_omelette"):
        config.hickory_omelette_mode({"sawmill": {"use_hickory_omelette": True}})


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


def test_oddvocado_trees_split_by_observed_weights():
    # Each tree bears one fruit, drawn by the observed drop weights, so the
    # separate daily producer deposits each fruit's expected share of the
    # trees, unscaled by the bonus or the shaker.
    production = passive_production(
        {
            "orchard": {
                "lemon_trees": 100,
                "bonus": 1.3,
                "have_tree_shaker": True,
                "oddvocado_trees": 120,
            }
        }
    )
    odd = next(p for p in production.producers if p.name == "orchard_oddvocado")
    assert odd.period == DAILY
    assert odd.outputs == pytest.approx(
        {
            "avocado": 33,
            "orange": 24,
            "lemon": 20,
            "cherries": 16,
            "raspberries": 14,
            "pear": 8,
            "blueberries": 5,
        }
    )
    assert sum(odd.outputs.values()) == pytest.approx(120)
    shaker = next(p for p in production.producers if p.name == "orchard_shaker")
    assert shaker.outputs == {"lemon": 13}


def test_no_oddvocado_trees_adds_no_producer():
    production = passive_production({"orchard": {"apple_trees": 100}})
    assert [p.name for p in production.producers] == ["orchard"]


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


def test_mariyas_wheelbarrow_adds_horns_to_the_same_daily_deposit():
    # Three horns per milk, in the pasture's own producer -- one deposit, so the
    # two share a storehouse-cap window.
    production = passive_production(
        {"cow_pasture": {"milk": 5000}}, has_mariyas_wheelbarrow=True
    )
    assert len(production.producers) == 1
    pasture = production.producers[0]
    assert pasture.name == "cow_pasture"
    assert pasture.period == DAILY
    assert pasture.outputs == {"milk": 5000, "horn": 15000}


def test_cow_pasture_without_the_wheelbarrow_produces_no_horns():
    # The flag is resolved by the caller (main.resolve_mariyas_wheelbarrow), so
    # the config key alone means nothing here -- only the argument does.
    production = passive_production({"cow_pasture": {"milk": 5000}})
    assert production.producers[0].outputs == {"milk": 5000}
    production = passive_production(
        {"cow_pasture": {"milk": 5000, "has_mariyas_wheelbarrow": True}},
        has_mariyas_wheelbarrow=False,
    )
    assert production.producers[0].outputs == {"milk": 5000}


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


def test_sugar_cane_mill_is_not_a_free_passive_producer():
    assert passive_production({"sugar_cane_mill": {}}).producers == []


def test_sugar_cane_mill_rates_default_one_batch_of_one():
    assert sugar_cane_mill_rates({"sugar_cane_mill": {}}) == (1, 1)


def test_sugar_cane_mill_rates_absent_section_is_no_mill():
    assert sugar_cane_mill_rates({}) == (0, 0)


def test_sugar_boost_i_doubles_the_batches():
    assert sugar_cane_mill_rates(
        {"sugar_cane_mill": {"have_sugar_boost_i": True}}
    ) == (2, 1)


def test_sugar_boost_ii_doubles_each_batch_output():
    assert sugar_cane_mill_rates(
        {
            "sugar_cane_mill": {
                "have_sugar_boost_i": True,
                "have_sugar_boost_ii": True,
            }
        }
    ) == (2, 2)


def test_sugar_boost_ii_without_i_is_an_error():
    with pytest.raises(ValueError):
        sugar_cane_mill_rates({"sugar_cane_mill": {"have_sugar_boost_ii": True}})


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
    # A level-8 Critter has all three tiers (12 items), fetching 8*24/12 = 16 of
    # each a day; Corn Oil is one of its tier-3 items.
    production = passive_production({"pets": {"Critter": 8}})
    critter = next(p for p in production.producers if p.name == "pet Critter")
    assert critter.period == DAILY
    assert critter.outputs["corn oil"] == 16


def test_pets_crunchy_omelette_boosts_producer_output():
    production = passive_production(
        {"pets": {"Critter": 8, "use_crunchy_omelette": True}}
    )
    critter = next(p for p in production.producers if p.name == "pet Critter")
    assert critter.outputs["corn oil"] == 24  # 16 * 1.5


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


def test_monthly_grants_off_by_default():
    # Without the flag the tower-artifact producer is absent.
    production = passive_production({})
    assert not any(p.name == MONTHLY_GRANT_PRODUCER for p in production.producers)


def test_monthly_grants_add_steady_state_daily_producers():
    # With no plan span the grants are their steady-state monthly average spread
    # over a ~30.4-day month: 3000 cider and 2000 Arnold Palmer a month.
    production = passive_production({}, monthly_grants=True)
    grants = [p for p in production.producers if p.name == MONTHLY_GRANT_PRODUCER]
    assert all(p.period == DAILY for p in grants)
    per_month = 365 / 12
    assert production.rate_per_minute("apple cider") == pytest.approx(
        3000 / per_month / 1440
    )
    assert production.rate_per_minute("arnold palmer") == pytest.approx(
        2000 / per_month / 1440
    )


def test_monthly_grants_count_the_days_that_land_in_the_plan_window():
    # A 31-day plan from the 1st catches one 7th (cider) and one 14th (AP), each
    # spread evenly across the 31 days.
    production = passive_production(
        {}, start=date(2026, 7, 1), horizon=timedelta(days=31), monthly_grants=True
    )
    assert production.rate_per_minute("apple cider") == pytest.approx(3000 / 31 / 1440)
    assert production.rate_per_minute("arnold palmer") == pytest.approx(2000 / 31 / 1440)


def test_monthly_grant_that_never_lands_in_the_window_is_dropped():
    # A 5-day plan from the 8th misses both the 7th and the 14th, so neither grant
    # produces anything and no tower-artifact producer is listed.
    production = passive_production(
        {}, start=date(2026, 7, 8), horizon=timedelta(days=5), monthly_grants=True
    )
    assert not any(p.name == MONTHLY_GRANT_PRODUCER for p in production.producers)


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


def test_trading_prices_converts_gk_and_cookie():
    # gk values are gold per 1000 (divided by 1000); cookie sets are gold each.
    # With crafting_bonus 1, a juice is its full 200 grapes at 5 apk, each grape
    # worth the Arnold Palmer's per-unit gold: 200 * (5/1000) * 0.06 = 0.06.
    prices = trading_prices(
        {
            "trading": {
                "orange_juice": 8,
                "apple_cider": 20,
                "arnold_palmer": 60,
                "large_net": 10,
                "grapes": 5,
                "cookie_set": 20,
                "hickory_omelette": 350,
            }
        },
        crafting_bonus=1.0,
    )
    assert prices == {
        "arnold palmer": 0.06,
        "large net": 0.01,
        "apple cider": 0.02,
        "orange juice": 0.008,
        "grape juice": 0.06,
        "cookie set": 20.0,
        "hickory omelette": 0.35,
    }


def test_trading_prices_grape_juice_is_a_production_cost():
    # Grape Juice is priced at its production cost: 200 grapes make crafting_bonus
    # juices, so a juice is 200/1.45 grapes -- 0.06 / 1.45 gold at the above rates.
    prices = trading_prices(
        {"trading": {"arnold_palmer": 60, "grapes": 5}}, crafting_bonus=1.45
    )
    assert prices["grape juice"] == pytest.approx(0.06 / 1.45)


def test_trading_prices_grape_juice_free_without_grape_price():
    # Absent a grape price, Grape Juice stays free (the earlier behaviour).
    prices = trading_prices({"trading": {"arnold_palmer": 60}})
    assert prices["grape juice"] == 0.0


def test_trading_prices_absent_section_is_none():
    assert trading_prices({"vineyard": {"grapes": 1}}) is None


def _ap_cfg(**ap):
    return {"trading": {"arnold_palmer": 60, "ap": {"enabled": True, **ap}}}


def test_arnold_palmer_prices_converts_apk_to_per_unit():
    prices = config.arnold_palmer_prices(_ap_cfg(prices={"red_dye": 8, "stone": 1}))
    # apk / 1000 = per-unit AP; keys normalise underscores to spaces.
    assert prices == {"red dye": 0.008, "stone": 0.001}


def test_arnold_palmer_prices_disabled_or_absent_is_empty():
    assert config.arnold_palmer_prices({"trading": {"arnold_palmer": 60}}) == {}
    off = {"trading": {"arnold_palmer": 60, "ap": {"enabled": False, "prices": {"red_dye": 8}}}}
    assert config.arnold_palmer_prices(off) == {}


def test_arnold_palmer_prices_enabled_override():
    off = {"trading": {"arnold_palmer": 60, "ap": {"enabled": False, "prices": {"red_dye": 8}}}}
    assert config.arnold_palmer_prices(off, enabled_override=True) == {"red dye": 0.008}
    on = _ap_cfg(prices={"red_dye": 8})
    assert config.arnold_palmer_prices(on, enabled_override=False) == {}


def test_arnold_palmer_prices_allow_and_deny_filters():
    both = _ap_cfg(prices={"red_dye": 8, "stone": 1, "grapes": 5})
    assert config.arnold_palmer_prices({**both, "trading": {**both["trading"], "ap": {**both["trading"]["ap"], "allow": ["red dye"]}}}) == {"red dye": 0.008}
    deny = _ap_cfg(prices={"red_dye": 8, "stone": 1}, deny=["stone"])
    assert config.arnold_palmer_prices(deny) == {"red dye": 0.008}


def test_arnold_palmer_prices_allow_and_deny_together_raises():
    cfg = _ap_cfg(prices={"red_dye": 8}, allow=["red dye"], deny=["stone"])
    with pytest.raises(ValueError, match="only one"):
        config.arnold_palmer_prices(cfg)


def test_arnold_palmer_prices_ignores_the_price_bounds_table():
    # [trading.ap.price_bounds] is UI-only metadata; the solver must not read it.
    cfg = _ap_cfg(prices={"red_dye": 8}, price_bounds={"red_dye": [8, 10]})
    assert config.arnold_palmer_prices(cfg) == {"red dye": 0.008}


@pytest.mark.parametrize("path", ["config.toml", "web_default_config.toml"])
def test_config_price_bounds_align_with_prices(path):
    # The generated [trading.ap.price_bounds] guide table must cover exactly the
    # priced items, each a [low, high] with low == the configured price and
    # low <= high (see scripts/generate_trade_prices.py).
    from pathlib import Path

    cfg = config.load_config(Path(path))
    ap = cfg["trading"]["ap"]
    prices, bounds = ap["prices"], ap["price_bounds"]
    assert set(bounds) == set(prices)
    for item, price in prices.items():
        low, high = bounds[item]
        assert low == price and low <= high


def test_trading_prices_folds_ap_items_as_gold_equivalent():
    # An AP-traded item's gold price is its per-unit AP * the Arnold Palmer's own
    # per-unit gold (60 gk -> 0.06 g): red dye at 8 apk -> 0.008 * 0.06.
    prices = trading_prices(_ap_cfg(prices={"red_dye": 8}))
    assert prices["red dye"] == pytest.approx(0.008 * 0.06)
    # A consumable already priced in gold keeps its gold price (not overwritten).
    both = _ap_cfg(prices={"red_dye": 8})
    both["trading"]["large_net"] = 10
    assert trading_prices(both)["large net"] == 0.01


def test_is_smeared_producer_flags_pets_and_grants():
    # Pets' daily output and the Mechanical Heart's monthly grants are averaged
    # rates standing in for lumpy real events; buildings are real depositors.
    assert is_smeared_producer("pet Dog")
    assert is_smeared_producer(MONTHLY_GRANT_PRODUCER)
    assert not is_smeared_producer("sawmill")


def test_current_tower_level_absent_is_none():
    assert current_tower_level({}) is None
    assert current_tower_level({"tower": {}}) is None


def test_current_tower_level_reads_valid_value():
    assert current_tower_level({"tower": {"level": 250}}) == 250
    assert current_tower_level({"tower": {"level": 0}}) == 0


def test_current_tower_level_rejects_negative_or_non_int():
    with pytest.raises(ValueError, match="non-negative integer"):
        current_tower_level({"tower": {"level": -1}})
    with pytest.raises(ValueError, match="non-negative integer"):
        current_tower_level({"tower": {"level": 12.5}})
    with pytest.raises(ValueError, match="non-negative integer"):
        current_tower_level({"tower": {"level": "250"}})


def test_crafting_bonus_for_absent_workshop_is_the_crafting_default():
    assert crafting_bonus_for({}, None) == crafting.DEFAULT_CRAFTING_BONUS
    assert crafting_bonus_for({}, 42) == crafting.DEFAULT_CRAFTING_BONUS


def test_crafting_bonus_for_each_resource_saver_level():
    assert crafting_bonus_for({"workshop": {"resource_saver": 1}}, None) == 1.10
    assert crafting_bonus_for({"workshop": {"resource_saver": 2}}, None) == 1.25
    assert crafting_bonus_for({"workshop": {"resource_saver": 3}}, None) == 1.45


def test_crafting_bonus_for_clamps_resource_saver_3_below_level_90():
    # Resource Saver 3 IS the level-90 tower reward, so a configured 3 below
    # that level is capped back to 2's bonus.
    cfg = {"workshop": {"resource_saver": 3}}
    assert crafting_bonus_for(cfg, 89) == 1.25


def test_crafting_bonus_for_forces_3_at_level_90():
    # The level-90 reward grants Resource Saver 3 regardless of what is configured.
    cfg = {"workshop": {"resource_saver": 2}}
    assert crafting_bonus_for(cfg, 90) == 1.45


def test_crafting_bonus_for_none_level_uses_configured_value_as_is():
    # None means "assume every level-gated reward owned" -- no clamp at all.
    assert crafting_bonus_for({"workshop": {"resource_saver": 3}}, None) == 1.45
    assert crafting_bonus_for({"workshop": {"resource_saver": 1}}, None) == 1.10


def test_crafting_bonus_for_rejects_invalid_resource_saver():
    with pytest.raises(ValueError, match="resource_saver"):
        crafting_bonus_for({"workshop": {"resource_saver": 4}}, None)
    with pytest.raises(ValueError, match="resource_saver"):
        crafting_bonus_for({"workshop": {"resource_saver": "3"}}, None)


def test_effective_grape_juices_doubles_when_level_absent():
    assert effective_grape_juices({"farming": {"grape_juices_per_day": 7}}, None) == 14


def test_effective_grape_juices_level_150_boundary():
    cfg = {"farming": {"grape_juices_per_day": 7}}
    assert effective_grape_juices(cfg, 149) == 7
    assert effective_grape_juices(cfg, 150) == 14


def test_effective_grape_juices_absent_key_default_preserves_todays_behaviour():
    # crops.farm_from_config's own missing-key default is 0, unaffected by
    # doubling either way.
    assert effective_grape_juices({}, None) == 0
    assert effective_grape_juices({}, 10) == 0


def test_passive_quantizer_wires_the_smeared_predicate_through():
    production = PassiveProduction(
        producers=[
            PassiveProducer("raptor pen", DAILY, {"antlers": 100}),
            PassiveProducer("pet Dog", DAILY, {"antlers": 50}),
        ]
    )
    quantize = passive_quantizer(production)
    # Matches calling quantized_wait with is_smeared_producer directly: the
    # pet's share ramps continuously while the pen's deposits step, so 100
    # antlers arrive with the pen's first whole deposit at day one -- treating
    # the pet as a real depositor too would say 12 hours (staggered half-day
    # slots), and ignoring it entirely would still say a day; the point is the
    # bound partial gives the same schedule as the explicit predicate.
    expected = production.quantized_wait("antlers", 100, is_smeared_producer)
    assert quantize("antlers", 100) == expected == timedelta(days=1)
