"""Logging that survives the frozen build.

The app ships with ``console=False``, and a windowed Windows process has **no
standard streams at all**: ``sys.stderr`` is ``None``, and ``print(..., file=None)``
silently does nothing rather than raising. Every diagnostic written that way is
dead code in the only form users actually run.

That matters more here than in most apps, because this one's dominant failure
mode is *quietly not alerting* -- a quarantined config, a refused token refresh,
a sync stuck in backoff. Without a log there is nothing to look at afterwards.

So: a rotating file in the directory the app already owns, plus stderr when a
console happens to exist (development).
"""

from __future__ import annotations

import io
import logging
import sys
from logging.handlers import RotatingFileHandler

from airplanenotifier import paths
from airplanenotifier.paths import ensure_config_dir

LOG_NAME = "airplane-notifier"
MAX_BYTES = 512 * 1024
BACKUP_COUNT = 3

_configured = False


def get_logger() -> logging.Logger:
    """The app's logger, configured on first use."""
    global _configured
    logger = logging.getLogger(LOG_NAME)
    if _configured:
        return logger

    logger.setLevel(logging.INFO)
    # No %(name)s: the messages already carry an "airplane-notifier: " prefix
    # from when they were stderr prints, and doubling it reads like a bug.
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        ensure_config_dir()
        # paths.CONFIG_DIR read at call time, not import time: binding the
        # constant meant the test fixture could not redirect it and the suite
        # wrote its warnings into the developer's real log file.
        handler = RotatingFileHandler(
            paths.CONFIG_DIR / "airplane-notifier.log",
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    except OSError:
        # An unwritable config dir must not stop the app; we simply lose the log.
        pass

    # Only useful in development -- absent in the frozen build by design.
    if sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        logger.addHandler(stream)

    logger.propagate = False
    _configured = True
    return logger


def log_path():
    """Where the log lives, for the README and for support questions."""
    return paths.CONFIG_DIR / "airplane-notifier.log"


class _LoggerStream(io.TextIOBase):
    """A file-like object that forwards writes to the logger.

    Every diagnostic in this package is already a ``print(..., file=...)`` call.
    Pointing those at this object routes all of them into the rotating log with
    a one-line change per module, instead of rewriting twenty call sites into
    logger calls and risking a typo in each.
    """

    def __init__(self, level: int = logging.WARNING) -> None:
        self._level = level
        self._buffer = ""

    def write(self, text: str) -> int:
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                get_logger().log(self._level, line.rstrip())
        return len(text)

    def flush(self) -> None:
        if self._buffer.strip():
            get_logger().log(self._level, self._buffer.strip())
        self._buffer = ""

    def writable(self) -> bool:
        return True


#: Drop-in replacement for ``sys.stderr`` in ``print(..., file=...)`` calls.
diagnostic = _LoggerStream()
