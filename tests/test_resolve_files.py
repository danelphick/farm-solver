import argparse
from pathlib import Path

import pytest

from main import resolve_files


def make_args(
    mastery_file=None, inventory_file=None, empty_mastery=False, empty_inventory=False
):
    return argparse.Namespace(
        mastery_file=mastery_file,
        inventory_file=inventory_file,
        empty_mastery=empty_mastery,
        empty_inventory=empty_inventory,
    )


CONFIG = {
    "solver": {
        "mastery_file": "./cfg-mastery.txt",
        "inventory_file": "./cfg-inventory.txt",
    }
}


def test_uses_config_when_no_cli():
    mastery, inventory = resolve_files(make_args(), CONFIG)
    assert mastery == Path("./cfg-mastery.txt")
    assert inventory == Path("./cfg-inventory.txt")


def test_cli_overrides_config():
    args = make_args(
        mastery_file=Path("cli-mastery.txt"),
        inventory_file=Path("cli-inventory.txt"),
    )
    mastery, inventory = resolve_files(args, CONFIG)
    assert mastery == Path("cli-mastery.txt")
    assert inventory == Path("cli-inventory.txt")


def test_cli_and_config_mix():
    # CLI mastery, config inventory.
    args = make_args(mastery_file=Path("cli-mastery.txt"))
    mastery, inventory = resolve_files(args, CONFIG)
    assert mastery == Path("cli-mastery.txt")
    assert inventory == Path("./cfg-inventory.txt")


def test_missing_from_both_is_an_error():
    with pytest.raises(ValueError, match="mastery_file"):
        resolve_files(make_args(), {})


def test_missing_inventory_only():
    args = make_args(mastery_file=Path("cli-mastery.txt"))
    with pytest.raises(ValueError, match="inventory_file"):
        resolve_files(args, {})


def test_empty_mastery_skips_resolution_even_when_unconfigured():
    args = make_args(empty_mastery=True, inventory_file=Path("cli-inventory.txt"))
    mastery, inventory = resolve_files(args, {})
    assert mastery is None
    assert inventory == Path("cli-inventory.txt")


def test_empty_inventory_skips_resolution_even_when_unconfigured():
    args = make_args(mastery_file=Path("cli-mastery.txt"), empty_inventory=True)
    mastery, inventory = resolve_files(args, {})
    assert mastery == Path("cli-mastery.txt")
    assert inventory is None


def test_empty_both_ignores_config():
    args = make_args(empty_mastery=True, empty_inventory=True)
    mastery, inventory = resolve_files(args, CONFIG)
    assert mastery is None
    assert inventory is None


def test_data_directory_picks_latest_dated_file(tmp_path):
    for name in [
        "mastery-2026-06-29.txt",
        "mastery-2026-07-08.txt",
        "mastery-2026-07-04.txt",
        "inventory-2026-06-29.txt",
        "inventory-2026-07-08.txt",
        "inventory-2026-07-04.txt",
    ]:
        (tmp_path / name).write_text("")
    cfg = {"solver": {"data_directory": str(tmp_path)}}
    mastery, inventory = resolve_files(make_args(), cfg)
    assert mastery == tmp_path / "mastery-2026-07-08.txt"
    assert inventory == tmp_path / "inventory-2026-07-08.txt"


def test_data_directory_only_fills_in_unset_file(tmp_path):
    (tmp_path / "mastery-2026-07-08.txt").write_text("")
    (tmp_path / "inventory-2026-07-08.txt").write_text("")
    cfg = {
        "solver": {
            "data_directory": str(tmp_path),
            "inventory_file": "./cfg-inventory.txt",
        }
    }
    args = make_args(mastery_file=Path("cli-mastery.txt"))
    mastery, inventory = resolve_files(args, cfg)
    assert mastery == Path("cli-mastery.txt")
    assert inventory == Path("./cfg-inventory.txt")


def test_data_directory_without_matching_file_is_an_error(tmp_path):
    cfg = {"solver": {"data_directory": str(tmp_path)}}
    with pytest.raises(ValueError, match="mastery_file"):
        resolve_files(make_args(), cfg)
