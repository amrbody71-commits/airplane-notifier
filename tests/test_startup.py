"""U7 -- auto-start registry integration (R15, R16).

These exercise the real ``winreg`` API rather than a mock, but against a
scratch key under ``HKCU\\Software\\AirplaneNotifierTest``. Pointing them at
the live Run key would install a genuine auto-start entry on whoever ran the
suite, which is not a side effect a test may have.
"""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows registry only")

from airplanenotifier import startup  # noqa: E402  (import after the skip guard)

TEST_KEY = r"Software\AirplaneNotifierTest"


@pytest.fixture
def scratch_registry(monkeypatch):
    """Redirect the module at a scratch key and remove it afterwards."""
    import winreg

    monkeypatch.setattr(startup, "REGISTRY_KEY", TEST_KEY)
    monkeypatch.setattr(startup, "APP_NAME", "AirplaneNotifierTest")
    yield
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, TEST_KEY)
    except OSError:
        pass


def test_reports_disabled_when_no_key_exists(scratch_registry):
    assert startup.is_autostart_enabled() is False


def test_enable_creates_the_value(scratch_registry):
    assert startup.enable_autostart() is True
    assert startup.is_autostart_enabled() is True


def test_disable_removes_the_value(scratch_registry):
    startup.enable_autostart()
    assert startup.disable_autostart() is False
    assert startup.is_autostart_enabled() is False


def test_disable_is_safe_when_absent(scratch_registry):
    assert startup.disable_autostart() is False  # must not raise


def test_enable_is_idempotent(scratch_registry):
    startup.enable_autostart()
    startup.enable_autostart()
    assert startup.is_autostart_enabled() is True


def test_toggle_from_disabled_enables(scratch_registry):
    assert startup.toggle_autostart() is True
    assert startup.is_autostart_enabled() is True


def test_toggle_from_enabled_disables(scratch_registry):
    startup.enable_autostart()
    assert startup.toggle_autostart() is False
    assert startup.is_autostart_enabled() is False


def test_stored_command_points_at_the_running_interpreter(scratch_registry):
    import winreg

    startup.enable_autostart()
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, TEST_KEY) as key:
        stored, _ = winreg.QueryValueEx(key, "AirplaneNotifierTest")

    assert sys.executable in stored


def test_dev_command_invokes_the_module(monkeypatch):
    """R16: in development the value must run python -m, not a bare exe."""
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)

    command = startup.launch_command()
    assert command.startswith('"')
    assert "-m airplanenotifier" in command


def test_frozen_command_is_the_executable_alone(monkeypatch):
    """R16: PyInstaller points sys.executable at the built exe."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\apps\airplane-notifier.exe")

    assert startup.launch_command() == r'"C:\apps\airplane-notifier.exe"'


def test_registry_failure_is_reported_not_raised(monkeypatch):
    """A locked-down registry must not crash the tray menu."""
    def boom(*args, **kwargs):
        raise OSError("access denied")

    monkeypatch.setattr(startup.winreg, "CreateKeyEx", boom)
    monkeypatch.setattr(startup, "REGISTRY_KEY", TEST_KEY)
    monkeypatch.setattr(startup, "APP_NAME", "AirplaneNotifierTest")

    assert startup.enable_autostart() is False
