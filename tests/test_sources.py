from datetime import timedelta

import pytest

from crops import Crop, Farm
from exploring import (
    CiderModifiers,
    Exploring,
    ExploringLocation,
    ExploringModifiers,
)
from fishing import Fishing, FishingModifiers, FishingZone
from locksmith import Locksmith, Openable, PoolItem
from passive import DAILY, HOURLY, PassiveProducer, PassiveProduction
from sources import (
    CIDER_DROP_RATE,
    DRINKING_TRACK,
    Draw,
    Offer,
    aggregate,
    cider_offers,
    exploring_offers,
    farming_offers,
    fishing_offers,
    open_flows,
    passive_offers,
    stamina_offers,
)


def test_units_for_is_driven_by_the_binding_item():
    offer = Offer(name="fish Lake", track="fishing", yields={"trout": 100, "minnow": 25})
    # 500 trout needs 5 units; 25 minnow only 1 -- the trout binds, and the
    # minnow rides along for free.
    assert offer.units_for({"trout": 500, "minnow": 25}) == pytest.approx(5)


def test_units_for_ignores_items_outside_the_bundle():
    offer = Offer(name="fish Lake", track="fishing", yields={"trout": 100})
    assert offer.units_for({"salmon": 999}) == 0


def test_units_for_matches_items_case_insensitively():
    offer = Offer(name="fish Lake", track="fishing", yields={"trout": 100})
    assert offer.units_for({"Trout": 200}) == pytest.approx(2)


def test_draw_scales_the_whole_bundle_and_the_costs():
    offer = Offer(
        name="explore Cave",
        track="exploring",
        yields={"wood": 80, "stone": 20},
        ap=1,
    )
    draw = Draw(offer, units=3)
    assert draw.produced == {"wood": 240, "stone": 60}
    assert draw.ap == 3
    assert draw.ln == 0
    assert draw.time == timedelta(0)


def test_aggregate_sums_within_a_track_and_takes_the_max_across_tracks():
    corn = Offer("grow Corn", "farming", {"corn": 1}, time_per_unit=timedelta(hours=1))
    beet = Offer("grow Beet", "farming", {"beet": 1}, time_per_unit=timedelta(hours=2))
    fish = Offer("fish Lake", "fishing", {"trout": 1}, ln=1)
    time, ap, ln = aggregate(
        [Draw(corn, 1), Draw(beet, 1), Draw(fish, 10)]
    )
    # Farming track: 1h + 2h = 3h; fishing track: instant. Max is 3h.
    assert time == timedelta(hours=3)
    assert ap == 0
    assert ln == 10


def test_aggregate_of_nothing_is_zero():
    assert aggregate([]) == (timedelta(0), 0, 0)


def test_fishing_offers_carry_the_zone_bundle_and_cost_a_net():
    fishing = Fishing(
        modifiers=FishingModifiers(),  # base catch 250
        zones={"lake": FishingZone("Lake", {"trout": 0.4, "minnow": 0.1})},
    )
    (offer,) = fishing_offers(fishing)
    assert offer.name == "fish Lake"
    assert offer.ln == 1 and offer.ap == 0
    # 250 fish * the drop rates.
    assert offer.yields == {"trout": 100.0, "minnow": 25.0}


def test_exploring_offers_carry_the_location_bundle_and_cost_a_palmer():
    exploring = Exploring(
        modifiers=ExploringModifiers(),  # base yield 200
        locations={"cave": ExploringLocation("Cave", {"wood": 0.4, "stone": 0.1})},
    )
    (offer,) = exploring_offers(exploring)
    assert offer.name == "explore Cave"
    assert offer.ap == 1 and offer.ln == 0
    assert offer.yields == {"wood": 80.0, "stone": 20.0}


def test_farming_offers_carry_the_harvest_bundle_and_grow_time():
    farm = Farm(
        crops={
            "carrot": Crop("Carrot", timedelta(hours=1), {"carrot": 4, "gold carrot": 0.1})
        },
        rows=10,
    )
    (offer,) = farming_offers(farm)
    assert offer.name == "grow Carrot"
    assert offer.ap == 0 and offer.ln == 0
    # per_harvest = rows * rate: 40 carrots and 1 gold carrot a planting.
    assert offer.yields == {"carrot": pytest.approx(40), "gold carrot": pytest.approx(1)}
    # No grape juice, so one planting takes the full grow time.
    assert offer.time_per_unit == timedelta(hours=1)


def test_farming_offer_time_shortens_with_grape_juice():
    farm = Farm(
        crops={"slow": Crop("Slow", timedelta(days=1), {"slow": 1})},
        rows=1,
        grape_juices_per_day=11,
    )
    blended, natural = farming_offers(farm)
    # 1 natural + 11 instant harvests a day = 12, so one planting is 2 hours, and
    # its Grape Juice share is 11/12 of a juice.
    assert blended.name == "grow Slow"
    assert blended.time_per_unit == timedelta(hours=2)
    assert blended.gj == pytest.approx(11 / 12)
    # The Grape-Juice-free alternative waits out the full day for one planting.
    assert natural.name == "grow Slow without grape juice"
    assert natural.time_per_unit == timedelta(days=1)
    assert natural.gj == 0


def test_passive_offer_bundles_the_whole_economy_at_per_minute_rates():
    production = PassiveProduction(
        producers=[
            PassiveProducer("sawmill", HOURLY, {"board": 6000, "wood": 600}),
            PassiveProducer("pet cat", DAILY, {"drum": 1440}),
        ]
    )
    (offer,) = passive_offers(production)
    assert offer.name == "passive" and offer.track == "passive"
    assert offer.ap == 0 and offer.ln == 0
    assert offer.time_per_unit == timedelta(minutes=1)
    # 6000/hr = 100/min, 600/hr = 10/min, 1440/day = 1/min. Pets are in the bundle.
    assert offer.yields == {
        "board": pytest.approx(100),
        "wood": pytest.approx(10),
        "drum": pytest.approx(1),
    }


def test_passive_offer_adds_rates_across_producers_of_one_item():
    production = PassiveProduction(
        producers=[
            PassiveProducer("a", HOURLY, {"steel": 600}),
            PassiveProducer("b", HOURLY, {"steel": 1200}),
        ]
    )
    (offer,) = passive_offers(production)
    # 600/hr + 1200/hr = 1800/hr = 30/min: the concurrent producers' rates add.
    assert offer.yields["steel"] == pytest.approx(30)


def test_passive_offer_units_take_the_longest_item_wait():
    production = PassiveProduction(
        producers=[PassiveProducer("mill", HOURLY, {"board": 60, "wood": 6})]
    )
    (offer,) = passive_offers(production)
    # board at 1/min needs 100 min; wood at 0.1/min needs 100 too. 300 wood binds
    # at 3000 min while 100 board would only need 100 -- the max drives it.
    assert offer.units_for({"board": 100, "wood": 300}) == pytest.approx(3000)


def test_passive_offer_is_empty_without_producers():
    assert passive_offers(PassiveProduction(producers=[])) == []


def test_open_flow_for_a_chest_consumes_the_chest_and_its_key():
    smith = Locksmith(
        openables={
            "small chest 01": Openable(
                name="Small Chest 01", key="Small Key", fixed={"ruby": 10, "jade": 10}
            )
        }
    )
    (flow,) = open_flows(smith)
    assert flow.name == "Small Chest 01"
    # One open consumes the chest token and one Small Key.
    assert flow.consumes == {"small chest 01": 1.0, "small key": 1.0}
    assert flow.yields == {"ruby": 10, "jade": 10}


def test_open_flow_for_a_bag_consumes_only_itself():
    smith = Locksmith(
        openables={
            "grab bag": Openable(
                name="Grab Bag", pool=[PoolItem("Silver", 1, 3), PoolItem("Gold", 1, 3)]
            )
        }
    )
    (flow,) = open_flows(smith)
    assert flow.consumes == {"grab bag": 1.0}
    # Average bundle: each of two picks averages 2/2 = 1.
    assert flow.yields == {"silver": 1.0, "gold": 1.0}


def test_open_flows_skips_empty_openables():
    smith = Locksmith(openables={"empty": Openable(name="Empty")})
    assert open_flows(smith) == []


def test_farming_offers_add_a_cookie_day_for_qualifying_crops():
    farm = Farm(
        crops={"slow": Crop("Slow", timedelta(hours=1), {"slow": 1})},
        rows=1,
        grape_juices_per_day=2,
        cookie_time_threshold=timedelta(minutes=18),
    )
    offers = farming_offers(farm)
    names = {o.name for o in offers}
    assert names == {
        "grow Slow",
        "grow Slow without grape juice",
        "grow Slow with cookies",
    }
    cookie = next(o for o in offers if o.cookies)
    assert cookie.cookies == 1
    assert cookie.gj == 2  # a cookie day spends the day's Grape Juice allowance
    assert cookie.track == "farming"
    assert cookie.time_per_unit == timedelta(days=1)
    # A boosted day: (24 - 1) plain + 9*(1 natural + 2 grape-juice) boosted = 50.
    assert cookie.yields == {"slow": pytest.approx(50)}


def test_farming_offer_excludes_grape_juice_for_instant_crops():
    farm = Farm(
        crops={"peppers": Crop("Peppers", timedelta(minutes=1), {"peppers": 1})},
        rows=1,
        grape_juices_per_day=14,
        instant_grow_time=timedelta(seconds=3),
    )
    (offer,) = farming_offers(farm)  # fast crop, so no cookie offer either
    # No Grape Juice folded in, so a planting is exactly the instant grow time.
    assert offer.time_per_unit == timedelta(seconds=3)


def test_farming_offers_skip_cookies_for_fast_crops():
    farm = Farm(
        crops={"fast": Crop("Fast", timedelta(minutes=5), {"fast": 1})},
        rows=1,
        grape_juices_per_day=2,
        cookie_time_threshold=timedelta(minutes=18),
    )
    assert all(not o.cookies for o in farming_offers(farm))


def test_draw_scales_cookie_cost():
    offer = Offer("grow Slow with cookies", "farming", {"slow": 42}, cookies=1,
                  time_per_unit=timedelta(days=1))
    assert Draw(offer, units=3).cookies == 3


def test_farming_offer_time_matches_time_to_grow():
    farm = Farm(
        crops={"beet": Crop("Beet", timedelta(days=9), {"beet": 4})},
        rows=13,
        grape_juices_per_day=14,
    )
    offer = next(o for o in farming_offers(farm) if o.name == "grow Beet")
    # Ten full-field plantings via the blended offer should equal Farm.time_to_grow
    # for exactly ten plantings' worth of beet (52 a planting at 13 rows).
    quantity = 10 * round(farm.per_harvest("Beet"))
    grown = farm.time_to_grow("Beet", quantity)
    assert (offer.time_per_unit * 10).total_seconds() == pytest.approx(
        grown.total_seconds()
    )


def test_farming_offers_fold_in_the_dewstar_bonus():
    farm = Farm(
        crops={
            "peppers": Crop("Peppers", timedelta(minutes=1), {"peppers": 1}),
            "carrot": Crop("Carrot", timedelta(minutes=2), {"carrot": 4}),
        },
        rows=1,
        dewstar=True,
    )
    offers = {o.name: o for o in farming_offers(farm)}
    # 4 plots split between the two eligible crops (Peppers, Carrot): +2 each.
    assert offers["grow Carrot"].yields == {
        "carrot": pytest.approx(4 + 2),
        "peppers": pytest.approx(2),
    }


def test_farming_offers_dewstar_bonus_without_grape_juice_matches_the_gj_offer():
    farm = Farm(
        crops={
            "peppers": Crop("Peppers", timedelta(minutes=1), {"peppers": 1}),
            "slow": Crop("Slow", timedelta(days=1), {"slow": 1}),
        },
        rows=1,
        grape_juices_per_day=11,
        dewstar=True,
    )
    offers = {o.name: o for o in farming_offers(farm)}
    # Slow (1d) only ever qualifies alongside Peppers (1m): 4 plots / 2 crops.
    assert offers["grow Slow"].yields == {
        "slow": pytest.approx(1 + 2), "peppers": pytest.approx(2),
    }
    assert offers["grow Slow without grape juice"].yields == offers["grow Slow"].yields


def test_farming_offers_dewstar_cookie_bonus_uses_the_journal_rules():
    farm = Farm(
        crops={
            "peppers": Crop("Peppers", timedelta(minutes=1), {"peppers": 1}),
            "x": Crop("X", timedelta(hours=1), {"x": 1}),
        },
        rows=1,
        grape_juices_per_day=2,
        cookie_time_threshold=timedelta(minutes=18),
        dewstar=True,
    )
    cookie = next(o for o in farming_offers(farm) if o.name == "grow X with cookies")
    # Without the journal the bonus is flat: 26 harvests (24 natural + 2 GJ) a day,
    # split 4/2 crops = 2 each, so 52 of each bonus item; X's own cookie yield (50)
    # is unaffected.
    assert cookie.yields == {
        "x": pytest.approx(50 + 52),
        "peppers": pytest.approx(52),
    }


def test_farming_offers_dewstar_cookie_bonus_boosted_with_the_journal():
    farm = Farm(
        crops={
            "peppers": Crop("Peppers", timedelta(minutes=1), {"peppers": 1}),
            "x": Crop("X", timedelta(hours=1), {"x": 1}),
        },
        rows=1,
        grape_juices_per_day=2,
        cookie_time_threshold=timedelta(minutes=18),
        dewstar=True,
        dewstar_journal=True,
    )
    cookie = next(o for o in farming_offers(farm) if o.name == "grow X with cookies")
    # With the journal the bonus follows the same boosted count (50) as X's own
    # cookie yield: 2 a harvest * 50 = 100 of each bonus item.
    assert cookie.yields == {
        "x": pytest.approx(50 + 100),
        "peppers": pytest.approx(100),
    }


def test_farming_offers_no_dewstar_bonus_when_off():
    farm = Farm(
        crops={
            "peppers": Crop("Peppers", timedelta(minutes=1), {"peppers": 1}),
            "carrot": Crop("Carrot", timedelta(minutes=2), {"carrot": 4}),
        },
        rows=1,
    )
    (offer,) = [o for o in farming_offers(farm) if o.name == "grow Carrot"]
    assert offer.yields == {"carrot": pytest.approx(4)}


# --- Inventory plan mode: grounded consumables + Apple Cider -------------------


def test_grounded_exploring_consumes_a_real_arnold_palmer():
    exploring = Exploring(locations={"cave": ExploringLocation("Cave", {"wood": 0.4})})
    [offer] = exploring_offers(exploring, ground=True)
    assert offer.ap == 1  # still tallied/priced
    assert offer.consumes == {"arnold palmer": 1.0}


def test_ungrounded_exploring_has_no_consumes():
    exploring = Exploring(locations={"cave": ExploringLocation("Cave", {"wood": 0.4})})
    [offer] = exploring_offers(exploring)
    assert offer.consumes == {}


def test_grounded_fishing_consumes_a_real_large_net():
    fishing = Fishing(zones={"lake": FishingZone("Lake", {"trout": 0.4})})
    [offer] = fishing_offers(fishing, ground=True)
    assert offer.ln == 1
    assert offer.consumes == {"large net": 1.0}


def test_grounded_farming_consumes_grape_juice_matching_its_gj_cost():
    farm = Farm(
        crops={"grape": Crop("Grape", timedelta(hours=6), {"grape": 1})},
        rows=1,
        grape_juices_per_day=14,
    )
    offers = {o.name: o for o in farming_offers(farm, ground=True)}
    gj_offer = offers["grow Grape"]
    assert gj_offer.gj > 0
    assert gj_offer.consumes == {"grape juice": gj_offer.gj}
    # The grape-juice-free variant spends none, so it grounds nothing.
    assert offers["grow Grape without grape juice"].consumes == {}


def test_cider_offer_yields_drop_rate_times_zone_rates_and_consumes_stamina():
    exploring = Exploring(
        locations={"cave": ExploringLocation("Cave", {"wood": 0.5, "stone": 0.25})},
        cider=CiderModifiers(),  # 1 stamina/explore, 1010 explores at ee=1
        effectiveness={"cave": 1},
    )
    [offer] = cider_offers(exploring)
    assert offer.name == "cider Cave"
    assert offer.yields == {
        "wood": CIDER_DROP_RATE * 0.5,
        "stone": CIDER_DROP_RATE * 0.25,
    }
    # One explore costs one stamina and 1/1010 of a cider (ee=1, no cinnamon).
    assert offer.consumes["stamina"] == 1.0
    assert offer.consumes["apple cider"] == pytest.approx(1 / 1010)


def test_cider_offers_skip_zones_without_configured_effectiveness():
    exploring = Exploring(
        locations={
            "cave": ExploringLocation("Cave", {"wood": 0.5}),
            "forest": ExploringLocation("Forest", {"fern": 0.5}),
        },
        effectiveness={"cave": 10},
    )
    assert [o.name for o in cider_offers(exploring)] == ["cider Cave"]


def test_stamina_offers_turn_orange_juice_and_apples_into_stamina():
    by_name = {o.name: o for o in stamina_offers()}
    assert set(by_name) == {"drink orange juice", "eat apple"}

    juice = by_name["drink orange juice"]
    assert juice.track == DRINKING_TRACK
    assert juice.yields == {"stamina": 100.0}
    assert juice.consumes == {"orange juice": 1.0}
    assert juice.time_per_unit == timedelta(0)

    apple = by_name["eat apple"]
    assert apple.track == DRINKING_TRACK
    assert apple.yields == {"stamina": 15.0}
    assert apple.consumes == {"apple": 1.0}
    assert apple.time_per_unit == timedelta(0)
