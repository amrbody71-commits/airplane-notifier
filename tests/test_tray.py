"""U5 -- system tray (R12, R13, R14, R36).

The offscreen platform reports no system tray, so these assert the menu the
manager builds and the callbacks it wires, rather than a real tray icon
appearing. Actually seeing the icon is a manual check on the desktop.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("PyQt6", reason="PyQt6 is required for widget tests")

from airplanenotifier.tray import TrayManager  # noqa: E402


@pytest.fixture
def tray(qapp):
    calls = {"authorize": 0, "toggle": 0, "quit": 0}
    made = []

    def _make(**overrides):
        manager = TrayManager(
            qapp,
            on_authorize=lambda: calls.__setitem__("authorize", calls["authorize"] + 1),
            on_toggle_startup=lambda: calls.__setitem__("toggle", calls["toggle"] + 1),
            on_quit=lambda: calls.__setitem__("quit", calls["quit"] + 1),
            **overrides,
        )
        made.append(manager)
        return manager

    yield _make, calls
    for manager in made:
        manager.hide()


def _labels(manager):
    return [a.text() for a in manager.menu.actions() if not a.isSeparator()]


def test_menu_offers_authorize_autostart_and_quit(tray):
    make, _ = tray
    labels = _labels(make())

    assert any("uthorize" in label for label in labels)
    assert any("uto-start" in label for label in labels)
    assert any("Quit" in label for label in labels)


def test_overlay_closing_must_not_quit_the_app(tray, qapp):
    """R14: the overlay is the only window, so this would end the process."""
    make, _ = tray
    make()
    assert qapp.quitOnLastWindowClosed() is False


def test_authorize_action_invokes_the_callback(tray):
    make, calls = tray
    manager = make()
    manager.authorize_action.trigger()
    assert calls["authorize"] == 1


def test_autostart_action_is_checkable_and_invokes_the_callback(tray):
    make, calls = tray
    manager = make()
    assert manager.autostart_action.isCheckable() is True
    manager.autostart_action.trigger()
    assert calls["toggle"] == 1


def test_quit_action_invokes_the_callback(tray):
    make, calls = tray
    manager = make()
    manager.quit_action.trigger()
    assert calls["quit"] == 1


def test_autostart_checkbox_reflects_current_state(tray):
    make, _ = tray
    manager = make()

    manager.update_autostart_checked(True)
    assert manager.autostart_action.isChecked() is True
    manager.update_autostart_checked(False)
    assert manager.autostart_action.isChecked() is False


def test_updating_the_checkbox_does_not_refire_the_callback(tray):
    """Setting the state programmatically must not look like a user click."""
    make, calls = tray
    manager = make()
    manager.update_autostart_checked(True)
    assert calls["toggle"] == 0


def test_tooltip_reports_the_last_sync_in_local_time(tray):
    """Shown to a human at a glance, so it must not be in UTC."""
    make, _ = tray
    manager = make()
    synced = datetime(2026, 8, 18, 14, 5, tzinfo=timezone.utc)
    manager.set_last_sync(synced)

    assert f"{synced.astimezone():%H:%M}" in manager.tooltip()


def test_tooltip_before_any_sync_says_so(tray):
    make, _ = tray
    assert "not synced yet" in make().tooltip().lower()


def test_tooltip_reports_a_stalled_sync(tray):
    """R36: the point of the tooltip is noticing when sync has died."""
    make, _ = tray
    manager = make()
    stale = datetime.now(timezone.utc) - timedelta(minutes=20)
    manager.set_last_sync(stale)

    assert "20 minutes ago" in manager.tooltip()


def test_show_message_is_safe_without_a_real_tray(tray):
    """Offscreen has no tray; a balloon must degrade, not raise."""
    make, _ = tray
    make().show_message("Title", "Body")  # must not raise
