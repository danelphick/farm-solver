from datetime import timedelta

import pytest

from crops import Crop, Farm
from exploring import Exploring, ExploringLocation
from fishing import Fishing, FishingZone
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


def test_minimize_crafts_when_cheaper_than_gathering():
    # Glass Bottle can be fished (1 net each) or crafted from Glass Orb, which
    # explores 10-to-a-palmer. Crafting 100 costs ~10 palmers vs ~100 nets, so
    # the optimiser crafts rather than fishes.
    offers = [
        Offer("fish Lake", "fishing", {"glass bottle": 1}, ln=1),
        Offer("explore Cave", "exploring", {"glass orb": 10}, ap=1),
    ]
    recipes = {"glass bottle": {"glass orb": 1}}
    plan = minimize(
        offers, {"Glass Bottle": 100}, recipes, 1.0, price_ap=1e-6, price_ln=1e-6
    )
    assert plan.large_nets == 0
    assert plan.crafts.get("glass bottle") == pytest.approx(100)
    assert plan.arnold_palmers == pytest.approx(10)


def test_minimize_gathers_when_cheaper_than_crafting():
    # If the same bottle fishes 100-to-a-net, fishing beats crafting from orbs.
    offers = [
        Offer("fish Lake", "fishing", {"glass bottle": 100}, ln=1),
        Offer("explore Cave", "exploring", {"glass orb": 10}, ap=1),
    ]
    recipes = {"glass bottle": {"glass orb": 1}}
    plan = minimize(
        offers, {"Glass Bottle": 100}, recipes, 1.0, price_ap=1e-6, price_ln=1e-6
    )
    assert plan.crafts == {}
    assert plan.large_nets == pytest.approx(1)


def test_minimize_does_not_craft_surplus_into_unneeded_items():
    # Passive yields Milk plus 1000 Board a minute as a co-product, so covering
    # 100 Butter (which needs 100 Milk, ~100 minutes of passive) leaves ~100,000
    # surplus Board. The Barrel->Churn->Butter chain only needs 100 Barrels, but
    # crafting more costs no time or consumable, so the bare objective is
    # indifferent and would spin the whole Board surplus into Barrels. The
    # least-craft tie-break must craft only the 100 the demand actually needs.
    passive = Offer(
        "passive", "passive", {"milk": 1, "board": 1000}, time_per_unit=timedelta(minutes=1)
    )
    recipes = {
        "butter": {"milk": 1, "churn": 1},
        "churn": {"barrel": 1},
        "barrel": {"board": 1},
    }
    plan = minimize(
        [passive], {"Butter": 100}, recipes, 1.0, price_ap=1e-6, price_ln=1e-6
    )
    assert plan.crafts.get("barrel") == pytest.approx(100)


def test_minimize_leaves_items_needing_unobtainable_materials_in_remainder():
    # Widget is only craftable, and its material has no source, so it cannot be
    # made -- it falls to the remainder rather than making the LP infeasible.
    recipes = {"widget": {"unobtanium": 1}}
    plan = minimize([], {"Widget": 5}, recipes)
    assert plan.remainder == {"Widget": 5}
    assert plan.crafts == {}


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


def test_frontier_fills_a_flat_segment_a_price_sweep_would_skip():
    # Two items, each farmable (1 min each, one sequential track) or fishable (1
    # net each), with identical time-per-net. Farming both (200 min, 0 nets) and
    # fishing both (0 min, 200 nets) are the ends of a flat frontier segment, so
    # every weighted-sum price collapses onto one end -- a price sweep would find
    # only the extremes. The epsilon-constraint sweep must surface an interior
    # plan that trades some time for some nets.
    offers = [
        Offer("grow A", "farming", {"a": 1}, time_per_unit=timedelta(minutes=1)),
        Offer("grow B", "farming", {"b": 1}, time_per_unit=timedelta(minutes=1)),
        Offer("fish A", "fishing", {"a": 1}, ln=1),
        Offer("fish B", "fishing", {"b": 1}, ln=1),
    ]
    plans = frontier(offers, {"A": 100, "B": 100}, count=5)
    interior = [
        p
        for p in plans
        if timedelta(0) < p.time < timedelta(minutes=200) and p.large_nets > 0
    ]
    assert interior, "the flat segment's interior was not sampled"


def test_frontier_thins_to_the_requested_count():
    # A staircase of offers at rising net-cost for speed gives many vertices;
    # ask for 3 and get at most 3, still including both extremes.
    offers = [
        Offer("grow It", "farming", {"it": 1}, time_per_unit=timedelta(minutes=1)),
        Offer("fish A", "fishing", {"it": 1}, ln=1),
    ]
    plans = frontier(offers, {"It": 1000}, count=3)
    assert 1 <= len(plans) <= 3
