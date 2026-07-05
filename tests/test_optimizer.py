from datetime import timedelta

import pytest

from crops import Crop, Farm
from exploring import Exploring, ExploringLocation
from fishing import Fishing, FishingZone
from optimizer import (
    SourcingPlan,
    _passive_completion_caps,
    _thin,
    all_offers,
    frontier,
    minimize,
)
from passive import HOURLY, PassiveProducer, PassiveProduction
from sources import Draw, Offer, OpenFlow

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


def test_minimize_does_not_farm_a_crop_nothing_needs():
    # Passive covers the demand over 100 minutes, which is the makespan. A crop on
    # its own farming track could grow for those 100 minutes for free (farming is
    # concurrent with passive, so it adds no time), producing a mushroom nothing
    # consumes. The least-activity tie-break must leave that crop unplanted.
    passive = Offer(
        "passive", "passive", {"it": 1}, time_per_unit=timedelta(minutes=1)
    )
    mushrooms = Offer(
        "grow Mushroom", "farming", {"mushroom": 10}, time_per_unit=timedelta(minutes=1)
    )
    plan = minimize([passive, mushrooms], {"It": 100}, price_ap=1e-6, price_ln=1e-6)
    grown = [d for d in plan.draws if d.offer.name == "grow Mushroom"]
    assert grown == []


def test_minimize_opens_a_chest_when_cheaper_than_gathering():
    # A Gem fishes one-to-a-net, or comes 100 to a chest that itself fishes
    # one-to-a-net. Opening 100 Gems is one chest (~1 net) vs ~100 direct nets, so
    # the optimiser fishes the chest and opens it.
    offers = [
        Offer("fish Gem", "fishing", {"gem": 1}, ln=1),
        Offer("fish Vault", "fishing", {"treasure chest": 1}, ln=1),
    ]
    chest = OpenFlow(
        name="Treasure Chest", consumes={"treasure chest": 1}, yields={"gem": 100}
    )
    plan = minimize(
        offers, {"Gem": 100}, price_ap=1e-6, price_ln=1e-6, openables=[chest]
    )
    assert plan.opens.get("Treasure Chest") == pytest.approx(1)
    assert plan.large_nets == pytest.approx(1)


def test_minimize_gathers_a_chest_and_its_key_to_open_it():
    # The chest needs a Key to open. Both the chest and the key must be fished, so
    # opening one chest for its 50 Gems costs two nets (chest + key).
    offers = [
        Offer("fish Vault", "fishing", {"locked chest": 1}, ln=1),
        Offer("fish Keys", "fishing", {"brass key": 1}, ln=1),
    ]
    chest = OpenFlow(
        name="Locked Chest",
        consumes={"locked chest": 1, "brass key": 1},
        yields={"gem": 50},
    )
    plan = minimize(
        offers, {"Gem": 50}, price_ap=1e-6, price_ln=1e-6, openables=[chest]
    )
    assert plan.opens.get("Locked Chest") == pytest.approx(1)
    assert plan.large_nets == pytest.approx(2)  # one chest + one key


def test_minimize_leaves_an_item_only_a_keyless_uncraftable_chest_gives_in_remainder():
    # The chest yields the Gem but nothing sources the chest itself, so the Gem is
    # unobtainable and lands in the remainder rather than being opened for free.
    chest = OpenFlow(name="Ghost Chest", consumes={"ghost chest": 1}, yields={"gem": 5})
    plan = minimize([], {"Gem": 5}, openables=[chest])
    assert plan.remainder == {"Gem": 5}
    assert plan.opens == {}


def test_minimize_ignores_openables_for_items_it_does_not_help():
    # An openable unrelated to the demand is simply not opened.
    chest = OpenFlow(name="Junk Box", consumes={"junk box": 1}, yields={"junk": 100})
    plan = minimize([FISH_ITEM], {"It": 10}, openables=[chest])
    assert plan.opens == {}
    assert plan.large_nets == pytest.approx(10)


def test_minimize_draws_intermediate_materials_from_inventory():
    # Widget crafts from one Gear each. With 100 Gears already in stock, covering
    # 100 Widgets needs no Gears fished at all.
    offers = [Offer("fish Gears", "fishing", {"gear": 1}, ln=1)]
    recipes = {"widget": {"gear": 1}}
    plan = minimize(
        offers, {"Widget": 100}, recipes, 1.0,
        price_ap=1e-6, price_ln=1e-6, inventory={"gear": 100},
    )
    assert plan.crafts.get("widget") == pytest.approx(100)
    assert plan.large_nets == pytest.approx(0)  # all Gears came from stock


def test_minimize_gathers_only_the_shortfall_beyond_inventory():
    # 100 Widgets need 100 Gears but only 30 are in stock, so 70 must be fished.
    offers = [Offer("fish Gears", "fishing", {"gear": 1}, ln=1)]
    recipes = {"widget": {"gear": 1}}
    plan = minimize(
        offers, {"Widget": 100}, recipes, 1.0,
        price_ap=1e-6, price_ln=1e-6, inventory={"gear": 30},
    )
    assert plan.large_nets == pytest.approx(70)


def test_minimize_does_not_credit_inventory_against_the_direct_demand():
    # The demand itself is already net of what is held (mastery progress or the
    # acquired count), so stock of a *demanded* item earns no extra credit: all
    # 100 are still gathered.
    plan = minimize(
        [FISH_ITEM], {"It": 100}, price_ap=1e-6, price_ln=1e-6, inventory={"it": 100}
    )
    assert plan.large_nets == pytest.approx(100)


def test_minimize_does_not_open_a_chest_when_inventory_covers_the_contents():
    # A Gem needs opening a chest, but 100 Gems are already held, so no chest is
    # fished or opened.
    offers = [Offer("fish Vault", "fishing", {"treasure chest": 1}, ln=1)]
    chest = OpenFlow(
        name="Treasure Chest", consumes={"treasure chest": 1}, yields={"gem": 100}
    )
    recipes = {"trinket": {"gem": 1}}
    plan = minimize(
        offers, {"Trinket": 100}, recipes, 1.0,
        price_ap=1e-6, price_ln=1e-6, openables=[chest], inventory={"gem": 100},
    )
    assert plan.opens == {}
    assert plan.large_nets == pytest.approx(0)
    assert plan.crafts.get("trinket") == pytest.approx(100)


def test_minimize_spends_a_cookie_day_to_speed_farming():
    # One cookie-boosted day grows 10 Slow; a plain planting grows 1 a day. To get
    # 10 Slow the cookie day is one day versus ten, so the optimiser spends the set.
    normal = Offer("grow Slow", "farming", {"slow": 1}, time_per_unit=timedelta(days=1))
    cookie = Offer(
        "grow Slow with cookies", "farming", {"slow": 10}, cookies=1,
        time_per_unit=timedelta(days=1),
    )
    plan = minimize(
        [normal, cookie], {"Slow": 10}, price_ap=1e-6, price_ln=1e-6, cookie_sets=1
    )
    assert plan.cookie_sets == pytest.approx(1)
    assert plan.time == timedelta(days=1)


def test_minimize_respects_the_cookie_cap():
    normal = Offer("grow Slow", "farming", {"slow": 1}, time_per_unit=timedelta(days=1))
    cookie = Offer(
        "grow Slow with cookies", "farming", {"slow": 10}, cookies=1,
        time_per_unit=timedelta(days=1),
    )
    # No sets on hand: the boost is unavailable, so all ten days are plain farming.
    plan = minimize(
        [normal, cookie], {"Slow": 10}, price_ap=1e-6, price_ln=1e-6, cookie_sets=0
    )
    assert plan.cookie_sets == 0
    assert plan.time == timedelta(days=10)


def test_minimize_does_not_spend_cookies_off_the_critical_path():
    # Passive sets a 100-day makespan; farming's ten Slow fit in ten plain days
    # under that, so a boost would not finish the plan any sooner. Priced even a
    # hair, a cookie that saves no time is a net cost, so none are spent.
    normal = Offer("grow Slow", "farming", {"slow": 1}, time_per_unit=timedelta(days=1))
    cookie = Offer(
        "grow Slow with cookies", "farming", {"slow": 10}, cookies=1,
        time_per_unit=timedelta(days=1),
    )
    passive = Offer("passive", "passive", {"other": 1}, time_per_unit=timedelta(days=1))
    plan = minimize(
        [normal, cookie, passive], {"Slow": 10, "Other": 100},
        price_ap=1e-6, price_ln=1e-6, price_cookie=1e-6, price_gj=1e-6, cookie_sets=5,
    )
    assert plan.time == timedelta(days=100)
    assert plan.cookie_sets == 0


def test_minimize_avoids_priced_cookies_and_grape_juice_for_a_thrifty_plan():
    # Farming X three ways: plain natural (slow, free), with Grape Juice (faster,
    # a juice a planting), or a cookie day (fastest, a set and the day's juices).
    natural = Offer(
        "grow X without grape juice", "farming", {"x": 1}, time_per_unit=timedelta(hours=3)
    )
    juiced = Offer("grow X", "farming", {"x": 1}, gj=1, time_per_unit=timedelta(hours=1))
    cookie = Offer(
        "grow X with cookies", "farming", {"x": 10}, cookies=1, gj=2,
        time_per_unit=timedelta(days=1),
    )
    offers = [natural, juiced, cookie]
    # A high price on both makes the plan farm naturally: slower, but spending none.
    thrifty = minimize(offers, {"X": 30}, price_cookie=1e9, price_gj=1e9, cookie_sets=5)
    assert thrifty.cookie_sets == 0
    assert thrifty.grape_juices == 0
    # A tiny price spends them to go faster.
    fast = minimize(offers, {"X": 30}, price_cookie=1e-6, price_gj=1e-6, cookie_sets=5)
    assert fast.cookie_sets + fast.grape_juices > 0
    assert fast.time < thrifty.time


def test_frontier_spreads_cookie_and_grape_juice_use_across_plans():
    # A crop only farmable (no consumable channel) still yields a spread of plans,
    # from cookie/Grape-Juice-fast to a thrifty natural plan spending neither.
    offers = [
        Offer("grow X without grape juice", "farming", {"x": 1}, time_per_unit=timedelta(hours=3)),
        Offer("grow X", "farming", {"x": 1}, gj=1, time_per_unit=timedelta(hours=1)),
        Offer("grow X with cookies", "farming", {"x": 10}, cookies=1, gj=2, time_per_unit=timedelta(days=1)),
    ]
    plans = frontier(offers, {"X": 500}, cookie_sets=5)
    assert len(plans) > 1
    assert plans[0].cookie_sets + plans[0].grape_juices > 0  # fastest spends them
    thriftiest = min(plans, key=lambda p: p.cookie_sets + p.grape_juices)
    assert thriftiest.cookie_sets == 0 and thriftiest.grape_juices == 0


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


def test_passive_completion_caps_are_each_item_finish_time():
    # Passive makes 10 Oak and 1 Horn a minute; 100 Oak finishes in 10 min, 60 Horn
    # in 60 min. The caps are those completion times, in seconds.
    passive = Offer(
        "passive", "passive", {"oak": 10.0, "horn": 1.0}, time_per_unit=timedelta(minutes=1)
    )
    plan = SourcingPlan(draws=[Draw(passive, 100)])
    caps = _passive_completion_caps(plan, {"Oak": 100, "Horn": 60}, {}, 1.0, [], {})
    assert sorted(round(c) for c in caps) == [600, 3600]


def test_passive_completion_caps_net_of_other_channels():
    # Fishing already lands 90 of the 100 Oak, so passive need only finish the last
    # 10 -- its completion is 10/10 = 1 min, not the full 10.
    passive = Offer("passive", "passive", {"oak": 10.0}, time_per_unit=timedelta(minutes=1))
    fished = Draw(Offer("fish Oak", "fishing", {"oak": 1}, ln=1), 90)
    plan = SourcingPlan(draws=[Draw(passive, 1), fished])
    caps = _passive_completion_caps(plan, {"Oak": 100}, {}, 1.0, [], {})
    assert [round(c) for c in caps] == [60]


def test_thin_keeps_anchor_plans():
    plans = [SourcingPlan(time=timedelta(days=d)) for d in range(1, 11)]  # 1..10 days
    # count=3 would keep just the ends and the middle; the anchor at 3 days pins a
    # plan the even spread would drop.
    kept = _thin(plans, 3, anchors=(timedelta(days=3).total_seconds(),))
    days = [p.time.days for p in kept]
    assert days[0] == 1 and days[-1] == 10  # both ends kept
    assert 3 in days  # the anchored breakpoint survives
    assert len(kept) == 3


def test_frontier_clamps_to_a_passive_completion_breakpoint():
    # Oak and Horn can be fished (fast, costly) or made passively (free, slow). Horn
    # finishes passively at 100/10 = 10 min; Oak only at 100/1 = 100 min (the cheap
    # end). So 10 min is a breakpoint, and a plan should land there.
    offers = [
        Offer("passive", "passive", {"oak": 1.0, "horn": 10.0}, time_per_unit=timedelta(minutes=1)),
        Offer("fish Oak", "fishing", {"oak": 1}, ln=1),
        Offer("fish Horn", "fishing", {"horn": 1}, ln=1),
    ]
    plans = frontier(offers, {"Oak": 100, "Horn": 100}, count=5)
    times = [p.time.total_seconds() for p in plans]
    assert any(abs(t - 600) < 1 for t in times)  # a plan clamped at Horn's 10 min
