"""Generate the app's PNG assets.

These are flat-design placeholders drawn with Pillow so the app has working art
without waiting on hand-drawn or generated illustration. Everything is drawn at
``SS``x and downscaled with LANCZOS, which is what keeps the edges smooth.

The characters are deliberately **side-facing with both legs visible in a
stride** -- a front-facing character reads as a sliding sticker once the walk
animation translates it horizontally (see the plan's Q5 / risk note).

Run:  python tools/generate_placeholder_assets.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

SS = 4  # supersampling factor

ASSETS = Path(__file__).resolve().parent.parent / "assets"

SKIN = (242, 197, 158, 255)
HAIR = (58, 44, 38, 255)
SHOE = (44, 52, 62, 255)

FOOD_SHIRT = (214, 122, 48, 255)
FOOD_TROUSER = (94, 74, 62, 255)
WATER_SHIRT = (58, 122, 184, 255)
WATER_TROUSER = (52, 62, 78, 255)

PLANE_BODY = (232, 238, 245, 255)
PLANE_EDGE = (44, 62, 88, 255)
PLANE_WINDOW = (86, 140, 200, 255)
TRAY_BLUE = (27, 79, 143, 255)


def _canvas(w: int, h: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def _finish(img: Image.Image, w: int, h: int, path: Path) -> None:
    img.resize((w, h), Image.LANCZOS).save(path)
    print(f"  {path.name:<20} {w}x{h}")


def _capsule(draw: ImageDraw.ImageDraw, box, fill) -> None:
    """Rounded bar; radius is half the short side so ends are semicircles."""
    x0, y0, x1, y1 = box
    radius = min(x1 - x0, y1 - y0) // 2
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def _shade(colour, factor: float):
    """Darken a colour, used so an arm reads as separate from the torso."""
    r, g, b, a = colour
    return (int(r * factor), int(g * factor), int(b * factor), a)


def _limb(base: Image.Image, anchor, length: int, width: int, angle: float, fill,
          shoe=None) -> tuple[int, int]:
    """Paste a capsule limb rotated `angle` degrees, hanging from `anchor`.

    Angle is measured from straight-down, positive swinging forward (to the
    right). Returns the final-pixel coordinate of the limb's far end, so the
    caller can put a shoe or a hand there.

    Rotating a drawn capsule is what gives a real stride; axis-aligned bars
    read as a figure standing still no matter how they are offset.
    """
    import math

    s = SS
    ax, ay = anchor[0] * s, anchor[1] * s
    lw, ll = width * s, length * s

    pad = ll + lw
    limb = Image.new("RGBA", (pad * 2, pad * 2), (0, 0, 0, 0))
    ld = ImageDraw.Draw(limb)
    # draw pointing straight down from the pad centre
    cx, cy = pad, pad
    ld.rounded_rectangle((cx - lw // 2, cy - lw // 2, cx + lw // 2, cy + ll),
                         radius=lw // 2, fill=fill)
    if shoe is not None:
        ld.rounded_rectangle((cx - lw // 2, cy + ll - lw, cx + int(lw * 1.7), cy + ll),
                             radius=lw // 3, fill=shoe)

    rotated = limb.rotate(-angle, resample=Image.BICUBIC, center=(cx, cy))
    base.alpha_composite(rotated, (ax - pad, ay - pad))

    rad = math.radians(angle)
    end_x = ax + math.sin(rad) * ll
    end_y = ay + math.cos(rad) * ll
    return int(end_x / s), int(end_y / s)


def draw_walker(path: Path, shirt, trouser, prop: str) -> None:
    """A side-facing person mid-stride, facing right, carrying `prop`.

    Coordinates are in final-pixel space and multiplied by SS on use, so the
    numbers below read as the ~130x220 character they describe.
    """
    w, h = 130, 220
    img, d = _canvas(w, h)
    s = SS

    def box(x0, y0, x1, y1):
        return (x0 * s, y0 * s, x1 * s, y1 * s)

    shirt_back = _shade(shirt, 0.72)
    trouser_back = _shade(trouser, 0.72)

    hip = (64, 138)
    shoulder = (64, 88)

    # --- far side limbs first, darker, so they sit visually behind ---
    _limb(img, hip, 58, 17, -28, trouser_back, shoe=_shade(SHOE, 0.75))
    _limb(img, shoulder, 46, 14, -30, shirt_back)

    # --- torso: narrow for a side profile, leaning slightly into the walk ---
    d.polygon([(52 * s, 92 * s), (78 * s, 86 * s), (76 * s, 146 * s), (54 * s, 146 * s)],
              fill=shirt)
    _capsule(d, box(52, 82, 78, 108), shirt)

    # --- near leg, forward and planted ---
    _limb(img, hip, 58, 18, 24, trouser, shoe=SHOE)

    # --- head, facing right ---
    d.ellipse(box(54, 30, 98, 74), fill=SKIN)
    d.polygon([(96 * s, 50 * s), (103 * s, 55 * s), (96 * s, 59 * s)], fill=SKIN)  # nose
    # hair covers the back and crown only, leaving the face clear
    d.chord(box(52, 28, 96, 72), 172, 350, fill=HAIR)
    d.ellipse(box(84, 47, 90, 54), fill=(40, 40, 46, 255))  # eye
    _capsule(d, box(60, 68, 74, 92), SKIN)  # neck

    # --- near arm, forward and down, carrying the prop ---
    hand = _limb(img, shoulder, 44, 15, 34, shirt)
    hx, hy = hand
    d.ellipse(box(hx - 9, hy - 9, hx + 9, hy + 9), fill=SKIN)

    if prop == "water":
        d.rounded_rectangle(box(hx - 4, hy - 26, hx + 18, hy + 6), radius=3 * s,
                            fill=(206, 232, 248, 215), outline=(74, 132, 180, 255),
                            width=2 * s)
        d.rounded_rectangle(box(hx - 1, hy - 12, hx + 15, hy + 3), radius=2 * s,
                            fill=(74, 154, 214, 255))
    else:
        d.ellipse(box(hx - 4, hy - 20, hx + 22, hy + 6), fill=(200, 62, 54, 255))
        d.ellipse(box(hx + 10, hy - 26, hx + 22, hy - 16), fill=(96, 158, 74, 255))
        d.rectangle(box(hx + 8, hy - 30, hx + 11, hy - 19), fill=(96, 72, 48, 255))

    _finish(img, w, h, path)


def draw_airplane(path: Path) -> None:
    """Side-on airliner silhouette, nose pointing right (direction of travel)."""
    w, h = 220, 96
    img, d = _canvas(w, h)
    s = SS

    def box(x0, y0, x1, y1):
        return (x0 * s, y0 * s, x1 * s, y1 * s)

    def poly(points):
        return [(x * s, y * s) for x, y in points]

    # tail fin
    d.polygon(poly([(18, 44), (40, 8), (54, 8), (48, 46)]), fill=PLANE_EDGE)
    # horizontal stabiliser
    d.polygon(poly([(20, 46), (46, 46), (40, 62), (18, 60)]), fill=PLANE_EDGE)
    # fuselage: long capsule, nose cone added separately so it tapers
    _capsule(d, box(16, 38, 186, 74), PLANE_BODY)
    d.polygon(poly([(170, 38), (212, 56), (170, 74)]), fill=PLANE_BODY)
    # swept wing
    d.polygon(poly([(92, 60), (140, 60), (120, 92), (88, 92)]), fill=PLANE_EDGE)
    # engine
    _capsule(d, box(96, 62, 132, 78), (168, 182, 200, 255))
    # windows
    for i in range(7):
        x = 60 + i * 16
        d.ellipse(box(x, 48, x + 9, 57), fill=PLANE_WINDOW)
    # cockpit glass
    d.polygon(poly([(176, 46), (196, 54), (176, 60)]), fill=PLANE_WINDOW)

    _finish(img, w, h, path)


def draw_tray_icon(path: Path) -> None:
    """32x32 tray glyph: a simple upward plane, legible at 16px."""
    w = h = 32
    img, d = _canvas(w, h)
    s = SS

    def poly(points):
        return [(x * s, y * s) for x, y in points]

    d.ellipse((1 * s, 1 * s, 31 * s, 31 * s), fill=TRAY_BLUE)
    d.polygon(poly([(16, 5), (23, 20), (16, 17), (9, 20)]), fill=(255, 255, 255, 255))
    d.polygon(poly([(13, 22), (16, 20), (19, 22), (16, 27)]), fill=(255, 255, 255, 255))

    _finish(img, w, h, path)


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    print(f"Writing assets to {ASSETS}")
    draw_airplane(ASSETS / "airplane.png")
    draw_tray_icon(ASSETS / "tray_icon.png")
    draw_walker(ASSETS / "walker_food.png", FOOD_SHIRT, FOOD_TROUSER, "food")
    draw_walker(ASSETS / "walker_water.png", WATER_SHIRT, WATER_TROUSER, "water")
    print("Done.")


if __name__ == "__main__":
    main()
