# -*- coding: utf-8 -*-
"""Build the dancing perch GIFs, for celebrating in chat.

Per dance:  perch-<dance>-128.gif (custom emoji) | perch-<dance>-512.gif (posted in a message)

Each dance is a pose function over loop time t in [0, 1). The owl's shapes and
colours come from gen_round3.py, the same source as the static mark; only the
wings and the motion are defined here.
"""
import math, os, shutil, subprocess, tempfile
import gen_round3 as g

OUT = "assets/perch-dance"
SIZES = (128, 512)

S = g.P_SHAPES["base"]
C = next(col for sid, _, _, col, _ in g.PERCH if sid == "p-cream")
NAVY, STEEL = g.NAVY, g.STEEL
IRIS = "#c98a3c"  # perch_sym writes the iris amber inline too

# 24 frames of 40ms: a 0.96s loop holding two beats, which is 125 bpm.
FRAMES, DELAY_CS = 24, 4

# The mark's 64-unit frame plus room for the hop, the sway, and raised wings.
VIEWBOX = (-10, -12, 84, 84)
FEET_L, FEET_R, FEET_Y = S["feet"]
PIVOT = (32.0, FEET_Y + 4.5)  # between the feet, on the ground: leans and squashes turn here

# The navy body sits within a few steps of a dark chat background, where the owl
# reduces to its eyes and chest. An off-white rim separates it there and vanishes
# into a light one. GIF transparency is all or nothing per pixel, so the rim also
# hides the hard-cut edge where it would show most.
OUTLINE, OUTLINE_WIDTH = C["disc"], 2.8

# The static mark has no wings. Each one is rooted inside the body and drawn
# behind it, so only the part that swings clear shows. Defined for the left wing:
# root at the origin, hanging down +y, outer edge on -x.
WING = "M-3.5,-3 C-11,2 -10.5,15 -2.5,22 C1.5,15 4.5,6 3.5,-3 Z"
WING_ROOT = (14.0, 31.0)
TUFT_BASE_L, TUFT_BASE_R = (21.0, 11.0), (43.0, 11.0)


def wave(t, phase=0.0):
    return math.sin(2 * math.pi * (t - phase))


def rest():
    return {"sway": 0.0, "hop": 0.0, "sx": 1.0, "sy": 1.0, "tuft": 0.0, "spread": 0.0,
            "look": 0.0, "look_y": 0.0, "wing_l": 22.0, "wing_r": 22.0,
            "foot_l": 0.0, "foot_r": 0.0}


def hop(t):
    """Hop side to side, landing on a lean with a squash.

    The wing opposite the lean goes up with a flutter and both are level
    mid-hop. The pupils follow the beat and the tufts trail the body.
    """
    s, c = wave(t), math.cos(2 * math.pi * t)
    contact = max(0.0, 1 - abs(c) / 0.35)  # 1 at each landing
    sway = 10 * s
    return {**rest(),
            "sway": sway,
            "hop": 5.5 * abs(c),
            "sx": 1 + 0.08 * contact,
            "sy": 1 - 0.10 * contact,
            # How far the sway has moved since a moment ago, so the tufts lag it.
            "tuft": 1.3 * (10 * wave(t, 0.1) - sway),
            "look": 1.5 * wave(t, 0.04),
            "wing_l": 22 + 108 * (0.5 + 0.5 * s) + 14 * max(0.0, s) ** 2 * wave(t * 4),
            "wing_r": 22 + 108 * (0.5 - 0.5 * s) + 14 * max(0.0, -s) ** 2 * wave(t * 4),
            "foot_l": 2.4 * max(0.0, s),
            "foot_r": 2.4 * max(0.0, -s)}


def roof(t):
    """Raise the roof: both wings push up on every beat while the body dips.

    The head tilts to alternate sides on alternate beats, the tufts flick out,
    and the eyes look up with each push.
    """
    pump = (0.5 + 0.5 * math.cos(2 * math.pi * ((2 * t) % 1))) ** 1.5  # 1 on the beat
    return {**rest(),
            "sway": 5 * math.cos(2 * math.pi * t),
            "hop": 3 * (1 - pump),
            "sx": 1 + 0.06 * pump ** 2,
            "sy": 1 - 0.08 * pump ** 2,
            "spread": 9 * pump,
            "look_y": -1.4 * pump,
            "wing_l": 68 + 90 * pump,
            "wing_r": 68 + 90 * pump,
            "foot_l": 1.5 * (1 - pump),
            "foot_r": 1.5 * (1 - pump)}


DANCES = {
    "hop": (hop, True),
    "hop-bare": (hop, False),  # for backgrounds known to be light
    "roof": (roof, True),
}


def owl(p, outline):
    """The mark in pose p. Outline mode draws the silhouette only, stroked wide."""
    if outline:
        paint = lambda _fill: (f'fill="{OUTLINE}" stroke="{OUTLINE}" '
                               f'stroke-width="{OUTLINE_WIDTH}" stroke-linejoin="round"')
    else:
        paint = lambda fill: f'fill="{fill}"'
    wx, wy = WING_ROOT
    tl, tr = p["tuft"] - p["spread"], p["tuft"] + p["spread"]
    parts = [
        # Each wing takes the other half's colour, so it separates from the
        # side it emerges from.
        f'<g transform="translate({wx},{wy}) rotate({p["wing_l"]:.2f})">'
        f'<path {paint(STEEL)} d="{WING}"/></g>',
        f'<g transform="translate({64 - wx},{wy}) scale(-1,1) rotate({p["wing_r"]:.2f})">'
        f'<path {paint(NAVY)} d="{WING}"/></g>',
        f'<path {paint(NAVY)} transform="rotate({tl:.2f},{TUFT_BASE_L[0]},{TUFT_BASE_L[1]})" d="{S["tl"]}"/>',
        f'<path {paint(STEEL)} transform="rotate({tr:.2f},{TUFT_BASE_R[0]},{TUFT_BASE_R[1]})" d="{S["tr"]}"/>',
        f'<rect {paint(C["feet"])} x="{FEET_L}" y="{FEET_Y - p["foot_l"]:.2f}" width="5" height="4.5" rx="2"/>',
        f'<rect {paint(C["feet"])} x="{FEET_R}" y="{FEET_Y - p["foot_r"]:.2f}" width="5" height="4.5" rx="2"/>',
        f'<path {paint(NAVY)} d="{S["hl"]}"/>',
        f'<path {paint(STEEL)} d="{S["hr"]}"/>',
    ]
    if not outline:
        lx, ly = p["look"], p["look_y"]
        parts.append(f'<path fill="{C["belly"]}" d="{S["belly"]}"/>')
        for ex in (S["ecx"], 64 - S["ecx"]):
            ix, iy = ex + lx, S["ecy"] + ly
            parts += [
                f'<circle fill="{C["disc"]}" cx="{ex}" cy="{S["ecy"]}" r="{S["er"]}"/>',
                f'<circle fill="{IRIS}" cx="{ix:.2f}" cy="{iy:.2f}" r="{S["ir"]}"/>',
                f'<circle fill="{NAVY}" cx="{ix:.2f}" cy="{iy:.2f}" r="{S["pr"]}"/>',
                f'<circle fill="{C["cat"]}" cx="{ex - 1.9 + 0.6 * lx:.2f}" '
                f'cy="{S["ecy"] - 2.0 + 0.6 * ly:.2f}" r="{S["cr"]}"/>',
            ]
        parts.append(f'<path fill="{C["beak"]}" d="{S["beak"]}"/>')
    return "".join(parts)


def frame(pose, outlined, t):
    p = pose(t)
    px, py = PIVOT
    body = (f'translate(0,{-p["hop"]:.2f}) rotate({p["sway"]:.2f},{px},{py}) '
            f'translate({px},{py}) scale({p["sx"]:.3f},{p["sy"]:.3f}) translate({-px},{-py})')
    layers = ([owl(p, True)] if outlined else []) + [owl(p, False)]
    x, y, w, h = VIEWBOX
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x} {y} {w} {h}">\n'
            + "".join(f'<g transform="{body}">{layer}</g>\n' for layer in layers)
            + "</svg>\n")


def run(*a):
    subprocess.run(a, check=True)


shutil.rmtree(OUT, ignore_errors=True)
os.makedirs(OUT)
for name, (pose, outlined) in DANCES.items():
    with tempfile.TemporaryDirectory() as tmp:
        svgs = []
        for i in range(FRAMES):
            svgs.append(os.path.join(tmp, f"{i:03d}.svg"))
            open(svgs[-1], "w").write(frame(pose, outlined, i / FRAMES))
        for px in SIZES:
            pngs = [f"{svg[:-4]}-{px}.png" for svg in svgs]
            for svg, png in zip(svgs, pngs):
                run("rsvg-convert", "-w", str(px), "-h", str(px), svg, "-o", png)
            gif = f"{OUT}/perch-{name}-{px}.gif"
            # Background disposal clears each frame, or a transparent GIF smears.
            run("magick", "-delay", str(DELAY_CS), "-loop", "0", "-dispose", "Background",
                *pngs, "-layers", "Optimize", gif)
            print("built", gif, f"{os.path.getsize(gif) // 1024} KB")
