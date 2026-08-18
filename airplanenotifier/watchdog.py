"""A tiny always-on process that keeps the main app alive.

The registry Run key and a login-triggered scheduled task both fire *once*,
at login, with no retry. If the main app crashes, is killed, or a login
launch silently fails, nothing brings it back until the next reboot -- which
is exactly what happened on 2026-08-18: a restart killed the app at 17:32,
and it stayed dead through two missed meeting alerts until this was fixed.

This process holds its own mutex, then loops forever: check whether the main
app's single-instance mutex still exists; if not, relaunch it. The check is
a single cheap Win32 call with no disk or network I/O, so the loop can run
every few seconds for free, and any single exception is caught and logged
rather than killing the loop -- the one job of this process is to never stop
watching.

Deliberately has almost no dependencies (stdlib plus this package's own
``log``/``paths``, neither of which touches PyQt6 or the Google client) so it
builds into a tiny second executable rather than doubling the size of the
main one.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from airplanenotifier.log import get_logger

WATCHDOG_MUTEX_NAME = "AirplaneNotifierWatchdogSingleInstance"
APP_MUTEX_NAME = "AirplaneNotifierSingleInstance"

# How often the loop checks. Cheap enough to run tight: this number is the
# whole reason a crash is invisible for seconds, not the 30+ minutes it used
# to take with a periodic-relaunch scheduled task alone.
CHECK_INTERVAL_SECONDS = 10

SYNCHRONIZE = 0x00100000
ERROR_ALREADY_EXISTS = 183


def _acquire_watchdog_lock() -> bool:
    """True if this is the only watchdog running."""
    ctypes.windll.kernel32.CreateMutexW(None, False, WATCHDOG_MUTEX_NAME)
    return ctypes.windll.kernel32.GetLastError() != ERROR_ALREADY_EXISTS


def _main_app_alive() -> bool:
    """Whether the main app's single-instance mutex still exists.

    Windows closes every handle a process holds -- including this mutex --
    the instant it exits, whether cleanly or by crashing. A missing mutex
    therefore reliably means the app is not running right now, with no need
    to track a PID that could be reused by an unrelated process.
    """
    handle = ctypes.windll.kernel32.OpenMutexW(SYNCHRONIZE, False, APP_MUTEX_NAME)
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    return False


def _main_exe_path() -> Path:
    """The main app, expected beside this executable in the same frozen build."""
    return Path(sys.executable).resolve().parent / "airplane-notifier.exe"


def _relaunch_main_app() -> None:
    exe = _main_exe_path()
    if not exe.exists():
        get_logger().warning("watchdog: main exe missing at %s", exe)
        return
    subprocess.Popen([str(exe)], cwd=str(exe.parent))


def watchdog_loop(
    is_alive: Callable[[], bool],
    relaunch: Callable[[], None],
    sleep_fn: Callable[[float], None],
    max_iterations: Optional[int] = None,
) -> None:
    """The decision loop, injectable so it is testable without a real Win32 mutex.

    Checks first, sleeps after: a freshly (re)started watchdog acts on its
    very first iteration rather than waiting out a full interval before its
    first check.
    """
    logger = get_logger()
    i = 0
    while max_iterations is None or i < max_iterations:
        try:
            if not is_alive():
                logger.warning("watchdog: main app is not running -- relaunching")
                relaunch()
        except Exception as exc:  # noqa: BLE001 - this loop must never stop watching
            logger.warning("watchdog: check failed (%s)", exc)
        sleep_fn(CHECK_INTERVAL_SECONDS)
        i += 1


def main() -> int:
    if sys.platform != "win32":
        return 0
    if not _acquire_watchdog_lock():
        return 0  # another watchdog already holds the lock; nothing to do

    get_logger().info("watchdog started (pid %s)", os.getpid())
    watchdog_loop(_main_app_alive, _relaunch_main_app, time.sleep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
