"""U3 -- calendar sync (R4-R7, R31-R36).

This unit carries the product's primary property, so the coverage below leans
hard on the sync behaviours rather than just the happy path: reschedules,
deletions, events created inside the alert window, and offline backoff.

``now`` is injected everywhere so the tests are deterministic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from airplanenotifier.calendar_client import CalendarClient

NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)


def iso(moment: datetime) -> str:
    return moment.isoformat()


def event(event_id="evt-1", starts_in_minutes=3.0, duration_minutes=30,
          summary="Team Standup", all_day=False, status="confirmed"):
    """Build a Google Calendar API event payload."""
    start = NOW + timedelta(minutes=starts_in_minutes)
    end = start + timedelta(minutes=duration_minutes)
    if all_day:
        start_field = {"date": start.date().isoformat()}
        end_field = {"date": end.date().isoformat()}
    else:
        start_field = {"dateTime": iso(start)}
        end_field = {"dateTime": iso(end)}
    payload = {"id": event_id, "start": start_field, "end": end_field, "status": status}
    if summary is not None:
        payload["summary"] = summary
    return payload


def make_client(*items, side_effect=None):
    """A CalendarClient wired to a mock Google service returning `items`."""
    service = MagicMock()
    execute = service.events.return_value.list.return_value.execute
    if side_effect is not None:
        execute.side_effect = side_effect
    else:
        execute.return_value = {"items": list(items)}
    client = CalendarClient(credentials=MagicMock(), service=service)
    return client, service


def test_no_events_returns_empty():
    client, _ = make_client()
    assert client.get_upcoming_meetings(now=NOW) == []


def test_event_starting_in_three_minutes_is_returned():
    client, _ = make_client(event(starts_in_minutes=3))
    meetings = client.get_upcoming_meetings(now=NOW)
    assert len(meetings) == 1
    assert meetings[0]["summary"] == "Team Standup"
    assert meetings[0]["id"] == "evt-1"


def test_event_starting_in_ten_minutes_is_ignored():
    client, _ = make_client(event(starts_in_minutes=10))
    assert client.get_upcoming_meetings(now=NOW) == []


def test_event_that_already_started_is_ignored():
    """The window is bounded at both ends, so a restart cannot replay history."""
    client, _ = make_client(event(starts_in_minutes=-20))
    assert client.get_upcoming_meetings(now=NOW) == []


def test_same_event_does_not_alert_twice():
    client, _ = make_client(event(starts_in_minutes=3))
    assert len(client.get_upcoming_meetings(now=NOW)) == 1
    assert client.get_upcoming_meetings(now=NOW) == []


def test_rescheduled_event_alerts_again_at_its_new_time():
    """R33/KTD17: dedup on (id, start_time), so a move is alertable again."""
    client, service = make_client(event(event_id="evt-1", starts_in_minutes=3))
    assert len(client.get_upcoming_meetings(now=NOW)) == 1

    # Same id, new start time, and now inside the window again.
    service.events.return_value.list.return_value.execute.return_value = {
        "items": [event(event_id="evt-1", starts_in_minutes=4)]
    }
    assert len(client.get_upcoming_meetings(now=NOW)) == 1


def test_deleted_event_never_alerts():
    """R32: a removed event simply stops being returned."""
    client, service = make_client(event(starts_in_minutes=4))
    service.events.return_value.list.return_value.execute.return_value = {"items": []}
    assert client.get_upcoming_meetings(now=NOW) == []


def test_cancelled_event_is_excluded():
    client, _ = make_client(event(starts_in_minutes=3, status="cancelled"))
    assert client.get_upcoming_meetings(now=NOW) == []


def test_all_day_event_is_excluded():
    client, _ = make_client(event(starts_in_minutes=3, all_day=True))
    assert client.get_upcoming_meetings(now=NOW) == []


def test_event_created_inside_the_window_still_alerts():
    """R35: an ad-hoc call 3 minutes out is not skipped as 'already started'."""
    client, _ = make_client(event(event_id="adhoc", starts_in_minutes=2.5))
    assert len(client.get_upcoming_meetings(now=NOW)) == 1


def test_multiple_due_events_are_all_returned():
    client, _ = make_client(
        event(event_id="a", starts_in_minutes=2),
        event(event_id="b", starts_in_minutes=4),
    )
    assert len(client.get_upcoming_meetings(now=NOW)) == 2


def test_event_without_summary_gets_a_placeholder():
    client, _ = make_client(event(summary=None))
    assert client.get_upcoming_meetings(now=NOW)[0]["summary"] == "(No title)"


def test_query_is_bounded_to_the_next_two_hours():
    """KTD15: a bounded window query, not a syncToken."""
    client, service = make_client()
    client.get_upcoming_meetings(now=NOW)

    kwargs = service.events.return_value.list.call_args.kwargs
    assert kwargs["calendarId"] == "primary"
    assert kwargs["singleEvents"] is True
    assert kwargs["orderBy"] == "startTime"
    assert kwargs["timeMin"].startswith("2026-08-18T12:00:00")
    assert kwargs["timeMax"].startswith("2026-08-18T14:00:00")


# --- failure handling (R34) -------------------------------------------------


def test_network_error_returns_empty_and_does_not_raise():
    client, _ = make_client(side_effect=OSError("network is unreachable"))
    assert client.get_upcoming_meetings(now=NOW) == []


def test_backoff_grows_then_caps_at_five_minutes():
    client, _ = make_client(side_effect=OSError("down"))
    delays = []
    moment = NOW
    for _ in range(6):
        client.get_upcoming_meetings(now=moment)
        delays.append((client.backoff_until() - moment).total_seconds())
        moment = client.backoff_until()  # jump past the backoff each time
    assert delays == [30, 60, 120, 240, 300, 300]


def test_backoff_suppresses_calls_until_it_expires():
    client, service = make_client(side_effect=OSError("down"))
    client.get_upcoming_meetings(now=NOW)
    execute = service.events.return_value.list.return_value.execute
    calls_after_first = execute.call_count

    client.get_upcoming_meetings(now=NOW + timedelta(seconds=5))
    assert execute.call_count == calls_after_first  # still backed off


def test_first_success_resets_the_backoff():
    client, service = make_client(side_effect=OSError("down"))
    client.get_upcoming_meetings(now=NOW)
    assert client.backoff_until() is not None

    service.events.return_value.list.return_value.execute.side_effect = None
    service.events.return_value.list.return_value.execute.return_value = {"items": []}
    client.get_upcoming_meetings(now=client.backoff_until())

    assert client.backoff_until() is None
    assert client.last_sync_time() is not None


# --- meeting-in-progress, used by the nudge scheduler (R23) ------------------


def test_meeting_in_progress_is_true_during_an_event():
    client, _ = make_client(event(starts_in_minutes=-10, duration_minutes=30))
    client.get_upcoming_meetings(now=NOW)
    assert client.is_meeting_in_progress(now=NOW) is True


def test_meeting_in_progress_is_false_between_events():
    client, _ = make_client(event(starts_in_minutes=45, duration_minutes=30))
    client.get_upcoming_meetings(now=NOW)
    assert client.is_meeting_in_progress(now=NOW) is False


def test_meeting_in_progress_ignores_all_day_events():
    """An all-day event must not suppress nudges for the whole day."""
    client, _ = make_client(event(starts_in_minutes=-60, all_day=True))
    client.get_upcoming_meetings(now=NOW)
    assert client.is_meeting_in_progress(now=NOW) is False


def test_meeting_in_progress_makes_no_network_call():
    """It reads the last sync's cache; it must never issue its own request."""
    client, service = make_client(event(starts_in_minutes=-5))
    client.get_upcoming_meetings(now=NOW)
    execute = service.events.return_value.list.return_value.execute
    before = execute.call_count
    client.is_meeting_in_progress(now=NOW)
    assert execute.call_count == before


def test_last_sync_time_is_none_before_any_success():
    client, _ = make_client(side_effect=OSError("down"))
    client.get_upcoming_meetings(now=NOW)
    assert client.last_sync_time() is None


def test_reset_alerts_allows_a_repeat_alert():
    client, _ = make_client(event(starts_in_minutes=3))
    assert len(client.get_upcoming_meetings(now=NOW)) == 1
    client.reset_alerts()
    assert len(client.get_upcoming_meetings(now=NOW)) == 1


def test_malformed_event_is_skipped_without_killing_the_batch():
    """One bad payload must not cost us the good meeting beside it."""
    broken = {"id": "bad", "start": {"dateTime": "not-a-date"}, "end": {}}
    client, _ = make_client(broken, event(event_id="good", starts_in_minutes=3))
    meetings = client.get_upcoming_meetings(now=NOW)
    assert [m["id"] for m in meetings] == ["good"]


@pytest.mark.parametrize("stamp", ["2026-08-18T12:03:00Z", "2026-08-18T13:03:00+01:00"])
def test_accepts_both_z_and_offset_timestamps(stamp):
    """Google returns 'Z' for UTC calendars and offsets for local ones."""
    payload = {
        "id": "evt", "summary": "S", "status": "confirmed",
        "start": {"dateTime": stamp},
        "end": {"dateTime": "2026-08-18T14:00:00Z"},
    }
    client, _ = make_client(payload)
    assert len(client.get_upcoming_meetings(now=NOW)) == 1
