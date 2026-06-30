# Farm Solver

This is an attempt to find the optimal way to spend resources to climb the tower in FarmRPG.

Farm Solver loads in the current state of your mastery and inventory and calculates how many Arnold Palmers and Large Nets are required to get the mastery levels required to climb the tower.

## Usage

Run the script passing the masteries and inventory copied and pasted from with FarmRPG directly into a text file.

``` shell
python main.py [-t <tower-requirements>] [-l level] [-i item:amount] [-m item amount] [-r] [--crafting-bonus num] [--mastery-bonus num] <masteries> <inventory>
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

By default, materials that are themselves craftable (for example a chair made from wood) are reported as-is. Pass `-r`/`--recursive` to expand them into their own materials, all the way down to the base, uncraftable ones:

```
python main.py -r sample-mastery.txt sample-inventory.txt
```

When expanding recursively, counts are kept as fractions through the whole crafting tree and only the final totals are rounded, so rounding errors do not accumulate level by level.
