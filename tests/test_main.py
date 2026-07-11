from datetime import timedelta

from crafting import Workshop, WorkshopRecipe
from crops import Farm
import pytest

import argparse

from farm_solver import (
    GameState,
    Inventory,
    InventoryItem,
    ItemNeed,
    Mastery,
    Solution,
    TowerRequirements,
)
from main import (
    DEWSTAR_TOWER_LEVEL,
    MONTHLY_GRANT_TOWER_LEVEL,
    _should_model_calendar,
    resolve_calendar_mode,
    strip_dated_sources,
    apply_inventory_overrides,
    build_parser,
    cheapest_plan,
    cookie_sets_available,
    limit_shortfall,
    parse_gold_budget,
    parse_target,
    pumpkin_juice_gains,
    pumpkin_juice_schedule,
    resolve_dewstars_journal,
    resolve_fungus_processing,
    resolve_monthly_grants,
    resolve_pumpkin_juice,
    resolve_scythe_of_dewstar,
    selected_plan,
    within_limits,
)
from report import (
    SectionedReporter,
    _frontier_reasons,
    _slowest_passive_item,
    print_crafts,
    print_critical_path,
    print_frontier,
    print_plan,
)
from optimizer import SourcingPlan
from passive import MINUTE, PassiveProducer, PassiveProduction
from sources import Draw, Offer, OpenFlow

PLANS = [
    SourcingPlan(time=timedelta(0), arnold_palmers=100, large_nets=20, gold=12),
    SourcingPlan(time=timedelta(hours=5), arnold_palmers=10, large_nets=0, gold=5),
    SourcingPlan(time=timedelta(days=3), arnold_palmers=0, large_nets=0, gold=0),
]


def test_cheapest_plan_picks_the_least_gold():
    # The last plan buys nothing (gold 0), so it is cheapest despite being slowest.
    assert cheapest_plan(PLANS) == 2


def test_cheapest_plan_breaks_ties_on_time():
    plans = [
        SourcingPlan(time=timedelta(hours=2), arnold_palmers=0, large_nets=0),
        SourcingPlan(time=timedelta(hours=1), arnold_palmers=0, large_nets=0),
    ]
    assert cheapest_plan(plans) == 1


def test_cheapest_plan_ranks_by_net_gold_crediting_banked_surplus():
    # Plan 0 buys 5 and banks nothing (net 5); plan 1 buys 8 but banks 20 of surplus
    # (net -12), so on net gold it is the cheapest despite buying more.
    plans = [
        SourcingPlan(time=timedelta(hours=1), gold=5, surplus_gold=0),
        SourcingPlan(time=timedelta(hours=3), gold=8, surplus_gold=20),
    ]
    assert cheapest_plan(plans) == 1


def test_limit_shortfall_reports_a_too_tight_time_limit():
    # Every plan is 3d+ here, so a 1d cap can never be met -- say so, naming both.
    plans = [
        SourcingPlan(time=timedelta(days=3), arnold_palmers=0, large_nets=0),
        SourcingPlan(time=timedelta(days=5), arnold_palmers=0, large_nets=0),
    ]
    message = limit_shortfall(plans, timedelta(days=1), None, None)
    assert "under 1d" in message
    assert "fastest is 3d" in message
    assert "showing the full frontier" in message


def test_limit_shortfall_reports_too_tight_consumable_limits():
    # Only the 100-AP plan, so a 5 AP cap is unmeetable; report the axis and best.
    message = limit_shortfall(PLANS[:1], None, 5, None)
    assert "within 5 AP (fewest is 100)" in message


def test_limit_shortfall_names_joint_infeasibility():
    # Each limit is reachable alone (a 5h plan, a 0-AP plan) but no single plan
    # meets both, so no axis is individually to blame.
    plans = [
        SourcingPlan(time=timedelta(hours=1), arnold_palmers=100, large_nets=0),
        SourcingPlan(time=timedelta(hours=9), arnold_palmers=0, large_nets=0),
    ]
    message = limit_shortfall(plans, timedelta(hours=5), 5, None)
    assert "every limit at once" in message


def test_average_calendar_flag_defaults_off():
    parser = build_parser()
    assert parser.parse_args([]).average_calendar is False
    assert parser.parse_args(["--average-calendar"]).average_calendar is True


def test_should_model_calendar_full_mode_with_a_seasonal_pet():
    # In the full mode with a seasonal pet, the calendar is modelled.
    assert _should_model_calendar({"Wolf": 8}, "full")


def test_average_and_ignore_modes_never_model_the_calendar():
    assert not _should_model_calendar({"Wolf": 8}, "average")
    assert not _should_model_calendar({"Wolf": 8}, "ignore")


def test_should_model_calendar_needs_an_actual_seasonal_pet():
    # The full mode with only year-round pets (and no grants) has nothing to model.
    assert not _should_model_calendar({"Dog": 8}, "full")


def test_calendar_flags_default_off():
    # The flags' own defaults are off; the resolved mode ('full') comes from
    # resolve_calendar_mode's fallback when neither flag nor config say otherwise.
    parser = build_parser()
    args = parser.parse_args([])
    assert (args.model_calendar, args.average_calendar, args.ignore_calendar) == (
        False, False, False,
    )
    assert parser.parse_args(["--model-calendar"]).model_calendar is True
    assert parser.parse_args(["--ignore-calendar"]).ignore_calendar is True


def test_calendar_flags_are_mutually_exclusive():
    parser = build_parser()
    for pair in (
        ["--model-calendar", "--average-calendar"],
        ["--model-calendar", "--ignore-calendar"],
        ["--average-calendar", "--ignore-calendar"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(pair)


def test_resolve_calendar_mode_flags_beat_the_config():
    parser = build_parser()
    cfg = {"solver": {"calendar": "average"}}
    assert resolve_calendar_mode(parser.parse_args(["--model-calendar"]), cfg) == "full"
    assert (
        resolve_calendar_mode(parser.parse_args(["--ignore-calendar"]), cfg)
        == "ignore"
    )
    assert resolve_calendar_mode(parser.parse_args([]), cfg) == "average"


def test_resolve_calendar_mode_defaults_to_full():
    assert resolve_calendar_mode(build_parser().parse_args([]), {}) == "full"


def test_resolve_calendar_mode_rejects_a_bad_config_value():
    args = build_parser().parse_args([])
    with pytest.raises(ValueError, match="solver.calendar must be one of"):
        resolve_calendar_mode(args, {"solver": {"calendar": "sometimes"}})


def test_resolve_calendar_mode_rejects_the_old_boolean_key():
    args = build_parser().parse_args([])
    with pytest.raises(ValueError, match="solver.model_calendar is gone"):
        resolve_calendar_mode(args, {"solver": {"model_calendar": True}})


def test_strip_dated_sources_drops_only_seasonal_pets():
    cfg = {
        "pets": {"Wolf": 8, "Dog": 8, "Polar Bear": 6},
        "sawmill": {"oak": 10},
    }
    stripped = strip_dated_sources(cfg)
    assert stripped["pets"] == {"Dog": 8}
    assert stripped["sawmill"] == {"oak": 10}
    # The original config is untouched (a copy is returned).
    assert cfg["pets"] == {"Wolf": 8, "Dog": 8, "Polar Bear": 6}


def test_monthly_grants_are_a_dated_source_even_without_a_seasonal_pet():
    # The Mechanical Heart's grants are date-dependent, so they alone justify the
    # calendar model even when every pet is year-round.
    assert _should_model_calendar({"Dog": 8}, "full", monthly_grants=True)
    # ...but not in the average or ignore modes.
    assert not _should_model_calendar({"Dog": 8}, "average", monthly_grants=True)
    assert not _should_model_calendar({"Dog": 8}, "ignore", monthly_grants=True)


def test_selected_plan_defaults_to_the_fastest():
    # No --plan and no --cheapest-plan: the fastest (plan 0, sorted first) wins,
    # even though PLANS[2] is the cheapest.
    assert selected_plan(PLANS, plan=None, cheapest=False) == 0


def test_selected_plan_honours_cheapest():
    assert selected_plan(PLANS, plan=None, cheapest=True) == 2


def test_selected_plan_honours_explicit_number():
    assert selected_plan(PLANS, plan=2, cheapest=False) == 1


def test_selected_plan_rejects_out_of_range_number():
    with pytest.raises(ValueError):
        selected_plan(PLANS, plan=4, cheapest=False)


def test_within_limits_checks_time_and_consumables():
    plan = SourcingPlan(time=timedelta(days=2), arnold_palmers=500, large_nets=50)
    assert within_limits(plan)  # no limits
    assert within_limits(plan, time_limit=timedelta(days=3), max_ap=500, max_ln=50)
    assert not within_limits(plan, time_limit=timedelta(days=1))  # too slow
    assert not within_limits(plan, max_ap=499)  # too many palmers
    assert not within_limits(plan, max_ln=49)  # too many nets


def test_within_limits_checks_the_gold_ceiling():
    plan = SourcingPlan(time=timedelta(days=2), gold=5000)
    assert within_limits(plan, max_gold=5000)
    assert not within_limits(plan, max_gold=4999)
    assert within_limits(plan, max_gold=None)  # unlimited


def test_parse_gold_budget_maps_the_named_aliases():
    assert parse_gold_budget("inventory") == 0  # self-source everything
    assert parse_gold_budget("unlimited") is None  # no cap
    assert parse_gold_budget("5000") == 5000
    with pytest.raises(argparse.ArgumentTypeError):
        parse_gold_budget("-1")
    with pytest.raises(argparse.ArgumentTypeError):
        parse_gold_budget("lots")


def test_parse_target_accepts_tower_case_insensitively():
    assert parse_target("tower") == "tower"
    assert parse_target("Tower") == "tower"


def test_parse_target_accepts_a_bare_level_number():
    assert parse_target("150") == 150


def test_parse_target_accepts_a_t_prefixed_level():
    assert parse_target("T150") == 150
    assert parse_target("t150") == 150


def test_parse_target_rejects_garbage():
    with pytest.raises(argparse.ArgumentTypeError):
        parse_target("banana")


def test_parse_target_rejects_non_positive_levels():
    with pytest.raises(argparse.ArgumentTypeError):
        parse_target("0")
    with pytest.raises(argparse.ArgumentTypeError):
        parse_target("-5")


def test_target_defaults_to_none_so_bare_dash_m_ignores_the_tower():
    parser = build_parser()
    assert parser.parse_args([]).target is None
    assert parser.parse_args(["--target", "T150"]).target == 150
    assert parser.parse_args(["--target", "tower"]).target == "tower"


def test_mastery_and_items_flags_can_be_combined():
    # Previously mutually exclusive; -m and -i must now both be settable at
    # once (each contributes its own goals, see farm_solver.solve_goals).
    parser = build_parser()
    args = parser.parse_args(["-m", "Board:5000", "-i", "Board:10000"])
    assert args.mastery == "Board:5000"
    assert args.items == "Board:10000"


def test_override_inventory_flag_defaults_to_none():
    parser = build_parser()
    assert parser.parse_args([]).override_inventory is None
    args = parser.parse_args(
        ["--override-inventory", "Medium Chest:0,Arnold Palmer:100"]
    )
    assert args.override_inventory == "Medium Chest:0,Arnold Palmer:100"


def test_apply_inventory_overrides_replaces_held_amounts():
    inventory = Inventory(
        items={
            "medium chest": InventoryItem(name="Medium Chest", quantity=5, category="Chests"),
        }
    )
    apply_inventory_overrides(
        inventory, {"Medium Chest": 0, "Arnold Palmer": 100}
    )
    assert inventory.amount("Medium Chest") == 0
    assert inventory.amount("Arnold Palmer") == 100
    # An overridden item that already had a category keeps it.
    assert inventory.get("Medium Chest").category == "Chests"
    # A brand-new item (not in the file at all) has no category to keep.
    assert inventory.get("Arnold Palmer").category is None


def test_print_frontier_tags_fastest_selected_and_cheapest(capsys):
    print_frontier(PLANS, selected=2, cheapest=2)
    lines = [line.rstrip() for line in capsys.readouterr().out.splitlines()]
    assert "   1. 0m + 100 AP + 20 LN  [fastest]" in lines
    assert " > 3. 3d + 0 AP + 0 LN  [cheapest]" in lines


def test_frontier_reasons_name_the_full_requirement_resource():
    # Steel Wire is at its full 100 (max_passive_residuals) and finishes at the
    # 10-min makespan, so the plan's reason is that it gathers all of it passively.
    passive_offer = Offer(
        "passive", "passive", {"steel wire": 10.0}, time_per_unit=timedelta(minutes=1)
    )
    plan = SourcingPlan(draws=[Draw(passive_offer, 10)], time=timedelta(minutes=10))
    reasons = _frontier_reasons(
        [plan], {"steel wire": 100}, {}, 1.0, [], {},
        max_passive_residuals={"steel wire": 100}, production=None,
        display={"steel wire": "Steel Wire"},
    )
    assert reasons == ["all Steel Wire from passive"]


def test_print_frontier_shows_per_plan_reasons(capsys):
    # A resource plan's reason rides alongside its number (and any fastest/cheapest
    # tag); a plan with no reason shows nothing extra.
    print_frontier(
        PLANS, selected=0, cheapest=2,
        reasons=[None, "all Oak from passive", None],
    )
    out = capsys.readouterr().out
    assert "2. 5h + 10 AP + 0 LN  — all Oak from passive" in out
    assert "1. 0m + 100 AP + 20 LN  [fastest]" in out


def test_print_frontier_omits_cookie_and_grape_juice_columns_when_unused(capsys):
    # PLANS spend no cookies or Grape Juices, so the table stays plain.
    print_frontier(PLANS, selected=0, cheapest=2)
    out = capsys.readouterr().out
    assert "Sourcing plans (time + Arnold Palmers + Large Nets):" in out
    assert "cookie sets" not in out and "Grape Juice" not in out


def test_print_frontier_shows_cookie_sets_and_grape_juice(capsys):
    cookie_draw = Draw(
        Offer("grow Beet", "farming", {"beet": 1}, cookies=1, time_per_unit=timedelta(days=1)),
        5,
    )
    plan = SourcingPlan(
        draws=[cookie_draw], time=timedelta(days=1), cookie_sets=5, grape_juices=14
    )
    print_frontier([plan], selected=0, cheapest=0)
    out = capsys.readouterr().out
    assert "time + Arnold Palmers + Large Nets + cookie sets + Grape Juice" in out
    assert "5 sets" in out
    assert "14 GJ" in out


def test_print_frontier_rounds_partial_cookie_sets_up(capsys):
    # A partial cookie set still consumes a whole one, so 11.4 sets reads as 12.
    cookie_draw = Draw(
        Offer("grow Beet", "farming", {"beet": 1}, cookies=1, time_per_unit=timedelta(days=1)),
        11.4,
    )
    plan = SourcingPlan(draws=[cookie_draw], time=timedelta(days=1), cookie_sets=11.4)
    print_frontier([plan], selected=0, cheapest=0)
    out = capsys.readouterr().out
    assert "12 sets" in out


def test_print_plan_renders_each_channel_and_the_remainder(capsys):
    demand = {"Drum": 500, "Bone": 100, "Corn": 1000, "Board": 2000}
    plan = SourcingPlan(
        draws=[
            Draw(Offer("fish Small Pond", "fishing", {"drum": 100}, ln=1), 5),
            Draw(Offer("explore Cave", "exploring", {"bone": 50}, ap=1), 2),
            Draw(
                Offer("grow Corn", "farming", {"corn": 40}, time_per_unit=timedelta(hours=1)),
                25,
            ),
            Draw(
                Offer("passive", "passive", {"board": 10}, time_per_unit=timedelta(minutes=1)),
                200,
            ),
        ],
        remainder={"Magicite": 3},
    )
    print_plan(plan, demand, Farm(grape_juices_per_day=14), Workshop(), 1.45)
    out = capsys.readouterr().out

    assert "Fishing:" in out
    assert "5x Large Net at Small Pond (Drum)" in out
    assert "Exploring:" in out
    assert "2x Arnold Palmer at Cave (100x Bone)" in out
    assert "Farming (1d 1h):" in out  # 25 Harvest Alls * 1h, grown one at a time
    assert "Harvest Alls of Corn" in out
    # Board at 10/min needs 200 min = 3h 20m, shown per item under passive.
    assert "Passive production (concurrent):" in out
    assert "2,000x Board in 3h 20m" in out
    # Items with no offer to make them are listed as a remainder.
    assert "3x Magicite" in out


def test_print_plan_critical_path_counts_marginal_passive_as_fill(capsys):
    # Farming caps at 100h; passive overshoots to 101h (~1%) making the last Beets.
    # That sliver is fill, so farming stays the named critical path.
    demand = {"Beet": 201}
    farming = Offer("grow Beet", "farming", {"beet": 1}, time_per_unit=timedelta(hours=1))
    passive_offer = Offer("passive", "passive", {"beet": 1}, time_per_unit=timedelta(hours=1))
    plan = SourcingPlan(draws=[Draw(farming, 100), Draw(passive_offer, 101)])
    print_plan(plan, demand, Farm(), Workshop(), 1.0)
    out = capsys.readouterr().out
    critical = out.split("Critical path")[1].splitlines()[1]
    assert "Harvest Alls of Beet" in critical
    assert "passive" not in critical


def test_print_plan_highlights_the_passive_bottleneck(capsys):
    # Passive makes 1 Slow and 10 Fast a minute; 100 of each needs 100 minutes for
    # Slow but only 10 for Fast, so Slow is the critical path that caps the total.
    demand = {"Slow": 100, "Fast": 100}
    offer = Offer(
        "passive", "passive", {"slow": 1, "fast": 10}, time_per_unit=timedelta(minutes=1)
    )
    plan = SourcingPlan(draws=[Draw(offer, 100)])
    print_plan(plan, demand, Farm(), Workshop(), 1.0)
    out = capsys.readouterr().out
    assert "Critical path (sets the 1h 40m total):" in out
    assert "100x Slow from passive production (1h 40m)" in out
    assert "Fast" not in out.split("Critical path")[1].split("Passive")[0]


def test_slowest_passive_item_skips_pet_topups():
    # A sawmill makes Oak; a slow pet makes Horn. Horn is mostly sourced elsewhere
    # (e.g. exploring), so the pet only tops up a small residual. Even though that
    # residual is marginally slower to trickle out, Oak from the building is the
    # real bottleneck, so the pet top-up is not named as the critical path.
    production = PassiveProduction(
        producers=[
            PassiveProducer("sawmill", MINUTE, {"oak": 100.0}),
            PassiveProducer("pet Dog", MINUTE, {"horn": 1.0}),
        ]
    )
    offer = Offer("passive", "passive", {"oak": 100.0, "horn": 1.0}, time_per_unit=MINUTE)
    plan = SourcingPlan(draws=[Draw(offer, 1)])
    need = {"oak": 1000.0, "horn": 1000.0}
    elsewhere = {"horn": 900.0}  # exploring supplies most Horn; 100 left to the pet
    display = {"oak": "Oak", "horn": "Horn"}
    # Oak wait 1000/100 = 10 min; Horn residual 100/1 = 100 min (slower) but a top-up.
    item = _slowest_passive_item(plan, need, elsewhere, display, production)
    assert item is not None and item[0] == "Oak"


def test_slowest_passive_item_keeps_pet_when_it_is_primary_source():
    # When a pet is the only (hence primary) source of an item, it legitimately
    # caps the plan and is still named.
    production = PassiveProduction(
        producers=[PassiveProducer("pet Dog", MINUTE, {"horn": 1.0})]
    )
    offer = Offer("passive", "passive", {"horn": 1.0}, time_per_unit=MINUTE)
    plan = SourcingPlan(draws=[Draw(offer, 1)])
    item = _slowest_passive_item(plan, {"horn": 100.0}, {}, {"horn": "Horn"}, production)
    assert item is not None and item[0] == "Horn"


def test_print_plan_passive_section_leads_with_building_over_pet_topup(capsys):
    # Oak (sawmill) takes 10 min; Horn's pet residual takes 100 min but is a top-up
    # of an item exploring mostly supplies. The critical path names Oak, so the
    # passive section leads with Oak too — not the raw-slowest pet Horn.
    demand = {"Oak": 1000, "Horn": 1000}
    production = PassiveProduction(
        producers=[
            PassiveProducer("sawmill", MINUTE, {"oak": 100.0}),
            PassiveProducer("pet Dog", MINUTE, {"horn": 1.0}),
        ]
    )
    passive_offer = Offer(
        "passive", "passive", {"oak": 100.0, "horn": 1.0}, time_per_unit=MINUTE
    )
    explore = Offer("explore Cave", "exploring", {"horn": 900}, ap=1)
    plan = SourcingPlan(draws=[Draw(passive_offer, 1), Draw(explore, 1)])
    print_plan(plan, demand, Farm(), Workshop(), 1.0, production=production)
    tail = capsys.readouterr().out.split("Passive production (concurrent):")[1]
    lines = [line for line in tail.splitlines() if line.strip()]
    assert "Oak" in lines[0] and "Horn" in lines[1]


def test_print_plan_lists_critical_passive_item_first(capsys):
    # Passive makes 1 Slow and 100 Big a minute. 50 Slow needs 50 min (the critical
    # path); 1000 Big needs only 10 min. Big has more items, but Slow is on the
    # critical path, so Slow is listed first, then the rest by quantity.
    demand = {"Slow": 50, "Big": 1000}
    offer = Offer(
        "passive", "passive", {"slow": 1, "big": 100}, time_per_unit=timedelta(minutes=1)
    )
    plan = SourcingPlan(draws=[Draw(offer, 50)])
    print_plan(plan, demand, Farm(), Workshop(), 1.0)
    tail = capsys.readouterr().out.split("Passive production (concurrent):")[1]
    lines = [line for line in tail.splitlines() if line.strip()]
    assert "Slow" in lines[0] and "Big" in lines[1]


def test_print_plan_right_aligns_farming_yields(capsys):
    # Yield counts of different widths line up their "x" within the section.
    demand = {"Big": 1000, "Small": 5}
    plan = SourcingPlan(
        draws=[
            Draw(Offer("grow Big", "farming", {"big": 1000}, time_per_unit=timedelta(hours=1)), 1),
            Draw(Offer("grow Small", "farming", {"small": 5}, time_per_unit=timedelta(hours=2)), 1),
        ]
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.0)
    out = capsys.readouterr().out
    assert "    1,000x Big" in out
    assert "        5x Small" in out


def test_critical_path_names_the_resource_at_its_full_requirement(capsys):
    # Passive makes 10 Steel Wire and 10 Oak a minute, both finishing the plan's 100
    # each at 10 min. But Oak's full requirement is 1000 (it is reduced here, sped up
    # with consumables), while Steel Wire is at its full 100 -- so Steel Wire is the
    # resource whose complete gathering gates this makespan.
    passive = Offer(
        "passive", "passive", {"steel wire": 10.0, "oak": 10.0},
        time_per_unit=timedelta(minutes=1),
    )
    plan = SourcingPlan(draws=[Draw(passive, 10)])
    print_critical_path(
        plan, {"steel wire": 100, "oak": 100}, {},
        {"steel wire": "Steel Wire", "oak": "Oak"}, None, None,
        max_passive_residuals={"steel wire": 100, "oak": 1000},
    )
    out = capsys.readouterr().out
    assert "Steel Wire from passive production" in out
    assert "full requirement" in out
    assert "Oak" not in out


def test_critical_path_names_passive_when_the_farming_is_fill(capsys):
    # Farming grows Mushroom (which exploring could supply) to 10 min; passive makes
    # Oak (building-only) also to 10 min. The Mushroom farming is free fill of the
    # Oak-forced wait, so Oak is the driver, not the farming.
    farming = Offer("grow Mushroom", "farming", {"mushroom": 1}, time_per_unit=timedelta(minutes=1))
    passive = Offer("passive", "passive", {"oak": 1.0}, time_per_unit=timedelta(minutes=1))
    plan = SourcingPlan(draws=[Draw(farming, 10), Draw(passive, 10)])
    print_critical_path(
        plan, {"mushroom": 10, "oak": 10}, {},
        {"mushroom": "Mushroom", "oak": "Oak"}, None, sourceable_elsewhere={"mushroom"},
    )
    out = capsys.readouterr().out
    assert "Oak from passive production" in out
    assert "Mushroom" not in out


def test_critical_path_keeps_farming_when_the_crop_is_not_fill(capsys):
    # Pine cannot be sourced elsewhere, so even with Oak co-maximal the farming that
    # grows it is the genuine driver.
    farming = Offer("grow Pine Tree", "farming", {"pine tree": 1}, time_per_unit=timedelta(minutes=1))
    passive = Offer("passive", "passive", {"oak": 1.0}, time_per_unit=timedelta(minutes=1))
    plan = SourcingPlan(draws=[Draw(farming, 10), Draw(passive, 10)])
    print_critical_path(
        plan, {"pine tree": 10, "oak": 10}, {},
        {"pine tree": "Pine Tree", "oak": "Oak"}, None, sourceable_elsewhere={"mushroom"},
    )
    out = capsys.readouterr().out
    assert "Pine Tree" in out
    assert "passive production" not in out


def test_print_plan_highlights_the_farming_bottleneck(capsys):
    # A single crop grown over 100 hours is the whole makespan, so the critical
    # path names it and notes crops grow one at a time.
    demand = {"Corn": 100}
    offer = Offer("grow Corn", "farming", {"corn": 1}, time_per_unit=timedelta(hours=1))
    plan = SourcingPlan(draws=[Draw(offer, 100)])
    print_plan(plan, demand, Farm(), Workshop(), 1.0)
    out = capsys.readouterr().out
    assert "Critical path (sets the 4d 4h total):" in out
    assert "100 Harvest Alls of Corn (4d 4h) — crops grow one at a time" in out


def test_print_plan_critical_path_prefers_farming_over_passive_fill(capsys):
    # Farming forces a 100-minute wait. Passive runs concurrently and fills that
    # same window making a slow item that could have been sourced elsewhere, so its
    # track ties farming's 100 minutes -- but it is fill, not the constraint. The
    # critical path names farming alone, not the passive item.
    demand = {"Corn": 100, "Slow": 100}
    plan = SourcingPlan(
        draws=[
            Draw(Offer("grow Corn", "farming", {"corn": 1}, time_per_unit=timedelta(minutes=1)), 100),
            Draw(Offer("passive", "passive", {"slow": 1}, time_per_unit=timedelta(minutes=1)), 100),
        ],
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.0)
    out = capsys.readouterr().out
    assert "Critical path (sets the 1h 40m total):" in out
    critical = out.split("Critical path")[1].split("\n\n")[0]
    assert "Harvest Alls of Corn" in critical
    assert "Slow" not in critical  # the passive fill is not the critical path
    assert "passive production" not in critical


def test_print_plan_omits_critical_path_when_everything_is_instant(capsys):
    # Fishing is instant, so there is no time to explain -- no critical path line.
    demand = {"It": 100}
    offer = Offer("fish Lake", "fishing", {"it": 100}, ln=1)
    plan = SourcingPlan(draws=[Draw(offer, 1)])
    print_plan(plan, demand, Farm(), Workshop(), 1.0)
    out = capsys.readouterr().out
    assert "Critical path" not in out


def test_print_plan_farming_splits_produced_by_grape_juice(capsys):
    # The blended offer carries its Grape Juice share (half a juice a planting
    # here); over 10 plantings that is 5 juices, and both kinds yield the same
    # bundle, so of the 1,000 Corn, 500 come from Grape Juice and 500 from normal.
    demand = {"Corn": 1000}
    offer = Offer("grow Corn", "farming", {"corn": 100}, gj=0.5, time_per_unit=timedelta(hours=1))
    plan = SourcingPlan(draws=[Draw(offer, 10)])
    print_plan(plan, demand, Farm(grape_juices_per_day=12), Workshop(), 1.0)
    out = capsys.readouterr().out
    assert "10 Harvest Alls of Corn" in out
    assert "using 5 Grape Juice" in out
    assert "1,000x Corn (500 from Grape Juice, 500 from normal farming)" in out


def test_print_plan_farming_heading_sums_the_crop_times(capsys):
    # Crops grow one at a time, so the Farming heading shows their total wait:
    # 2h of Corn plus 3h of Wheat is 5h.
    demand = {"Corn": 100, "Wheat": 100}
    plan = SourcingPlan(
        draws=[
            Draw(Offer("grow Corn", "farming", {"corn": 50}, time_per_unit=timedelta(hours=1)), 2),
            Draw(Offer("grow Wheat", "farming", {"wheat": 100}, time_per_unit=timedelta(hours=1)), 3),
        ],
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.0)
    out = capsys.readouterr().out
    assert "Farming (5h):" in out


def test_print_plan_farming_omits_split_without_grape_juice(capsys):
    # With no grape juice every harvest is natural, so the produced count is shown
    # plainly with no breakdown.
    demand = {"Corn": 1000}
    offer = Offer("grow Corn", "farming", {"corn": 100}, time_per_unit=timedelta(hours=1))
    plan = SourcingPlan(draws=[Draw(offer, 10)])
    print_plan(plan, demand, Farm(grape_juices_per_day=0), Workshop(), 1.0)
    out = capsys.readouterr().out
    assert "1,000x Corn" in out
    assert "Grape Juice" not in out


def test_print_plan_orders_fishing_by_large_nets(capsys):
    # Two zones; the one using more Large Nets is listed first regardless of the
    # draw order, so the biggest consumable spend leads.
    demand = {"Cod": 100, "Eel": 100}
    plan = SourcingPlan(
        draws=[
            Draw(Offer("fish Small Pond", "fishing", {"cod": 100}, ln=1), 1),
            Draw(Offer("fish Big Lake", "fishing", {"eel": 10}, ln=1), 10),
        ],
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.0)
    lines = [line for line in capsys.readouterr().out.splitlines() if "Large Net at" in line]
    assert lines[0].strip().startswith("10x Large Net at Big Lake")
    assert lines[1].strip().startswith("1x Large Net at Small Pond")


def test_print_plan_orders_exploring_by_arnold_palmers(capsys):
    # The location using more Arnold Palmers is listed first.
    demand = {"Ore": 100, "Gem": 100}
    plan = SourcingPlan(
        draws=[
            Draw(Offer("explore Cave", "exploring", {"ore": 10}, ap=1), 10),
            Draw(Offer("explore Ridge", "exploring", {"gem": 2}, ap=1), 50),
        ],
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.0)
    lines = [line for line in capsys.readouterr().out.splitlines() if "Arnold Palmer at" in line]
    assert lines[0].strip().startswith("50x Arnold Palmer at Ridge")
    assert lines[1].strip().startswith("10x Arnold Palmer at Cave")


def test_print_plan_orders_farming_by_time(capsys):
    # Crops are listed longest-growing first, so the farming bottleneck leads.
    demand = {"Corn": 100, "Wheat": 100}
    plan = SourcingPlan(
        draws=[
            Draw(Offer("grow Corn", "farming", {"corn": 50}, time_per_unit=timedelta(hours=1)), 2),
            Draw(Offer("grow Wheat", "farming", {"wheat": 20}, time_per_unit=timedelta(hours=1)), 5),
        ],
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.0)
    lines = [
        line
        for line in capsys.readouterr().out.splitlines()
        if "Harvest All" in line and " in " in line  # the per-crop farming lines
    ]
    assert "Wheat" in lines[0]  # 5h of Wheat outlasts 2h of Corn
    assert "Corn" in lines[1]


def test_print_plan_farming_drops_residue_draws(capsys):
    # A sliver of a planting on a crop nothing needs rounds to zero produced in
    # zero time -- LP residue, not a real action -- so it is omitted entirely.
    demand = {"Corn": 100, "Mushroom": 5}
    plan = SourcingPlan(
        draws=[
            Draw(Offer("grow Corn", "farming", {"corn": 50}, time_per_unit=timedelta(hours=1)), 2),
            Draw(Offer("grow Mushroom", "farming", {"mushroom": 10}, time_per_unit=timedelta(hours=1)), 0.01),
        ],
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.0)
    out = capsys.readouterr().out
    assert "Harvest Alls of Corn" in out
    assert "Mushroom" not in out  # the zero-yield residue draw is not shown


def test_print_plan_fishing_drops_residue_draws(capsys):
    # A sliver of a net whose interesting yields all round to zero produced is LP
    # residue, not a real cast, so the whole line is omitted.
    demand = {"Frost Snapper Shell": 100, "Glass Bottle": 5}
    plan = SourcingPlan(
        draws=[
            Draw(Offer("fish Glacier Lake", "fishing", {"frost snapper shell": 100}, ln=1), 1),
            Draw(Offer("fish Crystal River", "fishing", {"glass bottle": 10}, ln=1), 0.01),
        ],
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.0)
    out = capsys.readouterr().out
    assert "Frost Snapper Shell" in out
    assert "Crystal River" not in out  # the zero-yield residue net is not shown


def test_print_plan_exploring_drops_residue_draws(capsys):
    # Same for a sliver of a palmer that gathers nothing that rounds up.
    demand = {"Moonstone": 100, "Glass Orb": 5}
    plan = SourcingPlan(
        draws=[
            Draw(Offer("explore Ember Lagoon", "exploring", {"moonstone": 100}, ap=1), 1),
            Draw(Offer("explore Crystal River", "exploring", {"glass orb": 10}, ap=1), 0.01),
        ],
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.0)
    out = capsys.readouterr().out
    assert "Moonstone" in out
    assert "Crystal River" not in out  # the zero-yield residue palmer is not shown


def test_print_plan_reports_opening_chests(capsys):
    # The plan fishes a chest and its key, then opens the chest for its contents.
    # The chest also drops Trinket, which nothing needs -- a byproduct, so it is
    # left out of the reported yields.
    demand = {"Gem": 50}
    plan = SourcingPlan(
        draws=[
            Draw(Offer("fish Vault", "fishing", {"locked chest": 1}, ln=1), 1),
            Draw(Offer("fish Keys", "fishing", {"brass key": 1}, ln=1), 1),
        ],
        opens={"Locked Chest": 1},
    )
    chest = OpenFlow(
        name="Locked Chest",
        consumes={"locked chest": 1, "brass key": 1},
        yields={"gem": 50, "trinket": 99},
    )
    names = {
        "locked chest": "Locked Chest",
        "brass key": "Brass Key",
        "gem": "Gem",
        "trinket": "Trinket",
    }
    print_plan(plan, demand, Farm(), Workshop(), 1.0, open_flows=[chest], openable_names=names)
    out = capsys.readouterr().out
    assert "Opening (Locksmith):" in out
    assert "1x Locked Chest using 1 Brass Key -> 50x Gem" in out
    assert "Trinket" not in out  # the unneeded byproduct is not listed
    # The chest and key show up as what the fishing casts were for.
    assert "Locked Chest" in out.split("Fishing:")[1]


def test_print_plan_reports_opens_that_restock_a_consumable_tool(capsys):
    # The plan spends Large Nets (a fishing *tool*, not a craft material) and opens
    # a chest that yields them. Large Net is never crafting `need`, but the open
    # still surfaces because it restocks a tool the plan burns.
    demand = {"Gem": 10}
    plan = SourcingPlan(
        draws=[Draw(Offer("fish Cove", "fishing", {"gem": 1}, ln=1), 10)],
        opens={"Net Chest": 4},
        large_nets=30,
    )
    chest = OpenFlow(
        name="Net Chest",
        consumes={"net chest": 1},
        yields={"large net": 3, "trinket": 9},
    )
    names = {"net chest": "Net Chest", "large net": "Large Net", "gem": "Gem"}
    print_plan(plan, demand, Farm(), Workshop(), 1.0, open_flows=[chest], openable_names=names)
    out = capsys.readouterr().out
    assert "4x Net Chest -> 12x Large Net" in out
    assert "Trinket" not in out  # a byproduct nothing needs, still hidden


def test_print_plan_reports_opens_that_gather_a_consumed_byproduct(capsys):
    # The plan eats Apple for stamina and opens a chest that yields it as one of
    # several drops. Apple is neither demand nor a craft material, so without
    # counting `draw_consumed` this open would look worthless and vanish entirely.
    demand = {"Gem": 10}
    eat_apple = Offer("eat apple", "drinking", {"stamina": 15}, consumes={"apple": 1})
    plan = SourcingPlan(
        draws=[Draw(Offer("fish Cove", "fishing", {"gem": 1}, ln=1), 10), Draw(eat_apple, 100)],
        opens={"Fruit Chest": 1},
    )
    chest = OpenFlow(
        name="Fruit Chest",
        consumes={"fruit chest": 1},
        yields={"apple": 100, "trinket": 9},
    )
    names = {"fruit chest": "Fruit Chest", "gem": "Gem"}
    print_plan(plan, demand, Farm(), Workshop(), 1.0, open_flows=[chest], openable_names=names)
    out = capsys.readouterr().out
    assert "Opening (Locksmith):" in out
    assert "1x Fruit Chest -> 100x apple" in out
    assert "Trinket" not in out  # still hidden -- nothing consumes it


def test_print_plan_lists_held_consumables_from_inventory(capsys):
    # A plan can spend far more of a tool than it makes because it held a stack.
    # The held portion (min of stock and spend) is surfaced so the spend reconciles.
    demand = {"Gem": 10}
    plan = SourcingPlan(
        draws=[Draw(Offer("fish Cove", "fishing", {"gem": 1}, ln=1), 10)],
        large_nets=1000,
    )
    inventory = {"large net": 712, "arnold palmer": 5}
    print_plan(
        plan, demand, Farm(), Workshop(), 1.0, inventory=inventory,
        openable_names={"large net": "Large Net", "arnold palmer": "Arnold Palmer"},
    )
    out = capsys.readouterr().out
    assert "From inventory (held consumables):" in out
    assert "712x Large Net" in out
    # Arnold Palmers are held but the plan spends none, so they are not listed.
    assert "Arnold Palmer" not in out


def test_print_plan_reduces_passive_residual_by_inventory(capsys):
    # 100 Widgets each consume a Gear that passive makes. With 40 Gears in stock,
    # passive need only build the 60 shortfall.
    demand = {"Widget": 100}
    workshop = Workshop(recipes={"widget": WorkshopRecipe("Widget", {"Gear": 1})})
    passive_offer = Offer(
        "passive", "passive", {"gear": 1}, time_per_unit=timedelta(minutes=1)
    )
    plan = SourcingPlan(draws=[Draw(passive_offer, 60)], crafts={"widget": 100})
    print_plan(plan, demand, Farm(), workshop, 1.0, inventory={"gear": 40})
    out = capsys.readouterr().out
    assert "60x Gear" in out  # 100 needed minus 40 already held


def test_cookie_sets_available_takes_the_scarcest_type():
    inv = {"happy cookies": 88, "spooky cookies": 78, "lovely cookies": 65}
    assert cookie_sets_available(inv, None) == 65


def test_cookie_sets_available_override_wins_in_either_direction():
    inv = {"happy cookies": 88, "spooky cookies": 78, "lovely cookies": 65}
    assert cookie_sets_available(inv, 200) == 200
    assert cookie_sets_available(inv, 0) == 0


def test_cookie_sets_available_is_zero_without_cookies():
    assert cookie_sets_available({}, None) == 0


def test_print_plan_reports_cookie_boosted_farming(capsys):
    demand = {"Slow": 420}
    offer = Offer(
        "grow Slow with cookies", "farming", {"slow": 42}, cookies=1, gj=2,
        time_per_unit=timedelta(days=1),
    )
    plan = SourcingPlan(draws=[Draw(offer, 10)], cookie_sets=10)
    print_plan(plan, demand, Farm(grape_juices_per_day=2), Workshop(), 1.0)
    out = capsys.readouterr().out
    assert "Farming (10d, 10 cookie sets):" in out
    assert "Cookie-boosted Slow in 10d" in out
    assert "420x Slow" in out
    assert "20 Grape Juice" in out  # 2 a day over 10 days


def test_print_plan_shares_cookie_days_between_crops(capsys):
    # Two crops spanning 10.16 + 0.33 = 10.49 cookie-days share the boundary day,
    # so the plan spends ceil(10.49) = 11 sets, not ceil(10.16) + ceil(0.33) = 12.
    demand = {"Big": 100, "Small": 100}
    big = Offer(
        "grow Big with cookies", "farming", {"big": 10}, cookies=1,
        time_per_unit=timedelta(days=1),
    )
    small = Offer(
        "grow Small with cookies", "farming", {"small": 300}, cookies=1,
        time_per_unit=timedelta(days=1),
    )
    plan = SourcingPlan(draws=[Draw(big, 10.16), Draw(small, 0.33)], cookie_sets=10.49)
    print_plan(plan, demand, Farm(), Workshop(), 1.0)
    out = capsys.readouterr().out
    assert "11 cookie sets" in out
    assert "12 cookie sets" not in out


def test_print_plan_drops_residue_opens(capsys):
    # A sliver of an open whose interesting yields all round to zero is LP residue.
    demand = {"Gem": 50}
    plan = SourcingPlan(
        draws=[Draw(Offer("fish Vault", "fishing", {"gem": 50}, ln=1), 1)],
        opens={"Junk Box": 0.001},
    )
    junk = OpenFlow(name="Junk Box", consumes={"junk box": 1}, yields={"gem": 50})
    print_plan(plan, demand, Farm(), Workshop(), 1.0, open_flows=[junk],
               openable_names={"junk box": "Junk Box", "gem": "Gem"})
    out = capsys.readouterr().out
    assert "Opening (Locksmith):" not in out


def test_print_plan_farming_singular_harvest_all(capsys):
    # A single full-field harvest is "1 Harvest All", not "1 Harvest Alls".
    demand = {"Corn": 50}
    offer = Offer("grow Corn", "farming", {"corn": 50}, time_per_unit=timedelta(hours=1))
    plan = SourcingPlan(draws=[Draw(offer, 1)])
    print_plan(plan, demand, Farm(), Workshop(), 1.0)
    out = capsys.readouterr().out
    assert "1 Harvest All of Corn" in out
    assert "Harvest Alls" not in out


def test_print_plan_passive_names_a_single_source(capsys):
    # One producer makes the item, so it is named plainly with "(from ...)".
    demand = {"Flower": 10}
    plan = SourcingPlan(
        draws=[Draw(Offer("passive", "passive", {"flower": 5}, time_per_unit=MINUTE), 2)]
    )
    production = PassiveProduction(
        producers=[PassiveProducer("Windmill", MINUTE, {"flower": 5})]
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.0, production)
    out = capsys.readouterr().out
    assert "10x Flower in 2m (from Windmill)" in out


def test_print_plan_passive_shows_a_consumed_tool_the_passive_track_makes(capsys):
    # Arnold Palmer is a consumed tool, never a crafting `need`, so it has no
    # need-minus-elsewhere residual -- but the passive track (e.g. the Mechanical
    # Heart's monthly grants) can still make it, free supply worth surfacing. Show
    # what passive produced, capped at what the plan burns, named by its producer.
    demand = {"Horn": 2000}
    explore = Offer(
        "explore Cave", "exploring", {"horn": 10},
        consumes={"arnold palmer": 1}, ap=1,
    )
    passive_offer = Offer(
        "passive", "passive", {"arnold palmer": 1}, time_per_unit=MINUTE
    )
    plan = SourcingPlan(draws=[Draw(explore, 200), Draw(passive_offer, 60)])
    production = PassiveProduction(
        producers=[PassiveProducer("Mechanical Heart", MINUTE, {"arnold palmer": 1})]
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.0, production,
               openable_names={"arnold palmer": "Arnold Palmer"})
    out = capsys.readouterr().out
    # Passive made 60 palmers (rate 1/min * 60 units), all of them burned by the
    # 200-palmer explore, so 60 show under the Mechanical Heart -- flagged
    # (smeared) since the monthly grant is spread to a daily rate, not a lump.
    assert "60x Arnold Palmer in 1h (from Mechanical Heart (smeared))" in out


def test_print_plan_passive_caps_a_consumed_tool_at_what_the_plan_burns(capsys):
    # If passive overproduces the tool past what the plan spends, only the used
    # portion shows (the rest is surplus, accounted separately).
    demand = {"Horn": 2000}
    explore = Offer(
        "explore Cave", "exploring", {"horn": 10},
        consumes={"arnold palmer": 1}, ap=1,
    )
    passive_offer = Offer(
        "passive", "passive", {"arnold palmer": 1}, time_per_unit=MINUTE
    )
    # Passive makes 500 palmers but the explore only burns 40.
    plan = SourcingPlan(draws=[Draw(explore, 40), Draw(passive_offer, 500)])
    production = PassiveProduction(
        producers=[PassiveProducer("Mechanical Heart", MINUTE, {"arnold palmer": 1})]
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.0, production)
    out = capsys.readouterr().out
    assert "40x Arnold Palmer" in out
    assert "500x Arnold Palmer" not in out


def test_print_plan_passive_breaks_down_multiple_sources(capsys):
    # Two producers make the item; the quantity is split by their rate share, on
    # the same line, largest contributor first.
    demand = {"Flower": 100}
    plan = SourcingPlan(
        draws=[Draw(Offer("passive", "passive", {"flower": 4}, time_per_unit=MINUTE), 25)]
    )
    production = PassiveProduction(
        producers=[
            PassiveProducer("Greenhouse", MINUTE, {"flower": 1}),
            PassiveProducer("Windmill", MINUTE, {"flower": 3}),
        ]
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.0, production)
    out = capsys.readouterr().out
    # 3-to-1 rate split of 100 is 75 from Windmill and 25 from Greenhouse.
    assert "100x Flower in 25m (75 from Windmill, 25 from Greenhouse)" in out


def test_print_plan_passive_splits_pets_by_rate(capsys):
    # Two pets return the same item at different hauls; the split follows their
    # rates (3-to-1 of 100 is 75/25), largest share first.
    demand = {"Flower": 100}
    plan = SourcingPlan(
        draws=[Draw(Offer("passive", "passive", {"flower": 4}, time_per_unit=MINUTE), 25)]
    )
    production = PassiveProduction(
        producers=[
            PassiveProducer("pet Fox", MINUTE, {"flower": 1}),
            PassiveProducer("pet Squirrel", MINUTE, {"flower": 3}),
        ]
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.0, production)
    out = capsys.readouterr().out
    assert "100x Flower in 25m (75 from pet Squirrel, 25 from pet Fox)" in out


def test_print_plan_passive_flags_seasonal_pets(capsys):
    # A seasonal pet fetches only part of the year, so its share is a yearly
    # average; the report flags the active window that explains the lower amount.
    demand = {"Flower": 10}
    plan = SourcingPlan(
        draws=[Draw(Offer("passive", "passive", {"flower": 5}, time_per_unit=MINUTE), 2)]
    )
    production = PassiveProduction(
        producers=[PassiveProducer("pet Hummingbird", MINUTE, {"flower": 5})]
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.0, production)
    out = capsys.readouterr().out
    assert "10x Flower in 2m (from pet Hummingbird [seasonal: Mar–Apr])" in out


def test_print_plan_passive_excludes_items_covered_elsewhere(capsys):
    demand = {"Horn": 1000}
    plan = SourcingPlan(
        draws=[
            # Exploring makes all 1000 Horn, so passive has nothing left to add.
            Draw(Offer("explore Canyon", "exploring", {"horn": 100}, ap=1), 10),
            Draw(
                Offer("passive", "passive", {"horn": 1}, time_per_unit=timedelta(minutes=1)),
                50,
            ),
        ],
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.45)
    out = capsys.readouterr().out
    assert "10x Arnold Palmer at Canyon (1,000x Horn)" in out  # Horn from exploring
    assert "Passive production" not in out  # nothing left for passive to cover


def test_print_plan_exploring_reports_stamina_negative_and_resource_counts(capsys):
    # A cider explore should show what it gathered (with counts, not just names)
    # and its stamina cost as a negative, since stamina goes down, not up.
    demand = {"Oak": 1000}
    cider = Offer(
        "cider Whispering Creek",
        "exploring",
        {"oak": 400},
        consumes={"apple cider": 1, "stamina": 1216},
    )
    plan = SourcingPlan(draws=[Draw(cider, 3)])
    print_plan(plan, demand, Farm(), Workshop(), 1.0)
    out = capsys.readouterr().out
    assert "Exploring (Apple Cider):" in out
    assert "3x Apple Cider at Whispering Creek (-3,648 stamina, 1,200x Oak)" in out


def test_print_plan_exploring_lists_byproducts_the_plan_goes_on_to_use(capsys):
    # The cider explore throws off Apple as a byproduct alongside the needed Oak;
    # Apple is not itself demanded or a craft material, but the plan eats it for
    # stamina, so it should still show up at the zone that produced it.
    demand = {"Oak": 1000}
    cider = Offer(
        "cider Whispering Creek",
        "exploring",
        {"oak": 400, "apple": 50},
        consumes={"apple cider": 1, "stamina": 1216},
    )
    eat_apple = Offer(
        "eat apple", "drinking", {"stamina": 15}, consumes={"apple": 1}
    )
    plan = SourcingPlan(draws=[Draw(cider, 3), Draw(eat_apple, 150)])
    print_plan(plan, demand, Farm(), Workshop(), 1.0)
    out = capsys.readouterr().out
    # No display-name mapping for a bare "apple" in this minimal setup (it isn't
    # demanded, crafted, or an openable), so it falls back to its raw lower-case key.
    assert (
        "3x Apple Cider at Whispering Creek"
        " (-3,648 stamina, 150x apple, 1,200x Oak)" in out
    )


def test_print_plan_passive_shows_only_the_residual(capsys):
    demand = {"Horn": 1000}
    plan = SourcingPlan(
        draws=[
            # Exploring covers 600; passive must make the remaining 400.
            Draw(Offer("explore Canyon", "exploring", {"horn": 100}, ap=1), 6),
            Draw(
                Offer("passive", "passive", {"horn": 1}, time_per_unit=timedelta(minutes=1)),
                400,
            ),
        ],
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.45)
    out = capsys.readouterr().out
    assert "Passive production (concurrent):" in out
    assert "400x Horn in 6h 40m" in out  # 400 at 1/min = 400 minutes


def test_print_crafts_lists_parents_before_children(capsys):
    workshop = Workshop(
        recipes={
            "hammer": WorkshopRecipe("Hammer", {"Board": 2, "Iron": 1}),
            "board": WorkshopRecipe("Board", {"Wood": 5}),
        }
    )
    plan = SourcingPlan(crafts={"board": 10, "hammer": 5})
    print_crafts(plan, workshop, crafting_bonus=1.0, mastery_bonus=1.0)
    out = capsys.readouterr().out
    assert "5x Hammer (Produces 5, Adds 5 Mastery)" in out
    assert "10x Board (Produces 10, Adds 10 Mastery)" in out
    assert "50x Wood" in out  # 10 Board crafts * 5 Wood
    assert "Iron" not in out  # auto-supplied, never shown
    assert out.index("Hammer") < out.index("Board")  # parent before child


def test_print_plan_orders_passive_items_longest_wait_first(capsys):
    demand = {"Slow": 100, "Fast": 100}
    offer = Offer("passive", "passive", {"slow": 1, "fast": 10}, time_per_unit=timedelta(minutes=1))
    plan = SourcingPlan(draws=[Draw(offer, 100)])
    print_plan(plan, demand, Farm(), Workshop(), 1.45)
    lines = [line.strip() for line in capsys.readouterr().out.splitlines() if "in " in line]
    # Slow (100 min) is a longer wait than Fast (10 min), so it is listed first.
    assert lines[0].endswith("Slow in 1h 40m")
    assert lines[1].endswith("Fast in 10m")


_GOLD = {
    "arnold palmer": 0.06, "large net": 0.01, "apple cider": 0.02,
    "orange juice": 0.008, "cookie set": 20.0, "grape juice": 0.0,
}


def test_cheapest_plan_ranks_by_gold_spent():
    # Plan 0 spends more gold buying, plan 1 less; cheapest is the least-gold plan
    # regardless of how many consumables each ends up using.
    plans = [
        SourcingPlan(time=timedelta(0), arnold_palmers=200, gold=12),
        SourcingPlan(time=timedelta(0), large_nets=500, gold=5),
    ]
    assert cheapest_plan(plans) == 1  # 5 g < 12 g


def test_print_frontier_shows_a_gold_column_when_priced(capsys):
    plans = [SourcingPlan(time=timedelta(0), arnold_palmers=1000, large_nets=0)]
    print_frontier(plans, 0, 0, None, _GOLD)
    out = capsys.readouterr().out
    assert "gold" in out
    assert "60 g" in out  # 1000 * 0.06


def test_print_frontier_omits_gold_column_without_prices(capsys):
    plans = [SourcingPlan(time=timedelta(0), arnold_palmers=1000, large_nets=0)]
    print_frontier(plans, 0, 0)
    out = capsys.readouterr().out
    assert "gold" not in out
    assert " g" not in out


def test_print_plan_reports_surplus_sale_value_when_grounded_and_priced(capsys):
    # Passive banks 1,000 Straw that nothing needs; each Large Net (priced) crafts
    # from 2 Straw, so the surplus is worth 500 nets. The grounded fishing draw (it
    # consumes a net, as in the inventory mode) makes the section coherent.
    workshop = Workshop({"large net": WorkshopRecipe("Large Net", {"Straw": 2})})
    passive_offer = Offer(
        "passive", "passive", {"straw": 10}, time_per_unit=timedelta(minutes=1)
    )
    grounded = Offer("fish Z", "fishing", {"cod": 1}, consumes={"large net": 1}, ln=1)
    plan = SourcingPlan(draws=[Draw(passive_offer, 100), Draw(grounded, 1)])
    print_plan(plan, {}, Farm(), workshop, 1.0, prices=_GOLD)
    out = capsys.readouterr().out
    assert "Surplus (accumulates past the goal)" in out
    assert "500x Large Net (0 inventory + 500 accumulated) = 5 g" in out
    assert "nominal value = 5 g (0 g inventory + 5 g accumulated)" in out

    print_plan(plan, {}, Farm(), workshop, 1.0)  # no prices -> no surplus section
    assert "Surplus" not in capsys.readouterr().out


def test_print_plan_splits_surplus_into_inventory_and_accumulated(capsys):
    # 300 Large Net already held (none of it consumed) plus 1,000 surplus Straw
    # the plan crafts into 500 more -- the 800 total should read as 300 held +
    # 500 made, and the nominal value the same way in gold.
    workshop = Workshop({"large net": WorkshopRecipe("Large Net", {"Straw": 2})})
    passive_offer = Offer(
        "passive", "passive", {"straw": 10}, time_per_unit=timedelta(minutes=1)
    )
    plan = SourcingPlan(draws=[Draw(passive_offer, 100)])
    print_plan(
        plan,
        {},
        Farm(),
        workshop,
        1.0,
        prices=_GOLD,
        inventory={"large net": 300.0},
    )
    out = capsys.readouterr().out
    assert "800x Large Net (300 inventory + 500 accumulated) = 8 g" in out
    assert "nominal value = 8 g (3 g inventory + 5 g accumulated)" in out


def test_print_frontier_splits_gold_into_buy_and_self_sourced(capsys):
    # With per-plan (to_buy, self_sourced) gold the row shows both subtotals and
    # their total, and the header flags the split.
    plans = [SourcingPlan(time=timedelta(0), arnold_palmers=1000, large_nets=0)]
    print_frontier(plans, 0, 0, None, _GOLD, golds=[(48.0, 12.0)])
    out = capsys.readouterr().out
    assert "gold, as buy + self-sourced" in out
    assert "-> 48 + 12 = 60 g" in out


def test_print_frontier_shows_banked_surplus_and_net_gold(capsys):
    # A plan that banks surplus adds "(bank B, net N)"; net is buy - bank, here below
    # zero (buys 12, banks 50 -> net -38), so the row shows the frontier past 0 g.
    plan = SourcingPlan(
        time=timedelta(0), arnold_palmers=1000, gold=12.0, surplus_gold=50.0
    )
    print_frontier([plan], 0, 0, None, _GOLD, golds=[(12.0, 48.0)])
    out = capsys.readouterr().out
    assert "-> 12 + 48 = 60 g (bank 50, net -38)" in out


# --- Pumpkin Juice ---


def test_use_pumpkin_juice_flag_parses_bare_and_capped_forms():
    parser = build_parser()
    assert parser.parse_args([]).use_pumpkin_juice is None
    assert parser.parse_args(["--use-pumpkin-juice"]).use_pumpkin_juice == -1
    assert parser.parse_args(["--use-pumpkin-juice=5"]).use_pumpkin_juice == 5


def test_resolve_pumpkin_juice_cli_overrides_config():
    # The bare flag (-1) enables with no limit even when the config turns it off.
    assert resolve_pumpkin_juice(-1, {"mastery": {"use_pumpkin_juice": False}}) == (
        True, None, 230.0
    )
    # An explicit cap wins too, with the default price when unset.
    assert resolve_pumpkin_juice(5, {}) == (True, 5, 230.0)


def test_resolve_pumpkin_juice_from_config():
    assert resolve_pumpkin_juice(None, {}) == (False, None, 230.0)
    assert resolve_pumpkin_juice(None, {"mastery": {"use_pumpkin_juice": True}}) == (
        True, None, 230.0
    )
    assert resolve_pumpkin_juice(
        None, {"mastery": {"use_pumpkin_juice": 7, "pumpkin_juice_price": 200}}
    ) == (True, 7, 200.0)


def test_resolve_fungus_processing_checks_inventory_by_default():
    held = Inventory(
        items={
            "fungus processing and you": InventoryItem(
                name="Fungus Processing and You", quantity=1
            )
        }
    )
    assert resolve_fungus_processing({}, held) is True
    assert resolve_fungus_processing({}, Inventory()) is False
    # No inventory to check (e.g. the --unobtainable report) defaults to off.
    assert resolve_fungus_processing({}) is False


def test_resolve_fungus_processing_config_override_wins():
    held = Inventory(
        items={
            "fungus processing and you": InventoryItem(
                name="Fungus Processing and You", quantity=1
            )
        }
    )
    assert (
        resolve_fungus_processing(
            {"exploring": {"have_fungus_processing": False}}, held
        )
        is False
    )
    assert (
        resolve_fungus_processing(
            {"exploring": {"have_fungus_processing": True}}, Inventory()
        )
        is True
    )


def test_resolve_scythe_of_dewstar_defaults_off_with_nothing_to_check():
    # A perk, not a held item -- with no state/requirements (e.g. --unobtainable),
    # only the config counts.
    assert resolve_scythe_of_dewstar({}) is False
    assert resolve_scythe_of_dewstar({"farming": {"have_scythe_of_dewstar": False}}) is False


def test_resolve_scythe_of_dewstar_config_override_wins():
    requirements = TowerRequirements(levels={DEWSTAR_TOWER_LEVEL: {"Butter": 1_000_000}})
    complete = GameState(
        masteries={"butter": Mastery("Butter", progress=1_000_000, required=None)}
    )
    assert (
        resolve_scythe_of_dewstar(
            {"farming": {"have_scythe_of_dewstar": False}}, complete, requirements
        )
        is False
    )
    assert (
        resolve_scythe_of_dewstar(
            {"farming": {"have_scythe_of_dewstar": True}}, GameState(), requirements
        )
        is True
    )


def test_resolve_scythe_of_dewstar_auto_detects_from_dewstars_tower():
    requirements = TowerRequirements(
        levels={DEWSTAR_TOWER_LEVEL: {"Butter": 1_000_000}}
    )
    # Every requirement through the level met: the tower solve reports no
    # shortfall, so the Scythe is assumed held.
    complete = GameState(
        masteries={"butter": Mastery("Butter", progress=1_000_000, required=None)}
    )
    assert resolve_scythe_of_dewstar({}, complete, requirements) is True
    # A shortfall anywhere through the level means the tower isn't cleared yet.
    incomplete = GameState(
        masteries={"butter": Mastery("Butter", progress=1, required=1_000_000)}
    )
    assert resolve_scythe_of_dewstar({}, incomplete, requirements) is False
    # Nothing to check (unobtainable report) defaults to off.
    assert resolve_scythe_of_dewstar({}, complete, None) is False
    assert resolve_scythe_of_dewstar({}, None, requirements) is False


def test_resolve_dewstars_journal_checks_inventory_by_default():
    held = Inventory(
        items={"dewstar's journal": InventoryItem(name="Dewstar's Journal", quantity=1)}
    )
    assert resolve_dewstars_journal({}, held) is True
    assert resolve_dewstars_journal({}, Inventory()) is False
    assert resolve_dewstars_journal({}) is False


def test_resolve_dewstars_journal_config_override_wins():
    held = Inventory(
        items={"dewstar's journal": InventoryItem(name="Dewstar's Journal", quantity=1)}
    )
    assert (
        resolve_dewstars_journal(
            {"farming": {"have_dewstars_journal": False}}, held
        )
        is False
    )
    assert (
        resolve_dewstars_journal(
            {"farming": {"have_dewstars_journal": True}}, Inventory()
        )
        is True
    )


def test_resolve_monthly_grants_defaults_off_with_nothing_to_check():
    # A tower perk, not a held item -- with no state/requirements (e.g.
    # --unobtainable), only the config counts.
    assert resolve_monthly_grants({}) is False
    assert resolve_monthly_grants({"exploring": {"have_mechanical_heart": False}}) is False


def test_resolve_monthly_grants_auto_detects_from_the_tower_level():
    requirements = TowerRequirements(
        levels={MONTHLY_GRANT_TOWER_LEVEL: {"Butter": 1_000_000}}
    )
    # Every requirement through level 300 met: the tower solve reports no
    # shortfall, so the grants are assumed active.
    complete = GameState(
        masteries={"butter": Mastery("Butter", progress=1_000_000, required=None)}
    )
    assert resolve_monthly_grants({}, complete, requirements) is True
    # A shortfall anywhere through the level means it isn't reached yet.
    incomplete = GameState(
        masteries={"butter": Mastery("Butter", progress=1, required=1_000_000)}
    )
    assert resolve_monthly_grants({}, incomplete, requirements) is False
    # Nothing to check (unobtainable report) defaults to off.
    assert resolve_monthly_grants({}, complete, None) is False
    assert resolve_monthly_grants({}, None, requirements) is False


def test_resolve_monthly_grants_config_override_wins():
    requirements = TowerRequirements(
        levels={MONTHLY_GRANT_TOWER_LEVEL: {"Butter": 1_000_000}}
    )
    complete = GameState(
        masteries={"butter": Mastery("Butter", progress=1_000_000, required=None)}
    )
    # Override off beats a met tower; override on beats an unmet one.
    assert (
        resolve_monthly_grants(
            {"exploring": {"have_mechanical_heart": False}}, complete, requirements
        )
        is False
    )
    assert (
        resolve_monthly_grants(
            {"exploring": {"have_mechanical_heart": True}}, GameState(), requirements
        )
        is True
    )


def test_pumpkin_juice_gains_uses_the_mastery_threshold():
    # A juice grants 10% of the item's tier (its required denominator), in the
    # optimiser's items-to-find units (a found item is worth mastery_bonus mastery).
    state = GameState(
        masteries={"butter": Mastery("Butter", progress=0, required=1_000_000)}
    )
    needs = [ItemNeed("Butter", have=0, target=1_000_000, is_mastery=True)]
    gains = pumpkin_juice_gains(needs, state, 1.1)
    assert gains == {"butter": pytest.approx(0.1 * 1_000_000 / 1.1)}


def test_pumpkin_juice_gains_falls_back_to_the_target_when_untracked():
    # An item absent from the mastery file uses the goal target as its tier.
    needs = [ItemNeed("Gainite", have=0, target=100_000, is_mastery=True)]
    gains = pumpkin_juice_gains(needs, GameState(), 1.0)
    assert gains == {"gainite": pytest.approx(0.1 * 100_000)}


def test_pumpkin_juice_gains_skips_acquire_goals_and_met_needs():
    needs = [
        ItemNeed("Butter", have=0, target=100, is_mastery=False),  # -i, no mastery
        ItemNeed("Hops", have=50, target=50, is_mastery=True),  # already met
    ]
    assert pumpkin_juice_gains(needs, GameState(), 1.1) == {}


def test_print_frontier_shows_the_pumpkin_juice_column(capsys):
    plans = [SourcingPlan(time=timedelta(0), pumpkin_juices={"butter": 3})]
    print_frontier(plans, 0, 0)
    out = capsys.readouterr().out
    assert "Pumpkin Juice" in out
    assert "3 PJ" in out


def test_print_frontier_omits_the_pumpkin_juice_column_when_unused(capsys):
    print_frontier([SourcingPlan(time=timedelta(0))], 0, 0)
    assert "Pumpkin Juice" not in capsys.readouterr().out


def test_pumpkin_juice_schedule_names_the_drink_levels():
    # Two juices step down from the goal by the LP's gain (1,000,000 / 1.1 per juice
    # for an MM item), so the last lands the item on target and the one before a step
    # below -- both above the current 750,000 progress.
    gain = 0.1 * 1_000_000 / 1.1  # 90,909.09, matching pumpkin_juice_gains
    needs = [ItemNeed("Butter", have=750_000, target=1_000_000, is_mastery=True)]
    plan = SourcingPlan(pumpkin_juices={"butter": 2})
    assert pumpkin_juice_schedule(plan, needs, {"butter": gain}) == {
        "butter": [round(1_000_000 - 2 * gain), round(1_000_000 - gain)]
    }


def test_pumpkin_juice_schedule_clamps_to_current_progress():
    # Only ~40,000 of shortfall left, so target - gain (~909,091) sits below the
    # current 960,000 -- the drink point clamps up to where the item already is.
    gain = 0.1 * 1_000_000 / 1.1
    needs = [ItemNeed("Butter", have=960_000, target=1_000_000, is_mastery=True)]
    plan = SourcingPlan(pumpkin_juices={"butter": 1})
    assert pumpkin_juice_schedule(plan, needs, {"butter": gain}) == {"butter": [960_000]}


def test_pumpkin_juice_schedule_skips_items_with_no_juice():
    needs = [ItemNeed("Butter", have=0, target=1_000_000, is_mastery=True)]
    assert pumpkin_juice_schedule(SourcingPlan(), needs, {}) == {}


def test_print_plan_shows_the_pumpkin_juice_section(capsys):
    demand = {"Butter": 500}
    plan = SourcingPlan(
        draws=[Draw(Offer("fish Pond", "fishing", {"butter": 100}, ln=1), 5)],
        pumpkin_juices={"butter": 3},
        pumpkin_gold=690.0,
        pumpkin_credit={"butter": 300},
    )
    print_plan(
        plan, demand, Farm(), Workshop(), 1.0,
        pumpkin_schedule={"butter": [700_000, 800_000, 900_000]},
    )
    out = capsys.readouterr().out
    assert "Pumpkin Juice (mastery boost):" in out
    assert "3x Pumpkin Juice on Butter = 690 g" in out
    assert "drink at Butter mastery 700,000 / 800,000 / 900,000" in out
    assert "total 690 g" in out


def test_print_plan_buy_total_excludes_pumpkin_gold(capsys):
    # The Buy total is the consumable gold alone; the juice is listed apart, so the
    # plan's gold (691) splits into 1 g of nets bought and 690 g of juice.
    demand = {"Butter": 500}
    plan = SourcingPlan(
        draws=[
            Draw(
                Offer("fish Pond", "fishing", {"butter": 100},
                      consumes={"large net": 1}, ln=1),
                5,
            )
        ],
        bought={"large net": 100},
        gold=100 * 0.01 + 690,
        pumpkin_juices={"butter": 3},
        pumpkin_gold=690.0,
        pumpkin_credit={"butter": 300},
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.0, prices={"large net": 0.01})
    out = capsys.readouterr().out
    buy_section = out.split("Buy (with gold):")[1].split("Pumpkin Juice")[0]
    assert "total 1 g" in buy_section
    assert "total 690 g" in out


def test_sectioned_reporter_renders_each_section_independently():
    # Drives SectionedReporter with the same calls main.run makes on the
    # Reporter interface, and the same synthetic fixtures the print_*/markdown_*
    # tests above use, then checks each section's own MarkdownDoc renders
    # independently (no cross-section bleed) with the expected heading.
    reporter = SectionedReporter()

    solution = Solution(needs=[ItemNeed(item="Beet", have=10, target=100)])
    reporter.solution(solution)
    reporter.note("Starting 01 Jan 2026; seasonal pets count only for part of it.")

    workshop = Workshop(
        recipes={"board": WorkshopRecipe(name="Board", materials={"oak": 2})}
    )
    demand = {"Drum": 500, "Board": 10}
    plan = SourcingPlan(
        draws=[Draw(Offer("fish Small Pond", "fishing", {"drum": 100}, ln=1), 5)],
        crafts={"board": 10},
    )
    reporter.frontier([plan], selected=0, cheapest=0)
    reporter.plan(plan, demand, Farm(grape_juices_per_day=14), workshop, 1.0)
    reporter.crafts(plan, workshop, 1.0, 1.1)
    reporter.finish()  # no-op; nothing to assert

    assert reporter.notes == [
        "Starting 01 Jan 2026; seasonal pets count only for part of it."
    ]
    solution_text = reporter.docs["solution"].render()
    assert "## Goal" in solution_text
    assert "Beet" in solution_text

    notes_text = reporter.docs["notes"].render()
    assert "seasonal pets" in notes_text
    assert "Goal" not in notes_text  # sections don't bleed into each other

    frontier_text = reporter.docs["frontier"].render()
    assert "## Sourcing plans" in frontier_text

    # The plan and crafts docs render under the page's own "Selected plan"
    # heading, so their sections are demoted one level to nest beneath it.
    plan_text = reporter.docs["plan"].render()
    assert "### Fishing" in plan_text

    crafts_text = reporter.docs["crafts"].render()
    assert crafts_text.splitlines()[0] == "### Required Crafts"
    assert "Board" in crafts_text
