"""Tests for webapi.py -- plain pytest, no Django (see web/solverapp/tests.py)."""

import textwrap
from datetime import date, timedelta

import pytest

import main
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
        "mastery.pumpkin_juice_price=200.0",
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
        "start_date": {"start_date": "2026-01-01"},
        "calendar": {"calendar": "average"},
        "crafting_bonus": {"crafting_bonus": 2.0},
        "mastery_bonus": {"mastery_bonus": 2.0},
        "override_inventory": {"override_inventory": "Wood:100"},
        "overrides": {"overrides": ["sawmill.use_hickory_omelette=false"]},
        "price_arnold_palmer": {"price_arnold_palmer": 55},
        "instant_grow_time": {"instant_grow_time": "5s"},
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
    assert set(result.sections) == {"solution", "notes", "frontier", "plan", "crafts"}
    assert "Board" in result.sections["solution"]
    assert result.frontier_rows
    assert any(row["selected"] for row in result.frontier_rows)
