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
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import timedelta
from itertools import pairwise
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
from highs_backend import FastHiGHS
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

# Slack on the refinement tower's pins (see :func:`_solve_configured`): each stage
# locks the last one's optimum at ``value * (1 + _PIN_REL) + _PIN_ABS``. Sized for the
# solver's own feasibility tolerance rather than for exactness: the whole-juice ``pj``
# variables make this a MIP, and a MIP optimum the solver reproduces only to within
# ~1e-6 is cut off by a tighter pin -- the solve then returns Infeasible with nothing
# loaded, and a capped sweep solve (which skips _assert_optimal) reads that back as an
# all-zero plan, landing on the frontier as a bogus 0-minute, 0-gold "fastest".
#
# The slack is **relative**, with only a token absolute floor. A flat 1e-6 floor is far
# too coarse where the pinned quantity is itself tiny: the fastest plan's gold is ~1e-6,
# so the pin stopped binding and the LP simply parked inside the slack. Relative slack
# tracks the quantity instead -- ~1e-3 gold on a 1000-gold plan, ~0 on a 0-gold one --
# and stays orders of magnitude below anything the report can show.
_PIN_REL = 1e-6
_PIN_ABS = 1e-9

# How many makespan caps the epsilon-constraint sweep samples, at least. Kept
# well above the requested plan count so thinning has a dense frontier to spread
# over; the LPs are tiny, so a few dozen extra solves are cheap.
_MIN_CAP_SAMPLES = 40

# Threads a frontier spreads its independent solves over -- the sweeps' capped
# solves and the exact finish (see :func:`_sweep`, :func:`_frontier_sweeps`).
# HiGHS releases the GIL while it solves, so these overlap; each extra sweep
# thread needs its own copy of the base, which costs a build. 1 = sequential.
FRONTIER_WORKERS = 4

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
# "highs" is PuLP's HiGHS backend with a faster model hand-over (see
# :mod:`highs_backend`): the same model and solves, built without PuLP's
# per-solve Python rebuild. HiGHS's feasibility-jump heuristic is off: it hunts
# for a first incumbent at a MIP's root, which on these MIPs (one integer, the
# daily-budget day count, or a few Pumpkin Juices) the root LP all but hands
# over anyway, and it was half of every MIP solve's time.
_HIGHS_OPTIONS = {"mip_heuristic_run_feasibility_jump": False}
_SOLVER_BACKENDS = {
    "highs": lambda: FastHiGHS(msg=False, **_HIGHS_OPTIONS),
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

    ``boost_offers`` are the optional extra building offers: the Hickory Omelette
    sawmill boost (:func:`sources.sawmill_boost_offers`, grounded in an omelette
    the plan holds or buys) and the Sugar Cane Mill
    (:func:`sources.sugar_cane_mill_offers`, grounded in sugar cane and machine
    presses), so each is just another gathering channel here.
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
    # Veggie Juices drunk to speed plantings (see :meth:`crops.Farm.veggie_juice_steps`)
    # -- crafted, held or AP-traded, and valued like the other consumables.
    veggie_juices: float = 0.0
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
    # Opportunity cost (gold) of the free stamina this plan spends on Apple Cider
    # explores -- the banked ``--current-stamina`` and the Farmhouse's passive
    # deposit, which arrive at no gold but could have fuelled something else, so are
    # valued at the Orange-Juice equivalent (``orange_juice_price / 100``, see
    # :func:`_build_base`). Stamina drunk from Orange Juice is excluded (already
    # priced via the juice). 0 unless the ``price_stamina`` option is on. It buys
    # nothing -- so it is not in ``gold``, nor a tradeable consumable so not in
    # :func:`plan_gold` -- but :attr:`cost` nets it in, so the frontier ranks a
    # stamina-draining plan for it (and the report prices it on its own line).
    stamina_gold: float = 0.0
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
    # Whether the Pumpkin Juice opportunity cost (``pumpkin_gold``) counts in net
    # gold (see :attr:`cost`). Default True -- juice is priced like any buy. Set
    # False by the ``price_pumpkin_juice`` option: the LP still prices juice (so it
    # is used only when it beats its alternative -- the price stays a *gate*), but
    # its cost is dropped from the reported/ranked net gold, so a plan is compared on
    # real gold spent with the juice shown as its own count. Not symmetric with
    # ``price_stamina=false``, which zeroes the price in the LP too.
    price_pumpkin_juice: bool = True
    remainder: dict[str, int] = field(default_factory=dict)
    # This stage's ascension outcome (which items were mastered/grandmastered/
    # mega'd, AK earned from steps vs. chores, gross mastery supply) -- set only
    # when :func:`minimize`/:func:`_build_base` was given an :class:`AscensionSpec`.
    ascension: AscensionOutcome | None = None
    # What would reproduce this exact solve: ``(base, kwargs)`` for
    # :func:`_solve_configured`. Stamped there on every plan; :func:`frontier` uses it
    # to re-solve the plans it returns with integer Pumpkin Juice after sweeping the
    # cheap continuous relaxation (see there). ``None`` on hand-built plans (tests) and
    # never read except by the finish step. ``repr=False``/``compare=False`` so it does
    # not clutter reprs or perturb equality, and excluded from ``cost``-based ranking.
    _origin: tuple[_Base, dict] | None = field(
        default=None, repr=False, compare=False
    )

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

        The stamina opportunity cost (``stamina_gold``, the free banked/passive
        stamina a plan burns, valued at its Orange-Juice equivalent) is a real cost
        added on: a fast plan that drains a big stamina bank is not free, it spent
        stamina worth gold. 0 unless the ``price_stamina`` option is on.

        Pumpkin Juice's cost (``pumpkin_gold``, part of ``gold``) is netted back out
        when :attr:`price_pumpkin_juice` is False -- the ``price_pumpkin_juice``
        option: the LP still priced juice (so it was used only where it beat its
        alternative), but the reported net gold then reflects real gold spent, with
        the juice a separate count to weigh against one's own value for it.
        """
        net = self.gold - self.surplus_gold + self.stamina_gold
        if not self.price_pumpkin_juice:
            net -= self.pumpkin_gold
        return (self.time.total_seconds(), net)


def _minutes(time: timedelta) -> float:
    return time.total_seconds() / 60


# Building the LP's rows. PuLP's operator style (``rate * x``, ``lpSum``, ``a + b``)
# allocates a fresh LpAffineExpression for every term and copies the whole running
# expression at every ``+``/``-``, which made model building the biggest cost of a
# --mastery-costs sweep. The hot rows are instead collected as plain
# ``{variable: coefficient}`` dicts and turned into one LpConstraint each.
# ``_accumulate`` mirrors PuLP's arithmetic exactly -- a zero coefficient adds no
# term (as ``0 * x`` is empty), a repeated variable sums in place, insertion order
# is kept -- so the model handed to the solver is identical to the operator-built
# one (same rows, same order, same coefficients).


def _accumulate(
    terms: dict[pulp.LpVariable, float],
    pairs: Iterable[tuple[pulp.LpVariable, float]],
    sign: int = 1,
) -> None:
    """Add ``sign * coefficient`` for each ``(variable, coefficient)`` of ``pairs``
    into ``terms``."""
    for var, coef in pairs:
        if coef == 0:
            continue
        if var in terms:
            terms[var] += coef * sign
        else:
            terms[var] = coef * sign


def _row(
    terms: dict[pulp.LpVariable, float],
    sense: int,
    constant: float = 0.0,
    rhs: float | None = None,
) -> pulp.LpConstraint:
    """``terms + constant <sense> rhs`` as one PuLP constraint."""
    return pulp.LpConstraint(
        pulp.LpAffineExpression(terms, constant=constant), sense, rhs=rhs
    )


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
    # What each item's juices earn toward its goal, clamped at the goal itself
    # (``pj_credit <= pj_gain*pj``, ``<= need``). The balance credits this, not
    # ``pj_gain*pj``, so an overshooting last juice is legal but earns nothing extra.
    pj_credit: dict[str, pulp.LpVariable]
    gold: pulp.LpAffineExpression
    spend_value: pulp.LpAffineExpression
    # The market value of everything the plan spends, whichever way it was sourced:
    # ``spend_value`` (every consumable, bought or drawn from stock) plus the juice
    # gold. The LP form of :func:`plan_gold`, and what stage 2 of the refinement
    # tower minimises -- see :func:`_solve_configured`.
    total_value: pulp.LpAffineExpression
    used: list[Offer]
    crafted: list[str]
    opened: list[OpenFlow]
    remainder: dict[str, float]
    pj_gain: dict[str, float]
    pj_price: float
    # Stamina opportunity cost (see _build_base): ``stamina_op`` meters the free
    # (banked + passive) stamina consumed and ``stamina_price`` is its per-unit
    # Orange-Juice-equivalent gold value, folded into ``spend_value``/``total_value``
    # above. ``None``/``0`` when the option is off (no ``prices["stamina"]``).
    stamina_op: pulp.LpVariable | None = None
    stamina_price: float = 0.0
    # Stamped onto every plan this base yields (see :attr:`SourcingPlan.cost`); the
    # LP prices juice regardless, this only governs whether that cost shows in net.
    price_pumpkin_juice: bool = True
    # The daily Grape Juice instant-grow allowance the daily-budget constraints were
    # built with (0 = unconfigured, no cap). Kept so the readout can charge a plan
    # the wait its juice spend implies -- see :func:`_solve_configured`.
    grape_juices_per_day: float = 0.0
    # How many daily allocations (cookie set, Grape Juice quota) the plan collects --
    # an integer, since days arrive whole. ``None`` when no offer spends a daily
    # resource, which keeps such a model a pure LP. See the daily-budget constraints
    # in :func:`_build_base`; :func:`_solve_configured` relaxes it for cheap sweeps.
    days_available: pulp.LpVariable | None = None
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
    grape_juices_per_day: float = 0.0,
    ap_tradeable: frozenset[str] = frozenset(),
    non_mastery_goals: frozenset[str] = frozenset(),
    no_buy: frozenset[str] = frozenset(),
    ascension: AscensionSpec | None = None,
    price_pumpkin_juice: bool = True,
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
    # (see config.trading_prices for the presence-not-value convention). The buyable
    # universe is the gold consumables (BUYABLE) plus any Arnold-Palmer-tradeable
    # items (``ap_tradeable``, from config.arnold_palmer_prices): a trade is just a
    # buy priced in AP-converted-to-gold (its gold-equivalent is already folded into
    # ``prices``), so it flows through the same buy machinery below.
    purchasable = BUYABLE | ap_tradeable
    buyable = {item for item in purchasable if item in prices}
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
            buy={}, buy_cookie=None, pj={}, pj_credit={}, gold=0, spend_value=0,
            total_value=0,
            used=[], crafted=[], opened=[], remainder=remainder,
            pj_gain=pj_gain, pj_price=pj_price, remainder_only=True,
            price_pumpkin_juice=price_pumpkin_juice,
            grape_juices_per_day=grape_juices_per_day,
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
    # ``max_units`` caps an offer's usage (the once-per-day cookie burst, which a
    # plan cannot simply repeat back-to-back -- see :func:`sources.farming_offers`).
    usage = [
        pulp.LpVariable(f"x{i}", lowBound=0, upBound=o.max_units)
        for i, o in enumerate(used)
    ]
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

    # Cookie sets and Grape Juice instant-grows spent, capped below at held +
    # bought sets and (both of them) at what the plan's span makes available. The
    # other consumable tallies (Arnold Palmers, Large Nets, ...) are read off the
    # solved draws in the plan built at the end, not needed as LP expressions here.
    cookies = pulp.LpAffineExpression(
        [(x, o.cookies) for o, x in zip(used, usage) if o.cookies]
    )
    juices_used = pulp.LpAffineExpression(
        [(x, o.gj) for o, x in zip(used, usage) if o.gj]
    )

    # Buy variables: a buyable consumable can be bought (free, instant supply) for
    # gold instead of self-sourced (which costs makespan). Only reachable, priced
    # ones get a variable (a *present* price, even 0 -- free); cookie sets are
    # bought by lifting their cap below. An item in ``no_buy`` (the ``--self-sourced``
    # tools -- Arnold Palmer / Large Net / Apple Cider) gets no buy variable, so its
    # balance must be met by self-sourcing it; it stays in ``buyable`` above (still
    # obtainable, and channels grounded in it still usable) -- only the purchase is
    # withheld. Its price stays in ``prices``, valuing it in ``spend_value`` as
    # before, but ``gold`` (below) never charges it since there is no buy var.
    buy = {
        item: pulp.LpVariable(f"b{i}", lowBound=0)
        for i, item in enumerate(sorted(items & purchasable))
        if item in prices and item not in no_buy
    }
    price_cookie = prices.get("cookie set", 0.0)
    buy_cookie = (
        pulp.LpVariable("bcookie", lowBound=0) if "cookie set" in prices else None
    )
    # Held cookie sets are free; beyond them a purchase lifts the cap at gold cost.
    problem += cookies <= cookie_sets + (buy_cookie if buy_cookie is not None else 0)

    # Daily budgets. A cookie set and the day's Grape Juice instant-grow allowance
    # are once-a-*day* resources shared by the whole farm, so how much of each a
    # plan may spend is set by how many days it runs -- not by how many different
    # crops it touches. ``+ 1`` because the plan's first day is available at once:
    # without it a plan wanting a single cookie burst (an instant action, the whole
    # point of that offer) would have to stretch its makespan to a full day, which
    # is the spurious-wait artifact the burst exists to avoid.
    #
    # These are what hold "once a day" across crops. Bounding each crop's burst
    # offer on its own (the old ``max_units=1``) only said "once per crop", so the
    # LP could burst every qualifying crop within the same day, spending a cookie
    # set and a full day's juice allowance on each -- on a real save that meant 16
    # sets and 211 juices inside six days, most of them on crops the goal did not
    # want, harvested purely for their Scythe-of-Dewstar bonus drops. Sharing the
    # budget also *frees* the burst in the other direction: a plan running N days
    # may now burst the same crop on each of them, which it could not before.
    #
    # Note ``makespan`` is the max over concurrent tracks, which is the right base:
    # a plan that spends 30 days fishing really does accrue 30 days of cookie sets
    # to spend on farming, whatever the farming track's own length.
    #
    # ``days_available`` -- how many daily allocations the plan collects -- is an
    # INTEGER, and has to be: days arrive whole. A plan running 6d 4h touches seven
    # days, so it collects seven sets, not 7.17. Left continuous the LP spends that
    # fractional remainder, and since a part set still costs a whole one the readout
    # rounds it up -- reporting an eighth set the plan never ran long enough to
    # collect. Integrality makes ``<= makespan_days + 1`` do the flooring exactly:
    # 7.17 rounds down to 7, and buying an eighth means running a seventh full day.
    #
    # An integer variable turns the whole model into a MIP, so build it only where a
    # daily budget can actually *bind*: some offer must spend the resource, and the
    # resource must be available at all. With no sets held and none buyable
    # ``cookies <= 0`` already, and with no allowance configured the juice cap is not
    # applied -- in either case the day count would constrain nothing while costing
    # every solve MIP overhead. A fishing/exploring-only model likewise stays pure LP.
    # See ``_solve_configured``, which relaxes this alongside the juice variables for
    # the frontier's cheap sweep.
    #
    # Grape Juice is bounded twice over and needs both: the item balance (a grow
    # ``consumes`` real juice, so a plan cannot use more than it holds or presses) and
    # this daily allowance (holding thousands does not let you drink more than the
    # day's quota). ``grape_juices_per_day`` of 0 means "no allowance configured"
    # rather than "none allowed", keeping callers that do not thread the farm's
    # setting through unchanged.
    cookies_bind = (cookie_sets > 0 or buy_cookie is not None) and any(
        o.cookies for o in used
    )
    juices_bind = grape_juices_per_day > 0 and any(o.gj for o in used)
    days_available: pulp.LpVariable | None = None
    if cookies_bind or juices_bind:
        days_available = pulp.LpVariable("days", lowBound=0, cat=pulp.LpInteger)
        problem += days_available <= makespan / _MINUTES_PER_DAY + 1
        if cookies_bind:
            problem += cookies <= days_available
        if juices_bind:
            problem += juices_used <= grape_juices_per_day * days_available

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
    # Gated on ``pj_gain`` alone, never on the price: ``pj_gain`` is already empty
    # when juice is switched off (see main.build_inputs), so a present price of 0
    # means "juice is free", not "no juice" -- the same falsy-zero trap fixed for
    # ``[trading]`` prices.
    #
    # **Integer**: a juice is indivisible, and a continuous ``pj`` bought fractions of
    # one -- 0.23 of a juice covering the whole of a nearly-done goal, which no player
    # can drink. Bounded above by the juices it takes to *cover* the goal (rounded up,
    # so the last one is allowed to overshoot); ``pj_credit`` below clamps what that
    # overshoot earns.
    pj = {}
    if pj_gain:
        pj = {
            item: pulp.LpVariable(
                f"pj{i}", lowBound=0,
                upBound=math.ceil(wanted[item][1] / pj_gain[item]),
                cat="Integer",
            )
            for i, item in enumerate(sorted(items & wanted.keys() & pj_gain.keys()))
            if pj_gain[item] > 0
        }
    # What the juices actually earn toward the goal: ``pj_gain`` each, but never more
    # than the goal itself (mastery cannot exceed the requirement, so a juice past full
    # coverage is wasted, not banked). This is the clamp that used to be written as
    # ``pj*gain <= need`` -- a cap on the *credit*, which also banned the whole juice
    # that earns it: an item needing less than one juice's worth (Frost Shield: 2,112
    # units against a 9,091-unit juice) was capped at 0.23 juices instead of paying for
    # one and covering it outright. Capping the count and clamping the credit separately
    # keeps the last juice legal while still refusing to pay it for overshoot.
    pj_credit = {
        item: pulp.LpVariable(f"pjc{i}", lowBound=0, upBound=wanted[item][1])
        for i, item in enumerate(sorted(pj))
    }
    for item, var in pj.items():
        problem += pj_credit[item] <= pj_gain[item] * var
    if pj_limit is not None and pj:
        problem += pulp.lpSum(pj.values()) <= pj_limit
    if pj:
        gold = gold + pj_price * pulp.lpSum(pj.values())

    # Market value of every consumable the plan spends -- Arnold Palmers, Large Nets,
    # Grape Juice, Apple Cider, Orange Juice, Veggie Juice (from the offers' per-unit
    # tallies) and
    # cookie sets. The ``favour_passive`` mode minimises this to push sourcing off the
    # tool-hungry fishing/exploring/GJ channels and onto free passive production and
    # basic farming -- the slow, resource-banking end of the frontier (see
    # :func:`frontier`). Grape Juice carries no gold price, so a nominal weight keeps
    # it penalised alongside the priced tools rather than free.
    spend_terms: dict[pulp.LpVariable, float] = {}
    _accumulate(spend_terms, (
        (
            x,
            o.ap * prices.get("arnold palmer", 0.0)
            + o.ln * prices.get("large net", 0.0)
            + o.gj * (prices.get("grape juice", 0.0) or 1e-4)
            + o.consumes.get("apple cider", 0.0) * prices.get("apple cider", 0.0)
            + o.consumes.get("orange juice", 0.0) * prices.get("orange juice", 0.0)
            + o.consumes.get("hickory omelette", 0.0)
            * prices.get("hickory omelette", 0.0)
            # Veggie Juice (juiced farming) is not a gold consumable; a nominal
            # weight keeps it penalised, like Grape Juice, when it has no price.
            + o.consumes.get("veggie juice", 0.0)
            * (prices.get("veggie juice", 0.0) or 1e-4),
        )
        for o, x in zip(used, usage)
    ))
    spend_value = (
        pulp.LpAffineExpression(spend_terms) + cookies * prices.get("cookie set", 0.0)
    )

    # Total market value spent, the stage-2 tie-break (see :func:`_solve_configured`).
    # ``spend_value`` already values every consumable at its price whether bought or
    # drawn from stock, so adding the juice gold -- juice being the one consumable
    # with no self-sourced form, and so absent from ``spend_value`` -- completes it.
    # Buying is not double-charged: ``buy[i] <= consumed + used_up + emptied`` caps
    # purchases at what the plan actually spends, which ``spend_value`` already counts.
    #
    # Arnold-Palmer trades are the exception ``spend_value`` misses: a traded material
    # (an ``ap_tradeable`` item) is bought -- its gold is in ``gold`` -- but it is not
    # one of the consumables ``spend_value`` enumerates, so without this its market
    # value is invisible to the tie-break and the stage-2 solve would treat trading as
    # *free*. That made the fast endpoint prefer a huge cheap-per-unit trade (millions
    # of Oak) over a fractionally-priced Pumpkin Juice that covers the same mastery
    # goal, since only the juice showed up as a cost. Charge each trade its buy gold
    # here so the tie-break weighs trading honestly against juice and self-sourcing.
    ap_trade_value = pulp.lpSum(
        prices[item] * var for item, var in buy.items() if item in ap_tradeable
    )
    total_value = (
        spend_value + ap_trade_value + (pj_price * pulp.lpSum(pj.values()) if pj else 0)
    )

    # Index each offer's yields/consumes -- and each open's -- by item once, as
    # ``(variable, coefficient)`` pairs (see ``_accumulate``), so the per-item loop
    # below neither rescans every offer and openable for every item nor builds an
    # expression per term.
    yield_terms: dict[str, list[tuple[pulp.LpVariable, float]]] = {}
    consume_terms: dict[str, list[tuple[pulp.LpVariable, float]]] = {}
    for o, x in zip(used, usage):
        for item, rate in o.yields.items():
            if rate > 0:
                yield_terms.setdefault(item, []).append((x, rate))
        for item, qty in o.consumes.items():
            if qty > 0:
                consume_terms.setdefault(item, []).append((x, qty))
    open_yield_terms: dict[str, list[tuple[pulp.LpVariable, float]]] = {}
    open_consume_terms: dict[str, list[tuple[pulp.LpVariable, float]]] = {}
    for f, o in zip(opened, opens):
        for item, qty in f.yields.items():
            if qty:
                open_yield_terms.setdefault(item, []).append((o, qty))
        for item, qty in f.consumes.items():
            if qty:
                open_consume_terms.setdefault(item, []).append((o, qty))

    # Stamina opportunity cost. Stamina an Apple Cider explore burns that arrived
    # *free* -- the banked ``--current-stamina`` (an inventory ``held`` credit) and
    # the Farmhouse's daily passive deposit, neither of which consumes anything --
    # could have fuelled something else, so its real cost is what it would take to
    # replace: an Orange Juice yields ``ORANGE_JUICE_STAMINA`` per unit, hence
    # ``prices["stamina"] = orange_juice_price / 100`` gold each (a valuation-only
    # key, like grape juice -- never in ``BUYABLE``, so it never gets a buy var; see
    # config.trading_prices). ``config`` injects it only when the option is on, so an
    # absent key (the default in every optimizer unit test) leaves this path off and
    # every existing solve byte-identical.
    #
    # Charge the free stamina consumed = all stamina an offer spends less only the
    # stamina *topped up by drinking Orange Juice*. A unit of stamina an explore burns
    # has the same replacement cost -- one Orange Juice's worth -- whatever its source,
    # so all of it is priced EXCEPT juice-drinking stamina: the juice is already paid
    # for as the juice in ``spend_value``/``gold``, and charging its stamina here too
    # would double count. Eating an Apple is deliberately NOT exempt: those Apples are
    # overwhelmingly free byproducts of opening idle chests (unpriced keys), so exempting
    # their stamina handed the solver a loophole -- manufacture free Apples to fuel
    # explores and never touch the priced banked/passive stamina, dodging the charge
    # entirely (a real plan did exactly this). Charging Apple-eaten stamina closes it;
    # the Apple's own trade value is a separate, near-zero cost for a surplus Apple, so
    # this reflects the stamina's real value rather than double counting in practice.
    # A free variable pinned at ``>= consumed - topped_up`` (never below 0) so a plan
    # that drinks more juice-stamina than it burns is not handed a negative charge. Only
    # in ``spend_value`` (hence ``total_value``), never ``gold``: using free stamina
    # spends no real gold, so it must stay a zero-gold action for the buy-nothing /
    # --max-gold endpoints (an opportunity cost the tie-break and favour_passive lean
    # weigh, and ``SourcingPlan.cost`` nets in for the frontier -- not a purchase).
    stamina_price = prices.get("stamina", 0.0)
    stamina_op: pulp.LpVariable | None = None
    if stamina_price > 0:
        stamina_op = pulp.LpVariable("stamina_op", lowBound=0)
        consumed_stamina = pulp.LpAffineExpression(consume_terms.get("stamina", []))
        topped_up_stamina = pulp.lpSum(
            o.yields.get("stamina", 0.0) * x
            for o, x in zip(used, usage)
            if o.yields.get("stamina", 0.0) and "orange juice" in o.consumes
        )
        problem += (
            stamina_op >= consumed_stamina - topped_up_stamina, "stamina_op_free"
        )
        spend_value = spend_value + stamina_price * stamina_op
        total_value = total_value + stamina_price * stamina_op

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
        # Each part of the balance as ``(variable, coefficient)`` pairs.
        gathered = yield_terms.get(item, [])
        # What offers use up as inputs (Arnold Palmers an explore burns, the
        # stamina and Apple Cider a cider explore spends) -- these must be
        # sourced like any other need.
        used_up = consume_terms.get(item, [])
        made = [(craft[item], crafting_bonus)] if item in craft else []
        consumed = [(craft[j], qty) for j, qty in consumers.get(item, [])]
        unpacked = open_yield_terms.get(item, [])
        emptied = open_consume_terms.get(item, [])
        # Stock on hand is free supply for intermediates; a directly-demanded
        # item's balance is already net of what is held, so it gets no credit.
        held = inventory.get(item, 0.0) if item not in wanted else 0.0
        need = wanted[item][1] if item in wanted else 0
        # gathered + made - consumed - used_up + unpacked - emptied + held
        #   + bought + boosted >= need
        balance: dict[pulp.LpVariable, float] = {}
        _accumulate(balance, gathered)
        _accumulate(balance, made)
        _accumulate(balance, consumed, -1)
        _accumulate(balance, used_up, -1)
        _accumulate(balance, unpacked)
        _accumulate(balance, emptied, -1)
        if item in buy:
            _accumulate(balance, [(buy[item], 1)])
        # A Pumpkin Juice on this goal item covers pj_gain[item] units of its demand,
        # clamped at the goal by pj_credit (built above).
        if item in pj:
            _accumulate(balance, [(pj_credit[item], 1)])
        problem += _row(balance, pulp.LpConstraintGE, constant=0.0 + held, rhs=need)
        # Buying can never earn an item's own mastery: a bought unit only covers a
        # non-goal use (a craft material, or a channel's tool), so cap purchases at
        # that derived demand. The goal ``need`` must then come from making or
        # gathering. Slack for non-goal items (they can be bought up to their use).
        # Exception: a **plain -i count goal** (in ``non_mastery_goals``, never a
        # mastery goal) may be satisfied by *trading* -- so an AP-tradeable item's
        # cap includes its own ``need``. Scoped to ``ap_tradeable`` so gold-buy of
        # the consumables is untouched; a mastery goal is never in the set, so its
        # floor stays tight and it must be produced.
        if item in buy:
            # buy <= consumed + used_up + emptied (+ need)
            cap: dict[pulp.LpVariable, float] = {buy[item]: 1}
            _accumulate(cap, consumed, -1)
            _accumulate(cap, used_up, -1)
            _accumulate(cap, emptied, -1)
            trade_goal = item in ap_tradeable and item in non_mastery_goals
            problem += _row(cap, pulp.LpConstraintLE, constant=-need if trade_goal else 0.0)

        # Ascension mastery supply: GROSS production only (gathered + crafted +
        # unpacked, the same terms the balance above uses before ``consumed``/
        # ``used_up``/``emptied`` net anything off) -- deliberately not the net
        # balance, since mastery progress accrues even on production this plan
        # goes on to consume as a craft material (mastery-chaining).
        if item in ascend_y:
            supply: dict[pulp.LpVariable, float] = {}
            _accumulate(supply, gathered)
            _accumulate(supply, made)
            _accumulate(supply, unpacked)
            bonus = ascension.mastery_bonus
            S_i = pulp.LpAffineExpression(
                {var: bonus * coef for var, coef in supply.items()} if bonus else {}
            )
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
    tracks: dict[str, dict[pulp.LpVariable, float]] = {}
    for offer, x in zip(used, usage):
        _accumulate(
            tracks.setdefault(offer.track, {}), [(x, _minutes(offer.time_per_unit))]
        )
    for terms in tracks.values():
        _accumulate(terms, [(makespan, 1)], -1)
        problem += _row(terms, pulp.LpConstraintLE)

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
        buy=buy, buy_cookie=buy_cookie, pj=pj, pj_credit=pj_credit, gold=gold,
        spend_value=spend_value,
        total_value=total_value,
        used=used, crafted=crafted, opened=opened, remainder=remainder,
        pj_gain=pj_gain, pj_price=pj_price,
        stamina_op=stamina_op, stamina_price=stamina_price,
        price_pumpkin_juice=price_pumpkin_juice,
        grape_juices_per_day=grape_juices_per_day, days_available=days_available,
        ascension=ascension, ascend_y=ascend_y, ascend_S=ascend_S,
        unmasterable=unmasterable,
    )


class _UnmeetableCap(Exception):
    """A capped sweep solve whose ``time_cap`` no plan can actually meet.

    The sweep's caps are drawn between the two *active-regime* endpoints, so they are
    feasible for the regime that produced them -- but not for every regime run against
    them. ``favour_passive`` switches Pumpkin Juice off, and juice is exactly what
    makes ``t_fast`` instant, so a cap taken from a juice-fed 0-minute endpoint can be
    milliseconds long and flatly unreachable once juice is gone.

    That is a fact about the cap, not about the demand: the same base solves fine
    uncapped. So it is not :func:`_assert_optimal`'s "the demand is unsatisfiable"
    error -- :func:`frontier` just drops the sample and sweeps on. Before whole-juice
    ``pj`` made this a MIP, these solves came back with the previous solution still
    loaded and were silently read out as empty, cost-(0, 0) plans -- which then sorted
    to the head of the frontier as a bogus 0-minute, 0-gold "fastest".
    """


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
    seen flagging a feasible model "Unbounded" or "Infeasible" (impossible here --
    the objective is a sum of nonnegative terms -- so it is presolve mis-flagging
    the tiny ~1e-8 gold-weight coefficients; on the web config's mastery-costs
    sweep, a third of the items). Before declaring the demand unsatisfiable,
    re-solve with second opinions: HiGHS with presolve off first (in-process, a
    few milliseconds) -- and, since a MIP (the integer ``days_available``) still
    presolves at its search nodes, with that restricted to the root, which
    together solve every such model seen so far -- then the bundled CBC (a
    subprocess per solve). The first that finds an optimum leaves
    that (equally valid) solution loaded and is returned, so the caller's
    remaining refinement solves stay on a backend that can handle this model.
    Returns the solver the rest of the call should use.
    """
    if pulp.LpStatus[problem.status] == "Optimal":
        return solver
    fallbacks = [pulp.PULP_CBC_CMD(msg=False)]
    if isinstance(solver, pulp.HiGHS):
        fallbacks.insert(
            0,
            FastHiGHS(
                msg=False, presolve="off", mip_root_presolve_only=True,
                **_HIGHS_OPTIONS,
            ),
        )
    for fallback in fallbacks:
        problem.solve(fallback)
        status = pulp.LpStatus[problem.status]
        if status == "Optimal":
            return fallback
    raise ValueError(
        f"no plan can satisfy the demand (solver status: {status}); "
        "a demanded item likely needs more than its limited sources "
        "(e.g. held Mega/Spring seeds) can ever supply"
    )


# Which lower steps a step needs pinned alongside it (mega needs grandmaster
# needs master, among whichever exist for that item) -- see
# :func:`_round_and_fix_ascension`.
_ASCEND_PREREQS: dict[str, tuple[str, ...]] = {
    "master": (),
    "grandmaster": ("master",),
    "mega": ("master", "grandmaster"),
}


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

    A trial pin is a *bound flip* (``y`` fixed to 0 or 1), never a structural
    edit, so trials skip pulp's full model rebuild (which costs several times
    the actual solve on real candidate sets): when the primary solve ran on
    HiGHS, ``problem.solverModel`` is the still-loaded in-process highspy
    model with every variable's column index on ``var.index``, and trials
    flip column bounds there and re-solve warm-started from the previous
    basis. On any other backend (CBC fallback, or after a mid-loop rescue)
    trials flip the pulp variables' own bounds and re-solve through pulp.
    Each round keeps the winning trial's solution snapshot -- no third solve
    to restore the winner -- and skips the pin-down trial entirely whenever
    pin-up achieves the running relaxation bound (provably unbeatable, see
    the loop comment). The decisions are materialised at the end as
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
    # The objective of the solve that produced ``yvals`` -- the relaxation
    # optimum of the remaining (partially pinned) problem, hence a lower bound
    # on every further-pinned solve. Lets the loop skip the pin-down trial
    # when pin-up already achieves it (see the loop comment). Read from the
    # same backend the trials use so the two are comparable (the highs model
    # drops any constant term pulp's objective value would include).
    if highs is not None:
        bound = highs.getObjectiveValue()
    else:
        bound = pulp.value(problem.objective)

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
        # Provable shortcut: ``bound`` is the relaxation optimum of the
        # remaining problem, and any pin-down solution is feasible in that
        # relaxation, so pin-down can never solve strictly below it. When
        # pin-up already achieves the bound it cannot lose -- and a tie
        # already prefers up (the `<=` below) -- so the down trial is
        # skipped. On the fast endpoint of a low-mastery climb most steps
        # free-ride on supply the plan produces anyway (pinning them up
        # costs nothing), so this collapses most rounds to a single solve.
        if up is not None and up[0] <= bound + 1e-6 + 1e-9 * abs(bound):
            commit(up_pins, up[1])
            bound = up[0]
            continue
        down = trial({(item, step): 0})
        if up is not None and (down is None or up[0] <= down[0]):
            commit(up_pins, up[1])
            bound = up[0]
        elif down is not None:
            commit({(item, step): 0}, down[1])
            bound = down[0]
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
            bound = pulp.value(problem.objective)

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


def _has_integers(base: _Base) -> bool:
    """Whether ``base`` has any integer variable, so its relaxation is not exact.

    The Pumpkin Juice counts and ``days_available`` (the daily-budget day count).
    Where neither exists the sweep's relaxed solves already *are* the exact ones and
    the frontier can skip its re-solve pass entirely.
    """
    return bool(base.pj) or base.days_available is not None


def _solve_configured(
    base: _Base,
    *,
    gold_weight: float = 0.0,
    time_cap: timedelta | None = None,
    favour_passive: bool = False,
    exact_integers: bool = True,
    juice_cap: int | None = None,
) -> SourcingPlan:
    """Solve ``base`` under one call's mode -- the per-call counterpart of
    :func:`_build_base`. Reusable across many calls sharing a base: every
    constraint this call adds is named and recorded in ``base.ephemeral`` so
    the *next* call's first step can remove exactly them, leaving the shared
    balance/track constraints untouched. See ``plans/MODEL_REUSE_PLAN.md``.

    ``exact_integers`` decides whether the model's integer variables -- the Pumpkin
    Juice counts and ``days_available``, the daily-budget day count -- solve as whole
    units (a MIP: ~50x the per-solve cost, see :func:`frontier`) or as the continuous
    relaxation. :func:`frontier` sweeps its ~hundreds of interior points with
    ``exact_integers=False`` and re-solves only the plans it returns with ``True``; a
    bare :func:`minimize` and every other caller keep the exact default. Relaxed,
    ``days_available`` reverts to the fractional ``makespan_days + 1`` -- generous by
    a part day, which only ever mis-ranks an interior sweep point that the exact
    finish then replaces.

    ``juice_cap`` bounds the whole-juice total at ``sum(pj) <= juice_cap`` -- the
    lever a ``--spread pumpkin-juice`` uses to pin the juice axis *directly* (one
    exact solve per target, reaching 0), rather than bisecting the time cap for a
    plan that happens to land on a given count. Paired with ``gold_weight`` it asks
    "the fastest plan using at most N juices". No-op when the model has no juice.
    """
    origin = (
        base,
        {
            "gold_weight": gold_weight,
            "time_cap": time_cap,
            "favour_passive": favour_passive,
            "juice_cap": juice_cap,
        },
    )
    if base.remainder_only:
        return SourcingPlan(
            remainder=base.remainder, _origin=origin,
            price_pumpkin_juice=base.price_pumpkin_juice,
        )

    # Toggle the model's integer variables for this solve. Cheap and idempotent;
    # reuses the one shared base rather than building a second integer copy.
    cat = pulp.LpInteger if exact_integers else pulp.LpContinuous
    for var in base.pj.values():
        var.cat = cat
    if base.days_available is not None:
        base.days_available.cat = cat

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

    if juice_cap is not None and base.pj:
        problem += (pulp.lpSum(base.pj.values()) <= juice_cap, "juice_cap")
        base.ephemeral.append("juice_cap")

    if favour_passive:
        # Slowest, most passive-leaning end: fewest tools spent (so passive and
        # farming carry the demand), makespan bounded by ``time_cap`` so it cannot
        # run away onto a centuries-long pet trickle.
        primary = base.spend_value
    elif time_cap is not None:
        primary = base.gold  # least gold under the time cap (epsilon-constraint)
    else:
        # A tiny gold_weight buys freely for the fastest plan; a huge one buys
        # nothing (gold 0) for the slowest, fully self-sourced plan.
        primary = base.makespan + gold_weight * base.gold
    problem.setObjective(primary)
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
    #   2. value     -- least total_value at that point: the market value of
    #                   everything spent, however sourced
    #   3. activity  -- least crafts + gathering + buying, shedding pointless slack
    #
    # Stage 2 is the crux. In a weighted-sum endpoint the makespan term (~1e5
    # minutes) dwarfs the tiny-weighted gold (~1e-6 for the fastest plan), so within
    # the solver's tolerance it is free and the first solve parks the buys on an
    # arbitrary degenerate vertex -- buying Large Nets or Grape Juice that bought no
    # time. A plan spending more for no time gain is strictly worse, so with the
    # primary pinned we re-minimise that spend alone (now the sole objective, at a
    # scale the solver resolves), pinning the genuinely cheapest plan. Stage 3 then
    # sheds surplus crafts / off-track farming without moving time or value.
    #
    # It minimises ``total_value``, not ``gold``, because gold alone charges only what
    # a plan *buys* -- and Pumpkin Juice, having no self-sourced form, is the one
    # consumable whose every use is a purchase. Least-gold therefore read as
    # "fewest juices" and would burn tens of thousands of held Arnold Palmers, Large
    # Nets and Apple Cider (thousands of gold at market) to dodge a handful of juices
    # worth a fraction of a gold. Charging held stock at the same price as bought
    # stock makes that trade honest. It also fixes a numerical failure: a juice column
    # carries a ~1e5 demand coefficient (``pj_gain``) against a gold coefficient of
    # the juice price, so at a low price the least-gold optimum sat below HiGHS's
    # resolution -- whether it shed juice at all depended on presolve luck, and the
    # juice count fell from 13 to 5 when the price rose from just 0.0005 to 0.001.
    # Against ``total_value`` the same choice is decided by thousands of gold, not
    # thousandths. The frontier's Pareto axis stays bought gold: in epsilon mode that
    # is what stage 1 minimised, and it is pinned below before this stage runs.
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
            problem += (base.spend_value <= sv_star * (1 + _PIN_REL) + _PIN_ABS, "stage1_pin")
        else:
            t_star = base.makespan.value() or 0.0
            problem += (base.makespan <= t_star * (1 + _PIN_REL) + _PIN_ABS, "stage1_pin")
        base.ephemeral.append("stage1_pin")

        # Stage 2 -- least total value at the pinned primary, then lock it. Skipped
        # when nothing priced is spendable at all (an all-zero objective, which the
        # solvers reject). In epsilon-constraint mode stage 1 minimised gold under
        # `makespan <= time_cap` and that is the frontier's Pareto axis, so pin it
        # first: this stage may then only choose among equally cheap-to-buy plans.
        if any(base.total_value.values()):
            if time_cap is not None and not favour_passive:
                problem += (
                    base.gold <= (pulp.value(base.gold) or 0.0) * (1 + _PIN_REL) + _PIN_ABS,
                    "stage2_gold_pin",
                )
                base.ephemeral.append("stage2_gold_pin")
            problem.setObjective(base.total_value)
            problem.solve(solver)
            problem += (
                base.total_value <= (pulp.value(base.total_value) or 0.0) * (1 + _PIN_REL) + _PIN_ABS,
                "stage2_pin",
            )
            base.ephemeral.append("stage2_pin")
            # Lock the Pareto axis too, at whatever the least-value plan spends, so
            # stage 3 cannot trade gold for activity behind the value pin.
            if "stage2_gold_pin" not in base.ephemeral:
                problem += (
                    base.gold <= (pulp.value(base.gold) or 0.0) * (1 + _PIN_REL) + _PIN_ABS,
                    "stage2_gold_pin",
                )
                base.ephemeral.append("stage2_gold_pin")

        # Freeze the juice counts stage 2 settled on. They are decided by now (stage 2
        # is what weighs juice against everything else), and stage 3 may not move them
        # anyway -- its own pins forbid it. Nailing them down lets the solver's presolve
        # drop those integer columns, so stage 3 runs closer to the pure LP it was
        # before whole juices, which is most of the MIP's cost paid back. (The other
        # integer, ``days_available``, is not frozen -- it is a bound, not a decision
        # stage 2 weighs.) Only when
        # solving integer: with the continuous relaxation the values are fractional, and
        # fixing them to their rounded selves would force an integrality the sweep is
        # deliberately dropping for speed.
        if exact_integers:
            for item, var in base.pj.items():
                fixed = var == round(var.value() or 0.0)
                fixed.name = f"pj_fix_{item}"
                problem.addConstraint(fixed)
                base.ephemeral.append(fixed.name)

        # Stage 3 -- least activity, so no reported metric moves.
        problem.setObjective(
            pulp.lpSum(base.craft.values()) + pulp.lpSum(base.usage)
            + pulp.lpSum(base.opens) + pulp.lpSum(base.buy.values())
            + pulp.lpSum(base.pj.values())
            + (base.buy_cookie if base.buy_cookie is not None else 0)
        )
        problem.solve(solver)

        # Every pin above only narrows a problem that already had an optimum, so the
        # tower cannot make the demand unsatisfiable -- but it can still come back
        # Infeasible on numerics, when the solver cannot reproduce the previous
        # stage's optimum inside the pin's slack. That is survivable for an LP (the
        # last solution stays loaded, which is what _assert_optimal's docstring banks
        # on) but not for a MIP: HiGHS returns with *nothing* loaded, every value
        # reads back 0, and the readout below would invent an empty plan -- which,
        # at a 0 makespan and 0 gold, lands on the frontier as a bogus "fastest".
        # Recover the honest way: drop the refinement's pins and re-solve the primary
        # objective, which we solved successfully above. The plan loses only its
        # tie-break determinism, not its correctness.
        if pulp.LpStatus[problem.status] != "Optimal":
            for name in ("stage1_pin", "stage2_pin", "stage2_gold_pin"):
                if name in base.ephemeral:
                    problem.constraints.pop(name, None)
                    base.ephemeral.remove(name)
            problem.setObjective(primary)
            problem.solve(solver)
            if pulp.LpStatus[problem.status] != "Optimal":
                # Unpinned and still no solution: the cap itself is the problem, not
                # the refinement. Uncapped, that means the demand really cannot be
                # met, which _assert_optimal says properly (and second-opinions on
                # CBC first); capped, the sweep just loses this sample.
                if time_cap is None:
                    solver = _assert_optimal(problem, solver)
                else:
                    raise _UnmeetableCap(_minutes(time_cap))

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
    # Integer vars, but the solver returns them as floats a hair off (2.9999999996):
    # round, or the readout re-introduces the fractions the integrality removed.
    pumpkin_juices = {
        item: units
        for item, var in base.pj.items()
        if (units := round(var.value() or 0.0)) > 0
    }
    time, ap, ln = aggregate(draws)
    # Daily budgets (see _build_base): a plan spending N cookie sets, or N days'
    # worth of Grape Juice, has to wait out those days to collect them -- but the
    # actions themselves can be near-instant (a cookie burst is five minutes), so
    # the draws alone under-report the span. Charge that wait here: N of a daily
    # resource means N-1 days of waiting after the first day's, which is exactly
    # the floor `cookies <= makespan/day + 1` puts on the LP's own makespan.
    #
    # Derived from the plan's own tallies rather than read off ``base.makespan``,
    # which is only meaningful where the LP actually minimised it: under an
    # epsilon-constraint cap it minimises *gold* and the makespan variable is free
    # to park anywhere up to the cap, so reading it back would report the cap as
    # the plan's time.
    # Counted in WHOLE days, matching both the constraint's integer
    # ``days_available`` and the report's own ceil: a part set still costs a whole
    # one, and the day it is collected on is a whole day too. The epsilon absorbs
    # the solver returning an integer a hair over (7.0000000001), which would
    # otherwise ceil to 8 and charge a spurious extra day of waiting.
    budget_days = math.ceil(sum(d.cookies for d in draws) - UNIT_EPSILON)
    if base.grape_juices_per_day > 0:
        budget_days = max(budget_days, math.ceil(
            sum(d.gj for d in draws) / base.grape_juices_per_day - UNIT_EPSILON
        ))
    if budget_days > 1:
        time = max(time, timedelta(days=budget_days - 1))
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
        veggie_juices=sum(
            d.offer.consumes.get("veggie juice", 0.0) * d.units for d in draws
        ),
        bought=bought,
        gold=pulp.value(base.gold) or 0.0,
        # Opportunity cost of the free (banked + passive) stamina this plan burns,
        # valued at the Orange-Juice equivalent (see _build_base). 0 when the option
        # is off (``base.stamina_op is None``). Not part of ``gold`` (no real gold is
        # spent), but priced by ``plan_gold`` and netted into ``cost``.
        stamina_gold=(
            base.stamina_price * (base.stamina_op.value() or 0.0)
            if base.stamina_op is not None
            else 0.0
        ),
        pumpkin_juices=pumpkin_juices,
        pumpkin_gold=sum(pumpkin_juices.values()) * base.pj_price,
        # Read the credit off its own variable rather than recomputing
        # ``pj_gain * juices``: the two part company on the last juice, the one
        # allowed to overshoot the goal and earn only the slice up to it.
        pumpkin_credit={
            item: base.pj_credit[item].value() or 0.0 for item in pumpkin_juices
        },
        price_pumpkin_juice=base.price_pumpkin_juice,
        remainder=base.remainder,
        ascension=ascension_outcome,
        _origin=origin,
    )


def _ascension_outcome(base: _Base) -> AscensionOutcome:
    """Read the solved, round-and-fixed ``base`` into an :class:`AscensionOutcome`.

    Every step var is exactly 0 or 1 by this point (see
    :func:`_round_and_fix_ascension`), so a step counts as selected past 0.5.
    ``mastery_supply`` reports every surviving candidate's gross supply
    expression, including incidental production of one nothing pinned it to
    (a candidate produced only as a side effect of another item's crafting
    still earns real mastery progress -- deliberate, per the module docstring).

    ``masters``/``grandmasters`` are the *selected* steps -- they are what
    the AK ledger charges (``ak_from_steps``), and every threshold an item's
    supply happens to cross is worth selecting anyway (its AK is free), so
    selection tracks reality for those two. ``megas`` instead reports every
    **threshold crossing** -- items whose gross supply covers their full
    remaining distance to 1M (the sum of their remaining step deltas),
    mirroring :func:`ascension.advance`'s state evolution. Selection would
    badly under-report megas: the mega constraint/floors are satisfied the
    moment *enough* megas exist, so an item that crosses 1M purely as a side
    effect of the plan's mass production (ubiquitous on a fresh, low-mastery
    account) never gets its ``y_mega`` selected, yet it *is* a megamastery
    the climb earns and `mega_count` will count it next stage. A pinned
    ``y_mega`` always implies the crossing (its supply constraint forces the
    full remaining delta via the ordering links), so crossings are a
    superset of selections.
    """
    spec = base.ascension
    assert spec is not None

    def chosen(step: str) -> list[str]:
        return sorted(
            item
            for item, steps in base.ascend_y.items()
            if step in steps and (steps[step].value() or 0.0) > 0.5
        )

    masters = chosen("master")
    grandmasters = chosen("grandmaster")
    ak_from_steps = AK_PER_MASTER * len(masters) + AK_PER_GRANDMASTER * len(grandmasters)
    makespan_minutes = base.makespan.value() or 0.0
    ak_from_chores = spec.chore_rate_per_day * makespan_minutes / _MINUTES_PER_DAY
    mastery_supply = {
        item: value
        for item, expr in base.ascend_S.items()
        if (value := pulp.value(expr) or 0.0) > UNIT_EPSILON
    }
    megas = sorted(
        cand.item
        for cand in spec.candidates
        if cand.delta_mega is not None
        # round() to match ascension.advance, which advances progress by
        # round(supply): report exactly the crossings the state evolution
        # will count.
        and round(mastery_supply.get(cand.item, 0.0))
        >= (cand.delta_master or 0) + (cand.delta_grandmaster or 0) + cand.delta_mega
    )
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
    ap_tradeable: frozenset[str] = frozenset(),
    non_mastery_goals: frozenset[str] = frozenset(),
    no_buy: frozenset[str] = frozenset(),
    ascension: AscensionSpec | None = None,
    price_pumpkin_juice: bool = True,
    grape_juices_per_day: float = 0.0,
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

    **Daily budgets.** ``cookie_sets`` is the stock of sets held; ``grape_juices_
    per_day`` the farm's daily instant-grow allowance (``0`` = unconfigured, no cap).
    Both are once-a-*day*, whole-farm resources, so beyond the stock cap the plan may
    spend only what its **span** earns it -- see :func:`_build_base` for the pair of
    constraints and why a per-offer cap (the cookie burst's old ``max_units=1``)
    could not express it.

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
        grape_juices_per_day=grape_juices_per_day,
        ap_tradeable=ap_tradeable, non_mastery_goals=non_mastery_goals,
        no_buy=no_buy, ascension=ascension, price_pumpkin_juice=price_pumpkin_juice,
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
        "veggie juice": plan.veggie_juices,
    }


def plan_gold(plan: SourcingPlan, prices: dict[str, float]) -> float:
    """Total gold ``plan`` spends on tradable consumables.

    ``prices`` is the per-unit gold map from :func:`config.trading_prices`; the
    result is the dot product of that map with the plan's consumable tally, so
    plans can be ranked by real gold cost. Grape Juice, priced from its grape
    content (or 0 if no grape price is configured), is included like the rest.
    Pumpkin Juice has no self-sourced form, so its whole cost (``plan.pumpkin_gold``)
    is added on -- it only ever counts as bought, never made. The free-stamina
    opportunity cost (``plan.stamina_gold``) is deliberately **not** here: it is not
    a tradeable consumable's market value but a notional charge, carried instead by
    :attr:`SourcingPlan.cost` (net gold) and shown as its own report line.
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
    # Sorted, like every set this function walks: set order varies with
    # PYTHONHASHSEED, and it decides the LP's row order (so which of several
    # equally good allocations the solver returns) and how tied rows are listed.
    net = {
        item: (
            leftovers.get(item, 0.0)
            + (crafting_bonus * craft[item] if item in craft else 0)
            - pulp.lpSum(quantity * craft[j] for j, quantity in consumers.get(item, []))
        )
        for item in sorted(items)
    }
    for expr in net.values():
        problem += expr >= 0  # cannot consume more of a material than is available
    problem += pulp.lpSum(prices[item] * net[item] for item in sorted(sellable))
    problem.solve(_solver())
    if pulp.LpStatus[problem.status] != "Optimal":
        return []
    rows = [
        (item, sold, sold * prices[item])
        for item in sorted(sellable)
        if (sold := pulp.value(net[item]) or 0.0) >= 1
    ]
    rows.sort(key=lambda row: row[2], reverse=True)  # stable: ties stay A-Z
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
    grape_juices_per_day: float = 0.0,
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
        cookie_sets=cookie_sets, grape_juices_per_day=grape_juices_per_day,
        quantize=quantize,
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
    ap_tradeable: frozenset[str] = frozenset(),
    non_mastery_goals: frozenset[str] = frozenset(),
    no_buy: frozenset[str] = frozenset(),
    price_pumpkin_juice: bool = True,
    grape_juices_per_day: float = 0.0,
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
        cookie_sets=cookie_sets, grape_juices_per_day=grape_juices_per_day,
        pj_gain=pj_gain, pj_price=pj_price, pj_limit=pj_limit,
        quantize=quantize, ap_tradeable=ap_tradeable, non_mastery_goals=non_mastery_goals,
        no_buy=no_buy, price_pumpkin_juice=price_pumpkin_juice,
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


def _sweep(
    base: _Base, caps: list[float], favour_passive: bool = False,
    exact_integers: bool = True, *,
    finish: Callable[[SourcingPlan], None] | None = None,
    pool: ThreadPoolExecutor | None = None,
    make_base: Callable[[], _Base] | None = None,
) -> Callable[[], list[SourcingPlan]]:
    """Solve ``base`` at each of ``caps`` (seconds), dropping the unmeetable ones.

    A cap no plan in this regime can meet is a gap in the sample, not an error -- see
    :class:`_UnmeetableCap`. The remaining caps still describe the frontier; a regime
    where *every* cap is unmeetable simply contributes nothing, and the endpoints
    (solved uncapped) carry it.

    ``exact_integers`` is forwarded to every solve -- :func:`frontier` sweeps ``False`` (the
    cheap continuous relaxation) and finishes the survivors exact. ``finish`` runs on
    each plan as it is solved (``frontier``'s quantize-and-credit ``post``).

    Returns a function that waits for and returns the plans, in cap order. Given a
    ``pool`` and ``make_base`` (a factory for an identical copy of ``base``), the caps
    are split into contiguous runs solved concurrently -- the first on ``base``, each
    other on its own copy, since a base is one mutable LP -- while the caller carries
    on; HiGHS releases the GIL as it solves, so the runs overlap. Every capped solve
    is a cold solve of the same model whichever base it runs on, so the plans are
    the same either way.
    """
    def run(run_base: _Base, run_caps: list[float]) -> list[SourcingPlan]:
        plans = []
        for cap in run_caps:
            try:
                plan = _solve_configured(
                    run_base, favour_passive=favour_passive,
                    time_cap=timedelta(seconds=cap), exact_integers=exact_integers,
                )
            except _UnmeetableCap:
                continue
            if finish is not None:
                finish(plan)
            plans.append(plan)
        return plans

    if pool is None or make_base is None or len(caps) < 2:
        plans = run(base, caps)
        return lambda: plans
    runs = min(FRONTIER_WORKERS, len(caps))
    cuts = [len(caps) * i // runs for i in range(runs + 1)]
    futures = [pool.submit(run, base, caps[cuts[0]:cuts[1]])] + [
        pool.submit(lambda run_caps: run(make_base(), run_caps), caps[cuts[i]:cuts[i + 1]])
        for i in range(1, runs)
    ]
    return lambda: [plan for future in futures for plan in future.result()]


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


def _juice_count(plan: SourcingPlan) -> int:
    """A plan's whole Pumpkin Juice count (exact plans only -- relaxed plans carry
    a fractional sum that :func:`frontier` never rounds)."""
    return round(sum(plan.pumpkin_juices.values()))


def _pumpkin_gap_fill(
    base: _Base, lo_t: float, lo_pj: int, hi_t: float, hi_pj: int,
) -> dict[int, SourcingPlan]:
    """The fastest exact plan at each whole-juice count in ``[hi_pj, lo_pj)``,
    found by bisecting the time-cap axis between two bracket plans.

    Juice is the model's only integer *goal* decision (``days_available`` is a
    daily-budget bound, not a thing to trade off) and its count is monotone
    non-increasing in the time cap (each juice buys a mastery-tier slice of
    farming time), yet the sweep's *relaxed* count is an unreliable proxy for the
    exact one -- the integrality gap runs to a whole juice or two in the
    transition. So when the exact-finished frontier skips a count (a wall of
    3-juice fast plans jumping straight to the juice-free tail, ``3, 3, 2, 0``),
    the missing rows cannot be recovered from the relaxation; they must be pinned
    with real MIP solves. Bisection needs only a handful, and only here, where a
    gap actually exists (normal juice prices leave none). The lower bracket level
    ``hi_pj`` is included so its *fastest* representative surfaces too -- a fast
    juice-free plan distinct from the slow cheapest endpoint.
    """
    found: dict[int, SourcingPlan] = {}

    def at(cap: float) -> SourcingPlan:
        return _solve_configured(
            base, exact_integers=True, time_cap=timedelta(seconds=cap))

    def record(plan: SourcingPlan) -> int:
        level = _juice_count(plan)
        if hi_pj <= level < lo_pj:
            best = found.get(level)
            if best is None or plan.time < best.time:
                found[level] = plan
        return level

    def bisect(a: float, ka: int, b: float, kb: int, depth: int) -> None:
        if ka - kb <= 1 or b - a < 60 or depth > 24:
            return
        mid = (a + b) / 2
        plan = at(mid)
        km = record(plan)
        bisect(a, ka, mid, km, depth + 1)
        bisect(mid, km, b, kb, depth + 1)

    bisect(lo_t, lo_pj, hi_t, hi_pj, 0)
    return found


def _thin_pumpkin(plans: list[SourcingPlan], count: int) -> list[SourcingPlan]:
    """Thin an exact, fastest-first juice frontier to ``count`` rows, keeping both
    ends and the *fastest* plan at each distinct juice count.

    Unlike :func:`_thin`'s even time-spread, this spends the scarce rows on the
    decreasing-juice story: one representative per count, so redundant same-count
    plans give way to the missing intermediates (see :func:`_pumpkin_gap_fill`).
    """
    if len(plans) <= count:
        return plans
    keep = {0, len(plans) - 1}  # fastest (max juice) and cheapest (juice-free)
    seen: set[int] = set()
    for i, plan in enumerate(plans):  # fastest-first: the fastest plan per count
        if len(keep) >= count:
            break
        level = _juice_count(plan)
        if level not in seen:
            seen.add(level)
            keep.add(i)
    step = (len(plans) - 1) / (count - 1)
    for i in range(count):
        if len(keep) >= count:
            break
        keep.add(round(i * step))
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


# --- Spread engine: pin plans linearly along a caller-chosen axis --------------
#
# The default frontier spreads its shown plans evenly across *time* (`_thin`). That
# answers "what changes between fast and slow" but not "how should I spend my
# Pumpkin Juice" or "what does buying more speed cost me right now" -- both better
# read off a spread on the juice or gold axis instead. `SpreadSpec`/`frontier`'s
# `spread=` kwarg opt into that: `count` plans sampled linearly along one axis
# (`_spread`), plus `efficiency`/`spread_graph_series` as read-only helpers over
# whatever plan list is shown (spread or the default time-thin, either one).


class SpreadSpec(NamedTuple):
    """A request to spread a frontier's shown plans along ``axis`` instead of the
    default time-thin (see :func:`_spread`, :func:`frontier`'s ``spread=`` kwarg).

    ``lo``/``hi`` override that axis's default range; ``None`` picks the natural
    bound (the fast/cheap endpoints' own values, or -- for ``pumpkin-juice`` --
    ``[0, feasible max]``). Ignored for ``"passive"``, which enumerates breakpoints
    rather than sampling a range.
    """

    axis: str  # "time" | "gold" | "pumpkin-juice" | "passive"
    lo: float | None = None
    hi: float | None = None


class GraphSeries(NamedTuple):
    """The (x, y) points and axis labels for a spread's impact graph (see
    :func:`spread_graph_series`) -- what a renderer plots, terminal or web."""

    xs: list[float]
    ys: list[float]
    x_label: str
    y_label: str


# How many times :func:`_solve_at_axis_target` bisects the time-cap axis chasing a
# gold/juice target, and the bracket width (seconds) below which it stops --
# mirrors :func:`_pumpkin_gap_fill`'s own ``depth > 24`` / ``b - a < 60`` limits,
# since this generalizes that same search.
_SPREAD_BISECT_DEPTH = 24
_SPREAD_MIN_BRACKET = 60.0

_SPREAD_AXIS_LABELS = {
    "time": "Time",
    "gold": "Net gold",
    "pumpkin-juice": "Pumpkin Juice",
    "passive": "Time",
}
_SPREAD_METRIC_LABELS = {"gold": "Net gold", "time": "Time"}


def _axis_value(plan: SourcingPlan, axis: str) -> float:
    """``plan``'s value on a spread axis ("passive" has none of its own -- a
    passive spread reads its x-axis off "time" instead, see
    :func:`spread_graph_series`)."""
    if axis == "time":
        return plan.time.total_seconds()
    if axis == "gold":
        return plan.cost[1]
    if axis == "pumpkin-juice":
        return _juice_count(plan)
    raise ValueError(f"no axis value for {axis!r}")


def _linspace(lo: float, hi: float, count: int) -> list[float]:
    """``count`` values evenly spaced across ``[lo, hi]`` inclusive of both ends.

    ``count <= 1`` collapses to just ``lo`` -- the same "no step to divide by"
    guard :func:`_thin` uses for a one-plan request.
    """
    if count <= 1 or hi == lo:
        return [lo]
    return [lo + (hi - lo) * i / (count - 1) for i in range(count)]


def _solve_at_axis_target(
    base: _Base, axis: str, target: float, t_lo: float, t_hi: float,
) -> SourcingPlan:
    """The fastest exact plan whose ``axis`` value lands closest to ``target``,
    found within the time-cap range ``[t_lo, t_hi]`` -- for the ``"time"`` and
    ``"gold"`` axes only (``"pumpkin-juice"`` pins its juice variable directly, see
    :func:`_spread`).

    ``"time"`` is trivial: the cap *is* the target, simply clamped into range.
    ``"gold"`` net gold is monotone **non-increasing** in the time cap (it falls as
    a plan is given more time to self-source -- see :func:`frontier`'s module
    notes) but is not a clean LP expression to pin (surplus/stamina fold in after
    the solve), so this bisects the cap axis exactly like :func:`_pumpkin_gap_fill`
    brackets a juice count, generalized to any gold target. Every solve here is
    exact (``exact_integers=True``) -- unlike the dense sweep :func:`frontier` thins
    from, a spread only ever solves the handful of points it actually shows, so
    there is no relaxed-then-finished two-phase step to reuse.
    """
    if axis == "time":
        cap = min(max(target, t_lo), t_hi)
        return _solve_configured(base, exact_integers=True, time_cap=timedelta(seconds=cap))

    def at(cap: float) -> SourcingPlan:
        return _solve_configured(base, exact_integers=True, time_cap=timedelta(seconds=cap))

    a, b = t_lo, t_hi
    best = at(a)
    best_val = _axis_value(best, axis)
    hi_plan = at(b)
    hi_val = _axis_value(hi_plan, axis)
    if abs(hi_val - target) < abs(best_val - target):
        best, best_val = hi_plan, hi_val
    for _ in range(_SPREAD_BISECT_DEPTH):
        if b - a < _SPREAD_MIN_BRACKET or abs(best_val - target) < 1e-6:
            break
        mid = (a + b) / 2
        try:
            plan = at(mid)
        except _UnmeetableCap:
            # A gap in this regime's reachable caps (see _sweep) -- the bracket
            # can no longer narrow past it, so stop with the best point found.
            break
        val = _axis_value(plan, axis)
        if abs(val - target) < abs(best_val - target):
            best, best_val = plan, val
        # Both axes fall as the cap grows: above target, the answer needs more
        # time (raise the low bracket); below, it needs less (lower the high one).
        if val > target:
            a = mid
        else:
            b = mid
    return best


def _spread(
    base_active: _Base,
    fast: SourcingPlan,
    cheap: SourcingPlan,
    spec: SpreadSpec,
    count: int,
    demand: dict[str, float],
    recipes: dict[str, dict[str, float]] | None,
    crafting_bonus: float,
    openables: list[OpenFlow] | None,
    inventory: dict[str, float] | None,
    passive_pet_items: set[str],
    prices: dict[str, float] | None,
    quantize: Callable[[str, float], timedelta | None] | None,
    base_passive: _Base | None = None,
    probe: SourcingPlan | None = None,
) -> list[SourcingPlan]:
    """:func:`frontier`'s ``spread=`` path: ``count`` plans sampled linearly along
    ``spec.axis`` (fastest-first, deduped), in place of the default time-thin.
    Supersedes the default :func:`_pumpkin_gap_fill` gap-fill for this call --
    that mechanism exists to recover missing juice counts from a *time*-spread
    frontier, which spreading directly on the juice axis makes moot.

    ``fast``/``cheap`` are the two endpoints :func:`frontier` already built and
    quantized; re-solved here exact (see :func:`_solve_at_axis_target`'s note)
    when juice vars exist, so every returned plan -- endpoints included -- drinks
    a whole number of juices like the rest of the frontier's contract.

    ``base_passive``/``probe`` are supplied only for a ``passive`` spread (when
    surplus is priced): the ``favour_passive`` base and its uncapped probe plan
    that the passive breakpoints anchor on -- see that branch for why the cheap
    endpoint is the wrong anchor.
    """
    if _has_integers(base_active):
        if fast._origin is not None:
            base, kwargs = fast._origin
            fast = _solve_configured(base, exact_integers=True, **kwargs)
        if cheap._origin is not None:
            base, kwargs = cheap._origin
            cheap = _solve_configured(base, exact_integers=True, **kwargs)

    def post(plan: SourcingPlan) -> None:
        _quantize_plan_time(
            plan, quantize, demand, recipes or {}, crafting_bonus,
            openables or [], inventory or {},
        )
        plan.surplus_gold = plan_surplus_gold(
            plan, demand, recipes or {}, crafting_bonus, openables or [],
            inventory or {}, prices or {},
        )

    t_fast, t_cheap = fast.time.total_seconds(), cheap.time.total_seconds()

    if spec.axis == "pumpkin-juice":
        # Juice is the one spread axis that is itself an LP variable sum, so pin it
        # *directly* -- ``sum(pj) <= N`` (see _solve_configured's ``juice_cap``),
        # taking the fastest plan under it, one exact solve per target -- rather than
        # bisecting the time cap for a plan that happens to land on N juices. That
        # cap bisection only ever spanned ``t_fast..t_cheap`` (the *top* of the juice
        # range), so every target below the cheap endpoint's count collapsed onto it;
        # a direct cap reaches 0. The fast/cheap endpoints are also **not** seeded
        # here: every shown plan is a sampled target, so a ``5:18`` spread shows only
        # counts within [5, 18] and never leaks the 20-juice fastest plan in from an
        # unconditional endpoint.
        #
        # Objective per target: the *fastest* plan using at most N juices
        # (``gold_weight=_FAST_PRICE``) -- "if I ration myself to N juices, how fast
        # can I finish and what does it cost". These rows are **deliberately
        # dominated**: whenever juice is cheaper per unit than its substitute (the
        # usual case -- and exactly why the user rations it: the model prices juice
        # below the future tower levels they are saving it for), a lower-juice plan
        # is same-speed-or-slower *and* costlier, so :func:`_non_dominated` would
        # throw all but the top count away. So this branch skips that prune entirely
        # and returns one plan per distinct juice count, fastest-first -- the whole
        # point is to *see* the juice-vs-speed/gold trade the frontier hides.
        feasible_max = _juice_count(fast)
        lo = 0 if spec.lo is None else max(int(spec.lo), 0)
        hi = feasible_max if spec.hi is None else min(int(spec.hi), feasible_max)
        by_count: dict[int, SourcingPlan] = {}
        seen: set[int] = set()
        for target in _linspace(lo, hi, count):
            n = round(target)
            if n in seen:
                continue
            seen.add(n)
            plan = _solve_configured(
                base_active, gold_weight=_FAST_PRICE, exact_integers=True, juice_cap=n,
            )
            post(plan)
            by_count.setdefault(_juice_count(plan), plan)  # dedup by *actual* count
        return sorted(by_count.values(), key=lambda p: p.cost)
    elif spec.axis == "passive":
        plans = [fast, cheap]
        for plan in plans:
            post(plan)
        # No range to sample -- one plan per passive-completion breakpoint (see
        # :func:`_passive_completion_caps`). Anchor those breakpoints on the
        # ``favour_passive`` probe (built pet-free by :func:`frontier`, see there)
        # and solve each cap ``favour_passive`` too, not on the cheap-explore
        # endpoint through ``base_active``: when exploring is cheap the active base
        # never *chooses* to wait for passive output, so a base_active-solved
        # passive spread collapses to the single explore plan (the user's "why is
        # there no wait-it-out option?" bug). The pet-free probe leans on the real
        # non-pet passive plateaus, so its residuals expose every passive completion
        # as a "wait it out" milestone -- the levers the user boosts from. Falls
        # back to the cheap plan / base_active when no probe was built (surplus
        # unpriced, or demand unsatisfiable pet-free).
        if probe is not None and base_passive is not None:
            anchor, solve_base, fp = probe, base_passive, True
        else:
            anchor, solve_base, fp = cheap, base_active, False
        caps = _passive_completion_caps(
            anchor, demand, recipes or {}, crafting_bonus, openables or [],
            inventory or {}, passive_pet_items, quantize,
        )
        for cap in caps:
            try:
                plan = _solve_configured(
                    solve_base, favour_passive=fp, exact_integers=not fp,
                    time_cap=timedelta(seconds=cap),
                )
            except _UnmeetableCap:
                continue
            post(plan)
            plans.append(plan)
    else:  # "time" / "gold": bisect the time cap (net gold has no clean LP pin)
        plans = [fast, cheap]
        for plan in plans:
            post(plan)
        if spec.axis == "time":
            lo = t_fast if spec.lo is None else spec.lo
            hi = t_cheap if spec.hi is None else spec.hi
        else:  # "gold"
            lo = fast.cost[1] if spec.lo is None else spec.lo
            hi = cheap.cost[1] if spec.hi is None else spec.hi
        for target in _linspace(lo, hi, count):
            plan = _solve_at_axis_target(base_active, spec.axis, target, t_fast, t_cheap)
            post(plan)
            plans.append(plan)

    # Prune like the default frontier: drop dominated samples (a target that lands
    # on a slower-but-costlier point than another, and the endpoint/target pair that
    # coincide) so no row is beaten outright by another and the efficiency between
    # adjacent rows stays meaningful. Also dedups and sorts fastest-first.
    return _non_dominated(plans)


def efficiency(plans: list[SourcingPlan]) -> list[float | None]:
    """Gold per second of makespan saved going one row faster, per row (fastest
    first): row ``i`` vs the next *slower* row ``i+1`` is ``(net_gold[i] -
    net_gold[i+1]) / (time[i+1] - time[i])`` -- positive when the faster row costs
    more (the usual case, buying speed), the marginal price of shaving that time
    off. ``None`` for the last row (no slower neighbour to compare against) and
    for a tied time (would divide by zero -- two rows landing on the same
    makespan have no time saved to price).
    """
    result: list[float | None] = []
    for i, plan in enumerate(plans):
        if i + 1 >= len(plans):
            result.append(None)
            continue
        nxt = plans[i + 1]
        dt = nxt.time.total_seconds() - plan.time.total_seconds()
        if dt <= 0:
            result.append(None)
            continue
        result.append((plan.cost[1] - nxt.cost[1]) / dt)
    return result


def spread_graph_series(
    plans: list[SourcingPlan], axis: str, metric: str,
) -> GraphSeries:
    """The impact-graph series for ``plans``: ``y`` (``metric``) against each
    plan's value on ``axis``, for a renderer to plot (terminal ASCII or a web SVG).

    ``metric`` is ``"gold"``, ``"time"``, or ``"auto"`` -- the *other* of the two,
    since plotting an axis against itself is a straight line: gold, unless the
    spread axis already is gold, in which case time. A ``"passive"`` spread has no
    axis value of its own, so its x is each plan's time, like the "time" axis.
    """
    if metric == "auto":
        metric = "time" if axis == "gold" else "gold"
    x_axis = "time" if axis == "passive" else axis
    xs = [_axis_value(p, x_axis) for p in plans]
    ys = [_axis_value(p, metric) for p in plans]
    return GraphSeries(
        xs=xs, ys=ys,
        x_label=_SPREAD_AXIS_LABELS[axis], y_label=_SPREAD_METRIC_LABELS[metric],
    )


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
    ap_tradeable: frozenset[str] = frozenset(),
    non_mastery_goals: frozenset[str] = frozenset(),
    no_buy: frozenset[str] = frozenset(),
    spread: SpreadSpec | None = None,
    price_pumpkin_juice: bool = True,
    grape_juices_per_day: float = 0.0,
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

    ``spread`` (default ``None``, byte-identical to the behaviour above) opts out of
    all of it: instead of the time-thinned sweep, :func:`_spread` pins ``count``
    plans sampled linearly along ``spread.axis`` (time / gold / whole Pumpkin Juice
    / a passive-completion breakpoint), still fastest-first, deduped, with the fast
    and cheap endpoints always included.
    """
    base_active = _build_base(
        offers, demand, recipes, crafting_bonus, openables, inventory,
        cookie_sets, prices, pj_gain, pj_price, pj_limit,
        grape_juices_per_day=grape_juices_per_day,
        ap_tradeable=ap_tradeable, non_mastery_goals=non_mastery_goals,
        no_buy=no_buy, price_pumpkin_juice=price_pumpkin_juice,
    )
    fast = _solve_configured(base_active, gold_weight=_FAST_PRICE, exact_integers=False)
    cheap = _solve_configured(base_active, gold_weight=_FREE_PRICE, exact_integers=False)
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

    if spread is not None:
        # A passive spread anchors on the favour_passive probe (see _spread's
        # passive branch); build it here, mirroring the slow-regime extension
        # below. Only when surplus is priced -- else banking is worthless and the
        # slow passive plans it would surface are all dominated.
        base_passive = probe = None
        if spread.axis == "passive" and prices:
            # Anchor the passive breakpoints on a favour_passive probe -- but built
            # over a passive bundle with the *pet-dominated* items removed (their
            # yields dropped from the one aggregate passive offer). Without that the
            # probe leans on the ultra-slow pet trickle for exactly those items
            # (Purple Flower/Twine here, a 200-year tail), which is the tail
            # _passive_completion_caps already excludes -- so every residual it
            # returns is a pet item, the caps come back empty, and the spread
            # collapses to the single explore plan. Dropping the pet supply forces
            # the probe onto the real non-pet plateaus (wait for Straw, craft Twine),
            # whose completions are the "wait it out" milestones the user boosts
            # from. If that leaves the probe infeasible (an item *only* a pet makes),
            # fall back to no probe -> _spread uses the cheap/base_active anchor.
            probe_offers = [
                replace(o, yields={
                    k: v for k, v in o.yields.items() if k not in passive_pet_items
                })
                if o.track == PASSIVE_TRACK and passive_pet_items else o
                for o in offers
            ]
            base_passive = _build_base(
                probe_offers, demand, recipes, crafting_bonus, openables, inventory,
                cookie_sets, prices, pj_gain, pj_price, pj_limit,
                grape_juices_per_day=grape_juices_per_day,
                ap_tradeable=ap_tradeable, non_mastery_goals=non_mastery_goals,
                no_buy=no_buy, price_pumpkin_juice=price_pumpkin_juice,
            )
            try:
                probe = _solve_configured(
                    base_passive, favour_passive=True, exact_integers=False,
                )
            except ValueError:  # _assert_optimal: demand unsatisfiable pet-free
                base_passive = probe = None
        return _spread(
            base_active, fast, cheap, spread, count, demand, recipes,
            crafting_bonus, openables, inventory, passive_pet_items, prices,
            quantize, base_passive=base_passive, probe=probe,
        )

    # Quantize every plan's reported time (idempotent -- fast/cheap are quantized
    # a second time here harmlessly) so `_non_dominated`/`_thin`/dedup rank on
    # real deposit-boundary times: two epsilon-cap plans that landed inside the
    # same deposit window now collapse to equal (quantized) time, and the
    # lower-gold one dominates. Then credit each plan for the surplus it banks
    # (see :func:`plan_surplus_gold`), so its ``cost`` gold is net of what it can
    # sell -- a slow, passive-leaning plan dips below zero and the frontier
    # extends past the "buy nothing" line. Both done before pruning:
    # ``_non_dominated`` and ``_thin`` both rank by the net cost. Run on each sweep
    # plan as it is solved (on the sweep's thread), on the endpoints below.
    def post(plan: SourcingPlan) -> None:
        _quantize_plan_time(
            plan, quantize, demand, recipes or {}, crafting_bonus, openables or [], inventory or {},
        )
        plan.surplus_gold = plan_surplus_gold(
            plan, demand, recipes or {}, crafting_bonus, openables or [],
            inventory or {}, prices or {},
        )

    def make_base() -> _Base:
        # An identical copy of base_active/base_passive (built from the same
        # inputs), for a sweep run on another thread -- see _sweep.
        return _build_base(
            offers, demand, recipes, crafting_bonus, openables, inventory,
            cookie_sets, prices, pj_gain, pj_price, pj_limit,
            grape_juices_per_day=grape_juices_per_day,
            ap_tradeable=ap_tradeable, non_mastery_goals=non_mastery_goals,
            no_buy=no_buy, price_pumpkin_juice=price_pumpkin_juice,
        )

    with ThreadPoolExecutor(max_workers=FRONTIER_WORKERS) as pool:
        return _frontier_sweeps(
            pool, make_base, post, base_active, fast, cheap, t_cheap_raw, count,
            demand, recipes, crafting_bonus, openables, inventory,
            passive_pet_items, prices, quantize,
        )


def _frontier_sweeps(
    pool: ThreadPoolExecutor,
    make_base: Callable[[], _Base],
    post: Callable[[SourcingPlan], None],
    base_active: _Base,
    fast: SourcingPlan,
    cheap: SourcingPlan,
    t_cheap_raw: float,
    count: int,
    demand: dict[str, float],
    recipes: dict[str, dict[str, float]] | None,
    crafting_bonus: float,
    openables: list[OpenFlow] | None,
    inventory: dict[str, float] | None,
    passive_pet_items: set[str],
    prices: dict[str, float] | None,
    quantize: Callable[[str, float], timedelta | None] | None,
) -> list[SourcingPlan]:
    """:func:`frontier` past its endpoints: the two sweeps, thinning, and the exact
    finish -- the independent solves spread over ``pool`` (see :func:`_sweep`)."""
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
    # Started, not awaited: the slow regime's probe below runs meanwhile.
    collect_active = _sweep(
        base_active, caps, exact_integers=False,
        finish=post, pool=pool, make_base=make_base,
    )
    collect_passive = None

    # Slow regime: extend past the cheap endpoint with favour_passive plans that lean
    # ever harder on passive/farming and bank the surplus. Bounded at t_bound, the
    # slowest non-pet passive completion, found from one uncapped probe -- its own
    # makespan would chase the pet tail (centuries), but its non-pet breakpoints are
    # the real completions. Only worthwhile when surplus is priced (else banking is
    # worth nothing and these slower plans are dominated).
    anchors = list(breakpoints)
    if prices:
        base_passive = make_base()
        probe = _solve_configured(base_passive, favour_passive=True, exact_integers=False)
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
            collect_passive = _sweep(
                base_passive, ext_caps, favour_passive=True, exact_integers=False,
                finish=post, pool=pool, make_base=make_base,
            )

    post(fast)
    post(cheap)
    plans += collect_active()
    if collect_passive is not None:
        plans += collect_passive()
    thinned = _thin(_non_dominated(plans), count, tuple(anchors))

    # A MIP costs ~50x an LP per solve (see :func:`_solve_configured`), and the model
    # has integer variables whenever juice is on or a daily budget binds. The whole
    # sweep above therefore ran the cheap *continuous* relaxation, letting juice and
    # the day count go fractional; only the handful
    # of plans that survive thinning -- the ones actually shown, each row of the
    # frontier table and the expanded plan -- are re-solved exact here, from the
    # ``(base, kwargs)`` each was stamped with. ~5 MIP solves instead of hundreds. The
    # exact plan for a point can differ from its relaxed row by up to a juice's worth of
    # gold/time; that shifts interior *ranking* slightly (already-thinned, so harmless)
    # but every returned plan is a real whole-juice plan. Skipped entirely when the
    # model has no integer variable at all -> the relaxation was already exact.
    if _has_integers(base_active):
        # The plans are re-solved concurrently: the first plan from each base on
        # that base, any other on a fresh copy of it (a base is one mutable LP) --
        # the same cold solve of the same model either way. A hand-built/
        # remainder-only plan has nothing to re-solve.
        finished = list(thinned)
        claimed: set[int] = set()

        def finish_exact(index: int, base: _Base | None) -> None:
            kwargs = thinned[index]._origin[1]
            if base is None:
                base = make_base()
            exact = _solve_configured(base, exact_integers=True, **kwargs)
            post(exact)
            finished[index] = exact

        futures = []
        for index, plan in enumerate(thinned):
            if plan._origin is None:
                continue
            origin = plan._origin[0]
            reuse = id(origin) not in claimed
            claimed.add(id(origin))
            futures.append(pool.submit(finish_exact, index, origin if reuse else None))
        for future in futures:
            future.result()
        # Two survivors can land on the same integer point once juice is whole; drop the
        # duplicate rows, keeping the first (fastest-first order is preserved).
        seen: set[tuple[int, int]] = set()
        deduped = []
        for plan in finished:
            key = (round(plan.time.total_seconds()), round(plan.gold - plan.surplus_gold))
            if key not in seen:
                seen.add(key)
                deduped.append(plan)
        thinned = deduped

        # A steep juice price makes the exact frontier jump counts (``3, 3, 2, 0``):
        # the relaxation under-counted juice, so thinning kept no plan at the skipped
        # counts and none can be recovered from it. Bisect the cap axis for the
        # missing whole-juice plans (see :func:`_pumpkin_gap_fill`) and re-thin,
        # spending the redundant same-count rows on the decreasing-juice curve. Gated
        # on there being a spare row to show them in (distinct counts below ``count``)
        # so the common case -- juice cheap enough that every row already carries a
        # different count -- does no extra solves and is byte-identical to no gap-fill.
        distinct = len({_juice_count(p) for p in thinned})
        if count >= 3 and len(thinned) >= 2 and distinct < count:
            extra: list[SourcingPlan] = []
            for lo, hi in pairwise(thinned):
                lo_pj, hi_pj = _juice_count(lo), _juice_count(hi)
                if lo_pj - hi_pj >= 2:
                    fill = _pumpkin_gap_fill(
                        base_active, lo.time.total_seconds(), lo_pj,
                        hi.time.total_seconds(), hi_pj,
                    )
                    extra.extend(fill.values())
            if extra:
                for plan in extra:
                    post(plan)
                merged: dict[tuple[int, int], SourcingPlan] = {}
                for plan in sorted(thinned + extra, key=lambda p: p.cost):
                    key = (
                        round(plan.time.total_seconds()),
                        round(plan.gold - plan.surplus_gold),
                    )
                    merged.setdefault(key, plan)
                thinned = _thin_pumpkin(list(merged.values()), count)

    return thinned
