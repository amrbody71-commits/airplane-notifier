"""Application wiring and the two timers (U6 -- R4, R5, R14; U10 wiring).

Everything the app does hangs off two independent 30-second timers: one syncs
the calendar, one ticks the nudge scheduler. They are separate so nudge
responsiveness never depends on network latency or a calendar outage (KTD13).

**Threading (plan review P0/P1).** Qt runs the UI on one thread, and both the
calendar request and the OAuth consent flow are blocking network operations. Run
either on the main thread and the UI freezes -- which for this app means a
character stopping dead mid-stride, far more visible than a stalled tray icon.
Both are therefore run on worker threads that hand their result back through a
Qt signal, which Qt delivers on the main thread.
"""

from __future__ import annotations

import sys
import threading
from typing import Callable, Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication

from airplanenotifier import auth, config, startup
from airplanenotifier.calendar_client import CalendarClient
from airplanenotifier.nudge_overlay import NudgeOverlay
from airplanenotifier.nudges import ACKNOWLEDGED, IGNORED, NudgeScheduler
from airplanenotifier.overlay import OverlayWindow
from airplanenotifier.paths import asset_path, make_stdout_safe
from airplanenotifier.tray import TrayManager
from airplanenotifier.log import diagnostic

SYNC_INTERVAL_MS = 30_000
NUDGE_INTERVAL_MS = 30_000
AUTH_RETRY_MS = 60_000

WALKER_ASSETS = {"water": "walker_water.png", "food": "walker_food.png"}


def _run_off_thread(work: Callable[[], object], done: Callable[[object], None],
                    failed: Callable[[Exception], None]) -> None:
    """Run `work` on a daemon thread, reporting back through callables.

    The callables here are always bound Qt signal emissions, so Qt queues the
    delivery onto the main thread for us.
    """
    def runner() -> None:
        try:
            done(work())
        except Exception as exc:  # noqa: BLE001 - surfaced via `failed`
            failed(exc)

    threading.Thread(target=runner, daemon=True).start()


class NotifierApp(QObject):
    """Owns the timers, the overlays, and the coordination between them."""

    _sync_done = pyqtSignal(object)
    _sync_failed = pyqtSignal(object)
    _auth_done = pyqtSignal(object)
    _auth_failed = pyqtSignal(object)

    def __init__(self, app: QApplication, client_factory=None, tray_factory=None) -> None:
        super().__init__()
        self._app = app
        self._client_factory = client_factory or (lambda creds: CalendarClient(creds))
        self._client: Optional[CalendarClient] = None

        self._pending_meetings: list[dict] = []
        self._active_overlay = None
        self._sync_in_flight = False

        self.tray = (tray_factory or self._default_tray)(app)

        self.scheduler = NudgeScheduler(
            show_nudge=self._show_nudge,
            is_meeting_in_progress=self._is_meeting_in_progress,
            is_overlay_active=self.is_overlay_active,
        )

        self._sync_done.connect(self._on_sync_done)
        self._sync_failed.connect(self._on_sync_failed)
        self._auth_done.connect(self._on_auth_done)
        self._auth_failed.connect(self._on_auth_failed)

        self.sync_timer = QTimer(self)
        self.sync_timer.setInterval(SYNC_INTERVAL_MS)
        self.sync_timer.timeout.connect(self.sync_now)

        self.nudge_timer = QTimer(self)
        self.nudge_timer.setInterval(NUDGE_INTERVAL_MS)
        self.nudge_timer.timeout.connect(self.scheduler.tick)

        # Single-shot: a transient authorization failure (offline at login)
        # retries on its own rather than leaving the app permanently silent.
        self._auth_retry_timer = QTimer(self)
        self._auth_retry_timer.setInterval(AUTH_RETRY_MS)
        self._auth_retry_timer.setSingleShot(True)
        self._auth_retry_timer.timeout.connect(lambda: self._authorize(interactive=False))

    # -- setup ---------------------------------------------------------------

    def _default_tray(self, app: QApplication) -> TrayManager:
        tray = TrayManager(
            app,
            on_authorize=self.reauthorize,
            on_toggle_startup=self.toggle_autostart,
        )
        tray.update_autostart_checked(startup.is_autostart_enabled())
        return tray

    def start(self) -> None:
        """Authorize, then begin both timers."""
        config.load_config()  # create config.json on first run so it is editable
        self.nudge_timer.start()
        self._authorize(interactive=False)

    # -- authorization -------------------------------------------------------

    def _authorize(self, interactive: bool) -> None:
        if interactive:
            auth.clear_credentials()
        _run_off_thread(
            auth.get_credentials,
            self._auth_done.emit,
            self._auth_failed.emit,
        )

    def reauthorize(self) -> None:
        self.tray.show_message("Airplane Notifier", "Opening the Google sign-in page...")
        self._authorize(interactive=True)

    def _on_auth_done(self, credentials) -> None:
        self._auth_retry_timer.stop()
        self._client = self._client_factory(credentials)
        self.sync_timer.start()
        self.sync_now()

    def _on_auth_failed(self, exc: Exception) -> None:
        self.sync_timer.stop()
        if isinstance(exc, auth.TransientAuthError):
            # Almost always "the network is not up yet" at Windows login: the
            # token expired overnight and the refresh could not reach Google.
            # The grant is fine, so retry on a timer instead of stopping and
            # waiting for the user to notice a balloon they will never see.
            self._auth_retry_timer.start()
            print(
                f"airplane-notifier: authorization deferred ({exc}); "
                f"retrying in {AUTH_RETRY_MS // 1000}s",
                file=diagnostic,
            )
            return
        if isinstance(exc, FileNotFoundError):
            # Expected on a fresh install. Keep running so the tray's
            # Authorize action stays reachable once the file is in place.
            self.tray.show_message(
                "Airplane Notifier needs credentials.json",
                "Add your Google OAuth client file, then choose Authorize.",
            )
        else:
            self.tray.show_message("Airplane Notifier", f"Sign-in failed: {exc}")
        print(f"airplane-notifier: authorization failed: {exc}", file=diagnostic)

    # -- calendar sync -------------------------------------------------------

    def sync_now(self) -> None:
        if self._client is None:
            return
        if self._sync_in_flight:
            # googleapiclient's default HTTP timeout is 60s against a 30s tick,
            # so on a slow link syncs would otherwise overlap: two threads
            # racing the same non-thread-safe Http object and the same dedup
            # set, which can fly two planes for one meeting, and a stale
            # response landing after a fresh one.
            return
        self._sync_in_flight = True
        client = self._client
        _run_off_thread(
            client.get_upcoming_meetings,
            self._sync_done.emit,
            self._sync_failed.emit,
        )

    def _on_sync_done(self, meetings) -> None:
        self._sync_in_flight = False
        if self._client is not None:
            self.tray.set_last_sync(self._client.last_sync_time())
        # Queue every meeting rather than showing only the first: the client
        # has already marked them all alerted, so anything dropped here is
        # dropped permanently (plan review item 6).
        self._pending_meetings.extend(meetings or [])
        self._drain_meetings()

    def _on_sync_failed(self, exc: Exception) -> None:
        self._sync_in_flight = False
        print(f"airplane-notifier: sync error: {exc}", file=diagnostic)

    def _is_meeting_in_progress(self) -> bool:
        return self._client is not None and self._client.is_meeting_in_progress()

    # -- overlays ------------------------------------------------------------

    def is_overlay_active(self) -> bool:
        return self._active_overlay is not None

    def _drain_meetings(self) -> None:
        """Show the next queued meeting, one at a time."""
        if self._active_overlay is not None or not self._pending_meetings:
            return
        meeting = self._pending_meetings.pop(0)
        cfg = config.load_config()
        overlay = OverlayWindow(
            meeting.get("summary", ""),
            asset_path("airplane.png"),
            speed_px_per_sec=config.as_int(cfg.get("flyover_speed"), 190),
        )
        overlay.finished.connect(self._on_meeting_overlay_finished)
        self._active_overlay = overlay
        overlay.start()

    def _on_meeting_overlay_finished(self) -> None:
        self._active_overlay = None
        # Keeps a character from walking on immediately behind the plane (R23).
        self.scheduler.note_meeting_overlay_closed()
        self._drain_meetings()

    def _show_nudge(self, nudge_type: str, question: str) -> None:
        cfg = config.load_config()
        overlay = NudgeOverlay(
            question,
            asset_path(WALKER_ASSETS.get(nudge_type, "walker_water.png")),
            corner=config.nudge_corner(cfg),
            hold_seconds=config.as_int(cfg.get("hold_seconds"), 4),
        )
        overlay.acknowledged.connect(
            lambda: self._on_nudge_resolved(nudge_type, ACKNOWLEDGED)
        )
        overlay.ignored.connect(lambda: self._on_nudge_resolved(nudge_type, IGNORED))
        self._active_overlay = overlay
        overlay.start()

    def _on_nudge_resolved(self, nudge_type: str, outcome: str) -> None:
        self._active_overlay = None
        self.scheduler.resolve(nudge_type, outcome)
        self._drain_meetings()

    # -- tray actions --------------------------------------------------------

    def toggle_autostart(self) -> None:
        enabled = startup.toggle_autostart()
        self.tray.update_autostart_checked(enabled)
        self.tray.show_message(
            "Airplane Notifier",
            "Will start with Windows." if enabled else "Will no longer start with Windows.",
        )


def main() -> int:
    make_stdout_safe()
    app = QApplication(sys.argv)
    notifier = NotifierApp(app)
    notifier.start()
    return app.exec()
