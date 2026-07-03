# Plan: multi-objective sourcing solver (time × AP × LN)

Status of this file: living plan for turning the solver from "report mastery
shortfalls" into "find the best way to source everything, trading time against
Arnold Palmers (AP) and Large Nets (LN)." Update as phases land.

## Goal

Every needed item — a mastery/inventory shortfall, or a base material a craft
needs — can be satisfied by one or more **channels**. Two spend a consumable to
buy speed; the rest cost time but no consumables:

| Channel | Consumable | Time cost | Joint production |
|---|---|---|---|
| Passive (buildings) | — | concurrent wait (max) | one cycle yields many items |
| Pets | — | concurrent wait (max) | bundle per pet |
| Farming | — | concurrent grow time (+ grape juice) | harvest yields crop + rare drops |
| Crafting | — | none (an LP flow: consume materials → produce output) | one craft = one output item |
| Fishing | **Large Net** | ~instant | one net at a zone yields a bundle of fish |
| Exploring | **Arnold Palmer** | ~instant | one palmer at a location yields a bundle |

Spending an AP or LN pulls items out of the slow farming/passive queues for zero
time but +1 consumable. Sweeping "how much time is a consumable worth" traces a
**Pareto frontier** over `(total_time, AP, LN)`.

## Decisions (locked)

- **Output:** a Pareto frontier of non-dominated plans (~**5** to start; add more
  if short-duration plans need finer AP/LN resolution).
- **Channel choice:** the **optimizer decides** per item — no fixed
  free-channels-first order; the objective alone picks where AP/LN are worth it.
- **Time tracks are concurrent:** farming and all passives run at the same time,
  so `total_time = max(track times)`; fishing/exploring are ~instant.
- **AP mechanics:** one Arnold Palmer yields **200** items in a location, **500**
  with the Lemon Squeezer perk, ×**1.1** with Quandary Chowder (550 with both).
  (Fishing/LN: 250 base, 400 reinforced, +100 trawl, ×1.1 sea pincher.)

## Phases

**1 — Model exploring (AP channel). DONE (`f4c48b7`).** `exploring.py` mirrors
`fishing.py`: `ExploringModifiers.palmer_yield()` (200/500/×1.1) and
`ExploringLocation.explore(yield)` splitting across drop rates.

**2 — Loaders + config wiring for fishing and exploring. DONE (`be82865`).**
`read_zones`/`read_locations`, `load_*`, `*_from_config` on `fishing.py` and
`exploring.py` build fully-populated models from the CSVs + `[fishing]`/
`[exploring]` flags. Item keys lower-cased for matching; display names kept.

**3 — Unify channels behind one interface. DONE.** `sources.py` defines `Offer`
(carries a full `yields` bundle + `ap`/`ln`/`time_per_unit` on a named `track`)
and `Draw` (an offer scaled to N units). Builders cover fishing, exploring,
farming (`da57956`) and passive+pets (`passive_offers`): the whole passive
economy is one offer whose bundle is each item's combined per-minute rate, so its
concurrency falls out of the binding-item `units_for`. Compromise (documented in
code): passive uses a continuous per-minute rate rather than rounding each wait
up to a whole cycle — smooth costs for the optimiser, error at most one cycle,
and consistent with what `time_to_produce` already does for multi-producer items.
Crafting stays as the recursion that expands demand to base materials feeding
these offers.

**4 — Time-aggregation model. DONE for offers (`da57956`).** `aggregate(draws)`
sums time within a track and takes the max across tracks (farming crops share
one sequential track; fishing/exploring instant), and sums AP/LN. Joint
production is handled inside each offer via `units_for` (the binding item drives
it, the rest of the bundle rides along). Passive folds in via `passive_offers`
(one bundled offer at combined per-minute rates); `aggregate` covers all tracks.

**5 — Pareto search. DONE.** `optimizer.py`: `all_offers` gathers every channel,
`minimize` solves one LP with PuLP (linear coverage + per-track makespan), and
`frontier` sweeps the price of time into a thinned, non-dominated set of
`SourcingPlan`s. Verified end-to-end on the real data (fishing/exploring/farming/
passive all selectable). Consumable budgets ("constrained" mode) deferred: hard
coverage makes a binding budget infeasible, so they need shortfall slack vars.

### Solver design (Phase 5)

**It is an LP.** Decision variables are one continuous usage level `x_o ≥ 0` per
offer (nets, palmers, plantings, or minutes of the passive economy) plus a
makespan `T ≥ 0`. Every cost is linear in `x`, and the awkward "max across
tracks" time becomes linear with the `T` trick. So a single weighted objective is
a plain LP — no integrality needed (fractional units are averages; round up only
at reporting).

For demand `d[i]` (base materials left after craft expansion, restricted to items
some offer yields):

```text
minimize   T + λ_ap·AP + λ_ln·LN
  where    AP = Σ_o ap_o·x_o ,  LN = Σ_o ln_o·x_o
subject to
  coverage:  Σ_o yields_o[i]·x_o ≥ d[i]      for each demanded item i
  makespan:  Σ_{o in track t} time_o·x_o ≤ T for each track t
  budgets:   AP ≤ max_ap,  LN ≤ max_ln       (optional; the "constrained" mode)
  x_o ≥ 0,  T ≥ 0
```

Joint production needs no special handling: co-products are the *same* `x_o`
appearing in several coverage rows, so one fishing trip is automatically credited
to every fish it lands. Items no offer yields never enter the LP — they stay in
the remainder for crafting, exactly as today.

**Why an LP solver, not a metaheuristic.** An LP's Pareto front is piecewise-
linear and convex, so weighted-sum scalarisation recovers *every* vertex exactly.
Evolutionary multi-objective tools (pymoo/NSGA-II) would only approximate what we
can get exactly and instantly. So: scalarise + sweep, don't evolve.

**Frontier sweep** (`frontier(offers, demand, count=5)`):

1. **All-free anchor:** large `λ` → minimise consumables; realised `T` is the
   slowest, cheapest plan (spends AP/LN only where an item has no free source).
2. **Fastest anchor:** `λ = 0` → minimise `T` alone, then a lexicographic tidy
   (fix `T ≤ T*`, minimise `AP+LN`) to shed consumables that buy no extra speed.
3. **Interior:** sweep `λ` on a log scale between the anchors, solve the LP each
   time, keep the realised `(T, AP, LN)`, drop dominated points, thin to ~`count`
   spread by time. One shared `λ` for AP and LN by default (fish↔LN and
   explore↔AP touch largely disjoint items); separate prices / `--max-ap` /
   `--max-ln` reuse the same LP via the budget rows. Sample denser near the fast
   end if short-duration plans swing wildly in AP/LN (the noted open question).

**Module shape:** new `optimizer.py`:

- `all_offers(passive, fishing, exploring, farm) -> list[Offer]` (gather builders).
- `SourcingPlan`: `draws: list[Draw]`, `time`, `arnold_palmers`, `large_nets`,
  `remainder: dict[str,int]` (demand no offer covers).
- `minimize(offers, demand, price_ap, price_ln, max_ap=None, max_ln=None)
  -> SourcingPlan` — build & solve one LP with **PuLP** (see Dependencies).
- `frontier(...) -> list[SourcingPlan]` — the sweep, dedup, thin.

**Honesty/edges:** `≥` coverage + round-up reporting slightly over-provisions
(safe). Without budgets the LP is always feasible (remainder absorbs the
uncoverable). Million-scale demands are fine for CBC; keep a small `λ` floor to
avoid degeneracy.

**6 — Solution + reporting. DONE.** `run()` sources via the frontier: the offers
and `frontier` produce the plans, and the report prints a frontier table
(`print_frontier`, time/AP/LN, fastest/cheapest tagged) then expands one plan
(`print_plan`: Fishing/Exploring/Farming sections + a per-item Passive list that
surfaces the bottleneck + a no-source remainder), with `print_crafts` for `-b`.
Selection: `--cheapest-plan` (default, fewest consumables) or `--plan N`.
`parse_duration` accepts `y` (years). `Solution.arnold_palmers`/`large_nets` set
from the chosen plan. Cleanup: removed the whole superseded fixed-order pipeline
(`craft_plan`, `plan_farming`, `source_materials`, `split_pet_production`, old
`print_*`) and the inert `-r`, with their tests. `farming.py` keeps only
`FARMING_ITEMS`/`is_farmed` (for `--unobtainable`).

**7 — Tests + README. DONE.** Loaders, offers, aggregation, the LP optimiser and
frontier, crafting-in-LP, and the reporting are all covered; README rewritten.

**8 — Crafting folded into the LP. DONE (`50c7777`, `021db7a`).** Each recipe is a
flow (consume materials, produce `crafting_bonus` of the output, no time/
consumable cost), so craft-vs-gather is optimised and co-product surpluses reused
— not pre-decided by a heuristic. The LP is built over the *obtainable* items only
(gatherable, or craftable from obtainable, a fixed point), so coverage is always
feasible and no shortfall slack is needed; anything else falls to the remainder.
`run()` feeds `optimizer.frontier` the top-level demand (`top_level_demand`) plus
the recipe flows (`recipe_flows`). Retired the `gather_demand`/`craft_breakdown`/
`gatherable_items` heuristic layer. Fixed the Glass Bottle case (fished → crafted
from explored Glass Orb), cutting the fastest plan's Large Nets ~68k → ~11k. This
supersedes the "base materials left after craft expansion" framing in the Phase 5
design below — the LP now sources the full tree, not pre-expanded leaves.

**9 — Frontier controls. DONE.** `--plans N` sets how many plans span the frontier
(default 5, for finer resolution where the fast end swings in cost). `--max-ap` /
`--max-ln` join `--passive-limit` as ceilings that drop plans exceeding them
(unless that leaves none); the shared filter is `main.within_limits`.

## Dependencies

The solver adds the project's first runtime dependency. Choice: **PuLP**.

- **PuLP (chosen).** Pure-Python modelling with a bundled CBC solver — no numpy /
  BLAS / Fortran, so it installs from a wheel on Python 3.14 with no ABI risk
  (verified: `uv run --with pulp` solves a test LP here). Its declarative model
  (`prob += Σ …`) keeps the LP auditable and reads like the rest of the codebase,
  and switching a variable to integer later (whole nets/palmers) is one keyword.
  We solve only ~5–15 tiny LPs per run, so CBC's subprocess overhead is a non-
  issue. ~15 MiB.
- **SciPy `linprog` (HiGHS) — alternative.** Also installs and solves on 3.14
  (verified), in-process and fast, but needs the model assembled as raw
  `A_ub/b_ub` matrices (sign flips + index bookkeeping — the bug-prone part we are
  trying to avoid), and pulls in numpy (~40 MiB total). Reach for it only if
  in-process solving or avoiding a solver subprocess ever matters.
- **pymoo / NSGA-II — rejected.** Approximate multi-objective; unnecessary since
  the LP frontier is exactly recoverable by scalarisation.

First implementation step: `uv add pulp`, then a smoke test mirroring the check
above, before building `optimizer.py`.

## Status

All phases (1–9) are landed; the plan is complete. Resolved open questions:

- **Frontier granularity** — tunable via `--plans N` (Phase 9).
- **Hard AP/LN ceiling** — `--max-ap` / `--max-ln` filter the frontier (Phase 9).
- **Craft-vs-gather** — optimised by folding crafting into the LP (Phase 8).

Remaining possibilities, only if wanted (none blocking):

- A true **constrained solve** (minimise time subject to exact AP/LN budgets)
  rather than filtering the ~5 computed plans; needs shortfall slack + big-M in a
  single solve, so kept out until there's a need the filter cannot meet.
- Whole-number **integer** craft/net/palmer counts (MILP) instead of rounding the
  LP's fractional solution at reporting; PuLP makes this a one-keyword change if
  the rounding ever proves too loose.
