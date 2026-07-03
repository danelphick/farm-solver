from datetime import timedelta

from crafting import Workshop, WorkshopRecipe
from crops import Farm
import pytest

from main import (
    _should_model_calendar,
    build_parser,
    cheapest_plan,
    print_crafts,
    print_frontier,
    print_plan,
    selected_plan,
    within_limits,
)
from optimizer import SourcingPlan
from passive import MINUTE, PassiveProducer, PassiveProduction
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


def test_average_seasons_flag_defaults_off():
    parser = build_parser()
    assert parser.parse_args([]).average_seasons is False
    assert parser.parse_args(["--average-seasons"]).average_seasons is True


def test_should_model_calendar_is_off_by_default():
    # Even with a seasonal pet, the calendar is not modelled unless opted into.
    assert not _should_model_calendar({"Wolf": 8}, average_seasons=False)


def test_should_model_calendar_when_config_opts_in_with_a_seasonal_pet():
    assert _should_model_calendar(
        {"Wolf": 8, "model_calendar": True}, average_seasons=False
    )


def test_average_seasons_forces_the_calendar_off():
    # The CLI flag overrides the config opt-in.
    assert not _should_model_calendar(
        {"Wolf": 8, "model_calendar": True}, average_seasons=True
    )


def test_should_model_calendar_needs_an_actual_seasonal_pet():
    # Opting in with only year-round pets has nothing to model.
    assert not _should_model_calendar(
        {"Dog": 8, "model_calendar": True}, average_seasons=False
    )


def test_selected_plan_defaults_to_the_fastest():
    # No --plan and no --cheapest-plan: the fastest (plan 0, sorted first) wins,
    # even though PLANS[2] is the cheapest.
    assert selected_plan(PLANS, plan=None, cheapest=False) == 0


def test_selected_plan_honours_cheapest():
    assert selected_plan(PLANS, plan=None, cheapest=True) == 2


def test_selected_plan_honours_explicit_number():
    assert selected_plan(PLANS, plan=2, cheapest=False) == 1


def test_selected_plan_rejects_out_of_range_number():
    with pytest.raises(ValueError):
        selected_plan(PLANS, plan=4, cheapest=False)


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
    assert "Farming (1d 1h):" in out  # 25 Harvest Alls * 1h, grown one at a time
    assert "Harvest Alls of Corn" in out
    # Board at 10/min needs 200 min = 3h 20m, shown per item under passive.
    assert "Passive production (concurrent):" in out
    assert "2,000x Board in 3h 20m" in out
    # Items with no offer to make them are listed as a remainder.
    assert "3x Magicite" in out


def test_print_plan_highlights_the_passive_bottleneck(capsys):
    # Passive makes 1 Slow and 10 Fast a minute; 100 of each needs 100 minutes for
    # Slow but only 10 for Fast, so Slow is the critical path that caps the total.
    demand = {"Slow": 100, "Fast": 100}
    offer = Offer(
        "passive", "passive", {"slow": 1, "fast": 10}, time_per_unit=timedelta(minutes=1)
    )
    plan = SourcingPlan(draws=[Draw(offer, 100)])
    print_plan(plan, demand, Farm(), Workshop(), 1.0)
    out = capsys.readouterr().out
    assert "Critical path (sets the 1h 40m total):" in out
    assert "100x Slow from passive production (1h 40m)" in out
    assert "Fast" not in out.split("Critical path")[1].split("Passive")[0]


def test_print_plan_highlights_the_farming_bottleneck(capsys):
    # A single crop grown over 100 hours is the whole makespan, so the critical
    # path names it and notes crops grow one at a time.
    demand = {"Corn": 100}
    offer = Offer("grow Corn", "farming", {"corn": 1}, time_per_unit=timedelta(hours=1))
    plan = SourcingPlan(draws=[Draw(offer, 100)])
    print_plan(plan, demand, Farm(), Workshop(), 1.0)
    out = capsys.readouterr().out
    assert "Critical path (sets the 4d 4h total):" in out
    assert "100 Harvest Alls of Corn (4d 4h) — crops grow one at a time" in out


def test_print_plan_critical_path_prefers_farming_over_passive_fill(capsys):
    # Farming forces a 100-minute wait. Passive runs concurrently and fills that
    # same window making a slow item that could have been sourced elsewhere, so its
    # track ties farming's 100 minutes -- but it is fill, not the constraint. The
    # critical path names farming alone, not the passive item.
    demand = {"Corn": 100, "Slow": 100}
    plan = SourcingPlan(
        draws=[
            Draw(Offer("grow Corn", "farming", {"corn": 1}, time_per_unit=timedelta(minutes=1)), 100),
            Draw(Offer("passive", "passive", {"slow": 1}, time_per_unit=timedelta(minutes=1)), 100),
        ],
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.0)
    out = capsys.readouterr().out
    assert "Critical path (sets the 1h 40m total):" in out
    critical = out.split("Critical path")[1].split("\n\n")[0]
    assert "Harvest Alls of Corn" in critical
    assert "Slow" not in critical  # the passive fill is not the critical path
    assert "passive production" not in critical


def test_print_plan_omits_critical_path_when_everything_is_instant(capsys):
    # Fishing is instant, so there is no time to explain -- no critical path line.
    demand = {"It": 100}
    offer = Offer("fish Lake", "fishing", {"it": 100}, ln=1)
    plan = SourcingPlan(draws=[Draw(offer, 1)])
    print_plan(plan, demand, Farm(), Workshop(), 1.0)
    out = capsys.readouterr().out
    assert "Critical path" not in out


def test_print_plan_farming_splits_produced_by_grape_juice(capsys):
    # 10 plantings at 1h each is 10h; at 12 grape juices a day that is 5 instant
    # grows, half the plantings. Both kinds yield the same bundle, so of the
    # 1,000 Corn produced, 500 come from Grape Juice and 500 from normal farming.
    demand = {"Corn": 1000}
    offer = Offer("grow Corn", "farming", {"corn": 100}, time_per_unit=timedelta(hours=1))
    plan = SourcingPlan(draws=[Draw(offer, 10)])
    print_plan(plan, demand, Farm(grape_juices_per_day=12), Workshop(), 1.0)
    out = capsys.readouterr().out
    assert "10 Harvest Alls of Corn" in out
    assert "using 5 Grape Juice" in out
    assert "1,000x Corn (500 from Grape Juice, 500 from normal farming)" in out


def test_print_plan_farming_heading_sums_the_crop_times(capsys):
    # Crops grow one at a time, so the Farming heading shows their total wait:
    # 2h of Corn plus 3h of Wheat is 5h.
    demand = {"Corn": 100, "Wheat": 100}
    plan = SourcingPlan(
        draws=[
            Draw(Offer("grow Corn", "farming", {"corn": 50}, time_per_unit=timedelta(hours=1)), 2),
            Draw(Offer("grow Wheat", "farming", {"wheat": 100}, time_per_unit=timedelta(hours=1)), 3),
        ],
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.0)
    out = capsys.readouterr().out
    assert "Farming (5h):" in out


def test_print_plan_farming_omits_split_without_grape_juice(capsys):
    # With no grape juice every harvest is natural, so the produced count is shown
    # plainly with no breakdown.
    demand = {"Corn": 1000}
    offer = Offer("grow Corn", "farming", {"corn": 100}, time_per_unit=timedelta(hours=1))
    plan = SourcingPlan(draws=[Draw(offer, 10)])
    print_plan(plan, demand, Farm(grape_juices_per_day=0), Workshop(), 1.0)
    out = capsys.readouterr().out
    assert "1,000x Corn" in out
    assert "Grape Juice" not in out


def test_print_plan_orders_fishing_by_large_nets(capsys):
    # Two zones; the one using more Large Nets is listed first regardless of the
    # draw order, so the biggest consumable spend leads.
    demand = {"Cod": 100, "Eel": 100}
    plan = SourcingPlan(
        draws=[
            Draw(Offer("fish Small Pond", "fishing", {"cod": 100}, ln=1), 1),
            Draw(Offer("fish Big Lake", "fishing", {"eel": 10}, ln=1), 10),
        ],
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.0)
    lines = [line for line in capsys.readouterr().out.splitlines() if "Large Net at" in line]
    assert lines[0].strip().startswith("10x Large Net at Big Lake")
    assert lines[1].strip().startswith("1x Large Net at Small Pond")


def test_print_plan_orders_exploring_by_arnold_palmers(capsys):
    # The location using more Arnold Palmers is listed first.
    demand = {"Ore": 100, "Gem": 100}
    plan = SourcingPlan(
        draws=[
            Draw(Offer("explore Cave", "exploring", {"ore": 10}, ap=1), 10),
            Draw(Offer("explore Ridge", "exploring", {"gem": 2}, ap=1), 50),
        ],
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.0)
    lines = [line for line in capsys.readouterr().out.splitlines() if "Arnold Palmer at" in line]
    assert lines[0].strip().startswith("50x Arnold Palmer at Ridge")
    assert lines[1].strip().startswith("10x Arnold Palmer at Cave")


def test_print_plan_orders_farming_by_time(capsys):
    # Crops are listed longest-growing first, so the farming bottleneck leads.
    demand = {"Corn": 100, "Wheat": 100}
    plan = SourcingPlan(
        draws=[
            Draw(Offer("grow Corn", "farming", {"corn": 50}, time_per_unit=timedelta(hours=1)), 2),
            Draw(Offer("grow Wheat", "farming", {"wheat": 20}, time_per_unit=timedelta(hours=1)), 5),
        ],
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.0)
    lines = [
        line
        for line in capsys.readouterr().out.splitlines()
        if "Harvest All" in line and " in " in line  # the per-crop farming lines
    ]
    assert "Wheat" in lines[0]  # 5h of Wheat outlasts 2h of Corn
    assert "Corn" in lines[1]


def test_print_plan_farming_drops_residue_draws(capsys):
    # A sliver of a planting on a crop nothing needs rounds to zero produced in
    # zero time -- LP residue, not a real action -- so it is omitted entirely.
    demand = {"Corn": 100, "Mushroom": 5}
    plan = SourcingPlan(
        draws=[
            Draw(Offer("grow Corn", "farming", {"corn": 50}, time_per_unit=timedelta(hours=1)), 2),
            Draw(Offer("grow Mushroom", "farming", {"mushroom": 10}, time_per_unit=timedelta(hours=1)), 0.01),
        ],
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.0)
    out = capsys.readouterr().out
    assert "Harvest Alls of Corn" in out
    assert "Mushroom" not in out  # the zero-yield residue draw is not shown


def test_print_plan_fishing_drops_residue_draws(capsys):
    # A sliver of a net whose interesting yields all round to zero produced is LP
    # residue, not a real cast, so the whole line is omitted.
    demand = {"Frost Snapper Shell": 100, "Glass Bottle": 5}
    plan = SourcingPlan(
        draws=[
            Draw(Offer("fish Glacier Lake", "fishing", {"frost snapper shell": 100}, ln=1), 1),
            Draw(Offer("fish Crystal River", "fishing", {"glass bottle": 10}, ln=1), 0.01),
        ],
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.0)
    out = capsys.readouterr().out
    assert "Frost Snapper Shell" in out
    assert "Crystal River" not in out  # the zero-yield residue net is not shown


def test_print_plan_exploring_drops_residue_draws(capsys):
    # Same for a sliver of a palmer that gathers nothing that rounds up.
    demand = {"Moonstone": 100, "Glass Orb": 5}
    plan = SourcingPlan(
        draws=[
            Draw(Offer("explore Ember Lagoon", "exploring", {"moonstone": 100}, ap=1), 1),
            Draw(Offer("explore Crystal River", "exploring", {"glass orb": 10}, ap=1), 0.01),
        ],
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.0)
    out = capsys.readouterr().out
    assert "Moonstone" in out
    assert "Crystal River" not in out  # the zero-yield residue palmer is not shown


def test_print_plan_farming_singular_harvest_all(capsys):
    # A single full-field harvest is "1 Harvest All", not "1 Harvest Alls".
    demand = {"Corn": 50}
    offer = Offer("grow Corn", "farming", {"corn": 50}, time_per_unit=timedelta(hours=1))
    plan = SourcingPlan(draws=[Draw(offer, 1)])
    print_plan(plan, demand, Farm(), Workshop(), 1.0)
    out = capsys.readouterr().out
    assert "1 Harvest All of Corn" in out
    assert "Harvest Alls" not in out


def test_print_plan_passive_names_a_single_source(capsys):
    # One producer makes the item, so it is named plainly with "(from ...)".
    demand = {"Flower": 10}
    plan = SourcingPlan(
        draws=[Draw(Offer("passive", "passive", {"flower": 5}, time_per_unit=MINUTE), 2)]
    )
    production = PassiveProduction(
        producers=[PassiveProducer("Windmill", MINUTE, {"flower": 5})]
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.0, production)
    out = capsys.readouterr().out
    assert "10x Flower in 2m (from Windmill)" in out


def test_print_plan_passive_breaks_down_multiple_sources(capsys):
    # Two producers make the item; the quantity is split by their rate share, on
    # the same line, largest contributor first.
    demand = {"Flower": 100}
    plan = SourcingPlan(
        draws=[Draw(Offer("passive", "passive", {"flower": 4}, time_per_unit=MINUTE), 25)]
    )
    production = PassiveProduction(
        producers=[
            PassiveProducer("Greenhouse", MINUTE, {"flower": 1}),
            PassiveProducer("Windmill", MINUTE, {"flower": 3}),
        ]
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.0, production)
    out = capsys.readouterr().out
    # 3-to-1 rate split of 100 is 75 from Windmill and 25 from Greenhouse.
    assert "100x Flower in 25m (75 from Windmill, 25 from Greenhouse)" in out


def test_print_plan_passive_splits_pets_by_rate(capsys):
    # Two pets return the same item at different hauls; the split follows their
    # rates (3-to-1 of 100 is 75/25), largest share first.
    demand = {"Flower": 100}
    plan = SourcingPlan(
        draws=[Draw(Offer("passive", "passive", {"flower": 4}, time_per_unit=MINUTE), 25)]
    )
    production = PassiveProduction(
        producers=[
            PassiveProducer("pet Fox", MINUTE, {"flower": 1}),
            PassiveProducer("pet Squirrel", MINUTE, {"flower": 3}),
        ]
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.0, production)
    out = capsys.readouterr().out
    assert "100x Flower in 25m (75 from pet Squirrel, 25 from pet Fox)" in out


def test_print_plan_passive_flags_seasonal_pets(capsys):
    # A seasonal pet fetches only part of the year, so its share is a yearly
    # average; the report flags the active window that explains the lower amount.
    demand = {"Flower": 10}
    plan = SourcingPlan(
        draws=[Draw(Offer("passive", "passive", {"flower": 5}, time_per_unit=MINUTE), 2)]
    )
    production = PassiveProduction(
        producers=[PassiveProducer("pet Hummingbird", MINUTE, {"flower": 5})]
    )
    print_plan(plan, demand, Farm(), Workshop(), 1.0, production)
    out = capsys.readouterr().out
    assert "10x Flower in 2m (from pet Hummingbird [seasonal: Mar–Apr])" in out


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
