"""The clock and the calendar the router reads, in one place, so tests can set it and every module agrees on
time zones and weeks.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

WEEK_LABEL = re.compile(r"(\d{4})-?W(\d{2})")


def now() -> datetime:
    """Return the current moment in UTC. Tests replace this function to freeze time."""
    return datetime.now(UTC)


def stamp() -> str:
    """Return the current moment as an ISO text with microseconds, so two changes a moment apart still sort."""
    return now().isoformat(timespec="microseconds")


def local(moment: datetime) -> str:
    """Return a moment in this machine's time zone, the way a person reads a reset: 'Sat 24/10 15:00 CEST'."""
    return moment.astimezone().strftime("%a %d/%m %H:%M %Z")


def parse_moment(text: object) -> datetime | None:
    """Return the moment an ISO text names, read as UTC when it carries no zone, or None when it is not one."""
    try:
        moment = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def week_of(moment: datetime) -> str:
    """Return the ISO week of a moment, as `2026-W42`."""
    year, week, _ = moment.astimezone(UTC).isocalendar()
    return f"{year}-W{week:02d}"


def week_start(label: str) -> datetime | None:
    """Return the Monday 00:00 UTC a week label (`2026-W42`) starts on, or None for a bad label."""
    match = WEEK_LABEL.fullmatch(label)
    if not match:
        return None
    try:
        day = date.fromisocalendar(int(match[1]), int(match[2]), 1)
    except ValueError:
        return None
    return datetime(day.year, day.month, day.day, tzinfo=UTC)
