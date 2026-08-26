# Header lockup

The README banner: the wordmark, a telephone wire, the `perch` owl perched
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
| `sentinel-header-dark.svg` | 1221x208, 5.9:1 | Off-white wordmark and moon, steel wire, for dark backgrounds. |
| `sentinel-header-compact-{light,dark}.svg` | 751x198, 3.8:1 | The narrow crop, for fixed-size placements. No moon: see below. |
| `*-2x.png` | | 2x rasters of all four, for anywhere SVG is awkward. |
| `sentinel-social-card.png` | 1280x640, 2:1 | The repo's social preview. Not a lockup: see below. |

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

## The social preview card

`sentinel-social-card.png` is what GitHub unfurls when the repo is linked, set
under Settings, General, Social preview. It is the only file here that is not a
lockup, and it breaks both of this directory's conventions on purpose.

**It has an opaque ground.** Everything else here is transparent, which is right
for a README that already has a background. A social card does not: it is
unfurled by Slack, X, LinkedIn and Discord, each compositing transparency
against its own backdrop, so a transparent card's wordmark would disappear on
whichever clients happen to disagree with it. The card carries an opaque white
ground and the light lockup.

The ground has to be white rather than brand navy, and that is not a taste
call. The owl's near half is filled `#1b2741`, brand navy exactly, so on a navy
card that half and its ear tuft are not low-contrast but literally the same
pixels as the background: the bird renders as a lopsided offcut with the eyes
and chest floating free. The dark lockup only recolors the wordmark, wire and
moon, so there is no dark-ground owl to reach for. GitHub's own dark mode gets
away with the same lockup because `#0d1117` is darker than the owl.

**It is 2:1.** GitHub wants 1280x640 and letterboxes anything else against a
background it picks, not one we control. No lockup aspect here is close: wide is
5.9:1 and compact is 3.8:1. Wide is the one composited, at full bleed, so the
wire runs edge to edge rather than stopping inside a padded box, and the moon
survives, which the compact crop has nowhere to put.

Rebuild it with `../build_social_card.py`, which composites the wide light lockup
onto the ground and letterboxes it. It needs no font, because the wordmark is
already outlined:

```bash
cd docs/brand && python3 build_social_card.py
```

It used to be a hand-run `magick` incantation, which is how it came to be left
carrying the old palette after every other asset had moved.

GitHub caps the upload at 1MB. This lands around 120KB. Changing the file does
not change what GitHub serves: re-upload it under Settings, General, Social
preview.

## Regenerating

`../build_header.py` rebuilds every lockup here; `../build_social_card.py` rebuilds
the social card. The lockup builder needs `hb-view` (harfbuzz) and
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
