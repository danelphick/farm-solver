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


def _patch_ascend_offers(monkeypatch, offers, catalogue=None, **setup_kwargs):
    """Replace the config/game-data-driven half of solve_ascension_frontier's
    per-stage setup with a fixed, tiny offer list and synthetic channel setup,
    so a test drives the real sweep/evolution logic against a deterministic LP
    instead of the real fishing/exploring/workshop/passive-production data
    main._build_channel_setup would otherwise always pull in. The masterable
    catalogue is pinned too (``catalogue`` -- a ``{key: display}`` dict,
    default empty), so the shipped mastery_items.txt's ~500 items can't flood
    a tiny synthetic LP with progress-0 candidates.
    """
    setup = _synthetic_setup(**setup_kwargs)
    monkeypatch.setattr(main, "_build_channel_setup", lambda *a, **kw: setup)
    monkeypatch.setattr(main.optimizer, "all_offers", lambda *a, **kw: list(offers))
    monkeypatch.setattr(
        main, "load_mastery_catalogue", lambda path=None: dict(catalogue or {})
    )
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
    empty = GameState(masteries={})
    with pytest.raises(ValueError, match="--target"):
        main._validate_ascend(
            parser.parse_args(["--ascend", "50", "--target", "40"]), {}, empty
        )
    with pytest.raises(ValueError, match="-m/--mastery"):
        main._validate_ascend(parser.parse_args(["--ascend", "50", "-m", "Board:1"]), {}, empty)
    with pytest.raises(ValueError, match="-i/--items"):
        main._validate_ascend(parser.parse_args(["--ascend", "50", "-i", "Board:1"]), {}, empty)
    with pytest.raises(ValueError, match="--use-pumpkin-juice"):
        main._validate_ascend(
            parser.parse_args(["--ascend", "50", "--use-pumpkin-juice"]), {}, empty
        )


def test_validate_ascend_rejects_targets_out_of_range():
    parser = main.build_parser()
    empty = GameState(masteries={})
    with pytest.raises(ValueError):
        main._validate_ascend(parser.parse_args(["--ascend", "250"]), {}, empty)
    with pytest.raises(ValueError):
        main._validate_ascend(
            parser.parse_args(["--ascend", "10", "--tower-level", "10"]), {}, empty
        )


def test_validate_ascend_missing_tower_level_defaults_to_zero():
    parser = main.build_parser()
    current, target, starting_ak = main._validate_ascend(
        parser.parse_args(["--ascend", "5"]), {}, GameState(masteries={})
    )
    assert (current, target, starting_ak) == (0, 5, 0)


def test_validate_ascend_now_allows_targets_past_100_with_enough_mega_candidates():
    # The old target>100 guard is gone (milestone 6): --ascend 101 now validates
    # fine given a state with at least one item that has a mega step available
    # (megas_required_by(101) == 1).
    parser = main.build_parser()
    state = GameState(masteries={"gizmo": Mastery("gizmo", progress=500_000, required=1_000_000)})
    current, target, starting_ak = main._validate_ascend(
        parser.parse_args(["--ascend", "101"]), {}, state
    )
    assert (current, target) == (0, 101)


def test_validate_ascend_rejects_target_past_100_with_no_mega_candidates(
    monkeypatch, tmp_path
):
    # No catalogue AND an empty paste: nothing can ever reach a mega. (With
    # the shipped catalogue this state has ~500 progress-0 mega candidates,
    # so the bound never trips -- that's the fresh-account feature.)
    monkeypatch.setattr(main, "MASTERY_CATALOGUE_PATH", tmp_path / "absent.txt")
    parser = main.build_parser()
    with pytest.raises(ValueError, match="not enough megamasterable items"):
        main._validate_ascend(
            parser.parse_args(["--ascend", "102"]), {}, GameState(masteries={})
        )


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


# --- 8. Megamastery scheduling (milestone 6, levels 101-200) -----------------


def test_ascend_100_to_105_schedules_and_completes_one_new_mega(monkeypatch):
    # megas_required_by(105) == 2; one item is already fully mega'd
    # (mega_count == 1), leaving exactly one NEW mega needed -- "gizmo", parked
    # just short of its own mega threshold so the synthetic offer only has to
    # produce a small top-up.
    gizmo_offer = sources.Offer(
        "gather gizmo", "farming", {"gizmo": 1}, time_per_unit=timedelta(microseconds=1),
    )
    _patch_ascend_offers(monkeypatch, [gizmo_offer])
    state = GameState(
        masteries={
            "gizmo": Mastery("gizmo", progress=999_000, required=1_000_000),
            "owned": Mastery("owned", progress=0, required=None),  # already mega'd
        }
    )
    args = main.build_parser().parse_args(
        ["--ascend", "105", "--tower-level", "100", "--plans", "3"]
    )
    result = main.solve_ascension_frontier(args, {}, state)
    assert result.stage_bounds == [(100, 105)]  # no reward threshold crossed
    assert result.plans

    for plan in result.plans:
        # A single stage: due immediately, so the new mega completes right there.
        stage = plan.stages[0]
        assert stage.plan.ascension.megas == ["gizmo"]
        assert stage.plan.ascension.mastery_supply["gizmo"] >= 1_000 - 1e-6

        # Ledger reconciles: starting + steps + chores == required + carry_out.
        steps = sum(s.plan.ascension.ak_from_steps for s in plan.stages)
        chores = sum(s.plan.ascension.ak_from_chores for s in plan.stages)
        required = ascension.AK_PER_LEVEL * (105 - 100)
        assert 0 + steps + chores == pytest.approx(required + plan.carry_out, abs=1e-6)


def test_ascend_past_100_with_no_megamasterable_items_raises_clearly(monkeypatch):
    _patch_ascend_offers(monkeypatch, [])
    state = GameState(masteries={})
    args = main.build_parser().parse_args(["--ascend", "102", "--tower-level", "100"])
    with pytest.raises(ValueError, match="not enough megamasterable items"):
        main.solve_ascension_frontier(args, {}, state)


def test_select_megas_returns_empty_when_no_mega_is_needed():
    setup = _synthetic_setup()
    state = GameState(masteries={})
    chosen, unmasterable = main._select_megas(
        state, current=0, target=50, starting_ak=0,
        args=main.build_parser().parse_args(["--ascend", "50"]),
        last_crafting_bonus=1.45, last_setup=setup, last_offers=[],
    )
    assert chosen == []
    assert unmasterable == set()


def test_select_megas_picks_the_cheapest_candidate(monkeypatch):
    cheap = sources.Offer(
        "gather cheap", "farming", {"cheap": 1}, time_per_unit=timedelta(minutes=1)
    )
    expensive = sources.Offer(
        "gather expensive", "farming", {"expensive": 1}, time_per_unit=timedelta(minutes=100)
    )
    setup = _synthetic_setup()
    state = GameState(
        masteries={
            "cheap": Mastery("cheap", progress=900_000, required=1_000_000),
            "expensive": Mastery("expensive", progress=900_000, required=1_000_000),
        }
    )
    chosen, unmasterable = main._select_megas(
        state, current=100, target=104, starting_ak=0,
        args=main.build_parser().parse_args(["--ascend", "104", "--tower-level", "100"]),
        last_crafting_bonus=1.45, last_setup=setup, last_offers=[cheap, expensive],
    )
    assert chosen == ["cheap"]
    assert unmasterable == set()


# --- 9. AK ledger: a mega item's master/GM steps count exactly once ----------


def test_mega_item_master_and_grandmaster_ak_is_counted_exactly_once_across_stages(
    monkeypatch,
):
    """Drives _run_ascend_pass directly (not the full frontier) over two
    synthetic stages, with a mega floor forcing "gizmo"'s production to spread
    across both -- stage 0's own production alone crosses the master AND
    grandmaster thresholds (so the LP has real incentive to select both,
    cheaply, since the floor already dominates their deltas -- see
    optimizer.py's "Ascension extension"/mega-floor interplay note), while the
    mega itself is only due (and only completes) at stage 1. The AK ledger
    must count the master/GM steps' 10+100 AK exactly once total, not once per
    stage (structurally guaranteed by candidates() dropping a completed step
    from later stages -- this test is the regression net for that guarantee).
    """
    import config
    import passive as passive_mod

    gizmo_offer = sources.Offer(
        "gather gizmo", "farming", {"gizmo": 1000}, time_per_unit=timedelta(seconds=1),
    )
    setup = _synthetic_setup()
    # Stage 0 ends AT 100 (megas_required_by(100) == 0 -- the per-stage
    # megas_required safety net, computed straight off megas_required_by
    # independent of the schedule, must NOT already force the mega at stage 0);
    # stage 1 ends at 101 (megas_required_by(101) == 1, matching the schedule's
    # due=1 below).
    bounds = [(99, 100), (100, 101)]
    stage_setups = [setup, setup]
    stage_offers = [[gizmo_offer], [gizmo_offer]]
    stage_crafting_bonus = [1.0, 1.0]
    quantize = config.passive_quantizer(passive_mod.PassiveProduction(producers=[]))
    stage_quantize = [quantize, quantize]
    stage_production = [None, None]

    state = GameState(masteries={"gizmo": Mastery("gizmo", progress=0, required=10_000)})
    schedule = {"gizmo": 1}  # due at stage index 1 (the second/last stage)
    args = main.build_parser().parse_args(["--ascend", "101", "--tower-level", "99"])

    staged, _ = main._run_ascend_pass(
        stage_crafting_bonus, stage_setups, stage_offers, stage_quantize,
        stage_production, bounds, state, args, starting_ak=0, gold_weight=1e-6,
        mega_schedule=schedule,
    )
    assert len(staged.stages) == 2
    stage0, stage1 = staged.stages

    # Master/grandmaster only ever appear as candidates before the item
    # crosses those thresholds -- structurally, they can be selected in at
    # most one stage each.
    master_stages = [s for s in staged.stages if "gizmo" in s.plan.ascension.masters]
    gm_stages = [s for s in staged.stages if "gizmo" in s.plan.ascension.grandmasters]
    assert len(master_stages) <= 1
    assert len(gm_stages) <= 1

    # The mega itself completes at its due stage (index 1) only.
    assert "gizmo" not in stage0.plan.ascension.megas
    assert stage1.plan.ascension.megas == ["gizmo"]

    total_step_ak = sum(s.plan.ascension.ak_from_steps for s in staged.stages)
    # Whatever mix of stages earned the master (+10) and grandmaster (+100)
    # steps, the total across the whole pass reflects each exactly once --
    # never 0 (silently dropped) or 220 (double-counted).
    if master_stages and gm_stages:
        assert total_step_ak == pytest.approx(110)
    elif master_stages or gm_stages:
        assert total_step_ak in (pytest.approx(10), pytest.approx(100))
    # (If neither ever got selected the AK came entirely from chores instead --
    # still a single, non-duplicated source; the ledger check below covers it.)

    # Ledger reconciles across the whole (2-level) pass.
    steps = sum(s.plan.ascension.ak_from_steps for s in staged.stages)
    chores = sum(s.plan.ascension.ak_from_chores for s in staged.stages)
    required = ascension.AK_PER_LEVEL * (101 - 99)
    assert 0 + steps + chores == pytest.approx(required + staged.carry_out, abs=1e-6)


# --- The masterable catalogue is the candidate universe ----------------------


def test_empty_mastery_paste_still_masters_catalogue_items(monkeypatch):
    # An empty paste used to leave zero candidates -- the only "plan" was a
    # pure chore wait (100 AK at ~2.61/day: over a month for one level). The
    # catalogue supplies every masterable item at progress 0 instead.
    grow = sources.Offer(
        "grow gizmo", "farming", {"gizmo": 100.0},
        time_per_unit=timedelta(minutes=1),
    )
    _patch_ascend_offers(monkeypatch, [grow], catalogue={"gizmo": "Gizmo"})
    args = main.build_parser().parse_args(["--ascend", "1"])
    result = main.solve_ascension_frontier(args, {}, GameState(masteries={}))
    fastest = result.plans[0]
    outcome = fastest.stages[0].plan.ascension
    assert outcome.masters == ["gizmo"]
    # Far faster than the ~38-day chore-only wait 100 AK would otherwise take.
    assert fastest.time < timedelta(days=2)
    assert result.unexpected_masteries == []


def test_paste_items_missing_from_the_catalogue_are_reported(monkeypatch):
    grow = sources.Offer(
        "grow gizmo", "farming", {"gizmo": 100.0},
        time_per_unit=timedelta(minutes=1),
    )
    _patch_ascend_offers(monkeypatch, [grow], catalogue={"gizmo": "Gizmo"})
    state = GameState(
        masteries={
            "gizmo": Mastery(item="Gizmo", progress=0, required=10),
            "mystery doodad": Mastery(item="Mystery Doodad", progress=5, required=10),
        }
    )
    args = main.build_parser().parse_args(["--ascend", "1"])
    result = main.solve_ascension_frontier(args, {}, state)
    # Display-cased, and only the item the catalogue doesn't know.
    assert result.unexpected_masteries == ["Mystery Doodad"]


def test_no_catalogue_disables_the_unexpected_check(monkeypatch):
    grow = sources.Offer(
        "grow gizmo", "farming", {"gizmo": 100.0},
        time_per_unit=timedelta(minutes=1),
    )
    _patch_ascend_offers(monkeypatch, [grow], catalogue={})
    state = GameState(
        masteries={"gizmo": Mastery(item="Gizmo", progress=0, required=10)}
    )
    args = main.build_parser().parse_args(["--ascend", "1"])
    result = main.solve_ascension_frontier(args, {}, state)
    assert result.unexpected_masteries == []


def test_load_mastery_catalogue_missing_file_is_empty(tmp_path):
    assert main.load_mastery_catalogue(tmp_path / "absent.txt") == {}
    listing = tmp_path / "mastery_items.txt"
    listing.write_text("Apple\n\n  Zebra Fruit\n")
    assert main.load_mastery_catalogue(listing) == {
        "apple": "Apple", "zebra fruit": "Zebra Fruit",
    }
