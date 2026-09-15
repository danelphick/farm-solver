"""Turn a web-app backup file into dated ``mydata/`` snapshot files.

The web app's "Back up" button downloads a JSON file (``backup.js``) whose
``state`` carries the mastery and inventory text as pasted into the browser.
This writes that text out as the pair of files the CLI (and
``scripts/generate_items.py``) read from ``mydata/``:

* ``mastery-YYYY-MM-DD.txt``
* ``inventory-YYYY-MM-DD.txt``

The date is the backup's own ``exported_at`` in local time (``--date``
overrides it), so the new pair sorts as the latest snapshot when
``main._latest_dated_file`` picks one. The file is validated by
``main.load_backup``, both pastes must be non-empty and parse, and nothing is
written unless both files can be -- an existing file is only replaced with
``--force``.

Run with: ``python scripts/import_backup.py ~/Downloads/farm-solver-backup-*.json``
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import main  # noqa: E402
import parsing  # noqa: E402

MYDATA_DIR = REPO_ROOT / "mydata"


def backup_date(path: Path) -> date:
    """The local date of the backup's ``exported_at``, else today."""
    exported_at = json.loads(path.read_text(encoding="utf-8")).get("exported_at")
    try:
        return datetime.fromisoformat(exported_at).astimezone().date()
    except (TypeError, ValueError):
        return date.today()


def import_backup(
    path: Path,
    out_dir: Path = MYDATA_DIR,
    day: date | None = None,
    force: bool = False,
) -> list[tuple[Path, int]]:
    """Write the backup's pastes to ``out_dir``; ``[(file, entry count)]``.

    Raises :class:`ValueError` -- before writing anything -- for an invalid
    backup, an empty or unparseable paste, or an existing file without
    ``force``.
    """
    backup = main.load_backup(path)
    day = day or backup_date(path)
    pastes = [
        ("mastery", backup.mastery_text, lambda t: len(parsing.parse_masteries(t))),
        ("inventory", backup.inventory_text,
         lambda t: len(parsing.parse_inventory(t).items)),
    ]

    planned = []
    for kind, text, count in pastes:
        if not text.strip():
            raise ValueError(f"{path} has no pasted {kind} text")
        entries = count(text)
        if entries == 0:
            raise ValueError(f"{path}'s pasted {kind} text has no {kind} entries")
        target = out_dir / f"{kind}-{day.isoformat()}.txt"
        if target.exists() and not force:
            raise ValueError(f"{target} already exists (pass --force to replace it)")
        planned.append((target, text if text.endswith("\n") else text + "\n", entries))

    out_dir.mkdir(parents=True, exist_ok=True)
    for target, text, _ in planned:
        target.write_text(text, encoding="utf-8")
    return [(target, entries) for target, _, entries in planned]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Write a web-app backup's mastery/inventory pastes to mydata/."
    )
    parser.add_argument("backup", type=Path, help="the farm-solver-backup-*.json file")
    parser.add_argument(
        "--out-dir", type=Path, default=MYDATA_DIR,
        help=f"directory to write into (default: {MYDATA_DIR})",
    )
    parser.add_argument(
        "--date", type=date.fromisoformat, default=None,
        help="YYYY-MM-DD for the filenames (default: the backup's export date)",
    )
    parser.add_argument(
        "--force", action="store_true", help="replace files that already exist"
    )
    return parser


def run(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        written = import_backup(args.backup, args.out_dir, args.date, args.force)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"error: {exc}") from None
    for target, entries in written:
        print(f"wrote {target} ({entries} entries)")


if __name__ == "__main__":
    run()
