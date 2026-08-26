# -*- coding: utf-8 -*-
# Round 3: variations on Curious (B) and Perch (E) with the silver grey retired.

C_SHAPES = {
 "base": dict(
   hl="M32,7 C21,7 12.5,15 11,26 C9.5,38 17,50 32,56.5 Z",
   hr="M32,7 C43,7 51.5,15 53,26 C54.5,38 47,50 32,56.5 Z",
   tl="M20,10.5 C17,7 14.5,4.5 12,3.5 C12,8 13.5,13.5 16,17.5 C17.5,15 18.8,12.5 20,10.5 Z",
   tr="M44,10.5 C47,7 49.5,4.5 52,3.5 C52,8 50.5,13.5 48,17.5 C46.5,15 45.2,12.5 44,10.5 Z",
   ecx=22.5, ecy=31, er=8.6, ir=4.7, pr=2.3, cr=1.6,
   bl="M15.6,21.6 C18.6,18.4 23.4,17.6 27.4,19.4",
   br="M48.4,21.6 C45.4,18.4 40.6,17.6 36.6,19.4",
   beak="M32,41 C29.4,41 27.9,42.2 27.9,43.7 C27.9,46.1 30.1,48.9 32,49.8 C33.9,48.9 36.1,46.1 36.1,43.7 C36.1,42.2 34.6,41 32,41 Z"),
 "wide": dict(
   hl="M32,8 C20,8 11.5,16 10.5,27 C9.5,38.5 17,49.5 32,56 Z",
   hr="M32,8 C44,8 52.5,16 53.5,27 C54.5,38.5 47,49.5 32,56 Z",
   tl="M21,11 C18.5,8 16.5,6.5 14.5,6 C14.5,9.5 15.5,13.5 17.5,17 C18.6,14.7 19.8,12.6 21,11 Z",
   tr="M43,11 C45.5,8 47.5,6.5 49.5,6 C49.5,9.5 48.5,13.5 46.5,17 C45.4,14.7 44.2,12.6 43,11 Z",
   ecx=22, ecy=31, er=8.8, ir=4.8, pr=2.3, cr=1.7,
   bl="M14.8,21.4 C18,18.2 23.2,17.4 27.2,19.2",
   br="M49.2,21.4 C46,18.2 40.8,17.4 36.8,19.2",
   beak="M32,41 C29.4,41 27.9,42.2 27.9,43.7 C27.9,46.1 30.1,48.9 32,49.8 C33.9,48.9 36.1,46.1 36.1,43.7 C36.1,42.2 34.6,41 32,41 Z"),
 "bigeye": dict(
   hl="M32,7 C21,7 12.5,15 11,26 C9.5,38 17,50 32,56.5 Z",
   hr="M32,7 C43,7 51.5,15 53,26 C54.5,38 47,50 32,56.5 Z",
   tl="M20,10.5 C17,7 14.5,4.5 12,3.5 C12,8 13.5,13.5 16,17.5 C17.5,15 18.8,12.5 20,10.5 Z",
   tr="M44,10.5 C47,7 49.5,4.5 52,3.5 C52,8 50.5,13.5 48,17.5 C46.5,15 45.2,12.5 44,10.5 Z",
   ecx=22.3, ecy=30.5, er=9.5, ir=5.3, pr=2.6, cr=1.8,
   bl="M15.2,19.8 C18.4,16.6 23.4,15.9 27.5,17.8",
   br="M48.8,19.8 C45.6,16.6 40.6,15.9 36.5,17.8",
   beak="M32,42 C29.7,42 28.4,43.1 28.4,44.4 C28.4,46.5 30.3,49 32,49.8 C33.7,49 35.6,46.5 35.6,44.4 C35.6,43.1 34.3,42 32,42 Z"),
 "tall": dict(
   hl="M32,6 C21.5,6 13.5,14 12,25.5 C10.5,38 17.5,50.5 32,57.5 Z",
   hr="M32,6 C42.5,6 50.5,14 52,25.5 C53.5,38 46.5,50.5 32,57.5 Z",
   tl="M20,9.5 C16.5,5.5 13.5,2.5 11,1.5 C11,6.5 12.5,12.5 15.5,17 C17,14.3 18.6,11.6 20,9.5 Z",
   tr="M44,9.5 C47.5,5.5 50.5,2.5 53,1.5 C53,6.5 51.5,12.5 48.5,17 C47,14.3 45.4,11.6 44,9.5 Z",
   ecx=22.8, ecy=31.5, er=8.4, ir=4.6, pr=2.2, cr=1.6,
   bl="M16,21.8 C19,18.6 23.6,17.9 27.5,19.7",
   br="M48,21.8 C45,18.6 40.4,17.9 36.5,19.7",
   beak="M32,41.5 C29.6,41.5 28.2,42.7 28.2,44.1 C28.2,46.4 30.2,49.2 32,50 C33.8,49.2 35.8,46.4 35.8,44.1 C35.8,42.7 34.4,41.5 32,41.5 Z"),
}

P_SHAPES = {
 "base": dict(
   hl="M32,7 C19,7 10,19 10,33 C10,47 20,57 32,57 Z",
   hr="M32,7 C45,7 54,19 54,33 C54,47 44,57 32,57 Z",
   tl="M24,8.5 C21,5 18,3 16,2.5 C16,6 17,10 19,13 C20.5,11.2 22.3,9.7 24,8.5 Z",
   tr="M40,8.5 C43,5 46,3 48,2.5 C48,6 47,10 45,13 C43.5,11.2 41.7,9.7 40,8.5 Z",
   belly="M32,36 C25.5,36 21.5,42 21.5,48 C21.5,53.6 26,57 32,57 C38,57 42.5,53.6 42.5,48 C42.5,42 38.5,36 32,36 Z",
   ecx=23, ecy=23, er=7.6, ir=4.1, pr=2.0, cr=1.4,
   beak="M32,29.5 C30.1,29.5 29,30.5 29,31.7 C29,33.5 30.6,35.4 32,36.2 C33.4,35.4 35,33.5 35,31.7 C35,30.5 33.9,29.5 32,29.5 Z",
   feet=(25.5, 33.5, 54.0)),
 "round": dict(
   hl="M32,8 C18,8 9,20 9,34 C9,48 19,57.5 32,57.5 Z",
   hr="M32,8 C46,8 55,20 55,34 C55,48 45,57.5 32,57.5 Z",
   tl="M24.5,9.5 C21.5,6 18.5,4 16.5,3.5 C16.5,7 17.5,11 19.5,14 C21,12.2 22.8,10.7 24.5,9.5 Z",
   tr="M39.5,9.5 C42.5,6 45.5,4 47.5,3.5 C47.5,7 46.5,11 44.5,14 C43,12.2 41.2,10.7 39.5,9.5 Z",
   belly="M32,35 C24.5,35 20,41.5 20,48 C20,54 25,57.5 32,57.5 C39,57.5 44,54 44,48 C44,41.5 39.5,35 32,35 Z",
   ecx=22.5, ecy=24, er=8.0, ir=4.3, pr=2.1, cr=1.5,
   beak="M32,30.5 C30,30.5 28.8,31.6 28.8,32.9 C28.8,34.8 30.5,36.8 32,37.6 C33.5,36.8 35.2,34.8 35.2,32.9 C35.2,31.6 34,30.5 32,30.5 Z",
   feet=(25.0, 34.0, 54.5)),
 "egg": dict(
   hl="M32,5 C19.5,5 10.5,18 10.5,33.5 C10.5,48 20.5,57.5 32,57.5 Z",
   hr="M32,5 C44.5,5 53.5,18 53.5,33.5 C53.5,48 43.5,57.5 32,57.5 Z",
   tl="M24,6.5 C21,3 18,1.5 16,1 C16,4.5 17,8.5 19,11.5 C20.5,9.7 22.3,8 24,6.5 Z",
   tr="M40,6.5 C43,3 46,1.5 48,1 C48,4.5 47,8.5 45,11.5 C43.5,9.7 41.7,8 40,6.5 Z",
   belly="M32,36 C26.5,36 23,42 23,48 C23,53.8 27,57.5 32,57.5 C37,57.5 41,53.8 41,48 C41,42 37.5,36 32,36 Z",
   ecx=23.2, ecy=22, er=7.4, ir=4.0, pr=1.9, cr=1.4,
   beak="M32,28.5 C30.2,28.5 29.2,29.5 29.2,30.6 C29.2,32.3 30.7,34.2 32,35 C33.3,34.2 34.8,32.3 34.8,30.6 C34.8,29.5 33.8,28.5 32,28.5 Z",
   feet=(25.8, 33.2, 55.0)),
}

NAVY, STEEL = "#1b2741", "#2f485e"

CURIOUS = [
 ("c-frost", "Frost", "base",
  dict(disc="#eef4f9", brow="#7ba7c8", beak="#c98a3c", cat="#ffffff"),
  "Blue-50 eyes and a blue-300 brow. The coolest of the four, and the smallest step from where we were."),
 ("c-cream", "Cream", "wide",
  dict(disc="#e6ecf2", brow="#ddb377", beak="#c98a3c", cat="#ffffff"),
  "Off-white eyes with an amber-300 brow. The amber reads stronger without a warm disc competing with it."),
 ("c-teal", "Teal", "bigeye",
  dict(disc="#d3e9e9", brow="#74b8b8", beak="#3a8f8f", cat="#ffffff"),
  "Teal as the second hue, eyes enlarged. Amber stays on the irises, so the mark carries two accents pulling opposite ways."),
 ("c-sky", "Sky", "tall",
  dict(disc="#d6e4f0", brow="#4aa3c7", beak="#4aa3c7", cat="#ffffff"),
  "A taller head with longer tufts, and sky blue doing the brow and beak. The most awake of the set."),
]

PERCH = [
 ("p-cream", "Cream", "base",
  dict(belly="#e6ecf2", disc="#f4f7fa", beak="#ddb377", feet="#c98a3c", cat="#ffffff"),
  "An off-white chest against the navy body. The amber irises and feet are the only warm notes left, which is what makes them read."),
 ("p-sky", "Sky", "round",
  dict(belly="#d6e4f0", disc="#eef4f9", beak="#4aa3c7", feet="#c98a3c", cat="#ffffff"),
  "A rounder body with a pale blue chest. Stays entirely inside the existing blue ramp, no new hue introduced."),
 ("p-teal", "Teal", "egg",
  dict(belly="#d3e9e9", disc="#eef6f6", beak="#3a8f8f", feet="#c98a3c", cat="#ffffff"),
  "Taller egg body, narrower chest, teal throughout. The most distinct from the product palette."),
]

def curious_sym(sid, shape, c):
    s = C_SHAPES[shape]
    lx, rx = s["ecx"], 64 - s["ecx"]
    cy = s["ecy"]
    e = []
    for cx in (lx, rx):
        e.append(f'<circle fill="{c["disc"]}" cx="{cx}" cy="{cy}" r="{s["er"]}"/>')
    for cx in (lx, rx):
        e.append(f'<circle fill="#c98a3c" cx="{cx}" cy="{cy}" r="{s["ir"]}"/>')
        e.append(f'<circle fill="{NAVY}" cx="{cx}" cy="{cy}" r="{s["pr"]}"/>')
    for cx in (lx, rx):
        e.append(f'<circle fill="{c["cat"]}" cx="{cx-2.2}" cy="{cy-2.3}" r="{s["cr"]}"/>')
    bw = 2.6
    return f'''<symbol id="{sid}" viewBox="0 0 64 64">
  <path fill="{NAVY}" d="{s["tl"]}"/>
  <path fill="{STEEL}" d="{s["tr"]}"/>
  <path fill="{NAVY}" d="{s["hl"]}"/>
  <path fill="{STEEL}" d="{s["hr"]}"/>
  {"".join(e)}
  <path stroke="{c["brow"]}" stroke-width="{bw}" stroke-linecap="round" fill="none" d="{s["bl"]}"/>
  <path stroke="{c["brow"]}" stroke-width="{bw}" stroke-linecap="round" fill="none" d="{s["br"]}"/>
  <path fill="{c["beak"]}" d="{s["beak"]}"/>
</symbol>'''

def perch_sym(sid, shape, c, band=None):
    s = P_SHAPES[shape]
    lx, rx = s["ecx"], 64 - s["ecx"]
    cy = s["ecy"]
    fl, fr, fy = s["feet"]
    e = []
    for cx in (lx, rx):
        e.append(f'<circle fill="{c["disc"]}" cx="{cx}" cy="{cy}" r="{s["er"]}"/>')
    for cx in (lx, rx):
        e.append(f'<circle fill="#c98a3c" cx="{cx}" cy="{cy}" r="{s["ir"]}"/>')
        e.append(f'<circle fill="{NAVY}" cx="{cx}" cy="{cy}" r="{s["pr"]}"/>')
    for cx in (lx, rx):
        e.append(f'<circle fill="{c["cat"]}" cx="{cx-1.9}" cy="{cy-2.0}" r="{s["cr"]}"/>')
    if band:
        chest = (f'<clipPath id="{sid}-clip"><path d="{s["hl"]} {s["hr"]}"/></clipPath>'
                 f'<g clip-path="url(#{sid}-clip)">'
                 f'<path fill="{band}" d="M6,41 C17,34 47,34 58,41 L58,62 L6,62 Z"/></g>')
    else:
        chest = f'<path fill="{c["belly"]}" d="{s["belly"]}"/>'
    return f'''<symbol id="{sid}" viewBox="0 0 64 64">
  <path fill="{NAVY}" d="{s["tl"]}"/>
  <path fill="{STEEL}" d="{s["tr"]}"/>
  <rect fill="{c["feet"]}" x="{fl}" y="{fy}" width="5" height="4.5" rx="2"/>
  <rect fill="{c["feet"]}" x="{fr}" y="{fy}" width="5" height="4.5" rx="2"/>
  <path fill="{NAVY}" d="{s["hl"]}"/>
  <path fill="{STEEL}" d="{s["hr"]}"/>
  {chest}
  {"".join(e)}
  <path fill="{c["beak"]}" d="{s["beak"]}"/>
</symbol>'''

SYMS, CARDS = [], []
TILE = {"c-frost":"#eef4f9","c-cream":"#eef4f9","c-teal":"#eef6f6","c-sky":"#eef4f9",
        "p-cream":"#c98a3c","p-sky":"#eef4f9","p-teal":"#eef6f6","p-shaded":"#eef4f9"}

for sid, name, shape, col, blurb in CURIOUS:
    SYMS.append(curious_sym(sid, shape, col))
    CARDS.append(("Curious", sid, name, blurb))

for sid, name, shape, col, blurb in PERCH:
    SYMS.append(perch_sym(sid, shape, col))
    CARDS.append(("Perch", sid, name, blurb))

# fourth Perch: no chest patch at all, a shaded lower body instead
SYMS.append(perch_sym("p-shaded", "base",
    dict(belly="", disc="#eef4f9", beak="#ddb377", feet="#c98a3c", cat="#ffffff"),
    band="#4d82a8"))
CARDS.append(("Perch", "p-shaded", "Shaded",
    "No chest patch at all. The lower half of the body simply lightens to blue-400, which reads as a bird catching light rather than as a panel."))

src = open("marks-preview.local.html").read()
fonts = src[src.index("@font-face"):src.rindex('format("woff2")}') + len('format("woff2")}')]

def card(fam, sid, name, blurb):
    tile = TILE[sid]
    sizes = lambda: "".join(
        f'<svg class="mark" width="{n}" height="{n}"><use href="#{sid}"/></svg>' for n in (38, 24, 16))
    tiles = "".join(
        f'<span class="tile" style="background:{tile};width:{n+10}px;height:{n+10}px;border-radius:{max(4,n//4)}px">'
        f'<svg class="mark" width="{n}" height="{n}"><use href="#{sid}"/></svg></span>' for n in (38, 24, 16))
    return f'''<article class="card">
  <div class="card-top">
    <div class="hero"><svg class="mark" width="118" height="118"><use href="#{sid}"/></svg></div>
    <div class="meta">
      <span class="idx">{fam}</span>
      <h2>{name}</h2>
      <p>{blurb}</p>
    </div>
  </div>
  <div class="gauntlet">
    <div class="bay on-light"><div class="sizes">{sizes()}</div><span class="bay-label">Light</span></div>
    <div class="bay on-dark"><div class="sizes">{tiles}</div><span class="bay-label">Avatar tile</span></div>
  </div>
</article>'''

cur = "".join(card(*c) for c in CARDS if c[0] == "Curious")
per = "".join(card(*c) for c in CARDS if c[0] == "Perch")

html = f'''<title>Sentinel — color variations</title>
<style>
{fonts}
:root{{--navy:#1b2741;--canvas:#f3f5f8;--surface:#fff;--n200:#d3dae3;--ink:#222a34;
  --ink-muted:#4e5762;--ink-subtle:#646e7c;
  --shadow-sm:0 1px 2px rgba(27,39,65,.06),0 2px 8px rgba(27,39,65,.08)}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--canvas);color:var(--ink);font-family:"Touche",-apple-system,sans-serif;
  font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}}
.wrap{{max-width:1060px;margin:0 auto;padding:52px 24px 90px}}
.eyebrow{{font-size:11px;font-weight:600;letter-spacing:.14em;text-transform:uppercase;
  color:var(--ink-subtle);margin:0 0 8px}}
h1{{font-size:40px;line-height:1.05;letter-spacing:-.024em;font-weight:600;color:var(--navy);margin:0 0 12px}}
.lede{{font-size:17px;color:var(--ink-muted);max-width:60ch;margin:0 0 8px}}
h2.fam{{font-size:26px;letter-spacing:-.018em;color:var(--navy);font-weight:600;margin:44px 0 4px}}
.famnote{{margin:0 0 18px;color:var(--ink-muted);font-size:15px;max-width:62ch}}
.grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:22px}}
.card{{background:var(--surface);border-radius:6px;box-shadow:var(--shadow-sm);overflow:hidden}}
.card-top{{display:flex;gap:20px;padding:24px 24px 18px;align-items:flex-start}}
.hero{{flex:0 0 auto;width:142px;height:142px;border-radius:6px;background:var(--canvas);
  display:grid;place-items:center}}
.meta{{flex:1 1 auto;min-width:0}}
.idx{{font-size:11px;font-weight:600;letter-spacing:.14em;color:var(--ink-subtle);
  text-transform:uppercase;display:block;margin-bottom:2px}}
.card h2{{margin:0 0 8px;font-size:23px;letter-spacing:-.015em;font-weight:600;color:var(--navy)}}
.card p{{margin:0;font-size:14px;color:var(--ink-muted)}}
.gauntlet{{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--n200);
  border-top:1px solid var(--n200)}}
.bay{{padding:15px 14px 13px;display:flex;flex-direction:column;align-items:center;gap:11px}}
.bay.on-light{{background:var(--surface)}}
.bay.on-dark{{background:var(--navy)}}
.sizes{{display:flex;align-items:center;gap:12px;height:50px}}
.tile{{display:grid;place-items:center;flex:0 0 auto}}
.bay-label{{font-size:10px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;
  color:var(--ink-subtle)}}
.bay.on-dark .bay-label{{color:#aecae0}}
.mark{{display:block;flex:0 0 auto}}
.note{{margin-top:44px;background:var(--surface);border-radius:6px;box-shadow:var(--shadow-sm);padding:22px 26px}}
.note h3{{margin:0 0 6px;font-size:11px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;color:#2f6f9c}}
.note p{{margin:0;font-size:14px;color:var(--ink-muted);max-width:72ch}}
@media (max-width:820px){{.grid{{grid-template-columns:1fr}}h1{{font-size:32px}}}}
</style>
<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>
{"".join(SYMS)}
</defs></svg>
<div class="wrap">
  <p class="eyebrow">Sentinel, round three</p>
  <h1>Curious and Perch, without the gray</h1>
  <p class="lede">Silver is gone from both. Every pale area now carries a hue: blue, cream, teal, or sky. Navy and steel still build the bird, and amber still holds the irises, so the family rule survives intact.</p>

  <h2 class="fam">Curious</h2>
  <p class="famnote">Four takes on the head. Each pairs a different pale tone in the eyes with a matching brow, and adjusts the head shape a little to suit it.</p>
  <div class="grid">{cur}</div>

  <h2 class="fam">Perch</h2>
  <p class="famnote">Four takes on the whole bird. The first three swap the chest color and reshape the body; the fourth drops the chest patch entirely.</p>
  <div class="grid">{per}</div>

  <div class="note">
    <h3>On the dark side</h3>
    <p>These carry real hues now, so the old trick of flipping every tone for a dark background stops working. The right answer on navy is a pale tile, shown in the right-hand bay above, which is also how Teams and GitHub render an avatar anyway.</p>
  </div>
</div>'''

open("sentinel-color.local.html", "w").write(html)
print("wrote sentinel-color.local.html", len(html))
