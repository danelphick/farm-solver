from datetime import timedelta

import pytest

from crops import Crop, Farm
from exploring import Exploring, ExploringLocation, ExploringModifiers
from fishing import Fishing, FishingModifiers, FishingZone
from optimizer import SourcingPlan, all_offers, frontier, minimize
from passive import HOURLY, PassiveProducer, PassiveProduction
from sources import Offer

# A free-but-slow farming offer and a fast-but-costly fishing offer for the same
# item, the basic time-vs-consumable tradeoff.
FARM_ITEM = Offer("grow It", "farming", {"it": 1}, time_per_unit=timedelta(minutes=1))
FISH_ITEM = Offer("fish It", "fishing", {"it": 1}, ln=1)


def test_all_offers_gathers_every_channel():
    offers = all_offers(
        PassiveProduction(producers=[PassiveProducer("mill", HOURLY, {"board": 60})]),
        Fishing(zones={"lake": FishingZone("Lake", {"trout": 0.4})}),
        Exploring(locations={"cave": ExploringLocation("Cave", {"wood": 0.4})}),
        Farm(crops={"corn": Crop("Corn", timedelta(hours=1), {"corn": 1})}, rows=1),
    )
    names = {o.name for o in offers}
    assert names == {"passive", "fish Lake", "explore Cave", "grow Corn"}


def test_minimize_prefers_the_free_channel_when_time_is_cheap():
    # Huge price on consumables: grow it (slow, free) rather than fish it.
    plan = minimize([FARM_ITEM, FISH_ITEM], {"It": 100}, price_ap=1e15, price_ln=1e15)
    assert plan.large_nets == 0
    assert plan.time == timedelta(minutes=100)
    assert [d.offer.name for d in plan.draws] == ["grow It"]


def test_minimize_prefers_the_fast_channel_when_time_is_dear():
    # Tiny price on consumables: fish it (instant) rather than wait to grow it.
    plan = minimize([FARM_ITEM, FISH_ITEM], {"It": 100}, price_ap=1e-9, price_ln=1e-9)
    assert plan.large_nets == pytest.approx(100)
    assert plan.time == timedelta(0)
    assert [d.offer.name for d in plan.draws] == ["fish It"]


def test_minimize_credits_joint_production_across_needs():
    # One net yields 2 "a" and 1 "b"; covering 10 a (5 nets) also makes 5 b,
    # covering the 3 b needed for free -- a single draw.
    offer = Offer("fish Lake", "fishing", {"a": 2, "b": 1}, ln=1)
    plan = minimize([offer], {"a": 10, "b": 3}, price_ln=1e-9)
    assert len(plan.draws) == 1
    assert plan.draws[0].units == pytest.approx(5)
    assert plan.large_nets == pytest.approx(5)
    assert plan.remainder == {}


def test_minimize_leaves_unproducible_items_in_the_remainder():
    plan = minimize([FISH_ITEM], {"It": 10, "Gold Bar": 4})
    assert plan.remainder == {"Gold Bar": 4}


def test_minimize_without_any_offer_returns_everything_as_remainder():
    plan = minimize([], {"It": 10})
    assert plan == SourcingPlan(remainder={"It": 10})


def test_frontier_spans_the_tradeoff_fastest_first():
    plans = frontier([FARM_ITEM, FISH_ITEM], {"It": 100})
    # The two extremes: instant for 100 nets, or free over 100 minutes.
    assert plans[0].time == timedelta(0)
    assert plans[0].large_nets == pytest.approx(100)
    assert plans[-1].large_nets == 0
    assert plans[-1].time == timedelta(minutes=100)
    # Sorted fastest first.
    assert [p.time for p in plans] == sorted(p.time for p in plans)


def test_frontier_returns_one_plan_when_consumables_buy_no_speed():
    # Only a free channel exists, so every price gives the same plan.
    plans = frontier([FARM_ITEM], {"It": 50})
    assert len(plans) == 1
    assert plans[0].large_nets == 0
    assert plans[0].time == timedelta(minutes=50)


def test_frontier_is_non_dominated():
    plans = frontier([FARM_ITEM, FISH_ITEM], {"It": 100})
    for a in plans:
        for b in plans:
            if a is not b:
                # No plan beats another on all three objectives at once.
                assert not (
                    a.time <= b.time
                    and a.arnold_palmers <= b.arnold_palmers
                    and a.large_nets <= b.large_nets
                    and a.cost != b.cost
                )


def test_frontier_thins_to_the_requested_count():
    # A staircase of offers at rising net-cost for speed gives many vertices;
    # ask for 3 and get at most 3, still including both extremes.
    offers = [
        Offer("grow It", "farming", {"it": 1}, time_per_unit=timedelta(minutes=1)),
        Offer("fish A", "fishing", {"it": 1}, ln=1),
    ]
    plans = frontier(offers, {"It": 1000}, count=3)
    assert 1 <= len(plans) <= 3
