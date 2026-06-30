from datetime import timedelta

from crafting import CraftStep, Workshop, WorkshopRecipe, craft_plan
from farm_solver import ItemNeed
from passive import HOURLY, PassiveProducer, PassiveProduction, PassiveUse

WORKSHOP = Workshop(
    recipes={
        "bucket": WorkshopRecipe(
            name="Bucket", materials={"Board": 5, "Iron": 1}
        ),
        "board": WorkshopRecipe(name="Board", materials={"Wood": 5}),
    }
)

# A sawmill making 60000 board and 60000 wood an hour (1000 each a minute).
SAWMILL = PassiveProduction(
    producers=[
        PassiveProducer(
            name="sawmill",
            period=HOURLY,
            outputs={"board": 60000, "wood": 60000},
        )
    ]
)


def test_no_producers_crafts_everything():
    plan = craft_plan(
        [ItemNeed(item="Board", have=0, target=10)],
        WORKSHOP,
        PassiveProduction(),
        crafting_bonus=1.0,
        mastery_bonus=1.0,
    )
    assert plan.passive == []
    assert plan.steps == [
        CraftStep(
            item="Board",
            crafts=10,
            materials=[("Wood", 50)],
            produced=10,
            mastery_added=10,
        )
    ]
    # Board is craftable, so its base material Wood is what is left to gather.
    assert plan.materials == {"Wood": 50}


def test_prefers_passive_without_a_limit():
    plan = craft_plan(
        [ItemNeed(item="Board", have=0, target=100000)],
        WORKSHOP,
        SAWMILL,
        crafting_bonus=1.0,
        mastery_bonus=1.0,
    )
    # Boards are taken entirely from the sawmill, so nothing is crafted. The
    # 100000 needs a second hourly cycle, so the wait rounds up to 2 hours.
    assert plan.passive == [PassiveUse("Board", 100000, timedelta(hours=2))]
    assert plan.steps == []
    assert plan.materials == {}


def test_passive_limit_crafts_the_remainder():
    plan = craft_plan(
        [ItemNeed(item="Board", have=0, target=100000)],
        WORKSHOP,
        SAWMILL,
        crafting_bonus=1.0,
        mastery_bonus=1.0,
        passive_limit=timedelta(hours=1),
    )
    # In an hour the sawmill makes 60000 board and (for the crafts) 60000 wood.
    assert plan.passive == [
        PassiveUse("Board", 60000, timedelta(hours=1)),
        PassiveUse("Wood", 60000, timedelta(hours=1)),
    ]
    # The 40000 remaining boards are crafted, needing 200000 wood.
    assert plan.steps == [
        CraftStep(
            item="Board",
            crafts=40000,
            materials=[("Wood", 200000)],
            produced=40000,
            mastery_added=40000,
        )
    ]
    # 200000 wood needed, 60000 from the sawmill, 140000 left to gather.
    assert plan.materials == {"Wood": 140000}


def test_total_passive_time_is_concurrent():
    plan = craft_plan(
        [ItemNeed(item="Board", have=0, target=100000)],
        WORKSHOP,
        SAWMILL,
        crafting_bonus=1.0,
        mastery_bonus=1.0,
        passive_limit=timedelta(hours=2),
    )
    # 100000 board needs 2 hourly cycles; wood for the 0 crafts is 0.
    assert plan.passive == [PassiveUse("Board", 100000, timedelta(hours=2))]


def test_inventory_size_does_not_cap_the_total_over_time():
    # The storehouse holds 80000, above board production (60000/hr) so the rate
    # is unaffected; all 100000 are sourced passively given enough time -- the
    # storehouse caps the per-cycle amount, not the total.
    production = PassiveProduction(
        producers=[
            PassiveProducer(name="sawmill", period=HOURLY, outputs={"board": 60000})
        ],
        inventory_size=80000,
    )
    plan = craft_plan(
        [ItemNeed(item="Board", have=0, target=100000)],
        WORKSHOP,
        production,
        crafting_bonus=1.0,
        mastery_bonus=1.0,
    )
    assert plan.passive == [PassiveUse("Board", 100000, timedelta(hours=2))]
    assert plan.steps == []


def test_passive_limit_uses_the_daily_throughput():
    # Board makes 60000/hr but only 49440 fit the storehouse, so the daily
    # throughput is 24 * min(60000, 49440) = 1,186,560; the rest of a 2,000,000
    # goal is crafted.
    production = PassiveProduction(
        producers=[
            PassiveProducer(name="sawmill", period=HOURLY, outputs={"board": 60000})
        ],
        inventory_size=49440,
    )
    plan = craft_plan(
        [ItemNeed(item="Board", have=0, target=2_000_000)],
        WORKSHOP,
        production,
        crafting_bonus=1.0,
        mastery_bonus=1.0,
        passive_limit=timedelta(days=1),
    )
    board = next(u for u in plan.passive if u.item == "Board")
    assert board == PassiveUse("Board", 1_186_560, timedelta(days=1))
    assert plan.steps == [
        CraftStep(
            item="Board",
            crafts=813_440,
            materials=[("Wood", 4_067_200)],
            produced=813_440,
            mastery_added=813_440,
        )
    ]


def test_mastery_goal_sourced_passively():
    # 110000 mastery / 1.1 bonus = 100000 boards, all from the sawmill.
    plan = craft_plan(
        [ItemNeed(item="Board", have=0, target=110000, is_mastery=True)],
        WORKSHOP,
        SAWMILL,
        crafting_bonus=1.0,
        mastery_bonus=1.1,
    )
    assert plan.passive == [PassiveUse("Board", 100000, timedelta(hours=2))]
    assert plan.steps == []


def test_non_passive_items_are_still_crafted():
    # Buckets are not produced passively, so they are crafted; their board
    # material is taken from the sawmill, and iron is gathered.
    plan = craft_plan(
        [ItemNeed(item="Bucket", have=0, target=10)],
        WORKSHOP,
        SAWMILL,
        crafting_bonus=1.0,
        mastery_bonus=1.0,
    )
    bucket_step = next(s for s in plan.steps if s.item == "Bucket")
    assert bucket_step.materials == [("Board", 50), ("Iron", 10)]
    # The 50 boards come from the sawmill, not a Board craft.
    assert [u.item for u in plan.passive] == ["Board"]
    assert all(step.item != "Board" for step in plan.steps)
    assert plan.materials == {"Iron": 10}
