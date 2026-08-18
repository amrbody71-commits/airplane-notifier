"""U9 -- walk-in nudge overlay (R20, R20a, R20b, R25, R29).

Geometry gets the most attention here, because the corner is configurable and
each corner changes the entry edge, the walk direction, which way the character
faces, and which side the speech bubble sits on.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6", reason="PyQt6 is required for widget tests")

from PyQt6.QtCore import QPoint, Qt  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402

from airplanenotifier.nudge_overlay import NudgeOverlay  # noqa: E402
from airplanenotifier.paths import asset_path  # noqa: E402

FAST = {"walk_ms": 30, "fade_ms": 10, "hold_seconds": 0.02}


@pytest.fixture
def nudge(qapp):
    made = []

    def _make(question="Did you drink water?", corner="bottom-right", image=None, **kw):
        options = {**FAST, **kw}
        window = NudgeOverlay(
            question,
            image if image is not None else asset_path("walker_water.png"),
            corner=corner,
            **options,
        )
        made.append(window)
        return window

    yield _make
    for window in made:
        window.close()


# --- geometry ---------------------------------------------------------------


@pytest.mark.parametrize("corner", ["bottom-right", "top-right", "bottom-left", "top-left"])
def test_character_starts_fully_off_screen(nudge, corner):
    window = nudge(corner=corner)
    start = window.walk_in.startValue()

    if corner.endswith("right"):
        assert start.x() >= window.width()
    else:
        assert start.x() <= -window.character.width()


@pytest.mark.parametrize("corner", ["bottom-right", "top-right", "bottom-left", "top-left"])
def test_character_rests_fully_on_screen(nudge, corner):
    window = nudge(corner=corner)
    rest = window.walk_in.endValue()

    assert rest.x() >= 0
    assert rest.x() + window.character.width() <= window.width()
    assert rest.y() >= 0
    assert rest.y() + window.character.height() <= window.height()


def test_bottom_corner_puts_the_feet_near_the_bottom(nudge):
    """R20b: a walking character needs a floor to walk on."""
    window = nudge(corner="bottom-right")
    feet = window.walk_in.endValue().y() + window.character.height()
    assert feet > window.height() * 0.85


def test_top_corner_puts_the_character_near_the_top(nudge):
    window = nudge(corner="top-right")
    assert window.walk_in.endValue().y() < window.height() * 0.2


def test_walk_travels_roughly_eighteen_percent_of_the_width(nudge):
    window = nudge(corner="bottom-right")
    travelled = abs(window.walk_in.endValue().x() - window.walk_in.startValue().x())
    assert 0.10 * window.width() <= travelled <= 0.32 * window.width()


def test_walk_out_returns_to_the_entry_edge(nudge):
    window = nudge(corner="bottom-right")
    assert window.walk_out.endValue().x() == window.walk_in.startValue().x()


def test_unknown_corner_falls_back_to_bottom_right(nudge):
    window = nudge(corner="middle")
    assert window.corner == "bottom-right"


# --- facing -----------------------------------------------------------------


def test_entering_from_the_right_the_character_faces_left(nudge):
    """The art faces right, so walking left must mirror it."""
    window = nudge(corner="bottom-right")
    assert window.faces_left_on_entry is True


def test_entering_from_the_left_the_character_faces_right(nudge):
    window = nudge(corner="bottom-left")
    assert window.faces_left_on_entry is False


def test_character_turns_around_to_leave(nudge):
    """Whichever way it came in, it must face the other way going out."""
    for corner in ("bottom-right", "bottom-left"):
        window = nudge(corner=corner)
        assert window.faces_left_on_exit is not window.faces_left_on_entry


# --- speech bubble ----------------------------------------------------------


def test_bubble_shows_the_question(nudge):
    assert nudge("Did you eat?").bubble.text() == "Did you eat?"


def test_bubble_sits_beside_the_character_on_the_inward_side(nudge):
    window = nudge(corner="bottom-right")
    window.start()
    QTest.qWait(80)
    # Entering from the right, the bubble must sit to the character's left.
    assert window.bubble.x() + window.bubble.width() <= window.character.x() + 20


def test_bubble_stays_on_screen_in_a_top_corner(nudge):
    """A bubble above the head would be clipped by the top edge."""
    window = nudge(corner="top-right")
    window.start()
    QTest.qWait(80)

    assert window.bubble.y() >= 0
    assert window.bubble.y() + window.bubble.height() <= window.height()


def test_long_question_wraps(nudge):
    window = nudge("Did you remember to drink a full glass of water this afternoon?")
    assert window.bubble.wordWrap() is True
    assert window.bubble.width() <= window.width() * 0.5


# --- interaction ------------------------------------------------------------


def test_ignoring_the_character_emits_ignored_once(nudge):
    window = nudge()
    seen = []
    window.acknowledged.connect(lambda: seen.append("ack"))
    window.ignored.connect(lambda: seen.append("ignored"))

    window.start()
    QTest.qWait(600)

    assert seen == ["ignored"]


def test_clicking_the_character_acknowledges(nudge):
    window = nudge(hold_seconds=5)
    seen = []
    window.acknowledged.connect(lambda: seen.append("ack"))
    window.ignored.connect(lambda: seen.append("ignored"))

    window.start()
    QTest.qWait(80)
    window.click_at(window.character.geometry().center())

    assert seen == ["ack"]


def test_clicking_beside_the_character_passes_through(nudge):
    """R29: only the character is a hit target; the rest is click-through."""
    window = nudge(hold_seconds=5)
    seen = []
    window.acknowledged.connect(lambda: seen.append("ack"))

    window.start()
    QTest.qWait(80)
    handled = window.click_at(QPoint(5, 5))

    assert seen == []
    assert handled is False


def test_a_nudge_resolves_exactly_once(nudge):
    """A click during the walk-out must not double-fire."""
    window = nudge(hold_seconds=0.02)
    seen = []
    window.acknowledged.connect(lambda: seen.append("ack"))
    window.ignored.connect(lambda: seen.append("ignored"))

    window.start()
    QTest.qWait(600)
    window.click_at(window.character.geometry().center())

    assert len(seen) == 1


def test_overlay_is_not_click_through_at_the_window_level(nudge):
    """The plane sets this attribute; the nudge must not, or clicks never land."""
    window = nudge()
    assert not window.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)


# --- robustness -------------------------------------------------------------


def test_missing_character_image_still_shows_the_question(nudge, tmp_path):
    window = nudge(image=tmp_path / "nope.png")
    assert window.bubble.text()
    window.start()  # must not raise


def test_animation_group_is_retained_on_the_instance(nudge):
    window = nudge()
    assert window.animation is window._animation
    assert window.animation.parent() is not None
