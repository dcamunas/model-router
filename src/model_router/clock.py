"""The clock the learning code reads, in one place, so tests can set it and every module agrees on time zones."""

from __future__ import annotations

from datetime import UTC, datetime


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
