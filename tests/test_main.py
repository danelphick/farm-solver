from datetime import timedelta

from crafting import Workshop, WorkshopRecipe
from crops import Farm
from main import cheapest_plan, print_crafts, print_frontier, print_plan, within_limits
from optimizer import SourcingPlan
from sources import Draw, Offer

PLANS = [
    SourcingPlan(time=timedelta(0), arnold_palmers=100, large_nets=20),
    SourcingPlan(time=timedelta(hours=5), arnold_palmers=10, large_nets=0),
    SourcingPlan(time=timedelta(days=3), arnold_palmers=0, large_nets=0),
]


def test_cheapest_plan_picks_the_fewest_consumables():
    # The last plan spends nothing, so it is the cheapest despite being slowest.
    assert cheapest_plan(PLANS) == 2


def test_cheapest_plan_breaks_ties_on_time():
    plans = [
        SourcingPlan(time=timedelta(hours=2), arnold_palmers=0, large_nets=0),
        SourcingPlan(time=timedelta(hours=1), arnold_palmers=0, large_nets=0),
    ]
    assert cheapest_plan(plans) == 1


def test_within_limits_checks_time_and_consumables():
    plan = SourcingPlan(time=timedelta(days=2), arnold_palmers=500, large_nets=50)
    assert within_limits(plan)  # no limits
    assert within_limits(plan, time_limit=timedelta(days=3), max_ap=500, max_ln=50)
    assert not within_limits(plan, time_limit=timedelta(days=1))  # too slow
    assert not within_limits(plan, max_ap=499)  # too many palmers
    assert not within_limits(plan, max_ln=49)  # too many nets


def test_print_frontier_tags_fastest_selected_and_cheapest(capsys):
    print_frontier(PLANS, selected=2, cheapest=2)
    lines = [line.rstrip() for line in capsys.readouterr().out.splitlines()]
    assert "   1. 0m / 100 AP / 20 LN  [fastest]" in lines
    assert " > 3. 3d / 0 AP / 0 LN  [cheapest]" in lines


def test_print_plan_renders_each_channel_and_the_remainder(capsys):
    demand = {"Drum": 500, "Bone": 100, "Corn": 1000, "Board": 2000}
    plan = SourcingPlan(
        draws=[
            Draw(Offer("fish Small Pond", "fishing", {"drum": 100}, ln=1), 5),
            Draw(Offer("explore Cave", "exploring", {"bone": 50}, ap=1), 2),
            Draw(
                Offer("grow Corn", "farming", {"corn": 40}, time_per_unit=timedelta(hours=1)),
                25,
            ),
            Draw(
                Offer("passive", "passive", {"board": 10}, time_per_unit=timedelta(minutes=1)),
                200,
            ),
        ],
        remainder={"Magicite": 3},
    )
    print_plan(plan, demand, Farm(grape_juices_per_day=14), Workshop(), 1.45)
    out = capsys.readouterr().out

    assert "Fishing:" in out
    assert "5x Large Net at Small Pond (Drum)" in out
    assert "Exploring:" in out
    assert "2x Arnold Palmer at Cave (Bone)" in out
    assert "Farming:" in out
    assert "plantings of Corn" in out
    # Board at 10/min needs 200 min = 3h 20m, shown per item under passive.
    assert "Passive production (concurrent):" in out
    assert "2,000x Board in 3h 20m" in out
    # Items with no offer to make them are listed as a remainder.
    assert "3x Magicite" in out


def test_print_plan_passive_excludes_items_covered_elsewhere(capsys):
    demand = {"Horn": 1000}
    plan = SourcingPlan(
        draws=[
            # Exploring makes all 1000 Horn, so passive has nothing left to add.
            Draw(Offer("explore Canyon", "exploring", {"horn": 100}, ap=1), 10),
            Draw(
                Offer("passive", "passive", {"horn": 1}, time_per_unit=timedelta(minutes=1)),
                50,
            ),
        ],
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.45)
    out = capsys.readouterr().out
    assert "10x Arnold Palmer at Canyon (Horn)" in out  # Horn comes from exploring
    assert "Passive production" not in out  # nothing left for passive to cover


def test_print_plan_passive_shows_only_the_residual(capsys):
    demand = {"Horn": 1000}
    plan = SourcingPlan(
        draws=[
            # Exploring covers 600; passive must make the remaining 400.
            Draw(Offer("explore Canyon", "exploring", {"horn": 100}, ap=1), 6),
            Draw(
                Offer("passive", "passive", {"horn": 1}, time_per_unit=timedelta(minutes=1)),
                400,
            ),
        ],
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.45)
    out = capsys.readouterr().out
    assert "Passive production (concurrent):" in out
    assert "400x Horn in 6h 40m" in out  # 400 at 1/min = 400 minutes


def test_print_crafts_lists_parents_before_children(capsys):
    workshop = Workshop(
        recipes={
            "hammer": WorkshopRecipe("Hammer", {"Board": 2, "Iron": 1}),
            "board": WorkshopRecipe("Board", {"Wood": 5}),
        }
    )
    plan = SourcingPlan(crafts={"board": 10, "hammer": 5})
    print_crafts(plan, workshop, crafting_bonus=1.0, mastery_bonus=1.0)
    out = capsys.readouterr().out
    assert "5x Hammer (Produces 5, Adds 5 Mastery)" in out
    assert "10x Board (Produces 10, Adds 10 Mastery)" in out
    assert "50x Wood" in out  # 10 Board crafts * 5 Wood
    assert "Iron" not in out  # auto-supplied, never shown
    assert out.index("Hammer") < out.index("Board")  # parent before child


def test_print_plan_orders_passive_items_longest_wait_first(capsys):
    demand = {"Slow": 100, "Fast": 100}
    offer = Offer("passive", "passive", {"slow": 1, "fast": 10}, time_per_unit=timedelta(minutes=1))
    plan = SourcingPlan(draws=[Draw(offer, 100)])
    print_plan(plan, demand, Farm(), Workshop(), 1.45)
    lines = [line.strip() for line in capsys.readouterr().out.splitlines() if "in " in line]
    # Slow (100 min) is a longer wait than Fast (10 min), so it is listed first.
    assert lines[0].endswith("Slow in 1h 40m")
    assert lines[1].endswith("Fast in 10m")
