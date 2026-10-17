"""Dated game events whose sources the solver can switch on.

An event source (the Haunted House exploring location, Lake Minerva's October
fishing table) is open only for part of the year. The LP does not model that
window -- an open event source is assumed to cover everything a plan wants
from it -- so the caller resolves whether it is open from the plan's start
date (see ``main.resolve_haunted_house`` / ``main.resolve_october_fishing``)
and the report warns how many event days are left.
"""

from __future__ import annotations

from datetime import date

# The October (Halloween) event's first and last (inclusive) days as
# (month, day). It opened on 13 October; the 31 October close is expected but
# not confirmed.
HALLOWEEN_OPENS = (10, 13)
HALLOWEEN_CLOSES = (10, 31)


def halloween_window(year: int) -> tuple[date, date]:
    """The October event's first and last (inclusive) days in ``year``."""
    return date(year, *HALLOWEEN_OPENS), date(year, *HALLOWEEN_CLOSES)


def october_window(year: int) -> tuple[date, date]:
    """The whole of October in ``year`` -- when Lake Minerva's October table applies."""
    return date(year, 10, 1), date(year, 10, 31)
