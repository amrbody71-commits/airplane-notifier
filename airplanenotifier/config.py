"""Nudge configuration, scheduler state, and the nudge log (U8 -- R26, R27).

Three files, all under ``~/.airplane-notifier/``:

``config.json``
    Hand-editable timings, re-read on **every** tick (R26). There is no cached
    instance and no filesystem watcher: re-reading a small JSON file every 30
    seconds is free, and one tick of latency is imperceptible when you are
    tuning intervals by hand.

``state.json``
    Wall-clock ``next_due`` per nudge type, so restarting the app does not
    reset the cadence or fire an immediate burst (R27, KTD10).

``nudge-log.jsonl``
    One line per nudge appearance. This is the raw material for deciding
    whether 45 minutes is actually the right interval (R28).

Nothing here may prevent the app from starting. A corrupt file is quarantined
and replaced with defaults, and a failed log write is swallowed -- losing a log
line is a far better outcome than losing the nudge.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from typing import Any, Optional

from airplanenotifier import paths
from airplanenotifier.paths import ensure_config_dir
from airplanenotifier.timeutil import parse_iso8601
from airplanenotifier.log import diagnostic

VALID_CORNERS = ("bottom-right", "top-right", "bottom-left", "top-left")
DEFAULT_CORNER = "bottom-right"

DEFAULT_CONFIG: dict[str, Any] = {
    "active_hours": {"start": "09:00", "end": "23:00"},
    "idle_suppress_minutes": 10,
    # A walking character needs a floor; entering at the top of the screen
    # reads as walking through mid-air (R20b).
    "nudge_corner": DEFAULT_CORNER,
    # Calendars to skip, by name or id. Holiday feeds and empty imports cost a
    # request every cycle and can never produce an alert.
    "ignored_calendars": [],
    # How late a fixed-time nudge may still fire if the machine was off or
    # locked at the appointed hour. Past this it waits for tomorrow, so a
    # laptop opened at 20:00 is not asked about lunch.
    "catch_up_minutes": 120,
    # Pixels per second, not a total duration: a fixed duration would make the
    # plane faster the more screens you attach. Lower = slower and easier to read.
    "flyover_speed": 240,
    "nudges": {
        # Interval-based: every N minutes while active.
        "water": {
            "enabled": True,
            "interval_minutes": 90,
            "question": "Did you drink water?",
        },
        # Time-based: fires at these clock times, whatever the calendar says.
        # A meal is an appointment with yourself, not something that drifts.
        "food": {
            "enabled": True,
            "at": ["12:30"],
            "question": "Did you have lunch?",
        },
    },
    "ignore_backoff_minutes": 15,
    "max_consecutive_reasks": 3,
    "hold_seconds": 4,
}


def as_int(value: object, fallback: int) -> int:
    """Coerce a hand-edited config value to int, falling back on nonsense.

    config.json is advertised as user-editable and is re-read on every tick, so
    a typo like "45m" reaches arithmetic inside a QTimer slot. PyQt routes an
    unhandled exception in a slot to qFatal(), so that typo does not skip a
    tick -- it kills the tray app outright, with no window and (before the log
    existed) no trace. Every numeric read goes through here.
    """
    try:
        if isinstance(value, bool):  # bool is an int subclass; treat as invalid
            raise TypeError
        return int(value)
    except (TypeError, ValueError):
        print(
            f"airplane-notifier: config value {value!r} is not a number; "
            f"using {fallback}",
            file=diagnostic,
        )
        return fallback


def _deep_merge(defaults: dict, override: dict) -> dict:
    """Overlay `override` on `defaults`, recursing into nested dicts.

    A shallow merge would mean a config naming only ``nudges.water`` silently
    lost the ``food`` nudge entirely.
    """
    merged = dict(defaults)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _write_default_config() -> dict:
    ensure_config_dir()
    try:
        paths.CONFIG_PATH.write_text(
            json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        print(f"airplane-notifier: could not write config: {exc}", file=diagnostic)
    return json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy


def _quarantine_config(reason: str) -> None:
    """Move an unusable config aside so the user can see what they broke."""
    bad = paths.CONFIG_PATH.with_suffix(".json.bad")
    print(
        f"airplane-notifier: {reason}; moving it to {bad.name} and restoring defaults",
        file=diagnostic,
    )
    try:
        os.replace(paths.CONFIG_PATH, bad)
    except OSError as exc:
        print(f"airplane-notifier: could not quarantine config: {exc}", file=diagnostic)


def load_config() -> dict:
    """Read the config, repairing or creating it as needed. Never raises."""
    try:
        ensure_config_dir()
    except OSError as exc:
        # Called from a QTimer slot on every tick, so escaping here would abort
        # the process rather than skip a tick.
        print(f"airplane-notifier: config dir unusable ({exc})", file=diagnostic)
        return json.loads(json.dumps(DEFAULT_CONFIG))

    if not paths.CONFIG_PATH.exists():
        return _write_default_config()

    try:
        raw = json.loads(paths.CONFIG_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        _quarantine_config(f"config.json is unreadable ({exc})")
        return _write_default_config()

    if not isinstance(raw, dict):
        _quarantine_config("config.json is not a JSON object")
        return _write_default_config()

    merged = _deep_merge(DEFAULT_CONFIG, raw)
    # Guarantee the shapes the scheduler indexes into, so a scalar where a dict
    # belongs (e.g. "nudges": {"water": "on"}) cannot reach attribute access.
    if not isinstance(merged.get("active_hours"), dict):
        merged["active_hours"] = dict(DEFAULT_CONFIG["active_hours"])
    nudges = merged.get("nudges")
    if not isinstance(nudges, dict):
        merged["nudges"] = json.loads(json.dumps(DEFAULT_CONFIG["nudges"]))
    else:
        merged["nudges"] = {
            name: settings for name, settings in nudges.items()
            if isinstance(settings, dict) and settings
        } or json.loads(json.dumps(DEFAULT_CONFIG["nudges"]))
    return merged


def nudge_corner(cfg: dict) -> str:
    """The validated entry corner (R20a), falling back rather than raising."""
    corner = cfg.get("nudge_corner", DEFAULT_CORNER)
    if corner in VALID_CORNERS:
        return corner
    print(
        f"airplane-notifier: unknown nudge_corner {corner!r}; "
        f"using {DEFAULT_CORNER} (valid: {', '.join(VALID_CORNERS)})",
        file=diagnostic,
    )
    return DEFAULT_CORNER


# --- scheduler state --------------------------------------------------------


def load_state() -> dict:
    """Read scheduler state, treating any problem as 'no state yet'."""
    if not paths.STATE_PATH.exists():
        return {}
    try:
        raw = json.loads(paths.STATE_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        print(f"airplane-notifier: ignoring unreadable state ({exc})", file=diagnostic)
        return {}
    if not isinstance(raw, dict):
        return {}

    for entry in raw.values():
        if not isinstance(entry, dict):
            continue
        due = entry.get("next_due")
        # assume_utc so a hand-written naive timestamp cannot produce a value
        # that raises TypeError when compared against an aware "now".
        entry["next_due"] = parse_iso8601(due, assume_utc=True) if isinstance(due, str) else None
    return {k: v for k, v in raw.items() if isinstance(v, dict)}


def save_state(state: dict) -> None:
    """Write scheduler state atomically.

    Writing in place risks a half-written file if the process dies mid-write,
    which would then be discarded on next load and reset the user's cadence.
    Writing a sibling temp file and ``os.replace``-ing it means a reader always
    sees either the whole old file or the whole new one.
    """
    try:
        ensure_config_dir()
    except OSError as exc:
        print(f"airplane-notifier: cannot save state ({exc})", file=diagnostic)
        return
    payload = {
        key: {
            "next_due": (
                entry["next_due"].isoformat()
                if isinstance(entry.get("next_due"), datetime)
                else entry.get("next_due")
            ),
            "consecutive_ignores": entry.get("consecutive_ignores", 0),
        }
        for key, entry in state.items()
    }

    tmp = paths.STATE_PATH.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, paths.STATE_PATH)
    except OSError as exc:
        print(f"airplane-notifier: could not save state: {exc}", file=diagnostic)
        try:
            tmp.unlink()
        except OSError:
            pass


# --- alerted meetings -------------------------------------------------------

# Anything older than this cannot still be inside the alert window, so keeping
# it would only grow the file forever.
ALERTED_RETENTION = timedelta(hours=6)


def load_alerted() -> set:
    """Which (event id, start time) pairs have already flown a plane.

    Persisted, because holding this only in memory means every restart forgets
    and re-alerts anything still inside its five-minute window -- so a reboot,
    a re-authorization, or a crash mid-window shows the same meeting twice.
    """
    if not paths.ALERTED_PATH.exists():
        return set()
    try:
        raw = json.loads(paths.ALERTED_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return set()
    if not isinstance(raw, list):
        return set()
    return {
        (str(item[0]), str(item[1]))
        for item in raw
        if isinstance(item, (list, tuple)) and len(item) == 2
    }


def save_alerted(pairs, now: Optional[datetime] = None) -> None:
    """Write the alerted set, dropping entries too old to matter."""
    moment = now or datetime.now().astimezone()
    keep = []
    for event_id, start_iso in pairs:
        started = parse_iso8601(start_iso, assume_utc=True)
        if started is not None and moment - started > ALERTED_RETENTION:
            continue
        keep.append([event_id, start_iso])

    tmp = paths.ALERTED_PATH.with_suffix(".json.tmp")
    try:
        ensure_config_dir()
        tmp.write_text(json.dumps(keep), encoding="utf-8")
        os.replace(tmp, paths.ALERTED_PATH)
    except OSError as exc:
        print(f"airplane-notifier: could not save alerted list: {exc}", file=diagnostic)
        try:
            tmp.unlink()
        except OSError:
            pass


# --- nudge log --------------------------------------------------------------


def append_nudge_log(entry: dict) -> None:
    """Append one JSON line describing a nudge appearance (R28).

    Timestamped in local time with an offset, because the point of this file is
    that a human can open it and see when the nudges actually landed.

    Swallows every OSError: a failed log write must never break a nudge.
    """
    record = {"timestamp": datetime.now().astimezone().isoformat(), **entry}
    try:
        ensure_config_dir()
        with open(paths.NUDGE_LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except OSError as exc:
        print(f"airplane-notifier: could not write nudge log: {exc}", file=diagnostic)
