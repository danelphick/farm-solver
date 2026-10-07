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

### Solving from a web-app backup

If you use the [web app](#web-app), its **Back up** button writes one JSON file holding everything that browser has for you. `--backup-file` reads it directly, so you don't have to keep a separate pair of text files in sync:

```shell
python main.py --backup-file ~/Downloads/farm-solver-backup-2026-09-01.json
```

That solves the *same* run the web page would: the backup carries the pasted mastery and inventory text **and** the goal and options of the solve it was exported from (`-i`/`-m`/`--target`, budgets, `--plans`, the preference panels' config overrides, and which frontier row was selected). It replaces `--mastery-file`/`--inventory-file` — giving either alongside it is an error, since half a state from each is nobody's intent — and needs no `[solver]` file config at all.

Anything you also put on the command line wins over the file, so you can reuse a saved state with a different question:

```shell
python main.py --backup-file backup.json -i "Gold Bar:5000"   # same save, new goal
python main.py --backup-file backup.json --plans 8 --format json
python main.py --backup-file backup.json --mastery-costs      # the goal is not inherited
```

`--override` is the exception to "yours wins outright": the backup's overrides and yours are *combined*, with yours applied last so it wins on any flag both set. `--empty-mastery`/`--empty-inventory` still drop their half of the backup. And `--mastery-costs`, `--ascend` and `--unobtainable` solve their own goals, so they never inherit the backup's — only its game state and solver settings.

A backup's browser-only blocks (pane preferences, the mastery-snapshot history) are ignored. A file that isn't a backup, or one written by a newer version of the app than this build knows, is refused by name rather than half-read.

To force specific items to a given held amount regardless of what the inventory file says, use `--override-inventory`, e.g. `--override-inventory "Medium Chest:0,Arnold Palmer:100"`. It's applied after the inventory file is loaded, so it replaces whatever amount the file gave those items (and adds the item if the file didn't have it at all); everything else from the file is left alone.

Instead of naming a fixed file, you can point `data_directory` at a folder of dated exports (e.g. `mastery-2026-07-08.txt`, `inventory-2026-07-08.txt`) and let the solver pick the newest one for whichever of `mastery_file` / `inventory_file` is left unset:

```toml
[solver]
data_directory = "./mydata"
```

The LP backend is selectable with `backend` under `[solver]`, or `--solver NAME` for a one-off run (the flag wins). The default is `highs`, which solves in-process and is much faster; `cbc` (PuLP's bundled solver, and the fallback whenever the `highspy` package is missing) needs no extra dependency but launches a subprocess per solve. Any other installed [PuLP solver](https://coin-or.github.io/pulp/technical/solvers.html) can be named too (e.g. `GLPK_CMD`). Plans differing only in which equally-good alternative fills the same time and gold can differ between backends, just as they can between runs.

```shell
python main.py [-t <tower-requirements>] [-l level] [--target tower|N|TN] \
    [-i item:amount] [-m item:amount] \
    [-b] [--crafting-bonus num] [--mastery-bonus num] [--start-date YYYY-MM-DD] [--model-calendar | --average-calendar | --ignore-calendar] \
    [--solver highs|cbc|NAME] \
    [--fastest-plan | --cheapest-plan | --plan N] [--passive-limit duration] [--unobtainable] \
    [--mastery-costs [--sort time|gold]] [--self-sourced] \
    [--use-pumpkin-juice[=N]] [--mastery-file <file>] [--inventory-file <file>] \
    [--backup-file <file>] \
    [--empty-mastery] [--empty-inventory] [--override-inventory item:amount] \
    [--format text|markdown|json] [--pager]
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

By default the solver models the calendar: how much a seasonal pet contributes depends on how much of the plan runs while it is in season, given the plan's start (today, or `--start-date YYYY-MM-DD`). A short plan that misses a pet's window sees nothing from it; one that lands in the window sees its full haul; a plan spanning several years still settles to the yearly share whatever the start date. So the same goal can source differently in March than in July. The [Mechanical Heart](#mechanical-heart) grants are made date-aware by the same setting. `calendar` under `[solver]` picks between three modes (or pass the matching flag for a one-off run, without editing the config):

- `"full"` (`--model-calendar`, the default) — model the calendar exactly as above.
- `"average"` (`--average-calendar`) — smear each seasonal pet and the monthly grants to their steady-state yearly/monthly share, so the plan's date does not matter.
- `"ignore"` (`--ignore-calendar`) — leave seasonal pets and the monthly grants out of the plan entirely: plan as if those sources did not exist.

Modelling the calendar costs an extra solve: how much of the plan is in season depends on its length, which is itself an output of the solve, so the solver iterates the start date and the makespan to a fixed point (typically two passes). The solve runs many small LPs, so on a slower machine that doubling adds up — which is why the `average` mode exists as a fast-path escape hatch. The three CLI flags override the config either way for a quick run (they are mutually exclusive).

Three ceilings drop plans that exceed them: `--passive-limit <duration>` on total time, and `--max-ap` / `--max-ln` on the Arnold Palmers and Large Nets spent. If no plan meets the limits (for instance a time cap below the farming critical path, which consumables cannot shorten), the whole frontier is shown with a note saying so and naming the best each axis reaches. The duration is written as `2y`, `5d`, `3h`, `90m`, or a combination like `1y6d`, `1d6h30m`:

```shell
python main.py --passive-limit 100y --max-ln 0    # no fishing, finish within a century
```

## Report format

Pass `--format markdown` (`md` also accepted) to get the same report as Markdown, with a table for the frontier and for each tabular section of the expanded plan (Fishing, Exploring, Farming, Passive production, Buy, Surplus, ...); narrative bits like the critical path stay plain text. `--format text` (`txt`) is the default. When stdout is a terminal, Markdown is rendered through it via [rich](https://github.com/Textualize/rich) rather than printed as raw source; redirected to a file (`python main.py --format markdown > report.md`) it prints the Markdown source itself, so the file is a real `.md` document.

Pass `--format json` to get the whole report as one JSON document instead — every section a normal run prints, machine-readable, so you can pipe a solve into `jq` or a script:

```bash
# the frontier as "plan, seconds, gold" triples
python main.py -i Corn:5000 --format json | jq -r '.sections[] | select(.name=="frontier") | .blocks[] | select(.kind=="table") | .rows[] | "\(.["#"])  \(.Time)  \(.Gold)"'
```

The document is `{"sections": [{"name": ..., "blocks": [...]}]}`. `sections` is a **list**, not an object, because the order is the report's own and a name can repeat (a run emits several `notes`; an `--ascend` run a `plan` per stage). Names are `solution`, `unobtainable`, `mastery_costs`, `notes`, `frontier`, `plan` and `crafts`; a section a run leaves empty is omitted, exactly as the other formats print nothing for it. Each block is one of:

| `kind` | fields |
| --- | --- |
| `heading` | `level`, `text` |
| `paragraph` | `text` |
| `list` | `items` |
| `table` | `columns` (the column names, in order), `rows` (one object per row, keyed by column name) |
| `graph` | `x_label`, `y_label`, `points` (`{"x", "y"}` objects) — the `--graph` impact plot, as its points rather than the SVG the Markdown format draws |

**Values are machine-readable, not the printed text.** A quantity is a bare number — no thousands separators and no unit suffix, so the frontier's `1,234 g` is `1234` and a count of `148,290` is `148290`. **Every duration is in whole seconds**: `6d 5h 16m` is `538560`, and an instant plan's `0m` is `0`. The text and Markdown reports are unchanged — they still print for a reader; only the JSON drops the formatting.

Some cells carry more than a number, since JSON has room for the structure the printed sentence flattens: a **Yields** cell is an item→quantity object (`{"Wood": 148290}`, or a plain list of names where the report shows no counts). Columns that are genuinely prose — Action, Detail, Source, Notes, Materials used — stay strings.

Two cells drop a human annotation, but nothing is lost from the document: the Goal table's **Still needed** is the shortfall itself (`0` where the report prints `done`, and no `✗` — the unreachable items are named in full by the *Materials with no known source* table right below it), and the frontier's **Gold** is the headline total the report's `buy + passive = total g` cell adds up to.

`--format json` works with every mode, including `--unobtainable`, `--mastery-costs` and `--ascend`. `--pager` needs `--format markdown` and so does not apply.

Add `--pager` (with `--format markdown`, on a terminal) to page the rendered report through the system pager (`$PAGER`/`$MANPAGER`, else `less`) instead of dumping it straight to the scrollback — reports with a lot of sections (a full tower solve) can be long. This pages the *rendered* tables and styling; piping the command's own output to a pager yourself (`python main.py --format markdown | less`) doesn't work for that, since the pipe makes stdout a non-terminal before the report is ever rendered, so it falls back to raw Markdown source instead.

## Spending gold: make vs buy

Every consumable is a real item you either **self-source** (costs time) or **buy** (costs gold). Exploring consumes real **Arnold Palmers** (Iced Tea + Lemonade, down to Orchard Lemons and explored Tea Leaves / Shimmer Stone), fishing consumes real **Large Nets** (down to passive stone/wood/straw/antlers), Grape-Juice grows consume real **Grape Juice** (from Vineyard Grapes). Given a **gold budget**, the solver decides make-vs-buy for each, and the frontier sweeps the budget from **`--max-gold inventory`** (0 — self-source everything, slowest) up to **`--max-gold unlimited`** (the default — no cap, buy whatever gets you to the fastest plan). Any number in between caps the gold a plan may spend. One catch: **buying an item never earns its own mastery**, so a Large Net or Orange Juice the tower wants *mastered* must be crafted or gathered — gold can only buy the nets/palmers a channel *burns as a tool*, not the deliverables.

There is a second way to explore: **Apple Cider**. Each cider spends **stamina** to explore a zone many times at a fixed 0.40 drop rate. Stamina comes from four places: a starting balance (`[farmhouse] current_stamina`, or `--current-stamina N`), the **Farmhouse**'s daily passive deposit (see [Configuration](#configuration)), and two items consumed on demand to top up the rest — drinking **Orange Juice** (100 stamina each, from Orchard Oranges) or eating **Apples** (15 each, from the orchard or thrown off as a cider byproduct). With `price_stamina` on (the default) every stamina point an explore burns is valued at its Orange-Juice replacement cost whatever its source — banked, passive or eaten Apples alike — so the solver weighs them honestly (see **Pricing stamina** below); only stamina from drinking Orange Juice is exempt, since the juice is already paid for. How many explores a cider does depends on the zone's Exploring Effectiveness and your cider perks/meals (see [Fishing and exploring](#fishing-and-exploring)). The frontier shows an **Apple Cider** column when a plan uses ciders, and the breakdown gains an *Exploring (Apple Cider)* section and a *Stamina* section accounting for where the stamina came from:

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

It is off by default. Turn it on with `--use-pumpkin-juice` (or `mastery.use_pumpkin_juice = true`), and the solver decides how many each frontier plan drinks — the fast plans buy juice to finish quickly, the cheap ones self-source and drink none. `--use-pumpkin-juice=N` caps the total spent across a plan (the cap cannot be exceeded but need not be reached); each juice is priced at `trading.pumpkin_juice` gold (default 230) — nominal, since juice cannot be bought from another player, but still a cost plans are ranked on:

```shell
python main.py -m "Butter:1000000" --use-pumpkin-juice      # any number
python main.py --use-pumpkin-juice=20                       # at most 20 for the tower
```

A plan that drinks juice shows a **Pumpkin Juice** column in the frontier table and a **Pumpkin Juice (mastery boost)** section in its breakdown, naming each goal boosted, how many juices, the gold, and — since a juice only grants 10% of the item's *current* tier — the **mastery level to start drinking at**, late in the goal's final tier so each lands its full boost and the last hits the goal:

```text
Pumpkin Juice (mastery boost):
  1x Pumpkin Juice on Butter = 258 g
    start drinking at Butter mastery 909,091
  total 258 g
```

The start sits below the goal by the mastery the juices are worth (`0.1 × tier ÷ mastery-bonus` each — the same amount the solver credits them, `1,000,000 ÷ 1.1 ≈ 90,909` for an `MM` item). Each juice lifts mastery exactly to the next one's drink point, so you drink them all back to back from there and the last lands the item on target.

(No wall-clock time is given for each juice: mastery is earned by crafting, which the model treats as instant, so there is no reliable time-of-day to attach — the mastery level is the actionable cue.) A juice can only ever cover up to 100% of a goal's mastery (ten juices fill a fresh tier), so the count is naturally bounded per item even with no cap. Its gold folds into each plan's **buy** total (there is no self-sourced alternative), so it is part of the net cost the frontier trades against time. Juice held in inventory is not credited — every juice is costed at its nominal price.

## Materials and crafting

To reach a tower requirement the solver expands each needed item into the workshop materials that craft it. Crafting stops at any item that can be **gathered** (produced passively, fished, explored or farmed): rather than craft such an item from its own materials, the solver sources it directly — so a passively-made Board stays free instead of being crafted from Wood. Everything else is expanded down to its base materials, which the sourcing plans then gather.

Two bonuses affect how many crafts (and therefore materials) are needed:

- `--crafting-bonus <num>` (default `1.45`): the average number of items produced per craft. A higher bonus means fewer crafts, and so fewer materials, are required.
- `--mastery-bonus <num>` (default `1.1`): the mastery gained per item crafted or otherwise found. This only reduces the crafts needed to satisfy a *mastery* shortfall (the tower or `-m`); it does not apply to a plain item count (`-i`), nor to the quantity of an item consumed as a material by another craft.

Whatever is already in your **inventory** is drawn on before anything is gathered, crafted or opened: the quantity held is credited to each intermediate material, so a plan that needs 2,000 Red Shields to craft Frost Shields but already holds 37,000 makes none (and does not fish the chests it would have opened for them). The directly demanded items get no such credit, since their shortfall is already measured against what you hold — mastery progress for the tower and `-m` goals, the current count for `-i`.

A few materials (Iron and Nails) are supplied automatically by the game whenever a craft needs them, so they are never counted, gathered, or expanded.

Corn Seeds and Sunflower Seeds (used by the Pinecone Bird Feeder) are bought from the Country Store for Silver. The solver treats them as free, but the plan lists how many to buy under **Buy (at the Country Store)**, net of any you already hold.

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

A material that is really obtainable in game by a route the solver does not model is flagged with that route, from [`unmodelled.csv`](unmodelled.csv) (see [Mastery costs](#mastery-costs)), so the bare names are the ones still lacking any known source:

```text
The following materials are required for some crafts but are unobtainable:
  Esperium
  Fairy Crystal (Mining not modelled)
  White Sugar (Sugar Cane Mill not modelled)
```

## Mastery costs

Pass `--mastery-costs` to see, for **every** item you have not finished mastering, how much effort each remaining tier still takes. For each masterable item and each tier it has not yet reached — **Master** (10,000 mastery), **Grandmaster** (100,000) and **Megamaster** (1,000,000) — the solver finds the single *fastest* plan to get there from your current progress and reports that plan's makespan and the **gold it spends buying** (the consumables it purchases at the [trading](#trading-gold-prices) exchange rates; a tier it self-sources entirely — a farmed crop, say — costs no gold). Each tier is measured cumulatively from where the item stands now, so a Grandmaster row already includes the work Master needed. It reads your mastery and inventory files like a normal run (the catalogue supplies every masterable item, so items you have never touched appear at progress 0), but solves its own per-item goals instead of the tower/`-m`/`-i` demand, and so conflicts with `--ascend`, `--unobtainable`, `--target`, `-m` and `-i`.

The rows are ranked by `--sort time` (the default) or `--sort gold`, each falling back to the other as a tie-breaker:

```text
Mastery costs (Fastest plan to reach each remaining tier ... sorted by gold (then time).)
  Item                Target   Time         Cost
  Ship in a Bottle    M          5d          10 g
  Valve               GM         2d         100 g
  Ship in a Bottle    GM        50d         105 g
  ...
```

Many items master **instantly** (`0m`) when the fastest plan can just buy the Arnold Palmers and Large Nets a channel burns and explore/fish the item on the spot — there the whole cost is the gold. A non-zero time appears only where the item is gated on farming or passive production that no purchase can shorten (a slow crop, or a material only a building makes), which is exactly what you would have to wait out.

Anything the solver **cannot** master — because the item, or a material it needs, has no source it models (not produced, fished, explored, farmed, fetched by a pet, nor buyable/tradeable) — is listed at the end with an explanation. When item A needs B which needs an unsourceable C, the whole chain is shown:

```text
Items that cannot be mastered (a material has no source):
  Cicada (seasonal)
  Compass needs Magna Core (Wishing Well not modelled)
  Crystal Pickaxe needs Fairy Crystal (Mining not modelled), Lapis Lazuli (Mining not modelled)
  Festive Eggnog (Cooking not modelled; seasonal)
  Neigh (Cooking not modelled)
  Steel Pickaxe needs Flarite (which needs Unpolished Flarite (Mining not modelled))
```

Most of these are not really unobtainable at all — they just come from something the solver does not model (cooking, the Wishing Well, mining, the Sugar Cane Mill) or are only available at certain times of year, and saying so is far more useful than a blanket "unobtainable". Those reasons live in [`unmodelled.csv`](unmodelled.csv), one `item,source` row per reason:

```csv
item,source
Neigh,Cooking             # -> "Cooking not modelled"
Magna Core,Wishing Well   # -> "Wishing Well not modelled"
Yule Goat,seasonal        # -> "seasonal"
Gold Feed,unknown         # -> "not modelled" (route known, numbers not)
Lobster,none              # -> "unobtainable" (genuinely)
```

Event items are flagged in [`items.csv`](items.csv) instead (the full game item list, `item,event`): a non-blank `event` column marks an item only available at certain times of year, and both reports print its window — `King Apple (seasonal: September Apple Bobbing Event)`, or just `(seasonal)` while the window is `true` (unknown). The `event` text is free-form and never parsed, so fill it in however suits the event. Keep `seasonal` rows in `unmodelled.csv` for seasonal items outside the museum's Event section only.

An item may have several rows (a seasonal item you also have to cook reads `Cooking not modelled; seasonal`). Naming an item there also stops the chain descending into its recipe — the reason *is* the explanation, and Magna Core's paper recipe is a red herring. Anything not listed keeps the plain `(unobtainable)`, so the file is purely additive: append a row when a new one turns up.

A second, different failure gets its own list: an item whose sources exist but
cannot supply the **quantity** a tier needs — a finitely bounded supply, like a
crop limited by the seeds you hold, or a material you have excluded from
[Arnold-Palmer trading](#trading-resources-for-arnold-palmers). Lower tiers of
that item still appear in the table above; only the tier it cannot stretch to
(and anything above it) is listed here:

```text
Tiers that cannot be reached (sourceable, but not that many):
  Bouquet of Flowers cannot reach Megamaster: its sources cannot supply that many
```

```shell
python main.py --mastery-costs                 # fastest-first
python main.py --mastery-costs --sort gold      # cheapest-first
python main.py --mastery-costs --self-sourced   # never buy AP/LN/AC (see below)
```

### Self-sourcing the tools (`--self-sourced`)

By default the fastest plan buys the Arnold Palmers, Large Nets and Apple Cider a channel burns whenever that finishes sooner. Pass `--self-sourced` to forbid buying those three **tools** — the solver must craft or gather them from your own economy instead, which usually costs time (so the makespan grows) and drops the gold you spend on them to zero. Everything else stays buyable (Orange Juice, cookie sets, and [Arnold-Palmer trades](#trading-resources-for-arnold-palmers) for raw materials), so a plan can still lean on those; where a tier is capped by farming anyway, forbidding the direct tool buy can even push the spend onto a pricier substitute (buying Orange Juice to self-make cider, say), so the flag is "don't buy these tools", not "minimise gold". It applies to the normal frontier too, not just `--mastery-costs`:

```shell
python main.py -m "Butter:1000000" --self-sourced   # climb without buying AP/LN/AC
```

Buying an item never earns its own mastery, so a tier's cost is always the cost of *producing* the item (gathering it, or crafting it from materials that may themselves be bought or traded) — the same rule the sourcing plans follow. The report models the yearly-average calendar (seasonal pets and the Mechanical Heart grants smeared to their steady share), so a single run covers every item quickly regardless of the date.

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
oak = 6000                # 0 (or absent) if you have no Sturdy Saw
have_pine_boost = true    # two pine board a minute instead of one
# Hickory Omelette: a held/bought meal (priced in [trading]) that, for the hour
# it lasts, makes the sawmill deposit +20% of its hourly output every 10 minutes
# -- six deposits, i.e. +120% over the hour (each storehouse-capped). "none",
# "all" (boost oak/board/wood, one omelette per boosted hour), or "only_oak"
# (only ever spend omelettes to speed the scarce Oak; Board and Wood are not
# boosted). See Trading below.
hickory_omelette = "all"

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
oddvocado_trees = 0    # one random fruit each a day at noon, weighted by observed drops:
                       # Avocado > Orange > Lemon > Cherries > Raspberries > Pear > Blueberries

[raptor_pen]    # antlers daily, plus a separate 10% deposit with the snare
antlers = 2617810
have_antler_snare = true

[cow_pasture]    # milk daily
milk = 5000
# has_mariyas_wheelbarrow = true  # held item; unset auto-detects from inventory

[vineyard]    # grapes daily
grapes = 43000

[farmhouse]    # daily stamina for Apple Cider
stamina_cap = 98387               # natural max stamina on registered_on
registered_on = "2026-07-05"
have_mattress_pad = true          # the cap grows +2/day instead of +1
have_apple_pie_enthusiast = true  # daily stamina gain capped at 1M, not 500k
current_stamina = 0               # banked right now; --current-stamina N overrides

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
"Critter" = 8
"Owl" = 8
# ... one line per pet
```

Production arrives in **whole deposits**, and the reported waits respect that: needing 250 Antlers at 100 a day is a 3-day wait (the third deposit lands at day 3), not the continuous-rate 2.5 days. Producers sharing a cadence are phase-staggered across it — the raptor pen and its snare each fill the storehouse 12 hours apart, so one storehouse-worth of Antlers takes 12h, not a day. Plan times, the frontier's makespans and every per-item passive wait in the report land on these deposit boundaries **when passive gathering is what the plan actually waits on**; passive that merely fills another track's wait (the fastest plan leaning on free orchard fruit inside its farming span) keeps its continuous share, since in practice the last deposit's sliver is covered by the channels already co-sourcing those items. Averaged sources (pets, the Mechanical Heart's monthly grants) always keep their smooth continuous rates, since their per-day figure is already a smear over lumpy real events.

Pets are passive producers too. A pet fetches a number of items an hour equal to its level, spread across the item tiers it has unlocked (more tiers unlock at levels 3 and 6), so the solver models each as a daily producer of its average haul — the tier items live in `pets.csv`, and the `[pets]` config gives each pet's level. `use_crunchy_omelette` fetches 50% more. Seasonal pets only fetch for two months a year, so by default a seasonal pet's haul is scaled by how much of the plan actually falls in its window given the start date; with `calendar = "average"` (`--average-calendar`) it is smeared to its steady-state yearly share instead, and with `calendar = "ignore"` (`--ignore-calendar`) seasonal pets drop out of the plan entirely (see [Sourcing plans](#sourcing-plans)). Pets fetch **very slowly**, so the optimiser only relies on one when nothing faster (or cheaper) can supply an item — no special rule is needed; a pet's low rate simply makes any item it alone provides expensive in time, which the frontier reflects.

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
hickory_omelette = 350  # gk; a sawmill-boost meal (see [sawmill] hickory_omelette)
price_stamina = true    # value free stamina at its Orange-Juice equivalent (default)
price_pumpkin_juice = true  # count Pumpkin Juice's cost in net gold (default; see below)
```

The `gk` prices are gold per 1000 units (divided by 1000 for the per-unit gold); cookie sets are gold each. These are the **buyable** consumables. The Hickory Omelette is one of them: with a price set (default 350 gk = 0.35 gold each) and `[sawmill] hickory_omelette` set to `all` or `only_oak`, the solver eats one per boosted sawmill-hour — from your held stock first, then bought — and trades that gold against the faster completion the boost gives (+20% of the hourly output every 10 minutes, so +120% over the boosted hour, each deposit storehouse-capped; a **Sawmill boost** section in the plan shows how many it eats and the extra output). Grape Juice is not traded directly — it is priced at its **production cost** in grapes only so it can be valued (grapes trade for **Arnold Palmers** rather than gold, so `grapes` is quoted in "apk", Arnold Palmers per 1000); it is never bought, always pressed from grapes. With this section present the solver can buy consumables (and the frontier trades makespan against real gold); without it nothing is buyable and every plan is fully self-sourced.

Each plan's gold is shown as **`buy + self-sourced = total`**, and a plan that banks surplus or drains stamina adds **`(bank B, stamina S, net N)`**. The *total* is the market value of every consumable the plan spends; **buy** is the gold the solver actually spends buying them (exactly `plan.gold` — what `--max-gold` caps), and **self-sourced** is the market value of everything it made or gathered instead. The two are exact — the LP decides which consumables to buy — and always sum to the total. **Bank** is the sale value of the surplus the plan produces past everything it consumes (the same figure as the **Surplus** section below), **stamina** is the free-stamina opportunity cost (below), and **net** is `buy − bank + stamina`: the gold you are really out of pocket once those banked materials are sold or spent on other tasks. Net is the axis the frontier trades against time — and what `--cheapest-plan` ranks by — so a slow, passive-leaning plan that banks more than it buys sits at *negative* gold, extending the frontier past the "buy nothing" line.

**Pricing stamina.** Stamina an Apple Cider explore burns is otherwise a free resource, so a plan that leans on it looks cheaper than it really is. With `price_stamina` on (the default), each stamina point is valued at what it would cost to replace by drinking Orange Juice (`orange_juice / 100`, since a juice yields 100 stamina) and folded into the plan's **net** gold, so the frontier prices a stamina-heavy plan honestly — whatever the source: the banked `--current-stamina` you start with, the Farmhouse's daily passive deposit, and **Apples eaten** for stamina are all charged. Only stamina you get by *drinking Orange Juice* is exempt (it is already priced as the juice — charging it again would double-count). Charging eaten Apples matters because they are often near-free byproducts of opening chests; leaving them free let the solver manufacture cheap stamina to dodge the charge entirely. An expanded plan lists the charge in a **Stamina (opportunity cost)** section. Turn it off with `--no-price-stamina` (or `price_stamina = false`) to treat that stamina as free, as before.

**Pricing Pumpkin Juice.** The `pumpkin_juice` price (above) does two jobs. It **gates** juice — the solver drinks one only where it beats what cider/nets would cost for that slice of mastery, so at 230 gold each it uses juice only when the alternative costs more — and it **charges** that cost into the plan's **net** gold like any buy. Those are usually both wanted, but a juice's *real* worth to you can be higher than any single-level price (you are saving it for tower levels not yet released). Set `--no-price-pumpkin-juice` (or `price_pumpkin_juice = false`) to keep the **gate** but drop the **charge**: the LP still prices juice, so it is still only used where it wins, but the reported net gold nets that cost back out — plans are then compared on the *real* gold they spend, with the juice count left as its own column for you to weigh against your own value for it. A row that leaned on juice then shows a **`juice J`** piece in its `(bank …, juice J, stamina …, net N)` note, explaining why net sits below the gold cell (the gold cell still includes the juice buy; only net nets it out). This pairs naturally with **`--spread pumpkin-juice`** (see [Pumpkin Juice](#pumpkin-juice-mastery-boost)): sweep 0..N juices and read off the real gold each one saves, e.g. "20 juices for ~8,400 gold ⇒ ~420 g/juice — worth it, or save them?"

An expanded priced plan also lists a **Buy (with gold)** section (what it purchases and each cost) and closes with a **Surplus** section. Passive production runs until the slowest resource finishes, so everything faster overshoots — a plan waiting weeks on Oak keeps banking straw, stone and wood far past what its goal needs. Assuming you craft those leftovers into the sellable priced goods (Arnold Palmers, Large Nets, Apple Cider, Orange Juice — Grape Juice is not sellable, and cookie sets are baked from held cookies), the solver finds the most gold they are worth, sending shared materials (straw/wood/stone/antlers all feed Large Nets, say) to whichever good pays best, and lists each good with its gold and the nominal total:

```text
Surplus (accumulates past the goal), craftable into sellable goods:
  520,115x Large Net = 5,201 g
  nominal value = 5,201 g
```

It is rigorous for every plan: consumables are always grounded, so a plan self-sources exactly what it needs (net of what it buys) and the leftovers are genuine spare capacity — naturally zero when the binding materials are all used up.

#### Trading resources for Arnold Palmers

Beyond the handful of gold-buyable consumables above, most FarmRPG resources trade between players for **Arnold Palmers** — a finer-grained currency than gold, which only settles in units of 5. Buying a resource this way is often far cheaper than making it (trading for Red Dye beats exploring Whispering Creek and crafting it), so the `[trading.ap]` section turns each such resource into an extra **instant buy** the solver can choose instead of self-sourcing:

```toml
[trading.ap]
enabled = true                # master switch (also --use-trading / --no-trading)
# allow = ["red dye", "grapes"]   # if set, ONLY these trade
# deny  = ["veggie juice"]        # if set, all priced items EXCEPT these; allow/deny are mutually exclusive

[trading.ap.prices]           # AP trade prices in "apk" (Arnold Palmers per 1000), low end of the range
"red dye" = 8
"grapes"  = 5
"stone"   = 1
# … ~110 resources the solver knows (raw materials, crafting inputs, meals) …
"acorn pie" = 5000            # a per-each price stored as apk (5 AP each × 1000)

[trading.ap.price_bounds]     # [low, high] of each quoted range — a guide for the web UI; the solver ignores it
"red dye" = [8, 10]
```

Just because a resource *can* be traded doesn't mean people actually do — some trade only at prices not worth your time, others are hoarded by the same players chasing the same tower goals. The `allow`/`deny` filters (or, in the web app, the **Material trading** pane's per-item checkboxes) let you restrict trading to the ones you would realistically make, so plans lean only on trades you can actually apply. The `[trading.ap.prices]` and `[trading.ap.price_bounds]` blocks are generated together by `scripts/generate_trade_prices.py` from a committed price snapshot — regenerate with the script, don't hand-edit.

Each trade's AP cost is **converted to gold** (at the `arnold_palmer` rate in `[trading]` above — 60 gk → 0.06 g/AP) and folded into the same gold the frontier ranks on, so trading and self-sourcing compete on one axis; no separate AP budget. The solver still weighs it against getting the item free as an explore *byproduct* — an exploring Arnold Palmer yields a whole bundle, so a resource the plan already gathers on the way to something else is never paid for twice. The section is `[trading.ap]` rather than `[trading.arnold_palmer]` only because the latter would collide with the `arnold_palmer` gold-price key.

**Trading earns no mastery** (like any purchase): a trade may cover a material/tool use, or a plain **`-i` count** goal, but **never an item's own `-m` mastery** goal, which must still be crafted or gathered. The five gold-buyable consumables are left gold-only (no duplicate AP channel). A priced plan lists what it trades in a **Trade (for Arnold Palmers)** section — each resource with the Arnold Palmers it costs and their gold value, plus a total — so you can see how many Arnold Palmers the plan needs:

```text
Trade (for Arnold Palmers):
  5,000x Red Dye = 40 AP (2 g)
  total 40 AP (2 g)
```

The price table is generated from a captured community price list by `scripts/generate_trade_prices.py` (offline, from a committed snapshot); regenerate with it rather than hand-editing en masse. Ascension mode does not trade.

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
use_neigh = true                 # then ×0.8 stamina (the Neigh meal)

# have_fungus_processing = true  # force Gary's Crushroom's drop table on/off;
                                  # unset auto-detects the artifact from inventory
# have_swamp_puzzle = true       # force the Sinking Swamp (exploring AND fishing)
                                  # open/locked; unset auto-detects from inventory
# have_mechanical_heart = true     # force the T300 monthly cider/palmer grants
                                  # on/off; unset auto-detects from having T300

[exploring.effectiveness]        # per-zone Exploring Effectiveness for cider
Forest = 136
"Small Cave" = 104
# ... one line per explorable zone
```

Each Apple Cider does `1000 + 10 × Effectiveness` explores in a zone (`1250 + 12.5 ×` with Cinnamon Sticks), each costing one stamina before the multiplicative Wanderer and Neigh reductions. A zone with no configured Effectiveness gets no cider offer.

#### Swamp Puzzle (the Sinking Swamp lock)

The **Sinking Swamp** is locked until you hold the **Swamp Puzzle**: without it you can neither explore nor fish there, so the solver drops the zone from both channels entirely and anything it alone drops becomes unsourceable. That matters well past the swamp's own loot — Scrap Cloth is swamp-only, and so is the cloth chain behind the **Basic Pillow** that Tower level 301 needs, which caps the climb at level 300 for a player without the puzzle.

The puzzle is a held item, so it is auto-detected from your inventory file (its own pieces drop nowhere the solver models, so there is nothing for a plan to craft its way to). Set it explicitly to plan for a state you're not in yet:

```toml
[exploring]
# have_swamp_puzzle = true  # force the swamp open/locked; unset auto-detects
                            # from inventory. Gates the FISHING zone too --
                            # there is no [fishing] counterpart.
```

The `[farmhouse]` section is the passive stamina source. The building raises your natural stamina cap by one a day (two with the Mattress Pad perk), so the cap recorded on `registered_on` has grown that much per day since (worked out for the plan's start date, like the storehouse). Each day your actual stamina then rises by `8 × cap`, limited to 500,000 (1,000,000 with the Apple Pie Enthusiast perk) — an automatic daily deposit the plan draws on before eating Apples or drinking Orange Juice for the rest. Stamina is a stat, not a stored good, so the storehouse cap does not apply to it.

`current_stamina` sits in the same section but is a different kind of value: not a property of the building, just how much stamina you happen to have banked right now, seeding the Apple Cider channel before any is gained. It goes stale the moment you explore, so `--current-stamina N` (or the web app's **Current stamina** box) still overrides it per run — including an explicit `0`, for "none right now" against a config value you haven't updated.

## Farming

Farming grows items from seeds. A seed's harvest is a *distribution* of items — mostly its primary crop, occasionally a rarer drop — so one crop can supply several different items. The model has three parts:

- `crops.csv` — each crop's base growth time, and (via an optional `Primary` column) the crop a differently-named seed grows, such as a *Mega* seed.
- `seed_drops.csv` — each seed's in-game drop table (`Crops/harvest all` for common items, `Harvest alls/crop` for rare ones), captured at a fixed row count and normalised to a per-row rate (a row is four seeds).
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

A seed whose `Primary` differs from its crop's name — the *Mega* seeds and *Spring Seeds* — is **limited**: unlike ordinary seeds (bought from the store in any quantity, so not modelled) it cannot be readily replaced, so each planting consumes held seeds from your inventory (one per plot, four per row) and the plan can never plant more than you hold plus whatever passive production or chest openings supply (the Hummingbird pet makes Spring Seeds; a Bag of Spring Seeds opens into 25). The seed spend appears in the plan's *From inventory* section, and a goal that would need more than the seeds can ever grow is reported as unsatisfiable rather than silently planned. Spring Seeds each grow ten flowers, split evenly in expectation across Red, Blue, White, Orange and Yellow Flowers; their nominal primary, "Mixed Flowers", is just the seed description's collective name and the Scythe of Dewstar never picks it.

Growth can be sped up with **Grape Juice**, which grows the whole field instantly but only `grape_juices_per_day` times a day — a hard daily quota the solver enforces across the whole plan (see [Cookies](#cookies)), on top of having to actually source the juice. Because the rows grow one crop at a time, whichever crop is being farmed gets the full daily allowance, adding those instant harvests on top of natural growth — a big saving for slow crops (a nine-day Beet gains far more from a free daily harvest than a fast crop does) and a small one for quick ones. When a plan farms a crop, the report notes the Harvest Alls (full-field harvests), the wait, how many Grape Juices it consumes, and how many of each item those harvests produce — split between the instant Grape Juice grows and normal farming:

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

That five-minute window is also the **best home for your daily Grape Juice**, and it is effectively instant — but it can only be done **once a day**. So each qualifying crop also gets an instant **cookie burst** action: the window's boost alone (its handful of boosted harvests plus that day's Grape Juice grows, ~9× each), for one cookie set, at no real wait. The solver uses it to grab a *sliver* of a boosted crop right now rather than model a fraction of a whole day (which used to read as a spurious few-minute "wait"). The day's remaining *un*-boosted natural harvests stay on the ordinary `grow` offer. A burst shows as `cookie burst of <crop>` on the critical path; several show as `N cookie bursts of <crop>`, one on each of N days.

**Cookie sets and Grape Juice are daily budgets**, and the solver treats them as such: what a plan may spend of each is set by how many days it runs, not by how many crops it grows. One set and one day's juice allowance a day, shared across the whole farm — so a second burst, whether of the same crop or a different one, has to come out of another day. Days are counted **whole**: a plan running 6d 4h touches seven days, so it collects seven sets, and an eighth means running a seventh full day. (This matters more than it sounds: bursting a crop you don't want still yields a little of what you do want, via the Scythe of Dewstar's bonus crops, so without a shared budget the solver would burst every qualifying crop at once and collect the scraps — 16 sets and 211 juices in six days, on a real save, for a few percent more yield.) Holding a large stock does not lift either quota: you can only drink the day's juice, however many are in the cupboard. Because the actions themselves are near-instant, a plan's reported time includes the days it spends waiting to collect the sets it spends.

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

### Mechanical Heart

The **Mechanical Heart** (the **Tower level 300** artifact) hands you a bulk of consumables on a fixed day of every month: **3000 Apple Cider** on the 7th and **2000 Arnold Palmer** on the 14th. Like the Scythe of Dewstar it is an account perk, not a held item, so it is assumed active once every mastery requirement through Tower level 300 is met:

```toml
[exploring]
# have_mechanical_heart = true  # force on/off; unset auto-detects from having T300
```

These grants join passive production as free supply of those consumables, spread to an average daily rate over the plan (like a pet's averaged haul). By default that rate counts exactly how many grant days — 7ths and 14ths — fall inside the plan's window given its start date, so a short plan that lands on the 7th sees the cider while one that misses it does not; with `calendar = "average"` (under `[solver]`, or `--average-calendar` on the command line) it instead uses the steady-state monthly average, and with `calendar = "ignore"` (`--ignore-calendar`) the grants drop out entirely. A plan then leans on the granted ciders and palmers before crafting, gathering or buying more, and the passive breakdown lists them under the `Mechanical Heart` producer, flagged `(smeared)` as a reminder that the figure is the monthly lump spread to a daily rate, not what actually lands on the day.

## Web app

The same solver is also available as a small Django web app (`web/`) — paste
your masteries and inventory into two boxes in a browser instead of files on
disk, adjust the same goal/limit/pumpkin-juice/calendar controls the CLI
flags expose, and get back the frontier table and a plan breakdown. Everything
except the **Goal** lives behind the **⚙ Settings** button in the top-right
corner: the paste boxes, the limits and every preference pane. Edits there
take effect when you press **Save** (**Cancel**, **Escape** or a click outside
discards them), so pasting a save or retyping a handful of prices costs one
solve rather than one per keystroke; the Goal itself still re-solves as you
type. There are no accounts and nothing is stored server-side: your pasted text
and parameter choices live in the browser's `localStorage` only, resent with
every request (and the dated mastery snapshots behind the
[timeline page](#mastery-timeline-page) live in the same browser's IndexedDB).
The only thing that ever leaves the browser is a
[backup file](#backup-and-restore) you ask for.
A solve takes roughly the same ~0.5s a CLI run does on capable hardware (see
"Server hardware reality" below); a *refilter* — changing a limit or which
plan is expanded, without changing what needs solving — is answered from an
in-process cache in a few milliseconds.

Most of the `config.toml` settings have their own controls too, in collapsible
panes inside that Settings popover: Fishing (the Large Net perks), Exploring
(the Arnold Palmer and Apple Cider perks, plus the Fungus Processing / Swamp
Puzzle unlocks), Exploring
effectiveness, Farming, Pets, Buildings, Trading values, and a
**Material trading** pane listing every resource that can be traded for Arnold
Palmers with a checkbox (trade it or not) and an editable price (the going-rate
range shown greyed as a guide). Untick the ones you wouldn't realistically
trade; **Enable all** / **Disable all** / **Restore defaults** reset the list
(Restore returns it to the server config's own allow/deny). Anything without its
own control is still reachable via the Advanced panel's raw `--override` box.

### Mastery costs page

The **Mastery costs** link in the header opens `/masteries`, the browser face
of [`--mastery-costs`](#mastery-costs): every mastery you have left, ranked by
how long the fastest plan to finish it takes and what that plan spends. It
reads the same saved state the solver page uses, and carries the same **⚙
Settings** popover — so you can paste there and change any setting without
leaving the page; saving re-solves the table and the solver page picks the same
settings up (your goal there is left alone).

- **Sort by** Time or Cost (the other breaks ties). Sorting is instant: it
  reorders the rows already loaded rather than re-solving.
- **Self-source tools** is `--self-sourced`: never buy Arnold Palmers, Large
  Nets or Apple Cider, make them instead. Costs drop to what you would really
  spend; plans usually get longer.
- **Cannot be mastered**, collapsed at the bottom, lists the items no plan can
  reach, each with the reason — e.g. `Compass needs Magna Core (Wishing Well not
  modelled)`, `Neigh (Cooking not modelled)`, `Yule Goat (seasonal)`. See
  [`unmodelled.csv`](unmodelled.csv) above. **Tiers that cannot be reached** is
  the second collapsed list: items that *can* be sourced, just not in the
  quantity that tier needs.
- **Clicking a row** opens the solver page for that one mastery, seeded exactly
  as `-m <Item>:<tier>` would (no tower goal, every other setting kept), so you
  get the full frontier and plan breakdown for it. The browser's Back button
  returns to the list.
- The small **timeline** link beside each item name opens that item's
  [mastery timeline](#mastery-timeline-page) instead.

Times use the steady-state (yearly-average) calendar, and each row is
cumulative from where you are now — a Grandmaster row includes the work Master
needed.

### Mastery timeline page

The mastery costs page answers "what is left"; the **Timeline** page answers
"how fast am I actually going". Under the Masteries box in **⚙ Settings**
there is a **Save dated snapshot** button: press it whenever you paste a fresh
mastery list, and the app records that list — item by item — in this browser.
`/timeline?item=<Item>` then charts one item across those snapshots, with the
Master / Grandmaster / Megamaster thresholds marked as dotted lines, above a
table of each snapshot's progress, its gain since the one before, and what that
works out at per day. Open `/timeline` with no item (the **Timeline** link in
the header) for the list of everything you have history for, with its tier,
where it stands now, and what it gained over the last week and the last day.

- Those two **Change** columns are windows measured back from now, not totals
  since your first snapshot: each compares where the item stands now with where
  it stood when the window opened. A dash means the window has no answer —
  nothing recorded since it opened, or no earlier snapshot to measure from —
  rather than no progress.
- Beside each item name is a link to the **solver, seeded with that item's next
  goal**: the tier above where it stands now (`→ GM`), or `→ 10,000` for one you
  have already Megamastered — there is no tier left to aim at, and a mastery
  goal you have already passed would plan nothing, so it asks for 10,000 of the
  item instead — an item goal (`-i`), so whatever you already hold counts
  towards it. Clicking the row itself still opens that item's chart.
- **Filters** sit above that list. **Tier** picks which of Not mastered / M /
  GM / MM to show; MM is off by default, since those are finished.
  **Made from** answers "I have a pile of this — what could it become?": add
  Straw and the list narrows to everything Straw can end up as, following the
  recipes the whole way (Twine, then Rope and everything made from Rope, and
  so on). Add as many materials as you like — anything reachable from any of
  them is listed — and **Add a zone's drops** fills them in from an exploring
  location or fishing zone in one go, so you can see what a trip there could
  turn into. Only drops that are a material for something are added. **Clear
  all** empties the list, and both filters are remembered for your next visit.

- **One snapshot per day.** Each day keeps its last snapshot, so pressing the
  button again after another hour's play replaces that day's reading with the
  newer one rather than recording a second point — the chart is one point a day,
  and a year of history stays small.
- **The button saves immediately.** Unlike every other control in the popover,
  it does not wait for **Save** and is not undone by **Cancel** — a snapshot is
  history, not a setting. It does not re-solve anything either.
- **The chart's scale follows the data, and you can override it.** It runs from
  zero to the next tier while an item's movement fills at least half of that;
  below that the chart zooms to the data instead, and shows only the tier lines
  that fall inside. The button above the chart's top-right corner switches
  between the two — **Show next tier** for the tier context, **Zoom to data**
  for the shape. A
  zoomed axis is rounded out to tidy bounds — an item running 212,401 → 333,631
  is drawn 210k to 350k — so the labels are figures you can reason about. The
  axis always gives real figures, and the table has the exact numbers.
- **The chart can be narrowed to a recent stretch.** **1d / 7d / 1m / 1y / All**
  above the chart's top-left corner restrict it to the last day, week, 30 days
  or 365 — each reaching back one reading further, so 1d draws a single day's
  climb and 7d runs Monday to Monday rather than stopping a day short. A window
  with no snapshots in it is greyed out, the table below always lists every
  snapshot, and both this and the scale are remembered for your next visit.
- **The Master line bows out when it is in the way.** On a chart whose scale
  reaches Megamaster, the Master threshold sits at 1% of the height, hard
  against the axis, so only the Grandmaster and Megamaster lines are drawn.
- **Gaps are gaps, not zeroes.** A snapshot taken before you started an item
  simply has no point for it, rather than a drop to zero.
- **This history lives in your browser only** (in IndexedDB, not the
  `localStorage` the pasted state uses — snapshots are far too big for that).
  It is never sent to the server, and clearing the site's data loses it. To
  carry it (and everything else) to another browser, use **Backup** below.

### Backup and restore

Nothing is stored on a server, so a different browser or machine starts empty
and there is no account to log back into. At the bottom of the **⚙ Settings**
popover:

- **Back up to file** downloads one JSON file —
  `farm-solver-backup-2026-07-31.json` — holding your pasted masteries and
  inventory, your goal, every option and preference, and your whole mastery
  history. It captures what is on screen, so a paste you have not pressed
  **Save** on yet is still in the file.
- **Restore from file…** reads one back. It asks first, because the game state
  and options in this browser are replaced; your mastery history is **merged**,
  so restoring on a browser that already has snapshots keeps both sides
  (snapshots taken at the same instant are the same snapshot). The page reloads
  when it is done and re-solves with what it found.

A file that is not a backup — or one written by a newer version of the app, or
one whose contents have been mangled — is refused with a reason, and nothing in
the browser is touched.

### Running it locally

```shell
uv sync --group web
uv run --group web python web/manage.py runserver
```

Then open `http://127.0.0.1:8000/`. `web/manage.py` puts the repo root on
`sys.path` itself, so no packaging or install step is needed for `main.py`/
`webapi.py`/`optimizer.py`/etc. to be importable from `web/solverapp/`.

Its own test suite runs separately from the main one, since it needs Django
installed and a running Django test harness:

```shell
uv run --group web python web/manage.py test solverapp
```

(`uv run pytest -q` never collects these — they live outside `tests/` and
are named `tests.py`, not `test_*.py` — so the plain test suite needs no
Django dependency at all.)

### Per-player config caveat

`config.toml` (pets, buildings, crafting/exploring/fishing effectiveness,
trading prices — everything under [Configuration](#configuration) above) is
normally *per player*: the CLI reads whatever `config.toml` sits next to
`main.py`. The web app is a single running server shared by everyone who
uses it, and this first iteration serves **one** `config.toml` — the
server's own — to every visitor; it does not (yet) let each player supply
their own. If your setup differs from the server's checked-in config (a pet
you don't have, an effectiveness perk you haven't unlocked, different
trading prices), the escape hatch is the same `--override
section.flag=value` mechanism the CLI has, exposed in the web UI's Advanced
panel as one `section.flag=value` override per line — anything you could
pass as a repeated `--override` flag on the command line works there too.

### Deploying it

For anything beyond local use, run it behind gunicorn with a reverse proxy
handling TLS (gunicorn itself only speaks plain HTTP):

```shell
uv run --group web gunicorn \
    --chdir web \
    --workers 2 --worker-class gthread --threads 4 \
    --bind 127.0.0.1:8000 \
    farmweb.wsgi:application
```

Two gthread workers × 4 threads keeps cache-hit (refilter) requests
responsive even while a worker's solve semaphore is held by an in-flight
solve elsewhere in that same process — `solverapp/cache.py`'s
`SOLVE_SEMAPHORE` limits each *process* (not thread) to one LP solve at a
time, since PuLP/HiGHS hold solver state that is not safe to touch from two
threads at once. The session cache (`solverapp/cache.py`'s `SessionCache`,
an in-process LRU(64)/TTL(30 min) `OrderedDict`) is per-worker-process, not
shared, so a request that lands on a different worker than the one that
solved it is simply a cache miss — it just re-solves, never returns a wrong
answer, at the cost of ~0.5–3s instead of a few milliseconds. This is a
deliberate simplicity trade-off for a small deployment; there is no sticky
routing or shared cache to keep consistent.

Before deploying, run `web/manage.py collectstatic` so whitenoise
(`WhiteNoiseMiddleware`, already wired into `MIDDLEWARE`) can serve
`app.js`/`style.css` straight out of `web/staticfiles/` without Django's
per-request static-file machinery:

```shell
uv run --group web python web/manage.py collectstatic --noinput
```

Set these environment variables in production (see
`web/farmweb/settings.py`; the checked-in defaults are dev-only and unsafe
to deploy as-is):

```shell
DJANGO_SECRET_KEY=<a long random string>
DJANGO_DEBUG=false
DJANGO_ALLOWED_HOSTS=your-domain.example
```

### Container image and Cloud Run

The `Dockerfile` packages the gunicorn recipe above, and
`deploy/cloudrun.sh` puts it on Google Cloud Run:

```shell
./deploy/cloudrun.sh                    # uses the current gcloud project
REGION=us-east1 ./deploy/cloudrun.sh
```

Cloud Run suits this app because the app is *stateless* — `settings.py`
sets `DATABASES = {}` and keeps no server-side player data (each browser
holds its own pasted state in localStorage), so there is nothing to mount,
migrate or back up and the image is the whole deployment. It is also idle
almost all the time and bursty when used, which is what per-request billing
plus scale-to-zero is priced for: a small deployment sits inside the
always-free monthly allowance (2M requests, 180k vCPU-seconds, 360k
GiB-seconds), which applies only in `us-central1`, `us-east1` and
`us-west1`.

The image builds dependencies with `uv sync --frozen --no-dev --group web`
against the checked-in `uv.lock`, runs `collectstatic` so whitenoise serves
`web/staticfiles/` directly, and byte-compiles the tree — `main.py`,
`optimizer.py` and `report.py` are ~435 KB of source between them, which a
scale-to-zero service would otherwise recompile on every cold start. Two
build-time gates fail the build rather than the first request: an import of
`optimizer` that pins the HiGHS backend (the same check
`solverapp.apps.SolverappConfig.ready` makes before it will start, and note
that PuLP's CBC fallback has no bundled binary on arm64 — prefer x86_64),
and `manage.py check`.

`DJANGO_DEBUG=false` is baked into the image, not left to the deploy
environment: `settings.py` defaults `DEBUG` to *true* for local use, and
with it on in production `views.index` renders the CLI-command popup and
`webapi.capture_debug_state` writes every visitor's pasted mastery and
inventory text to the container's temp dir. `DJANGO_ALLOWED_HOSTS` ships as
`.run.app` (Django reads a leading dot as a subdomain wildcard) so the first
deploy can serve before its own URL is known; the script then narrows it to
the exact hostname.

`deploy/gunicorn.conf.py` deliberately differs from the two-worker recipe
above, because a Cloud Run instance is sized in whole vCPUs and billed by
the vCPU-second. It runs **one worker per vCPU** — `SOLVE_SEMAPHORE` already
caps each process at one solve, so a second worker on a 1-vCPU instance buys
no throughput and just puts two frontier sweeps on one core — and scales out
horizontally instead. Threads stay at 4 so a cache-hit refilter does not
queue behind someone else's re-solve, `max_requests` is 0 so a recycled
worker never throws away its `SessionCache`, and `preload_app` is off to
avoid forking a process that holds live HiGHS state.

Two things to set on a public deployment. `api_solve` and
`api_mastery_costs` are `csrf_exempt` and unauthenticated — there is no
login to gate them — and each POST buys 1–3s of CPU, so anyone who finds the
URL can spend your quota: keep `--max-instances` small (the script defaults
to 3) and add a billing budget alert. The script's `--concurrency 4` is
sized against `_SEMAPHORE_TIMEOUT_SECONDS` (a queued request has 20s to
acquire the solve semaphore); lower it if you see 503s.

The script also asserts `deploy/artifact-cleanup-policy.json` on the
`cloud-run-source-deploy` repository each run. Every `--source` deploy
pushes a fresh ~150 MB image and they accumulate forever, so roughly four
deploys crosses Artifact Registry's 500 MB free tier and starts billing
storage — and that spend sits *outside* a Cloud Run spend cap, which
neither counts nor stops it. The policy keeps the 3 most recent versions
and deletes anything else over 30 days old; because Artifact Registry lets
Keep win over Delete, a rollback target always survives.

### Server hardware reality

The solver's own README/ARCHITECTURE performance figures (a solve in
~0.4–0.7s) come from a fast desktop-class CPU. The frontier sweep is ~80
*sequential* single-threaded LP solves, so a typical cloud VPS core — often
1.5–4× slower single-thread than a top-tier desktop chip — should budget
**~1–3s per re-solve** instead. Benchmark the actual target host before
tuning anything (`time python main.py` with a representative mastery/
inventory pair is a reasonable proxy): the web UI shows an explicit pending
state on a re-solve, and the solve-semaphore acquire timeout
(`_SEMAPHORE_TIMEOUT_SECONDS` in `solverapp/views.py`) is sized for that
budget rather than the desktop figure.

A measured data point, from the first real Cloud Run deployment
(`us-central1`, 1 vCPU): a fresh-account tower solve that takes **0.61s** on
an Apple-silicon desktop takes **~3.5s** there — about 5.7× slower, and
above the 1–3s guess above. Still comfortably inside the 20s semaphore
budget, but budget nearer 4s than 2s when sizing anything against a
shared cloud vCPU. A cache-hit refilter on the same box is ~40ms.
