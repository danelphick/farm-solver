# ISSUES

1. ~~The option --ascend doesn't work properly when the masteries file does not contain all the
   available masteries. This is very likely when at tower level 0.~~ Fixed 2026-07-17 ("Serve the
   fresh-account climb"): the report hid almost every megamastery a fresh climb earns (the megas
   readout now reports threshold crossings, mirroring `ascension.advance` — a sparse 100→200
   climb shows its full mega count instead of the handful the LP happened to pin), and the
   wall-time of the low-mastery candidate flood is governed with `--sweep N` (fresh 0→200: ~92s
   at the default 16 weights, ~9s at `--sweep 2` with exact fastest/cheapest plans). Two
   MIP-based rounding replacements and a cross-weight selection cache were also built against
   this scenario and rejected on measurement — junk selections or slower than the loop; the LP
   relaxation bound is too weak in the gapped regimes to gate either — see the sweep bullet in
   ARCHITECTURE.md's "Ascension orchestration".

2. ~~Pumpkin Juice is not being selected properly unless the price is very near 0. (It should also
   allow a price of 0).~~ Fixed 2026-07-17: two causes. `_build_base` gated the `pj` vars on
   `pj_price > 0`, so an explicit price of 0 disabled juice outright instead of making it free
   (`pj_gain` already carries the on/off decision) — the falsy-zero trap d78542e fixed for
   `[trading]` prices. And the refinement tower's stage 2 minimised **bought gold**, which juice
   alone pays (it has no self-sourced form), so it read as "fewest juices" and burned tens of
   thousands of held AP/LN/cider — thousands of gold at market — to dodge juice worth a fraction
   of a gold. That choice also sat under the solver's resolution (a ~1e5 `pj_gain` against a
   ~1e-3 cost), so whether it shed juice at all depended on HiGHS's presolve: the count fell from
   13 to 5 when the price rose from just 0.0005 to 0.001. Stage 2 now minimises `total_value` (everything
   spent at market price, however sourced), with the Pareto gold axis pinned separately.

3. Performance is not great.
