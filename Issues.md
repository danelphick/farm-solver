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

2. Pumpkin Juice is not being selected properly unless the price is very near 0. (It should also
   allow a price of 0).

3. Performance is not great.
