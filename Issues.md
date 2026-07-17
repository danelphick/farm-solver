# ISSUES

1. ~~The option --ascend doesn't work properly when the masteries file does not contain all the
   available masteries. This is very likely when at tower level 0.~~ Fixed 2026-07-17: the sparse
   paste flooded the LP with hundreds of interchangeable progress-0 catalogue candidates, which
   (a) degenerated the round-and-fix loop into ~90s+ of churn even for a one-level climb (now a
   round budget hands the selection to one HiGHS MIP solve — sparse `--ascend 10` ~89s → ~13s,
   real-save runs unchanged) and (b) left incidental threshold crossings uncredited: AK the
   plan's own draws earn was missing from the ledger (later stages over-ground — the old
   cheapest sparse plan waited 15 days on chores where 12-hour plans exist) and the to-do
   list/frontier said "6 megas" for a 25-mega climb whose staged accounting was itself relying
   on the other 19 crossings (outcomes now credit any step the solved supply carries the item
   past, matching `ascension.advance`). See ARCHITECTURE.md's "Round-and-fix" / "Outcome
   readout" bullets.

2. Pumpkin Juice is not being selected properly unless the price is very near 0. (It should also
   allow a price of 0).

3. Performance is not great.
