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


def test_params_to_args_calendar_maps_to_model_or_average_flags():
    model = webapi.params_to_args({"calendar": "model"})
    assert (model.model_calendar, model.average_calendar) == (True, False)
    average = webapi.params_to_args({"calendar": "average"})
    assert (average.model_calendar, average.average_calendar) == (False, True)
    neither = webapi.params_to_args({})
    assert (neither.model_calendar, neither.average_calendar) == (False, False)


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


# --- fingerprint -------------------------------------------------------------


def test_fingerprint_is_stable_for_identical_inputs():
    args = webapi.params_to_args({"target": "150"})
    a = webapi.fingerprint("mastery text", "inventory text", args)
    b = webapi.fingerprint("mastery text", "inventory text", args)
    assert a == b


def test_fingerprint_is_invariant_under_refilter_field_changes():
    base = webapi.params_to_args({})
    changed = webapi.params_to_args(
        {"max_gold": "500", "max_ap": 100, "max_ln": 50, "passive_limit": "5d"}
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
