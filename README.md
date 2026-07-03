# Farm Solver

This is an attempt to find the optimal way to spend resources to climb the tower in FarmRPG.

Farm Solver loads in the current state of your mastery and inventory, works out what each tower level still needs, expands the crafting, and then finds the best ways to gather everything — trading total **time** against the **Arnold Palmers** and **Large Nets** you spend. It reports a range of plans along that tradeoff, from "spend nothing and wait" to "spend freely and finish fast".

## Usage

Copy and paste your masteries and inventory from FarmRPG into text files. Point the solver at them either with `--mastery-file` / `--inventory-file`, or by setting them once in the `[solver]` section of `config.toml`:

```toml
[solver]
mastery_file = "./sample-mastery.txt"
inventory_file = "./sample-inventory.txt"
```

A command-line flag overrides the config; if a file is given in neither place, the solver reports an error. The examples below rely on the `[solver]` config, but you can add `--mastery-file <file> --inventory-file <file>` to any of them.

```shell
python main.py [-t <tower-requirements>] [-l level] [-i item:amount] [-m item:amount] \
    [-b] [--crafting-bonus num] [--mastery-bonus num] \
    [--fastest-plan | --cheapest-plan | --plan N] [--passive-limit duration] [--unobtainable] \
    [--mastery-file <file>] [--inventory-file <file>]
```

By default it will assume you want to reach the highest possible level, but you can override this with `-l`. Different tower requirements can be passed in with `-t` to be used rather than the builtin ones.

The solver can also ignore the tower and solve for mastering a particular item using `-m`. E.g.

```shell
python main.py -m "Flying Machine:100000"
```

You can pass multiple items by comma-separating the requirements. E.g.

```shell
python main.py -m "Flying Machine:100000,Gainite:100000"
```

Rather than masterying an item, it can solve for acquiring a certain number of an item using `-i` (including what's already in your current inventory).

```shell
python main.py -i "Flying Machine:2000,Gainite:20"
```

## Sourcing plans

Every item you still need can be obtained several ways, and the ways differ in what they cost:

- **Passive production** — buildings (and pets) generate items over time for free. They all run concurrently, so their cost is *time*, and the wait is the slowest single item.
- **Fishing** — each **Large Net** instantly catches a bundle of fish from a zone. It costs a net, not time.
- **Exploring** — each **Arnold Palmer** instantly yields a bundle of finds from a location. It costs a palmer, not time.
- **Farming** — crops grow over time (see [Farming](#farming)); grown one at a time, so their times add up, but they run concurrently with the passive buildings.

Spending a net or a palmer buys speed: it lifts an item out of the slow farming/passive queues for zero time, at the cost of one consumable. So there is a genuine tradeoff between time, Arnold Palmers, and Large Nets. Farm Solver models the whole choice as a linear program and reports the **Pareto frontier** — a handful of plans, none strictly worse than another, spanning "all free but slow" to "fast but costly":

```text
Sourcing plans (time / Arnold Palmers / Large Nets):
 > 1. 87y 62d / 71,136 AP / 0 LN  [fastest]
   2. 293y 319d / 3,931 AP / 0 LN  [cheapest]
```

One action often produces many different items at once (a net catches every fish in its zone, a harvest drops its crop plus rarer items), and the plan credits that whole bundle against everything you need — so a single fishing trip can cover several requirements together.

By default the **fastest** plan is expanded in full; the `>` marks which plan is shown. Pass `--plan N` to expand a different one by its number, or `--cheapest-plan` to expand the one spending the fewest Arnold Palmers and Large Nets (`--fastest-plan` is the explicit form of the default):

```shell
python main.py --cheapest-plan        # expand the cheapest plan instead
```

The chosen plan opens with its **critical path** — the track, and the item or crop within it, that caps how fast the plan can finish. Fishing and exploring are instant, so the limit is a passive wait or a farming sequence. The two are not symmetric: passive production runs for free alongside everything, so it opportunistically fills whatever wait farming already forces (making an item passively to save a consumable rather than fishing or exploring it), which pads the passive track out to the makespan without being the real constraint. So when farming sets the pace it is named alone; only when passive is the sole longest track is a passive item the critical path. Either way it is what you would have to speed up — by sourcing it with consumables, or picking a faster plan — to finish sooner:

```text
Critical path (sets the 293y 319d total):
  2,574,345x Horn from passive production (293y 319d) — the slowest item to build up
```

The plan is then broken down by channel, each ordered so the heaviest lines lead: fishing and exploring by the Large Nets and Arnold Palmers they spend, farming by how long each crop takes, and the passive section by each item's own wait — all longest/largest-first, so the runner-up bottlenecks are visible too. Each passive line also names the producer that makes the item; when several producers make it, the quantity is split between them by their per-minute rate share, largest first. A seasonal pet only fetches for part of the year, so its output is modelled as a long-run yearly average — which is why it contributes less than a year-round source — and its active window is flagged (`[seasonal: Mar–Apr]`) to explain the smaller share:

```text
Exploring:
  3,931x Arnold Palmer at Jundland Desert (Small Bolt)

Passive production (concurrent):
  2,574,345x Horn in 293y 319d (from pet Dog)
  2,858,281x Purple Flower in 279y 246d (2,449,955 from pet Squirrel, 408,326 from pet Hummingbird [seasonal: Mar–Apr])
  ...
```

Anything no channel can supply is listed as a remainder to craft or provide yourself. Pass `--plans N` to spread more (or fewer) plans across the frontier when you want finer resolution — useful where the fast end swings wildly in cost (default 5).

Three ceilings drop plans that exceed them (unless that would leave none): `--passive-limit <duration>` on total time, and `--max-ap` / `--max-ln` on the Arnold Palmers and Large Nets spent. The duration is written as `2y`, `5d`, `3h`, `90m`, or a combination like `1y6d`, `1d6h30m`:

```shell
python main.py --passive-limit 100y --max-ln 0    # no fishing, finish within a century
```

## Materials and crafting

To reach a tower requirement the solver expands each needed item into the workshop materials that craft it. Crafting stops at any item that can be **gathered** (produced passively, fished, explored or farmed): rather than craft such an item from its own materials, the solver sources it directly — so a passively-made Board stays free instead of being crafted from Wood. Everything else is expanded down to its base materials, which the sourcing plans then gather.

Two bonuses affect how many crafts (and therefore materials) are needed:

- `--crafting-bonus <num>` (default `1.45`): the average number of items produced per craft. A higher bonus means fewer crafts, and so fewer materials, are required.
- `--mastery-bonus <num>` (default `1.1`): the mastery gained per item crafted or otherwise found. This only reduces the crafts needed to satisfy a *mastery* shortfall (the tower or `-m`); it does not apply to a plain item count (`-i`), nor to the quantity of an item consumed as a material by another craft.

A few materials (Iron and Nails) are supplied automatically by the game whenever a craft needs them, so they are never counted, gathered, or expanded.

Pass `-b`/`--breakdown` to list every craft that needs to happen, each with the materials it consumes. Every craftable item gets its own entry (quantities aggregated across everywhere it is used), while base and gathered materials appear only inline. Each craft also notes how many of the item those crafts produce and how much mastery that adds:

```text
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

Pass `--unobtainable` to list every workshop material that currently has no known source — one that is not produced passively, fished, explored, farmed, fetched by a pet, auto-supplied, nor craftable from a recipe of its own. This is a standalone report over the whole workshop: it does not run the solver, so the mastery and inventory files are not needed. It is handy for spotting which items still need a source or more drop-rate data:

```shell
python main.py --unobtainable
```

## Configuration

### Passive producers

Some buildings generate items on a fixed cadence (every minute, ten minutes, hourly, or daily) without crafting. These are described in `config.toml`:

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

[cow_pasture]    # milk daily
milk = 5000

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

[pets]         # each pet's level; tier items come from pets.csv
use_crunchy_omelette = true   # 50% more items fetched by every pet
"Skunk" = 8
"Owl" = 8
# ... one line per pet
```

Pets are passive producers too. A pet fetches a number of items an hour equal to its level, spread across the item tiers it has unlocked (more tiers unlock at levels 3 and 6), so the solver models each as a daily producer of its average haul — the tier items live in `pets.csv`, and the `[pets]` config gives each pet's level. `use_crunchy_omelette` fetches 50% more. Seasonal pets (which only fetch for two months a year) are scaled down to their long-run average daily rate. Pets fetch **very slowly**, so the optimiser only relies on one when nothing faster (or cheaper) can supply an item — no special rule is needed; a pet's low rate simply makes any item it alone provides expensive in time, which the frontier reflects.

Passive production is shaped by how much you can hold at once. The `[storehouse]` section records its capacity on a given day and how much it grows daily:

```toml
[storehouse]
inventory = 49440
updated_on = "2026-06-30"
growth = 20
```

The solver works out the size today (`inventory + growth × days since updated_on`). Each cycle the output is deposited into the storehouse, which holds at most that size and voids the excess; you then use it before the next cycle. So the effective per-cycle output is `min(production, storehouse size)` — the storehouse caps the per-cycle amount, not the total obtainable over many cycles.

### Fishing and exploring

The consumable channels have their own perk flags; the zones and drop rates come from `fishing.csv` and `exploring.csv`:

```toml
[fishing]    # how many fish one Large Net catches
have_reinforced_netting = true   # base catch 400 instead of 250
have_fishing_trawl = true        # +100 fish per net
use_sea_pincher = true           # then ×1.1

[exploring]    # how many items one Arnold Palmer yields
have_lemon_squeezer = true       # base yield 500 instead of 200
use_quandary_chowder = true      # then ×1.1 (550 with the squeezer)
```

## Farming

Farming grows items from seeds. A seed's harvest is a *distribution* of items — mostly its primary crop, occasionally a rarer drop — so one crop can supply several different items. The model has three parts:

- `crops.csv` — each crop's base growth time, and (via an optional `Primary` column) the crop a differently-named seed grows, such as a *Mega* seed.
- `seed_drops.csv` — each seed's in-game drop table (`Crops/harvest all` for common items, `Harvest alls/crop` for rare ones), captured at a fixed row count and normalised to a per-seed rate.
- the `[farming]` config — your row count and modifiers:

```toml
[farming]
rows = 13
double_crop_chance = 25    # percent chance a seed drops a second item
speed_reduction = 90       # percent faster every crop grows
corn_speed_reduction = 20  # corn's further percent reduction
grape_juices_per_day = 14  # instant whole-field grows available each day
```

From these the solver works out, for each crop, how much a full-field planting yields and how long it takes. Mushroom and the *Mega* seeds (Cotton, Sunflower, Beet) yield ten times as much per seed. Crops are grown one at a time, so a crop's plantings run back-to-back, but farming as a whole runs concurrently with the passive buildings.

Growth can be sped up with **Grape Juice**, which grows the whole field instantly but only `grape_juices_per_day` times a day. Because the rows grow one crop at a time, whichever crop is being farmed gets the full daily allowance, adding those instant harvests on top of natural growth — a big saving for slow crops (a nine-day Beet gains far more from a free daily harvest than a fast crop does) and a small one for quick ones. When a plan farms a crop, the report notes the Harvest Alls (full-field harvests), the wait, how many Grape Juices it consumes, and how many of each item those harvests produce — split between the instant Grape Juice grows and normal farming:

```text
Farming (5y 202d):
  352,711 Harvest Alls of Mushroom in 5y 202d using 28,379 Grape Juice:
    229,261,734x Mushroom (18,446,346 from Grape Juice, 210,815,388 from normal farming)
```

The `Farming` heading's time is the whole track's total — crops grow one at a time, so their waits add up.
