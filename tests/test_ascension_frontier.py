"""Tests for main.py's ascension-mode orchestration (milestone 4).

Monkeypatch-style, like tests/test_main.py: real player/game data (fishing
zones, workshop recipes, ...) always loads unconditionally underneath
main._build_channel_setup, so tests that need precise, deterministic control
over which candidates are obtainable and at what cost patch
main._build_channel_setup/optimizer.all_offers/config.passive_production to
substitute a tiny, hand-built channel instead -- the same LP-construction
primitives tests/test_optimizer.py's ascension tests already exercise
directly, just driven through main.solve_ascension_frontier's real
orchestration (stage cfgs, the gold-weight sweep, pruning, evolution) rather
than a single minimize() call.
"""

from datetime import timedelta

import pytest

import ascension
import fishing
import main
import passive
import sources
from farm_solver import GameState, Mastery


def _synthetic_setup(
    recipes=None, open_flows=None, prices=None, inventory=None, cookie_sets=0.0
) -> main._ChannelSetup:
    """A minimal _ChannelSetup for a monkeypatched main._build_channel_setup.

    Only the fields main._run_ascend_pass actually reads (recipes, open_flows,
    inventory, cookie_sets, prices) matter; the rest (farm/fish/explore/...)
    are never touched once _build_channel_setup itself is replaced, so they
    are left as harmless placeholders.
    """
    return main._ChannelSetup(
        requirements=None,
        farm=None,
        explore=None,
        fish=None,
        workshop=None,
        monthly_grants=False,
        recipes=recipes or {},
        smith=None,
        open_flows=open_flows or [],
        inventory=dict(inventory or {}),
        cookie_sets=cookie_sets,
        prices=prices,
        boost_offers=[],
        model_calendar=False,
    )


def _patch_ascend_offers(monkeypatch, offers, **setup_kwargs):
    """Replace the config/game-data-driven half of solve_ascension_frontier's
    per-stage setup with a fixed, tiny offer list and synthetic channel setup,
    so a test drives the real sweep/evolution logic against a deterministic LP
    instead of the real fishing/exploring/workshop/passive-production data
    main._build_channel_setup would otherwise always pull in.
    """
    setup = _synthetic_setup(**setup_kwargs)
    monkeypatch.setattr(main, "_build_channel_setup", lambda *a, **kw: setup)
    monkeypatch.setattr(main.optimizer, "all_offers", lambda *a, **kw: list(offers))
    monkeypatch.setattr(
        main.config,
        "passive_production",
        lambda *a, **kw: passive.PassiveProduction(producers=[]),
    )


# --- 1. Stage configs apply rewards at the right boundary -------------------


def test_ascend_stage_cfgs_apply_the_fishing_trawl_reward_across_level_70():
    stage_cfgs = main._ascend_stage_cfgs(65, 75, {})
    assert len(stage_cfgs) == 2
    # Stage 0 (65->70) climbs FROM 65, so it owns rewards only through 65 --
    # the Fishing Trawl (level 70) isn't granted until stage 0 completes.
    assert stage_cfgs[0].get("fishing", {}).get("have_fishing_trawl") is not True
    assert stage_cfgs[1]["fishing"]["have_fishing_trawl"] is True
    # Same conclusion via the built Fishing objects, not just the raw cfg dict.
    fish0 = fishing.fishing_from_config(stage_cfgs[0].get("fishing", {}))
    fish1 = fishing.fishing_from_config(stage_cfgs[1].get("fishing", {}))
    assert fish0.modifiers.have_fishing_trawl is False
    assert fish1.modifiers.have_fishing_trawl is True


# --- 2. Starting AK is netted off the first stage's requirement -------------


def test_ascension_starting_ak_only_needs_the_remainder(monkeypatch):
    _patch_ascend_offers(monkeypatch, [])
    state = GameState(masteries={})
    args = main.build_parser().parse_args(
        ["--ascend", "22", "--tower-level", "20", "--starting-ak", "150", "--plans", "3"]
    )
    result = main.solve_ascension_frontier(args, {}, state)
    assert result.stage_bounds == [(20, 22)]  # no reward crossed between 20 and 22

    # 2 levels cost 200 AK; 150 already banked leaves only 50 -- with nothing
    # masterable (no offers at all), every plan waits purely on chores for
    # exactly that 50, not the full 200.
    expected_minutes = 50 / ascension.chore_rate_per_day() * 1440
    for plan in result.plans:
        stage = plan.stages[0]
        assert stage.plan.time.total_seconds() == pytest.approx(
            expected_minutes * 60, rel=1e-6
        )
        ledger = stage.plan.ascension.ak_from_steps + stage.plan.ascension.ak_from_chores
        assert ledger >= 50 - 1e-6


# --- 3. Frontier shape -------------------------------------------------------


def test_ascension_frontier_spans_distinct_endpoints_with_a_gold_tradeoff(monkeypatch):
    # Mastering "gizmo" needs ~90.9 units gathered (S = 1.1 * gathered >= 100);
    # each explore burns one Arnold Palmer, buyable at 1 gold/unit or
    # self-sourced 1000x slower -- a real fast-vs-slow, gold-vs-time lever.
    explore = sources.Offer(
        "explore for gizmo",
        "exploring",
        {"gizmo": 1},
        {"arnold palmer": 1},
        time_per_unit=timedelta(minutes=1),
    )
    gather_ap = sources.Offer(
        "gather ap", "farming", {"arnold palmer": 1}, time_per_unit=timedelta(minutes=1000)
    )
    _patch_ascend_offers(monkeypatch, [explore, gather_ap], prices={"arnold palmer": 1.0})

    state = GameState(masteries={"gizmo": Mastery("gizmo", progress=89900, required=100000)})
    args = main.build_parser().parse_args(["--ascend", "1", "--plans", "6"])
    result = main.solve_ascension_frontier(args, {}, state)

    assert len(result.plans) >= 2
    times = [p.time.total_seconds() for p in result.plans]
    assert times == sorted(times)  # fastest first
    net_golds = [p.cost[1] for p in result.plans]
    # Net gold is non-increasing along the (ascending-time) frontier.
    assert all(a >= b - 1e-6 for a, b in zip(net_golds, net_golds[1:]))
    # The two ends are genuinely distinct: buying freely finishes in about the
    # ~91 explore-minutes; buying nothing waits on ~91 self-sourced APs at
    # 1000 min each (a two-orders-of-magnitude gap).
    assert result.plans[0].time < result.plans[-1].time
    assert result.plans[0].cost[1] > result.plans[-1].cost[1]


# --- 4. State evolution ------------------------------------------------------


def test_ascension_state_evolution_drops_a_mastered_item_from_the_next_stage(monkeypatch):
    # Free, instant gathering: cheap enough that every pass masters "gizmo" in
    # stage 0 (it strictly shortens the chore wait at no cost).
    gizmo = sources.Offer("gather gizmo", "farming", {"gizmo": 1}, time_per_unit=timedelta(seconds=1))
    _patch_ascend_offers(monkeypatch, [gizmo])
    state = GameState(masteries={"gizmo": Mastery("gizmo", progress=9990, required=10000)})
    args = main.build_parser().parse_args(
        ["--ascend", "75", "--tower-level", "65", "--plans", "3"]
    )
    result = main.solve_ascension_frontier(args, {}, state)
    assert len(result.stage_bounds) == 2
    assert result.plans
    for plan in result.plans:
        stage0, stage1 = plan.stages
        assert "gizmo" in stage0.plan.ascension.masters
        # Fully mastered by advance(): no "master" step remains a candidate.
        assert "gizmo" not in stage1.plan.ascension.masters


# --- 5. Validation ------------------------------------------------------------


def test_validate_ascend_conflicts_with_goal_only_flags():
    parser = main.build_parser()
    with pytest.raises(ValueError, match="--target"):
        main._validate_ascend(parser.parse_args(["--ascend", "50", "--target", "40"]), {})
    with pytest.raises(ValueError, match="-m/--mastery"):
        main._validate_ascend(parser.parse_args(["--ascend", "50", "-m", "Board:1"]), {})
    with pytest.raises(ValueError, match="-i/--items"):
        main._validate_ascend(parser.parse_args(["--ascend", "50", "-i", "Board:1"]), {})
    with pytest.raises(ValueError, match="--use-pumpkin-juice"):
        main._validate_ascend(
            parser.parse_args(["--ascend", "50", "--use-pumpkin-juice"]), {}
        )


def test_validate_ascend_rejects_targets_out_of_range():
    parser = main.build_parser()
    with pytest.raises(ValueError):
        main._validate_ascend(parser.parse_args(["--ascend", "250"]), {})
    with pytest.raises(ValueError, match="megamastery"):
        main._validate_ascend(parser.parse_args(["--ascend", "101"]), {})
    with pytest.raises(ValueError):
        main._validate_ascend(
            parser.parse_args(["--ascend", "10", "--tower-level", "10"]), {}
        )


def test_validate_ascend_missing_tower_level_defaults_to_zero():
    parser = main.build_parser()
    current, target, starting_ak = main._validate_ascend(
        parser.parse_args(["--ascend", "5"]), {}
    )
    assert (current, target, starting_ak) == (0, 5, 0)


def test_ascend_with_no_tower_level_works_from_a_fresh_account(monkeypatch):
    _patch_ascend_offers(monkeypatch, [])
    state = GameState(masteries={})
    args = main.build_parser().parse_args(["--ascend", "1"])
    result = main.solve_ascension_frontier(args, {}, state)
    assert result.stage_bounds == [(0, 1)]
    assert result.plans


# --- 6. Chore-only when candidates are absurdly expensive --------------------


def test_ascension_waits_on_chores_when_the_only_candidate_is_absurdly_dear(monkeypatch):
    gizmo = sources.Offer("gather gizmo", "farming", {"gizmo": 1}, time_per_unit=timedelta(days=1))
    _patch_ascend_offers(monkeypatch, [gizmo])
    state = GameState(masteries={"gizmo": Mastery("gizmo", progress=0, required=10000)})
    args = main.build_parser().parse_args(
        ["--ascend", "21", "--tower-level", "20", "--plans", "3"]
    )
    result = main.solve_ascension_frontier(args, {}, state)
    assert result.stage_bounds == [(20, 21)]

    fastest = result.plans[0]
    expected_minutes = ascension.AK_PER_LEVEL / ascension.chore_rate_per_day() * 1440
    assert fastest.time.total_seconds() == pytest.approx(expected_minutes * 60, rel=1e-6)
    stage = fastest.stages[0]
    assert stage.plan.ascension.masters == []
    assert stage.plan.ascension.grandmasters == []


# --- 7. Carryover ledger across a multi-stage run ----------------------------


def test_ascension_carryover_ledger_covers_every_level_crossed(monkeypatch):
    gizmo = sources.Offer("gather gizmo", "farming", {"gizmo": 1}, time_per_unit=timedelta(seconds=1))
    _patch_ascend_offers(monkeypatch, [gizmo])
    state = GameState(masteries={"gizmo": Mastery("gizmo", progress=9990, required=10000)})
    starting_ak = 30
    args = main.build_parser().parse_args(
        [
            "--ascend", "75", "--tower-level", "65",
            "--starting-ak", str(starting_ak), "--plans", "4",
        ]
    )
    result = main.solve_ascension_frontier(args, {}, state)
    assert len(result.stage_bounds) == 2
    needed = ascension.AK_PER_LEVEL * (75 - 65)
    for plan in result.plans:
        earned = sum(
            stage.plan.ascension.ak_from_steps + stage.plan.ascension.ak_from_chores
            for stage in plan.stages
        )
        assert earned + starting_ak >= needed - 1e-6
