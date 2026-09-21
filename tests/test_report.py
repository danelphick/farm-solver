"""Tests for report.py's frontier-spread rendering: the per-row marginal-
efficiency note and the impact-graph plots (ASCII for the terminal, an inline
SVG for the web app) -- report.py's slice of the "configurable frontier spread
+ impact graph" feature (see /Users/dan/.claude/plans/jiggly-dancing-petal.md).

optimizer.py (already landed) owns SpreadSpec/GraphSeries/efficiency/
spread_graph_series; this module only exercises the rendering layer built on
top of them: print_frontier/markdown_frontier's new spread_axis/graph_metric
kwargs, _efficiency_note, _ascii_plot and _svg_plot.
"""

from datetime import timedelta

import passive
from crops import Crop, Farm
from optimizer import GraphSeries, SourcingPlan
from report import (
    MarkdownDoc,
    SectionedReporter,
    _ascii_plot,
    _cookie_harvest_note,
    _crop_of,
    _efficiency_note,
    _farming_action,
    _farming_grow_time,
    _net_gold_note,
    _plan_makespan,
    _svg_plot,
    markdown_frontier,
    print_frontier,
)
from sources import Draw, Offer


def test_net_gold_note_breaks_out_excluded_juice_cost():
    # price_pumpkin_juice off: net excludes pumpkin_gold, and the note shows a
    # "juice" piece explaining why net sits below the gold cell.
    priced = SourcingPlan(time=timedelta(0), gold=4600, pumpkin_gold=4600)
    assert _net_gold_note(priced) == ""  # net == gold, nothing to explain
    free = SourcingPlan(
        time=timedelta(0), gold=4600, pumpkin_gold=4600, price_pumpkin_juice=False,
    )
    assert "juice 4,600" in _net_gold_note(free)
    assert "net 0" in _net_gold_note(free)

PLANS = [
    SourcingPlan(time=timedelta(days=1), gold=100_000),
    SourcingPlan(time=timedelta(days=4), gold=0),
]


# --- _plan_makespan / farming whole-grow round-up -------------------------

_PINE = Offer("grow Pine Tree", "farming", {"pine": 1}, time_per_unit=timedelta(minutes=60))


def test_farming_grow_time_rounds_each_draw_up_to_whole_grows():
    # 3.4 grows at 60 min each cannot finish early -- a crop grows as a whole
    # cycle -- so the honest farming time is 4 whole grows, not the continuous 204.
    plan = SourcingPlan(time=timedelta(minutes=204), draws=[Draw(_PINE, 3.4)])
    assert _farming_grow_time(plan) == timedelta(minutes=240)


def test_plan_makespan_rounds_a_driving_farming_grow_up():
    # Farming is the sole track and the driver, so the makespan is its whole-grow
    # time (240), never the fractional 204 the LP left on plan.time.
    plan = SourcingPlan(time=timedelta(minutes=204), draws=[Draw(_PINE, 3.4)])
    assert _plan_makespan(plan) == timedelta(minutes=240)


def test_plan_makespan_leaves_fill_farming_on_its_continuous_time():
    # A short farming fill (0.4 grow = 24 min) beside a longer passive track (240)
    # is not the driver: rounding it up would wrongly push the plan past the track
    # that really caps it, so the makespan stays the passive 240.
    passive_offer = Offer(
        "passive", "passive", {"board": 1}, time_per_unit=timedelta(minutes=60)
    )
    plan = SourcingPlan(
        time=timedelta(minutes=240),
        draws=[Draw(_PINE, 0.4), Draw(passive_offer, 4.0)],
    )
    assert _plan_makespan(plan) == timedelta(minutes=240)


def test_frontier_row_quotes_whole_grow_farming_time(capsys):
    plan = SourcingPlan(time=timedelta(minutes=204), draws=[Draw(_PINE, 3.4)])
    print_frontier([plan], selected=0, cheapest=0)
    out = capsys.readouterr().out
    assert passive.format_duration(timedelta(minutes=240)) in out
    assert passive.format_duration(timedelta(minutes=204)) not in out


def test_farming_action_names_a_cookie_burst():
    # The once-per-day cookie burst reads as an instant burst, not "N cookie-boosted
    # days" (which is the whole-day offer) nor "M Harvest Alls" (a plain grow).
    burst = Offer(
        "grow Pine Tree with cookies (burst)", "farming", {"pine": 1}, cookies=1
    )
    assert _crop_of(burst) == "Pine Tree"
    assert _farming_action(Draw(burst, 0.3)) == "cookie burst of Pine Tree"
    day = Offer("grow Pine Tree with cookies", "farming", {"pine": 1}, cookies=1)
    assert _farming_action(Draw(day, 1.2)) == "2 cookie-boosted days of Pine Tree"


def _cookie_farm() -> Farm:
    return Farm(
        crops={"pine tree": Crop("Pine Tree", timedelta(hours=1), {"pine": 1})},
        rows=1,
        grape_juices_per_day=2,
        cookie_time_threshold=timedelta(minutes=18),
    )


def test_cookie_harvest_note_spells_out_the_whole_day():
    # A cookie day farms the WHOLE day -- 23 unboosted Harvest Alls outside the
    # window on top of the 3 boosted ones -- so the note has to say so; the action
    # name alone read as if only the five-minute window were farmed.
    day = Offer("grow Pine Tree with cookies", "farming", {"pine": 1}, cookies=1)
    assert _cookie_harvest_note(Draw(day, 10), _cookie_farm()) == (
        "each day: 23 natural + 3 boosted 9x Harvest Alls, 2 from Grape Juice"
    )


def test_cookie_harvest_note_marks_the_burst_as_window_only():
    burst = Offer(
        "grow Pine Tree with cookies (burst)", "farming", {"pine": 1}, cookies=1
    )
    assert _cookie_harvest_note(Draw(burst, 1), _cookie_farm()) == (
        "the 5m window only: 3 boosted 9x Harvest Alls, 2 from Grape Juice"
    )


def test_cookie_harvest_note_empty_for_an_unknown_crop():
    # A Farm with no crops (the bare-offer test fixtures) yields no split, and the
    # note drops out rather than rendering a bogus "0 natural".
    day = Offer("grow Pine Tree with cookies", "farming", {"pine": 1}, cookies=1)
    assert _cookie_harvest_note(Draw(day, 10), Farm()) == ""


# --- _efficiency_note -----------------------------------------------------


def test_efficiency_note_empty_when_eff_is_none():
    assert _efficiency_note(PLANS[0], PLANS[1], None) == ""


def test_efficiency_note_formats_time_gold_and_rate():
    # 3 days saved (4d - 1d) for 100,000.0 g more spent -> 100,000 / 3 g/s,
    # which at *86400 s/day is exactly 33,300 g/day (rounded).
    eff = (PLANS[0].cost[1] - PLANS[1].cost[1]) / (
        (PLANS[1].time - PLANS[0].time).total_seconds()
    )
    note = _efficiency_note(PLANS[0], PLANS[1], eff)
    assert note.startswith("(saves 3d for 100,000.0 g = ")
    assert note.endswith("g/day)")
    # Sanity-check the day conversion is in the right ballpark (not e.g. left
    # as a per-second or per-minute figure).
    per_day = int(note.split("= ")[1].split(" g/day")[0].replace(",", ""))
    assert 33_000 < per_day < 33_400


# --- print_frontier / markdown_frontier: efficiency notes -----------------


def test_print_frontier_efficiency_note_only_with_spread_axis(capsys):
    print_frontier(PLANS, selected=0, cheapest=1)
    out = capsys.readouterr().out
    assert "saves" not in out

    print_frontier(PLANS, selected=0, cheapest=1, spread_axis="time")
    out = capsys.readouterr().out
    assert "saves 3d for 100,000.0 g" in out


def test_print_frontier_efficiency_note_absent_on_slowest_row(capsys):
    print_frontier(PLANS, selected=0, cheapest=1, spread_axis="time")
    out = capsys.readouterr().out
    row1, row2 = (line for line in out.splitlines() if "1." in line or "2." in line)
    # Row 1 (fastest) carries the note; row 2 (slowest, no slower neighbour to
    # compare against) does not.
    assert "saves" in row1
    assert "saves" not in row2


def test_markdown_frontier_efficiency_note_only_with_spread_axis():
    doc = MarkdownDoc()
    markdown_frontier(doc, PLANS, selected=0, cheapest=1)
    assert "saves" not in doc.render()

    doc = MarkdownDoc()
    markdown_frontier(doc, PLANS, selected=0, cheapest=1, spread_axis="time")
    text = doc.render()
    assert "saves 3d for 100,000.0 g" in text


# --- _ascii_plot ------------------------------------------------------------


def test_ascii_plot_nonempty_multiline():
    series = GraphSeries(xs=[0, 1, 2], ys=[10, 5, 20], x_label="Time (s)", y_label="Net gold")
    text = _ascii_plot(series)
    assert text
    assert "\n" in text
    assert "Time (s)" in text  # x-axis caption
    # y-axis shows the metric's scale (its high/low values), not a repeated label.
    assert "20" in text
    assert "5" in text


def test_ascii_plot_single_point_does_not_crash():
    series = GraphSeries(xs=[5.0], ys=[7.0], x_label="Time (s)", y_label="Net gold")
    text = _ascii_plot(series)
    assert text
    assert "\n" in text


def test_ascii_plot_flat_range_does_not_crash():
    series = GraphSeries(xs=[1.0, 2.0, 3.0], ys=[9.0, 9.0, 9.0], x_label="Time (s)", y_label="Net gold")
    text = _ascii_plot(series)
    assert text
    assert "\n" in text


def test_ascii_plot_empty_series_does_not_crash():
    series = GraphSeries(xs=[], ys=[], x_label="Time (s)", y_label="Net gold")
    assert _ascii_plot(series) == "(no data)"


# --- _svg_plot ---------------------------------------------------------------


def test_svg_plot_has_svg_pt_hover_and_one_title_per_plan():
    series = GraphSeries(
        xs=[p.time.total_seconds() for p in PLANS],
        ys=[p.cost[1] for p in PLANS],
        x_label="Time (s)",
        y_label="Net gold",
    )
    svg = _svg_plot(series, PLANS)
    assert "<svg" in svg
    assert 'class="pt"' in svg
    assert ":hover" in svg
    assert svg.count("<title>") == len(PLANS)


def test_svg_plot_points_carry_plan_index_and_number():
    # Each point is clickable-to-select: it stamps its 0-based data-plan-index and
    # names its 1-based plan number in the tooltip, so the web app can wire both.
    series = GraphSeries(
        xs=[p.time.total_seconds() for p in PLANS],
        ys=[p.cost[1] for p in PLANS],
        x_label="Time (s)",
        y_label="Net gold",
    )
    svg = _svg_plot(series, PLANS)
    for i in range(len(PLANS)):
        assert f'data-plan-index="{i}"' in svg
        assert f"Plan {i + 1}" in svg
    assert "cursor: pointer" in svg


def test_svg_plot_single_point_does_not_crash():
    plan = SourcingPlan(time=timedelta(days=1), gold=10)
    series = GraphSeries(xs=[1.0], ys=[10.0], x_label="Time (s)", y_label="Net gold")
    svg = _svg_plot(series, [plan])
    assert "<svg" in svg
    assert svg.count("<title>") == 1


# --- print_frontier / markdown_frontier: graph -----------------------------


def test_print_frontier_graph_only_when_graph_metric_set(capsys):
    print_frontier(PLANS, selected=0, cheapest=1)
    out = capsys.readouterr().out
    assert "vs" not in out

    print_frontier(PLANS, selected=0, cheapest=1, graph_metric="gold")
    out = capsys.readouterr().out
    assert "Net gold vs Time:" in out


def test_markdown_frontier_graph_embeds_svg_when_requested():
    doc = MarkdownDoc()
    markdown_frontier(doc, PLANS, selected=0, cheapest=1, graph_metric="gold")
    text = doc.render()
    assert "<svg" in text
    assert 'class="pt"' in text

    doc = MarkdownDoc()
    markdown_frontier(doc, PLANS, selected=0, cheapest=1)
    assert "<svg" not in doc.render()


# --- SectionedReporter: the "graph" section ---------------------------------


def test_sectioned_reporter_graph_section_empty_by_default():
    reporter = SectionedReporter()
    assert "graph" in reporter.docs
    reporter.frontier(PLANS, selected=0, cheapest=1)
    assert reporter.docs["graph"].render().strip() == ""
    # And the frontier table itself carries no efficiency note either.
    assert "saves" not in reporter.docs["frontier"].render()


def test_sectioned_reporter_graph_section_populated_when_requested():
    reporter = SectionedReporter()
    reporter.frontier(
        PLANS, selected=0, cheapest=1, spread_axis="time", graph_metric="gold",
    )
    graph_text = reporter.docs["graph"].render()
    assert "<svg" in graph_text
    assert 'class="pt"' in graph_text
    # The graph must NOT also land inline in the frontier table's own doc.
    assert "<svg" not in reporter.docs["frontier"].render()
    # But the efficiency note does land in the table, same as the CLI.
    assert "saves 3d for 100,000.0 g" in reporter.docs["frontier"].render()


# --- Regression: defaults are byte-identical to before -----------------------


def test_print_frontier_default_kwargs_unchanged(capsys):
    print_frontier(PLANS, selected=0, cheapest=1, reasons=None, prices=None, golds=None)
    baseline = capsys.readouterr().out
    print_frontier(
        PLANS, selected=0, cheapest=1, reasons=None, prices=None, golds=None,
        spread_axis=None, graph_metric=None,
    )
    assert capsys.readouterr().out == baseline


def test_markdown_frontier_default_kwargs_unchanged():
    doc1 = MarkdownDoc()
    markdown_frontier(doc1, PLANS, selected=0, cheapest=1)
    doc2 = MarkdownDoc()
    markdown_frontier(
        doc2, PLANS, selected=0, cheapest=1, spread_axis=None, graph_metric=None,
    )
    assert doc1.render() == doc2.render()
