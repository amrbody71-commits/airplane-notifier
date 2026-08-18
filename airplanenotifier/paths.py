"""Filesystem locations, shared by every module.

Two path families live here:

* ``CONFIG_DIR`` -- the per-user directory holding the OAuth token, the
  editable config, the scheduler state, and the nudge log.
* ``asset_path()`` -- bundled read-only images, which move under
  ``sys._MEIPASS`` once PyInstaller freezes the app (R3).
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

CONFIG_DIR = Path.home() / ".airplane-notifier"

TOKEN_PATH = CONFIG_DIR / "token.json"
CONFIG_PATH = CONFIG_DIR / "config.json"
STATE_PATH = CONFIG_DIR / "state.json"
NUDGE_LOG_PATH = CONFIG_DIR / "nudge-log.jsonl"
ALERTED_PATH = CONFIG_DIR / "alerted.json"


def ensure_config_dir() -> Path:
    """Create the per-user config directory if it does not exist yet."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return CONFIG_DIR


def _bundle_root() -> Path:
    """Directory that holds bundled data files.

    PyInstaller unpacks bundled data under ``sys._MEIPASS``; in development the
    files sit next to the package, one level up from this module.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent.parent


def credentials_path() -> Path:
    """Location of the GCP OAuth client file (R3).

    Searched next to the executable first, then inside the bundle. Keeping the
    file beside the exe means it is *not* baked into the distributed build,
    which would publish the OAuth client secret to anyone holding a copy (plan
    review item 7). It also lets the file be replaced without a rebuild.

    When nothing is found, the returned path is where the user should put it,
    so the error message can name a directory that actually helps.
    """
    candidates = []
    if getattr(sys, "_MEIPASS", None):
        candidates.append(Path(sys.executable).resolve().parent / "credentials.json")
    candidates.append(_bundle_root() / "credentials.json")

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def asset_path(name: str) -> Path:
    """Location of a bundled asset such as ``airplane.png``."""
    return _bundle_root() / "assets" / name


def make_stdout_safe() -> None:
    """Stop the Windows console throwing ``UnicodeEncodeError`` on non-ASCII.

    The legacy console codepage cannot encode much of what we log, and an
    unhandled ``UnicodeEncodeError`` inside a Qt timer callback kills the tick.
    Reconfiguring to UTF-8 with replacement is the repo's standing pattern.
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            # Detached or already-wrapped streams (pythonw.exe has none at all).
            buffer = getattr(stream, "buffer", None)
            if buffer is None:
                continue
            try:
                setattr(
                    sys,
                    stream_name,
                    io.TextIOWrapper(buffer, encoding="utf-8", errors="replace"),
                )
            except (AttributeError, ValueError, OSError):
                pass
