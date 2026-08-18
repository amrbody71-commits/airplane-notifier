"""Nudge scheduling (U10 -- R19-R25, R28, R30).

Decides when a nudge is due, whether it is allowed to appear right now, and
what to do with the answer. Pure local computation over ``config.json`` and
``state.json`` -- no network, no Qt, nothing that can block the main thread.

**The central rule: a gate that blocks a nudge must not touch state.** If a
blocked nudge rolled its ``next_due`` forward, one falling due at 03:00 would
quietly skip to tomorrow and never be seen, and one blocked by a meeting would
be lost instead of deferred by a tick. Blocking defers; only *appearing*
advances the schedule.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Callable, Optional

from airplanenotifier import config
from airplanenotifier.config import as_int
from airplanenotifier.log import diagnostic
from airplanenotifier.idle import idle_seconds as _default_idle
from airplanenotifier.idle import is_workstation_locked as _default_locked

ACKNOWLEDGED = "acknowledged"
IGNORED = "ignored"

# Gap after a meeting overlay before a character may appear, so the airplane
# and a nudge never arrive back to back (R23).
POST_MEETING_PAUSE = timedelta(seconds=60)

# Any next_due further out than this is treated as corrupt and re-seeded.
_MAX_SANE_LEAD = timedelta(days=2)


def _parse_hhmm_or_none(value: object) -> Optional[time]:
    """Parse "HH:MM", returning None rather than raising on a typo."""
    try:
        hour, minute = str(value).split(":")
        return time(int(hour), int(minute))
    except (ValueError, AttributeError, TypeError):
        return None


def _parse_hhmm(value: str, fallback: time) -> time:
    try:
        hour, minute = value.split(":")
        return time(int(hour), int(minute))
    except (ValueError, AttributeError):
        return fallback


class NudgeScheduler:
    """Owns nudge cadence. Call :meth:`tick` on a timer, then :meth:`resolve`."""

    def __init__(
        self,
        show_nudge: Callable[[str, str], None],
        is_meeting_in_progress: Optional[Callable[[], bool]] = None,
        is_overlay_active: Optional[Callable[[], bool]] = None,
        idle_seconds: Optional[Callable[[], float]] = None,
        is_locked: Optional[Callable[[], bool]] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._show_nudge = show_nudge
        self._meeting = is_meeting_in_progress or (lambda: False)
        self._overlay = is_overlay_active or (lambda: False)
        self._idle = idle_seconds or _default_idle
        self._locked = is_locked or _default_locked
        self._now = now_fn or (lambda: datetime.now().astimezone())
        self._meeting_overlay_closed_at: Optional[datetime] = None

    # -- public API ----------------------------------------------------------

    def note_meeting_overlay_closed(self, when: Optional[datetime] = None) -> None:
        """Record that an airplane overlay just finished (R23)."""
        self._meeting_overlay_closed_at = when or self._now()

    def tick(self) -> Optional[str]:
        """Show one nudge if any is due and allowed. Returns the type shown."""
        now = self._now()
        cfg = config.load_config()
        state = config.load_state()

        # Seeding must persist: otherwise every restart re-seeds from "now" and
        # a nudge whose interval exceeds the uptime never arrives at all.
        if self._seed_missing(cfg, state, now):
            config.save_state(state)

        if not self._is_allowed(cfg, now):
            return None

        nudge_type = self._most_overdue(cfg, state, now)
        if nudge_type is None:
            return None

        question = self._settings(cfg, nudge_type).get("question", "")
        try:
            self._show_nudge(nudge_type, question)
        except Exception as exc:  # noqa: BLE001 - runs in a QTimer slot
            # An exception here would reach Qt and abort the process, and since
            # tick() does not advance next_due, it would repeat every 30s.
            # Reschedule so a broken overlay degrades to a skipped nudge.
            print(f"airplane-notifier: could not show {nudge_type} nudge: {exc}",
                  file=diagnostic)
            self.resolve(nudge_type, IGNORED)
            return None
        return nudge_type

    def resolve(self, nudge_type: str, outcome: str) -> None:
        """Record how a nudge ended and schedule the next one (R25, R28)."""
        cfg = config.load_config()
        settings = self._settings(cfg, nudge_type)
        if not settings:
            return  # a type that no longer exists in config

        now = self._now()
        state = config.load_state()
        entry = state.setdefault(nudge_type, {"next_due": None, "consecutive_ignores": 0})

        backoff = timedelta(minutes=as_int(cfg.get("ignore_backoff_minutes"), 15))
        max_reasks = as_int(cfg.get("max_consecutive_reasks"), 3)

        if outcome == IGNORED:
            ignores = int(entry.get("consecutive_ignores", 0)) + 1
            # <= not <: with max_consecutive_reasks=3, "<" gives only two
            # short re-asks before resetting, one fewer than R25 specifies.
            if ignores <= max_reasks:
                entry["next_due"] = now + backoff
                entry["consecutive_ignores"] = ignores
            else:
                # Enough. Stop nagging and go back to the normal rhythm.
                entry["next_due"] = self._next_due(settings, now)
                entry["consecutive_ignores"] = 0
        else:
            entry["next_due"] = self._next_due(settings, now)
            entry["consecutive_ignores"] = 0

        config.save_state(state)
        config.append_nudge_log({"type": nudge_type, "outcome": outcome})

    # -- gates ---------------------------------------------------------------

    def _is_allowed(self, cfg: dict, now: datetime) -> bool:
        """Global gates, cheapest first. None of these may mutate state."""
        if not self._within_active_hours(cfg, now):
            return False
        if self._overlay():
            return False
        if self._meeting():
            return False
        if self._in_post_meeting_pause(now):
            return False
        idle_limit = as_int(cfg.get("idle_suppress_minutes"), 10) * 60
        if self._idle() >= idle_limit:
            return False
        if self._locked():
            return False
        return True

    def _in_post_meeting_pause(self, now: datetime) -> bool:
        closed = self._meeting_overlay_closed_at
        return closed is not None and now - closed < POST_MEETING_PAUSE

    @staticmethod
    def _within_active_hours(cfg: dict, now: datetime) -> bool:
        hours = cfg.get("active_hours") or {}
        start = _parse_hhmm(hours.get("start", "09:00"), time(9, 0))
        end = _parse_hhmm(hours.get("end", "23:00"), time(23, 0))
        current = now.astimezone().time()

        if start == end:
            # Equal bounds read as "always on". Treating it as an instant window
            # would need microsecond-exact equality, i.e. permanent silence with
            # nothing in the UI to explain it.
            return True
        if start < end:
            return start <= current <= end
        # Window crosses midnight (e.g. 22:00-04:00), so it is the union of
        # [start, midnight] and [midnight, end] rather than an empty range.
        return current >= start or current <= end

    # -- selection -----------------------------------------------------------

    @staticmethod
    def _settings(cfg: dict, nudge_type: str) -> dict:
        return (cfg.get("nudges") or {}).get(nudge_type) or {}

    @staticmethod
    def _fixed_times(settings: dict) -> list[time]:
        """The clock times this nudge fires at, if any."""
        raw = settings.get("at")
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return []
        parsed = [_parse_hhmm_or_none(v) for v in raw]
        return sorted(t for t in parsed if t is not None)

    def _next_due(self, settings: dict, now: datetime) -> datetime:
        """When this nudge should next fire.

        A fixed-time nudge lands on the next listed clock time; an
        interval-based one simply counts forward from now.
        """
        times = self._fixed_times(settings)
        if not times:
            minutes = as_int(settings.get("interval_minutes"), 45)
            return now + timedelta(minutes=max(1, minutes))

        local = now.astimezone()
        for slot in times:
            candidate = local.replace(
                hour=slot.hour, minute=slot.minute, second=0, microsecond=0
            )
            if candidate > local:
                return candidate
        # All of today's times have passed, so the first one tomorrow.
        first = times[0]
        tomorrow = local + timedelta(days=1)
        return tomorrow.replace(
            hour=first.hour, minute=first.minute, second=0, microsecond=0
        )

    def _is_stale(self, settings: dict, due: datetime, now: datetime, cfg: dict) -> bool:
        """True when a fixed-time nudge is too late to still be worth asking.

        Being asked "did you have lunch?" at 20:00 because the laptop was shut
        at 13:00 is worse than not being asked at all.
        """
        if not self._fixed_times(settings):
            return False
        grace = timedelta(minutes=as_int(cfg.get("catch_up_minutes"), 120))
        return now - due > grace

    def _is_aligned(self, settings: dict, entry: Optional[dict]) -> bool:
        """Whether a fixed-time nudge's next_due is actually one of its times.

        Lateness alone is not enough to judge a stored schedule. A next_due of
        21:37 for a 12:30 lunch nudge looks perfectly fresh the moment it
        arrives, so it fires -- and asks about lunch at nearly ten at night.
        That is exactly what a stray write to state.json (a test, a hand edit,
        a corrupted file) produces, and what changing "12:30" to another time
        in config leaves behind.

        An ignore-backoff re-ask is deliberately off-schedule, so a nudge
        mid-backoff is left alone; those chains reset to a real clock time on
        their own once the re-ask limit is reached.
        """
        times = self._fixed_times(settings)
        if not times:
            return True  # interval nudges have no clock time to align to
        if int((entry or {}).get("consecutive_ignores", 0)) > 0:
            return True  # a backoff re-ask, legitimately between slots
        due = (entry or {}).get("next_due")
        if due is None:
            return False
        local = due.astimezone()
        return any(local.hour == t.hour and local.minute == t.minute for t in times)

    def _seed_missing(self, cfg: dict, state: dict, now: datetime) -> bool:
        """Give any unscheduled type a first due time. Returns True if changed."""
        changed = False
        for nudge_type, settings in (cfg.get("nudges") or {}).items():
            entry = state.get(nudge_type)
            due = entry.get("next_due") if entry else None
            # A next_due absurdly far out is a clock-skew artefact: booting with
            # a wrong RTC once would otherwise park a nudge a year ahead and it
            # would never fire again, surviving every restart.
            if (
                due is not None
                and due - now <= _MAX_SANE_LEAD
                and self._is_aligned(settings, entry)
            ):
                continue
            state[nudge_type] = {
                "next_due": self._next_due(settings, now),
                "consecutive_ignores": (entry or {}).get("consecutive_ignores", 0),
            }
            changed = True
        return changed

    def _most_overdue(self, cfg: dict, state: dict, now: datetime) -> Optional[str]:
        """The type that has been waiting longest, or None.

        Only one may appear per tick: two characters walking on at once would
        collide, and the second's question would be unreadable behind the first.
        """
        candidates = []
        for nudge_type, settings in (cfg.get("nudges") or {}).items():
            if not settings.get("enabled", True):
                continue
            due = (state.get(nudge_type) or {}).get("next_due")
            if due is None or now < due:
                continue
            if self._is_stale(settings, due, now, cfg):
                # Missed its window entirely; roll it forward rather than
                # asking about lunch in the evening.
                state[nudge_type]["next_due"] = self._next_due(settings, now)
                config.save_state(state)
                continue
            candidates.append((due, nudge_type))

        if not candidates:
            return None
        return min(candidates)[1]
