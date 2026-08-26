# Sentinel mark assets

Four candidate marks, each as a complete asset set. The README banner that pairs
`perch-cream` with the wordmark lives in [../header](../header/README.md). All four
are built from the Sentasity palette: navy `#1b2741` and steel `#2f485e` build the
bird, amber `#c98a3c` holds the irises, and a single pale hue carries the eyes or
chest. No silver anywhere.

| Mark | Pale hue | Notes |
|---|---|---|
| `curious-cream` | amber-100 `#f5e7d0` | Head only. Warm eyes and an amber-300 brow. |
| `curious-frost` | blue-50 `#eef4f9` | Head only. Cool eyes and a blue-300 brow. |
| `perch-cream` | amber-100 `#f5e7d0` | Whole bird with a warm chest and amber feet. |
| `perch-shaded` | blue-400 `#4d82a8` | Whole bird, no chest patch; the lower body lightens instead. |

## What is in each folder

| File | Use |
|---|---|
| `<name>.svg` | The mark on a transparent ground. The source of truth; scale this, do not upscale a PNG. |
| `<name>-tile.svg` | The mark inset on a square white tile. Use wherever the background is dark or unknown. |
| `<name>-outline.svg` | White silhouette with the eyes knocked out. Monochrome contexts only. |
| `<name>-{16..1024}.png` | Transparent raster at 16, 32, 48, 64, 128, 256, 512, 1024. |
| `<name>-tile-192.png` | Opaque tile at 192px. This is the size Teams wants for a color icon. |
| `<name>-tile-512.png` | Opaque tile at 512px, for GitHub org avatars and anywhere else an avatar is wanted. |
| `<name>-outline-32.png` | White silhouette at 32px, transparent. This is the size Teams wants for an outline icon. |
| `<name>-sentry.svg` | Black silhouette with the face knocked out, inset to clear a circular crop. |
| `<name>-sentry-256.png` | Black silhouette at 256px, transparent. This is what Sentry's small icon upload accepts. |
| `<name>-favicon.ico` | Multi-resolution icon bundling 16, 32, and 48. |

## Why the tiles inset the mark

A tile that bleeds the mark to its edges is mostly bird, and the bird is mostly
navy, so at the size an avatar is actually rendered the tile reads as a navy
square with two eyes and a chest floating on it. The owl stops being an owl.
Insetting it to 58% of the frame puts white back around the silhouette, which is
the read the README header gets for free from the page behind it.

That number is also what Microsoft asks for. It masks the color icon's corners
itself and wants the brand mark inside a 120x120 safe area of the 192x192
canvas, balanced around 96x96; the tallest of the four marks lands 98px tall.
The tiles are square for the same reason: a pre-rounded upload is something the
Teams icon guidance calls out by name, because the client rounds it again.

## Wiring one into the Teams app

`teams-app/` needs exactly two files, a 192px color icon and a 32px outline icon,
and `build-package.sh` refuses to build without them. To adopt a mark:

```bash
cp docs/brand/assets/<name>/<name>-tile-192.png   teams-app/color.png
cp docs/brand/assets/<name>/<name>-outline-32.png teams-app/outline.png
```

The manifest's `accentColor` is currently `#1F2937`, which is close to but not the
same as brand navy. Worth setting to `#1b2741` at the same time.

## Wiring one into the Sentry integration

Sentry → Settings → Developer Settings → the `sentinel` integration → Small Icon →
Upload an image, then Save Avatar. Sentry recolors the icon and crops it to a
circle, so the upload has to be square, between 256px and 1024px, and made of
black and transparent pixels only.

```
docs/brand/assets/perch-cream/perch-cream-sentry-256.png
```

The `-sentry` files differ from `-outline` in three ways that matter here: black
instead of white, the eyes knocked out as full discs with the pupils left behind
as islands so the face still reads, and the whole mark inset to 88% so the ear
tufts and feet clear the circle. Sentry renders this icon small inside UI
components, which is what the larger eyes and the knocked-out beak are for.

## Regenerating

`docs/brand/build_assets.py` rebuilds every file here from the shape and color
definitions in `docs/brand/gen_round3.py`. It needs `rsvg-convert` and `magick`
on PATH.

```bash
cd docs/brand && python3 build_assets.py
```
