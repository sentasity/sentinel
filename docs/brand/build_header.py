#!/usr/bin/env python3
"""Build the README header lockup: the wordmark, a telephone wire, and the
perch-cream owl perched at the point where the wire bottoms out, with a
crescent moon off its shoulder.

Two aspects, from the same geometry. The `wide` pair is the shipped banner and
is meant to run the full column width, so the wire reaches both edges. The
`compact` pair is the original narrower crop, for anywhere the banner has to sit
at a fixed size.

The wordmark is converted to outlines, because GitHub has no Gotham Rounded and
live <text> would fall back to whatever the reader happens to have installed.
Needs `hb-view` (harfbuzz), `rsvg-convert` (librsvg), and Gotham Rounded Bold
installed locally, so regeneration only works on a machine with the licensed
font. The committed SVGs are self-contained and need none of that.

    cd docs/brand && python3 build_header.py
"""
import math
import os
import re
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "header")
FONT = os.path.expanduser("~/Library/Fonts/GothamRnd-Bold.otf")

WORD, SIZE, TRACK = "Sentinel", 116, -2.0
NAVY, STEEL, AMBER, CREAM = "#1b2741", "#2f485e", "#c98a3c", "#f5e7d0"

THEMES = {
    "light": dict(word=NAVY, wire=NAVY, wire_op=0.9, moon=AMBER),
    "dark": dict(word="#f2e6d2", wire="#8aa6c0", wire_op=0.95, moon="#f2e6d2"),
}

# perch-cream, verbatim from assets/perch-cream/perch-cream.svg. 64x64 space,
# with the amber feet running y 54.0 -> 58.5.
OWL = '''<path fill="#1b2741" d="M24,8.5 C21,5 18,3 16,2.5 C16,6 17,10 19,13 C20.5,11.2 22.3,9.7 24,8.5 Z"/>
<path fill="#2f485e" d="M40,8.5 C43,5 46,3 48,2.5 C48,6 47,10 45,13 C43.5,11.2 41.7,9.7 40,8.5 Z"/>
<rect fill="#c98a3c" x="25.5" y="54.0" width="5" height="4.5" rx="2"/>
<rect fill="#c98a3c" x="33.5" y="54.0" width="5" height="4.5" rx="2"/>
<path fill="#1b2741" d="M32,7 C19,7 10,19 10,33 C10,47 20,57 32,57 Z"/>
<path fill="#2f485e" d="M32,7 C45,7 54,19 54,33 C54,47 44,57 32,57 Z"/>
<path fill="#f5e7d0" d="M32,36 C25.5,36 21.5,42 21.5,48 C21.5,53.6 26,57 32,57 C38,57 42.5,53.6 42.5,48 C42.5,42 38.5,36 32,36 Z"/>
<circle fill="#faf4ec" cx="23" cy="23" r="7.6"/><circle fill="#faf4ec" cx="41" cy="23" r="7.6"/>
<circle fill="#c98a3c" cx="23" cy="23" r="4.1"/><circle fill="#1b2741" cx="23" cy="23" r="2.0"/>
<circle fill="#c98a3c" cx="41" cy="23" r="4.1"/><circle fill="#1b2741" cx="41" cy="23" r="2.0"/>
<circle fill="#fffdf9" cx="21.1" cy="21.0" r="1.4"/><circle fill="#fffdf9" cx="39.1" cy="21.0" r="1.4"/>
<path fill="#ddb377" d="M32,29.5 C30.1,29.5 29,30.5 29,31.7 C29,33.5 30.6,35.4 32,36.2 C33.4,35.4 35,33.5 35,31.7 C35,30.5 33.9,29.5 32,29.5 Z"/>'''

FEET_BOTTOM, OWL_W = 58.5, 44.0   # in the owl's own 64-unit space
PAD, WIRE_W, OWL_H = 30, 4.0, 128

# gap:  space between the wordmark and the owl
# drop: how far the wire sags. A longer span needs a deeper sag, or the curve
#       reads as a wobble rather than weight
# moon: how far left of the owl the crescent sits, its radius, and how far above
#       the wordmark baseline it floats. The compact aspect has no room for it:
#       anywhere it fits, it lands between the final l and the owl and reads as
#       punctuation rather than sky, so that aspect goes without.
VARIANTS = {
    "":         dict(gap=620, drop=36, moon=dict(dx=120, r=27, rise=48)),
    "compact-": dict(gap=150, drop=26, moon=None),
}


def outlined(word, size, tracking):
    """The word as glyph outlines, baseline at the origin, first glyph at x=0."""
    tmp = os.path.join(OUT, ".hb.svg")
    subprocess.run(["hb-view", f"--font-file={FONT}", "--output-format=svg",
                    f"--font-size={size}", word, "-o", tmp], check=True)
    doc = open(tmp).read()
    os.remove(tmp)
    glyphs = {m.group(1): m.group(2).strip() for m in
              re.finditer(r'<g id="(glyph-[^"]+)">\s*<path d="([^"]+)"', doc)}
    uses = re.findall(r'<use xlink:href="#(glyph-[^"]+)" x="([\d.-]+)" y="([\d.-]+)"', doc)
    if not uses:
        raise SystemExit(f"hb-view produced no glyphs for {word!r}; is {FONT} installed?")
    x0 = float(uses[0][1])
    return "".join(
        f'<path transform="translate({float(x) - x0 + i * tracking:.3f},0)" d="{glyphs[g]}"/>'
        for i, (g, x, _) in enumerate(uses))


def crescent(cx, cy, r, fill, rot=-22, k=1.0):
    """A crescent, as the major arc of the rim and the minor arc of a bite disc
    of the same radius offset by k*r, meeting at the horn tips.

    Not two circles with fill-rule="evenodd": that only holds while the bite
    stays entirely inside the rim, and any crescent worth the name needs it
    further out than that, at which point the overhang fills too and you get two
    overlapping discs. Horns sit at x = kr/2, y = +/- r*sqrt(1 - k^2/4), so
    anything below about k = 1 wraps the rim right round and reads as a ring.
    """
    xi = k * r / 2.0
    yi = r * math.sqrt(max(0.0, 1.0 - k * k / 4.0))
    d = (f"M{cx + xi:.2f},{cy - yi:.2f} "
         f"A{r:.2f},{r:.2f} 0 1,0 {cx + xi:.2f},{cy + yi:.2f} "
         f"A{r:.2f},{r:.2f} 0 0,1 {cx + xi:.2f},{cy - yi:.2f} Z")
    return f'<path fill="{fill}" transform="rotate({rot} {cx:.2f} {cy:.2f})" d="{d}"/>'


def svg(w, h, body):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" '
            f'height="{h}" role="img" aria-label="Sentinel">\n{body}\n</svg>\n')


def ink_box(group):
    """Ink bounding box of a glyph group whose baseline sits at the origin."""
    W, H, ax, ay = 3000, 700, 200, 500
    probe, png = os.path.join(OUT, ".probe.svg"), os.path.join(OUT, ".probe.png")
    open(probe, "w").write(svg(W, H, f'<g fill="#000" transform="translate({ax},{ay})">{group}</g>'))
    subprocess.run(["rsvg-convert", "-w", str(W), "-h", str(H), probe, "-o", png], check=True)
    out = subprocess.run(["magick", png, "-trim", "-format", "%w %h %X %Y", "info:"],
                         capture_output=True, text=True, check=True).stdout.split()
    for f in (probe, png):
        os.remove(f)
    w, h, ox, oy = (float(v.lstrip("+")) for v in out)
    return dict(w=w, left=ox - ax, cap=ay - oy)


def header(theme, group, box, gap, drop, moon):
    """The wire bottoms out exactly under the owl, wherever the owl is put."""
    base = 24 + box["cap"]
    y0 = base + 40
    ow = OWL_W * OWL_H / 64
    w = int(PAD + box["w"] + gap + ow + PAD)
    ox = w - PAD - ow / 2
    yl = y0 + drop
    h = int(yl + 22)

    s = OWL_H / 64.0
    owl_tx = ox - 32.0 * s
    owl_ty = yl + WIRE_W / 2.0 + 0.8 - FEET_BOTTOM * s
    toes = "".join(
        f'<rect fill="{AMBER}" x="{ox + (fx - 32.0) * s - 2.5 * s:.2f}" '
        f'y="{yl - (WIRE_W + 4) / 2:.2f}" width="{5.0 * s:.2f}" '
        f'height="{WIRE_W + 4}" rx="{2.5 * s:.2f}"/>' for fx in (28.0, 36.0))

    d = (f"M0,{y0} Q{ox * 0.55:.1f},{yl} {ox},{yl} "
         f"Q{ox + (w - ox) * 0.45:.1f},{yl} {w},{y0}")
    return w, h, "\n".join(part for part in [
        f'<path d="{d}" fill="none" stroke="{theme["wire"]}" stroke-width="{WIRE_W}" '
        f'stroke-linecap="round" opacity="{theme["wire_op"]}"/>',
        f'<g transform="translate({owl_tx:.2f},{owl_ty:.2f}) scale({s:.4f})">{OWL}</g>',
        toes,
        f'<g fill="{theme["word"]}" transform="translate({PAD - box["left"]:.2f},{base:.2f})">'
        f'{group}</g>',
        crescent(ox - moon["dx"], base - moon["rise"], moon["r"], theme["moon"])
        if moon else ""] if part)


def main():
    os.makedirs(OUT, exist_ok=True)
    group = outlined(WORD, SIZE, TRACK)
    box = ink_box(group)
    for prefix, geom in VARIANTS.items():
        for name, theme in THEMES.items():
            w, h, body = header(theme, group, box, **geom)
            stem = f"sentinel-header-{prefix}{name}"
            path = os.path.join(OUT, f"{stem}.svg")
            open(path, "w").write(svg(w, h, body))
            subprocess.run(["rsvg-convert", "-w", str(w * 2), path, "-o",
                            os.path.join(OUT, f"{stem}-2x.png")], check=True)
            print(f"{stem}.svg  {w}x{h}  ({w / h:.2f}:1)")


if __name__ == "__main__":
    main()
