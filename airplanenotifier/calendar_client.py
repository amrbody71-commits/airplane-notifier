"""Google Calendar sync (U3 -- R4-R7, R31-R36).

This module carries the product's primary property: the calendar connection is
automatic and continuous, with no refresh control anywhere in the app.

**Why a bounded window query and not ``syncToken`` (KTD15).** ``events.list``
rejects ``syncToken`` alongside ``timeMin``/``timeMax``/``orderBy``, so
incremental sync would return changes across the *entire* calendar and force us
to maintain a local event cache purely to answer "what starts in the next five
minutes". Re-querying ``now .. now+2h`` every cycle is one small request that
stays trivially correct for creates, edits, reschedules, and deletions: a
deleted event simply stops coming back, and there is no cache to invalidate.

**Threading note.** ``get_upcoming_meetings`` performs blocking network I/O and
must not be called on the Qt main thread (plan review P0).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from googleapiclient.discovery import build

from airplanenotifier import config
from airplanenotifier.config import as_int
from airplanenotifier.timeutil import parse_iso8601
from airplanenotifier.log import diagnostic

# How far ahead we look. Wider than the alert window so `is_meeting_in_progress`
# has something to answer with between alerts.
LOOKAHEAD = timedelta(hours=2)

# The query window must reach past the longest lead time, or an event would
# not even be visible by the time it should have been announced.
LOOKAHEAD_MARGIN = timedelta(minutes=30)

# Fallback when config supplies nothing. Each alert is bounded at both ends,
# so restarting the app cannot replay meetings that already began.
DEFAULT_LEAD_MINUTES = [5]

# A lead of exactly 0 means "at the event's start", not "before it" -- Maghrib
# starting is itself the moment worth announcing. Polling every 30s means the
# tick that notices "delay is now <= 0" typically lands a few seconds to a
# minute after the real start, so this is how far past start the alert may
# still fire and be honestly called "just started" rather than stale. Kept
# short and deliberately separate from LOOKAHEAD_MARGIN / ALERT_WINDOW, which
# both look forward in time, not back.
START_GRACE = timedelta(seconds=90)

BASE_BACKOFF = timedelta(seconds=30)
MAX_BACKOFF = timedelta(minutes=5)
# 30s * 2**4 = 480s, already past MAX_BACKOFF, so nothing beyond this changes
# the result -- it exists purely to keep the intermediate small.
_MAX_BACKOFF_STEPS = 4

# How long a cached event list may keep suppressing nudges once syncing has
# stopped working. Without a bound, one stale "meeting in progress" silences
# every nudge indefinitely.
EVENTS_STALE_AFTER = timedelta(minutes=15)

# The set of calendars changes rarely, so it is not worth re-fetching on every
# 30-second cycle.
CALENDAR_LIST_TTL = timedelta(minutes=10)

NO_TITLE = "(No title)"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CalendarClient:
    """Queries the primary calendar and reports meetings about to start."""

    def __init__(self, credentials: Any, service: Any = None) -> None:
        # `service` is injectable so the tests never touch the network.
        self._service = service or build(
            "calendar", "v3", credentials=credentials, cache_discovery=False
        )
        # Keyed on (event id, start time) rather than id alone: a rescheduled
        # meeting must alert again at its new time (KTD17).
        # Loaded from disk: an in-memory-only set means every restart re-alerts
        # anything still inside its window (see config.load_alerted).
        self._alerted: set[tuple[str, str]] = config.load_alerted()
        self._events: list[dict] = []
        self._last_sync: Optional[datetime] = None
        self._backoff_until: Optional[datetime] = None
        self._consecutive_failures = 0
        self._calendar_names: dict[str, str] = {}
        self._last_unmatched: list[str] = []
        self._calendar_ids_cache: Optional[list[str]] = None
        self._calendar_list_fetched: Optional[datetime] = None

    # -- introspection used by the tray and the nudge scheduler --------------

    def last_sync_time(self) -> Optional[datetime]:
        """When the last successful sync completed, for the tray tooltip (R36)."""
        return self._last_sync

    def backoff_until(self) -> Optional[datetime]:
        """When the next attempt is allowed, or None when not backed off."""
        return self._backoff_until

    def reset_alerts(self) -> None:
        """Forget which meetings have alerted. Used by tests."""
        self._alerted.clear()

    def is_meeting_in_progress(self, now: Optional[datetime] = None) -> bool:
        """True when a timed event from the last sync spans `now` (R23).

        Reads the cached result of the last successful sync -- it never issues
        its own request, because the nudge tick must stay free of network I/O.
        """
        moment = now or _utcnow()
        # Refuse to answer from a stale cache: if syncing has been broken for a
        # while, a long-finished event would otherwise keep nudges suppressed.
        if self._last_sync is None or moment - self._last_sync > EVENTS_STALE_AFTER:
            return False
        return any(e["start"] <= moment <= e["end"] for e in self._events)

    # -- the sync cycle ------------------------------------------------------

    def get_upcoming_meetings(self, now: Optional[datetime] = None) -> list[dict]:
        """Return meetings starting within the alert window, once each.

        Never raises: a failure is logged, backs off, and returns an empty
        list so the caller's timer keeps ticking (R34).
        """
        moment = now or _utcnow()

        if self._backoff_until is not None and moment < self._backoff_until:
            return []

        try:
            cfg = config.load_config()
            items = []
            for calendar_id in self._calendar_ids(moment):
                leads = self._lead_times_for(
                    cfg, calendar_id, self._calendar_names.get(calendar_id, "")
                )
                horizon = max(LOOKAHEAD, timedelta(minutes=leads[0]) + LOOKAHEAD_MARGIN)
                response = (
                    self._service.events()
                    .list(
                        calendarId=calendar_id,
                        timeMin=self._rfc3339(moment),
                        timeMax=self._rfc3339(moment + horizon),
                        singleEvents=True,
                        orderBy="startTime",
                    )
                    .execute()
                )
                for item in response.get("items", []):
                    item["_leads"] = leads
                    items.append(item)
        except Exception as exc:  # noqa: BLE001 - any failure must back off, not crash
            self._register_failure(self._completed(now), exc)
            return []

        self._register_success(self._completed(now))
        self._events = self._parse_events(items)

        due = []
        newly_alerted = False
        for parsed in self._events:
            delay = parsed["start"] - moment
            # Shortest matching lead wins, so a 30-minute warning is not
            # announced as the 60-minute one when both windows are open. Lead
            # 0 sorts first and is evaluated against its own window below,
            # since "at start" and "N minutes before" open at opposite ends
            # of `delay`'s sign.
            for lead in sorted(parsed["leads"]):
                if lead == 0:
                    # "Just started": delay is at or slightly past zero, not
                    # still positive (that is what leads > 0 are for) and not
                    # so far negative that the event has been running a while.
                    in_window = -START_GRACE <= delay <= timedelta(0)
                else:
                    in_window = timedelta(0) <= delay <= timedelta(minutes=lead)
                if not in_window:
                    continue
                # The lead is part of the key: an hour-before and a
                # half-hour-before alert for the same event are separate.
                key = (parsed["id"], parsed["start"].isoformat(), str(lead))
                if key in self._alerted:
                    break
                self._alerted.add(key)
                newly_alerted = True
                due.append({
                    "id": parsed["id"],
                    "summary": parsed["summary"],
                    "start_time": parsed["start"],
                    "lead_minutes": lead,
                })
                break
        if newly_alerted:
            config.save_alerted(self._alerted, moment)
        return due

    # -- internals -----------------------------------------------------------

    def _calendar_ids(self, moment: datetime) -> list[str]:
        """Every calendar to watch, cached.

        "Any calendar event I have" means all of them, not just `primary` --
        a secondary or subscribed calendar is just as much the user's diary.
        The list changes rarely, so it is cached rather than re-fetched on
        every 30-second cycle.

        A failure here is not fatal: fall back to `primary` so a hiccup in the
        calendar list cannot stop meeting alerts entirely.
        """
        fresh = (
            self._calendar_ids_cache is not None
            and self._calendar_list_fetched is not None
            and moment - self._calendar_list_fetched < CALENDAR_LIST_TTL
        )
        if fresh:
            return self._calendar_ids_cache

        try:
            entries = self._service.calendarList().list().execute().get("items", [])
        except Exception as exc:  # noqa: BLE001 - degrade, never fail the sync
            print(
                f"airplane-notifier: could not list calendars ({exc}); "
                f"watching primary only",
                file=diagnostic,
            )
            return self._calendar_ids_cache or ["primary"]

        ignored = {
            str(name).strip().casefold()
            for name in (config.load_config().get("ignored_calendars") or [])
            if str(name).strip()
        }
        ids = []
        self._calendar_names = {
            e["id"]: str(e.get("summary") or "") for e in entries if e.get("id")
        }
        self._warn_unmatched_rules(entries)
        for entry in entries:
            calendar_id = entry.get("id")
            if not calendar_id or entry.get("deleted"):
                continue
            # Match on either the id or the display name: the ids are opaque
            # hashes, so a hand-edited config is far likelier to use the name.
            summary = str(entry.get("summary") or "")
            if {calendar_id.casefold(), summary.strip().casefold()} & ignored:
                continue
            ids.append(calendar_id)
        self._calendar_ids_cache = ids or ["primary"]
        self._calendar_list_fetched = moment
        return self._calendar_ids_cache

    @staticmethod
    def _rfc3339(moment: datetime) -> str:
        return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _warn_unmatched_rules(self, entries: list[dict]) -> None:
        """Log any config rule that matches no calendar.

        Rules may name a calendar rather than its id, so renaming one in Google
        Calendar quietly turns its rule into a no-op. Saying so is the
        difference between a five-second fix and wondering for a week why the
        lecture reminders stopped.
        """
        cfg = config.load_config()
        known = set()
        for entry in entries:
            if entry.get("id"):
                known.add(str(entry["id"]).casefold())
            if entry.get("summary"):
                known.add(str(entry["summary"]).strip().casefold())

        def matches(probe: str) -> bool:
            probe = probe.strip().casefold()
            return bool(probe) and any(probe == k or probe in k for k in known)

        rules = list(cfg.get("ignored_calendars") or [])
        section = cfg.get("lead_times")
        if isinstance(section, dict) and isinstance(section.get("by_calendar"), dict):
            rules += list(section["by_calendar"])

        unmatched = [str(r) for r in rules if not matches(str(r))]
        if unmatched != self._last_unmatched:
            for rule in unmatched:
                print(
                    f"airplane-notifier: config rule {rule!r} matches no calendar "
                    f"(renamed or removed?)",
                    file=diagnostic,
                )
            self._last_unmatched = unmatched

    def _lead_times_for(self, cfg: dict, calendar_id: str, calendar_name: str) -> list[int]:
        """Minutes-before values for one calendar, longest first.

        Matching is deliberately forgiving: an imported feed is often named
        after its raw .ics URL, so an exact name is impractical to type.
        """
        section = cfg.get("lead_times")
        if not isinstance(section, dict):
            return list(DEFAULT_LEAD_MINUTES)

        by_calendar = section.get("by_calendar")
        chosen = None
        if isinstance(by_calendar, dict):
            name = (calendar_name or "").casefold()
            for key, leads in by_calendar.items():
                probe = str(key).strip().casefold()
                if not probe:
                    continue
                if probe == calendar_id.casefold() or probe == name or probe in name:
                    chosen = leads
                    break
        if chosen is None:
            chosen = section.get("default", DEFAULT_LEAD_MINUTES)

        if not isinstance(chosen, list):
            chosen = [chosen]
        # -1 is a sentinel meaning "as_int could not parse this", not a real
        # lead value, so it is filtered out below along with any (equally
        # nonsensical) negative lead a user might type by hand. 0 is a real,
        # meaningful value here -- "alert at the event's start" -- and must
        # survive this filter rather than being treated as a parse failure.
        candidates = {as_int(v, -1) for v in chosen}
        minutes = sorted((m for m in candidates if m >= 0), reverse=True)
        return minutes or list(DEFAULT_LEAD_MINUTES)

    @staticmethod
    def _parse_events(items: list[dict]) -> list[dict]:
        """Keep timed, live events; drop all-day, cancelled, and malformed ones."""
        parsed: list[dict] = []
        for item in items:
            if item.get("status") == "cancelled":
                continue
            start_field = item.get("start") or {}
            end_field = item.get("end") or {}
            # An all-day event carries `date`; a timed one carries `dateTime`.
            if "dateTime" not in start_field:
                continue

            start = parse_iso8601(start_field["dateTime"], assume_utc=True)
            if start is None:
                # One malformed payload must not cost us the good meeting
                # beside it in the same response.
                print(
                    f"airplane-notifier: skipping unparseable event "
                    f"{item.get('id')!r}",
                    file=diagnostic,
                )
                continue

            end = parse_iso8601(end_field.get("dateTime"), assume_utc=True) or start
            parsed.append(
                {
                    "id": item.get("id", ""),
                    "summary": item.get("summary") or NO_TITLE,
                    "start": start,
                    "end": end,
                    "leads": item.get("_leads") or list(DEFAULT_LEAD_MINUTES),
                }
            )
        return parsed

    @staticmethod
    def _completed(now: Optional[datetime]) -> datetime:
        """When the request actually finished.

        Anchoring backoff to the request's *start* means a call that hangs for
        200s and then fails computes a deadline already in the past, so the
        backoff is a no-op and the next tick retries immediately. Tests inject
        a fixed clock, in which case start and finish are the same instant.
        """
        return now if now is not None else _utcnow()

    def _register_success(self, moment: datetime) -> None:
        self._last_sync = moment
        self._consecutive_failures = 0
        self._backoff_until = None

    def _register_failure(self, moment: datetime, exc: Exception) -> None:
        self._consecutive_failures += 1
        # Clamp the exponent BEFORE multiplying. Computing the huge intermediate
        # first raises OverflowError at 42 consecutive failures (~3.3h offline),
        # which escapes this handler, leaves _backoff_until frozen in the past,
        # and then hammers the API every 30s with no backoff at all.
        steps = min(self._consecutive_failures - 1, _MAX_BACKOFF_STEPS)
        delay = min(BASE_BACKOFF * (2 ** steps), MAX_BACKOFF)
        self._backoff_until = moment + delay
        print(
            f"airplane-notifier: calendar sync failed ({exc}); "
            f"retrying in {int(delay.total_seconds())}s",
            file=diagnostic,
        )
