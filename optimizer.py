"""The sourcing optimiser for Farm Solver.

Given the items still needed (base materials left after crafting expansion) this
picks how to obtain them across every channel -- passive production, fishing,
exploring and farming -- trading total time against the Arnold Palmers and Large
Nets spent. As worked out in ``PLAN.md``, that choice is a small **linear
program**: one continuous usage level per :class:`~sources.Offer` plus a makespan
``T``, with linear coverage and per-track makespan constraints. Because every
objective is linear, the whole ``(time, AP, LN)`` Pareto frontier is recovered
exactly by minimising ``T + price*(AP + LN)`` over a sweep of the price of time
(:func:`frontier`) -- no metaheuristic needed.

Joint production needs no special handling: a co-product is just the same offer
variable appearing in several coverage rows, so one fishing trip is credited to
every fish it lands. Items no offer yields never enter the LP; they stay in the
plan's ``remainder`` for crafting, as before.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import pulp

from crops import Farm
from exploring import Exploring
from fishing import Fishing
from passive import PassiveProduction
from sources import (
    Draw,
    Offer,
    aggregate,
    exploring_offers,
    farming_offers,
    fishing_offers,
    passive_offers,
)

# Usage below this many units is treated as zero (CBC leaves tiny residuals).
UNIT_EPSILON = 1e-6

# Price-of-time anchors for the frontier sweep, in minutes saved per consumable.
# A tiny price barely penalises consumables (the fastest plan, using them freely
# but not wastefully); a large one all but forbids them (the cheapest, slowest
# plan) -- 1e9 dwarfs any realistic per-consumable time saving without the wild
# coefficient magnitudes that upset the solver. The interior is swept
# geometrically between.
_FAST_PRICE = 1e-6
_FREE_PRICE = 1e9
_SWEEP_EXPONENTS = range(-6, 10)


def all_offers(
    production: PassiveProduction,
    fishing: Fishing,
    exploring: Exploring,
    farm: Farm,
) -> list[Offer]:
    """Every production offer across the channels, ready for the optimiser."""
    return [
        *passive_offers(production),
        *fishing_offers(fishing),
        *exploring_offers(exploring),
        *farming_offers(farm),
    ]


@dataclass
class SourcingPlan:
    """One way to source the demand: the draws made, the cost, and the remainder.

    ``time`` is the makespan (concurrent tracks, the longest), ``arnold_palmers``
    and ``large_nets`` the consumables spent (raw, un-rounded), and ``remainder``
    the demanded items no offer could supply, left for crafting.
    """

    draws: list[Draw] = field(default_factory=list)
    time: timedelta = timedelta(0)
    arnold_palmers: float = 0.0
    large_nets: float = 0.0
    remainder: dict[str, int] = field(default_factory=dict)

    @property
    def cost(self) -> tuple[float, float, float]:
        """The objective triple ``(time seconds, AP, LN)`` for comparisons."""
        return (self.time.total_seconds(), self.arnold_palmers, self.large_nets)


def _minutes(time: timedelta) -> float:
    return time.total_seconds() / 60


def minimize(
    offers: list[Offer],
    demand: dict[str, int],
    price_ap: float = 0.0,
    price_ln: float = 0.0,
) -> SourcingPlan:
    """Solve one LP: cover ``demand`` at least cost for the given prices.

    Minimises ``T + price_ap*AP + price_ln*LN`` (``T`` the makespan in minutes)
    subject to covering every demanded item some offer yields and each track's
    time summing to at most ``T``. Items no offer yields come back in the plan's
    ``remainder`` for crafting. (Consumable budgets -- the "constrained" mode --
    are deferred: with hard coverage a binding budget makes the LP infeasible, so
    they need shortfall slack variables, added when that mode is built.)
    """
    # Match demand to offers case-insensitively, but keep display names for the
    # remainder.
    wanted = {item.lower(): (item, qty) for item, qty in demand.items() if qty > 0}
    coverable = {
        key for key in wanted if any(o.yield_of(key) > 0 for o in offers)
    }
    # Only offers that yield something wanted can help; the rest just add cost.
    relevant = [o for o in offers if any(o.yield_of(k) > 0 for k in coverable)]

    remainder = {
        display: qty
        for key, (display, qty) in wanted.items()
        if key not in coverable
    }

    if not coverable:
        return SourcingPlan(remainder=remainder)

    problem = pulp.LpProblem("sourcing", pulp.LpMinimize)
    usage = [
        pulp.LpVariable(f"x{i}", lowBound=0) for i in range(len(relevant))
    ]
    makespan = pulp.LpVariable("T", lowBound=0)

    arnold_palmers = pulp.lpSum(o.ap * x for o, x in zip(relevant, usage))
    large_nets = pulp.lpSum(o.ln * x for o, x in zip(relevant, usage))

    problem += makespan + price_ap * arnold_palmers + price_ln * large_nets

    # Coverage: enough of every wanted item this set of offers can make.
    for key in coverable:
        _, qty = wanted[key]
        problem += (
            pulp.lpSum(o.yield_of(key) * x for o, x in zip(relevant, usage)) >= qty
        )

    # Makespan: each track's summed time is at most T (tracks run concurrently).
    tracks: dict[str, list] = {}
    for offer, x in zip(relevant, usage):
        tracks.setdefault(offer.track, []).append(_minutes(offer.time_per_unit) * x)
    for terms in tracks.values():
        problem += pulp.lpSum(terms) <= makespan

    problem.solve(pulp.PULP_CBC_CMD(msg=0))

    draws = [
        Draw(offer, units)
        for offer, x in zip(relevant, usage)
        if (units := x.value() or 0.0) > UNIT_EPSILON
    ]
    time, ap, ln = aggregate(draws)
    return SourcingPlan(
        draws=sorted(draws, key=lambda d: d.offer.name),
        time=time,
        arnold_palmers=ap,
        large_nets=ln,
        remainder=remainder,
    )


def _dominates(a: SourcingPlan, b: SourcingPlan) -> bool:
    """Whether ``a`` is at least as good as ``b`` on all three objectives, better
    on one -- so ``b`` is not worth keeping."""
    return all(x <= y for x, y in zip(a.cost, b.cost)) and a.cost != b.cost


def _non_dominated(plans: list[SourcingPlan]) -> list[SourcingPlan]:
    """The Pareto-efficient plans, de-duplicated, sorted fastest first."""
    unique: dict[tuple, SourcingPlan] = {}
    for plan in plans:
        key = (round(plan.time.total_seconds()), round(plan.arnold_palmers),
               round(plan.large_nets))
        unique.setdefault(key, plan)
    kept = [
        p for p in unique.values()
        if not any(_dominates(q, p) for q in unique.values() if q is not p)
    ]
    return sorted(kept, key=lambda p: p.cost)


def _thin(plans: list[SourcingPlan], count: int) -> list[SourcingPlan]:
    """Keep ``count`` plans spread across the frontier, including both ends."""
    if len(plans) <= count:
        return plans
    # Even indices across the sorted list, always keeping first and last.
    step = (len(plans) - 1) / (count - 1)
    picked = {round(i * step) for i in range(count)}
    return [plans[i] for i in sorted(picked)]


def frontier(
    offers: list[Offer], demand: dict[str, int], count: int = 5
) -> list[SourcingPlan]:
    """The ``(time, AP, LN)`` Pareto frontier of sourcing plans, fastest first.

    Sweeps the price of time from tiny (spend consumables freely -- the fastest
    plan) to huge (avoid them -- the cheapest, slowest), solving the LP at each
    price, then keeps the non-dominated plans and thins them to about ``count``
    spread across the frontier. Returns a single plan when consumables buy no
    speed (the two ends coincide).
    """
    prices = [_FAST_PRICE, *(10.0**e for e in _SWEEP_EXPONENTS), _FREE_PRICE]
    plans = [minimize(offers, demand, price, price) for price in prices]
    return _thin(_non_dominated(plans), count)
