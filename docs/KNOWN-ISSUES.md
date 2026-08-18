# Residual review findings — feat/airplane-notifier

Six independent reviewers (correctness, reliability, security, testing,
adversarial, maintainability) reviewed the airplane-notifier package. The
fixed items are in the commit "fix(airplane-notifier): address code review
findings". Everything below was **deliberately not fixed** in that pass and is
recorded here rather than dropped.

Two reviewer claims were **rejected after checking them directly**:

- *"`QIcon(missing).isNull()` is always False, so `_fallback_icon` is dead."*
  Not true on PyQt6 6.7 here — it returns `True`, the fallback is reachable.
- *"The nudge overlay swallows every desktop click."* Inconclusive. The window
  is `WS_EX_LAYERED` composited via `UpdateLayeredWindow`, which does per-pixel
  hit testing, so transparent pixels should fall through — but a `WindowFromPoint`
  probe was confounded by another always-on-top window and could not confirm it.
  An explicit input mask was added anyway, so the behaviour is now deterministic
  regardless of which reading was right.

---

## Deferred — worth doing

### P2 · Overlay windows are retained until a cyclic GC pass
`overlay.py`, `nudge_overlay.py`. Each overlay forms a reference cycle
(widget → animation → connection proxy → lambda closure → widget), so dropping
`_active_overlay` does not free it; only `gc.collect()` does. Each retained
instance holds a screen-sized ARGB backing store (~15 MB at 2560×1440), and the
app creates ~40/day. Fix: `setAttribute(WA_DeleteOnClose)` or an explicit
`deleteLater()`.

### P2 · Abandoned "Authorize" clicks leak a thread and a listening socket
`auth.py` calls `run_local_server(port=0)` with `timeout_seconds=None`, which
blocks forever. `main.py:reauthorize` has no guard, so N clicks leave N wedged
daemon threads, N bound ports, and N browser tabs. This is the one genuinely
unbounded resource in the app. Fix: pass a timeout and refuse a second
concurrent authorization.

### P2 · The walk cycle is invisible
`nudge_overlay._bob` writes `y` every 120 ms while the `pos` animation rewrites
it every ~16 ms, so the bob survives roughly 2% of frames — measured
independently by two reviewers. The intended footfall is a one-frame flicker.
The docstring claiming it "rides along with the position animation" is wrong.
Fix: animate an x-only property, or apply the bob inside the animation's
`valueChanged` handler.

### P2 · Meeting overlays do not preempt a nudge
R30 says "a meeting overlay always wins", but `_drain_meetings` returns early
whenever any overlay is active, including a nudge. A meeting alert can wait up
to ~7.6 s behind a character. Priority is currently reversed.

### P2 · Everything renders on the primary monitor only
`TransparentFullScreenWindow._screen_geometry` always uses
`QApplication.primaryScreen()`, which can also return `None` (all monitors off,
RDP disconnect) and is unguarded. Acceptable for v1 per the plan, but it is now
an undocumented decision with no test.

### P2 · Threading has no test coverage
Mutation-proven: reverting the worker thread to a blocking main-thread call, and
swapping the success/failure callbacks so no meeting overlay ever appears, both
leave 162/162 tests and the smoke run green. Every test calls the handlers
directly. The primary auto-sync chain is unverified end to end.

---

## Deferred — minor

- ~~**`_alerted` is not persisted.**~~ **FIXED.** This one reached the user: a
  rebuild mid-window flew a second plane for the same meeting. The alerted set
  now persists to `alerted.json`, pruned after 6 hours, so a restart, reboot,
  re-authorization, or crash cannot replay an alert.
- **A meeting can be lost across a long outage.** `ALERT_WINDOW` and
  `MAX_BACKOFF` are both 5 minutes with no lower grace, so a backoff straddling
  the pre-meeting window can skip it entirely.
- **`_deep_merge` shares nested default objects.** It starts from
  `dict(defaults)`, so an un-overridden subtree is the *same object* as
  `DEFAULT_CONFIG`'s. Latent — nothing mutates a loaded config today.
- **`idle.OpenInputDesktop` also lacks `restype`.** Benign in practice because
  Windows keeps handle values inside 32 bits.
- **Tray tooltip renders a negative age** ("-3 minutes ago") after a backwards
  clock step.
- **The post-meeting pause is extended by a backwards clock step** — a 1-hour
  NTP correction suppresses nudges for an hour. Self-healing.
- **A missing walker asset yields a 1×1 character** that cannot be clicked, so
  every appearance resolves as ignored.
- **`nudge-log.jsonl` grows unbounded** at ~0.6 MB/year — real but not urgent.
  (`_alerted` is now pruned, so that half is resolved.)
- **`VALID_CORNERS`/`DEFAULT_CORNER` are defined twice** (`config.py`,
  `nudge_overlay.py`) and validated twice. Adding a corner to one and not the
  other silently downgrades to bottom-right.
- **`DEFAULT_CONFIG` is shadowed by ~10 literal fallbacks** at call sites, one of
  which (`nudges.py`, water's 45) is already wrong for `food`.

## Deferred — test quality

- `test_deleted_event_never_alerts` is a tautology: it empties the mock before
  the first call, so the event it builds is never seen. Mutating the client to
  accumulate events instead of replacing them still passes.
- `test_nudges.py` captures a **fixed** UTC offset at collection time while the
  scheduler re-resolves the offset per call. Green only while today's DST state
  matches 2026-08-18's — a dormant flake that will fail on a winter re-run.
- `test_a_scheduler_tick_is_blocked_while_an_overlay_is_up` uses the real wall
  clock, so it passes vacuously outside 09:00–23:00.
- `mousePressEvent` is never exercised through Qt; `QTest.mouseClick` does work
  under the offscreen plugin.
- No test asserts the *absence* of a refresh control, despite "no refresh button
  anywhere" being the stated primary property.
