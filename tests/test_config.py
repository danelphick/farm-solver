import textwrap
from datetime import date, timedelta

from config import load_config, passive_production, storehouse_size
from passive import DAILY, HOURLY


def test_load_config_reads_toml(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        textwrap.dedent(
            """
            [sawmill]
            wood = 46000
            board = 60000
            oak = 6000
            """
        )
    )
    config = load_config(path)
    assert config["sawmill"] == {"wood": 46000, "board": 60000, "oak": 6000}


def test_passive_production_reads_sawmill():
    config = {"sawmill": {"wood": 46000, "board": 60000, "oak": 6000}}
    production = passive_production(config)
    assert len(production.producers) == 1
    sawmill = production.producers[0]
    assert sawmill.name == "sawmill"
    assert sawmill.period == HOURLY
    assert sawmill.outputs == {"wood": 46000, "board": 60000, "oak": 6000}
    # The built production answers timing queries.
    assert production.time_to_produce("board", 60000) == timedelta(hours=1)


def test_passive_production_lowercases_item_names():
    config = {"sawmill": {"Board": 60000}}
    production = passive_production(config)
    assert production.producers[0].outputs == {"board": 60000}


def test_passive_production_reads_chicken_coop():
    # produces eggs and feathers daily; gold feather at 5% when starmap is done.
    production = passive_production(
        {"chicken_coop": {"produces": 18271, "completed_starmap": True}}
    )
    coop = production.producers[0]
    assert coop.name == "chicken_coop"
    assert coop.period == DAILY
    assert coop.outputs == {
        "eggs": 18271,
        "feathers": 18271,
        "gold feather": 913,  # 18271 // 20
    }


def test_chicken_coop_without_starmap_has_no_gold_feather():
    production = passive_production(
        {"chicken_coop": {"produces": 18271, "completed_starmap": False}}
    )
    assert production.producers[0].outputs == {
        "eggs": 18271,
        "feathers": 18271,
    }


def test_passive_production_reads_hay_field_as_straw():
    # The hay_field section reports straw produced every ten minutes.
    production = passive_production({"hay_field": {"hay": 30000}})
    assert production.produces("straw")
    assert not production.produces("hay")
    assert production.rate_per_minute("straw") == 3000  # 30000 / 10


def test_passive_production_reads_quarry_with_two_cadences():
    # Stone every ten minutes, coal every hour.
    production = passive_production({"quarry": {"stone": 8000, "coal": 8000}})
    assert production.rate_per_minute("stone") == 800  # 8000 / 10
    assert production.rate_per_minute("coal") == 8000 / 60  # hourly


def test_passive_production_reads_steelworks_with_derived_wire():
    # Steel hourly, steel wire at a third of that, rounded down.
    production = passive_production({"steelworks": {"steel": 100}})
    assert production.rate_per_minute("steel") == 100 / 60
    assert production.rate_per_minute("steel wire") == 33 / 60  # 100 // 3


def test_passive_production_attaches_storehouse_size():
    config = {
        "sawmill": {"board": 60000},
        "storehouse": {
            "inventory": 100,
            "updated_on": "2026-06-30",
            "growth": 20,
        },
    }
    production = passive_production(config, today=date(2026, 7, 2))
    assert production.inventory_size == 140


def test_passive_production_without_sawmill_is_empty():
    assert passive_production({}).producers == []


def test_storehouse_size_grows_each_day():
    config = {
        "storehouse": {
            "inventory": 49440,
            "updated_on": "2026-06-30",
            "growth": 20,
        }
    }
    # Five days on, the storehouse has grown by 5 * 20.
    assert storehouse_size(config, today=date(2026, 7, 5)) == 49540


def test_storehouse_size_on_updated_day_is_recorded_value():
    config = {"storehouse": {"inventory": 100, "updated_on": "2026-06-30"}}
    assert storehouse_size(config, today=date(2026, 6, 30)) == 100


def test_storehouse_size_ignores_dates_before_updated_on():
    config = {
        "storehouse": {
            "inventory": 100,
            "updated_on": "2026-06-30",
            "growth": 20,
        }
    }
    assert storehouse_size(config, today=date(2026, 6, 1)) == 100


def test_storehouse_size_parses_toml_date_value():
    # An unquoted TOML date is parsed as a date object, not a string.
    config = load_config_from_text(
        """
        [storehouse]
        inventory = 100
        updated_on = 2026-06-30
        growth = 20
        """
    )
    assert storehouse_size(config, today=date(2026, 7, 2)) == 140


def test_no_storehouse_section_is_none():
    assert storehouse_size({}) is None


def load_config_from_text(text: str) -> dict:
    import tomllib

    return tomllib.loads(textwrap.dedent(text))
