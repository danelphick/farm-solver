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
    passive_residuals,
    plan_balance,
    plan_gold,
    plan_gold_breakdown,
    plan_leftovers,
    plan_surplus_gold,
    self_sourced_endpoint,
    surplus_sale_value,
)
from passive import HOURLY, PassiveProducer, PassiveProduction
from sources import Draw, Offer, OpenFlow

# Per-unit gold prices for the buyable consumables (see config.trading_prices).
GOLD = {
    "arnold palmer": 0.06,
    "large net": 0.01,
    "apple cider": 0.02,
    "orange juice": 0.008,
    "cookie set": 20.0,
    "grape juice": 0.0,
}

# A tiny gold weight buys freely for the fastest plan; a huge one buys nothing for
# the slowest, fully self-sourced plan (see minimize's two endpoint modes).
BUY_FREELY = 1e-6
SELF_SOURCE = 1e9

# A free-but-slow farming offer and a fast fishing offer that *burns* a Large Net.
# With no recipe or passive source for nets they can only be bought, so fishing
# costs gold and farming costs time -- the basic tradeoff.
FARM_ITEM = Offer("grow It", "farming", {"it": 1}, time_per_unit=timedelta(minutes=1))
FISH_ITEM = Offer("fish It", "fishing", {"it": 1}, consumes={"large net": 1}, ln=1)

# An ungrounded fishing offer (a net is a bare tally, not a real item) -- a stand-in
# for a free, instant gather in tests that are not about the make-vs-buy tradeoff.
FREE_FISH = Offer("fish It", "fishing", {"it": 1}, ln=1)


def test_all_offers_gathers_every_channel():
    offers = all_offers(
        PassiveProduction(producers=[PassiveProducer("mill", HOURLY, {"board": 60})]),
        Fishing(zones={"lake": FishingZone("Lake", {"trout": 0.4})}),
        Exploring(locations={"cave": ExploringLocation("Cave", {"wood": 0.4})}),
        Farm(crops={"corn": Crop("Corn", timedelta(hours=1), {"corn": 1})}, rows=1),
    )
    names = {o.name for o in offers}
    # Every channel, and (always grounded now) the item-to-stamina flows (drink
    # Orange Juice, eat Apple). No cider offer here: the cave has no Effectiveness.
    assert names == {"passive", "fish Lake", "explore Cave", "grow Corn",
                     "drink orange juice", "eat apple"}


def test_all_offers_grounds_consumables_and_adds_the_cider_channel():
    offers = all_offers(
        PassiveProduction(producers=[PassiveProducer("mill", HOURLY, {"board": 60})]),
        Fishing(zones={"lake": FishingZone("Lake", {"trout": 0.4})}),
        Exploring(
            locations={"cave": ExploringLocation("Cave", {"wood": 0.4})},
            effectiveness={"cave": 10},
        ),
        Farm(crops={"corn": Crop("Corn", timedelta(hours=1), {"corn": 1})}, rows=1),
    )
    by_name = {o.name: o for o in offers}
    assert by_name["fish Lake"].consumes == {"large net": 1.0}
    assert by_name["explore Cave"].consumes == {"arnold palmer": 1.0}
    # The Apple Cider channel (the cave has an Effectiveness) and stamina flows.
    assert "cider Cave" in by_name
    assert "drink orange juice" in by_name
    assert "eat apple" in by_name


def test_minimize_self_sources_when_gold_is_dear():
    # A huge gold weight buys nothing: grow it (slow, free) rather than buy nets.
    plan = minimize([FARM_ITEM, FISH_ITEM], {"It": 100}, prices=GOLD,
                    gold_weight=SELF_SOURCE)
    assert plan.gold == 0
    assert plan.bought == {}
    assert plan.time == timedelta(minutes=100)
    assert [d.offer.name for d in plan.draws] == ["grow It"]


def test_minimize_buys_when_gold_is_cheap():
    # A tiny gold weight buys the nets to fish it (instant) rather than wait.
    plan = minimize([FARM_ITEM, FISH_ITEM], {"It": 100}, prices=GOLD,
                    gold_weight=BUY_FREELY)
    assert plan.bought.get("large net") == pytest.approx(100)
    assert plan.gold == pytest.approx(100 * 0.01)
    assert plan.time == timedelta(0)
    assert [d.offer.name for d in plan.draws] == ["fish It"]


def test_minimize_credits_joint_production_across_needs():
    # One net yields 2 "a" and 1 "b"; covering 10 a (5 nets) also makes 5 b,
    # covering the 3 b needed for free -- a single draw.
    offer = Offer("fish Lake", "fishing", {"a": 2, "b": 1}, ln=1)
    plan = minimize([offer], {"a": 10, "b": 3})
    assert len(plan.draws) == 1
    assert plan.draws[0].units == pytest.approx(5)
    assert plan.large_nets == pytest.approx(5)
    assert plan.remainder == {}


def test_minimize_leaves_unproducible_items_in_the_remainder():
    plan = minimize([FREE_FISH], {"It": 10, "Gold Bar": 4})
    assert plan.remainder == {"Gold Bar": 4}


def test_minimize_without_any_offer_returns_everything_as_remainder():
    plan = minimize([], {"It": 10})
    assert plan == SourcingPlan(remainder={"It": 10})


def test_minimize_crafts_when_cheaper_in_gold_than_gathering():
    # Glass Bottle can be fished (a net each) or crafted from Glass Orb, which
    # explores 10-to-a-palmer. Buying to craft 100 costs ~10 palmers (0.6 g) vs
    # ~100 nets (1 g), so the least-gold plan crafts rather than fishes.
    offers = [
        Offer("fish Lake", "fishing", {"glass bottle": 1}, consumes={"large net": 1}, ln=1),
        Offer("explore Cave", "exploring", {"glass orb": 10}, consumes={"arnold palmer": 1}, ap=1),
    ]
    recipes = {"glass bottle": {"glass orb": 1}}
    plan = minimize(offers, {"Glass Bottle": 100}, recipes, 1.0, prices=GOLD,
                    gold_weight=BUY_FREELY)
    assert plan.large_nets == 0
    assert plan.crafts.get("glass bottle") == pytest.approx(100)
    assert plan.arnold_palmers == pytest.approx(10)


def test_minimize_gathers_when_cheaper_in_gold_than_crafting():
    # If the same bottle fishes 100-to-a-net, one net (0.01 g) beats 10 palmers
    # (0.6 g) worth of orbs, so fishing wins.
    offers = [
        Offer("fish Lake", "fishing", {"glass bottle": 100}, consumes={"large net": 1}, ln=1),
        Offer("explore Cave", "exploring", {"glass orb": 10}, consumes={"arnold palmer": 1}, ap=1),
    ]
    recipes = {"glass bottle": {"glass orb": 1}}
    plan = minimize(offers, {"Glass Bottle": 100}, recipes, 1.0, prices=GOLD,
                    gold_weight=BUY_FREELY)
    assert plan.crafts == {}
    assert plan.large_nets == pytest.approx(1)


def test_minimize_does_not_craft_surplus_into_unneeded_items():
    # Passive yields Milk plus 1000 Board a minute as a co-product, so covering
    # 100 Butter (which needs 100 Milk, ~100 minutes of passive) leaves ~100,000
    # surplus Board. The Barrel->Churn->Butter chain only needs 100 Barrels, but
    # crafting more costs no time or gold, so the bare objective is indifferent and
    # would spin the whole Board surplus into Barrels. The least-craft tie-break
    # must craft only the 100 the demand actually needs.
    passive = Offer(
        "passive", "passive", {"milk": 1, "board": 1000}, time_per_unit=timedelta(minutes=1)
    )
    recipes = {
        "butter": {"milk": 1, "churn": 1},
        "churn": {"barrel": 1},
        "barrel": {"board": 1},
    }
    plan = minimize([passive], {"Butter": 100}, recipes, 1.0)
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
    plan = minimize([passive, mushrooms], {"It": 100})
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
    plan = minimize(offers, {"Gem": 100}, openables=[chest])
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
    plan = minimize(offers, {"Gem": 50}, openables=[chest])
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
    plan = minimize([FREE_FISH], {"It": 10}, openables=[chest])
    assert plan.opens == {}
    assert plan.large_nets == pytest.approx(10)


def test_minimize_draws_intermediate_materials_from_inventory():
    # Widget crafts from one Gear each. With 100 Gears already in stock, covering
    # 100 Widgets needs no Gears fished at all.
    offers = [Offer("fish Gears", "fishing", {"gear": 1}, ln=1)]
    recipes = {"widget": {"gear": 1}}
    plan = minimize(offers, {"Widget": 100}, recipes, 1.0, inventory={"gear": 100})
    assert plan.crafts.get("widget") == pytest.approx(100)
    assert plan.large_nets == pytest.approx(0)  # all Gears came from stock


def test_minimize_gathers_only_the_shortfall_beyond_inventory():
    # 100 Widgets need 100 Gears but only 30 are in stock, so 70 must be fished.
    offers = [Offer("fish Gears", "fishing", {"gear": 1}, ln=1)]
    recipes = {"widget": {"gear": 1}}
    plan = minimize(offers, {"Widget": 100}, recipes, 1.0, inventory={"gear": 30})
    assert plan.large_nets == pytest.approx(70)


def test_minimize_does_not_credit_inventory_against_the_direct_demand():
    # The demand itself is already net of what is held (mastery progress or the
    # acquired count), so stock of a *demanded* item earns no extra credit: all
    # 100 are still gathered.
    plan = minimize([FREE_FISH], {"It": 100}, inventory={"it": 100})
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
        offers, {"Trinket": 100}, recipes, 1.0, openables=[chest], inventory={"gem": 100}
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
    plan = minimize([normal, cookie], {"Slow": 10}, cookie_sets=1)
    assert plan.cookie_sets == pytest.approx(1)
    assert plan.time == timedelta(days=1)


def test_minimize_respects_the_cookie_cap():
    normal = Offer("grow Slow", "farming", {"slow": 1}, time_per_unit=timedelta(days=1))
    cookie = Offer(
        "grow Slow with cookies", "farming", {"slow": 10}, cookies=1,
        time_per_unit=timedelta(days=1),
    )
    # No sets held and none priced (unbuyable), so all ten days are plain farming.
    plan = minimize([normal, cookie], {"Slow": 10}, cookie_sets=0)
    assert plan.cookie_sets == 0
    assert plan.time == timedelta(days=10)


def test_minimize_buys_a_cookie_set_to_speed_farming():
    # No sets held, but cookie sets are priced (buyable). Buying one to grow the ten
    # Slow in a day instead of ten is worth 20 g, and the fast plan pays it.
    normal = Offer("grow Slow", "farming", {"slow": 1}, time_per_unit=timedelta(days=1))
    cookie = Offer(
        "grow Slow with cookies", "farming", {"slow": 10}, cookies=1,
        time_per_unit=timedelta(days=1),
    )
    plan = minimize([normal, cookie], {"Slow": 10}, prices=GOLD,
                    gold_weight=BUY_FREELY, cookie_sets=0)
    assert plan.bought.get("cookie set") == pytest.approx(1)
    assert plan.gold == pytest.approx(20.0)
    assert plan.time == timedelta(days=1)


def test_minimize_grape_juice_is_self_sourced_not_bought():
    # Growing X the fast way spends a Grape Juice a planting; Grape Juice is never
    # buyable, so it is pressed from grapes (grown slowly) rather than bought.
    juiced = Offer("grow X", "farming", {"x": 1}, consumes={"grape juice": 1}, gj=1,
                   time_per_unit=timedelta(hours=1))
    grapes = Offer("passive", "passive", {"grapes": 200.0}, time_per_unit=timedelta(minutes=1))
    recipes = {"grape juice": {"grapes": 200}}
    plan = minimize([juiced, grapes], {"X": 10}, recipes, 1.0, prices=GOLD,
                    gold_weight=BUY_FREELY)
    assert plan.bought == {}  # nothing to buy: Grape Juice is not sellable
    assert plan.crafts.get("grape juice") == pytest.approx(10)


def test_minimize_leaves_items_needing_unobtainable_materials_in_remainder():
    # Widget is only craftable, and its material has no source, so it cannot be
    # made -- it falls to the remainder rather than making the LP infeasible.
    recipes = {"widget": {"unobtanium": 1}}
    plan = minimize([], {"Widget": 5}, recipes)
    assert plan.remainder == {"Widget": 5}
    assert plan.crafts == {}


def test_minimize_forbids_buying_an_items_own_mastery():
    # A demanded Large Net (a tower/-m/-i goal) must be crafted from Wire; buying
    # nets cannot earn Net mastery, so the goal 100 are all crafted, none bought.
    wire = Offer("passive", "passive", {"wire": 1.0}, time_per_unit=timedelta(minutes=1))
    recipes = {"large net": {"wire": 1}}
    plan = minimize([wire], {"large net": 100}, recipes, 1.0, prices=GOLD,
                    gold_weight=BUY_FREELY)
    assert plan.bought == {}
    assert plan.crafts.get("large net") == pytest.approx(100)


def test_minimize_buys_a_demanded_items_tool_use_but_not_its_mastery():
    # Large Nets are a goal (100, crafted from Wire) *and* a fishing tool (50 burned
    # to catch Fish, also demanded). Buying may cover the 50 tool nets, but the 100
    # goal nets stay crafted.
    wire = Offer("passive", "passive", {"wire": 1.0}, time_per_unit=timedelta(minutes=1))
    fish = Offer("fish Z", "fishing", {"fish": 1}, consumes={"large net": 50})
    recipes = {"large net": {"wire": 1}}
    plan = minimize([wire, fish], {"large net": 100, "fish": 1}, recipes, 1.0,
                    prices=GOLD, gold_weight=BUY_FREELY)
    assert plan.bought.get("large net", 0) <= 50 + 1e-6
    assert plan.crafts.get("large net") >= 100 - 1e-6


def test_frontier_spans_the_tradeoff_fastest_first():
    plans = frontier([FARM_ITEM, FISH_ITEM], {"It": 100}, prices=GOLD)
    # The two extremes: instant by buying 100 nets, or free over 100 minutes.
    assert plans[0].time == timedelta(0)
    assert plans[0].bought.get("large net") == pytest.approx(100)
    assert plans[-1].gold == 0
    assert plans[-1].time == timedelta(minutes=100)
    # Sorted fastest first.
    assert [p.time for p in plans] == sorted(p.time for p in plans)


def test_frontier_returns_one_plan_when_gold_buys_no_speed():
    # Only a free channel exists (nothing to buy), so every budget gives one plan.
    plans = frontier([FARM_ITEM], {"It": 50}, prices=GOLD)
    assert len(plans) == 1
    assert plans[0].gold == 0
    assert plans[0].time == timedelta(minutes=50)


def test_frontier_is_non_dominated():
    plans = frontier([FARM_ITEM, FISH_ITEM], {"It": 100}, prices=GOLD)
    for a in plans:
        for b in plans:
            if a is not b:
                # No plan beats another on both time and gold at once.
                assert not (
                    a.time <= b.time and a.gold <= b.gold and a.cost != b.cost
                )


def test_frontier_fills_a_flat_segment_a_price_sweep_would_skip():
    # Two items, each farmable (1 min each, one sequential track) or fishable (a net
    # each, bought), same time-per-net. Farming both (200 min, 0 g) and buying-and-
    # fishing both (0 min, 2 nets) are the ends of a flat frontier segment, so every
    # weighted-sum gold price collapses onto one end -- a price sweep would find only
    # the extremes. The epsilon-constraint sweep must surface an interior plan.
    offers = [
        Offer("grow A", "farming", {"a": 1}, time_per_unit=timedelta(minutes=1)),
        Offer("grow B", "farming", {"b": 1}, time_per_unit=timedelta(minutes=1)),
        Offer("fish A", "fishing", {"a": 1}, consumes={"large net": 1}, ln=1),
        Offer("fish B", "fishing", {"b": 1}, consumes={"large net": 1}, ln=1),
    ]
    plans = frontier(offers, {"A": 100, "B": 100}, count=5, prices=GOLD)
    interior = [
        p
        for p in plans
        if timedelta(0) < p.time < timedelta(minutes=200) and p.gold > 0
    ]
    assert interior, "the flat segment's interior was not sampled"


def test_frontier_thins_to_the_requested_count():
    # A staircase of offers trading time for gold gives many vertices; ask for 3 and
    # get at most 3, still including both extremes.
    offers = [
        Offer("grow It", "farming", {"it": 1}, time_per_unit=timedelta(minutes=1)),
        Offer("fish A", "fishing", {"it": 1}, consumes={"large net": 1}, ln=1),
    ]
    plans = frontier(offers, {"It": 1000}, count=3, prices=GOLD)
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
    # Oak and Horn can be fished (fast, bought nets) or made passively (free, slow).
    # Horn finishes passively at 100/10 = 10 min; Oak only at 100/1 = 100 min (the
    # cheap end). So 10 min is a breakpoint, and a plan should land there.
    offers = [
        Offer("passive", "passive", {"oak": 1.0, "horn": 10.0}, time_per_unit=timedelta(minutes=1)),
        Offer("fish Oak", "fishing", {"oak": 1}, consumes={"large net": 1}, ln=1),
        Offer("fish Horn", "fishing", {"horn": 1}, consumes={"large net": 1}, ln=1),
    ]
    plans = frontier(offers, {"Oak": 100, "Horn": 100}, count=5, prices=GOLD)
    times = [p.time.total_seconds() for p in plans]
    assert any(abs(t - 600) < 1 for t in times)  # a plan clamped at Horn's 10 min


def test_passive_residuals_are_net_of_other_channels():
    # 100 Oak needed; fishing lands 30, so passive is left to make the other 70.
    passive = Offer("passive", "passive", {"oak": 10.0}, time_per_unit=timedelta(minutes=1))
    fished = Draw(Offer("fish Oak", "fishing", {"oak": 1}, ln=1), 30)
    plan = SourcingPlan(draws=[Draw(passive, 1), fished])
    assert passive_residuals(plan, {"Oak": 100}, {}, 1.0, [], {}) == {"oak": 70.0}


def test_passive_completion_caps_skip_excluded_items():
    # Oak completes at 100/10 = 10 min; Horn is a pet trickle we exclude, so only
    # Oak's breakpoint is returned.
    passive = Offer(
        "passive", "passive", {"oak": 10.0, "horn": 1.0}, time_per_unit=timedelta(minutes=1)
    )
    plan = SourcingPlan(draws=[Draw(passive, 100)])
    caps = _passive_completion_caps(
        plan, {"Oak": 100, "Horn": 60}, {}, 1.0, [], {}, exclude={"horn"}
    )
    assert [round(c) for c in caps] == [600]


# --- Grounded consumables: offer inputs (consumes) -----------------------------


def test_minimize_sources_a_consumed_input_through_its_recipe():
    # Exploring the cave yields "wood" but burns an "arnold palmer", which is not
    # gathered anywhere -- only crafted from "lemon" (a gathered passive item).
    explore = Offer(
        "explore Cave", "exploring", {"wood": 10}, consumes={"arnold palmer": 1.0}
    )
    lemon = Offer("passive", "passive", {"lemon": 1.0}, time_per_unit=timedelta(minutes=1))
    recipes = {"arnold palmer": {"lemon": 6.0}}
    plan = minimize([explore, lemon], {"Wood": 100}, recipes, crafting_bonus=1.0)
    assert plan.remainder == {}
    # 100 wood needs 10 explores -> 10 palmers -> 60 lemons gathered passively.
    assert plan.crafts["arnold palmer"] == pytest.approx(10)
    lemon_draw = next(d for d in plan.draws if d.offer.track == "passive")
    assert lemon_draw.produced["lemon"] == pytest.approx(60)


def test_minimize_buys_a_consumed_input_when_cheaper_than_making_it():
    # The same explore, but now the palmer is buyable and there is no lemon source
    # to craft it from. Buying the 10 palmers it burns is the only way, so it does.
    explore = Offer(
        "explore Cave", "exploring", {"wood": 10}, consumes={"arnold palmer": 1.0}, ap=1
    )
    plan = minimize([explore], {"Wood": 100}, prices=GOLD, gold_weight=BUY_FREELY)
    assert plan.remainder == {}
    assert plan.bought.get("arnold palmer") == pytest.approx(10)
    assert plan.gold == pytest.approx(10 * 0.06)


def test_minimize_drops_an_offer_whose_input_is_unobtainable():
    # The palmer cannot be crafted (no recipe), gathered, nor bought (no price), so
    # the explore offer is unusable and the wood demand lands in the remainder.
    explore = Offer(
        "explore Cave", "exploring", {"wood": 10}, consumes={"arnold palmer": 1.0}
    )
    plan = minimize([explore], {"Wood": 100})
    assert [d.offer.name for d in plan.draws] == []
    assert plan.remainder == {"Wood": 100}


def test_minimize_counts_apple_ciders_spent():
    # A cider explore consumes stamina (gathered) and a tenth of an Apple Cider
    # (craftable from a gathered apple), yielding the wanted item.
    cider = Offer(
        "cider Cave",
        "exploring",
        {"wood": 4},
        consumes={"stamina": 1.0, "apple cider": 0.1},
    )
    stamina = Offer(
        "drink", "drinking", {"stamina": 100.0}, consumes={"orange juice": 1.0}
    )
    orange = Offer(
        "passive", "passive", {"orange": 1.0}, time_per_unit=timedelta(minutes=1)
    )
    recipes = {"orange juice": {"orange": 6.0}, "apple cider": {"orange": 1.0}}
    plan = minimize([cider, stamina, orange], {"Wood": 40}, recipes, crafting_bonus=1.0)
    # 40 wood needs 10 explores -> 1 Apple Cider spent.
    assert plan.apple_ciders == pytest.approx(1.0)


def test_minimize_eats_apples_for_cider_stamina():
    # Stamina for cider explores can also come from eating Apples (15 each). With
    # no orange source, eating passive Apples is the only route to stamina here.
    cider = Offer(
        "cider Cave",
        "exploring",
        {"wood": 4},
        consumes={"stamina": 15.0, "apple cider": 0.1},
    )
    eat_apple = Offer(
        "eat apple", "drinking", {"stamina": 15.0}, consumes={"apple": 1.0}
    )
    apple = Offer(
        "passive", "passive", {"apple": 1.0}, time_per_unit=timedelta(minutes=1)
    )
    recipes = {"apple cider": {"apple": 1.0}}
    plan = minimize([cider, eat_apple, apple], {"Wood": 40}, recipes, crafting_bonus=1.0)
    # 40 wood -> 10 explores -> 150 stamina -> eat 10 apples (each gives 15).
    eaten = sum(
        d.units * d.offer.consumes.get("apple", 0.0)
        for d in plan.draws
        if d.offer.name == "eat apple"
    )
    assert eaten == pytest.approx(10.0)


# --- Gold accounting -----------------------------------------------------------


def test_plan_gold_is_the_priced_consumable_total():
    plan = SourcingPlan(
        arnold_palmers=100, large_nets=200, apple_ciders=50,
        orange_juice=1000, cookie_sets=3, grape_juices=999,
    )
    # 100*.06 + 200*.01 + 50*.02 + 1000*.008 + 3*20 + 999*0 = 77
    assert plan_gold(plan, GOLD) == pytest.approx(77.0)


def test_plan_gold_breakdown_splits_bought_from_self_sourced():
    # to-buy is exactly what the plan bought (plan.gold); the rest of every
    # consumable's market value is self-sourced. The two sum to plan_gold.
    plan = SourcingPlan(
        arnold_palmers=100, large_nets=500,
        bought={"arnold palmer": 70}, gold=70 * 0.06,
    )
    to_buy, self_sourced = plan_gold_breakdown(plan, GOLD)
    assert to_buy == pytest.approx(70 * 0.06)  # bought 70 AP
    # self-sourced: the other 30 AP + all 500 nets (market value).
    assert self_sourced == pytest.approx(30 * 0.06 + 500 * 0.01)
    assert to_buy + self_sourced == pytest.approx(plan_gold(plan, GOLD))


def test_plan_leftovers_are_supply_past_consumption():
    # Passive banks 1,000 straw; a craft consumes 600 of it into 300 twine, leaving
    # 400 straw and the 300 twine sitting as surplus.
    passive = Offer(
        "passive", "passive", {"straw": 10.0}, time_per_unit=timedelta(minutes=1)
    )
    plan = SourcingPlan(draws=[Draw(passive, 100)], crafts={"twine": 300})
    leftovers = plan_leftovers(plan, {}, {"twine": {"straw": 2}}, 1.0, [], {})
    assert leftovers == pytest.approx({"straw": 400.0, "twine": 300.0})


def test_surplus_sale_value_crafts_leftovers_into_the_richest_good():
    # One straw makes either a Large Net or an Apple Cider; cider is worth more per
    # unit, so all 100 surplus straw becomes 100 cider (not nets), valued in gold.
    recipes = {"large net": {"straw": 1}, "apple cider": {"straw": 1}}
    rows = surplus_sale_value({"straw": 100.0}, recipes, 1.0, GOLD)
    assert rows == [("apple cider", pytest.approx(100.0), pytest.approx(2.0))]


def test_surplus_sale_value_excludes_unsellable_grape_juice():
    # Grape Juice has a recipe and (here) a price, but is not sellable, so surplus
    # grapes whose only outlet is Grape Juice yield no gold.
    prices = {**GOLD, "grape juice": 5.0}
    recipes = {"grape juice": {"grapes": 2}}
    assert surplus_sale_value({"grapes": 500.0}, recipes, 1.0, prices) == []


def test_plan_surplus_gold_sums_the_sale_value_of_banked_surplus():
    # Passive banks 1,000 straw; nothing consumes it and one straw crafts a Large Net
    # (0.01 g each), so the leftover is worth 1,000 * 0.01 = 10 gold.
    passive = Offer(
        "passive", "passive", {"straw": 10.0}, time_per_unit=timedelta(minutes=1)
    )
    plan = SourcingPlan(draws=[Draw(passive, 100)])
    banked = plan_surplus_gold(plan, {}, {"large net": {"straw": 1}}, 1.0, [], {}, GOLD)
    assert banked == pytest.approx(1000 * 0.01)


def test_cost_is_time_and_gold_net_of_banked_surplus():
    # The Pareto cost's gold axis is net: what the plan buys, less the sale value of
    # the surplus it banks -- which can carry it below zero.
    plan = SourcingPlan(time=timedelta(minutes=30), gold=100.0, surplus_gold=140.0)
    assert plan.cost == (pytest.approx(30 * 60), pytest.approx(-40.0))


def test_self_sourced_endpoint_buys_nothing():
    # The cheap endpoint self-sources everything, so it farms all 100 It (100 min)
    # rather than buying nets to fish -- spending no gold.
    endpoint = self_sourced_endpoint([FARM_ITEM, FISH_ITEM], {"It": 100}, prices=GOLD)
    assert endpoint.gold == 0
    assert endpoint.time == timedelta(minutes=100)


def test_minimize_favour_passive_leans_on_passive_over_tools():
    # A gadget is either crafted from slow passive Wire or fished with a Large Net.
    # favour_passive minimises tool spend, so it crafts all 100 from Wire (no nets,
    # ~1,000 min); the fast plan instead buys nets and fishes them instantly.
    passive = Offer(
        "passive", "passive", {"wire": 0.1}, time_per_unit=timedelta(minutes=1)
    )
    fish = Offer("fish gadget", "fishing", {"gadget": 1},
                 consumes={"large net": 1}, ln=1)
    recipes = {"gadget": {"wire": 1.0}}
    passive_plan = minimize([passive, fish], {"gadget": 100}, recipes, 1.0,
                            prices=GOLD, favour_passive=True)
    assert round(passive_plan.large_nets) == 0
    assert passive_plan.crafts.get("gadget") == pytest.approx(100)
    fast_plan = minimize([passive, fish], {"gadget": 100}, recipes, 1.0,
                         prices=GOLD, gold_weight=BUY_FREELY)
    assert round(fast_plan.large_nets) == 100


def test_frontier_extends_past_cheap_endpoint_with_max_passive_plan():
    # The fastest zero-gold plan fishes gadgets (nets self-sourced from fast straw),
    # finishing quickly. The favour_passive extension crafts them from slow passive
    # Wire instead -- spending no nets, running far longer, banking the surplus straw
    # -- so the frontier reaches a slower plan with a lower (more negative) net gold.
    passive = Offer(
        "passive", "passive", {"wire": 0.1, "straw": 10.0},
        time_per_unit=timedelta(minutes=1),
    )
    fish = Offer("fish gadget", "fishing", {"gadget": 1},
                 consumes={"large net": 1}, ln=1)
    recipes = {
        "gadget": {"wire": 1.0},
        "large net": {"straw": 1.0},
        "apple cider": {"straw": 1.0},
    }
    plans = frontier([passive, fish], {"gadget": 100}, recipes, prices=GOLD)
    cheap = minimize([passive, fish], {"gadget": 100}, recipes, 1.0,
                     prices=GOLD, gold_weight=SELF_SOURCE)
    slow = plans[-1]
    assert round(cheap.large_nets) > 0      # cheap fishes (self-sourced nets)
    assert slow.time > cheap.time           # the extension runs longer on passive
    assert round(slow.large_nets) == 0      # spending no fishing tools
    assert slow.cost[1] < cheap.cost[1]     # for a lower, more negative net gold


def test_frontier_credits_banked_surplus_below_zero_gold():
    # Passive makes slow Wire and fast straw together; the demanded gadget is either
    # crafted from Wire (passive-only, ~1,000 min) or fished with a bought net. The
    # slow, self-sourced plan waits on Wire and banks ~10,000 surplus straw, sellable
    # as cider -- a gold credit that drives its net cost below zero, past "buy
    # nothing".
    passive = Offer(
        "passive", "passive", {"wire": 0.1, "straw": 10.0},
        time_per_unit=timedelta(minutes=1),
    )
    fish = Offer("fish gadget", "fishing", {"gadget": 1},
                 consumes={"large net": 1}, ln=1)
    recipes = {"gadget": {"wire": 1}, "apple cider": {"straw": 1}}
    plans = frontier([passive, fish], {"gadget": 100}, recipes, prices=GOLD)
    slow = plans[-1]
    assert slow.gold < 1e-6                  # buys nothing (bar a solver residual)
    assert slow.surplus_gold > 0             # but banks sellable surplus
    assert slow.cost[1] < 0                  # so its net gold sits below zero
    assert slow.cost[1] == pytest.approx(slow.gold - slow.surplus_gold)


def test_frontier_prices_pick_the_cheaper_gold_channel():
    # The same item is obtainable by fishing (a bought Large Net) or exploring (a
    # bought Arnold Palmer), both instant. Large Nets are far cheaper in gold, so
    # the fastest plan buys nets, not palmers.
    fish = Offer("fish Z", "fishing", {"it": 1}, consumes={"large net": 1}, ln=1)
    explore = Offer("explore L", "exploring", {"it": 1}, consumes={"arnold palmer": 1}, ap=1)
    plans = frontier([fish, explore], {"it": 100}, prices=GOLD)
    fast = plans[0]
    assert round(fast.large_nets) == 100
    assert round(fast.arnold_palmers) == 0
    assert fast.bought.get("large net") == pytest.approx(100)


def test_frontier_without_prices_is_fully_self_sourced():
    # No prices -> nothing is buyable, so the frontier is the single self-sourced
    # plan (fishing here is free, an ungrounded tally).
    plans = frontier([FARM_ITEM, FREE_FISH], {"It": 100})
    assert plans
    assert all(p.gold == 0 for p in plans)


# --- Pumpkin Juice: a bought mastery boost that covers demand directly ---


def test_minimize_drinks_pumpkin_juice_to_cover_mastery_when_gold_is_cheap():
    # Each juice covers 10 units of "it" demand for 5 gold; with a tiny gold weight
    # the solver buys 10 (covering all 100) rather than wait 100 minutes to grow it.
    plan = minimize([FARM_ITEM], {"It": 100}, prices=GOLD, gold_weight=BUY_FREELY,
                    pj_gain={"it": 10}, pj_price=5)
    assert plan.pumpkin_juices.get("it") == pytest.approx(10)
    assert plan.pumpkin_gold == pytest.approx(50)
    assert plan.gold == pytest.approx(50)  # PJ gold folds into the plan's gold
    assert plan.pumpkin_credit.get("it") == pytest.approx(100)
    assert plan.time == timedelta(0)
    assert plan.draws == []


def test_minimize_respects_the_pumpkin_juice_limit():
    # Capped at 3 juices (covering 30), the other 70 must be grown (70 minutes).
    plan = minimize([FARM_ITEM], {"It": 100}, prices=GOLD, gold_weight=BUY_FREELY,
                    pj_gain={"it": 10}, pj_price=5, pj_limit=3)
    assert plan.pumpkin_juices.get("it") == pytest.approx(3)
    assert plan.time == timedelta(minutes=70)


def test_minimize_caps_pumpkin_juice_at_the_goal():
    # 15 demand, each juice covers 10, so at most 1.5 juices are useful -- a juice
    # past full coverage earns no mastery, so the solver never buys more.
    plan = minimize([FARM_ITEM], {"It": 15}, prices=GOLD, gold_weight=BUY_FREELY,
                    pj_gain={"it": 10}, pj_price=5)
    assert plan.pumpkin_juices.get("it") == pytest.approx(1.5)
    assert plan.time == timedelta(0)


def test_minimize_self_sources_over_pumpkin_juice_when_gold_is_dear():
    # A huge gold weight buys nothing, juice included: grow it instead.
    plan = minimize([FARM_ITEM], {"It": 100}, prices=GOLD, gold_weight=SELF_SOURCE,
                    pj_gain={"it": 10}, pj_price=5)
    assert plan.pumpkin_juices == {}
    assert plan.time == timedelta(minutes=100)


def test_minimize_skips_pumpkin_juice_when_favouring_passive():
    # The buy-nothing slow regime never drinks juice (a gold purchase).
    plan = minimize([FARM_ITEM], {"It": 100}, prices=GOLD, favour_passive=True,
                    pj_gain={"it": 10}, pj_price=5)
    assert plan.pumpkin_juices == {}


def test_plan_balance_nets_out_pumpkin_credit():
    # A juice covering 40 of a 100 demand leaves only 60 for the channels to make.
    plan = SourcingPlan(pumpkin_credit={"it": 40})
    balance = plan_balance(plan, {"It": 100}, {}, 1.0, [], {})
    assert balance.need["it"] == pytest.approx(60)


def test_plan_gold_includes_pumpkin_juice():
    # Pumpkin Juice has no self-sourced form, so its whole gold adds to the total.
    plan = SourcingPlan(large_nets=100, pumpkin_gold=50)
    assert plan_gold(plan, GOLD) == pytest.approx(100 * 0.01 + 50)
