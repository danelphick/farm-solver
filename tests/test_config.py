import textwrap
from datetime import timedelta

from config import load_config, passive_production
from passive import HOURLY


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


def test_passive_production_without_sawmill_is_empty():
    assert passive_production({}).producers == []
