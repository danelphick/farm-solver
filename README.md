# Farm Solver

This is an attempt to find the optimal way to spend resources to climb the tower in FarmRPG.

Farm Solver loads in the current state of your mastery and inventory, works out what each tower level still needs, expands the crafting, and then finds the best ways to source everything — trading total **time** against **gold**: self-sourcing a consumable (Arnold Palmers, Large Nets, …) from your passive economy costs time, buying it costs gold. It reports a range of plans along that tradeoff, from "spend no gold and wait" to "buy freely and finish fast".

## Usage

Copy and paste your masteries and inventory from FarmRPG into text files. Point the solver at them either with `--mastery-file` / `--inventory-file`, or by setting them once in the `[solver]` section of `config.toml`:

```toml
[solver]
mastery_file = "./sample-mastery.txt"
inventory_file = "./sample-inventory.txt"
```

A command-line flag overrides the config; if a file is given in neither place, the solver reports an error. The examples below rely on the `[solver]` config, but you can add `--mastery-file <file> --inventory-file <file>` to any of them. Pass `--empty-mastery` / `--empty-inventory` to ignore the mastery/inventory file (and config) entirely and start from nothing — neither flag nor config is required for the one it ignores.

To force specific items to a given held amount regardless of what the inventory file says, use `--override-inventory`, e.g. `--override-inventory "Medium Chest:0,Arnold Palmer:100"`. It's applied after the inventory file is loaded, so it replaces whatever amount the file gave those items (and adds the item if the file didn't have it at all); everything else from the file is left alone.

Instead of naming a fixed file, you can point `data_directory` at a folder of dated exports (e.g. `mastery-2026-07-08.txt`, `inventory-2026-07-08.txt`) and let the solver pick the newest one for whichever of `mastery_file` / `inventory_file` is left unset:

```toml
[solver]
data_directory = "./mydata"
```

```shell
python main.py [-t <tower-requirements>] [-l level] [--target tower|N|TN] \
    [-i item:amount] [-m item:amount] \
    [-b] [--crafting-bonus num] [--mastery-bonus num] [--start-date YYYY-MM-DD] [--average-seasons] \
    [--fastest-plan | --cheapest-plan | --plan N] [--passive-limit duration] [--unobtainable] \
    [--use-pumpkin-juice[=N]] [--mastery-file <file>] [--inventory-file <file>] \
    [--empty-mastery] [--empty-inventory] [--override-inventory item:amount]
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

`-m` and `-i` can be combined, each contributing its own goals:

```shell
python main.py -m "Flying Machine:100000" -i "Gainite:20"
```

On their own (with no `--target`), `-m`/`-i` replace the tower goal entirely, as above. Pass `--target` to solve the tower goal too, with `-m`/`-i` layered on top as *extra* goals instead of replacing it — `--target=tower` (the default level, or `-l`'s) plus a level number or `T<level>` (e.g. `--target=150` or `--target=T150`) to climb to just that level:

```shell
python main.py --target=tower -m "Grand Piano:1000000"
```

climbs the whole tower *and* masters Grand Piano to 1,000,000. A found item counts toward mastery *and* sits in your inventory, so an extra `-m`/`-i` goal for an item the tower (or another `-m`/`-i` goal) already targets is compared on that same footing — whichever needs the most items *produced* wins, and the smaller goal is simply cleared as a side effect: a weaker `-m` target than the tower's own is absorbed (mastery is cumulative), and an `-i` floor only takes over once it calls for more items than a mastery goal for that item already produces. An `-i` goal for an item that is otherwise only a crafting material — not itself mastered — is different: it is a floor on top of however many the tower's crafting already consumes of it, so the solver crafts extra to leave that many spare:

```shell
python main.py --target=tower -i "Steel Wire:50"
```

## Sourcing plans

Every item you still need can be obtained several ways, and the ways differ in what they cost:

- **Passive production** — buildings (and pets) generate items over time for free. They all run concurrently, so their cost is *time*, and the wait is the slowest single item.
- **Fishing** — each **Large Net** instantly catches a bundle of fish from a zone. It costs a net, not time.
- **Exploring** — each **Arnold Palmer** instantly yields a bundle of finds from a location. It costs a palmer, not time.
- **Farming** — crops grow over time (see [Farming](#farming)); grown one at a time, so their times add up, but they run concurrently with the passive buildings.

On top of the four channels, chests and bags can be **opened** at the Locksmith for their contents (see [Openables](#openables)). Opening costs no time or consumable of its own — the cost is having gathered the chest (and its key) in the first place, usually by fishing or exploring — so the solver acquires and opens a chest whenever that beats gathering its contents directly, and chains opens where the contents are themselves openable (a chest yielding a bag, a bag yielding keys).

Every consumable a plan spends is either **self-sourced** from your passive economy — costing *time* — or **bought** for *gold* (see [Spending gold: make vs buy](#spending-gold-make-vs-buy)). So the real tradeoff each plan strikes is **time against gold**: buy freely and finish fast, or spend nothing, wait, and let passive production carry the load — banking the surplus materials it throws off along the way, which are worth gold sold or spent on other tasks. Cookie sets and Grape Juices (see [Cookies](#cookies)) are a related scarce speed-up on the farming track. Farm Solver models the whole choice as a linear program and reports the **Pareto frontier** — a handful of plans, none strictly worse than another on both axes, spanning "buy freely and finish fast" to "bank surplus and wait", where the net gold goes *below zero*. Each row shows its makespan, the consumables it spends, and — with trading [prices](#trading-gold-prices) — its gold as `buy + self-sourced = total`; a plan that banks surplus adds `(bank B, net N)`, its **net** gold `buy − bank`, which is what the frontier trades against time and can fall past zero:

```text
Sourcing plans (time + Arnold Palmers + Large Nets + cookie sets + Grape Juice + Apple Cider -> gold, as buy + self-sourced):
 > 1. 10d 11h 40m + 31,055 AP + 109,769 LN + 11 sets + 147 GJ + 86,406 cider -> 4,245 + 4,533 = 8,778 g  [fastest]
   2. 23d 3h 28m + 32,109 AP + 32,926 LN + 11 sets + 323 GJ + 74,521 cider -> 2,139 + 3,957 = 6,096 g (bank 1,808, net 331)
   ...
   5. 143d 17m + 0 AP + 0 LN + 0 sets + 0 GJ + 44,839 cider -> 0 + 897 = 897 g (bank 17,180, net -17,180)  [cheapest] — all Oak from passive
```

Cookie sets, Grape Juices and Apple Ciders each earn their own column only when some plan spends them (a goal grown entirely by farming, say a heap of Beet, might show none). They are priced and swept like the buyable consumables, so the frontier offers plans that lean on them to finish sooner or save them to finish cheaper.

The **cheapest** plan is the one with the lowest **net** gold — the far, patient end of the frontier, where the plan buys nothing, sources everything it can from free passive production (fishing nothing at all — note the 0 Large Nets above), and banks the most surplus. Credited for what it banks, its net cost goes well negative (−17,180 g above). This end is bounded so it cannot chase a resource only a pet trickles out over centuries: it runs only until the slowest **non-pet** passively-sourceable resource is done (Oak, ~143 days above), leaving that pet-only remainder to the faster channels.

One action often produces many different items at once (a net catches every fish in its zone, a harvest drops its crop plus rarer items), and the plan credits that whole bundle against everything you need — so a single fishing trip can cover several requirements together.

By default the **fastest** plan is expanded in full; the `>` marks which plan is shown. Pass `--plan N` to expand a different one by its number, or `--cheapest-plan` to expand the one with the lowest net gold — buys least, banks most (`--fastest-plan` is the explicit form of the default):

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

A plan often spends more of a consumable than any channel makes, because it drew on stock you already hold — 712 Large Nets in inventory, say, on top of the ones it crafts or opens chests for. So a **From inventory (held consumables)** section lists the held portion of each tool the plan burns (nets, Arnold Palmers, ciders, …), letting the spend reconcile — held plus opened plus crafted plus bought — instead of the held nets seeming to come from nowhere.

Anything no channel can supply is listed as a remainder to craft or provide yourself. Pass `--plans N` to spread more (or fewer) plans across the frontier when you want finer resolution — useful where the fast end swings wildly in cost (default 5).

The frontier is built around the **long-gathering passive resources** — each building-made material is needed only up to its products' full demand, and the makespan at which that full amount finishes gathering is a natural plateau: cap the plan there and every slower material must be sped up with consumables, while that one, and all faster ones, still come for free. So the plans segment by resource, in order, and the frontier table labels each resource plan with why it is there — the material it gathers to completion:

```text
Sourcing plans (time + Arnold Palmers + Large Nets + cookie sets + Grape Juice + Apple Cider -> gold, as buy + self-sourced):
 > 1. 10d 11h 40m + 31,055 AP + 109,769 LN + 11 sets + 147 GJ + 86,406 cider -> 4,245 + 4,533 = 8,778 g  [fastest]
   2. 23d 3h 28m + 32,109 AP + 32,926 LN + 11 sets + 323 GJ + 74,521 cider -> 2,139 + 3,957 = 6,096 g (bank 1,808, net 331)
   3. 27d 7h 39m + 32,457 AP + 29,265 LN + 11 sets + 146 GJ + 70,911 cider -> 1,480 + 3,974 = 5,454 g (bank 2,189, net -709)  — all Steel Wire from passive
   4. 49d 4h 37m + 34,279 AP + 13,268 LN + 11 sets + 145 GJ + 61,992 cider -> 0 + 5,207 = 5,207 g (bank 4,180, net -4,180)  — all Orange from passive
   5. 143d 17m + 0 AP + 0 LN + 0 sets + 0 GJ + 44,839 cider -> 0 + 897 = 897 g (bank 17,180, net -17,180)  [cheapest] — all Oak from passive
```

Plan 3 is clamped to when Steel Wire finishes (its full requirement, ~27 days) and plan 4 to Orange (~49 days); the chosen plan's critical path names the same gating resource ("its full requirement, after which no more is needed"). Plan 4 is the fastest **fully self-sourced** plan — it buys nothing (`0 +` gold). Past it the frontier keeps going: plan 5 sources yet more from passive, spending *no* fishing or exploring tools at all (0 AP, 0 LN), running out to when the slowest non-pet resource, Oak, is gathered in full (~143 days), and banking a large surplus along the way. So as the plans slow they buy less and bank more, and the net gold falls the whole way — crossing zero around plan 3 and reaching −17,180 g at the cheapest. Pet-made materials are skipped here — their trickle is a slow last resort, not a real plateau, and it is why the slow end stops at Oak rather than chasing a centuries-long pet item. Grape Juice and cookie sets accelerate farming, so if more plans are asked for (`--plans N`) than there are resource plateaus, farming-accelerated intermediates (like plan 2 above) are shown in between with no resource label.

By default a seasonal pet is scaled to its steady-state yearly share, so the plan's date does not matter. Set `model_calendar = true` under `[pets]` to instead model the calendar: how much a seasonal pet contributes then depends on how much of the plan runs while it is in season, given the plan's start (today, or `--start-date YYYY-MM-DD`). A short plan that misses a pet's window sees nothing from it; one that lands in the window sees its full haul; a plan spanning several years still settles to the yearly share whatever the start date. So the same goal can source differently in March than in July.

Modelling the calendar costs an extra solve: how much of the plan is in season depends on its length, which is itself an output of the solve, so the solver iterates the start date and the makespan to a fixed point (typically two passes). The solve runs many small LPs, so on a slower machine that doubling adds up — which is why it is off by default. `--average-seasons` forces the yearly-average fast path even when the config opts in, so you can flip it off for a quick run without editing the config.

Three ceilings drop plans that exceed them: `--passive-limit <duration>` on total time, and `--max-ap` / `--max-ln` on the Arnold Palmers and Large Nets spent. If no plan meets the limits (for instance a time cap below the farming critical path, which consumables cannot shorten), the whole frontier is shown with a note saying so and naming the best each axis reaches. The duration is written as `2y`, `5d`, `3h`, `90m`, or a combination like `1y6d`, `1d6h30m`:

```shell
python main.py --passive-limit 100y --max-ln 0    # no fishing, finish within a century
```

## Spending gold: make vs buy

Every consumable is a real item you either **self-source** (costs time) or **buy** (costs gold). Exploring consumes real **Arnold Palmers** (Iced Tea + Lemonade, down to Orchard Lemons and explored Tea Leaves / Shimmer Stone), fishing consumes real **Large Nets** (down to passive stone/wood/straw/antlers), Grape-Juice grows consume real **Grape Juice** (from Vineyard Grapes). Given a **gold budget**, the solver decides make-vs-buy for each, and the frontier sweeps the budget from **`--max-gold inventory`** (0 — self-source everything, slowest) up to **`--max-gold unlimited`** (the default — no cap, buy whatever gets you to the fastest plan). Any number in between caps the gold a plan may spend. One catch: **buying an item never earns its own mastery**, so a Large Net or Orange Juice the tower wants *mastered* must be crafted or gathered — gold can only buy the nets/palmers a channel *burns as a tool*, not the deliverables.

There is a second way to explore: **Apple Cider**. Each cider spends **stamina** to explore a zone many times at a fixed 0.40 drop rate. Stamina comes from four places: a starting balance (`--current-stamina N`), the **Farmhouse**'s daily passive deposit (see [Configuration](#configuration)), and two items consumed on demand to top up the rest — drinking **Orange Juice** (100 stamina each, from Orchard Oranges) or eating **Apples** (15 each, from the orchard or thrown off as a cider byproduct). The solver picks whichever is cheaper at the margin: eating the surplus Apples a cider explore already drops is near-free stamina, so it leans on those before pressing Oranges into juice. How many explores a cider does depends on the zone's Exploring Effectiveness and your cider perks/meals (see [Fishing and exploring](#fishing-and-exploring)). The frontier shows an **Apple Cider** column when a plan uses ciders, and the breakdown gains an *Exploring (Apple Cider)* section and a *Stamina* section accounting for where the stamina came from:

```shell
python main.py -i 'Mushroom:20000000'
```

```text
Sourcing plans (time + Arnold Palmers + Large Nets + Grape Juice + Apple Cider -> gold, as buy + self-sourced):
 > 1. 0m + 19,450 AP + 17,975 LN + 0 GJ + 9,357 cider -> 788 + 1,417 = 2,205 g  [fastest]
   ...
   5. 7d 1h 50m + 20,570 AP + 78,759 LN + 99 GJ + 7,467 cider -> 0 + 2,297 = 2,297 g  [cheapest] — all Lemon from passive

Exploring (Apple Cider):
  9,358x Apple Cider at Gary's Crushroom (9,830,107 stamina, Mushroom)

Stamina:
  9,830,107 spent on Apple Cider
  Drink 83,867x Orange Juice -> 8,386,657 stamina
  Eat 96,230x Apple -> 1,443,450 stamina
```

### Pumpkin Juice (mastery boost)

**Pumpkin Juice** is a rare bought item that, drunk on an item, instantly grants **10% of that item's mastery tier** (100,000 mastery for an item mastering to `MM`). It is a pure gold-for-progress lever — bought, never made — so it trades exactly like the other purchases: a juice on a goal item covers a slice of its mastery shortfall directly, so much less of it needs crafting or gathering, which can finish a plan sooner (and sometimes cheaper, when the sourcing it saves is worth more than the juice). It only helps **mastery** goals (the tower and `-m`); an `-i` acquire goal earns no mastery, so a juice does nothing for it.

It is off by default. Turn it on with `--use-pumpkin-juice` (or `mastery.use_pumpkin_juice = true`), and the solver decides how many each frontier plan drinks — the fast plans buy juice to finish quickly, the cheap ones self-source and drink none. `--use-pumpkin-juice=N` caps the total spent across a plan (the cap cannot be exceeded but need not be reached); each juice is priced at `mastery.pumpkin_juice_price` gold (default 230):

```shell
python main.py -m "Butter:1000000" --use-pumpkin-juice      # any number
python main.py --use-pumpkin-juice=20                       # at most 20 for the tower
```

A plan that drinks juice shows a **Pumpkin Juice** column in the frontier table and a **Pumpkin Juice (mastery boost)** section in its breakdown, naming each goal boosted, how many juices, the gold, and — since a juice only grants 10% of the item's *current* tier — the **mastery levels to drink each at**, late in the goal's final tier so each lands its full boost and the last hits the goal:

```text
Pumpkin Juice (mastery boost):
  1x Pumpkin Juice on Butter = 258 g
    drink at Butter mastery 909,091
  total 258 g
```

Each juice steps the drink point down from the goal by the mastery it is worth (`0.1 × tier ÷ mastery-bonus` — the same amount the solver credits it, `1,000,000 ÷ 1.1 ≈ 90,909` for an `MM` item), so the last juice lands the item on target, the one before a step below, and so on — all above your current progress.

(No wall-clock time is given for each juice: mastery is earned by crafting, which the model treats as instant, so there is no reliable time-of-day to attach — the mastery level is the actionable cue.) A juice can only ever cover up to 100% of a goal's mastery (ten juices fill a fresh tier), so the count is naturally bounded per item even with no cap. Its gold folds into each plan's **buy** total (there is no self-sourced alternative), so it is part of the net cost the frontier trades against time. Juice held in inventory is not credited — every juice is costed at its nominal price.

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

Opening is folded into the same linear program as crafting: it is a free, instant flow that consumes the chest (and key) and produces its average contents. So the solver treats "fish a chest and open it" as just another way to source the contents, taking it whenever that costs fewer consumables than gathering them directly — and it chains, since a chest's contents may themselves be openable (a Medium Chest yields a Grab Bag, whose pool yields the keys that open more chests). The opens that a plan performs are reported in their own section, each naming the key it spends and, of its contents, the items the plan actually needs — including any **consumable it restocks** (a Mystical Chest opened for its Large Nets), even though a net is a fishing tool rather than a craft material. The rest are byproducts and left out:

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
use_hickory_omelette = true  # +20% of the hourly output every ten minutes

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

[farmhouse]    # daily stamina for Apple Cider
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

The four `gk` prices are gold per 1000 units (divided by 1000 for the per-unit gold); cookie sets are gold each. These are the **buyable** consumables. Grape Juice is not traded directly — it is priced at its **production cost** in grapes only so it can be valued (grapes trade for **Arnold Palmers** rather than gold, so `grapes` is quoted in "apk", Arnold Palmers per 1000); it is never bought, always pressed from grapes. With this section present the solver can buy consumables (and the frontier trades makespan against real gold); without it nothing is buyable and every plan is fully self-sourced.

Each plan's gold is shown as **`buy + self-sourced = total`**, and a plan that banks surplus adds **`(bank B, net N)`**. The *total* is the market value of every consumable the plan spends; **buy** is the gold the solver actually spends buying them (exactly `plan.gold` — what `--max-gold` caps), and **self-sourced** is the market value of everything it made or gathered instead. The two are exact — the LP decides which consumables to buy — and always sum to the total. **Bank** is the sale value of the surplus the plan produces past everything it consumes (the same figure as the **Surplus** section below), and **net** is `buy − bank`: the gold you are really out of pocket once those banked materials are sold or spent on other tasks. Net is the axis the frontier trades against time — and what `--cheapest-plan` ranks by — so a slow, passive-leaning plan that banks more than it buys sits at *negative* gold, extending the frontier past the "buy nothing" line.

An expanded priced plan also lists a **Buy (with gold)** section (what it purchases and each cost) and closes with a **Surplus** section. Passive production runs until the slowest resource finishes, so everything faster overshoots — a plan waiting weeks on Oak keeps banking straw, stone and wood far past what its goal needs. Assuming you craft those leftovers into the sellable priced goods (Arnold Palmers, Large Nets, Apple Cider, Orange Juice — Grape Juice is not sellable, and cookie sets are baked from held cookies), the solver finds the most gold they are worth, sending shared materials (straw/wood/stone/antlers all feed Large Nets, say) to whichever good pays best, and lists each good with its gold and the nominal total:

```text
Surplus (accumulates past the goal), craftable into sellable goods:
  520,115x Large Net = 5,201 g
  nominal value = 5,201 g
```

It is rigorous for every plan: consumables are always grounded, so a plan self-sources exactly what it needs (net of what it buys) and the leftovers are genuine spare capacity — naturally zero when the binding materials are all used up.

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

# Apple Cider perks/meals
have_cinnamon_sticks = true      # 12.5 explores per Effectiveness point, not 10
wanderer_level = 4               # cider stamina −4/11/20/33% at level 1/2/3/4
have_neigh_neigh = true          # then ×0.8 stamina (perk)
use_neigh = true                 # then ×0.8 stamina again (meal)

# have_fungus_processing = true  # force Gary's Crushroom's drop table on/off;
                                  # unset auto-detects the artifact from inventory

[exploring.effectiveness]        # per-zone Exploring Effectiveness for cider
Forest = 136
"Small Cave" = 104
# ... one line per explorable zone
```

Each Apple Cider does `1000 + 10 × Effectiveness` explores in a zone (`1250 + 12.5 ×` with Cinnamon Sticks), each costing one stamina before the multiplicative Wanderer / Neigh Neigh / Neigh reductions. A zone with no configured Effectiveness gets no cider offer.

The `[farmhouse]` section is the passive stamina source. The building raises your natural stamina cap by one a day (two with the Mattress Pad perk), so the cap recorded on `registered_on` has grown that much per day since (worked out for the plan's start date, like the storehouse). Each day your actual stamina then rises by `8 × cap`, limited to 500,000 (1,000,000 with the Apple Pie Enthusiast perk) — an automatic daily deposit the plan draws on before eating Apples or drinking Orange Juice for the rest. Stamina is a stat, not a stored good, so the storehouse cap does not apply to it.

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

### Scythe of Dewstar

The **Scythe of Dewstar** adds 4 bonus crops per row to every harvest (so `4 * rows`, split evenly across your farm plots). Each bonus crop is chosen uniformly at random from the crops no slower than the one you're growing — Mushroom excluded — and gives exactly one of that crop's *primary* item, never a rarer drop. The Scythe is an account perk, not a held item, so it has no inventory presence to auto-detect the way an artifact can — instead, it is assumed held once every mastery requirement through Tower level 250 is met:

```toml
[farming]
# have_scythe_of_dewstar = true  # force on/off; unset auto-detects from having T250
# have_dewstars_journal = true   # held item; unset auto-detects from inventory
```

For example, growing Carrot (2m) rolls its bonus crops from Carrot and Peppers (1m) only, while growing Radish (10m) rolls from everything up to and including Radish itself (Peppers, Carrot, Peas, Cucumber, Eggplant, Radish). A picked Mega seed still gives just one of its base crop — its own tenfold yield doesn't apply to a Dewstar pick.

Without **Dewstar's Journal**, Cookies don't touch these bonus crops — they accrue at the same flat per-harvest rate all day, cookie window or not. With the journal, bonus crops follow the same boosted-harvest count as the crop's own Cookie yield, so the windowed harvests give ninefold bonus crops too.
