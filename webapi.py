"""Framework-free web API layer over the solver, for the Django app (web/).

Kept separate from :mod:`main` (which owns the CLI: argument parsing, file
loading, printing dispatch) and from Django (which owns HTTP: routing,
request/response, the process-wide cache and solve semaphore -- see
web/solverapp/). This module bridges the two: it turns a web request's JSON
``params`` into the same :class:`argparse.Namespace` the CLI builds, drives
:mod:`main`'s three solve stages (``build_inputs``/``solve_frontier``/
``derive_report_inputs``) from pasted text instead of files, and renders a
report.SectionedReporter's sections into the fields a JSON response needs.
Nothing here imports Django, so it is plain-pytest testable.

Two parameter classes drive the caching/re-solve split (ARCHITECTURE.md has
the full design writeup): a change to a **resolve** field
(:data:`RESOLVE_FIELDS`) changes what gets solved, so it is part of
:func:`fingerprint`'s cache key; a change to a **refilter** field
(:data:`REFILTER_FIELDS`) only changes how the *same* cached frontier is
displayed (which plans pass the limits, which one is selected, whether the
crafts breakdown is shown), so :func:`render` recomputes it against the
cached :class:`SolveSession` without a re-solve -- except the one horizon-guard
edge case documented on :func:`render`.
"""

from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass, replace
from datetime import date

import farm_solver
import main
import optimizer
import parsing
import passive
import pets
import report

# Args fields that change what gets solved -- part of the cache key (see
# fingerprint). A change to any of these invalidates the cached frontier.
RESOLVE_FIELDS = (
    "target", "level", "mastery", "items", "use_pumpkin_juice",
    "current_stamina", "cookie_sets", "plans", "start_date",
    "model_calendar", "average_calendar", "ignore_calendar", "crafting_bonus",
    "mastery_bonus", "override_inventory", "override",
)

# Args fields that only change how the cached frontier is displayed --
# render() recomputes these against the cached SolveSession, no re-solve.
REFILTER_FIELDS = (
    "max_gold", "max_ap", "max_ln", "passive_limit",
    "plan", "cheapest_plan", "fastest_plan", "breakdown",
)

# JSON keys accepted in a request's "params" object; anything else is a
# request error (params_to_args fails loud rather than silently ignoring a
# typo'd or client/server-version-mismatched key).
_KNOWN_PARAM_KEYS = frozenset({
    "target", "level", "mastery", "items", "override_inventory",
    "use_pumpkin_juice", "max_gold", "cookie_sets", "current_stamina",
    "plans", "start_date", "calendar", "plan_select", "max_ap", "max_ln",
    "passive_limit", "overrides", "crafting_bonus", "mastery_bonus",
    "breakdown",
})


def _wrap_argument_error(fn, value):
    """Call an ``argparse``-flavoured parser (raises ``ArgumentTypeError``),
    re-raising as ``ValueError`` so every ``params_to_args`` failure is the
    same exception type regardless of which CLI helper it reuses.
    """
    try:
        return fn(value)
    except argparse.ArgumentTypeError as exc:
        raise ValueError(str(exc)) from exc


def params_to_args(params: dict) -> argparse.Namespace:
    """Build a CLI-equivalent ``argparse.Namespace`` from a request's params.

    Starts from ``build_parser().parse_args([])`` (every default) and applies
    each recognised key on top; an unrecognised key raises ``ValueError``
    rather than being silently ignored. ``mastery``/``items``/
    ``override_inventory`` are validated here (so a bad spec fails before any
    solving starts) but the *raw string* is stored on ``args`` -- exactly what
    the CLI itself carries, so ``main.build_inputs`` re-parses it the same way
    either path.

    Never sets ``args.solver``, ``args.format``, ``args.pager``, any file
    path, or either ``--empty-*`` flag: the LP backend is pinned once at
    Django startup (never per-request), and there is no file path or output
    format in a web request.
    """
    unknown = set(params) - _KNOWN_PARAM_KEYS
    if unknown:
        raise ValueError(f"unknown param(s): {', '.join(sorted(unknown))}")

    args = main.build_parser().parse_args([])

    if "target" in params:
        args.target = _wrap_argument_error(main.parse_target, str(params["target"]))
    if "level" in params and params["level"] is not None:
        args.level = int(params["level"])
    if "mastery" in params and params["mastery"] is not None:
        raw = params["mastery"]
        parsing.parse_item_amounts(raw)
        args.mastery = raw
    if "items" in params and params["items"] is not None:
        raw = params["items"]
        parsing.parse_item_amounts(raw)
        args.items = raw
    if "override_inventory" in params and params["override_inventory"] is not None:
        raw = params["override_inventory"]
        parsing.parse_item_amounts(raw)
        args.override_inventory = raw
    if "use_pumpkin_juice" in params:
        value = params["use_pumpkin_juice"]
        args.use_pumpkin_juice = None if value is None else int(value)
    if "max_gold" in params and params["max_gold"] is not None:
        args.max_gold = _wrap_argument_error(
            main.parse_gold_budget, str(params["max_gold"])
        )
    if "cookie_sets" in params and params["cookie_sets"] is not None:
        args.cookie_sets = int(params["cookie_sets"])
    if "current_stamina" in params and params["current_stamina"] is not None:
        args.current_stamina = int(params["current_stamina"])
    if "plans" in params and params["plans"] is not None:
        args.plans = int(params["plans"])
    if "start_date" in params:
        value = params["start_date"]
        args.start_date = date.fromisoformat(value) if value else None
    if "max_ap" in params:
        value = params["max_ap"]
        args.max_ap = int(value) if value is not None else None
    if "max_ln" in params:
        value = params["max_ln"]
        args.max_ln = int(value) if value is not None else None
    if "passive_limit" in params:
        raw = params["passive_limit"]
        if raw is not None:
            passive.parse_duration(raw)
        args.passive_limit = raw
    if "crafting_bonus" in params and params["crafting_bonus"] is not None:
        args.crafting_bonus = float(params["crafting_bonus"])
    if "mastery_bonus" in params and params["mastery_bonus"] is not None:
        args.mastery_bonus = float(params["mastery_bonus"])
    if "breakdown" in params:
        args.breakdown = bool(params["breakdown"])

    calendar = params.get("calendar")
    if calendar in ("full", "model"):  # "model" = the pre-enum wire value
        args.model_calendar = True
    elif calendar == "average":
        args.average_calendar = True
    elif calendar == "ignore":
        args.ignore_calendar = True
    elif calendar is not None:
        raise ValueError(
            f"calendar must be 'full', 'average', 'ignore', or null, not {calendar!r}"
        )

    plan_select = params.get("plan_select")
    if plan_select is not None:
        kind = plan_select.get("kind")
        if kind == "fastest":
            args.fastest_plan = True
        elif kind == "cheapest":
            args.cheapest_plan = True
        elif kind == "index":
            index = plan_select.get("index")
            if index is None:
                raise ValueError("plan_select kind 'index' needs an 'index'")
            args.plan = int(index)
        else:
            raise ValueError(
                "plan_select.kind must be 'fastest', 'cheapest' or 'index', "
                f"not {kind!r}"
            )

    if "overrides" in params and params["overrides"] is not None:
        overrides = list(params["overrides"])
        main.apply_overrides({}, overrides)  # validate syntax only; cfg discarded
        args.override = overrides

    return args


def fingerprint(mastery_text: str, inventory_text: str, args: argparse.Namespace) -> str:
    """Session identity: a sha256 of both pasted texts plus every resolve field.

    Two requests that agree on this hash would produce the identical frontier,
    so a cache hit skips solving entirely; anything that only changes
    :data:`REFILTER_FIELDS` is deliberately excluded, so those requests keep
    hitting the same cache entry. ``start_date`` is special-cased to today's
    date when unset (rather than hashing ``None``), so the *default* key
    doesn't quietly rot at midnight between an initial solve and a same-day
    refilter.
    """
    parts = [mastery_text, inventory_text]
    for name in RESOLVE_FIELDS:
        value = date.today().isoformat() if name == "start_date" and not args.start_date else getattr(args, name)
        parts.append(repr(value))
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


@dataclass
class SolveSession:
    """Everything cached for a fingerprint: the solved frontier and its context.

    ``cfg`` (the loaded, ``--override``-applied config) is not part of the
    plan's original 3-field sketch, but is needed here: :func:`main.solve_frontier`
    takes it as an explicit parameter (see main.py's docstring on that
    function for why it isn't folded into ``SolveInputs``), and the horizon
    guard in :func:`render` re-solves without going through :func:`solve`
    again, so it has to come from somewhere -- caching it alongside the rest
    of the session is cheaper than reloading/reapplying overrides every
    refilter and is safe: it depends only on ``args.override``, itself a
    :data:`RESOLVE_FIELDS` member, so it can never go stale within one
    fingerprint's session.
    """

    inputs: main.SolveInputs
    frontier: main.FrontierResult
    ctx: main.PlanContextData
    cfg: dict


@dataclass
class SolveResult:
    """What a request needs back: rendered markdown sections + the frontier table."""

    sections: dict[str, str]  # section name -> markdown (SectionedReporter.docs)
    notes: list[str]
    warnings: list[str]
    frontier_rows: list[dict]
    selected: int
    resolved: bool  # True when this render triggered a re-solve (horizon guard)
    # The tower level the goal targeted (see SolveInputs.target_level) -- the
    # UI shows what "highest reachable" resolved to. None for pure -m/-i goals.
    target_level: int | None = None


def _empty_plan_context() -> main.PlanContextData:
    """A placeholder PlanContextData for the "nothing left to source" case.

    derive_report_inputs would crash on an empty frontier (cheapest_plan's
    min() over no plans); nothing calls it in that case.
    """
    return main.PlanContextData(
        cheapest=0, display={}, max_passive_residuals={}, reasons=[],
        golds=None, sourceable_elsewhere=set(),
    )


def _nothing_to_source_result(inputs: main.SolveInputs, resolved: bool) -> SolveResult:
    reporter = report.SectionedReporter()
    reporter.solution(inputs.solution)
    reporter.note("Nothing left to source.")
    return SolveResult(
        sections={name: doc.render() for name, doc in reporter.docs.items()},
        notes=list(reporter.notes), warnings=[], frontier_rows=[],
        selected=-1, resolved=resolved, target_level=inputs.target_level,
    )


def solve(mastery_text: str, inventory_text: str, args: argparse.Namespace) -> SolveSession:
    """Run a full (re-)solve from pasted text: the ~0.5-3s path (see ARCHITECTURE.md).

    Mirrors main.run's file-loading + override-inventory step (there:
    load_state on the CLI's files; here: a GameState built directly from
    request text) followed by the same three solve stages build_inputs /
    solve_frontier / derive_report_inputs main.run itself drives.
    """
    cfg = main.load_config()
    main.apply_overrides(cfg, args.override)
    # Mirror main.run: the calendar 'ignore' mode drops seasonal pets from the
    # config before anything reads [pets].
    if main.resolve_calendar_mode(args, cfg) == "ignore":
        cfg = main.strip_dated_sources(cfg)
    state = farm_solver.GameState(
        masteries=parsing.parse_masteries(mastery_text),
        inventory=parsing.parse_inventory(inventory_text),
    )
    if args.override_inventory is not None:
        main.apply_inventory_overrides(
            state.inventory, parsing.parse_item_amounts(args.override_inventory)
        )

    inputs = main.build_inputs(args, cfg, state)
    fr = main.solve_frontier(inputs, args, cfg)
    ctx = main.derive_report_inputs(inputs, fr, args) if fr.plans else _empty_plan_context()
    return SolveSession(inputs=inputs, frontier=fr, ctx=ctx, cfg=cfg)


def render(
    session: SolveSession, args: argparse.Namespace
) -> tuple[SolveResult, SolveSession]:
    """Apply a refilter (instant) against ``session``'s cached frontier.

    HORIZON GUARD -- the one refilter that can force a re-solve: with the
    seasonal calendar modelled, the cached frontier's ``horizon`` is the
    makespan the fixed point converged on for whichever plan was selected at
    solve time. A refilter (limits, or an explicit plan/cheapest selection)
    can land on a *different* plan whose own makespan no longer agrees with
    that horizon -- a date-dependent source (a seasonal pet, the Mechanical
    Heart's grants) would then be modelled at the wrong span, so its passive
    breakpoints and the plan's own numbers could be inconsistent. When that
    happens, re-solve the fixed point seeded from the newly selected plan's
    makespan instead of displaying stale seasonal figures; every other
    refilter is pure post-processing over the cached frontier, no LP solve.
    """
    inputs = session.inputs
    fr = session.frontier

    if not fr.plans:
        return _nothing_to_source_result(inputs, resolved=False), session

    time_limit = (
        passive.parse_duration(args.passive_limit)
        if args.passive_limit is not None
        else None
    )

    def _refilter(
        frontier: main.FrontierResult,
    ) -> tuple[list[optimizer.SourcingPlan], list[optimizer.SourcingPlan], int]:
        within = [
            p for p in frontier.plans
            if main.within_limits(p, time_limit, args.max_ap, args.max_ln, args.max_gold)
        ]
        shown = within or frontier.plans
        selected = main.selected_plan(shown, args.plan, args.cheapest_plan)
        return within, shown, selected

    within, shown, selected = _refilter(fr)
    resolved = False

    if inputs.model_calendar and not main._makespans_close(
        shown[selected].time, fr.horizon
    ):
        fr = main.solve_frontier(
            inputs, args, session.cfg, horizon_seed=shown[selected].time
        )
        resolved = True
        if not fr.plans:
            session = SolveSession(
                inputs=inputs, frontier=fr, ctx=_empty_plan_context(), cfg=session.cfg
            )
            return _nothing_to_source_result(inputs, resolved=True), session
        session = SolveSession(
            inputs=inputs, frontier=fr,
            ctx=main.derive_report_inputs(inputs, fr, args), cfg=session.cfg,
        )
        within, shown, selected = _refilter(fr)

    # ctx.reasons/golds/cheapest were computed over whatever `shown` looked
    # like at solve() (or the re-solve above) time; a refilter can leave
    # `shown` different (fewer plans, different order), so recompute those
    # three here against the current `shown` -- cheap (no LP; the reference
    # plan's self_sourced_endpoint/passive_residuals in max_passive_residuals
    # stay cached, as does display).
    ctx = session.ctx
    reasons = report._frontier_reasons(
        shown, inputs.demand, inputs.recipes, args.crafting_bonus,
        inputs.open_flows, inputs.inventory, ctx.max_passive_residuals,
        fr.production, ctx.display,
    )
    golds = None
    if inputs.prices is not None:
        golds = [optimizer.plan_gold_breakdown(p, inputs.prices) for p in shown]
    cheapest = main.cheapest_plan(shown)
    ctx = replace(ctx, reasons=reasons, golds=golds, cheapest=cheapest)
    session = replace(session, ctx=ctx)

    reporter = report.SectionedReporter()
    reporter.solution(inputs.solution)
    if inputs.model_calendar:
        dated = []
        if any(name.lower() in pets.SEASONAL_MONTHS for name in session.cfg.get("pets", {})):
            dated.append("seasonal pets")
        if inputs.monthly_grants:
            dated.append("the Mechanical Heart's grants")
        reporter.note(
            f"Starting {inputs.start:%d %b %Y}; {' and '.join(dated)} count only "
            "for the part of the plan they fall in."
        )
    warnings = []
    if not within:
        message = main.limit_shortfall(
            fr.plans, time_limit, args.max_ap, args.max_ln, args.max_gold
        )
        reporter.note(message)
        warnings.append(message)

    reporter.frontier(shown, selected, ctx.cheapest, ctx.reasons, inputs.prices, ctx.golds)
    chosen = shown[selected]
    inputs.solution.arnold_palmers = round(chosen.arnold_palmers)
    inputs.solution.large_nets = round(chosen.large_nets)
    pj_schedule = main.pumpkin_juice_schedule(chosen, inputs.solution.needs, inputs.pj_gain)
    reporter.plan(
        chosen, inputs.demand, inputs.farm, inputs.workshop, args.crafting_bonus,
        fr.production, inputs.open_flows, inputs.smith.names, inputs.inventory,
        ctx.sourceable_elsewhere, ctx.max_passive_residuals, inputs.prices, pj_schedule,
    )
    if args.breakdown:
        reporter.crafts(chosen, inputs.workshop, args.crafting_bonus, args.mastery_bonus)

    frontier_rows = [
        {
            "index": i,
            "time": passive.format_duration(plan.time),
            "time_seconds": plan.time.total_seconds(),
            "gold": plan.gold,
            "net_gold": plan.gold - plan.surplus_gold,
            "pumpkin_juices": round(report._pumpkin_juices(plan)),
            "selected": i == selected,
            "cheapest": i == ctx.cheapest,
        }
        for i, plan in enumerate(shown)
    ]

    result = SolveResult(
        sections={name: doc.render() for name, doc in reporter.docs.items()},
        notes=list(reporter.notes),
        warnings=warnings,
        frontier_rows=frontier_rows,
        selected=selected,
        resolved=resolved,
        target_level=session.inputs.target_level,
    )
    return result, session
