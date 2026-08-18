"""The walk-in nudge character (U9 -- R20, R20a, R20b, R25, R29).

A character walks in from a screen corner, asks one question, waits, and walks
back out. Clicking it acknowledges; ignoring it lets it leave.

This is a separate class from :class:`~airplanenotifier.overlay.OverlayWindow`
rather than a mode of it (KTD9). The two differ in entry geometry, in lifecycle
(a five-phase sequence versus a single traverse), and most importantly in
hit-testing: the plane is click-through everywhere, while the nudge needs the
character itself to be a click target.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Optional, Union

from PyQt6.QtCore import (
    QEasingCurve,
    QPauseAnimation,
    QPoint,
    QPropertyAnimation,
    QSequentialAnimationGroup,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QRegion, QTransform
from PyQt6.QtWidgets import QGraphicsOpacityEffect, QLabel

from airplanenotifier.overlay import TransparentFullScreenWindow, load_pixmap

VALID_CORNERS = ("bottom-right", "top-right", "bottom-left", "top-left")
DEFAULT_CORNER = "bottom-right"

WALK_MS = 1500
FADE_MS = 300
HOLD_SECONDS = 4

# How far across the screen the character walks before stopping.
TRAVEL_FRACTION = 0.18
# Distance from the screen edge to the character's feet / head.
BOTTOM_MARGIN_FRACTION = 0.04
TOP_MARGIN_FRACTION = 0.08

BOB_INTERVAL_MS = 120
BOB_PIXELS = 6

BUBBLE_STYLE = """
    background-color: rgba(16, 22, 29, 225);
    color: #FFFFFF;
    font-family: 'Segoe UI Variable Display', 'Segoe UI', sans-serif;
    font-size: 22px;
    font-weight: 600;
    padding: 14px 20px;
    border-radius: 14px;
"""


class NudgeOverlay(TransparentFullScreenWindow):
    """One nudge appearance: walk in, ask, walk out."""

    acknowledged = pyqtSignal()
    ignored = pyqtSignal()

    def __init__(
        self,
        question: str,
        character_path: Optional[Union[str, Path]] = None,
        corner: str = DEFAULT_CORNER,
        hold_seconds: float = HOLD_SECONDS,
        walk_ms: int = WALK_MS,
        fade_ms: int = FADE_MS,
    ) -> None:
        # Stays on the primary screen: a character walking the full width of a
        # two-monitor desk to ask about water would be absurd.
        super().__init__(span_all_screens=False)
        # Deliberately NOT WA_TransparentForMouseEvents: unlike the plane, this
        # overlay has a hit target. Click-through for everything else is
        # handled per-click in mousePressEvent (R29).
        self.corner = corner if corner in VALID_CORNERS else DEFAULT_CORNER
        self._resolved = False
        self._bob_phase = 0
        self._base_y = 0

        self._walk_ms = walk_ms
        self._fade_ms = fade_ms
        self._hold_ms = max(1, int(hold_seconds * 1000))

        self._build(question, character_path)

    # -- construction --------------------------------------------------------

    def _build(self, question: str, character_path) -> None:
        pixmap = load_pixmap(character_path)
        self._pixmap = pixmap
        self._mirrored = (
            pixmap.transformed(QTransform().scale(-1, 1)) if not pixmap.isNull() else pixmap
        )

        self.character = QLabel(self)
        self.character.resize(max(pixmap.width(), 1), max(pixmap.height(), 1))

        self.bubble = QLabel(question, self)
        # Same reasoning as the meeting banner: never let text become markup.
        self.bubble.setTextFormat(Qt.TextFormat.PlainText)
        self.bubble.setStyleSheet(BUBBLE_STYLE)
        self.bubble.setWordWrap(True)
        self.bubble.setMaximumWidth(int(self.width() * 0.5))
        self.bubble.adjustSize()

        self._bubble_opacity = QGraphicsOpacityEffect(self.bubble)
        self._bubble_opacity.setOpacity(0.0)
        self.bubble.setGraphicsEffect(self._bubble_opacity)

        start, rest = self._entry_geometry()
        self._base_y = start.y()
        self.character.move(start)
        self._face(self.faces_left_on_entry)
        self._position_bubble()

        self._build_sequence(start, rest)

        # Bob only while walking; a character bobbing on the spot mid-sentence
        # reads as idle fidgeting rather than motion.
        self._bob_timer = QTimer(self)
        self._bob_timer.setInterval(BOB_INTERVAL_MS)
        self._bob_timer.timeout.connect(self._bob)

    def _entry_geometry(self) -> tuple[QPoint, QPoint]:
        """Off-screen start point and on-screen rest point for this corner."""
        width, height = self.width(), self.height()
        char_w, char_h = self.character.width(), self.character.height()

        if self.corner.endswith("right"):
            start_x = width
            rest_x = width - int(width * TRAVEL_FRACTION) - char_w // 2
        else:
            start_x = -char_w
            rest_x = int(width * TRAVEL_FRACTION) - char_w // 2

        # Keep the character fully on screen at rest regardless of art size.
        rest_x = max(0, min(rest_x, width - char_w))

        if self.corner.startswith("bottom"):
            y = height - char_h - int(height * BOTTOM_MARGIN_FRACTION)
        else:
            y = int(height * TOP_MARGIN_FRACTION)
        y = max(0, min(y, height - char_h))

        return QPoint(start_x, y), QPoint(rest_x, y)

    def _build_sequence(self, start: QPoint, rest: QPoint) -> None:
        self.walk_in = QPropertyAnimation(self.character, b"pos", self)
        self.walk_in.setDuration(self._walk_ms)
        self.walk_in.setStartValue(start)
        self.walk_in.setEndValue(rest)
        # Decelerate into the stop; a linear stop reads as hitting a wall.
        self.walk_in.setEasingCurve(QEasingCurve.Type.OutQuad)
        self.walk_in.valueChanged.connect(lambda _v: self._position_bubble())

        fade_in = QPropertyAnimation(self._bubble_opacity, b"opacity", self)
        fade_in.setDuration(self._fade_ms)
        fade_in.setStartValue(0.0)
        fade_in.setEndValue(1.0)

        hold = QPauseAnimation(self._hold_ms, self)

        fade_out = QPropertyAnimation(self._bubble_opacity, b"opacity", self)
        fade_out.setDuration(self._fade_ms)
        fade_out.setStartValue(1.0)
        fade_out.setEndValue(0.0)

        self.walk_out = QPropertyAnimation(self.character, b"pos", self)
        self.walk_out.setDuration(self._walk_ms)
        self.walk_out.setStartValue(rest)
        self.walk_out.setEndValue(start)
        self.walk_out.setEasingCurve(QEasingCurve.Type.InQuad)
        self.walk_out.valueChanged.connect(lambda _v: self._position_bubble())

        # Held on the instance so it is not garbage-collected mid-sequence.
        self._animation = QSequentialAnimationGroup(self)
        for step in (self.walk_in, fade_in, hold, fade_out, self.walk_out):
            self._animation.addAnimation(step)

        self.walk_in.finished.connect(self._stop_bob)
        fade_out.finished.connect(self._turn_around)
        self._animation.finished.connect(self._on_sequence_finished)

    @property
    def animation(self) -> QSequentialAnimationGroup:
        return self._animation

    # -- facing --------------------------------------------------------------

    @property
    def faces_left_on_entry(self) -> bool:
        """Entering from the right means walking left, so the art mirrors."""
        return self.corner.endswith("right")

    @property
    def faces_left_on_exit(self) -> bool:
        return not self.faces_left_on_entry

    def _face(self, left: bool) -> None:
        """Point the character at its direction of travel."""
        self.character.setPixmap(self._mirrored if left else self._pixmap)

    def _turn_around(self) -> None:
        self._face(self.faces_left_on_exit)
        self._start_bob()

    # -- bubble --------------------------------------------------------------

    def _update_input_mask(self) -> None:
        """Restrict the window's input region to the character and bubble.

        Qt composites this window with UpdateLayeredWindow, so Windows already
        does per-pixel hit testing and transparent pixels should fall through.
        Two reviewers disagreed on whether that holds in every configuration,
        and relying on undocumented compositing behaviour for "does the app eat
        every click on the desktop for 8 seconds" is not a good trade. An
        explicit mask makes it deterministic either way.
        """
        region = QRegion(self.character.geometry()).united(QRegion(self.bubble.geometry()))
        self.setMask(region)

    def _position_bubble(self) -> None:
        """Sit the bubble on the character's inward side, clamped on screen."""
        gap = 16
        if self.corner.endswith("right"):
            x = self.character.x() - self.bubble.width() - gap
        else:
            x = self.character.x() + self.character.width() + gap
        x = max(8, min(x, self.width() - self.bubble.width() - 8))

        # Beside the head rather than above it, so a top corner cannot clip it.
        y = self.character.y() + self.character.height() // 6
        y = max(8, min(y, self.height() - self.bubble.height() - 8))

        self.bubble.move(x, y)
        self._update_input_mask()

    # -- walk cycle ----------------------------------------------------------

    def _start_bob(self) -> None:
        self._bob_timer.start()

    def _stop_bob(self) -> None:
        self._bob_timer.stop()

    def _bob(self) -> None:
        """A small vertical oscillation that reads as footfall.

        Applied as an offset from the animated y, so it rides along with the
        position animation rather than fighting it.
        """
        self._bob_phase += 1
        offset = int(math.sin(self._bob_phase * math.pi / 2) * BOB_PIXELS / 2)
        self.character.move(self.character.x(), self._base_y + offset)

    # -- interaction ---------------------------------------------------------

    def click_at(self, point: QPoint) -> bool:
        """Handle a click at `point`. Returns True when it hit the character.

        Exposed so the behaviour is directly testable without synthesising
        native mouse events through the offscreen platform plugin.
        """
        if not self.character.geometry().contains(point):
            return False
        self._resolve(self.acknowledged)
        return True

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self.click_at(event.pos()):
            event.accept()
        else:
            # Not ours: ignoring lets it fall through to whatever is beneath.
            event.ignore()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self.reveal()
        self._start_bob()
        self._animation.start()

    def _on_sequence_finished(self) -> None:
        self._resolve(self.ignored)

    def _resolve(self, signal) -> None:
        """Emit exactly one outcome per appearance, then close.

        Guarded because a click landing during the walk-out would otherwise
        fire alongside the sequence's own completion.
        """
        if self._resolved:
            return
        self._resolved = True
        self._stop_bob()
        self._animation.stop()
        signal.emit()
        self.close()
