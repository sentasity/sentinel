# -*- coding: utf-8 -*-
"""Build the full asset set for the four chosen Sentinel marks.

Per mark:  mark.svg (transparent) | tile.svg (inset on amber)
           PNGs at 16..1024 transparent | tile-192.png (Teams color icon)
           outline-32.png (Teams outline icon: white silhouette, eyes knocked out)
           sentry-256.png (Sentry small icon: black silhouette, face knocked out)
           favicon.ico (16/32/48)
"""
import os, subprocess, shutil
import gen_round3 as g

OUT = "assets"
NAVY, STEEL = g.NAVY, g.STEEL

# The selection round is over: `perch` is the adopted mark and the only one built.
# The three candidates it beat stay defined in gen_round3.py as the record of that
# round, and are recoverable from git history.
CHOSEN = {
    "perch": ("perch", "cream"),
}
CUR = {n: (sid, shape, col) for sid, n, shape, col, _ in
       [(s, n.lower(), sh, c, b) for s, n, sh, c, b in g.CURIOUS]}
PER = {n: (sid, shape, col) for sid, n, shape, col, _ in
       [(s, n.lower(), sh, c, b) for s, n, sh, c, b in g.PERCH]}
PER["shaded"] = ("p-shaded", "base",
                 dict(belly="", disc="#eef4f9", beak="#ddb377", feet="#c98a3c", cat="#ffffff"))
# A tile has to let the navy body and the off-white chest read at once. White never
# did: the chest sits at 1.19:1 against it and reads only because the dark body
# encircles it. Against amber the body measures 5.08:1 and the chest 2.46:1.
TILE_BG = "#c98a3c"

def sym_body(sid, kind, shape, col):
    fn = g.curious_sym if kind == "curious" else g.perch_sym
    s = fn(sid, shape, col, band="#4d82a8") if (kind == "perch" and sid == "p-shaded") \
        else fn(sid, shape, col)
    return s[s.index(">") + 1: s.index("</symbol>")].strip()

def wrap(body, label, extra=""):
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64" '
            f'role="img" aria-label="{label}">\n{extra}  {body}\n</svg>\n')

def silhouette(kind, shape):
    """White mark with the eyes knocked out — Teams renders outline icons monochrome."""
    s = (g.C_SHAPES if kind == "curious" else g.P_SHAPES)[shape]
    lx, rx, cy, er = s["ecx"], 64 - s["ecx"], s["ecy"], s["er"]
    solid = [s["tl"], s["tr"], s["hl"], s["hr"]]
    holes = "".join(f'<circle fill="#000" cx="{cx}" cy="{cy}" r="{er * 0.62:.2f}"/>' for cx in (lx, rx))
    feet = ""
    if kind == "perch":
        fl, fr, fy = s["feet"]
        feet = "".join(f'<rect fill="#fff" x="{x}" y="{fy}" width="5" height="4.5" rx="2"/>'
                       for x in (fl, fr))
    paths = "".join(f'<path fill="#fff" d="{d}"/>' for d in solid)
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="32" height="32">\n'
            f'  <mask id="k">{paths}{feet}{holes}</mask>\n'
            '  <rect width="64" height="64" fill="#ffffff" mask="url(#k)"/>\n</svg>\n')

# Sentry recolors a small icon and crops it to a circle, so the file may hold only
# black and transparent pixels and the mark has to clear the circle. The marks reach
# 32.25 units from center in the 64-unit frame, so 0.88 leaves ~3.5 units of margin.
SENTRY_FIT = 0.88

# Tiles inset the mark rather than bleeding it to the edges. A full-bleed mark
# fills an avatar with the bird's dark body, so at the size Teams actually renders
# it the tile reads as a navy ground with eyes and a belly floating on it instead
# of as an owl. Insetting puts the tile's own ground back around the mark, which
# is the read the README header gets for free from the page behind it.
#
# Microsoft masks the color icon's corners at runtime and asks for the brand mark
# inside a 120x120 safe area of the 192x192 canvas, balanced around 96x96. The mark
# spans ~56 units of its 64-unit frame, so in a 192px tile its height is
# 56*3*TILE_FIT. 0.70 lands it at 117.6px, the largest value that still clears the
# safe area; 0.71 breaches it.
TILE_FIT = 0.70

MARK_CY = 30.5  # measured vertical center of the marks, which sit high in the frame


def sentry_mask(kind, shape):
    """Black silhouette with the eyes and beak knocked out, inset for a circular crop.

    The pupils stay behind as black islands inside the knocked-out eye discs, which is
    what keeps the face reading as the mark rather than as a generic bird.
    """
    s = (g.C_SHAPES if kind == "curious" else g.P_SHAPES)[shape]
    lx, rx, cy = s["ecx"], 64 - s["ecx"], s["ecy"]
    solid = "".join(f'<path fill="#fff" d="{s[k]}"/>' for k in ("tl", "tr", "hl", "hr"))
    if kind == "perch":
        fl, fr, fy = s["feet"]
        solid += "".join(f'<rect fill="#fff" x="{x}" y="{fy}" width="5" height="4.5" rx="2"/>'
                         for x in (fl, fr))
    discs = "".join(f'<circle fill="#000" cx="{cx}" cy="{cy}" r="{s["er"] * 0.95:.2f}"/>'
                    for cx in (lx, rx))
    pupils = "".join(f'<circle fill="#fff" cx="{cx}" cy="{cy}" r="{s["pr"] * 1.2:.2f}"/>'
                     for cx in (lx, rx))
    beak = f'<path fill="#000" d="{s["beak"]}"/>'
    fit = (f'<g transform="translate(32,32) scale({SENTRY_FIT}) '
           f'translate(-32,{-MARK_CY})">{solid}{discs}{pupils}{beak}</g>')
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="256" height="256">\n'
            f'  <mask id="k" maskUnits="userSpaceOnUse" x="0" y="0" width="64" height="64">{fit}</mask>\n'
            '  <rect width="64" height="64" fill="#000000" mask="url(#k)"/>\n</svg>\n')


def inset(body):
    """The mark centered in the 64-unit frame at TILE_FIT, so ground surrounds it."""
    return (f'<g transform="translate(32,32) scale({TILE_FIT}) '
            f'translate(-32,{-MARK_CY})">{body}</g>')


def run(*a):
    subprocess.run(a, check=True)

os.makedirs(OUT, exist_ok=True)
for name, (kind, key) in CHOSEN.items():
    sid, shape, col = (CUR if kind == "curious" else PER)[key]
    d = os.path.join(OUT, name)
    shutil.rmtree(d, ignore_errors=True)  # per mark, so assets/README.md survives a rebuild
    os.makedirs(d, exist_ok=True)
    body = sym_body(sid, kind, shape, col)

    open(f"{d}/{name}.svg", "w").write(wrap(body, f"Sentinel — {name}"))
    # Square corners on purpose: Teams and GitHub both round the tile themselves,
    # and Microsoft calls a pre-rounded upload out by name.
    open(f"{d}/{name}-tile.svg", "w").write(wrap(
        inset(body), f"Sentinel — {name} tile",
        extra=f'  <rect width="64" height="64" fill="{TILE_BG}"/>\n'))
    open(f"{d}/{name}-outline.svg", "w").write(silhouette(kind, shape))
    open(f"{d}/{name}-sentry.svg", "w").write(sentry_mask(kind, shape))

    for px in (16, 32, 48, 64, 128, 256, 512, 1024):
        run("rsvg-convert", "-w", str(px), "-h", str(px),
            f"{d}/{name}.svg", "-o", f"{d}/{name}-{px}.png")
    run("rsvg-convert", "-w", "192", "-h", "192", "-b", TILE_BG,
        f"{d}/{name}-tile.svg", "-o", f"{d}/{name}-tile-192.png")
    run("rsvg-convert", "-w", "512", "-h", "512",
        f"{d}/{name}-tile.svg", "-o", f"{d}/{name}-tile-512.png")
    run("rsvg-convert", "-w", "32", "-h", "32",
        f"{d}/{name}-outline.svg", "-o", f"{d}/{name}-outline-32.png")
    run("rsvg-convert", "-w", "256", "-h", "256",
        f"{d}/{name}-sentry.svg", "-o", f"{d}/{name}-sentry-256.png")
    run("magick", f"{d}/{name}-16.png", f"{d}/{name}-32.png", f"{d}/{name}-48.png",
        f"{d}/{name}-favicon.ico")
    print("built", name)
