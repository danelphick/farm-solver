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
from dataclasses import dataclass, field
from datetime import timedelta
from typing import NamedTuple

import pulp

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

# Consumables that can be bought for gold instead of self-sourced (the items with
# a player-to-player gold price). Cookie sets are buyable too, but handled apart
# (they are a farming cap, not a balance item). Grape Juice is deliberately absent:
# it is not traded, only pressed from grapes, so it is always self-sourced.
BUYABLE = frozenset({"arnold palmer", "large net", "apple cider", "orange juice"})

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
) -> list[Offer]:
    """Every production offer across the channels, ready for the optimiser.

    Consumables are always **grounded**: the exploring/fishing/farming offers
    consume real Arnold Palmers / Large Nets / Grape Juice the plan must craft,
    hold, or *buy* (see :func:`minimize`), and the Apple Cider exploring channel
    (fed by stamina from drinking Orange Juice or eating Apples) is always
    available. Whether a
    consumable is self-sourced (costs time) or bought (costs gold) is the LP's
    call, set by the gold budget the frontier sweeps.
    """
    return [
        *passive_offers(production),
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
) -> set[str]:
    """Every item that can be produced: gathered, crafted, opened, or bought.

    A fixed point: an offer's yields are obtainable once its ``consumes`` inputs
    all are (an offer with no inputs is unconditionally so); a craftable item once
    its materials all are; an openable's outputs once its consumed items (the
    openable and its key) all are. A ``buyable`` consumable is unconditionally
    obtainable -- it can always be bought for gold, so a channel grounded in one is
    usable even where nothing crafts or gathers it. So an item only an *unusable*
    offer could make -- one whose input can never be sourced -- is correctly *not*
    obtainable, and a demand for it falls to the remainder rather than an infeasible
    LP. Auto-supplied materials (iron, nails) are expected stripped from ``recipes``.
    """
    obtainable: set[str] = set(buyable)
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
    per-unit gold from :func:`config.trading_prices`; absent, nothing is buyable and
    the plan is fully self-sourced.

    **Pumpkin Juice.** ``pj_gain`` (lower-cased goal item -> units of demand one juice
    covers, ``0.1 * mastery_threshold / mastery_bonus``) turns on the mastery-boost
    lever: a ``pj`` variable per goal item adds free, instant supply toward that item's
    demand at ``pj_price`` gold each, capped at fully covering the demand (a juice past
    the goal buys nothing). ``pj_limit`` caps the total juices spent. Like buying it is
    a gold-for-time trade, so it is off in ``favour_passive`` mode (the buy-nothing
    slow regime); its gold folds into ``gold``, tracked apart on the plan.
    """
    recipes = recipes or {}
    openables = openables or []
    inventory = inventory or {}
    prices = prices or {}
    pj_gain = pj_gain or {}
    wanted = {item.lower(): (item, qty) for item, qty in demand.items() if qty > 0}

    # A buyable consumable can always be bought, so it is obtainable even where
    # nothing crafts or gathers it (a channel grounded in one is then still usable).
    buyable = {item for item in BUYABLE if prices.get(item, 0.0) > 0}
    obtainable = _obtainable(offers, recipes, openables, buyable)
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
    items = _reachable(
        {k for k in wanted if k in obtainable}, recipes, craftable, openers,
        offer_inputs,
    )

    remainder = {
        display: round(qty)
        for key, (display, qty) in wanted.items()
        if key not in obtainable
    }
    if not items:
        return SourcingPlan(remainder=remainder)

    used = [o for o in usable if any(o.yield_of(i) > 0 for i in items)]
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

    # Cookie-boosted farming days used, capped below at held + bought sets. The
    # other consumable tallies (Arnold Palmers, Large Nets, ...) are read off the
    # solved draws in the plan built at the end, not needed as LP expressions here.
    cookies = pulp.lpSum(o.cookies * x for o, x in zip(used, usage) if o.cookies)

    # Buy variables: a buyable consumable can be bought (free, instant supply) for
    # gold instead of self-sourced (which costs makespan). Only reachable, priced
    # ones get a variable; cookie sets are bought by lifting their cap below.
    buy = {
        item: pulp.LpVariable(f"b{i}", lowBound=0)
        for i, item in enumerate(sorted(items & BUYABLE))
        if prices.get(item, 0.0) > 0
    }
    price_cookie = prices.get("cookie set", 0.0)
    buy_cookie = pulp.LpVariable("bcookie", lowBound=0) if price_cookie > 0 else None
    # Held cookie sets are free; beyond them a purchase lifts the cap at gold cost.
    problem += cookies <= cookie_sets + (buy_cookie if buy_cookie is not None else 0)
    gold = pulp.lpSum(prices[item] * var for item, var in buy.items())
    if buy_cookie is not None:
        gold = gold + price_cookie * buy_cookie

    # Pumpkin Juice: a bought mastery boost, one variable per reachable goal item.
    # Each juice on item i adds pj_gain[i] units toward its demand for pj_price gold;
    # capped below at fully covering the goal (a juice past it buys nothing). It is a
    # gold-for-time trade like buying, so it is off in the buy-nothing favour_passive
    # regime. Its gold folds into the same ``gold`` the LP minimises.
    pj = {}
    if pj_gain and pj_price > 0 and not favour_passive:
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
        )
        * x
        for o, x in zip(used, usage)
    ) + cookies * prices.get("cookie set", 0.0)

    if favour_passive:
        # Slowest, most passive-leaning end: fewest tools spent (so passive and
        # farming carry the demand), makespan bounded by ``time_cap`` so it cannot
        # run away onto a centuries-long pet trickle.
        if time_cap is not None:
            problem += makespan <= _minutes(time_cap)
        problem += spend_value
    elif time_cap is not None:
        problem += makespan <= _minutes(time_cap)
        problem += gold  # least gold under the time cap (epsilon-constraint)
    else:
        # A tiny gold_weight buys freely for the fastest plan; a huge one buys
        # nothing (gold 0) for the slowest, fully self-sourced plan.
        problem += makespan + gold_weight * gold

    # Net production of each item covers its demand:
    # gathered + crafted + opened - consumed-by-crafts - consumed-by-opens.
    # Iterate in a fixed (sorted) order: ``items`` is a set, whose iteration
    # order varies with PYTHONHASHSEED across processes, and that is the order
    # constraints enter the LP. The two refinement solves below pin every
    # *reported* metric, but plans identical on all of them can still differ in
    # which equivalent draws fill the same time/activity (grow crop A vs crop B);
    # constraint order is what CBC breaks those ties by, so a fixed order keeps
    # the printed plan reproducible run to run.
    for item in sorted(items):
        gathered = pulp.lpSum(
            o.yield_of(item) * x for o, x in zip(used, usage) if o.yield_of(item) > 0
        )
        # What offers use up as inputs (Arnold Palmers an explore burns, the
        # stamina and Apple Cider a cider explore spends) -- these must be
        # sourced like any other need.
        used_up = pulp.lpSum(
            o.consumes[item] * x
            for o, x in zip(used, usage)
            if o.consumes.get(item, 0.0) > 0
        )
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

    # Makespan: each track's summed time is at most T (tracks run concurrently).
    tracks: dict[str, list] = {}
    for offer, x in zip(used, usage):
        tracks.setdefault(offer.track, []).append(_minutes(offer.time_per_unit) * x)
    for terms in tracks.values():
        problem += pulp.lpSum(terms) <= makespan

    problem.solve(_solver())

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
    if used or craft or opens or buy or buy_cookie is not None or pj:
        if favour_passive:
            sv_star = pulp.value(spend_value) or 0.0
            problem += spend_value <= sv_star * (1 + 1e-9) + 1e-9
        else:
            t_star = makespan.value() or 0.0
            problem += makespan <= t_star * (1 + 1e-9) + 1e-9

        # Stage 2 -- least gold at the pinned primary, then lock it. Skipped when
        # nothing is buyable (gold is a constant 0, an empty objective CBC rejects).
        if buy or buy_cookie is not None or pj:
            problem.setObjective(gold)
            problem.solve(_solver())
            problem += gold <= (pulp.value(gold) or 0.0) * (1 + 1e-9) + 1e-9

        # Stage 3 -- least activity, so no reported metric moves.
        problem.setObjective(
            pulp.lpSum(craft.values()) + pulp.lpSum(usage) + pulp.lpSum(opens)
            + pulp.lpSum(buy.values()) + pulp.lpSum(pj.values())
            + (buy_cookie if buy_cookie is not None else 0)
        )
        problem.solve(_solver())

    draws = [
        Draw(offer, units)
        for offer, x in zip(used, usage)
        if (units := x.value() or 0.0) > UNIT_EPSILON
    ]
    crafts = {
        item: count
        for item in crafted
        if (count := craft[item].value() or 0.0) > UNIT_EPSILON
    }
    opens_done = {
        flow.name: count
        for flow, o in zip(opened, opens)
        if (count := o.value() or 0.0) > UNIT_EPSILON
    }
    bought = {
        item: units
        for item, var in buy.items()
        if (units := var.value() or 0.0) > UNIT_EPSILON
    }
    if buy_cookie is not None and (bc := buy_cookie.value() or 0.0) > UNIT_EPSILON:
        bought["cookie set"] = bc
    pumpkin_juices = {
        item: units
        for item, var in pj.items()
        if (units := var.value() or 0.0) > UNIT_EPSILON
    }
    time, ap, ln = aggregate(draws)
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
        bought=bought,
        gold=pulp.value(gold) or 0.0,
        pumpkin_juices=pumpkin_juices,
        pumpkin_gold=sum(pumpkin_juices.values()) * pj_price,
        pumpkin_credit={
            item: pj_gain[item] * units for item, units in pumpkin_juices.items()
        },
        remainder=remainder,
    )


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
) -> SourcingPlan:
    """The cheap endpoint: the fastest plan that buys nothing (fully self-sourced).

    Solved with the buy-nothing weight (``_FREE_PRICE``). It is both the fast
    regime's slow end and the **stable reference** for each passive item's *full*
    requirement (see :func:`passive_residuals` and :func:`main.run`): it self-sources
    with a moderate strategy, unlike the passive-leaning extension past it -- which
    can source cider stamina from Apples rather than Orange Juice and so understate
    the Orange requirement -- and unlike the fast plans, which over-buy net materials
    like Antler. Recomputed rather than read off the thinned frontier, where it may
    not survive as an interior point.
    """
    return minimize(
        offers, demand, recipes, crafting_bonus, prices=prices,
        gold_weight=_FREE_PRICE, openables=openables, inventory=inventory,
        cookie_sets=cookie_sets,
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
    """Total gold ``plan``'s banked surplus is worth.

    The sum of :func:`surplus_sale_value` over the plan's :func:`plan_leftovers` --
    the sale value of everything the plan produces past all it consumes, as sellable
    goods crafted from the leftovers. This is the credit :attr:`SourcingPlan.cost`
    subtracts from bought gold, so :func:`frontier` sets it on every plan and the
    gold axis can dip below zero for one that banks more than it buys. Zero when
    nothing is priced or nothing is left over.
    """
    leftovers = plan_leftovers(
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
) -> list[float]:
    """Makespan seconds at which each passively-made item finishes, in ``plan``.

    Each item finishes after ``residual / rate`` (see :func:`passive_residuals`) --
    the natural plateaus of the frontier: cap the makespan at one and every slower
    item must be sped up with consumables while that item, and all faster ones,
    still come for free. Items in ``exclude`` -- those a pet mostly supplies, a
    slow last-resort trickle rather than a real gathering plateau -- are skipped.
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
        if rate > 0:
            caps.append(residual / rate * 60.0)  # rate is per minute -> seconds
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

    ``prices`` (per-unit gold, from :func:`config.trading_prices`) is what makes a
    consumable buyable and banked surplus sellable; without it nothing can be bought,
    the slow regime is skipped, and the frontier is the single self-sourced plan.
    """
    fast = minimize(
        offers, demand, recipes, crafting_bonus, prices=prices,
        gold_weight=_FAST_PRICE, openables=openables, inventory=inventory,
        cookie_sets=cookie_sets, pj_gain=pj_gain, pj_price=pj_price, pj_limit=pj_limit,
    )
    cheap = minimize(
        offers, demand, recipes, crafting_bonus, prices=prices,
        gold_weight=_FREE_PRICE, openables=openables, inventory=inventory,
        cookie_sets=cookie_sets, pj_gain=pj_gain, pj_price=pj_price, pj_limit=pj_limit,
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
            inventory or {}, passive_pet_items,
        )
        if lo < cap < hi
    ]
    caps += breakpoints
    plans += [
        minimize(
            offers, demand, recipes, crafting_bonus, prices=prices,
            time_cap=timedelta(seconds=cap), openables=openables,
            inventory=inventory, cookie_sets=cookie_sets,
            pj_gain=pj_gain, pj_price=pj_price, pj_limit=pj_limit,
        )
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
        probe = minimize(
            offers, demand, recipes, crafting_bonus, prices=prices,
            favour_passive=True, openables=openables, inventory=inventory,
            cookie_sets=cookie_sets,
        )
        nonpet = _passive_completion_caps(
            probe, demand, recipes or {}, crafting_bonus, openables or [],
            inventory or {}, passive_pet_items,
        )
        t_bound = max(nonpet, default=t_cheap)
        if t_bound > t_cheap * (1 + _CAP_MARGIN):
            ext_lo, ext_hi = t_cheap * (1 + _CAP_MARGIN), t_bound * (1 - _CAP_MARGIN)
            ext_breaks = [cap for cap in nonpet if ext_lo < cap < ext_hi]
            anchors += ext_breaks
            ext_caps = _time_caps(t_cheap, t_bound, samples) + ext_breaks + [t_bound]
            plans += [
                minimize(
                    offers, demand, recipes, crafting_bonus, prices=prices,
                    favour_passive=True, time_cap=timedelta(seconds=cap),
                    openables=openables, inventory=inventory, cookie_sets=cookie_sets,
                )
                for cap in ext_caps
            ]

    # Credit each plan for the surplus it banks (see :func:`plan_surplus_gold`), so
    # its ``cost`` gold is net of what it can sell -- a slow, passive-leaning plan
    # dips below zero and the frontier extends past the "buy nothing" line. Done
    # before pruning: ``_non_dominated`` and ``_thin`` both rank by the net cost.
    for plan in plans:
        plan.surplus_gold = plan_surplus_gold(
            plan, demand, recipes or {}, crafting_bonus, openables or [],
            inventory or {}, prices or {},
        )
    return _thin(_non_dominated(plans), count, tuple(anchors))
