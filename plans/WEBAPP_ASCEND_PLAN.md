# Webapp ascension plan

Making `--ascend` viable as part of the web app. Prompted by the fresh-account
worst case
`python main.py --tower-level 0 --ascend 200 --empty-mastery --empty-inventory --override farming.speed_reduction=80`
taking ~72s. Per the owner this is a **realistic** worst case, not a rare one.

## Diagnosis (profiled 2026-07-17, M-series Mac, Python 3.14, PuLP + HiGHS)

- Imports are negligible (~53ms). The 72s is genuine solve time.
- **Trial-solve bound, not MIP-bound.** `--ascend` *rejects* `--use-pumpkin-juice`
  (`_validate_ascend`), and juice is the model's only integer variable — so there
  is **no MIP anywhere** in a climb. The ascension step vars
  (`master`/`grandmaster`/`mega`) are continuous `[0,1]`; integrality comes from
  `optimizer._round_and_fix_ascension`'s two-sided re-solve loop. `--empty-mastery`
  makes the whole ~430-item catalogue fresh candidates and the fastest plan selects
  ~360 of them → hundreds of fractional steps per stage → ~10k warm HiGHS
  re-solves at ~2.4ms each.
- Structure: 16 gold-weight passes × 10 reward stages = 160 stage solves
  (`solve_ascension_frontier`'s sweep → `_run_ascend_pass` → per-stage loop).
- Algorithmic levers (true MIP, candidate pre-pruning, MIP finisher,
  cached-selection re-adoption) are documented dead-ends — see ARCHITECTURE.md's
  "The sweep" bullet. The honest algorithmic lever is `--sweep`; the real
  wall-time win is parallelism.
- **The web app doesn't expose ascend at all yet** — `ascend`/`sweep`/
  `tower_level`/`starting_ak`/`empty_mastery` are absent from `webapi.py`
  (`RESOLVE_FIELDS`, `_KNOWN_PARAM_KEYS`), and `web/solverapp/views.py::api_solve`
  solves synchronously under `SOLVE_SEMAPHORE = BoundedSemaphore(1)` (waiters bail
  at 20s). That model assumes the 0.5–3s normal solve; a 72s solve blows past HTTP
  timeouts and starves every other user. So "make ascend viable in the webapp" is
  partly *building the web ascend feature*, designed around a 10–90s solve.

---

## DONE — A2: parallel gold-weight sweep

Committable unit, implemented and verified (`main.py`, `tests/test_ascension_frontier.py`,
`ARCHITECTURE.md`). The independent gold-weight passes fan out across a
`ProcessPoolExecutor` (processes, not threads — HiGHS state isn't thread-safe).
Invariant per-stage setup ships to each worker once via the pool `initializer`;
each task carries only its `gold_weight`. New `--jobs N` flag (`--jobs 1` = old
sequential path) + `FARM_SOLVER_ASCEND_JOBS` env var. `_strip_ascend_origins`
clears the unpicklable `_origin` (live HiGHS model) in the worker before results
cross back. Correctness rests on base_cache-independence (ephemeral reuse is
exact), guarded by `test_parallel_and_sequential_sweeps_produce_the_same_frontier`.

**Measured, byte-identical output:** `0→200 --empty-mastery` 72s → ~15s (~4.9×);
`40→60 --empty-mastery` 69s → ~8s (~8×). The 0→200 factor is lower because the
buy-freely fast-endpoint pass dominates and its stages are inherently sequential
(each depends on the previous stage's evolved inventory/masteries/AK carry) — that
single pass is the floor.

---

## TODO — remaining items, in order of value

Each is separately committable and verifiable.

### A1. `sweep` (and ascend) params in webapi — small, backend-only
Wire `ascend`/`sweep`/`tower_level`/`starting_ak`/`empty_mastery`/`empty_inventory`
into `webapi.py`: `RESOLVE_FIELDS`, `_KNOWN_PARAM_KEYS`, `params_to_args`. Lets the
interactive path solve at a low sweep (2–4) for fast first paint (`--sweep 2` ≈ the
two endpoints, exact fastest+cheapest), then optionally kick a background job for
the full-density 16-point frontier and push it as an update (interior plans are
purely additional frontier rows; fastest/cheapest never move). Combines with A2:
sparse first paint, then a parallel full-density refine.
Verify: a webapi request with `params={"ascend": 200, "sweep": 2, ...}` returns a
frontier; `test_webapi.py` coverage for the new params.

### B4. Degenerate-input guard in `_validate_ascend` — small
A paste full of already-grandmastered items but stated level 0 / starting-AK 0
makes AK *harder* to earn (easy masteries gone) → even slower, chore-bound plans.
Warn (in `_validate_ascend`, `main.py`) when mastery progress is inconsistent with
the stated tower level / starting AK — the user's starting AK should be > 0.
Correctness/UX guard, not a perf fix; prevents runs slower than the intended worst
case. Verify: a unit test with a high-mastery paste + `--tower-level 0` emits the
warning.

### B1 + B2. Async job + non-blocking worker pool — LARGE (Django + frontend)
The actual "webapp viability" crux, and greenfield (ascend isn't in the web UI).
- **B1 async job with progress:** a 72s (or 9s) solve can't be a synchronous
  request holding the global semaphore. `POST` submits a job (key via existing
  `webapi.fingerprint`), returns a job id; client polls or SSE for status/result.
  Progress is natural — the sweep is the meter (pass k of N); with A2's parallel
  sweep, report completed-pass count. Endpoints finish first → push a usable
  frontier early (dovetails with A1's progressive densify).
- **B2 remove single-solve starvation:** `SOLVE_SEMAPHORE = BoundedSemaphore(1)`
  + 20s waiter means one slow solve 503s everyone. Move solving off request
  threads into a worker-process pool / job queue so a 72s ascend job doesn't block
  a 2s normal solve. Keep one HiGHS solve per *process* (that constraint is real —
  `web/solverapp/cache.py:54-58`), but run several solve workers. Aligns with A2
  (each parallel pass already wants its own process); set `FARM_SOLVER_ASCEND_JOBS`
  so nested pools stay controlled.
- **Frontend:** ascend controls + progressive frontier rendering. `SectionedReporter`
  already emits the ascension sections (ARCHITECTURE.md "Ascension report sections"),
  so the render side is largely wired — the missing pieces are the input controls,
  the job/poll flow, and progress UI.
Files: `webapi.py`, `web/solverapp/views.py`, `web/solverapp/cache.py`, templates/
static/JS. Verify: drive the endpoint with an ascend request + a concurrent normal
solve; confirm the normal solve is NOT 503'd/blocked while ascend runs, and that
progress updates arrive.

### B3. Shared / precomputed cache for canonical inputs — medium
`SessionCache` is per-worker in-memory (`web/solverapp/cache.py`); a request on
another worker or after eviction re-solves. The fresh-account 0→200 (and a small
set of canonical starting points) is a *fixed* input → precompute at deploy/warm-up
and serve from a cross-worker persistent cache (disk/Redis) so the worst case is a
cache hit for most users. Per-user pastes still vary, so this only covers
canonical/empty inputs and repeat queries — but the fresh-account case is exactly a
canonical one.

---

## Testing notes (carried from memory + this session)

- Benchmark on the fresh-account `0→200 --empty-mastery` command, not the real
  save (the real save is much faster and skips most of the mega path).
- Mind CBC/HiGHS non-determinism for degenerate plans — compare same-timeframe.
- `--jobs 1` forces the in-process path (needed for any test that monkeypatches
  `_run_ascend_pass` itself — worker processes re-import `main` unpatched;
  monkeypatches of the *setup* half do reach workers, since setup is built in the
  parent and pickled into the payload).
