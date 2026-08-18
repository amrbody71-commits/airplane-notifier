"""Transparent full-screen overlays (U4 -- R8-R11).

Also home to :class:`TransparentFullScreenWindow`, the small base the nudge
overlay reuses (KTD9). Only the window setup is shared: the two overlays differ
in entry geometry, lifecycle, and hit-testing, and folding them into one class
would produce a flag-driven mess.
"""

from __future__ import annotations

import sys

from airplanenotifier.log import diagnostic
from pathlib import Path
from typing import Optional, Union

from PyQt6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRect,
    Qt,
    pyqtSignal,
)
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QApplication, QLabel, QWidget

# Speed, not a fixed duration. A fixed time means the plane gets faster the
# wider your desk is -- plugging in a second monitor would silently double the
# speed and make the banner unreadable. Pixels per second keeps it legible on
# any setup, and the flight simply takes longer across more screens.
FLIGHT_SPEED_PX_PER_SEC = 240

# Bounds so a tiny screen is not over in a blink, nor a wall of monitors a chore.
MIN_FLIGHT_MS = 6000
MAX_FLIGHT_MS = 40000

# Gap between the plane's tail and the banner it tows.
ROPE_LENGTH = 46

ROPE_STYLE = "background-color: rgba(238, 242, 246, 200); border-radius: 1px;"

BANNER_STYLE = """
    background-color: rgba(16, 22, 29, 210);
    color: #FFFFFF;
    font-family: 'Segoe UI Variable Display', 'Segoe UI', sans-serif;
    font-size: 30px;
    font-weight: 600;
    padding: 14px 26px;
    border-radius: 12px;
"""


MAX_TITLE_CHARS = 120


def _clamp_title(title: str) -> str:
    """Trim a title that would otherwise dominate the screen.

    Word wrap cannot break an unbroken string (a pasted meeting URL is the
    common case), so long titles either clip silently or grow the label past
    the screen. Truncating is honest about it.
    """
    collapsed = " ".join(title.split())
    if len(collapsed) <= MAX_TITLE_CHARS:
        return collapsed
    return collapsed[: MAX_TITLE_CHARS - 1].rstrip() + "\u2026"


def load_pixmap(path: Optional[Union[str, Path]]) -> QPixmap:
    """Load an image, returning a null pixmap rather than raising.

    A missing asset must degrade the overlay, never crash the timer that
    triggered it.
    """
    pixmap = QPixmap()
    if path is None:
        return pixmap
    if not pixmap.load(str(path)):
        print(f"airplane-notifier: could not load image {path}", file=diagnostic)
    return pixmap


class TransparentFullScreenWindow(QWidget):
    """A frameless, always-on-top, fully transparent full-screen window.

    ``Tool`` keeps it out of the taskbar and the alt-tab list, which matters
    for something that appears unannounced several times a day.
    """

    def __init__(self, span_all_screens: bool = False) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._spans_all_screens = span_all_screens
        self.setGeometry(self._screen_geometry(span_all_screens))

    @staticmethod
    def _screen_geometry(span_all_screens: bool = False):
        """The area to cover: one monitor, or the whole desk.

        ``virtualGeometry`` is the bounding rectangle of every connected
        screen. Its origin is NOT necessarily (0, 0) -- a monitor placed to the
        left of the laptop gives negative x -- so the window is positioned at
        that rect rather than assuming the desktop starts at the origin.
        """
        screen = QApplication.primaryScreen()
        if screen is None:  # no display attached (monitors off, RDP disconnect)
            return QRect(0, 0, 1920, 1080)
        return screen.virtualGeometry() if span_all_screens else screen.geometry()

    def reveal(self) -> None:
        """Show the window.

        ``showFullScreen()`` snaps to a single monitor, which defeats a window
        deliberately sized to span several, so a spanning window is shown at
        its explicit geometry instead.
        """
        if self._spans_all_screens:
            self.setGeometry(self._screen_geometry(True))
            self.show()
        else:
            self.showFullScreen()
        self.raise_()


class OverlayWindow(TransparentFullScreenWindow):
    """An airplane crossing the screen carrying the meeting name.

    Fully click-through: it appears without warning, so it must never swallow
    a click meant for whatever is underneath.
    """

    finished = pyqtSignal()

    def __init__(
        self,
        meeting_name: str,
        airplane_path: Optional[Union[str, Path]] = None,
        duration_ms: Optional[int] = None,
        speed_px_per_sec: Optional[int] = None,
    ) -> None:
        # The plane crosses the whole desk, monitor through to laptop, rather
        # than appearing on one screen and stopping dead at its edge.
        super().__init__(span_all_screens=True)
        # Set unconditionally: WA_TranslucentBackground makes the window see
        # through, but it still captures clicks without this.
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        self._duration = duration_ms or self._duration_for_width(speed_px_per_sec)
        self._build(meeting_name or "Meeting starting", airplane_path)

    def _build(self, meeting_name: str, airplane_path) -> None:
        pixmap = load_pixmap(airplane_path)

        self.airplane = QLabel(self)
        self.airplane.setPixmap(pixmap)
        self.airplane.resize(
            pixmap.width() or 1,
            pixmap.height() or 1,
        )

        self.banner = QLabel(_clamp_title(meeting_name), self)
        # PlainText, never AutoText. A meeting title is attacker-influenced data
        # from Google; AutoText runs mightBeRichText() and parses markup, so a
        # title containing <img src="file://host/share/x.png"> makes Qt resolve
        # a UNC path -- an outbound SMB connection that leaks NetNTLMv2, and a
        # ~21s freeze on the main thread while it times out.
        self.banner.setTextFormat(Qt.TextFormat.PlainText)
        self.banner.setStyleSheet(BANNER_STYLE)
        self.banner.setWordWrap(True)
        self.banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Bounded so a long title wraps instead of running off the screen.
        self.banner.setMaximumWidth(int(self.width() * 0.5))
        # Height needs bounding too: setMaximumWidth alone let a long title grow
        # to thousands of pixels tall and render entirely off-screen.
        self.banner.setMaximumHeight(int(self.height() * 0.25))
        self.banner.adjustSize()

        # The tow rope, a thin bar between the tail and the banner.
        self.rope = QLabel(self)
        self.rope.setStyleSheet(ROPE_STYLE)
        self.rope.resize(ROPE_LENGTH, 3)

        flight_y = self.height() // 2 - self.airplane.height() // 2
        self.airplane.move(-self._trailing_width(), flight_y)
        self._position_banner()

        # Held on the instance: a local QPropertyAnimation can be collected
        # before it finishes, silently stopping the plane mid-flight.
        self._animation = QPropertyAnimation(self.airplane, b"pos", self)
        self._animation.setDuration(self._duration)
        self._animation.setStartValue(QPoint(-self._trailing_width(), flight_y))
        self._animation.setEndValue(QPoint(self.width(), flight_y))
        self._animation.setEasingCurve(QEasingCurve.Type.Linear)
        self._animation.valueChanged.connect(lambda _value: self._position_banner())
        self._animation.finished.connect(self._on_finished)

    def _duration_for_width(self, speed: Optional[int]) -> int:
        """How long to cross everything, at a constant readable speed."""
        pixels_per_sec = speed or FLIGHT_SPEED_PX_PER_SEC
        if pixels_per_sec <= 0:
            pixels_per_sec = FLIGHT_SPEED_PX_PER_SEC
        millis = int(self.width() / pixels_per_sec * 1000)
        return max(MIN_FLIGHT_MS, min(millis, MAX_FLIGHT_MS))

    @property
    def animation(self) -> QPropertyAnimation:
        return self._animation

    def _trailing_width(self) -> int:
        """Width of the whole rig, so plane AND banner start fully off-screen."""
        return self.airplane.width() + ROPE_LENGTH + self.banner.width()

    def _position_banner(self) -> None:
        """Tow the banner behind the plane, the way an advertising plane does.

        The plane flies left to right, so the banner trails to its LEFT on a
        rope from the tail. All three sit at the same height so the rig reads
        as one object being towed, not a plane with a caption underneath.
        """
        tail_x = self.airplane.x()
        centre_y = self.airplane.y() + self.airplane.height() // 2

        rope_x = tail_x - ROPE_LENGTH
        self.rope.move(rope_x, centre_y - self.rope.height() // 2)

        banner_y = centre_y - self.banner.height() // 2
        # Keep it on screen whatever the title does to the label's size.
        banner_y = max(8, min(banner_y, self.height() - self.banner.height() - 8))
        self.banner.move(rope_x - self.banner.width(), banner_y)

    def start(self) -> None:
        self.reveal()
        self._animation.start()

    def _on_finished(self) -> None:
        self.finished.emit()
        self.close()
