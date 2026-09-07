"""Tests for ``--backup-file``: reading the web app's backup file from the CLI.

The web app stores nothing server-side, so its "Back up" button's JSON file is
the only way a browser-held save moves anywhere (see
``web/solverapp/static/solverapp/backup.js``). This lets ``main.py`` solve
straight from one: the pasted mastery/inventory text *and* the params of the
web solve it came from.

Two halves matter here -- that a malformed file is refused with a message
naming it (nothing else in the CLI reads a user-supplied JSON document), and
that the precedence rules hold: a flag you typed beats the file, overrides
concatenate, and the goal-solving modes never inherit the backup's goal.
"""

import json
from datetime import date
from pathlib import Path

import pytest

import main

# The real paste shapes (see mydata/ and parsing.parse_masteries/parse_inventory):
# a mastery block is name / progress / percentage, an inventory block is
# category, then name / description / tier / count.
MASTERY = "Corn\n1,500 / 10,000 Progress\n15% Track\n"
INVENTORY = "Crops chevron_down\n\nCorn\nA yellow vegetable\nMastered\n384\n"


def write_backup(tmp_path: Path, **overrides) -> Path:
    """A minimal, valid backup file; ``overrides`` replace any top-level key."""
    data = {
        "format": main.BACKUP_FORMAT,
        "version": main.BACKUP_VERSION,
        "exported_at": "2026-09-01T10:00:00.000Z",
        "state": {
            "mastery_text": MASTERY,
            "inventory_text": INVENTORY,
            "params": {"target": "none", "items": "Corn:5000"},
        },
        # Browser-only blocks the CLI has no use for; present so the fixture is
        # a real backup, and to pin that their presence bothers nothing.
        "ui": {"panes": {"crafts": False}},
        "snapshots": [
            {"taken_at": "2026-09-01T10:00:00Z", "date": "2026-09-01",
             "items": {"Corn": 1500}},
        ],
    }
    data.update(overrides)
    path = tmp_path / "backup.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# --- load_backup: what it reads, and what it refuses ------------------------


def test_reads_the_pastes_and_the_params(tmp_path):
    backup = main.load_backup(write_backup(tmp_path))
    assert backup.mastery_text == MASTERY
    assert backup.inventory_text == INVENTORY
    assert backup.params == {"target": "none", "items": "Corn:5000"}


def test_a_state_without_params_is_fine(tmp_path):
    path = write_backup(
        tmp_path, state={"mastery_text": MASTERY, "inventory_text": INVENTORY}
    )
    backup = main.load_backup(path)
    assert backup.params == {}
    assert backup.mastery_text == MASTERY


def test_a_file_that_is_not_json_is_refused(tmp_path):
    path = tmp_path / "backup.json"
    path.write_text("<html>not a backup</html>")
    with pytest.raises(ValueError, match="is not JSON"):
        main.load_backup(path)


def test_a_json_file_that_is_not_a_backup_is_refused(tmp_path):
    # The format tag is exactly why backup.js stamps one: a JSON file that
    # happens to be lying in the Downloads folder is not a backup.
    path = tmp_path / "backup.json"
    path.write_text(json.dumps({"hello": 1}))
    with pytest.raises(ValueError, match="is not a farm-solver backup"):
        main.load_backup(path)


def test_a_newer_backup_version_is_refused(tmp_path):
    path = write_backup(tmp_path, version=main.BACKUP_VERSION + 1)
    with pytest.raises(ValueError, match="can only read version"):
        main.load_backup(path)


def test_a_missing_version_is_refused(tmp_path):
    # Including `true`, which is an int in Python but not a version.
    for version in (None, "1", 0, True):
        path = write_backup(tmp_path, version=version)
        with pytest.raises(ValueError, match="has no backup version"):
            main.load_backup(path)


def test_a_non_object_state_is_refused(tmp_path):
    path = write_backup(tmp_path, state=["not", "an", "object"])
    with pytest.raises(ValueError, match="saved state is not an object"):
        main.load_backup(path)


def test_the_message_names_the_file(tmp_path):
    path = tmp_path / "backup.json"
    path.write_text("nope")
    with pytest.raises(ValueError, match=str(path)):
        main.load_backup(path)


# --- apply_backup_params: precedence ----------------------------------------


def _args(argv: list[str]):
    return main.build_parser().parse_args(argv)


def test_a_param_fills_in_an_option_left_at_its_default():
    args = _args([])
    main.apply_backup_params(args, {"items": "Corn:5000", "plans": 7})
    assert args.items == "Corn:5000"
    assert args.plans == 7


def test_an_option_you_typed_beats_the_file():
    args = _args(["--plans", "2", "-i", "Wood:10"])
    main.apply_backup_params(args, {"items": "Corn:5000", "plans": 7})
    assert args.plans == 2
    assert args.items == "Wood:10"


def test_overrides_concatenate_with_the_command_line_winning():
    # Both sides carry overrides (the preference panels turn config knobs into
    # them), so neither may replace the other -- and apply_overrides applies in
    # order, so the command line's must come last to win.
    args = _args(["--override", "farming.rows=5"])
    main.apply_backup_params(args, {"overrides": ["farming.rows=30"]})
    assert args.override == ["farming.rows=30", "farming.rows=5"]
    cfg: dict = {}
    main.apply_overrides(cfg, args.override)
    assert cfg["farming"]["rows"] == 5


def test_params_the_backup_never_set_are_left_alone():
    args = _args([])
    before = vars(_args([]))
    main.apply_backup_params(args, {"plans": 7})
    changed = {k for k, v in vars(args).items() if v != before[k]}
    assert changed == {"plans"}


def test_empty_params_change_nothing():
    args = _args([])
    main.apply_backup_params(args, {})
    assert vars(args) == vars(_args([]))


def test_an_unknown_param_is_refused():
    # webapi.params_to_args rejects rather than ignores, so a hand-edited file
    # fails loudly instead of silently solving something else.
    with pytest.raises(ValueError, match="unknown param"):
        main.apply_backup_params(_args([]), {"nope": 1})


def test_with_goal_off_drops_the_goal_but_keeps_the_knobs():
    # --mastery-costs/--ascend refuse a --target/-m/-i, so inheriting a
    # backup's goal would make the obvious command fail on a flag the user
    # never typed.
    args = _args([])
    main.apply_backup_params(
        args,
        {"target": "none", "items": "Corn:5000", "mastery": "Butter:1",
         "plan_select": {"kind": "cheapest"}, "plans": 7, "max_gold": "500"},
        with_goal=False,
    )
    assert args.target is None
    assert args.items is None
    assert args.mastery is None
    assert args.cheapest_plan is False
    assert args.plans == 7
    assert args.max_gold == 500


def test_with_goal_off_and_nothing_left_changes_nothing():
    args = _args([])
    main.apply_backup_params(args, {"items": "Corn:5000"}, with_goal=False)
    assert vars(args) == vars(_args([]))


# --- load_game_state --------------------------------------------------------


def test_state_comes_from_the_backup_text(tmp_path):
    backup = main.load_backup(write_backup(tmp_path))
    state = main.load_game_state(_args([]), {}, backup)
    assert state.mastery("Corn").progress == 1500
    assert state.inventory.amount("Corn") == 384


def test_empty_flags_still_drop_their_half_of_a_backup(tmp_path):
    backup = main.load_backup(write_backup(tmp_path))
    state = main.load_game_state(_args(["--empty-mastery"]), {}, backup)
    assert state.mastery("Corn") is None
    assert state.inventory.amount("Corn") == 384

    state = main.load_game_state(_args(["--empty-inventory"]), {}, backup)
    assert state.mastery("Corn").progress == 1500
    assert state.inventory.amount("Corn") == 0


def test_without_a_backup_the_files_are_read(tmp_path):
    (tmp_path / "m.txt").write_text(MASTERY)
    (tmp_path / "i.txt").write_text(INVENTORY)
    args = _args(
        ["--mastery-file", str(tmp_path / "m.txt"),
         "--inventory-file", str(tmp_path / "i.txt")]
    )
    state = main.load_game_state(args, {}, None)
    assert state.mastery("Corn").progress == 1500


# --- run(): the wiring ------------------------------------------------------


def test_a_backup_conflicts_with_an_explicit_state_file(tmp_path):
    # Half a state from a file and half from a backup is nobody's intent, so
    # this is refused rather than silently resolved one way.
    path = write_backup(tmp_path)
    for flag in ("--mastery-file", "--inventory-file"):
        args = _args(["--backup-file", str(path), flag, str(tmp_path / "m.txt")])
        with pytest.raises(ValueError, match=f"conflicts with {flag}"):
            main.run(args)


def test_solving_from_a_backup_matches_the_equivalent_flags(tmp_path, capsys):
    # The whole point: the file's params are the command line you would
    # otherwise have typed.
    path = write_backup(tmp_path)
    (tmp_path / "m.txt").write_text(MASTERY)
    (tmp_path / "i.txt").write_text(INVENTORY)

    main.run(_args(["--backup-file", str(path), "--start-date", "2026-09-01"]))
    from_backup = capsys.readouterr().out

    main.run(_args([
        "--mastery-file", str(tmp_path / "m.txt"),
        "--inventory-file", str(tmp_path / "i.txt"),
        "--target", "none", "-i", "Corn:5000", "--start-date", "2026-09-01",
    ]))
    assert capsys.readouterr().out == from_backup
    assert "Corn" in from_backup


def test_the_goal_less_modes_do_not_inherit_the_backup_goal(tmp_path, capsys):
    # --unobtainable is the cheap one of the three to run; all take the same
    # with_goal=False path in run().
    path = write_backup(tmp_path)
    args = _args(["--backup-file", str(path), "--unobtainable"])
    assert main.run(args) == 0
    assert "unobtainable" in capsys.readouterr().out
    # The goal really was withheld, not merely unused.
    assert args.items is None


def test_start_date_defaults_are_untouched_by_a_backup(tmp_path):
    # A regression guard for the "value == default" merge rule: start_date's
    # default is None and the backup here sets none, so it must stay None (the
    # run then falls back to date.today(), not to some stale exported day).
    backup = main.load_backup(write_backup(tmp_path))
    args = _args([])
    main.apply_backup_params(args, backup.params)
    assert args.start_date is None
    assert (args.start_date or date.today()) == date.today()
