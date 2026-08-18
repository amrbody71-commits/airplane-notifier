"""Windows auto-start via the registry (U7 -- R15, R16).

Writes ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``, which is
per-user and needs no elevation -- unlike the HKLM equivalent, which would
demand admin rights for a personal tray app.

Every registry call is wrapped: a locked-down or policy-managed machine should
leave the tray menu working, not crash it.
"""

from __future__ import annotations

import sys

from airplanenotifier.log import diagnostic

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows, import-time only
    winreg = None  # type: ignore[assignment]

REGISTRY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "AirplaneNotifier"


def launch_command() -> str:
    """The command Windows should run at login.

    Frozen, ``sys.executable`` is the built exe and stands alone. In
    development it is ``python.exe``, which needs ``-m airplanenotifier`` to
    know what to run (R16). Quoted either way, because both paths routinely
    contain spaces.
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" -m airplanenotifier'


def is_autostart_enabled() -> bool:
    """True when our value is present under the Run key."""
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        print(f"airplane-notifier: cannot read auto-start setting: {exc}", file=diagnostic)
        return False


def enable_autostart() -> bool:
    """Register for auto-start. Returns the resulting state."""
    if winreg is None:
        return False
    try:
        # CreateKeyEx opens the key when it exists and creates it when it does
        # not, so this works on a profile that has never had a Run entry.
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, REGISTRY_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, launch_command())
        return True
    except OSError as exc:
        print(f"airplane-notifier: could not enable auto-start: {exc}", file=diagnostic)
        return False


def disable_autostart() -> bool:
    """Unregister auto-start. Returns the resulting state (always False)."""
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, REGISTRY_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, APP_NAME)
    except FileNotFoundError:
        pass  # already absent, which is the state we wanted
    except OSError as exc:
        print(f"airplane-notifier: could not disable auto-start: {exc}", file=diagnostic)
    return False


def toggle_autostart() -> bool:
    """Flip auto-start and return the new state."""
    if is_autostart_enabled():
        return disable_autostart()
    return enable_autostart()
