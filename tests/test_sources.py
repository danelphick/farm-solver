from datetime import timedelta

import pytest

from crops import Crop, Farm
from exploring import Exploring, ExploringLocation, ExploringModifiers
from fishing import Fishing, FishingModifiers, FishingZone
from sources import (
    Draw,
    Offer,
    aggregate,
    exploring_offers,
    farming_offers,
    fishing_offers,
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
    (offer,) = farming_offers(farm)
    # 1 natural + 11 instant harvests a day = 12, so one planting is 2 hours.
    assert offer.time_per_unit == timedelta(hours=2)


def test_farming_offer_time_matches_time_to_grow():
    farm = Farm(
        crops={"beet": Crop("Beet", timedelta(days=9), {"beet": 4})},
        rows=13,
        grape_juices_per_day=14,
    )
    (offer,) = farming_offers(farm)
    # Ten full-field plantings via the offer should equal Farm.time_to_grow for
    # exactly ten plantings' worth of beet (52 a planting at 13 rows).
    quantity = 10 * round(farm.per_harvest("Beet"))
    grown = farm.time_to_grow("Beet", quantity)
    assert (offer.time_per_unit * 10).total_seconds() == pytest.approx(
        grown.total_seconds()
    )
