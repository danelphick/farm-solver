import argparse
from pathlib import Path

import pytest

from main import resolve_files


def make_args(mastery_file=None, inventory_file=None):
    return argparse.Namespace(
        mastery_file=mastery_file, inventory_file=inventory_file
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
