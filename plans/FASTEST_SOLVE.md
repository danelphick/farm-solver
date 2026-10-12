# Fastest solve (branch `perf/fastest-solve`)

Goal: make the two web views -- the **solver page** (`/api/solve`:
`webapi.solve` + `webapi.render`) and the **mastery-costs page**
(`/api/mastery-costs`: `webapi.mastery_costs`) -- and their CLI twins
(`python main.py`, `python main.py --mastery-costs`) as fast as possible,
**without changing any result**.

## How it is measured

`uv run python scripts/bench_solve.py` against the newest `mydata/` pastes:

| case | what | config |
| --- | --- | --- |
| `solve` | `webapi.solve` + `webapi.render`, default params (the tower goal), warm process | `web_default_config.toml` |
| `mastery` | `webapi.mastery_costs`, warm process | `web_default_config.toml` |
| `cli` | `python main.py`, a fresh process (imports included) | `config.toml` |
| `cli-mc` | `python main.py --mastery-costs`, a fresh process | `config.toml` |

Each figure is the median of 5 runs (the in-process cases after a warm-up run)
on a 14-core Apple Silicon Mac, Python 3.14.2, highspy 1.15.1.

## How each step is verified

Every step must leave the output byte-identical to `main`'s, checked three
ways:

* a harness dumps **every LP `_build_base` builds** (every row in order, every
  coefficient, bound and variable) and the printed report, for twelve CLI
  scenarios -- the default tower goal, `-i`/`-m` goals (incl. Pumpkin Juice,
  `--self-sourced`, AP-trading and seed-limited crops, the Sugar Cane Mill),
  `--mastery-costs` (both ways), `--ascend` (incl. the fresh-account 0->200
  climb) and Veggie Juice off -- run on `main` and on the branch with the same
  `PYTHONHASHSEED`, then diffed;
* `uv run pytest -q`;
* `uv run --group web python web/manage.py test solverapp`.

A step that cannot keep the output identical says so, and why, below.

## Results

| step | solve | mastery | cli | cli-mc |
| --- | --- | --- | --- | --- |
| `main` (`0875ebf`) | 1.079s | 5.646s | 1.068s | 2.904s |
| 1. `FastHiGHS`: a fast PuLP -> HiGHS hand-over | 0.770s | 5.155s | 0.716s | 2.386s |
| 2. HiGHS without presolve before the CBC fallback | 0.684s | 3.019s | 0.703s | 2.365s |
| 3. Mastery costs' items on a thread pool | 0.684s | 0.951s | 0.716s | 1.025s |
| 4. The frontier's sweeps and exact finish on a thread pool | 0.486s | 0.943s | 0.497s | 1.032s |
| 5. Every exact-finish plan concurrently | 0.426s | 0.946s | 0.466s | 1.018s |
| 6. HiGHS's feasibility-jump heuristic off | 0.338s | 0.901s | 0.448s | 1.007s |
| 7. Surplus leftovers in one pass over the touched items | 0.306s | 0.886s | 0.414s | 0.999s |
| 8. Reuse what builds share; re-tune the thread counts | 0.283s | 0.526s | 0.387s | 0.632s |
| 9. Thread counts capped at the CPUs available | 0.275s | 0.521s | 0.374s | 0.625s |

`main` already includes the coefficient-dict row building (`afe461d`), which
took `--mastery-costs` from 3.85s to 2.81s on its own.

### 1. `FastHiGHS` -- output identical

Profiling the solver page after the row-building fix: of ~1.6s (profiled), PuLP's
HiGHS backend spent ~0.9s *handing the model over* -- `LpProblem.variables()`
(called three times per solve, each walking every constraint's terms and
re-sorting the variables), then one `addCol`/`addRow` call per column and row,
then reading every value, dual and slack back -- against ~0.4s in HiGHS itself.
The same `_Base` is re-solved dozens of times with only the objective, a few
pins and a time cap changing, yet every solve rebuilt it from nothing.

`highs_backend.FastHiGHS` (the "highs" backend) gives HiGHS the *identical*
model -- columns in PuLP's order (sorted by name), rows in constraint order with
each row's nonzero entries in order, the same bounds, costs, integrality and
options, a fresh `Highs` per solve -- but caches the converted rows on the
problem and re-converts only the constraints added since the last solve (the
shared prefix is reused; a base's ephemeral pins come and go at the end), then
passes the model in one `passModel`, and reads back only the primal values (no
caller reads duals or slacks). Verified identical on every scenario and on both
web views.

### 2. HiGHS without presolve before the CBC fallback -- web output differs at solver tolerance

`_assert_optimal` re-solved any non-optimal first solve on CBC (a subprocess,
~20ms a solve) before believing the demand unsatisfiable, and the CBC solver
then carried the rest of that call's refinement. On the web config that was not
rare: **62 of the mastery-costs page's 192 items** (37 "Infeasible", 24
"Unbounded", 1 "UnboundedOrInfeasible" from HiGHS -- all feasible, CBC solved
every one) and 3 of the solver page's solves -- ~45% of the mastery-costs page's
time. Every one of those models solves to optimality in HiGHS with presolve off
-- presolve mis-flags them; the costs reach 6e-11 against a 1e-7..1e5 matrix --
once a MIP (the models carry one integer, `days_available`) also stops presolving
at its search nodes (`mip_root_presolve_only`; without it 12 of the 62 still
failed). So `_assert_optimal` now tries `FastHiGHS(presolve="off",
mip_root_presolve_only=True)` first and keeps CBC as the last resort; neither web
view calls CBC any more.

The CLI scenarios never hit the fallback and are unchanged. The web views now
take those items' optima from HiGHS instead of CBC -- equally optimal, but where
the optimum is not unique the reported plan can differ at solver-tolerance level:

* mastery-costs page: every time identical, and the "cannot" lists; 61 of 192
  gold figures differ, all by under 0.0001% except Sunflower Oil's Megamaster
  (665,219.36 -> 665,249.62 gold, +0.005%: the stage-2 tie-break minimises total
  value, not gold, so equally-valued plans can spend different gold);
* solver page: one frontier plan's time moves by 0.002 seconds and its gold by
  0.0002 (both print the same); the selected plan's Small Spring explores
  yield 1 Wood fewer, Gary's Crushroom 3 Mushroom Paste fewer, the Surplus
  section 5 Hammer fewer, and the passive-production rows tied on quantity and
  time now list alphabetically instead of in the CBC solution's order.

### 3. Mastery costs' items on a thread pool -- output identical

The mastery-costs sweep is 192 independent `fastest_endpoint` solves, and
highspy releases the GIL inside `Highs.run` (8 threads ran a batch of HiGHS
solves 5.8x faster than one). `solve_mastery_costs` now walks the candidates
first (status, notes, which tiers to solve -- no solving), then runs each item's
tier climb on a `ThreadPoolExecutor` and gathers the results back in candidate
order, so the table is identical at any worker count (checked at 1, 2, 4, 6, 8,
10 and 14). Each item builds its own LP; the threads share only read-only
inputs.

The CLI sweep by worker count (`--jobs`): 1 -> 2.34s, 2 -> 1.45s, 4 -> 1.07s, 6
and up -> 1.01s. Past ~6 the Python model building between solves -- which
still holds the GIL -- is the limit, so the default is 8 (`MASTERY_COST_WORKERS`;
re-tuned to 6 in step 8).

### 4. The frontier's sweeps and exact finish on a thread pool -- output identical

After step 2, three quarters of a solver-page request was HiGHS itself: ~220
solves (192 LPs at ~1.4ms, 27 MIPs at ~9ms). Most of them are independent: the
epsilon sweep's ~45 capped solves, the slow regime's ~45, and the exact
(integer) re-solves of the plans that survive thinning.

`frontier` now runs them on a `ThreadPoolExecutor` (`FRONTIER_WORKERS` = 4; 5
after step 8):

* `_sweep` splits its caps into contiguous runs solved concurrently -- the first
  on the given base, each other on its own copy (`make_base`: `_build_base` from
  the same inputs; a base is one mutable LP, so threads cannot share one) -- and
  returns a collector instead of a list, so the epsilon sweep runs while the
  main thread builds the slow regime's base and solves its probe. `post`
  (quantize + surplus credit) runs on each plan on its sweep thread.
* the exact finish re-solves each thinned plan on the base it came from; plans
  sharing a base take turns, plans on different bases run concurrently.

A capped solve is a cold solve of the same model whichever base copy runs it
(same rows, same order, same columns), so the plans -- and their order, which
the dedup's first-wins relies on -- are unchanged; `post` is per plan. By worker
count (solver page / CLI): 1 -> 0.684s / 0.705s, 2 -> 0.533s / 0.556s, 3 ->
0.520s / 0.526s, 4 -> 0.474s / 0.489s, 6 -> 0.482s / 0.491s, 8 -> 0.497s /
0.500s; past 4 each extra base copy's build (Python, so GIL-bound) costs more
than the extra overlap saves.

### 5. Every exact-finish plan concurrently -- output identical

A timeline of the threads showed the exact finish's critical path was the
plans sharing a base (the fast and cheap endpoints' plans both live on
`base_active`), re-solved one after another as integer MIPs (~25-60ms each).
Now every plan is its own task: the first plan from each base re-solves on
that base, any other on a fresh copy -- the same cold solve of the same model.

### 6. HiGHS's feasibility-jump heuristic off -- output identical, except two wrong rows put right

The integer (MIP) solves -- the fixed point's fastest endpoints, the exact
finish, the reference endpoint -- were now the critical path: 27 MIPs a solver
page at ~9ms against ~1.4ms for an LP, though each has a single integer
(`days_available`; a few Pumpkin Juices when on). Half of each MIP's time was
HiGHS's feasibility-jump heuristic hunting for a first incumbent, which the root
LP all but hands over on these models: with it off the MIPs took 0.128s instead
of 0.274s and returned bit-identical solutions on all 27.

Across the CLI scenarios and the solver page the output is identical. On the
mastery-costs page two rows change -- and both were wrong before:

| row | before | after |
| --- | --- | --- |
| Runestone 10 -> Grandmaster | 51d 2h, 0 gold | 17d 9h, 2,536 gold |
| Runestone 16 -> Grandmaster | 82d 23h, 0 gold | 27d 22h, 1,522 gold |

With the heuristic on, HiGHS's presolve returned these two "fastest" plans as
*optimal* at ~3x the true optimum (objective 121,976 vs 40,259 minutes for
Runestone 16) -- the same numerical fragility as step 2, silently this time.
With it off, presolve calls those models infeasible instead, so step 2's
fallback re-solves them without presolve and finds the real optimum. A check of
all 192 first solves of the sweep, presolve on vs off, finds no other model
where both claim optimal but disagree by more than a minute.

### 7. Surplus leftovers in one pass over the touched items -- output identical

With the solves overlapping, the Python between them -- which takes turns on
the GIL -- set the sweeps' pace, and the largest piece was `post`'s surplus
credit on every frontier plan. `accumulated_leftovers` called `plan_balance`
twice and walked the whole inventory (thousands of items on a real save) three
more times, to subtract from each leftover the held share that cancels it
exactly for every item the plan never touches. It now computes the balance
once and walks only the touched items (needed, made, opened, gathered or
passively produced); the arithmetic per item is unchanged -- the result was
checked equal, float for float, on all 148 frontier plans of three web solves.

### 8. Reuse what builds share; re-tune the thread counts -- output identical

The threaded mastery-costs sweep was now paced by its GIL-bound model building:
`_build_base` was over half of a sweep's Python. Two of its parts depend only on
inputs every build in a sweep shares (the offers, recipes and openables -- and
the frontier's base copies share all of them):

* **`_obtainable`** and what derives from it (craftable recipes, usable offers
  and openables, the reachability indexes), recomputed per build -- now cached
  per `(offers, recipes, openables, buyable, held)`;
* **the offer scan**: every build walked every usable offer's whole bundle (on a
  real save, ~12,000 item-offer pairs) to find the offers yielding a reachable
  item and to index their terms -- now a per-item index of producing/consuming
  offers is built once per offers list, and a build reads just its reachable
  items' entries.

`_cached` keys an entry by the inputs' identity and keeps the inputs alive in
it (so an `id` cannot be reused while cached), under a lock since builds run on
solver threads. Variables are now created with `problem.add_variable`, PuLP 4.0's
form, which skips a deprecation warning per variable.

The models are unchanged -- every row of every build dumps identical -- and so
is every output. With building cheaper, the thread counts were re-measured: the
mastery sweep is flat from 6 to 8 threads (0.524s / 0.530s) and slower past it,
the frontier best at 5 (0.285s against 0.301s at 4), so the defaults are now
`MASTERY_COST_WORKERS = 6` and `FRONTIER_WORKERS = 5`.

### 9. Thread counts capped at the CPUs available -- output identical

The deployment (`deploy/cloudrun.sh`) runs on **1 vCPU** by default, where the
solver threads of steps 3-5 can only take turns -- and the frontier's per-thread
base copies would be wasted builds. `optimizer.solver_threads(preferred)` now
caps every thread count at `available_cpus()` -- the process's affinity, then a
Linux cgroup v2 CPU quota (a container's `--cpu`) -- with `FARM_SOLVER_THREADS`
as an explicit override; at 1 the frontier skips its pool and copies entirely
(the original sequential path) and the mastery sweep runs in turn. Outputs are
identical either way (checked with `FARM_SOLVER_THREADS=1` and at the default).
`scripts/bench_solve.py --threads 1` measures the single-CPU case.

## On one CPU

What a 1-vCPU instance (the deployment's default) gains -- `bench_solve.py
--threads 1`. `main` never used threads, so its figures are the same:

| | solve | mastery | cli | cli-mc |
| --- | --- | --- | --- | --- |
| `main` (`0875ebf`) | 1.079s | 5.646s | 1.068s | 2.904s |
| branch, 1 thread | 0.508s | 1.247s | 0.614s | 1.331s |
| speed-up | 2.1x | 4.5x | 1.7x | 2.2x |

Everything except steps 3-5 (the threads) counts here: the PuLP hand-over, the
CBC fallback gone, the MIP heuristic, the leftovers pass and the build reuse.
With more vCPUs each request also uses the threads -- the main table's figures
are this 14-core machine at the defaults (5 frontier threads, 6 mastery).
