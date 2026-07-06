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
import math
from collections.abc import Iterable
from datetime import timedelta

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


def print_unobtainable(materials: list[str]) -> None:
    print("The following materials are required for some crafts but are unobtainable:")
    if not materials:
        print("  (none)")
        return
    for material in materials:
        print(f"  {material}")


def _cookie_sets_used(plan: optimizer.SourcingPlan) -> int:
    """Whole cookie sets a plan spends — partial sets aren't usable, so round up.

    One cookie set covers one real day, boosting whichever crops you harvest that
    day alongside the day's Grape Juice allocation. Crops grow one after another,
    so the sets a plan needs is the whole number of days its cookie-boosted
    farming spans (``ceil`` of the total), not the sum of each crop's rounded-up
    days — that would double-count a day two crops share.
    """
    return math.ceil(plan.cookie_sets)


def print_frontier(
    plans: list[optimizer.SourcingPlan],
    selected: int,
    cheapest: int,
    reasons: list[str | None] | None = None,
    prices: dict[str, float] | None = None,
    golds: list[tuple[float, float]] | None = None,
) -> None:
    """The frontier table: each plan's time and consumables, fastest first.

    Cookie sets and Grape Juices are shown as extra columns only when some plan
    actually spends them, so a plan that uses neither keeps the plain time/AP/LN
    table. With trading ``prices`` a trailing **gold** column shows each plan's
    real gold cost (what "cheapest" ranks by). When ``golds`` is given (one
    ``(to_buy, passive)`` pair per plan, see :func:`optimizer.plan_gold_breakdown`)
    that total is split into the gold you must still **buy** and the gold value of
    what the plan makes/holds **passively** -- so a plan that accumulates its Large
    Nets from straw/wood/stone/antlers shows them in the total but not the to-buy
    figure. ``reasons`` annotates why each plan is on the frontier -- a resource
    plan says "all Steel Wire from passive" (its gathering plateau); endpoints and
    farming intermediates carry only their fastest/cheapest tag.
    """
    show_cookies = any(_cookie_sets_used(p) >= 1 for p in plans)
    show_juices = any(round(p.grape_juices) >= 1 for p in plans)
    show_ciders = any(round(p.apple_ciders) >= 1 for p in plans)

    columns = ["time", "Arnold Palmers", "Large Nets"]
    if show_cookies:
        columns.append("cookie sets")
    if show_juices:
        columns.append("Grape Juice")
    if show_ciders:
        columns.append("Apple Cider")
    legend = ""
    if prices is not None:
        legend = " = gold, as to-buy + passive" if golds is not None else " = gold"
    print(f"\nSourcing plans ({' + '.join(columns)}{legend}):")

    for i, plan in enumerate(plans):
        marker = ">" if i == selected else " "
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
            parts.append(f"{round(plan.apple_ciders):,} cider")
        if prices is not None:
            if golds is not None:
                buy, passv = (round(g) for g in golds[i])
                note = f" = {buy:,} + {passv:,} = {buy + passv:,} g"
            else:
                note = f" = {round(optimizer.plan_gold(plan, prices)):,} g"
        print(f" {marker} {i + 1}. {' + '.join(parts)}{note}")


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
    with consumables, or pick a faster plan) to finish sooner.
    """
    # Concurrent tracks; the makespan is the longest one (derived from the draws,
    # not plan.time, so directly-built plans without a cached time still work).
    makespan = max(plan.track_times().values(), default=timedelta(0))
    if makespan <= timedelta(0):
        return  # everything is instant; there is no wait to explain

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
        return
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
        return f" (from {name}{_season_note(name)})"
    counts = _apportion(quantity, [rate for rate, _ in contributors])
    labelled = sorted(
        ((count, rate, name) for count, (rate, name) in zip(counts, contributors)),
        key=lambda t: (-t[0], -t[1], t[2]),  # biggest share, then rate, then name
    )
    parts = [f"{count:,} from {name}{_season_note(name)}" for count, _, name in labelled]
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
) -> None:
    """Expand one plan into its per-channel actions and leftover materials."""
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

    def wanted(offer: sources.Offer) -> str:
        return ", ".join(
            sorted(display.get(key, key) for key in offer.yields if key in interesting)
        )

    def sourced(draws: list[sources.Draw]) -> list[sources.Draw]:
        # Drop residue draws: CBC can leave a sliver of a net, palmer or planting
        # on a channel nothing needs, which rounds to zero produced in ~zero time
        # or consumable -- not a real action, so keep it out of the report.
        return [
            d
            for d in draws
            if any(round(d.produced[k]) >= 1 for k in d.offer.yields if k in interesting)
        ]

    def draws_on(track: str) -> list[sources.Draw]:
        return [d for d in plan.draws if d.offer.track == track]

    print_critical_path(
        plan, need, elsewhere, display, production, sourceable_elsewhere,
        max_passive_residuals,
    )

    fishing_draws = sourced(draws_on(sources.FISHING_TRACK))
    if fishing_draws:
        print("\nFishing:")
        ordered = sorted(fishing_draws, key=lambda d: d.ln, reverse=True)
        width = _num_width(math.ceil(d.units) for d in ordered)
        for draw in ordered:
            zone = draw.offer.name.removeprefix("fish ")
            print(
                f"  {math.ceil(draw.units):>{width},}x Large Net at {zone}"
                f" ({wanted(draw.offer)})"
            )

    exploring_draws = sourced(draws_on(sources.EXPLORING_TRACK))
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
                f" ({wanted(draw.offer)})"
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
                f" ({round(stamina):,} stamina, {wanted(draw.offer)})"
            )

    # Stamina for Apple Cider (inventory plan mode): what the ciders spend, and
    # where it comes from -- the banked start (--current-stamina), the Farmhouse's
    # daily passive deposit, and any Orange Juice drunk to top it up.
    drinking_draws = draws_on(sources.DRINKING_TRACK)
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
        total_oj = sum(
            d.units * d.offer.consumes.get("orange juice", 0.0) for d in drinking_draws
        )
        if round(total_oj) >= 1:
            total_stamina = sum(d.produced.get("stamina", 0.0) for d in drinking_draws)
            print(
                f"  Drink {round(total_oj):,}x Orange Juice"
                f" -> {round(total_stamina):,} stamina"
            )

    # Opening chests and bags at the Locksmith. Skip residue opens that yield less
    # than one of anything wanted (CBC can leave a sliver, as with draws).
    opens = [
        (name, count)
        for name, count in plan.opens.items()
        if (flow := flows_by_name.get(name.lower())) is not None
        and any(
            round(flow.yields[item] * count) >= 1
            for item in flow.yields
            if item in need
        )
    ]
    if opens:
        print("\nOpening (Locksmith):")
        ordered = sorted(opens, key=lambda nc: nc[1], reverse=True)
        width = _num_width(math.ceil(count) for _, count in ordered)
        for name, count in ordered:
            flow = flows_by_name[name.lower()]
            keys = [
                f"{round(quantity * count):,} {display.get(item, item)}"
                for item, quantity in flow.consumes.items()
                if item != name.lower() and round(quantity * count) >= 1
            ]
            cost = f" using {', '.join(keys)}" if keys else ""
            gained = ", ".join(
                f"{round(flow.yields[item] * count):,}x {display.get(item, item)}"
                for item in sorted(flow.yields)
                if item in need and round(flow.yields[item] * count) >= 1
            )
            detail = f" -> {gained}" if gained else ""
            print(
                f"  {math.ceil(count):>{width},}x"
                f" {display.get(name.lower(), name)}{cost}{detail}"
            )

    farming_draws = sourced(draws_on(sources.FARMING_TRACK))
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
            if key in interesting
        )
        for draw in sorted(farming_draws, key=lambda d: d.time, reverse=True):
            juices = draw.gj  # LP-tracked; zero for the without-grape-juice offer
            if draw.offer.cookies:
                _print_cookie_farming(draw, juices, interesting, display, width)
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
            for key in sorted(k for k in draw.offer.yields if k in interesting):
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
            if residual > 1e-6:
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

    if plan.remainder:
        print("\nMaterials with no known source (supply yourself):")
        for material in sorted(plan.remainder):
            print(f"  {plan.remainder[material]:,}x {material}")


def print_crafts(
    plan: optimizer.SourcingPlan,
    workshop: crafting.Workshop,
    crafting_bonus: float,
    mastery_bonus: float,
) -> None:
    """List the crafts the plan performs, parents before the items they consume."""
    if not plan.crafts:
        return
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

    print("\nRequired Crafts:")
    for key in order:
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
