"""The sourcing optimiser for Farm Solver.

Given the items still needed, this picks how to obtain them -- gathering across
every channel (passive production, fishing, exploring, farming), crafting, *and*
opening chests -- trading total time against the Arnold Palmers and Large Nets
spent. That choice is a small **linear program**:
one continuous usage level per :class:`~sources.Offer`, one craft count per
craftable item, one open count per openable, plus a makespan ``T``. Because every
objective is linear, the whole ``(time, AP, LN)`` Pareto frontier is recovered
exactly by minimising ``T + price*(AP + LN)`` over a sweep of the price of time
(:func:`frontier`) -- no metaheuristic needed.

Crafting is folded into the LP so the craft-vs-gather choice is optimised rather
than pre-decided: each recipe is a flow that consumes its materials and produces
``crafting_bonus`` of its output, at no time or consumable cost. So the solver
crafts an item when that is cheaper than gathering it (e.g. crafting Glass Bottle
from cheaply-explored Glass Orb rather than fishing it net by net), and it even
exploits a co-product it already has in surplus. Joint production needs no
special handling either: a co-product is the same offer variable appearing in
several coverage rows, so one fishing trip is credited to every fish it lands.

Opening chests is folded in the same way (see :class:`~sources.OpenFlow`): an
open consumes the chest and its key and produces the chest's average contents, at
no time or consumable cost. Unlike a craft it is multi-output -- one open yields
the whole bundle -- so it is its own flow variable rather than a recipe. This
lets the solver acquire a chest by fishing or exploring and open it whenever that
beats gathering the contents directly, chaining through chests whose contents are
themselves openable.

The least-cost plan is degenerate, so :func:`minimize` refines it in three
lexicographic solves (see there). First it minimises the weighted cost. Then,
with the makespan pinned, it minimises the priced consumables: a weighted-sum
endpoint prices them too faintly (~1e-6) for the solver to distinguish against
the makespan constraint, so the first solve parks them on an arbitrary vertex --
one that can waste consumables that buy no time (the fastest plan spent Large
Nets and Grape Juice it did not need). Finally, with cost and consumables locked,
it minimises total activity: surplus gathered material can be spun into items the
demand never asked for, and a crop farmed off the bottleneck track adds no time,
so either can sit as slack at no penalty -- dropping it leaves only what the
demand actually needs.

To keep every coverage constraint satisfiable (so no shortfall slack is needed),
the LP is built only over **obtainable** items -- those gatherable, or craftable
from obtainable materials (a fixed point, :func:`_obtainable`). A demanded item
that is neither is dropped into the plan's ``remainder``.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import NamedTuple

import pulp

from ascension import (
    AK_PER_GRANDMASTER,
    AK_PER_MASTER,
    AscensionOutcome,
    AscensionSpec,
    StepCandidate,
)
from crops import Farm
from exploring import Exploring
from fishing import Fishing
from passive import PassiveProduction
from sources import (
    PASSIVE_TRACK,
    Draw,
    Offer,
    OpenFlow,
    aggregate,
    cider_offers,
    exploring_offers,
    farming_offers,
    fishing_offers,
    passive_offers,
    stamina_offers,
)

# Usage below this many units is treated as zero (CBC leaves tiny residuals).
UNIT_EPSILON = 1e-6

# Minutes in a day, for converting the makespan (minutes, like every other track
# quantity) into days for the ascension chore-AK term (see :func:`_build_base`).
_MINUTES_PER_DAY = 1440.0

# Consumables that can be bought for gold instead of self-sourced (the items with
# a player-to-player gold price). Cookie sets are buyable too, but handled apart
# (they are a farming cap, not a balance item). Grape Juice is deliberately absent:
# it is not traded, only pressed from grapes, so it is always self-sourced. The
# Hickory Omelette (a sawmill-boost meal, see :func:`sources.sawmill_boost_offers`)
# has no self-sourced recipe, so it is only ever bought or drawn from held stock.
BUYABLE = frozenset(
    {"arnold palmer", "large net", "apple cider", "orange juice", "hickory omelette"}
)

# Price-of-time anchors for the two frontier endpoints, in minutes saved per
# consumable. A tiny price barely penalises consumables (the fastest plan, using
# them freely but not wastefully); a large one all but forbids them (the
# cheapest, slowest plan) -- 1e9 dwarfs any realistic per-consumable time saving
# without the wild coefficient magnitudes that upset the solver. The interior is
# then filled by an epsilon-constraint sweep on time (see :func:`frontier`).
_FAST_PRICE = 1e-6
_FREE_PRICE = 1e9

# How many makespan caps the epsilon-constraint sweep samples, at least. Kept
# well above the requested plan count so thinning has a dense frontier to spread
# over; the LPs are tiny, so a few dozen extra solves are cheap.
_MIN_CAP_SAMPLES = 40

# Passive-completion breakpoints this close (fraction of the endpoint) to either
# frontier end are dropped: the slowest one always coincides with the cheap plan's
# own makespan, and one hugging an endpoint would just duplicate it.
_CAP_MARGIN = 0.005

# The LP backend. PuLP is the modelling layer, so any of its solvers can run
# these problems; the run makes hundreds of sub-millisecond solves, so what
# matters is per-solve overhead. HiGHS (via highspy) solves in-process and is
# the default whenever it is installed; CBC -- PuLP's bundled, zero-extra-
# dependency fallback -- launches a subprocess per solve (~8 ms of process
# startup each), which once dominated the whole run. Other PuLP solvers
# (``pulp.listSolvers()``) bolt on by name through :func:`set_solver_backend`.
_SOLVER_BACKENDS = {
    "highs": lambda: pulp.HiGHS(msg=False),
    "cbc": lambda: pulp.PULP_CBC_CMD(msg=0),
}
_backend = "highs" if pulp.HiGHS(msg=False).available() else "cbc"


def set_solver_backend(name: str) -> None:
    """Select the LP backend: a key of ``_SOLVER_BACKENDS`` (case-insensitive),
    or any installed PuLP solver name for bolt-on solvers. Raises ValueError
    for an unknown or unavailable one, naming the choices."""
    global _backend
    if name.lower() in _SOLVER_BACKENDS:
        _backend = name.lower()
        return
    if name in pulp.listSolvers(onlyAvailable=True):
        _backend = name
        return
    choices = sorted(_SOLVER_BACKENDS) + sorted(
        set(pulp.listSolvers(onlyAvailable=True)) - {"HiGHS", "PULP_CBC_CMD"}
    )
    raise ValueError(f"unknown solver backend {name!r} (choices: {', '.join(choices)})")


def _solver() -> pulp.LpSolver:
    """A fresh configured solver for one solve, per the selected backend."""
    factory = _SOLVER_BACKENDS.get(_backend)
    return factory() if factory else pulp.getSolver(_backend, msg=0)


def all_offers(
    production: PassiveProduction,
    fishing: Fishing,
    exploring: Exploring,
    farm: Farm,
    boost_offers: list[Offer] | None = None,
) -> list[Offer]:
    """Every production offer across the channels, ready for the optimiser.

    Consumables are always **grounded**: the exploring/fishing/farming offers
    consume real Arnold Palmers / Large Nets / Grape Juice the plan must craft,
    hold, or *buy* (see :func:`minimize`), and the Apple Cider exploring channel
    (fed by stamina from drinking Orange Juice or eating Apples) is always
    available. Whether a
    consumable is self-sourced (costs time) or bought (costs gold) is the LP's
    call, set by the gold budget the frontier sweeps.

    ``boost_offers`` (from :func:`sources.sawmill_boost_offers`) are the optional
    Hickory Omelette sawmill-boost offers: each is grounded in an omelette the plan
    holds or buys, so it too is just another gathering channel here.
    """
    return [
        *passive_offers(production),
        *(boost_offers or []),
        *fishing_offers(fishing, ground=True),
        *exploring_offers(exploring, ground=True),
        *farming_offers(farm, ground=True),
        *cider_offers(exploring),
        *stamina_offers(),
    ]


@dataclass
class SourcingPlan:
    """One way to source the demand: the actions taken, the cost, the remainder.

    ``draws`` are the gathering actions, ``crafts`` maps each crafted item
    (lower-cased) to how many crafts of it to do, and ``opens`` maps each opened
    openable (by display name) to how many of it to open at the Locksmith.
    ``time`` is the makespan (concurrent tracks, the longest),
    ``arnold_palmers``, ``large_nets``, ``cookie_sets`` and ``grape_juices`` the
    consumables spent (raw, un-rounded), and ``remainder`` the demanded items that
    are neither gatherable nor craftable, which you must supply yourself.
    """

    draws: list[Draw] = field(default_factory=list)
    crafts: dict[str, float] = field(default_factory=dict)
    opens: dict[str, float] = field(default_factory=dict)
    time: timedelta = timedelta(0)
    arnold_palmers: float = 0.0
    large_nets: float = 0.0
    cookie_sets: float = 0.0
    grape_juices: float = 0.0
    apple_ciders: float = 0.0
    orange_juice: float = 0.0
    # Hickory Omelettes eaten (one per boosted sawmill-hour, see
    # :func:`sources.sawmill_boost_offers`) -- a held/bought meal, so it is priced
    # like the other consumables and never self-sourced.
    hickory_omelettes: float = 0.0
    # Consumables bought for gold (lower-cased name -> units) and the gold spent on
    # them. The rest of each consumable's spend is self-sourced (costs time, not
    # gold); ``gold`` is the plan's real cost, what the frontier trades against time.
    bought: dict[str, float] = field(default_factory=dict)
    gold: float = 0.0
    # Pumpkin Juices spent (lower-cased goal item -> count) and the gold they cost.
    # A Pumpkin Juice is a bought mastery boost: each one applied to a mastery goal
    # covers part of that item's shortfall directly (see :func:`minimize`). It has no
    # self-sourced alternative, so its cost is folded into ``gold`` (a pure buy) and
    # tracked apart here so the report can name which items it boosts. ``pumpkin_gold``
    # is ``sum(pumpkin_juices) * price``, the slice of ``gold`` spent on juice.
    pumpkin_juices: dict[str, float] = field(default_factory=dict)
    pumpkin_gold: float = 0.0
    # Demand each item's juices cover (lower-cased goal item -> units, ``count *
    # pj_gain``). This is a credit against that item's requirement, so
    # :func:`plan_balance` nets it out of ``need`` and the passive/residual accounting
    # reflects that the boost -- not passive production -- covered that slice.
    pumpkin_credit: dict[str, float] = field(default_factory=dict)
    # Sale value of everything the plan produces past all it consumes -- surplus it
    # banks (see :func:`plan_surplus_gold`), for other tasks or to sell. A credit
    # against ``gold``: the plan's net cost, what the frontier really trades against
    # time, is ``gold - surplus_gold``. Set by :func:`frontier`; a plan straight out
    # of :func:`minimize` leaves it 0 (its net is just what it bought).
    surplus_gold: float = 0.0
    remainder: dict[str, int] = field(default_factory=dict)
    # This stage's ascension outcome (which items were mastered/grandmastered/
    # mega'd, AK earned from steps vs. chores, gross mastery supply) -- set only
    # when :func:`minimize`/:func:`_build_base` was given an :class:`AscensionSpec`.
    ascension: AscensionOutcome | None = None

    def track_times(self) -> dict[str, timedelta]:
        """Total time each track spends, summed across this plan's draws.

        Tracks run concurrently, so ``self.time`` (the makespan) is the max of
        these; within a track (notably farming, grown one crop at a time) the
        draws add up. Shared by the critical-path helpers in :mod:`main`.
        """
        times: dict[str, timedelta] = {}
        for draw in self.draws:
            times[draw.offer.track] = (
                times.get(draw.offer.track, timedelta(0)) + draw.time
            )
        return times

    @property
    def passive_draw(self) -> Draw | None:
        """The single aggregate passive draw in this plan, or ``None``.

        Passive production is one combined offer (see
        :func:`~sources.passive_offers`), so a plan has at most one draw on the
        passive track. Centralised here since many callers need to reach it.
        """
        return next(
            (d for d in self.draws if d.offer.track == PASSIVE_TRACK), None
        )

    @property
    def cost(self) -> tuple[float, float]:
        """The objective ``(time, net gold)`` for comparisons.

        The whole tradeoff is time against gold: self-sourcing a consumable costs
        makespan, buying it costs gold, and everything else (which consumables, how
        many) is just how a plan lands on that curve. The gold is **net** --
        ``gold - surplus_gold``, the gold spent buying less the sale value of the
        surplus the plan banks -- so a slower plan that leans on passive production
        and banks sellable materials can fall *below* zero (resources reserved for
        other tasks), extending the frontier past the "buy nothing" line. Two plans
        with the same time and net gold are equivalent however they mix making,
        buying and banking.
        """
        return (self.time.total_seconds(), self.gold - self.surplus_gold)


def _minutes(time: timedelta) -> float:
    return time.total_seconds() / 60


def _obtainable(
    offers: list[Offer],
    recipes: dict[str, dict[str, float]],
    openables: list[OpenFlow],
    buyable: set[str] = frozenset(),
    held: set[str] = frozenset(),
) -> set[str]:
    """Every item that can be produced: gathered, crafted, opened, bought, or held.

    A fixed point: an offer's yields are obtainable once its ``consumes`` inputs
    all are (an offer with no inputs is unconditionally so); a craftable item once
    its materials all are; an openable's outputs once its consumed items (the
    openable and its key) all are. A ``buyable`` consumable is unconditionally
    obtainable -- it can always be bought for gold, so a channel grounded in one is
    usable even where nothing crafts or gathers it. A ``held`` item likewise seeds
    the set: something the plan holds in inventory grounds any offer that burns it
    (e.g. an Acorn Pie the exploring channel spends) even when nothing crafts or
    gathers it. So an item only an *unusable*
    offer could make -- one whose input can never be sourced -- is correctly *not*
    obtainable, and a demand for it falls to the remainder rather than an infeasible
    LP. Auto-supplied materials (iron, nails) are expected stripped from ``recipes``.
    """
    obtainable: set[str] = set(buyable) | set(held)
    changed = True
    while changed:
        changed = False
        for offer in offers:
            if all(c in obtainable for c in offer.consumes):
                for out in offer.yields:
                    if out not in obtainable:
                        obtainable.add(out)
                        changed = True
        for item, materials in recipes.items():
            if item not in obtainable and all(m in obtainable for m in materials):
                obtainable.add(item)
                changed = True
        for flow in openables:
            if all(c in obtainable for c in flow.consumes):
                for out in flow.yields:
                    if out not in obtainable:
                        obtainable.add(out)
                        changed = True
    return obtainable


def _reachable(
    seeds: set[str],
    recipes: dict[str, dict[str, float]],
    craftable: set[str],
    openers: dict[str, list[OpenFlow]],
    offer_inputs: dict[str, list[dict[str, float]]] | None = None,
) -> set[str]:
    """Every item touched by covering ``seeds``.

    Follows recipes we may craft (down to their materials); for any item an
    openable yields, the openable's consumed items (its own token and key); and
    for any item an offer yields, that offer's consumed inputs (``offer_inputs``
    maps a yielded item to the ``consumes`` bundles of the offers making it) --
    so the Arnold Palmers an explore burns, and the crafts beneath them, are
    pulled into the LP too.
    """
    offer_inputs = offer_inputs or {}
    seen: set[str] = set()
    stack = list(seeds)
    while stack:
        item = stack.pop()
        if item in seen:
            continue
        seen.add(item)
        if item in craftable:
            stack.extend(recipes[item])
        for flow in openers.get(item, ()):
            stack.extend(flow.consumes)
        for consumes in offer_inputs.get(item, ()):
            stack.extend(consumes)
    return seen


@dataclass
class _Base:
    """The part of :func:`minimize`'s LP shared by every call in a sweep that
    holds ``offers``/``demand``/``recipes``/... fixed (see :func:`_build_base`).

    ``pj`` is always built (unlike the old per-call gating on ``favour_passive``)
    since one base must serve calls with either setting; :func:`_solve_configured`
    neutralises it per call instead. ``ephemeral`` names the constraints the most
    recent :func:`_solve_configured` call added, so the next call knows what to
    pop before adding its own.
    """

    problem: pulp.LpProblem
    usage: list[pulp.LpVariable]
    craft: dict[str, pulp.LpVariable]
    opens: list[pulp.LpVariable]
    makespan: pulp.LpVariable
    buy: dict[str, pulp.LpVariable]
    buy_cookie: pulp.LpVariable | None
    pj: dict[str, pulp.LpVariable]
    gold: pulp.LpAffineExpression
    spend_value: pulp.LpAffineExpression
    used: list[Offer]
    crafted: list[str]
    opened: list[OpenFlow]
    remainder: dict[str, float]
    pj_gain: dict[str, float]
    pj_price: float
    # True when no demanded item is obtainable at all -- the LP is never built
    # (mirrors ``minimize``'s old early return), so ``_solve_configured`` must
    # skip straight to the trivial remainder-only plan without touching ``problem``.
    remainder_only: bool = False
    ephemeral: list[str] = field(default_factory=list)
    # Ascension extension (see _build_base's docstring section): the spec this
    # base was built for (None outside ascension mode), each surviving candidate
    # item's step variables (item -> step name -> 0/1-bounded LpVariable, step
    # name one of "master"/"grandmaster"/"mega"), each surviving candidate's gross
    # mastery-supply expression, and the candidates dropped for having no channel/
    # recipe/open at all (never obtainable, so never selectable).
    ascension: AscensionSpec | None = None
    ascend_y: dict[str, dict[str, pulp.LpVariable]] = field(default_factory=dict)
    ascend_S: dict[str, pulp.LpAffineExpression] = field(default_factory=dict)
    unmasterable: list[str] = field(default_factory=list)
    # The integral step selection the last _mip_finish adopted on this base
    # (None until a MIP has run): a sweep re-solves one base at many gold
    # weights whose optimal selections mostly coincide, so the next call first
    # re-tries this selection with one pinned LP solve and keeps it when it
    # prices out within the MIP's own gap -- see _round_and_fix_ascension.
    # Only ever set by the MIP path, so every case the rounding loop handles
    # itself (all real-save-sized candidate sets) is byte-identical to before.
    ascend_mip: dict[tuple[str, str], int] | None = None


def _build_base(
    offers: list[Offer],
    demand: dict[str, float],
    recipes: dict[str, dict[str, float]] | None = None,
    crafting_bonus: float = 1.0,
    openables: list[OpenFlow] | None = None,
    inventory: dict[str, float] | None = None,
    cookie_sets: float = 0.0,
    prices: dict[str, float] | None = None,
    pj_gain: dict[str, float] | None = None,
    pj_price: float = 0.0,
    pj_limit: int | None = None,
    *,
    ascension: AscensionSpec | None = None,
) -> _Base:
    """Build the part of :func:`minimize`'s LP that is identical across every
    call in a sweep sharing these inputs -- variables, the balance/track
    constraints, ``gold``/``spend_value`` -- everything except the per-call
    objective and time cap, which :func:`_solve_configured` adds. See
    ``plans/MODEL_REUSE_PLAN.md`` for why this split exists.

    ``ascension`` (optional) turns on the ascension extension: per surviving
    candidate item (see :data:`ascension.AscensionSpec`), continuous ``y`` step
    variables in ``[0, 1]`` for each remaining mastery step (master/grandmaster/
    mega) it has, a gross mastery-supply expression, an AK constraint (step AK
    plus smeared chore AK over the makespan) and, when required, a mega-count
    constraint. See the "Ascension extension" note near :func:`_solve_configured`
    for the full picture (round-and-fix, the plan-time rule, the outcome readout).
    """
    recipes = recipes or {}
    openables = openables or []
    inventory = inventory or {}
    prices = prices or {}
    pj_gain = pj_gain or {}
    wanted = {item.lower(): (item, qty) for item, qty in demand.items() if qty > 0}

    # A buyable consumable can always be bought, so it is obtainable even where
    # nothing crafts or gathers it (a channel grounded in one is then still usable).
    # A *present* price (even 0 -- free) makes it buyable; an absent one does not
    # (see config.trading_prices for the presence-not-value convention).
    buyable = {item for item in BUYABLE if item in prices}
    # A held, non-demanded item an offer consumes (e.g. an Acorn Pie the exploring
    # channel burns) grounds that offer even when nothing crafts or gathers it. It
    # is bounded by the held stock in each item's balance, and excluding demanded
    # items keeps their demand-vs-inventory netting (and remainder) unchanged.
    offer_inputs_held = {
        item
        for offer in offers
        for item in offer.consumes
        if inventory.get(item, 0.0) > 0 and item not in wanted
    }
    obtainable = _obtainable(offers, recipes, openables, buyable, offer_inputs_held)
    craftable = {
        item
        for item, materials in recipes.items()
        if all(m in obtainable for m in materials)
    }
    # Which openables are usable (all their inputs obtainable), indexed by the
    # items they yield so reachability can walk from a wanted drop to its chest.
    openable_flows = [f for f in openables if all(c in obtainable for c in f.consumes)]
    openers: dict[str, list[OpenFlow]] = {}
    for flow in openable_flows:
        for out in flow.yields:
            openers.setdefault(out, []).append(flow)
    # Offers whose inputs are all obtainable can actually run; index their
    # consumed bundles by each item they yield so reachability walks from a
    # wanted drop into the consumables (Arnold Palmers, stamina, ...) it burns.
    usable = [o for o in offers if all(c in obtainable for c in o.consumes)]
    offer_inputs: dict[str, list[dict[str, float]]] = {}
    for offer in usable:
        if not offer.consumes:
            continue
        for out in offer.yields:
            offer_inputs.setdefault(out, []).append(offer.consumes)

    # Ascension candidates: an obtainable one joins the reachable seed set (its
    # channels/crafts/opens must enter the model too, same as a wanted item); one
    # that is not obtainable can never be selected, so it is dropped and recorded
    # on the base instead. Sorted for determinism (see the sorted(items) comment
    # in the per-item loop below).
    cand_by_item: dict[str, StepCandidate] = {}
    ascend_survivors: list[str] = []
    unmasterable: list[str] = []
    if ascension is not None:
        cand_by_item = {c.item.lower(): c for c in ascension.candidates}
        for item in sorted(cand_by_item):
            (ascend_survivors if item in obtainable else unmasterable).append(item)

    seeds = {k for k in wanted if k in obtainable} | set(ascend_survivors)
    items = _reachable(seeds, recipes, craftable, openers, offer_inputs)

    remainder = {
        display: round(qty)
        for key, (display, qty) in wanted.items()
        if key not in obtainable
    }
    if not items and ascension is None:
        return _Base(
            problem=pulp.LpProblem("sourcing", pulp.LpMinimize),
            usage=[], craft={}, opens=[], makespan=pulp.LpVariable("T", lowBound=0),
            buy={}, buy_cookie=None, pj={}, gold=0, spend_value=0,
            used=[], crafted=[], opened=[], remainder=remainder,
            pj_gain=pj_gain, pj_price=pj_price, remainder_only=True,
        )

    used = [
        o for o in usable
        if any(rate > 0 and i in items for i, rate in o.yields.items())
    ]
    crafted = sorted(items & craftable)
    # Openables worth modelling: those touching a reachable item, either yielding
    # one or consuming one (a chest reached only as the thing a key gates still
    # needs its open variable). Keep a stable order for naming.
    opened = [
        f
        for f in openable_flows
        if any(i in items for i in f.yields) or any(i in items for i in f.consumes)
    ]
    # For each material, which crafts consume it and how much.
    consumers: dict[str, list[tuple[str, float]]] = {}
    for item in crafted:
        for material, quantity in recipes[item].items():
            consumers.setdefault(material, []).append((item, quantity))

    problem = pulp.LpProblem("sourcing", pulp.LpMinimize)
    usage = [pulp.LpVariable(f"x{i}", lowBound=0) for i in range(len(used))]
    craft = {item: pulp.LpVariable(f"c{i}", lowBound=0) for i, item in enumerate(crafted)}
    opens = [pulp.LpVariable(f"o{i}", lowBound=0) for i in range(len(opened))]
    makespan = pulp.LpVariable("T", lowBound=0)

    # Ascension step variables: one continuous var in [0, 1] per remaining step
    # (master/grandmaster/mega) a surviving candidate has (see
    # :data:`ascension.StepCandidate`). Ordering links a step to the one below it
    # -- grandmaster needs master, mega needs grandmaster -- among whichever of
    # the two exist (the deltas ``ascension.candidates`` builds are contiguous, so
    # a higher step is never present without the one below it). Sorted-item order
    # for determinism, matching every other per-item loop in this function.
    ascend_y: dict[str, dict[str, pulp.LpVariable]] = {}
    for i, item in enumerate(ascend_survivors):
        c = cand_by_item[item]
        steps: dict[str, pulp.LpVariable] = {}
        if c.delta_master is not None:
            steps["master"] = pulp.LpVariable(f"ym{i}", lowBound=0, upBound=1)
        if c.delta_grandmaster is not None:
            steps["grandmaster"] = pulp.LpVariable(f"ygm{i}", lowBound=0, upBound=1)
        if c.delta_mega is not None:
            steps["mega"] = pulp.LpVariable(f"ymega{i}", lowBound=0, upBound=1)
        ascend_y[item] = steps
        if "master" in steps and "grandmaster" in steps:
            problem += steps["grandmaster"] <= steps["master"]
        if "grandmaster" in steps and "mega" in steps:
            problem += steps["mega"] <= steps["grandmaster"]

    # Cookie-boosted farming days used, capped below at held + bought sets. The
    # other consumable tallies (Arnold Palmers, Large Nets, ...) are read off the
    # solved draws in the plan built at the end, not needed as LP expressions here.
    cookies = pulp.lpSum(o.cookies * x for o, x in zip(used, usage) if o.cookies)

    # Buy variables: a buyable consumable can be bought (free, instant supply) for
    # gold instead of self-sourced (which costs makespan). Only reachable, priced
    # ones get a variable (a *present* price, even 0 -- free); cookie sets are
    # bought by lifting their cap below.
    buy = {
        item: pulp.LpVariable(f"b{i}", lowBound=0)
        for i, item in enumerate(sorted(items & BUYABLE))
        if item in prices
    }
    price_cookie = prices.get("cookie set", 0.0)
    buy_cookie = (
        pulp.LpVariable("bcookie", lowBound=0) if "cookie set" in prices else None
    )
    # Held cookie sets are free; beyond them a purchase lifts the cap at gold cost.
    problem += cookies <= cookie_sets + (buy_cookie if buy_cookie is not None else 0)
    gold = pulp.lpSum(prices[item] * var for item, var in buy.items())
    if buy_cookie is not None:
        gold = gold + price_cookie * buy_cookie

    # Pumpkin Juice: a bought mastery boost, one variable per reachable goal item.
    # Each juice on item i adds pj_gain[i] units toward its demand for pj_price gold;
    # capped below at fully covering the goal (a juice past it buys nothing). It is a
    # gold-for-time trade like buying, so it is off in the buy-nothing favour_passive
    # regime -- built unconditionally here since one base serves both regimes;
    # ``_solve_configured`` neutralises it per call with an ephemeral ``pj <= 0``
    # when ``favour_passive``. Its gold folds into the same ``gold`` the LP minimises.
    pj = {}
    if pj_gain and pj_price > 0:
        pj = {
            item: pulp.LpVariable(f"pj{i}", lowBound=0)
            for i, item in enumerate(sorted(items & wanted.keys() & pj_gain.keys()))
            if pj_gain[item] > 0
        }
    if pj_limit is not None and pj:
        problem += pulp.lpSum(pj.values()) <= pj_limit
    if pj:
        gold = gold + pj_price * pulp.lpSum(pj.values())

    # Market value of every consumable the plan spends -- Arnold Palmers, Large Nets,
    # Grape Juice, Apple Cider, Orange Juice (from the offers' per-unit tallies) and
    # cookie sets. The ``favour_passive`` mode minimises this to push sourcing off the
    # tool-hungry fishing/exploring/GJ channels and onto free passive production and
    # basic farming -- the slow, resource-banking end of the frontier (see
    # :func:`frontier`). Grape Juice carries no gold price, so a nominal weight keeps
    # it penalised alongside the priced tools rather than free.
    spend_value = pulp.lpSum(
        (
            o.ap * prices.get("arnold palmer", 0.0)
            + o.ln * prices.get("large net", 0.0)
            + o.gj * (prices.get("grape juice", 0.0) or 1e-4)
            + o.consumes.get("apple cider", 0.0) * prices.get("apple cider", 0.0)
            + o.consumes.get("orange juice", 0.0) * prices.get("orange juice", 0.0)
            + o.consumes.get("hickory omelette", 0.0)
            * prices.get("hickory omelette", 0.0)
        )
        * x
        for o, x in zip(used, usage)
    ) + cookies * prices.get("cookie set", 0.0)

    # Index each offer's yields/consumes by item once, so the per-item loop below
    # doesn't rescan every offer twice (via ``yield_of``) for every item.
    yield_terms: dict[str, list] = {}
    consume_terms: dict[str, list] = {}
    for o, x in zip(used, usage):
        for item, rate in o.yields.items():
            if rate > 0:
                yield_terms.setdefault(item, []).append(rate * x)
        for item, qty in o.consumes.items():
            if qty > 0:
                consume_terms.setdefault(item, []).append(qty * x)

    # Net production of each item covers its demand:
    # gathered + crafted + opened - consumed-by-crafts - consumed-by-opens.
    # Iterate in a fixed (sorted) order: ``items`` is a set, whose iteration
    # order varies with PYTHONHASHSEED across processes, and that is the order
    # constraints enter the LP. The two refinement solves below pin every
    # *reported* metric, but plans identical on all of them can still differ in
    # which equivalent draws fill the same time/activity (grow crop A vs crop B);
    # constraint order is what CBC breaks those ties by, so a fixed order keeps
    # the printed plan reproducible run to run.
    ascend_S: dict[str, pulp.LpAffineExpression] = {}
    for item in sorted(items):
        gathered = pulp.lpSum(yield_terms.get(item, []))
        # What offers use up as inputs (Arnold Palmers an explore burns, the
        # stamina and Apple Cider a cider explore spends) -- these must be
        # sourced like any other need.
        used_up = pulp.lpSum(consume_terms.get(item, []))
        made = crafting_bonus * craft[item] if item in craft else 0
        consumed = pulp.lpSum(qty * craft[j] for j, qty in consumers.get(item, []))
        unpacked = pulp.lpSum(
            f.yields.get(item, 0.0) * o
            for f, o in zip(opened, opens)
            if f.yields.get(item, 0.0)
        )
        emptied = pulp.lpSum(
            f.consumes.get(item, 0.0) * o
            for f, o in zip(opened, opens)
            if f.consumes.get(item, 0.0)
        )
        # Stock on hand is free supply for intermediates; a directly-demanded
        # item's balance is already net of what is held, so it gets no credit.
        held = inventory.get(item, 0.0) if item not in wanted else 0.0
        need = wanted[item][1] if item in wanted else 0
        bought = buy.get(item, 0)
        # A Pumpkin Juice on this goal item covers pj_gain[item] units of its demand.
        boosted = pj_gain[item] * pj[item] if item in pj else 0
        problem += (
            gathered + made - consumed - used_up + unpacked - emptied + held
            + bought + boosted
            >= need
        )
        # Cap the mastery boost at the goal: juice past full coverage buys no
        # progress (mastery cannot exceed the requirement), so it would only waste gold.
        if item in pj:
            problem += pj_gain[item] * pj[item] <= need
        # Buying can never earn an item's own mastery: a bought unit only covers a
        # non-goal use (a craft material, or a channel's tool), so cap purchases at
        # that derived demand. The goal ``need`` must then come from making or
        # gathering. Slack for non-goal items (they can be bought up to their use).
        if item in buy:
            problem += buy[item] <= consumed + used_up + emptied

        # Ascension mastery supply: GROSS production only (gathered + crafted +
        # unpacked, the same terms the balance above uses before ``consumed``/
        # ``used_up``/``emptied`` net anything off) -- deliberately not the net
        # balance, since mastery progress accrues even on production this plan
        # goes on to consume as a craft material (mastery-chaining).
        if item in ascend_y:
            S_i = ascension.mastery_bonus * (gathered + made + unpacked)
            ascend_S[item] = S_i
            steps = ascend_y[item]
            c = cand_by_item[item]
            terms = []
            if "master" in steps:
                terms.append(c.delta_master * steps["master"])
            if "grandmaster" in steps:
                terms.append(c.delta_grandmaster * steps["grandmaster"])
            if "mega" in steps:
                terms.append(c.delta_mega * steps["mega"])
            # A mega supply floor is a SEPARATE lower bound, not an addend:
            # the same produced supply counts toward the floor and the step
            # thresholds at once (an item at 700k with a 300k floor and
            # y_mega=1 needs 300k produced, not 600k), so the pair expresses
            # S >= max(step terms, floor). Folding the floor into one additive
            # constraint double-charged a mega's due stage its whole remaining
            # supply, and made the +10/+100 AK steps on a floored item cost
            # their full deltas on top of production the floor already forced.
            problem += S_i >= pulp.lpSum(terms)
            floor = ascension.mega_supply_floors.get(item, 0.0)
            if floor > 0:
                problem += S_i >= floor

    # Makespan: each track's summed time is at most T (tracks run concurrently).
    tracks: dict[str, list] = {}
    for offer, x in zip(used, usage):
        tracks.setdefault(offer.track, []).append(_minutes(offer.time_per_unit) * x)
    for terms in tracks.values():
        problem += pulp.lpSum(terms) <= makespan

    # Ascension AK constraint: step AK (10/master, 100/grandmaster) plus smeared
    # chore AK over the makespan (in days) must clear the stage's requirement.
    # Chore AK grows with the makespan itself, so a stage whose candidates are all
    # too dear can still be met by simply running longer -- the LP's call, traded
    # off against every other cost like any other makespan decision.
    if ascension is not None:
        ak_terms = []
        for steps in ascend_y.values():
            if "master" in steps:
                ak_terms.append(AK_PER_MASTER * steps["master"])
            if "grandmaster" in steps:
                ak_terms.append(AK_PER_GRANDMASTER * steps["grandmaster"])
        ak_expr = pulp.lpSum(ak_terms)
        if ascension.chore_rate_per_day:
            ak_expr = ak_expr + ascension.chore_rate_per_day * (makespan / _MINUTES_PER_DAY)
        problem += ak_expr >= ascension.ak_required

        # Mega requirement: at least this many candidates reach megamastery. Left
        # unconditional-on-existence: a requirement with no mega steps in the
        # model at all is infeasible, surfaced by _assert_optimal like any other
        # over-demanded LP (a clearer pre-check is milestone 4's job).
        if ascension.megas_required > 0:
            mega_terms = [steps["mega"] for steps in ascend_y.values() if "mega" in steps]
            problem += pulp.lpSum(mega_terms) >= ascension.megas_required

    return _Base(
        problem=problem, usage=usage, craft=craft, opens=opens, makespan=makespan,
        buy=buy, buy_cookie=buy_cookie, pj=pj, gold=gold, spend_value=spend_value,
        used=used, crafted=crafted, opened=opened, remainder=remainder,
        pj_gain=pj_gain, pj_price=pj_price,
        ascension=ascension, ascend_y=ascend_y, ascend_S=ascend_S,
        unmasterable=unmasterable,
    )


def _assert_optimal(problem: pulp.LpProblem, solver) -> object:
    """Raise when a solve did not reach an optimum.

    With every channel unbounded an obtainable demand is always satisfiable, but
    a finitely-bounded supply -- a limited-seed crop (see
    :meth:`crops.Crop.seed_item`) whose held seeds cap what it can ever grow --
    makes a too-large demand *infeasible* rather than unobtainable. Unchecked,
    the solver then leaves every variable at zero and the empty plan would be
    reported as a 0-minute success.

    Only an *uncapped* call's first solve is checked: those run first (the
    endpoints, the favour_passive probe, a bare ``minimize``), so a genuine
    capacity overflow raises there before any sweep begins. A capped sweep solve
    and the refinement stages are feasible by construction -- the caps sit at or
    above the endpoint times, and the pins at the previous stage's optimum -- so
    a non-optimal status there is solver numerics (observed: HiGHS calls a
    constraint with a ~1e-8-minute RHS infeasible), where keeping the
    still-loaded solution is the right fallback.

    A non-optimal status is itself only *suspected* infeasibility: HiGHS has been
    seen flagging a feasible model "Unbounded" (impossible here -- the objective
    is a sum of nonnegative terms -- so it is presolve mis-flagging the tiny
    ~1e-8 gold-weight coefficients). Before declaring the demand unsatisfiable,
    re-solve once with the bundled CBC as a second opinion; if it finds an
    optimum, that (equally valid) solution is left loaded and the CBC solver is
    returned so the caller's remaining refinement solves stay on the backend
    that can actually handle this model. Returns the solver the rest of the
    call should use.
    """
    if pulp.LpStatus[problem.status] == "Optimal":
        return solver
    fallback = pulp.PULP_CBC_CMD(msg=False)
    problem.solve(fallback)
    status = pulp.LpStatus[problem.status]
    if status != "Optimal":
        raise ValueError(
            f"no plan can satisfy the demand (solver status: {status}); "
            "a demanded item likely needs more than its limited sources "
            "(e.g. held Mega/Spring seeds) can ever supply"
        )
    return fallback


# Which lower steps a step needs pinned alongside it (mega needs grandmaster
# needs master, among whichever exist for that item) -- see
# :func:`_round_and_fix_ascension`.
_ASCEND_PREREQS: dict[str, tuple[str, ...]] = {
    "master": (),
    "grandmaster": ("master",),
    "mega": ("master", "grandmaster"),
}

# The sparse-mastery escape hatch's knobs (see _round_and_fix_ascension's
# docstring): how many one-step rounds the two-sided loop may spend before its
# remaining fractions go to one HiGHS MIP solve, and that MIP's termination
# tolerances -- relative/absolute optimality gap (the loop itself never had a
# bound at all; the absolute gap, in objective units of minutes + weighted
# gold, stops branch-and-bound proving exactness on the near-zero degenerate
# objectives the symmetric candidate flood produces) and a wall-clock cap
# (a limit hit still adopts a feasible incumbent). Real-save candidate sets
# settle within ~a hundred rounds at well under a millisecond per trial (the
# models are small and warm-started), so they never reach the MIP and keep
# the loop's exact historical plans; the flood pays the budget's trials once
# per base -- later calls skip straight to the MIP via the selection cache.
_ASCEND_ROUND_BUDGET = 128
_ASCEND_MIP_REL_GAP = 0.01
_ASCEND_MIP_ABS_GAP = 1.0
_ASCEND_MIP_TIME_LIMIT = 1.0


def _round_and_fix_ascension(
    base: _Base, solver: pulp.LpSolver, *, check: bool = True
) -> pulp.LpSolver:
    """Round the primary solve's fractional ascension step selections to 0/1.

    The relaxed LP is free to split a step across several items -- e.g. select
    1.5 masters' worth fractionally -- which is a valid relaxation optimum but
    not a real plan (you cannot half-master an item). This drives every ``y``
    in ``base.ascend_y`` to exactly 0 or 1 by **two-sided iterative
    rounding**: each round takes ONE step -- the largest fractional ``y``,
    the relaxation's most-wanted (ties prefer the lower step, then the item
    name) -- and tries it BOTH WAYS, re-solving under "pin up to 1 (with
    prerequisites)" and under "pin down to 0", keeping whichever solves to
    the better objective. The re-solve lets the LP re-decide every remaining
    fraction against that decision -- whether the AK the step carried is now
    cheaper from another item, from more chore-waiting (the makespan term),
    or not needed at all. Repeats until nothing is fractional, then pins
    every remaining step at its (now integral) value and solves once more,
    leaving the problem solved against a fully integral selection for the
    refinement tower that follows.

    Every cheaper scheme tried here first got a real case badly wrong,
    because nothing but the LP can price a step's *completion*: rounding by
    descending ``y`` (or AK-per-delta) turned a 10-AK gap into a whole
    grandmaster -- the relaxation loves fractional grandmaster slices, 100 AK
    per 90k of delta beating a master's 10 per 10k (see the corn regression
    test); a supply-per-needed-AK cover fixed that but pinned items the
    relaxation had merely *trickle-fed*, turning the buy-nothing endpoint
    into a 7-year plan on real data; up-only iterative rounding fixed the
    single-stage cases but had no way back from committing a trickle-fed
    step whose fractional supply was free while chore-waiting (a passive
    source sipped at 10% costs 10x the makespan completed), locking a 3-year
    multi-stage climb into 7+ years; and batch-committing every ``y >= 0.5``
    over-selects in the other direction (the moment the first commitments
    land, the LP would have re-sourced the rest cheaper -- observed turning a
    4-day fast endpoint into 41 days). Trying each side costs one extra solve
    per fractional step and settles all of it. Termination: every round pins
    one previously-fractional step, either way. The AK/mega constraints stay
    in the model throughout, so coverage is never a bookkeeping target: a
    pin-down that would break the requirement simply solves worse (or not at
    all, with a zero chore rate) and loses to the pin-up.

    One escape hatch, for the sparse-mastery flood: when the paste is missing
    most of the masterable catalogue (an empty or level-0 save), hundreds of
    interchangeable progress-0 candidates enter the model, shared draws
    co-produce whole channels of them at once, and every pin-down re-spreads
    the same fraction onto a fresh item -- one-variable-per-round rounding
    then walks the entire catalogue (~380 rounds, ~90s, for a one-level
    climb). A round budget detects the churn and hands the remaining
    selection to one HiGHS MIP solve over the same model (``_mip_finish``):
    the exact decision the trials approximate, priced by branch-and-bound
    instead of enumeration. Small fractional sets -- every real-save case --
    never trip the budget, so their behaviour is unchanged, as is every
    solve on a backend with no in-process model (CBC).

    A trial pin is a *bound flip* (``y`` fixed to 0 or 1), never a structural
    edit, so trials skip pulp's full model rebuild (which costs several times
    the actual solve on real candidate sets): when the primary solve ran on
    HiGHS, ``problem.solverModel`` is the still-loaded in-process highspy
    model with every variable's column index on ``var.index``, and trials
    flip column bounds there and re-solve warm-started from the previous
    basis. On any other backend (CBC fallback, or after a mid-loop rescue)
    trials flip the pulp variables' own bounds and re-solve through pulp.
    Each round keeps the winning trial's solution snapshot -- no third solve
    to restore the winner. The decisions are materialised at the end as
    ordinary ephemeral pin constraints (with the y bounds restored to
    ``[0, 1]``), so the refinement tower and the next call's ephemeral pop
    see exactly the constraints the bound-free implementation left.

    The final solve is passed through :func:`_assert_optimal` like every
    other uncapped first solve in the file (skipped when ``check`` is False
    -- a capped call, matching the primary solve's own exemption): on real,
    dense candidate sets (hundreds of items) hard 0/1 pins can land HiGHS on
    the same falsely-"Unbounded" degenerate vertex the module docstring
    documents elsewhere, which -- unlike a plain non-optimal status -- leaves
    every variable's ``.value()`` reading back nonsense (a negative makespan,
    every step un-selected) rather than simply stale; a trial that solves
    that way simply reads as failed. Returns the solver the caller should
    keep using (CBC, if a rescue happened) for the refinement tower.
    """
    assert base.ascension is not None
    # Below this, a step is dust, not a commitment: the relaxation will shave
    # micro-slivers of y (e.g. 1.6e-5 of a master, gathered concurrently, to
    # save a fraction of a minute of chore wait) that must never be forced up
    # -- the final integral pinning rounds them to 0 and the re-solve absorbs
    # the AK sliver through the chore term or another step.
    eps = 1e-3
    step_order = {"master": 0, "grandmaster": 1, "mega": 2}
    problem = base.problem

    all_steps = [
        (item, step, var)
        for item, steps in base.ascend_y.items()
        for step, var in steps.items()
    ]
    highs = (
        getattr(problem, "solverModel", None)
        if isinstance(solver, pulp.HiGHS)
        and all(hasattr(var, "index") for _, _, var in all_steps)
        else None
    )
    if highs is not None:
        from pulp.apis.highs_api import highspy

        _optimal_status = highspy.HighsModelStatus.kOptimal

    yvals: dict[tuple[str, str], float] = {
        (item, step): var.value() or 0.0 for item, step, var in all_steps
    }
    decided: dict[tuple[str, str], int] = {}

    # Round budget: one decision per round is fine when the fractional set is
    # small and stable (every real-save case: a full paste leaves few
    # candidates, and pins settle quickly -- measured <= ~0.1s per call), but
    # it degenerates when a sparse or empty mastery paste floods the model
    # with hundreds of interchangeable progress-0 catalogue candidates --
    # shared draws co-produce whole channels of them, so every pin-down just
    # re-spreads the same fraction onto a fresh item (observed ~380 rounds /
    # ~760 re-solves per call, ~90s for a ONE-level sparse climb, vs ~1.4s
    # for a 20-level real-save climb). Past the budget the remaining
    # selection is handed to one HiGHS MIP solve (_mip_finish below):
    # branch-and-bound prices completion exactly the way the two-sided
    # trials do -- the AK/mega constraints stay in the model, the objective
    # is unchanged -- but explores the symmetric fodder without enumerating
    # it one variable per round. Every small case stays on the loop's exact
    # historical behaviour; CBC (no in-process model) has no MIP escape and
    # keeps looping as before.
    rounds = 0
    mip_failed = False
    # The primary (relaxed) solve's objective: the integral problem's lower
    # bound, which prices both the MIP's gap and the cached-selection re-try.
    bound = pulp.value(problem.objective) or 0.0

    def set_bounds(item: str, step: str, low: int, up: int) -> None:
        var = base.ascend_y[item][step]
        if highs is not None:
            highs.changeColBounds(var.index, low, up)
        else:
            var.lowBound, var.upBound = low, up

    def trial(
        pins: dict[tuple[str, str], int],
    ) -> tuple[float, dict[tuple[str, str], float]] | None:
        """Solve with ``pins`` on top of ``decided``; ``(objective, y snapshot)``
        on an optimal solve, ``None`` when the solve is unusable. The trial
        pins are released again either way (committed decisions stay put)."""
        for (item, step), value in pins.items():
            set_bounds(item, step, value, value)
        if highs is not None:
            highs.run()
            ok = highs.getModelStatus() == _optimal_status
            if ok:
                col = highs.getSolution().col_value
                snapshot = {
                    (item, step): col[var.index] for item, step, var in all_steps
                }
                objective = highs.getObjectiveValue()
        else:
            problem.solve(solver)
            ok = pulp.LpStatus[problem.status] == "Optimal"
            if ok:
                snapshot = {
                    (item, step): var.value() or 0.0 for item, step, var in all_steps
                }
                objective = pulp.value(problem.objective)
        for key in pins:
            if key not in decided:
                set_bounds(*key, 0, 1)
        return (objective, snapshot) if ok else None

    def commit(
        pins: dict[tuple[str, str], int], snapshot: dict[tuple[str, str], float]
    ) -> None:
        decided.update(pins)
        yvals.update(snapshot)
        for (item, step), value in pins.items():
            set_bounds(item, step, value, value)

    def _mip_finish() -> bool:
        """Settle every remaining fraction with one HiGHS MIP solve.

        Flips the step columns to integer on the still-loaded in-process model
        (committed decisions stay pinned via their bound flips) and lets
        branch-and-bound pick the integral selection under the same
        constraints and objective the trials solve. Gap tolerances keep it
        from proving exact optimality on the near-degenerate objectives the
        symmetric flood produces (the two-sided loop never had a bound at
        all), and the time limit bounds the worst case; on either a clean
        finish or a limit hit with a feasible incumbent the snapshot is
        adopted, otherwise everything is restored and the loop carries on.
        """
        if highs is None:
            return False
        for _, _, var in all_steps:
            highs.changeColIntegrality(var.index, highspy.HighsVarType.kInteger)
        highs.setOptionValue("mip_rel_gap", _ASCEND_MIP_REL_GAP)
        highs.setOptionValue("mip_abs_gap", _ASCEND_MIP_ABS_GAP)
        highs.setOptionValue("time_limit", _ASCEND_MIP_TIME_LIMIT)
        # Stop the moment an incumbent prices out within the gap of the
        # RELAXATION bound, rather than waiting for branch-and-bound to lift
        # its own lower bound to prove it: on the flood's degenerate
        # objectives the incumbent is found in moments and nearly all the
        # solve time is proof (the rounding loop this replaces never had a
        # proof either). ``bound`` is a valid lower bound on every integral
        # solution, so a hit is genuinely within the gap of optimal.
        highs.setOptionValue(
            "objective_target",
            bound * (1 + _ASCEND_MIP_REL_GAP) + _ASCEND_MIP_ABS_GAP,
        )
        if base.ascend_mip is not None:
            # Hand the previous call's integral selection to branch-and-bound
            # as a partial starting solution (HiGHS completes the continuous
            # columns itself): adjacent sweep weights mostly share a
            # selection, so it usually prices out as an immediate incumbent
            # and the search only has to close the gap, not find a solution.
            # Committed round decisions override the cache -- their bounds
            # are already flipped, so a conflicting hint could never
            # complete. An infeasible hint is simply ignored by HiGHS.
            hint = [
                (
                    var.index,
                    float(decided.get((item, step), base.ascend_mip.get((item, step), 0))),
                )
                for item, step, var in all_steps
            ]
            highs.setSolution(
                len(hint), [i for i, _ in hint], [v for _, v in hint]
            )
        highs.run()
        highs.setOptionValue("time_limit", float("inf"))
        highs.setOptionValue("objective_target", float("-inf"))
        # kObjectiveTarget / kTimeLimit exits are usable through the primal-
        # status check below: any feasible incumbent is integral on the y's.
        usable = highs.getModelStatus() == _optimal_status or (
            highs.getInfo().primal_solution_status
            == int(highspy.kSolutionStatusFeasible)
        )
        if usable:
            col = highs.getSolution().col_value
            yvals.update(
                {(item, step): col[var.index] for item, step, var in all_steps}
            )
            base.ascend_mip = {
                key: 1 if y > 0.5 else 0 for key, y in yvals.items()
            }
        for item, step, var in all_steps:
            highs.changeColIntegrality(var.index, highspy.HighsVarType.kContinuous)
        return usable

    # Cached-selection re-try: a sweep re-solves this base at many gold
    # weights, and adjacent weights mostly keep the same integral selection
    # -- so when a previous call's MIP already settled one, try it first with
    # a single pinned LP re-solve and keep it whenever it prices out within
    # the MIP's own gap of this call's relaxation bound. Only the MIP path
    # ever populates the cache, so a base the loop always handled (every
    # real-save case) never takes this shortcut.
    if base.ascend_mip is not None:
        cached = trial(dict(base.ascend_mip))
        if cached is not None and cached[0] <= (
            bound * (1 + _ASCEND_MIP_REL_GAP) + _ASCEND_MIP_ABS_GAP
        ):
            yvals.update(cached[1])

    while True:
        fractional = sorted(
            (
                (item, step, y)
                for (item, step), y in yvals.items()
                if (item, step) not in decided and eps < y < 1 - eps
            ),
            key=lambda t: (-t[2], step_order[t[1]], t[0]),
        )
        if not fractional:
            break
        # A populated cache means a previous call on this base already tripped
        # the budget (the flood is established), so later calls skip straight
        # to the MIP instead of re-walking the loop's budget first.
        if (
            rounds >= _ASCEND_ROUND_BUDGET or base.ascend_mip is not None
        ) and not mip_failed:
            if _mip_finish():
                break
            mip_failed = True
        rounds += 1
        # Decide ONE step per round -- the largest fraction, the relaxation's
        # most-wanted -- and decide it BOTH WAYS: completing it (pin up, with
        # prerequisites) versus dropping it (pin down; the chore term or other
        # steps re-cover its AK), keeping whichever re-solves better. Rounding
        # up only was observed adopting a passive-trickle item the relaxation
        # merely sipped (its fractional supply is free while chore-waiting),
        # locking a 3-year climb into 7+ years with no way back -- only the
        # LP itself can price completion against more waiting. Batch pinning
        # over-selects the other way (the moment the first commitments land,
        # the LP would have re-sourced the rest cheaper).
        item, step, _ = fractional[0]
        up_pins = {
            (item, s): 1
            for s in (*_ASCEND_PREREQS[step], step)
            if s in base.ascend_y[item] and (item, s) not in decided
        }
        up = trial(up_pins)
        down = trial({(item, step): 0})
        if up is not None and (down is None or up[0] <= down[0]):
            commit(up_pins, up[1])
        elif down is not None:
            commit({(item, step): 0}, down[1])
        else:
            # Neither direction solved cleanly -- keep the down pin (as the
            # constraint implementation did) and surface it like any other
            # uncapped non-optimal solve rather than looping forever: re-solve
            # through pulp with every decision moved onto the pulp variables'
            # bounds and let _assert_optimal rescue via CBC or raise. After a
            # rescue the loop continues on the pulp trial path.
            decided[(item, step)] = 0
            if highs is not None:
                highs = None
                for (d_item, d_step), value in decided.items():
                    set_bounds(d_item, d_step, value, value)
            else:
                set_bounds(item, step, 0, 0)
            problem.solve(solver)
            solver = _assert_optimal(problem, solver)
            yvals = {
                (item, step): var.value() or 0.0 for item, step, var in all_steps
            }

    # Everything left is integral; materialise every decision (round commits
    # and integral leftovers alike) as ephemeral pin constraints, restoring
    # the y bounds the trials flipped, so the refinement tower cannot drift a
    # selection while trading gold/activity and the next call's ephemeral pop
    # finds every step var pinned, fractional or not.
    for item, steps in sorted(base.ascend_y.items()):
        for step, var in steps.items():
            var.lowBound, var.upBound = 0, 1
            value = decided.get((item, step))
            if value is None:
                value = 1 if yvals[(item, step)] > 0.5 else 0
            constraint = var == value
            # See the pj_off gotcha above: read the name back after assignment,
            # since an item like "fancy violin" gets sanitised on the way in.
            constraint.name = f"ascend_pin_{item}_{step}"
            problem.addConstraint(constraint)
            base.ephemeral.append(constraint.name)
    problem.solve(solver)
    # A capped solve is exempt from the optimality checks, exactly like the
    # primary solve above it (see _assert_optimal's docstring: HiGHS mis-flags
    # tiny-RHS pins under a cap, where keeping the loaded solution is right).
    return _assert_optimal(problem, solver) if check else solver


def _solve_configured(
    base: _Base,
    *,
    gold_weight: float = 0.0,
    time_cap: timedelta | None = None,
    favour_passive: bool = False,
) -> SourcingPlan:
    """Solve ``base`` under one call's mode -- the per-call counterpart of
    :func:`_build_base`. Reusable across many calls sharing a base: every
    constraint this call adds is named and recorded in ``base.ephemeral`` so
    the *next* call's first step can remove exactly them, leaving the shared
    balance/track constraints untouched. See ``plans/MODEL_REUSE_PLAN.md``.
    """
    if base.remainder_only:
        return SourcingPlan(remainder=base.remainder)

    problem = base.problem
    # NOTE: PuLP deprecates dict-style access to `.constraints` (removal by name
    # will need `prob.constraints()` -- a list -- plus `get_constraint_by_name`
    # once PuLP 4.0 lands; there is no replacement removal API yet). This still
    # works correctly today (verified against both `highs` and `cbc`), but pins
    # this whole reuse mechanism to that mapping staying functional. Watch for
    # it on the next PuLP upgrade -- `uv run pytest -q` surfaces the
    # DeprecationWarning (a plain script run does not, since Python suppresses
    # DeprecationWarning outside `__main__` by default).
    for name in base.ephemeral:
        problem.constraints.pop(name, None)
    base.ephemeral = []

    # ``pj`` is built unconditionally in the base (see _build_base); favour_passive
    # neutralises it here instead, exactly like the old variable-absence did.
    if favour_passive and base.pj:
        for item, var in base.pj.items():
            # LpConstraint's name setter sanitises the name (spaces/punctuation ->
            # "_"), so an item name like "fancy violin" is stored under a different
            # key than the raw f-string -- read the constraint's own ``.name`` back
            # so ``ephemeral`` records exactly what ``problem.constraints`` uses,
            # or the next call's pop misses it and re-adding collides.
            constraint = var <= 0
            constraint.name = f"pj_off_{item}"
            problem.addConstraint(constraint)
            base.ephemeral.append(constraint.name)

    if time_cap is not None:
        problem += (base.makespan <= _minutes(time_cap), "cap")
        base.ephemeral.append("cap")

    if favour_passive:
        # Slowest, most passive-leaning end: fewest tools spent (so passive and
        # farming carry the demand), makespan bounded by ``time_cap`` so it cannot
        # run away onto a centuries-long pet trickle.
        problem.setObjective(base.spend_value)
    elif time_cap is not None:
        problem.setObjective(base.gold)  # least gold under the time cap (epsilon-constraint)
    else:
        # A tiny gold_weight buys freely for the fastest plan; a huge one buys
        # nothing (gold 0) for the slowest, fully self-sourced plan.
        problem.setObjective(base.makespan + gold_weight * base.gold)
    solver = _solver()
    problem.solve(solver)
    if time_cap is None:
        solver = _assert_optimal(problem, solver)

    # Ascension round-and-fix: snap the primary solve's fractional step
    # selections to 0/1 and re-solve, before the refinement tower below runs on
    # top of that (now-integral) solution. See _round_and_fix_ascension.
    if base.ascension is not None:
        solver = _round_and_fix_ascension(base, solver, check=time_cap is None)

    # The first solve fixes the *cost* but not a unique plan: it has alternate
    # optima CBC picks between at random across processes. Refine it in a
    # lexicographic tower, each stage minimising one objective then locking it at
    # its optimum so the next cannot spoil it:
    #
    #   1. cost      -- the first solve above (weighted sum, epsilon cap, or, in
    #                   favour_passive mode, least spend_value under the time cap)
    #   2. gold      -- least gold at that point, the single priced axis
    #   3. activity  -- least crafts + gathering + buying, shedding pointless slack
    #
    # Stage 2 is the crux. In a weighted-sum endpoint the makespan term (~1e5
    # minutes) dwarfs the tiny-weighted gold (~1e-6 for the fastest plan), so within
    # CBC's tolerance it is free and the first solve parks the buys on an arbitrary
    # degenerate vertex -- buying Large Nets or Grape Juice that bought no time. Gold
    # is the Pareto cost, so a plan paying more for no time gain is strictly worse:
    # with the primary pinned we re-minimise gold alone (now the sole objective, at
    # a scale CBC resolves), pinning the genuinely cheapest plan. Stage 3 then sheds
    # surplus crafts / off-track farming without moving time or gold.
    #
    # Stage 1's pin differs by mode. Normally the makespan is pinned (the first solve
    # minimised it). In favour_passive mode it is spend_value that was minimised, so
    # *that* is pinned and the makespan is left free -- stage 3's activity term then
    # sheds any spare passive draw, settling the makespan at just what passive needs,
    # rather than the plan buying tools to finish early and undoing the passive lean.
    if (
        base.used or base.craft or base.opens or base.buy
        or base.buy_cookie is not None or base.pj
    ):
        if favour_passive:
            sv_star = pulp.value(base.spend_value) or 0.0
            problem += (base.spend_value <= sv_star * (1 + 1e-9) + 1e-9, "stage1_pin")
        else:
            t_star = base.makespan.value() or 0.0
            problem += (base.makespan <= t_star * (1 + 1e-9) + 1e-9, "stage1_pin")
        base.ephemeral.append("stage1_pin")

        # Stage 2 -- least gold at the pinned primary, then lock it. Skipped when
        # nothing is buyable (gold is a constant 0, an empty objective CBC rejects).
        # Also skipped in epsilon-constraint mode: stage 1 already minimised gold
        # under `makespan <= time_cap`, and stage 1's `t_star` pin above only
        # tightens that same bound, so a re-solve here is a provable no-op -- pin
        # gold from the stage-1 solution directly instead.
        if base.buy or base.buy_cookie is not None or base.pj:
            if time_cap is not None and not favour_passive:
                problem += (
                    base.gold <= (pulp.value(base.gold) or 0.0) * (1 + 1e-9) + 1e-9,
                    "stage2_pin",
                )
            else:
                problem.setObjective(base.gold)
                problem.solve(solver)
                problem += (
                    base.gold <= (pulp.value(base.gold) or 0.0) * (1 + 1e-9) + 1e-9,
                    "stage2_pin",
                )
            base.ephemeral.append("stage2_pin")

        # Stage 3 -- least activity, so no reported metric moves.
        problem.setObjective(
            pulp.lpSum(base.craft.values()) + pulp.lpSum(base.usage)
            + pulp.lpSum(base.opens) + pulp.lpSum(base.buy.values())
            + pulp.lpSum(base.pj.values())
            + (base.buy_cookie if base.buy_cookie is not None else 0)
        )
        problem.solve(solver)

    draws = [
        Draw(offer, units)
        for offer, x in zip(base.used, base.usage)
        if (units := x.value() or 0.0) > UNIT_EPSILON
    ]
    crafts = {
        item: count
        for item in base.crafted
        if (count := base.craft[item].value() or 0.0) > UNIT_EPSILON
    }
    opens_done = {
        flow.name: count
        for flow, o in zip(base.opened, base.opens)
        if (count := o.value() or 0.0) > UNIT_EPSILON
    }
    bought = {
        item: units
        for item, var in base.buy.items()
        if (units := var.value() or 0.0) > UNIT_EPSILON
    }
    if base.buy_cookie is not None and (bc := base.buy_cookie.value() or 0.0) > UNIT_EPSILON:
        bought["cookie set"] = bc
    pumpkin_juices = {
        item: units
        for item, var in base.pj.items()
        if (units := var.value() or 0.0) > UNIT_EPSILON
    }
    time, ap, ln = aggregate(draws)
    # Ascension: the chore term can make the optimal makespan exceed every
    # track's own time (the plan idles, waiting on daily chores, to close the
    # AK gap) -- draws alone would then under-report it, so take the makespan
    # variable's own value too. Never lowers it: the LP never sets makespan
    # below what the tracks need (see the track constraints above).
    if base.ascension is not None:
        time = max(time, timedelta(minutes=base.makespan.value() or 0.0))
    ascension_outcome = _ascension_outcome(base) if base.ascension is not None else None
    return SourcingPlan(
        draws=sorted(draws, key=lambda d: d.offer.name),
        crafts=crafts,
        opens=opens_done,
        time=time,
        arnold_palmers=ap,
        large_nets=ln,
        cookie_sets=sum(d.cookies for d in draws),
        grape_juices=sum(d.gj for d in draws),
        apple_ciders=sum(
            d.offer.consumes.get("apple cider", 0.0) * d.units for d in draws
        ),
        orange_juice=sum(
            d.offer.consumes.get("orange juice", 0.0) * d.units for d in draws
        ),
        hickory_omelettes=sum(
            d.offer.consumes.get("hickory omelette", 0.0) * d.units for d in draws
        ),
        bought=bought,
        gold=pulp.value(base.gold) or 0.0,
        pumpkin_juices=pumpkin_juices,
        pumpkin_gold=sum(pumpkin_juices.values()) * base.pj_price,
        pumpkin_credit={
            item: base.pj_gain[item] * units for item, units in pumpkin_juices.items()
        },
        remainder=base.remainder,
        ascension=ascension_outcome,
    )


def _ascension_outcome(base: _Base) -> AscensionOutcome:
    """Read the solved, round-and-fixed ``base`` into an :class:`AscensionOutcome`.

    Every step var is exactly 0 or 1 by this point (see
    :func:`_round_and_fix_ascension`), so a step counts as selected past 0.5.
    ``mastery_supply`` reports every surviving candidate's gross supply
    expression, including incidental production of one nothing pinned it to
    (a candidate produced only as a side effect of another item's crafting
    still earns real mastery progress -- deliberate, per the module docstring).

    A step also counts as reached when that same solved supply alone carries
    the item past its threshold, selected or not: ``ascension.advance``
    already moves the item's progress across it (that is how an incidental
    mega satisfies a later stage's mega requirement via ``mega_count``), and
    in the game every crossing pays its master/grandmaster AK whether the
    plan "meant" it or not. Reading only the pinned ``y``'s here made the two
    disagree -- rampant with a sparse mastery paste, where mass-production
    channels co-produce whole catalogues of progress-0 candidates: the AK
    ledger under-credited what the plan's own draws earn (so later stages
    over-grind), and the to-do list / frontier counts told the player to stop
    at grandmaster on items whose mega crossing the staged mega accounting
    was itself relying on. The supply check mirrors ``advance``'s rounding,
    against the item's cumulative remaining deltas up to the step.
    """
    spec = base.ascension
    assert spec is not None
    cand_by_item = {c.item: c for c in spec.candidates}

    def crossed(item: str, step: str) -> bool:
        cand = cand_by_item.get(item)
        expr = base.ascend_S.get(item)
        if cand is None or expr is None:
            return False
        needed = 0.0
        for s in ("master", "grandmaster", "mega"):
            delta = getattr(cand, f"delta_{s}")
            if delta is not None:
                needed += delta
            if s == step:
                break
        return needed > 0 and round(pulp.value(expr) or 0.0) >= needed

    def chosen(step: str) -> list[str]:
        return sorted(
            item
            for item, steps in base.ascend_y.items()
            if step in steps
            and ((steps[step].value() or 0.0) > 0.5 or crossed(item, step))
        )

    masters = chosen("master")
    grandmasters = chosen("grandmaster")
    megas = chosen("mega")
    ak_from_steps = AK_PER_MASTER * len(masters) + AK_PER_GRANDMASTER * len(grandmasters)
    makespan_minutes = base.makespan.value() or 0.0
    ak_from_chores = spec.chore_rate_per_day * makespan_minutes / _MINUTES_PER_DAY
    mastery_supply = {
        item: value
        for item, expr in base.ascend_S.items()
        if (value := pulp.value(expr) or 0.0) > UNIT_EPSILON
    }
    return AscensionOutcome(
        masters=masters,
        grandmasters=grandmasters,
        megas=megas,
        ak_from_steps=ak_from_steps,
        ak_from_chores=ak_from_chores,
        mastery_supply=mastery_supply,
    )


def _quantize_plan_time(
    plan: SourcingPlan,
    quantize: Callable[[str, float], timedelta | None] | None,
    demand: dict[str, float],
    recipes: dict[str, dict[str, float]],
    crafting_bonus: float,
    openables: list[OpenFlow],
    inventory: dict[str, float],
) -> None:
    """Lift ``plan.time`` in place to the next real passive deposit boundary --
    but only when passive production is the plan's own bottleneck.

    The LP's own makespan is the continuous ``residual / rate`` estimate for
    every passive item; the real wait for a whole deposit schedule to supply
    that residual (see :meth:`passive.PassiveProduction.quantized_wait`) is
    never sooner and often later, so this only ever *raises* ``plan.time``,
    never lowers it -- idempotent, safe to call more than once on the same
    plan.

    **Fill exemption**: when another track (farming, in practice) sets the
    makespan, the passive track is *fill* -- the LP opportunistically leans on
    free passive supply up to exactly that wait because, continuously, it costs
    nothing. Rounding such an item's last deposit up would then push the whole
    plan past the track that really drives it (the fastest plan's Orange fill
    completing a few hours after farming would round it from 6d 5h to 7d), when
    in reality the boundary sliver is trivially absorbed by the channels that
    already co-source those items (buy a few more Orange Juice, explore a few
    more ciders) -- an allocation the continuous LP cannot see it should make.
    So the lift applies only when the passive track's own time exceeds every
    other track's: then the plan genuinely waits on deposits and the boundary
    is real. The accepted residue: a fill item nothing else could source
    overshoots by at most one cycle, the same one-cycle error the continuous
    offer already carries (see :func:`sources.passive_offers`).

    A no-op when ``quantize`` is not supplied (quantisation is opt-in per
    caller) or the plan has no passive draw at all (nothing to quantize). The
    LP itself, ``Draw.units``/``Draw.produced`` and surplus accounting are
    untouched -- only the reported completion time moves. Parameter order
    mirrors :func:`passive_residuals`, whose result this walks. Cost: one
    :func:`plan_balance` (the same work :func:`passive_residuals` already does)
    plus a handful of small schedule walks -- negligible next to the LP solve
    itself.
    """
    if quantize is None or plan.passive_draw is None:
        return
    track_times = plan.track_times()
    passive_time = track_times.get(PASSIVE_TRACK, timedelta(0))
    others = max(
        (t for track, t in track_times.items() if track != PASSIVE_TRACK),
        default=timedelta(0),
    )
    # LP float noise between two views of the same pinned makespan is far below
    # a second; a real bottleneck gap is minutes to days.
    if passive_time <= others + timedelta(seconds=1):
        return  # passive is fill inside another track's wait (see docstring)
    residuals = passive_residuals(
        plan, demand, recipes, crafting_bonus, openables, inventory
    )
    plan.time = max(
        plan.time,
        max(
            (quantize(item, residual) or timedelta(0) for item, residual in residuals.items()),
            default=timedelta(0),
        ),
    )


def minimize(
    offers: list[Offer],
    demand: dict[str, float],
    recipes: dict[str, dict[str, float]] | None = None,
    crafting_bonus: float = 1.0,
    prices: dict[str, float] | None = None,
    gold_weight: float = 0.0,
    time_cap: timedelta | None = None,
    openables: list[OpenFlow] | None = None,
    inventory: dict[str, float] | None = None,
    cookie_sets: float = 0.0,
    favour_passive: bool = False,
    pj_gain: dict[str, float] | None = None,
    pj_price: float = 0.0,
    pj_limit: int | None = None,
    quantize: Callable[[str, float], timedelta | None] | None = None,
    ascension: AscensionSpec | None = None,
) -> SourcingPlan:
    """Solve one LP: cover ``demand``, trading makespan against gold spent.

    ``recipes`` maps a lower-cased craftable item to its ``{material: quantity}``
    (auto-supplied materials already stripped); one craft consumes those and
    yields ``crafting_bonus`` of the item. ``openables`` are Locksmith flows:
    opening one consumes its token (and key) and yields its average bundle, at no
    time cost -- a free multi-output flow the LP drives like crafts. Net production
    (gathered + crafted + opened + **bought** - consumed) must cover ``demand`` for
    every obtainable item, and each track's time sums to at most the makespan
    ``T``. A demanded item that is neither gatherable, craftable nor openable lands
    in the ``remainder``.

    ``inventory`` (lower-cased item -> quantity held) is free supply the plan need
    not produce: it is credited to every item's balance *except* those directly
    demanded, since the demand is already net of what is held (mastery progress or
    the acquired count). The credit is a constant, so surplus stock simply leaves
    the balance slack; it never makes an otherwise-unproducible item obtainable.

    **Buying.** Each buyable consumable (see :data:`BUYABLE`; cookie sets too, via
    ``cookie_sets`` + a purchase) gets a ``buy`` variable that adds free, instant
    supply at its ``prices`` gold cost -- the alternative to self-sourcing it, which
    costs makespan. Buying cannot earn an item's own **mastery**, so where an item
    is itself demanded (a tower/`-m`/`-i` goal) a ``buy <= derived`` floor keeps
    purchases to its non-goal uses (as a craft material or a channel's tool), and
    the goal must be crafted or gathered. ``gold`` is ``sum(buy * price)``.

    Three modes trace the frontier from different sides. By default the LP minimises
    ``T + gold_weight * gold`` (a weighted sum): a tiny ``gold_weight`` buys freely
    for the fastest plan, a huge one buys nothing for the slowest self-sourced one.
    With ``time_cap`` set it instead minimises ``gold`` subject to ``T <= time_cap``
    -- an epsilon-constraint solve, which unlike the weighted sum can land on the
    *interior* of a flat frontier segment. With ``favour_passive`` it minimises
    ``spend_value`` (the market value of every consumable spent) instead of gold, so
    the plan leans as hard as possible on free passive production and basic farming,
    spending the fewest tools and banking the over-production; ``time_cap`` then
    bounds how far it runs (see :func:`frontier`, the slow regime). ``prices`` are the
    per-unit gold from :func:`config.trading_prices`; a buyable is buyable iff its key
    is **present** (``item in prices``, even at ``0`` = free), so an item missing from
    ``prices`` -- or an empty/``None`` dict -- is not buyable and is fully self-sourced.

    **Pumpkin Juice.** ``pj_gain`` (lower-cased goal item -> units of demand one juice
    covers, ``0.1 * mastery_threshold / mastery_bonus``) turns on the mastery-boost
    lever: a ``pj`` variable per goal item adds free, instant supply toward that item's
    demand at ``pj_price`` gold each, capped at fully covering the demand (a juice past
    the goal buys nothing). ``pj_limit`` caps the total juices spent. Like buying it is
    a gold-for-time trade, so it is off in ``favour_passive`` mode (the buy-nothing
    slow regime); its gold folds into ``gold``, tracked apart on the plan.

    **Quantisation.** The LP itself always stays continuous -- ``quantize``
    (a ``(item, quantity) -> wait`` callable, see :func:`config.passive_quantizer`)
    only lifts the *reported* ``plan.time`` to the next real passive deposit
    boundary after solving (see :func:`_quantize_plan_time`), never the makespan
    constraint the LP optimised against. ``None`` (the default) leaves ``plan.time``
    at the continuous estimate, as before.

    **Ascension.** ``ascension`` (optional) turns on the ascension extension (see
    :data:`ascension.AscensionSpec`/:func:`_build_base`): the LP additionally
    chooses which candidate items to master/grandmaster/mega-master to clear the
    stage's AK requirement (steps plus chore AK over the makespan), attaching an
    :class:`ascension.AscensionOutcome` to the returned plan (``plan.ascension``).
    ``None`` (the default) leaves every code path identical to today.

    A single-shot call: builds one :func:`_build_base` and solves it once via
    :func:`_solve_configured`. :func:`frontier` instead builds a base per
    ``favour_passive`` regime and drives every sweep call through
    ``_solve_configured`` directly, since almost everything below is identical
    call to call within one sweep.
    """
    base = _build_base(
        offers, demand, recipes, crafting_bonus, openables, inventory,
        cookie_sets, prices, pj_gain, pj_price, pj_limit,
        ascension=ascension,
    )
    plan = _solve_configured(
        base, gold_weight=gold_weight, time_cap=time_cap, favour_passive=favour_passive,
    )
    _quantize_plan_time(
        plan, quantize, demand, recipes or {}, crafting_bonus, openables or [], inventory or {},
    )
    return plan


def _consumable_spend(plan: SourcingPlan) -> dict[str, float]:
    """Each tradable consumable's spent tally, keyed by its lower-cased item name.

    The single list the gold maths iterate over, so :func:`plan_gold` and
    :func:`plan_gold_breakdown` price exactly the same set of consumables.
    """
    return {
        "arnold palmer": plan.arnold_palmers,
        "large net": plan.large_nets,
        "apple cider": plan.apple_ciders,
        "orange juice": plan.orange_juice,
        "grape juice": plan.grape_juices,
        "cookie set": plan.cookie_sets,
        "hickory omelette": plan.hickory_omelettes,
    }


def plan_gold(plan: SourcingPlan, prices: dict[str, float]) -> float:
    """Total gold ``plan`` spends on tradable consumables.

    ``prices`` is the per-unit gold map from :func:`config.trading_prices`; the
    result is the dot product of that map with the plan's consumable tally, so
    plans can be ranked by real gold cost. Grape Juice, priced from its grape
    content (or 0 if no grape price is configured), is included like the rest.
    Pumpkin Juice has no self-sourced form, so its whole cost (``plan.pumpkin_gold``)
    is added on -- it only ever counts as bought, never made.
    """
    return sum(
        spent * prices.get(item, 0.0)
        for item, spent in _consumable_spend(plan).items()
    ) + plan.pumpkin_gold


def plan_gold_breakdown(
    plan: SourcingPlan,
    prices: dict[str, float],
) -> tuple[float, float]:
    """Split a plan's consumable value into ``(to_buy, passive)`` gold.

    The LP decides exactly which consumables to buy, so the split is exact:
    *to-buy* is ``plan.gold`` -- the gold actually spent buying -- and *passive* is
    the market value of everything it self-sourced instead (made, gathered, or held
    at no gold), i.e. :func:`plan_gold` less what was bought. The two sum to
    :func:`plan_gold`, the total market value of every consumable the plan spends.
    """
    to_buy = plan.gold
    return to_buy, plan_gold(plan, prices) - to_buy


def plan_leftovers(
    plan: SourcingPlan,
    demand: dict[str, float],
    recipes: dict[str, dict[str, float]],
    crafting_bonus: float,
    openables: list[OpenFlow],
    inventory: dict[str, float],
) -> dict[str, float]:
    """Each item's leftover -- what a plan produces past everything it consumes.

    Passive production runs to the makespan set by the slowest item, so faster
    resources overshoot: a plan waiting on Oak keeps banking Straw, Stone and Wood
    long past what its crafts need. The leftover of an item is its whole supply
    (the passive draw, the other channels, crafting, opening and held stock) less
    everything that consumes it (demand, craft materials, chest tokens/keys, and
    the Arnold Palmers / Large Nets / stamina the draws burn): ``passive.produced
    + elsewhere - need - draw-consumes`` in the terms of :func:`plan_balance`
    (whose ``elsewhere`` excludes the passive draw and whose ``need`` excludes
    offer inputs, both added back here). Only strictly-positive leftovers are
    returned. Feed them to :func:`surplus_sale_value` to see what they are worth.
    """
    balance = plan_balance(
        plan, demand, recipes, crafting_bonus, openables, inventory
    )
    passive = plan.passive_draw
    produced = passive.produced if passive is not None else {}
    consumed = draw_consumed(plan)
    leftovers: dict[str, float] = {}
    keys = set(balance.need) | set(balance.elsewhere) | set(produced)
    for key in keys:
        supply = balance.elsewhere.get(key, 0.0) + produced.get(key, 0.0)
        used = balance.need.get(key, 0.0) + consumed.get(key, 0.0)
        if (left := supply - used) > UNIT_EPSILON:
            leftovers[key] = left
    return leftovers


def draw_consumed(plan: SourcingPlan) -> dict[str, float]:
    """Each item a plan's draws consume (chest keys, Large Nets, stamina, ...)."""
    consumed: dict[str, float] = {}
    for draw in plan.draws:
        for item, quantity in draw.offer.consumes.items():
            consumed[item] = consumed.get(item, 0.0) + quantity * draw.units
    return consumed


def leftover_inventory_share(
    plan: SourcingPlan,
    demand: dict[str, float],
    recipes: dict[str, dict[str, float]],
    crafting_bonus: float,
    openables: list[OpenFlow],
    inventory: dict[str, float],
) -> dict[str, float]:
    """How much of each :func:`plan_leftovers` leftover was already held, not made.

    Held stock is spent before anything the plan itself produces (the "From
    inventory" note in :func:`report.print_plan` makes the same assumption for
    consumables), so an item's leftover only counts as inventory-sourced past
    what its own consumption used: ``max(0, held - (need + draw-consumes))``.
    Directly-demanded items are skipped -- their held stock already nets out of
    ``demand`` (see :func:`plan_balance`), so none of it reaches ``elsewhere`` or
    a leftover here. The rest of a leftover (see :func:`plan_leftovers`) is
    whatever the plan accumulated: crafted, opened, drawn or passively produced.
    """
    balance = plan_balance(
        plan, demand, recipes, crafting_bonus, openables, inventory
    )
    demanded = {item.lower() for item in demand}
    consumed = draw_consumed(plan)
    shares: dict[str, float] = {}
    for item, held in inventory.items():
        if item in demanded:
            continue
        used = balance.need.get(item, 0.0) + consumed.get(item, 0.0)
        if (share := held - used) > UNIT_EPSILON:
            shares[item] = share
    return shares


def accumulated_leftovers(
    plan: SourcingPlan,
    demand: dict[str, float],
    recipes: dict[str, dict[str, float]],
    crafting_bonus: float,
    openables: list[OpenFlow],
    inventory: dict[str, float],
) -> dict[str, float]:
    """The leftovers a plan itself accumulates, net of stock already held.

    :func:`plan_leftovers` with each item's already-held share
    (:func:`leftover_inventory_share`) subtracted, so pre-existing inventory sitting
    unused past the goal is not mistaken for surplus the plan banks. What remains is
    only what executing the plan produced beyond all it consumes (passive, crafts,
    opens, gathering). This is what the surplus value (and its gold credit against
    the frontier's net cost) should be measured on -- see
    :func:`plan_surplus_gold`.
    """
    leftovers = plan_leftovers(
        plan, demand, recipes, crafting_bonus, openables, inventory
    )
    shares = leftover_inventory_share(
        plan, demand, recipes, crafting_bonus, openables, inventory
    )
    accumulated: dict[str, float] = {}
    for item, left in leftovers.items():
        if (net := left - shares.get(item, 0.0)) > UNIT_EPSILON:
            accumulated[item] = net
    return accumulated


def surplus_sale_value(
    leftovers: dict[str, float],
    recipes: dict[str, dict[str, float]],
    crafting_bonus: float,
    prices: dict[str, float],
) -> list[tuple[str, float, float]]:
    """The most gold a plan's leftovers are worth, as sellable goods to craft.

    Assumes the player turns the leftover materials (see :func:`plan_leftovers`)
    into the **sellable** priced consumables -- Arnold Palmers, Large Nets, Apple
    Cider, Orange Juice: the priced items that have a recipe, so Grape Juice (not
    sold, only a production-cost proxy) and cookie sets (baked from held cookies,
    no recipe) are excluded. It is an allocation: shared materials
    (straw/wood/stone/antlers all feed Large Nets, apples feed cider, ...) go to
    whichever good yields the most gold, so this is one small LP maximising
    ``sum(price * sold)`` over crafts bounded by the leftovers. Returns
    ``(item, count, gold)`` per sellable good with a whole unit or more, gold
    highest first; the total nominal value is their gold summed.
    """
    sellable = {
        item
        for item, price in prices.items()
        if price > 0 and item in recipes and item != "grape juice"
    }
    if not sellable:
        return []
    # Only the goods and the materials beneath them matter; walk the recipes down.
    items: set[str] = set()
    stack = list(sellable)
    while stack:
        item = stack.pop()
        if item in items:
            continue
        items.add(item)
        stack.extend(recipes.get(item, {}))
    craftable = sorted(item for item in items if item in recipes)
    consumers: dict[str, list[tuple[str, float]]] = {}
    for item in craftable:
        for material, quantity in recipes[item].items():
            consumers.setdefault(material, []).append((item, quantity))

    problem = pulp.LpProblem("surplus_sale", pulp.LpMaximize)
    craft = {item: pulp.LpVariable(f"s{i}", lowBound=0) for i, item in enumerate(craftable)}
    # Net leftover of each item: what is on hand plus crafted, less crafted-away.
    net = {
        item: (
            leftovers.get(item, 0.0)
            + (crafting_bonus * craft[item] if item in craft else 0)
            - pulp.lpSum(quantity * craft[j] for j, quantity in consumers.get(item, []))
        )
        for item in items
    }
    for expr in net.values():
        problem += expr >= 0  # cannot consume more of a material than is available
    problem += pulp.lpSum(prices[item] * net[item] for item in sellable)
    problem.solve(_solver())
    if pulp.LpStatus[problem.status] != "Optimal":
        return []
    rows = [
        (item, sold, sold * prices[item])
        for item in sellable
        if (sold := pulp.value(net[item]) or 0.0) >= 1
    ]
    rows.sort(key=lambda row: row[2], reverse=True)
    return rows


def self_sourced_endpoint(
    offers: list[Offer],
    demand: dict[str, float],
    recipes: dict[str, dict[str, float]] | None = None,
    crafting_bonus: float = 1.0,
    openables: list[OpenFlow] | None = None,
    inventory: dict[str, float] | None = None,
    cookie_sets: float = 0.0,
    prices: dict[str, float] | None = None,
    quantize: Callable[[str, float], timedelta | None] | None = None,
) -> SourcingPlan:
    """The cheap endpoint: the fastest plan that buys nothing (fully self-sourced).

    Solved with the buy-nothing weight (``_FREE_PRICE``). It is both the fast
    regime's slow end and the **stable reference** for each passive item's *full*
    requirement (see :func:`passive_residuals` and :func:`main.run`): it self-sources
    with a moderate strategy, unlike the passive-leaning extension past it -- which
    can source cider stamina from Apples rather than Orange Juice and so understate
    the Orange requirement -- and unlike the fast plans, which over-buy net materials
    like Antler. Recomputed rather than read off the thinned frontier, where it may
    not survive as an interior point. ``quantize`` forwards to :func:`minimize`, so
    this endpoint's own reported time is lifted to a real deposit boundary too.
    """
    return minimize(
        offers, demand, recipes, crafting_bonus, prices=prices,
        gold_weight=_FREE_PRICE, openables=openables, inventory=inventory,
        cookie_sets=cookie_sets, quantize=quantize,
    )


def fastest_endpoint(
    offers: list[Offer],
    demand: dict[str, float],
    recipes: dict[str, dict[str, float]] | None = None,
    crafting_bonus: float = 1.0,
    openables: list[OpenFlow] | None = None,
    inventory: dict[str, float] | None = None,
    cookie_sets: float = 0.0,
    prices: dict[str, float] | None = None,
    pj_gain: dict[str, float] | None = None,
    pj_price: float = 0.0,
    pj_limit: int | None = None,
    quantize: Callable[[str, float], timedelta | None] | None = None,
) -> SourcingPlan:
    """The fast endpoint: buys as freely as needed to finish soonest.

    Solved with the buy-freely weight (``_FAST_PRICE``), factored out so a
    caller that only needs that one plan (see :func:`main.run`'s calendar
    fixed point) can seed a solve without building the rest of the frontier.

    ``frontier`` no longer calls this directly -- it builds its own shared
    base and drives the fast endpoint through ``_solve_configured(base_active,
    gold_weight=_FAST_PRICE)`` so that solve can share the base with the rest
    of its sweep (see ``plans/MODEL_REUSE_PLAN.md``). The two are equivalent
    (same ``_FAST_PRICE``, same underlying inputs, both bottoming out in
    ``_solve_configured``), but that equivalence is no longer enforced by
    shared code -- if the fast-endpoint call in either place ever changes,
    update the other to match. This one matters for ``quantize``, too:
    ``main.solve_frontier``'s fast path seeds the seasonal fixed-point horizon
    from this call's ``.time``, and ``_makespans_close``'s tolerance is only
    ``max(1h, 0.1%)`` -- an unquantized seed against a quantized frontier
    horizon would force an extra full-frontier iteration to reconverge, so both
    must be quantized alike.
    """
    return minimize(
        offers, demand, recipes, crafting_bonus, prices=prices,
        gold_weight=_FAST_PRICE, openables=openables, inventory=inventory,
        cookie_sets=cookie_sets, pj_gain=pj_gain, pj_price=pj_price, pj_limit=pj_limit,
        quantize=quantize,
    )


def plan_surplus_gold(
    plan: SourcingPlan,
    demand: dict[str, float],
    recipes: dict[str, dict[str, float]],
    crafting_bonus: float,
    openables: list[OpenFlow],
    inventory: dict[str, float],
    prices: dict[str, float],
) -> float:
    """Gold the surplus ``plan`` *accumulates* past its goal is worth.

    The sale value (:func:`surplus_sale_value` over :func:`plan_leftovers`) of what
    the plan produces past all it consumes, as sellable goods -- but **only the
    portion the plan itself accumulates, not stock already held**. A player's
    pre-existing inventory sitting unused is not something executing the plan banks;
    crediting its market value against gold only confused the frontier (every plan
    looked hugely profitable by the sale value of an untouched stockpile). So the
    held share of each good (:func:`leftover_inventory_share`) is netted off, exactly
    the "accumulated" half of the report's Surplus section. This is the credit
    :attr:`SourcingPlan.cost` subtracts from bought gold, so :func:`frontier` sets it
    on every plan and the gold axis can dip below zero for one that banks more than
    it buys. Zero when nothing is priced, nothing is left over, or every leftover was
    already held.
    """
    leftovers = accumulated_leftovers(
        plan, demand, recipes, crafting_bonus, openables, inventory
    )
    rows = surplus_sale_value(leftovers, recipes, crafting_bonus, prices)
    return sum(gold for _, _, gold in rows)


def _dominates(a: SourcingPlan, b: SourcingPlan) -> bool:
    """Whether ``a`` is at least as good as ``b`` on every objective, better on one
    -- so ``b`` is not worth keeping."""
    return all(x <= y for x, y in zip(a.cost, b.cost)) and a.cost != b.cost


def _non_dominated(plans: list[SourcingPlan]) -> list[SourcingPlan]:
    """The Pareto-efficient plans, de-duplicated, sorted fastest first."""
    unique: dict[tuple, SourcingPlan] = {}
    for plan in plans:
        key = (round(plan.time.total_seconds()), round(plan.gold - plan.surplus_gold))
        unique.setdefault(key, plan)
    kept = [
        p for p in unique.values()
        if not any(_dominates(q, p) for q in unique.values() if q is not p)
    ]
    return sorted(kept, key=lambda p: p.cost)


def _thin(
    plans: list[SourcingPlan], count: int, anchors: tuple[float, ...] = ()
) -> list[SourcingPlan]:
    """Keep ``count`` plans spread across the frontier, including both ends.

    ``anchors`` are makespan seconds worth landing on -- the passive-completion
    breakpoints where an item stops needing consumables (see
    :func:`_passive_completion_caps`). The plan nearest each anchor is kept before
    the remaining slots are filled by an even spread, so a meaningful intermediate
    (say the plan clamped to when Steel Wire finishes) is not thinned away.
    """
    if len(plans) <= count:
        return plans
    if count <= 1:
        return [plans[0]]  # fastest
    keep = {0, len(plans) - 1}  # always both ends
    for target in sorted(anchors):
        if len(keep) >= count:
            break
        keep.add(min(range(len(plans)), key=lambda i: abs(
            plans[i].time.total_seconds() - target)))
    # Fill any remaining slots with an even spread across the whole frontier.
    step = (len(plans) - 1) / (count - 1)
    for i in range(count):
        if len(keep) >= count:
            break
        keep.add(round(i * step))
    # Collisions with already-kept indices can leave us short; top up in order.
    for i in range(len(plans)):
        if len(keep) >= count:
            break
        keep.add(i)
    return [plans[i] for i in sorted(keep)]


class PlanBalance(NamedTuple):
    """A plan's per-item production accounting (all keys lower-cased).

    ``need`` is everything the plan must produce: the demand, plus what its crafts
    consume as materials and what its opens consume (chest tokens and keys).
    ``elsewhere`` is everything the non-passive channels supply for free: crafting
    output, open yields, held stock (except directly-demanded items, whose demand
    is already net of stock), and every non-passive draw. ``crafted`` is the
    crafting output alone (``crafting_bonus`` per craft). An item's residual left
    for passive production is ``need - elsewhere``.
    """

    need: dict[str, float]
    elsewhere: dict[str, float]
    crafted: dict[str, float]


def plan_balance(
    plan: SourcingPlan,
    demand: dict[str, float],
    recipes: dict[str, dict[str, float]],
    crafting_bonus: float,
    openables: list[OpenFlow],
    inventory: dict[str, float],
) -> PlanBalance:
    """The plan's ``need``/``elsewhere``/``crafted`` accounting (see
    :class:`PlanBalance`).

    This is the single source of that accounting, shared by
    :func:`passive_residuals` and :func:`main.print_plan` (which both work in the
    residual ``need - elsewhere``) so the two cannot drift. ``recipes`` are the
    lower-cased, IGNORED-stripped recipe flows (see
    :func:`crafting.recipe_flows`).
    """
    demanded = {item.lower() for item in demand}
    need = {item.lower(): float(qty) for item, qty in demand.items() if qty > 0}
    # A Pumpkin Juice covers part of a goal's mastery directly, so that slice is not a
    # requirement for any channel to make -- net it out of the item's need.
    for item, credit in plan.pumpkin_credit.items():
        if item in need:
            need[item] = max(0.0, need[item] - credit)
    for item, count in plan.crafts.items():
        for material, quantity in recipes.get(item, {}).items():
            need[material] = need.get(material, 0.0) + quantity * count
    flows = {flow.name.lower(): flow for flow in openables}
    for name, count in plan.opens.items():
        flow = flows.get(name.lower())
        if flow is not None:
            for item, quantity in flow.consumes.items():
                need[item] = need.get(item, 0.0) + quantity * count
    crafted = {item: crafting_bonus * count for item, count in plan.crafts.items()}
    elsewhere = dict(crafted)
    for name, count in plan.opens.items():
        flow = flows.get(name.lower())
        if flow is not None:
            for item, quantity in flow.yields.items():
                elsewhere[item] = elsewhere.get(item, 0.0) + quantity * count
    for item, quantity in inventory.items():
        if item not in demanded:  # demand is already net of held stock
            elsewhere[item] = elsewhere.get(item, 0.0) + quantity
    for draw in plan.draws:
        if draw.offer.track == PASSIVE_TRACK:
            continue
        for item, quantity in draw.produced.items():
            elsewhere[item] = elsewhere.get(item, 0.0) + quantity
    return PlanBalance(need=need, elsewhere=elsewhere, crafted=crafted)


def passive_residuals(
    plan: SourcingPlan,
    demand: dict[str, float],
    recipes: dict[str, dict[str, float]],
    crafting_bonus: float,
    openables: list[OpenFlow],
    inventory: dict[str, float],
) -> dict[str, float]:
    """How much of each passively-made item ``plan`` leaves to passive production.

    An item's residual is its net requirement (demand plus what crafting and
    opening consume) less what crafting, opening, stock and the other tracks
    already supply -- i.e. ``need - elsewhere`` from :func:`plan_balance`, taken
    over the passive offer's yields. Computed on the cheapest plan -- which crafts
    the most and buys the least -- it is that item's *full* requirement, the most
    passive ever has to make; a plan whose residual for an item reaches this has
    that item at its full completion (see :func:`main.print_critical_path`).
    """
    passive = plan.passive_draw
    if passive is None:
        return {}
    balance = plan_balance(
        plan, demand, recipes, crafting_bonus, openables, inventory
    )
    residuals = {}
    for item, rate in passive.offer.yields.items():
        if rate <= 0:
            continue
        residual = balance.need.get(item, 0.0) - balance.elsewhere.get(item, 0.0)
        if residual > 1e-6:
            residuals[item] = residual
    return residuals


def _passive_completion_caps(
    plan: SourcingPlan,
    demand: dict[str, float],
    recipes: dict[str, dict[str, float]],
    crafting_bonus: float,
    openables: list[OpenFlow],
    inventory: dict[str, float],
    exclude: set[str] = frozenset(),
    quantize: Callable[[str, float], timedelta | None] | None = None,
) -> list[float]:
    """Makespan seconds at which each passively-made item finishes, in ``plan``.

    Each item finishes after ``residual / rate`` (see :func:`passive_residuals`) --
    the natural plateaus of the frontier: cap the makespan at one and every slower
    item must be sped up with consumables while that item, and all faster ones,
    still come for free. With ``quantize`` supplied, an item's completion is
    instead its real deposit-schedule wait (``quantize(item, residual)``) when
    that resolves to an answer, falling back to ``residual / rate`` only when it
    does not (e.g. an item with no producers at all, which cannot happen for a
    passive-yielded item, but keeps this total). Items in ``exclude`` -- those a
    pet mostly supplies, a slow last-resort trickle rather than a real gathering
    plateau -- are skipped.
    """
    passive = plan.passive_draw
    if passive is None:
        return []
    residuals = passive_residuals(
        plan, demand, recipes, crafting_bonus, openables, inventory
    )
    caps = []
    for item, residual in residuals.items():
        if item in exclude:
            continue
        rate = passive.offer.yields.get(item, 0.0)
        if rate <= 0:
            continue
        wait = quantize(item, residual) if quantize is not None else None
        caps.append(
            wait.total_seconds() if wait is not None else residual / rate * 60.0
        )
    return caps


def _time_caps(t_fast: float, t_slow: float, count: int) -> list[float]:
    """``count`` makespan caps (seconds) geometric between the two endpoints.

    Geometric spacing samples the fast end (sub-year plans) and the slow end
    (multi-decade plans) alike, so no part of the frontier is starved. Endpoints
    are excluded (solved separately); a degenerate range yields no caps.
    """
    if count < 1 or t_slow <= t_fast:
        return []
    lo = math.log(max(t_fast, t_slow * 1e-9))
    hi = math.log(t_slow)
    return [math.exp(lo + (hi - lo) * (i + 1) / (count + 1)) for i in range(count)]


def frontier(
    offers: list[Offer],
    demand: dict[str, float],
    recipes: dict[str, dict[str, float]] | None = None,
    crafting_bonus: float = 1.0,
    count: int = 5,
    openables: list[OpenFlow] | None = None,
    inventory: dict[str, float] | None = None,
    cookie_sets: float = 0.0,
    passive_pet_items: set[str] = frozenset(),
    prices: dict[str, float] | None = None,
    pj_gain: dict[str, float] | None = None,
    pj_price: float = 0.0,
    pj_limit: int | None = None,
    quantize: Callable[[str, float], timedelta | None] | None = None,
) -> list[SourcingPlan]:
    """The Pareto frontier of sourcing plans over ``(time, net gold)``, fastest first.

    The whole tradeoff is makespan against net gold (bought less banked surplus, see
    :attr:`SourcingPlan.cost`). The frontier spans two regimes that meet at the
    fastest fully self-sourced plan (``cheap`` -- buys nothing, finishes soonest):

    * **Fast regime** ``[t_fast, t_cheap]`` -- trade gold for time. From ``fast`` (buy
      freely, quickest) an **epsilon-constraint sweep** caps the makespan at each of
      many times and minimises gold under the cap. This is what a plain price sweep
      cannot do -- where the frontier is flat every weighted-sum gold price collapses
      onto an endpoint, hiding the moderate-spend plans between (buying some Arnold
      Palmers to explore a slow-farmed crop rather than all or none).
    * **Slow regime** ``[t_cheap, t_bound]`` -- trade time for banked surplus. Past
      ``cheap``, a ``favour_passive`` sweep sources ever more of the demand from free
      passive production and basic farming (fewest tools spent), running longer and
      banking the over-production, so the net gold falls below zero. Bounded at
      ``t_bound``, the slowest **non-pet** passive completion, so it cannot chase a
      centuries-long pet trickle; ``maxpassive`` is that endpoint (the "all passively
      sourceable resources sourced passively" plan). Only built when ``prices`` make
      the banked surplus worth gold.

    The non-dominated results are thinned to about ``count`` plans. Returns a single
    plan when gold buys no speed and there is nothing to bank (the ends coincide).
    With ``quantize`` supplied, ``fast``/``cheap`` are quantized before their times
    seed the sweep range and every breakpoint/plan below is computed or lifted
    through it too, so the whole frontier -- caps, bounds and every plan's reported
    time -- lands on real passive deposit boundaries rather than continuous
    estimates (see :func:`_quantize_plan_time`, :func:`_passive_completion_caps`).

    ``prices`` (per-unit gold, from :func:`config.trading_prices`) is what makes a
    consumable buyable and banked surplus sellable; without it nothing can be bought,
    the slow regime is skipped, and the frontier is the single self-sourced plan.

    Builds two shared :func:`_build_base` bases -- ``base_active`` for every
    ``favour_passive=False`` call (fast/cheap endpoints, the epsilon-cap sweep) and
    ``base_passive`` for every ``favour_passive=True`` call (the probe, the
    extension-cap sweep) -- and drives every solve in the sweep through
    :func:`_solve_configured` against one of them, instead of a fresh :func:`minimize`
    (which rebuilds the whole LP) per call. See ``plans/MODEL_REUSE_PLAN.md``.
    """
    base_active = _build_base(
        offers, demand, recipes, crafting_bonus, openables, inventory,
        cookie_sets, prices, pj_gain, pj_price, pj_limit,
    )
    fast = _solve_configured(base_active, gold_weight=_FAST_PRICE)
    cheap = _solve_configured(base_active, gold_weight=_FREE_PRICE)
    # The cheap endpoint's *continuous* makespan -- where its actual sourcing
    # finishes, before quantisation lifts the reported time to a deposit
    # boundary. The slow-regime gate below needs this, not the quantized time:
    # a self-sourced cheap plan can lean on a *daily* deposit (orchard fruit for
    # cider stamina) and quantize up past ``t_bound`` -- board's own *hourly*
    # passive completion -- even though its real sourcing finishes well before
    # it. Comparing the quantized times would then wrongly skip the passive
    # extension and miss the maximally-passive plan that dominates it (faster on
    # the hourly board *and* spends fewer tools).
    t_cheap_raw = cheap.time.total_seconds()
    # Quantize the two endpoints before anything downstream reads their time --
    # the epsilon-sweep range and the _CAP_MARGIN lo/hi filter should sit on
    # real deposit boundaries, not the continuous estimate.
    _quantize_plan_time(
        fast, quantize, demand, recipes or {}, crafting_bonus, openables or [], inventory or {},
    )
    _quantize_plan_time(
        cheap, quantize, demand, recipes or {}, crafting_bonus, openables or [], inventory or {},
    )
    plans = [fast, cheap]
    # Sample well above `count` so thinning has a dense frontier to spread over.
    samples = max(_MIN_CAP_SAMPLES, 4 * count)
    t_fast, t_cheap = fast.time.total_seconds(), cheap.time.total_seconds()

    # Fast regime: the epsilon sweep between the two gold endpoints. Add the
    # passive-completion breakpoints (see _passive_completion_caps): a plan clamped to
    # each is a natural intermediate the geometric sweep can miss. Keep only those
    # comfortably inside the endpoints -- the slowest coincides with the cheap plan's
    # own binding item -- and use them as thinning anchors so they survive.
    caps = _time_caps(t_fast, t_cheap, samples)
    lo, hi = t_fast * (1 + _CAP_MARGIN), t_cheap * (1 - _CAP_MARGIN)
    breakpoints = [
        cap for cap in _passive_completion_caps(
            cheap, demand, recipes or {}, crafting_bonus, openables or [],
            inventory or {}, passive_pet_items, quantize,
        )
        if lo < cap < hi
    ]
    caps += breakpoints
    plans += [
        _solve_configured(base_active, time_cap=timedelta(seconds=cap))
        for cap in caps
    ]

    # Slow regime: extend past the cheap endpoint with favour_passive plans that lean
    # ever harder on passive/farming and bank the surplus. Bounded at t_bound, the
    # slowest non-pet passive completion, found from one uncapped probe -- its own
    # makespan would chase the pet tail (centuries), but its non-pet breakpoints are
    # the real completions. Only worthwhile when surplus is priced (else banking is
    # worth nothing and these slower plans are dominated).
    anchors = list(breakpoints)
    if prices:
        base_passive = _build_base(
            offers, demand, recipes, crafting_bonus, openables, inventory,
            cookie_sets, prices, pj_gain, pj_price, pj_limit,
        )
        probe = _solve_configured(base_passive, favour_passive=True)
        nonpet = _passive_completion_caps(
            probe, demand, recipes or {}, crafting_bonus, openables or [],
            inventory or {}, passive_pet_items, quantize,
        )
        t_bound = max(nonpet, default=t_cheap_raw)
        if t_bound > t_cheap_raw * (1 + _CAP_MARGIN):
            ext_lo, ext_hi = t_cheap_raw * (1 + _CAP_MARGIN), t_bound * (1 - _CAP_MARGIN)
            ext_breaks = [cap for cap in nonpet if ext_lo < cap < ext_hi]
            anchors += ext_breaks
            ext_caps = _time_caps(t_cheap_raw, t_bound, samples) + ext_breaks + [t_bound]
            plans += [
                _solve_configured(
                    base_passive, favour_passive=True, time_cap=timedelta(seconds=cap),
                )
                for cap in ext_caps
            ]

    # Quantize every plan's reported time (idempotent -- fast/cheap are quantized
    # a second time here harmlessly) so `_non_dominated`/`_thin`/dedup rank on
    # real deposit-boundary times: two epsilon-cap plans that landed inside the
    # same deposit window now collapse to equal (quantized) time, and the
    # lower-gold one dominates. Then credit each plan for the surplus it banks
    # (see :func:`plan_surplus_gold`), so its ``cost`` gold is net of what it can
    # sell -- a slow, passive-leaning plan dips below zero and the frontier
    # extends past the "buy nothing" line. Both done before pruning:
    # ``_non_dominated`` and ``_thin`` both rank by the net cost.
    for plan in plans:
        _quantize_plan_time(
            plan, quantize, demand, recipes or {}, crafting_bonus, openables or [], inventory or {},
        )
        plan.surplus_gold = plan_surplus_gold(
            plan, demand, recipes or {}, crafting_bonus, openables or [],
            inventory or {}, prices or {},
        )
    return _thin(_non_dominated(plans), count, tuple(anchors))
