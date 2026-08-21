# Header lockup

The README banner: the wordmark, a telephone wire, the `perch-cream` owl perched
at the point where the wire bottoms out under its weight, and a crescent moon off
its shoulder. Same palette as the marks in [../assets](../assets/README.md), on a
transparent ground.

Two aspects, same geometry. **Wide** is the shipped banner and wants to run the
full column width, so the wire reaches both edges rather than stopping halfway
across. **Compact** is the original narrower crop, for anywhere the lockup has
to sit at a fixed size.

| File | Aspect | Use |
|---|---|---|
| `sentinel-header-light.svg` | 1221x208, 5.9:1 | The shipped banner. Navy wordmark and wire, amber moon, for light backgrounds. |
| `sentinel-header-dark.svg` | 1221x208, 5.9:1 | Cream wordmark and moon, steel wire, for dark backgrounds. |
| `sentinel-header-compact-{light,dark}.svg` | 751x198, 3.8:1 | The narrow crop, for fixed-size placements. No moon: see below. |
| `*-2x.png` | | 2x rasters of all four, for anywhere SVG is awkward. |

The wordmark is Gotham Rounded Bold **converted to outlines**. GitHub has no
Gotham Rounded, so live `<text>` would fall back to whatever the reader happens
to have installed. Nothing in the committed SVGs references a font.

## The two side by side

Both render below exactly as they would at the top of a README, so this page is
the comparison. Wide first, at `width="100%"`:

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="sentinel-header-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="sentinel-header-light.svg">
  <img alt="Sentinel, wide" src="sentinel-header-light.svg" width="100%">
</picture>

Then compact, at `width="470"`:

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="sentinel-header-compact-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="sentinel-header-compact-light.svg">
  <img alt="Sentinel, compact" src="sentinel-header-compact-light.svg" width="470">
</picture>

The trade is mobile. GitHub caps images at container width, so on a narrow
screen both land at the same rendered width, and the wider source is therefore
the smaller lockup: roughly a 25px cap height against the compact crop's 40px.

## Wiring one into a README

GitHub picks between light and dark with `<picture>` and `prefers-color-scheme`.
The older `#gh-dark-mode-only` URL-fragment trick is deprecated, so don't use it.

```html
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/brand/header/sentinel-header-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="docs/brand/header/sentinel-header-light.svg">
  <img alt="Sentinel" src="docs/brand/header/sentinel-header-light.svg" width="100%">
</picture>
```

## Regenerating

`../build_header.py` rebuilds every file here. It needs `hb-view` (harfbuzz) and
`rsvg-convert` on PATH, plus Gotham Rounded Bold installed locally, so it only
runs on a machine with the licensed font. The committed SVGs need none of that.

```bash
cd docs/brand && python3 build_header.py
```

To change the name, edit `WORD` at the top of the script and re-run. To retune
either aspect, edit `VARIANTS`, which maps each one to the gap between wordmark
and owl, the depth of the sag, and where the moon sits.

## Two things that are less obvious than they look

**The compact aspect has no moon.** There is nowhere to put it. Anywhere it
fits, it lands between the final `l` and the owl and reads as punctuation rather
than sky, so that aspect goes without and its `moon` entry in `VARIANTS` is
`None`.

**The crescent is a two-arc path, not two circles.** The obvious approach, two
circle subpaths with `fill-rule="evenodd"`, only holds while the bite disc stays
entirely inside the rim. Any crescent worth the name needs it further out than
that, and once it overhangs, the overhanging part fills too and you get two
overlapping discs. `crescent()` draws the major arc of the rim and the minor arc
of the bite, meeting at the horn tips. Horns sit at `x = kr/2` and
`y = ±r·sqrt(1 − k²/4)`, so anything below about `k = 1` wraps the rim right
round and reads as a ring.
