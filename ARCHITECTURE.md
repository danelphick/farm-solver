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
explore is cut by Wanderer and the Neigh meal (`use_neigh` -- the one Neigh
reduction, applied once).

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

1. **Load state** — `main.load_game_state` (the single load point all three
   modes -- normal, `--mastery-costs`, `--ascend` -- go through) → `parsing`
   builds `farm_solver.GameState` (masteries + inventory) either from the
   pasted-in text files (`resolve_files` + `load_state`) or, with
   `--backup-file`, from the text a web-app backup carries (see "Solving from a
   web-app backup" below); then `main.apply_inventory_overrides` forces any
   `--override-inventory` items to their given held amount, replacing whatever
   the file said.
2. **Goal → demand** — `farm_solver.solve_tower/solve_mastery/solve_acquire`
   produce a `Solution` with `ItemNeed`s. `build_inputs` computes a `goals_only`
   flag = `args.target == "none"` **or** (`args.target is None` and `-m`/`-i`
   set) — i.e. `--target none` drops the tower explicitly, and omitting
   `--target` does the same implicitly when `-m`/`-i` are set (the previous
   behaviour). When `goals_only`, `farm_solver.solve_goals` combines `-m`/`-i`
   with no tower (each just contributes its own need list; empty when neither is
   set). Otherwise (`--target tower`/`N`, or a bare tower run),
   `farm_solver.solve_target` concatenates the tower's needs with `solve_goals`'
   extra `-m`/`-i` need list. `parse_target` accepts `tower`/`none`/`N`/`TN`.
   `--ignore-goal` then subtracts from whatever that produced:
   `apply_ignored_goals` flags the named items' needs (`ItemNeed.ignored`,
   kept on the `Solution` so the Goal report still lists them, marked) and
   `build_inputs` feeds only the unflagged `active_needs` to demand — see the
   web section's Ignore bullet for the whole round trip. `crafting.top_level_demand` is
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
   `crafting.recipe_flows` gives the craft graph (`FREE_MATERIALS` =
   IGNORED_MATERIALS + STORE_MATERIALS stripped, lower-cased).
3. **Build channels** — `config.passive_production` (buildings + pets),
   `fishing/exploring/crops` loaders, then `optimizer.all_offers` folds them into
   one `list[Offer]`; `sources.open_flows` builds the Locksmith `OpenFlow`s.
4. **Solve frontier** — `optimizer.frontier(...)` → list of `SourcingPlan`,
   fastest first.
5. **Print** — `main.run` picks a `report.Reporter` from `--format`
   (`report.build_reporter`; `TextReporter` prints immediately as before,
   `MarkdownReporter` buffers into a `MarkdownDoc`, `JsonReporter` buffers one
   `MarkdownDoc` per section and dumps them as JSON — see "JSON output" below)
   and calls `solve_frontier`
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
frontier as before. Either way the loop **stops without re-solving when the new
horizon rebuilds a `PassiveProduction` equal to the one the last frontier ran on**
(`solved_for`): seasonal rates average over whole windows, so a moved horizon often
yields the same production, hence the same frontier and selected makespan -- the
third of three ~2s frontiers on a `--plan 3` tower run was exactly that.

## Mastery-costs mode (`--mastery-costs`)

A standalone report dispatched from its own `run` branch (like `--unobtainable`,
but it *does* read the mastery/inventory state): for every remaining masterable
item, the fastest plan to reach its **next** unfinished tier (only that one — an
item at 50k gets a GM row, no MM; the rest are named in `MasteryItemInfo.
more_tiers` and solved on demand via `expand_item`) and that plan's gold. Driven
by `main.solve_mastery_costs` → `MasteryCostsResult`; conflicts with
`--ascend`/`--unobtainable`/`--target`/`-m`/`-i` (checked before any file load or
solve). `--sort time|gold` picks the primary rank axis, the other the tie-breaker
(`_sorted_mastery_rows`). Report: `report.print_mastery_costs`/`markdown_*` via
`Reporter.mastery_costs`. The web app surfaces the same mode on its own page —
see "Web app (web/)" below; that path calls `solve_mastery_costs` directly, so
the conflict check above (a `run`-branch concern) has no counterpart there.

**Why, not just "unobtainable"**: most items the solver cannot source are
perfectly obtainable in game by a route it does not model (cooking, the Wishing
Well, mining, the Sugar Cane Mill), or only at certain times of year, so a bare
"(unobtainable)" is misleading. `unmodelled.csv` (loaded by
`main.load_unmodelled_reasons` → `{lower item: reason}`) names the real reason
per item: a `source` column holding the game feature, rendered
`"<source> not modelled"`, plus three special values —
`seasonal`/`unknown`/`none` → "seasonal"/"not modelled"/"unobtainable"
(`main._UNMODELLED_REASON_TEXT`). Several rows for one item join with "; " in
file order. A named item also **ends** `unmasterable_chain`'s descent: the reason
*is* the explanation, so unpicking a recipe the item does not really need would
mislead (Magna Core is craftable on paper but comes from the Wishing Well).
Unlisted items keep the old wording, so the file is additive — append a row when
a new one shows up. Both reports actually read `main.load_item_reasons`, which layers the
event windows from `items.csv` (`main.load_event_windows`) on top: an event item
gains `"seasonal: <window>"` (bare `"seasonal"` when its event is just `true`),
replacing a plain unmodelled.csv `seasonal` row, else appended after its other
reasons -- so unmodelled.csv keeps `seasonal` rows only for items outside the
museum's Event section (the Polar Bear's frozen crops). The `--unobtainable`
report uses the same reasons. Its scope (`main.UNOBTAINABLE_SCOPES`, a bare
flag meaning `crafting`) picks which items it checks, all against
`main.sourceable_from_game_data` -- `obtainable_items` closed over crafts and
opens via `optimizer._obtainable`. `crafting`: `main.unobtainable_materials`,
workshop materials with no source and no recipe, a listed one suffixed
`" (<reason>)"`, so only the bare names lack a known route. `masterable` (the
mastery catalogue) and `all` (that plus `items.csv` via
`main.load_item_catalogue`, plus every workshop item/material):
`main.unobtainable_items`, which also counts an item blocked by a material at
any depth and explains each with `unmasterable_chain`. The report's wording per
scope is `report._UNOBTAINABLE_TITLES` (`Reporter.unobtainable(materials,
scope)`).

**Filter facts** (the web page's filters; the CLI report ignores them):
`MasteryCostsResult.items` maps each candidate's display name to a
`MasteryItemInfo` -- `categories` (`main.item_categories`, values from
`ITEM_CATEGORIES`), `event` (`load_event_windows`) and `status`, one of three
disjoint classes: `OBTAINABLE` (masterable), `MATERIALS_UNOBTAINABLE` (not
masterable but has a recipe) or `UNOBTAINABLE` (neither). `item_categories`
reads types off the game-data CSVs (crops/seed drops, fishing, exploring),
the workshop recipes, the config's non-pet farm buildings and
`load_unmodelled_sources` (unmodelled.csv's raw `source` cells:
Cooking/Mining/Manual fishing/Sugar Cane Mill); Pets, Locksmith and Other are
**fallbacks** only for an item none of those claims, so a pet's fish stays just
Fishing. The "cannot" lists are `MasteryNote(item, text)`s so the page can
filter them by item too.

Two distinct "cannot" outcomes, kept as separate lists on `MasteryCostsResult`
because they have different causes and different fixes: `unmasterable` (no
source at all — `unmasterable_chain` explains it) and `limited` (sourceable, but
the LP calls a tier's *quantity* infeasible — a finitely bounded supply, e.g. a
limited-seed crop or a material an AP allow-list keeps off the trading table).
The per-tier `fastest_endpoint` call catches that `ValueError` (see
`optimizer._assert_optimal`) and records the tier (only the next tier is ever
tried, so there is nothing further to climb). One bad tier must never abort the whole sweep, which is what it did before: on the
web config, `Bouquet of Flowers` at Megamaster killed all 201 other rows.

Mechanics: `_build_channel_setup` builds the goal-independent channels once, and
production is **steady-state** (`config.passive_production` with no `start`/
`horizon` — the yearly-average calendar), so each item is a single
`optimizer.fastest_endpoint` solve rather than a per-item seasonal fixed point.
The candidate universe is `ascension.candidates(state, catalogue)` (the same
`mastery_items.txt` catalogue the ascension mode uses, so progress-0 items are
included and fully-mega'd ones drop out). For each candidate: `sourceable_items`
(an obtainability oracle over `optimizer._obtainable`) plus a one-layer
producible-direct check decides **masterability** — the item's own mastery must be
*produced* (gathered/opened, or crafted from sourceable materials), never merely
bought/held, mirroring the LP's `buy ≤ derived` floor. `sourceable_items` seeds
`_obtainable` with **exactly** what `_build_base` does (priced `BUYABLE |
ap_tradeable`, and held items an *offer consumes*), so masterability agrees with a
real `-m Item:tier` solve's remainder. The held seed is the load-bearing subtlety:
it is **not** all of inventory — a material merely *held* is a finite hoard, not a
renewable source, so an item craftable only from held-but-unproduced materials
(e.g. Compass ← Magna Core ← Seeing Stone) is correctly unmasterable, not a free
row. A non-masterable item is explained by `unmasterable_chain` (a recursive walk
over `crafting.recipe_flows`, descending only materials **not** in the sourceable
set, with a cycle guard) as e.g. `A needs B (which needs C (unobtainable))`. A
masterable item gets one row, for the first tier in `_MASTERY_TIERS` it has not
reached, demand `(threshold − progress) / mastery_bonus` (from current progress), `non_mastery_goals` empty so the buy-floor forces
production; time is `plan.time`, cost is `plan.gold` (the gold the plan actually
*spends buying*, not `plan_gold`'s market value of everything spent — so a
self-sourced/farmed tier reads 0 g, and `--self-sourced` shows the real drop).
`--self-sourced` (`main.self_sourced_tools` → `optimizer` `no_buy`) forbids buying
Arnold Palmer / Large Net / Apple Cider here too. Fast in practice (~1s on a real
save) because each per-item reachable subtree is small.

## Module map

| File | Owns | Key names |
| --- | --- | --- |
| `main.py` | CLI, orchestration (no printing except ascension mode's TEMPORARY report) | `run` (setup + file loading + the `--unobtainable` branch, then delegates to the three stage functions below, or to ascension mode), `build_inputs` → `SolveInputs` (now `_build_channel_setup` → `_ChannelSetup` + goal-solving layered on top), `solve_frontier` → `FrontierResult`, `derive_report_inputs` → `PlanContextData`, `build_parser`, `parse_target`, `resolve_files` (falls back to `_latest_dated_file` on `solver.data_directory`), `load_game_state` (the one state-load point: a `--backup-file`'s pasted text, else `resolve_files`+`load_state`), `load_backup`/`apply_backup_params`/`Backup`/`BACKUP_FORMAT`/`BACKUP_VERSION`/`_BACKUP_GOAL_PARAMS` (the web app's backup JSON as a CLI input), `selected_plan`, `cheapest_plan`, `resolve_pumpkin_juice`, `resolve_fungus_processing`, `resolve_scythe_of_dewstar`, `resolve_dewstars_journal`, `resolve_mariyas_wheelbarrow`, `resolve_held_item` (the shared config-override-then-inventory body behind all four), `resolve_swamp_puzzle`, `resolve_monthly_grants`, `resolve_current_stamina` (`--current-stamina` over `[farmhouse] current_stamina`, then 0 -- a *reading*, not a building property, so the flag stays and an explicit 0 must beat a stale config value), `resolve_calendar_mode`/`strip_dated_sources` (the `solver.calendar` full/average/ignore enum), `apply_inventory_overrides`, `daily_grape_juices` (the farm's Grape-Juice allowance for the optimiser's daily budget; 0/no-farm = uncapped), `pumpkin_juice_gains`/`PUMPKIN_JUICE_BOOST`, `pumpkin_juice_schedule`, `load_config` (takes an optional path, from `--config`); ascension mode (milestone 4, see "Ascension orchestration" below): `solve_ascension_frontier` → `AscensionFrontierResult`, `_validate_ascend`/`resolve_ascend_params`, `_ascend_stage_cfgs`, `_run_ascend_pass`, `StagedPlan`/`StageResult`; milestone 6 (levels 101-200): `_select_megas`; **`--mastery-costs` mode** (a standalone report, see "Mastery-costs mode" below): `solve_mastery_costs` → `MasteryCostsResult` (`MasteryCostRow` rows + `unmasterable`/`limited` `MasteryNote`s + `items` → `MasteryItemInfo`), `item_categories`/`ITEM_CATEGORIES`, `OBTAINABLE`/`MATERIALS_UNOBTAINABLE`/`UNOBTAINABLE`, `load_unmodelled_sources`, `_sorted_mastery_rows`, `unmasterable_chain`, `_MASTERY_TIERS`/`_MASTERY_TIER_NAMES`, `load_unmodelled_reasons`/`UNMODELLED_PATH`/`_UNMODELLED_REASON_TEXT` (why an unsourceable item really cannot be had) |
| `webapi.py` | framework-free web API layer (no Django import; drives `main`'s stage functions from pasted text instead of files) | `params_to_args` (JSON `params` → `argparse.Namespace`), `_CONFIG_PARAM_FLAGS` (Trading-values price params + `instant_grow_time` → `args.override` entries, so a price change re-solves via the `override` RESOLVE_FIELD), `_ap_trading_override_lines` (the Material-trading pane's `ap_trading` param → `trading.ap.enabled`/`allow`/`deny`/`prices` overrides -- an ALLOW list only, empty allow ⇒ `enabled=false`, prices as a whole-table inline-table replace; see "Material trading pane" below), the generic `config` param → `_config_override_line` (the preference panels' `{section.flag: value}` object → typed `args.override` lines, same channel as the free-text overrides box), `load_web_config`/`WEB_CONFIG_PATH` (the web app's own defaults live in `web_default_config.toml`, separate from the CLI's `config.toml`; every web solve and `config_defaults` load it), `config_defaults` (that config minus `[solver]`, with `[pets]` completed from `pets.csv`, injected into the page for the panels' placeholders, bool checkbox states and the Pets pane's pet list), `args_to_command` (reconstruct the equivalent `python main.py …` command line from a request's args -- the DEBUG-only "CLI command" popup; reuses `args.override` verbatim; when given the `capture_debug_state` paths it emits `--mastery-file`/`--inventory-file` so the command reproduces the exact pasted state), `capture_debug_state`/`DEBUG_CAPTURE_DIR` (DEBUG-only: dump the browser's pasted mastery/inventory text to a fresh timestamped `mastery-<ts>.txt`/`inventory-<ts>.txt` pair under the system temp dir -- one per solve, so past captures survive; `web/solverapp/views._debug_command` wires it in), `fingerprint` (session cache key), `RESOLVE_FIELDS`/`REFILTER_FIELDS`, `SolveSession`/`SolveResult`, `_prepare` (the config-load/calendar-strip/crafting-bonus + `GameState`-from-text preamble both solve paths share), `solve` (full re-solve), `render` (refilter against a cached `SolveSession`, with the one horizon-guard re-solve edge case); **mastery-costs**: `mastery_costs` → `MasteryCostsPayload` (structured rows, not rendered markdown — the client sorts and links per row), `mastery_costs_fingerprint`/`MASTERY_COST_FIELDS` (its own narrower, `"mc:"`-namespaced cache key: the goal/frontier fields never reach `solve_mastery_costs`, so editing a goal must not evict the table) |
| `report.py` | **all printing** + critical-path naming | `Reporter`/`TextReporter`/`MarkdownReporter`/`JsonReporter`/`SectionedReporter`/`build_reporter` (the `--format` facade), `print_frontier`/`print_plan`/`print_critical_path` (text) and their `markdown_*` counterparts, `MarkdownDoc` + its `Block` types (`Heading`/`Paragraph`/`Bullets`/`Table`/`Graph`, each with `markdown()` and `as_json()`) and `Cell`/`Cell.num`/`Cell.duration` (a table cell's printed text + its machine-readable value), `_wanted_amounts`/`_open_yields`/`_format_amounts` (the item->quantity maps behind a "Yields" cell, and the shared joiner both renderers print), `_build_plan_context`/`_PlanContext` (setup shared by both renderers), `build_display`, `_defining_passive_resources`, `_passive_wait` (quantized deposit waits), `_farming_action`/`_cookie_harvest_note` (both renderers' cookie day/burst wording — a repeated burst names its count and its once-a-day cadence, since the quoted time is farming-track time, five minutes a burst); `--ascend` mode (milestone 5, see "Ascension report sections" below): `print_ascension_goal`/`print_ascension_frontier`/`print_ascension_plan` and their `markdown_ascension_*` counterparts (the `Reporter.ascension_goal`/`ascension_frontier`/`ascension_plan` facade methods), `_ascension_ledger`, `_ascension_timeline`, `_ascension_todo`, `_ascension_chore_wait`; `--mastery-costs` mode: `print_mastery_costs`/`markdown_mastery_costs` (the `Reporter.mastery_costs` facade — the table plus its two "cannot" sections, "Items that cannot be mastered" and "Tiers that cannot be reached"), `_mastery_costs_caption` |
| `optimizer.py` | the LP and the frontier, LP backend choice | `SourcingPlan` (`passive_draw`, `track_times`, `cost`, `ascension`), `_Base`/`_build_base`/`_solve_configured` (`minimize`'s split, see below; `_build_base`'s optional `ascension` param and `_solve_configured`'s `_round_and_fix_ascension` are the ascension extension, see below), `minimize`, `frontier` (sweeps the continuous juice relaxation, exact-finishes only the plans it returns — see its cost bullet), `_sweep`/`_UnmeetableCap` (capped sweep solves; a cap the regime cannot reach is dropped, not an error), `fastest_endpoint`, `self_sourced_endpoint`, `plan_balance` (`elsewhere` counts `plan.bought` -- gold buys and AP trades are supply, as in the LP rows; omitting them read AP-traded Twine as a passive residual and `_quantize_plan_time` lifted a 4-minute plan to 213 years), `passive_residuals`, `plan_gold`, `plan_gold_breakdown`, `plan_leftovers`, `leftover_inventory_share`, `accumulated_leftovers`, `surplus_sale_value`, `plan_surplus_gold`, `_quantize_plan_time`, `set_solver_backend`, `_has_integers`; the shared **daily budgets** for cookie sets / Grape Juice live in `_build_base` (`grape_juices_per_day` param, `_Base.grape_juices_per_day`/`days_available` — the integer day count) and are charged as reported time in `_solve_configured` — see that bullet |
| `sources.py` | offer/draw model, per-channel offer builders | `Offer`, `Draw`, `OpenFlow`, `*_offers` (`farming_offers` builds the cookie day/burst pair; the burst carries **no** `max_units` — "once a day" is the optimiser's shared budget), `sawmill_boost_offers`/`_omelette_bonus` (Hickory Omelette boost — 20% every 10 min = +120%/hr, per-deposit storehouse-capped; `SAWMILL_BOOST_TRACK`/`HICKORY_OMELETTE`), `sugar_cane_mill_offers` (Sugar Cane Mill conversion, `SUGAR_CANE_MILL_TRACK`), track constants |
| `farm_solver.py` | game state, goal→needs | `GameState`, `Inventory`, `Solution`, `solve_tower`/`solve_mastery`/`solve_acquire`/`solve_goals`/`solve_target`, `has_tower_level` |
| `ascension.py` | AK/chore/mega arithmetic, stage splitting, reward deltas -- pure domain, no solver imports (only `farm_solver` + stdlib) | `ak_cost`, `megas_required_by`, `chore_rate_per_day`/`chore_ak`, `candidates`/`StepCandidate`, `mega_count`, `advance`, `AscensionSpec`/`AscensionOutcome` (the milestone-3 optimiser handoff), `REWARD_LEVELS`, `stage_bounds`, `apply_rewards`; milestone 6 (levels 101-200): `mega_remaining`, `schedule_megas`, `mega_stage_floors` |
| `parsing.py` | parse pasted mastery/inventory text | `_split_blocks`, `_parse_mastery_block` |
| `crafting.py` | workshop recipes, demand expansion | `Workshop`, `recipe_flows`, `top_level_demand`, `non_mastery_goal_items` (items demanded only as a plain `-i` count -- trade-satisfiable, see AP trading below), `IGNORED_MATERIALS` (auto-supplied Iron/Nails), `STORE_MATERIALS` (Country Store seeds -- free to the LP, but `store_purchases` recounts them from `plan.crafts` for `report._store_buys`' "Buy (at the Country Store)" section, net of held stock), `FREE_MATERIALS` (their union, what the LP strips) |
| `crops.py` | farming model (grow time, GJ, cookies, instant-grow, Dewstar, limited seeds) | `Crop` (`seed_item`), `Farm` (`dewstar_bonus`, `dewstar_cookie_bonus`, `cookie_day_yield`/`cookie_day_harvests` (the latter reporting-only: a cookie day's `(natural, boosted)` Harvest All *counts*, vs. the former's boosted-equivalent yield), `cookie_burst_yield`/`cookie_burst_plantings`/`dewstar_cookie_burst_bonus`), `COOKIE_*` (incl. `COOKIE_WINDOW`), `INSTANT_GROW_MAX_BASE`, `PLOTS_PER_ROW`, `per_row_rate`, `SEED_ITEM_SUFFIX`, `DEWSTAR_EXCLUDED` |
| `passive.py` | passive producers, durations, deposit schedules | `PassiveProducer`, `PassiveProduction` (`producer_rates`, `rate_per_minute`, `per_cycle`, `quantized_wait`), `parse_duration`, `format_duration` (two unit levels by default, "75d 5h"; `units=3` for minutes) |
| `pets.py` | pets as (seasonal) passive producers | `Pet`, `Pets`, `seasonal_fraction` |
| `config.py` | `config.toml` → producers/pets/storehouse/trading | `passive_production` (`monthly_grants` flag; `[orchard] oddvocado_trees` -> a separate daily `orchard_oddvocado` producer splitting the trees by the observed drop-count weights in `ODDVOCADO_FRUIT`, an expectation, unscaled by `bonus`/shaker), `sawmill_outputs` (numeric `[sawmill]` keys only, a zero rate dropped -- an absent/0 `oak` IS "no Sturdy Saw", so no separate flag says it)/`hickory_omelette_mode` (the `[sawmill] hickory_omelette` = none/all/only_oak enum, boost built as offers not baked here), `sugar_cane_mill_rates` (`[sugar_cane_mill]` perks -> `(batches/min, sugar/batch)`, `(0, 0)` unless `have_sugar_cane_mill` (the bought building) is true; built as an offer, not a producer), `PET_PRODUCER_PREFIX`, `MONTHLY_GRANTS`/`_monthly_grant_producers`, `is_smeared_producer`/`passive_quantizer` (deposit quantisation, see gotchas), `storehouse_size`, `trading_prices` (folds AP-trade items in as gold-equivalents, see AP trading below), `arnold_palmer_prices` (the `[trading.ap]` section -> `{item: AP/unit}`; `enabled`/`allow`/`deny`, `enabled_override` for `--use-trading`/`--no-trading`), `current_tower_level`/`crafting_bonus_for`/`effective_grape_juices` (level-gated reward helpers, pure functions of `(cfg, level)` -- see "Tower-level gating" below) |
| `fishing.py`/`exploring.py` | LN/AP channels, modifiers, zone unlocks | `Fishing`/`Exploring` (`acorn_flag`/`acorn_table`), `CiderModifiers`, `*_from_config` (`have_fungus_processing`/`have_swamp_puzzle`), `ACORN_PIE_FLAG`/`FUNGUS_ACORN_PIE_FLAG`/`ACORN_PIE_EXPLORES`, `SWAMP_PUZZLE_ZONE`/`SWAMP_PUZZLE_LOCATION` |
| `locksmith.py` | chests/bags openables | `Openable`, `Locksmith`, `load_openables` |
| `farming.py` | which items are farmed | `FARMING_ITEMS`, `is_farmed` |
| `web/` | Django web app over `webapi.py` (see "Web app (web/)" below) | `solverapp.views.{index,masteries,api_solve,api_mastery_costs,_decode_request}`, `solverapp.cache.{SessionCache,SOLVE_SEMAPHORE,key_lock}`, `solverapp.apps.SolverappConfig.ready` |
| `Dockerfile`/`.dockerignore`/`deploy/` | container image + Google Cloud Run deployment (see "Deployment (Dockerfile, deploy/)" below; README's "Container image and Cloud Run" is the user-facing version) | `deploy/gunicorn.conf.py` (one worker per vCPU, `preload_app=False`, `max_requests=0`), `deploy/cloudrun.sh` (`--source .` build, concurrency/max-instances, the `.run.app` → exact-host `DJANGO_ALLOWED_HOSTS` narrowing) |

Data lives in `*.csv` (crops, fishing, exploring, pets, openables, tower,
seed_drops, plus `unmodelled.csv` — hand-maintained, `#`-commented, the only
one describing what the solver *does not* model; see "Mastery-costs mode"),
`config.toml` and `data/mastery_items.txt` (the masterable-item
catalogue -- the ascension mode's candidate universe and the web Mastery
picker's list, generated by `scripts/generate_items.py` alongside
`data/items.txt`; regenerate with the script, never edit by hand); `items.csv` (`item,event`) is the hand-maintained full game
catalogue, imported from a saved Museum Completion wiki page by
`scripts/import_museum_items.py` (a merge: hand-filled `event` text survives a
re-import); `event` is free text, blank for an ordinary item, else when an
event item is available (`March-April`, a named event, or `true` if unknown) --
`main.load_event_windows` reads it for the reports' seasonal reasons, never parsing the text; game state comes from pasted text files. The `[trading.ap.prices]` block
(and its sibling `[trading.ap.price_bounds]`, a `{item: [low, high]}` guide table
read ONLY by the web Material-trading pane -- `config.arnold_palmer_prices`
ignores it) in `config.toml`/`web_default_config.toml` is likewise generated, by
`scripts/generate_trade_prices.py` from `scripts/trade_prices_snapshot.txt` (see
"Arnold-Palmer trading" above): the price is the low end of each quoted range,
the bounds table carries both ends. The
web app has its own `web_default_config.toml` (a copy of `config.toml`, edited
independently) for a fresh session's defaults -- see `webapi.load_web_config`.

## Core data types

- **`sources.Offer(name, track, yields, consumes, ap, ln, cookies, gj,
  time_per_unit, max_units)`** — one way to gather; `yields` is `{item: qty_or_rate}`.
  `consumes` is `{item: qty}` an activity **uses up** as an input (always grounded:
  an explore consumes an Arnold Palmer, a cider explore consumes stamina + part of
  an Apple Cider, a net a Large Net, a GJ grow a Grape Juice — real crafted / held /
  bought items). `track` is `FARMING/FISHING/EXPLORING/PASSIVE_TRACK/DRINKING_TRACK`.
  Cost fields (`ap/ln/cookies/gj`) are per-unit tallies — for reporting, and (for
  `cookies`/`gj`) for the LP's shared **daily budgets**, see that bullet. `max_units`
  (default `None`) caps the offer's LP usage var (its `upBound`); **no builder sets
  it today** — the cookie burst did, to mean "once a day", until that turned out to
  be a shared budget a per-offer bound cannot express (see the cookie-farming
  section).
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
  (sale value of banked surplus, set by `frontier`), `stamina_gold` (the free-stamina
  opportunity cost, see the Gold-prices section), `remainder`, and `_origin`
  (`(base, kwargs)` stamped by `_solve_configured`, `repr=False`/`compare=False`, so
  `frontier` can re-solve a returned plan exact — see its cost bullet).
  `.cost` is the pair `(time, net gold)` where **net gold = `gold − surplus_gold`
  plus `stamina_gold`** — bought less what the plan banks, plus the free stamina
  it drains — so a slow, passive-leaning plan that banks more than it buys sits at
  *negative* gold and the frontier extends past the "buy nothing" line. Two plans
  equal on both are equivalent however they mix making, buying and banking.
- **`sources.OpenFlow(name, consumes, yields, place)`** — opening a chest/bag: a free,
  instant multi-output LP flow with no crafting bonus. `place` (`LOCKSMITH`
  default, or `SUGAR_CANE_MILL`) picks its report section: the Opening (Locksmith)
  section shows only Locksmith flows.
- **`crops.Farm`** — grow times, `grape_juices_for`, `is_instant`, `veggie_juice_steps`,
  `takes_cookies`, `cookie_day_yield`, `cookie_burst_yield`. **`passive.PassiveProduction`** —
  `producers` list, `rate_per_minute`, storehouse cap.

**Cookie farming — day vs burst** (`sources.farming_offers`): a slow-enough crop
(`Farm.takes_cookies`) gets **two** cookie offers. `grow X with cookies` is a whole
day (`time_per_unit=1 day`, yields `cookie_day_yield` = the day's natural harvests
plus the 5-minute window's 9× boost); repeatable, so an N-day plan gets N daily
bursts. `grow X with cookies (burst)` is the **once-per-day burst alone** — the
boost slice only (`cookie_burst_yield` = `9·(in_window + grape_juices)`, the window
crops + that day's Grape Juice instant grows, the best home for the daily GJ), at
`time_per_unit=COOKIE_WINDOW` (~instant). So a plan that needs
just a sliver of a boosted crop takes it *now* for one cookie set instead of a
fractional day (the old artifact: 0.02 of a day read as a spurious 29-minute
"wait"). The day's *non*-boosted natural harvests remain the plain
`grow X` offer's job. Grounded burst offers spend seeds for `cookie_burst_plantings`
(window + GJ plantings) and the day's Grape Juice; Dewstar's burst bonus is
`dewstar_cookie_burst_bonus`. Adding the instant burst reshapes the frontier for
cookie crops (a fast plan can now cheaply grab a boosted crop with no day-long wait).

**"Once a day" is a *shared* budget, not a per-offer cap** — see the daily-budgets
bullet under "The LP and frontier". The burst offer used to carry `max_units=1`,
which could only ever say "once per **crop**": with ~10 qualifying crops the LP
took one burst of *each* on the same day, spending a cookie set and a whole day's
Grape Juice allowance on every one. On a real save (`-m Pumpkin:MM`) that meant
**16 sets and 211 juices inside six days**, most of them spent bursting crops the
goal did not want — Beet, Broccoli, Cotton, Rice — purely to harvest their Scythe-
of-Dewstar bonus Pumpkins (~280 each, against 9,188 from a Pumpkin burst). The cap
is now `optimizer._build_base`'s makespan-linked pair, so the offer has no
`max_units` at all — which also *frees* the burst the other way: an N-day plan may
burst the **same** crop on each of its days, which the per-offer cap forbade.

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
  - **Daily budgets** (`_build_base`, `_Base.days_available`): a cookie set and the
    day's Grape Juice instant-grow allowance are once-a-**day**, whole-farm
    resources, so what a plan may spend of each is set by **how long it runs**, not
    by how many crops it touches. One shared day-count variable carries both:
    `days_available ≤ makespan/day + 1`, then `cookies ≤ days_available` and (when
    `grape_juices_per_day` is passed — 0 means "unconfigured", cap not applied)
    `Σ o.gj·x ≤ grape_juices_per_day · days_available`. Every farming offer tallies
    its spend in `Offer.cookies`/`Offer.gj`, so one budget is shared across all of
    them — which is what makes the cookie burst once a day *across crops*; see the
    cookie-farming section above for the pathology a per-offer cap allowed. The
    `+ 1` grants the first day's budget at once, so a single burst stays the
    zero-wait action it exists to be (without it a plan wanting one burst would
    have to stretch to a full day — the spurious-wait artifact all over again).
    `makespan` is the max over concurrent tracks, which is the right base: 30 days
    spent fishing really do accrue 30 days of cookie sets to spend on farming.
    Grape Juice is bounded **twice** and needs both: this allowance (how much may
    be drunk in a day) *and* the ordinary item balance (how much is held or
    pressed) — holding 36,691 juices does not lift the daily quota.
    - **`days_available` is an INTEGER**, and has to be: days arrive whole. A plan
      running 6d 4h touches seven days, so it collects **seven** sets, not 7.17.
      Continuous, the LP spent that fractional remainder — and since a part set
      still costs a whole one, the readout's `ceil` then reported an eighth set the
      plan had never run long enough to collect. Integrality makes
      `≤ makespan_days + 1` do the flooring exactly: 7.17 → 7, and an eighth set
      means running a seventh full day. It is the **second** integer in the model
      (juice is the other) and rides the same relaxed-sweep/exact-finish machinery
      — see `_solve_configured`'s `exact_integers` (renamed from `pj_integer`,
      which now toggles both) and `_has_integers`, which decides whether `frontier`
      needs its re-solve pass at all.
    - **Only built when a budget can bind** — some offer spends the resource *and*
      the resource exists (sets held or buyable; an allowance configured).
      Otherwise the variable would constrain nothing while making every solve a
      MIP, so a fishing/exploring-only model stays pure LP. Where it does bind the
      MIP cost is real but modest (one bounded integer, a trivial tree):
      `--mastery-costs` ~2.7s → ~3.9s, `--ascend 5` ~0.85s → ~1.7s on the sample
      save; a single frontier run is unchanged to the tenth of a second.
    - **Reported time**: the actions themselves are near-instant, so the draws
      alone under-report a plan that waits days to collect its sets. `_solve_
      configured` charges that wait at readout — `budget_days − 1` days in **whole**
      days (`ceil`, matching both `days_available` and the report's own rounding,
      less `UNIT_EPSILON` so a solver-returned `7.0000000001` does not charge a
      spurious eighth day) — computed from the plan's **own tallies**, *not* read
      off `base.makespan`. Reading the variable back would only be valid where the
      LP actually minimised it: under an epsilon-constraint cap it minimises *gold*,
      leaving makespan free to park anywhere up to the cap, which would report the
      cap as the plan's time. (Ascension's chore-wait line just below *does* read
      the variable — it only ever solves weighted-sum.)
  - **Buying** (`buy` vars, gold): a buyable consumable (`BUYABLE` — AP/LN/cider/OJ;
    cookie sets via the `cookie_sets` cap + a purchase) gets a `buy` variable adding
    free, instant supply to its balance at `prices` gold cost, the alternative to
    self-sourcing it. `gold = Σ buy*price`. A **`buy ≤ derived`** floor per buyable
    item (derived = its material + channel + open consumption) keeps purchases to
    non-goal uses, so buying can never earn an item's own mastery — a demanded
    consumable is crafted/gathered. `_obtainable` seeds the `buyable` set (a buyable
    is always obtainable), so a channel grounded in one is usable even where nothing
    crafts it; `_reachable` walks from a wanted drop into the consumables it burns.
    A **`no_buy`** frozenset (`_build_base`/`minimize`/`frontier`/`fastest_endpoint`,
    default empty → byte-identical) drops the `buy` var for its items so they must be
    self-sourced — the `--self-sourced` flag (`main.self_sourced_tools` = AP/LN/cider).
    They stay in `buyable` (still obtainable, channels grounded in them still usable)
    and in `prices` (still valued in `spend_value`); only the purchase is withheld, so
    `gold` never charges them. A time-capped plan may then substitute a *pricier*
    allowed buy (Orange Juice to self-make cider), so `no_buy` forbids three buys, it
    does not minimise gold.
  - **Arnold-Palmer trading** (`ap_tradeable`/`non_mastery_goals` params on
    `_build_base`/`minimize`/`frontier`/`fastest_endpoint`, both defaulting empty →
    every existing path byte-identical): resources tradeable for AP (from
    `config.arnold_palmer_prices`, the `[trading.ap]` section) join the buyable
    universe — `buyable`/`buy` gate on `BUYABLE | ap_tradeable`. Their **gold**-
    equivalent (AP × the Arnold Palmer's gold rate) is already folded into `prices`
    by `trading_prices`, so `gold = Σ buy*price` and the LP formula are unchanged;
    a trade is just a buy on the same gold axis (per the user's "convert AP to gold",
    no separate AP objective). **`total_value`** (the stage-2 tie-break, see
    `_solve_configured`) also adds the AP-trade buy gold — `spend_value` only
    enumerates the *consumables*, so without this a traded material read as *free* to
    the tie-break and the fast endpoint (which does **not** pin gold before stage 2,
    unlike the epsilon caps) preferred a huge cheap-per-unit trade over a
    fractionally-priced Pumpkin Juice covering the same mastery goal. Traded items
    earn no mastery like any buy — but the
    `buy ≤ derived` floor is **relaxed to `derived + need`** for an item in
    `ap_tradeable ∩ non_mastery_goals` (a plain `-i` count goal, never `-m`), so a
    trade may satisfy a count goal while a mastery goal must still be produced. Off
    in ascension (those `_build_base` calls pass neither param). `main.SolveInputs`/
    `_ChannelSetup` carry `ap_prices` (per-unit AP, for the report) and
    `non_mastery_goals`; `solve_frontier` derives `ap_tradeable = frozenset(ap_prices)`.
    `report.print_plan`/`markdown_plan` split `plan.bought` into the gold **Buy** and
    an AP **Trade (for Arnold Palmers)** section (AP + gold-equiv totals). Prices are
    generated offline by `scripts/generate_trade_prices.py` from
    `scripts/trade_prices_snapshot.txt` (a committed, hand-captured price list — no
    live-fetch/anti-bot code) intersected with the solver's item universe; the same
    script also emits `[trading.ap.price_bounds]` (`{item: [low, high]}`, the range's
    two ends) for the web pane's guide, which the solver never reads. The web
    Material-trading pane edits `enabled`/`allow`/`prices` per-session (allow list
    only); see "Web app (web/)".
  - **Pumpkin Juice** (`pj` vars, gold): a bought mastery boost, and one of the model's
    two **integer variables** (`days_available`, the daily-budget day count, is the
    other; everything else is a continuous flow). `pj_gain` (from
    `main.pumpkin_juice_gains`) is, per goal item, the demand units each juice covers,
    **best first**: a juice multiplies *current* mastery by `PUMPKIN_JUICE_BOOST`
    (1.1), so `n` juices drunk last cover `T*(1 - 1.1**-n)` and the k-th adds
    `T*0.1/1.1**k` (`/ mastery_bonus` for items), stopping once the run starts at or
    below `have` or a juice adds under 1 mastery (a bare float is the legacy flat
    form, every juice equal); one `pj` var per reachable goal, priced at `pj_price`
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
    - **Compounding, as chords**: the per-juice credits shrink, so the credit of `n`
      juices is concave and `pj_credit <= f(k) + step_k*(pj - k)` for every `k` (the
      chord from k to k+1) bounds it exactly at each whole `pj` -- still one integer
      per item, no extra variables; `upBound = len(steps)`. (Earlier models credited
      10% of the current *tier*, flat per juice: Gold Drum at 49,855/100,000 -> 1M got
      10,000-mastery juices. The compounding model also turned the default tower run
      with juice from ~36s into ~2s.)
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
      (`exact_integers=False`, fractional juice, all LP) and re-solves only the handful of
      plans it *returns* exact (`exact_integers=True`) — the rows of the summary table and
      the expanded plan, the only juice counts a user reads. Cut ~273 MIP solves → ~21;
      `--use-pumpkin-juice` wall-clock 3.15s → 0.76s (juice off, no daily budget
      binding: no integer vars, no MIP, unchanged — but a farming plan with cookies
      held now has `days_available`, so "juice off" no longer implies "pure LP"). Interior *ranking* then uses relaxed costs (off by ≤ one juice's
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
    `pj ≤ 0` neutralisation, and `exact_integers` — toggles the juice vars' **and `days_available`'s** `.cat` between
    `Integer` and `Continuous` for this solve, see the Pumpkin Juice cost bullet) then
    runs the refinement above and reads out the `SourcingPlan`, stamping it with
    `_origin = (base, kwargs)` so `frontier` can re-solve it exact. `frontier` (below)
    reuses one `_Base` across its ~80-call sweep instead of rebuilding the LP from
    scratch per call (`plans/MODEL_REUSE_PLAN.md`).
    **How `_build_base` builds rows**: the hot ones (each item's balance, its buy
    cap, the ascension supply, the track rows, `spend_value`) are collected as plain
    `{variable: coefficient}` dicts — offer/open terms indexed per item once as
    `(variable, coefficient)` pairs — and become one `LpConstraint` each via
    `_accumulate`/`_row`, not PuLP operators: `rate * x` + `lpSum` + `a + b`
    allocates an expression per term and copies the running sum at every `+`, which
    was the biggest cost of a `--mastery-costs` sweep (build 1.58s → 0.60s over its
    192 builds). `_accumulate` mirrors PuLP's arithmetic exactly — a zero coefficient
    adds no term (`0 * x` is empty), a repeat sums in place, insertion order is kept —
    so the model is identical to the operator-built one (verified by dumping every
    row of every build across the CLI modes). The few small constraints (cookie/day
    budgets, juice, stamina, ascension AK) still use operators.
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
  `SourcingPlan` bullet). `main.cheapest_plan` ranks by **net** gold (`plan.cost[1]`)
  — buys least, banks most, drains least free stamina.
  **`accumulated_leftovers`** = `plan_leftovers` minus each item's
  `leftover_inventory_share`: pre-existing inventory sitting unused is **not** counted
  as surplus the plan banks (otherwise every plan on a save with a big consumable
  stockpile shows a large phantom credit and a misleadingly negative net gold). Only
  what executing the plan produces beyond all it consumes (passive, crafts, opens,
  gathering) is credited — the same figure the report's Surplus section now shows
  (accumulated-only; the old inventory/accumulated split is gone). The Surplus
  section (`_print_surplus_value`/`markdown_surplus_value`) closes with a caption
  flagging it as **banked byproduct** already credited into net gold, not gold
  conjured on top — so a big banked figure beside a big Buy line (a plan that buys
  its ciders/OJ at full price and banks the explore loot, net-equivalent to
  self-sourcing) doesn't read as free money.
- **Stamina opportunity cost** (`price_stamina` option, default on): stamina an Apple
  Cider explore burns that arrived free — the banked `--current-stamina` (an inventory
  `held` credit) and the Farmhouse passive deposit — is otherwise a free resource, so a
  plan draining it looks cheaper than it is. `main._build_channel_setup` injects a
  **valuation-only** `prices["stamina"] = orange_juice_price / ORANGE_JUICE_STAMINA`
  key (like grape juice: never in `BUYABLE`, so never a buy var; needs an OJ price;
  off in ascension mode). `optimizer._build_base` then meters the stamina consumed
  with a `stamina_op ≥ Σ(cider stamina) − Σ(Orange-Juice stamina)` variable (`≥ 0`):
  a stamina unit costs its OJ replacement value whatever its source, so **all** of it
  is priced except stamina from *drinking Orange Juice* — the juice is already priced
  as the juice, so charging its stamina too would double-count. Eating an Apple is
  **not** exempt (it once was): those Apples are near-free byproducts of opening idle
  chests (unpriced keys), so exempting their stamina handed the solver a loophole —
  manufacture free Apples to fuel explores and never touch the priced banked/passive
  stamina, dodging the charge (a real plan did exactly this). It adds
  `stamina_price * stamina_op` to **`spend_value`/`total_value`
  only, never `gold`** (using free stamina spends no real gold, so it must stay a
  zero-gold action for the buy-nothing / `--max-gold` endpoints). The readout puts
  `stamina_price * stamina_op.value()` on `plan.stamina_gold`; it is **not** in
  `plan_gold` (not a tradeable consumable's market value) but `cost`'s net gold nets it
  in, so the frontier ranks a stamina-draining plan for it. `main.price_stamina_enabled`
  resolves `--price-stamina`/`--no-price-stamina` over `[trading] price_stamina`.
  `report._net_gold_note`/`_print_stamina_cost` surface it (a `(stamina N, net M)`
  frontier annotation and a "Stamina (opportunity cost)" plan section).
- **Pumpkin Juice net-gold exclusion** (`price_pumpkin_juice` option, default on):
  `pj_price` plays two roles — it **gates** juice (folded into `base.gold` at
  `_build_base`, so an epsilon-min-gold solve drinks a juice only where it beats the
  alternative) and **charges** that cost into net gold. Unlike `price_stamina`, this
  option does **not** touch the LP — the gate always stays. When off it only re-labels
  the readout: the flag rides `_build_base(price_pumpkin_juice=…)` → `_Base` →
  stamped onto every `SourcingPlan`, and `SourcingPlan.cost` subtracts `pumpkin_gold`
  (the `pj_price · juices` slice already inside `gold`) from net when it is False. So
  plans are compared/ranked on **real gold spent**, the juice left as its own count to
  weigh against a value higher than any single tower level's price (why juice is worth
  rationing — see the `--spread pumpkin-juice` bullet, which this pairs with).
  Threaded through `minimize`/`frontier`/`fastest_endpoint` (default True keeps every
  existing caller byte-identical); `main.price_pumpkin_juice_enabled` resolves
  `--price-pumpkin-juice`/`--no-price-pumpkin-juice` over `[trading]
  price_pumpkin_juice`; `report._net_gold_note` breaks the dropped cost out as a
  `(…, juice J, …, net N)` piece so net staying far below the gold cell is legible.
- **`frontier(...)`** spans two regimes meeting at the cheap endpoint, then credits
  each plan's `surplus_gold` (`plan_surplus_gold`) so `cost` is net, then
  `_non_dominated` then `_thin` to `count`, then **exact-finishes** the survivors
  when the model has integer vars at all (`_has_integers`: juice, or a binding daily
  budget): the whole sweep runs `exact_integers=False` (the cheap
  continuous relaxation, since a MIP is ~50x an LP per solve — see the Pumpkin Juice
  cost bullet), and only the returned plans are re-solved `exact_integers=True` from their
  `_origin`, re-quantized/re-surplus'd, and **re-pruned with `_non_dominated`** on those exact costs (an exact re-solve can land a row slower *and* dearer than its neighbour, which the relaxed pruning could not see; it also re-sorts fastest first). It builds
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
- **Juice-count gap-fill** (`_pumpkin_gap_fill`/`_thin_pumpkin`, after the exact-finish):
  a steep juice price (e.g. `trading.pumpkin_juice` pushed to `1e8` to make juice a last
  resort) makes the exact frontier *jump* whole-juice counts — `3, 3, 2, 0`, skipping the
  lone 1-juice plan. The relaxed sweep can't recover it: relaxed juice under-counts the
  exact by a whole juice or two in the transition (the integrality gap), so no swept plan
  rounds to the missing count. Since exact juice is monotone non-increasing in the time
  cap, `_pumpkin_gap_fill` **bisects the cap axis** between two adjacent shown plans that
  differ by ≥2 juice, MIP-solving each midpoint, to pin the fastest real plan at every
  count in `[hi_pj, lo_pj)` (the lower bracket level included, so a *fast* juice-free plan
  surfaces alongside the slow cheapest endpoint). Bounded two ways: `want` (`count −
  distinct`, the spare rows, spent fast bracket first) limits each bracket to the counts
  just under its fast end -- all `_thin_pumpkin` could show -- and a bracket stops
  narrowing at `span/1024` (a minute at least), since a count that *jumps* (23 → 21,
  no 22) otherwise chases its boundary to the minute. The merged set is
  `_non_dominated`-pruned first: where juice is not monotone in the cap the fastest plan
  at a count can be slower *and* dearer than a neighbour. `_thin_pumpkin` then re-thins the merged
  set keeping both ends and the fastest plan per count, spending the redundant same-count
  rows on the decreasing-juice curve (`3, 2, 1, 0, 0`). **Gated** on `distinct counts <
  count`: when juice is cheap enough that every shown row already carries a different
  count there is no spare row for a filler, so the branch does zero extra MIP solves and
  the output is byte-identical to no gap-fill. This is the *only* place the frontier
  MIP-solves points it did not sweep.
- **Spread engine** (`SpreadSpec`/`GraphSeries`, `frontier`'s `spread=` kwarg, `_spread`):
  an alternative to the default time-thin above -- spread `count` plans linearly along a
  caller-chosen axis (`time`/`gold`/`pumpkin-juice`/`passive`) instead of makespan, e.g.
  "show me 0..10 Pumpkin Juice" or "show me the marginal cost of buying more speed".
  `_axis_value` reads a plan's value on an axis. The axes split by *how the value is
  pinned*:
  - **`time`/`gold`** go through `_solve_at_axis_target`, which pins the **time cap**
    (`time`: the cap *is* the target; `gold`: net gold is monotone non-increasing in
    the cap but isn't a clean LP expression to constrain — surplus/stamina fold in
    after the solve — so it bisects the cap like `_pumpkin_gap_fill`). These seed the
    fast/cheap endpoints and dedupe/sort via `_non_dominated` like the default path.
  - **`pumpkin-juice`** pins the juice **variable** directly: juice is the model's one
    integer var, so `_solve_configured(juice_cap=N)` adds `sum(pj) <= N` and takes the
    *fastest* plan under it (`_FAST_PRICE`), one exact MILP per target — no cap
    bisection (which only spanned `t_fast..t_cheap`, the top of the juice range, so
    every target below the cheap endpoint's count collapsed onto it and 0 was
    unreachable). Endpoints are **not** seeded (so a `5:18` range shows only counts in
    [5,18], never the 20-juice fastest leaking in), and the result is **not**
    `_non_dominated`-pruned: whenever juice is cheaper per unit than its substitute
    (the usual case, and why the user rations it — the model prices juice below the
    future tower levels they save it for) a lower-juice row is same-time-or-slower and
    costlier, i.e. dominated, and pruning would collapse the spread to one plan. It
    returns one plan per distinct actual juice count, fastest-first. This supersedes
    `_pumpkin_gap_fill` for that call.
  - **`passive`** anchors its breakpoints on the
  **`favour_passive` probe** (built in `frontier` when surplus is priced) and solves
  each cap `favour_passive` too, not on the cheap-explore endpoint through `base_active`:
  when an active source is cheap in gold but priced in `spend_value` (e.g. an explore
  burning banked stamina), the cheap plan sources everything through it and has no
  passive residual, so a cheap-anchored passive spread collapses to that one explore
  plan and hides the "wait it out" milestones the user wants to boost from. The probe's
  passive bundle has the **pet-dominated items dropped** (`passive_pet_items` yields
  removed from the aggregate passive offer via `dataclasses.replace`) — otherwise it
  leans on the ultra-slow pet trickle for exactly those items (a 200-year tail that
  `_passive_completion_caps` already excludes), so every residual it returns is a pet
  item and the caps come back empty. Dropping the pet supply forces the probe onto the
  real non-pet plateaus (wait for Straw, craft Twine) whose completions are the
  milestones. Falls back to cheap/`base_active` when no probe was built (surplus
  unpriced, or the demand is unsatisfiable pet-free). `spread=None` (the default) leaves
  `frontier` on its old path,
  byte-identical. `efficiency` (gold per second of makespan saved, row vs. the next
  slower row) and `spread_graph_series` (axis + secondary metric -> `GraphSeries` for a
  rendered impact graph) are read-only helpers over any shown plan list, spread or not.

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
  `main.load_mastery_catalogue` reads `data/mastery_items.txt` (`{lower
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

**Whole-grow makespan** (`_farming_grow_time`, folded into `_plan_makespan`): a
crop grows as an indivisible cycle, so a fractional `Draw.units` (0.4 of a pine
grow) still costs a whole grow of real time — the LP leaves `units` continuous, so
raw `Draw.time` under-reports a farming-bound plan ("waits 26 min" for a grow that
truly takes 1h 5m). When farming **strictly** drives (its continuous track time
exceeds every other track by more than `_FARMING_COMAX_MARGIN`, so it is not
co-maximal fill), `_plan_makespan` substitutes the farming track's whole-grow time
(`Σ time_per_unit·ceil(units)`, the same `ceil` `_farming_action` already shows).
Report-layer only — `plan.time`/`cost`/the frontier's sort are untouched, so this
never shifts which plans appear; it only lifts the **displayed** makespan (frontier
row via `_plan_makespan`, plan header, `_farming_bottleneck`). `_farming_drives`
compares on the same whole-grow time so the named driver stays consistent with the
rounded figure. Co-maximal fill farming stays on its continuous time (rounding it
up would wrongly push a passive-bottlenecked plan past its real driver).

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

There are exactly **two** ways a report is laid out, text and Markdown --
`--format json` is a third *serialization* of the Markdown one, not a third
layout (see "JSON output" below), and neither is the web app's
`SectionedReporter`.

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
into a `MarkdownDoc` -- a list of `Block`s (`Heading`/`Paragraph`/`Bullets`/
`Table`/`Graph`), one per report element, kept *structured* rather than
pre-rendered so more than one serializer can read them back (`Block.markdown()`
for the Markdown source `MarkdownDoc.render()` joins, `Block.as_json()` for
`JsonReporter`) -- and only renders in `.finish()`: via `rich.markdown.Markdown` through
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

### JSON output (`--format json`, report.JsonReporter)

`--format json` is deliberately **not** a fourth set of report functions: `JsonReporter` calls the same `markdown_*` functions as
`MarkdownReporter` -- so every row, wording and decision is theirs -- and
serializes the `Block`s they produce via `Block.as_json()` instead of rendering
them to Markdown source. Adding a report section therefore needs no JSON work
at all; the invariant is checked directly by
`tests/test_json_report.py::test_json_blocks_match_the_markdown_reporters`
(identical block streams for identical reporter calls).

The document is `{"sections": [{"name", "blocks"}]}` -- a **list**, because
order is the report's own and a name repeats (a run emits several `notes`, an
ascension run a `plan` per stage). Section names are `SectionedReporter`'s
(`solution`/`unobtainable`/`mastery_costs`/`notes`/`frontier`/`plan`/`crafts`),
so the two structured consumers speak one vocabulary; `ascension_goal`/
`ascension_frontier`/`ascension_plan` land under `solution`/`frontier`/`plan`
for the same reason. A section a call left empty is dropped, so the JSON says
what the other formats print.

Block shapes: `{"kind": "heading", "level", "text"}`, `{"kind": "paragraph",
"text"}`, `{"kind": "list", "items"}`, `{"kind": "table", "columns", "rows"}`
(rows are objects keyed by column name -- every table's headers are unique and
non-empty, being the printed column titles), and `{"kind": "graph", "x_label",
"y_label", "points"}`. `--pager` still requires `--format markdown`.

**`Cell`: the same number, two ways.** A table cell is either a plain `str`
(prose -- an Action, a Detail, a Notes tag, which serialize as themselves) or a
`report.Cell`, which carries *both* the printed `text` and the machine-readable
`value`. `Cell.num(x)` renders `"1,234"` (or `"1,234 g"` / `"1,234 AP"` with a
unit) and emits `x`; `Cell.gold(x)` renders gold to the nearest tenth
(`"1,234.5 g"`, via `_gold`/`_gold_text`, which every gold figure in the text
reports also goes through) and emits that rounded value; `Cell.duration(td)` renders the report's own `"6d 5h 16m"`
and emits whole **seconds**. `MarkdownDoc.table` keeps `Cell`s intact,
`Table.markdown()` renders their text (and runs the `_column_is_numeric`
alignment heuristic over that text, so alignment still follows what a reader
sees), and `Table.as_json()` takes their values. So JSON carries bare numbers
-- no thousands separators, no `g`/`AP` suffix, no `"6d 5h"` -- while text and
Markdown are **byte-identical to before** (verified by a same-timeframe
stash-diff, per the CBC-non-determinism note; the block-parity test also
covers it).

**The `Cell` invariant:** `value` is *the number the report shows*, already
rounded exactly as the printed text rounds it -- never the raw float behind it.
Build a cell from the same rounded expression the text uses (`Cell.num(round(x))`,
`Cell.num(math.ceil(units))`), or the two serializations quietly disagree, which
is the one thing the shared-decisions design exists to prevent.

A few cells are richer than a single number, since JSON has somewhere to put
the structure the printed sentence flattens:

- **"Yields"** (Fishing / Exploring / Opening) is the item->quantity map behind
  the printed `"148,290x Wood"` -- `_wanted_amounts`/`_open_yields` produce the
  map and `_format_amounts` joins it, so *both* renderers print the same
  sentence off the same data (`print_plan`'s Opening yields was folded onto
  `_open_yields` too, removing a duplicated comprehension). Fishing's
  quantity-less variant emits the plain list of names (`_wanted_items`).
- **Graph** carries the `optimizer.GraphSeries` points where Markdown needs the
  inline SVG (`_svg_plot`) -- which is why `markdown_frontier` and
  `SectionedReporter` append a graph via `doc.graph(series, plans)` rather than
  a pre-rendered `doc.para(_svg_plot(...))`.

Three cells deliberately keep a *human annotation* that JSON drops, because the
information survives elsewhere in the document:

- The Goal table's **"Still needed"** prints `"done"` or `"4,616 ✗"`; the value
  is the shortfall itself (`0` when done). The `✗` items are named in full by
  the "Materials with no known source" table right below it.
- The frontier's **Gold** cell prints the `"buy + passive = total g"` split plus
  `_net_gold_note`; the value is that headline total.
- Prose columns (Action, Detail, Source, Notes, Materials used) stay strings:
  they are sentences, not data.

## Printing conventions (report.print_plan)

- Sections: Fishing, Exploring, Opening (Locksmith), Farming, Passive production,
  From inventory (held consumables), Buy (with gold), Pumpkin Juice (mastery boost),
  Surplus. (`plan.remainder` — materials with no known source — is reported once, up
  in the Goal section via `print_solution`/`markdown_solution`, not repeated here; see
  the pipeline's "Print" step.) The Buy total is `plan.gold` **net of `pumpkin_gold`** (the
  juice is listed apart); the frontier table gains a **PJ** column
  (`_pumpkin_juices`) when a plan drinks any. The Pumpkin Juice section names the
  **mastery level to start drinking at** (`main.pumpkin_juice_schedule`: `target /
  1.1**count`, the same compounding climb `pumpkin_juice_gains` credits, clamped ≥
  progress); each juice lifts mastery to the next one's point, so they are drunk back
  to back from there and the last hits the goal -- only the start is shown; no wall-clock time
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
  (`_cookie_sets_used`), not per-crop rounding. Both renderers name the draw with
  the shared `_farming_action` (`"10,471 cookie-boosted days of Corn"` /
  `"cookie burst of Corn"`) and carry a `_cookie_harvest_note` line (the Detail
  column in Markdown) breaking **one unit** into its Harvest Alls —
  `"each day: 74 natural + 15 boosted 9x Harvest Alls, 14 from Grape Juice"`,
  off `Farm.cookie_day_harvests`. That note is the fix for a real misreading:
  the old `"Cookie-boosted Corn in 28y 250d (yield 9x)"` label named the whole
  *span* with the boost multiplier attached, so a plan farming a crop every day
  for 29 years read as if only the 5-minute window were farmed and the other
  23h55m of each day were idle. The burst offer *is* window-only and says so.
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
- **Swamp Puzzle** (`main.resolve_swamp_puzzle`, `exploring.SWAMP_PUZZLE_LOCATION`
  / `fishing.SWAMP_PUZZLE_ZONE`): the **Sinking Swamp** is locked until the
  player holds the Swamp Puzzle, and it locks *both* channels — so unlike Fungus
  Processing (an alternate drop *table*) an un-owned puzzle **deletes the
  location/zone outright** from `Exploring.locations` / `Fishing.zones`, in the
  `*_from_config` builders (`have_swamp_puzzle`, defaulted **True** so every
  pre-existing caller and test is byte-identical). Whatever the swamp alone
  drops then has no source at all, which is the point: Scrap Cloth (and hence
  the Basic Pillow tower level 301 needs) is swamp-only, so a puzzle-less
  account cannot climb past level 300 — the ordinary
  `report.UNREACHABLE_GOAL_MARK` path reports it. The puzzle IS a held item, so
  it auto-detects from inventory like Fungus Processing / Dewstar's Journal,
  overridden by **`[exploring] have_swamp_puzzle`** — one key gating both
  channels (there is deliberately no `[fishing]` counterpart), the same
  cross-section precedent `have_mechanical_heart` sets. Its pieces drop nowhere
  the solver models, so the LP can never craft its way to one; inventory
  presence really is the whole gate. **Unlike every sibling resolver it defaults
  to *on* with no inventory to check** (the `--unobtainable` report, which reads
  no inventory file): that report asks what the game data can source *at all*,
  and defaulting off would silently call every swamp drop sourceless.
  `main._build_channel_setup` resolves it **once** and passes the one bool to
  both `fishing_from_config` and `exploring_from_config`; the `--unobtainable`
  branch in `run` does the same (which is why it builds its own `fish`).
  Fungus Processing needs no such default there: `main.obtainable_items` counts
  every exploring location's **flagged** drop tables as well as the one in
  force, since a flagged table holds items the base lacks (Painite, Hide at
  Gary's Crushroom) and swapping one in would drop the base's Mushroom.
- **Held-item flags** (`main.resolve_held_item`): Fungus Processing and You,
  the Swamp Puzzle, Dewstar's Journal and Mariya's Wheelbarrow are all
  *held items*, so each resolves the same way — `cfg[section][key]` when set
  (planning for a state you are not in yet, in either direction), else the
  item's presence in the inventory, else a `default` with no inventory to check
  (the `--unobtainable` report). That body lives once in `resolve_held_item`;
  each `resolve_*` stays a named one-liner over it because what a reader needs
  is the *domain* fact — what the item changes, and why the Swamp Puzzle alone
  defaults to **True** — which lives in those docstrings. **The resolvers are
  pure**: none writes back into `cfg`. The resolved bool is passed to whatever
  builder consumes it (`fishing_from_config`/`exploring_from_config`/
  `farm_from_config`/`config.passive_production`), which never re-reads the
  config key itself — one owner for the override, no second opinion.
- **Mariya's Wheelbarrow** (`main.resolve_mariyas_wheelbarrow`,
  `config.COW_HORN_MULTIPLIER`): with it the `[cow_pasture]` producer deposits
  `COW_HORN_MULTIPLIER` (3) horns per milk **in the same daily deposit** as the
  milk — one producer, so the two share a storehouse-cap window (deliberately
  unlike the raptor snare / tree shaker, which are separate producers precisely
  to get their own). Its consumer is `config.passive_production` rather than a
  channel builder, so it rides the same route `monthly_grants` does:
  `_build_channel_setup` resolves it → `_ChannelSetup.has_mariyas_wheelbarrow`
  → `SolveInputs.has_mariyas_wheelbarrow` → the `has_mariyas_wheelbarrow=`
  kwarg at every `passive_production` call (`solve_frontier`'s three,
  `solve_mastery_costs`, the ascend stage loop; the `--unobtainable` branch
  resolves inline, inventory-less, like its neighbours). Overridden by
  **`[cow_pasture] has_mariyas_wheelbarrow`**.
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
- `recipe_flows`/optimizer recipes are already lower-cased and `FREE_MATERIALS`
  (iron, nails, and the Country Store's corn/sunflower seeds) stripped.
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
- **Sugar Cane Mill**: not a free passive producer but a grounded **conversion**
  (`sources.sugar_cane_mill_offers`, rates from `config.sugar_cane_mill_rates`):
  a batch presses 7 Sugar Cane + 1 Machine Press into Unrefined Sugar.
  `have_sugar_boost_i` runs two batches a minute (double the inputs),
  `have_sugar_boost_ii` doubles each batch's output (1/min from 7+1 → 2/min from
  14+2 → 4/min from 14+2; II without I is an error). One unit is one mill-minute on
  its own concurrent `SUGAR_CANE_MILL_TRACK`, so like the sawmill boost it costs
  makespan and the mill can't run longer than the plan spans. Built in
  `main._build_channel_setup` alongside the sawmill boost onto the same `boost_offers`
  list (the field now means "extra building offers"). The report's **Sugar Cane
  Mill** section (`report._sugar_cane_mill_summary`) shows sugar, time and inputs.
  **Refining**: `sugar_cane_mill.csv` (`product,material,quantity`, several rows
  per product sum) lists the mill's conversions — Molasses/Brown Sugar/White
  Sugar from 3/5/7 Unrefined Sugar. `sources.sugar_cane_mill_flows` reads it into
  `OpenFlow`s with `place=SUGAR_CANE_MILL`: instant and, being opens not recipes,
  **no crafting bonus** (the mastery bonus still applies, as it divides any
  mastery shortfall whatever the source). `main._build_channel_setup` appends
  them to `open_flows` only when the config has the mill; the report lists them
  in the Sugar Cane Mill section (`report._sugar_cane_mill_conversions`).
  `main.sourceable_from_game_data` folds in a default mill offer (and the
  `--unobtainable` caller the conversion flows) so the mill's products count as
  sourceable, and `item_categories` tags them all Farming.
- **Veggie Juice** (`[farming] use_veggie_juice` → `crops.Farm.veggie_juice`): each
  juice cuts a growing planting's time **left** by 10%, compounding — `k` juices
  grow it in `max(VEGGIE_JUICE_FLOOR (1m), T·0.9^k)`. `Farm.veggie_juice_steps`
  samples `k` (every count to `VEGGIE_JUICE_DENSE`, then ×`VEGGIE_JUICE_STRIDE`,
  ending at the fewest juices reaching the floor; none for instant or
  already-at-floor crops). Time is convex in `k`, so the LP mixing two sampled
  counts over-estimates the time between them, never under. Grounded
  `sources.farming_offers` adds per `k` a `grow X with k veggie juice` (the
  Grape-Juice-folded form: only the natural plantings drink) and, when the crop
  takes GJ, a `grow X without grape juice with k veggie juice`; cookie days stay
  unjuiced. The juice is an ordinary consumed input (crafted, held, AP-traded),
  tallied as `SourcingPlan.veggie_juices`, priced in `_consumable_spend` and in
  `spend_value` (nominal 1e-4 when unpriced, like GJ). `report._crop_of` strips the
  ` with k veggie juice` suffix; `report._juices_used` prints the `using ...` note.
  **Cost**: ~6x the farming offers, so model build time grows (`--mastery-costs`
  ~2s → ~5.7s on the sample save); coarsen the sampling constants if it bites.
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
parameter choices -- one saved state, written by whichever page's settings
popover the user edits (plus browser **IndexedDB** for the dated mastery
snapshots the timeline page charts; see "Mastery history" below). `web/manage.py` inserts the repo root onto `sys.path` (the
solver package is not installed, just imported directly) before delegating
to Django; `web/farmweb/wsgi.py` does the same for a production WSGI server
that imports it without going through `manage.py`.

- **`web/farmweb/`** — the Django project: `settings.py` (deliberately no
  `django.contrib.auth`/`admin`/`sessions`; `DATABASES = {}`, since there is
  nothing server-side to persist; `DEBUG`/`SECRET_KEY`/`ALLOWED_HOSTS` are
  env-driven), `urls.py` (`/` → `index`, `/masteries` → `masteries`,
  `/timeline` → `timeline`, `/api/catalogue` → `api_catalogue`, `/api/solve` →
  `api_solve`, `/api/mastery-costs` → `api_mastery_costs`), `wsgi.py`.
- **`web/solverapp/`** — the one app: `apps.py` (`AppConfig.ready()` pins
  `optimizer.set_solver_backend("highs")` once at process startup -- never
  per-request, since `optimizer._backend` is a module global unsafe under
  concurrent solves in the same process -- and refuses to start without it),
  `views.py` (`index`/`masteries`, the two pages; `api_solve` and
  `api_mastery_costs`, `csrf_exempt` JSON POSTs -- no session/cookie auth
  exists to forge -- sharing `_decode_request`, which turns the common
  `{mastery_text, inventory_text, params}` body into `args` or the 400 to
  return instead), `cache.py` (`SessionCache`: an in-process LRU(64)/TTL(30
  min) `OrderedDict` holding **both** artefact kinds -- a
  `webapi.SolveSession` keyed by `webapi.fingerprint` and a
  `webapi.MasteryCostsPayload` keyed by `webapi.mastery_costs_fingerprint`,
  whose `"mc:"` prefix keeps the key spaces apart; one cache, not two, since a
  second `SessionCache` instance would share the module-level `_entries` dict
  anyway. Each view narrows what it gets back with an `isinstance` check rather
  than trusting the prefix. Plus `SOLVE_SEMAPHORE`, one LP solve at a time per
  process; `key_lock`, a per-fingerprint lock guarding against a dogpile of
  identical concurrent requests all missing the cache at once), `tests.py`
  (Django `SimpleTestCase` -- no DB to wrap in a transaction), `templates/`
  (the three pages plus `_settings.html`, the settings popover they all
  include), `static/`. `views.timeline` is the odd one out: it renders
  `timeline.html` (plus `config_defaults`, which the popover needs) and does
  nothing else -- that page has no API and never solves, because everything it
  charts lives in the browser (see "Mastery history" below).
- **`api_solve`'s flow**: `_decode_request` → `webapi.params_to_args`
  (`ValueError` → 400) → `webapi.fingerprint` → `cache.key_lock` (serialises
  same-fingerprint requests) → cache lookup → `SOLVE_SEMAPHORE.acquire(timeout=20)`
  (else 503) → `webapi.solve` on a miss (`ValueError` from an unparseable paste
  → 422) → `webapi.render` → `cache.CACHE.put` → each `SolveResult.sections[...]`
  markdown rendered to HTML via `markdown.markdown(text,
  extensions=["tables"])` → JSON response (`cached`, `solved_ms`,
  `solution_html`, `notes`, `warnings`, `frontier.{html,plans}`,
  `plan.{index,html}`).
- **`api_mastery_costs`'s flow**: the same shape, but keyed by
  `webapi.mastery_costs_fingerprint`, solved by `webapi.mastery_costs`, with no
  refilter step and **its own longer semaphore budget**
  (`_MASTERY_SEMAPHORE_TIMEOUT_SECONDS = 60`): the sweep is one LP solve per
  remaining (item, tier) pair -- ~1s on a well-progressed save but ~5s on an
  empty one and slower on the deploy box, so `api_solve`'s 20s would spuriously
  503 a request queued behind one. The response is `{cached, solved_ms, rows,
  unmasterable, limited, items}` with `rows` **structured** (`item`/`tier`/`time`/
  `time_seconds`/`gold`), not markdown-rendered HTML like every other section:
  the client sorts the rows itself and turns each into a link, both of which
  need the item and tier as data. `unmasterable` and `limited` are the two
  "cannot" lists (see "Mastery-costs mode" above) as `{item, text}` entries,
  each rendered into its own collapsed `<details>` on the page; `items` is the
  per-item filter facts (`MasteryItemInfo`). `masteries.js` filters all three
  sections client-side (`itemMatches`/`filterEntries`, driven by the Filters
  pane: name substring, event toggle, type and obtainability checkboxes,
  persisted in the UI localStorage key as the *unticked* values). The template
  writes the checkboxes out literally; a Django test keeps them in step with
  `main.ITEM_CATEGORIES` and the three status constants.
- **`static/solverapp/app.js`** (vanilla JS, no build step): state
  (`{mastery_text, inventory_text, params}`, mirroring `POST /api/solve`'s
  body) round-trips through `localStorage["farm-solver.v1"]`, restored and
  auto-solved on load. It owns only the **Goal** pane and the results -- every
  other parameter lives in the settings popover (`settings.js` below), so
  `readParams`/`applyParams` are each two halves: the goal block here, plus
  `readSettingsParams`/`applySettingsParams` there. Three request cadences: a
  Goal edit debounces 400ms then re-solves; a popover **Save** re-solves once
  (its edits are staged until then); a frontier row click re-solves
  immediately. A monotonic sequence number drops a response superseded by a
  newer request. The frontier table's rows are zipped with the response's
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
  `npm install && npm test` from `web/`; the page-level tests build their DOM
  from the **real** templates through `template-fixture.js`, which expands the
  `_settings.html` include before stripping the Django tags) convert a row list
  to/from the exact
  `"Item:Amount,Item:Amount"` string `readParams`/`applyParams` always sent,
  so the wire format and `webapi.params_to_args` are untouched. **DEBUG-only CLI command** (`initDebugCommand`): when
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
  git under the repo-root `data/`, which `settings.STATICFILES_DIRS` serves at
  `/static/solverapp/`): `mastery_items.txt` (items the
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
  currently-shown one at once. The calendar caveat ("starting <date>; seasonal
  pets ... count only for the part of the plan they fall in") rides
  `SolveResult.plan_note` → `plan.note` into `#plan-note`, a small
  parenthetical inside that heading, not the notes area. `collapseGoalSection` (called after
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
- **`static/solverapp/settings.js` + `templates/solverapp/_settings.html`** —
  the **settings popover**, shared verbatim by both pages (each template
  `{% include %}`s the partial and injects `config_defaults`). A cog button
  fixed top-right (`#settings-btn`) toggles a panel holding every parameter
  except the solver page's Goal: the pasted game state, the frontier Limits,
  and all the preference panes. Unlike `app.js` this module registers nothing
  at module scope, which is what lets the mastery-costs page import it.
  **Edits in it are staged**: opening snapshots `readSettingsState()`, Save
  closes and calls the page's `onSave`, and Cancel/Escape/the x/an outside
  click re-apply the snapshot -- so pasting a save costs one solve, not one per
  keystroke, and no control inside the panel gets a change listener that
  solves (only the UI-only side effects: the juice cap's enable, the calendar's
  `#start-date` enable, the spread range row, the material filter/bulk
  buttons, and the "You have N in inventory." appended to the juice cap's help tooltip, which
  `inventoryQuantity` -- a pure, unit-tested JS reading of a pasted inventory,
  on `history.js`'s exported `splitBlocks` -- recomputes as the paste changes). `readSettingsParams`/`applySettingsParams` are exact inverses (both
  run on every page load), which is what makes Cancel a one-liner.
  `GOAL_PARAM_KEYS` names what the panel does *not* own and
  `mergeSettingsIntoState` (pure, unit-tested) is how a page folds a Save back
  into the saved state: the goal keys are kept, every other key is **dropped
  and replaced**, so a field the user blanked really goes away. The module also
  owns the shared `STORAGE_KEY`/`UI_STORAGE_KEY` constants, `readUi`/`writeUi`,
  and `restorePaneStates` (each `details.pane`'s open/closed state under the UI
  key -- never a re-solve, and document-wide so it covers each page's own panes
  too).

  **Pane layout (`style.css`)**: every pane in the popover wraps its content in a
  `.pane-body` div, which is an `auto-fit`/`minmax` grid -- so the field rows flow
  into two columns on a desktop window (the popover is `min(52rem, 100vw - 2rem)`)
  and one on a phone, with no media query. Two details make it work: the grid has
  to be that wrapper rather than the `<details>` itself (a `<details>` renders its
  non-summary children inside a `::details-content` box, so gridding the
  `<details>` lays out only summary + that box), and the generated wrappers
  (`.config-fields`, `.config-section`) are `display: contents` so their rows
  become cells of the pane grid instead of stacking inside one. Anything that
  reads across the pane spans `1 / -1`: hints, textarea `.field`s, Buildings'
  `<h4>` sub-headings, the nested grids (`.paste-fields`, `.pet-grid`, the
  material list), and rows tagged `.field-row.wide` -- the Calendar radios and the
  spread `lo:hi` pair, which carry more than one control after their label. The
  track minimum (21.5rem) is the widest row that must not wrap: an 11rem label
  plus a 10rem date field.

  The **config preference panes** (Exploring, Fishing, Exploring effectiveness,
  Farming, Pets, Buildings, plus generated rows inside Resources -- Resource
  Saver and the Mechanical Heart -- and Trading values -- the grapes AP price
  and the stamina charge, the two `[trading]` values with no dedicated price
  param) are
  *generated* from declarative schemas (`CONFIG_FIELDS`/`BUILDING_SECTIONS` +
  `EXPLORING_ZONES`, and `petNames()` -- the numeric keys of the injected
  `[pets]` defaults, which `webapi.config_defaults` completes from `pets.csv`
  (level 0 for an unconfigured pet), so a new pet needs no UI change) rather
  than hand-written markup:
  `buildConfigPanels` fills each template shell (`.config-fields[data-config=…]`)
  with `createConfigField` controls, each carrying `data-config-key` (a config
  dotted key) / `data-config-type` (int/float/text/date/bool). `readConfigParams`
  collects every set control into one `{dotted key: value}` object sent as the
  `config` param (blank/"Default" omitted); `applyConfigParams` restores them.
  They all funnel through `webapi`'s generic `config` override channel, so no
  per-field wiring exists server-side.
  A schema field's `enabledBy: "<bool key>"` gates it (`data-enabled-by`):
  `syncGatedConfigFields` disables it unless that checkbox is ticked and itself
  enabled (so chains cascade: Sugar Boost II <- Boost I <- the Sugar Cane Mill),
  re-run on the gate's change and after `applyConfigParams`; a disabled bool
  reads as unticked in `readConfigParams`.
  **`data-config-key` is a page-wide contract, not just the generated panes'
  plumbing**: every control mirroring a config key carries it (plus a
  `data-config-type`), *including* the hand-written ones -- the prices, the
  calendar radios, Current stamina, Hickory Omelette, instant grow time, the two
  Pumpkin Juice toggles -- so an external tool (the intended one being a browser
  extension that scrapes the game and fills the panel in) can address a field by
  what it *means* rather than by CSS id, then Save. Those hand-written controls
  ship under their **own** request param, though, so they also carry
  `data-config-own-param` and both readers select
  `GENERIC_CONFIG_CONTROLS` = `[data-config-key]:not([data-config-own-param])` --
  without that they would be sent twice, by two routes, and the panel's own
  read/apply pair would fight the generic one. `applyOwnParamPlaceholders`
  (`initSettings`, right after `buildConfigPanels`) fills those controls'
  placeholders from the injected config too, so a price hint comes from
  `web_default_config.toml` rather than a number hard-coded in the markup that
  drifts from it. A control with *no* key has no config key to mirror: the paste
  boxes, Cookie sets (read off the pasted inventory), everything under Limits.
  The calendar's three radios deliberately share one key -- a radio group is one
  setting -- which is safe precisely because own-param controls never reach
  `readConfigParams`, whose `{key: value}` object could not hold a duplicate. Definite booleans are plain checkboxes
  initialised to the config default, only sent when toggled *away* from it;
  perks the solver **auto-detects** from mastery/inventory (Scythe of Dewstar
  and the Mechanical Heart from tower progress; Dewstar's Journal, Fungus
  Processing and the Swamp Puzzle from inventory -- the `autobool` type) instead
  get an Auto-detect / On / Off `<select>`, where "Auto-detect" (the default)
  sends nothing and leaves the auto-detection in place. An `int`/`float` entry may
  carry optional `min`/`max` bounds for a fixed-range field (Wanderer level 0-4,
  Resource Saver 1-3); an out-of-range value the browser lets through is caught
  server-side and comes back as a 422 with the config loader's own message.
  A section reaches the page only if it is BOTH in `buildConfigPanels`' list and
  has a `.config-fields[data-config=…]` div in `_settings.html` -- adding a
  schema without the div (or vice versa) silently drops the whole pane, which is
  what `settings.page.test.js`'s "every config schema section is generated"
  test pins. Panes are grouped by *what a setting does*, not which config
  section it lives in: the `[exploring]` keys that set Arnold Palmer yield or
  cider stamina cost, plus the two unlocks changing what a zone drops (or
  whether it can be entered), all sit in the **Exploring** pane, while
  `have_mechanical_heart` -- free consumable supply --
  sits in **Resources**, immediately above the Calendar radios that decide how
  its monthly grant days are counted; `[workshop] resource_saver` (what a craft
  yields) joins it there, above the Heart. Likewise `[trading] grapes` and
  `price_stamina` ride the generic channel into the hand-written **Trading
  values** pane rather than becoming `_CONFIG_PARAM_FLAGS` price params: one is
  priced in AP not gold, the other is a switch, so neither fits that pane's
  "gold price" shape. Placeholders and the controls' initial states
  come from `configDefault`, which walks the `#config-defaults` `<script>` JSON
  both views inject via `webapi.config_defaults` (from
  `web_default_config.toml`). Buildings groups its controls under `<h4>`
  sub-sections (mills combined); Pets renders a compact `.pet-grid` (three
  columns of name + level across the open popover, one on a phone); Instant
  grow time moved from Advanced into the Farming pane (still its own
  `instant_grow_time` param, not the generic channel). `buildConfigPanels` runs
  at the top of `initSettings()`, so every generated control exists before a
  saved state is applied into it. **Every
  parameter** carries a `[?]` help icon (`.help-icon`, a `<button>` with its
  description in `data-help`) -- generated fields from each schema entry's
  `help` string (`createHelpIcon`, appended inside the label), hand-written
  controls from an inline `data-help` in the template. `initHelpTooltips`
  (in **`help.js`**, shared with the mastery-costs page) wires one delegated
  hover/focus listener set that shows a single shared floating `.help-tooltip`
  (positioned in JS above the icon, flipped below near the top edge, clamped to
  the viewport, hidden on scroll/Escape) -- no library, works for keyboard focus
  too. The **Material trading pane** (`buildMaterialTradingPane`,
  also run from `initSettings()`) is a checkbox + price +
  guide-range row per tradeable material, built from the injected config's
  `trading.ap` (`prices` is the item universe and default apk, `price_bounds` the
  greyed `low–high` guide, `enabled`/`allow`/`deny` the default checkbox state via
  `computeDefaultAllowed`). It always emits an **allow** list, never a deny list:
  `readMaterialTrading` → `computeApTradingParam` (both exported, unit-tested) sends
  the `ap_trading` param **only when the pane deviates** from the config default (a
  narrowed allow set, or an edited price), so an untouched pane adds nothing to the
  request; `prices` is included only once a price is actually edited, and then
  carries every allowed item (edited value or default) because the server-side
  override *replaces* the whole `[trading.ap.prices]` table (see
  `webapi._ap_trading_override_lines`). Enable-all / Disable-all / Restore-defaults
  buttons and a UI-only name filter (`filterMaterialRows`, which never becomes part
  of the state) sit above the list; "Restore defaults"
  resets to `computeDefaultAllowed` and clears price edits. (The pane's buttons carry
  `.btn-add` for styling but no `data-goal`, so the goal-list append wiring is scoped
  to `.btn-add[data-goal]`.)
- **`static/solverapp/masteries.js`** — the mastery-costs page's client,
  deliberately **not** an `app.js` import: `app.js` registers its
  `DOMContentLoaded` → `init()` listener at module scope, so importing it would
  run the solver page's whole wiring against markup that isn't there. What is
  shared instead are the modules that register nothing: `settings.js` (the
  popover above), `help.js` (the `[?]` tooltip machinery) and `tiers.js`
  (`tierLabel`: the `M`/`GM`/`MM` ↔ Master/Grandmaster/Megamaster mapping,
  mirroring `main._MASTERY_TIERS`; plus `tierRank`, the M<GM<MM sort order the
  Target column uses — never alphabetical, a plain amount ranks last); the
  handful of one-line helpers (`$`, `escapeHtml`) are duplicated rather than
  given a module. The page has no *goal* of its own: it reads
  `localStorage["farm-solver.v1"]` for what to solve (adding a single
  `self_sourced` param on top) and, on a popover save, **writes** it back
  through `mergeSettingsIntoState` -- keeping the solver page's goal, which the
  popover never shows -- then re-requests. So both pages always agree on what
  "your save" means, and either can edit it. **Expand toggle**: the table
  solves only each item's next tier; `MasteryItemInfo.more_tiers` (in the
  payload's `items`) names the rest, and such an item gets a `.tier-toggle`
  before its name (a same-width `.tier-toggle-slot` on the others, so names
  align). The first open POSTs `/api/mastery-costs/tiers` (`views.
  api_mastery_cost_tiers` → `webapi.mastery_cost_tiers` →
  `solve_mastery_costs(..., expand_item=item)`, which solves only those further
  tiers and stops climbing at the first unreachable one; cached server-side
  under `mastery_cost_tiers_fingerprint`, the table's key plus the item). The
  result lives in the client's `expansions` map (item → open/status/rows/
  limited) until the next table load, so closing and reopening never refetches;
  a response for a since-reloaded table is dropped. Expanded rows render
  directly under their item whatever the sort (`expansionHtml`), not ranked
  with the rest. `sortRows(rows, key, direction)` and `masteryHref` are the pure,
  unit-tested exports (`masteries.test.js`); `SORT_COLUMNS`/`SORT_KEYS` name the
  four sortable columns and the value tuple each ranks on (primary then
  tie-breaks — the two numeric axes still break on each other like the CLI's
  `main._sorted_mastery_rows`, Item on the target). A key may also carry `fixed`
  tie-breaks, which are compared **ascending whichever direction the primary
  sorts**: Target breaks on time then gold, so flipping it to MM/GM/M reorders the
  target groups while each group stays cheapest-first. **Every column
  header carries a sort button** (`#mastery-costs-head`, one delegated click
  listener): clicking a column sorts ascending, clicking the active column again
  flips it (arrow glyph + `aria-sort` via `renderSortIndicators`). Rows are built
  in JS rather than markdown→HTML because each `<tr>` has to carry its own
  item/tier to become a link. Sorting reorders the loaded rows with no request;
  the self-sourced checkbox re-POSTs; the sort key + direction
  (`mastery_sort`/`mastery_sort_dir`) and the checkbox all persist under the
  shared `farm-solver.ui.v1` UI key.
- **Drill-down is a plain navigation, not a third view**: a mastery-costs row
  links to `/?mastery=<Item>:<TIER>` — the solver page itself, so the whole
  existing frontier/plan/crafts UI is reused and the browser's Back button
  returns to the list natively. `app.js`'s `parseSeededGoal` (exported, unit
  tested; reuses `parseGoalSpec`'s grammar so a colon-bearing item name still
  splits correctly) reads that goal off the query string and `applySeededGoal`
  sets Target = "none" plus that single goal row, on top of the restored
  settings, revealing a `#seeded-banner`. **Two goal kinds**: `?mastery=` seeds
  the mastery list, `?items=<Item>:<N>` the item list (a mastery goal wins if
  both are somehow present). The second exists for the timeline index's
  next-goal links (`timeline.nextGoalHref`), which aim at the tier above where
  an item stands — but an already-Megamastered item has no tier left, and a
  mastery goal below its progress is already met and would plan nothing, so it
  asks for `MEGAMASTERED_GOAL` (10,000) more of the item instead. It deliberately does **not**
  `saveState()`: following a link must not overwrite the goal the user had
  saved, though their first edit persists it as usual via `onAnyChange`. The
  tier token survives as free text end to end (`parsing.parse_amount` reads
  `M`/`GM`/`MM` as the absolute 10k/100k/1M threshold), which is exactly the
  cumulative target the row was solved for -- so the drill-down reproduces its
  own row.
- **Focus: the solver page drills into its own goals the same way.** When the
  rendered Goal table (`report.markdown_solution`'s Purpose/Item/Have/Target/
  Still needed) has more than one row -- a tower run expands to dozens of
  required masteries -- `app.js`'s `addGoalRowActions` appends a small "Focus"
  link to each row's Item cell, built by `focusGoalHref` (exported, unit
  tested): `Inventory` reproduces as `?items=`, a tower level (`T240`) or
  `Mastery` as `?mastery=`, seeded with that row's own **Target** (commas
  stripped; a non-numeric target means no link). A sole goal gets none -- it is
  already focused. Plain `<a>`s, so Back returns to the original goals natively,
  and `localStorage` still holds them (the seeded page saves nothing). The
  Goal table is located by `findGoalSection`, shared with `collapseGoalSection`
  so both agree which table is the goal's -- "Materials with no known source"
  is a later `<h2>` section and never gets buttons.
- **"Make Sole Goal"** (`#seeded-adopt` in the banner, `adoptSeededGoal`) is the
  other half: the seeded page's form already holds that goal and nothing else,
  so `saveState()` **is** the substitution into the main parameters (every other
  setting, restored as usual, is untouched). It does not re-solve -- the results
  on screen are already that goal's -- hides the banner, and drops the
  `?mastery=`/`?items=` query with `replaceState` (not `pushState`, so Back is
  left alone) to stop a reload re-seeding what is now the saved goal.
  `seededOrigin` reads the `from=goal` the Focus links add, which is what points
  the banner's way back at `/` ("back to your goals") instead of the
  mastery-costs table every older seeded link came from.
- **Ignore: a goal the plan should skip** (`--ignore-goal`, web param
  `ignore_goals`). The second control `addGoalRowActions` puts in each Item cell,
  beside Focus and under the same more-than-one-row rule (ignoring a sole goal
  would leave nothing to solve). It is a `<button>`, not a link: it toggles the
  item in `app.js`'s module-level `ignoredGoals` (pure helpers `parseIgnoreSpec`/
  `isIgnoredItem`/`toggleIgnoreSpec`, unit tested), saves and re-solves at once,
  undebounced. **The row has to survive the re-solve for the toggle to be
  reversible**, which is why ignoring is not a filter: `main.apply_ignored_goals`
  flags the matching needs with `ItemNeed.ignored` and leaves them on the
  `Solution`, and `build_inputs` passes only the *unflagged* ones
  (`active_needs`) to `crafting.top_level_demand`/`non_mastery_goal_items`/
  `pumpkin_juice_gains`. So the Goal table still lists the item --
  `report.IGNORED_GOAL_TEXT` in its "Still needed" cell, value 0 in JSON -- while
  the LP never sees it. Ignoring is per **item**, not per row
  (`main.parse_ignored_goals` lower-cases a comma list of names), so one toggle
  covers a tower requirement repeated across levels. Only the *goal* goes: an
  ignored item is still crafted when another goal needs it as a material, which
  is the LP's own balance and not a demand figure. The greying
  (`tr.ignored-goal`, a muted line-through; `.focus-link` is an `inline-block` so
  the strike does not propagate into it) and the button's pressed state both come
  from `ignoredGoals`, not from the server's cell text -- one source of truth.
  `ignore_goals` is a `RESOLVE_FIELDS` member (it changes the solve, so it
  re-keys the cache) and a `GOAL_PARAM_KEYS` member (the settings popover must
  not drop it on Save). `applySeededGoal` clears it: a seeded page is "just this
  goal", and an ignore naming that very item would otherwise solve for nothing.
- **Mastery history (`static/solverapp/history.js`) and the timeline page** —
  the one feature with **no server side at all**. The settings popover's
  **"Save dated snapshot"** button (in the `#paste-details` pane, under the
  mastery textarea) parses the mastery box and records it in this browser's
  **IndexedDB**; `/timeline?item=X` charts one item across those snapshots.
  Nothing is sent to or stored on the server; the only way it leaves the
  browser is the backup file the user asks for (`backup.js` below).
  - **Why IndexedDB, not the `localStorage` everything else uses**: one
    snapshot of a well-progressed save is ~15 KB of JSON and history
    accumulates, which would eat the ~5 MB budget the solver state lives in.
  - **`parseMasteryText`** is a JS mirror of `parsing.parse_masteries` /
    `_parse_mastery_block` (same block splitting, same `<n> / <total|∞>
    Progress` line, same "throw rather than silently drop items" contract, the
    client-side twin of the 422 an unreadable paste gets from `/api/solve`) --
    so saving a snapshot and drawing a timeline need no round trip. It is one
    of the tested mirrors, like `tiers.js` and `masteries.js`'s `sortRows`.
  - **Store**: database `farm-solver-history`, object store
    `mastery_snapshots`, `keyPath: "taken_at"` (an ISO timestamp, so key order
    *is* chronological order) with a `date` index. One record is `{taken_at,
    date, items}`, where `date` is the **local** calendar day (`localDateKey`,
    never `toISOString().slice(0, 10)`, which files an evening paste under
    tomorrow east of UTC) and `items` maps item name → progress. Progress only:
    the tiers are fixed 10k/100k/1M thresholds (`TIER_THRESHOLDS`,
    `tiers.tierForProgress`), so they are derived, not stored.
  - **Retention** (`keysToPrune`, pure and unit-tested; run inside each save's
    own transaction, deletes issued synchronously from the index cursor's final
    `onsuccess` so the transaction cannot auto-commit in between): **one
    snapshot per day**, every day thinned to its **last** one -- the most recent
    state of that day, and the one whose gains cover the whole of it. There is
    no keep-everything window, so the day being saved is thinned like any other:
    a second save today supersedes this morning's. `keysToPrune(entries)` takes
    nothing else -- no "today", no window length.
  - **The button is the only unstaged control in the popover**: it writes the
    moment it is clicked and so survives Cancel (a snapshot is history, not a
    setting). It neither solves nor touches `farm-solver.v1`.
  - **`static/solverapp/timeline.js`** follows `masteries.js`'s shape (shares
    `settings.js`/`help.js`/`tiers.js`/`catalogue.js`/`history.js`, imports no
    `app.js`, duplicates the one-line helpers). With no `?item=` it renders the
    index of everything with history (`history.historyIndex`); with one it draws
    `chartSvg` plus `deltaRows` (per snapshot: progress, gain since the previous
    snapshot *that mentions the item*, gain/day -- suppressed under an hour's
    gap -- and tier). Picking an item `replaceState`s the query string rather
    than reloading. Its `#status` box is **exception-only** (unlike the solver
    and mastery-costs pages, which always report): `setStatus("")` hides it, and
    the ordinary chart/index renders do exactly that -- it appears only for an
    item no snapshot mentions, filters that match nothing, or a history store
    that will not read. **Both tables sort per column** the way the mastery-costs
    table does (`sortIndexRows`/`sortSeriesRows` over the `INDEX_SORTS`/
    The index's two **Change columns are windows measured back from now**, not
    "since your first ever snapshot": `indexRows(snapshots, now)` walks
    `history.seriesByItem` (one pass giving every item its own series) and calls
    `history.changeSince(points, cutoff)` per `CHANGE_WINDOWS` entry (7d, 1d).
    `changeSince` measures from the **last reading before the window opened** --
    the right baseline for the ordinary once-a-day snapshot, at the cost of
    over-covering when readings are sparser than the window -- and returns
    `null` (rendered "—", never a 0 that would read as "no progress") when the
    window cannot be answered: nothing recorded since it opened, or no earlier
    reading to measure from.
    `SERIES_SORTS` axis maps, one delegated listener per header row, with the
    arrow glyph and `aria-sort` set by `renderSortIndicators`), persisting under the shared
    `farm-solver.ui.v1` key (`timeline_index_sort`/`timeline_sort`, each with a
    `_dir`); a sort click redraws only its own table, never the chart. Two rules
    differ from that table: **Tier ranks on the progress it was derived from**,
    not the M/GM/MM label, so sorting by Tier reads exactly like sorting by the
    mastery and rows inside a tier stay in progress order; and a **missing
    figure is not a small one** (the oldest row's gain, a rate over too short a
    gap -- both "—"), so it stays at the bottom whichever way a column points.
    A snapshot that lacks the item is a **gap, not a zero**
    (`history.seriesFor` skips it): a paste lists what you have started, so a 0
    would draw a cliff that never happened.
  - **`chartSvg`** is the client-side counterpart of `report._svg_plot` and
    copies its conventions (self-contained inline SVG, `--mg-*` custom
    properties re-themed by one `prefers-color-scheme: dark` block). X is time-proportional
    from the timestamps, so two snapshots an hour apart sit together and a
    fortnight's gap reads as one. Y is one of **two** ranges — `fullRange`
    (0 → the next tier above the best progress) or `zoomedRange` (the data's own
    window) — and **`resolveScale(series, scale)`** picks between them, with
    `chartRange` returning whichever it names. `scale` is `"auto"` until the
    reader presses the chart's toggle, and auto keeps the full range unless the
    series moves less than `ZOOM_BELOW` (**half**) of it -- a lone snapshot
    always keeps it, having no shape to show. The tier scale has to earn the
    space it takes: a week of real progress is ~1% of a tier (from zero, a flat
    line), and even a 307k → 603k climb used a third of a 0 → 1M chart with the
    rest empty, which is why the bar is half and not the fifth it started at.
    A zoomed window is rounded out by
    **`niceRange`**, whose step is the span's **second significant place** (10k
    across a 121k window, 100 across a 3.4k one): the bounds get a 0 in their
    third figure while still hugging the data, and an **even** number of steps
    apart so the midpoint tick is round too — 212,401→333,631 draws 210k / 280k
    / 350k, not 194k / 273k / 352k. Taking the step from the span rather than
    the values is what keeps 995,000 → 1,005,000 stepping in 1,000 instead of
    blowing out to 990k → 1.1M. `axisLabel` spells figures out in full on a
    zoomed (<10k wide) axis, where every tick would otherwise round to the same
    "339k".
  - **Tier lines** (`visibleTiers`): dotted M/GM/MM references, drawn only where
    the threshold falls inside the plotted range, **minus Master whenever GM and
    MM are both drawn** — on a scale reaching Megamaster it sits at 1% of the
    height, crushed against the axis and saying nothing the GM line does not.
  - **The chart's controls** live in `.chart-controls`, a band above the plot —
    `.timeline-chart` is `position: relative` with a `padding-top` for it, and
    the scrolling moved to the inner `#chart-canvas` so the band stays pinned
    rather than scrolling with a wide chart. They are in the *template* rather
    than in `chartSvg`'s output (which stays pure SVG): `renderChart` replaces
    `#chart-canvas` wholesale on every redraw, so a control inside it would need
    re-binding each time. Both only ever redraw the chart — the delta table
    lists every snapshot whatever the chart shows — and both persist under the
    shared `farm-solver.ui.v1` key, so a choice outlives the item and the visit.
    - **Scale toggle** (`#chart-scale`, `timeline_scale`): **names the scale you
      are not looking at** ("Show next tier" / "Zoom to data", `aria-pressed` =
      zoomed) and flips `chartScale`.
    - **Time windows** (`#chart-range`, `timeline_window`): a button per
      `CHART_WINDOWS` entry (1d/7d/1m/1y/All — 30 and 365 days; a rough span to
      read the shape over, not a calendar boundary), generated by
      `renderWindowButtons` and filtered by **`windowSeries`**. Its cutoff
      reaches back **n + 1 days**: a window of n days wants n days of *growth*,
      which needs the reading the day before it, so "1d" is a day's climb rather
      than one lonely point and "7d" runs Monday to Monday. A window holding no
      snapshot is **disabled** (nothing to draw), and a *saved* window that has
      since emptied falls back to All (`windowed`) rather than drawing an empty
      axis. The X-axis midpoint label is dropped when it repeats an end, which a
      window this short otherwise does ("Aug 16, Aug 16").
  - **Point tooltips** are the one part of the chart that needs the DOM (the
    rest is markup that just sits there). Hovering a point raises a box naming
    the snapshot: its date and time, and the mastery it recorded -- **not** the
    tier, which the chart's own M/GM/MM lines and the table's Tier column
    already say. It replaced a native `<title>` per circle: that waits on the OS
    tooltip delay, cannot be themed, and hangs off a 4px target. The split keeps
    the pure/impure line the rest of the file draws -- `chartSvg` emits **one**
    hidden `g.tip` (last, so it is over every point rather than under the ones
    plotted after it) plus a `circle.pt-hit` per point, three times the dot's
    radius and carrying its own text; `tooltipLines`/`tooltipBox` decide what it
    says and where it goes (above and centred, flipped below a point too near
    the top, clamped inside the viewBox at either end -- sized off character
    counts, since nothing here can measure text: `chartSvg` is pure markup and
    jsdom has no `getBBox`); and `wireChartTooltip` fills and moves it from
    listeners delegated on the `<svg>`, which `renderChart` replaces wholesale,
    so a redraw has nothing to unbind. The lines cross into the markup as one
    pipe-joined `data-tip` attribute -- **not** newline-joined, which XML
    attribute normalisation turns into spaces (`innerHTML` in the page keeps
    them, `DOMParser` in the tests does not). Each point repeats the same text
    as an `aria-label`, which is what a screen reader gets in place of a hover.
  - **The index's filters** (`#filter-details`, shown with the index and never
    with a chart): tier checkboxes (**MM off by default** -- a Megamastered item
    is finished, so it is noise in a list read to decide what to do next), and
    **"made from"**, which lists only what a set of materials could end up as.
    `transitiveConsumers(uses, seeds)` breadth-firsts the inverted recipe graph,
    so Straw brings in Twine, then Rope, then Fishing Net -- the whole chain,
    not one step -- and several materials **union** (which is what makes a
    zone's entire drop list a sensible thing to add). `filterIndexRows` applies
    both. A zone picker fills the materials in from what an exploring location
    or fishing zone drops, keeping only drops the graph knows as a material
    (a chip for a chest item could never match anything); zones are identified
    by **index**, since one name can be both an exploring location and a fishing
    zone with different drops (Sinking Swamp). Tier boxes and the material list
    persist under the shared `farm-solver.ui.v1` key.
  - **`GET /api/catalogue`** (`views.api_catalogue` → `webapi.crafting_catalogue`,
    `lru_cache`d per process) feeds that filter: `{uses, names, zones}` where
    `uses` is the workshop graph **inverted** (material → what consumes it
    directly), lower-cased, plus display names and each zone's drops. The only
    GET API here -- static game content, no player state in either direction,
    nothing to key a cache on. The page treats it as a nicety: an empty or
    failed response leaves the tier filter and everything else working.
  - **`static/solverapp/catalogue.js`** holds `loadCatalogue` and the two
    catalogue URLs, factored out of `app.js` so the timeline's item picker and
    the solver's goal rows share one loader.
  - Entry points: a `Timeline →` link in each page's `.page-nav`, and a
    `.timeline-link` in every mastery-costs row's Item cell (that table's
    delegated row-click handler bails on `event.target.closest("a")`, so the
    anchor navigates instead of the row's plan link).
  - Tests: `history.test.js` and `timeline.test.js` (pure), plus
    `timeline.page.test.js` and the snapshot-button/backup cases in
    `settings.page.test.js`, all driven against **fake-indexeddb** -- jsdom
    ships no IndexedDB, and `openHistoryDb` resolves `null` when there is none
    (the same shrug `readUi()` gives unusable `localStorage`). Note fake
    timers deadlock fake-indexeddb's queue: page tests wait with `vi.waitFor`.
- **Backup / restore (`static/solverapp/backup.js`)** — because nothing is
  stored server-side, one JSON file is the *only* way to move a save to another
  browser (or to survive clearing site data). The pair of buttons in the
  settings popover's action bar (`.settings-actions`, at the far end from
  Cancel/Save; `#backup-status` is last in that row and `flex-basis: 100%`, so
  a long message takes its own line instead of squeezing the buttons, and
  collapses while empty) writes and reads `{format: "farm-solver-backup", version,
  exported_at, state, ui, snapshots}` -- the whole solver state, the
  `farm-solver.ui.v1` preferences, and every mastery snapshot.
  - `backup.js` owns the **format** and nothing else: `buildBackup`,
    `parseBackup`, `backupFilename`, `formatBytes`/`byteLength` (pure, tested in
    `backup.test.js`), plus the two browser shims `downloadJson` (an object URL,
    not a `data:` one -- a long history is megabytes) and `readFileText`
    (`FileReader`, since jsdom and older Safari have no `Blob.text()`).
    `settings.js` keeps the storage access, since it owns the keys.
  - **A restore is destructive, so `parseBackup` validates the whole file
    before a byte is written** -- format tag, a version it can read, the shape
    of each block, and every snapshot record (a timestamp that parses, numeric
    progress) -- and throws a message fit to show the user. A newer `version`
    is refused outright; a missing snapshot `date` is derived from the
    timestamp, since the local day of the exporting browser cannot be recovered.
  - **Back up takes the panel's live values** folded over the saved state's
    goal (`mergeSettingsIntoState`), so a paste made seconds ago is in the file
    even though Save has not been pressed. **Restore** confirms first, writes
    both `localStorage` keys, merges the snapshots via `history.putSnapshots`
    (a merge, not a replace -- an identical `taken_at` wins, then the usual
    prune runs), and **reloads**: the goal pane, the results and the timeline
    each read their own slice of the state at init, so re-reading it beats
    grafting a foreign state onto a live page.
  - **The CLI reads the same file** (`main.load_backup`, `--backup-file`) --
    see "Solving from a web-app backup" below. `backup.js` stays the owner of
    the format; `main.BACKUP_FORMAT`/`BACKUP_VERSION` mirror its constants and
    must be bumped with them.
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

## Solving from a web-app backup (`--backup-file`, main.py)

The web app stores nothing server-side, so its "Back up" button's JSON file is
the only way a browser-held save moves anywhere (see "Backup / restore" above).
`--backup-file` makes the CLI a second reader of that file, so a save does not
have to be kept twice.

A backup is `{format, version, exported_at, state, ui, snapshots}`.
`main.load_backup` reads **only `state`** into a `Backup` (`mastery_text`,
`inventory_text`, `params`) -- `ui` (pane/sort preferences) and `snapshots` (the
mastery history) are browser-only. It validates before returning anything and
names the file in every message, mirroring `backup.js`'s `parseBackup`: a JSON
file that happens to be lying in the Downloads folder must not be mistaken for
a backup, and a file from a *newer* `version` is refused because this build
cannot know what the new shape means. `BACKUP_FORMAT`/`BACKUP_VERSION` mirror
`backup.js`'s own constants and must be bumped with them.

**Two things come out of the file, at two different moments.** The `params` are
merged into `args` at the very top of `run()` -- before `load_config`, which
already reads `args.override` -- while the game state is read later, at each
mode's own load point via `load_game_state`. That split is why `run()` holds
the `Backup` in a local rather than converting it once.

**`params` *is* the body of `POST /api/solve`** (settings.js keeps the saved
state in exactly that shape), so `apply_backup_params` hands it to
`webapi.params_to_args` rather than reimplementing the mapping -- which means
`main` importing `webapi`, and `webapi` already imports `main`. The import is
**function-local**, the same cycle-breaking move `farm_solver.
load_tower_requirements` makes for `parsing`. The layering inversion is
deliberate and worth its cost: the alternative is a second copy of a ~150-line
mapping (plus `_CONFIG_PARAM_FLAGS`, `_ap_trading_override_lines`,
`_config_override_line`) that would silently drift from the web app's. It is
also self-limiting -- `params_to_args` never touches `--format`, `--pager`,
`--solver`, a file path or the `--empty-*` flags, which stay the CLI's alone.

**Precedence.** `params_to_args` builds a *fresh defaults* namespace and
applies the params to it; `apply_backup_params` then diffs that against the
same defaults, so it knows exactly which options the backup set, and writes
each onto `args` only where `args` is *still at its default*. So an option you
actually typed wins, and one you left alone takes the backup's value. Two
deliberate exceptions:

- **`--override` concatenates** rather than being replaced -- both sides carry
  overrides (the preference panels turn config knobs into them), so the
  backup's go first and the command line's last, which is what makes the
  command line win under `apply_overrides`' in-order application.
- **The goal is withheld from the goal-less modes.** `--mastery-costs`,
  `--ascend` and `--unobtainable` solve their own goals and *reject* a
  `--target`/`-m`/`-i`, so inheriting a backup's goal made
  `--backup-file save.json --mastery-costs` fail citing a flag the user never
  typed. `apply_backup_params(..., with_goal=False)` drops
  `_BACKUP_GOAL_PARAMS` (settings.js's own `GOAL_PARAM_KEYS`, in its
  vocabulary rather than argparse's, since that is the shape a backup stores)
  and keeps every solver knob.

`--backup-file` **conflicts with `--mastery-file`/`--inventory-file`** (half a
state from each is nobody's intent, so it is refused rather than silently
resolved one way), but composes with `--empty-mastery`/`--empty-inventory`,
which still drop their half. With a backup, `resolve_files` never runs, so no
`[solver]` file config is needed at all.

The near relative of this is the DEBUG-only "CLI command" popup
(`webapi.capture_debug_state` + `args_to_command`), which dumps the pasted text
to a timestamped file pair and reconstructs a `--mastery-file`/`--inventory-file`
command line; `--backup-file` is the offline, no-temporary-files version of the
same idea.

## Deployment (Dockerfile, deploy/)

`Dockerfile` + `deploy/` package the web app for Google Cloud Run; README's
"Container image and Cloud Run" is the user-facing version. What matters
architecturally:

- **The image is the whole deployment.** `settings.py` sets `DATABASES = {}`
  and holds no server-side player state, so there is nothing to mount or
  migrate. Two build stages on `python:3.14-slim`: `uv sync --frozen --no-dev
  --group web` against `uv.lock`, then the source. The project has no
  `[build-system]`, so it is a *virtual* project -- `uv sync` installs the
  dependency tree and never the project, which is why `farmweb/wsgi.py` still
  has to put the repo root on `sys.path` by hand.
- **The root layer is data, not just modules.** `COPY *.py *.csv *.txt *.toml`
  plus `COPY data/` exist because the solver resolves its game data at import
  time through `Path(__file__).with_name(...)` (`crops.csv`, `exploring.csv`,
  `fishing.csv`, `pets.csv`, `seed_drops.csv`, `openables.csv`, `tower.csv`,
  `unmodelled.csv`, `workshop.txt`, `data/mastery_items.txt`; `data/` is also
  where collectstatic finds the item catalogues) and `webapi.WEB_CONFIG_PATH` reads
  `web_default_config.toml` the same way. Dropping a data file from the copy
  breaks at import, not at request time.
- **Two build-time gates**, both failing the build rather than the first
  request: an `optimizer.set_solver_backend("highs")` import (the same
  requirement `solverapp.apps.SolverappConfig.ready` enforces before it will
  start -- and PuLP's CBC fallback in `optimizer._assert_optimal` has **no
  bundled binary on arm64**, so build x86_64), and `manage.py check`.
  `compileall` runs too: `main.py`/`optimizer.py`/`report.py` are ~435 KB of
  source and a scale-to-zero service would otherwise recompile them per cold
  start.
- **`DJANGO_DEBUG=false` is an image `ENV`, not a deploy-time variable.**
  `settings.py` defaults `DEBUG` to *true* for local use; with it on in
  production `views.index` renders the CLI-command popup and
  `webapi.capture_debug_state` writes every visitor's pasted game state to the
  container's temp dir. `DJANGO_ALLOWED_HOSTS` ships as `.run.app` (Django
  reads a leading dot as a subdomain wildcard) purely to break the
  chicken-and-egg on the first deploy; `deploy/cloudrun.sh` then narrows it to
  the service's real hostname.
- **`deploy/gunicorn.conf.py` deliberately diverges from README's
  two-worker recipe**, because a Cloud Run instance is sized in whole vCPUs
  and billed per vCPU-second: **one worker per vCPU** (`SOLVE_SEMAPHORE`
  already caps a *process* at one solve, so a second worker on 1 vCPU adds no
  throughput and just puts two frontier sweeps on one core), 4 threads so a
  cache-hit refilter never queues behind a re-solve, `max_requests = 0` so a
  recycled worker never discards its `SessionCache`, `preload_app = False` to
  avoid forking a process holding live HiGHS state (the same non-fork-safety
  behind `main.py`'s `ProcessPoolExecutor`), and `timeout = 120` to clear
  `views._MASTERY_SEMAPHORE_TIMEOUT_SECONDS` (60s) plus the sweep itself.
- **`deploy/cloudrun.sh` sizing is keyed to the semaphores, not throughput.**
  `--concurrency 4` is chosen against `views._SEMAPHORE_TIMEOUT_SECONDS` (a
  queued request has 20s to acquire the solve semaphore before a 503); Cloud
  Run scales out past that. `--max-instances` (default 3) is the only real
  bound on spend: `api_solve`/`api_mastery_costs` are `csrf_exempt` and
  unauthenticated, and each POST buys 1-3s of CPU, so a public URL is a
  cost-amplification surface. The script reuses the existing
  `DJANGO_SECRET_KEY` across deploys rather than rotating it, appends
  `EXTRA_ALLOWED_HOSTS` to the two computed run.app hostnames (the update
  *replaces* `DJANGO_ALLOWED_HOSTS`, so a mapped custom domain left out of it
  would be dropped by the next deploy), and asserts
  `deploy/artifact-cleanup-policy.json` on the `cloud-run-source-deploy`
  repository — keep the 3 most recent images, delete the rest past 30 days.
  Artifact Registry resolves Keep before Delete, so a rollback target always
  survives; the point is that image storage accrues outside a Cloud Run spend
  cap, which neither counts nor stops it.

## Dev workflow

- Run: `uv run pytest -q` (tests in `tests/`). Lint: `uvx ruff check .`.
- Try it: `python main.py` (default), `-i Item:N`, `-m Item:N`, `--plan K`,
  `--plans N`, `--cookie-sets N`.
- Web app: `uv sync --group web`, then `uv run --group web python
  web/manage.py runserver`; its own tests via `uv run --group web python
  web/manage.py test solverapp` (see "Web app (web/)" above). JS unit and
  page tests (goal-list parse/serialize, the mastery-costs sort, the settings
  popover's staging, the mastery-history store and the timeline chart): `cd web
  && npm install && npm test` (Vitest, jsdom + fake-indexeddb). Regenerate the goal pickers' item catalogues after mastery/
  inventory data or game-content data files change: `python
  scripts/generate_items.py` (writes `mastery_items.txt`/`items.txt` under
  `data/`).
- Snapshot a web-app backup into `mydata/`: `python scripts/import_backup.py
  <backup.json>` (`import_backup.import_backup`) writes the backup's pasted
  text as `mastery-<date>.txt`/`inventory-<date>.txt`, dated by its
  `exported_at` (`--date` overrides); validates via `main.load_backup`, refuses
  empty/unparseable pastes, and never overwrites without `--force`.
- Deploy the web app: `./deploy/cloudrun.sh` (see "Deployment (Dockerfile,
  deploy/)" above; README's "Container image and Cloud Run" has the prose).
- Commit directly to `main`; end commit messages with the Co-Authored-By trailer.
