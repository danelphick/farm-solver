# Architecture — code map

Navigational map of the FarmRPG **farm-solver** LP optimiser, for finding code
fast. README.md covers *behaviour/config* (user-facing); this file is the
*where-does-X-live* index. Reference by function name (stable); line numbers
drift.

## What it does

Given a FarmRPG mastery/inventory state and a goal (tower level, `-m` mastery, or
`-i` item counts), work out the ways to source everything, **trading time against
gold**: self-sourcing a consumable (Arnold Palmer / Large Net / Apple Cider /
Orange Juice / cookie set / Grape Juice) costs makespan, buying it costs gold. It
prints a Pareto **frontier** of plans from "spend nothing, slowest" to "buy
freely, fastest", plus a per-channel breakdown of a chosen one.

**Always grounded.** Every consumable is a real item: exploring/fishing/GJ-farming
offers carry a `consumes` input (Arnold Palmer / Large Net / Grape Juice, chained
down to Orchard Lemons, passive stone/wood/straw/antlers, and Vineyard Grapes), and
the **Apple Cider** exploring channel (`sources.cider_offers`) is always present,
fed by **stamina** from drinking Orange Juice (`sources.drink_offers`, the
`DRINKING_TRACK`; `--current-stamina` seeds a starting balance) and from the
**Farmhouse** (`config._farmhouse_producer` — a daily passive stamina deposit,
exempt from the storehouse cap via `passive.UNCAPPED_ITEMS`). Cider math
(`exploring.CiderModifiers`): each stamina buys one explore at drop rate
`CIDER_DROP_RATE` (0.40); explores per cider scale with per-zone Exploring
Effectiveness (`[exploring.effectiveness]`) and Cinnamon Sticks; stamina per
explore is cut by Wanderer / Neigh Neigh / Neigh.

**Gold budget** (`--max-gold`, see `optimizer.minimize`'s `buy` variables): the LP
decides make-vs-buy per consumable subject to a gold budget the frontier sweeps.
`--max-gold inventory` = 0 (self-source everything, the old inventory mode);
`--max-gold unlimited` (default) = no cap (the whole frontier up to the fastest
plan, the old unlimited mode); any value in between caps the buy spend. **Buying
cannot earn an item's own mastery** — a `buy ≤ derived` floor keeps purchases to an
item's non-goal uses (a craft material, or a channel's tool), so a demanded Large
Net / Orange Juice must be crafted or gathered.

## Pipeline (one run)

`main.run(args)` orchestrates, roughly:

1. **Load state** — `main.load_state` → `parsing` builds `farm_solver.GameState`
   (masteries + inventory) from the pasted-in text files.
2. **Goal → demand** — `farm_solver.solve_tower/solve_mastery/solve_acquire`
   produce a `Solution` with `ItemNeed`s; `crafting.top_level_demand` turns them
   into `{item: qty}`; `crafting.recipe_flows` gives the craft graph
   (IGNORED_MATERIALS stripped, lower-cased).
3. **Build channels** — `config.passive_production` (buildings + pets),
   `fishing/exploring/crops` loaders, then `optimizer.all_offers` folds them into
   one `list[Offer]`; `sources.open_flows` builds the Locksmith `OpenFlow`s.
4. **Solve frontier** — `optimizer.frontier(...)` → list of `SourcingPlan`,
   fastest first.
5. **Print** — `report.print_frontier` (the table), then `report.print_plan` for
   the selected plan (critical path + per-channel actions), optional
   `report.print_crafts`. All printing lives in `report.py`; `main.run` calls it.

The whole thing loops up to `_MAX_SEASON_ITERS` when `model_calendar` is on, since
a seasonal pet's yield depends on the plan span (a fixed point).

## Module map

| File | Owns | Key names |
|---|---|---|
| `main.py` | CLI, orchestration (no printing) | `run`, `build_parser`, `resolve_files`, `selected_plan`, `cheapest_plan` |
| `report.py` | **all printing** + critical-path naming | `print_frontier`, `print_plan`, `print_critical_path`, `build_display`, `_defining_passive_resource` |
| `optimizer.py` | the LP and the frontier | `SourcingPlan` (`passive_draw`, `track_times`), `minimize`, `frontier`, `plan_balance`, `passive_residuals`, `plan_gold`, `plan_gold_breakdown`, `plan_leftovers`, `surplus_sale_value` |
| `sources.py` | offer/draw model, per-channel offer builders | `Offer`, `Draw`, `OpenFlow`, `*_offers`, track constants |
| `farm_solver.py` | game state, goal→needs | `GameState`, `Inventory`, `Solution`, `solve_*` |
| `parsing.py` | parse pasted mastery/inventory text | `_split_blocks`, `_parse_mastery_block` |
| `crafting.py` | workshop recipes, demand expansion | `Workshop`, `recipe_flows`, `top_level_demand`, `IGNORED_MATERIALS` |
| `crops.py` | farming model (grow time, GJ, cookies, instant-grow) | `Crop`, `Farm`, `COOKIE_*`, `INSTANT_GROW_MAX_BASE` |
| `passive.py` | passive producers, durations | `PassiveProducer`, `PassiveProduction` (`producer_rates`, `rate_per_minute`), `parse_duration`, `format_duration` |
| `pets.py` | pets as (seasonal) passive producers | `Pet`, `Pets`, `seasonal_fraction` |
| `config.py` | `config.toml` → producers/pets/storehouse/trading | `passive_production`, `PET_PRODUCER_PREFIX`, `storehouse_size`, `trading_prices` |
| `fishing.py`/`exploring.py` | LN/AP channels, modifiers | `Fishing`/`Exploring`, `CiderModifiers`, `*_from_config` |
| `locksmith.py` | chests/bags openables | `Openable`, `Locksmith`, `load_openables` |
| `farming.py` | which items are farmed | `FARMING_ITEMS`, `is_farmed` |

Data lives in `*.csv` (crops, fishing, exploring, pets, openables, tower,
seed_drops) and `config.toml`; game state comes from pasted text files.

## Core data types

- **`sources.Offer(name, track, yields, consumes, ap, ln, cookies, gj,
  time_per_unit)`** — one way to gather; `yields` is `{item: qty_or_rate}`.
  `consumes` is `{item: qty}` an activity **uses up** as an input (always grounded:
  an explore consumes an Arnold Palmer, a cider explore consumes stamina + part of
  an Apple Cider, a net a Large Net, a GJ grow a Grape Juice — real crafted / held /
  bought items). `track` is `FARMING/FISHING/EXPLORING/PASSIVE_TRACK/DRINKING_TRACK`.
  Cost fields (`ap/ln/cookies/gj`) are per-unit tallies for reporting.
- **`sources.Draw(offer, units)`** — an offer used at scale. Properties:
  `produced` (`rate*units` per item — **overproduces** fast passive items),
  `ap/ln/cookies/gj`, `time` (`time_per_unit*units`).
- **`optimizer.SourcingPlan`** — `draws`, `crafts` (`{item: count}`), `opens`
  (`{openable_name: count}`), `time`, the consumable tallies spent
  (`arnold_palmers/large_nets/cookie_sets/grape_juices/apple_ciders/orange_juice`),
  `bought` (`{item: units}` purchased) and `gold` (`Σ bought*price`), `remainder`.
  `.cost` is the pair `(time, gold)` — the whole tradeoff, so two plans equal on
  both are equivalent however they mix making and buying.
- **`sources.OpenFlow(name, consumes, yields)`** — opening a chest/bag: a free,
  instant multi-output LP flow.
- **`crops.Farm`** — grow times, `grape_juices_for`, `is_instant`,
  `takes_cookies`, `cookie_day_yield`. **`passive.PassiveProduction`** —
  `producers` list, `rate_per_minute`, storehouse cap.

## The LP and frontier (optimizer.py)

- **`minimize(...)`** solves one LP, trading makespan against **gold**. Two modes:
  - *weighted-sum* (default): minimise `time + gold_weight*gold`. A tiny
    `gold_weight` (`_FAST_PRICE`) buys freely for the fastest plan; a huge one
    (`_FREE_PRICE`) buys nothing (`gold = 0`) for the slowest self-sourced plan.
  - *epsilon-constraint* (`time_cap` set): minimise `gold` subject to `makespan ≤
    time_cap`. This reaches the **interior** of a flat frontier segment; the price
    sweep alone collapses onto endpoints.
  Crafting/opening are free LP flows; passive is one aggregate offer whose bundle
  is per-minute rates (drives on the slowest bound item). An offer's `consumes`
  are subtracted in each item's balance like a craft's materials, so a grounded
  explore/fish/GJ-grow must itself source the Arnold Palmer / Large Net / Grape
  Juice it burns.
  - **Buying** (`buy` vars, gold): a buyable consumable (`BUYABLE` — AP/LN/cider/OJ;
    cookie sets via the `cookie_sets` cap + a purchase) gets a `buy` variable adding
    free, instant supply to its balance at `prices` gold cost, the alternative to
    self-sourcing it. `gold = Σ buy*price`. A **`buy ≤ derived`** floor per buyable
    item (derived = its material + channel + open consumption) keeps purchases to
    non-goal uses, so buying can never earn an item's own mastery — a demanded
    consumable is crafted/gathered. `_obtainable` seeds the `buyable` set (a buyable
    is always obtainable), so a channel grounded in one is usable even where nothing
    crafts it; `_reachable` walks from a wanted drop into the consumables it burns.
  - `inventory` credits every item's balance **except directly-demanded ones**
    (their need is already net of stock; stamina is seeded by `--current-stamina`).
  - **Refinement** (determinism): pin makespan, minimise `gold`, then minimise
    activity (crafts + gathering + buys) — pins `(time, gold)` then sheds slack.
- **Gold prices** (`config.trading_prices`, the `[trading]` section): per-unit gold
  for each buyable consumable (AP/LN/cider/OJ per-1000 → ÷1000; cookie sets per
  unit; Grape Juice at its production cost, kept for `plan_gold`'s market value but
  never bought). Absent, nothing is buyable and every plan is fully self-sourced.
  `plan_gold(plan, prices)` is the market value of **all** consumables spent;
  **`plan_gold_breakdown`** splits it exactly into `(to_buy, self_sourced)` =
  `(plan.gold, plan_gold − plan.gold)` — to-buy is what the LP actually bought.
  `main.cheapest_plan` ranks by `plan.gold` (least bought).
- **`frontier(...)`** = fast endpoint (`gold_weight=_FAST_PRICE`, buy freely) + cheap
  endpoint (`_FREE_PRICE`, self-source, `gold = 0`) + an epsilon sweep of makespan
  caps (each minimising gold), then `_non_dominated` then `_thin` to `count`. Two
  cap sources feed the sweep:
  - `_time_caps` — geometric spacing between the endpoints.
  - `_passive_completion_caps` — the **passive-resource breakpoints** (see
    below), also passed to `_thin` as **anchors** so they survive thinning
    (smallest-first, since useful tradeoffs cluster at the fast end).
    `passive_pet_items` are excluded (pet trickle isn't a real plateau).
- `_dominates`/`_non_dominated` prune by the `cost` tuple `(time, gold)`; dedup key
  rounds both.

### Passive-resource breakpoints — the frontier's spine

A building material (Steel Wire, Oak) is needed only up to its products' full
demand. **`passive_residuals(plan, ...)`** gives each passive item's net
requirement in a plan; computed on the **cheapest plan** (crafts the most) it is
that item's *full* requirement. `residual/rate` = the makespan at which it
finishes gathering — a natural plateau. So the frontier segments by resource:
fast crop-bound → Steel Wire's full completion → Oak's → cheapest. `main.run`
passes `_pet_dominated_items(production)` to exclude pet materials, and passes the
cheapest plan's `passive_residuals` into `print_plan` as `max_passive_residuals`.
It also builds the frontier table's per-plan **reason** (`_frontier_reasons` →
"all Steel Wire from passive") from the same `_defining_passive_resource`, so the
table label and the chosen plan's critical path agree.

## Critical-path naming (report.print_critical_path) — read this before touching it

This logic took many iterations; the precedence is deliberate. Tracks run
concurrently; fishing/exploring are instant, so the makespan is a **farming** or
**passive** wait. Decision order in `print_critical_path`:

1. **Defining passive resource** (`_defining_passive_resource`, takes the plan's
   passive `residuals`) — a passive item whose residual has reached its **full**
   requirement (`max_passive_residuals`, within `_FULL_REQUIREMENT_MARGIN`) **and**
   completes at the makespan. This is the gate ("its full requirement, after which
   no more is needed"). At the 27d plan Steel Wire is at full (named) while Oak is
   reduced/sped-up (not). Pet-made items are skipped here (pet-primary falls to
   step 3). Also drives the frontier table reason via `_frontier_reasons`.
2. else **farming drives** (`_farming_drives`) — farming co-maximal within
   `_FARMING_COMAX_MARGIN` **and** its longest crop is *not* fill. A crop is
   **fill** (`_binding_crop_is_fill`) if a consumable channel could supply it
   (`sourceable_elsewhere`) — then farming is just filling a passive-forced wait,
   so passive drives instead.
3. else **slowest passive item** (`_slowest_passive_item`) — but a **pet
   top-up** is skipped: a pet supplying most of an item's *passive* rate yet a
   minority of its *total* need is a last resort, not a bottleneck, unless the
   pet is the item's primary source.

`_farming_drives` is shared by the critical-path line and the **Passive
production** section's ordering (lead item = the critical one) so they agree.
`_pet_passive_rate` distinguishes pets (name-prefixed `PET_PRODUCER_PREFIX`) from
buildings.

## Printing conventions (report.print_plan)

- Sections: Fishing, Exploring, Opening (Locksmith), Farming, Passive production,
  remainder. Counts are **right-aligned per section** (`_num_width`; the `x`
  lines up; not shared across sections).
- Cookie farming: `_print_cookie_farming`; a cookie set is a **whole real day**
  shared across crops, so the count is `ceil(total cookie-days)`
  (`_cookie_sets_used`), not per-crop rounding.
- Passive lines name producers via `_passive_sources`; seasonal window flagged by
  `_season_note`. Only items the plan actually needs are shown.
- **Buy section** (`print_plan`, with trading `prices`): lists `plan.bought` and
  the gold each purchase costs, totalling `plan.gold`.
- **Surplus section** (`_print_surplus_value`, with trading `prices`): passive
  overshoots every resource but the slowest, so a plan banks materials past its
  goal. `optimizer.plan_leftovers` gives each item's leftover (supply past all
  consumption); `optimizer.surplus_sale_value` then runs one small **max-gold LP**
  crafting those leftovers into the sellable priced goods (AP, Large Net, Apple
  Cider, Orange Juice — Grape Juice and cookie sets excluded, no sale recipe),
  allocating shared materials to the richest good. Lists each good with its gold
  and the nominal total. Rigorous for every plan: consumables are always grounded,
  so a plan self-sources exactly what it needs (net of buying) and the leftovers
  are genuine spare capacity (naturally zero when the binding materials are used
  up).

## Invariants & gotchas

- **Instant-grow**: crops with base growth ≤ `INSTANT_GROW_MAX_BASE` (5m) grow in
  `instant_grow_time` (~3s); GJ is never spent on them.
- **Cookies**: full set = `COOKIE_SET_MULTIPLIER` (9×) yield in a 5-min window,
  used only with GJ, on crops ≥ `cookies_grow_time_threshold`.
- `Draw.produced` overproduces fast passive items — use `passive_residuals` for
  "how much passive actually needs to make", not `produced`.
- `recipe_flows`/optimizer recipes are already lower-cased and IGNORED_MATERIALS
  (iron, nails) stripped.
- The frontier's whole tradeoff is `(time, gold)`; that pair is `SourcingPlan.cost`
  and the dedup key. A consumable's spend is not itself a Pareto axis — only the
  gold spent buying it is.
- **Buying can't earn mastery**: a demanded (goal) consumable must be crafted or
  gathered; `buy ≤ derived` confines purchases to non-goal (tool/material) uses.

## Dependency

One runtime dep: **PuLP** (with its bundled CBC solver). Chosen over SciPy/HiGHS
because it installs from a pure-Python wheel on 3.14 with no numpy/BLAS/ABI risk,
its declarative model reads like the codebase, and flipping units to integer
(MILP — whole nets/palmers, instead of rounding the LP solution at reporting) is a
one-keyword change. We solve only a handful of tiny LPs per run, so CBC's
subprocess overhead is immaterial. NSGA-II/pymoo was rejected: the LP frontier is
piecewise-linear/convex, so scalarise-and-sweep recovers every vertex exactly.

## Dev workflow

- Run: `uv run pytest -q` (tests in `tests/`). Lint: `uvx ruff check .`.
- Try it: `python main.py` (default), `-i Item:N`, `-m Item:N`, `--plan K`,
  `--plans N`, `--cookie-sets N`.
- Commit directly to `main`; end commit messages with the Co-Authored-By trailer.
