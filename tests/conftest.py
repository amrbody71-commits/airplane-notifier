"""Shared fixtures.

Every test that touches the config directory gets its own tmp_path, so the
suite never reads or writes the developer's real ``~/.airplane-notifier``.

Modules read their locations as ``paths.TOKEN_PATH`` rather than importing the
constants by value, so patching the ``paths`` module here is enough to redirect
all of them.
"""

from __future__ import annotations

import os

import pytest

# Qt must pick the offscreen platform before any QApplication is constructed,
# so the widget tests run without a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def config_dir(tmp_path, monkeypatch):
    """Redirect every config path at a temp directory.

    Autouse, deliberately. Opting in per test was not enough: as soon as any
    module read persisted state at construction time, tests that had not asked
    for isolation started reading -- and writing -- the developer's real
    ~/.airplane-notifier, which both polluted their machine and leaked state
    between tests.
    """
    from airplanenotifier import paths

    target = tmp_path / ".airplane-notifier"
    target.mkdir()

    monkeypatch.setattr(paths, "CONFIG_DIR", target)
    monkeypatch.setattr(paths, "TOKEN_PATH", target / "token.json")
    monkeypatch.setattr(paths, "CONFIG_PATH", target / "config.json")
    monkeypatch.setattr(paths, "STATE_PATH", target / "state.json")
    monkeypatch.setattr(paths, "NUDGE_LOG_PATH", target / "nudge-log.jsonl")
    monkeypatch.setattr(paths, "ALERTED_PATH", target / "alerted.json")

    return target


@pytest.fixture(scope="session")
def qapp():
    """A single QApplication for the whole session.

    Qt allows only one per process, and destroying it between tests upsets the
    offscreen plugin, so this is deliberately session-scoped.
    """
    pytest.importorskip("PyQt6", reason="PyQt6 is required for widget tests")
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
