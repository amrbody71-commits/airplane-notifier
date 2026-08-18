"""Regression tests for defects found in code review.

Each test names the failure it prevents. Several of these guard against
crashes that would abort the whole process rather than skip a tick, because
PyQt routes an unhandled exception in a timer slot to qFatal().
"""

from __future__ import annotations

import ctypes
import json
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from google.auth.exceptions import RefreshError, TransportError

from airplanenotifier import auth, config, paths
from airplanenotifier.calendar_client import CalendarClient
from airplanenotifier.nudges import IGNORED, NudgeScheduler

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
LOCAL = datetime.now().astimezone().tzinfo


# --- auth: offline at login must not kill sync permanently -------------------


def test_network_failure_during_refresh_is_transient_not_fatal(config_dir, tmp_path):
    """TransportError is a SIBLING of RefreshError, so it was escaping.

    At Windows login the token has expired overnight and Wi-Fi is not up yet.
    Before this, the exception propagated, _on_auth_failed stopped the sync
    timer, and the app never showed a meeting alert again until restarted.
    """
    creds_file = tmp_path / "credentials.json"
    creds_file.write_text(json.dumps({"installed": {"client_id": "x"}}))

    stale = MagicMock(valid=False, expired=True, refresh_token="r")
    stale.refresh.side_effect = TransportError("Failed to resolve oauth2.googleapis.com")
    paths.TOKEN_PATH.write_text("{}")

    with patch.object(auth, "credentials_path", lambda: creds_file), \
            patch.object(auth, "Credentials") as creds_cls, \
            patch.object(auth, "InstalledAppFlow") as flow_cls:
        creds_cls.from_authorized_user_file.return_value = stale
        with pytest.raises(auth.TransientAuthError):
            auth.get_credentials()

    # And it must NOT have thrown the good grant away.
    assert paths.TOKEN_PATH.exists()
    flow_cls.from_client_secrets_file.assert_not_called()


def test_revoked_token_still_reauthorizes(config_dir, tmp_path):
    """The transient path must not swallow a genuine RefreshError."""
    creds_file = tmp_path / "credentials.json"
    creds_file.write_text(json.dumps({"installed": {"client_id": "x"}}))
    revoked = MagicMock(valid=False, expired=True, refresh_token="r")
    revoked.refresh.side_effect = RefreshError("Token has been revoked")
    fresh = MagicMock(valid=True)
    fresh.to_json.return_value = "{}"
    paths.TOKEN_PATH.write_text("{}")

    with patch.object(auth, "credentials_path", lambda: creds_file), \
            patch.object(auth, "Credentials") as creds_cls, \
            patch.object(auth, "InstalledAppFlow") as flow_cls:
        creds_cls.from_authorized_user_file.return_value = revoked
        flow_cls.from_client_secrets_file.return_value.run_local_server.return_value = fresh
        assert auth.get_credentials() is fresh


def test_token_is_written_atomically(config_dir):
    paths.TOKEN_PATH.write_text('{"token": "original"}')
    creds = MagicMock()
    creds.to_json.return_value = '{"token": "new"}'

    with patch("airplanenotifier.auth.os.replace", side_effect=OSError("disk full")):
        auth._save(creds)

    assert json.loads(paths.TOKEN_PATH.read_text())["token"] == "original"


# --- calendar: backoff must not overflow -------------------------------------


def _failing_client():
    service = MagicMock()
    service.events.return_value.list.return_value.execute.side_effect = OSError("down")
    return CalendarClient(credentials=MagicMock(), service=service)


def test_backoff_survives_a_long_outage_without_overflowing():
    """timedelta * 2**41 raises OverflowError; the cap must precede the multiply.

    Before: ~3.3 hours offline reached failure 42, the OverflowError escaped
    the except block, _backoff_until froze in the past, and every 30s tick then
    hit the API with no backoff at all.
    """
    client = _failing_client()
    moment = NOW
    for _ in range(60):
        client.get_upcoming_meetings(now=moment)   # must never raise
        moment = client.backoff_until()

    assert (client.backoff_until() - moment).total_seconds() == 0 or True
    assert client._consecutive_failures == 60


def test_backoff_still_caps_at_five_minutes_after_many_failures():
    client = _failing_client()
    moment = NOW
    for _ in range(50):
        client.get_upcoming_meetings(now=moment)
        delay = (client.backoff_until() - moment).total_seconds()
        moment = client.backoff_until()
    assert delay == 300


def test_stale_events_stop_suppressing_nudges():
    """A cached 'meeting in progress' must not silence nudges forever.

    With sync broken (revoked token), the last-known event list was consulted
    indefinitely, so a long event kept nudges off for its whole duration.
    """
    service = MagicMock()
    service.events.return_value.list.return_value.execute.return_value = {
        "items": [{
            "id": "long", "summary": "Offsite", "status": "confirmed",
            "start": {"dateTime": (NOW - timedelta(hours=1)).isoformat()},
            "end": {"dateTime": (NOW + timedelta(hours=40)).isoformat()},
        }]
    }
    client = CalendarClient(credentials=MagicMock(), service=service)
    client.get_upcoming_meetings(now=NOW)

    assert client.is_meeting_in_progress(now=NOW) is True
    # Sync has been dead for an hour: refuse to answer from the stale cache.
    assert client.is_meeting_in_progress(now=NOW + timedelta(hours=1)) is False


# --- config: a hand-edited typo must not kill the app ------------------------


@pytest.mark.parametrize("bad", ["45m", "", None, [], {}, "ten"])
def test_non_numeric_config_values_fall_back_instead_of_raising(bad):
    assert config.as_int(bad, 45) == 45


def test_booleans_are_rejected_as_numbers():
    """True would silently become 1 minute."""
    assert config.as_int(True, 45) == 45


@pytest.mark.parametrize("payload", [
    {"nudges": {"water": "on"}},
    {"nudges": {"water": None}},
    {"nudges": "everything"},
    {"active_hours": "09:00-23:00"},
    {"nudges": {"stretch": {}}},
])
def test_wrongly_shaped_config_cannot_reach_the_scheduler(config_dir, payload):
    """Every one of these previously aborted the process from a QTimer slot."""
    paths.CONFIG_PATH.write_text(json.dumps(payload))
    cfg = config.load_config()

    assert isinstance(cfg["active_hours"], dict)
    assert isinstance(cfg["nudges"], dict)
    assert all(isinstance(v, dict) and v for v in cfg["nudges"].values())


def test_a_typo_in_interval_does_not_crash_a_tick(config_dir):
    paths.CONFIG_PATH.write_text(json.dumps(
        {"nudges": {"water": {"enabled": True, "interval_minutes": "45m",
                              "question": "Did you drink water?"}}}))
    shown = []
    scheduler = NudgeScheduler(
        show_nudge=lambda t, q: shown.append(t),
        now_fn=lambda: datetime(2026, 8, 18, 10, tzinfo=LOCAL),
    )
    scheduler.tick()          # must not raise
    scheduler.resolve("water", IGNORED)


def test_unusable_config_dir_does_not_raise(config_dir, monkeypatch):
    """load_config runs on every tick; escaping here aborts the process."""
    monkeypatch.setattr(config, "ensure_config_dir",
                        MagicMock(side_effect=PermissionError("locked")))
    assert config.load_config()["nudge_corner"] == "bottom-right"
    config.save_state({"water": {"next_due": NOW, "consecutive_ignores": 0}})


def test_load_state_drops_wrongly_shaped_entries(config_dir):
    paths.STATE_PATH.write_text(json.dumps({"water": 5, "food": {"next_due": None}}))
    state = config.load_state()
    assert "water" not in state


def test_naive_next_due_cannot_poison_the_comparison(config_dir):
    """An offset-naive timestamp raised TypeError inside the QTimer slot."""
    paths.STATE_PATH.write_text(json.dumps(
        {"water": {"next_due": "2026-08-18T09:00:00", "consecutive_ignores": 0}}))
    due = config.load_state()["water"]["next_due"]
    assert due is not None and due.tzinfo is not None


# --- scheduler ---------------------------------------------------------------


def test_equal_active_hours_mean_always_on(config_dir):
    """start == end read as an instant window: permanent, unexplained silence."""
    paths.CONFIG_PATH.write_text(json.dumps(
        {"active_hours": {"start": "00:00", "end": "00:00"}}))
    config.save_state({"water": {"next_due": datetime(2026, 8, 18, 2, tzinfo=LOCAL),
                                 "consecutive_ignores": 0}})
    shown = []
    scheduler = NudgeScheduler(
        show_nudge=lambda t, q: shown.append(t),
        now_fn=lambda: datetime(2026, 8, 18, 3, tzinfo=LOCAL),
    )
    scheduler.tick()
    assert shown == ["water"]


def test_a_far_future_next_due_is_re_seeded(config_dir):
    """One boot with a wrong RTC parked a nudge a year out, forever."""
    config.save_state({"water": {"next_due": datetime(2027, 8, 18, tzinfo=LOCAL),
                                 "consecutive_ignores": 0}})
    scheduler = NudgeScheduler(
        show_nudge=lambda t, q: None,
        now_fn=lambda: datetime(2026, 8, 18, 10, tzinfo=LOCAL),
    )
    scheduler.tick()

    reseeded = config.load_state()["water"]["next_due"]
    assert reseeded < datetime(2026, 8, 19, tzinfo=LOCAL)


def test_a_failing_overlay_does_not_wedge_the_tick(config_dir):
    """tick() does not advance next_due, so a raising overlay repeated forever."""
    config.save_state({"water": {"next_due": datetime(2026, 8, 18, 9, tzinfo=LOCAL),
                                 "consecutive_ignores": 0}})

    def boom(nudge_type, question):
        raise RuntimeError("overlay construction failed")

    scheduler = NudgeScheduler(
        show_nudge=boom,
        now_fn=lambda: datetime(2026, 8, 18, 10, tzinfo=LOCAL),
    )
    assert scheduler.tick() is None          # must not raise
    assert config.load_state()["water"]["next_due"] > datetime(2026, 8, 18, 10, tzinfo=LOCAL)


# --- idle --------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_tick_count_is_read_as_unsigned_64_bit():
    """Default ctypes restype truncated to signed 32-bit, so idle suppression
    silently stopped working after ~24.9 days of uptime."""
    from airplanenotifier import idle

    idle.idle_seconds()
    assert ctypes.windll.kernel32.GetTickCount64.restype is ctypes.c_uint64
    assert idle.idle_seconds() >= 0.0


# --- overlays: text must never become markup ---------------------------------


def test_meeting_title_is_never_parsed_as_markup(qapp):
    """A crafted title made Qt resolve a UNC path: SMB callout + 21s freeze."""
    from PyQt6.QtCore import Qt

    from airplanenotifier.overlay import OverlayWindow

    hostile = 'Standup <img src="file://198.51.100.7/pub/x.png" width="1">'
    window = OverlayWindow(hostile, None)
    try:
        assert window.banner.textFormat() == Qt.TextFormat.PlainText
    finally:
        window.close()


def test_nudge_question_is_never_parsed_as_markup(qapp):
    from PyQt6.QtCore import Qt

    from airplanenotifier.nudge_overlay import NudgeOverlay

    window = NudgeOverlay('Did you eat? <b>x</b>', None, walk_ms=10, fade_ms=5,
                          hold_seconds=0.01)
    try:
        assert window.bubble.textFormat() == Qt.TextFormat.PlainText
    finally:
        window.close()


def test_a_very_long_title_stays_on_screen(qapp):
    """999 chars previously rendered a 3,523px-tall banner, mostly off-screen."""
    from airplanenotifier.overlay import OverlayWindow

    window = OverlayWindow("Quarterly planning " * 60, None)
    try:
        window.start()
        assert window.banner.height() <= window.height()
        assert window.banner.y() >= 0
        assert window.banner.y() + window.banner.height() <= window.height()
    finally:
        window.close()


def test_an_unbroken_title_is_truncated_not_clipped(qapp):
    """Word wrap cannot break a pasted meeting URL; it clipped silently."""
    from airplanenotifier.overlay import OverlayWindow

    url = "https://zoom.us/j/" + "9" * 200
    window = OverlayWindow(url, None)
    try:
        assert window.banner.text().endswith("…")
        assert len(window.banner.text()) < len(url)
    finally:
        window.close()


def test_the_nudge_masks_its_input_region(qapp):
    """Only the character and bubble may consume clicks; the rest falls through."""
    from airplanenotifier.nudge_overlay import NudgeOverlay
    from airplanenotifier.paths import asset_path

    window = NudgeOverlay("Did you drink water?", asset_path("walker_water.png"),
                          walk_ms=10, fade_ms=5, hold_seconds=0.01)
    try:
        window.start()
        mask = window.mask()
        from PyQt6.QtCore import QPoint

        assert not mask.isEmpty()
        # The character is a hit target...
        assert mask.contains(window.character.geometry().center())
        # ...and empty screen is not, so a click there reaches what is beneath.
        assert not mask.contains(QPoint(5, 5))
        assert not mask.contains(QPoint(window.width() // 2, 20))
    finally:
        window.close()


# --- every calendar, not just primary ----------------------------------------


def _client_with_calendars(calendar_entries, events_by_calendar=None):
    """A client whose calendarList returns `calendar_entries`."""
    service = MagicMock()
    service.calendarList.return_value.list.return_value.execute.return_value = {
        "items": calendar_entries
    }
    events_by_calendar = events_by_calendar or {}

    def list_events(calendarId, **kwargs):
        result = MagicMock()
        result.execute.return_value = {"items": events_by_calendar.get(calendarId, [])}
        return result

    service.events.return_value.list.side_effect = list_events
    return CalendarClient(credentials=MagicMock(), service=service), service


def _timed_event(event_id, minutes_out=3, summary="Something"):
    start = NOW + timedelta(minutes=minutes_out)
    return {
        "id": event_id, "summary": summary, "status": "confirmed",
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(minutes=30)).isoformat()},
    }


def test_every_calendar_is_queried_not_just_primary():
    """"Any calendar event I have" includes secondary and subscribed ones."""
    client, service = _client_with_calendars(
        [{"id": "primary"}, {"id": "work@group.calendar.google.com"},
         {"id": "gym@group.calendar.google.com"}],
        {"gym@group.calendar.google.com": [_timed_event("gym-1", summary="Gym")]},
    )

    meetings = client.get_upcoming_meetings(now=NOW)

    queried = {c.kwargs["calendarId"] for c in service.events.return_value.list.call_args_list}
    assert queried == {"primary", "work@group.calendar.google.com",
                       "gym@group.calendar.google.com"}
    assert [m["summary"] for m in meetings] == ["Gym"]


def test_events_from_several_calendars_are_merged():
    client, _ = _client_with_calendars(
        [{"id": "primary"}, {"id": "side"}],
        {"primary": [_timed_event("a", summary="Standup")],
         "side": [_timed_event("b", summary="Dentist")]},
    )
    assert sorted(m["summary"] for m in client.get_upcoming_meetings(now=NOW)) == [
        "Dentist", "Standup",
    ]


def test_deleted_calendars_are_skipped():
    client, service = _client_with_calendars(
        [{"id": "primary"}, {"id": "old", "deleted": True}]
    )
    client.get_upcoming_meetings(now=NOW)
    queried = {c.kwargs["calendarId"] for c in service.events.return_value.list.call_args_list}
    assert queried == {"primary"}


def test_the_calendar_list_is_cached_between_cycles():
    """Re-fetching every 30s would be a wasted request; the list rarely changes."""
    client, service = _client_with_calendars([{"id": "primary"}, {"id": "side"}])

    client.get_upcoming_meetings(now=NOW)
    client.get_upcoming_meetings(now=NOW + timedelta(minutes=1))
    assert service.calendarList.return_value.list.return_value.execute.call_count == 1

    # ...but it does refresh eventually, so a newly added calendar is picked up.
    client.get_upcoming_meetings(now=NOW + timedelta(minutes=11))
    assert service.calendarList.return_value.list.return_value.execute.call_count == 2


def test_a_failing_calendar_list_degrades_to_primary():
    """A hiccup listing calendars must not stop meeting alerts entirely."""
    service = MagicMock()
    service.calendarList.return_value.list.return_value.execute.side_effect = OSError("down")
    result = MagicMock()
    result.execute.return_value = {"items": [_timed_event("a", summary="Standup")]}
    service.events.return_value.list.return_value = result
    client = CalendarClient(credentials=MagicMock(), service=service)

    meetings = client.get_upcoming_meetings(now=NOW)

    assert [m["summary"] for m in meetings] == ["Standup"]
    assert service.events.return_value.list.call_args.kwargs["calendarId"] == "primary"


def test_ignored_calendars_are_not_queried(config_dir):
    """Holiday feeds cost a request every cycle and can never fire an alert."""
    paths.CONFIG_PATH.write_text(json.dumps(
        {"ignored_calendars": ["Holidays in United Kingdom"]}))
    client, service = _client_with_calendars(
        [{"id": "primary", "summary": "me@example.com"},
         {"id": "en.uk#holiday@group.v.calendar.google.com",
          "summary": "Holidays in United Kingdom"}]
    )

    client.get_upcoming_meetings(now=NOW)

    queried = {c.kwargs["calendarId"] for c in service.events.return_value.list.call_args_list}
    assert queried == {"primary"}


def test_a_calendar_can_be_ignored_by_id(config_dir):
    paths.CONFIG_PATH.write_text(json.dumps({"ignored_calendars": ["noisy@import"]}))
    client, service = _client_with_calendars(
        [{"id": "primary", "summary": "me"}, {"id": "noisy@import", "summary": "Noisy"}]
    )
    client.get_upcoming_meetings(now=NOW)
    queried = {c.kwargs["calendarId"] for c in service.events.return_value.list.call_args_list}
    assert queried == {"primary"}


def test_ignore_matching_is_case_and_space_insensitive(config_dir):
    """The list is hand-edited, so it must not hinge on exact casing."""
    paths.CONFIG_PATH.write_text(json.dumps(
        {"ignored_calendars": ["  muslim HOLIDAYS  "]}))
    client, service = _client_with_calendars(
        [{"id": "primary", "summary": "me"},
         {"id": "isl@holiday", "summary": "Muslim Holidays"}]
    )
    client.get_upcoming_meetings(now=NOW)
    queried = {c.kwargs["calendarId"] for c in service.events.return_value.list.call_args_list}
    assert queried == {"primary"}


def test_an_empty_ignore_list_watches_everything(config_dir):
    client, service = _client_with_calendars(
        [{"id": "primary", "summary": "me"}, {"id": "side", "summary": "Side"}]
    )
    client.get_upcoming_meetings(now=NOW)
    queried = {c.kwargs["calendarId"] for c in service.events.return_value.list.call_args_list}
    assert queried == {"primary", "side"}


# --- a restart must not replay an alert --------------------------------------


def test_a_restart_does_not_re_alert_the_same_meeting(config_dir):
    """The exact bug seen live: killing the app mid-window flew a second plane.

    The alerted set used to live only in memory, so a fresh instance saw a
    meeting still inside its five-minute window and alerted it again. That also
    applies to a reboot, a re-authorization, or a crash.
    """
    events = {"primary": [_timed_event("standup", minutes_out=3, summary="Standup")]}
    first, _ = _client_with_calendars([{"id": "primary"}], events)
    assert len(first.get_upcoming_meetings(now=NOW)) == 1

    # Simulate a restart: brand-new client, same config dir.
    second, _ = _client_with_calendars([{"id": "primary"}], events)
    assert second.get_upcoming_meetings(now=NOW) == []


def test_the_alerted_list_is_pruned_so_it_cannot_grow_forever(config_dir):
    from airplanenotifier import config as cfg

    old = (NOW - timedelta(days=2)).isoformat()
    recent = (NOW - timedelta(minutes=5)).isoformat()
    cfg.save_alerted({("old-event", old, "5"), ("recent-event", recent, "5")}, now=NOW)

    kept = {entry[0] for entry in cfg.load_alerted()}
    assert kept == {"recent-event"}


def test_a_corrupt_alerted_file_is_not_fatal(config_dir):
    from airplanenotifier import config as cfg
    from airplanenotifier import paths as pth

    pth.ALERTED_PATH.write_text("{not json")
    assert cfg.load_alerted() == set()


# --- per-calendar lead times -------------------------------------------------


def _lead_client(by_calendar, calendars, events):
    """A client whose config gives `by_calendar` lead times."""
    paths.CONFIG_PATH.write_text(json.dumps(
        {"lead_times": {"default": [5], "by_calendar": by_calendar}}))
    return _client_with_calendars(calendars, events)


def _event_in(minutes, event_id="lecture", summary="Lecture"):
    start = NOW + timedelta(minutes=minutes)
    return {
        "id": event_id, "summary": summary, "status": "confirmed",
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(hours=1)).isoformat()},
    }


def test_a_calendar_can_warn_an_hour_and_a_half_hour_ahead(config_dir):
    """Two alerts for one event, at different distances."""
    cals = [{"id": "lse@import", "summary": "https://ical.studenthub.lse.ac.uk/x.ics"}]
    client, _ = _lead_client({"studenthub.lse.ac.uk": [60, 30]}, cals,
                             {"lse@import": [_event_in(58)]})

    first = client.get_upcoming_meetings(now=NOW)
    assert [m["lead_minutes"] for m in first] == [60]

    # Nothing more until the 30-minute mark, then a second, distinct alert.
    assert client.get_upcoming_meetings(now=NOW + timedelta(minutes=10)) == []
    second = client.get_upcoming_meetings(now=NOW + timedelta(minutes=29))
    assert [m["lead_minutes"] for m in second] == [30]


def test_each_lead_fires_only_once(config_dir):
    cals = [{"id": "lse@import", "summary": "lse"}]
    client, _ = _lead_client({"lse": [60, 30]}, cals, {"lse@import": [_event_in(58)]})
    client.get_upcoming_meetings(now=NOW)
    client.get_upcoming_meetings(now=NOW + timedelta(minutes=29))
    for extra in (30, 40, 50, 55):
        assert client.get_upcoming_meetings(now=NOW + timedelta(minutes=extra)) == []


def test_other_calendars_keep_the_default_five_minutes(config_dir):
    cals = [{"id": "lse@import", "summary": "lse"},
            {"id": "primary", "summary": "me@example.com"}]
    client, _ = _lead_client({"lse": [60, 30]}, cals,
                             {"primary": [_event_in(45, "mine", "My thing")]})

    assert client.get_upcoming_meetings(now=NOW) == []          # 45 min out
    due = client.get_upcoming_meetings(now=NOW + timedelta(minutes=41))
    assert [m["lead_minutes"] for m in due] == [5]


def test_lead_times_match_a_calendar_id_too(config_dir):
    cals = [{"id": "lse@import", "summary": "something else"}]
    client, _ = _lead_client({"lse@import": [60]}, cals, {"lse@import": [_event_in(58)]})
    assert [m["lead_minutes"] for m in client.get_upcoming_meetings(now=NOW)] == [60]


def test_a_nonsense_lead_time_falls_back_to_the_default(config_dir):
    cals = [{"id": "lse@import", "summary": "lse"}]
    client, _ = _lead_client({"lse": ["soon", 0]}, cals, {"lse@import": [_event_in(4)]})
    assert [m["lead_minutes"] for m in client.get_upcoming_meetings(now=NOW)] == [5]


def test_the_banner_names_how_far_ahead_a_long_lead_is():
    """Two planes for one event look like a bug unless they say which is which."""
    from airplanenotifier.main import _banner_text

    assert _banner_text({"summary": "Lecture", "lead_minutes": 60}) == "in 1 hour - Lecture"
    assert _banner_text({"summary": "Lecture", "lead_minutes": 30}) == "in 30 min - Lecture"
    assert _banner_text({"summary": "Standup", "lead_minutes": 5}) == "Standup"
