# Farm Solver

This is an attempt to find the optimal way to spend resources to climb the tower in FarmRPG.

Farm Solver loads in the current state of your mastery and inventory and calculates how many Arnold Palmers and Large Nets are required to get the mastery levels required to climb the tower.

## Usage

Run the script passing the masteries and inventory copied and pasted from with FarmRPG directly into a text file.

``` shell
python main.py [-t <tower-requirements>] [-l level] [-i item:amount] [-m item amount] <masteries> <inventory>
```

By default it will assume you want to reach the highest possible level, but you can override this with `-l`. Different tower requirements can be passed in with `-t` to be used rather than the builtin ones.

The solver can also ignore the tower and solve for mastering a particular item using `-m`. E.g.

```
python main.py -m "Flying Machine:100000"
```

You can pass multiple items by comma-separating the requirements. E.g.

```
python main.py -m "Flying Machine:100000,Gainite:100000"
```

Rather than masterying an item, it can solve for acquiring a certain number of an item using `-i` (including what's already in your current inventory).

```
python main.py -i "Flying Machine:2000,Gainite:20"
```
