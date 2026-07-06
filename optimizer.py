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
    INVENTORY,
    PASSIVE_TRACK,
    UNLIMITED,
    Draw,
    Offer,
    OpenFlow,
    aggregate,
    cider_offers,
    drink_offers,
    exploring_offers,
    farming_offers,
    fishing_offers,
    passive_offers,
)

# Usage below this many units is treated as zero (CBC leaves tiny residuals).
UNIT_EPSILON = 1e-6

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


def all_offers(
    production: PassiveProduction,
    fishing: Fishing,
    exploring: Exploring,
    farm: Farm,
    mode: str = UNLIMITED,
) -> list[Offer]:
    """Every production offer across the channels, ready for the optimiser.

    In the ``INVENTORY`` mode the exploring/fishing/farming offers are *grounded*
    -- they consume real Arnold Palmers / Large Nets / Grape Juice the plan must
    craft or hold -- and the Apple Cider exploring channel (fed by stamina from
    drinking Orange Juice) is added. In ``UNLIMITED`` mode those consumables stay
    abstract, unbounded costs (the original behaviour).
    """
    ground = mode == INVENTORY
    offers = [
        *passive_offers(production),
        *fishing_offers(fishing, ground=ground),
        *exploring_offers(exploring, ground=ground),
        *farming_offers(farm, ground=ground),
    ]
    if ground:
        offers += cider_offers(exploring)
        offers += drink_offers()
    return offers


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
    def cost(self) -> tuple[float, float, float, float, float, float]:
        """The objective ``(time, AP, LN, cookie sets, GJ, cider)`` for comparisons.

        Cookie sets, Grape Juices and Apple Ciders are scarce resources traded
        against time like the consumables, so they belong in the Pareto cost --
        otherwise a slower plan that conserves them would look dominated by a
        faster one that spends them, and get pruned from the frontier. Apple
        Ciders are only spent in the inventory plan mode (zero otherwise).
        """
        return (
            self.time.total_seconds(),
            self.arnold_palmers,
            self.large_nets,
            self.cookie_sets,
            self.grape_juices,
            self.apple_ciders,
        )


def _minutes(time: timedelta) -> float:
    return time.total_seconds() / 60


def _obtainable(
    offers: list[Offer],
    recipes: dict[str, dict[str, float]],
    openables: list[OpenFlow],
) -> set[str]:
    """Every item that can be produced: gathered, crafted, or opened from such.

    A fixed point: an offer's yields are obtainable once its ``consumes`` inputs
    all are (an offer with no inputs is unconditionally so); a craftable item once
    its materials all are; an openable's outputs once its consumed items (the
    openable and its key) all are. So an item only an *unusable* offer could make
    -- one whose input can never be sourced -- is correctly *not* obtainable, and
    a demand for it falls to the remainder rather than an infeasible LP.
    Auto-supplied materials (iron, nails) are expected stripped from ``recipes``.
    """
    obtainable: set[str] = set()
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
    price_ap: float = 0.0,
    price_ln: float = 0.0,
    price_cookie: float = 0.0,
    price_gj: float = 0.0,
    price_cider: float = 0.0,
    price_oj: float = 0.0,
    time_cap: timedelta | None = None,
    openables: list[OpenFlow] | None = None,
    inventory: dict[str, float] | None = None,
    cookie_sets: float = 0.0,
) -> SourcingPlan:
    """Solve one LP: cover ``demand`` at least cost for the given prices.

    ``recipes`` maps a lower-cased craftable item to its ``{material: quantity}``
    (auto-supplied materials already stripped); one craft consumes those and
    yields ``crafting_bonus`` of the item. ``openables`` are Locksmith flows:
    opening one consumes its token (and key) and yields its average bundle, at no
    time or consumable cost -- a free multi-output flow the LP drives like crafts.
    Net production (gathered + crafted + opened - consumed) must cover ``demand``
    for every obtainable item, and each track's time sums to at most the makespan
    ``T``. A demanded item that is neither gatherable, craftable nor openable
    lands in the ``remainder``.

    ``inventory`` (lower-cased item -> quantity held) is free supply the plan need
    not produce: it is credited to every item's balance *except* those directly
    demanded, since the demand is already net of what is held (mastery progress or
    the acquired count). So an intermediate material already in stock -- Red
    Shields to craft Frost Shields, say -- is drawn from inventory rather than
    gathered or opened for anew. The credit is a constant, so surplus stock simply
    leaves the balance slack; it never makes an otherwise-unproducible item
    obtainable (that could render the LP infeasible once stock ran out).

    ``cookie_sets`` caps how many cookie-boosted farming days the plan may use
    (see :func:`~sources.farming_offers`): the total of every cookie offer's usage
    is held at or below it. Cookie sets and Grape Juices are scarce resources that
    only shorten the farming track, so -- like Arnold Palmers and Large Nets --
    they are priced (``price_cookie``, ``price_gj``) rather than spent freely: a
    tiny price uses them wherever they cut the makespan (never gratuitously, since
    a use that saves no time is a net cost), a large price avoids them for a
    slower, thriftier plan. The frontier sweeps these prices to offer plans that
    spend more or fewer of them (see :func:`frontier`).

    Two modes trace the frontier from different sides. By default the LP minimises
    ``T + sum(price_i * consumable_i)`` (a weighted sum). With ``time_cap`` set it
    instead minimises just the price-weighted consumables ``sum(price_i *
    consumable_i)`` subject to ``T <= time_cap`` -- an epsilon-constraint solve,
    which unlike the weighted sum can land on the *interior* of a flat frontier
    segment (see :func:`frontier`), so it surfaces the moderate-consumable plans a
    price sweep would skip. The ``price_*`` are the objective weights in both
    modes: the frontier passes per-unit **gold** for them (so the interior trades
    time against real gold, :func:`frontier`), or a uniform 1 for the plain
    consumable count. ``T`` drops out of the objective when ``time_cap`` is given.
    """
    recipes = recipes or {}
    openables = openables or []
    inventory = inventory or {}
    wanted = {item.lower(): (item, qty) for item, qty in demand.items() if qty > 0}

    obtainable = _obtainable(offers, recipes, openables)
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

    arnold_palmers = pulp.lpSum(o.ap * x for o, x in zip(used, usage))
    large_nets = pulp.lpSum(o.ln * x for o, x in zip(used, usage))
    cookies = pulp.lpSum(o.cookies * x for o, x in zip(used, usage) if o.cookies)
    grape_juices = pulp.lpSum(o.gj * x for o, x in zip(used, usage) if o.gj)
    # Apple Ciders spent -- the count each cider explore burns (its
    # ``consumes["apple cider"]``); zero outside the inventory plan mode.
    ciders = pulp.lpSum(
        o.consumes["apple cider"] * x
        for o, x in zip(used, usage)
        if o.consumes.get("apple cider", 0.0) > 0
    )
    # Orange Juice drunk for stamina (the drinking track) -- a tradable input the
    # cider channel spends; zero outside the inventory plan mode.
    orange_juice = pulp.lpSum(
        o.consumes["orange juice"] * x
        for o, x in zip(used, usage)
        if o.consumes.get("orange juice", 0.0) > 0
    )
    # Only so many full cookie sets are on hand to spend boosting farming days.
    problem += cookies <= cookie_sets
    if time_cap is not None:
        problem += makespan <= _minutes(time_cap)
        # Minimise the price-weighted consumables under the time cap. The frontier
        # passes gold prices here (so the interior trades time against real gold);
        # with uniform weight-1 prices it reduces to the plain consumable count.
        problem += (
            price_ap * arnold_palmers
            + price_ln * large_nets
            + price_cookie * cookies
            + price_gj * grape_juices
            + price_cider * ciders
            + price_oj * orange_juice
        )
    else:
        problem += (
            makespan
            + price_ap * arnold_palmers
            + price_ln * large_nets
            + price_cookie * cookies
            + price_gj * grape_juices
            + price_cider * ciders
            + price_oj * orange_juice
        )

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
        # sourced like any other need (empty in unlimited mode).
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
        problem += (
            gathered + made - consumed - used_up + unpacked - emptied + held >= need
        )

    # Makespan: each track's summed time is at most T (tracks run concurrently).
    tracks: dict[str, list] = {}
    for offer, x in zip(used, usage):
        tracks.setdefault(offer.track, []).append(_minutes(offer.time_per_unit) * x)
    for terms in tracks.values():
        problem += pulp.lpSum(terms) <= makespan

    problem.solve(pulp.PULP_CBC_CMD(msg=0))

    # The first solve fixes the *cost* but not a unique plan: it has alternate
    # optima CBC picks between at random across processes. Refine it in a
    # lexicographic tower, each stage minimising one objective then locking it at
    # its optimum so the next cannot spoil it:
    #
    #   1. cost           -- the first solve above (weighted sum, or epsilon cap)
    #   2. priced spend   -- least gold-weighted consumables at that cost
    #   3. unpriced spend -- least of the consumables the prices ignore (Grape
    #                        Juice is gold-free, so stage 2 leaves it unbounded)
    #   4. activity       -- least crafts + gathering, shedding pointless surplus
    #
    # Stage 2 is the crux. In a weighted-sum endpoint the makespan term (~1e5
    # minutes) dwarfs the price-weighted consumables (~1e-6 each for the fastest
    # plan), so within CBC's tolerance they are free and the first solve parks
    # them on an arbitrary degenerate vertex -- the fastest plan spent Large Nets
    # and Grape Juice that bought no time, and its Grape-Juice count flapped
    # 144<->151 run to run. Every consumable is a Pareto cost, so a plan spending
    # more of one for no time gain is strictly worse: with the makespan pinned we
    # re-minimise them alone (the objective is now just the consumables, at a scale
    # CBC resolves -- see the normalisation), pinning the genuinely thriftiest
    # plan, for any config, not an arbitrary-but-fixed vertex. Stage 3 then reins
    # in the consumables stage 2's weights price at zero (Grape Juice), which it
    # would otherwise leave free to drift; stage 4 sheds surplus crafts / off-track
    # farming. Stages 3-4 move no priced metric.
    if used or craft or opens:
        t_star = makespan.value() or 0.0
        problem += makespan <= t_star * (1 + 1e-9) + 1e-9

        # Each consumable, its price, and whether any used offer actually spends
        # it. Only a spent consumable gives an objective a variable term -- an
        # unspent one is a constant 0, which CBC rejects as an empty objective, and
        # is nothing to minimise anyway.
        consumables = [
            (price_ap, arnold_palmers, any(o.ap for o in used)),
            (price_ln, large_nets, any(o.ln for o in used)),
            (price_cookie, cookies, any(o.cookies for o in used)),
            (price_gj, grape_juices, any(o.gj for o in used)),
            (price_cider, ciders, any(o.consumes.get("apple cider") for o in used)),
            (price_oj, orange_juice,
             any(o.consumes.get("orange juice") for o in used)),
        ]

        def minimise_then_lock(terms: list[tuple[float, object]]) -> None:
            """Minimise ``sum(weight * expr)`` then pin each ``expr`` at its optimum."""
            if not terms:
                return
            problem.setObjective(pulp.lpSum(w * expr for w, expr in terms))
            problem.solve(pulp.PULP_CBC_CMD(msg=0))
            for _, expr in terms:
                problem.addConstraint(
                    expr <= (pulp.value(expr) or 0.0) * (1 + 1e-9) + 1e-9
                )

        # Stage 2 -- priced consumables, weights normalised to O(1) so they sit on
        # the makespan constraint's scale. At the raw endpoint magnitudes (~1e-6
        # fast, ~1e9 cheap) the coefficients fall below CBC's tolerance against
        # that ~1e5 constraint, so it cannot minimise them (it returned a *worse*
        # total than the first solve's own vertex); dividing by the largest price
        # keeps every relative weight -- gold ratios interior, uniform at an
        # endpoint -- at a resolvable scale. With nothing priced (a direct
        # weighted-sum call), both stages 2 and 3 are empty and the first solve's
        # consumable levels stand.
        scale = max((p for p, _, spent in consumables if spent and p > 0), default=0.0)
        if scale > 0:
            minimise_then_lock(
                [(p / scale, expr) for p, expr, spent in consumables if spent and p > 0]
            )
            # Stage 3 -- the gold-free spent consumables (Grape Juice), which stage
            # 2 does not price and so leaves free to sit high.
            minimise_then_lock(
                [(1.0, expr) for p, expr, spent in consumables if spent and p == 0]
            )

        # Stage 4 -- least activity, so no reported metric moves.
        problem.setObjective(
            pulp.lpSum(craft.values()) + pulp.lpSum(usage) + pulp.lpSum(opens)
        )
        problem.solve(pulp.PULP_CBC_CMD(msg=0))

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
    """
    return sum(
        spent * prices.get(item, 0.0)
        for item, spent in _consumable_spend(plan).items()
    )


def plan_gold_breakdown(
    plan: SourcingPlan,
    prices: dict[str, float],
    holdings: dict[str, float],
) -> tuple[float, float]:
    """Split :func:`plan_gold` into ``(to_buy, passive)`` gold.

    A consumable a plan sources for itself costs no gold: its market value is the
    *passive* subtotal. Only what a plan must still acquire by trading is the
    *to-buy* subtotal -- the gold to spend on top of what you have. The split turns
    on whether a consumable is **grounded** in this plan (some draw ``consumes`` it,
    the inventory plan mode where a net/palmer/juice is a real item sourced from
    your inventory and passive economy). A grounded consumable is fully self-sourced
    by construction, so all of its value is passive; an abstract one (the unlimited
    mode, where the consumable is unbounded spend) is passive only up to what you
    already hold (``holdings``), the rest to-buy. Cookie sets are never grounded but
    come from held cookies, so their allowance is passed in ``holdings`` too. The
    two subtotals always sum to :func:`plan_gold`.
    """
    grounded = {item for draw in plan.draws for item in draw.offer.consumes}
    to_buy = passive = 0.0
    for item, spent in _consumable_spend(plan).items():
        price = prices.get(item, 0.0)
        if spent <= 0 or price <= 0:
            continue
        if item in grounded:
            passive += spent * price  # sourced entirely from inventory + passive
            continue
        held = holdings.get(item, 0.0)
        bought = max(0.0, spent - held)
        to_buy += bought * price
        passive += (spent - bought) * price
    return to_buy, passive


def _dominates(a: SourcingPlan, b: SourcingPlan) -> bool:
    """Whether ``a`` is at least as good as ``b`` on every objective, better on one
    -- so ``b`` is not worth keeping."""
    return all(x <= y for x, y in zip(a.cost, b.cost)) and a.cost != b.cost


def _non_dominated(plans: list[SourcingPlan]) -> list[SourcingPlan]:
    """The Pareto-efficient plans, de-duplicated, sorted fastest first."""
    unique: dict[tuple, SourcingPlan] = {}
    for plan in plans:
        key = (round(plan.time.total_seconds()), round(plan.arnold_palmers),
               round(plan.large_nets), round(plan.cookie_sets),
               round(plan.grape_juices), round(plan.apple_ciders))
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
) -> list[SourcingPlan]:
    """The Pareto frontier of sourcing plans, fastest first.

    Every scarce resource is traded against time: Arnold Palmers, Large Nets,
    cookie sets and Grape Juices. First finds the two endpoints -- the fastest
    plan (spend them freely) and the cheapest, slowest one (avoid them) -- then
    fills the frontier between them by an **epsilon-constraint sweep**: cap the
    makespan at each of many times spanning the two ends and minimise the
    consumables under that cap. This
    is what a plain price sweep cannot do -- where the frontier has a flat
    (linear) segment, every weighted-sum price collapses onto an endpoint, hiding
    the moderate-consumable plans in between (e.g. spending some Arnold Palmers to
    explore a slow-farmed crop rather than all or none). The non-dominated results
    are thinned to about ``count`` plans spread across the frontier. Returns a
    single plan when consumables buy no speed (the two ends coincide).

    ``prices`` (per-unit gold, from :func:`config.trading_prices`) makes "cheap"
    mean **least gold**: the cheap endpoint and every interior cap minimise the
    gold-weighted consumables, so the frontier trades makespan against real gold
    (Grape Juice is priced from its grape content, or 0 if no grape price is set --
    then :func:`minimize`'s stage-3 refinement still reins it in). Without it the
    weights are a uniform 1 -- the plain consumable count, the original
    gold-agnostic behaviour.
    """
    # Per-consumable weight for the cheap side: per-unit gold, or a uniform 1
    # (plain count, Orange Juice ignored) when untraded.
    if prices is None:
        w = {"ap": 1.0, "ln": 1.0, "cookie": 1.0, "gj": 1.0, "cider": 1.0, "oj": 0.0}
    else:
        w = {
            "ap": prices.get("arnold palmer", 0.0),
            "ln": prices.get("large net", 0.0),
            "cookie": prices.get("cookie set", 0.0),
            "gj": prices.get("grape juice", 0.0),
            "cider": prices.get("apple cider", 0.0),
            "oj": prices.get("orange juice", 0.0),
        }
    fast = minimize(
        offers, demand, recipes, crafting_bonus, _FAST_PRICE, _FAST_PRICE,
        price_cookie=_FAST_PRICE, price_gj=_FAST_PRICE, price_cider=_FAST_PRICE,
        price_oj=_FAST_PRICE if w["oj"] else 0.0,
        openables=openables, inventory=inventory, cookie_sets=cookie_sets,
    )
    cheap = minimize(
        offers, demand, recipes, crafting_bonus,
        w["ap"] * _FREE_PRICE, w["ln"] * _FREE_PRICE,
        price_cookie=w["cookie"] * _FREE_PRICE, price_gj=w["gj"] * _FREE_PRICE,
        price_cider=w["cider"] * _FREE_PRICE, price_oj=w["oj"] * _FREE_PRICE,
        openables=openables, inventory=inventory, cookie_sets=cookie_sets,
    )
    plans = [fast, cheap]
    # Sample well above `count` so thinning has a dense frontier to spread over.
    samples = max(_MIN_CAP_SAMPLES, 4 * count)
    t_fast, t_slow = fast.time.total_seconds(), cheap.time.total_seconds()
    caps = _time_caps(t_fast, t_slow, samples)
    # Add the passive-completion breakpoints (see _passive_completion_caps): a plan
    # clamped to each is a natural intermediate the geometric sweep can miss. Keep
    # only those comfortably inside the endpoints -- the slowest breakpoint is the
    # cheap plan's own binding item, so it coincides with that endpoint -- and use
    # them as thinning anchors so they survive to the shown set.
    lo, hi = t_fast * (1 + _CAP_MARGIN), t_slow * (1 - _CAP_MARGIN)
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
            offers, demand, recipes, crafting_bonus,
            price_ap=w["ap"], price_ln=w["ln"], price_cookie=w["cookie"],
            price_gj=w["gj"], price_cider=w["cider"], price_oj=w["oj"],
            time_cap=timedelta(seconds=cap), openables=openables,
            inventory=inventory, cookie_sets=cookie_sets,
        )
        for cap in caps
    ]
    return _thin(_non_dominated(plans), count, tuple(breakpoints))
