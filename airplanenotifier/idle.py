"""Idle time and workstation-lock detection (part of U10 -- R24).

Implemented with ``ctypes`` against ``user32``/``kernel32`` rather than adding
``pywin32`` as a dependency for two calls (KTD12).

Both functions return a permissive default on any failure. A nudge that fires
when it should not is a minor annoyance; a ctypes quirk that silently
suppresses every nudge forever would look like the feature is simply broken.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

_IS_WINDOWS = sys.platform == "win32"

# Any access right will do -- we only care whether the handle opens at all.
_DESKTOP_SWITCHDESKTOP = 0x0100


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def idle_seconds() -> float:
    """Seconds since the last keyboard or mouse input, 0.0 if unknown."""
    if not _IS_WINDOWS:
        return 0.0
    try:
        info = _LastInputInfo()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        # restype MUST be set. ctypes defaults to signed 32-bit, which truncates
        # the 64-bit tick count and flips it negative at ~24.9 days of uptime --
        # reintroducing, and worsening, the exact rollover this call avoids.
        # idle_seconds() would then read 0.0 forever and idle suppression would
        # silently stop working on a machine that is never rebooted.
        get_ticks = ctypes.windll.kernel32.GetTickCount64
        get_ticks.restype = ctypes.c_uint64
        ticks = get_ticks()
        return max(0.0, (ticks - info.dwTime) / 1000.0)
    except (OSError, AttributeError, ValueError):
        return 0.0


def is_workstation_locked() -> bool:
    """True when the machine is locked, False if it cannot be determined.

    When the workstation is locked, Windows switches to the secure desktop and
    ``OpenInputDesktop`` returns NULL for a normal process.
    """
    if not _IS_WINDOWS:
        return False
    try:
        user32 = ctypes.windll.user32
        handle = user32.OpenInputDesktop(0, False, _DESKTOP_SWITCHDESKTOP)
        if not handle:
            return True
        user32.CloseDesktop(handle)
        return False
    except (OSError, AttributeError, ValueError):
        return False
