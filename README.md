# Farm Solver

This is an attempt to find the optimal way to spend resources to climb the tower in FarmRPG.

Farm Solver loads in the current state of your mastery and inventory and calculates how many Arnold Palmers and Large Nets are required to get the mastery levels required to climb the tower.

## Usage

Run the script passing the masteries and inventory copied and pasted from with FarmRPG directly into a text file.

``` shell
python main.py [-t <tower-requirements>] [-l level] [-i item:amount] [-m item amount] [-r] [-b] [--crafting-bonus num] [--mastery-bonus num] [--passive-limit duration] <masteries> <inventory>
```

By default it will assume you want to reach the highest possible level, but you can override this with `-l`. Different tower requirements can be passed in with `-t` to be used rather than the builtin ones.

The solver can also ignore the tower and solve for mastering a particular item using `-m`. E.g.

```
python main.py -m "Flying Machine:100000" sample-mastery.txt sample-inventory.txt
```

You can pass multiple items by comma-separating the requirements. E.g.

```
python main.py -m "Flying Machine:100000,Gainite:100000" sample-mastery.txt sample-inventory.txt
```

Rather than masterying an item, it can solve for acquiring a certain number of an item using `-i` (including what's already in your current inventory).

```
python main.py -i "Flying Machine:2000,Gainite:20" sample-mastery.txt sample-inventory.txt
```

## Materials and crafting

After listing what you still need, the solver works out the workshop materials required to craft those items and prints them as a total.

Two bonuses affect how many crafts (and therefore materials) are needed:

- `--crafting-bonus <num>` (default `1.45`): the average number of items produced per craft. A higher bonus means fewer crafts, and so fewer materials, are required.
- `--mastery-bonus <num>` (default `1.1`): the mastery gained per item crafted or otherwise found. This only reduces the crafts needed to satisfy a *mastery* shortfall (the tower or `-m`); it does not apply to a plain item count (`-i`), nor to the quantity of an item consumed as a material by another craft.

A few materials (Iron and Nails) are supplied automatically by the game whenever a craft needs them, so they are never counted, gathered, or expanded — they simply do not appear in the materials, breakdown, or passive output.

Some materials are grown on the farm (crops like Corn and Cotton, the flowers, Pine Tree). These are split out of the totals and reported in their own `Farming required:` section, much like passive production:

```
Farming required:
  58,694x Corn
  121,051x Cotton
  161,401x Pine Tree
```

By default, materials that are themselves craftable (for example a chair made from wood) are reported as-is. Pass `-r`/`--recursive` to expand them into their own materials, all the way down to the base, uncraftable ones:

```
python main.py -r sample-mastery.txt sample-inventory.txt
```

When expanding recursively, counts are kept as fractions through the whole crafting tree and only the final totals are rounded, so rounding errors do not accumulate level by level.

Pass `-b`/`--breakdown` to list every craft that needs to happen, each with the materials it consumes. Every craftable item gets its own entry (quantities aggregated across everywhere it is used), while base materials appear only inline. Each craft also notes how many of the item those crafts produce and how much mastery that adds:

```
Required Crafts:

5x Hammer (Produces 5, Adds 5 Mastery)
  5x Board
  5x Iron
  5x Mushroom Paste

5x Mushroom Paste (Produces 5, Adds 5 Mastery)
  15x Mushroom

5x Board (Produces 5, Adds 5 Mastery)
  25x Wood
```

## Passive production

Some buildings generate items on a fixed cadence (every ten minutes or hourly) without crafting. These are described in `config.toml`:

```toml
[sawmill]      # wood/board/oak hourly; pine board one a minute
wood = 46000
board = 60000
oak = 6000
have_sturdy_saw = true    # oak is only produced with the sturdy saw
have_pine_boost = true    # two pine board a minute instead of one

[chicken_coop]              # eggs and feathers daily
produces = 18271
completed_starmap = true    # also gold feather at 5% of the feathers

[worm_habitat]    # hourly
worms = 10000
gummy_worms = 4000
mealworms = 4000

[trout_bait_farm]    # grubs and minnows hourly, trout daily
trout = 25000
grubs = 7000
minnows = 6000

[orchard]    # apples, oranges and lemons daily, each scaled by bonus
apple_trees = 6162
orange_trees = 6175
lemon_trees = 6165
bonus = 1.3
have_tree_shaker = true    # separate 10% deposit of each fruit, like the snare

[raptor_pen]    # antlers daily, plus a separate 10% deposit with the snare
antlers = 2617810
have_antler_snare = true

[vineyard]    # grapes daily
grapes = 43000

[flour_mill]    # one flour a minute, two with flour power
have_flour_power = true

[feed_mill]    # one feed a minute, two with the feed boost
have_feed_boost = true

[sugar_cane_mill]    # one sugar cane a minute, 2 with boost I, 4 with boost II
have_sugar_boost_i = true
have_sugar_boost_ii = true

[hay_field]    # straw every ten minutes
hay = 30000

[quarry]       # stone every ten minutes, coal hourly
stone = 8000
coal = 8000

[steelworks]   # steel hourly, plus steel wire at a third of that (rounded down)
steel = 10296
```

When a craft needs an item that is produced passively, the solver **prefers passive sourcing** over crafting it: it takes the item from production instead, and reports it in a separate section with how long it would take. Since production arrives in whole cycles, each wait is rounded up to a whole number of the producer's periods (so a ten-minute producer never reports a fraction of ten minutes). Because producers run concurrently, the total time is the longest single wait, not the sum:

```
Passive Production required:
  18,000x Oak in 3h
  180,000x Board in 3h
  138,000x Wood in 3h
  Total time: 3h (concurrent)
```

Passive production is also shaped by how much you can hold at once. The `[storehouse]` section records its capacity on a given day and how much it grows daily:

```toml
[storehouse]
inventory = 49440
updated_on = "2026-06-30"
growth = 20
```

The solver works out the size today (`inventory + growth × days since updated_on`). Each cycle the output is deposited into the storehouse, which holds at most that size and voids the excess; you then use it before the next cycle. So the effective per-cycle output is `min(production, storehouse size)` — the storehouse caps the per-cycle amount, not the total obtainable over many cycles. For the hourly sawmill that makes the daily limit `24 × min(production, storehouse size)` per item.

By default passively-produced items are otherwise taken entirely from production, however long that takes. Pass `--passive-limit <duration>` to cap how long you are willing to wait; anything production cannot supply within that time is crafted instead. The duration is written as `5d`, `3h`, `90m`, or a combination like `1d6h30m`:

```
python main.py --passive-limit 3h sample-mastery.txt sample-inventory.txt
```
