# Airplane Notifier

A Windows tray app that flies an airplane across your screen before a meeting,
and walks a character in from the corner to ask whether you've eaten or had water.

Calendar notifications are easy to miss and easier to dismiss. A full-screen
airplane towing a banner with the meeting name is neither.

---

## What it does

**Meeting alerts.** It stays continuously in sync with Google Calendar and,
five minutes before an event starts, flies an airplane across **every** monitor
you have — entering off one edge of your leftmost screen and exiting off the
far edge of your rightmost — towing a banner with the event title.

**Ambient nudges.** On their own schedule, a character walks in from a screen
corner, asks a single question, and walks back out. Two are configured by
default: water on an interval, and lunch at a fixed clock time. Clicking the
character acknowledges it; ignoring it lets it leave and re-ask sooner.

It runs in the system tray and needs no daily interaction. Google Calendar does
not need to be open, or even installed.

## Design notes

A few decisions that aren't obvious from the outside:

- **Flight speed is pixels per second, not a fixed duration.** A fixed duration
  makes the plane faster the more monitors you attach — plugging in a second
  screen would silently double its speed and make the banner unreadable.
- **The window spans the whole virtual desktop.** Its origin is not necessarily
  `(0, 0)`: a monitor placed left of your laptop reports a negative x. Assuming
  the desktop starts at the origin confines the plane to one screen.
- **Nudges are suppressed** during calendar events, while the workstation is
  locked, while you've been idle, and outside your configured active hours. A
  gate that blocks a nudge never advances its schedule, so a blocked nudge is
  deferred rather than lost.
- **Alerted meetings persist to disk.** Holding that only in memory means a
  restart inside an event's five-minute window alerts it a second time.
- **Text is rendered as plain text, never markup.** Qt's `QLabel` defaults to
  auto-detecting rich text, so an event title containing an `<img>` tag pointing
  at a UNC path makes Qt open an SMB connection — leaking a Windows credential
  hash and freezing the UI while it times out.

## Requirements

- Windows 10 or 11
- Python 3.9+ (3.12 recommended)
- A Google account

## Setup

### 1. Google Cloud credentials

The app talks to your calendar with your own OAuth client — there is no shared
server and no third party involved.

1. Create a project at [console.cloud.google.com](https://console.cloud.google.com/)
2. Enable the **Google Calendar API**
3. Create an **OAuth client ID** of type **Desktop app**
4. Download the JSON as `credentials.json` in the project root
5. Under *Google Auth Platform → Audience*, click **Publish app**

Step 5 matters: an app left in *Testing* has its refresh token expired by Google
after seven days, so you would be re-authorising every week.

`credentials.json` is gitignored and is never bundled into builds.

### 2. Install and run

```bash
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m airplanenotifier
```

The first run opens a browser once for consent. The token is saved to
`~/.airplane-notifier/token.json`.

You will see an "unverified app" warning — expected, since Google only verifies
apps intended for public distribution. This one is yours alone.

## Configuration

`~/.airplane-notifier/config.json`, created on first run and **re-read every 30
seconds**, so edits apply without restarting.

```json
{
  "active_hours": {"start": "09:00", "end": "23:00"},
  "idle_suppress_minutes": 10,
  "nudge_corner": "bottom-right",
  "flyover_speed": 240,
  "ignored_calendars": [],
  "catch_up_minutes": 120,
  "nudges": {
    "water": {"enabled": true, "interval_minutes": 90, "question": "Did you drink water?"},
    "food":  {"enabled": true, "at": ["12:30"], "question": "Did you have lunch?"}
  },
  "ignore_backoff_minutes": 15,
  "max_consecutive_reasks": 3,
  "hold_seconds": 4
}
```

| Key | Meaning |
|---|---|
| `flyover_speed` | Pixels per second. Higher is faster; the flight lengthens with more screens rather than speeding up. |
| `nudge_corner` | `bottom-right` (default), `top-right`, `bottom-left`, `top-left`. |
| `interval_minutes` | Count forward from the last time the nudge was resolved. |
| `at` | Fixed clock times, e.g. `["08:00", "12:30"]`. Takes precedence over an interval. |
| `catch_up_minutes` | How late a fixed-time nudge may still fire if the machine was off. Past this it waits for tomorrow. |
| `ignored_calendars` | Calendar names or ids to skip. Holiday feeds carry only all-day events and can never alert. |

**A calendar hidden in the Google Calendar UI is still watched.** Unticking a
calendar only hides it visually; it stays in your subscription list, which is
what the API reads. That is useful — you can subscribe to something purely for
reminders without it cluttering your calendar view.

## Which events alert

Any **timed** event on any calendar you subscribe to. All-day events are
excluded, because "five minutes before" resolves to midnight for them.

## Nudge tuning

The default intervals are starting guesses, not researched values. Every nudge
appearance is appended to `~/.airplane-notifier/nudge-log.jsonl`:

```json
{"timestamp": "2026-08-18T15:33:53+01:00", "type": "water", "outcome": "ignored"}
```

Run it for a few days, read the log, and set intervals from what actually
happened rather than from what seemed reasonable up front.

## Diagnostics

`~/.airplane-notifier/airplane-notifier.log` (rotating, 512 KB × 3).

A packaged build is a windowed process with **no standard streams at all** —
`sys.stderr` is `None` and `print(..., file=None)` silently does nothing. This
file is the only place errors surface, so check it first if alerts stop.

## Tests

```bash
.venv\Scripts\python -m pytest
```

218 tests. Qt tests run against the offscreen platform plugin and need no
display. `tools/smoke_run.py` boots the whole application headlessly.

## Building a standalone .exe

```bash
build.bat
```

Produces `dist/airplane-notifier/`. Copy your `credentials.json` beside the
`.exe` afterwards — it is deliberately **not** bundled, since baking it in would
publish your OAuth client secret to anyone holding a copy of the build.

The build creates its own clean venv rather than using whatever Python is
active; Anaconda in particular drags extra DLLs into the bundle.

## Known issues

[`docs/KNOWN-ISSUES.md`](docs/KNOWN-ISSUES.md) lists what six independent code
reviewers found and what was deliberately not fixed — including a memory leak
and gaps in test coverage. It is published as-is rather than quietly trimmed.

[`docs/PLAN.md`](docs/PLAN.md) is the original implementation plan.

## Privacy

- Requests `calendar.readonly` only, and never writes to your calendar
- Your token and config never leave your machine
- No analytics, no telemetry, no server
- Your OAuth client is yours; nothing is shared with the author

## Licence

MIT — see [LICENSE](LICENSE).
