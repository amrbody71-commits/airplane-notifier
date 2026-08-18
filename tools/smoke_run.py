"""Start the real application headlessly and report what happened.

Unit tests exercise pieces; this proves the whole thing boots -- QApplication,
tray, both timers, the auth attempt, and a forced nudge -- inside a real Qt
event loop. Nothing here touches the developer's own config directory.

Run:  QT_QPA_PLATFORM=offscreen python tools/smoke_run.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Redirect the config directory before anything imports it.
_TEMP_HOME = Path(tempfile.mkdtemp(prefix="airplane-smoke-"))

from airplanenotifier import paths  # noqa: E402

paths.CONFIG_DIR = _TEMP_HOME
paths.TOKEN_PATH = _TEMP_HOME / "token.json"
paths.CONFIG_PATH = _TEMP_HOME / "config.json"
paths.STATE_PATH = _TEMP_HOME / "state.json"
paths.NUDGE_LOG_PATH = _TEMP_HOME / "nudge-log.jsonl"

from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from airplanenotifier import config  # noqa: E402
from airplanenotifier.main import NotifierApp  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {label}{(' - ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


def main() -> int:
    print(f"Smoke run (config dir: {_TEMP_HOME})\n")

    app = QApplication(sys.argv)
    notifier = NotifierApp(app)
    notifier.start()

    check("QApplication does not quit when an overlay closes",
          app.quitOnLastWindowClosed() is False)
    check("nudge timer is running", notifier.nudge_timer.isActive())
    check("config.json created on first run", paths.CONFIG_PATH.exists())

    # Force a nudge to be due right now, then let the tick pick it up.
    config.save_state({
        "water": {"next_due": datetime.now().astimezone() - timedelta(minutes=1),
                  "consecutive_ignores": 0},
        "food": {"next_due": datetime.now().astimezone() + timedelta(hours=5),
                 "consecutive_ignores": 0},
    })

    results = {}

    def force_nudge():
        cfg = config.load_config()
        # Widen active hours so the smoke test works at any hour of the day.
        cfg["active_hours"] = {"start": "00:00", "end": "23:59"}
        paths.CONFIG_PATH.write_text(__import__("json").dumps(cfg))
        results["shown"] = notifier.scheduler.tick()

    def inspect_nudge():
        overlay = notifier._active_overlay
        results["overlay"] = overlay
        if overlay is not None:
            results["question"] = overlay.bubble.text()
            results["corner"] = overlay.corner
            # Acknowledge it, which should resolve and clear the overlay.
            overlay.click_at(overlay.character.geometry().center())

    def finish():
        check("scheduler fired the due nudge", results.get("shown") == "water",
              str(results.get("shown")))
        check("a character overlay appeared", results.get("overlay") is not None)
        check("it asked the water question",
              results.get("question") == "Did you drink water?",
              str(results.get("question")))
        check("it used the default bottom-right corner",
              results.get("corner") == "bottom-right", str(results.get("corner")))
        check("acknowledging cleared the overlay", notifier.is_overlay_active() is False)
        check("acknowledging rescheduled the nudge",
              config.load_state().get("water", {}).get("next_due") is not None)
        check("the appearance was logged", paths.NUDGE_LOG_PATH.exists())
        check("auth failure left the app running (no credentials.json)",
              notifier.sync_timer.isActive() is False)
        app.quit()

    QTimer.singleShot(300, force_nudge)
    QTimer.singleShot(600, inspect_nudge)
    QTimer.singleShot(900, finish)

    app.exec()

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("All smoke checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
