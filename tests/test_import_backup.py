"""Tests for ``scripts/import_backup.py``: backup file -> dated mydata/ files."""

from datetime import date

import pytest

from scripts import import_backup
from tests.test_backup_file import INVENTORY, MASTERY, write_backup

DAY = date(2026, 9, 15)


def test_writes_both_pastes_as_dated_files(tmp_path):
    out = tmp_path / "mydata"
    written = import_backup.import_backup(write_backup(tmp_path), out, DAY)
    mastery, inventory = out / "mastery-2026-09-15.txt", out / "inventory-2026-09-15.txt"
    assert written == [(mastery, 1), (inventory, 1)]
    assert mastery.read_text() == MASTERY
    assert inventory.read_text() == INVENTORY


def test_date_defaults_to_the_export_date(tmp_path):
    # Midday UTC is the same calendar day in every timezone the tests run in.
    path = write_backup(tmp_path, exported_at="2026-08-20T12:00:00.000Z")
    import_backup.import_backup(path, tmp_path)
    assert (tmp_path / "mastery-2026-08-20.txt").exists()
    assert (tmp_path / "inventory-2026-08-20.txt").exists()


def test_a_missing_trailing_newline_is_added(tmp_path):
    state = {"mastery_text": MASTERY.rstrip("\n"), "inventory_text": INVENTORY}
    import_backup.import_backup(write_backup(tmp_path, state=state), tmp_path, DAY)
    assert (tmp_path / "mastery-2026-09-15.txt").read_text() == MASTERY


def test_an_existing_file_is_refused_and_nothing_is_written(tmp_path):
    (tmp_path / "inventory-2026-09-15.txt").write_text("old\n")
    with pytest.raises(ValueError, match="already exists"):
        import_backup.import_backup(write_backup(tmp_path), tmp_path, DAY)
    assert not (tmp_path / "mastery-2026-09-15.txt").exists()
    assert (tmp_path / "inventory-2026-09-15.txt").read_text() == "old\n"


def test_force_replaces_an_existing_file(tmp_path):
    (tmp_path / "inventory-2026-09-15.txt").write_text("old\n")
    import_backup.import_backup(write_backup(tmp_path), tmp_path, DAY, force=True)
    assert (tmp_path / "inventory-2026-09-15.txt").read_text() == INVENTORY


@pytest.mark.parametrize("kind", ["mastery", "inventory"])
def test_an_empty_paste_is_refused(tmp_path, kind):
    state = {"mastery_text": MASTERY, "inventory_text": INVENTORY}
    state[f"{kind}_text"] = "  \n"
    with pytest.raises(ValueError, match=f"no pasted {kind} text"):
        import_backup.import_backup(write_backup(tmp_path, state=state), tmp_path, DAY)
    assert list(tmp_path.glob("*.txt")) == []


def test_a_file_that_is_not_a_backup_is_refused(tmp_path):
    path = write_backup(tmp_path, format="something-else")
    with pytest.raises(ValueError, match="not a farm-solver backup"):
        import_backup.import_backup(path, tmp_path, DAY)


def test_cli_reports_errors_without_a_traceback(tmp_path):
    (tmp_path / "mastery-2026-09-15.txt").write_text("old\n")
    with pytest.raises(SystemExit, match="^error: .*already exists"):
        import_backup.run(
            [str(write_backup(tmp_path)), "--out-dir", str(tmp_path),
             "--date", "2026-09-15"]
        )
