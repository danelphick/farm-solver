#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""

import os
import sys
from pathlib import Path


def main() -> None:
    """Run administrative tasks."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "farmweb.settings")
    # The solver package (main.py, webapi.py, optimizer.py, ...) lives at the
    # repo root, one level above this Django project; put both this directory
    # (for "farmweb"/"solverapp") and the repo root (for "main"/"webapi"/...)
    # on sys.path, so there is no packaging/install step.
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    sys.path.insert(0, str(here.parent))
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to run `uv sync --group web`?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
