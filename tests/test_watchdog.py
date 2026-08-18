"""U-watchdog -- keeps the main app alive across a crash or a missed login launch.

The Win32-specific pieces (mutex create/open) are proven live rather than
unit-tested, the same convention as idle.py's ctypes calls. What is tested is
the decision logic: check, relaunch iff dead, never let one bad check kill the
loop.
"""

from __future__ import annotations

from airplanenotifier.watchdog import watchdog_loop


class FakeClock:
    def __init__(self):
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)


def test_relaunches_when_the_main_app_is_not_running():
    relaunched = []
    watchdog_loop(is_alive=lambda: False, relaunch=lambda: relaunched.append(1),
                 sleep_fn=FakeClock().sleep, max_iterations=1)
    assert relaunched == [1]


def test_does_not_relaunch_a_healthy_app():
    relaunched = []
    watchdog_loop(is_alive=lambda: True, relaunch=lambda: relaunched.append(1),
                 sleep_fn=FakeClock().sleep, max_iterations=3)
    assert relaunched == []


def test_relaunches_again_if_still_dead_next_check():
    """A relaunch that itself fails to start must not stop the watchdog trying."""
    relaunched = []
    watchdog_loop(is_alive=lambda: False, relaunch=lambda: relaunched.append(1),
                 sleep_fn=FakeClock().sleep, max_iterations=3)
    assert relaunched == [1, 1, 1]


def test_stops_relaunching_once_the_app_recovers():
    calls = {"n": 0}

    def is_alive():
        calls["n"] += 1
        return calls["n"] > 1  # dead on the first check, alive after that

    relaunched = []
    watchdog_loop(is_alive=is_alive, relaunch=lambda: relaunched.append(1),
                 sleep_fn=FakeClock().sleep, max_iterations=4)
    assert relaunched == [1]


def test_a_raising_alive_check_does_not_kill_the_loop():
    """The one job of this process is to never stop watching."""
    calls = {"n": 0}

    def flaky_is_alive():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient Win32 hiccup")
        return False

    relaunched = []
    watchdog_loop(is_alive=flaky_is_alive, relaunch=lambda: relaunched.append(1),
                 sleep_fn=FakeClock().sleep, max_iterations=3)
    # Iteration 1 raised (no relaunch that round); iterations 2-3 saw it dead.
    assert relaunched == [1, 1]
    assert calls["n"] == 3


def test_a_raising_relaunch_does_not_kill_the_loop_either():
    attempts = []

    def flaky_relaunch():
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("could not spawn process")

    watchdog_loop(is_alive=lambda: False, relaunch=flaky_relaunch,
                 sleep_fn=FakeClock().sleep, max_iterations=3)
    assert len(attempts) == 3


def test_the_loop_sleeps_once_per_iteration():
    clock = FakeClock()
    watchdog_loop(is_alive=lambda: True, relaunch=lambda: None,
                 sleep_fn=clock.sleep, max_iterations=5)
    assert len(clock.slept) == 5


def test_main_exe_path_is_beside_this_executable(monkeypatch):
    from pathlib import Path

    from airplanenotifier.watchdog import _main_exe_path

    monkeypatch.setattr("sys.executable",
                        r"C:\apps\airplane-notifier\airplane-notifier-watchdog.exe")
    assert _main_exe_path() == Path(r"C:\apps\airplane-notifier\airplane-notifier.exe")


def test_relaunch_reports_a_missing_exe_rather_than_raising(tmp_path, monkeypatch):
    from airplanenotifier.watchdog import _relaunch_main_app

    monkeypatch.setattr("sys.executable", str(tmp_path / "airplane-notifier-watchdog.exe"))
    _relaunch_main_app()  # the main exe does not exist here; must not raise
