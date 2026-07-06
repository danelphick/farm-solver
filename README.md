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
    [-b] [--crafting-bonus num] [--mastery-bonus num] [--start-date YYYY-MM-DD] [--average-seasons] \
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

On top of the four channels, chests and bags can be **opened** at the Locksmith for their contents (see [Openables](#openables)). Opening costs no time or consumable of its own — the cost is having gathered the chest (and its key) in the first place, usually by fishing or exploring — so the solver acquires and opens a chest whenever that beats gathering its contents directly, and chains opens where the contents are themselves openable (a chest yielding a bag, a bag yielding keys).

Spending a net or a palmer buys speed: it lifts an item out of the slow farming/passive queues for zero time, at the cost of one consumable. Cookie sets and Grape Juices (see [Cookies](#cookies)) are the same kind of scarce speed-for-time trade on the farming track. So there is a genuine tradeoff between time and all four. Farm Solver models the whole choice as a linear program and reports the **Pareto frontier** — a handful of plans, none strictly worse than another, spanning "all free but slow" to "fast but costly":

```text
Sourcing plans (time / Arnold Palmers / Large Nets):
 > 1. 87y 62d / 71,136 AP / 0 LN  [fastest]
   2. 293y 319d / 3,931 AP / 0 LN  [cheapest]
```

Each of cookie sets and Grape Juices is priced and swept like the consumables, so the frontier offers plans that spend more of them to finish sooner or none of them to save them — a goal grown entirely by farming (say a heap of Beet) is no longer a single take-it-or-leave-it plan. When some plan spends them, each gets its own column:

```text
Sourcing plans (time / Arnold Palmers / Large Nets / cookie sets / Grape Juice):
 > 1. 11d 8h 47m / 0 AP / 0 LN / 11 sets / 159 GJ  [fastest]
   2. 36d 8h 46m / 0 AP / 0 LN / 11 sets / 156 GJ
   ...
   5. 3y 242d / 0 AP / 0 LN / 0 sets / 0 GJ  [cheapest]
```

The **cheapest** plan is the one spending the fewest of all four (leaning hardest on free farming and passive production), so it is not always the slowest overall.

One action often produces many different items at once (a net catches every fish in its zone, a harvest drops its crop plus rarer items), and the plan credits that whole bundle against everything you need — so a single fishing trip can cover several requirements together.

By default the **fastest** plan is expanded in full; the `>` marks which plan is shown. Pass `--plan N` to expand a different one by its number, or `--cheapest-plan` to expand the one spending the fewest Arnold Palmers and Large Nets (`--fastest-plan` is the explicit form of the default):

```shell
python main.py --cheapest-plan        # expand the cheapest plan instead
```

The chosen plan opens with its **critical path** — the track, and the item or crop within it, that caps how fast the plan can finish. Fishing and exploring are instant, so the limit is a passive wait or a farming sequence. The two are not symmetric: passive production runs for free alongside everything, so it opportunistically fills whatever wait farming already forces (making an item passively to save a consumable rather than fishing or exploring it), which pads the passive track out to the makespan without being the real constraint. So when farming sets the pace it is usually named alone. The reverse fill happens too, though: on a plan clamped to a passive item's completion, farming grows free crops to fill the passive-forced wait, so if the longest-grown crop is one a consumable channel could supply instead (Mushroom, say — grown here only because it is free), the co-maximal passive item is named as the driver, not the farming. And on a plan built around a long-gathering resource, the resource whose *full* requirement finishes exactly at the makespan is named ("its full requirement, after which no more is needed") — the true gate, even where another passive item, still being sped up with consumables, reaches the makespan alongside it. Pets are a slow last resort, so a pet quietly trickling out a small top-up of an item another channel mostly supplies is not named over a building doing the real work: a passive item is the critical path only when a building drives it, or when a pet is that item's primary source. Either way it is what you would have to speed up — by sourcing it with consumables, or picking a faster plan — to finish sooner:

```text
Critical path (sets the 293y 319d total):
  2,574,345x Horn from passive production (293y 319d) — the slowest item to build up
```

The plan is then broken down by channel, each ordered so the heaviest lines lead: fishing and exploring by the Large Nets and Arnold Palmers they spend, and farming by how long each crop takes. The passive section leads with the item on the critical path (the one named above, when passive is the bottleneck track), then orders the rest by quantity and finally by wait, so the runner-up bottlenecks are still visible. Within a section the counts are right-aligned so the `x` lines up. Chests and bags the plan opens for their contents get their own section (see [Openables](#openables)). Each passive line also names the producer that makes the item; when several producers make it, the quantity is split between them by their per-minute rate share, largest first. A seasonal pet only fetches during its calendar window, so it counts only for the part of the plan that overlaps that window (given the plan's start date — see below) and its window is flagged (`[seasonal: Mar–Apr]`); a pet out of season for the whole plan drops out entirely:

```text
Exploring:
  3,931x Arnold Palmer at Jundland Desert (Small Bolt)

Passive production (concurrent):
  2,574,345x Horn in 293y 319d (from pet Dog)
  2,858,281x Purple Flower in 279y 246d (2,449,955 from pet Squirrel, 408,326 from pet Hummingbird [seasonal: Mar–Apr])
  ...
```

Anything no channel can supply is listed as a remainder to craft or provide yourself. Pass `--plans N` to spread more (or fewer) plans across the frontier when you want finer resolution — useful where the fast end swings wildly in cost (default 5).

The frontier is built around the **long-gathering passive resources** — each building-made material is needed only up to its products' full demand, and the makespan at which that full amount finishes gathering is a natural plateau: cap the plan there and every slower material must be sped up with consumables, while that one, and all faster ones, still come for free. So the plans segment by resource, in order, and the frontier table labels each resource plan with why it is there — the material it gathers to completion:

```text
Sourcing plans (time / Arnold Palmers / Large Nets / cookie sets / Grape Juice):
 > 1. 10d 11h 40m / 209,031 AP / 48,511 LN / 11 sets / 147 GJ  [fastest]
   2. 20d 17h 22m / 189,637 AP / 35,050 LN / 11 sets / 290 GJ
   3. 27d 7h 39m / 178,119 AP / 29,265 LN / 10 sets / 136 GJ  — all Steel Wire from passive
   4. 143d 17m / 90,406 AP / 0 LN / 0 sets / 0 GJ  — all Oak from passive
   5. 251y 10d / 421 AP / 0 LN / 0 sets / 0 GJ  [cheapest]
```

Plan 3 is clamped to when Steel Wire finishes (its full requirement, ~27 days), plan 4 to Oak (~143 days); the chosen plan's critical path names the same gating resource ("its full requirement, after which no more is needed"). Pet-made materials are skipped here — their trickle is a slow last resort, not a real plateau. Grape Juice and cookie sets accelerate farming, so if more plans are asked for (`--plans N`) than there are resource plateaus, farming-accelerated intermediates (like plan 2 above) are shown in between with no resource label.

By default a seasonal pet is scaled to its steady-state yearly share, so the plan's date does not matter. Set `model_calendar = true` under `[pets]` to instead model the calendar: how much a seasonal pet contributes then depends on how much of the plan runs while it is in season, given the plan's start (today, or `--start-date YYYY-MM-DD`). A short plan that misses a pet's window sees nothing from it; one that lands in the window sees its full haul; a plan spanning several years still settles to the yearly share whatever the start date. So the same goal can source differently in March than in July.

Modelling the calendar costs an extra solve: how much of the plan is in season depends on its length, which is itself an output of the solve, so the solver iterates the start date and the makespan to a fixed point (typically two passes). The solve runs many small LPs, so on a slower machine that doubling adds up — which is why it is off by default. `--average-seasons` forces the yearly-average fast path even when the config opts in, so you can flip it off for a quick run without editing the config.

Three ceilings drop plans that exceed them: `--passive-limit <duration>` on total time, and `--max-ap` / `--max-ln` on the Arnold Palmers and Large Nets spent. If no plan meets the limits (for instance a time cap below the farming critical path, which consumables cannot shorten), the whole frontier is shown with a note saying so and naming the best each axis reaches. The duration is written as `2y`, `5d`, `3h`, `90m`, or a combination like `1y6d`, `1d6h30m`:

```shell
python main.py --passive-limit 100y --max-ln 0    # no fishing, finish within a century
```

## Plan modes: unlimited vs inventory

By default (`--plan-mode unlimited`) Arnold Palmers, Large Nets and Grape Juices are abstract, unbounded costs — you can spend as many as a plan wants, and the frontier just trades them against time. `--plan-mode inventory` instead sources **only from your inventory and passive economy**: every consumable has to actually be crafted or held. Exploring now consumes real **Arnold Palmers** (Iced Tea + Lemonade, down to Orchard Lemons and explored Tea Leaves / Shimmer Stone), fishing consumes real **Large Nets** (down to passive stone/wood/straw/antlers), and Grape-Juice grows consume real **Grape Juice** (from Vineyard Grapes). So a consumable is only as plentiful as the passive materials beneath it, and the frontier trades makespan against how much of your **AP / LN / GJ / cider** stockpile a plan burns — the thrifty end leaves the most in reserve.

Inventory mode adds a second way to explore: **Apple Cider**. Each cider spends **stamina** to explore a zone many times at a fixed 0.40 drop rate. Stamina comes from three places: a starting balance (`--current-stamina N`), the **Farmhouse**'s daily passive deposit (see [Configuration](#configuration)), and drinking **Orange Juice** (100 each, from Orchard Oranges) to top up the rest. How many explores a cider does depends on the zone's Exploring Effectiveness and your cider perks/meals (see [Fishing and exploring](#fishing-and-exploring)). The frontier shows an **Apple Cider** column when a plan uses ciders, and the breakdown gains an *Exploring (Apple Cider)* section and a *Stamina* section accounting for where the stamina came from:

```shell
python main.py --plan-mode inventory --current-stamina 200000 -i 'Mushroom:2000000'
```

```text
Sourcing plans (time / Arnold Palmers / Large Nets / Grape Juice / Apple Cider):
 > 3. 1h 4m / 3,196 AP / 0 LN / 1 GJ / 235 cider

Exploring (Apple Cider):
  200x Apple Cider at Gary's Crushroom (209,555 stamina, Mushroom)

Stamina:
  209,555 spent on Apple Cider
  50,000 banked at the start
  787,096/day from the Farmhouse (passive)
  Drink 634x Orange Juice -> 63,378 stamina
```

## Materials and crafting

To reach a tower requirement the solver expands each needed item into the workshop materials that craft it. Crafting stops at any item that can be **gathered** (produced passively, fished, explored or farmed): rather than craft such an item from its own materials, the solver sources it directly — so a passively-made Board stays free instead of being crafted from Wood. Everything else is expanded down to its base materials, which the sourcing plans then gather.

Two bonuses affect how many crafts (and therefore materials) are needed:

- `--crafting-bonus <num>` (default `1.45`): the average number of items produced per craft. A higher bonus means fewer crafts, and so fewer materials, are required.
- `--mastery-bonus <num>` (default `1.1`): the mastery gained per item crafted or otherwise found. This only reduces the crafts needed to satisfy a *mastery* shortfall (the tower or `-m`); it does not apply to a plain item count (`-i`), nor to the quantity of an item consumed as a material by another craft.

Whatever is already in your **inventory** is drawn on before anything is gathered, crafted or opened: the quantity held is credited to each intermediate material, so a plan that needs 2,000 Red Shields to craft Frost Shields but already holds 37,000 makes none (and does not fish the chests it would have opened for them). The directly demanded items get no such credit, since their shortfall is already measured against what you hold — mastery progress for the tower and `-m` goals, the current count for `-i`.

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

## Openables

Chests and bags — dropped by fishing zones and exploring locations — can be opened at the Locksmith for their contents, defined in `openables.csv`. There are two kinds: a **fixed** openable always yields a set number of each of its items; a **random** one yields a single item drawn from a pool, so each entry averages its own count spread over the pool size. A chest also costs one **key** of its tier to open (Small Chest → Small Key, Medium → Square Key, Large → Treasure Key, Mystical → Skeleton Key); bags open freely.

Opening is folded into the same linear program as crafting: it is a free, instant flow that consumes the chest (and key) and produces its average contents. So the solver treats "fish a chest and open it" as just another way to source the contents, taking it whenever that costs fewer consumables than gathering them directly — and it chains, since a chest's contents may themselves be openable (a Medium Chest yields a Grab Bag, whose pool yields the keys that open more chests). The opens that a plan performs are reported in their own section, each naming the key it spends and, of its contents, only the items the plan actually needs (the rest are byproducts):

```text
Opening (Locksmith):
  4,664x Large Chest 03 using 4,663 Treasure Key -> 46,631x Corn, 23,316x Glass Bottle
    802x Grab Bag 08 -> 200x Skeleton Key, 401x Treasure Key
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

[farmhouse]    # daily stamina for Apple Cider (inventory plan mode only)
stamina_cap = 98387               # natural max stamina on registered_on
registered_on = "2026-07-05"
have_mattress_pad = true          # the cap grows +2/day instead of +1
have_apple_pie_enthusiast = true  # daily stamina gain capped at 1M, not 500k

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

Pets are passive producers too. A pet fetches a number of items an hour equal to its level, spread across the item tiers it has unlocked (more tiers unlock at levels 3 and 6), so the solver models each as a daily producer of its average haul — the tier items live in `pets.csv`, and the `[pets]` config gives each pet's level. `use_crunchy_omelette` fetches 50% more. Seasonal pets only fetch for two months a year, so a seasonal pet's haul is scaled to its steady-state yearly share by default, or — with `model_calendar = true` — by how much of the plan actually falls in its window given the start date (see [Sourcing plans](#sourcing-plans)). Pets fetch **very slowly**, so the optimiser only relies on one when nothing faster (or cheaper) can supply an item — no special rule is needed; a pet's low rate simply makes any item it alone provides expensive in time, which the frontier reflects.

Passive production is shaped by how much you can hold at once. The `[storehouse]` section records its capacity on a given day and how much it grows daily:

```toml
[storehouse]
inventory = 49440
updated_on = "2026-06-30"
growth = 20
```

The solver works out the size today (`inventory + growth × days since updated_on`). Each cycle the output is deposited into the storehouse, which holds at most that size and voids the excess; you then use it before the next cycle. So the effective per-cycle output is `min(production, storehouse size)` — the storehouse caps the per-cycle amount, not the total obtainable over many cycles.

### Trading (gold prices)

The scarce consumables can be bought and sold between players for gold, so an optional `[trading]` section prices them and lets the solver rank plans by the **real gold** each one costs:

```toml
[trading]
orange_juice  = 8     # gold per 1000 ("gk")
apple_cider   = 20
arnold_palmer = 60
large_net     = 10
grapes        = 5     # in apk (Arnold Palmers per 1000); a juice presses 200 grapes
cookie_set    = 20    # priced individually, in gold each (not gk)
```

The four `gk` prices are gold per 1000 units (divided by 1000 for the per-unit gold); cookie sets are gold each. Grape Juice is not traded directly — it is priced at its **production cost** in grapes, and grapes trade for **Arnold Palmers** rather than gold, so `grapes` is quoted in "apk" (Arnold Palmers per 1000). Pressing 200 grapes yields `--crafting-bonus` juices, so a juice costs `200 / crafting_bonus` grapes, each worth `grapes/1000` Arnold Palmers, converted to gold through the Arnold Palmer's own price (with the defaults, `200/1.45` grapes ≈ 0.69 AP ≈ 0.041 gold). Omit `grapes` and Grape Juice stays free. With this section present the frontier trades makespan against gold (not an abstract consumable count), the table gains a **gold** column, and `--cheapest-plan` picks the least-gold plan. Omit the section and the solver stays on its earlier gold-agnostic behaviour.

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

# Apple Cider perks/meals (used only in --plan-mode inventory)
have_cinnamon_sticks = true      # 12.5 explores per Effectiveness point, not 10
wanderer_level = 4               # cider stamina −4/11/20/33% at level 1/2/3/4
have_neigh_neigh = true          # then ×0.8 stamina (perk)
use_neigh = true                 # then ×0.8 stamina again (meal)

[exploring.effectiveness]        # per-zone Exploring Effectiveness for cider
Forest = 136
"Small Cave" = 104
# ... one line per explorable zone
```

Each Apple Cider does `1000 + 10 × Effectiveness` explores in a zone (`1250 + 12.5 ×` with Cinnamon Sticks), each costing one stamina before the multiplicative Wanderer / Neigh Neigh / Neigh reductions. A zone with no configured Effectiveness gets no cider offer.

The `[farmhouse]` section is the passive stamina source. The building raises your natural stamina cap by one a day (two with the Mattress Pad perk), so the cap recorded on `registered_on` has grown that much per day since (worked out for the plan's start date, like the storehouse). Each day your actual stamina then rises by `8 × cap`, limited to 500,000 (1,000,000 with the Apple Pie Enthusiast perk) — an automatic daily deposit the plan draws on before drinking Orange Juice for the rest. Stamina is a stat, not a stored good, so the storehouse cap does not apply to it.

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
cookies_grow_time_threshold = "18m"  # slowest crop worth a cookie set (omit to disable)
instant_grow_time = "3s"   # effective grow time of crops with <=5m base growth (omit to disable)
```

From these the solver works out, for each crop, how much a full-field planting yields and how long it takes. Mushroom and the *Mega* seeds (Cotton, Sunflower, Beet) yield ten times as much per seed. Crops are grown one at a time, so a crop's plantings run back-to-back, but farming as a whole runs concurrently with the passive buildings.

Growth can be sped up with **Grape Juice**, which grows the whole field instantly but only `grape_juices_per_day` times a day. Because the rows grow one crop at a time, whichever crop is being farmed gets the full daily allowance, adding those instant harvests on top of natural growth — a big saving for slow crops (a nine-day Beet gains far more from a free daily harvest than a fast crop does) and a small one for quick ones. When a plan farms a crop, the report notes the Harvest Alls (full-field harvests), the wait, how many Grape Juices it consumes, and how many of each item those harvests produce — split between the instant Grape Juice grows and normal farming:

```text
Farming (5y 202d):
  352,711 Harvest Alls of Mushroom in 5y 202d using 28,379 Grape Juice:
    229,261,734x Mushroom (18,446,346 from Grape Juice, 210,815,388 from normal farming)
```

The `Farming` heading's time is the whole track's total — crops grow one at a time, so their waits add up.

The very fast crops — those with a base growth of five minutes or less (Peppers, Carrot, Peas, Cucumber and Eggplant, the slowest at exactly 5m) — grow quicker than you can click once the speed perks are on, so `instant_grow_time` (default `"3s"`) sets their effective harvest cadence instead of the speed formula. Omit it to leave them on the formula. These crops are never given Grape Juice: they already grow faster than a wait, so an instant grow would be wasted on them (the daily allowance is saved for the slow crops that gain from it).

### Cookies

Farming can be sped up further with **Cookies**. There are three types — Happy, Spooky and Lovely — and each active cookie triples a harvest's yield, so all three at once (a full **set**) makes it ninefold. The boost applies to whatever you *harvest* during the set's five-minute window: you time it so a crop finishes just as you eat the set, harvest it, then burn your day's Grape Juice grows — all boosted (for a slow crop like a 48-minute Pine that is one natural harvest plus ~14 Grape Juice grows, so ~15 boosted harvests a set). The model uses sets only in full and only on qualifying crops. One set is modelled as a single **cookie-boosted day**: a real day of farming a crop, but with that window's harvests boosted ninefold.

Sets are scarce, so they are spent carefully. Only crops whose grow time reaches `cookies_grow_time_threshold` qualify — on a slow crop each boosted harvest stands in for a long grow, so a set saves a lot of farming time, whereas a fast crop is cheap to grow anyway and a scarce set is wasted (at the sample perks a `18m` threshold lets them go to Corn but not quicker crops). The solver then spends its sets where a boosted day shortens the farming critical path the most, and never on a plan where farming isn't the bottleneck (a set that wouldn't finish the plan any sooner is kept in the cupboard).

The number of sets available is the fewest of the three cookie types in your inventory; `--cookie-sets N` overrides it in either direction (`--cookie-sets 0` turns cookies off). A cookie set covers one real day and boosts whichever crops you harvest that day alongside the day's Grape Juice allocation, so the sets a plan spends is the whole number of days its cookie-boosted farming spans — the `Farming` heading totals them (`ceil` of the total, so crops that share a boundary day don't each round it up) and each boosted crop reports its span, juice and yield:

```text
Farming (10d 11h 40m, 11 cookie sets):
  Cookie-boosted Pine Tree in 10d 3h 47m using 142 Grape Juice (yield 9x):
    108,280x Pine Tree
  Cookie-boosted Mega Cotton in 7h 53m using 5 Grape Juice (yield 9x):
    29,138x Cotton
```
