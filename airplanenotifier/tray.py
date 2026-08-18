"""System tray icon and menu (U5 -- R12, R13, R14, R36).

QSystemTrayIcon rather than pystray: pystray runs its own event loop, which
fights Qt's. This shares the one QApplication loop (KTD2).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Callable, Optional

from PyQt6.QtCore import QObject
from PyQt6.QtGui import QAction, QIcon, QPixmap
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from airplanenotifier.paths import asset_path
from airplanenotifier.log import diagnostic

APP_TITLE = "Airplane Notifier"


def _fallback_icon() -> QIcon:
    """A plain coloured square, so a missing asset still yields a visible icon."""
    pixmap = QPixmap(32, 32)
    pixmap.fill()
    return QIcon(pixmap)


class TrayManager(QObject):
    """Owns the tray icon, its menu, and the tooltip."""

    def __init__(
        self,
        app: QApplication,
        on_authorize: Callable[[], None],
        on_toggle_startup: Callable[[], None],
        on_quit: Optional[Callable[[], None]] = None,
        icon_path=None,
    ) -> None:
        super().__init__()
        self._app = app
        self._on_authorize = on_authorize
        self._on_toggle_startup = on_toggle_startup
        self._on_quit = on_quit or app.quit
        self._last_sync: Optional[datetime] = None

        # Critical: the overlay is the only window this app ever shows, so
        # without this the app exits the first time one closes (R14).
        app.setQuitOnLastWindowClosed(False)

        icon = QIcon(str(icon_path or asset_path("tray_icon.png")))
        if icon.isNull():
            icon = _fallback_icon()

        self.tray_icon = QSystemTrayIcon(icon, self)
        self.menu = QMenu()

        self.authorize_action = QAction("Authorize / Re-authorize", self)
        self.authorize_action.triggered.connect(lambda: self._on_authorize())

        self.autostart_action = QAction("Auto-start at login", self)
        self.autostart_action.setCheckable(True)
        self.autostart_action.triggered.connect(lambda: self._on_toggle_startup())

        self.quit_action = QAction("Quit", self)
        self.quit_action.triggered.connect(lambda: self._on_quit())

        self.menu.addAction(self.authorize_action)
        self.menu.addAction(self.autostart_action)
        self.menu.addSeparator()
        self.menu.addAction(self.quit_action)

        self.tray_icon.setContextMenu(self.menu)
        self._refresh_tooltip()
        self.show()

    # -- lifecycle -----------------------------------------------------------

    def show(self) -> None:
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray_icon.show()

    def hide(self) -> None:
        self.tray_icon.hide()

    # -- menu state ----------------------------------------------------------

    def update_autostart_checked(self, enabled: bool) -> None:
        """Sync the checkbox without it looking like a user click.

        ``setChecked`` does not emit ``triggered``, but blocking signals makes
        that guarantee explicit rather than incidental.
        """
        self.autostart_action.blockSignals(True)
        self.autostart_action.setChecked(enabled)
        self.autostart_action.blockSignals(False)

    # -- notifications -------------------------------------------------------

    def show_message(self, title: str, message: str) -> None:
        """Show a balloon, degrading quietly where there is no tray."""
        try:
            if QSystemTrayIcon.supportsMessages() and self.tray_icon.isVisible():
                self.tray_icon.showMessage(title, message)
            else:
                print(f"airplane-notifier: {title} - {message}", file=diagnostic)
        except Exception as exc:  # noqa: BLE001 - a balloon must never be fatal
            print(f"airplane-notifier: could not show message ({exc})", file=diagnostic)

    # -- tooltip (R36) -------------------------------------------------------

    def set_last_sync(self, moment: Optional[datetime]) -> None:
        self._last_sync = moment
        self._refresh_tooltip()

    def tooltip(self) -> str:
        """Text shown on hover.

        The whole point is noticing when sync has quietly died, so this reports
        how long ago the last success was, not just that there was one.
        """
        if self._last_sync is None:
            return f"{APP_TITLE}\nNot synced yet"

        local = self._last_sync.astimezone()
        age = datetime.now(timezone.utc) - self._last_sync
        minutes = int(age.total_seconds() // 60)

        if minutes < 1:
            freshness = "just now"
        elif minutes == 1:
            freshness = "1 minute ago"
        else:
            freshness = f"{minutes} minutes ago"

        return f"{APP_TITLE}\nLast synced {local:%H:%M} ({freshness})"

    def _refresh_tooltip(self) -> None:
        self.tray_icon.setToolTip(self.tooltip())
