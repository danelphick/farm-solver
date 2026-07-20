from datetime import timedelta

import pytest

import ascension
import optimizer
from crops import Crop, Farm
from exploring import Exploring, ExploringLocation
from fishing import Fishing, FishingZone
from optimizer import (
    GraphSeries,
    SourcingPlan,
    SpreadSpec,
    _build_base,
    _passive_completion_caps,
    _thin,
    all_offers,
    draw_consumed,
    efficiency,
    frontier,
    minimize,
    passive_residuals,
    plan_balance,
    plan_gold,
    leftover_inventory_share,
    plan_gold_breakdown,
    plan_leftovers,
    plan_surplus_gold,
    self_sourced_endpoint,
    spread_graph_series,
    surplus_sale_value,
)
from passive import DAILY, HOURLY, PassiveProducer, PassiveProduction
from sources import (
    Draw,
    Offer,
    OpenFlow,
    passive_offers,
    sawmill_boost_offers,
    stamina_offers,
)

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


def test_sawmill_boost_is_grounded_and_speeds_oak_for_omelette_gold():
    # Sawmill makes 6000 oak/hour (100/min). The omelette deposits 20% of the hourly
    # output every ten minutes, i.e. 6 * 20% = +120% (7200/hour, 120/min) over a
    # boosted hour -- not +20%. So the boost track runs at 120/min alongside the
    # 100/min base: 220/min combined. To make 60000 oak, base passive alone takes
    # 600 min; the boost cuts that to 60000/220 min, eating that many omelette-hours.
    passive = PassiveProduction([PassiveProducer("sawmill", HOURLY, {"oak": 6000})])
    offers = passive_offers(passive) + sawmill_boost_offers({"oak": 6000}, "all")
    prices = {"hickory omelette": 0.35}
    boosted_min = 60000 / 220  # 272.7 min: base 100/min + boost 120/min
    omelettes = boosted_min / 60  # one omelette per boosted hour

    # Buy freely: takes the boost, buying every omelette it needs.
    fast = minimize(offers, {"oak": 60000}, prices=prices, gold_weight=BUY_FREELY)
    assert fast.time == pytest.approx(
        timedelta(minutes=boosted_min), abs=timedelta(seconds=1)
    )
    assert fast.hickory_omelettes == pytest.approx(omelettes, rel=1e-3)
    assert fast.bought["hickory omelette"] == pytest.approx(omelettes, rel=1e-3)
    assert fast.gold == pytest.approx(omelettes * 0.35, rel=1e-3)

    # Buy nothing: the omelette has no self-sourced form, so the boost is dropped
    # and oak comes entirely from the slower base passive run.
    cheap = minimize(offers, {"oak": 60000}, prices=prices, gold_weight=SELF_SOURCE)
    assert cheap.time == pytest.approx(timedelta(minutes=600), abs=timedelta(seconds=1))
    assert cheap.hickory_omelettes == 0

    # Unpriced and unheld: the boost is ungroundable and simply unusable.
    none = minimize(offers, {"oak": 60000}, prices={}, gold_weight=BUY_FREELY)
    assert none.hickory_omelettes == 0


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


def test_held_consumable_grounds_an_offer_and_is_consumed():
    # An explore that drops Hide but burns a held Acorn Pie (like the real acorn
    # channel): with pies on hand the Hide is produced and the pies are spent...
    offer = Offer("explore pie", "exploring", {"hide": 100}, consumes={"acorn pie": 1.0})
    plan = minimize([offer], {"Hide": 250}, inventory={"acorn pie": 5})
    assert plan.remainder == {}
    assert draw_consumed(plan)["acorn pie"] == pytest.approx(2.5)


def test_limited_seeds_bound_mega_plantings_to_the_held_stock():
    # Mega Cotton out-yields plain Cotton tenfold in the same time, but each
    # planting burns 10 held seeds (see crops.Crop.seed_item / farming_offers).
    # With 20 seeds only two plantings fit; plain Cotton grows the rest.
    mega = Offer(
        "grow Mega Cotton", "farming", {"cotton": 400},
        consumes={"mega cotton seeds": 10.0}, time_per_unit=timedelta(days=4),
    )
    plain = Offer(
        "grow Cotton", "farming", {"cotton": 40}, time_per_unit=timedelta(days=4)
    )
    plan = minimize(
        [mega, plain], {"Cotton": 1000}, inventory={"mega cotton seeds": 20}
    )
    units = {d.offer.name: d.units for d in plan.draws}
    assert units["grow Mega Cotton"] == pytest.approx(2)
    assert units["grow Cotton"] == pytest.approx(5)
    assert draw_consumed(plan)["mega cotton seeds"] == pytest.approx(20)


def test_demand_beyond_a_limited_channel_capacity_raises():
    # Flowers grow only from held Spring Seeds; a demand past what the stock can
    # ever produce makes the LP infeasible -- surfaced as an error, not silently
    # returned as an empty 0-minute plan (see _assert_optimal).
    spring = Offer(
        "grow Spring", "farming", {"red flowers": 96},
        consumes={"spring seeds": 48.0}, time_per_unit=timedelta(hours=2),
    )
    with pytest.raises(ValueError, match="no plan can satisfy the demand"):
        minimize([spring], {"Red Flowers": 1000}, inventory={"spring seeds": 96})


def test_offer_grounded_only_in_an_unheld_consumable_is_unobtainable():
    # ...but with no pies held (and nothing crafting them) the offer cannot run,
    # so its Hide is unobtainable and falls to the remainder rather than infeasible.
    offer = Offer("explore pie", "exploring", {"hide": 100}, consumes={"acorn pie": 1.0})
    plan = minimize([offer], {"Hide": 250})
    assert plan.remainder == {"Hide": 250}


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


def test_minimize_trades_for_a_plain_count_goal():
    # Red Dye is only self-sourceable by a slow farm, or traded for AP (an instant
    # buy priced in AP-converted-to-gold). As a plain -i count goal it may be traded.
    farm = Offer("grow dye", "farming", {"red dye": 1}, time_per_unit=timedelta(minutes=1))
    prices = {**GOLD, "red dye": 0.008 * 0.06}
    plan = minimize(
        [farm], {"red dye": 100}, prices=prices, gold_weight=BUY_FREELY,
        ap_tradeable=frozenset({"red dye"}), non_mastery_goals=frozenset({"red dye"}),
    )
    assert plan.bought.get("red dye") == pytest.approx(100)
    assert plan.time == timedelta(0)


def test_minimize_never_trades_for_a_mastery_goal():
    # Same setup, but Red Dye is a mastery goal (not in non_mastery_goals): trading
    # earns no mastery, so the floor stays tight and all 100 are self-sourced.
    farm = Offer("grow dye", "farming", {"red dye": 1}, time_per_unit=timedelta(minutes=1))
    prices = {**GOLD, "red dye": 0.008 * 0.06}
    plan = minimize(
        [farm], {"red dye": 100}, prices=prices, gold_weight=BUY_FREELY,
        ap_tradeable=frozenset({"red dye"}), non_mastery_goals=frozenset(),
    )
    assert plan.bought.get("red dye", 0) == pytest.approx(0)
    assert plan.time == timedelta(minutes=100)


def test_minimize_trades_for_a_material_use_even_when_the_item_is_a_mastery_goal():
    # Red Dye is a mastery goal AND a craft material for Cloth (also demanded).
    # The goal dye must be produced, but the material dye Cloth consumes may be traded.
    farm = Offer("grow dye", "farming", {"red dye": 1}, time_per_unit=timedelta(minutes=1))
    recipes = {"cloth": {"red dye": 5}}
    prices = {**GOLD, "red dye": 0.008 * 0.06}
    plan = minimize(
        [farm], {"red dye": 100, "cloth": 10}, recipes, 1.0, prices=prices,
        gold_weight=BUY_FREELY, ap_tradeable=frozenset({"red dye"}),
        non_mastery_goals=frozenset(),  # red dye is a mastery goal here
    )
    # 10 cloth need 50 material dye (tradeable); the 100 goal dye stay self-sourced.
    assert plan.bought.get("red dye", 0) <= 50 + 1e-6
    assert plan.crafts.get("red dye", 0) == pytest.approx(0)  # never crafted here
    assert plan.time >= timedelta(minutes=100) - timedelta(seconds=1)


def test_minimize_default_ap_tradeable_is_unchanged():
    # No ap_tradeable (the default) leaves the buy set at the gold consumables only.
    plan = minimize([FARM_ITEM], {"it": 50}, prices=GOLD, gold_weight=BUY_FREELY)
    assert "it" not in plan.bought


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


def test_thin_count_one_keeps_the_fastest_plan():
    # count=1 with more than one candidate used to divide by zero computing the
    # even-spread step; it should instead just keep the fastest (first) plan.
    plans = [SourcingPlan(time=timedelta(days=d)) for d in range(1, 11)]  # 1..10 days
    kept = _thin(plans, 1)
    assert len(kept) == 1
    assert kept[0].time.days == 1


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


# --- Deposit quantisation: plan times lifted to whole passive cycles -----------

# Antlers land once a day; needing 250 at 100 a deposit means the third deposit,
# at day 3 -- the continuous rate estimate of 2.5 days credits half a deposit
# that has not landed yet.
ANTLER_PEN = PassiveProduction(
    producers=[PassiveProducer("raptor pen", DAILY, {"antlers": 100})]
)


def test_minimize_lifts_plan_time_to_whole_deposits():
    offers = passive_offers(ANTLER_PEN)
    plan = minimize(offers, {"Antlers": 250}, quantize=ANTLER_PEN.quantized_wait)
    assert plan.time == timedelta(days=3)
    # Without quantize the continuous 2.5-day estimate stands, as before.
    continuous = minimize(offers, {"Antlers": 250})
    assert abs(continuous.time - timedelta(days=2.5)) < timedelta(seconds=1)


def test_minimize_quantized_exact_multiple_is_unchanged():
    offers = passive_offers(ANTLER_PEN)
    plan = minimize(offers, {"Antlers": 200}, quantize=ANTLER_PEN.quantized_wait)
    assert abs(plan.time - timedelta(days=2)) < timedelta(seconds=1)


def test_minimize_does_not_lift_passive_fill_past_the_driving_track():
    # Farming the 3,900 It (one a minute) takes 2d 17h and sets the makespan; the
    # 250 antlers finish continuously at 2.5 days *inside* that wait, so passive
    # is fill -- rounding its last deposit up to day 3 would push the whole plan
    # past the track that really drives it, for a sliver the plan would absorb
    # elsewhere. The lift only applies when passive is itself the bottleneck.
    offers = passive_offers(ANTLER_PEN) + [
        Offer("grow It", "farming", {"it": 1}, time_per_unit=timedelta(minutes=1)),
    ]
    plan = minimize(
        offers, {"Antlers": 250, "It": 3900}, quantize=ANTLER_PEN.quantized_wait
    )
    assert abs(plan.time - timedelta(minutes=3900)) < timedelta(seconds=1)


def test_passive_completion_caps_quantize_to_deposit_boundaries():
    (offer,) = passive_offers(ANTLER_PEN)
    plan = SourcingPlan(draws=[Draw(offer, 3600)])
    caps = _passive_completion_caps(
        plan, {"Antlers": 250}, {}, 1.0, [], {}, quantize=ANTLER_PEN.quantized_wait
    )
    # 3 whole days, not the continuous 250 / (100/1440) * 60 = 216000 s.
    assert [round(c) for c in caps] == [3 * 86400]


def test_frontier_plans_land_on_deposit_boundaries():
    # Antlers come free from the daily pen or fast from bought-net fishing, so the
    # frontier trades gold for time -- and with quantize every plan's time sits on
    # a whole-day deposit boundary (or zero for the all-fished fast end), with the
    # cheap end at the full 3-deposit wait.
    offers = passive_offers(ANTLER_PEN) + [
        Offer("fish Lake", "fishing", {"antlers": 1}, consumes={"large net": 1}, ln=1),
    ]
    plans = frontier(
        offers, {"Antlers": 250}, count=5, prices=GOLD,
        quantize=ANTLER_PEN.quantized_wait,
    )
    day = 86400.0
    for plan in plans:
        seconds = plan.time.total_seconds()
        offset = seconds % day
        assert min(offset, day - offset) < 1, f"{plan.time} is off-boundary"
    assert max(p.time for p in plans) == timedelta(days=3)


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


def test_leftover_inventory_share_is_held_stock_past_consumption():
    # 1,000 Arnold Palmer held plus 300 more made by passive; the craft's own
    # consumption (none here) would come out of the held stock first, so all
    # 1,000 held count as leftover, on top of the 300 the plan itself made.
    passive = Offer(
        "passive", "passive", {"arnold palmer": 3.0}, time_per_unit=timedelta(minutes=1)
    )
    plan = SourcingPlan(draws=[Draw(passive, 100)])
    inventory = {"arnold palmer": 1000.0}
    leftovers = plan_leftovers(plan, {}, {}, 1.0, [], inventory)
    assert leftovers == pytest.approx({"arnold palmer": 1300.0})
    share = leftover_inventory_share(plan, {}, {}, 1.0, [], inventory)
    assert share == pytest.approx({"arnold palmer": 1000.0})


def test_leftover_inventory_share_nets_out_what_the_plan_consumed():
    # 100 held Straw, but a craft consumes 60 of it -- only the 40 that survive
    # count as an inventory-sourced leftover; passive makes none of it itself.
    plan = SourcingPlan(crafts={"twine": 30})
    inventory = {"straw": 100.0}
    share = leftover_inventory_share(
        plan, {}, {"twine": {"straw": 2}}, 1.0, [], inventory
    )
    assert share == pytest.approx({"straw": 40.0})


def test_leftover_inventory_share_skips_directly_demanded_items():
    # Demand is already net of held stock (plan_balance's convention), so an
    # item's own inventory never reaches its leftover when it is the goal itself.
    plan = SourcingPlan(draws=[Draw(Offer("buy", "buy", {"twine": 1.0}), 500)])
    share = leftover_inventory_share(
        plan, {"twine": 100}, {}, 1.0, [], {"twine": 1000.0}
    )
    assert "twine" not in share


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


def test_frontier_finds_max_passive_plan_when_cheap_endpoint_quantizes_past_it():
    # Regression: the buy-nothing "cheap" endpoint self-sources nets from a
    # fast-accruing material (wire, 10/min) to fish the goal in ~10 min
    # continuously -- far faster than sourcing the goal straight from passive
    # (0.1/min, ~16 h 40 m). But wire deposits only in whole *days*, so
    # quantisation lifts the cheap plan's reported time to a full day, *past*
    # the goal's own ~16 h 40 m passive completion. That inversion is the trap:
    #   * the fast-regime epsilon sweep is geometric over [t_fast, ~1 day] and
    #     sparse near the top, so it never samples a cap in the 14-24 h band
    #     where the pure-passive plan lives (and the cider-ish cheap plan injects
    #     no full-completion breakpoint there either); and
    #   * the slow regime only runs when t_bound exceeds t_cheap -- so comparing
    #     against the *quantized* cheap day (24 h > 16 h 40 m) wrongly skips it.
    # The gate must use the cheap endpoint's *continuous* makespan, or the
    # maximally-passive plan (goal from passive, zero nets) is never generated
    # and a `--max-ln 0` run wrongly reports no zero-net plan exists.
    passive = Offer(
        "passive", "passive", {"goal": 0.1, "wire": 10.0},
        time_per_unit=timedelta(minutes=1),
    )
    fish = Offer("fish goal", "fishing", {"goal": 1},
                 consumes={"large net": 1}, ln=1)
    recipes = {"large net": {"wire": 1.0}}

    def quantize(item, residual):
        if item == "wire":  # lumpy: any positive residual waits a whole day
            return timedelta(days=1) if residual > 1e-6 else timedelta(0)
        if item == "goal":  # fine-grained: the continuous 0.1/min wait
            return timedelta(minutes=residual / 0.1)
        return None

    plans = frontier([passive, fish], {"goal": 100}, recipes, prices=GOLD,
                     quantize=quantize)
    cheap = minimize([passive, fish], {"goal": 100}, recipes, 1.0,
                     prices=GOLD, gold_weight=SELF_SOURCE, quantize=quantize)
    assert round(cheap.large_nets) > 0          # cheap self-sources nets to go fast
    assert cheap.time >= timedelta(days=1)       # but quantizes past the goal's wait
    # The frontier must include the maximally-passive plan: no nets, goal sourced
    # straight from passive, finishing well before the cheap plan's quantized day.
    passive_plan = min(plans, key=lambda p: p.large_nets)
    assert round(passive_plan.large_nets) == 0
    assert passive_plan.time < cheap.time


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


def test_fast_endpoint_tie_break_counts_trade_gold_against_pumpkin_juice():
    # A mastery goal "it" (need 100) is either crafted from material "mat" (10 each,
    # only AP-tradeable) or covered directly by Pumpkin Juice -- both instant, so the
    # fast endpoint's stage-2 tie-break decides. total_value must charge the trade its
    # gold, else trading reads as free and beats any priced juice: the Oak-trade vs
    # Pumpkin-Juice regression. Here PJ (10 * 0.1 = 1 g) undercuts the trade (1000 mat
    # * 0.01 = 10 g), so the plan drinks juice and buys no material.
    recipes = {"it": {"mat": 10}}
    plan = minimize(
        [], {"it": 100}, recipes, 1.0, prices={**GOLD, "mat": 0.01},
        gold_weight=BUY_FREELY, pj_gain={"it": 10}, pj_price=0.1,
        ap_tradeable=frozenset({"mat"}),
    )
    assert plan.pumpkin_juices.get("it") == pytest.approx(10)
    assert plan.bought.get("mat", 0) == pytest.approx(0)
    assert plan.time == timedelta(0)


def test_fast_endpoint_still_trades_when_the_trade_is_cheaper_than_juice():
    # The honest comparison cuts both ways: with the material trade cheap (1000 mat *
    # 0.0001 = 0.1 g) and juice dear (10 * 1 = 10 g), the plan trades for the material
    # and drinks no juice -- the fix values trades, it does not ban them.
    recipes = {"it": {"mat": 10}}
    plan = minimize(
        [], {"it": 100}, recipes, 1.0, prices={**GOLD, "mat": 0.0001},
        gold_weight=BUY_FREELY, pj_gain={"it": 10}, pj_price=1.0,
        ap_tradeable=frozenset({"mat"}),
    )
    assert plan.bought.get("mat") == pytest.approx(1000)
    assert plan.pumpkin_juices.get("it", 0) == pytest.approx(0)
    assert plan.time == timedelta(0)


def test_minimize_respects_the_pumpkin_juice_limit():
    # Capped at 3 juices (covering 30), the other 70 must be grown (70 minutes).
    plan = minimize([FARM_ITEM], {"It": 100}, prices=GOLD, gold_weight=BUY_FREELY,
                    pj_gain={"it": 10}, pj_price=5, pj_limit=3)
    assert plan.pumpkin_juices.get("it") == pytest.approx(3)
    assert plan.time == timedelta(minutes=70)


def test_minimize_caps_pumpkin_juice_at_the_goal():
    # 15 demand, each juice covers 10. Juices are whole, so covering the goal takes 2
    # -- the second overshoots, and the clamp means it earns only the 5 units left,
    # not 10 (mastery cannot exceed the requirement). Capping the *credit* instead
    # used to cap the count at a fractional 1.5 juices, which nobody can drink.
    plan = minimize([FARM_ITEM], {"It": 15}, prices=GOLD, gold_weight=BUY_FREELY,
                    pj_gain={"it": 10}, pj_price=5)
    assert plan.pumpkin_juices.get("it") == 2
    assert plan.pumpkin_credit.get("it") == pytest.approx(15)  # clamped, not 20
    assert plan.time == timedelta(0)


def test_minimize_drinks_a_whole_juice_for_a_goal_smaller_than_one_juice():
    # The goal needs less than a single juice covers (3 units against a 10-unit juice).
    # One whole juice covers it outright; the old credit cap (`pj*gain <= need`) banned
    # that juice and settled for 0.3 of one -- a plan that reported no juice at all and
    # no craft either, leaving the item looking silently skipped.
    plan = minimize([FARM_ITEM], {"It": 3}, prices=GOLD, gold_weight=BUY_FREELY,
                    pj_gain={"it": 10}, pj_price=5)
    assert plan.pumpkin_juices.get("it") == 1
    assert plan.pumpkin_credit.get("it") == pytest.approx(3)  # clamped at the goal
    assert plan.time == timedelta(0)
    assert plan.draws == []  # fully covered by the juice: nothing to grow


def test_minimize_self_sources_over_pumpkin_juice_when_gold_is_dear():
    # A huge gold weight buys nothing, juice included: grow it instead.
    plan = minimize([FARM_ITEM], {"It": 100}, prices=GOLD, gold_weight=SELF_SOURCE,
                    pj_gain={"it": 10}, pj_price=5)
    assert plan.pumpkin_juices == {}
    # approx, not ==: the whole-juice `pj` vars make this a MIP, and stage 3 settles
    # anywhere inside the stage-1 makespan pin's own slack (t*(1+1e-9)+1e-9 minutes),
    # landing a few microseconds above 100 minutes. Immaterial -- the report rounds to
    # minutes -- but no longer exact to the femtosecond the pure LP happened to give.
    assert plan.time.total_seconds() == pytest.approx(6000, abs=1e-3)


def test_minimize_drinks_pumpkin_juice_priced_at_zero():
    # A present price of 0 means juice is *free*, not off: gating the pj vars on
    # `pj_price > 0` silently disabled the whole mechanism (the falsy-zero trap).
    # `pj_gain` alone says whether juice is enabled -- main leaves it empty when not.
    plan = minimize([FARM_ITEM], {"It": 100}, prices=GOLD, gold_weight=BUY_FREELY,
                    pj_gain={"it": 10}, pj_price=0)
    assert plan.pumpkin_juices.get("it") == pytest.approx(10)
    assert plan.pumpkin_gold == pytest.approx(0)
    assert plan.time == timedelta(0)


def test_minimize_pumpkin_juice_count_is_stable_across_tiny_prices():
    # Juice is the only consumable with no self-sourced form, so its every use is a
    # purchase -- minimising *bought gold* in the refinement's stage 2 therefore read
    # as "fewest juices" and traded away near-free juice for held stock. It also put
    # the choice below the solver's resolution (a ~1e5 pj_gain against a ~1e-3 cost),
    # so the count flipped on a price change from 0.0005 to 0.001. Stage 2 minimises
    # total_value instead, so a cheaper juice is never used *less*.
    counts = [
        minimize([FARM_ITEM], {"It": 100}, prices=GOLD, gold_weight=BUY_FREELY,
                 pj_gain={"it": 10}, pj_price=price).pumpkin_juices.get("it", 0.0)
        for price in (0.0, 0.0001, 0.0005, 0.001, 0.01)
    ]
    assert counts == [pytest.approx(10)] * 5


def test_sweep_drops_caps_no_plan_in_the_regime_can_meet():
    # The sweep's caps come from the active endpoints, where juice makes t_fast
    # instant -- but favour_passive switches juice off, so such a cap is unreachable
    # and its solve is genuinely infeasible. That is a gap in the sample, not a bad
    # demand: skip it. It used to read back as an empty, cost-(0, 0) plan and sort to
    # the head of the frontier as a bogus 0-minute, 0-gold "fastest".
    base = optimizer._build_base(
        [FARM_ITEM], {"It": 100}, {}, 1.0, [], {}, 0.0, GOLD,
        pj_gain={"it": 10}, pj_price=5,
    )
    # 100 units at 1/minute: a 1-second cap is impossible with juice off.
    plans = optimizer._sweep(base, [1.0], favour_passive=True)
    assert plans == []
    # ...and the same base still solves at a cap it can actually meet.
    assert optimizer._sweep(base, [100 * 60], favour_passive=True)


def test_solve_configured_relaxes_pumpkin_juice_when_pj_integer_false():
    # The mechanism behind the frontier's speed: juice is normally integer (a MIP,
    # ~50x an LP per solve), but the sweep solves the continuous relaxation. 15 units,
    # each juice covers 10, so the relaxation drinks a fractional 1.5 juices where the
    # integer solve rounds to 2. frontier sweeps this cheap form and only finishes the
    # plans it returns exact.
    base = optimizer._build_base(
        [FARM_ITEM], {"It": 15}, {}, 1.0, [], {}, 0.0, GOLD,
        pj_gain={"it": 10}, pj_price=5,
    )
    optimizer._solve_configured(base, gold_weight=BUY_FREELY, pj_integer=False)
    # The relaxation is what the LP actually solved -- the reported count rounds it, but
    # the variable itself is fractional (and the plan's gold is priced off it).
    assert base.pj["it"].value() == pytest.approx(1.5)
    exact = optimizer._solve_configured(base, gold_weight=BUY_FREELY, pj_integer=True)
    assert base.pj["it"].value() == pytest.approx(2)
    assert exact.pumpkin_juices.get("it") == 2


def test_frontier_returns_whole_pumpkin_juices():
    # The visible contract: every plan the frontier returns -- each a row of the
    # summary table -- drinks a whole number of juices, even though the interior sweep
    # that ranked them ran the fractional relaxation. 15 units, gain 10: the fast plan
    # would drink a fractional 1.5 unfinished.
    plans = frontier([FARM_ITEM], {"It": 15}, prices=GOLD, pj_gain={"it": 10}, pj_price=5)
    drank = False
    for plan in plans:
        for count in plan.pumpkin_juices.values():
            assert count == round(count), f"fractional juice {count} in a returned plan"
            drank = True
    assert drank  # the setup actually exercises juice, or the test proves nothing


def test_frontier_juice_count_is_non_increasing_with_time():
    # The frontier's whole-juice count only falls as makespan grows (each juice buys
    # a slice of farming time), so the rows read as a decreasing-juice curve.
    plans = frontier([FARM_ITEM], {"It": 50}, prices=GOLD, count=6,
                     pj_gain={"it": 10}, pj_price=5)
    counts = [round(sum(p.pumpkin_juices.values())) for p in plans]
    assert counts == sorted(counts, reverse=True)


def test_pumpkin_gap_fill_recovers_intermediate_juice_counts():
    # Between a 5-juice fast plan and a juice-free 50-minute plan, the whole counts
    # 4, 3, 2, 1 all exist; bisecting the cap axis pins a real plan at each even when
    # the relaxed sweep skipped it. (Count 0 sits on the bracket's far endpoint here,
    # already a shown row, so it is not among the recovered interior counts.)
    base = optimizer._build_base(
        [FARM_ITEM], {"It": 50}, prices=GOLD, pj_gain={"it": 10}, pj_price=5)
    fill = optimizer._pumpkin_gap_fill(base, 0.0, 5, 50 * 60, 0)
    assert set(fill) == {1, 2, 3, 4}
    for level, plan in fill.items():
        assert round(sum(plan.pumpkin_juices.values())) == level


def test_thin_pumpkin_keeps_one_plan_per_juice_count():
    # Fastest-first plans carrying juice 3, 3, 3, 2, 1, 0, 0. Thinned to 5 must keep
    # both ends and the *fastest* plan at each distinct count -- the decreasing-juice
    # curve -- rather than an even time-spread that repeats the 3-juice wall.
    def plan(juice: int) -> SourcingPlan:
        return SourcingPlan(pumpkin_juices={"it": juice})

    plans = [plan(3), plan(3), plan(3), plan(2), plan(1), plan(0), plan(0)]
    kept = optimizer._thin_pumpkin(plans, 5)
    counts = [round(sum(p.pumpkin_juices.values())) for p in kept]
    assert counts == [3, 2, 1, 0, 0]


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


# --- Stamina opportunity cost (the ``price_stamina`` option) ---

# A cider-like explore: instant, burns one stamina, yields 0.4 wood. 100 explores
# (250 stamina) cover a demand of 100 wood; the stamina comes from the bank, the
# Farmhouse-like passive deposit, or drinking Orange Juice.
_STAMINA_EXPLORE = Offer(
    "explore", "exploring", {"wood": 0.4}, consumes={"stamina": 1.0},
    time_per_unit=timedelta(0),
)
# Per-stamina gold: an Orange Juice (0.008 g) yields 100 stamina.
_STAMINA_GOLD = {**GOLD, "stamina": 0.008 / 100}


def test_stamina_cost_charges_banked_stamina_at_orange_juice_value():
    offers = [_STAMINA_EXPLORE, *stamina_offers()]
    demand = {"wood": 40}  # 100 explores -> 100 stamina, all from the 1000 banked
    priced = minimize(offers, demand, prices=_STAMINA_GOLD, gold_weight=BUY_FREELY,
                      inventory={"stamina": 1000})
    # 100 free stamina at 0.008/100 each (solver slack of a few 1e-9 aside).
    assert priced.stamina_gold == pytest.approx(100 * 0.008 / 100, abs=1e-5)
    # It buys nothing, so it is not gold, but cost's net gold charges it.
    assert priced.gold == pytest.approx(0, abs=1e-6)
    assert priced.cost[1] == pytest.approx(priced.stamina_gold, abs=1e-6)


def test_stamina_cost_off_without_the_price_key():
    # No "stamina" key (the option off): banked stamina stays free, byte-identical.
    offers = [_STAMINA_EXPLORE, *stamina_offers()]
    plan = minimize(offers, {"wood": 40}, prices=GOLD, gold_weight=BUY_FREELY,
                    inventory={"stamina": 1000})
    assert plan.stamina_gold == 0
    assert plan.cost[1] == pytest.approx(0)


def test_stamina_cost_excludes_orange_juice_stamina():
    # No bank: the plan must drink Orange Juice, which is already priced via the
    # juice (in gold) -- so the stamina charge must not double-count it.
    offers = [_STAMINA_EXPLORE, *stamina_offers()]
    plan = minimize(offers, {"wood": 40}, prices=_STAMINA_GOLD,
                    gold_weight=BUY_FREELY, inventory={})
    assert plan.orange_juice == pytest.approx(1)  # 100 stamina / 100 per juice
    assert plan.stamina_gold == pytest.approx(0, abs=1e-6)
    assert plan.gold == pytest.approx(0.008)  # the juice, once


def test_stamina_cost_charges_apple_stamina():
    # Eating an Apple for stamina is NOT exempt (unlike drinking Orange Juice): the
    # Apples are near-free chest byproducts, so exempting their stamina let the solver
    # dodge the banked/passive charge by manufacturing free stamina. A stamina unit
    # costs its Orange-Juice replacement value whatever its source. Here the only
    # stamina source is eating Apples (from a held stock), so the whole burn is charged.
    eat_apple = next(o for o in stamina_offers() if "apple" in o.consumes)
    offers = [_STAMINA_EXPLORE, eat_apple]
    plan = minimize(offers, {"wood": 40}, prices=_STAMINA_GOLD,
                    gold_weight=BUY_FREELY, inventory={"apple": 1000})
    # 100 stamina burned (100 explores for 40 wood at 0.4 each), all charged.
    assert plan.stamina_gold == pytest.approx(100 * 0.008 / 100, abs=1e-5)


def test_stamina_cost_absent_from_plan_gold():
    # plan_gold is the market value of tradeable consumables; the stamina
    # opportunity cost is carried by cost, not folded in here.
    plan = SourcingPlan(large_nets=100, stamina_gold=50)
    assert plan_gold(plan, _STAMINA_GOLD) == pytest.approx(100 * 0.01)


# --- Ascension: the LP chooses which candidates to master/grandmaster/mega ---


def test_minimize_ascension_selects_the_cheaper_candidate_to_cover_ak():
    # Two candidates each need 10 mastery progress (delta_master) for a plain
    # master; "cheap" gathers in 1 min/unit, "expensive" in 100 min/unit. AK 10
    # is exactly one master, so the LP -- minimising time -- masters "cheap" and
    # leaves "expensive" untouched.
    cheap = Offer("gather cheap", "farming", {"cheap": 1}, time_per_unit=timedelta(minutes=1))
    expensive = Offer(
        "gather expensive", "farming", {"expensive": 1}, time_per_unit=timedelta(minutes=100)
    )
    spec = ascension.AscensionSpec(
        candidates=[
            ascension.StepCandidate("cheap", 10, None, None),
            ascension.StepCandidate("expensive", 10, None, None),
        ],
        ak_required=10,
        chore_rate_per_day=0,
        megas_required=0,
    )
    plan = minimize([cheap, expensive], {}, ascension=spec)
    assert plan.ascension.masters == ["cheap"]
    assert plan.ascension.grandmasters == []
    assert plan.ascension.ak_from_steps == pytest.approx(10)
    # S_i (gross gathered * mastery_bonus) covers the delta with no spare needed.
    assert plan.ascension.mastery_supply["cheap"] == pytest.approx(10)
    assert "expensive" not in plan.ascension.mastery_supply


def test_minimize_ascension_orders_grandmaster_after_master():
    # One candidate, AK 110 = one master (10) + one grandmaster (100): the
    # ordering link (y_gm <= y_m) forces both selected, and S covers both deltas.
    item = Offer("gather x", "farming", {"x": 1}, time_per_unit=timedelta(minutes=1))
    spec = ascension.AscensionSpec(
        candidates=[ascension.StepCandidate("x", delta_master=10, delta_grandmaster=90, delta_mega=None)],
        ak_required=110,
        chore_rate_per_day=0,
        megas_required=0,
    )
    plan = minimize([item], {}, ascension=spec)
    assert plan.ascension.masters == ["x"]
    assert plan.ascension.grandmasters == ["x"]
    assert plan.ascension.ak_from_steps == pytest.approx(110)
    assert plan.ascension.mastery_supply["x"] >= 100 - 1e-6


def test_minimize_ascension_mastery_supply_counts_gross_chained_production():
    # "a" is gathered then fully consumed as a material of crafted "b" (demand on
    # b) -- mastery-chaining means a's gross production still counts toward its
    # own mastery, even though none of it survives to a balance.
    gather_a = Offer("gather a", "farming", {"a": 1}, time_per_unit=timedelta(minutes=1))
    recipes = {"b": {"a": 1.0}}
    spec = ascension.AscensionSpec(
        candidates=[ascension.StepCandidate("a", 10, None, None)],
        ak_required=10,
        chore_rate_per_day=0,
        megas_required=0,
    )
    plan = minimize([gather_a], {"b": 5}, recipes=recipes, ascension=spec)
    assert plan.crafts.get("b") == pytest.approx(5)
    assert plan.ascension.masters == ["a"]
    # S_a >= delta_master with no "a" left over (5 of the ~9.09 gathered go into b).
    assert plan.ascension.mastery_supply["a"] == pytest.approx(10)


def test_minimize_ascension_excludes_bought_units_from_mastery_supply():
    # A buyable, cheaply-priced candidate that is also (slowly) gatherable: even
    # buying freely (a tiny gold weight), mastery must come from gathering --
    # bought units never enter S_i.
    gather_ap = Offer(
        "gather ap", "farming", {"arnold palmer": 1}, time_per_unit=timedelta(minutes=10)
    )
    spec = ascension.AscensionSpec(
        candidates=[ascension.StepCandidate("arnold palmer", 10, None, None)],
        ak_required=10,
        chore_rate_per_day=0,
        megas_required=0,
    )
    plan = minimize(
        [gather_ap], {}, prices={"arnold palmer": 0.01}, ascension=spec, gold_weight=BUY_FREELY,
    )
    assert plan.ascension.masters == ["arnold palmer"]
    assert plan.bought == {}
    assert sum(d.units for d in plan.draws) == pytest.approx(10 / 1.1)


def test_minimize_ascension_mastery_supply_counts_opens():
    # "gem" is only obtainable by opening a chest -- mastery must still be
    # satisfiable through the open flow's yield.
    gather_key = Offer("gather key", "farming", {"key": 1}, time_per_unit=timedelta(minutes=1))
    flow = OpenFlow("chest", consumes={"key": 1}, yields={"gem": 2})
    spec = ascension.AscensionSpec(
        candidates=[ascension.StepCandidate("gem", 10, None, None)],
        ak_required=10,
        chore_rate_per_day=0,
        megas_required=0,
    )
    plan = minimize([gather_key], {}, openables=[flow], ascension=spec)
    assert plan.ascension.masters == ["gem"]
    assert plan.opens.get("chest", 0) > 0
    assert plan.ascension.mastery_supply["gem"] == pytest.approx(10)


def test_minimize_ascension_waits_on_chores_when_production_is_dear():
    # Production is 1000 min/unit (dear); a large chore rate makes waiting for
    # chore AK cheaper than gathering -- the plan idles past every track's own
    # time, and plan.time reflects the makespan (not any draw).
    expensive = Offer("gather x", "farming", {"x": 1}, time_per_unit=timedelta(minutes=1000))
    spec = ascension.AscensionSpec(
        candidates=[ascension.StepCandidate("x", 10000, None, None)],
        ak_required=10,
        chore_rate_per_day=100,
        megas_required=0,
    )
    plan = minimize([expensive], {}, ascension=spec)
    assert plan.ascension.masters == []
    assert plan.ascension.ak_from_chores >= 10 - 1e-6
    assert plan.time > timedelta(0)
    # No draws at all -- the whole 10 AK comes from chores, not any track.
    assert plan.draws == []


def test_minimize_ascension_masters_instead_of_waiting_with_no_chore_rate():
    # Same dear production, but chore_rate_per_day=0 -- no free lunch, so the LP
    # has to master the item after all.
    expensive = Offer("gather x", "farming", {"x": 1}, time_per_unit=timedelta(minutes=1000))
    spec = ascension.AscensionSpec(
        candidates=[ascension.StepCandidate("x", 10000, None, None)],
        ak_required=10,
        chore_rate_per_day=0,
        megas_required=0,
    )
    plan = minimize([expensive], {}, ascension=spec)
    assert plan.ascension.masters == ["x"]
    assert plan.ascension.ak_from_chores == 0


def test_minimize_ascension_selects_the_cheaper_candidate_for_a_mega():
    # Both candidates are already grandmastered (delta_master/grandmaster None),
    # each needing 1000 more progress to mega; one is far cheaper to produce.
    cheap = Offer("gather cheap", "farming", {"cheap": 1}, time_per_unit=timedelta(minutes=1))
    expensive = Offer(
        "gather expensive", "farming", {"expensive": 1}, time_per_unit=timedelta(minutes=100)
    )
    spec = ascension.AscensionSpec(
        candidates=[
            ascension.StepCandidate("cheap", None, None, 1000),
            ascension.StepCandidate("expensive", None, None, 1000),
        ],
        ak_required=0,
        chore_rate_per_day=0,
        megas_required=1,
    )
    plan = minimize([cheap, expensive], {}, ascension=spec)
    assert plan.ascension.megas == ["cheap"]


def test_minimize_ascension_mega_floor_lets_the_step_still_get_selected():
    # Milestone 6's per-stage scheduling forces a chosen mega item's supply via
    # mega_supply_floors, independent of whether its own y_mega is selected --
    # but the mega COUNT constraint (megas_required) still needs y_mega=1 to
    # count it. With only one mega-step candidate, pinning y_mega down to 0
    # during round-and-fix's two-sided trial leaves megas_required unmet (no
    # other item can cover it) -- infeasible -- so the pin-up trial wins and
    # the step ends up selected despite the floor already covering its supply.
    gather = Offer("gather x", "farming", {"x": 1}, time_per_unit=timedelta(seconds=1))
    spec = ascension.AscensionSpec(
        candidates=[ascension.StepCandidate("x", 10, 90, 900)],
        ak_required=0,
        chore_rate_per_day=0,
        megas_required=1,
        mega_supply_floors={"x": 900},
    )
    plan = minimize([gather], {}, ascension=spec)
    assert plan.ascension.megas == ["x"]
    # Selecting mega requires its prerequisites (see _ASCEND_PREREQS): master
    # and grandmaster get pinned up alongside it.
    assert plan.ascension.masters == ["x"]
    assert plan.ascension.grandmasters == ["x"]


def test_minimize_ascension_mega_floor_does_not_change_which_item_is_cheapest():
    # A floor on the cheap item (forcing its supply regardless of selection)
    # must not make it MORE expensive to select than the alternative -- the
    # cheaper item should still win the mega pick.
    cheap = Offer("gather cheap", "farming", {"cheap": 1}, time_per_unit=timedelta(minutes=1))
    expensive = Offer(
        "gather expensive", "farming", {"expensive": 1}, time_per_unit=timedelta(minutes=100)
    )
    spec = ascension.AscensionSpec(
        candidates=[
            ascension.StepCandidate("cheap", None, None, 1000),
            ascension.StepCandidate("expensive", None, None, 1000),
        ],
        ak_required=0,
        chore_rate_per_day=0,
        megas_required=1,
        mega_supply_floors={"cheap": 1000},
    )
    plan = minimize([cheap, expensive], {}, ascension=spec)
    assert plan.ascension.megas == ["cheap"]


def test_minimize_ascension_reports_incidental_mega_crossings():
    # ``megas`` reports 1M-threshold CROSSINGS, not y selections (see
    # _ascension_outcome): an item whose gross supply covers its whole
    # remaining distance to 1M is a real megamastery the climb earns --
    # ascension.advance will count it next stage -- even though nothing
    # needed its y_mega (megas_required is 0 here). "ore" is only produced
    # as the ingredient of the ring master the AK requirement forces:
    # ~91 ring crafts consume ~909 ore, whose gross supply (~1000) covers
    # ore's remaining 500 to mega.
    gather = Offer("gather ore", "farming", {"ore": 1}, time_per_unit=timedelta(seconds=1))
    spec = ascension.AscensionSpec(
        candidates=[
            ascension.StepCandidate("ring", 100, None, None),
            ascension.StepCandidate("ore", None, None, 500),
        ],
        ak_required=10,
        chore_rate_per_day=0,
        megas_required=0,
    )
    plan = minimize([gather], {}, recipes={"ring": {"ore": 10}}, ascension=spec)
    assert plan.ascension.masters == ["ring"]
    assert plan.ascension.megas == ["ore"]


def test_minimize_ascension_rounds_fractional_relaxation_to_integral_selection():
    # Two equal-cost candidates each need delta_master=10; ak_required=15 means
    # the LP relaxation can satisfy it with 1.5 masters' worth split across both
    # (fractional y). Round-and-fix must snap to a real, integral plan: since
    # neither single master alone reaches AK 15, both get selected (target_ak=15,
    # covered by 10+10=20).
    a = Offer("gather a", "farming", {"a": 1}, time_per_unit=timedelta(minutes=1))
    b = Offer("gather b", "farming", {"b": 1}, time_per_unit=timedelta(minutes=1))
    spec = ascension.AscensionSpec(
        candidates=[
            ascension.StepCandidate("a", 10, None, None),
            ascension.StepCandidate("b", 10, None, None),
        ],
        ak_required=15,
        chore_rate_per_day=0,
        megas_required=0,
    )
    plan = minimize([a, b], {}, ascension=spec)
    assert sorted(plan.ascension.masters) == ["a", "b"]
    assert plan.ascension.ak_from_steps == pytest.approx(20)
    assert plan.ascension.mastery_supply["a"] == pytest.approx(10)
    assert plan.ascension.mastery_supply["b"] == pytest.approx(10)


def test_minimize_ascension_rounds_to_a_master_not_a_grandmaster_for_a_small_gap():
    # Regression: the relaxation covers a 10-AK requirement with fractional
    # *grandmaster* slices (100 AK per 90k of delta is more AK-efficient than a
    # master's 10 per 10k), landing at y_m = y_gm = 1/11 on the cheap item. A
    # rounder picking by descending y (or AK-per-delta) then rounds the whole
    # grandmaster up -- pinning 100k of mastery supply (~15h of farming) where a
    # lone master's 10k (~91min) covers the gap. The greedy cover must pick the
    # master alone: 10000/min(10, 10) = 1000 beats the master+grandmaster
    # package's 100000/min(110, 10) = 10000.
    cheap = Offer("grow corn", "farming", {"corn": 100}, time_per_unit=timedelta(minutes=1))
    dear = Offer(
        "grow gold rose", "farming", {"gold rose": 1}, time_per_unit=timedelta(minutes=100)
    )
    spec = ascension.AscensionSpec(
        candidates=[
            ascension.StepCandidate("corn", 10000, 90000, 900000),
            ascension.StepCandidate("gold rose", 10000, 90000, 900000),
        ],
        ak_required=10,
        chore_rate_per_day=0,
        megas_required=0,
    )
    plan = minimize([cheap, dear], {}, ascension=spec, gold_weight=1e-9)
    assert plan.ascension.masters == ["corn"]
    assert plan.ascension.grandmasters == []
    assert plan.ascension.ak_from_steps == pytest.approx(10)
    assert plan.ascension.mastery_supply["corn"] == pytest.approx(10000)
    assert plan.time < timedelta(hours=2)


def test_minimize_ascension_is_deterministic():
    a = Offer("gather a", "farming", {"a": 1}, time_per_unit=timedelta(minutes=1))
    b = Offer("gather b", "farming", {"b": 1}, time_per_unit=timedelta(minutes=1))
    spec = ascension.AscensionSpec(
        candidates=[
            ascension.StepCandidate("a", 10, None, None),
            ascension.StepCandidate("b", 10, None, None),
        ],
        ak_required=15,
        chore_rate_per_day=0,
        megas_required=0,
    )
    plan1 = minimize([a, b], {}, ascension=spec)
    plan2 = minimize([a, b], {}, ascension=spec)
    assert plan1.ascension == plan2.ascension
    assert plan1.time == plan2.time
    assert [d.offer.name for d in plan1.draws] == [d.offer.name for d in plan2.draws]


def test_minimize_ascension_none_matches_plain_minimize():
    # A plain, non-ascension scenario run both ways must agree byte-for-byte.
    plan_default = minimize([FARM_ITEM, FISH_ITEM], {"It": 100}, prices=GOLD,
                            gold_weight=SELF_SOURCE)
    plan_explicit = minimize([FARM_ITEM, FISH_ITEM], {"It": 100}, prices=GOLD,
                             gold_weight=SELF_SOURCE, ascension=None)
    assert plan_default.ascension is None
    assert plan_explicit.ascension is None
    assert plan_default.time == plan_explicit.time
    assert plan_default.gold == plan_explicit.gold
    assert plan_default.draws == plan_explicit.draws
    assert plan_default.bought == plan_explicit.bought


def test_minimize_ascension_with_empty_demand_does_not_early_exit():
    # demand={} with a spec whose candidate is obtainable must still build and
    # solve the LP (the old "no items -> trivial remainder-only plan" early
    # return must not fire just because nothing is directly demanded).
    cheap = Offer("gather cheap", "farming", {"cheap": 1}, time_per_unit=timedelta(minutes=1))
    spec = ascension.AscensionSpec(
        candidates=[ascension.StepCandidate("cheap", 10, None, None)],
        ak_required=10,
        chore_rate_per_day=0,
        megas_required=0,
    )
    plan = minimize([cheap], {}, ascension=spec)
    assert plan.remainder == {}
    assert plan.ascension.masters == ["cheap"]


def test_ascension_free_crossings_end_up_selected():
    # "side" and "bonus" ride along on the demanded item's own offer (100
    # "main" co-produces 100 of each = 110 gross supply after the 1.1
    # mastery bonus), so their steps cost nothing; ak_required=115 exceeds
    # what side alone pays (110), so bonus must be picked up too. Whatever
    # vertex the relaxation lands on -- integral, or fractional across the
    # free steps -- every supply-covered step the AK requirement leans on
    # must come back SELECTED (the loop's provable pin-up shortcut prefers
    # completing a free step over dropping it). "scrap" (~5.5 supply, short
    # of its 10 delta) must stay unselected: forcing it up would cost real
    # production.
    bundle = Offer(
        "gather main", "farming",
        {"main": 1, "side": 1, "bonus": 1, "scrap": 0.05},
        time_per_unit=timedelta(minutes=1),
    )
    spec = ascension.AscensionSpec(
        candidates=[
            ascension.StepCandidate("side", 10, 90, None),
            ascension.StepCandidate("bonus", 10, None, None),
            ascension.StepCandidate("scrap", 10, 90, None),
        ],
        ak_required=115,
        chore_rate_per_day=0,
        megas_required=0,
    )
    plan = minimize([bundle], {"main": 100}, ascension=spec)
    assert plan.ascension.masters == ["bonus", "side"]
    assert plan.ascension.grandmasters == ["side"]
    assert plan.ascension.ak_from_steps == pytest.approx(120)
    # The free steps cost nothing: the plan is still just the demand's own
    # 100 gathers (a second of tolerance absorbs makespan-variable float dust).
    assert abs(plan.time - timedelta(minutes=100)) < timedelta(seconds=1)


def test_build_base_drops_unmasterable_candidates():
    # "ghost" has no channel/recipe/open at all -- it lands in unmasterable and
    # is never selectable, but the LP still solves via the other candidate.
    cheap = Offer("gather cheap", "farming", {"cheap": 1}, time_per_unit=timedelta(minutes=1))
    spec = ascension.AscensionSpec(
        candidates=[
            ascension.StepCandidate("cheap", 10, None, None),
            ascension.StepCandidate("ghost", 10, None, None),
        ],
        ak_required=10,
        chore_rate_per_day=0,
        megas_required=0,
    )
    base = _build_base([cheap], {}, ascension=spec)
    assert base.unmasterable == ["ghost"]
    assert "ghost" not in base.ascend_y

    plan = minimize([cheap], {}, ascension=spec)
    assert plan.ascension.masters == ["cheap"]
    assert "ghost" not in plan.ascension.masters
    assert "ghost" not in plan.ascension.mastery_supply


# --- Spread engine: SpreadSpec / _spread / _axis_value / efficiency / graphs ---
#
# FARM_ITEM + FISH_ITEM (defined at the top of this file) give a simple, exactly
# linear time<->gold tradeoff at these prices: buying ``b`` of 100 "It" via
# FISH_ITEM costs ``b * 0.01`` gold (a large net each) and leaves ``100 - b``
# minutes of farming, so gold = 0.01 * (100 - time_minutes). That linearity is
# what lets the gold-axis test check bisected targets tightly.


def test_axis_value_reads_time_gold_and_juice():
    plan = SourcingPlan(
        time=timedelta(seconds=120), gold=5.0, pumpkin_juices={"it": 3}
    )
    assert optimizer._axis_value(plan, "time") == pytest.approx(120.0)
    assert optimizer._axis_value(plan, "gold") == pytest.approx(plan.cost[1])
    assert optimizer._axis_value(plan, "pumpkin-juice") == 3


def test_solve_at_axis_target_bisects_gold_to_the_target():
    base = _build_base([FARM_ITEM, FISH_ITEM], {"It": 100}, {}, 1.0, [], {}, 0.0, GOLD)
    plan = optimizer._solve_at_axis_target(base, "gold", 0.5, 0.0, 6000.0)
    # b = 100 * gold, so a target of 0.5 gold sits at 50 minutes (3000 s).
    assert plan.cost[1] == pytest.approx(0.5, abs=0.02)
    assert plan.time.total_seconds() == pytest.approx(3000, abs=120)


def test_spread_time_axis_tracks_linear_targets():
    plans = frontier(
        [FARM_ITEM, FISH_ITEM], {"It": 100}, prices=GOLD, count=5,
        spread=SpreadSpec("time", 0.0, 6000.0),
    )
    assert [p.time for p in plans] == sorted(p.time for p in plans)  # fastest-first
    keys = [(round(p.time.total_seconds()), round(p.gold - p.surplus_gold)) for p in plans]
    assert len(keys) == len(set(keys))  # deduped
    # "time" pins the cap directly, so each linear target lands almost exactly.
    for target in (0, 1500, 3000, 4500, 6000):
        assert any(abs(p.time.total_seconds() - target) < 1 for p in plans), target


def test_spread_gold_axis_tracks_linear_targets():
    plans = frontier(
        [FARM_ITEM, FISH_ITEM], {"It": 100}, prices=GOLD, count=5,
        spread=SpreadSpec("gold", 0.0, 1.0),
    )
    assert [p.time for p in plans] == sorted(p.time for p in plans)
    golds = [p.cost[1] for p in plans]
    for target in (0.0, 0.25, 0.5, 0.75, 1.0):
        assert any(abs(g - target) < 0.02 for g in golds), (target, golds)


def test_spread_pumpkin_juice_axis_tracks_linear_targets():
    # The flagship case from the design doc: 50 demand, 10/juice -> feasible max 5.
    plans = frontier(
        [FARM_ITEM], {"It": 50}, prices=GOLD, pj_gain={"it": 10}, pj_price=5,
        count=6, spread=SpreadSpec("pumpkin-juice", 0, 5),
    )
    assert [p.time for p in plans] == sorted(p.time for p in plans)
    counts = sorted(
        {round(sum(p.pumpkin_juices.values())) for p in plans}, reverse=True
    )
    assert counts == [5, 4, 3, 2, 1, 0]


def test_spread_pumpkin_juice_clamps_above_feasible_max():
    # Feasible max is 5 (50 demand / 10 per juice); asking for up to 100 must
    # clamp rather than crash, and never invent a count past what is reachable.
    plans = frontier(
        [FARM_ITEM], {"It": 50}, prices=GOLD, pj_gain={"it": 10}, pj_price=5,
        count=6, spread=SpreadSpec("pumpkin-juice", 0, 100),
    )
    counts = {round(sum(p.pumpkin_juices.values())) for p in plans}
    assert max(counts) == 5
    assert len(counts) <= 6  # feasible (5) + 1


def test_spread_passive_axis_yields_one_plan_per_breakpoint():
    # Horn finishes passively at 100/10 = 10 min (600 s); Oak, the slower item,
    # only at 100/1 = 100 min (6000 s, the cheap endpoint itself).
    offers = [
        Offer("passive", "passive", {"oak": 1.0, "horn": 10.0}, time_per_unit=timedelta(minutes=1)),
        Offer("fish Oak", "fishing", {"oak": 1}, consumes={"large net": 1}, ln=1),
        Offer("fish Horn", "fishing", {"horn": 1}, consumes={"large net": 1}, ln=1),
    ]
    plans = frontier(
        offers, {"Oak": 100, "Horn": 100}, prices=GOLD, count=5,
        spread=SpreadSpec("passive"),
    )
    times = {round(p.time.total_seconds()) for p in plans}
    assert 0 in times  # fast endpoint: buy everything, instant
    assert 600 in times  # Horn's breakpoint
    assert max(times) >= 6000  # Oak's breakpoint / the cheap endpoint


def test_spread_is_deduped_and_fastest_first():
    offers = [
        Offer("grow It", "farming", {"it": 1}, time_per_unit=timedelta(minutes=1)),
        Offer("fish A", "fishing", {"it": 1}, consumes={"large net": 1}, ln=1),
    ]
    plans = frontier(offers, {"It": 1000}, count=5, prices=GOLD, spread=SpreadSpec("time"))
    assert [p.time for p in plans] == sorted(p.time for p in plans)
    keys = [(round(p.time.total_seconds()), round(p.gold - p.surplus_gold)) for p in plans]
    assert len(keys) == len(set(keys))
    # No shown plan is dominated by another (a sample landing on a slower-but-
    # costlier point, or an endpoint/target that coincide) -- the spread prunes
    # like the default frontier so the per-row efficiency stays meaningful.
    assert not any(
        optimizer._dominates(a, b)
        for a in plans for b in plans if a is not b
    )


def test_frontier_spread_none_matches_default_behaviour():
    # The regression contract: an explicit spread=None is byte-identical to
    # omitting the argument entirely.
    baseline = frontier([FARM_ITEM, FISH_ITEM], {"It": 100}, prices=GOLD, count=5)
    explicit = frontier(
        [FARM_ITEM, FISH_ITEM], {"It": 100}, prices=GOLD, count=5, spread=None
    )
    assert len(baseline) == len(explicit)
    assert [p.cost for p in baseline] == [p.cost for p in explicit]
    assert baseline[0].cost == explicit[0].cost
    assert baseline[-1].cost == explicit[-1].cost


def test_efficiency_is_positive_when_faster_costs_more_and_none_for_the_last_row():
    plans = [
        SourcingPlan(time=timedelta(seconds=0), gold=10.0),
        SourcingPlan(time=timedelta(seconds=100), gold=4.0),
        SourcingPlan(time=timedelta(seconds=300), gold=0.0),
    ]
    effs = efficiency(plans)
    assert effs[0] == pytest.approx((10.0 - 4.0) / (100 - 0))
    assert effs[1] == pytest.approx((4.0 - 0.0) / (300 - 100))
    assert effs[2] is None  # no slower neighbour


def test_efficiency_guards_equal_time():
    plans = [
        SourcingPlan(time=timedelta(seconds=0), gold=5.0),
        SourcingPlan(time=timedelta(seconds=0), gold=2.0),
    ]
    assert efficiency(plans) == [None, None]


def _graph_fixture() -> list[SourcingPlan]:
    return [
        SourcingPlan(time=timedelta(seconds=0), gold=10.0, pumpkin_juices={"it": 3}),
        SourcingPlan(time=timedelta(seconds=50), gold=5.0, pumpkin_juices={"it": 1}),
        SourcingPlan(time=timedelta(seconds=100), gold=0.0, pumpkin_juices={"it": 0}),
    ]


def test_spread_graph_series_auto_picks_gold_for_a_non_gold_axis():
    plans = _graph_fixture()
    series = spread_graph_series(plans, "time", "auto")
    assert isinstance(series, GraphSeries)
    assert series.x_label == "Time"
    assert series.y_label == "Net gold"
    assert series.xs == [p.time.total_seconds() for p in plans]
    assert series.ys == [p.cost[1] for p in plans]
    assert len(series.xs) == len(series.ys) == len(plans)


def test_spread_graph_series_auto_picks_time_for_the_gold_axis():
    plans = _graph_fixture()
    series = spread_graph_series(plans, "gold", "auto")
    assert series.x_label == "Net gold"
    assert series.y_label == "Time"
    assert series.xs == [p.cost[1] for p in plans]
    assert series.ys == [p.time.total_seconds() for p in plans]


def test_spread_graph_series_passive_axis_uses_time_as_x():
    plans = _graph_fixture()
    series = spread_graph_series(plans, "passive", "gold")
    assert series.x_label == "Time"
    assert series.xs == [p.time.total_seconds() for p in plans]
    assert series.ys == [p.cost[1] for p in plans]


def test_spread_graph_series_explicit_metric_overrides_auto():
    plans = _graph_fixture()
    series = spread_graph_series(plans, "pumpkin-juice", "time")
    assert series.x_label == "Pumpkin Juice"
    assert series.y_label == "Time"
    assert series.xs == [round(sum(p.pumpkin_juices.values())) for p in plans]
    assert series.ys == [p.time.total_seconds() for p in plans]
