from datetime import timedelta

from crops import Farm
from main import cheapest_plan, print_frontier, print_plan
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
    print_plan(plan, demand, Farm(grape_juices_per_day=14))
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


def test_print_plan_orders_passive_items_longest_wait_first(capsys):
    demand = {"Slow": 100, "Fast": 100}
    offer = Offer("passive", "passive", {"slow": 1, "fast": 10}, time_per_unit=timedelta(minutes=1))
    plan = SourcingPlan(draws=[Draw(offer, 100)])
    print_plan(plan, demand, Farm())
    lines = [line.strip() for line in capsys.readouterr().out.splitlines() if "in " in line]
    # Slow (100 min) is a longer wait than Fast (10 min), so it is listed first.
    assert lines[0].endswith("Slow in 1h 40m")
    assert lines[1].endswith("Fast in 10m")
