"""Timestamp parsing shared by the calendar client and the state store.

Both read ISO 8601 strings written by someone else -- Google for events, a
previous run of this app for scheduler state -- and both have to cope with the
same awkwardness: ``datetime.fromisoformat`` did not accept a trailing 'Z'
before Python 3.11, and Google emits 'Z' for UTC calendars and a numeric offset
for local ones.

Keeping that in one place means if the handling is wrong, it is wrong once.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def parse_iso8601(value: object, assume_utc: bool = False) -> Optional[datetime]:
    """Parse an ISO 8601 / RFC 3339 timestamp, or return None.

    Args:
        value: The string to parse. Non-strings return None rather than
            raising, since both callers read from files they do not control.
        assume_utc: Attach UTC to a timestamp that carries no offset. Google
            always sends one; our own state files always write one; anything
            naive is malformed and UTC is the safest reading.
    """
    if not isinstance(value, str):
        return None

    normalised = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value

    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError:
        return None

    if parsed.tzinfo is None and assume_utc:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
