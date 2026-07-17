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
   `MarkdownReporter` buffers into a `MarkdownDoc`) and calls `solve_frontier`
   *before* `.solution` (not after, despite the Goal section printing first) so
   it can pass the frontier's `plans[0].remainder` in — identical across every
   plan in one run (demand vs. obtainability, not any plan's own choices), so
   any plan names the same goal items nothing can source. `.solution` flags
   each such row with `report.UNREACHABLE_GOAL_MARK` ("✗") and lists them in a
   "Materials with no known source" table right below the Goal table — moved
   there (from the foot of `.plan`) so it reads before, not after, what the
   frontier *did* work out; the web UI (`app.js`'s `markUnreachableGoalRows`)
   tints that row red client-side by detecting the same mark. Markdown itself
   (and `rich`'s parser of it) has no per-row colour, so `MarkdownReporter`'s
   terminal path gets the same red row a different way:
   `report._highlight_unreachable_rows` renders the document once through the
   normal `rich` Markdown path, then walks the resulting lines and overrides
   just the foreground of any line containing the mark — everything else
   (borders, headers, wrapping) stays as `rich` laid it out. Skipped (falls
   straight back to `RichMarkdown`) when the mark never appears, and moot for
   a non-tty redirect, which prints the raw Markdown source with no ANSI at
   all. Then `.frontier`
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
| `main.py` | CLI, orchestration (no printing except ascension mode's TEMPORARY report) | `run` (setup + file loading + the `--unobtainable` branch, then delegates to the three stage functions below, or to ascension mode), `build_inputs` → `SolveInputs` (now `_build_channel_setup` → `_ChannelSetup` + goal-solving layered on top), `solve_frontier` → `FrontierResult`, `derive_report_inputs` → `PlanContextData`, `build_parser`, `parse_target`, `resolve_files` (falls back to `_latest_dated_file` on `solver.data_directory`), `selected_plan`, `cheapest_plan`, `resolve_pumpkin_juice`, `resolve_fungus_processing`, `resolve_scythe_of_dewstar`, `resolve_dewstars_journal`, `resolve_monthly_grants`, `resolve_calendar_mode`/`strip_dated_sources` (the `solver.calendar` full/average/ignore enum), `apply_inventory_overrides`, `pumpkin_juice_gains`, `pumpkin_juice_schedule`, `load_config` (takes an optional path, from `--config`); ascension mode (milestone 4, see "Ascension orchestration" below): `solve_ascension_frontier` → `AscensionFrontierResult`, `_validate_ascend`/`resolve_ascend_params`, `_ascend_stage_cfgs`, `_run_ascend_pass`, `StagedPlan`/`StageResult`; milestone 6 (levels 101-200): `_select_megas` |
| `webapi.py` | framework-free web API layer (no Django import; drives `main`'s stage functions from pasted text instead of files) | `params_to_args` (JSON `params` → `argparse.Namespace`), `_CONFIG_PARAM_FLAGS` (Trading-values price params + `instant_grow_time` → `args.override` entries, so a price change re-solves via the `override` RESOLVE_FIELD), the generic `config` param → `_config_override_line` (the preference panels' `{section.flag: value}` object → typed `args.override` lines, same channel as the free-text overrides box), `load_web_config`/`WEB_CONFIG_PATH` (the web app's own defaults live in `web_default_config.toml`, separate from the CLI's `config.toml`; every web solve and `config_defaults` load it), `config_defaults` (that config minus `[solver]`, injected into the page for the panels' placeholders and bool checkbox states), `args_to_command` (reconstruct the equivalent `python main.py …` command line from a request's args -- the DEBUG-only "CLI command" popup; reuses `args.override` verbatim), `fingerprint` (session cache key), `RESOLVE_FIELDS`/`REFILTER_FIELDS`, `SolveSession`/`SolveResult`, `solve` (full re-solve), `render` (refilter against a cached `SolveSession`, with the one horizon-guard re-solve edge case) |
| `report.py` | **all printing** + critical-path naming | `Reporter`/`TextReporter`/`MarkdownReporter`/`SectionedReporter`/`build_reporter` (the `--format` facade), `print_frontier`/`print_plan`/`print_critical_path` (text) and their `markdown_*` counterparts, `MarkdownDoc`, `_build_plan_context`/`_PlanContext` (setup shared by both renderers), `build_display`, `_defining_passive_resources`, `_passive_wait` (quantized deposit waits); `--ascend` mode (milestone 5, see "Ascension report sections" below): `print_ascension_goal`/`print_ascension_frontier`/`print_ascension_plan` and their `markdown_ascension_*` counterparts (the `Reporter.ascension_goal`/`ascension_frontier`/`ascension_plan` facade methods), `_ascension_ledger`, `_ascension_timeline`, `_ascension_todo`, `_ascension_chore_wait` |
| `optimizer.py` | the LP and the frontier, LP backend choice | `SourcingPlan` (`passive_draw`, `track_times`, `cost`, `ascension`), `_Base`/`_build_base`/`_solve_configured` (`minimize`'s split, see below; `_build_base`'s optional `ascension` param and `_solve_configured`'s `_round_and_fix_ascension` are the ascension extension, see below), `minimize`, `frontier` (sweeps the continuous juice relaxation, exact-finishes only the plans it returns — see its cost bullet), `_sweep`/`_UnmeetableCap` (capped sweep solves; a cap the regime cannot reach is dropped, not an error), `fastest_endpoint`, `self_sourced_endpoint`, `plan_balance`, `passive_residuals`, `plan_gold`, `plan_gold_breakdown`, `plan_leftovers`, `leftover_inventory_share`, `accumulated_leftovers`, `surplus_sale_value`, `plan_surplus_gold`, `_quantize_plan_time`, `set_solver_backend` |
| `sources.py` | offer/draw model, per-channel offer builders | `Offer`, `Draw`, `OpenFlow`, `*_offers`, `sawmill_boost_offers`/`_omelette_bonus` (Hickory Omelette boost — 20% every 10 min = +120%/hr, per-deposit storehouse-capped; `SAWMILL_BOOST_TRACK`/`HICKORY_OMELETTE`), track constants |
| `farm_solver.py` | game state, goal→needs | `GameState`, `Inventory`, `Solution`, `solve_tower`/`solve_mastery`/`solve_acquire`/`solve_goals`/`solve_target`, `has_tower_level` |
| `ascension.py` | AK/chore/mega arithmetic, stage splitting, reward deltas -- pure domain, no solver imports (only `farm_solver` + stdlib) | `ak_cost`, `megas_required_by`, `chore_rate_per_day`/`chore_ak`, `candidates`/`StepCandidate`, `mega_count`, `advance`, `AscensionSpec`/`AscensionOutcome` (the milestone-3 optimiser handoff), `REWARD_LEVELS`, `stage_bounds`, `apply_rewards`; milestone 6 (levels 101-200): `mega_remaining`, `schedule_megas`, `mega_stage_floors` |
| `parsing.py` | parse pasted mastery/inventory text | `_split_blocks`, `_parse_mastery_block` |
| `crafting.py` | workshop recipes, demand expansion | `Workshop`, `recipe_flows`, `top_level_demand`, `IGNORED_MATERIALS` |
| `crops.py` | farming model (grow time, GJ, cookies, instant-grow, Dewstar, limited seeds) | `Crop` (`seed_item`), `Farm` (`dewstar_bonus`, `dewstar_cookie_bonus`), `COOKIE_*`, `INSTANT_GROW_MAX_BASE`, `PLOTS_PER_ROW`, `per_row_rate`, `SEED_ITEM_SUFFIX`, `DEWSTAR_EXCLUDED` |
| `passive.py` | passive producers, durations, deposit schedules | `PassiveProducer`, `PassiveProduction` (`producer_rates`, `rate_per_minute`, `per_cycle`, `quantized_wait`), `parse_duration`, `format_duration` |
| `pets.py` | pets as (seasonal) passive producers | `Pet`, `Pets`, `seasonal_fraction` |
| `config.py` | `config.toml` → producers/pets/storehouse/trading | `passive_production` (`monthly_grants` flag), `sawmill_outputs`/`hickory_omelette_mode` (the `[sawmill] hickory_omelette` = none/all/only_oak enum, boost built as offers not baked here), `PET_PRODUCER_PREFIX`, `MONTHLY_GRANTS`/`_monthly_grant_producers`, `is_smeared_producer`/`passive_quantizer` (deposit quantisation, see gotchas), `storehouse_size`, `trading_prices`, `current_tower_level`/`crafting_bonus_for`/`effective_grape_juices` (level-gated reward helpers, pure functions of `(cfg, level)` -- see "Tower-level gating" below) |
| `fishing.py`/`exploring.py` | LN/AP channels, modifiers | `Fishing`/`Exploring` (`acorn_flag`/`acorn_table`), `CiderModifiers`, `*_from_config`, `ACORN_PIE_FLAG`/`FUNGUS_ACORN_PIE_FLAG`/`ACORN_PIE_EXPLORES` |
| `locksmith.py` | chests/bags openables | `Openable`, `Locksmith`, `load_openables` |
| `farming.py` | which items are farmed | `FARMING_ITEMS`, `is_farmed` |
| `web/` | Django web app over `webapi.py` (see "Web app (web/)" below) | `solverapp.views.api_solve`, `solverapp.cache.{SessionCache,SOLVE_SEMAPHORE,key_lock}`, `solverapp.apps.SolverappConfig.ready` |

Data lives in `*.csv` (crops, fishing, exploring, pets, openables, tower,
seed_drops), `config.toml` and `mastery_items.txt` (the masterable-item
catalogue -- the ascension mode's candidate universe, generated by
`scripts/generate_items.py`, which writes the web Mastery picker's static
copy from the same set; regenerate with the script, never edit either by
hand); game state comes from pasted text files. The
web app has its own `web_default_config.toml` (a copy of `config.toml`, edited
independently) for a fresh session's defaults -- see `webapi.load_web_config`.

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
  (sale value of banked surplus, set by `frontier`), `remainder`, and `_origin`
  (`(base, kwargs)` stamped by `_solve_configured`, `repr=False`/`compare=False`, so
  `frontier` can re-solve a returned plan exact — see its cost bullet).
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
    `spend_value` (not makespan), then `total_value`, then activity — so it self-sources and
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
  - **Pumpkin Juice** (`pj` vars, gold): a bought mastery boost, and the model's **only
    integer variables** — everything else is a continuous flow. `pj_gain` (from
    `main.pumpkin_juice_gains` = `0.1*threshold/mastery_bonus`) is the demand units one
    juice covers for a goal item; one `pj` var per reachable goal, priced at `pj_price`
    gold each (from `trading.pumpkin_juice`, **nominal** — juice is never bought from a
    player, but it is still a cost plans are ranked on), capped by an optional `pj_limit`
    total. Its gold folds into `gold`; **off in `favour_passive`** (the buy-nothing
    regime). `plan_balance` nets `pumpkin_credit` out of `need` so the residual/passive
    accounting reflects the boost, not passive, covered that slice.
    - **Integrality**: `cat="Integer"`, `upBound = ceil(need/pj_gain)` — a juice is
      indivisible, and a continuous `pj` bought fractions of one (0.23 of a juice
      covering a nearly-done goal outright, which nobody can drink; the report then hid
      it as `whole < 1` and the item looked silently skipped, needing neither juice nor
      craft). The bound rounds **up**, so the last juice may overshoot the goal.
    - **`pj_credit`** carries what the juices earn (`≤ pj_gain*pj`, `≤ need`), and it is
      `pj_credit` — not `pj_gain*pj` — that the balance credits and the readout reports.
      This replaced a `pj*gain ≤ need` cap, which capped the *credit* and so banned the
      whole juice that earns it: a goal needing less than one juice's worth (Frost
      Shield, 2,112 units against a 9,091-unit juice) got 0.23 juices instead of one.
      Capping the count and clamping the credit separately keeps the last juice legal
      while still refusing to pay it for overshoot.
    - Gated on `pj_gain` alone, **never on `pj_price > 0`**: `pj_gain` is already empty
      when juice is off, so a present price of 0 means *free juice*, not *no juice* (the
      falsy-zero trap `[trading]` prices hit in d78542e). The report is gated on the
      juices, never on their gold, for the same reason.
    - **Cost & the continuous-sweep dodge**: an integer `pj` makes any solve a MIP,
      and a MIP is **~50x an LP per solve** (measured 10.5 ms vs 0.2 ms) — not from
      branch-and-bound (only ~6 integers bounded to ~3–5, a trivial tree; `mip_rel_gap`
      and heuristics-off change nothing) but from the fixed MIP machinery HiGHS applies
      to the *whole* thousands-of-vars model the moment one variable is integer. So
      `frontier` sweeps its ~hundreds of interior points on the **continuous relaxation**
      (`pj_integer=False`, fractional juice, all LP) and re-solves only the handful of
      plans it *returns* exact (`pj_integer=True`) — the rows of the summary table and
      the expanded plan, the only juice counts a user reads. Cut ~273 MIP solves → ~21;
      `--use-pumpkin-juice` wall-clock 3.15s → 0.76s (juice off: no `pj` vars, no MIP,
      unchanged). Interior *ranking* then uses relaxed costs (off by ≤ one juice's
      gold/time — already-thinned, harmless); every returned plan is a real whole-juice
      plan. The finish reuses each plan's `_origin` (the `(base, kwargs)`
      `_solve_configured` stamps on it) to reproduce its exact solve. Within one solve,
      stage 3 additionally freezes the counts stage 2 settled on (`pj_fix_*`, integer
      solves only) so presolve drops the integer columns and that stage runs as pure LP.
      The readout **rounds** `pj.value()`, so a relaxed sweep plan's reported count is
      integer even mid-sweep (its *gold*, priced off the fractional var, is not — but
      those interior plans are always replaced by the exact finish before returning).
  - `inventory` credits every item's balance **except directly-demanded ones**
    (their need is already net of stock; stamina is seeded by `--current-stamina`).
  - **Refinement** (determinism): pin makespan, minimise `total_value`, then minimise
    activity (crafts + gathering + buys) — pins `(time, value)` then sheds slack.
    Stage 2 minimises **`total_value`** (`spend_value` + juice gold: the LP form of
    `plan_gold` — everything spent at market price, bought or drawn from stock), not
    `gold`. `gold` charges only what a plan *buys*, and juice — the one consumable with
    no self-sourced form — is always bought, so least-gold read as *fewest juices* and
    would burn tens of thousands of held AP/LN/cider to dodge a few juices. It was also
    numerically unresolvable: a `pj` column carries a ~1e5 `pj_gain` against a
    `pj_price` cost, so at a low price the least-gold optimum sat under HiGHS's
    tolerance and the juice count fell from 13 to 5 when the price rose 0.0005→0.001.
    The Pareto axis stays **bought gold**: stage 2 pins `gold` too (`stage2_gold_pin`),
    in epsilon mode *before* it runs, since that is what stage 1 minimised.
    Each pin locks the last stage's optimum with **relative** slack (`_PIN_REL`, token
    `_PIN_ABS` floor) sized for the solver's own feasibility tolerance: with integer
    `pj` this is a MIP, and a pin tighter than the tolerance cuts off the optimum the
    previous stage just found, returning Infeasible with *nothing* loaded. A flat 1e-6
    floor is the opposite failure — where the pinned quantity is itself ~1e-6 (the
    fastest plan's gold) the pin stops binding and the LP parks inside the slack.
    - **Recovery**: if the tower still ends non-Optimal, the pins are dropped and the
      *primary* objective re-solved (it solved once already, so it is feasible). The
      plan loses tie-break determinism, not correctness. Uncapped, a still-failing
      re-solve is a real unsatisfiable demand (`_assert_optimal`); capped, it is a
      `_UnmeetableCap` and `_sweep` drops the sample — the sweep's caps come from the
      *active* endpoints, and `favour_passive` turns off the very juice that made
      `t_fast` instant, so such a cap can be milliseconds long and simply unreachable.
      Before integrality these solves came back with the previous solution still loaded
      and read out as empty, cost-`(0, 0)` plans that sorted to the head of the frontier
      as a bogus 0-minute, 0-gold "fastest".
  - **`_build_base`/`_solve_configured` split**: `minimize` itself is `_build_base`
    (once) + one `_solve_configured` call. `_build_base` builds everything a sweep
    holds fixed call to call — variables, the balance/track constraints,
    `gold`/`spend_value`/`total_value` — and returns a `_Base`. `_solve_configured` adds the
    per-call bit (the objective, an optional `makespan ≤ time_cap`, `favour_passive`'s
    `pj ≤ 0` neutralisation, and `pj_integer` — toggles the juice vars' `.cat` between
    `Integer` and `Continuous` for this solve, see the Pumpkin Juice cost bullet) then
    runs the refinement above and reads out the `SourcingPlan`, stamping it with
    `_origin = (base, kwargs)` so `frontier` can re-solve it exact. `frontier` (below)
    reuses one `_Base` across its ~80-call sweep instead of rebuilding the LP from
    scratch per call (`plans/MODEL_REUSE_PLAN.md`).
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
  market value but never bought). Absent whole section → `None`, nothing is buyable
  and every plan is fully self-sourced. **Buyability is by key *presence*, not
  value** (`item in prices`, both the `_build_base` obtainability seed and the `buy`
  var gate): a buyable's key present in `[trading]` makes it buyable, **including an
  explicit `0` = free to buy**; an *absent* key means "not traded / self-source
  only". So zeroing a price makes an item free, not unbuyable. (Grape Juice is the
  lone always-present key — never in `BUYABLE`, so never gets a buy var regardless.)
  `pumpkin_juice` sits here too (gold each, like `cookie_set`) but is read by
  `main.resolve_pumpkin_juice`, not `trading_prices` — it is **nominal**, since juice
  cannot be bought from a player, and it is the one key where presence does *not*
  decide availability (`use_pumpkin_juice` does). A present `0` still means free. It
  moved here from `mastery.pumpkin_juice_price`, which is now **rejected with an
  error** rather than ignored — silently ignoring it would reprice every plan.
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
  `_non_dominated` then `_thin` to `count`, then **exact-finishes** the survivors
  when there are juice vars: the whole sweep runs `pj_integer=False` (the cheap
  continuous relaxation, since a MIP is ~50x an LP per solve — see the Pumpkin Juice
  cost bullet), and only the returned plans are re-solved `pj_integer=True` from their
  `_origin`, re-quantized/re-surplus'd, and deduped by `(time, net gold)`. It builds
  two `_build_base` bases up front — `base_active` (`favour_passive=False`) shared by
  the fast endpoint, cheap endpoint and every epsilon-cap solve; `base_passive`
  (`favour_passive=True`) shared by the probe and every extension-cap solve — and
  drives every solve through `_solve_configured` against one of them, instead of a
  fresh `minimize` (full LP rebuild) per call:
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
    offloading demand onto a pet trickle. **The `t_bound > t_cheap` gate uses the
    cheap endpoint's *continuous* makespan (`t_cheap_raw`), not its quantized time**
    — a self-sourced cheap plan can lean on a *daily* deposit (orchard fruit for
    cider stamina) and quantize up past `t_bound` (a goal item's own *hourly*
    passive completion) even though its real sourcing finishes well before it.
    Comparing the quantized times then wrongly skips the whole slow regime, so a
    fully-passive goal (e.g. Board straight from the sawmill) never gets its
    zero-tool `maxpassive` plan and a `--max-ac 0` / `--max-ln 0` run wrongly
    reports the goal impossible. The fast-regime epsilon sweep does not rescue it:
    it is geometric and sparse near `t_cheap`, and the cheap plan's *partial*
    passive residuals inject no full-completion breakpoint there.
- `_dominates`/`_non_dominated` prune by the `cost` tuple `(time, net gold)`; dedup key
  rounds both.

### Ascension extension (`_build_base`/`_solve_configured`, milestone 3)

An optional `ascension: ascension.AscensionSpec | None` param on `_build_base`
(and pass-through on `minimize`; **not** on `frontier`, which milestone 4's staged
loop drives directly) turns on a second thing the LP chooses, alongside how to
source `demand`: which candidate items to master/grandmaster/mega-master to clear
the stage's AK requirement. `None` (the default everywhere) leaves every existing
code path byte-identical — everything below is gated on the spec.

- **Reachability**: each `spec.candidates` item (`ascension.StepCandidate`) that is
  `_obtainable` joins the `_reachable` seed set alongside wanted demand keys, so
  its channels/crafts/opens enter the model like any other item; one that isn't
  obtainable is dropped and recorded on `_Base.unmasterable` (never selectable).
  This is also why the old "no items → trivial `remainder_only` plan" early return
  is skipped whenever a spec is given: a stage with no direct demand at all (an
  ascension-only call) still needs the makespan variable and the AK constraint
  below built, even with zero surviving candidates (the chore-only case).
- **Step variables** (`_Base.ascend_y`, item → step name → `LpVariable` in
  `[0, 1]`): one continuous `y` per remaining step (`"master"`/`"grandmaster"`/
  `"mega"`, whichever of `delta_master`/`delta_grandmaster`/`delta_mega` is not
  `None`) a surviving candidate has, in sorted-item order for determinism.
  Ordering links a step to the one below it among whichever exist for that item:
  `y_grandmaster <= y_master`, `y_mega <= y_grandmaster` (an item already past a
  lower step has no variable for it, so the link is simply skipped, not asserted
  — `ascension.candidates()`'s deltas are contiguous, so a step is never present
  without every step below it that isn't already done).
- **Gross mastery supply** (`_Base.ascend_S`, item → `LpAffineExpression`): built
  from the *same* indexed terms the balance loop already computes for that item —
  `gathered` (offer yields), `made` (`crafting_bonus * craft[item]`, when
  craftable) and `unpacked` (open-flow yields) — **not** the net balance, since
  mastery must count gross production even when this plan goes on to consume all
  of it as a craft material (mastery-chaining is deliberate, see the module
  docstring). `S_i = spec.mastery_bonus * (gathered + made + unpacked)`, excluding
  `held`/`bought`/`boosted` so buying or holding an item never earns its mastery.
  Constraints: `S_i >= Σ(delta_step * y_step)` over whichever steps exist,
  plus — when a milestone-6 mega supply floor is set — a **separate**
  `S_i >= spec.mega_supply_floors[item]`. The pair expresses
  `S_i >= max(step terms, floor)`, NEVER their sum: the same produced supply
  counts toward the floor and the step thresholds at once, so at an item's
  due stage (floor == its own `delta_mega`, both from the same "remaining to
  mega" reading) selecting `y_mega` rides the floor's production for free
  rather than doubling it, and harvesting the +10/+100 AK steps on a floored
  item is likewise free once the floor covers their deltas. (An additive
  `S >= steps + floor` was the original milestone-6 formulation — it
  double-charged a mega's due stage its whole remaining supply.) The floor
  guarantees the item's real in-game mastery crosses the threshold regardless
  of `y_mega`, but the LP's own mega-COUNT tally (below) only credits a
  selected `y_mega`, so pinning it down at the due stage would leave
  `megas_required` unmet with no other item to cover it (infeasible, or a
  worse-scoring fallback) — see the round-and-fix note below for how
  two-sided rounding resolves this in the item's favour.
- **AK constraint**: step AK (`AK_PER_MASTER * y_master + AK_PER_GRANDMASTER *
  y_grandmaster`, summed over surviving candidates) plus smeared chore AK
  (`spec.chore_rate_per_day * (makespan / 1440)`, the makespan variable itself, in
  days) must be at least `spec.ak_required` — so a stage whose candidates are all
  too dear can simply run longer instead; the chore term is skipped when
  `chore_rate_per_day == 0`. **Mega constraint** (only when `megas_required > 0`):
  the selected mega count (`Σ y_mega`) must be at least `spec.megas_required` — a
  requirement with no mega steps in the model at all makes this infeasible,
  surfaced by `_assert_optimal` like any other over-demanded LP (a clearer
  pre-check is milestone 4's job).
- **Round-and-fix** (`_round_and_fix_ascension`, called from `_solve_configured`
  right after the primary solve, before the refinement tower): the relaxed LP can
  split a step fractionally across several items (a valid relaxation optimum, not
  a real plan — you cannot half-master an item). It drives every `y` to exactly
  0 or 1 by **two-sided iterative rounding**: each round takes the largest
  fractional `y` (band `0.001 < y < 0.999`; below it a step is solver *dust* —
  micro-slivers shaved to save fractions of a minute — never force-committed;
  ties prefer the lower step then the item name) and tries it **both ways** —
  re-solving under "pin up to 1, with prerequisites" (mega needs grandmaster
  needs master, per the ordering constraints) and under "pin down to 0" — and
  keeps whichever solves to the better objective, repeating until nothing is
  fractional, then pinning the (now integral) remainder in place. The AK/mega
  constraints stay in the model throughout, so coverage is the LP's own job:
  a pin-down that breaks the requirement just solves worse (or not at all,
  with a zero chore rate) and loses to the pin-up. **Every cheaper scheme got
  a real case badly wrong** — this history is load-bearing, don't regress it:
  descending-`y` one-shot rounding turned a 10-AK gap into a whole grandmaster
  (the relaxation loves fractional grandmaster slices — 100 AK per 90k of
  delta beats a master's 10 per 10k; the corn regression test); a
  supply-per-needed-AK cover fixed that but pinned items the relaxation had
  merely *trickle-fed*, turning a 4-day fast plan into 43 days and the
  buy-nothing endpoint into 7 *years* (a passive source sipped at 10% costs
  10x the makespan completed); up-only iterative rounding had no way back
  from one such commitment (a 3-year multi-stage climb locked into 7+ years);
  batch-committing every `y >= 0.5` over-selects the other way (4-day fast
  endpoint → 41 days). Only a re-solve can price completing a step against
  more chore-waiting or switching items; two-sided trials cost one extra
  solve per fractional step and settle all of it. **Trial mechanics**: a
  trial pin is a *bound flip* (`y` fixed to 0 or 1), never a structural edit,
  so trials skip pulp's full model rebuild (several times the cost of the
  actual solve on real candidate sets) — on HiGHS they flip column bounds on
  the still-loaded in-process `problem.solverModel` (`var.index` from pulp's
  build) and re-solve warm-started; on any other backend (CBC fallback or a
  mid-loop rescue) they flip the pulp variables' own bounds and re-solve
  through pulp. Each round keeps the winning trial's `y` snapshot, so there
  is no third solve to restore the winner. Only at the end are the decisions
  materialised as ephemeral pin constraints (`ascend_pin_<item>_<step>`, read
  back off the constraint per the pj_off name-sanitisation gotcha, `y` bounds
  restored to `[0, 1]`), popped at the next call's top; the problem is
  left solved against the full integral pin set before the refinement tower
  runs on top, so the tower's stage-1 pin (makespan or `spend_value`)
  captures the *integral* plan's real cost, not the relaxation's. This
  fast path plus the cross-pass base cache (see "Ascension orchestration")
  took `--ascend 60 --tower-level 40` from ~8s to ~1.4s with byte-identical
  output. One provable trial shortcut on top: the objective of the solve
  that produced the current fractional values is the relaxation optimum of
  the remaining pinned problem, and any pin-down solution is feasible in
  that relaxation — so when pin-up already achieves that bound the down
  trial is skipped (ties already prefer up). It fires rarely (a step is
  fractional precisely when completing it costs something at the margin),
  but it is free.
- **`plan.time`**: when the base carries a spec, it is the max of the draws'
  aggregate time and `timedelta(minutes=base.makespan.value())`, instead of just
  the draws' time — the chore term can legitimately make the optimal makespan
  exceed every track's own time (the plan idles, waiting on daily chores, to
  close the AK gap), which the draws alone would under-report. Non-ascension
  plans are unaffected -- this path is gated on `base.ascension is not None`.
- **Outcome readout** (`_ascension_outcome`, attached as `SourcingPlan.ascension`):
  reads the solved, round-and-fixed base into an `ascension.AscensionOutcome` --
  `masters`/`grandmasters` (sorted item lists, `y > 0.5` past rounding — the
  *selected* steps, which is what the AK ledger charges, and selection tracks
  reality for those two because a threshold the supply crosses anyway is
  always worth selecting: its AK is free), `megas` (**threshold crossings**,
  not selections: every candidate whose gross supply covers its full
  remaining distance to 1M — the sum of its remaining step deltas, `round()`ed
  to mirror `ascension.advance` — because the mega constraint/floors stop
  pulling `y_mega` the moment enough megas exist, so on a fresh account the
  many items that cross 1M purely as a side effect of mass production would
  otherwise vanish from the report while `mega_count` still counts them;
  a pinned `y_mega` always implies the crossing), `ak_from_steps`
  (10/master + 100/grandmaster, selected only), `ak_from_chores`
  (`spec.chore_rate_per_day * final_makespan_minutes / 1440`, may overshoot the
  requirement), `mastery_supply` (`{item: S_i.value()}` for every surviving
  candidate with positive value — incidental production of a candidate item
  counts as real mastery progress even when nothing pinned it, deliberate).
- **Round-and-fix's own re-solve is rescued too** (`_round_and_fix_ascension`
  returns the solver `_solve_configured` should keep using): its final
  `problem.solve(solver)` — after every step var is pinned to a hard 0/1 — goes
  through `_assert_optimal` like every other uncapped first solve in this file.
  On real, dense candidate sets (hundreds of items, only reachable on live
  player data, past this module's tiny unit tests) that pinned re-solve can
  land HiGHS on the same falsely-"Unbounded" degenerate vertex documented under
  "Infeasible ≠ unobtainable" below — but unlike a plain non-optimal status,
  it leaves every variable's `.value()` reading back nonsense (a negative
  makespan, every step un-selected) rather than simply stale, so an unchecked
  call here silently produces a plan that violates its own AK requirement.
  Found via milestone 4's real-data acceptance run, fixed in the same commit.

### Ascension orchestration (`main.py`, milestone 4)

`main.solve_ascension_frontier(args, cfg, state) -> AscensionFrontierResult`
is `--ascend N`'s entry point: climb from a current level to `N`, producing a
Pareto frontier of `StagedPlan`s (`stages`, `time`, `gold`, `surplus_gold`,
`carry_out` — the AK left over after the final stage, the balancing term in the
report's AK ledger, see "Ascension report sections" below — `.cost` mirroring
`SourcingPlan.cost`) instead of a single plan, since a climb is many stages back
to back. `main.run` dispatches to it before any of the goal-solving setup
(`resolve_crafting_bonus`, `build_inputs`, ...) runs — `_validate_ascend(args,
cfg, state)` checks the mode conflicts (`--target`/`-m`/`-i`/
`--use-pumpkin-juice`) and the level range (`current < target <=
ascension.MAX_ASCEND_LEVEL`; levels 101-200, milestone 6, need `state` too —
see "Global mega selection" below for why it takes a `state` param, unlike
every other mode's validation, and `main.run`'s ascend branch loads state
*before* calling it for exactly that reason) and resolves `(current, target,
starting_ak)` via `resolve_ascend_params` (`--tower-level`/`--starting-ak`,
else config, else — **ascension-only** — `current` defaults to 0 rather than
`config.current_tower_level`'s usual "assume every reward owned"). The report
(milestone 5) drives `report.Reporter.ascension_goal`/`ascension_frontier`/
`ascension_plan` the same way `run`'s normal-mode branch drives
`solution`/`frontier`/`plan` — see "Ascension report sections" below;
`_print_ascension_report`, milestone 4's TEMPORARY plain-`print` dump, is gone.
`StageResult` (`from_level`, `to_level`, `plan`) also carries a per-stage
snapshot of everything the report needs to render `plan`'s full per-channel
breakdown: `farm`, `workshop`, `crafting_bonus`, `production`, `open_flows`,
`openable_names`, `inventory` (this stage's **starting** inventory, before its
own solve — what the base LP was built against), `prices`. One snapshot per
stage, since each stage's config/production/inventory differs.

- **Stage configs** (`_ascend_stage_cfgs`): `ascension.stage_bounds(current,
  target)` splits the climb at every reward threshold crossed. Stage 0 is a
  deep copy of `cfg` with `[tower] level` set to `current` directly (so a
  stale/absent `[tower] level` in the base config can't leak in — the
  CLI-resolved `current` always wins). Stage `k >= 1` applies
  `ascension.apply_rewards(stage_cfgs[k-1], bounds[k-1][0], bounds[k][0])` —
  the reward completing the *previous* stage earns.
- **`_build_channel_setup`** (`_ChannelSetup`: requirements/farm/explore/fish/
  workshop/monthly_grants/recipes/smith/open_flows/inventory/cookie_sets/
  prices/boost_offers/model_calendar) is `build_inputs` split in two: this is
  everything goal-independent (channels, prices, recipes, openables), taking
  an explicit `crafting_bonus` float rather than reading `args.crafting_bonus`
  so a caller solving several configs at once — ascension's per-stage cfgs,
  whose bonus changes once level 90 is crossed — resolves each stage's own
  value (`_crafting_bonus_for_cfg`, the pure half of `resolve_crafting_bonus`)
  without mutating `args`. `build_inputs` itself now just layers goal-solving
  (`solve_target`/`solve_goals`, `demand`, `pj_gain`) on top of one call to
  this — byte-identical behaviour to before the split. Built **once per
  stage**, shared across the whole gold-weight sweep below (it depends only
  on that stage's cfg, never on how a sweep pass's earlier stages evolved).
- **Global mega selection** (milestone 6, levels 101-200: `main._select_megas`,
  `ascension.schedule_megas`/`mega_stage_floors`): a megamastery is ~900k+ of
  mastery production — realistically spanning many stages — so it is decided
  in TWO phases, both computed ONCE in `solve_ascension_frontier`, before the
  gold-weight sweep, not per pass:
  1. **Which items** (`_select_megas`): one aggregate `AscensionSpec` over the
     *whole remaining climb* — full remaining AK (`ascension.ak_cost(current,
     target) - starting_ak`, floored at 0), full net mega requirement
     (`ascension.megas_required_by(target) - ascension.mega_count(state)`),
     candidates from the **pre-climb** `state` — solved once via
     `optimizer._build_base`/`_solve_configured` against the **last stage's**
     channel setup/offers (a representative stand-in: by the time most of a
     mega's production happens, most rewards are already owned) at the fast
     endpoint (`optimizer._FAST_PRICE`, buy freely — a supply decision this
     large is dominated by production cost, not the gold budget, so it barely
     moves across the sweep; one solve keeps the chosen set identical at every
     frontier point instead of a different climb per point). `chosen =
     sorted(plan.ascension.megas)`, trimmed to exactly the net requirement if
     the LP's `>=` mega-count constraint over-selects (a free byproduct);
     an infeasible selection solve is caught and re-raised as "not enough
     megamasterable items: need N more".
  2. **When** (`ascension.schedule_megas(chosen, bounds, already, per_item_remaining)`,
     pure): assigns each chosen item a DUE STAGE index — the first stage whose
     end level's `megas_required_by(b) - already` reaches that item's rank —
     cheapest-remaining-supply first, so the cheapest megas land on the
     earliest deadlines. Raises if `chosen`'s count doesn't exactly match the
     requirement (shouldn't happen; the selection solve's mega-count
     constraint saw to that).
  The schedule feeds every pass's `_run_ascend_pass` call unchanged (below).
- **The candidate universe is the masterable catalogue**, not the paste:
  `main.load_mastery_catalogue` reads the root `mastery_items.txt` (`{lower
  key: display name}`; a missing file → `{}` = "no catalogue", paste-only
  candidates) and `solve_ascension_frontier` hands its names to every
  `ascension.candidates(state, catalogue)` call (`_run_ascend_pass` per
  stage, `_select_megas`, `_validate_ascend`'s mega bound). The paste only
  lists items the player has *progress* on, so without this an
  `--empty-mastery` run had zero candidates (its only "plan" was a pure
  chore wait) and a one-item paste could only ever grind that one item. A
  catalogue item absent from the paste is a fresh candidate at progress 0;
  paste items the catalogue doesn't know come back on
  `AscensionFrontierResult.unexpected_masteries` and render as a stale-
  catalogue warning in the Goal header (`report._ascension_unexpected_line`).
- **The staged pass** (`_run_ascend_pass`, one call per gold weight): solves
  every stage in order, threading four things forward: the **AK carry**
  (`max(0, carry + ak_from_steps + ak_from_chores - level_ak)`, so a stage's
  own `ak_required` is only what's left after whatever surplus the previous
  stage earned), the **masteries** (`ascension.advance`, so an item mastered
  in stage `k` is a different — or absent — `StepCandidate` in stage `k+1`),
  the **inventory**, and (milestone 6) each chosen mega item's **per-stage
  floor**: `ascension.mega_stage_floors(masteries, mega_schedule, k)` —
  recomputed every stage from *this pass's own* evolved `masteries`, so an
  earlier stage's overshoot shrinks a later stage's floor — split evenly
  across the stages from `k` to the item's due stage inclusive (the due
  stage's own span is 1, so its floor is exactly the full remaining amount:
  the mega completes there). Goes into `AscensionSpec.mega_supply_floors`
  (mastery points — the LP holds `S_i >= max(step terms, floor)` via two
  separate constraints, see "Ascension extension" below for the y_mega/floor
  interplay at the due stage). `AscensionSpec.megas_required` is
  set per stage too (`max(0, ascension.megas_required_by(b) -
  ascension.mega_count(gs))` off the pass's current masteries, not threaded
  like the AK carry — `mega_count` already reflects every item that crossed
  the threshold so far this pass, scheduled or incidental) as a belt-and-braces
  safety net; the floors guarantee coverage by construction, so in practice
  this constraint is slack. Stage LP bases are cached across passes
  (`base_cache` in `solve_ascension_frontier`, threaded into
  `_run_ascend_pass`): the key is every `_build_base` input that can vary
  between passes — stage index (standing in for the pass-invariant
  offers/recipes/prices), the `AscensionSpec` contents, the starting inventory,
  the cookie sets — so a hit is *exact* reuse via `_solve_configured`'s
  ephemeral mechanism, never an approximation. Stage 0 hits on every pass
  (nothing has evolved yet); later stages hit whenever adjacent gold weights
  chose the same plan prefix, which is most passes (a 16-point sweep yields a
  handful of distinct plans — observed 65 builds → 24 on a 4-stage climb).
  Benchmarks on real data after the base cache and the round-and-fix trial
  fast path (see "Ascension extension"): `--ascend 60 --tower-level 40` ~1.4s
  (was ~8s), 4-stage `--ascend 100 --tower-level 40` ~4s (was ~21s),
  `--ascend 200 --tower-level 100` ~4.7s, byte-identical plans throughout.
  Remaining cost splits between the rounding trial re-solves and the
  primary/refinement-tower solves, which still rebuild through pulp because
  they change objectives and constraints, not just bounds.
  - **Inventory evolution**: `optimizer.plan_leftovers(plan, {}, ...)` with an
    *empty* demand — with nothing directly demanded, every held item lands in
    `plan_balance`'s `elsewhere` (its "net of demand" exception never applies),
    so `plan_leftovers`'s "supply − used" is exactly each item's ending stock
    (held + produced − consumed by crafts/opens/offer inputs): a genuine
    per-item balance, not an approximation, and it becomes the next stage's
    starting inventory directly.
  - `cookie_sets` is recomputed per stage per pass from the (evolving)
    inventory (`cookie_sets_available`), not taken from the pass-invariant
    `_ChannelSetup` — the one piece of "static setup" that still has to track
    evolution, since held cookies change pass to pass like everything else in
    inventory.
  - **AK is checked at stage ends only**: the LP's AK constraint
    (`step AK + chore_rate * makespan/1440 >= ak_required`) is per-stage, so a
    stage can only ever clear *that stage's own* requirement — there is no
    mid-stage AK check, matching how `stage_bounds` splits the climb (a reward
    is available for every stage after it, never mid-stage).
- **The sweep** (`_ascend_gold_weights`): the two frontier endpoints
  (`optimizer._FAST_PRICE`/`_FREE_PRICE`) plus 14 log-spaced weights between
  (mirrors `optimizer._time_caps`' geometric spacing, over `gold_weight`
  rather than a makespan cap). `--sweep N` solves N weights instead (min 2 =
  endpoints only) — every pass is a full staged solve, so the sweep count is
  the wall-time knob for the heavy low-mastery climbs (a fresh-account
  `--tower-level 0 --ascend 200 --empty-mastery`: ~92s at the default 16,
  ~9s at `--sweep 2`, which still produces the exact fastest and cheapest
  plans; interior weights only add frontier density between them, and
  occasionally a cheaper alternate rounding of an endpoint). That fresh-account
  case is trial-solve bound: ~10k warm HiGHS re-solves at ~2.4ms of genuine
  simplex each (measured; per-trial cost is NOT rebuild overhead there —
  presolve-off and warm-basis tweaks change nothing). Evaluated and rejected
  as further levers: solving a stage as a true HiGHS MIP (times out at 30s on
  the hard stages and its gap tolerances swamp the tiny fast-endpoint
  objectives, hitting the same false-"Unbounded" numerics documented under
  "Infeasible ≠ unobtainable"), and pre-pruning "too expensive" candidates
  (the relaxation already prices bad candidates out for free, and the fresh
  fastest plan *selects* ~360 of ~430 candidates — there is nothing to
  prune in exactly the case that is slow; expensiveness is relative to
  progress and channels, which only the LP evaluates — the real save's
  ascend-100 plan grandmasters Grand Piano precisely because its remaining
  delta is cheap there). Two further variants of the MIP idea were also
  built and rejected on the same data — the disease each time is that the
  **LP relaxation bound is far below the integral optimum in the gapped
  regimes** (fractional ys free-ride shared supply, so e.g. a buy-nothing
  endpoint's bound misses the real cost several-fold), leaving nothing
  sound to gate against: (1) a budget-gated MIP finisher with a
  bound-anchored `objective_target` and wall-cap — with any absolute gap
  loose enough to skip proof it returns junk selections (1.0 objective
  unit of slack = a million gold at `gold_weight` 1e-6; observed 41k-gold
  plans where 7.7k is optimal), and with honest relative-only gates it
  spends 10s+ per gapped stage proving, landing *slower* than the loop
  (fresh 0→200: 165s vs 86s); (2) caching a flood-priced call's settled
  selection on the base and re-trying it at later sweep weights behind a
  bound-relative adoption gate — the gate almost never validates (the
  correct selection still prices far above the weak bound), measured a
  ~0–6% no-op. `--sweep` remains the honest *algorithmic* lever; the passes
  are also fanned across processes (see **Parallel sweep** below), which is
  where the real wall-time wins now come from. No `favour_passive`
  slow-regime extension in
  v1: the chore-AK tradeoff already stretches the slow end on its own (a
  pricier gold weight masters fewer candidates and waits longer on chores
  instead of buying), so a separate buy-nothing-and-bank-surplus regime would
  mostly duplicate that shape. Before pruning, the slow end is bounded at the
  **chore-only time** (`ak_cost / chore_rate_per_day`, ×1.05 + 1d margin so
  quantisation can't evict the chore-only endpoint itself): no plan needs to
  run longer than earning every AK from chores alone, and without the cut the
  slowest passes survive on their banked-surplus credit alone (net gold falls
  without bound as the span grows — passive accumulates forever; 52-year
  "plans" were observed). It is the ascension analogue of the normal
  frontier's `t_bound` pet-tail cut. `optimizer._non_dominated`/`_thin` —
  which work purely off `.time`/`.gold`/`.surplus_gold`/`.cost` — then
  prune/thin the resulting `StagedPlan`s exactly as they do `SourcingPlan`s,
  reused as-is (duck-typed) rather than a parallel copy of the pruning logic.
- **Parallel sweep** (`_ascend_worker_init`/`_ascend_worker_run`/
  `_ascend_worker_count`, `--jobs`): the gold-weight passes are independent
  (each `_run_ascend_pass` is one weight in → one `StagedPlan` out), so they
  fan out across a `ProcessPoolExecutor` — **processes, not threads**, since
  HiGHS/PuLP hold non-thread-safe solver state (the same reason the web app
  serialises on a single-solve semaphore). The pass-invariant per-stage setup
  (offers/setups/production/`mega_schedule`/…, all built once in the parent) is
  shipped to each worker **once** via the pool `initializer` and stashed in a
  module global; each task ships only its `gold_weight`. `pool.map` preserves
  input order so `staged_plans` stays gold-weight-ordered exactly as the
  sequential path builds it. **Correctness rests on base_cache-independence:** a
  pass's result is identical whether its `_build_base` was freshly built, reused
  within a worker's own `base_cache`, or reused across the in-process sweep
  (ephemeral reuse is exact, never an approximation), so the parallel and
  `--jobs 1` frontiers are byte-for-byte identical — asserted by
  `test_parallel_and_sequential_sweeps_produce_the_same_frontier`. One wrinkle:
  `_solve_configured` stamps every plan with `_origin = (base, kwargs)` for
  `optimizer.frontier`'s exact-finish, and that `_Base` holds a live HiGHS model
  that **cannot pickle** (`cannot pickle 'Highs' object`); ascension never uses
  `_origin` (no juice → no integer finish), so `_strip_ascend_origins` clears it
  in the worker before the `StagedPlan` crosses back (`_origin` is
  `compare=False, repr=False`, invisible to the report and pruning). Worker
  count: `--jobs N` (`--jobs 1` forces the in-process path), else the
  `FARM_SOLVER_ASCEND_JOBS` env var (the web app sets this so its own worker
  pool controls nesting), else one per CPU, capped at the pass count.
  Measured on a fresh-account climb: `--tower-level 0 --ascend 200
  --empty-mastery` ~72s → ~15s, `--tower-level 40 --ascend 60 --empty-mastery`
  ~69s → ~8s, byte-identical output. The speedup is bounded by the single
  heaviest pass (the buy-freely fast endpoint selects the most candidates and
  its stages are inherently sequential — each depends on the previous stage's
  evolved inventory/masteries/AK carry), so it is sub-linear at low sweep
  counts but the whole 0→200 case now clears in seconds.
- **v1 approximations** (all called out again at their own site in the
  docstrings): **steady-state calendar** — every stage's passive production
  (seasonal pets, the Mechanical Heart's grants) is the yearly/monthly
  average, never calendar-exact; a stage's real span is only known after
  solving it, and nesting a per-stage fixed point inside the gold-weight sweep
  is a later milestone's job if it matters in practice. **Perk auto-detection
  uses the pre-climb state**: Scythe of Dewstar/Dewstar's Journal/Fungus
  Processing/the Mechanical Heart are detected once per stage from the
  state passed into `solve_ascension_frontier`, not re-evaluated against each
  pass's evolved masteries — they gate on tower levels (250/300) far past the
  101-200 range this milestone even allows. **Global mega selection against
  the last stage only, at one gold weight** (milestone 6) — see the "Global
  mega selection" bullet above for the full reasoning; not revisited per stage
  or per pass.

### Ascension report sections (`report.py`, milestone 5)

`--ascend` output is real `Reporter` output, not a bypass: `main.run`'s ascend
branch calls `reporter.ascension_goal`/`ascension_frontier`/`ascension_plan` —
paired `print_ascension_*`/`markdown_ascension_*` functions in `report.py`,
following the same convention as the normal-mode sections (see "Two renderers,
one set of decisions" above); `TextReporter`/`MarkdownReporter` dispatch to
them, `SectionedReporter` routes them into its `"solution"`/`"frontier"`/`"plan"`
docs alongside the normal-mode methods, so the web app gets them for free
whenever it wires up ascension mode. These functions type-hint `main`'s
ascension dataclasses (`AscensionFrontierResult`/`StagedPlan`/`StageResult`)
under a `TYPE_CHECKING`-only import (`main.py` imports `report.py`, so a real
import would cycle) but otherwise read them the same duck-typed way this module
already reads `optimizer.SourcingPlan`.

- **`ascension_goal`**: the AK arithmetic (`100 * (target - current)` total,
  starting AK banked, net) plus the smeared chore rate
  (`ascension.chore_rate_per_day()`, ≈2.61 AK/day); an "unmasterable" list
  (`AscensionFrontierResult.unmasterable`) prints/tables the same way
  `print_solution`/`markdown_solution` handle the remainder warning.
- **`ascension_frontier`**: one row per `StagedPlan` — `# | Time | Gold | Net
  gold | Masters | GMs | Megas | Chore AK | Notes` (Markdown) — with
  `[fastest]`/`[cheapest]`/`[selected]` tags exactly like `print_frontier`/
  `markdown_frontier` (the `>` marker replaces a textual "selected" tag in
  text; `include_selected_tag` is off for `SectionedReporter`, same reasoning
  as `markdown_frontier`'s). Masters/GMs/Megas are raw per-stage counts summed
  across the plan (`_ascension_plan_totals`), not deduped — a frontier-row
  summary, unlike the to-do list below. The cheapest index is computed by the
  caller (`main.cheapest_plan(result.plans)`, duck-typed against `StagedPlan`
  exactly as `optimizer._non_dominated`/`_thin` already are) and passed in, so
  `report.py` never needs to import `main` for real.
- **`ascension_plan`**: the selected `StagedPlan`, in order:
  - **Ascension summary** — an AK ledger (`_ascension_ledger`): AK from
    mastery/grandmastery steps, AK from chores, and the AK required
    (`100 * (target - current)`), plus `chosen.carry_out`. Reconciles exactly:
    `starting_ak + steps + chores == required + carry_out` (`carry_out` is the
    balancing term `_run_ascend_pass`'s own carry-threading already computes,
    not recomputed here — see "Ascension orchestration" above).
  - **Level-up timeline** (`_ascension_timeline`) — `start + cumulative stage
    time` per stage boundary ("reach level B ~ *date*"), approximate: v1 does
    not interpolate levels crossed *within* a stage, only each stage's own end
    level gets a date.
  - **Mastery to-do list** (`_ascension_todo`) — per stage, `masters -
    grandmasters` and `grandmasters - megas`: an item that reached grandmaster
    this stage also has its (required-by-the-ordering-constraint) master step
    selected, so it would otherwise show under both; deduped to show once, at
    its highest reached step.
  - **Per-stage sections** — heading `Stage k — level A → B`, with `(reward at
    B: <description>)` appended when `ascension.REWARD_LEVELS` has an entry for
    B, then that stage's full per-channel breakdown via `print_plan`/
    `markdown_plan` reused as-is against the stage's own `StageResult` fields
    (`demand={}` — see below). **Chore-wait critical path**
    (`_ascension_chore_wait`): when `stage.plan.time` exceeds every one of
    `stage.plan.track_times()`'s values (the chore-AK LP constraint can push
    the optimal makespan past every draw track's own time — see
    `SourcingPlan.time`'s ascension note under "The LP and frontier" above),
    the ascension renderer prints its own `"Critical path: waiting on daily
    chores (+X AK over the stage)"` line (`X` = that stage's
    `ak_from_chores`) **instead of** letting `print_plan`/`markdown_plan` call
    their usual `print_critical_path`/`_critical_path_summary` — a pre-check
    the ascension renderer makes itself, via a new `skip_critical_path: bool =
    False` parameter on `print_plan`/`markdown_plan` (default off, so every
    normal-mode call is untouched) that just omits their one internal call;
    `print_critical_path`/`_critical_path_summary`'s own precedence logic
    (see "Critical-path naming" above) is never modified. In practice the two
    never collide: a genuinely chore-bound stage has no draws at all past what
    the cheap steps already gathered, so the internal call finds nothing to
    say anyway (empty `need`/`elsewhere`, no passive draw) — the override
    exists for the (common) case where `need` would otherwise stay silent
    despite real waiting happening.
  - **Demand is always `{}`**: ascension stages have no goal item, only
    mastery candidates (see "Ascension extension" above), so every per-stage
    `print_plan`/`markdown_plan` call passes `demand={}`. `optimizer.plan_balance`
    then only populates `need` from craft-material consumption (an item
    mastered via crafting pulls its materials into `need`, so that gathering
    *does* show under Fishing/Exploring/Farming); an item mastered by *direct*
    gathering (no recipe) is never in `need` or `crafted`, so it doesn't count
    as `interesting` and its gathering draws don't get their own line — the
    quantity produced instead surfaces in the **Surplus** section (when
    `prices` are set) as accumulated leftover, since nothing "demanded" it in
    the classic sense. This is expected, not a bug: the dedicated **Mastery
    to-do list** above already names which items get mastered each stage; the
    per-channel sections show *how*, and for a directly-gathered mastery item
    that's simply "gathered until banked", which the Surplus section already
    expresses.

### Passive-resource breakpoints — the frontier's spine

A building material (Steel Wire, Oak) is needed only up to its products' full
demand. **`passive_residuals(plan, ...)`** gives each passive item's net
requirement in a plan; computed on the **fastest fully self-sourced plan** (the
cheap endpoint — buys nothing, moderate strategy) it is that item's *full*
requirement. Its completion — the makespan at which it finishes gathering, a
natural plateau — is `quantized_wait(item, residual)` (the real deposit-schedule
wait; `residual/rate` continuous only without a quantizer, see the deposit
quantisation gotcha). So the frontier segments by resource: fast crop-bound → Steel
Wire's full completion → Oak's → cheapest. `main.run` passes
`_pet_dominated_items(production)` to exclude pet materials, and passes that
reference plan's `passive_residuals` into `print_plan` as `max_passive_residuals`.
It picks the reference as the fastest plan with `round(gold) == 0` — **not** the
slowest (cheapest-net) plan: the slow, passive-leaning extension can adopt an
extreme strategy (cider stamina from Apples not Orange Juice) that *understates* an
item's requirement, so it is a poor yardstick. It also builds the frontier table's
per-plan **reason** (`_frontier_reasons` → "all Steel Wire from passive") from the
same `_defining_passive_resources`, so the table label and the chosen plan's
critical path agree.

## Critical-path naming (report.print_critical_path) — read this before touching it

This logic took many iterations; the precedence is deliberate. Tracks run
concurrently; fishing/exploring are instant, so the makespan is a **farming** or
**passive** wait. Decision order in `print_critical_path`:

1. **Defining passive resource** (`_defining_passive_resources`, takes the plan's
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
3. else **slowest passive item** (`_slowest_passive_items`) — but a **pet
   top-up** is skipped: a pet supplying most of an item's *passive* rate yet a
   minority of its *total* need is a last resort, not a bottleneck, unless the
   pet is the item's primary source.

`_farming_drives` is shared by the critical-path line and the **Passive
production** section's ordering (lead items = the critical ones) so they agree.
`_pet_passive_rate` distinguishes pets (name-prefixed `PET_PRODUCER_PREFIX`) from
buildings. Both pickers return **every item tied at the longest wait**
(`_tied_slowest`, exact timedelta equality — deposit quantisation makes exact
ties common, e.g. Orange and Lemon both completing at 7d), ordered
biggest-quantity-first; the line, the frontier reason ("all Orange and Lemon
from passive") and the section lead all name the whole tie, so which item an
alternate LP optimum happened to list first no longer flips the report between
runs.

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
- Every other named helper below (`_defining_passive_resources`, `_farming_drives`,
  `_slowest_passive_items`, `_passive_sources`, ...) is called directly by both
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
reporters -- only which `MarkdownDoc` a call's output lands in differs -- with
one deliberate exception: `SectionedReporter.frontier` passes
`markdown_frontier`'s `include_selected_tag=False`, dropping the "selected"
word from the Notes cell's `[fastest, cheapest, selected]` tag. The web app's
own row highlight (`wireFrontierRows` in app.js) already shows which plan is
selected, so the redundant word only cost a layout shift each time a click
moved it from one row's Notes cell to another's; the CLI (through
`MarkdownReporter`, no such highlight) keeps it, the default.
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
  Surplus. (`plan.remainder` — materials with no known source — is reported once, up
  in the Goal section via `print_solution`/`markdown_solution`, not repeated here; see
  the pipeline's "Print" step.) The Buy total is `plan.gold` **net of `pumpkin_gold`** (the
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
- **Ascension mode** (`--ascend`, milestone 5) reuses this exact function
  (`print_plan`/`markdown_plan`) per stage, unmodified apart from one new
  `skip_critical_path` parameter (default off) that lets the ascension-only
  chore-wait override replace the usual critical-path call without duplicating
  it — see "Ascension report sections" above for the full write-up (the AK
  ledger, level-up timeline, mastery to-do list, and why a stage's `demand` is
  always `{}`).

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
- **Tower-level gating** (`config.current_tower_level`/`crafting_bonus_for`/
  `effective_grape_juices`): three pure `(cfg, level)` functions, distinct from the
  Scythe/Monthly-grants pattern above (those auto-detect an *owned* perk from
  mastery progress; these instead read an explicit, optional `[tower] level`
  the player sets — the "ascension" mode (`ascension.py`, pure AK/chore/mega
  arithmetic and reward deltas; the optimiser side is the ascension extension
  under "The LP and frontier" above; `main.solve_ascension_frontier`'s staged
  loop, "Ascension orchestration" above, is milestone 4) climbs it
  mid-solve, calling `ascension.apply_rewards` per stage to bump `[tower]
  level` and apply each reward's own config delta -- these two functions are
  how levels 90/150 take effect from that).
  `current_tower_level` reads it; absent section/key → `None`, meaning "assume
  every level-gated reward owned" (today's behaviour, so a config predating
  this feature is unaffected). `crafting_bonus_for` maps `[workshop]
  resource_saver` (1/2/3) through `RESOURCE_SAVER_BONUSES` (1.10/1.25/1.45) to
  the crafting bonus, clamped by level: below level 90 a configured 3 is capped
  to 2's bonus (the level-90 reward *is* Resource Saver 3), at/above 90 it is
  always 3's; `level is None` applies the configured value with no clamp. No
  `[workshop]` section/key → `crafting.DEFAULT_CRAFTING_BONUS` (1.45), same as
  before this existed. `effective_grape_juices` doubles `[farming]
  grape_juices_per_day` — now the **undoubled** base value — when `level is
  None` or ≥ 150 (the level-150 reward), else leaves it at 1x; `crops.py`
  itself learns nothing about tower levels, `main.build_inputs` just substitutes
  the effective value into the `farming` section dict it hands
  `crops.farm_from_config`. Wiring: `--crafting-bonus` defaults to `None`
  (`main.build_parser`); `main.resolve_crafting_bonus(args, cfg)` fills it from
  the two functions above when unset, called early in both `main.run` (right
  after config load/overrides) and `webapi.solve` (the mirrored point) so every
  downstream reader sees a plain float. `webapi.render` calls it too — a
  cache-hit request builds a fresh, never-resolved `args`, so without this it
  would still read `None` there even though `crafting_bonus` is a
  `RESOLVE_FIELDS` member (a cache hit implies the same *unresolved* value, so
  resolving again against the cached `session.cfg` reproduces exactly what
  `solve()` used).
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
- **Passive deposit quantisation (always-on)**: the LP is pure continuous (the
  aggregate passive offer's smooth per-minute rates are load-bearing for the
  ~80-solve frontier sweep), but real production lands in whole deposits, so
  every residual→time boundary — `minimize`/`frontier` plan times
  (`optimizer._quantize_plan_time`, which only ever *raises* `plan.time`),
  `_passive_completion_caps`, and the report waits (`report._passive_wait`) —
  computes the wait from the deposit schedule via
  `passive.PassiveProduction.quantized_wait`: whole storehouse-clamped
  per-producer deposits (`per_cycle`), same-period producers evenly
  phase-staggered (the raptor pen + snare land 12h apart, so one
  storehouse-worth of Antlers takes 12h), unequal same-period deposits assumed
  to arrive smallest-first (worst case), and *smeared* producers — pets and the
  Mechanical Heart's monthly grants, per `config.is_smeared_producer` — as
  continuous ramps (their per-day figure is already an average; quantising it
  would quantise a fiction). **Fill exemption**: the lift applies only when the
  passive track is the plan's own bottleneck (its track time exceeds every
  other track's). Passive that merely *fills* another track's wait — the
  fastest plan's Orange/Lemon feeding OJ/Lemonade inside farming's 6d 5h — is
  not rounded up past the real driver: the boundary sliver is in practice
  absorbed by the channels already co-sourcing those items (buy a few more OJ),
  an allocation the continuous LP cannot see it should make. `report._passive_wait`
  applies the same rule per row via its `limit` (the plan makespan,
  `_plan_makespan`): a boundary past the plan shows the continuous fill
  estimate, so header, frontier row and rows stay consistent.
  `config.passive_quantizer(production)` binds the
  predicate; `main.solve_frontier` threads the callable into
  `fastest_endpoint`/`frontier` (both, so the seasonal fixed point's horizon
  seed and the frontier agree within `_makespans_close`). Draws, `produced`
  and surplus accounting stay continuous — only reported times move. The
  report's makespan (`_critical_path_summary`, `_farming_drives`) takes
  `max(plan.time, track times)` so the header matches the quantized frontier
  row.
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
  the sawmill boost is used. **The mechanic (load-bearing — get this right):** while
  an omelette is active (one hour) the sawmill deposits an extra
  `HICKORY_OMELETTE_FRACTION` (0.2) of its **hourly** output **every ten minutes**,
  *not* once. Over the boosted hour that is **six** deposits: the top-of-the-hour one
  coincides with the sawmill's normal run and merges into a single `(1+0.2)x`
  deposit, and the other five land on their own — so one omelette adds
  `6 * 0.2` = **+120%** of the hourly output, **each deposit storehouse-capped
  independently** (`config.storehouse_size`, exactly as
  `passive.PassiveProduction.per_cycle` caps the base run). `sources._omelette_bonus`
  computes that per-item bonus: `min(cap, base*1.2) + 5*min(cap, base*0.2) −
  min(cap, base)` (uncapped → `1.2*base`). A fat output whose plain run already fills
  the storehouse (Wood: 60k/h vs a ~50k cap) gets **no** top-of-hour gain — only its
  five small off-hour deposits get through — while the scarce output (Oak, 6k/h) sits
  far under the cap and gets the full +120%. **Regression watch:** commit 54c5a61 once
  collapsed this to a flat `0.2*base` offer (+20% per hour, six times too little);
  the every-ten-minutes-with-per-deposit-cap model above is the correct one — do not
  simplify it back. It is **not** baked into passive rates;
  `sources.sawmill_boost_offers` builds it as a grounded offer on its own concurrent
  `SAWMILL_BOOST_TRACK`: one unit is a boosted **hour**, yielding that whole capped
  bonus and consuming one `hickory omelette` (a `BUYABLE`, priced in `[trading]` at
  350 gk = 0.35/unit, or drawn from held stock). Its own track costs an hour of
  makespan per omelette, so the LP can't boost for more hours than the plan spans
  (past that the boost track becomes binding); nearer the cheap end it boosts fewer
  hours (trading omelette gold for makespan) and `favour_passive` drops it entirely
  (omelettes are in `spend_value`). `all` boosts oak+board+wood; `only_oak` boosts oak alone (board
  and wood are so much more plentiful per hour that oak is the only real value, and
  this guarantees an omelette is never spent to source them). `main.build_inputs`
  builds the offers once (static across the fixed point) onto `SolveInputs.boost_offers`,
  passed to `optimizer.all_offers`; the spend surfaces in the report's **Sawmill
  boost** section (`report._sawmill_boost_summary`) plus From-inventory/Buy, and the
  boost oak nets out of the passive section via `plan_balance`'s `elsewhere`. Web:
  the `hickory_omelette` select + `price_hickory_omelette` params
  (`webapi._CONFIG_PARAM_FLAGS` and the string-override special-case) map to the
  `sawmill.hickory_omelette` / `trading.hickory_omelette` overrides.
- **Limited seeds (Mega/Spring)**: a crop whose CSV `Primary` differs from its
  name (`crops.Crop.seed_item` — the Mega crops and Spring) has finite,
  not-readily-replaceable seeds, so its grounded farming offers consume the held
  seed item (`"<crop name> seeds"`, `PLOTS_PER_ROW * rows` seeds per full-field
  planting; a cookie day consumes a day's plantings' worth). The LP's inventory
  balance then caps plantings at seeds held **plus any modelled supply** (the
  Hummingbird pet passively makes Spring Seeds; Bag of Spring Seeds opens into
  them) — the same held-consumable grounding as the Acorn Pie, surfacing in the
  report's From-inventory section. Ordinary seeds are store-bought in any
  quantity and not modelled. Spring's real drops are the five flower colours
  (its `Primary`, "Mixed Flowers", is the seed description's collective name,
  not an item — hence `spring` sits in `DEWSTAR_EXCLUDED` alongside `mushroom`).
  Note `seed_drops.csv` figures are **per row** (4 seeds each, `per_row_rate`),
  not per seed — a normal crop is ~1/seed, a Mega/Spring seed 10/seed.
- **Infeasible ≠ unobtainable** (`optimizer._assert_optimal`): a demand an
  unusable channel can't source falls to `remainder`, but a demand *beyond a
  bounded channel's total capacity* (only limited seeds create one today) makes
  the LP infeasible — caught after every **uncapped** first solve (endpoints,
  probe, bare `minimize`; capped sweep/refinement solves are feasible by
  construction and HiGHS mis-flags their tiny-RHS pins, so they are exempt and
  fall back to the still-loaded previous solution) and raised as a `ValueError`
  (CLI: `error: ...`; web: HTTP error) instead of an empty 0-minute plan. A
  non-Optimal status is first re-checked with the bundled CBC — HiGHS has
  mis-flagged a feasible model "Unbounded" (impossible: the objective is a sum
  of nonnegative terms) on the tiny gold-weight coefficients — and on rescue the
  rest of that call's refinement stays on CBC.
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
  filtered frontier. The Mastery/Item goal fields are row-based (one
  `{item, amount}` pair per row, `+ Add`/delete buttons via `renderGoalRows`/
  `readGoalRows`/`wireGoalList`), each item input a text `<input list=...>`
  bound to a `<datalist>` catalogue so it suggests known items while still
  accepting free text. `parseGoalSpec`/`serializeGoalEntries` (the only
  exports; pure, unit-tested in `app.test.js` via `web/`'s Vitest setup --
  `npm install && npm test` from `web/`) convert a row list to/from the exact
  `"Item:Amount,Item:Amount"` string `readParams`/`applyParams` always sent,
  so the wire format and `webapi.params_to_args` are untouched. The **config
  preference panes** (Exploring effectiveness, Farming, Pets, Buildings) are
  *generated* from declarative schemas (`CONFIG_FIELDS`/`BUILDING_SECTIONS` +
  `EXPLORING_ZONES`/`PET_NAMES`) rather than hand-written markup:
  `buildConfigPanels` fills each template shell (`.config-fields[data-config=…]`)
  with `createConfigField` controls, each carrying `data-config-key` (a config
  dotted key) / `data-config-type` (int/float/text/date/bool). `readConfigParams`
  collects every set control into one `{dotted key: value}` object sent as the
  `config` param (blank/"Default" omitted); `applyConfigParams` restores them.
  They all funnel through `webapi`'s generic `config` override channel, so no
  per-field wiring exists server-side. Definite booleans are plain checkboxes
  initialised to the config default, only sent when toggled *away* from it;
  perks the solver **auto-detects** from mastery/inventory (Scythe of Dewstar,
  Dewstar's Journal -- the `autobool` type) instead get an Auto-detect / On /
  Off `<select>`, where "Auto-detect" (the default) sends nothing and leaves
  the auto-detection in place. Placeholders and the controls' initial states
  come from `configDefault`, which walks the `#config-defaults` `<script>` JSON
  the index view injects via `webapi.config_defaults` (from
  `web_default_config.toml`). Buildings groups its controls under `<h4>`
  sub-sections (mills combined); Pets renders a compact `.pet-grid`; Instant
  grow time moved from Advanced into the Farming pane (still its own
  `instant_grow_time` param, not the generic channel). `buildConfigPanels` runs
  at the top of `init()`, before the blanket input wiring, so each generated
  control gets its `onAnyChange` listener like a hand-written one. **Every
  parameter** carries a `[?]` help icon (`.help-icon`, a `<button>` with its
  description in `data-help`) -- generated fields from each schema entry's
  `help` string (`createHelpIcon`, appended inside the label), hand-written
  controls from an inline `data-help` in the template. `initHelpTooltips` wires
  one delegated hover/focus listener set that shows a single shared floating
  `.help-tooltip` (positioned in JS above the icon, flipped below near the top
  edge, clamped to the viewport, hidden on scroll/Escape) -- no library, works
  for keyboard focus too. **DEBUG-only CLI command** (`initDebugCommand`): when
  Django's `DEBUG` is on, `views.index` renders a `[</>]` button into the fixed
  `#bottom-bar` (just left of the status box) and a `#debug-popup`; each solve
  response carries a `debug_command` (`webapi.args_to_command`, `None` in
  production) that the popup's textarea shows -- the `python main.py …`
  equivalent of the current web config, for reproducing a solve from the CLI.
  It passes `--config web_default_config.toml` so the CLI loads the same base
  config as the web app (main.py's `--config` flag → `load_config(path)`); only
  the pasted mastery/inventory differ (the CLI reads those from files).
  Two catalogues,
  generated by `scripts/generate_items.py` (one-time/occasional, checked into
  git like the rest of `static/solverapp/`): `mastery_items.txt` (items the
  Mastery picker suggests -- sourced from a single snapshot, the newest
  `mydata/mastery-*.txt`, since not everything obtainable is masterable) and
  `items.txt` (the broad catalogue the Item picker suggests -- every
  `mydata/mastery-*.txt`/`inventory-*.txt` snapshot unioned with every
  per-domain data file: crops, seed drops, fishing, exploring, pets,
  openables, tower, workshop recipes + materials). `collapsePlanGroups`
  (called after `plan.html` is set) turns `markdown_plan`'s flat run of `<h3>`
  sections (Fishing, Exploring (Arnold Palmer), Farming, Surplus, ...) into
  individually collapsible `<details class="plan-group">`, closed by default
  the first time a group is seen; the "Expand all"/"Collapse all" buttons
  next to the "Selected plan" heading (`setAllPlanGroupsOpen`) toggle every
  currently-shown one at once. `collapseGoalSection` (called after
  `solution_html` is set) does the same for `markdown_solution`'s Goal heading
  and its table -- one collapsible `<details class="goal-group">` whose summary,
  when collapsed, adds a `.summary-detail` stat of the goal-row count plus the
  highest tower level named (the Purpose column's `T<level>` cells), defaulting
  open (session-only state in `goalSectionOpen`); the later "Materials with no
  known source" `<h2>` is deliberately left outside so that warning never hides. `#plan-section`'s whole DOM is torn down and
  rebuilt on every re-solve (a plan switch, any param edit), so each group's
  open/closed state is persisted separately, in the module-level
  `planGroupOpenState` map keyed by the group's label -- a `"toggle"`
  listener on each `<details>` covers a manual click, `setAllPlanGroupsOpen`
  writes the map directly for its batch of programmatic changes rather than
  relying on that listener (browsers -- and jsdom -- don't guarantee
  `"toggle"` fires synchronously for those). A never-before-seen group still
  defaults closed. Collapsed,
  a group still needs to read at a glance, so every group's `<summary>` is
  its plain label plus a stat in a `.summary-detail` span -- the same class
  the Goal pane's collapsed-state summary uses, so existing CSS
  (`details[open] > summary .summary-detail { display: none; }`) already
  hides it once expanded. The label/stat split comes from one of three
  places: `DYNAMIC_HEADINGS` regex-splits Farming's and Critical path's
  heading text (the only two that bake a number straight into the heading --
  `f"Farming ({duration}, ...)"` / `f"Critical path (sets the {duration}
  total)"` in `report.markdown_plan` -- unlike Exploring (Apple Cider)/Opening
  (Locksmith), where the parenthesised text is part of the group's *name*,
  not a figure); `GROUP_STAT_EXTRACTORS` (keyed by the group's exact static
  heading text) sums the one column each of Fishing/Exploring (Arnold
  Palmer)/Exploring (Apple Cider)/Opening (Locksmith) is "about" (Large
  Nets/Arnold Palmers/Apple Cider/opens -- always that table's first column)
  and parses Stamina's bullet list for its gained/used totals; failing both,
  `GROUP_TOTAL_LINE_RE` echoes a trailing "Total: .../Nominal value: ..."
  paragraph (Buy, Pumpkin Juice, Surplus). Groups matching none of the three
  (Passive production, From inventory, Sawmill boost) show no stat when
  collapsed -- per spec, only "if one exists".
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
  web/manage.py test solverapp` (see "Web app (web/)" above). JS unit tests
  (the goal-list parse/serialize logic): `cd web && npm install && npm test`
  (Vitest). Regenerate the goal pickers' item catalogues after mastery/
  inventory data or game-content data files change: `python
  scripts/generate_items.py` (writes `mastery_items.txt`/`items.txt` under
  `web/solverapp/static/solverapp/`).
- Commit directly to `main`; end commit messages with the Co-Authored-By trailer.
