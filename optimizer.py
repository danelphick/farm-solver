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

Because activity that costs no time or consumable is free in the objective, the
least-cost plan is degenerate: surplus gathered material can be spun into items
the demand never asked for, and a crop farmed off the bottleneck track adds no
time either, so either can appear as slack at no penalty. So :func:`minimize`
solves twice -- first for the least cost, then, with that cost locked, for the
least total activity (crafts plus gathering) -- reporting only what the demand
actually needs (see the second stage there).

To keep every coverage constraint satisfiable (so no shortfall slack is needed),
the LP is built only over **obtainable** items -- those gatherable, or craftable
from obtainable materials (a fixed point, :func:`_obtainable`). A demanded item
that is neither is dropped into the plan's ``remainder``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import timedelta

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
    remainder: dict[str, int] = field(default_factory=dict)

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

    Two modes trace the same ``(time, AP, LN)`` frontier from different sides. By
    default the LP minimises ``T + price_ap*AP + price_ln*LN`` (a weighted sum).
    With ``time_cap`` set it instead minimises the consumables ``AP + LN`` subject
    to ``T <= time_cap`` -- an epsilon-constraint solve, which unlike the weighted
    sum can land on the *interior* of a flat frontier segment (see
    :func:`frontier`), so it surfaces the moderate-consumable plans a price sweep
    would skip. ``price_ap``/``price_ln`` are ignored when ``time_cap`` is given.
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
    # Only so many full cookie sets are on hand to spend boosting farming days.
    problem += cookies <= cookie_sets
    if time_cap is not None:
        problem += makespan <= _minutes(time_cap)
        problem += arnold_palmers + large_nets + cookies + grape_juices + ciders
    else:
        problem += (
            makespan
            + price_ap * arnold_palmers
            + price_ln * large_nets
            + price_cookie * cookies
            + price_gj * grape_juices
            + price_cider * ciders
        )

    # Net production of each item covers its demand:
    # gathered + crafted + opened - consumed-by-crafts - consumed-by-opens.
    for item in items:
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

    # The objective is indifferent to any activity that costs no time or
    # consumable. Surplus gathered material can be spun into items the demand never
    # asked for (a craft is free), and gathering that fits under the makespan is
    # free too -- a crop farmed on a track that is not the bottleneck adds no time,
    # so the first solve may leave a whole field of an unneeded crop as slack. Both
    # are degenerate loops CBC may pick at random. Re-solve for the least total
    # activity among the least-cost plans: lock the realised time/AP/LN (with a
    # hair of slack for float noise), then minimise crafts plus gathering units. No
    # reported metric moves, but pointless surplus -- crafted or farmed -- drops
    # out, leaving only what the demand actually needs.
    if used or craft or opens:
        t_star = makespan.value() or 0.0
        ap_star = pulp.value(arnold_palmers) or 0.0
        ln_star = pulp.value(large_nets) or 0.0
        cookie_star = pulp.value(cookies) or 0.0
        gj_star = pulp.value(grape_juices) or 0.0
        cider_star = pulp.value(ciders) or 0.0
        problem += makespan <= t_star * (1 + 1e-9) + 1e-9
        problem += arnold_palmers <= ap_star * (1 + 1e-9) + 1e-9
        problem += large_nets <= ln_star * (1 + 1e-9) + 1e-9
        # Cookies, Grape Juices and Apple Ciders are priced, so the first solve
        # already uses only ones that pay for themselves in time; lock them too, so
        # the least-activity re-solve keeps the same thrifty mix and only sheds
        # pointless surplus.
        problem += cookies <= cookie_star * (1 + 1e-9) + 1e-9
        problem += grape_juices <= gj_star * (1 + 1e-9) + 1e-9
        problem += ciders <= cider_star * (1 + 1e-9) + 1e-9
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
        remainder=remainder,
    )


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
    already supply. Computed on the cheapest plan -- which crafts the most and
    buys the least -- it is that item's *full* requirement, the most passive ever
    has to make; a plan whose residual for an item reaches this has that item at
    its full completion (see :func:`main.print_critical_path`).
    """
    passive = next((d for d in plan.draws if d.offer.track == PASSIVE_TRACK), None)
    if passive is None:
        return {}
    demanded = {item.lower() for item in demand}
    # What must be produced: demand, plus crafting materials and open tokens/keys.
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
    # What the other channels, crafting, opening and stock already supply.
    elsewhere = {item: crafting_bonus * count for item, count in plan.crafts.items()}
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
    residuals = {}
    for item, rate in passive.offer.yields.items():
        if rate <= 0:
            continue
        residual = need.get(item, 0.0) - elsewhere.get(item, 0.0)
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
    passive = next((d for d in plan.draws if d.offer.track == PASSIVE_TRACK), None)
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
    """
    fast = minimize(
        offers, demand, recipes, crafting_bonus, _FAST_PRICE, _FAST_PRICE,
        price_cookie=_FAST_PRICE, price_gj=_FAST_PRICE, price_cider=_FAST_PRICE,
        openables=openables, inventory=inventory, cookie_sets=cookie_sets,
    )
    cheap = minimize(
        offers, demand, recipes, crafting_bonus, _FREE_PRICE, _FREE_PRICE,
        price_cookie=_FREE_PRICE, price_gj=_FREE_PRICE, price_cider=_FREE_PRICE,
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
            time_cap=timedelta(seconds=cap), openables=openables,
            inventory=inventory, cookie_sets=cookie_sets,
        )
        for cap in caps
    ]
    return _thin(_non_dominated(plans), count, tuple(breakpoints))
