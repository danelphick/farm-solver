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

`main.run(args)` orchestrates, roughly, by delegating most of the work to three
stage functions (split out so a non-CLI caller — the web app, see below — can
drive the same pipeline from an already-built `GameState` instead of files):
`main.build_inputs(args, cfg, state)` does goal-solving and channel/demand setup,
returning a `SolveInputs` (state, farm/explore/fish/workshop, the solved
`Solution`, recipes/smith/open_flows, demand, inventory, cookie_sets, prices,
pumpkin-juice terms, `model_calendar`, `start`); `main.solve_frontier(inputs,
args, cfg, horizon_seed=None)` runs the seasonal fixed-point loop and the
frontier sweep, returning a `FrontierResult` (`plans`/`within`/`shown`, `offers`,
`production`, the converged `horizon` — `horizon_seed` lets a caller resume the
fixed point instead of restarting it, used by the web API's horizon-guard
re-solve); `main.derive_report_inputs(inputs, fr, args)` computes the report-only
context (`cheapest`, `display`, `max_passive_residuals`, `reasons`, `golds`,
`sourceable_elsewhere`) into a `PlanContextData`. `run()` itself keeps setup,
file loading and the `--unobtainable` early exit, then calls the three in order
and drives the `Reporter` off their results:

1. **Load state** — `main.load_state` → `parsing` builds `farm_solver.GameState`
   (masteries + inventory) from the pasted-in text files, then
   `main.apply_inventory_overrides` forces any `--override-inventory` items to
   their given held amount, replacing whatever the file said.
2. **Goal → demand** — `farm_solver.solve_tower/solve_mastery/solve_acquire`
   produce a `Solution` with `ItemNeed`s. `build_inputs` computes a `goals_only`
   flag = `args.target == "none"` **or** (`args.target is None` and `-m`/`-i`
   set) — i.e. `--target none` drops the tower explicitly, and omitting
   `--target` does the same implicitly when `-m`/`-i` are set (the previous
   behaviour). When `goals_only`, `farm_solver.solve_goals` combines `-m`/`-i`
   with no tower (each just contributes its own need list; empty when neither is
   set). Otherwise (`--target tower`/`N`, or a bare tower run),
   `farm_solver.solve_target` concatenates the tower's needs with `solve_goals`'
   extra `-m`/`-i` need list. `parse_target` accepts `tower`/`none`/`N`/`TN`. `crafting.top_level_demand` is
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
5. **Print** — `main.run` picks a `report.Reporter` from `--format`
   (`report.build_reporter`; `TextReporter` prints immediately as before,
   `MarkdownReporter` buffers into a `MarkdownDoc`) and calls its `.frontier`
   (the table), `.plan` for the selected plan (critical path + per-channel
   actions), optional `.crafts`, then `.finish()` (a no-op for text; for
   Markdown, renders through `rich` to a terminal or prints the raw Markdown
   source otherwise). All printing lives in `report.py`; `main.run` calls it
   only through the `Reporter`.

The whole thing loops up to `_MAX_SEASON_ITERS` when `model_calendar` is on, since
a seasonal pet's yield depends on the plan span (a fixed point). In the default
selection case (no `--plan`/`--cheapest-plan`/limit — the selected plan is always
`shown[0]`, the fastest), `main.run` seeds that fixed point on repeated
`optimizer.fastest_endpoint` calls (one `minimize`, not a full frontier) and hands
the converged horizon to the loop above, which then runs the real frontier once
and breaks on its own convergence check. Any limit or explicit `--plan` can select
a different plan than the fastest, so that case falls back to iterating the full
frontier as before.

## Module map

| File | Owns | Key names |
| --- | --- | --- |
| `main.py` | CLI, orchestration (no printing) | `run` (setup + file loading + the `--unobtainable` branch, then delegates to the three stage functions below), `build_inputs` → `SolveInputs`, `solve_frontier` → `FrontierResult`, `derive_report_inputs` → `PlanContextData`, `build_parser`, `parse_target`, `resolve_files` (falls back to `_latest_dated_file` on `solver.data_directory`), `selected_plan`, `cheapest_plan`, `resolve_pumpkin_juice`, `resolve_fungus_processing`, `resolve_scythe_of_dewstar`, `resolve_dewstars_journal`, `resolve_monthly_grants`, `resolve_calendar_mode`/`strip_dated_sources` (the `solver.calendar` full/average/ignore enum), `apply_inventory_overrides`, `pumpkin_juice_gains`, `pumpkin_juice_schedule` |
| `webapi.py` | framework-free web API layer (no Django import; drives `main`'s stage functions from pasted text instead of files) | `params_to_args` (JSON `params` → `argparse.Namespace`), `_CONFIG_PARAM_FLAGS` (Trading-values price params + `instant_grow_time` → `args.override` entries, so a price change re-solves via the `override` RESOLVE_FIELD), `fingerprint` (session cache key), `RESOLVE_FIELDS`/`REFILTER_FIELDS`, `SolveSession`/`SolveResult`, `solve` (full re-solve), `render` (refilter against a cached `SolveSession`, with the one horizon-guard re-solve edge case) |
| `report.py` | **all printing** + critical-path naming | `Reporter`/`TextReporter`/`MarkdownReporter`/`SectionedReporter`/`build_reporter` (the `--format` facade), `print_frontier`/`print_plan`/`print_critical_path` (text) and their `markdown_*` counterparts, `MarkdownDoc`, `_build_plan_context`/`_PlanContext` (setup shared by both renderers), `build_display`, `_defining_passive_resource` |
| `optimizer.py` | the LP and the frontier, LP backend choice | `SourcingPlan` (`passive_draw`, `track_times`, `cost`), `_Base`/`_build_base`/`_solve_configured` (`minimize`'s split, see below), `minimize`, `frontier`, `fastest_endpoint`, `self_sourced_endpoint`, `plan_balance`, `passive_residuals`, `plan_gold`, `plan_gold_breakdown`, `plan_leftovers`, `leftover_inventory_share`, `accumulated_leftovers`, `surplus_sale_value`, `plan_surplus_gold`, `set_solver_backend` |
| `sources.py` | offer/draw model, per-channel offer builders | `Offer`, `Draw`, `OpenFlow`, `*_offers`, `sawmill_boost_offers` (Hickory Omelette boost, `SAWMILL_BOOST_TRACK`/`HICKORY_OMELETTE`), track constants |
| `farm_solver.py` | game state, goal→needs | `GameState`, `Inventory`, `Solution`, `solve_tower`/`solve_mastery`/`solve_acquire`/`solve_goals`/`solve_target`, `has_tower_level` |
| `parsing.py` | parse pasted mastery/inventory text | `_split_blocks`, `_parse_mastery_block` |
| `crafting.py` | workshop recipes, demand expansion | `Workshop`, `recipe_flows`, `top_level_demand`, `IGNORED_MATERIALS` |
| `crops.py` | farming model (grow time, GJ, cookies, instant-grow, Dewstar) | `Crop`, `Farm` (`dewstar_bonus`, `dewstar_cookie_bonus`), `COOKIE_*`, `INSTANT_GROW_MAX_BASE` |
| `passive.py` | passive producers, durations | `PassiveProducer`, `PassiveProduction` (`producer_rates`, `rate_per_minute`), `parse_duration`, `format_duration` |
| `pets.py` | pets as (seasonal) passive producers | `Pet`, `Pets`, `seasonal_fraction` |
| `config.py` | `config.toml` → producers/pets/storehouse/trading | `passive_production` (`monthly_grants` flag), `sawmill_outputs`/`hickory_omelette_mode` (the `[sawmill] hickory_omelette` = none/all/only_oak enum, boost built as offers not baked here), `PET_PRODUCER_PREFIX`, `MONTHLY_GRANTS`/`_monthly_grant_producers`, `storehouse_size`, `trading_prices` |
| `fishing.py`/`exploring.py` | LN/AP channels, modifiers | `Fishing`/`Exploring` (`acorn_flag`/`acorn_table`), `CiderModifiers`, `*_from_config`, `ACORN_PIE_FLAG`/`FUNGUS_ACORN_PIE_FLAG`/`ACORN_PIE_EXPLORES` |
| `locksmith.py` | chests/bags openables | `Openable`, `Locksmith`, `load_openables` |
| `farming.py` | which items are farmed | `FARMING_ITEMS`, `is_farmed` |
| `web/` | Django web app over `webapi.py` (see "Web app (web/)" below) | `solverapp.views.api_solve`, `solverapp.cache.{SessionCache,SOLVE_SEMAPHORE,key_lock}`, `solverapp.apps.SolverappConfig.ready` |

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
  - **`_build_base`/`_solve_configured` split**: `minimize` itself is `_build_base`
    (once) + one `_solve_configured` call. `_build_base` builds everything a sweep
    holds fixed call to call — variables, the balance/track constraints,
    `gold`/`spend_value` — and returns a `_Base`. `_solve_configured` adds the
    per-call bit (the objective, an optional `makespan ≤ time_cap`, `favour_passive`'s
    `pj ≤ 0` neutralisation) then runs the refinement above and reads out the
    `SourcingPlan`. `frontier` (below) reuses one `_Base` across its ~80-call sweep
    instead of rebuilding the LP from scratch per call (`plans/MODEL_REUSE_PLAN.md`).
    Every constraint a `_solve_configured` call adds is named and recorded in
    `base.ephemeral`; the *next* call on that base pops exactly those names first —
    **pop happens at the top of the call, not the end**, so a call that raises
    partway through never corrupts the base for a call that never happens. Since
    `pj` variable existence used to be gated on `favour_passive`, but one base must
    serve calls in both modes, `_build_base` now builds `pj` unconditionally and
    `_solve_configured` neutralises it per call instead (`pj[item] ≤ 0`, ephemeral,
    when `favour_passive`). Gotcha: `LpConstraint.name`'s setter sanitises the name
    (spaces/punctuation → `_`), so an ephemeral name built from an item string (e.g.
    `f"pj_off_{item}"` for `"fancy violin"`) must be read back off the constraint
    object (`constraint.name` after assignment) before it goes in `ephemeral`, or
    the recorded name won't match what `problem.constraints` actually stores and the
    next call's pop silently misses it. Gotcha 2: the pop itself
    (`problem.constraints.pop(name, None)`) uses PuLP's dict-style `.constraints`
    access, which is **deprecated** (PuLP 4.0 returns a list from
    `prob.constraints()` instead, with no replacement removal API yet as of PuLP
    3.3.2). Works correctly today on both `highs` and `cbc`, but is a forward-
    compatibility risk sitting at the core of this reuse mechanism — watch for it
    on the next PuLP upgrade. The `DeprecationWarning` only shows under `uv run
    pytest -q`, not a plain script run (Python suppresses `DeprecationWarning`
    outside `__main__` by default), which is why it wasn't caught until after
    the fact.
- **Gold prices** (`config.trading_prices`, the `[trading]` section): per-unit gold
  for each buyable consumable (AP/LN/cider/OJ/hickory-omelette per-1000 → ÷1000;
  cookie sets per unit; Grape Juice at its production cost, kept for `plan_gold`'s
  market value but never bought). Absent, nothing is buyable and every plan is fully
  self-sourced.
  `plan_gold(plan, prices)` is the market value of **all** consumables spent;
  **`plan_gold_breakdown`** splits it exactly into `(to_buy, self_sourced)` =
  `(plan.gold, plan_gold − plan.gold)` — to-buy is what the LP actually bought.
  **`plan_surplus_gold`** = `Σ surplus_sale_value(accumulated_leftovers(...))`, the
  sale value of what a plan **accumulates** past its goal; `frontier` stores it on
  each plan as `surplus_gold`, so `cost`'s net gold nets it off (see the
  `SourcingPlan` bullet). `main.cheapest_plan` ranks by **net** gold
  (`plan.gold − plan.surplus_gold`) — buys least, banks most.
  **`accumulated_leftovers`** = `plan_leftovers` minus each item's
  `leftover_inventory_share`: pre-existing inventory sitting unused is **not** counted
  as surplus the plan banks (otherwise every plan on a save with a big consumable
  stockpile shows a large phantom credit and a misleadingly negative net gold). Only
  what executing the plan produces beyond all it consumes (passive, crafts, opens,
  gathering) is credited — the same figure the report's Surplus section now shows
  (accumulated-only; the old inventory/accumulated split is gone).
- **`frontier(...)`** spans two regimes meeting at the cheap endpoint, then credits
  each plan's `surplus_gold` (`plan_surplus_gold`) so `cost` is net, then
  `_non_dominated` then `_thin` to `count`. It builds two `_build_base` bases up
  front — `base_active` (`favour_passive=False`) shared by the fast endpoint, cheap
  endpoint and every epsilon-cap solve; `base_passive` (`favour_passive=True`)
  shared by the probe and every extension-cap solve — and drives every solve
  through `_solve_configured` against one of them, instead of a fresh `minimize`
  (full LP rebuild) per call:
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

## Two renderers, one set of decisions (report.py)

`--format markdown` gets its own `markdown_*` function per `print_*` function
(`markdown_frontier`/`print_frontier`, `markdown_plan`/`print_plan`, ...), but
they never duplicate a *decision* -- only how a row is laid out differs. The
shared pieces:

- **`_build_plan_context`/`_PlanContext`** — the `need`/`elsewhere`/`crafted`
  balance (`optimizer.plan_balance`), `interesting`/`consumed` sets, spent
  consumables and `open_worthy`, computed once and read by both renderers via
  `_wanted`/`_wanted_counts`/`_sourced`/`_draws_on`.
- **`_critical_path_summary`** — the `(makespan, line)` decision `print_critical_path`
  and `markdown_plan` both print verbatim; see "Critical-path naming" below for
  the logic itself.
- **`_craft_order`** — the parents-before-materials ordering `print_crafts` and
  `markdown_crafts` both walk.
- Every other named helper below (`_defining_passive_resource`, `_farming_drives`,
  `_slowest_passive_item`, `_passive_sources`, ...) is called directly by both
  `print_plan` and `markdown_plan` -- there is no markdown-specific copy of any
  of this reasoning, only of the loop that turns its output into a printed line
  vs. a table row.

`report.Reporter` (`TextReporter`/`MarkdownReporter`, picked by
`report.build_reporter(args.format, pager=...)`) is the facade `main.run` calls
through, so it never branches on `--format` itself. A third implementation,
`SectionedReporter` (not reachable via `--format`; used by the web app, see
below), gives each section -- `solution`/`unobtainable` (shared), `notes`,
`frontier`, `plan`, `crafts` -- its own `MarkdownDoc` instead of one continuous
document, so a caller can render/refresh sections independently (e.g. a
refilter only touches the frontier and plan tables); `.finish()` is a no-op,
there being no single rendering step. It calls the exact same `markdown_*`
functions as `MarkdownReporter`, so it shares every decision with both other
reporters -- only which `MarkdownDoc` a call's output lands in differs.
`MarkdownReporter` buffers
into a `MarkdownDoc` (one block per heading/paragraph/bullet-list/table) and
only renders in `.finish()`: via `rich.markdown.Markdown` through
`rich.console.Console` when stdout is a terminal, or by printing the Markdown
source as-is otherwise -- so redirecting to a file (`> report.md`) gets real
Markdown, not rich's word-wrapped rendering of it. `--pager` (Markdown +
terminal only; `main.run` rejects it otherwise) wraps that same `console.print`
in `console.pager(styles=True)`, which captures the rendered segments and hands
them to `rich`'s `SystemPager` (`pydoc.pager` under the hood: `$PAGER`/`$MANPAGER`,
else `less`, already invoked with `LESS=-R...` for ANSI passthrough). This only
works because *we* launch the pager as a subprocess after rendering -- piping
this process's own stdout to a pager externally makes stdout a non-tty before
`.finish()` ever runs, so it takes the raw-source branch instead of the styled one.

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
  `_season_note`. Items the plan needs are shown by their `need − elsewhere`
  residual; a **consumed tool** (Arnold Palmer / Apple Cider) is never a crafting
  `need`, so it has no such residual — but when the passive track makes it (the
  Mechanical Heart's grants, a pet's haul) the line still appears, quantity =
  `min(passive_draw.produced, draw_consumed)`, so the free supply is visible.
  `_passive_sources` flags the Mechanical Heart producer `(smeared)` (via
  `_smeared_note`) since its monthly lump is modelled as an averaged daily rate,
  just as `_season_note` flags a seasonal pet's window.
- **Buy section** (`print_plan`, with trading `prices`): lists `plan.bought` and
  the gold each purchase costs, totalling `plan.gold`.
- **Surplus section** (`_print_surplus_value`, with trading `prices`): passive
  overshoots every resource but the slowest, so a plan banks materials past its
  goal. `optimizer.accumulated_leftovers` gives each item's leftover **net of stock
  already held** (`plan_leftovers` minus `leftover_inventory_share`);
  `optimizer.surplus_sale_value` then runs one small **max-gold LP** crafting those
  into the sellable priced goods (AP, Large Net, Apple Cider, Orange Juice — Grape
  Juice and cookie sets excluded, no sale recipe), allocating shared materials to
  the richest good. Lists each good with its gold and the nominal total — the same
  figure `plan_surplus_gold` sums for the frontier row's `(bank B, net N)` credit and
  the plan's net `cost`. **Only what the plan accumulates counts, never pre-held
  inventory**: an untouched stockpile is not surplus the plan produces, and counting
  it made every plan look profitable and its net gold misleadingly negative. Rigorous
  for every plan: consumables are always grounded, so a plan self-sources exactly
  what it needs (net of buying) and the accumulated leftovers are genuine spare
  capacity (naturally zero when the binding materials are used up, or when every
  leftover was already held — then the section is omitted). A directly-demanded item
  contributes nothing (its stock already nets out of `demand` in `plan_balance`).

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
  so it rides the same `model_calendar` fixed-point iteration. An active grant is a
  date-dependent source in its own right: `_should_model_calendar` treats it like a
  seasonal pet, so the calendar model (and its extra solve) engages for the grants
  even when every pet is year-round (on by default). **Calendar modes**
  (`main.resolve_calendar_mode`: config `solver.calendar`, overridden by the
  mutually exclusive `--model-calendar`/`--average-calendar`/`--ignore-calendar`):
  `full` (default) models the windows exactly; `average` smears every dated source
  to its steady-state share; `ignore` leaves them out entirely —
  `main.strip_dated_sources` drops seasonal pets from the cfg's `[pets]` in both
  callers (`main.run`, `webapi.solve`) and `build_inputs` gates the tower-derived
  `monthly_grants` off itself (they aren't in the config to strip). The old
  `solver.model_calendar` boolean is rejected with a migration error.
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
- **Acorn Pie** (a held meal, not an artifact): eating one makes a zone drop Hide
  in place of its primary drop for the next `ACORN_PIE_EXPLORES` (450) Arnold
  Palmers *or* Apple Ciders. Modelled (only for Gary's Crushroom so far) not as a
  base-table swap like Fungus Processing but as **extra offers** alongside the
  plain ones — `sources.exploring_offers`/`cider_offers` emit a `... with acorn
  pie` variant per location with an `Exploring.acorn_table` (the CSV's
  `acorn_pie` / `fungus_processing+acorn_pie` flagged rows, picked by
  `Exploring.acorn_flag` per whether Fungus Processing is active), each burning
  `1/450` of an `acorn pie` (a cider explore burns `cider_per_explore/450`) on top
  of its Arnold Palmer / Apple Cider. So the LP takes it only when it wants the
  Hide and only up to the pies held — "use whenever available, don't waste."
  Bounded by inventory in the `acorn pie` balance; the pie spend surfaces in the
  report's **From inventory** section via `draw_consumed` (it is a `consumes`, not
  a yield, so `report._build_plan_context` adds it to `spent_consumables`).
- **Hickory Omelette** (a held/bought sawmill-boost meal): the `[sawmill]
  hickory_omelette` setting (`config.hickory_omelette_mode`, enum
  none/all/only_oak; the old `use_hickory_omelette` boolean is rejected) picks how
  the sawmill's +20% (`sources.HICKORY_OMELETTE_FRACTION`) boost is used. It is
  **not** baked into passive rates any more; `sources.sawmill_boost_offers` builds
  it as a grounded offer on its own concurrent `SAWMILL_BOOST_TRACK`: one unit is a
  boosted **hour**, yielding that fraction of the boosted outputs and consuming one
  `hickory omelette` (a `BUYABLE`, priced in `[trading]` at 350 gk = 0.35/unit, or
  drawn from held stock). Its own track costs an hour of makespan per omelette, so
  the LP can't boost for more hours than the plan spans (past that the boost track
  becomes binding) — at the fast end this recovers the same +20% throughput as
  baking it into the rate, nearer the cheap end it boosts fewer hours (trading
  omelette gold for makespan) and `favour_passive` drops it entirely (omelettes are
  in `spend_value`). `all` boosts oak+board+wood; `only_oak` boosts oak alone (board
  and wood are so much more plentiful per hour that oak is the only real value, and
  this guarantees an omelette is never spent to source them). `main.build_inputs`
  builds the offers once (static across the fixed point) onto `SolveInputs.boost_offers`,
  passed to `optimizer.all_offers`; the spend surfaces in the report's **Sawmill
  boost** section (`report._sawmill_boost_summary`) plus From-inventory/Buy, and the
  boost oak nets out of the passive section via `plan_balance`'s `elsewhere`. Web:
  the `hickory_omelette` select + `price_hickory_omelette` params
  (`webapi._CONFIG_PARAM_FLAGS` and the string-override special-case) map to the
  `sawmill.hickory_omelette` / `trading.hickory_omelette` overrides.
- **Held items seed obtainability**: `_obtainable` takes a `held` set —
  `_build_base` passes the held, non-demanded items any offer *consumes* (e.g. an
  Acorn Pie) so a channel grounded only in a held consumable is usable. Excludes
  demanded items, leaving their demand/inventory netting and the remainder set
  unchanged; the buyable/craftable inputs it also covers were already obtainable,
  so the only practical new entrant today is `acorn pie`.

## Dependency

**PuLP** is the modelling layer (declarative, reads like the codebase, and
flipping units to integer MILP is a one-keyword change); the backend that solves
each LP is pluggable (`optimizer.set_solver_backend`, `[solver] backend` /
`--solver`, any PuLP solver by name). The default is **HiGHS** via `highspy`
(binary wheels on 3.14; pulls in numpy): a run makes hundreds of sub-millisecond
LPs, and HiGHS solves in-process where the bundled **CBC** — the fallback when
highspy is missing, since PuLP ships its binary — pays ~8 ms of subprocess
startup per solve (once ~75% of the runtime). Alternate optima differ between
backends just as they do between CBC runs; the refinement tower in `minimize`
pins the reported metrics either way. NSGA-II/pymoo was rejected: the LP frontier
is piecewise-linear/convex, so scalarise-and-sweep recovers every vertex exactly.

**rich** renders `--format markdown`'s output to a terminal (`report.MarkdownReporter.finish`,
via `rich.markdown.Markdown`/`rich.console.Console`) -- only used when stdout is
a tty; redirected output prints the Markdown source directly, no dependency
needed to consume it.

## Web app (web/)

A Django server-side wrapper over the same solver (`webapi.py` bridges the
two -- see its module-map entry above), for a browser UI: no accounts, no
server DB, browser `localStorage` holds each user's pasted state and
parameter choices. `web/manage.py` inserts the repo root onto `sys.path` (the
solver package is not installed, just imported directly) before delegating
to Django; `web/farmweb/wsgi.py` does the same for a production WSGI server
that imports it without going through `manage.py`.

- **`web/farmweb/`** — the Django project: `settings.py` (deliberately no
  `django.contrib.auth`/`admin`/`sessions`; `DATABASES = {}`, since there is
  nothing server-side to persist; `DEBUG`/`SECRET_KEY`/`ALLOWED_HOSTS` are
  env-driven), `urls.py` (`/` → `index`, `/api/solve` → `api_solve`),
  `wsgi.py`.
- **`web/solverapp/`** — the one app: `apps.py` (`AppConfig.ready()` pins
  `optimizer.set_solver_backend("highs")` once at process startup -- never
  per-request, since `optimizer._backend` is a module global unsafe under
  concurrent solves in the same process -- and refuses to start without it),
  `views.py` (`index`; `api_solve`, a `csrf_exempt` JSON POST -- no
  session/cookie auth exists to forge), `cache.py` (`SessionCache`: an
  in-process LRU(64)/TTL(30 min) `OrderedDict` of `webapi.SolveSession` keyed
  by `webapi.fingerprint`; `SOLVE_SEMAPHORE`, one LP solve at a time per
  process; `key_lock`, a per-fingerprint lock guarding against a dogpile of
  identical concurrent requests all missing the cache at once), `tests.py`
  (Django `SimpleTestCase` -- no DB to wrap in a transaction), `templates/`,
  `static/`.
- **`api_solve`'s flow**: decode JSON → `webapi.params_to_args` (`ValueError`
  → 400) → `webapi.fingerprint` → `cache.key_lock` (serialises same-fingerprint
  requests) → cache lookup → `SOLVE_SEMAPHORE.acquire(timeout=20)` (else 503)
  → `webapi.solve` on a miss (`ValueError` from an unparseable paste → 422) →
  `webapi.render` → `cache.CACHE.put` → each `SolveResult.sections[...]`
  markdown rendered to HTML via `markdown.markdown(text,
  extensions=["tables"])` → JSON response (`cached`, `solved_ms`,
  `solution_html`, `notes`, `warnings`, `frontier.{html,plans}`,
  `plan.{index,html}`).
- **`static/solverapp/app.js`** (vanilla JS, no build step): state
  (`{mastery_text, inventory_text, params}`, mirroring `POST /api/solve`'s
  body) round-trips through `localStorage["farm-solver.v1"]`, restored and
  auto-solved on load. Any control/paste edit debounces 400ms then re-solves;
  a monotonic sequence number drops a response superseded by a newer
  request. The frontier table's rows are zipped with the response's
  `frontier.plans` JSON array by index for selected/cheapest styling and
  click-to-select (sets `plan_select` and re-POSTs immediately, no debounce
  -- a refilter, so it is cache-hit fast); any other change resets the
  selection first, so it can never point past the end of a differently
  filtered frontier.
- **Per-worker caches are a deliberate simplicity trade-off**: no state is
  shared across gunicorn workers/processes, so a request landing on a
  different worker (or after a restart/eviction) is just a cache miss --
  `webapi.solve`/`render` re-run in ~0.5-3s. No sticky sessions, no shared
  cache to keep consistent; see `cache.py`'s module docstring.
- Install with `uv sync --group web`; run with `uv run --group web python
  web/manage.py runserver`. Django's own tests run via `uv run --group web
  python web/manage.py test solverapp` -- deliberately outside `tests/` and
  not named `test_*.py`, so plain `uv run pytest -q` neither collects nor
  needs Django (`webapi.py` itself has no Django dependency and is covered by
  `tests/test_webapi.py` under the plain suite).

## Dev workflow

- Run: `uv run pytest -q` (tests in `tests/`). Lint: `uvx ruff check .`.
- Try it: `python main.py` (default), `-i Item:N`, `-m Item:N`, `--plan K`,
  `--plans N`, `--cookie-sets N`.
- Web app: `uv sync --group web`, then `uv run --group web python
  web/manage.py runserver`; its own tests via `uv run --group web python
  web/manage.py test solverapp` (see "Web app (web/)" above).
- Commit directly to `main`; end commit messages with the Co-Authored-By trailer.
