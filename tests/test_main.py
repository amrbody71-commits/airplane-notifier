"""U6 -- application wiring (R4, R5, R14) and the U10 nudge wiring.

Covers the coordination the individual units cannot: overlay queueing, the
single-overlay-at-a-time rule, and the handoff between meeting overlays and the
nudge scheduler.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PyQt6", reason="PyQt6 is required for widget tests")

from PyQt6.QtTest import QTest  # noqa: E402

from airplanenotifier import config  # noqa: E402
from airplanenotifier.main import NUDGE_INTERVAL_MS, SYNC_INTERVAL_MS, NotifierApp  # noqa: E402
from airplanenotifier.nudges import ACKNOWLEDGED, IGNORED  # noqa: E402

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def meeting(event_id="evt", summary="Team Standup"):
    return {"id": event_id, "summary": summary, "start_time": NOW + timedelta(minutes=3)}


@pytest.fixture
def notifier(qapp, config_dir):
    made = []

    def _make(meetings=None, in_progress=False):
        client = MagicMock()
        client.get_upcoming_meetings.return_value = list(meetings or [])
        client.is_meeting_in_progress.return_value = in_progress
        client.last_sync_time.return_value = NOW

        tray = MagicMock()
        app = NotifierApp(
            qapp,
            client_factory=lambda creds: client,
            tray_factory=lambda _app: tray,
        )
        app._client = client
        made.append((app, client, tray))
        return app, client, tray

    yield _make
    for app, _client, _tray in made:
        if app._active_overlay is not None:
            app._active_overlay.close()


# --- timers -----------------------------------------------------------------


def test_sync_and_nudge_timers_are_independent(notifier):
    """KTD13: nudge responsiveness must not depend on the network."""
    app, _, _ = notifier()
    assert app.sync_timer is not app.nudge_timer
    assert app.sync_timer.interval() == SYNC_INTERVAL_MS == 30_000
    assert app.nudge_timer.interval() == NUDGE_INTERVAL_MS == 30_000


# --- meeting overlays -------------------------------------------------------


def test_a_due_meeting_shows_an_overlay(notifier):
    app, _, _ = notifier()
    app._on_sync_done([meeting(summary="Q3 Review")])

    assert app.is_overlay_active() is True
    assert "Q3 Review" in app._active_overlay.banner.text()


def test_two_due_meetings_are_shown_one_at_a_time(notifier):
    """Plan review item 6: the second must not be dropped, nor overlap."""
    app, _, _ = notifier()
    app._on_sync_done([meeting("a", "First"), meeting("b", "Second")])

    assert "First" in app._active_overlay.banner.text()
    assert len(app._pending_meetings) == 1

    app._on_meeting_overlay_finished()
    assert "Second" in app._active_overlay.banner.text()


def test_the_queue_empties_after_the_last_meeting(notifier):
    app, _, _ = notifier()
    app._on_sync_done([meeting()])
    app._on_meeting_overlay_finished()

    assert app.is_overlay_active() is False
    assert app._pending_meetings == []


def test_a_finished_meeting_overlay_pauses_nudges(notifier):
    """R23: a character must not walk on immediately behind the plane."""
    app, _, _ = notifier()
    app._on_sync_done([meeting()])
    app._on_meeting_overlay_finished()

    assert app.scheduler._meeting_overlay_closed_at is not None


def test_sync_updates_the_tray_tooltip(notifier):
    app, _, tray = notifier()
    app._on_sync_done([])

    tray.set_last_sync.assert_called_once_with(NOW)


def test_a_sync_failure_is_swallowed(notifier):
    app, _, _ = notifier()
    app._on_sync_failed(OSError("network down"))  # must not raise


def test_sync_does_nothing_before_authorization(notifier):
    app, client, _ = notifier()
    app._client = None
    app.sync_now()
    client.get_upcoming_meetings.assert_not_called()


# --- nudges -----------------------------------------------------------------


def test_a_nudge_becomes_the_active_overlay(notifier):
    app, _, _ = notifier()
    app._show_nudge("water", "Did you drink water?")

    assert app.is_overlay_active() is True
    assert app._active_overlay.bubble.text() == "Did you drink water?"


def test_acknowledging_a_nudge_reaches_the_scheduler(notifier):
    app, _, _ = notifier()
    app.scheduler.resolve = MagicMock()
    app._show_nudge("water", "Did you drink water?")

    app._active_overlay.click_at(app._active_overlay.character.geometry().center())

    app.scheduler.resolve.assert_called_once_with("water", ACKNOWLEDGED)


def test_ignoring_a_nudge_reaches_the_scheduler(notifier):
    app, _, _ = notifier()
    app.scheduler.resolve = MagicMock()
    app._show_nudge("water", "Did you drink water?")
    app._active_overlay._resolve(app._active_overlay.ignored)

    app.scheduler.resolve.assert_called_once_with("water", IGNORED)


def test_the_nudge_uses_the_configured_corner(notifier, config_dir):
    import json

    from airplanenotifier import paths
    paths.CONFIG_PATH.write_text(json.dumps({"nudge_corner": "top-left"}))

    app, _, _ = notifier()
    app._show_nudge("food", "Did you eat?")

    assert app._active_overlay.corner == "top-left"


def test_each_nudge_type_gets_its_own_character(notifier):
    app, _, _ = notifier()
    app._show_nudge("food", "Did you eat?")
    food_pixmap = app._active_overlay._pixmap.toImage()
    app._active_overlay.close()
    app._active_overlay = None

    app._show_nudge("water", "Did you drink water?")
    water_pixmap = app._active_overlay._pixmap.toImage()

    assert food_pixmap != water_pixmap


def test_a_scheduler_tick_is_blocked_while_an_overlay_is_up(notifier):
    """R30: the scheduler asks the app, so the two cannot collide."""
    app, _, _ = notifier()
    config.save_state({"water": {"next_due": NOW - timedelta(minutes=1),
                                 "consecutive_ignores": 0}})
    app._on_sync_done([meeting()])

    assert app.scheduler.tick() is None


def test_resolving_a_nudge_drains_any_queued_meeting(notifier):
    """A meeting that arrived during a nudge must not sit in the queue."""
    app, _, _ = notifier()
    app._show_nudge("water", "Did you drink water?")
    app._pending_meetings.append(meeting(summary="Later"))

    app._on_nudge_resolved("water", IGNORED)

    assert "Later" in app._active_overlay.banner.text()


# --- authorization ----------------------------------------------------------


def test_missing_credentials_keeps_the_app_running(notifier):
    app, _, tray = notifier()
    app._on_auth_failed(FileNotFoundError("credentials.json missing"))

    assert tray.show_message.called
    assert "credentials.json" in tray.show_message.call_args.args[0]
    assert app.sync_timer.isActive() is False


def test_successful_auth_starts_syncing(notifier):
    app, client, _ = notifier()
    app._on_auth_done(MagicMock())

    assert app.sync_timer.isActive() is True
    QTest.qWait(50)  # the first sync runs on a worker thread
    assert client.get_upcoming_meetings.called
