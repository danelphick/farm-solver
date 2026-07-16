"""Tests for report.py's ascension-mode sections (milestone 5).

Drives main.solve_ascension_frontier through the same monkeypatch technique as
tests/test_ascension_frontier.py (real game data always loads underneath
main._build_channel_setup, so a test that needs precise control over what's
masterable and at what cost replaces it, optimizer.all_offers and
config.passive_production with tiny, hand-built stand-ins), then feeds the
result through report.py's ascension_goal/ascension_frontier/ascension_plan --
the actual code path main.run's --ascend branch drives.
"""

from datetime import date, timedelta

import pytest

import crafting
import main
import passive
import report
import sources
from farm_solver import GameState, Mastery


def _synthetic_setup() -> main._ChannelSetup:
    """A minimal _ChannelSetup good enough for a full report render: a real
    (empty) Workshop -- report.print_plan/markdown_plan call workshop.recipes,
    so unlike tests/test_ascension_frontier.py's stand-in this can't be None --
    and otherwise-harmless placeholders for the rest.
    """
    return main._ChannelSetup(
        requirements=None,
        farm=None,
        explore=None,
        fish=None,
        workshop=crafting.Workshop(),
        monthly_grants=False,
        recipes={},
        smith=None,
        open_flows=[],
        inventory={},
        cookie_sets=0.0,
        prices=None,
        boost_offers=[],
        model_calendar=False,
    )


def _patch_ascend_stages(monkeypatch, offers_per_stage: list[list[sources.Offer]]) -> None:
    """Fix each of solve_ascension_frontier's stages -- in stage_bounds order --
    to its own tiny offer list, so a test can make one stage cheap (production-
    bound) and another absurdly dear (chore-bound). optimizer.all_offers is
    called exactly once per stage, in order, building the pass-invariant
    per-stage setup before the gold-weight sweep -- see
    solve_ascension_frontier's "static setup" note -- so an iterator over the
    per-stage lists lines up correctly.
    """
    setup = _synthetic_setup()
    monkeypatch.setattr(main, "_build_channel_setup", lambda *a, **kw: setup)
    calls = iter(list(offers_per_stage))
    monkeypatch.setattr(
        main.optimizer, "all_offers", lambda *a, **kw: list(next(calls))
    )
    monkeypatch.setattr(
        main.config,
        "passive_production",
        lambda *a, **kw: passive.PassiveProduction(producers=[]),
    )


def _widget_offers(n: int) -> list[sources.Offer]:
    """``n`` near-instant gather offers, one per distinct "widgetK" item -- cheap
    enough that mastering (and grandmastering) several of them costs a plan
    only microseconds, so the AK they earn beats waiting on chores outright.
    """
    return [
        sources.Offer(
            f"gather widget{i}", "farming", {f"widget{i}": 1},
            time_per_unit=timedelta(microseconds=1),
        )
        for i in range(n)
    ]


def _dear_offers() -> list[sources.Offer]:
    """One absurdly expensive gather offer -- mastering it would take far
    longer than simply waiting on daily chores, so a plan sourced from only
    this candidate waits purely on chores (see
    tests/test_ascension_frontier.py's matching "absurdly dear" test).
    """
    return [
        sources.Offer(
            "gather gadget", "farming", {"gadget": 1},
            time_per_unit=timedelta(days=1),
        )
    ]


def _two_stage_state() -> GameState:
    """6 widgets (all masterable/grandmasterable from progress 0) for stage 1,
    plus "gadget" (also from progress 0) for stage 2's chore-only candidate.
    """
    masteries = {
        f"widget{i}": Mastery(f"widget{i}", progress=0, required=10_000)
        for i in range(6)
    }
    masteries["gadget"] = Mastery("gadget", progress=0, required=10_000)
    return GameState(masteries=masteries)


def _solve_two_stage_climb(monkeypatch) -> main.AscensionFrontierResult:
    """current=65, target=75: stage_bounds crosses level 70 (Fishing Trawl,
    see ascension.REWARD_LEVELS), matching test_ascension_frontier.py's own
    stage-boundary test. Stage 1 (65->70, needs 500 AK) is cheaply covered by
    the widgets, so it's production-bound; stage 2 (70->75, needs up to 500 AK
    net of whatever stage 1's grandmastering overshot into carry) only has the
    absurdly dear "gadget" candidate, so it waits on chores.
    """
    _patch_ascend_stages(monkeypatch, [_widget_offers(6), _dear_offers()])
    state = _two_stage_state()
    args = main.build_parser().parse_args(
        ["--ascend", "75", "--tower-level", "65", "--plans", "3"]
    )
    return main.solve_ascension_frontier(args, {}, state)


def _render(result: main.AscensionFrontierResult, current: int, target: int,
            starting_ak: int, selected: int = 0) -> tuple[str, str]:
    """The ascension report as (text, markdown) -- both driven through the same
    Reporter facade main.run's --ascend branch uses, so a test comparing them
    exercises the exact production code path.
    """
    cheapest = main.cheapest_plan(result.plans)
    start = date(2026, 1, 1)

    text_reporter = report.build_reporter("text")
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        text_reporter.ascension_goal(current, target, starting_ak, result.unmasterable)
        text_reporter.ascension_frontier(result, selected, cheapest)
        text_reporter.ascension_plan(result, selected, current, target, starting_ak, start)
    text = buf.getvalue()

    md_reporter = report.build_reporter("markdown")
    md_reporter.ascension_goal(current, target, starting_ak, result.unmasterable)
    md_reporter.ascension_frontier(result, selected, cheapest)
    md_reporter.ascension_plan(result, selected, current, target, starting_ak, start)
    markdown = md_reporter.doc.render()

    return text, markdown


# --- 1. Markdown: Goal header, frontier table, stage headings, to-do, ledger --


def test_markdown_ascension_report_end_to_end(monkeypatch):
    result = _solve_two_stage_climb(monkeypatch)
    assert result.plans
    _, markdown = _render(result, current=65, target=75, starting_ak=0)

    assert "Goal: Climb tower from 65 to 75" in markdown
    assert "needs 1,000 AK total" in markdown
    assert "0 AK already banked" in markdown
    assert "1,000 AK net" in markdown

    assert "| # | Time | Gold | Net gold | Masters | GMs | Megas | Chore AK | Notes |" in markdown

    assert "Stage 1 — level 65 → 70 (reward at 70: Fishing Trawl)" in markdown
    # Stage 2 crosses no reward threshold (75 isn't a REWARD_LEVELS key).
    assert "Stage 2 — level 70 → 75" in markdown
    assert "Stage 2 — level 70 → 75 (reward at" not in markdown

    # Every widget grandmastered in stage 1 shows up somewhere in the to-do table.
    chosen = result.plans[0]
    outcome0 = chosen.stages[0].plan.ascension
    assert outcome0.grandmasters  # the scenario is built so >=1 gets grandmastered
    for item in outcome0.grandmasters:
        assert item.title() in markdown

    # Ledger reconciliation: starting + steps + chores == required + carry_out.
    steps = sum(s.plan.ascension.ak_from_steps for s in chosen.stages)
    chores = sum(s.plan.ascension.ak_from_chores for s in chosen.stages)
    required = 100 * (75 - 65)
    assert 0 + steps + chores == pytest.approx(required + chosen.carry_out, abs=1e-6)
    assert f"Carry-out AK: {round(chosen.carry_out):,}" in markdown


# --- 2. Dedupe: an item mastered AND grandmastered in one stage shows once ----


def test_todo_list_dedupes_master_and_grandmaster(monkeypatch):
    result = _solve_two_stage_climb(monkeypatch)
    chosen = result.plans[main.selected_plan(result.plans, None, False)]
    outcome0 = chosen.stages[0].plan.ascension
    overlap = set(outcome0.masters) & set(outcome0.grandmasters)
    assert overlap, "scenario should grandmaster (and thus also master) >=1 widget"

    masters, grandmasters, megas = report._ascension_todo(chosen.stages[0])
    for item in overlap:
        assert item not in masters
        assert item in grandmasters
    # Every raw master is accounted for exactly once across the two dedup lists.
    assert set(masters) | set(grandmasters) == set(outcome0.masters) | set(
        outcome0.grandmasters
    )
    assert set(masters) & set(grandmasters) == set()


# --- 3. Chore-wait critical path appears only for the chore-bound stage ------


def test_chore_wait_line_only_on_the_chore_bound_stage(monkeypatch):
    result = _solve_two_stage_climb(monkeypatch)
    text, markdown = _render(result, current=65, target=75, starting_ak=0, selected=0)

    chosen = result.plans[0]
    stage0, stage1 = chosen.stages
    assert report._ascension_chore_wait(stage0.plan) is False
    assert report._ascension_chore_wait(stage1.plan) is True

    # The override line appears once, for stage 2 only, in both renderers.
    assert text.count("waiting on daily chores") == 1
    assert markdown.count("Waiting on daily chores") == 1
    # And it never coincides with the normal _critical_path_summary heading for
    # the same stage (print_critical_path's own precedence logic, untouched).
    stage2_markdown = markdown.split("Stage 2 —", 1)[1]
    assert "Critical path" in stage2_markdown
    stage1_markdown = markdown.split("Stage 1 —", 1)[1].split("Stage 2 —", 1)[0]
    assert "chores" not in stage1_markdown.lower()


# --- 4. Text and markdown agree on every decision ----------------------------


def test_text_and_markdown_agree_on_todo_and_critical_path(monkeypatch):
    result = _solve_two_stage_climb(monkeypatch)
    text, markdown = _render(result, current=65, target=75, starting_ak=0)
    chosen = result.plans[0]

    for stage in chosen.stages:
        masters, grandmasters, megas = report._ascension_todo(stage)
        for item in masters + grandmasters + megas:
            assert item.title() in text
            assert item.title() in markdown

        chore_wait = report._ascension_chore_wait(stage.plan)
        if chore_wait:
            expected = f"+{stage.plan.ascension.ak_from_chores:,.1f} AK over the stage"
            assert expected in text
            assert expected in markdown
