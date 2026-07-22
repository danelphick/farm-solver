"""Reporting and printing for Farm Solver.

Everything user-facing: the solution summary, the frontier table, the expanded
per-channel plan, and the critical-path analysis. :mod:`main` owns the CLI and
orchestration and calls into here; this module depends only on the domain
modules (never back on :mod:`main`), so the reporting layer stays a leaf.

The critical-path naming logic (see :func:`print_critical_path`) is subtle and
its precedence is deliberate -- read ARCHITECTURE.md before touching it.
"""

from __future__ import annotations

import calendar
import dataclasses
import math
import re
import sys
from collections.abc import Iterable
from datetime import date, timedelta
from typing import TYPE_CHECKING

from rich.console import Console
from rich.markdown import Markdown as RichMarkdown
from rich.segment import Segment, Segments
from rich.style import Style

import ascension
import config
import crafting
import crops
import farm_solver
import optimizer
import passive
import pets
import sources

if TYPE_CHECKING:
    # main.py imports report.py, so this is TYPE_CHECKING-only (never evaluated
    # at runtime, see `from __future__ import annotations`) to avoid a cycle --
    # only used to type-hint the ascension orchestration dataclasses below.
    import main


# How close to the makespan the farming track may fall and still count as the
# critical path (the rest being passive fill); see :func:`print_critical_path`.
_FARMING_COMAX_MARGIN = 0.01

# How close to its full (cheapest-plan) requirement a passive item's residual must
# be to count as fully gathered -- the resource whose completion gates the plan
# (see :func:`_defining_passive_resources`).
_FULL_REQUIREMENT_MARGIN = 0.02


def _escape_cell(text: str) -> str:
    """Escape a Markdown table cell so an embedded ``|`` doesn't split columns."""
    return text.replace("|", "\\|")


# A cell holding a quantity: counts, gold, durations ("6d 5h 16m"), dates and
# percentages all start with a digit, possibly signed or parenthesised.
_NUMERIC_CELL_RE = re.compile(r"^\(?[-−+]?\d")


def _column_is_numeric(rows: list[list[str]], index: int) -> bool:
    """Whether most of a column's non-empty cells hold quantities.

    Majority rather than all: a stray word in a numeric column ("done" in the
    Goal table's *Still needed*) rides along right-aligned rather than
    flipping the whole column back to left.
    """
    cells = [
        str(row[index]).strip()
        for row in rows
        if index < len(row) and str(row[index]).strip()
    ]
    numeric = sum(bool(_NUMERIC_CELL_RE.match(cell)) for cell in cells)
    # >= not >: at least half (not a strict majority) reads as numeric, so an
    # even split -- one "done" Goal row against one real shortfall, the case
    # that motivated this heuristic -- still rides along right-aligned instead
    # of flipping the whole column back to left.
    return bool(cells) and numeric * 2 >= len(cells)


class MarkdownDoc:
    """Accumulates a report as Markdown source, rendered once at the end.

    Each call appends one self-contained block (a heading, a paragraph, a bullet
    list or a table); :meth:`render` joins them blank-line-separated, which is
    what makes each a distinct element to a Markdown parser/renderer.
    """

    def __init__(self, heading_shift: int = 0) -> None:
        self._blocks: list[str] = []
        # Demotes every heading by this many levels: a doc rendered under an
        # externally supplied heading (the web app's "Selected plan" pane)
        # shifts by 1 so its sections nest instead of competing with it.
        self._heading_shift = heading_shift

    def heading(self, text: str, level: int = 2) -> None:
        self._blocks.append(f"{'#' * (level + self._heading_shift)} {text}")

    def para(self, text: str) -> None:
        self._blocks.append(text)

    def bullets(self, lines: Iterable[str]) -> None:
        self._blocks.append("\n".join(f"- {line}" for line in lines))

    def table(
        self,
        headers: list[str],
        rows: list[list[str]],
        align: list[str] | None = None,
    ) -> None:
        if not rows:
            return
        head = "| " + " | ".join(_escape_cell(h) for h in headers) + " |"
        # Per column: an explicit "left"/"right" from ``align``, else "auto" --
        # right when most cells are quantities (counts, gold, durations, dates
        # all start with a digit, possibly signed/parenthesised; the odd word
        # like "done" rides along). Explicit alignment is for prose columns
        # whose text merely *starts* with a number ("8,541 from sawmill, ...").
        aligns = []
        for i in range(len(headers)):
            mode = align[i] if align is not None else "auto"
            if mode == "auto":
                mode = "right" if _column_is_numeric(rows, i) else "left"
            aligns.append("---:" if mode == "right" else "---")
        sep = "| " + " | ".join(aligns) + " |"
        body = [
            "| " + " | ".join(_escape_cell(str(c)) for c in row) + " |" for row in rows
        ]
        self._blocks.append("\n".join([head, sep, *body]))

    def render(self) -> str:
        return "\n\n".join(self._blocks) + "\n"


# Marks a goal item the frontier can't source at all (it's fully stuck in
# ``remainder`` -- see ``optimizer._obtainable``); appended to its "Still
# needed" cell/line. A plain Unicode glyph rather than markup, so it reads the
# same in plain text, a raw-markdown redirect, rich's terminal rendering and
# the web UI's rendered HTML -- the web UI additionally styles the row red by
# detecting this glyph client-side (see app.js's ``markUnreachableGoalRows``).
UNREACHABLE_GOAL_MARK = "✗"  # ✗


def print_solution(
    solution: farm_solver.Solution, remainder: dict[str, int] | None = None
) -> None:
    remainder = remainder or {}
    unreachable = {item.lower() for item in remainder}
    for need in solution.needs:
        prefix = f"T{need.level}: " if need.level is not None else ""
        if need.extra > 0:
            mark = f" {UNREACHABLE_GOAL_MARK}" if need.item.lower() in unreachable else ""
            print(
                f"{prefix}{need.item}: need {need.extra:,} more "
                f"(have {need.have:,} of {need.target:,}){mark}"
            )
        else:
            print(
                f"{prefix}{need.item}: already have "
                f"{need.have:,} of {need.target:,}"
            )

    for note in solution.notes:
        print(note)

    if remainder:
        print("\nMaterials with no known source (supply yourself):")
        for material in sorted(remainder):
            print(f"  {remainder[material]:,}x {material}")


def markdown_solution(
    doc: MarkdownDoc,
    solution: farm_solver.Solution,
    remainder: dict[str, int] | None = None,
) -> None:
    """Markdown counterpart to :func:`print_solution`: the goal needs as a table.

    ``remainder`` (a completed frontier's ``plans[0].remainder`` -- identical
    across every plan in one run, since it comes from demand vs. obtainability,
    not any plan's own choices) flags a goal item the frontier could not source
    at all: its "Still needed" cell gets :data:`UNREACHABLE_GOAL_MARK` appended,
    and the "Materials with no known source" table -- moved here from the foot
    of the selected plan so it reads before, not after, everything the frontier
    *did* work out -- lists what's missing right below.
    """
    remainder = remainder or {}
    unreachable = {item.lower() for item in remainder}
    if solution.needs:
        doc.heading("Goal")
        headers = ["Purpose", "Item", "Have", "Target", "Still needed"]
        rows = []
        for need in solution.needs:
            # Why the item is wanted: a tower level's requirement, or a plain
            # -m (mastery) / -i (inventory count) goal.
            if need.level is not None:
                purpose = f"T{need.level}"
            elif need.is_mastery:
                purpose = "Mastery"
            else:
                purpose = "Inventory"
            if need.extra > 0:
                still_needed = f"{need.extra:,}"
                if need.item.lower() in unreachable:
                    still_needed += f" {UNREACHABLE_GOAL_MARK}"
            else:
                still_needed = "done"
            rows.append([
                purpose,
                need.item,
                f"{need.have:,}",
                f"{need.target:,}",
                still_needed,
            ])
        doc.table(headers, rows)
    for note in solution.notes:
        doc.para(note)
    if remainder:
        rows = [[material, f"{remainder[material]:,}"] for material in sorted(remainder)]
        doc.heading("Materials with no known source (supply yourself)")
        doc.table(["Material", "Qty"], rows)


def print_unobtainable(materials: list[str]) -> None:
    print("The following materials are required for some crafts but are unobtainable:")
    if not materials:
        print("  (none)")
        return
    for material in materials:
        print(f"  {material}")


def markdown_unobtainable(doc: MarkdownDoc, materials: list[str]) -> None:
    """Markdown counterpart to :func:`print_unobtainable`."""
    doc.heading("Unobtainable materials")
    if not materials:
        doc.para("(none)")
        return
    doc.bullets(materials)


def _cookie_sets_used(plan: optimizer.SourcingPlan) -> int:
    """Whole cookie sets a plan spends — partial sets aren't usable, so round up.

    One cookie set covers one real day, boosting whichever crops you harvest that
    day alongside the day's Grape Juice allocation. Crops grow one after another,
    so the sets a plan needs is the whole number of days its cookie-boosted
    farming spans (``ceil`` of the total), not the sum of each crop's rounded-up
    days — that would double-count a day two crops share.
    """
    return math.ceil(plan.cookie_sets)


def _pumpkin_juices(plan: optimizer.SourcingPlan) -> float:
    """Total Pumpkin Juices a plan spends, summed across the goals they boost."""
    return sum(plan.pumpkin_juices.values())


def _net_gold_note(plan: optimizer.SourcingPlan) -> str:
    """`` (bank B, juice J, stamina S, net N)`` for a frontier row, or "" if nothing.

    Shows the surplus the plan banks (a credit), the Pumpkin Juice cost dropped from
    net when ``price_pumpkin_juice`` is off (a credit), and the free-stamina
    opportunity cost it spends (a charge), plus its **net** gold ``plan.cost[1]`` --
    the real cost the frontier trades against time (see
    :attr:`optimizer.SourcingPlan.cost`). Each piece is omitted when zero, so a plan
    with none adds nothing. ``juice`` explains why net sits far below the gold cell
    (that gold still includes the juice buy; only net nets it out).
    """
    banked = round(plan.surplus_gold)
    stamina = round(plan.stamina_gold)
    juice = 0 if plan.price_pumpkin_juice else round(plan.pumpkin_gold)
    pieces = []
    if banked:
        pieces.append(f"bank {banked:,}")
    if juice:
        pieces.append(f"juice {juice:,}")
    if stamina:
        pieces.append(f"stamina {stamina:,}")
    if not pieces:
        return ""
    return f" ({', '.join(pieces)}, net {round(plan.cost[1]):,})"


# Seconds per day, for turning `optimizer.efficiency`'s gold/second rate into a
# human "gold/day" figure -- the unit a plan's marginal speed is worth reasoning
# about in (nobody buys a few seconds; they buy a day off the makespan).
_SECONDS_PER_DAY = 86400


def _efficiency_note(
    plan: optimizer.SourcingPlan, next_plan: optimizer.SourcingPlan, eff: float | None
) -> str:
    """``"(saves 3d for 100,000 g = 33,300 g/day)"`` -- how much makespan ``plan``
    buys over the next **slower** shown plan (``next_plan``) and what that costs,
    expressed as a gold/day rate (see :func:`optimizer.efficiency`, which supplies
    ``eff``, the gold/second version of the same ratio). ``""`` when ``eff`` is
    ``None`` (the slowest row has no slower neighbour to compare against).
    Modelled on :func:`_net_gold_note`, but returned unspaced -- the caller joins
    it into a frontier row's other notes rather than appending it to the gold cell,
    since it should show up whether or not ``prices`` is set.
    """
    if eff is None:
        return ""
    dt = next_plan.time - plan.time
    dgold = round(plan.cost[1] - next_plan.cost[1])
    per_day = round(eff * _SECONDS_PER_DAY)
    return f"(saves {passive.format_duration(dt)} for {dgold:,} g = {per_day:,} g/day)"


def _stamina_units(plan: optimizer.SourcingPlan, prices: dict[str, float] | None) -> float:
    """Free stamina units behind ``plan.stamina_gold`` (gold / per-unit price)."""
    unit = (prices or {}).get("stamina", 0.0)
    return plan.stamina_gold / unit if unit else 0.0


def _print_stamina_cost(
    plan: optimizer.SourcingPlan, prices: dict[str, float] | None
) -> None:
    """Text ``Stamina (opportunity cost)`` section, skipped when the cost rounds to 0."""
    if prices is None or round(plan.stamina_gold) < 1:
        return
    units = _stamina_units(plan, prices)
    print("\nStamina (opportunity cost):")
    print(
        f"  {round(units):,} banked/passive stamina"
        f" = {round(plan.stamina_gold):,} g (at Orange-Juice value)"
    )


# --- Impact graph: series.ys vs series.xs, terminal ASCII or a web SVG --------
#
# Both renderers plot the same optimizer.GraphSeries (see frontier()'s
# spread_axis/graph_metric kwargs below): _ascii_plot for TextReporter/
# print_frontier, _svg_plot for SectionedReporter's web "graph" section (and
# MarkdownReporter, which embeds the same SVG inline -- markdown renders to
# HTML, so no separate ASCII path is needed there).

_ASCII_PLOT_WIDTH = 60
_ASCII_PLOT_HEIGHT = 12


def _ascii_plot(series: optimizer.GraphSeries) -> str:
    """A compact, fixed-size ASCII scatter of ``series`` for the terminal.

    Each point lands on the row/column its ``(x, y)`` maps to in a
    ``_ASCII_PLOT_WIDTH`` x ``_ASCII_PLOT_HEIGHT`` grid (y flipped so a larger
    value plots nearer the top, as any chart would); axis labels and the
    range's low/high values print below. A single point, or a flat axis (every
    x or every y equal -- a one-row spread, or a metric that doesn't move over
    the sampled range), would otherwise divide by zero mapping into the grid;
    both are guarded to land dead-centre on that axis instead of crashing.
    """
    xs, ys = series.xs, series.ys
    if not xs:
        return "(no data)"
    width, height = _ASCII_PLOT_WIDTH, _ASCII_PLOT_HEIGHT
    x_lo, x_hi = min(xs), max(xs)
    y_lo, y_hi = min(ys), max(ys)

    def col(x: float) -> int:
        if x_hi == x_lo:
            return width // 2
        return round((x - x_lo) / (x_hi - x_lo) * (width - 1))

    def row(y: float) -> int:
        if y_hi == y_lo:
            return height // 2
        return round((y_hi - y) / (y_hi - y_lo) * (height - 1))

    grid = [[" "] * width for _ in range(height)]
    for x, y in zip(xs, ys):
        grid[row(y)][col(x)] = "*"

    # The y-axis carries the metric's *scale* -- its value at the top, middle and
    # bottom gridlines (the label itself is in the caller's "Y vs X" heading, so
    # repeating it every row said nothing). Other rows keep a blank gutter.
    def y_at(r: int) -> float:
        if height <= 1 or y_hi == y_lo:
            return y_hi
        return y_hi - (y_hi - y_lo) * r / (height - 1)

    y_ticks = {
        r: _format_axis_value(series.y_label, y_at(r))
        for r in {0, (height - 1) // 2, height - 1}
    }
    gutter = max(len(label) for label in y_ticks.values())
    lines = [
        f"{y_ticks.get(r, ''):>{gutter}} |" + "".join(cells)
        for r, cells in enumerate(grid)
    ]
    indent = gutter + 2  # the "<gutter> |" prefix width
    lines.append(" " * indent + "-" * width)
    lo_label = _format_axis_value(series.x_label, x_lo)
    hi_label = _format_axis_value(series.x_label, x_hi)
    pad = max(1, width - len(lo_label) - len(hi_label))
    lines.append(" " * indent + lo_label + " " * pad + hi_label)
    lines.append(" " * indent + series.x_label)
    return "\n".join(lines)


def _format_axis_value(label: str, value: float) -> str:
    """Render one axis tick/tooltip value in the unit its ``GraphSeries`` label
    implies -- a duration for a time-flavoured axis, else a plain comma-grouped
    count (gold or a Pumpkin Juice count both read fine as one)."""
    if label.startswith("Time"):
        return passive.format_duration(timedelta(seconds=value))
    return f"{round(value):,}"


def _svg_escape(text: str) -> str:
    """Minimal XML escaping for text embedded in the SVG (labels, tooltips)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_SVG_WIDTH = 560
_SVG_HEIGHT = 280
_SVG_MARGIN_LEFT = 68
_SVG_MARGIN_RIGHT = 20
_SVG_MARGIN_TOP = 16
_SVG_MARGIN_BOTTOM = 44


def _svg_plot(
    series: optimizer.GraphSeries, plans: list[optimizer.SourcingPlan]
) -> str:
    """A self-contained, theme-aware inline SVG scatter of ``series`` -- the
    web report's impact graph (:class:`SectionedReporter`'s ``"graph"``
    section, and the same markup :class:`MarkdownReporter` embeds inline).

    No JavaScript: hover enlarge/highlight is a nested ``<style>``'s
    ``:hover`` rule on each ``.pt`` circle, and the tooltip is a native
    ``<title>`` child per point -- naming its **plan number** (1-based) and axis
    value plus, always, net gold and time (``plans``, matched index-for-index with
    ``series``, since the tooltip shows both regardless of which one
    ``series.y_label`` actually plots). Each point also carries ``data-plan-index``
    (its 0-based position) so the web app can wire a click to select that plan
    (``cursor: pointer`` marks them as actionable); the no-JS render just ignores it.
    Colors are CSS custom properties scoped to the root ``<svg>``, re-themed by one
    ``@media (prefers-color-scheme: dark)`` block, so the same markup reads on either
    background without a second render.
    """
    xs, ys = series.xs, series.ys
    w, h = _SVG_WIDTH, _SVG_HEIGHT
    left, right = _SVG_MARGIN_LEFT, w - _SVG_MARGIN_RIGHT
    top, bottom = _SVG_MARGIN_TOP, h - _SVG_MARGIN_BOTTOM

    x_lo, x_hi = (min(xs), max(xs)) if xs else (0.0, 1.0)
    y_lo, y_hi = (min(ys), max(ys)) if ys else (0.0, 1.0)
    if x_hi == x_lo:  # a flat/one-point range still needs a span to place in
        x_lo, x_hi = x_lo - 1, x_hi + 1
    if y_hi == y_lo:
        y_lo, y_hi = y_lo - 1, y_hi + 1

    def px(x: float) -> float:
        return left + (x - x_lo) / (x_hi - x_lo) * (right - left)

    def py(y: float) -> float:
        return bottom - (y - y_lo) / (y_hi - y_lo) * (bottom - top)

    circles = []
    for i, (x, y) in enumerate(zip(xs, ys)):
        # Point i is the i-th shown plan (see docstring), so it carries that plan's
        # 1-based number -- named in the tooltip, and stamped as data-plan-index (the
        # 0-based position) so the web app can select the plan on click.
        lines = [f"Plan {i + 1}", f"{series.x_label}: {_format_axis_value(series.x_label, x)}"]
        if i < len(plans):
            plan = plans[i]
            lines.append(f"Net gold: {round(plan.cost[1]):,} g")
            lines.append(f"Time: {passive.format_duration(plan.time)}")
        circles.append(
            f'<circle class="pt" data-plan-index="{i}" cx="{px(x):.1f}" cy="{py(y):.1f}" r="5">'
            f"<title>{_svg_escape(chr(10).join(lines))}</title></circle>"
        )

    x_ticks = [x_lo, (x_lo + x_hi) / 2, x_hi]
    y_ticks = [y_lo, (y_lo + y_hi) / 2, y_hi]
    x_tick_marks = "".join(
        f'<text class="muted" x="{px(v):.1f}" y="{bottom + 16}" text-anchor="middle">'
        f"{_svg_escape(_format_axis_value(series.x_label, v))}</text>"
        for v in x_ticks
    )
    y_tick_marks = "".join(
        f'<text class="muted" x="{left - 8}" y="{py(v) + 4:.1f}" text-anchor="end">'
        f"{_svg_escape(_format_axis_value(series.y_label, v))}</text>"
        for v in y_ticks
    )
    y_label_cy = (top + bottom) / 2

    return f'''<svg class="spread-graph" viewBox="0 0 {w} {h}" width="100%" \
height="auto" role="img" \
aria-label="{_svg_escape(series.y_label)} vs {_svg_escape(series.x_label)}" \
xmlns="http://www.w3.org/2000/svg">
<style>
.spread-graph {{
  --sg-surface: #fcfcfb;
  --sg-ink: #0b0b0b;
  --sg-muted: #898781;
  --sg-axis: #c3c2b7;
  --sg-accent: #2a78d6;
  --sg-accent-hover: #eb6834;
  font: 11px system-ui, -apple-system, "Segoe UI", sans-serif;
}}
@media (prefers-color-scheme: dark) {{
  .spread-graph {{
    --sg-surface: #1a1a19;
    --sg-ink: #ffffff;
    --sg-axis: #383835;
    --sg-accent: #3987e5;
    --sg-accent-hover: #d95926;
  }}
}}
.spread-graph .axis {{ stroke: var(--sg-axis); stroke-width: 1; }}
.spread-graph text {{ fill: var(--sg-ink); }}
.spread-graph .muted {{ fill: var(--sg-muted); }}
.spread-graph .label {{ fill: var(--sg-muted); }}
.spread-graph .pt {{
  fill: var(--sg-accent);
  stroke: var(--sg-surface);
  stroke-width: 2;
  cursor: pointer;
  transition: r 120ms ease, fill 120ms ease;
}}
.spread-graph .pt:hover {{ r: 8; fill: var(--sg-accent-hover); }}
.spread-graph .pt.selected {{ r: 7; fill: var(--sg-accent-hover); }}
</style>
<line class="axis" x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" />
<line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" />
{x_tick_marks}
{y_tick_marks}
<text class="label" x="{(left + right) / 2:.1f}" y="{h - 6}" text-anchor="middle">\
{_svg_escape(series.x_label)}</text>
<text class="label" x="14" y="{y_label_cy:.1f}" text-anchor="middle" \
transform="rotate(-90 14 {y_label_cy:.1f})">{_svg_escape(series.y_label)}</text>
{"".join(circles)}
</svg>'''


def print_frontier(
    plans: list[optimizer.SourcingPlan],
    selected: int,
    cheapest: int,
    reasons: list[str | None] | None = None,
    prices: dict[str, float] | None = None,
    golds: list[tuple[float, float]] | None = None,
    spread_axis: str | None = None,
    graph_metric: str | None = None,
) -> None:
    """The frontier table: each plan's time, consumables and gold, fastest first.

    The tradeoff is time against **gold**: the fast plans buy their consumables,
    the slow ones self-source them. Each row lists its makespan and the consumables
    it spends (cookie sets, Grape Juices and Apple Ciders appear as columns only
    when some plan uses them), then, with trading ``prices``, its gold. ``golds``
    (one ``(to_buy, passive)`` pair per plan, from
    :func:`optimizer.plan_gold_breakdown`) splits that into what the plan **buys**
    and the market value of what it **self-sources** instead, summing to the total.
    A plan that banks surplus or drains free stamina then shows ``(bank B,
    stamina S, net N)`` (each piece omitted when zero, see :func:`_net_gold_note`):
    the sale value of what it produces past all it consumes (``plan.surplus_gold``),
    the free-stamina opportunity cost it spends (``plan.stamina_gold``), and its
    **net** gold ``buy - bank + stamina`` -- the real cost the frontier trades
    against time, which for a slow, passive-leaning plan falls below zero (see
    :attr:`optimizer.SourcingPlan.cost`); this is what "cheapest" ranks by.
    ``reasons`` annotates why each plan is on the frontier (a resource plan says
    "all Steel Wire from passive"); endpoints and farming intermediates carry only
    their fastest/cheapest tag.

    ``spread_axis`` (the axis a ``--spread`` frontier was sampled along, or
    ``None`` in normal time-thinned mode) turns on a per-row marginal-efficiency
    note -- see :func:`_efficiency_note` -- computed from
    :func:`optimizer.efficiency`. ``graph_metric`` (``"auto"``/``"gold"``/
    ``"time"``, or ``None`` to leave the graph off) prints an ASCII impact-graph
    plot (:func:`_ascii_plot`) after the table, ``y=graph_metric`` against
    ``x=spread_axis`` (falling back to "time" when there was no spread, matching
    :func:`optimizer.spread_graph_series`'s own default). Both default to
    ``None``, leaving this function's output byte-identical to before either
    existed.
    """
    effs = optimizer.efficiency(plans) if spread_axis is not None else None
    show_cookies = any(_cookie_sets_used(p) >= 1 for p in plans)
    show_juices = any(round(p.grape_juices) >= 1 for p in plans)
    show_ciders = any(round(p.apple_ciders) >= 1 for p in plans)
    show_pumpkin = any(round(_pumpkin_juices(p)) >= 1 for p in plans)

    columns = ["time", "Arnold Palmers", "Large Nets"]
    if show_cookies:
        columns.append("cookie sets")
    if show_juices:
        columns.append("Grape Juice")
    if show_ciders:
        columns.append("Apple Cider")
    if show_pumpkin:
        columns.append("Pumpkin Juice")
    legend = ""
    if prices is not None:
        legend = " -> gold, as buy + self-sourced" if golds is not None else " -> gold"
    print(f"\nSourcing plans ({' + '.join(columns)}{legend}):")

    for i, plan in enumerate(plans):
        marker = ">" if i == selected else " "
        parts = [
            passive.format_duration(_plan_makespan(plan)),
            f"{round(plan.arnold_palmers):,} AP",
            f"{round(plan.large_nets):,} LN",
        ]
        if show_cookies:
            parts.append(f"{_cookie_sets_used(plan):,} sets")
        if show_juices:
            parts.append(f"{round(plan.grape_juices):,} GJ")
        if show_ciders:
            parts.append(f"{round(plan.apple_ciders):,} AC")
        if show_pumpkin:
            parts.append(f"{round(_pumpkin_juices(plan)):,} PJ")
        gold = ""
        if prices is not None:
            if golds is not None:
                buy, passv = (round(g) for g in golds[i])
                gold = f" -> {buy:,} + {passv:,} = {buy + passv:,} g"
            else:
                gold = f" -> {round(optimizer.plan_gold(plan, prices)):,} g"
            # Credit the surplus the plan banks and charge the free stamina it
            # drains: its net cost (what the frontier trades against time) is what
            # it buys, less what it can sell, plus the Orange-Juice-equivalent of
            # the banked/passive stamina spent -- which for a slow, passive-leaning
            # plan drops below zero (see SourcingPlan.cost).
            gold += _net_gold_note(plan)
        tags = []
        if i == 0:
            tags.append("fastest")
        if i == cheapest:
            tags.append("cheapest")
        note_parts = []
        if tags:
            note_parts.append(f"[{', '.join(tags)}]")
        reason = reasons[i] if reasons is not None else None
        if reason:
            note_parts.append(f"— {reason}")
        if effs is not None and i + 1 < len(plans):
            eff_note = _efficiency_note(plan, plans[i + 1], effs[i])
            if eff_note:
                note_parts.append(eff_note)
        note = "  " + " ".join(note_parts) if note_parts else ""
        print(f" {marker} {i + 1}. {' + '.join(parts)}{gold}{note}")

    if graph_metric is not None:
        series = optimizer.spread_graph_series(plans, spread_axis or "time", graph_metric)
        print(f"\n{series.y_label} vs {series.x_label}:")
        print(_ascii_plot(series))


def markdown_frontier(
    doc: MarkdownDoc,
    plans: list[optimizer.SourcingPlan],
    selected: int,
    cheapest: int,
    reasons: list[str | None] | None = None,
    prices: dict[str, float] | None = None,
    golds: list[tuple[float, float]] | None = None,
    include_selected_tag: bool = True,
    spread_axis: str | None = None,
    graph_metric: str | None = None,
) -> None:
    """Markdown counterpart to :func:`print_frontier`: the same rows, as a table.

    ``include_selected_tag`` is off for the web app (see
    ``SectionedReporter.frontier``): its frontier table already highlights the
    selected row with a CSS class/border (``wireFrontierRows`` in app.js), so
    a redundant "selected" word in the Notes cell only costs a reflow every
    time a click moves it from one row's text to another's. The CLI (through
    ``MarkdownReporter``, both the raw Markdown source and the terminal
    render) has no such highlight, so it keeps the word -- the default.

    ``spread_axis``/``graph_metric`` mirror :func:`print_frontier`'s own
    (efficiency notes, an impact-graph plot) -- here the plot is a self-
    contained inline SVG (:func:`_svg_plot`) appended after the table, since
    Markdown renders to HTML. :class:`SectionedReporter` calls this with
    ``graph_metric=None`` regardless and renders the SVG into its own
    ``"graph"`` section instead -- see its ``frontier`` method.
    """
    effs = optimizer.efficiency(plans) if spread_axis is not None else None
    show_cookies = any(_cookie_sets_used(p) >= 1 for p in plans)
    show_juices = any(round(p.grape_juices) >= 1 for p in plans)
    show_ciders = any(round(p.apple_ciders) >= 1 for p in plans)
    show_pumpkin = any(round(_pumpkin_juices(p)) >= 1 for p in plans)

    headers = ["#", "Time", "AP", "LN"]
    if show_cookies:
        headers.append("Cookie sets")
    if show_juices:
        headers.append("Grape Juice")
    if show_ciders:
        headers.append("Apple Cider")
    if show_pumpkin:
        headers.append("Pumpkin Juice")
    if prices is not None:
        headers.append("Gold")
    headers.append("Notes")

    rows = []
    for i, plan in enumerate(plans):
        row = [
            str(i + 1),
            passive.format_duration(_plan_makespan(plan)),
            f"{round(plan.arnold_palmers):,}",
            f"{round(plan.large_nets):,}",
        ]
        if show_cookies:
            row.append(f"{_cookie_sets_used(plan):,}")
        if show_juices:
            row.append(f"{round(plan.grape_juices):,}")
        if show_ciders:
            row.append(f"{round(plan.apple_ciders):,}")
        if show_pumpkin:
            row.append(f"{round(_pumpkin_juices(plan)):,}")
        if prices is not None:
            if golds is not None:
                buy, passv = (round(g) for g in golds[i])
                gold = f"{buy:,} + {passv:,} = {buy + passv:,} g"
            else:
                gold = f"{round(optimizer.plan_gold(plan, prices)):,} g"
            gold += _net_gold_note(plan)
            row.append(gold)
        tags = []
        if i == 0:
            tags.append("fastest")
        if i == cheapest:
            tags.append("cheapest")
        if include_selected_tag and i == selected:
            tags.append("selected")
        notes = []
        if tags:
            notes.append(f"[{', '.join(tags)}]")
        reason = reasons[i] if reasons is not None else None
        if reason:
            notes.append(reason)
        if effs is not None and i + 1 < len(plans):
            eff_note = _efficiency_note(plan, plans[i + 1], effs[i])
            if eff_note:
                notes.append(eff_note)
        row.append(" ".join(notes))
        rows.append(row)

    doc.heading("Sourcing plans")
    doc.table(headers, rows)
    if graph_metric is not None:
        series = optimizer.spread_graph_series(plans, spread_axis or "time", graph_metric)
        doc.heading("Impact graph")
        doc.para(_svg_plot(series, plans))


def _frontier_reasons(
    plans: list[optimizer.SourcingPlan],
    demand: dict[str, float],
    recipes: dict[str, dict[str, float]],
    crafting_bonus: float,
    open_flows: list[sources.OpenFlow],
    inventory: dict[str, float],
    max_passive_residuals: dict[str, float],
    production: passive.PassiveProduction | None,
    display: dict[str, str],
) -> list[str | None]:
    """Why each plan is on the frontier: the resource(s) whose full-requirement
    gathering plateau it sits at ("all Steel Wire from passive"; exact ties on a
    deposit boundary are all named, "all Orange and Lemon from passive"), else
    ``None`` (an endpoint or a farming-accelerated intermediate). Mirrors the
    chosen plan's critical path (see :func:`_defining_passive_resources`)."""
    reasons: list[str | None] = []
    for plan in plans:
        residuals = optimizer.passive_residuals(
            plan, demand, recipes, crafting_bonus, open_flows, inventory
        )
        tolerance = max(timedelta(seconds=1), plan.time * _FARMING_COMAX_MARGIN)
        defining = _defining_passive_resources(
            plan, residuals, display, plan.time, tolerance,
            max_passive_residuals, production,
        )
        names = _join_names([name for name, _, _, _ in defining])
        reasons.append(f"all {names} from passive" if defining else None)
    return reasons


def _pet_passive_rate(
    production: passive.PassiveProduction | None, item: str
) -> float:
    """Per-minute rate at which pets (not buildings) passively make ``item``."""
    if production is None:
        return 0.0
    return sum(
        rate
        for name, rate in production.producer_rates(item)
        if name.startswith(config.PET_PRODUCER_PREFIX)
    )


def _pet_dominated_items(production: passive.PassiveProduction) -> set[str]:
    """Passive items a pet mostly makes -- a slow trickle, not a real plateau.

    Their completion times are the multi-decade pet tail, not the gathering
    breakpoints worth building a plan around (see :func:`optimizer.frontier`).
    """
    items = {key for producer in production.producers for key in producer.outputs}
    return {
        key for key in items
        if _pet_passive_rate(production, key) > production.rate_per_minute(key) / 2
    }


def _farming_grow_time(plan: optimizer.SourcingPlan) -> timedelta:
    """The farming track's time counted in *whole* grows.

    A crop grows as an indivisible cycle -- you cannot harvest a field early --
    so a fractional ``Draw.units`` (0.4 of a pine-tree grow) still costs a whole
    grow of real time. The LP leaves ``units`` continuous, so raw ``Draw.time``
    under-reports a farming-bound plan's real finish (a plan "waits 26 minutes"
    for a grow that in truth takes the whole cycle); rounding each draw up to
    whole grows -- the same ``math.ceil`` the action count already uses (see
    :func:`_farming_action`) -- is the honest figure to quote. ``timedelta(0)``
    when the plan grows nothing.
    """
    return sum(
        (
            d.offer.time_per_unit * math.ceil(d.units)
            for d in plan.draws
            if d.offer.track == sources.FARMING_TRACK
        ),
        timedelta(0),
    )


def _plan_makespan(plan: optimizer.SourcingPlan) -> timedelta:
    """The plan's real makespan: ``plan.time`` or the longest draw track.

    Take whichever is bigger, not the draws alone: quantisation (see
    :func:`optimizer._quantize_plan_time`) can bump ``plan.time`` past every
    draw's own time to the next real deposit boundary, and the header, the
    frontier row and the per-item waits must all quote the same figure.
    Hand-built test plans with no cached time (``plan.time == 0``) still fall
    back to the draws' own times.

    When farming is the plan's *own* bottleneck -- its continuous track time
    strictly exceeds every other track (not merely co-maximal fill that another
    track's wait forces, see :func:`_binding_crop_is_fill`) -- the farming
    contribution is counted in whole grows (:func:`_farming_grow_time`), since a
    fractional last grow cannot finish early. Co-maximal fill farming stays on
    its continuous time, so rounding it up never wrongly pushes a
    passive-bottlenecked plan past the boundary that really drives it.
    """
    times = plan.track_times()
    farming = times.get(sources.FARMING_TRACK, timedelta(0))
    others = max(
        (t for track, t in times.items() if track != sources.FARMING_TRACK),
        default=timedelta(0),
    )
    tolerance = max(timedelta(seconds=1), farming * _FARMING_COMAX_MARGIN)
    if farming > others + tolerance:
        farming = _farming_grow_time(plan)
    return max(plan.time, others, farming)


def _passive_wait(
    production: passive.PassiveProduction | None,
    key: str,
    residual: float,
    rate: float,
    limit: timedelta | None = None,
) -> timedelta:
    """The real wait for ``residual`` of ``key`` to accumulate, on the actual
    deposit schedule when ``production`` is known -- the plain
    ``residual / rate`` continuous estimate otherwise.

    The one place every passive wait printed in a report goes through, so the
    text and Markdown renderers -- and the critical-path line, and the
    slowest-item/defining-resource pickers that feed it -- all show the same
    quantized number (see :meth:`passive.PassiveProduction.quantized_wait`,
    :func:`config.is_smeared_producer`).

    ``limit`` is the plan's makespan: a deposit boundary past it means this
    item is *fill* whose last sliver the plan absorbs through the channels that
    co-source it (see :func:`optimizer._quantize_plan_time`'s fill exemption),
    so the continuous estimate -- which fits the plan, exactly as reported
    before quantisation -- is shown instead of a wait the plan does not
    actually incur. Also falls back to the continuous estimate when
    ``production`` is absent (a hand-built plan/report call with no production
    passed) or the schedule genuinely has nothing for this item (should not
    happen for a real passive-offer key, since its ``rate`` is already
    positive, but stay defensive).
    """
    if production is not None:
        wait = production.quantized_wait(key, residual, config.is_smeared_producer)
        if wait is not None and (
            limit is None or wait <= limit + timedelta(seconds=1)
        ):
            return wait
    return timedelta(minutes=residual / rate)


def _tied_slowest(
    rows: list[tuple[str, int, timedelta, str]],
) -> list[tuple[str, int, timedelta, str]]:
    """The rows whose wait equals the longest, biggest-quantity-first.

    Deposit quantisation lands many items on the same boundary (Orange and
    Lemon both complete at exactly 7d), and which of an exact tie a picker
    happened to see first flapped run to run with the LP's alternate optima --
    so a tie is reported whole, in a deterministic order, rather than
    arbitrarily broken. Quantized waits are exact timedelta arithmetic, so
    equality (not a tolerance) is the right tie test.
    """
    if not rows:
        return []
    slowest = max(wait for _, _, wait, _ in rows)
    return sorted(
        (row for row in rows if row[2] == slowest),
        key=lambda row: (-row[1], row[0]),
    )


def _join_names(names: list[str]) -> str:
    """``"A"``, ``"A and B"``, ``"A, B and C"`` -- prose-joined names."""
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _slowest_passive_items(
    plan: optimizer.SourcingPlan,
    need: dict[str, float],
    elsewhere: dict[str, float],
    display: dict[str, str],
    production: passive.PassiveProduction | None = None,
    limit: timedelta | None = None,
) -> list[tuple[str, int, timedelta, str]]:
    """The passive item(s) that cap the plan's time: ``(name, quantity, wait, key)``.

    Each item's wait is its residual (what other channels and crafting did not
    cover) on the real deposit schedule (see :func:`_passive_wait`, to which
    ``limit`` -- the plan's makespan -- forwards, so a fill item's boundary past
    the plan shows its continuous estimate); the longest
    of these is the passive track's time, so it is what caps a passive-bound
    plan. Every item tied at that longest wait is returned (see
    :func:`_tied_slowest`); the list is empty when nothing qualifies.

    A pet slowly trickling out a small top-up is skipped, though: pets are a last
    resort, so unless a pet is the *primary* source of an item it should not be
    named as the critical path over a building doing the real work (a pet finishing
    a 2,302 residual of Horn just after a sawmill makes 13M Oak is co-maximal noise,
    not the bottleneck). Only if every passive item is such a pet top-up do we fall
    back to the slowest of them, so a genuinely pet-bound plan still reports one.
    """
    passive_draw = plan.passive_draw
    if passive_draw is None:
        return []
    offer = passive_draw.offer
    real: list[tuple[str, int, timedelta, str]] = []  # non-pet-top-up candidates
    topups: list[tuple[str, int, timedelta, str]] = []  # pet top-ups, backup
    for key, rate in offer.yields.items():
        if rate <= 0:
            continue
        total_need = need.get(key, 0.0)
        residual = total_need - elsewhere.get(key, 0.0)
        if residual <= 1e-6:
            continue
        wait = _passive_wait(production, key, residual, rate, limit)
        row = (display.get(key, key), round(residual), wait, key)
        # A pet top-up: pets make most of this item's passive supply yet supply
        # less than half of the item overall (another channel is its main source).
        pet_rate = _pet_passive_rate(production, key)
        pet_made = residual * (pet_rate / rate)
        if pet_rate > rate / 2 and pet_made < total_need / 2:
            topups.append(row)
        else:
            real.append(row)
    return _tied_slowest(real or topups)


def _num_width(counts: Iterable[int]) -> int:
    """Width of the widest comma-formatted count, for right-aligning a column."""
    return max((len(f"{n:,}") for n in counts), default=0)


def _farming_drives(
    plan: optimizer.SourcingPlan,
    need: dict[str, float],
    elsewhere: dict[str, float],
    display: dict[str, str],
    production: passive.PassiveProduction | None = None,
    sourceable_elsewhere: set[str] | None = None,
) -> bool:
    """Whether the farming track, not passive production, sets the makespan.

    Farming that reaches the makespan is usually the driver, but if its longest
    crop is fill that a consumable channel could supply and a passive item is
    co-maximal, that passive item drives instead (see :func:`print_critical_path`).
    Both the critical-path line and the passive section's ordering use this so they
    agree on which track is the bottleneck. The makespan itself takes ``plan.time``
    into account alongside the draws' own track times (see the matching comment
    in :func:`_critical_path_summary`), so quantization pushing the makespan past
    the farming track correctly makes farming no longer co-maximal here too --
    passive is named the driver instead of a farming track that only looked
    co-maximal against the unquantized time.
    """
    makespan = _plan_makespan(plan)
    if makespan <= timedelta(0):
        return False
    # Whole-grow farming time, matching what _plan_makespan quotes: comparing the
    # continuous track time against a rounded-up makespan would read a
    # farming-driven plan as "farming below makespan" and hand the driver to
    # passive (see _plan_makespan / _farming_grow_time).
    farming_time = _farming_grow_time(plan)
    if farming_time <= timedelta(0):
        return False  # no farming to drive the makespan (a sub-second tolerance on a
        # near-instant plan could otherwise round zero farming up to co-maximal)
    tolerance = max(timedelta(seconds=1), makespan * _FARMING_COMAX_MARGIN)
    if farming_time < makespan - tolerance:
        return False
    items = _slowest_passive_items(
        plan, need, elsewhere, display, production, limit=makespan
    )
    passive_comaximal = bool(items) and items[0][2] >= makespan - tolerance
    return not (passive_comaximal and _binding_crop_is_fill(plan, sourceable_elsewhere))


def _harvest_alls(count: int) -> str:
    """``count`` "Harvest All"s (the full-field harvest action), pluralised."""
    return f"{count:,} Harvest All{'' if count == 1 else 's'}"


def _crop_of(offer: sources.Offer) -> str:
    """The crop a farming offer grows, from its ``grow X[ variant]`` name.

    The single place farming offer names are parsed; the ``with cookies`` /
    ``without grape juice`` variant suffixes (an offer has at most one) are
    stripped, leaving the crop's display name.
    """
    return (
        offer.name.removeprefix("grow ")
        .removesuffix(" with cookies")
        .removesuffix(" without grape juice")
    )


def _farming_action(draw: sources.Draw) -> str:
    """The action and crop a farming draw stands for, cookie days included."""
    crop = _crop_of(draw.offer)
    if draw.offer.cookies:
        # A partial cookie set still consumes a whole one, so round days (and the
        # sets they stand for) up — never down.
        days = math.ceil(draw.units)
        noun = "day" if days == 1 else "days"
        return f"{days:,} cookie-boosted {noun} of {crop}"
    return f"{_harvest_alls(math.ceil(draw.units))} of {crop}"


def _farming_bottleneck(plan: optimizer.SourcingPlan, total: timedelta) -> str:
    """Describe the farming sequence that runs the makespan (grown one at a time)."""
    farming_draws = [d for d in plan.draws if d.offer.track == sources.FARMING_TRACK]
    biggest = max(farming_draws, key=lambda d: d.time)
    duration = passive.format_duration(total)
    if len(farming_draws) == 1:
        return (
            f"{_farming_action(biggest)} ({duration}) — crops grow one at a time"
        )
    return (
        f"{len(farming_draws)} crops grown back-to-back ({duration}), "
        f"longest {_farming_action(biggest)}"
    )


def _binding_crop_is_fill(
    plan: optimizer.SourcingPlan, sourceable_elsewhere: set[str] | None
) -> bool:
    """Whether the longest-grown crop could instead be sourced with consumables.

    A crop that fishing, exploring or opening can supply is grown only because
    doing so is free and fits inside a wait another track already forces -- so the
    farming that reaches the makespan is fill, not the real driver (see
    :func:`print_critical_path`). A crop with no such source (Pine, say) is
    genuinely farming-bound.
    """
    if not sourceable_elsewhere:
        return False
    farming = [d for d in plan.draws if d.offer.track == sources.FARMING_TRACK]
    if not farming:
        return False
    biggest = max(farming, key=lambda d: d.time)
    return _crop_of(biggest.offer).lower() in sourceable_elsewhere


def _defining_passive_resources(
    plan: optimizer.SourcingPlan,
    residuals: dict[str, float],
    display: dict[str, str],
    makespan: timedelta,
    tolerance: timedelta,
    max_passive_residuals: dict[str, float] | None,
    production: passive.PassiveProduction | None,
) -> list[tuple[str, int, timedelta, str]]:
    """The passive item(s) at full requirement whose completion sets the makespan.

    A material like Steel Wire is needed only up to its products' full demand;
    gather that much passively and no more is required, so the time it takes is a
    hard gate -- and a plan clamped there is *its* plan. It qualifies when its
    ``residuals`` entry has reached its full (cheapest-plan) requirement and it
    completes at the makespan; a resource still being sped up sits below its full
    requirement, and one that finishes early is not co-maximal. A pet-made
    material is skipped -- its trickle is a last resort, and the pet-primary case
    is handled by :func:`_slowest_passive_items`. Every qualifying item tied at
    the longest wait is returned (see :func:`_tied_slowest`; deposit boundaries
    make exact ties common); empty when none qualifies. Shared by the
    critical-path line and the frontier's per-plan reason (see
    :func:`print_frontier`).
    """
    if not max_passive_residuals:
        return []
    passive_draw = plan.passive_draw
    if passive_draw is None:
        return []
    rows: list[tuple[str, int, timedelta, str]] = []
    for key, rate in passive_draw.offer.yields.items():
        if rate <= 0:
            continue
        residual = residuals.get(key, 0.0)
        full = max_passive_residuals.get(key, 0.0)
        if residual <= 1e-6 or full <= 0:
            continue
        if residual < full * (1 - _FULL_REQUIREMENT_MARGIN):
            continue  # still being sped up with consumables, not the gate
        wait = _passive_wait(production, key, residual, rate, makespan)
        if wait < makespan - tolerance:
            continue  # finishes early, so it is not what caps the plan
        if _pet_passive_rate(production, key) > rate / 2:
            continue  # a pet trickle, not a real gathering plateau
        rows.append((display.get(key, key), round(residual), wait, key))
    return _tied_slowest(rows)


def _passive_residuals_of(
    plan: optimizer.SourcingPlan, need: dict[str, float], elsewhere: dict[str, float]
) -> dict[str, float]:
    """Each passive item's residual (net requirement) in ``plan``, from need/elsewhere."""
    passive_draw = plan.passive_draw
    if passive_draw is None:
        return {}
    return {
        key: need.get(key, 0.0) - elsewhere.get(key, 0.0)
        for key in passive_draw.offer.yields
    }


def _critical_path_summary(
    plan: optimizer.SourcingPlan,
    need: dict[str, float],
    elsewhere: dict[str, float],
    display: dict[str, str],
    production: passive.PassiveProduction | None = None,
    sourceable_elsewhere: set[str] | None = None,
    max_passive_residuals: dict[str, float] | None = None,
) -> tuple[timedelta, str] | None:
    """The track that truly caps how fast the plan can run: ``(makespan, line)``.

    Tracks run concurrently, so the total time is the slowest of them. Fishing and
    exploring are instant, so that leaves farming and passive production. They are
    not symmetric, though: passive runs for free alongside everything, so it
    opportunistically fills whatever wait another track already forces -- an item
    that could be fished or explored instead is made passively just to save a
    consumable, expanding the passive track to exactly the makespan. So when
    farming is (co-)maximal it is usually the real driver and the passive track
    beside it is fill.

    The reverse happens too, though: on a plan clamped to a passive item's
    completion, farming grows free crops to fill the passive-forced wait. So if the
    longest-grown crop could itself be sourced with consumables (it is fill, see
    :func:`_binding_crop_is_fill`) and a passive item is co-maximal, that passive
    item is the driver instead. Naming the driver shows what to attack (source it
    with consumables, or pick a faster plan) to finish sooner. Shared by
    :func:`print_critical_path` and :func:`markdown_plan` so both name the same
    driver from the same logic.
    """
    # Concurrent tracks; the makespan is the longest one -- see _plan_makespan
    # for why plan.time joins the draws' own track times (quantized deposit
    # boundaries live only on plan.time).
    makespan = _plan_makespan(plan)
    if makespan <= timedelta(0):
        return None  # everything is instant; there is no wait to explain

    tolerance = max(timedelta(seconds=1), makespan * _FARMING_COMAX_MARGIN)
    defining = _defining_passive_resources(
        plan, _passive_residuals_of(plan, need, elsewhere), display, makespan,
        tolerance, max_passive_residuals, production,
    )
    if defining:
        # Resource(s) gathered to their full requirement exactly here: their
        # completion is the gate, and this is their plan (more time gathers no
        # more). Deposit boundaries make exact ties common; name them all.
        counts = _join_names([f"{q:,}x {name}" for name, q, _, _ in defining])
        wait = defining[0][2]
        suffix = (
            "its full requirement" if len(defining) == 1
            else "their full requirements"
        )
        line = (
            f"{counts} from passive production "
            f"({passive.format_duration(wait)}) — {suffix}, "
            "after which no more is needed"
        )
    elif _farming_drives(
        plan, need, elsewhere, display, production, sourceable_elsewhere
    ):
        line = _farming_bottleneck(plan, makespan)
    elif items := _slowest_passive_items(
        plan, need, elsewhere, display, production, limit=makespan
    ):
        # Passive sets the pace: it is the sole longest track, or farming beside it
        # is merely filling the wait these items force with free crops.
        counts = _join_names([f"{q:,}x {name}" for name, q, _, _ in items])
        wait = items[0][2]
        suffix = (
            "the slowest item to build up" if len(items) == 1
            else "the slowest items to build up"
        )
        line = (
            f"{counts} from passive production "
            f"({passive.format_duration(wait)}) — {suffix}"
        )
    else:
        return None
    return makespan, line


def print_critical_path(
    plan: optimizer.SourcingPlan,
    need: dict[str, float],
    elsewhere: dict[str, float],
    display: dict[str, str],
    production: passive.PassiveProduction | None = None,
    sourceable_elsewhere: set[str] | None = None,
    max_passive_residuals: dict[str, float] | None = None,
) -> None:
    """Highlight the track that truly caps how fast the plan can run.

    See :func:`_critical_path_summary` for the decision logic.
    """
    summary = _critical_path_summary(
        plan, need, elsewhere, display, production, sourceable_elsewhere,
        max_passive_residuals,
    )
    if summary is None:
        return
    makespan, line = summary
    print(f"\nCritical path (sets the {passive.format_duration(makespan)} total):")
    print(f"  {line}")


def _apportion(quantity: int, weights: list[float]) -> list[int]:
    """Split ``quantity`` into integer parts proportional to ``weights``.

    Uses the largest-remainder method, so the parts always sum to exactly
    ``quantity`` with no rounding drift across the sources.
    """
    total = sum(weights)
    if total <= 0:
        return [0] * len(weights)
    raw = [quantity * w / total for w in weights]
    parts = [math.floor(r) for r in raw]
    for i in sorted(
        range(len(raw)), key=lambda i: (raw[i] - parts[i], -i), reverse=True
    )[: quantity - sum(parts)]:
        parts[i] += 1
    return parts


def _season_note(producer_name: str) -> str:
    """A bracketed active-months window for a seasonal pet, else empty.

    Seasonal pets only fetch during a short calendar window, so their output is
    modelled as a long-run yearly average (see :func:`config._pet_producers`) --
    which is why such a pet contributes less than a year-round one. Flagging the
    window in the report explains that reduced share (and that shifting the plan
    into or out of that window would change it in-game).
    """
    key = producer_name.removeprefix(config.PET_PRODUCER_PREFIX).lower()
    months = pets.SEASONAL_MONTHS.get(key)
    if not months:
        return ""
    lo, hi = min(months), max(months)
    span = (
        calendar.month_abbr[lo]
        if lo == hi
        else f"{calendar.month_abbr[lo]}–{calendar.month_abbr[hi]}"
    )
    return f" [seasonal: {span}]"


def _smeared_note(producer_name: str) -> str:
    """A ``(smeared)`` flag for the Mechanical Heart producer, else empty.

    The monthly tower-artifact grants arrive as a lump on a fixed day, but are
    modelled as an average daily rate spread across the plan (see
    :func:`config._monthly_grant_producers`), so the reported quantity is that
    smear, not the day's real bundle.
    """
    return " (smeared)" if producer_name == config.MONTHLY_GRANT_PRODUCER else ""


def _passive_sources(
    production: passive.PassiveProduction, item: str, quantity: int
) -> str:
    """Name the passive producer(s) that make ``quantity`` of ``item``.

    A single producer is named plainly (``(from Windmill)``); when several make
    the item, the quantity is broken down between them by their per-minute rate
    share (which is exactly each one's share of the shared wait), largest first.
    A seasonal pet contributes less because its output is a yearly average, and
    the breakdown flags its active window (see :func:`_season_note`). Returns an
    empty string when nothing known produces the item.
    """
    contributors = [(rate, name) for name, rate in production.producer_rates(item)]
    if not contributors:
        return ""
    if len(contributors) == 1:
        name = contributors[0][1]
        return f" (from {name}{_season_note(name)}{_smeared_note(name)})"
    counts = _apportion(quantity, [rate for rate, _ in contributors])
    labelled = sorted(
        ((count, rate, name) for count, (rate, name) in zip(counts, contributors)),
        key=lambda t: (-t[0], -t[1], t[2]),  # biggest share, then rate, then name
    )
    parts = [
        f"{count:,} from {name}{_season_note(name)}{_smeared_note(name)}"
        for count, _, name in labelled
    ]
    return " (" + ", ".join(parts) + ")"


def _print_cookie_farming(
    draw: sources.Draw,
    juices: float,
    interesting: set[str],
    display: dict[str, str],
    width: int = 0,
) -> None:
    """Render one cookie-boosted farming draw: a crop grown under cookie sets.

    A cookie set covers a whole real day, boosting the day's Grape Juice
    allocation :data:`crops.COOKIE_SET_MULTIPLIER`-fold. Sets are a farm-wide
    daily resource shared across crops, so the whole-set count lives in the
    Farming heading; each crop line just shows its span, juice and yield.
    """
    crop = _crop_of(draw.offer)
    line = f"  Cookie-boosted {crop} in {passive.format_duration(draw.time)}"
    if round(juices) >= 1:
        line += (
            f" using {round(juices):,} Grape Juice"
            f" (yield {crops.COOKIE_SET_MULTIPLIER}x)"
        )
    print(f"{line}:")
    for key in sorted(k for k in draw.offer.yields if k in interesting):
        print(f"    {round(draw.produced[key]):>{width},}x {display.get(key, key)}")


def build_display(
    demand: dict[str, float],
    workshop: crafting.Workshop,
    openable_names: dict[str, str] | None = None,
) -> dict[str, str]:
    """Lower-cased key -> original-case display name for everything reportable.

    Covers the demand, every recipe's item and its materials, and (when given)
    the openable names, keys and contents. Shared by :func:`run` (for the frontier
    reasons) and :func:`print_plan`, which additionally passes ``openable_names``.
    """
    display = {item.lower(): item for item in demand}
    for key, recipe in workshop.recipes.items():
        display.setdefault(key, recipe.name)
        for material in recipe.materials:
            display.setdefault(material.lower(), material)
    for key, name in (openable_names or {}).items():
        display.setdefault(key, name)
    return display


def _print_surplus_value(
    plan: optimizer.SourcingPlan,
    demand: dict[str, float],
    recipes: dict[str, dict[str, float]],
    crafting_bonus: float,
    open_flows: list[sources.OpenFlow],
    inventory: dict[str, float],
    prices: dict[str, float],
    display: dict[str, str],
) -> None:
    """The gold a plan's *accumulated* surplus is worth if crafted into sellable goods.

    Passive production overshoots every resource but the slowest, so a plan banks
    materials far past what its goal needs. Only what the plan itself accumulates
    counts (:func:`optimizer.accumulated_leftovers` -- stock already held is netted
    off, since an untouched stockpile is not surplus the plan produced): assuming
    those are crafted into the sellable priced goods -- Arnold Palmers, Large Nets,
    Apple Cider, Orange Juice -- :func:`optimizer.surplus_sale_value` finds the most
    gold they are worth. Lists each good and the nominal total, matching the frontier
    row's banked-surplus credit (:func:`optimizer.plan_surplus_gold`).
    """
    leftovers = optimizer.accumulated_leftovers(
        plan, demand, recipes, crafting_bonus, open_flows, inventory
    )
    rows = optimizer.surplus_sale_value(leftovers, recipes, crafting_bonus, prices)
    if not rows:
        return
    total = sum(gold for _, _, gold in rows)
    print("\nSurplus (accumulates past the goal), craftable into sellable goods:")
    width = _num_width(round(count) for _, count, _ in rows)
    for item, count, gold in rows:
        print(
            f"  {round(count):>{width},}x {display.get(item, item)} = {round(gold):,} g"
        )
    print(f"  nominal value = {round(total):,} g")
    print(
        "  banked byproduct: what the plan produces past the goal (loot from the"
        " exploring/passive it runs), already credited into its net gold on the"
        " frontier -- not gold conjured on top of it."
    )


def markdown_surplus_value(
    doc: MarkdownDoc,
    plan: optimizer.SourcingPlan,
    demand: dict[str, float],
    recipes: dict[str, dict[str, float]],
    crafting_bonus: float,
    open_flows: list[sources.OpenFlow],
    inventory: dict[str, float],
    prices: dict[str, float],
    display: dict[str, str],
) -> None:
    """Markdown counterpart to :func:`_print_surplus_value`."""
    leftovers = optimizer.accumulated_leftovers(
        plan, demand, recipes, crafting_bonus, open_flows, inventory
    )
    rows = optimizer.surplus_sale_value(leftovers, recipes, crafting_bonus, prices)
    if not rows:
        return
    total = sum(gold for _, _, gold in rows)
    doc.heading("Surplus (accumulates past the goal), craftable into sellable goods")
    table_rows = [
        [display.get(item, item), f"{round(count):,}", f"{round(gold):,} g"]
        for item, count, gold in rows
    ]
    doc.table(["Item", "Qty", "Gold"], table_rows, align=["left", "right", "right"])
    doc.para(f"Nominal value: {round(total):,} g")
    doc.para(
        "Banked byproduct: what the plan produces past the goal (loot from the "
        "exploring/passive it runs), already credited into its net gold on the "
        "frontier -- not gold conjured on top of it."
    )


@dataclasses.dataclass
class _PlanContext:
    """Shared accounting for one plan's report, computed once for both renderers.

    See :func:`optimizer.plan_balance` for ``need``/``elsewhere``/``crafted``; the
    rest is derived from them the same way for :func:`print_plan` and
    :func:`markdown_plan`, so both sections and both renderers agree.
    """

    display: dict[str, str]
    recipes: dict[str, dict[str, float]]
    open_flows: list[sources.OpenFlow]
    flows_by_name: dict[str, sources.OpenFlow]
    need: dict[str, float]
    elsewhere: dict[str, float]
    crafted: dict[str, float]
    interesting: set[str]
    consumed: dict[str, float]
    spent_consumables: dict[str, float]
    open_worthy: set[str]


def _build_plan_context(
    plan: optimizer.SourcingPlan,
    demand: dict[str, float],
    workshop: crafting.Workshop,
    crafting_bonus: float,
    open_flows: list[sources.OpenFlow] | None,
    openable_names: dict[str, str] | None,
    inventory: dict[str, float] | None,
) -> _PlanContext:
    open_flows = open_flows or []
    inventory = inventory or {}
    flows_by_name = {flow.name.lower(): flow for flow in open_flows}
    recipes = crafting.recipe_flows(workshop)

    # Display names for everything that can appear (demand plus every recipe item
    # and material, plus openable names, keys and contents), keyed lower-cased.
    display = build_display(demand, workshop, openable_names)

    # What the plan must produce (need) and what the non-passive channels,
    # crafting, opening and stock already supply (elsewhere) -- the accounting the
    # passive track and the critical-path summary both work in. Shared with the
    # optimiser's residual maths (see :func:`optimizer.plan_balance`).
    need, elsewhere, crafted = optimizer.plan_balance(
        plan, demand, recipes, crafting_bonus, open_flows, inventory
    )
    # Only genuinely-needed items (demand, craft materials, and the chests/keys
    # opens consume) are worth showing; an openable's other contents are byproducts.
    interesting = set(need) | set(crafted)
    # Items a draw consumes directly -- Apples eaten, Orange Juice drunk, Large
    # Nets/Arnold Palmers/Apple Cider burned -- never reach crafting `need` (only
    # `plan.crafts` materials do), but exploring for one is still the reason a zone
    # is worth visiting, so the zone listing below counts them alongside `interesting`.
    consumed = optimizer.draw_consumed(plan)

    # Tools the plan spends (nets, palmers, cider, ...) are sourced then burned by
    # fishing/exploring/farming, so they never appear as crafting `need`. Track how
    # many of each the plan spends, so an open performed purely to restock one -- a
    # Mystical Chest opened for its Large Nets -- surfaces, as does the held portion.
    spent_consumables = {
        item: spent
        for item, spent in {
            "large net": plan.large_nets,
            "arnold palmer": plan.arnold_palmers,
            "apple cider": plan.apple_ciders,
            "orange juice": plan.orange_juice,
            "grape juice": plan.grape_juices,
            # Acorn Pies are a held meal the exploring channel burns (1/450 per
            # explore) but never yields, so unlike the tools above it is read off
            # the draws' consumes, not a plan tally; surfaced here so its held
            # spend shows in the From-inventory section like any other tool.
            sources.ACORN_PIE: consumed.get(sources.ACORN_PIE, 0.0),
            # Hickory Omelettes are a held/bought meal the sawmill-boost channel
            # burns (one per boosted hour) but never yields; like the Acorn Pie it
            # is read off the draws' consumes, so its held spend shows in the
            # From-inventory section and the bought portion in the Buy section.
            sources.HICKORY_OMELETTE: consumed.get(sources.HICKORY_OMELETTE, 0.0),
        }.items()
        if round(spent) >= 1
    }
    # Limited seeds (Mega/Spring) are a held-only input a planting burns (see
    # crops.Crop.seed_item) -- like the meals above they never reach crafting
    # `need`, so surface the spend here for the From-inventory section.
    for item, spent in consumed.items():
        if item.endswith(crops.SEED_ITEM_SUFFIX) and round(spent) >= 1:
            spent_consumables[item] = spent
            display.setdefault(item, item.title())
    display.setdefault(sources.ACORN_PIE, "Acorn Pie")
    display.setdefault(sources.HICKORY_OMELETTE, "Hickory Omelette")
    # An open is worth reporting if it yields a needed item, restocks a tool, or
    # gathers something a draw goes on to consume (an Apple-yielding chest opened
    # to feed the eat-Apple stamina flow, say) -- the same `consumed` union the
    # zone listings use, so an open done purely for a byproduct doesn't vanish.
    open_worthy = set(need) | set(spent_consumables) | set(consumed)

    return _PlanContext(
        display=display, recipes=recipes, open_flows=open_flows,
        flows_by_name=flows_by_name, need=need, elsewhere=elsewhere, crafted=crafted,
        interesting=interesting, consumed=consumed,
        spent_consumables=spent_consumables, open_worthy=open_worthy,
    )


def _wanted(ctx: _PlanContext, offer: sources.Offer) -> str:
    return ", ".join(
        sorted(ctx.display.get(key, key) for key in offer.yields if key in ctx.interesting)
    )


def _zone_label(name: str, draw: sources.Draw) -> str:
    """A zone/location label with the Acorn Pies a pie-boosted draw eats spelled out.

    A pie-boosted offer's name ends ``with acorn pie``; rewrite that to
    ``with N acorn pie`` where N is the whole pies the draw burns (rounded up --
    you eat whole pies, each covering 450 explores). A draw that burns no pie is
    returned unchanged.
    """
    pies = draw.units * draw.offer.consumes.get(sources.ACORN_PIE, 0.0)
    if pies <= 0:
        return name
    return name.replace("with acorn pie", f"with {math.ceil(pies):,} acorn pie")


def _wanted_counts(ctx: _PlanContext, draw: sources.Draw) -> str:
    return ", ".join(
        f"{round(draw.produced[key]):,}x {ctx.display.get(key, key)}"
        for key in sorted(draw.offer.yields)
        if key in ctx.interesting or key in ctx.consumed
    )


def _sourced(ctx: _PlanContext, draws: list[sources.Draw]) -> list[sources.Draw]:
    # Drop residue draws: CBC can leave a sliver of a net, palmer or planting on a
    # channel nothing needs, which rounds to zero produced in ~zero time or
    # consumable -- not a real action, so keep it out of the report.
    return [
        d
        for d in draws
        if any(
            round(d.produced[k]) >= 1
            for k in d.offer.yields
            if k in ctx.interesting or k in ctx.consumed
        )
    ]


def _draws_on(plan: optimizer.SourcingPlan, track: str) -> list[sources.Draw]:
    return [d for d in plan.draws if d.offer.track == track]


def _sawmill_boost_summary(
    plan: optimizer.SourcingPlan, ctx: _PlanContext
) -> tuple[int, timedelta, list[tuple[str, int]]] | None:
    """The Hickory Omelette sawmill boost this plan runs, or None if it runs none.

    Returns ``(omelettes, hours, yields)`` where ``omelettes`` is how many are
    eaten (one per boosted hour), ``hours`` the boosted span, and ``yields`` the
    extra items the boost produced ``[(display name, quantity), ...]``, biggest
    first. Both renderers print the same tuple (see the "Sawmill boost" section).
    """
    draws = _draws_on(plan, sources.SAWMILL_BOOST_TRACK)
    if not draws:
        return None
    hours = sum((d.time for d in draws), timedelta(0))
    omelettes = round(sum(d.offer.consumes.get(sources.HICKORY_OMELETTE, 0.0) * d.units
                          for d in draws))
    produced: dict[str, float] = {}
    for draw in draws:
        for item, qty in draw.produced.items():
            produced[item] = produced.get(item, 0.0) + qty
    yields = sorted(
        ((ctx.display.get(item, item), round(qty)) for item, qty in produced.items()
         if round(qty) >= 1),
        key=lambda kv: -kv[1],
    )
    if omelettes < 1 or not yields:
        return None
    return omelettes, hours, yields


def print_plan(
    plan: optimizer.SourcingPlan,
    demand: dict[str, float],
    farm: crops.Farm,
    workshop: crafting.Workshop,
    crafting_bonus: float,
    production: passive.PassiveProduction | None = None,
    open_flows: list[sources.OpenFlow] | None = None,
    openable_names: dict[str, str] | None = None,
    inventory: dict[str, float] | None = None,
    sourceable_elsewhere: set[str] | None = None,
    max_passive_residuals: dict[str, float] | None = None,
    prices: dict[str, float] | None = None,
    pumpkin_schedule: dict[str, list[int]] | None = None,
    skip_critical_path: bool = False,
    ap_prices: dict[str, float] | None = None,
) -> None:
    """Expand one plan into its per-channel actions and leftover materials.

    ``pumpkin_schedule`` (from :func:`main.pumpkin_juice_schedule`) gives, per goal
    item, the mastery levels at which to drink its Pumpkin Juices so each lands its
    intended tier boost; the Pumpkin Juice section names them. ``skip_critical_path``
    (ascension mode only, see :func:`print_ascension_plan`) omits the usual
    :func:`print_critical_path` call -- a stage waiting on daily chores prints its
    own override line instead, ahead of this call; :func:`print_critical_path`'s
    own precedence logic is untouched either way.
    """
    inventory = inventory or {}
    ctx = _build_plan_context(
        plan, demand, workshop, crafting_bonus, open_flows, openable_names, inventory
    )
    display, recipes = ctx.display, ctx.recipes
    open_flows = ctx.open_flows
    need, elsewhere = ctx.need, ctx.elsewhere
    spent_consumables = ctx.spent_consumables

    if not skip_critical_path:
        print_critical_path(
            plan, need, elsewhere, display, production, sourceable_elsewhere,
            max_passive_residuals,
        )

    fishing_draws = _sourced(ctx, _draws_on(plan, sources.FISHING_TRACK))
    if fishing_draws:
        print("\nFishing:")
        ordered = sorted(fishing_draws, key=lambda d: d.ln, reverse=True)
        width = _num_width(math.ceil(d.units) for d in ordered)
        for draw in ordered:
            zone = draw.offer.name.removeprefix("fish ")
            print(
                f"  {math.ceil(draw.units):>{width},}x Large Net at {zone}"
                f" ({_wanted(ctx, draw.offer)})"
            )

    exploring_draws = _sourced(ctx, _draws_on(plan, sources.EXPLORING_TRACK))
    # Two ways to explore: Arnold Palmers (ap cost / consumes a palmer) and, in
    # the inventory plan mode, Apple Cider (consumes stamina and part of a cider).
    palmer_draws = [
        d for d in exploring_draws if not d.offer.consumes.get("apple cider")
    ]
    cider_draws = [d for d in exploring_draws if d.offer.consumes.get("apple cider")]
    if palmer_draws:
        print("\nExploring (Arnold Palmer):")
        ordered = sorted(palmer_draws, key=lambda d: d.ap, reverse=True)
        width = _num_width(math.ceil(d.units) for d in ordered)
        for draw in ordered:
            location = _zone_label(draw.offer.name.removeprefix("explore "), draw)
            print(
                f"  {math.ceil(draw.units):>{width},}x Arnold Palmer at {location}"
                f" ({_wanted_counts(ctx, draw)})"
            )
    if cider_draws:
        print("\nExploring (Apple Cider):")
        # A cider draw's units are explores; ciders used and stamina spent are its
        # per-explore consumes scaled up.
        def ciders(d: sources.Draw) -> float:
            return d.units * d.offer.consumes.get("apple cider", 0.0)

        ordered = sorted(cider_draws, key=ciders, reverse=True)
        width = _num_width(math.ceil(ciders(d)) for d in ordered)
        for draw in ordered:
            zone = _zone_label(draw.offer.name.removeprefix("cider "), draw)
            stamina = draw.units * draw.offer.consumes.get("stamina", 0.0)
            print(
                f"  {math.ceil(ciders(draw)):>{width},}x Apple Cider at {zone}"
                f" ({-round(stamina):,} stamina, {_wanted_counts(ctx, draw)})"
            )

    # Stamina for Apple Cider: what the ciders spend, and where it comes from --
    # the banked start (--current-stamina), the Farmhouse's daily passive deposit,
    # and any items consumed to top it up (Orange Juice drunk, Apples eaten).
    drinking_draws = _draws_on(plan, sources.DRINKING_TRACK)
    cider_stamina = sum(
        d.units * d.offer.consumes.get("stamina", 0.0) for d in cider_draws
    )
    if round(cider_stamina) >= 1:
        print("\nStamina:")
        print(f"  {round(cider_stamina):,} spent on Apple Cider")
        banked = inventory.get("stamina", 0.0)
        if round(banked) >= 1:
            print(f"  {round(banked):,} banked at the start")
        passive_draw = plan.passive_draw
        rate = (
            passive_draw.offer.yields.get("stamina", 0.0)
            if passive_draw is not None
            else 0.0
        )
        if rate > 0:
            print(f"  {round(rate * 1440):,}/day from the Farmhouse (passive)")
        # Each drinking-track draw consumes one item for stamina; tally the units
        # and stamina per item so Orange Juice and Apples are reported apart.
        topups: dict[str, list[float]] = {}
        for draw in drinking_draws:
            for item, per in draw.offer.consumes.items():
                units, gained = topups.setdefault(item, [0.0, 0.0])
                topups[item] = [
                    units + per * draw.units,
                    gained + draw.produced.get("stamina", 0.0),
                ]
        verbs = {"orange juice": "Drink", "apple": "Eat"}
        for item, (units, gained) in sorted(
            topups.items(), key=lambda kv: -kv[1][1]
        ):
            if round(units) < 1:
                continue
            verb = verbs.get(item, "Use")
            print(
                f"  {verb} {round(units):,}x {display.get(item, item)}"
                f" -> {round(gained):,} stamina"
            )

    # Opening chests and bags at the Locksmith. Skip residue opens that yield less
    # than one of anything wanted (CBC can leave a sliver, as with draws).
    opens = [
        (name, count)
        for name, count in plan.opens.items()
        if (flow := ctx.flows_by_name.get(name.lower())) is not None
        and any(
            round(flow.yields[item] * count) >= 1
            for item in flow.yields
            if item in ctx.open_worthy
        )
    ]
    if opens:
        print("\nOpening (Locksmith):")
        ordered = sorted(opens, key=lambda nc: nc[1], reverse=True)
        width = _num_width(math.ceil(count) for _, count in ordered)
        for name, count in ordered:
            flow = ctx.flows_by_name[name.lower()]
            keys = [
                f"{round(quantity * count):,} {display.get(item, item)}"
                for item, quantity in flow.consumes.items()
                if item != name.lower() and round(quantity * count) >= 1
            ]
            cost = f" using {', '.join(keys)}" if keys else ""
            gained = ", ".join(
                f"{round(flow.yields[item] * count):,}x {display.get(item, item)}"
                for item in sorted(flow.yields)
                if item in ctx.open_worthy and round(flow.yields[item] * count) >= 1
            )
            detail = f" -> {gained}" if gained else ""
            print(
                f"  {math.ceil(count):>{width},}x"
                f" {display.get(name.lower(), name)}{cost}{detail}"
            )

    farming_draws = _sourced(ctx, _draws_on(plan, sources.FARMING_TRACK))
    if farming_draws:
        # Crops grow one at a time, so the whole track's time is their sum.
        total_farming = sum((d.time for d in farming_draws), timedelta(0))
        heading = f"\nFarming ({passive.format_duration(total_farming)}"
        # One cookie set covers one real day, shared across whatever crops are
        # harvested that day, so the count is the whole days the cookie-boosted
        # farming spans (ceil of the total) — not each crop's days rounded up.
        total_sets = math.ceil(sum(d.cookies for d in farming_draws if d.cookies))
        if total_sets >= 1:
            heading += f", {total_sets:,} cookie sets"
        print(heading + "):")
        # Right-align the yield counts across the whole section (every crop's
        # produced items), so the "x" lines up regardless of which crop.
        width = _num_width(
            round(d.produced[key])
            for d in farming_draws
            for key in d.offer.yields
            if key in ctx.interesting
        )
        for draw in sorted(farming_draws, key=lambda d: d.time, reverse=True):
            juices = draw.gj  # LP-tracked; zero for the without-grape-juice offer
            if draw.offer.cookies:
                _print_cookie_farming(draw, juices, ctx.interesting, display, width)
                continue
            crop = _crop_of(draw.offer)
            line = (
                f"  {_harvest_alls(math.ceil(draw.units))} of {crop}"
                f" in {passive.format_duration(draw.time)}"
            )
            if round(juices) >= 1:
                line += f" using {round(juices):,} Grape Juice"
            print(f"{line}:")
            # Grape-juice and natural harvests yield the same bundle, so the
            # grape-juice share of the plantings splits every produced item the
            # same way.
            gj_share = juices / draw.units if draw.units else 0.0
            for key in sorted(k for k in draw.offer.yields if k in ctx.interesting):
                total = draw.produced[key]
                name = display.get(key, key)
                if round(juices) >= 1:
                    from_gj = total * gj_share
                    print(
                        f"    {round(total):>{width},}x {name}"
                        f" ({round(from_gj):,} from Grape Juice,"
                        f" {round(total - from_gj):,} from normal farming)"
                    )
                else:
                    print(f"    {round(total):>{width},}x {name}")

    passive_draw = plan.passive_draw
    if passive_draw is not None:
        offer = passive_draw.offer
        # Passive only covers what the other channels and crafting did not (see
        # `elsewhere` above), so an item fully fished, explored or crafted adds no
        # passive wait. Each item's own wait is its residual over its per-minute
        # rate; the longest is the whole track's time, so listing them longest-first
        # shows what drives it.
        makespan = _plan_makespan(plan)
        rows = []
        for key, rate in offer.yields.items():
            if rate <= 0:
                continue
            residual = need.get(key, 0.0) - elsewhere.get(key, 0.0)
            if residual <= 1e-6:
                # A consumed tool (Arnold Palmer / Apple Cider / ...) is never a
                # crafting `need`, so its `need - elsewhere` residual is not what
                # passive contributes -- but the passive track can still make it
                # (the Mechanical Heart's monthly grants, a pet's haul), free
                # supply that offsets what the channels must gather. Show the
                # amount passive actually produced, capped at what the plan burns.
                produced = passive_draw.produced.get(key, 0.0)
                if key not in ctx.consumed or produced <= 1e-6:
                    continue
                residual = min(produced, ctx.consumed[key])
                # An LP-epsilon consumption (a plan that spends "0 AP" often
                # burns a stray fraction) would print as a "0x ... in 0m" line.
                if round(residual) < 1:
                    continue
            wait = _passive_wait(production, key, residual, rate, makespan)
            rows.append((wait, display.get(key, key), round(residual), key))
        if rows:
            print("\nPassive production (concurrent):")
            # List the item(s) on the critical path first (only when passive is
            # the plan's bottleneck track), then the rest by quantity, then by how
            # long they take. The critical items are whatever print_critical_path
            # names -- not simply the slowest, since a pet top-up is skipped over
            # a building -- and an exact tie is a set, not one arbitrary pick.
            critical_keys: set[str] = set()
            if not _farming_drives(
                plan, need, elsewhere, display, production, sourceable_elsewhere
            ):
                critical_keys = {
                    key for _, _, _, key in _slowest_passive_items(
                        plan, need, elsewhere, display, production, limit=makespan
                    )
                }
            width = _num_width(quantity for _, _, quantity, _ in rows)
            # Final name tie-break: rows tied on quantity and wait otherwise sit
            # in yields-iteration order, which flaps run to run with the LP's
            # alternate optima (hash-seeded set orderings upstream).
            ordered = sorted(
                rows, key=lambda r: (r[3] not in critical_keys, -r[2], -r[0], r[1])
            )
            for wait, name, quantity, key in ordered:
                source = (
                    _passive_sources(production, key, quantity)
                    if production is not None
                    else ""
                )
                print(
                    f"  {quantity:>{width},}x {name}"
                    f" in {passive.format_duration(wait)}{source}"
                )

    boost = _sawmill_boost_summary(plan, ctx)
    if boost is not None:
        omelettes, hours, yields = boost
        extra = ", ".join(f"+{qty:,} {name}" for name, qty in yields)
        print("\nSawmill boost (Hickory Omelette):")
        print(
            f"  {omelettes:,} Hickory Omelette"
            f"{'s' if omelettes != 1 else ''} over {passive.format_duration(hours)}:"
            f" {extra}"
        )

    # Consumable tools drawn from held stock (spent before any are crafted, opened
    # or bought). Surfacing these makes the spend tallies reconcile: a plan can burn
    # far more Large Nets than it makes or opens because it held a stack to begin
    # with -- otherwise those nets look like they came from nowhere.
    held = [
        (item, min(inventory.get(item, 0.0), spent))
        for item, spent in spent_consumables.items()
        if round(min(inventory.get(item, 0.0), spent)) >= 1
    ]
    if held:
        print("\nFrom inventory (held consumables):")
        width = _num_width(round(units) for _, units in held)
        for item, units in sorted(held, key=lambda kv: -kv[1]):
            print(f"  {round(units):>{width},}x {display.get(item, item)}")

    ap_prices = ap_prices or {}
    purchases = [(item, units) for item, units in plan.bought.items() if round(units) >= 1]
    # Split buys into gold purchases (the consumables) and Arnold-Palmer trades (the
    # resources priced only in AP -- an item in ap_prices). Both fold into plan.gold
    # (a trade's gold-equivalent is AP-converted), but they read differently.
    gold_buys = [(i, u) for i, u in purchases if i not in ap_prices]
    trades = [(i, u) for i, u in purchases if i in ap_prices]
    if gold_buys and prices is not None:
        print("\nBuy (with gold):")
        ordered = sorted(
            gold_buys, key=lambda kv: kv[1] * prices.get(kv[0], 0.0), reverse=True
        )
        width = _num_width(round(units) for _, units in gold_buys)
        total = 0.0
        for item, units in ordered:
            cost = round(units * prices.get(item, 0.0))
            total += units * prices.get(item, 0.0)
            print(
                f"  {round(units):>{width},}x {display.get(item, item)}"
                f" = {cost:,} g"
            )
        print(f"  total {round(total):,} g")

    # Trade (for Arnold Palmers): the resources bought with AP rather than gold.
    # Each line shows the AP it costs and the gold that AP is worth (its ranking
    # cost); the section totals both so a plan says how many Arnold Palmers it needs.
    if trades:
        print("\nTrade (for Arnold Palmers):")
        ordered = sorted(trades, key=lambda kv: kv[1] * ap_prices.get(kv[0], 0.0), reverse=True)
        width = _num_width(round(units) for _, units in trades)
        total_ap = 0.0
        total_gold = 0.0
        for item, units in ordered:
            ap = units * ap_prices.get(item, 0.0)
            gold = units * (prices or {}).get(item, 0.0)
            total_ap += ap
            total_gold += gold
            print(
                f"  {round(units):>{width},}x {display.get(item, item)}"
                f" = {round(ap):,} AP ({round(gold):,} g)"
            )
        print(f"  total {round(total_ap):,} AP ({round(total_gold):,} g)")

    # Pumpkin Juice: a bought mastery boost, listed apart from the consumable buys
    # since it covers a goal's mastery directly rather than feeding a channel. Each
    # line names the goal it boosts, how many juices, and the gold and mastery gained.
    juices = [
        (item, units) for item, units in plan.pumpkin_juices.items() if round(units) >= 1
    ]
    # Gated on the juices, never on their gold: at a `trading.pumpkin_juice` of 0 the
    # juice is free, and hiding the section then dropped a plan's whole mastery boost
    # out of the report -- the very plans that lean on juice hardest.
    if juices:
        total_juices = _pumpkin_juices(plan)
        per_juice = plan.pumpkin_gold / total_juices if total_juices else 0.0
        print("\nPumpkin Juice (mastery boost):")
        width = _num_width(round(units) for _, units in juices)
        for item, units in sorted(juices, key=lambda kv: -kv[1]):
            cost = round(units * per_juice)
            name = display.get(item, item)
            line = f"  {round(units):>{width},}x Pumpkin Juice on {name} = {cost:,} g"
            # A juice grants 10% of the current tier, so it only lands its full boost
            # drunk late, in the goal's final tier -- name those mastery levels.
            levels = (pumpkin_schedule or {}).get(item)
            if levels:
                drink = " / ".join(f"{level:,}" for level in levels)
                line += f"\n    drink at {name} mastery {drink}"
            print(line)
        print(f"  total {round(plan.pumpkin_gold):,} g")

    # Stamina opportunity cost: the free (banked + passive) stamina spent on Apple
    # Cider explores, valued at the Orange-Juice equivalent (see
    # optimizer._build_base). Not a purchase -- so it sits apart from the buys --
    # but a real cost the plan is priced and ranked on.
    _print_stamina_cost(plan, prices)

    # Every consumable is grounded now, so the plan self-sources exactly what it
    # needs (net of buying) and the leftovers are genuine spare capacity -- the
    # surplus value is rigorous, shown whenever consumables are priced.
    if prices is not None:
        _print_surplus_value(
            plan, demand, recipes, crafting_bonus, open_flows, inventory, prices,
            display,
        )
    # plan.remainder (materials with no known source) is reported once, up in
    # the Goal section (print_solution/markdown_solution) rather than here --
    # it's identical across every plan in the frontier (a property of demand
    # vs. obtainability, not this plan's own choices), and reads better before
    # the frontier/plan detail than after it.


def markdown_plan(
    doc: MarkdownDoc,
    plan: optimizer.SourcingPlan,
    demand: dict[str, float],
    farm: crops.Farm,
    workshop: crafting.Workshop,
    crafting_bonus: float,
    production: passive.PassiveProduction | None = None,
    open_flows: list[sources.OpenFlow] | None = None,
    openable_names: dict[str, str] | None = None,
    inventory: dict[str, float] | None = None,
    sourceable_elsewhere: set[str] | None = None,
    max_passive_residuals: dict[str, float] | None = None,
    prices: dict[str, float] | None = None,
    pumpkin_schedule: dict[str, list[int]] | None = None,
    skip_critical_path: bool = False,
    ap_prices: dict[str, float] | None = None,
) -> None:
    """Markdown counterpart to :func:`print_plan`: the same sections, as tables.

    Shares :func:`_build_plan_context` and every decision helper (critical path,
    farming-drives, passive sourcing) with :func:`print_plan`, so the two renderers
    always agree on content -- only the layout (table vs. aligned text) differs.
    ``skip_critical_path`` mirrors :func:`print_plan`'s own parameter.
    """
    inventory = inventory or {}
    ctx = _build_plan_context(
        plan, demand, workshop, crafting_bonus, open_flows, openable_names, inventory
    )
    display = ctx.display
    need, elsewhere = ctx.need, ctx.elsewhere

    if not skip_critical_path:
        summary = _critical_path_summary(
            plan, need, elsewhere, display, production, sourceable_elsewhere,
            max_passive_residuals,
        )
        if summary is not None:
            makespan, line = summary
            doc.heading(f"Critical path (sets the {passive.format_duration(makespan)} total)")
            doc.para(line)

    fishing_draws = _sourced(ctx, _draws_on(plan, sources.FISHING_TRACK))
    if fishing_draws:
        ordered = sorted(fishing_draws, key=lambda d: d.ln, reverse=True)
        rows = [
            [
                f"{math.ceil(d.units):,}",
                d.offer.name.removeprefix("fish "),
                _wanted(ctx, d.offer),
            ]
            for d in ordered
        ]
        doc.heading("Fishing")
        doc.table(
            ["Large Nets", "Zone", "Yields"], rows, align=["right", "left", "left"]
        )

    exploring_draws = _sourced(ctx, _draws_on(plan, sources.EXPLORING_TRACK))
    palmer_draws = [
        d for d in exploring_draws if not d.offer.consumes.get("apple cider")
    ]
    cider_draws = [d for d in exploring_draws if d.offer.consumes.get("apple cider")]
    if palmer_draws:
        ordered = sorted(palmer_draws, key=lambda d: d.ap, reverse=True)
        rows = [
            [
                f"{math.ceil(d.units):,}",
                _zone_label(d.offer.name.removeprefix("explore "), d),
                _wanted_counts(ctx, d),
            ]
            for d in ordered
        ]
        doc.heading("Exploring (Arnold Palmer)")
        doc.table(
            ["Arnold Palmers", "Location", "Yields"],
            rows,
            align=["right", "left", "left"],
        )
    if cider_draws:
        def ciders(d: sources.Draw) -> float:
            return d.units * d.offer.consumes.get("apple cider", 0.0)

        ordered = sorted(cider_draws, key=ciders, reverse=True)
        rows = []
        for d in ordered:
            stamina = d.units * d.offer.consumes.get("stamina", 0.0)
            rows.append([
                f"{math.ceil(ciders(d)):,}",
                _zone_label(d.offer.name.removeprefix("cider "), d),
                f"{-round(stamina):,}",
                _wanted_counts(ctx, d),
            ])
        doc.heading("Exploring (Apple Cider)")
        doc.table(
            ["Apple Cider", "Zone", "Stamina", "Yields"],
            rows,
            align=["right", "left", "right", "left"],
        )

    drinking_draws = _draws_on(plan, sources.DRINKING_TRACK)
    cider_stamina = sum(
        d.units * d.offer.consumes.get("stamina", 0.0) for d in cider_draws
    )
    if round(cider_stamina) >= 1:
        lines = [f"{round(cider_stamina):,} spent on Apple Cider"]
        banked = inventory.get("stamina", 0.0)
        if round(banked) >= 1:
            lines.append(f"{round(banked):,} banked at the start")
        passive_draw = plan.passive_draw
        rate = (
            passive_draw.offer.yields.get("stamina", 0.0)
            if passive_draw is not None
            else 0.0
        )
        if rate > 0:
            lines.append(f"{round(rate * 1440):,}/day from the Farmhouse (passive)")
        topups: dict[str, list[float]] = {}
        for draw in drinking_draws:
            for item, per in draw.offer.consumes.items():
                units, gained = topups.setdefault(item, [0.0, 0.0])
                topups[item] = [
                    units + per * draw.units,
                    gained + draw.produced.get("stamina", 0.0),
                ]
        verbs = {"orange juice": "Drink", "apple": "Eat"}
        for item, (units, gained) in sorted(topups.items(), key=lambda kv: -kv[1][1]):
            if round(units) < 1:
                continue
            verb = verbs.get(item, "Use")
            lines.append(
                f"{verb} {round(units):,}x {display.get(item, item)}"
                f" -> {round(gained):,} stamina"
            )
        doc.heading("Stamina")
        doc.bullets(lines)

    opens = [
        (name, count)
        for name, count in plan.opens.items()
        if (flow := ctx.flows_by_name.get(name.lower())) is not None
        and any(
            round(flow.yields[item] * count) >= 1
            for item in flow.yields
            if item in ctx.open_worthy
        )
    ]
    if opens:
        ordered = sorted(opens, key=lambda nc: nc[1], reverse=True)
        rows = []
        for name, count in ordered:
            flow = ctx.flows_by_name[name.lower()]
            # Materials used splits into a right-aligned quantity and a
            # left-aligned name (in practice an openable consumes at most
            # one key; several would pair up positionally).
            used = [
                (f"{round(quantity * count):,}", display.get(item, item))
                for item, quantity in flow.consumes.items()
                if item != name.lower() and round(quantity * count) >= 1
            ]
            gained = ", ".join(
                f"{round(flow.yields[item] * count):,}x {display.get(item, item)}"
                for item in sorted(flow.yields)
                if item in ctx.open_worthy and round(flow.yields[item] * count) >= 1
            )
            rows.append([
                f"{math.ceil(count):,}",
                display.get(name.lower(), name),
                ", ".join(qty for qty, _ in used),
                ", ".join(material for _, material in used),
                gained,
            ])
        doc.heading("Opening (Locksmith)")
        doc.table(
            ["Count", "Item", "Qty", "Materials used", "Yields"],
            rows,
            align=["right", "left", "right", "left", "left"],
        )

    farming_draws = _sourced(ctx, _draws_on(plan, sources.FARMING_TRACK))
    if farming_draws:
        total_farming = sum((d.time for d in farming_draws), timedelta(0))
        heading = f"Farming ({passive.format_duration(total_farming)}"
        total_sets = math.ceil(sum(d.cookies for d in farming_draws if d.cookies))
        if total_sets >= 1:
            heading += f", {total_sets:,} cookie sets"
        rows = []
        for draw in sorted(farming_draws, key=lambda d: d.time, reverse=True):
            juices = draw.gj
            crop = _crop_of(draw.offer)
            duration = passive.format_duration(draw.time)
            if draw.offer.cookies:
                action = f"Cookie-boosted {crop}"
                if round(juices) >= 1:
                    action += (
                        f" using {round(juices):,} Grape Juice"
                        f" (yield {crops.COOKIE_SET_MULTIPLIER}x)"
                    )
                for key in sorted(k for k in draw.offer.yields if k in ctx.interesting):
                    rows.append([
                        action, duration, display.get(key, key),
                        f"{round(draw.produced[key]):,}", "",
                    ])
                continue
            action = f"{_harvest_alls(math.ceil(draw.units))} of {crop}"
            if round(juices) >= 1:
                action += f" using {round(juices):,} Grape Juice"
            gj_share = juices / draw.units if draw.units else 0.0
            for key in sorted(k for k in draw.offer.yields if k in ctx.interesting):
                total = draw.produced[key]
                name = display.get(key, key)
                detail = ""
                if round(juices) >= 1:
                    from_gj = total * gj_share
                    detail = (
                        f"{round(from_gj):,} from Grape Juice, "
                        f"{round(total - from_gj):,} from normal farming"
                    )
                rows.append([action, duration, name, f"{round(total):,}", detail])
        doc.heading(heading + ")")
        doc.table(
            ["Action", "Duration", "Item", "Qty", "Detail"],
            rows,
            align=["left", "right", "left", "right", "left"],
        )

    passive_draw = plan.passive_draw
    if passive_draw is not None:
        offer = passive_draw.offer
        makespan = _plan_makespan(plan)
        prod_rows = []
        for key, rate in offer.yields.items():
            if rate <= 0:
                continue
            residual = need.get(key, 0.0) - elsewhere.get(key, 0.0)
            if residual <= 1e-6:
                produced = passive_draw.produced.get(key, 0.0)
                if key not in ctx.consumed or produced <= 1e-6:
                    continue
                residual = min(produced, ctx.consumed[key])
                # An LP-epsilon consumption (a plan that spends "0 AP" often
                # burns a stray fraction) would print as a "0x ... in 0m" line.
                if round(residual) < 1:
                    continue
            wait = _passive_wait(production, key, residual, rate, makespan)
            prod_rows.append((wait, display.get(key, key), round(residual), key))
        if prod_rows:
            critical_keys: set[str] = set()
            if not _farming_drives(
                plan, need, elsewhere, display, production, sourceable_elsewhere
            ):
                critical_keys = {
                    key for _, _, _, key in _slowest_passive_items(
                        plan, need, elsewhere, display, production, limit=makespan
                    )
                }
            # Same final name tie-break as print_plan's passive section.
            ordered = sorted(
                prod_rows, key=lambda r: (r[3] not in critical_keys, -r[2], -r[0], r[1])
            )
            rows = []
            for wait, name, quantity, key in ordered:
                raw_source = (
                    _passive_sources(production, key, quantity)
                    if production is not None
                    else ""
                )
                source = raw_source.strip()
                if source.startswith("(") and source.endswith(")"):
                    source = source[1:-1]
                rows.append([
                    name, f"{quantity:,}", passive.format_duration(wait), source,
                ])
            doc.heading("Passive production (concurrent)")
            doc.table(
                ["Item", "Qty", "Time", "Source"],
                rows,
                align=["left", "right", "right", "left"],
            )

    boost = _sawmill_boost_summary(plan, ctx)
    if boost is not None:
        omelettes, hours, yields = boost
        extra = ", ".join(f"+{qty:,} {name}" for name, qty in yields)
        doc.heading("Sawmill boost (Hickory Omelette)")
        doc.para(
            f"{omelettes:,} Hickory Omelette"
            f"{'s' if omelettes != 1 else ''} over {passive.format_duration(hours)}:"
            f" {extra}"
        )

    held = [
        (item, min(inventory.get(item, 0.0), spent))
        for item, spent in ctx.spent_consumables.items()
        if round(min(inventory.get(item, 0.0), spent)) >= 1
    ]
    if held:
        rows = [
            [display.get(item, item), f"{round(units):,}"]
            for item, units in sorted(held, key=lambda kv: -kv[1])
        ]
        doc.heading("From inventory (held consumables)")
        doc.table(["Item", "Qty"], rows)

    ap_prices = ap_prices or {}
    purchases = [(item, units) for item, units in plan.bought.items() if round(units) >= 1]
    # Split into gold purchases and Arnold-Palmer trades (see print_plan).
    gold_buys = [(i, u) for i, u in purchases if i not in ap_prices]
    trades = [(i, u) for i, u in purchases if i in ap_prices]
    if gold_buys and prices is not None:
        ordered = sorted(
            gold_buys, key=lambda kv: kv[1] * prices.get(kv[0], 0.0), reverse=True
        )
        rows = [
            [
                display.get(item, item),
                f"{round(units):,}",
                f"{round(units * prices.get(item, 0.0)):,} g",
            ]
            for item, units in ordered
        ]
        total = sum(units * prices.get(item, 0.0) for item, units in gold_buys)
        doc.heading("Buy (with gold)")
        doc.table(["Item", "Qty", "Gold"], rows)
        doc.para(f"Total: {round(total):,} g")

    if trades:
        ordered = sorted(trades, key=lambda kv: kv[1] * ap_prices.get(kv[0], 0.0), reverse=True)
        rows = [
            [
                display.get(item, item),
                f"{round(units):,}",
                f"{round(units * ap_prices.get(item, 0.0)):,} AP",
                f"{round(units * (prices or {}).get(item, 0.0)):,} g",
            ]
            for item, units in ordered
        ]
        total_ap = sum(units * ap_prices.get(item, 0.0) for item, units in trades)
        total_gold = sum(units * (prices or {}).get(item, 0.0) for item, units in trades)
        doc.heading("Trade (for Arnold Palmers)")
        doc.table(["Item", "Qty", "Arnold Palmers", "Gold value"], rows)
        doc.para(f"Total: {round(total_ap):,} AP ({round(total_gold):,} g)")

    juices = [
        (item, units) for item, units in plan.pumpkin_juices.items() if round(units) >= 1
    ]
    # Gated on the juices, never on their gold: at a `trading.pumpkin_juice` of 0 the
    # juice is free, and hiding the section then dropped a plan's whole mastery boost
    # out of the report -- the very plans that lean on juice hardest.
    if juices:
        total_juices = _pumpkin_juices(plan)
        per_juice = plan.pumpkin_gold / total_juices if total_juices else 0.0
        rows = []
        for item, units in sorted(juices, key=lambda kv: -kv[1]):
            cost = round(units * per_juice)
            name = display.get(item, item)
            levels = (pumpkin_schedule or {}).get(item)
            drink = " / ".join(f"{level:,}" for level in levels) if levels else ""
            rows.append([name, f"{round(units):,}", f"{cost:,} g", drink])
        doc.heading("Pumpkin Juice (mastery boost)")
        doc.table(["Goal", "Juices", "Gold", "Drink at mastery"], rows)
        doc.para(f"Total: {round(plan.pumpkin_gold):,} g")

    # Stamina opportunity cost (see print_plan's matching section / _build_base).
    if prices is not None and round(plan.stamina_gold) >= 1:
        units = _stamina_units(plan, prices)
        doc.heading("Stamina (opportunity cost)")
        doc.para(
            f"{round(units):,} banked/passive stamina"
            f" = {round(plan.stamina_gold):,} g (at Orange-Juice value)"
        )

    if prices is not None:
        markdown_surplus_value(
            doc, plan, demand, ctx.recipes, crafting_bonus, ctx.open_flows,
            inventory, prices, display,
        )
    # plan.remainder (materials with no known source) is reported once, up in
    # the Goal section (print_solution/markdown_solution) rather than here --
    # see print_plan's matching comment.


def _craft_order(plan: optimizer.SourcingPlan, workshop: crafting.Workshop) -> list[str]:
    """Craft keys in ``plan.crafts``, parents before the items they consume.

    Shared by :func:`print_crafts` and :func:`markdown_crafts` so both list the
    same build order.
    """
    consumed = {
        material.lower()
        for item in plan.crafts
        if (recipe := workshop.get(item)) is not None
        for material in recipe.materials
    }
    order: list[str] = []
    seen: set[str] = set()

    def visit(item: str) -> None:
        if item in seen or item not in plan.crafts:
            return
        seen.add(item)
        order.append(item)
        recipe = workshop.get(item)
        if recipe is not None:
            for material in recipe.materials:
                visit(material.lower())

    for item in sorted(k for k in plan.crafts if k not in consumed):
        visit(item)
    for item in sorted(plan.crafts):  # any left in a cycle
        visit(item)
    return order


def print_crafts(
    plan: optimizer.SourcingPlan,
    workshop: crafting.Workshop,
    crafting_bonus: float,
    mastery_bonus: float,
) -> None:
    """List the crafts the plan performs, parents before the items they consume."""
    if not plan.crafts:
        return
    print("\nRequired Crafts:")
    for key in _craft_order(plan, workshop):
        count = plan.crafts[key]
        recipe = workshop.get(key)
        assert recipe is not None  # only craftable items are in plan.crafts
        produced = count * crafting_bonus
        print(
            f"\n{round(count):,}x {recipe.name} "
            f"(Produces {round(produced):,}, Adds {round(produced * mastery_bonus):,} Mastery)"
        )
        for material, quantity in recipe.materials.items():
            if material.lower() not in crafting.IGNORED_MATERIALS:
                print(f"  {round(count * quantity):,}x {material}")


def markdown_crafts(
    doc: MarkdownDoc,
    plan: optimizer.SourcingPlan,
    workshop: crafting.Workshop,
    crafting_bonus: float,
    mastery_bonus: float,
) -> None:
    """Markdown counterpart to :func:`print_crafts`."""
    if not plan.crafts:
        return
    doc.heading("Required Crafts")
    for key in _craft_order(plan, workshop):
        count = plan.crafts[key]
        recipe = workshop.get(key)
        assert recipe is not None
        produced = count * crafting_bonus
        doc.heading(
            f"{round(count):,}x {recipe.name} "
            f"(Produces {round(produced):,}, Adds {round(produced * mastery_bonus):,} Mastery)",
            3,
        )
        rows = [
            [material, f"{round(count * quantity):,}"]
            for material, quantity in recipe.materials.items()
            if material.lower() not in crafting.IGNORED_MATERIALS
        ]
        if rows:
            doc.table(["Material", "Qty"], rows)


# --- Ascension mode (--ascend) report sections (milestone 5) ----------------
#
# Renders main.AscensionFrontierResult/StagedPlan/StageResult (see ARCHITECTURE.md's
# "Ascension orchestration"). Type-hinted loosely via the TYPE_CHECKING-only `main`
# import above; every function here is otherwise a plain duck-typed reader of those
# dataclasses' fields, exactly like optimizer.SourcingPlan elsewhere in this module.


def _ascension_ak_summary(current: int, target: int, starting_ak: int) -> tuple[int, int]:
    """``(total AK needed, net AK still needed)`` for climbing ``current`` -> ``target``."""
    total = ascension.AK_PER_LEVEL * (target - current)
    net = max(0, total - starting_ak)
    return total, net


def _ascension_goal_line(current: int, target: int, starting_ak: int) -> str:
    total, net = _ascension_ak_summary(current, target, starting_ak)
    return (
        f"Goal: Climb tower from {current} to {target} "
        f"(needs {total:,} AK total, {starting_ak:,} AK already banked, "
        f"{net:,} AK net)"
    )


def _ascension_chore_rate_line() -> str:
    rate = ascension.chore_rate_per_day()
    return f"Daily chores alone earn ≈{rate:.2f} AK/day (smeared across the climb)."


def _ascension_unexpected_line(unexpected: list[str]) -> str:
    """The one-line warning for mastery-paste items the catalogue doesn't know.

    Either the catalogue (``mastery_items.txt``) is stale against newer game
    content, or the paste has something odd in it -- both worth a look, so the
    line names the regeneration script.
    """
    return (
        "Warning: mastery items not in the masterable catalogue "
        "(mastery_items.txt stale? regenerate with scripts/generate_items.py): "
        + ", ".join(unexpected)
    )


def print_ascension_goal(
    current: int,
    target: int,
    starting_ak: int,
    unmasterable: list[str],
    unexpected: list[str] | None = None,
) -> None:
    """The ascension Goal header: the AK arithmetic, plus any warnings.

    ``unmasterable`` (see :attr:`main.AscensionFrontierResult.unmasterable`,
    already display-cased) renders as one comma-joined paragraph right below
    the arithmetic -- there can be dozens on a real save (seasonal/unobtainable
    items), so a per-line list would drown the header. ``unexpected``
    (:attr:`main.AscensionFrontierResult.unexpected_masteries`) likewise --
    see :func:`_ascension_unexpected_line`.
    """
    print(_ascension_goal_line(current, target, starting_ak))
    print(_ascension_chore_rate_line())
    if unmasterable:
        print()
        print(
            "Not masterable (no known source), skipped as candidates: "
            + ", ".join(unmasterable)
        )
    if unexpected:
        print()
        print(_ascension_unexpected_line(unexpected))


def markdown_ascension_goal(
    doc: MarkdownDoc,
    current: int,
    target: int,
    starting_ak: int,
    unmasterable: list[str],
    unexpected: list[str] | None = None,
) -> None:
    """Markdown counterpart to :func:`print_ascension_goal`."""
    doc.heading("Goal")
    doc.para(_ascension_goal_line(current, target, starting_ak))
    doc.para(_ascension_chore_rate_line())
    if unmasterable:
        doc.para(
            "Not masterable (no known source), skipped as candidates: "
            + ", ".join(unmasterable)
        )
    if unexpected:
        doc.para(_ascension_unexpected_line(unexpected))


def _ascension_plan_totals(plan: main.StagedPlan) -> tuple[int, int, int, float]:
    """``(masters, grandmasters, megas, chore AK)`` summed across a staged plan's
    stages -- raw per-stage counts (not deduped the way the to-do list is; see
    :func:`_ascension_todo`), matching the old milestone-4 report's summary line.
    """
    masters = sum(len(s.plan.ascension.masters) for s in plan.stages)
    grandmasters = sum(len(s.plan.ascension.grandmasters) for s in plan.stages)
    megas = sum(len(s.plan.ascension.megas) for s in plan.stages)
    chore_ak = sum(s.plan.ascension.ak_from_chores for s in plan.stages)
    return masters, grandmasters, megas, chore_ak


def _ascension_tags(i: int, selected: int, cheapest: int, include_selected: bool) -> list[str]:
    tags = []
    if i == 0:
        tags.append("fastest")
    if i == cheapest:
        tags.append("cheapest")
    if include_selected and i == selected:
        tags.append("selected")
    return tags


def print_ascension_frontier(
    result: main.AscensionFrontierResult, selected: int, cheapest: int,
) -> None:
    """The ascension frontier: each staged plan's time, gold and AK mix.

    Mirrors :func:`print_frontier`'s row shape and tagging (`[fastest]`/
    `[cheapest]`, the ``>`` marker for the selected row, no textual "selected"
    tag -- see :func:`markdown_ascension_frontier`'s ``include_selected_tag``
    for why the two differ there) over :class:`main.StagedPlan` instead of
    :class:`optimizer.SourcingPlan` -- duck-typed the same way
    ``optimizer._non_dominated``/``_thin`` already treat the two interchangeably.
    """
    print("\nAscension plans (time -> gold, masteries, chore AK):")
    for i, plan in enumerate(result.plans):
        marker = ">" if i == selected else " "
        masters, grandmasters, megas, chore_ak = _ascension_plan_totals(plan)
        net_gold = plan.gold - plan.surplus_gold
        tags = _ascension_tags(i, selected, cheapest, include_selected=False)
        note = "  " + f"[{', '.join(tags)}]" if tags else ""
        print(
            f" {marker} {i + 1}. {passive.format_duration(plan.time)}"
            f" -> {round(plan.gold):,} g (net {round(net_gold):,} g),"
            f" masters {masters}, GMs {grandmasters}, megas {megas},"
            f" chore AK {chore_ak:,.1f}{note}"
        )


def markdown_ascension_frontier(
    doc: MarkdownDoc,
    result: main.AscensionFrontierResult,
    selected: int,
    cheapest: int,
    include_selected_tag: bool = True,
) -> None:
    """Markdown counterpart to :func:`print_ascension_frontier`: the same rows,
    as a table with columns ``# | Time | Gold | Net gold | Masters | GMs | Megas
    | Chore AK | Notes``. ``include_selected_tag`` mirrors
    :func:`markdown_frontier`'s (off for :class:`SectionedReporter`, whose own
    row highlight already shows the selection)."""
    headers = ["#", "Time", "Gold", "Net gold", "Masters", "GMs", "Megas", "Chore AK", "Notes"]
    rows = []
    for i, plan in enumerate(result.plans):
        masters, grandmasters, megas, chore_ak = _ascension_plan_totals(plan)
        net_gold = plan.gold - plan.surplus_gold
        tags = _ascension_tags(i, selected, cheapest, include_selected_tag)
        notes = f"[{', '.join(tags)}]" if tags else ""
        rows.append([
            str(i + 1),
            passive.format_duration(plan.time),
            f"{round(plan.gold):,} g",
            f"{round(net_gold):,} g",
            str(masters),
            str(grandmasters),
            str(megas),
            f"{chore_ak:,.1f}",
            notes,
        ])
    doc.heading("Ascension plans")
    doc.table(headers, rows)


def _ascension_ledger(
    chosen: main.StagedPlan, current: int, target: int,
) -> tuple[float, float, int]:
    """``(AK from steps, AK from chores, AK required)`` for the selected staged plan.

    Ledger identity (see :func:`print_ascension_plan`'s "Ascension summary" and
    :attr:`main.StagedPlan.carry_out`, the balancing term computed alongside the
    plan itself): ``starting_ak + steps + chores == required + chosen.carry_out``.
    """
    steps = sum(s.plan.ascension.ak_from_steps for s in chosen.stages)
    chores = sum(s.plan.ascension.ak_from_chores for s in chosen.stages)
    required = ascension.AK_PER_LEVEL * (target - current)
    return steps, chores, required


def _ascension_timeline(chosen: main.StagedPlan, start: date) -> list[tuple[int, date]]:
    """Each stage boundary's approximate calendar date: ``start`` plus the
    cumulative stage times up to and including that stage (see
    :class:`main.StageResult`). Approximate -- levels crossed *within* a stage
    are not interpolated, only the stage's own end level gets a date (v1, see
    ARCHITECTURE.md's "Ascension orchestration" v1-approximations note)."""
    cumulative = timedelta(0)
    rows = []
    for stage in chosen.stages:
        cumulative += stage.plan.time
        rows.append((stage.to_level, start + cumulative))
    return rows


def _ascension_todo(stage: main.StageResult) -> tuple[list[str], list[str], list[str]]:
    """A stage's mastery to-do lists, deduped so an item is named once, at the
    highest step it reaches this stage: ``masters - grandmasters`` (a master
    step that is merely a prerequisite of a listed grandmaster needs no separate
    mention) and ``grandmasters - megas`` likewise.
    """
    outcome = stage.plan.ascension
    masters = sorted(set(outcome.masters) - set(outcome.grandmasters))
    grandmasters = sorted(set(outcome.grandmasters) - set(outcome.megas))
    megas = sorted(outcome.megas)
    return masters, grandmasters, megas


def _ascension_chore_wait(plan: optimizer.SourcingPlan) -> bool:
    """Whether ``plan.time`` is set by waiting on daily chores, not any track.

    See :attr:`optimizer.SourcingPlan.time`'s ascension note: the chore-AK
    constraint can push the optimal makespan past every draw track's own time
    (the plan idles, waiting on daily chores, to close the AK gap). A one-second
    tolerance absorbs LP/quantisation float noise -- mirrors
    :func:`_farming_drives`'s own margin handling elsewhere in this module.
    Called once per stage by both :func:`print_ascension_plan` and
    :func:`markdown_ascension_plan`, so they always agree on which stages get the
    chore-wait override instead of the normal :func:`_critical_path_summary`
    (whose own precedence logic this never touches).
    """
    others = max([*plan.track_times().values()], default=timedelta(0))
    return plan.time > others + timedelta(seconds=1)


def _ascension_stage_heading(index: int, stage: main.StageResult) -> str:
    heading = f"Stage {index} — level {stage.from_level} → {stage.to_level}"
    reward = ascension.REWARD_LEVELS.get(stage.to_level)
    if reward:
        heading += f" (reward at {stage.to_level}: {reward})"
    return heading


def print_ascension_plan(
    result: main.AscensionFrontierResult,
    selected: int,
    current: int,
    target: int,
    starting_ak: int,
    start: date,
) -> None:
    """The selected staged plan: the AK ledger, a level-up timeline, the mastery
    to-do list, then each stage's own critical path and full per-channel plan
    (reusing :func:`print_plan`, see that function's ``skip_critical_path``).
    """
    chosen = result.plans[selected]
    steps, chores, required = _ascension_ledger(chosen, current, target)
    print(f"\nSelected plan (#{selected + 1}):")

    print("\nAscension summary:")
    print(f"  Starting AK: {starting_ak:,}")
    print(f"  AK from mastery/grandmastery steps: {round(steps):,}")
    print(f"  AK from chores: {round(chores):,}")
    print(f"  AK required (100 AK x levels climbed): {required:,}")
    print(f"  Carry-out AK: {round(chosen.carry_out):,}")

    print("\nLevel-up timeline (approximate):")
    for level, eta in _ascension_timeline(chosen, start):
        print(f"  reach level {level} ~ {eta:%d %b %Y}")

    print("\nMastery to-do list:")
    for i, stage in enumerate(chosen.stages, start=1):
        masters, grandmasters, megas = _ascension_todo(stage)
        print(f"  Stage {i} (L{stage.from_level}->L{stage.to_level}):")
        if masters:
            print(f"    Master: {', '.join(m.title() for m in masters)}")
        if grandmasters:
            print(f"    Grandmaster: {', '.join(m.title() for m in grandmasters)}")
        if megas:
            print(f"    Mega: {', '.join(m.title() for m in megas)}")
        if not (masters or grandmasters or megas):
            print("    (none)")

    for i, stage in enumerate(chosen.stages, start=1):
        print(f"\n{_ascension_stage_heading(i, stage)}:")
        chore_wait = _ascension_chore_wait(stage.plan)
        if chore_wait:
            print(
                f"\nCritical path (waiting on daily chores, "
                f"+{stage.plan.ascension.ak_from_chores:,.1f} AK over the stage)"
            )
        print_plan(
            stage.plan, {}, stage.farm, stage.workshop, stage.crafting_bonus,
            stage.production, stage.open_flows, stage.openable_names,
            stage.inventory, None, None, stage.prices, None,
            skip_critical_path=chore_wait,
        )


def markdown_ascension_plan(
    doc: MarkdownDoc,
    result: main.AscensionFrontierResult,
    selected: int,
    current: int,
    target: int,
    starting_ak: int,
    start: date,
) -> None:
    """Markdown counterpart to :func:`print_ascension_plan`."""
    chosen = result.plans[selected]
    steps, chores, required = _ascension_ledger(chosen, current, target)
    doc.heading(f"Selected plan (#{selected + 1})")

    doc.heading("Ascension summary")
    doc.bullets([
        f"Starting AK: {starting_ak:,}",
        f"AK from mastery/grandmastery steps: {round(steps):,}",
        f"AK from chores: {round(chores):,}",
        f"AK required (100 AK x levels climbed): {required:,}",
        f"Carry-out AK: {round(chosen.carry_out):,}",
    ])

    doc.heading("Level-up timeline (approximate)")
    doc.bullets(
        f"reach level {level} ~ {eta:%d %b %Y}"
        for level, eta in _ascension_timeline(chosen, start)
    )

    doc.heading("Mastery to-do list")
    rows = []
    for i, stage in enumerate(chosen.stages, start=1):
        masters, grandmasters, megas = _ascension_todo(stage)
        rows.append([
            f"{i} (L{stage.from_level}→L{stage.to_level})",
            ", ".join(m.title() for m in masters),
            ", ".join(m.title() for m in grandmasters),
            ", ".join(m.title() for m in megas),
        ])
    doc.table(["Stage", "Master", "Grandmaster", "Mega"], rows)

    for i, stage in enumerate(chosen.stages, start=1):
        doc.heading(_ascension_stage_heading(i, stage))
        chore_wait = _ascension_chore_wait(stage.plan)
        if chore_wait:
            doc.heading("Critical path")
            doc.para(
                "Waiting on daily chores "
                f"(+{stage.plan.ascension.ak_from_chores:,.1f} AK over the stage)"
            )
        markdown_plan(
            doc, stage.plan, {}, stage.farm, stage.workshop, stage.crafting_bonus,
            stage.production, stage.open_flows, stage.openable_names,
            stage.inventory, None, None, stage.prices, None,
            skip_critical_path=chore_wait,
        )


class Reporter:
    """Uniform interface over the two output formats ``main.run`` drives.

    Text prints immediately, matching every behaviour before this class existed;
    Markdown buffers into a :class:`MarkdownDoc` and renders once in
    :meth:`finish` -- via `rich` to the terminal, or as raw Markdown source when
    stdout isn't one (so redirecting to a ``.md`` file gets real Markdown).
    """

    def solution(
        self, solution: farm_solver.Solution, remainder: dict[str, int] | None = None
    ) -> None:
        raise NotImplementedError

    def unobtainable(self, materials: list[str]) -> None:
        raise NotImplementedError

    def note(self, text: str) -> None:
        raise NotImplementedError

    def ascension_goal(
        self,
        current: int,
        target: int,
        starting_ak: int,
        unmasterable: list[str],
        unexpected: list[str] | None = None,
    ) -> None:
        raise NotImplementedError

    def ascension_frontier(
        self, result: main.AscensionFrontierResult, selected: int, cheapest: int,
    ) -> None:
        raise NotImplementedError

    def ascension_plan(
        self,
        result: main.AscensionFrontierResult,
        selected: int,
        current: int,
        target: int,
        starting_ak: int,
        start: date,
    ) -> None:
        raise NotImplementedError

    def frontier(
        self,
        plans: list[optimizer.SourcingPlan],
        selected: int,
        cheapest: int,
        reasons: list[str | None] | None = None,
        prices: dict[str, float] | None = None,
        golds: list[tuple[float, float]] | None = None,
        spread_axis: str | None = None,
        graph_metric: str | None = None,
    ) -> None:
        raise NotImplementedError

    def plan(
        self,
        plan: optimizer.SourcingPlan,
        demand: dict[str, float],
        farm: crops.Farm,
        workshop: crafting.Workshop,
        crafting_bonus: float,
        production: passive.PassiveProduction | None = None,
        open_flows: list[sources.OpenFlow] | None = None,
        openable_names: dict[str, str] | None = None,
        inventory: dict[str, float] | None = None,
        sourceable_elsewhere: set[str] | None = None,
        max_passive_residuals: dict[str, float] | None = None,
        prices: dict[str, float] | None = None,
        pumpkin_schedule: dict[str, list[int]] | None = None,
        skip_critical_path: bool = False,
        ap_prices: dict[str, float] | None = None,
    ) -> None:
        raise NotImplementedError

    def crafts(
        self,
        plan: optimizer.SourcingPlan,
        workshop: crafting.Workshop,
        crafting_bonus: float,
        mastery_bonus: float,
    ) -> None:
        raise NotImplementedError

    def finish(self) -> None:
        """Flush any buffered output. A no-op for the text reporter."""


class TextReporter(Reporter):
    """Prints straight to stdout, exactly as every report function always has."""

    def solution(
        self, solution: farm_solver.Solution, remainder: dict[str, int] | None = None
    ) -> None:
        print_solution(solution, remainder)

    def unobtainable(self, materials: list[str]) -> None:
        print_unobtainable(materials)

    def note(self, text: str) -> None:
        print(f"\n{text}")

    def ascension_goal(self, *args, **kwargs) -> None:
        print_ascension_goal(*args, **kwargs)

    def ascension_frontier(self, *args, **kwargs) -> None:
        print_ascension_frontier(*args, **kwargs)

    def ascension_plan(self, *args, **kwargs) -> None:
        print_ascension_plan(*args, **kwargs)

    def frontier(self, *args, **kwargs) -> None:
        print_frontier(*args, **kwargs)

    def plan(self, *args, **kwargs) -> None:
        print_plan(*args, **kwargs)

    def crafts(self, *args, **kwargs) -> None:
        print_crafts(*args, **kwargs)


def _highlight_unreachable_rows(console: Console, text: str) -> Segments:
    """Recolor red any rendered line naming an unreachable goal.

    Plain Markdown (and rich's parser of it) has no per-row/per-cell colour, so
    :data:`UNREACHABLE_GOAL_MARK` is the only signal available -- this renders
    the document once via the normal Markdown path, then walks the resulting
    lines and overrides just the foreground colour of any line containing the
    mark, leaving everything else (bold headers, table rules, wrapping) as
    `rich` already laid it out.
    """
    lines = console.render_lines(RichMarkdown(text), console.options, pad=True)
    red = Style(color="red")
    segments: list[Segment] = []
    for line in lines:
        if UNREACHABLE_GOAL_MARK in "".join(segment.text for segment in line):
            line = [
                Segment(segment.text, (segment.style or Style()) + red, segment.control)
                for segment in line
            ]
        segments.extend(line)
        segments.append(Segment.line())
    return Segments(segments)


class MarkdownReporter(Reporter):
    """Buffers the whole report as Markdown, rendered once in :meth:`finish`.

    ``pager`` runs the rendered output through the system pager (`rich`'s
    ``Console.pager``, which shells out via :mod:`pydoc` -- respecting
    ``$PAGER``/``$MANPAGER``, else ``less``, with ANSI passthrough already set
    via its ``LESS`` env var). This is different from piping the process's own
    stdout to a pager externally: that makes stdout a non-tty *before* we ever
    see it, so :meth:`finish` would take the raw-source branch below and the
    pager would show unrendered Markdown, not the styled tables. Here the pager
    is a subprocess *we* launch, so our own stdout stays the terminal throughout.
    """

    def __init__(self, pager: bool = False) -> None:
        self.doc = MarkdownDoc()
        self.pager = pager

    def solution(
        self, solution: farm_solver.Solution, remainder: dict[str, int] | None = None
    ) -> None:
        markdown_solution(self.doc, solution, remainder)

    def unobtainable(self, materials: list[str]) -> None:
        markdown_unobtainable(self.doc, materials)

    def note(self, text: str) -> None:
        self.doc.para(text)

    def ascension_goal(self, *args, **kwargs) -> None:
        markdown_ascension_goal(self.doc, *args, **kwargs)

    def ascension_frontier(self, *args, **kwargs) -> None:
        markdown_ascension_frontier(self.doc, *args, **kwargs)

    def ascension_plan(self, *args, **kwargs) -> None:
        markdown_ascension_plan(self.doc, *args, **kwargs)

    def frontier(self, *args, **kwargs) -> None:
        markdown_frontier(self.doc, *args, **kwargs)

    def plan(self, *args, **kwargs) -> None:
        markdown_plan(self.doc, *args, **kwargs)

    def crafts(self, *args, **kwargs) -> None:
        markdown_crafts(self.doc, *args, **kwargs)

    def finish(self) -> None:
        text = self.doc.render()
        if not sys.stdout.isatty():
            # Not a terminal (e.g. redirected to a file): print the Markdown
            # source itself, so the redirected output is a real .md file rather
            # than rich's word-wrapped rendering of it. A pager would have
            # nothing to attach to here either, so it's ignored in this case.
            print(text)
            return
        console = Console()
        renderable = (
            _highlight_unreachable_rows(console, text)
            if UNREACHABLE_GOAL_MARK in text
            else RichMarkdown(text)
        )
        if self.pager:
            with console.pager(styles=True):
                console.print(renderable)
        else:
            console.print(renderable)


class SectionedReporter(Reporter):
    """Like :class:`MarkdownReporter`, but one :class:`MarkdownDoc` per section.

    Meant for a non-terminal caller (the web app) that renders each section
    separately rather than one continuous document: ``solution``/``unobtainable``
    share a "solution" doc, ``note`` calls go to "notes" (both accumulated there
    and appended to ``self.notes`` for a caller that wants the plain text too),
    and ``frontier``/``plan``/``crafts`` each get their own doc. :meth:`finish`
    is a no-op -- there is no single rendering step; the caller reads
    ``self.docs[...].render()`` per section instead.
    """

    def __init__(self) -> None:
        # The plan and crafts docs render inside the page's own "Selected plan"
        # heading, so their sections shift one level down to nest under it.
        self.docs: dict[str, MarkdownDoc] = {
            "solution": MarkdownDoc(),
            "notes": MarkdownDoc(),
            "frontier": MarkdownDoc(),
            "plan": MarkdownDoc(heading_shift=1),
            "crafts": MarkdownDoc(heading_shift=1),
            # The impact graph (see frontier()) -- its own section rather than
            # appended to "frontier", so the web page can lay it out as a
            # separate pane instead of inline after the table. Stays empty
            # (renders to "") when no graph is requested.
            "graph": MarkdownDoc(),
        }
        self.notes: list[str] = []

    def solution(
        self, solution: farm_solver.Solution, remainder: dict[str, int] | None = None
    ) -> None:
        markdown_solution(self.docs["solution"], solution, remainder)

    def unobtainable(self, materials: list[str]) -> None:
        markdown_unobtainable(self.docs["solution"], materials)

    def note(self, text: str) -> None:
        self.notes.append(text)
        self.docs["notes"].para(text)

    def ascension_goal(self, *args, **kwargs) -> None:
        markdown_ascension_goal(self.docs["solution"], *args, **kwargs)

    def ascension_frontier(self, *args, **kwargs) -> None:
        # Same reasoning as frontier(): the web app's own row highlight already
        # shows the selection.
        markdown_ascension_frontier(
            self.docs["frontier"], *args, include_selected_tag=False, **kwargs
        )

    def ascension_plan(self, *args, **kwargs) -> None:
        markdown_ascension_plan(self.docs["plan"], *args, **kwargs)

    def frontier(
        self,
        plans: list[optimizer.SourcingPlan],
        selected: int,
        cheapest: int,
        reasons: list[str | None] | None = None,
        prices: dict[str, float] | None = None,
        golds: list[tuple[float, float]] | None = None,
        spread_axis: str | None = None,
        graph_metric: str | None = None,
    ) -> None:
        # The web app's own row highlight already shows selection; see
        # markdown_frontier's include_selected_tag docstring. graph_metric is
        # withheld from markdown_frontier itself (the table's own doc) -- the
        # graph goes in its own "graph" section (a separate pane in the web
        # UI) instead of appended inline after the table like the CLI's
        # Markdown output.
        markdown_frontier(
            self.docs["frontier"], plans, selected, cheapest, reasons, prices, golds,
            include_selected_tag=False, spread_axis=spread_axis,
        )
        if graph_metric is not None:
            series = optimizer.spread_graph_series(
                plans, spread_axis or "time", graph_metric
            )
            self.docs["graph"].para(_svg_plot(series, plans))

    def plan(self, *args, **kwargs) -> None:
        markdown_plan(self.docs["plan"], *args, **kwargs)

    def crafts(self, *args, **kwargs) -> None:
        markdown_crafts(self.docs["crafts"], *args, **kwargs)

    def finish(self) -> None:
        pass


def build_reporter(format_: str, pager: bool = False) -> Reporter:
    """The :class:`Reporter` for ``--format`` (``"text"`` or ``"markdown"``).

    ``pager`` (Markdown only) pages the rendered output through the system
    pager instead of dumping it straight to the terminal.
    """
    if format_ == "markdown":
        return MarkdownReporter(pager=pager)
    return TextReporter()
