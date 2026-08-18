"""U8 -- nudge configuration, scheduler state, and the nudge log (R26, R27).

The config file is the whole tuning mechanism for nudge frequency, so these
tests care a lot about it staying readable and never being able to stop the app
from starting.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from airplanenotifier import config, paths

LATER = datetime(2026, 8, 18, 14, 5, tzinfo=timezone(timedelta(hours=1)))


# --- config -----------------------------------------------------------------


def test_first_run_writes_the_defaults(config_dir):
    loaded = config.load_config()

    assert paths.CONFIG_PATH.exists()
    assert loaded["nudges"]["water"]["interval_minutes"] == 90
    assert loaded["nudges"]["food"]["at"] == ["12:30"]
    assert loaded["nudge_corner"] == "bottom-right"
    assert loaded["active_hours"] == {"start": "09:00", "end": "23:00"}


def test_hand_edited_interval_is_picked_up_on_the_next_load(config_dir):
    config.load_config()
    raw = json.loads(paths.CONFIG_PATH.read_text())
    raw["nudges"]["water"]["interval_minutes"] = 5
    paths.CONFIG_PATH.write_text(json.dumps(raw))

    # No restart, no cached instance: the next read simply sees the new value.
    assert config.load_config()["nudges"]["water"]["interval_minutes"] == 5


def test_corrupt_config_is_quarantined_and_replaced(config_dir):
    paths.CONFIG_PATH.write_text("{{{")

    loaded = config.load_config()

    assert loaded["nudges"]["water"]["interval_minutes"] == 90
    assert (config_dir / "config.json.bad").exists()
    assert json.loads(paths.CONFIG_PATH.read_text())["nudge_corner"] == "bottom-right"


def test_partial_config_merges_over_the_defaults(config_dir):
    """A config written by an older version must not lose new keys."""
    paths.CONFIG_PATH.write_text(json.dumps({"nudges": {"water": {"interval_minutes": 20}}}))

    loaded = config.load_config()

    assert loaded["nudges"]["water"]["interval_minutes"] == 20   # kept
    assert loaded["nudges"]["water"]["question"]                 # filled in
    assert loaded["nudges"]["food"]["at"] == ["12:30"]           # whole type filled in
    assert loaded["idle_suppress_minutes"] == 10
    assert loaded["nudge_corner"] == "bottom-right"


def test_unknown_corner_falls_back_to_bottom_right(config_dir):
    paths.CONFIG_PATH.write_text(json.dumps({"nudge_corner": "middle"}))
    assert config.nudge_corner(config.load_config()) == "bottom-right"


@pytest.mark.parametrize("corner", ["bottom-right", "top-right", "bottom-left", "top-left"])
def test_all_four_corners_are_accepted(config_dir, corner):
    paths.CONFIG_PATH.write_text(json.dumps({"nudge_corner": corner}))
    assert config.nudge_corner(config.load_config()) == corner


def test_config_of_the_wrong_shape_is_quarantined(config_dir):
    """Valid JSON that is not an object must not crash the tick either."""
    paths.CONFIG_PATH.write_text("[1, 2, 3]")
    assert config.load_config()["nudge_corner"] == "bottom-right"


# --- state ------------------------------------------------------------------


def test_state_round_trips_with_timezone_intact(config_dir):
    config.save_state({"water": {"next_due": LATER, "consecutive_ignores": 2}})

    loaded = config.load_state()

    assert loaded["water"]["next_due"] == LATER
    assert loaded["water"]["next_due"].utcoffset() == timedelta(hours=1)
    assert loaded["water"]["consecutive_ignores"] == 2


def test_missing_state_loads_as_empty(config_dir):
    assert config.load_state() == {}


def test_corrupt_state_loads_as_empty_rather_than_raising(config_dir):
    paths.STATE_PATH.write_text("not json at all")
    assert config.load_state() == {}


def test_state_is_written_atomically(config_dir, monkeypatch):
    """A failure mid-write must leave the previous state readable, not a stub."""
    config.save_state({"water": {"next_due": LATER, "consecutive_ignores": 0}})
    original = paths.STATE_PATH.read_text()

    def failing_replace(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(config.os, "replace", failing_replace)
    config.save_state({"water": {"next_due": LATER, "consecutive_ignores": 99}})

    assert paths.STATE_PATH.read_text() == original
    assert config.load_state()["water"]["consecutive_ignores"] == 0


def test_state_write_leaves_no_temp_files_behind(config_dir):
    config.save_state({"water": {"next_due": LATER, "consecutive_ignores": 0}})
    leftovers = [p.name for p in config_dir.iterdir() if p.name != "state.json"]
    assert leftovers == []


# --- nudge log --------------------------------------------------------------


def test_each_nudge_appends_one_json_line(config_dir):
    for outcome in ("acknowledged", "ignored", "acknowledged"):
        config.append_nudge_log({"type": "water", "outcome": outcome})

    lines = paths.NUDGE_LOG_PATH.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    assert [json.loads(line)["outcome"] for line in lines] == [
        "acknowledged", "ignored", "acknowledged",
    ]


def test_log_entries_carry_a_timestamp(config_dir):
    config.append_nudge_log({"type": "food", "outcome": "ignored"})
    entry = json.loads(paths.NUDGE_LOG_PATH.read_text(encoding="utf-8").strip())

    assert "timestamp" in entry
    parsed = datetime.fromisoformat(entry["timestamp"])
    assert parsed.tzinfo is not None  # local time with offset, readable by hand


def test_log_failure_never_breaks_a_nudge(config_dir, monkeypatch):
    """A read-only config dir must not stop the character appearing."""
    def failing_open(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr("builtins.open", failing_open)

    config.append_nudge_log({"type": "water", "outcome": "ignored"})  # must not raise
