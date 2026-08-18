"""U10 -- nudge scheduling (R19-R25, R28, R30).

The scheduler is pure local computation over two small JSON files, so it is
fully testable with an injected clock and no Qt involved.

A recurring theme: a gate that blocks a nudge must not mutate state. If it did,
a nudge falling due at 03:00 would silently roll forward and the user would
never see it, and one blocked by a meeting would be lost rather than deferred.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from airplanenotifier import config, paths
from airplanenotifier.nudges import ACKNOWLEDGED, IGNORED, NudgeScheduler

LOCAL = datetime.now().astimezone().tzinfo


def at(hour, minute=0, day=18):
    """A local-time moment on a fixed day, so active-hours logic is testable."""
    return datetime(2026, 8, day, hour, minute, tzinfo=LOCAL)


class Recorder:
    """Stands in for the overlay: records what would have been shown."""

    def __init__(self):
        self.shown = []

    def __call__(self, nudge_type, question):
        self.shown.append((nudge_type, question))

    @property
    def types(self):
        return [t for t, _ in self.shown]


@pytest.fixture
def build(config_dir):
    """Make a scheduler with everything permissive unless overridden."""
    def _build(now=at(10), meeting=False, overlay=False, idle=0.0, locked=False):
        recorder = Recorder()
        clock = {"now": now}
        scheduler = NudgeScheduler(
            show_nudge=recorder,
            is_meeting_in_progress=lambda: meeting,
            is_overlay_active=lambda: overlay,
            idle_seconds=lambda: idle,
            is_locked=lambda: locked,
            now_fn=lambda: clock["now"],
        )
        return scheduler, recorder, clock
    return _build


def due_now(nudge_type="water", moment=None):
    """Write state making `nudge_type` due at `moment`."""
    config.save_state({nudge_type: {"next_due": moment or at(9), "consecutive_ignores": 0}})


# --- firing -----------------------------------------------------------------


def test_fresh_install_does_not_fire_immediately(build):
    scheduler, recorder, _ = build()
    scheduler.tick()

    assert recorder.shown == []
    # ...but it has seeded the schedule, so it will fire later.
    assert config.load_state()["water"]["next_due"] > at(10)


def test_due_nudge_fires_and_advances_the_schedule(build):
    due_now("water")
    scheduler, recorder, _ = build(now=at(10))

    scheduler.tick()
    scheduler.resolve("water", IGNORED)

    assert recorder.types == ["water"]
    assert recorder.shown[0][1] == "Did you drink water?"


def test_acknowledging_resets_to_the_full_interval(build):
    due_now("water")
    scheduler, _, _ = build(now=at(10))

    scheduler.tick()
    scheduler.resolve("water", ACKNOWLEDGED)

    state = config.load_state()["water"]
    assert state["next_due"] == at(10) + timedelta(minutes=90)
    assert state["consecutive_ignores"] == 0


def test_ignoring_re_asks_sooner(build):
    """R25: ignoring is a valid answer, but it earns a shorter backoff."""
    due_now("water")
    scheduler, _, _ = build(now=at(10))

    scheduler.tick()
    scheduler.resolve("water", IGNORED)

    state = config.load_state()["water"]
    assert state["next_due"] == at(10) + timedelta(minutes=15)
    assert state["consecutive_ignores"] == 1


def test_three_consecutive_ignores_all_get_a_short_re_ask(build):
    """R25 says up to 3; "<" instead of "<=" silently gave only 2."""
    for seeded in (0, 1, 2):
        config.save_state({"water": {"next_due": at(9), "consecutive_ignores": seeded}})
        scheduler, _, _ = build(now=at(10))
        scheduler.tick()
        scheduler.resolve("water", IGNORED)
        assert config.load_state()["water"]["next_due"] == at(10) + timedelta(minutes=15)


def test_a_fourth_consecutive_ignore_backs_off_to_the_full_interval(build):
    """Three short re-asks is enough (R25); the fourth stops nagging."""
    config.save_state({"water": {"next_due": at(9), "consecutive_ignores": 3}})
    scheduler, _, _ = build(now=at(10))

    scheduler.tick()
    scheduler.resolve("water", IGNORED)

    state = config.load_state()["water"]
    assert state["next_due"] == at(10) + timedelta(minutes=90)
    assert state["consecutive_ignores"] == 0


def test_acknowledging_clears_an_ignore_streak(build):
    config.save_state({"water": {"next_due": at(9), "consecutive_ignores": 2}})
    scheduler, _, _ = build(now=at(10))

    scheduler.tick()
    scheduler.resolve("water", ACKNOWLEDGED)

    assert config.load_state()["water"]["consecutive_ignores"] == 0


def test_only_the_most_overdue_nudge_fires(build):
    """R30: never stack two characters on screen."""
    config.save_state({
        "water": {"next_due": at(9, 50), "consecutive_ignores": 0},
        "food": {"next_due": at(9, 10), "consecutive_ignores": 0},
    })
    scheduler, recorder, _ = build(now=at(10))

    scheduler.tick()

    assert recorder.types == ["food"]  # overdue by 50 min vs 10


def test_a_disabled_nudge_never_fires(build, config_dir):
    paths.CONFIG_PATH.write_text(json.dumps({"nudges": {"water": {"enabled": False}}}))
    config.save_state({
        "water": {"next_due": at(9), "consecutive_ignores": 0},
        "food": {"next_due": at(9, 30), "consecutive_ignores": 0},
    })
    scheduler, recorder, _ = build(now=at(10))

    scheduler.tick()

    assert recorder.types == ["food"]


def test_a_hand_edited_interval_applies_without_a_restart(build, config_dir):
    """R26: the whole tuning workflow depends on this."""
    due_now("water")
    scheduler, _, clock = build(now=at(10))
    scheduler.tick()
    scheduler.resolve("water", ACKNOWLEDGED)

    raw = json.loads(paths.CONFIG_PATH.read_text())
    raw["nudges"]["water"]["interval_minutes"] = 5
    paths.CONFIG_PATH.write_text(json.dumps(raw))

    config.save_state({"water": {"next_due": at(11), "consecutive_ignores": 0}})
    clock["now"] = at(11)
    scheduler.tick()
    scheduler.resolve("water", ACKNOWLEDGED)

    assert config.load_state()["water"]["next_due"] == at(11) + timedelta(minutes=5)


# --- suppression: a blocked nudge is deferred, never dropped -----------------


@pytest.mark.parametrize("blocker", [
    {"meeting": True},
    {"overlay": True},
    {"locked": True},
    {"idle": 15 * 60},
])
def test_blocked_nudges_do_not_fire_and_do_not_lose_their_slot(build, blocker):
    due_now("water")
    scheduler, recorder, _ = build(now=at(10), **blocker)

    scheduler.tick()

    assert recorder.shown == []
    # State untouched, so it fires on the first tick after the blocker clears.
    assert config.load_state()["water"]["next_due"] == at(9)


def test_a_deferred_nudge_fires_once_the_blocker_clears(build):
    due_now("water")
    scheduler, recorder, _ = build(now=at(10), meeting=True)
    scheduler.tick()
    assert recorder.shown == []

    unblocked, recorder2, _ = build(now=at(10, 1))
    unblocked.tick()
    assert recorder2.types == ["water"]


def test_brief_idleness_does_not_suppress(build):
    """Ten minutes is the threshold; a short pause must still get nudged."""
    due_now("water")
    scheduler, recorder, _ = build(now=at(10), idle=4 * 60)

    scheduler.tick()

    assert recorder.types == ["water"]


def test_a_nudge_is_suppressed_just_after_a_meeting_overlay(build):
    """R23: the airplane and a character should not arrive back to back."""
    due_now("water")
    scheduler, recorder, _ = build(now=at(10))
    scheduler.note_meeting_overlay_closed(at(10) - timedelta(seconds=30))

    scheduler.tick()

    assert recorder.shown == []


def test_the_post_meeting_pause_expires(build):
    due_now("water")
    scheduler, recorder, _ = build(now=at(10))
    scheduler.note_meeting_overlay_closed(at(9, 58))

    scheduler.tick()

    assert recorder.types == ["water"]


# --- quiet hours ------------------------------------------------------------


def test_no_nudges_outside_active_hours(build):
    due_now("water")
    scheduler, recorder, _ = build(now=at(2))

    scheduler.tick()

    assert recorder.shown == []
    assert config.load_state()["water"]["next_due"] == at(9)


def test_quiet_hours_do_not_queue_up_a_burst(build):
    """R22: timers are held, not queued. One nudge at 09:00, not three."""
    config.save_state({
        "water": {"next_due": at(1), "consecutive_ignores": 0},
        # Parked well past the window under test, so only water is in play.
        "food": {"next_due": at(20), "consecutive_ignores": 0},
    })
    scheduler, recorder, clock = build(now=at(2))

    for hour in (2, 3, 4, 5, 6, 7, 8):
        clock["now"] = at(hour)
        scheduler.tick()
    assert recorder.shown == []

    clock["now"] = at(9, 1)
    scheduler.tick()
    scheduler.resolve("water", ACKNOWLEDGED)
    clock["now"] = at(9, 2)
    scheduler.tick()

    assert recorder.types == ["water"]


def test_an_overnight_active_window_is_supported(build, config_dir):
    """A window that crosses midnight must not read as an empty range."""
    paths.CONFIG_PATH.write_text(json.dumps({
        "active_hours": {"start": "22:00", "end": "04:00"}
    }))
    due_now("water", at(1))
    scheduler, recorder, _ = build(now=at(2))

    scheduler.tick()

    assert recorder.types == ["water"]


# --- restart behaviour ------------------------------------------------------


def test_restarting_keeps_the_existing_schedule(build):
    """R27: a restart must neither reset the cadence nor fire immediately."""
    config.save_state({"water": {"next_due": at(10, 5), "consecutive_ignores": 1}})

    scheduler, recorder, _ = build(now=at(10))
    scheduler.tick()
    assert recorder.shown == []

    later, recorder2, _ = build(now=at(10, 6))
    later.tick()
    assert recorder2.types == ["water"]


def test_seeded_schedule_is_persisted_immediately(build):
    """Otherwise every restart re-seeds and the nudge never arrives."""
    scheduler, _, _ = build(now=at(10))
    scheduler.tick()

    saved = config.load_state()
    assert saved["water"]["next_due"] == at(10) + timedelta(minutes=90)
    # food is fixed-time now, so it lands on today's 13:00 rather than
    # counting forward from whenever the app happened to start.
    assert saved["food"]["next_due"] == at(12, 30)


# --- logging ----------------------------------------------------------------


def test_each_appearance_is_logged_with_its_outcome(build):
    due_now("water")
    scheduler, _, _ = build(now=at(10))

    scheduler.tick()
    scheduler.resolve("water", ACKNOWLEDGED)

    entry = json.loads(paths.NUDGE_LOG_PATH.read_text(encoding="utf-8").strip())
    assert entry["type"] == "water"
    assert entry["outcome"] == ACKNOWLEDGED


def test_a_suppressed_nudge_is_not_logged(build):
    """The log must reflect what appeared, or it is useless for tuning."""
    due_now("water")
    scheduler, _, _ = build(now=at(10), meeting=True)

    scheduler.tick()

    assert not paths.NUDGE_LOG_PATH.exists()


def test_resolving_an_unknown_type_is_ignored(build):
    scheduler, _, _ = build(now=at(10))
    scheduler.resolve("coffee", ACKNOWLEDGED)  # must not raise


# --- fixed-time nudges -------------------------------------------------------


def test_a_fixed_time_nudge_lands_on_the_clock_not_an_interval(build):
    """Lunch is an appointment with yourself; it must not drift."""
    scheduler, recorder, clock = build(now=at(9, 30))
    scheduler.tick()
    assert config.load_state()["food"]["next_due"] == at(12, 30)

    # Park water well clear, so only food is in play at 13:01.
    state = config.load_state()
    state["water"]["next_due"] = at(20)
    config.save_state(state)

    clock["now"] = at(12, 31)
    scheduler.tick()
    assert "food" in recorder.types


def test_a_fixed_time_nudge_does_not_fire_early(build):
    scheduler, recorder, clock = build(now=at(9))
    scheduler.tick()
    clock["now"] = at(12, 29)
    scheduler.tick()
    assert "food" not in recorder.types


def test_acknowledging_a_fixed_time_nudge_schedules_tomorrow(build):
    config.save_state({"food": {"next_due": at(12, 30), "consecutive_ignores": 0}})
    scheduler, _, _ = build(now=at(12, 31))
    scheduler.tick()
    scheduler.resolve("food", ACKNOWLEDGED)

    assert config.load_state()["food"]["next_due"] == at(12, 30, day=19)


def test_a_missed_lunch_nudge_is_not_asked_in_the_evening(build):
    """A laptop opened at 20:00 should not ask about lunch."""
    config.save_state({"food": {"next_due": at(12, 30), "consecutive_ignores": 0}})
    scheduler, recorder, _ = build(now=at(20))

    scheduler.tick()

    assert "food" not in recorder.types
    # ...and it has rolled forward to tomorrow rather than staying stuck.
    assert config.load_state()["food"]["next_due"] == at(12, 30, day=19)


def test_a_slightly_late_lunch_nudge_still_fires(build):
    """Within the catch-up window it is still worth asking."""
    config.save_state({"food": {"next_due": at(12, 30), "consecutive_ignores": 0}})
    scheduler, recorder, _ = build(now=at(14))

    scheduler.tick()

    assert "food" in recorder.types


def test_several_fixed_times_are_supported(build, config_dir):
    paths.CONFIG_PATH.write_text(json.dumps(
        {"nudges": {"food": {"enabled": True, "at": ["08:00", "13:00", "19:00"],
                             "question": "Did you eat?"}}}))
    scheduler, _, _ = build(now=at(9))
    scheduler.tick()
    assert config.load_state()["food"]["next_due"] == at(13)


def test_a_malformed_time_is_ignored_not_fatal(build, config_dir):
    paths.CONFIG_PATH.write_text(json.dumps(
        {"nudges": {"food": {"enabled": True, "at": ["lunchtime", "13:00"],
                             "question": "Did you eat?"}}}))
    scheduler, _, _ = build(now=at(9))
    scheduler.tick()   # must not raise
    assert config.load_state()["food"]["next_due"] == at(13)


def test_water_still_uses_an_interval(build):
    """The two styles coexist: water counts forward, food waits for the clock."""
    due_now("water")
    scheduler, _, _ = build(now=at(10))
    scheduler.tick()
    scheduler.resolve("water", ACKNOWLEDGED)
    assert config.load_state()["water"]["next_due"] == at(10) + timedelta(minutes=90)
