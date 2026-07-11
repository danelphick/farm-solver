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
from datetime import timedelta

from rich.console import Console
from rich.markdown import Markdown as RichMarkdown

import config
import crafting
import crops
import farm_solver
import optimizer
import passive
import pets
import sources


# How close to the makespan the farming track may fall and still count as the
# critical path (the rest being passive fill); see :func:`print_critical_path`.
_FARMING_COMAX_MARGIN = 0.01

# How close to its full (cheapest-plan) requirement a passive item's residual must
# be to count as fully gathered -- the resource whose completion gates the plan
# (see :func:`_defining_passive_resource`).
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
    return bool(cells) and numeric * 2 > len(cells)


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

    def table(self, headers: list[str], rows: list[list[str]]) -> None:
        if not rows:
            return
        head = "| " + " | ".join(_escape_cell(h) for h in headers) + " |"
        # Right-align a column when most of its cells are quantities --
        # counts, gold, durations, dates all start with a digit (possibly
        # signed/parenthesised); the odd word ("done") rides along.
        aligns = [
            "---:" if _column_is_numeric(rows, i) else "---"
            for i in range(len(headers))
        ]
        sep = "| " + " | ".join(aligns) + " |"
        body = [
            "| " + " | ".join(_escape_cell(str(c)) for c in row) + " |" for row in rows
        ]
        self._blocks.append("\n".join([head, sep, *body]))

    def render(self) -> str:
        return "\n\n".join(self._blocks) + "\n"


def print_solution(solution: farm_solver.Solution) -> None:
    for need in solution.needs:
        prefix = f"T{need.level}: " if need.level is not None else ""
        if need.extra > 0:
            print(
                f"{prefix}{need.item}: need {need.extra:,} more "
                f"(have {need.have:,} of {need.target:,})"
            )
        else:
            print(
                f"{prefix}{need.item}: already have "
                f"{need.have:,} of {need.target:,}"
            )

    for note in solution.notes:
        print(note)


def markdown_solution(doc: MarkdownDoc, solution: farm_solver.Solution) -> None:
    """Markdown counterpart to :func:`print_solution`: the goal needs as a table."""
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
            rows.append([
                purpose,
                need.item,
                f"{need.have:,}",
                f"{need.target:,}",
                f"{need.extra:,}" if need.extra > 0 else "done",
            ])
        doc.table(headers, rows)
    for note in solution.notes:
        doc.para(note)


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


def print_frontier(
    plans: list[optimizer.SourcingPlan],
    selected: int,
    cheapest: int,
    reasons: list[str | None] | None = None,
    prices: dict[str, float] | None = None,
    golds: list[tuple[float, float]] | None = None,
) -> None:
    """The frontier table: each plan's time, consumables and gold, fastest first.

    The tradeoff is time against **gold**: the fast plans buy their consumables,
    the slow ones self-source them. Each row lists its makespan and the consumables
    it spends (cookie sets, Grape Juices and Apple Ciders appear as columns only
    when some plan uses them), then, with trading ``prices``, its gold. ``golds``
    (one ``(to_buy, passive)`` pair per plan, from
    :func:`optimizer.plan_gold_breakdown`) splits that into what the plan **buys**
    and the market value of what it **self-sources** instead, summing to the total.
    A plan that banks surplus then shows ``(bank B, net N)``: the sale value of what
    it produces past all it consumes (``plan.surplus_gold``), and its **net** gold
    ``buy - bank`` -- the real cost the frontier trades against time, which for a
    slow, passive-leaning plan falls below zero (see
    :attr:`optimizer.SourcingPlan.cost`); this is what "cheapest" ranks by.
    ``reasons`` annotates why each plan is on the frontier (a resource plan says
    "all Steel Wire from passive"); endpoints and farming intermediates carry only
    their fastest/cheapest tag.
    """
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
            passive.format_duration(plan.time),
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
            # Credit the surplus the plan banks: its net cost (what the frontier
            # trades against time) is what it buys less what it can sell, which for a
            # slow, passive-leaning plan drops below zero (see SourcingPlan.cost).
            banked = round(plan.surplus_gold)
            if banked:
                net = round(plan.gold - plan.surplus_gold)
                gold += f" (bank {banked:,}, net {net:,})"
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
        note = "  " + " ".join(note_parts) if note_parts else ""
        print(f" {marker} {i + 1}. {' + '.join(parts)}{gold}{note}")


def markdown_frontier(
    doc: MarkdownDoc,
    plans: list[optimizer.SourcingPlan],
    selected: int,
    cheapest: int,
    reasons: list[str | None] | None = None,
    prices: dict[str, float] | None = None,
    golds: list[tuple[float, float]] | None = None,
) -> None:
    """Markdown counterpart to :func:`print_frontier`: the same rows, as a table."""
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
            passive.format_duration(plan.time),
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
            banked = round(plan.surplus_gold)
            if banked:
                net = round(plan.gold - plan.surplus_gold)
                gold += f" (bank {banked:,}, net {net:,})"
            row.append(gold)
        tags = []
        if i == 0:
            tags.append("fastest")
        if i == cheapest:
            tags.append("cheapest")
        if i == selected:
            tags.append("selected")
        notes = []
        if tags:
            notes.append(f"[{', '.join(tags)}]")
        reason = reasons[i] if reasons is not None else None
        if reason:
            notes.append(reason)
        row.append(" ".join(notes))
        rows.append(row)

    doc.heading("Sourcing plans")
    doc.table(headers, rows)


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
    """Why each plan is on the frontier: the resource whose full-requirement
    gathering plateau it sits at ("all Steel Wire from passive"), else ``None``
    (an endpoint or a farming-accelerated intermediate). Mirrors the chosen plan's
    critical path (see :func:`_defining_passive_resource`)."""
    reasons: list[str | None] = []
    for plan in plans:
        residuals = optimizer.passive_residuals(
            plan, demand, recipes, crafting_bonus, open_flows, inventory
        )
        tolerance = max(timedelta(seconds=1), plan.time * _FARMING_COMAX_MARGIN)
        defining = _defining_passive_resource(
            plan, residuals, display, plan.time, tolerance,
            max_passive_residuals, production,
        )
        reasons.append(f"all {defining[0]} from passive" if defining else None)
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


def _slowest_passive_item(
    plan: optimizer.SourcingPlan,
    need: dict[str, float],
    elsewhere: dict[str, float],
    display: dict[str, str],
    production: passive.PassiveProduction | None = None,
) -> tuple[str, int, timedelta, str] | None:
    """The passive item that caps the plan's time: ``(name, quantity, wait, key)``.

    Each item's wait is its residual (what other channels and crafting did not
    cover) over its per-minute rate; the longest of these is the passive track's
    time, so it is what caps a passive-bound plan.

    A pet slowly trickling out a small top-up is skipped, though: pets are a last
    resort, so unless a pet is the *primary* source of an item it should not be
    named as the critical path over a building doing the real work (a pet finishing
    a 2,302 residual of Horn just after a sawmill makes 13M Oak is co-maximal noise,
    not the bottleneck). Only if every passive item is such a pet top-up do we fall
    back to the slowest of them, so a genuinely pet-bound plan still reports one.
    """
    passive_draw = plan.passive_draw
    if passive_draw is None:
        return None
    offer = passive_draw.offer
    best: tuple[str, int, timedelta, str] | None = None  # slowest non-pet-top-up
    fallback: tuple[str, int, timedelta, str] | None = None  # slowest of any, backup
    for key, rate in offer.yields.items():
        if rate <= 0:
            continue
        total_need = need.get(key, 0.0)
        residual = total_need - elsewhere.get(key, 0.0)
        if residual <= 1e-6:
            continue
        wait = timedelta(minutes=residual / rate)
        row = (display.get(key, key), round(residual), wait, key)
        if fallback is None or wait > fallback[2]:
            fallback = row
        # A pet top-up: pets make most of this item's passive supply yet supply
        # less than half of the item overall (another channel is its main source).
        pet_rate = _pet_passive_rate(production, key)
        pet_made = residual * (pet_rate / rate)
        pet_topup = pet_rate > rate / 2 and pet_made < total_need / 2
        if not pet_topup and (best is None or wait > best[2]):
            best = row
    return best or fallback


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
    agree on which track is the bottleneck.
    """
    track_time = plan.track_times()
    makespan = max(track_time.values(), default=timedelta(0))
    if makespan <= timedelta(0):
        return False
    farming_time = track_time.get(sources.FARMING_TRACK, timedelta(0))
    if farming_time <= timedelta(0):
        return False  # no farming to drive the makespan (a sub-second tolerance on a
        # near-instant plan could otherwise round zero farming up to co-maximal)
    tolerance = max(timedelta(seconds=1), makespan * _FARMING_COMAX_MARGIN)
    if farming_time < makespan - tolerance:
        return False
    item = _slowest_passive_item(plan, need, elsewhere, display, production)
    passive_comaximal = item is not None and item[2] >= makespan - tolerance
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


def _defining_passive_resource(
    plan: optimizer.SourcingPlan,
    residuals: dict[str, float],
    display: dict[str, str],
    makespan: timedelta,
    tolerance: timedelta,
    max_passive_residuals: dict[str, float] | None,
    production: passive.PassiveProduction | None,
) -> tuple[str, int, timedelta, str] | None:
    """The passive item at its full requirement whose completion sets the makespan.

    A material like Steel Wire is needed only up to its products' full demand;
    gather that much passively and no more is required, so the time it takes is a
    hard gate -- and a plan clamped there is *its* plan. It qualifies when its
    ``residuals`` entry has reached its full (cheapest-plan) requirement and it
    completes at the makespan; a resource still being sped up sits below its full
    requirement, and one that finishes early is not co-maximal. A pet-made
    material is skipped -- its trickle is a last resort, and the pet-primary case
    is handled by :func:`_slowest_passive_item`. Shared by the critical-path line
    and the frontier's per-plan reason (see :func:`print_frontier`).
    """
    if not max_passive_residuals:
        return None
    passive_draw = plan.passive_draw
    if passive_draw is None:
        return None
    best: tuple[str, int, timedelta, str] | None = None
    for key, rate in passive_draw.offer.yields.items():
        if rate <= 0:
            continue
        residual = residuals.get(key, 0.0)
        full = max_passive_residuals.get(key, 0.0)
        if residual <= 1e-6 or full <= 0:
            continue
        if residual < full * (1 - _FULL_REQUIREMENT_MARGIN):
            continue  # still being sped up with consumables, not the gate
        wait = timedelta(minutes=residual / rate)
        if wait < makespan - tolerance:
            continue  # finishes early, so it is not what caps the plan
        if _pet_passive_rate(production, key) > rate / 2:
            continue  # a pet trickle, not a real gathering plateau
        if best is None or wait > best[2]:
            best = (display.get(key, key), round(residual), wait, key)
    return best


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
    # Concurrent tracks; the makespan is the longest one (derived from the draws,
    # not plan.time, so directly-built plans without a cached time still work).
    makespan = max(plan.track_times().values(), default=timedelta(0))
    if makespan <= timedelta(0):
        return None  # everything is instant; there is no wait to explain

    tolerance = max(timedelta(seconds=1), makespan * _FARMING_COMAX_MARGIN)
    defining = _defining_passive_resource(
        plan, _passive_residuals_of(plan, need, elsewhere), display, makespan,
        tolerance, max_passive_residuals, production,
    )
    if defining is not None:
        # A resource gathered to its full requirement exactly here: its completion
        # is the gate, and this is its plan (more time gathers no more of it).
        name, quantity, wait, _ = defining
        line = (
            f"{quantity:,}x {name} from passive production "
            f"({passive.format_duration(wait)}) — its full requirement, "
            "after which no more is needed"
        )
    elif _farming_drives(
        plan, need, elsewhere, display, production, sourceable_elsewhere
    ):
        line = _farming_bottleneck(plan, makespan)
    elif (item := _slowest_passive_item(
        plan, need, elsewhere, display, production
    )) is not None:
        # Passive sets the pace: it is the sole longest track, or farming beside it
        # is merely filling the wait this item forces with free crops.
        name, quantity, wait, _ = item
        line = (
            f"{quantity:,}x {name} from passive production "
            f"({passive.format_duration(wait)}) — the slowest item to build up"
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
    """The gold a plan's leftovers are worth if crafted into sellable goods.

    Passive production overshoots every resource but the slowest, so a plan banks
    materials far past what its goal needs (see :func:`optimizer.plan_leftovers`).
    Assuming they are crafted into the sellable priced goods -- Arnold Palmers,
    Large Nets, Apple Cider, Orange Juice -- :func:`optimizer.surplus_sale_value`
    finds the most gold they are worth; this lists each good and the nominal total.
    Each count and the total always split into inventory (already held) versus
    accumulated (what the plan itself produced), via
    :func:`optimizer.leftover_inventory_share`, so the nominal value shows how
    much value executing the plan actually adds -- zero on either side when a
    good was entirely held or entirely made.
    """
    leftovers = optimizer.plan_leftovers(
        plan, demand, recipes, crafting_bonus, open_flows, inventory
    )
    rows = optimizer.surplus_sale_value(leftovers, recipes, crafting_bonus, prices)
    if not rows:
        return
    # Split each good's count (and so its gold) into what was already held versus
    # what the plan itself accumulated, so the nominal value shows how much value
    # executing the plan actually added on top of stock the player already owned.
    inventory_share = optimizer.leftover_inventory_share(
        plan, demand, recipes, crafting_bonus, open_flows, inventory
    )
    total = sum(gold for _, _, gold in rows)
    inventory_gold = sum(
        min(inventory_share.get(item, 0.0), count) * prices[item] for item, count, _ in rows
    )
    print("\nSurplus (accumulates past the goal), craftable into sellable goods:")
    width = _num_width(round(count) for _, count, _ in rows)
    for item, count, gold in rows:
        held = min(inventory_share.get(item, 0.0), count)
        print(
            f"  {round(count):>{width},}x {display.get(item, item)}"
            f" ({round(held):,} inventory + {round(count - held):,} accumulated)"
            f" = {round(gold):,} g"
        )
    print(
        f"  nominal value = {round(total):,} g"
        f" ({round(inventory_gold):,} g inventory"
        f" + {round(total - inventory_gold):,} g accumulated)"
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
    leftovers = optimizer.plan_leftovers(
        plan, demand, recipes, crafting_bonus, open_flows, inventory
    )
    rows = optimizer.surplus_sale_value(leftovers, recipes, crafting_bonus, prices)
    if not rows:
        return
    inventory_share = optimizer.leftover_inventory_share(
        plan, demand, recipes, crafting_bonus, open_flows, inventory
    )
    total = sum(gold for _, _, gold in rows)
    inventory_gold = sum(
        min(inventory_share.get(item, 0.0), count) * prices[item] for item, count, _ in rows
    )
    doc.heading("Surplus (accumulates past the goal), craftable into sellable goods")
    table_rows = []
    for item, count, gold in rows:
        held = min(inventory_share.get(item, 0.0), count)
        table_rows.append([
            display.get(item, item),
            f"{round(count):,}",
            f"{round(held):,}",
            f"{round(count - held):,}",
            f"{round(gold):,} g",
        ])
    doc.table(["Item", "Qty", "Inventory", "Accumulated", "Gold"], table_rows)
    doc.para(
        f"Nominal value: {round(total):,} g "
        f"({round(inventory_gold):,} g inventory + "
        f"{round(total - inventory_gold):,} g accumulated)"
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
        }.items()
        if round(spent) >= 1
    }
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
) -> None:
    """Expand one plan into its per-channel actions and leftover materials.

    ``pumpkin_schedule`` (from :func:`main.pumpkin_juice_schedule`) gives, per goal
    item, the mastery levels at which to drink its Pumpkin Juices so each lands its
    intended tier boost; the Pumpkin Juice section names them.
    """
    inventory = inventory or {}
    ctx = _build_plan_context(
        plan, demand, workshop, crafting_bonus, open_flows, openable_names, inventory
    )
    display, recipes = ctx.display, ctx.recipes
    open_flows = ctx.open_flows
    need, elsewhere = ctx.need, ctx.elsewhere
    spent_consumables = ctx.spent_consumables

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
        print("\nExploring:")
        ordered = sorted(palmer_draws, key=lambda d: d.ap, reverse=True)
        width = _num_width(math.ceil(d.units) for d in ordered)
        for draw in ordered:
            location = draw.offer.name.removeprefix("explore ")
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
            zone = draw.offer.name.removeprefix("cider ")
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
                if residual <= 1e-6:
                    continue
            wait = timedelta(minutes=residual / rate)
            rows.append((wait, display.get(key, key), round(residual), key))
        if rows:
            print("\nPassive production (concurrent):")
            # List the item on the critical path first (only when passive is the
            # plan's bottleneck track), then the rest by quantity, then by how long
            # they take. The critical item is whatever print_critical_path names --
            # not simply the slowest, since a pet top-up is skipped over a building.
            critical_key = None
            if not _farming_drives(
                plan, need, elsewhere, display, production, sourceable_elsewhere
            ):
                critical = _slowest_passive_item(
                    plan, need, elsewhere, display, production
                )
                if critical is not None:
                    critical_key = critical[3]
            width = _num_width(quantity for _, _, quantity, _ in rows)
            ordered = sorted(
                rows, key=lambda r: (r[3] != critical_key, -r[2], -r[0])
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

    purchases = [(item, units) for item, units in plan.bought.items() if round(units) >= 1]
    if purchases and prices is not None:
        print("\nBuy (with gold):")
        ordered = sorted(
            purchases, key=lambda kv: kv[1] * prices.get(kv[0], 0.0), reverse=True
        )
        width = _num_width(round(units) for _, units in purchases)
        for item, units in ordered:
            cost = round(units * prices.get(item, 0.0))
            print(
                f"  {round(units):>{width},}x {display.get(item, item)}"
                f" = {cost:,} g"
            )
        # Pumpkin Juice is bought too but has no per-unit price in ``prices`` (its
        # gold is on the plan), so the consumable buys total is plan.gold net of it.
        print(f"  total {round(plan.gold - plan.pumpkin_gold):,} g")

    # Pumpkin Juice: a bought mastery boost, listed apart from the consumable buys
    # since it covers a goal's mastery directly rather than feeding a channel. Each
    # line names the goal it boosts, how many juices, and the gold and mastery gained.
    juices = [
        (item, units) for item, units in plan.pumpkin_juices.items() if round(units) >= 1
    ]
    if juices and plan.pumpkin_gold > 0:
        per_juice = plan.pumpkin_gold / _pumpkin_juices(plan)
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

    # Every consumable is grounded now, so the plan self-sources exactly what it
    # needs (net of buying) and the leftovers are genuine spare capacity -- the
    # surplus value is rigorous, shown whenever consumables are priced.
    if prices is not None:
        _print_surplus_value(
            plan, demand, recipes, crafting_bonus, open_flows, inventory, prices,
            display,
        )

    if plan.remainder:
        print("\nMaterials with no known source (supply yourself):")
        for material in sorted(plan.remainder):
            print(f"  {plan.remainder[material]:,}x {material}")


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
) -> None:
    """Markdown counterpart to :func:`print_plan`: the same sections, as tables.

    Shares :func:`_build_plan_context` and every decision helper (critical path,
    farming-drives, passive sourcing) with :func:`print_plan`, so the two renderers
    always agree on content -- only the layout (table vs. aligned text) differs.
    """
    inventory = inventory or {}
    ctx = _build_plan_context(
        plan, demand, workshop, crafting_bonus, open_flows, openable_names, inventory
    )
    display = ctx.display
    need, elsewhere = ctx.need, ctx.elsewhere

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
        doc.table(["Large Nets", "Zone", "Yields"], rows)

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
                d.offer.name.removeprefix("explore "),
                _wanted_counts(ctx, d),
            ]
            for d in ordered
        ]
        doc.heading("Exploring")
        doc.table(["Arnold Palmers", "Location", "Yields"], rows)
    if cider_draws:
        def ciders(d: sources.Draw) -> float:
            return d.units * d.offer.consumes.get("apple cider", 0.0)

        ordered = sorted(cider_draws, key=ciders, reverse=True)
        rows = []
        for d in ordered:
            stamina = d.units * d.offer.consumes.get("stamina", 0.0)
            rows.append([
                f"{math.ceil(ciders(d)):,}",
                d.offer.name.removeprefix("cider "),
                f"{-round(stamina):,}",
                _wanted_counts(ctx, d),
            ])
        doc.heading("Exploring (Apple Cider)")
        doc.table(["Apple Cider", "Zone", "Stamina", "Yields"], rows)

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
            keys = [
                f"{round(quantity * count):,} {display.get(item, item)}"
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
                ", ".join(keys),
                gained,
            ])
        doc.heading("Opening (Locksmith)")
        doc.table(["Count", "Item", "Materials used", "Yields"], rows)

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
        doc.table(["Action", "Duration", "Item", "Qty", "Detail"], rows)

    passive_draw = plan.passive_draw
    if passive_draw is not None:
        offer = passive_draw.offer
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
                if residual <= 1e-6:
                    continue
            wait = timedelta(minutes=residual / rate)
            prod_rows.append((wait, display.get(key, key), round(residual), key))
        if prod_rows:
            critical_key = None
            if not _farming_drives(
                plan, need, elsewhere, display, production, sourceable_elsewhere
            ):
                critical = _slowest_passive_item(
                    plan, need, elsewhere, display, production
                )
                if critical is not None:
                    critical_key = critical[3]
            ordered = sorted(
                prod_rows, key=lambda r: (r[3] != critical_key, -r[2], -r[0])
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
            doc.table(["Item", "Qty", "Time", "Source"], rows)

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

    purchases = [(item, units) for item, units in plan.bought.items() if round(units) >= 1]
    if purchases and prices is not None:
        ordered = sorted(
            purchases, key=lambda kv: kv[1] * prices.get(kv[0], 0.0), reverse=True
        )
        rows = [
            [
                display.get(item, item),
                f"{round(units):,}",
                f"{round(units * prices.get(item, 0.0)):,} g",
            ]
            for item, units in ordered
        ]
        doc.heading("Buy (with gold)")
        doc.table(["Item", "Qty", "Gold"], rows)
        doc.para(f"Total: {round(plan.gold - plan.pumpkin_gold):,} g")

    juices = [
        (item, units) for item, units in plan.pumpkin_juices.items() if round(units) >= 1
    ]
    if juices and plan.pumpkin_gold > 0:
        per_juice = plan.pumpkin_gold / _pumpkin_juices(plan)
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

    if prices is not None:
        markdown_surplus_value(
            doc, plan, demand, ctx.recipes, crafting_bonus, ctx.open_flows,
            inventory, prices, display,
        )

    if plan.remainder:
        rows = [
            [material, f"{plan.remainder[material]:,}"]
            for material in sorted(plan.remainder)
        ]
        doc.heading("Materials with no known source (supply yourself)")
        doc.table(["Material", "Qty"], rows)


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


class Reporter:
    """Uniform interface over the two output formats ``main.run`` drives.

    Text prints immediately, matching every behaviour before this class existed;
    Markdown buffers into a :class:`MarkdownDoc` and renders once in
    :meth:`finish` -- via `rich` to the terminal, or as raw Markdown source when
    stdout isn't one (so redirecting to a ``.md`` file gets real Markdown).
    """

    def solution(self, solution: farm_solver.Solution) -> None:
        raise NotImplementedError

    def unobtainable(self, materials: list[str]) -> None:
        raise NotImplementedError

    def note(self, text: str) -> None:
        raise NotImplementedError

    def frontier(
        self,
        plans: list[optimizer.SourcingPlan],
        selected: int,
        cheapest: int,
        reasons: list[str | None] | None = None,
        prices: dict[str, float] | None = None,
        golds: list[tuple[float, float]] | None = None,
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

    def solution(self, solution: farm_solver.Solution) -> None:
        print_solution(solution)

    def unobtainable(self, materials: list[str]) -> None:
        print_unobtainable(materials)

    def note(self, text: str) -> None:
        print(f"\n{text}")

    def frontier(self, *args, **kwargs) -> None:
        print_frontier(*args, **kwargs)

    def plan(self, *args, **kwargs) -> None:
        print_plan(*args, **kwargs)

    def crafts(self, *args, **kwargs) -> None:
        print_crafts(*args, **kwargs)


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

    def solution(self, solution: farm_solver.Solution) -> None:
        markdown_solution(self.doc, solution)

    def unobtainable(self, materials: list[str]) -> None:
        markdown_unobtainable(self.doc, materials)

    def note(self, text: str) -> None:
        self.doc.para(text)

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
        if self.pager:
            with console.pager(styles=True):
                console.print(RichMarkdown(text))
        else:
            console.print(RichMarkdown(text))


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
        }
        self.notes: list[str] = []

    def solution(self, solution: farm_solver.Solution) -> None:
        markdown_solution(self.docs["solution"], solution)

    def unobtainable(self, materials: list[str]) -> None:
        markdown_unobtainable(self.docs["solution"], materials)

    def note(self, text: str) -> None:
        self.notes.append(text)
        self.docs["notes"].para(text)

    def frontier(self, *args, **kwargs) -> None:
        markdown_frontier(self.docs["frontier"], *args, **kwargs)

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
