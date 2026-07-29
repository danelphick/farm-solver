"""Tests for webapi.py -- plain pytest, no Django (see web/solverapp/tests.py)."""

import textwrap
from datetime import date, timedelta
from pathlib import Path

import pytest

import main
import optimizer
import webapi
from crafting import Workshop
from crops import Farm
from exploring import Exploring
from farm_solver import GameState, ItemNeed, Solution, TowerRequirements
from fishing import Fishing
from locksmith import Locksmith
from optimizer import SourcingPlan

# --- params_to_args ---------------------------------------------------------


def test_params_to_args_defaults_match_the_cli_parser_defaults():
    args = webapi.params_to_args({})
    defaults = main.build_parser().parse_args([])
    assert args == defaults


def test_params_to_args_rejects_unknown_keys():
    with pytest.raises(ValueError, match="unknown param"):
        webapi.params_to_args({"bogus": 1})


def test_params_to_args_never_sets_cli_only_fields():
    args = webapi.params_to_args({"target": "150", "max_gold": "500"})
    assert args.solver is None
    assert args.format == "text"
    assert args.pager is False
    assert args.mastery_file is None
    assert args.inventory_file is None
    assert args.empty_mastery is False
    assert args.empty_inventory is False


def test_params_to_args_parses_target_and_max_gold():
    args = webapi.params_to_args({"target": "150", "max_gold": "unlimited"})
    assert args.target == 150
    assert args.max_gold is None  # "unlimited" -> no cap


def test_params_to_args_rejects_bad_target_as_value_error():
    # main.parse_target raises argparse.ArgumentTypeError; params_to_args wraps
    # it so every params_to_args failure is a plain ValueError.
    with pytest.raises(ValueError):
        webapi.params_to_args({"target": "not-a-level"})


def test_params_to_args_rejects_bad_max_gold_as_value_error():
    with pytest.raises(ValueError):
        webapi.params_to_args({"max_gold": "not-a-number"})


def test_params_to_args_parses_consumable_limits():
    args = webapi.params_to_args({"max_ap": 100, "max_ln": 50, "max_ac": 25})
    assert (args.max_ap, args.max_ln, args.max_ac) == (100, 50, 25)
    # An explicit null clears a limit (the client sends null to unset it).
    assert webapi.params_to_args({"max_ac": None}).max_ac is None


def test_params_to_args_spread_and_graph_round_trip():
    # "spread" parses through main._parse_spread -- same AXIS[=LO:HI] grammar
    # --spread accepts on the CLI -- into an optimizer.SpreadSpec; "graph" is
    # stored as-is (one of --graph's own choices).
    args = webapi.params_to_args({"spread": "pj=0:10", "graph": "auto"})
    assert args.spread == optimizer.SpreadSpec("pumpkin-juice", 0.0, 10.0)
    assert args.graph == "auto"

    bare = webapi.params_to_args({"spread": "time", "graph": "gold"})
    assert bare.spread == optimizer.SpreadSpec("time")
    assert bare.graph == "gold"


def test_params_to_args_spread_and_graph_empty_means_off():
    args = webapi.params_to_args({"spread": "", "graph": ""})
    assert args.spread is None
    assert args.graph is None
    assert webapi.params_to_args({}).spread is None
    assert webapi.params_to_args({}).graph is None


def test_params_to_args_rejects_bad_spread_as_value_error():
    # main._parse_spread raises argparse.ArgumentTypeError; params_to_args
    # wraps it the same way it wraps target/max_gold's own parsers.
    with pytest.raises(ValueError):
        webapi.params_to_args({"spread": "not-an-axis"})


def test_params_to_args_rejects_bad_graph_metric():
    with pytest.raises(ValueError):
        webapi.params_to_args({"graph": "sometimes"})


def test_params_to_args_validates_mastery_spec_but_stores_the_raw_string():
    args = webapi.params_to_args({"mastery": "Gold Bar:5000000"})
    assert args.mastery == "Gold Bar:5000000"  # raw string, re-parsed by build_inputs


def test_params_to_args_rejects_malformed_mastery_spec():
    with pytest.raises(ValueError):
        webapi.params_to_args({"mastery": "not-a-valid-spec"})


def test_params_to_args_use_pumpkin_juice_maps_none_and_unlimited_and_cap():
    assert webapi.params_to_args({}).use_pumpkin_juice is None
    assert webapi.params_to_args({"use_pumpkin_juice": -1}).use_pumpkin_juice == -1
    assert webapi.params_to_args({"use_pumpkin_juice": 5}).use_pumpkin_juice == 5


def test_params_to_args_start_date_parses_iso_format():
    args = webapi.params_to_args({"start_date": "2026-07-10"})
    assert args.start_date == date(2026, 7, 10)


def test_params_to_args_calendar_maps_to_the_three_flags():
    full = webapi.params_to_args({"calendar": "full"})
    assert (full.model_calendar, full.average_calendar, full.ignore_calendar) == (
        True, False, False,
    )
    # "model" is the pre-enum wire value for "full" (old saved client state).
    legacy = webapi.params_to_args({"calendar": "model"})
    assert legacy.model_calendar is True
    average = webapi.params_to_args({"calendar": "average"})
    assert (average.model_calendar, average.average_calendar) == (False, True)
    ignore = webapi.params_to_args({"calendar": "ignore"})
    assert (ignore.average_calendar, ignore.ignore_calendar) == (False, True)
    neither = webapi.params_to_args({})
    assert (
        neither.model_calendar, neither.average_calendar, neither.ignore_calendar
    ) == (False, False, False)


def test_fingerprint_changes_when_calendar_ignore_toggles():
    base = webapi.params_to_args({})
    ignore = webapi.params_to_args({"calendar": "ignore"})
    assert webapi.fingerprint("m", "i", base) != webapi.fingerprint("m", "i", ignore)


def test_params_to_args_rejects_bad_calendar_value():
    with pytest.raises(ValueError):
        webapi.params_to_args({"calendar": "sometimes"})


def test_params_to_args_plan_select_fastest_cheapest_index():
    fastest = webapi.params_to_args({"plan_select": {"kind": "fastest"}})
    assert fastest.fastest_plan is True
    cheapest = webapi.params_to_args({"plan_select": {"kind": "cheapest"}})
    assert cheapest.cheapest_plan is True
    indexed = webapi.params_to_args({"plan_select": {"kind": "index", "index": 3}})
    assert indexed.plan == 3


def test_params_to_args_rejects_bad_plan_select_kind():
    with pytest.raises(ValueError):
        webapi.params_to_args({"plan_select": {"kind": "random"}})


def test_params_to_args_overrides_validated_and_stored():
    args = webapi.params_to_args({"overrides": ["sawmill.use_hickory_omelette=false"]})
    assert args.override == ["sawmill.use_hickory_omelette=false"]


def test_params_to_args_rejects_malformed_override():
    with pytest.raises(ValueError):
        webapi.params_to_args({"overrides": ["not-a-dotted-key"]})


def test_params_to_args_trading_prices_become_config_overrides():
    args = webapi.params_to_args(
        {
            "price_arnold_palmer": 55,
            "price_apple_cider": 18,
            "price_orange_juice": 7,
            "price_large_net": 9,
            "price_cookie_set": 25,
            "price_pumpkin_juice": 200,
        }
    )
    assert args.override == [
        "trading.orange_juice=7.0",
        "trading.apple_cider=18.0",
        "trading.arnold_palmer=55.0",
        "trading.large_net=9.0",
        "trading.cookie_set=25.0",
        "trading.pumpkin_juice=200.0",
    ]


def test_params_to_args_instant_grow_time_becomes_a_config_override():
    args = webapi.params_to_args({"instant_grow_time": "5s"})
    assert args.override == ['farming.instant_grow_time="5s"']


def test_params_to_args_rejects_bad_instant_grow_time():
    with pytest.raises(ValueError):
        webapi.params_to_args({"instant_grow_time": "not-a-duration"})


def test_params_to_args_hickory_omelette_setting_and_price_become_overrides():
    args = webapi.params_to_args(
        {"hickory_omelette": "only_oak", "price_hickory_omelette": 350}
    )
    assert args.override == [
        "trading.hickory_omelette=350.0",
        'sawmill.hickory_omelette="only_oak"',
    ]


def test_params_to_args_rejects_bad_hickory_omelette_setting():
    with pytest.raises(ValueError):
        webapi.params_to_args({"hickory_omelette": "sometimes"})


def test_params_to_args_config_panel_becomes_typed_overrides():
    args = webapi.params_to_args(
        {
            "config": {
                "farming.rows": 15,
                "orchard.bonus": 1.4,
                "raptor_pen.have_antler_snare": False,
                "exploring.effectiveness.Small Cave": 110,
                "storehouse.updated_on": "2026-07-14",
            }
        }
    )
    assert args.override == [
        "farming.rows=15",
        "orchard.bonus=1.4",
        "raptor_pen.have_antler_snare=false",
        "exploring.effectiveness.Small Cave=110",
        'storehouse.updated_on="2026-07-14"',
    ]


def test_params_to_args_config_panel_follows_free_text_overrides():
    # A panel control (explicit) wins over a conflicting free-text line: both
    # land in args.override, the panel last, and apply_overrides is last-wins.
    args = webapi.params_to_args(
        {
            "overrides": ["farming.rows=1"],
            "config": {"farming.rows": 13},
        }
    )
    assert args.override == ["farming.rows=1", "farming.rows=13"]


def test_params_to_args_ap_trading_allow_becomes_trading_overrides():
    args = webapi.params_to_args(
        {"ap_trading": {"allow": ["red dye", "grapes"]}}
    )
    assert args.override == [
        "trading.ap.enabled=true",
        'trading.ap.allow=["red dye", "grapes"]',
        "trading.ap.deny=[]",
    ]


def test_params_to_args_ap_trading_empty_allow_disables_the_feature():
    # An empty allow list would be falsey in config.arnold_palmer_prices (falling
    # through to "trade everything"), so all-off turns the whole feature off.
    args = webapi.params_to_args({"ap_trading": {"allow": []}})
    assert args.override == ["trading.ap.enabled=false"]


def test_params_to_args_ap_trading_prices_replace_the_table():
    args = webapi.params_to_args(
        {"ap_trading": {"allow": ["red dye"], "prices": {"red dye": 9}}}
    )
    assert args.override == [
        "trading.ap.enabled=true",
        'trading.ap.allow=["red dye"]',
        "trading.ap.deny=[]",
        'trading.ap.prices={"red dye" = 9}',
    ]


def test_params_to_args_ap_trading_overrides_apply_cleanly_to_web_config():
    # The generated override lines parse and drive config.arnold_palmer_prices
    # (an allow list wins, deny is cleared so the two never collide).
    import config

    args = webapi.params_to_args(
        {"ap_trading": {"allow": ["acorn"], "prices": {"acorn": 8}}}
    )
    cfg = webapi.load_web_config()
    main.apply_overrides(cfg, args.override)
    assert config.arnold_palmer_prices(cfg) == {"acorn": 0.008}


def test_params_to_args_ap_trading_rejects_bad_shapes():
    with pytest.raises(ValueError):
        webapi.params_to_args({"ap_trading": [1, 2, 3]})
    with pytest.raises(ValueError):
        webapi.params_to_args({"ap_trading": {"allow": "red dye"}})
    with pytest.raises(ValueError):
        webapi.params_to_args({"ap_trading": {"allow": ["x"], "prices": {"x": "cheap"}}})


def test_params_to_args_rejects_non_object_config():
    with pytest.raises(ValueError):
        webapi.params_to_args({"config": [1, 2, 3]})


def test_params_to_args_rejects_config_key_that_is_not_dotted():
    with pytest.raises(ValueError):
        webapi.params_to_args({"config": {"rows": 15}})


def test_load_web_config_reads_the_web_default_file():
    # The web app's defaults come from web_default_config.toml, separate from
    # the developer's personal config.toml that the CLI uses.
    assert webapi.WEB_CONFIG_PATH.name == "web_default_config.toml"
    cfg = webapi.load_web_config()
    assert cfg["farming"]["rows"] == 13


def test_args_to_command_reconstructs_the_cli_invocation():
    args = webapi.params_to_args({
        "target": "150",
        "mastery": "Gold Bar:1000000",
        "max_gold": "500",
        "current_stamina": 2000,
        "calendar": "average",
        "config": {"farming.rows": 15, "orchard.have_tree_shaker": False},
        "price_arnold_palmer": 55,
    })
    command = webapi.args_to_command(args)
    assert command.startswith("python main.py --config web_default_config.toml")
    assert "--target 150" in command
    assert "-m 'Gold Bar:1000000'" in command  # shell-quoted
    assert "--max-gold 500" in command
    assert "--current-stamina 2000" in command
    assert "--average-calendar" in command
    assert "--override farming.rows=15" in command
    assert "--override orchard.have_tree_shaker=false" in command
    assert "--override trading.arnold_palmer=55.0" in command


def test_args_to_command_omits_defaults():
    # A bare request is just the program, the web config, and the target.
    command = webapi.args_to_command(webapi.params_to_args({"target": "tower"}))
    assert command == "python main.py --config web_default_config.toml --target tower"


def test_shell_quote_uses_ansi_c_escape_only_when_needed():
    # Apostrophe-free tokens keep shlex.quote's output (bare / single-quoted).
    assert webapi._shell_quote("plain") == "plain"
    assert webapi._shell_quote("Gold Bar:1") == "'Gold Bar:1'"
    # An embedded apostrophe switches to ANSI-C quoting with a \' escape.
    assert webapi._shell_quote("Cat's Eye:5") == "$'Cat\\'s Eye:5'"
    # A literal backslash in such a token is doubled (backslash is $'...''s escape).
    assert webapi._shell_quote("a'b\\c") == "$'a\\'b\\\\c'"


def test_args_to_command_includes_spread_and_graph():
    args = webapi.params_to_args(
        {"target": "tower", "spread": "pj=0:10", "graph": "gold"}
    )
    command = webapi.args_to_command(args)
    assert "--spread pumpkin-juice=0:10" in command
    assert "--graph gold" in command


def test_args_to_command_spread_without_range():
    args = webapi.params_to_args({"target": "tower", "spread": "passive"})
    command = webapi.args_to_command(args)
    assert "--spread passive" in command
    assert "passive=" not in command  # no range appended


def test_params_to_args_count_pumpkin_juice_gold_round_trip():
    # Absent / true -> args.price_pumpkin_juice None (config default); explicit
    # false -> off. (The param is named apart from the "price_pumpkin_juice" gold
    # price, which funnels to trading.pumpkin_juice.)
    assert webapi.params_to_args({}).price_pumpkin_juice is None
    off = webapi.params_to_args({"count_pumpkin_juice_gold": False})
    assert off.price_pumpkin_juice is False
    on = webapi.params_to_args({"count_pumpkin_juice_gold": True})
    assert on.price_pumpkin_juice is True


def test_args_to_command_emits_no_price_pumpkin_juice_only_when_off():
    off = webapi.params_to_args({"target": "tower", "count_pumpkin_juice_gold": False})
    assert "--no-price-pumpkin-juice" in webapi.args_to_command(off)
    # Default (counted) emits nothing -- the CLI default is on.
    on = webapi.params_to_args({"target": "tower"})
    assert "price-pumpkin-juice" not in webapi.args_to_command(on)


def test_self_sourced_param_maps_onto_the_flag_and_the_command():
    off = webapi.params_to_args({"target": "tower"})
    assert off.self_sourced is False
    assert "--self-sourced" not in webapi.args_to_command(off)
    on = webapi.params_to_args({"target": "tower", "self_sourced": True})
    assert on.self_sourced is True
    assert "--self-sourced" in webapi.args_to_command(on)


def test_args_to_command_points_at_captured_files():
    args = webapi.params_to_args({"target": "tower"})
    command = webapi.args_to_command(
        args, mastery_file=Path("/tmp/m.txt"), inventory_file=Path("/tmp/i.txt")
    )
    assert "--mastery-file /tmp/m.txt" in command
    assert "--inventory-file /tmp/i.txt" in command


def test_capture_debug_state_writes_pasted_text(monkeypatch, tmp_path):
    monkeypatch.setattr(webapi, "DEBUG_CAPTURE_DIR", tmp_path / "capture")
    mastery_path, inventory_path = webapi.capture_debug_state(
        "mastery lines", "inventory lines"
    )
    assert mastery_path.read_text(encoding="utf-8") == "mastery lines"
    assert inventory_path.read_text(encoding="utf-8") == "inventory lines"
    assert mastery_path.name.startswith("mastery-")
    assert inventory_path.name.startswith("inventory-")


def test_capture_debug_state_keeps_each_solve_under_a_fresh_name(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(webapi, "DEBUG_CAPTURE_DIR", tmp_path / "capture")
    # A stable stamp per call would collide; force two distinct timestamps.
    stamps = iter(["2026-07-21T10-00-00-000001", "2026-07-21T10-00-00-000002"])

    class _Clock:
        @staticmethod
        def now():
            class _T:
                @staticmethod
                def strftime(_fmt):
                    return next(stamps)

            return _T()

    monkeypatch.setattr(webapi, "datetime", _Clock)
    first_mastery, _ = webapi.capture_debug_state("older", "older inv")
    second_mastery, _ = webapi.capture_debug_state("newer", "newer inv")
    assert first_mastery != second_mastery
    # The earlier capture survives the later solve.
    assert first_mastery.read_text(encoding="utf-8") == "older"
    assert second_mastery.read_text(encoding="utf-8") == "newer"


def test_config_defaults_exposes_game_sections_without_solver():
    defaults = webapi.config_defaults()
    assert "solver" not in defaults  # withheld: holds server file paths
    # A representative value from each panel is present for placeholders.
    assert defaults["farming"]["rows"] == 13
    assert defaults["exploring"]["effectiveness"]["Forest"] == 136
    assert defaults["pets"]["Cat"] == 8


def test_params_to_args_config_knobs_follow_free_text_overrides():
    # An explicit control wins over a conflicting free-text line: both land in
    # args.override, the dedicated knob last, and apply_overrides is last-wins.
    args = webapi.params_to_args(
        {
            "overrides": ["trading.arnold_palmer=1"],
            "price_arnold_palmer": 60,
        }
    )
    assert args.override == [
        "trading.arnold_palmer=1",
        "trading.arnold_palmer=60.0",
    ]


# --- fingerprint -------------------------------------------------------------


def test_fingerprint_is_stable_for_identical_inputs():
    args = webapi.params_to_args({"target": "150"})
    a = webapi.fingerprint("mastery text", "inventory text", args)
    b = webapi.fingerprint("mastery text", "inventory text", args)
    assert a == b


def test_fingerprint_is_invariant_under_refilter_field_changes():
    base = webapi.params_to_args({})
    changed = webapi.params_to_args(
        {"max_gold": "500", "max_ap": 100, "max_ln": 50, "max_ac": 25, "passive_limit": "5d"}
    )
    assert webapi.fingerprint("m", "i", base) == webapi.fingerprint("m", "i", changed)


def test_fingerprint_changes_with_every_resolve_field():
    base_args = webapi.params_to_args({})
    base = webapi.fingerprint("m", "i", base_args)
    variants = {
        "target": {"target": "150"},
        "level": {"level": 42},
        "mastery": {"mastery": "Gold Bar:100"},
        "items": {"items": "Steel:100"},
        "use_pumpkin_juice": {"use_pumpkin_juice": 5},
        "current_stamina": {"current_stamina": 500},
        "cookie_sets": {"cookie_sets": 3},
        "plans": {"plans": 9},
        "spread": {"spread": "pj=0:10"},
        "graph": {"graph": "auto"},
        "start_date": {"start_date": "2026-01-01"},
        "calendar": {"calendar": "average"},
        "crafting_bonus": {"crafting_bonus": 2.0},
        "mastery_bonus": {"mastery_bonus": 2.0},
        "override_inventory": {"override_inventory": "Wood:100"},
        "overrides": {"overrides": ["sawmill.use_hickory_omelette=false"]},
        "price_arnold_palmer": {"price_arnold_palmer": 55},
        "instant_grow_time": {"instant_grow_time": "5s"},
        "self_sourced": {"self_sourced": True},
    }
    for name, params in variants.items():
        args = webapi.params_to_args(params)
        fp = webapi.fingerprint("m", "i", args)
        assert fp != base, f"{name} did not change the fingerprint"


def test_fingerprint_pastes_are_part_of_the_key():
    args = webapi.params_to_args({})
    a = webapi.fingerprint("mastery A", "inventory", args)
    b = webapi.fingerprint("mastery B", "inventory", args)
    assert a != b


def test_fingerprint_default_start_date_does_not_depend_on_wall_clock_identity():
    # Two Namespaces both leaving start_date unset (None) hash the same "today"
    # string rather than repr(None), so a solve and a same-day refilter that
    # never touched start_date share a cache key.
    args1 = webapi.params_to_args({})
    args2 = webapi.params_to_args({})
    assert args1.start_date is None and args2.start_date is None
    assert webapi.fingerprint("m", "i", args1) == webapi.fingerprint("m", "i", args2)


# --- mastery_costs_fingerprint -----------------------------------------------


def test_mastery_costs_fingerprint_is_namespaced_away_from_solve_keys():
    # Both artefacts share one cache (web/solverapp/cache.py), so the two key
    # spaces must not overlap.
    args = webapi.params_to_args({})
    key = webapi.mastery_costs_fingerprint("m", "i", args)
    assert key.startswith("mc:")
    assert key != webapi.fingerprint("m", "i", args)


def test_mastery_costs_fingerprint_ignores_the_goal_and_frontier_fields():
    # solve_mastery_costs solves its own per-item goals, so changing the solver
    # page's goal must not throw the cached table away.
    base = webapi.params_to_args({})
    changed = webapi.params_to_args({
        "target": "150", "mastery": "Gold Bar:100", "items": "Steel:100",
        "plans": 9, "spread": "pj=0:10", "graph": "auto",
        "use_pumpkin_juice": 5, "max_gold": "500",
    })
    assert webapi.mastery_costs_fingerprint(
        "m", "i", base
    ) == webapi.mastery_costs_fingerprint("m", "i", changed)


def test_mastery_costs_fingerprint_changes_with_every_field_it_depends_on():
    base_args = webapi.params_to_args({})
    base = webapi.mastery_costs_fingerprint("m", "i", base_args)
    variants = {
        "start_date": {"start_date": "2026-01-01"},
        "calendar": {"calendar": "average"},
        "crafting_bonus": {"crafting_bonus": 2.0},
        "mastery_bonus": {"mastery_bonus": 2.0},
        "current_stamina": {"current_stamina": 500},
        "cookie_sets": {"cookie_sets": 3},
        "override_inventory": {"override_inventory": "Wood:100"},
        "overrides": {"overrides": ["sawmill.use_hickory_omelette=false"]},
        "price_arnold_palmer": {"price_arnold_palmer": 55},
        "self_sourced": {"self_sourced": True},
    }
    for name, params in variants.items():
        args = webapi.params_to_args(params)
        fp = webapi.mastery_costs_fingerprint("m", "i", args)
        assert fp != base, f"{name} did not change the mastery-costs fingerprint"


def test_mastery_costs_fingerprint_pastes_are_part_of_the_key():
    args = webapi.params_to_args({})
    a = webapi.mastery_costs_fingerprint("mastery A", "inventory", args)
    b = webapi.mastery_costs_fingerprint("mastery B", "inventory", args)
    assert a != b


# --- render: refilter vs. horizon-escalation --------------------------------


def _minimal_inputs(model_calendar: bool) -> main.SolveInputs:
    return main.SolveInputs(
        time_limit=None,
        state=GameState(),
        requirements=TowerRequirements(),
        farm=Farm(),
        explore=Exploring(),
        fish=Fishing(),
        workshop=Workshop(),
        monthly_grants=False,
        solution=Solution(needs=[ItemNeed(item="Beet", have=0, target=10)]),
        recipes={},
        smith=Locksmith(),
        open_flows=[],
        demand={},
        inventory={},
        cookie_sets=0.0,
        prices=None,
        boost_offers=[],
        pj_gain={},
        pj_price=0.0,
        pj_limit=None,
        model_calendar=model_calendar,
        start=date(2026, 1, 1),
    )


def _session(model_calendar: bool) -> webapi.SolveSession:
    fast = SourcingPlan(time=timedelta(hours=1), gold=100.0)
    slow = SourcingPlan(time=timedelta(days=30), gold=0.0, surplus_gold=50.0)
    fr = main.FrontierResult(
        plans=[fast, slow], within=[fast, slow], shown=[fast, slow],
        offers=[], production=None, horizon=fast.time,
    )
    ctx = main.PlanContextData(
        cheapest=1, display={}, max_passive_residuals={}, reasons=[None, None],
        golds=None, sourceable_elsewhere=set(),
    )
    return webapi.SolveSession(
        inputs=_minimal_inputs(model_calendar), frontier=fr, ctx=ctx, cfg={},
    )


def test_render_pure_refilter_does_not_resolve():
    # Default selection (fastest) matches the cached frontier's converged
    # horizon exactly, so no re-solve is needed.
    session = _session(model_calendar=True)
    args = webapi.params_to_args({})
    result, new_session = webapi.render(session, args)
    assert result.resolved is False
    assert result.selected == 0
    assert len(result.frontier_rows) == 2
    assert result.frontier_rows[0]["selected"] is True
    assert result.frontier_rows[1]["cheapest"] is True
    assert new_session.frontier is session.frontier  # untouched, no re-solve


def test_render_refilter_recomputes_frontier_rows_for_a_gold_cap():
    session = _session(model_calendar=False)  # no calendar -> no horizon guard
    args = webapi.params_to_args({"max_gold": "50"})
    result, _ = webapi.render(session, args)
    # Only the fast (100g) plan is over the 50g cap; the slow plan alone survives
    # the refilter, so it is the only (and therefore selected) row shown.
    assert len(result.frontier_rows) == 1
    assert result.frontier_rows[0]["net_gold"] == pytest.approx(-50.0)


def test_render_horizon_guard_triggers_a_resolve_when_selection_disagrees(monkeypatch):
    session = _session(model_calendar=True)
    # Selecting the cheapest plan (the slow one, 30 days) lands far from the
    # cached horizon (the fast plan's 1 hour), so the guard should fire.
    args = webapi.params_to_args({"plan_select": {"kind": "cheapest"}})

    calls = []

    def fake_solve_frontier(inputs, call_args, cfg, horizon_seed=None):
        calls.append(horizon_seed)
        fast = SourcingPlan(time=timedelta(hours=1), gold=100.0)
        slow = SourcingPlan(time=timedelta(days=30), gold=0.0, surplus_gold=50.0)
        return main.FrontierResult(
            plans=[fast, slow], within=[fast, slow], shown=[fast, slow],
            offers=[], production=None, horizon=horizon_seed,
        )

    monkeypatch.setattr(main, "solve_frontier", fake_solve_frontier)
    result, new_session = webapi.render(session, args)

    assert calls == [timedelta(days=30)]  # seeded from the newly selected plan
    assert result.resolved is True
    assert new_session.frontier.horizon == timedelta(days=30)


# --- end-to-end solve() ------------------------------------------------------

_MASTERY_TEXT = "Mastery In-ProgressCopy Mastery Data\nStop Tracking All\n"

_INVENTORY_TEXT = textwrap.dedent(
    """\

    Items chevron_down

    Board
    The foundation of any good farm
    MAX ON HAND
    Mega Mastered
    49,420

    Iron
    A pressing need
    1,234
    """
)


def test_solve_and_render_end_to_end_on_synthetic_paste_text():
    args = webapi.params_to_args({"items": "Board:50000", "plans": 2})
    session = webapi.solve(_MASTERY_TEXT, _INVENTORY_TEXT, args)
    assert session.frontier.plans  # something to source (580 more Board needed)

    result, _ = webapi.render(session, args)
    # "graph" is always present (see report.SectionedReporter.__init__) but
    # renders empty unless a graph was requested -- this request asked for
    # neither spread nor graph, so it stays "".
    assert set(result.sections) == {
        "solution", "notes", "frontier", "plan", "crafts", "graph",
    }
    assert result.sections["graph"].strip() == ""
    assert "Board" in result.sections["solution"]
    assert result.frontier_rows
    assert any(row["selected"] for row in result.frontier_rows)
    # Widened per row even with no spread requested: axis_value falls back to
    # time_seconds (the default time-thinned frontier's own axis, see
    # webapi.render's graph_axis/graph_y_metric).
    for row in result.frontier_rows:
        assert row["axis_value"] == row["time_seconds"]
        assert row["secondary"] == row["net_gold"]


def test_solve_and_render_with_spread_and_graph_renders_the_impact_graph():
    # A mastery goal (not -i) is what earns Pumpkin Juice gains (see
    # main.pumpkin_juice_gains), so this is the "pj=0:3" spread's flagship use
    # case: --use-pumpkin-juice --spread pj=0:3 --graph.
    args = webapi.params_to_args({
        "mastery": "Board:100000",
        "use_pumpkin_juice": -1,
        "plans": 4,
        "spread": "pj=0:3",
        "graph": "auto",
    })
    assert args.spread == optimizer.SpreadSpec("pumpkin-juice", 0.0, 3.0)
    assert args.graph == "auto"

    session = webapi.solve(_MASTERY_TEXT, _INVENTORY_TEXT, args)
    assert session.frontier.plans

    result, _ = webapi.render(session, args)
    graph_html = result.sections["graph"]
    assert "<svg" in graph_html
    assert 'class="pt"' in graph_html

    assert result.frontier_rows
    for row in result.frontier_rows:
        # Spread by pumpkin-juice -> each row's axis_value is its juice count;
        # graph="auto" on a pj spread plots the *other* metric, net gold (see
        # optimizer.spread_graph_series's own "auto" resolution).
        assert row["axis_value"] == row["pumpkin_juices"]
        assert row["secondary"] == row["net_gold"]


# --- end-to-end mastery_costs() ----------------------------------------------


def test_mastery_costs_end_to_end_on_synthetic_paste_text():
    payload = webapi.mastery_costs(
        _MASTERY_TEXT, _INVENTORY_TEXT, webapi.params_to_args({})
    )
    assert payload.rows
    for row in payload.rows:
        assert set(row) == {"item", "tier", "time", "time_seconds", "gold"}
        assert row["tier"] in {"M", "GM", "MM"}
        assert isinstance(row["time"], str)  # preformatted for display
        assert isinstance(row["time_seconds"], float)  # and again, for sorting
        assert row["gold"] >= 0
    # Rows arrive in the default --sort time order (gold breaking ties), which
    # is what masteries.js's own sortRows reproduces client-side.
    keys = [(row["time_seconds"], row["gold"]) for row in payload.rows]
    assert keys == sorted(keys)
    # Every unmasterable entry is a formatted chain ending in a parenthesised
    # reason -- "(unobtainable)" only where unmodelled.csv has nothing better to
    # say, otherwise the real one ("(Cooking not modelled)", "(seasonal)").
    assert payload.unmasterable
    assert all(chain.endswith(")") for chain in payload.unmasterable)
    assert any("not modelled" in chain for chain in payload.unmasterable)


def test_mastery_costs_ignores_the_goal_params_it_is_handed():
    # The mastery-costs page forwards the solver page's saved params verbatim;
    # solve_mastery_costs solves its own per-item goals and never reads them, so
    # the table must come out identical either way (the CLI's --mastery-costs
    # conflict check has no counterpart on this path).
    plain = webapi.mastery_costs(
        _MASTERY_TEXT, _INVENTORY_TEXT, webapi.params_to_args({})
    )
    with_goals = webapi.mastery_costs(
        _MASTERY_TEXT,
        _INVENTORY_TEXT,
        webapi.params_to_args({"target": "150", "items": "Board:50000"}),
    )
    assert with_goals.rows == plain.rows
    assert with_goals.unmasterable == plain.unmasterable


def test_crafting_catalogue_inverts_the_recipe_graph():
    catalogue = webapi.crafting_catalogue()

    # `uses` answers "what consumes this?", the direction the timeline page's
    # filter walks -- Straw is a material of Twine, not the other way about.
    assert "twine" in catalogue["uses"]["straw"]
    assert "straw" not in catalogue["uses"].get("twine", [])
    # One step further along the same chain, which is what makes the client's
    # transitive walk (Straw -> Twine -> Rope) worth doing.
    assert "rope" in catalogue["uses"]["twine"]

    # Every key and every consumer is lower-cased, like the rest of the model,
    # with the display spelling carried separately.
    assert all(key == key.lower() for key in catalogue["uses"])
    assert all(
        item == item.lower()
        for consumers in catalogue["uses"].values()
        for item in consumers
    )
    assert catalogue["names"]["straw"] == "Straw"


def test_crafting_catalogue_keeps_the_free_materials():
    # crafting.IGNORED_MATERIALS are free to the solver, but a player asking
    # what their Iron could become still wants an answer.
    assert webapi.crafting_catalogue()["uses"]["iron"]


def test_crafting_catalogue_lists_every_zone_with_its_drops():
    zones = webapi.crafting_catalogue()["zones"]

    keyed = {(zone["kind"], zone["name"]): zone for zone in zones}
    assert keyed[("exploring", "Small Cave")]["items"][0] == "wood"
    assert "drum" in keyed[("fishing", "Small Pond")]["items"]

    # Listed once per (kind, name), however many rows the CSV files them under
    # (a zone with flagged alternate drop rates repeats its items).
    assert len(keyed) == len(zones)
    assert all(len(set(zone["items"])) == len(zone["items"]) for zone in zones)

    # A name can be BOTH an exploring location and a fishing zone, with
    # different drops -- so a zone is identified by kind and name together,
    # never by name alone.
    swamps = [zone for zone in zones if zone["name"] == "Sinking Swamp"]
    assert {zone["kind"] for zone in swamps} == {"exploring", "fishing"}
    assert swamps[0]["items"] != swamps[1]["items"]


def test_crafting_catalogue_names_everything_it_mentions():
    catalogue = webapi.crafting_catalogue()
    mentioned = set(catalogue["uses"])
    mentioned.update(item for items in catalogue["uses"].values() for item in items)
    mentioned.update(item for zone in catalogue["zones"] for item in zone["items"])
    assert not mentioned - set(catalogue["names"])
