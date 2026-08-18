"""U4 -- transparent airplane overlay (R8-R11).

Driven against Qt's offscreen platform plugin, so the suite needs no display
and never flashes a real full-screen window at whoever is running it.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6", reason="PyQt6 is required for widget tests")

from PyQt6.QtCore import QAbstractAnimation, Qt  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402

from airplanenotifier.overlay import OverlayWindow  # noqa: E402
from airplanenotifier.paths import asset_path  # noqa: E402


@pytest.fixture
def overlay(qapp):
    made = []

    def _make(name="Team Standup", image=None, duration_ms=None,
              speed_px_per_sec=None):
        window = OverlayWindow(
            name,
            image if image is not None else asset_path("airplane.png"),
            duration_ms=duration_ms,
            speed_px_per_sec=speed_px_per_sec,
        )
        made.append(window)
        return window

    yield _make
    for window in made:
        window.close()


def test_window_is_frameless_topmost_and_off_the_taskbar(overlay):
    flags = overlay().windowFlags()
    assert flags & Qt.WindowType.FramelessWindowHint
    assert flags & Qt.WindowType.WindowStaysOnTopHint
    # The Tool flag is what keeps it out of the taskbar and alt-tab.
    assert flags & Qt.WindowType.Tool


def test_window_is_translucent_and_click_through(overlay):
    """R8: translucency alone does not pass clicks through; the attribute does."""
    window = overlay()
    assert window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert window.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)


def test_meeting_name_is_displayed(overlay):
    assert "Q3 Planning" in overlay("Q3 Planning").banner.text()


def test_long_meeting_name_wraps_rather_than_clipping(overlay):
    window = overlay("Q3 Planning Review with Engineering and Product Teams")
    assert window.banner.wordWrap() is True
    assert window.banner.width() <= window.width()


def test_airplane_crosses_the_full_screen_width(overlay):
    """R9: the whole rig starts off the left edge and ends off the right.

    The plane now tows a banner, so it must start far enough left that the
    banner is off-screen too -- otherwise the text is visible before the
    plane arrives.
    """
    window = overlay()
    start = window.animation.startValue()
    end = window.animation.endValue()

    assert start.x() == -window._trailing_width()
    assert start.x() < -window.airplane.width()   # room for the banner as well
    assert end.x() == window.width()
    assert start.y() == end.y()  # level flight


def test_the_flight_speed_is_constant_regardless_of_desk_width(overlay):
    """A fixed duration would speed the plane up as screens are added.

    The original 5s across a 1280px screen was 256 px/s and already too fast
    to read; the default is deliberately slower than that.
    """
    window = overlay()
    px_per_sec = window.width() / (window.animation.duration() / 1000)

    # Never faster than the version that was too quick to read...
    assert px_per_sec < 256
    # ...and on a narrow screen the minimum-duration clamp slows it further
    # rather than letting the flight be over in a blink.
    assert window.animation.duration() >= 6000


def test_a_lower_speed_setting_means_a_longer_flight(overlay):
    slow = overlay(speed_px_per_sec=100).animation.duration()
    fast = overlay(speed_px_per_sec=300).animation.duration()
    assert slow > fast


def test_a_nonsense_speed_falls_back_to_the_default(overlay):
    assert overlay(speed_px_per_sec=0).animation.duration() > 0


def test_flight_time_can_be_overridden(overlay):
    assert overlay(duration_ms=3000).animation.duration() == 3000


def test_animation_is_retained_on_the_instance(overlay):
    """P1 fix: a local QPropertyAnimation is garbage-collected mid-flight."""
    window = overlay()
    assert window.animation.parent() is not None
    assert window.animation is window._animation


def test_start_runs_the_animation(overlay):
    window = overlay(duration_ms=60)
    window.start()
    assert window.animation.state() == QAbstractAnimation.State.Running


def test_finished_signal_fires_when_the_animation_completes(overlay):
    window = overlay(duration_ms=40)
    seen = []
    window.finished.connect(lambda: seen.append(True))

    window.start()
    QTest.qWait(400)

    assert seen == [True]


def test_the_banner_is_towed_behind_the_plane(overlay):
    """Like an advertising plane: banner trails on a rope, at the same height."""
    window = overlay("Team Standup")
    window._position_banner()

    # Banner sits to the LEFT of the plane, because it flies left to right.
    assert window.banner.x() + window.banner.width() <= window.airplane.x()
    # The rope bridges the gap between them.
    assert window.rope.x() >= window.banner.x() + window.banner.width() - 2
    assert window.rope.x() + window.rope.width() <= window.airplane.x() + 2
    # All at the same height, so it reads as one towed rig.
    plane_mid = window.airplane.y() + window.airplane.height() // 2
    banner_mid = window.banner.y() + window.banner.height() // 2
    assert abs(plane_mid - banner_mid) <= window.banner.height()


def test_banner_tracks_the_airplane(overlay):
    """The name rides with the plane rather than sitting still under it."""
    window = overlay(duration_ms=60)
    window.start()
    first = window.banner.pos().x()
    QTest.qWait(120)
    assert window.banner.pos().x() != first


def test_missing_image_still_shows_the_name(overlay, tmp_path):
    """R: a missing asset degrades to a banner, it does not crash."""
    window = overlay("Standup", image=tmp_path / "nope.png")
    assert window.airplane.pixmap().isNull()
    assert "Standup" in window.banner.text()
    window.start()  # must not raise


def test_empty_meeting_name_falls_back_to_a_placeholder(overlay):
    assert overlay("").banner.text().strip() != ""
