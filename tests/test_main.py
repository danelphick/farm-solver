import argparse
from datetime import timedelta

import pytest
from rich.console import Console

import main
from crafting import Workshop, WorkshopRecipe, recipe_flows, store_purchases
from crops import Farm
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
    _parse_spread,
    _should_model_calendar,
    apply_ignored_goals,
    apply_inventory_overrides,
    build_parser,
    cheapest_plan,
    cookie_sets_available,
    limit_shortfall,
    parse_gold_budget,
    parse_ignored_goals,
    parse_target,
    price_pumpkin_juice_enabled,
    price_stamina_enabled,
    pumpkin_juice_gains,
    pumpkin_juice_schedule,
    resolve_calendar_mode,
    resolve_crafting_bonus,
    resolve_current_stamina,
    resolve_dewstars_journal,
    resolve_frontier_graph,
    resolve_frontier_spread,
    resolve_fungus_processing,
    resolve_held_item,
    resolve_mariyas_wheelbarrow,
    resolve_monthly_grants,
    resolve_pumpkin_juice,
    resolve_scythe_of_dewstar,
    resolve_swamp_puzzle,
    selected_plan,
    strip_dated_sources,
    within_limits,
)
from optimizer import SourcingPlan, SpreadSpec
from passive import DAILY, MINUTE, PassiveProducer, PassiveProduction
from report import (
    IGNORED_GOAL_TEXT,
    UNREACHABLE_GOAL_MARK,
    MarkdownDoc,
    SectionedReporter,
    _frontier_reasons,
    _highlight_unreachable_rows,
    _slowest_passive_items,
    markdown_frontier,
    markdown_solution,
    print_crafts,
    print_critical_path,
    print_frontier,
    print_plan,
    print_solution,
)
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


def test_cheapest_plan_ranks_by_net_gold_charging_stamina():
    # Plan 0 buys nothing but drains banked stamina worth 30 (net 30); plan 1 buys
    # 10 and burns no stamina (net 10), so it is the cheapest.
    plans = [
        SourcingPlan(time=timedelta(hours=1), gold=0, stamina_gold=30),
        SourcingPlan(time=timedelta(hours=3), gold=10, stamina_gold=0),
    ]
    assert cheapest_plan(plans) == 1


def test_resolve_current_stamina_cli_overrides_config():
    cfg = {"farmhouse": {"current_stamina": 5000}}
    assert resolve_current_stamina(build_parser().parse_args([]), cfg) == 5000
    # The flag wins in either direction, including an explicit zero: "I have
    # none right now" has to beat a config value that has gone stale.
    flagged = build_parser().parse_args(["--current-stamina", "120"])
    assert resolve_current_stamina(flagged, cfg) == 120
    zeroed = build_parser().parse_args(["--current-stamina", "0"])
    assert resolve_current_stamina(zeroed, cfg) == 0


def test_resolve_current_stamina_defaults_to_zero():
    unset = build_parser().parse_args([])  # current_stamina is None
    assert resolve_current_stamina(unset, {"farmhouse": {}}) == 0
    assert resolve_current_stamina(unset, {}) == 0


def test_resolve_current_stamina_rejects_a_nonsense_config_value():
    unset = build_parser().parse_args([])
    with pytest.raises(ValueError, match="must not be negative"):
        resolve_current_stamina(unset, {"farmhouse": {"current_stamina": -1}})
    with pytest.raises(ValueError, match="must be a number"):
        resolve_current_stamina(unset, {"farmhouse": {"current_stamina": "lots"}})


def test_price_stamina_enabled_cli_overrides_config():
    on = build_parser().parse_args(["--price-stamina"])
    off = build_parser().parse_args(["--no-price-stamina"])
    assert price_stamina_enabled(on, {"trading": {"price_stamina": False}}) is True
    assert price_stamina_enabled(off, {"trading": {"price_stamina": True}}) is False


def test_price_stamina_enabled_defaults_from_config_then_true():
    unset = build_parser().parse_args([])  # price_stamina is None
    assert price_stamina_enabled(unset, {"trading": {"price_stamina": False}}) is False
    assert price_stamina_enabled(unset, {"trading": {}}) is True  # default on
    assert price_stamina_enabled(unset, {}) is True


def test_price_pumpkin_juice_enabled_cli_overrides_config():
    on = build_parser().parse_args(["--price-pumpkin-juice"])
    off = build_parser().parse_args(["--no-price-pumpkin-juice"])
    cfg_off = {"trading": {"price_pumpkin_juice": False}}
    cfg_on = {"trading": {"price_pumpkin_juice": True}}
    assert price_pumpkin_juice_enabled(on, cfg_off) is True
    assert price_pumpkin_juice_enabled(off, cfg_on) is False


def test_price_pumpkin_juice_enabled_defaults_from_config_then_true():
    unset = build_parser().parse_args([])  # price_pumpkin_juice is None
    assert price_pumpkin_juice_enabled(unset, {"trading": {"price_pumpkin_juice": False}}) is False
    assert price_pumpkin_juice_enabled(unset, {"trading": {}}) is True  # default on
    assert price_pumpkin_juice_enabled(unset, {}) is True


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


def test_limit_shortfall_reports_a_too_tight_apple_cider_limit():
    plan = SourcingPlan(time=timedelta(0), apple_ciders=40)
    message = limit_shortfall([plan], None, None, None, None, max_ac=5)
    assert "within 5 AC (fewest is 40)" in message


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
    plan = SourcingPlan(
        time=timedelta(days=2), arnold_palmers=500, large_nets=50, apple_ciders=30
    )
    assert within_limits(plan)  # no limits
    assert within_limits(plan, time_limit=timedelta(days=3), max_ap=500, max_ln=50)
    assert within_limits(plan, max_ac=30)
    assert not within_limits(plan, time_limit=timedelta(days=1))  # too slow
    assert not within_limits(plan, max_ap=499)  # too many palmers
    assert not within_limits(plan, max_ln=49)  # too many nets
    assert not within_limits(plan, max_ac=29)  # too many ciders


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


def test_parse_spread_bare_axis_names():
    assert _parse_spread("time") == SpreadSpec("time")
    assert _parse_spread("gold") == SpreadSpec("gold")
    assert _parse_spread("passive") == SpreadSpec("passive")
    assert _parse_spread("pumpkin-juice") == SpreadSpec("pumpkin-juice")


def test_parse_spread_pj_is_an_alias_for_pumpkin_juice():
    assert _parse_spread("pj") == SpreadSpec("pumpkin-juice")


def test_parse_spread_is_case_insensitive():
    assert _parse_spread("PJ") == SpreadSpec("pumpkin-juice")
    assert _parse_spread("Time") == SpreadSpec("time")


def test_parse_spread_with_a_range():
    assert _parse_spread("pj=0:10") == SpreadSpec("pumpkin-juice", 0.0, 10.0)
    assert _parse_spread("gold=0:5000") == SpreadSpec("gold", 0.0, 5000.0)


def test_parse_spread_rejects_a_bad_axis():
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_spread("banana")


def test_parse_spread_rejects_a_malformed_range():
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_spread("pj=0")  # no colon
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_spread("pj=abc:10")  # not numbers
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_spread("time=:10")  # lo omitted -- both required together


def test_spread_flag_parses_through_build_parser():
    args = build_parser().parse_args(["--spread", "pj=0:10"])
    assert args.spread == SpreadSpec("pumpkin-juice", 0.0, 10.0)


def test_spread_flag_defaults_to_none():
    assert build_parser().parse_args([]).spread is None


def test_graph_flag_bare_defaults_to_auto():
    assert build_parser().parse_args(["--graph"]).graph == "auto"


def test_graph_flag_accepts_an_explicit_metric():
    assert build_parser().parse_args(["--graph", "gold"]).graph == "gold"
    assert build_parser().parse_args(["--graph", "time"]).graph == "time"


def test_graph_flag_defaults_to_none_when_absent():
    assert build_parser().parse_args([]).graph is None


def test_graph_flag_rejects_an_unknown_metric():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--graph", "nonsense"])


def test_resolve_frontier_spread_cli_overrides_config():
    args = build_parser().parse_args(["--spread", "gold=0:100"])
    cfg = {"frontier": {"spread": "pumpkin-juice"}}
    assert resolve_frontier_spread(args, cfg) == SpreadSpec("gold", 0.0, 100.0)


def test_resolve_frontier_spread_parses_the_config_string():
    args = build_parser().parse_args([])
    assert resolve_frontier_spread(args, {"frontier": {"spread": "time"}}) == (
        SpreadSpec("time")
    )


def test_resolve_frontier_spread_fills_range_from_spread_range():
    args = build_parser().parse_args([])
    cfg = {"frontier": {"spread": "pumpkin-juice", "spread_range": [0, 10]}}
    assert resolve_frontier_spread(args, cfg) == SpreadSpec("pumpkin-juice", 0.0, 10.0)


def test_resolve_frontier_spread_inline_range_wins_over_spread_range():
    args = build_parser().parse_args([])
    cfg = {"frontier": {"spread": "gold=5:50", "spread_range": [0, 10]}}
    assert resolve_frontier_spread(args, cfg) == SpreadSpec("gold", 5.0, 50.0)


def test_resolve_frontier_spread_none_when_unset():
    args = build_parser().parse_args([])
    assert resolve_frontier_spread(args, {}) is None
    assert resolve_frontier_spread(args, {"frontier": {}}) is None


def test_resolve_frontier_graph_cli_overrides_config():
    args = build_parser().parse_args(["--graph", "time"])
    assert resolve_frontier_graph(args, {"frontier": {"graph": "gold"}}) == "time"


def test_resolve_frontier_graph_from_config():
    args = build_parser().parse_args([])
    assert resolve_frontier_graph(args, {"frontier": {"graph": "auto"}}) == "auto"


def test_resolve_frontier_graph_none_when_unset():
    args = build_parser().parse_args([])
    assert resolve_frontier_graph(args, {}) is None
    assert resolve_frontier_graph(args, {"frontier": {}}) is None


def test_parse_target_accepts_tower_case_insensitively():
    assert parse_target("tower") == "tower"
    assert parse_target("Tower") == "tower"


def test_parse_target_accepts_none_case_insensitively():
    assert parse_target("none") == "none"
    assert parse_target("None") == "none"


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
    # 'none' is an explicit "drop the tower" that survives argparse as a string
    # distinct from the unset default (None); build_inputs treats it as goals-only.
    assert parser.parse_args(["--target", "none"]).target == "none"


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


def test_markdown_frontier_tags_selected_by_default_but_not_for_the_web_app():
    # The CLI's raw Markdown/terminal render has no row highlight, so it keeps
    # the "selected" word (default include_selected_tag=True); the web app's
    # SectionedReporter forces it off since its own CSS row highlight already
    # shows selection, and the word appearing/disappearing on click was
    # causing a layout shift.
    doc = MarkdownDoc()
    markdown_frontier(doc, PLANS, selected=2, cheapest=2)
    text = doc.render()
    assert "[cheapest, selected]" in text

    reporter = SectionedReporter()
    reporter.frontier(PLANS, selected=2, cheapest=2)
    web_text = reporter.docs["frontier"].render()
    assert "selected" not in web_text
    assert "[cheapest]" in web_text


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


def test_frontier_reasons_name_every_tied_resource():
    # Orange and Lemon both sit at their full requirement and finish at the same
    # 10-min makespan, so the reason names the whole tie instead of arbitrarily
    # picking whichever an alternate LP optimum listed first.
    passive_offer = Offer(
        "passive", "passive", {"orange": 10.0, "lemon": 10.0},
        time_per_unit=timedelta(minutes=1),
    )
    plan = SourcingPlan(draws=[Draw(passive_offer, 10)], time=timedelta(minutes=10))
    reasons = _frontier_reasons(
        [plan], {"orange": 100, "lemon": 100}, {}, 1.0, [], {},
        max_passive_residuals={"orange": 100, "lemon": 100}, production=None,
        display={"orange": "Orange", "lemon": "Lemon"},
    )
    assert reasons == ["all Lemon and Orange from passive"]


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


def test_print_plan_renders_each_channel(capsys):
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
        # Materials with no known source (e.g. Magicite here) are reported once,
        # up in the Goal section (print_solution), not repeated in the plan.
        remainder={"Magicite": 3},
    )
    print_plan(plan, demand, Farm(grape_juices_per_day=14), Workshop(), 1.45)
    out = capsys.readouterr().out

    assert "Fishing:" in out
    assert "5x Large Net at Small Pond (Drum)" in out
    assert "Exploring (Arnold Palmer):" in out
    assert "2x Arnold Palmer at Cave (100x Bone)" in out
    assert "Farming (1d 1h):" in out  # 25 Harvest Alls * 1h, grown one at a time
    assert "Harvest Alls of Corn" in out
    # Board at 10/min needs 200 min = 3h 20m, shown per item under passive.
    assert "Passive production (concurrent):" in out
    assert "2,000x Board in 3h 20m" in out
    assert "Magicite" not in out


def test_print_solution_flags_a_goal_with_no_known_source(capsys):
    solution = Solution(
        needs=[
            ItemNeed(item="Yule Goat", have=17991, target=20000, is_mastery=True),
            ItemNeed(item="Wood", have=10, target=100),
        ]
    )
    print_solution(solution, remainder={"Yule Goat": 1826})
    out = capsys.readouterr().out

    assert "Yule Goat: need 2,009 more (have 17,991 of 20,000) ✗" in out
    assert "Wood: need 90 more (have 10 of 100)\n" in out
    assert "Materials with no known source (supply yourself):" in out
    assert "1,826x Yule Goat" in out


def test_markdown_solution_flags_a_goal_with_no_known_source():
    doc = MarkdownDoc()
    solution = Solution(
        needs=[ItemNeed(item="Yule Goat", have=17991, target=20000, is_mastery=True)]
    )
    markdown_solution(doc, solution, remainder={"Yule Goat": 1826})
    text = doc.render()

    assert "## Goal" in text
    assert "| 2,009 ✗ |" in text
    heading_index = text.index("## Materials with no known source")
    goal_index = text.index("## Goal")
    assert goal_index < heading_index
    assert "| Yule Goat | 1,826 |" in text


def test_parse_ignored_goals_reads_a_comma_list_case_insensitively():
    assert parse_ignored_goals("Yule Goat, wood") == {"yule goat", "wood"}
    assert parse_ignored_goals("") == set()
    assert parse_ignored_goals(None) == set()
    assert parse_ignored_goals(" , Wood , ") == {"wood"}


def test_apply_ignored_goals_flags_every_need_for_the_named_item():
    # One item, two needs (two tower levels want it): ignoring the item drops
    # both, since ignoring is per item, not per row.
    solution = Solution(
        needs=[
            ItemNeed(item="Yule Goat", have=0, target=50, level=201, is_mastery=True),
            ItemNeed(item="Yule Goat", have=0, target=90, level=210, is_mastery=True),
            ItemNeed(item="Wood", have=10, target=100),
        ]
    )
    result = apply_ignored_goals(solution, "yule goat")

    assert [need.ignored for need in result.needs] == [True, True, False]
    # The needs themselves survive -- the Goal report still lists them.
    assert [need.item for need in result.needs] == ["Yule Goat", "Yule Goat", "Wood"]
    # Nothing ignored: the same object back, untouched.
    assert apply_ignored_goals(solution, None) is solution


def test_markdown_solution_marks_an_ignored_goal_rather_than_hiding_it():
    doc = MarkdownDoc()
    solution = Solution(
        needs=[
            ItemNeed(
                item="Yule Goat", have=17991, target=20000, is_mastery=True,
                ignored=True,
            ),
            ItemNeed(item="Wood", have=10, target=100),
        ]
    )
    markdown_solution(doc, solution)
    text = doc.render()

    assert f"| Yule Goat | 17,991 | 20,000 | {IGNORED_GOAL_TEXT} |" in text
    # The goal that is still being solved for reads as it always did.
    assert "| Wood | 10 | 100 | 90 |" in text


def test_print_solution_says_an_ignored_goal_is_ignored(capsys):
    solution = Solution(
        needs=[
            ItemNeed(
                item="Yule Goat", have=17991, target=20000, is_mastery=True,
                ignored=True,
            )
        ]
    )
    print_solution(solution)

    assert "Yule Goat: ignored (have 17,991 of 20,000)" in capsys.readouterr().out


def test_ignore_goal_end_to_end_leaves_the_solver_nothing_to_do(tmp_path, capsys):
    # The whole wiring in one run: the Goal section still names the item (as
    # ignored, so it is visibly skipped rather than missing), and the frontier
    # it would otherwise have driven collapses to a single do-nothing plan.
    mastery = tmp_path / "m.txt"
    inventory = tmp_path / "i.txt"
    mastery.write_text("Corn\n1,500 / 10,000 Progress\n15% Track\n")
    inventory.write_text("Crops chevron_down\n\nCorn\nA yellow vegetable\nMastered\n384\n")
    argv = [
        "--mastery-file", str(mastery), "--inventory-file", str(inventory),
        "--target", "none", "-i", "Corn:5000", "--start-date", "2026-09-01",
    ]

    main.run(build_parser().parse_args(argv))
    wanted = capsys.readouterr().out
    assert "Corn: need 4,616 more (have 384 of 5,000)" in wanted

    # Case-insensitively, like every other item name on the command line.
    main.run(build_parser().parse_args([*argv, "--ignore-goal", "corn"]))
    ignored = capsys.readouterr().out
    assert "Corn: ignored (have 384 of 5,000)" in ignored
    assert "0m + 0 AP + 0 LN -> 0.0 + 0.0 = 0.0 g" in ignored


def test_highlight_unreachable_rows_colours_only_the_flagged_line():
    # MarkdownReporter.finish's terminal path: plain Markdown/rich have no
    # per-row colour, so this walks rich's own rendered lines and recolours
    # the ones naming an unreachable goal -- everything else (borders,
    # headers, the "done" row) keeps rich's default styling.
    console = Console(width=40, force_terminal=True, color_system="standard")
    text = (
        "| Item | Still needed |\n"
        "| --- | --- |\n"
        f"| Yule Goat | 5 {UNREACHABLE_GOAL_MARK} |\n"
        "| Wood | done |\n"
    )
    segments = _highlight_unreachable_rows(console, text)
    with console.capture() as capture:
        console.print(segments)
    out = capture.get()
    goat_line = next(line for line in out.splitlines() if "Yule Goat" in line)
    wood_line = next(line for line in out.splitlines() if "Wood" in line)
    assert "\x1b[31m" in goat_line
    assert "\x1b[31m" not in wood_line


def test_print_plan_splits_gold_buys_from_arnold_palmer_trades(capsys):
    # A plan that buys a Large Net (gold) and trades for Red Dye (AP): the two land
    # in separate sections, with the trade showing its AP cost and gold-equivalent.
    plan = SourcingPlan(
        bought={"large net": 200, "red dye": 5000},
        gold=200 * 0.01 + 5000 * (0.008 * 0.06),
    )
    prices = {"large net": 0.01, "red dye": 0.008 * 0.06}
    ap_prices = {"red dye": 0.008}
    print_plan(
        plan, {}, Farm(), Workshop(), 1.0, prices=prices, ap_prices=ap_prices
    )
    out = capsys.readouterr().out
    assert "Buy (with gold):" in out
    assert "200x large net = 2.0 g" in out
    assert "Trade (for Arnold Palmers):" in out
    # 5000 red dye * 0.008 AP = 40 AP; * 0.06 g/AP = 2.4 g.
    assert "5,000x red dye = 40 AP (2.4 g)" in out
    assert "total 40 AP (2.4 g)" in out
    # Red Dye is a trade, not a gold buy -- it must not appear in the gold section.
    assert "red dye = 2.4 g" not in out


FEEDER_WORKSHOP = Workshop(
    recipes={
        "pinecone bird feeder": WorkshopRecipe(
            "Pinecone Bird Feeder",
            {"Corn Seeds": 2, "Pine Cone": 4, "Sunflower Seeds": 2, "Iron": 1},
        )
    }
)


def test_store_materials_are_free_to_the_solver_but_counted_for_the_report():
    # The LP never sees Country Store seeds (or Iron), only the gathered Pine Cone...
    assert recipe_flows(FEEDER_WORKSHOP) == {"pinecone bird feeder": {"pine cone": 4.0}}
    # ...but the crafts it chooses still say how many seeds to buy.
    assert store_purchases({"pinecone bird feeder": 10}, FEEDER_WORKSHOP) == {
        "corn seeds": 20.0,
        "sunflower seeds": 20.0,
    }


def test_print_plan_lists_country_store_buys_net_of_held_seeds(capsys):
    plan = SourcingPlan(crafts={"pinecone bird feeder": 10})
    print_plan(
        plan, {}, Farm(), FEEDER_WORKSHOP, 1.0, inventory={"corn seeds": 5}
    )
    out = capsys.readouterr().out
    assert "Buy (at the Country Store):" in out
    assert "15x Corn Seeds (needs 20, 5 held)" in out
    assert "20x Sunflower Seeds\n" in out


def test_print_plan_names_the_acorn_pies_a_pie_explore_eats(capsys):
    # A pie-boosted explore burns 1/450 of an Acorn Pie per palmer; 109 palmers is
    # 0.24 of a pie, but you eat whole pies, so the line rounds up to 1.
    offer = Offer(
        "explore Gary's Crushroom with acorn pie",
        "exploring",
        {"hide": 92},
        consumes={"arnold palmer": 1.0, "acorn pie": 1 / 450},
        ap=1,
    )
    plan = SourcingPlan(draws=[Draw(offer, 109)])
    print_plan(plan, {"Hide": 10000}, Farm(), Workshop(), 1.0)
    out = capsys.readouterr().out
    assert "Arnold Palmer at Gary's Crushroom with 1 acorn pie" in out


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


def test_print_plan_quantizes_passive_waits_to_whole_deposits(capsys):
    # Antlers land once a day, 100 a deposit; 250 need the third deposit at day 3.
    # The LP's continuous makespan is 2.5 days (Draw of 3600 rate-minutes), but the
    # plan time was lifted to the deposit boundary and every printed wait -- the
    # critical-path header, its passive line, and the passive section row -- agrees.
    production = PassiveProduction(
        producers=[PassiveProducer("raptor pen", DAILY, {"antlers": 100.0})]
    )
    offer = Offer(
        "passive", "passive", {"antlers": 100.0 / 1440}, time_per_unit=timedelta(minutes=1)
    )
    plan = SourcingPlan(draws=[Draw(offer, 3600)], time=timedelta(days=3))
    print_plan(plan, {"Antlers": 250}, Farm(), Workshop(), 1.0, production=production)
    out = capsys.readouterr().out
    assert "Critical path (sets the 3d total):" in out
    assert "250x Antlers from passive production (3d)" in out
    assert "250x Antlers in 3d" in out
    assert "2d 12h" not in out


def test_print_plan_shows_the_staggered_half_day_deposit(capsys):
    # The pen and its snare each fill the storehouse, phase-staggered 12h apart,
    # so one storehouse-worth of Antlers is a 12h wait -- not a whole day.
    production = PassiveProduction(
        producers=[
            PassiveProducer("raptor pen", DAILY, {"antlers": 2_617_810}),
            PassiveProducer("raptor pen snare", DAILY, {"antlers": 261_781}),
        ],
        inventory_size=49_440,
    )
    offer = Offer(
        "passive", "passive", {"antlers": 2 * 49_440 / 1440},
        time_per_unit=timedelta(minutes=1),
    )
    plan = SourcingPlan(draws=[Draw(offer, 720)], time=timedelta(hours=12))
    print_plan(
        plan, {"Antlers": 49_440}, Farm(), Workshop(), 1.0, production=production
    )
    out = capsys.readouterr().out
    assert "49,440x Antlers in 12h" in out


def test_print_plan_passive_fill_keeps_the_farming_critical_path(capsys):
    # Farming (2d 17h) drives the plan; the antlers' continuous wait (2d 12h)
    # fits inside it, so passive is fill and its day-3 deposit boundary must not
    # take over the critical path or the displayed wait -- the report reads as it
    # did before quantisation (see optimizer._quantize_plan_time's fill exemption;
    # plan.time was likewise not lifted).
    production = PassiveProduction(
        producers=[PassiveProducer("raptor pen", DAILY, {"antlers": 100.0})]
    )
    passive_offer = Offer(
        "passive", "passive", {"antlers": 100.0 / 1440},
        time_per_unit=timedelta(minutes=1),
    )
    farming = Offer(
        "grow Corn", "farming", {"corn": 1}, time_per_unit=timedelta(minutes=1)
    )
    plan = SourcingPlan(
        draws=[Draw(passive_offer, 3600), Draw(farming, 3900)],
        time=timedelta(minutes=3900),
    )
    print_plan(
        plan, {"Antlers": 250, "Corn": 3900}, Farm(), Workshop(), 1.0,
        production=production,
    )
    out = capsys.readouterr().out
    assert "Critical path (sets the 2d 17h total):" in out
    assert "Harvest Alls of Corn" in out
    assert "250x Antlers in 2d 12h" in out  # continuous fill, not the 3d boundary
    assert "3d" not in out


def test_print_plan_pet_waits_stay_continuous(capsys):
    # A pet's daily output is an averaged rate, not a real deposit, so its wait is
    # not lifted to a whole day: 250 Horn at 100/day reads 2d 12h, as before.
    production = PassiveProduction(
        producers=[PassiveProducer("pet Dog", DAILY, {"horn": 100.0})]
    )
    offer = Offer(
        "passive", "passive", {"horn": 100.0 / 1440}, time_per_unit=timedelta(minutes=1)
    )
    plan = SourcingPlan(draws=[Draw(offer, 3600)], time=timedelta(days=2.5))
    print_plan(plan, {"Horn": 250}, Farm(), Workshop(), 1.0, production=production)
    out = capsys.readouterr().out
    assert "250x Horn in 2d 12h" in out


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
    items = _slowest_passive_items(plan, need, elsewhere, display, production)
    assert [item[0] for item in items] == ["Oak"]


def test_slowest_passive_item_keeps_pet_when_it_is_primary_source():
    # When a pet is the only (hence primary) source of an item, it legitimately
    # caps the plan and is still named.
    production = PassiveProduction(
        producers=[PassiveProducer("pet Dog", MINUTE, {"horn": 1.0})]
    )
    offer = Offer("passive", "passive", {"horn": 1.0}, time_per_unit=MINUTE)
    plan = SourcingPlan(draws=[Draw(offer, 1)])
    items = _slowest_passive_items(
        plan, {"horn": 100.0}, {}, {"horn": "Horn"}, production
    )
    assert [item[0] for item in items] == ["Horn"]


def test_slowest_passive_items_reports_a_whole_tie():
    # Orange and Lemon both quantize to the same daily boundary; naming just one
    # flapped run to run with the LP's alternate optima, so the whole tie is
    # returned, biggest quantity first.
    production = PassiveProduction(
        producers=[PassiveProducer("orchard", DAILY, {"orange": 100.0, "lemon": 100.0})]
    )
    offer = Offer(
        "passive", "passive",
        {"orange": 100.0 / 1440, "lemon": 100.0 / 1440},
        time_per_unit=timedelta(minutes=1),
    )
    plan = SourcingPlan(draws=[Draw(offer, 3600)])
    need = {"orange": 250.0, "lemon": 240.0}  # both need the day-3 deposit
    display = {"orange": "Orange", "lemon": "Lemon"}
    items = _slowest_passive_items(plan, need, {}, display, production)
    assert [(name, quantity) for name, quantity, _, _ in items] == [
        ("Orange", 250),
        ("Lemon", 240),
    ]
    assert all(wait == timedelta(days=3) for _, _, wait, _ in items)


def test_print_plan_critical_path_names_every_tied_item(capsys):
    production = PassiveProduction(
        producers=[PassiveProducer("orchard", DAILY, {"orange": 100.0, "lemon": 100.0})]
    )
    offer = Offer(
        "passive", "passive",
        {"orange": 100.0 / 1440, "lemon": 100.0 / 1440},
        time_per_unit=timedelta(minutes=1),
    )
    plan = SourcingPlan(draws=[Draw(offer, 3600)], time=timedelta(days=3))
    print_plan(
        plan, {"Orange": 250, "Lemon": 240}, Farm(), Workshop(), 1.0,
        production=production,
    )
    out = capsys.readouterr().out
    assert (
        "250x Orange and 240x Lemon from passive production (3d)"
        " — the slowest items to build up"
    ) in out


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
    # The unit is a whole cookie *day*, and the line says so -- "Cookie-boosted
    # Slow in 10d" read as if only the boosted window were farmed (issue: the
    # rest of the day looked skipped).
    assert "10 cookie-boosted days of Slow in 10d" in out
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
    assert "60.0 g" in out  # 1000 * 0.06


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
    assert "500x Large Net = 5.0 g" in out
    assert "nominal value = 5.0 g" in out
    # The surplus is byproduct already credited into net gold, not conjured on top:
    # a caption says so, so a big banked figure does not read as free money.
    assert "already credited into its net gold" in out
    assert "not gold conjured on top of it" in out

    print_plan(plan, {}, Farm(), workshop, 1.0)  # no prices -> no surplus section
    assert "Surplus" not in capsys.readouterr().out


def test_print_plan_surplus_excludes_already_held_inventory(capsys):
    # 300 Large Net already held (none of it consumed) plus 1,000 surplus Straw the
    # plan crafts into 500 more. Only the 500 the plan *accumulates* is surplus --
    # the held 300 is a pre-existing stockpile, not something executing the plan
    # banks, so it must not inflate the surplus value (which credits the net cost).
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
    assert "500x Large Net = 5.0 g" in out  # only the accumulated 500, not 800
    assert "nominal value = 5.0 g" in out
    assert "800x Large Net" not in out  # the held 300 is not folded into surplus


def test_print_frontier_splits_gold_into_buy_and_self_sourced(capsys):
    # With per-plan (to_buy, self_sourced) gold the row shows both subtotals and
    # their total, and the header flags the split.
    plans = [SourcingPlan(time=timedelta(0), arnold_palmers=1000, large_nets=0)]
    print_frontier(plans, 0, 0, None, _GOLD, golds=[(48.0, 12.0)])
    out = capsys.readouterr().out
    assert "gold, as buy + self-sourced" in out
    assert "-> 48.0 + 12.0 = 60.0 g" in out


def test_print_frontier_shows_banked_surplus_and_net_gold(capsys):
    # A plan that banks surplus adds "(bank B, net N)"; net is buy - bank, here below
    # zero (buys 12, banks 50 -> net -38), so the row shows the frontier past 0 g.
    plan = SourcingPlan(
        time=timedelta(0), arnold_palmers=1000, gold=12.0, surplus_gold=50.0
    )
    print_frontier([plan], 0, 0, None, _GOLD, golds=[(12.0, 48.0)])
    out = capsys.readouterr().out
    assert "-> 12.0 + 48.0 = 60.0 g (bank 50.0, net -38.0)" in out


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
    # The price lives with the other prices, in [trading] -- not under [mastery],
    # which only decides whether juice is drunk at all.
    assert resolve_pumpkin_juice(
        None, {"mastery": {"use_pumpkin_juice": 7}, "trading": {"pumpkin_juice": 200}}
    ) == (True, 7, 200.0)
    # Present-but-0 is free, as everywhere in [trading] -- not "off".
    assert resolve_pumpkin_juice(
        None, {"mastery": {"use_pumpkin_juice": True}, "trading": {"pumpkin_juice": 0}}
    ) == (True, None, 0.0)


def test_resolve_pumpkin_juice_rejects_the_old_price_key():
    # Ignoring it would silently reprice every plan at the 230 default.
    with pytest.raises(ValueError, match="trading.pumpkin_juice"):
        resolve_pumpkin_juice(None, {"mastery": {"pumpkin_juice_price": 500}})


# --- Crafting bonus (Resource Saver / tower-level gating) ---


def test_crafting_bonus_flag_defaults_to_none():
    # Resolved from [workshop]/[tower] by resolve_crafting_bonus, not baked
    # into the parser default -- see build_parser's --crafting-bonus help text.
    parser = build_parser()
    assert parser.parse_args([]).crafting_bonus is None
    assert parser.parse_args(["--crafting-bonus", "1.25"]).crafting_bonus == 1.25


def test_resolve_crafting_bonus_explicit_flag_wins():
    args = argparse.Namespace(crafting_bonus=1.25)
    resolve_crafting_bonus(args, {"workshop": {"resource_saver": 1}})
    assert args.crafting_bonus == 1.25


def test_resolve_crafting_bonus_none_resolves_from_config():
    args = argparse.Namespace(crafting_bonus=None)
    resolve_crafting_bonus(args, {"workshop": {"resource_saver": 1}})
    assert args.crafting_bonus == 1.10


def test_resolve_crafting_bonus_regression_no_workshop_or_tower_is_1_45():
    # A config with neither [workshop] nor [tower] must resolve to exactly
    # today's crafting.DEFAULT_CRAFTING_BONUS (1.45) -- byte-identical
    # behaviour to before tower-level gating existed.
    args = argparse.Namespace(crafting_bonus=None)
    resolve_crafting_bonus(args, {})
    assert args.crafting_bonus == 1.45


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


def test_resolve_swamp_puzzle_checks_inventory_by_default():
    held = Inventory(
        items={"swamp puzzle": InventoryItem(name="Swamp Puzzle", quantity=1)}
    )
    assert resolve_swamp_puzzle({}, held) is True
    assert resolve_swamp_puzzle({}, Inventory()) is False


def test_resolve_swamp_puzzle_defaults_on_with_no_inventory():
    # Unlike the other perks: no inventory to check (the --unobtainable report)
    # means "assume unlocked", so the report does not call the swamp's own drops
    # sourceless just because it read no inventory file.
    assert resolve_swamp_puzzle({}) is True


def test_resolve_swamp_puzzle_config_override_wins():
    held = Inventory(
        items={"swamp puzzle": InventoryItem(name="Swamp Puzzle", quantity=1)}
    )
    assert resolve_swamp_puzzle({"exploring": {"have_swamp_puzzle": False}}, held) is False
    assert (
        resolve_swamp_puzzle({"exploring": {"have_swamp_puzzle": True}}, Inventory())
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


HELD_WHEELBARROW = Inventory(
    items={
        "mariya's wheelbarrow": InventoryItem(name="Mariya's Wheelbarrow", quantity=1)
    }
)


def test_resolve_mariyas_wheelbarrow_checks_inventory_by_default():
    assert resolve_mariyas_wheelbarrow({}, HELD_WHEELBARROW) is True
    assert resolve_mariyas_wheelbarrow({}, Inventory()) is False
    # Nothing to check (the --unobtainable report) defaults to off.
    assert resolve_mariyas_wheelbarrow({}) is False


def test_resolve_mariyas_wheelbarrow_config_override_wins():
    off = {"cow_pasture": {"milk": 5000, "has_mariyas_wheelbarrow": False}}
    assert resolve_mariyas_wheelbarrow(off, HELD_WHEELBARROW) is False
    on = {"cow_pasture": {"milk": 5000, "has_mariyas_wheelbarrow": True}}
    assert resolve_mariyas_wheelbarrow(on, Inventory()) is True


def test_resolve_mariyas_wheelbarrow_leaves_the_config_alone():
    # It reports, it does not settle: passive_production takes the resolved bool
    # as an argument, so no caller depends on cfg having been rewritten.
    cfg = {"cow_pasture": {"milk": 5000}}
    resolve_mariyas_wheelbarrow(cfg, HELD_WHEELBARROW)
    assert cfg == {"cow_pasture": {"milk": 5000}}


def test_resolve_held_item_defaults_only_apply_without_an_inventory():
    # The shared body behind every held-item resolver: an explicit config value
    # beats both the inventory and the default, in either direction.
    cfg = {"sec": {"key": False}}
    assert resolve_held_item(cfg, "sec", "key", "Thing", HELD_WHEELBARROW) is False
    assert resolve_held_item({}, "sec", "key", "Thing", None) is False
    assert resolve_held_item({}, "sec", "key", "Thing", None, default=True) is True
    # With an inventory the default never applies -- absence means absent.
    assert resolve_held_item({}, "sec", "key", "Thing", Inventory(), default=True) is False



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


def test_pumpkin_juice_schedule_names_the_start_level():
    # Two juices start two of the LP's gains (1,000,000 / 1.1 per juice for an MM
    # item) below the goal, so drunk back to back the last lands the item on target
    # -- the start above the current 750,000 progress.
    gain = 0.1 * 1_000_000 / 1.1  # 90,909.09, matching pumpkin_juice_gains
    needs = [ItemNeed("Butter", have=750_000, target=1_000_000, is_mastery=True)]
    plan = SourcingPlan(pumpkin_juices={"butter": 2})
    assert pumpkin_juice_schedule(plan, needs, {"butter": gain}) == {
        "butter": round(1_000_000 - 2 * gain)
    }


def test_pumpkin_juice_schedule_clamps_to_current_progress():
    # Only ~40,000 of shortfall left, so target - gain (~909,091) sits below the
    # current 960,000 -- the drink point clamps up to where the item already is.
    gain = 0.1 * 1_000_000 / 1.1
    needs = [ItemNeed("Butter", have=960_000, target=1_000_000, is_mastery=True)]
    plan = SourcingPlan(pumpkin_juices={"butter": 1})
    assert pumpkin_juice_schedule(plan, needs, {"butter": gain}) == {"butter": 960_000}


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
        pumpkin_schedule={"butter": 700_000},
    )
    out = capsys.readouterr().out
    assert "Pumpkin Juice (mastery boost):" in out
    assert "3x Pumpkin Juice on Butter = 690.0 g" in out
    assert "start drinking at Butter mastery 700,000\n" in out
    assert "total 690.0 g" in out


def test_print_plan_shows_the_pumpkin_juice_section_when_juice_is_free(capsys):
    # At a trading.pumpkin_juice of 0 the juice costs nothing, and gating the section
    # on its gold hid the whole mastery boost -- from exactly the plans that lean on
    # juice hardest. The juices are what decide the section, not what they cost.
    demand = {"Butter": 500}
    plan = SourcingPlan(
        draws=[Draw(Offer("fish Pond", "fishing", {"butter": 100}, ln=1), 5)],
        pumpkin_juices={"butter": 3},
        pumpkin_gold=0.0,
        pumpkin_credit={"butter": 300},
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.0)
    out = capsys.readouterr().out
    assert "Pumpkin Juice (mastery boost):" in out
    assert "3x Pumpkin Juice on Butter = 0.0 g" in out
    assert "total 0.0 g" in out


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
    assert "total 1.0 g" in buy_section
    assert "total 690.0 g" in out


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
