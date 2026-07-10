# Architecture — code map

Navigational map of the FarmRPG **farm-solver** LP optimiser, for finding code
fast. README.md covers *behaviour/config* (user-facing); this file is the
*where-does-X-live* index. Reference by function name (stable); line numbers
drift.

## What it does

Given a FarmRPG mastery/inventory state and a goal (tower level, `-m` mastery, or
`-i` item counts), work out the ways to source everything, **trading time against
net gold**: self-sourcing a consumable (Arnold Palmer / Large Net / Apple Cider /
Orange Juice / cookie set / Grape Juice) costs makespan, buying it costs gold, and
a slow plan that leans on passive production banks sellable surplus (a gold
*credit*, so its net gold can go negative). It prints a Pareto **frontier** of
plans from "buy freely, fastest" through "buy nothing, self-source" to "lean on
passive, bank surplus, slowest", plus a per-channel breakdown of a chosen one.

**Always grounded.** Every consumable is a real item: exploring/fishing/GJ-farming
offers carry a `consumes` input (Arnold Palmer / Large Net / Grape Juice, chained
down to Orchard Lemons, passive stone/wood/straw/antlers, and Vineyard Grapes), and
the **Apple Cider** exploring channel (`sources.cider_offers`) is always present,
fed by **stamina** from drinking Orange Juice (100 each) or eating Apples (15 each)
(`sources.stamina_offers`, the `DRINKING_TRACK`; `--current-stamina` seeds a
starting balance) and from the
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
   (masteries + inventory) from the pasted-in text files, then
   `main.apply_inventory_overrides` forces any `--override-inventory` items to
   their given held amount, replacing whatever the file said.
2. **Goal → demand** — `farm_solver.solve_tower/solve_mastery/solve_acquire`
   produce a `Solution` with `ItemNeed`s; without `--target`, `-m`/`-i` replace
   the tower goal (as before), and `farm_solver.solve_goals` combines them when
   both are given (each just contributes its own need list). With `--target`
   given, `farm_solver.solve_target` instead concatenates the tower's needs
   with `solve_goals`' extra `-m`/`-i` need list. `crafting.top_level_demand` is
   what reduces same-item needs (whatever the mix of tower/`-m`/`-i`) to one
   demand figure: converted to a common "items produced" unit (a mastery
   shortfall of `n` becomes `n / mastery_bonus`, a plain `-i` count stays `n`),
   every need for one item takes the *largest*, never a sum — a found item
   counts toward mastery and sits in inventory both, so producing enough for
   the bigger of two goals clears the smaller as a side effect (a weaker `-m`
   target than the tower's own is absorbed; an `-i` floor only takes over once
   it exceeds what a mastery goal already produces). This is separate from a
   plain (non-mastery) item's ordinary crafting consumption, which the LP
   balance in `optimizer.minimize` adds on top regardless — an `-i` floor on a
   pure material (never itself a mastery goal) still means "this many spare
   after whatever crafting consumes";
   `crafting.recipe_flows` gives the craft graph (IGNORED_MATERIALS stripped,
   lower-cased).
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
| `main.py` | CLI, orchestration (no printing) | `run`, `build_parser`, `parse_target`, `resolve_files` (falls back to `_latest_dated_file` on `solver.data_directory`), `selected_plan`, `cheapest_plan`, `resolve_pumpkin_juice`, `resolve_fungus_processing`, `resolve_scythe_of_dewstar`, `resolve_dewstars_journal`, `resolve_monthly_grants`, `apply_inventory_overrides`, `pumpkin_juice_gains`, `pumpkin_juice_schedule` |
| `report.py` | **all printing** + critical-path naming | `print_frontier`, `print_plan`, `print_critical_path`, `build_display`, `_defining_passive_resource` |
| `optimizer.py` | the LP and the frontier | `SourcingPlan` (`passive_draw`, `track_times`, `cost`), `minimize`, `frontier`, `plan_balance`, `passive_residuals`, `plan_gold`, `plan_gold_breakdown`, `plan_leftovers`, `leftover_inventory_share`, `surplus_sale_value`, `plan_surplus_gold` |
| `sources.py` | offer/draw model, per-channel offer builders | `Offer`, `Draw`, `OpenFlow`, `*_offers`, track constants |
| `farm_solver.py` | game state, goal→needs | `GameState`, `Inventory`, `Solution`, `solve_tower`/`solve_mastery`/`solve_acquire`/`solve_goals`/`solve_target`, `has_tower_level` |
| `parsing.py` | parse pasted mastery/inventory text | `_split_blocks`, `_parse_mastery_block` |
| `crafting.py` | workshop recipes, demand expansion | `Workshop`, `recipe_flows`, `top_level_demand`, `IGNORED_MATERIALS` |
| `crops.py` | farming model (grow time, GJ, cookies, instant-grow, Dewstar) | `Crop`, `Farm` (`dewstar_bonus`, `dewstar_cookie_bonus`), `COOKIE_*`, `INSTANT_GROW_MAX_BASE` |
| `passive.py` | passive producers, durations | `PassiveProducer`, `PassiveProduction` (`producer_rates`, `rate_per_minute`), `parse_duration`, `format_duration` |
| `pets.py` | pets as (seasonal) passive producers | `Pet`, `Pets`, `seasonal_fraction` |
| `config.py` | `config.toml` → producers/pets/storehouse/trading | `passive_production` (`monthly_grants` flag), `PET_PRODUCER_PREFIX`, `MONTHLY_GRANTS`/`_monthly_grant_producers`, `storehouse_size`, `trading_prices` |
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
  `bought` (`{item: units}` purchased), `gold` (`Σ bought*price`, **incl. Pumpkin
  Juice**), `pumpkin_juices` (`{goal item: count}` juices drunk), `pumpkin_gold`
  (their slice of `gold`), `pumpkin_credit` (`{goal item: demand units}` a juice
  covers, netted out of `need` in `plan_balance`), `surplus_gold`
  (sale value of banked surplus, set by `frontier`) and `remainder`.
  `.cost` is the pair `(time, net gold)` where **net gold = `gold − surplus_gold`**
  — bought less what the plan banks — so a slow, passive-leaning plan that banks
  more than it buys sits at *negative* gold and the frontier extends past the "buy
  nothing" line. Two plans equal on both are equivalent however they mix making,
  buying and banking.
- **`sources.OpenFlow(name, consumes, yields)`** — opening a chest/bag: a free,
  instant multi-output LP flow.
- **`crops.Farm`** — grow times, `grape_juices_for`, `is_instant`,
  `takes_cookies`, `cookie_day_yield`. **`passive.PassiveProduction`** —
  `producers` list, `rate_per_minute`, storehouse cap.

## The LP and frontier (optimizer.py)

- **`minimize(...)`** solves one LP, trading makespan against **gold**. Three modes:
  - *weighted-sum* (default): minimise `time + gold_weight*gold`. A tiny
    `gold_weight` (`_FAST_PRICE`) buys freely for the fastest plan; a huge one
    (`_FREE_PRICE`) buys nothing (`gold = 0`) for the fastest self-sourced plan.
  - *epsilon-constraint* (`time_cap` set): minimise `gold` subject to `makespan ≤
    time_cap`. This reaches the **interior** of a flat frontier segment; the price
    sweep alone collapses onto endpoints.
  - *favour_passive* (`favour_passive=True`, the slow regime): minimise `spend_value`
    — the market value of every consumable spent — so the plan leans as hard as it
    can on free passive/farming, spends the fewest tools and banks the
    over-production. `time_cap` bounds how far it runs. Its refinement pins
    `spend_value` (not makespan), then gold, then activity — so it self-sources and
    the makespan settles at just what passive needs, instead of buying tools to
    finish early and undoing the passive lean.
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
  - **Pumpkin Juice** (`pj` vars, gold): a bought mastery boost. `pj_gain` (from
    `main.pumpkin_juice_gains` = `0.1*threshold/mastery_bonus`) is the demand units one
    juice covers for a goal item; a `pj` var per reachable goal adds that supply at
    `pj_price` gold each, capped at fully covering the goal (`pj*gain ≤ need`) and by an
    optional `pj_limit` total. Its gold folds into `gold`; **off in `favour_passive`**
    (the buy-nothing regime). `plan_balance` nets `pumpkin_credit` out of `need` so the
    residual/passive accounting reflects the boost, not passive, covered that slice.
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
  **`plan_surplus_gold`** = `Σ surplus_sale_value(plan_leftovers(...))`, the sale
  value of what a plan banks; `frontier` stores it on each plan as `surplus_gold`,
  so `cost`'s net gold nets it off (see the `SourcingPlan` bullet). `main.cheapest_plan`
  ranks by **net** gold (`plan.gold − plan.surplus_gold`) — buys least, banks most.
- **`frontier(...)`** spans two regimes meeting at the cheap endpoint, then credits
  each plan's `surplus_gold` (`plan_surplus_gold`) so `cost` is net, then
  `_non_dominated` then `_thin` to `count`:
  - **Fast regime** `[t_fast, t_cheap]` — fast endpoint (`gold_weight=_FAST_PRICE`,
    buy freely) + cheap endpoint (`_FREE_PRICE`, self-source, `gold = 0`) + an epsilon
    sweep of makespan caps (each minimising gold). Two cap sources:
    - `_time_caps` — geometric spacing between the endpoints.
    - `_passive_completion_caps` — the **passive-resource breakpoints** (see below),
      also passed to `_thin` as **anchors** so they survive thinning (smallest-first,
      since useful tradeoffs cluster at the fast end). `passive_pet_items` excluded.
  - **Slow regime** `[t_cheap, t_bound]` (only with `prices`) — a `favour_passive`
    sweep past the cheap endpoint, leaning ever harder on passive and banking surplus
    so the net gold drops below zero. `t_bound` = the slowest **non-pet** passive
    completion, read from one uncapped `favour_passive` **probe** (whose own makespan
    would chase the pet tail — centuries — but whose non-pet breakpoints are the real
    completions). `maxpassive` is the `t_bound` endpoint; the extension breakpoints
    join the `_thin` anchors. Bounding at `t_bound` is what stops the slow end
    offloading demand onto a pet trickle.
- `_dominates`/`_non_dominated` prune by the `cost` tuple `(time, net gold)`; dedup key
  rounds both.

### Passive-resource breakpoints — the frontier's spine

A building material (Steel Wire, Oak) is needed only up to its products' full
demand. **`passive_residuals(plan, ...)`** gives each passive item's net
requirement in a plan; computed on the **fastest fully self-sourced plan** (the
cheap endpoint — buys nothing, moderate strategy) it is that item's *full*
requirement. `residual/rate` = the makespan at which it finishes gathering — a
natural plateau. So the frontier segments by resource: fast crop-bound → Steel
Wire's full completion → Oak's → cheapest. `main.run` passes
`_pet_dominated_items(production)` to exclude pet materials, and passes that
reference plan's `passive_residuals` into `print_plan` as `max_passive_residuals`.
It picks the reference as the fastest plan with `round(gold) == 0` — **not** the
slowest (cheapest-net) plan: the slow, passive-leaning extension can adopt an
extreme strategy (cider stamina from Apples not Orange Juice) that *understates* an
item's requirement, so it is a poor yardstick. It also builds the frontier table's
per-plan **reason** (`_frontier_reasons` → "all Steel Wire from passive") from the
same `_defining_passive_resource`, so the table label and the chosen plan's
critical path agree.

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
  From inventory (held consumables), Buy (with gold), Pumpkin Juice (mastery boost),
  Surplus, remainder. The Buy total is `plan.gold` **net of `pumpkin_gold`** (the
  juice is listed apart); the frontier table gains a **Pumpkin Juice** column
  (`_pumpkin_juices`) when a plan drinks any. The Pumpkin Juice section names the
  **mastery levels to drink each juice at** (`main.pumpkin_juice_schedule`: `target −
  k·pj_gain`, the LP's own `pj_gain = 0.1·threshold/mastery_bonus`, clamped ≥ progress)
  so each lands its boost and the last hits the goal; no wall-clock time
  is given (mastery accrues from instant crafts, so there is no reliable per-juice
  time to attach). Counts
  are **right-aligned per section** (`_num_width`; the `x` lines up; not shared
  across sections).
- **Exploring/Fishing/Farming zone lines** show each item a draw yields with its
  count when the item is in `interesting` (`need ∪ crafted`) **or**
  `optimizer.draw_consumed(plan)` — what any draw's `consumes` uses (Apples eaten,
  Orange Juice drunk, Large Nets/Arnold Palmers/Apple Cider burned, ...). Those
  items never reach crafting `need` (only `plan.crafts` materials do), but
  exploring/fishing/farming for one is still the reason the zone/crop is worth
  it, so `sourced` (the residue-draw filter) checks the same union — a draw
  whose only real output is an eaten/drunk byproduct no longer looks like
  residue and vanishes from the report.
- **`open_worthy`** = `need ∪ spent_consumables ∪ draw_consumed(plan)` gates what
  an open shows: an openable's yields are byproducts *unless* needed, a **tool**
  the plan spends (Large Net / Arnold Palmer / Apple Cider / Orange Juice / Grape
  Juice), or something a draw goes on to consume (a chest opened for the Apples
  it eats for stamina). None of these are crafting `need`, so without this an
  open done purely for one — a Mystical Chest opened for its Large Nets, a Fruit
  Chest opened for its Apples — would vanish from the report entirely. The
  **From inventory** section then shows each tool's held portion (`min(stock,
  spent)`), so the spend reconciles: held + opened + crafted + bought.
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
  and the nominal total — the same figure `plan_surplus_gold` sums for the frontier
  row's `(bank B, net N)` credit and the plan's net `cost`. Rigorous for every plan:
  consumables are always grounded,
  so a plan self-sources exactly what it needs (net of buying) and the leftovers
  are genuine spare capacity (naturally zero when the binding materials are used
  up). Each good's count and the total gold also split into *inventory* (already
  held) vs *accumulated* (what the plan itself produced), via
  `optimizer.leftover_inventory_share` — held stock is assumed spent before
  anything the plan makes (same convention as the "From inventory" consumables
  note), so a directly-demanded item contributes nothing (its stock already nets
  out of `demand` in `plan_balance`).

## Invariants & gotchas

- **Instant-grow**: crops with base growth ≤ `INSTANT_GROW_MAX_BASE` (5m) grow in
  `instant_grow_time` (~3s); GJ is never spent on them.
- **Cookies**: full set = `COOKIE_SET_MULTIPLIER` (9×) yield in a 5-min window,
  used only with GJ, on crops ≥ `cookies_grow_time_threshold`.
- **Scythe of Dewstar**: an account perk, not a held item, so unlike Fungus
  Processing and You or Dewstar's Journal it has no inventory presence to
  auto-detect — `resolve_scythe_of_dewstar` instead falls back to
  `farm_solver.has_tower_level(state, requirements, DEWSTAR_TOWER_LEVEL)` (tower level
  250): every mastery requirement through that level already met
  stands in for holding it. `has_tower_level` is a generic "has this tower level"
  query (no shortfall through it via `solve_tower`), meant for other
  level-gated auto-detections too. `Farm.dewstar_bonus` adds `4 * rows` bonus
  crops per harvest, split
  uniformly across every crop no slower than the one grown (compared on raw,
  unmodified `Crop.growth` — not the perked `grow_time`), Mushroom excluded; a
  picked Mega seed still gives one plain item, not its own tenfold yield.
  Without Dewstar's Journal (a held item, auto-detected as usual) the bonus
  ignores the Cookie window entirely (flat per-harvest, all day); with it,
  `dewstar_cookie_bonus` reuses
  `_cookie_boosted_harvests` so the bonus gets the same 9× windowed boost as the
  crop's own `cookie_day_yield`.
- **Monthly grants** (tower level 300 artifact): `main.resolve_monthly_grants`
  auto-detects it like the Scythe (via `has_tower_level`, config override
  `exploring.have_mechanical_heart`), and `config.passive_production(monthly_grants=…)`
  adds `config.MONTHLY_GRANTS` (3000 Apple Cider on the 7th, 2000 Arnold Palmer on
  the 14th of each month) as daily producers named `MONTHLY_GRANT_PRODUCER`. Each
  grant is spread to an average daily rate over the plan span (steady-state monthly
  average with no span; exact in-window grant-days otherwise, like a seasonal pet),
  so it rides the same `model_calendar` fixed-point iteration.
- `Draw.produced` overproduces fast passive items — use `passive_residuals` for
  "how much passive actually needs to make", not `produced`.
- `recipe_flows`/optimizer recipes are already lower-cased and IGNORED_MATERIALS
  (iron, nails) stripped.
- The frontier's whole tradeoff is `(time, net gold)` where net gold =
  `gold − surplus_gold`; that pair is `SourcingPlan.cost` and (rounded) the dedup
  key. A consumable's spend is not itself a Pareto axis — only the gold spent buying
  it, less the sale value of what the plan banks, is. `minimize` still optimises on
  raw bought `gold`; `frontier` credits the surplus afterwards, so the two endpoints
  and sweep are unchanged but their (and every plan's) `cost` is re-scored net.
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
