# -*- coding: utf-8 -*-
"""Build the repository's social preview card from the wide light lockup.

    cd docs/brand && python3 build_social_card.py

GitHub, Slack, and Teams render this when the repository or the documentation
site is linked. It is the light lockup at full card width on a white ground,
letterboxed to the 2:1 frame those services expect.

Needs `rsvg-convert` and `magick` on PATH, like the other builders here. This
exists because the card used to be produced by hand, which meant a palette
change silently left it stale.
"""
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "header")
SRC = os.path.join(OUT, "sentinel-header-light.svg")
DST = os.path.join(OUT, "sentinel-social-card.png")

W, H, GROUND = 1280, 640, "#ffffff"


def run(*a):
    subprocess.run(a, check=True)


strip = os.path.join(OUT, ".social-strip.png")
# The lockup is transparent; flatten onto the ground before extending so the
# letterbox and the artwork share exactly one white.
run("rsvg-convert", "-w", str(W), "-b", GROUND, SRC, "-o", strip)
run("magick", strip, "-background", GROUND, "-gravity", "center",
    "-extent", f"{W}x{H}", DST)
os.remove(strip)
print(f"built {os.path.relpath(DST, HERE)}  {W}x{H}")
