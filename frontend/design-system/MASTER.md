# Trace - design system

Supersedes the dark purple/pink direction entirely. Do not patch the old file; this replaces it.

Reference implementations live in `NeuLitTrace Redesign.dc.html` (new direction, all eight
sections + states gallery + this system rendered) and `NeuLitTrace Current UI.dc.html`
(faithful recreation of the build being replaced, for before/after).

---

## 1. Direction

White field. One blue. Black serif display. Hairline borders, generous whitespace,
restrained shadow. Blue is an accent and a shape colour, never ambient glow.

Removed from the old direction and not to be reintroduced: purple→pink gradients,
glassmorphic panels, 20 px backdrop blur, and the pink `--rare` / blue `--common`
semantic pair.

**Glow is kept**, rebuilt for a white field (revision 2). On black, glow was ambient haze.
On white it is *emission*: light coming off the blue, never a coloured background. Rules:

- Glow is always `--blue-500` at low alpha over white - never a second hue, never a
  gradient between two hues, never behind text.
- Three carriers only: (1) the hero blooms, (2) canvas `shadowBlur` on neuron strokes and
  atlas hotspots, (3) a blue-tinted drop shadow on the two hero-adjacent panels
  (`0 18px 50px oklch(0.6781 0.1215 258.28 / 0.12–0.14)`) and the primary button.
- Hero blooms: three radial-gradient discs, 0.15–0.52 alpha at centre to transparent at
  ~72%, `blur(18–40px)`, drifting on 17–26 s ease-in-out loops. Two hairline
  `--blue-200/300` circles sit over them so the glow has an edge to read against.
- Body text, panels, tables, and chips stay on flat white. Glow never touches a reading
  surface.

---

## 2. Colour tokens

Base hue is `258.28`, twelve degrees off the sampled `#529EDE` (hue `246.28`). The shift is
deliberate: close enough to read as the same family, far enough that it is ours and not a
sponsor's. If the project is scrapped after the weekend, revert to `246.28`; if it lives,
keep `258.28`.

```css
--blue-50   oklch(0.9800 0.0110 258.28)   /* page tint, hover fills            */
--blue-100  oklch(0.9560 0.0230 258.28)   /* selected rows, soft chips         */
--blue-200  oklch(0.9080 0.0470 258.28)   /* borders on tinted surfaces        */
--blue-300  oklch(0.8420 0.0760 258.28)   /* dividers, inactive states         */
--blue-400  oklch(0.7550 0.1050 258.28)   /* chart secondary series            */
--blue-500  oklch(0.6781 0.1215 258.28)   /* brand blue. FILLS AND SHAPES ONLY */
--blue-600  oklch(0.5900 0.1230 258.28)   /* chart primary, button fill floor  */
--blue-700  oklch(0.5050 0.1180 258.28)   /* blue TEXT, links, icons, buttons  */
--blue-800  oklch(0.4200 0.1080 258.28)   /* emphasis text, active nav         */
--blue-900  oklch(0.3450 0.0940 258.28)   /* headings on blue tint             */

--white     #FFFFFF                        /* page background                  */
--ink       oklch(0.2261 0.0210 264.02)    /* primary text, 17:1               */
--muted     oklch(0.4500 0.0180 264)       /* secondary body copy, ~8.5:1      */
--dim       oklch(0.5500 0.0200 264)       /* metadata, labels, ~6.2:1         */
--rule      oklch(0.8217 0.0244 272.76)    /* hairline borders                 */

--warn      #B45309                        /* unsupported citation, 5.9:1      */
--warn-bg   #FEF6EC
```

No third accent hue. Amber is the only non-blue semantic, and it means exactly one thing:
*this claim could not be verified against its source.*

### 2.1 The contrast rule - state this in code review

| Token | vs white | Verdict |
|---|---|---|
| `--blue-400` | 2.17:1 | decoration only |
| `--blue-500` | **2.88:1** | **fills, shapes, illustration, the neuron canvas. Never text. Nothing white on top of it.** |
| `--blue-600` | 4.06:1 | large text 24 px+; absolute floor for a filled button with a white label |
| `--blue-700` | 5.84:1 | blue text, links, icon glyphs - the working minimum |
| `--blue-800` | 8.46:1 | emphasis text, active nav |

White text on `--blue-500` is also 2.88:1 and fails. A brand-blue button with a white label
is the single most common way this palette gets ruined. Primary buttons in this system fill
with `--blue-700`.

Long-form reading text is `--ink` on white. Never set body copy in blue.

---

## 3. Type

Newsreader survives the move to white. On a white field the serif reads journal-adjacent
rather than decorative, and it separates the product from the geometric-sans reference
without weakening the display voice.

**Space Grotesk is dropped (revision 3).** Its geometric skeleton read mechanical next to
Newsreader - closed apertures, uniform stroke, single-storey feel at small sizes. The UI and
body face is now **Source Sans 3**: humanist, open apertures, a true italic, drawn for long
reading and small UI text alike, and already the house sans of a good deal of scholarly
publishing. It sits under Newsreader without arguing with it.

**Martian Mono is dropped** for the same reason - very wide, very engineered. Data is now
**IBM Plex Mono**, which keeps the "this is a record, not prose" signal but has humanist
letterforms and a far better small-size colour. Sora is dropped entirely.

| Role | Family | Size / weight | Notes |
|---|---|---|---|
| Display | Newsreader 500 | 92 px / 1.02 / -0.025em | hero only |
| Section | Newsreader 500 | 44 px / 1.12 / -0.02em | one per section |
| Panel head | Newsreader 500 | 27 px / 1.35 | analyst answers, pull quotes |
| Lede | Source Sans 3 400 | 18–19 px / 1.65 | capped at 560 px |
| Body | Source Sans 3 400 | 13–15 px / 1.7 | |
| UI label | Source Sans 3 500/600 | 12–14 px / 1 | buttons, tabs |
| Eyebrow | Source Sans 3 600 | 11 px / 0.16em / uppercase | inside the section rail, below |
| Data | IBM Plex Mono 400/500 | 10.5–12 px | PMIDs, prevalence, latency, cost, SQL |
| Metric | IBM Plex Mono 500 | 24–120 px / -0.02 to -0.04em | dashboard headline |

Metadata reads as data: every PMID, prevalence figure, latency, dollar amount, call-site
name, and condition chip is monospace. Prose is never monospace.

---

## 4. Surface

- **Border:** 1 px `--rule`. This is the primary means of separation. Panels are outlined,
  not floated.
- **Radius:** 4 px on panels, 3 px on controls, 2 px on chips and pills. Nothing is round
  except status dots and the toggle knob.
- **Shadow:** at most `0 1px 2px oklch(0.2261 0.021 264.02 / 0.04)` on the query composer.
  Nowhere else. No elevation system.
- **Tint:** `--blue-50` marks a section as secondary (atlas, memory, states) or a cell as
  the answer (the latency stat, warm memory rows). Two background colours total: white and
  `--blue-50`.
- **Spacing:** section padding 110 px vertical, 64 px horizontal. Panel padding 26–40 px.
  Grid gap 20–24 px. Content max width 1312 px.

---

## 4b. Layout motifs (revision 3)

The layout deliberately departs from the shape of the build it replaces. Three motifs carry
the "published literature" reading:

**Masthead.** The page opens on a 4 px ink bar and a hairline-ruled header, the wordmark
carrying an edition line (`Corpus ed. 2026.08 · 329 papers`) in mono beneath it. Rules are
ink, not `--rule` - the masthead and the hero's internal divisions are the only places
full-strength ink hairlines are used.

**Hero: the specimen field.** The neuron stays free-floating and full-bleed - no frame, no
panel - but the gesture is inverted from the build it replaces. There, the soma sat right of
centre and burst radially outward as a background wash. Here the soma sits *below the bottom
edge* at `rootX 0.70, rootY 1.04`, and eight primaries fan **upward** across a −2.62…−0.58
rad arc, so the structure grows into the page from beneath the fold like a specimen rising
through a field. Base length 196 px, depth 5, alpha cap 0.78.

Legibility is handled by two crossed white veils rather than by dimming the graphic:
a vertical one (white → 0.92 at 30% → 0.42 at 58% → clear at 84%) and a horizontal one
(white → 0.94 at 30% → 0.55 at 46% → clear at 66%). The text column therefore sits on near
solid white while the graph stays at full strength in the lower-right quadrant.

**Hero composition.** Display headline hangs at the top with the section rail; the lede,
the ruled "Begin a query ↓" link, and a right-aligned corpus rail (three figures on a
hairline) sit on one baseline far below it, with the graph filling the gap between. Four
`--blue-300` corner registration ticks sit on the section edges - the figure-plate cue
without a container - and a mono strip runs along the bottom: `Live render · depth 5 ·
8 primary · 0.72 decay · illustrative, not patient data`.

**Section rail.** Every section opens with `§NN — hairline — EYEBROW`: a mono section number
in `--blue-500`, a 28 px `--blue-300` rule, then the uppercase eyebrow. The old build's
bare eyebrow is gone; the numbering gives the page an apparatus a reader can cite back to.

**Marginal citations.** The summary is set as an annotated edition, not a paragraph with
superscripts: one grid row per sentence, citation marker right-aligned in a 56 px margin
column, sentence in the measure beside it, hairline between rows. An unsupported sentence
takes a 2 px amber left rule, an 18 px inset, a warm-white row fill, and its objection
written directly underneath - the flag is structural, not a footnote.

**Prevalence ladder.** The old rare/common chip rows are replaced by a single ranked table
of the whole corpus, rarest first, with a bar whose length is the retrieval boost and a
labelled **rarity floor** rule at the point where boosting stops. Order and fill do the work
the labels used to: rare rows are tinted, bold, blue-dotted, long-barred; common rows are
white, regular, hollow-dotted, short-barred. Rare-vs-common is legible from across the room.

---

## 5. Rare vs common

Legible without reading the labels:

- **Rare** - filled chip: `--blue-100` background, `--blue-200` border, `--blue-800` text,
  solid `--blue-500` dot, prevalence figure appended in mono.
- **Common** - outline chip: white background, `--rule` border, `--dim` text, hollow dot.

Same rule in result rows: rare-condition rows get a `--blue-50` row fill and a
`--blue-800` rank number; common rows stay white with a `--dim` rank.

---

## 6. Citation states

Three visually distinct states. Unsupported is never hidden.

| State | Marker | Source entry |
|---|---|---|
| Verified | `--blue-100` fill, `--blue-200` border, `--blue-800` mono text | normal |
| Unsupported | `--warn-bg` fill, `--warn` border and text | plus a sentence naming what the abstract actually says |
| Pending | white fill, `--rule` border, `--dim` text, 1.2 s opacity pulse | greyed |

---

## 7. Neuron canvas

Structure and motion are unchanged from `neuron-canvas.tsx`: recursive quadratic branches,
per-frame sinusoidal sway, 2–3 children per node, 0.72 length decay, terminal dots,
drifting particles, its own `requestAnimationFrame` loop. Not ported to anime.js.

Changed values only:

```
stroke gradient   rgba(78,138,226,α) → rgba(150,190,250,α*0.62)
stroke alpha      α = min(0.95, 0.44 + depth*0.10 + wave*0.18)   (was 0.16 + depth*0.09)
signal wave       wave = 0.5 + 0.5*sin(t*1.15 - depth*0.9 + seed*0.4)
                  a brightness band travelling outward through the tree — new
line width        max(0.7, depth * 0.82), round caps   (was max(0.6, depth*0.85))
stroke glow       shadowColor rgba(94,151,232, 0.30–0.64)
                  shadowBlur 5 + depth*2.6 + wave*5   — the emission, new
terminal dot      rgba(46,106,190, 0.70–1.0), r 1.9–3.2, shadowBlur 10–22,
                  firing on its own 1.8 Hz phase   (was rgba(255,220,240,0.5), r 1.4)
soma             r 5.5–6.7 solid + radial halo r 52–68 at 0.44–0.60, shadowBlur 18–26
ambient bloom    radial fill over the whole canvas, r = len*(3.0–3.35),
                  0.16 → 0.06 → 0, breathing at 0.42 rad/s
particles        rgba(78,138,226, 0.26–0.48), r ×1.7, shadowBlur 6–12, count 54
roots            10 branches (was 6), depth 6 (was 5)
root position    rootX 0.78, rootY 0.46, base length 176 (was 0.68 / 0.42 / 95)
```

It is now a confident element, not a wash: the structure bleeds off the right edge behind a
`--blue-500` circle and a `--blue-50` circle, both cropped by the viewport.

Under `prefers-reduced-motion: reduce` the loop renders one frame at `t = 0` and stops.

---

## 8. Motion - anime.js v4

`import { animate, stagger, createTimeline } from 'animejs'`. `animejs` is a new dependency
in `frontend/package.json` - note it in `Handoff-Log.md`.

| Interaction | Property | From → to | Duration | Easing | Stagger |
|---|---|---|---|---|---|
| Section entrance | translateY, opacity | 28 → 0 px, 0 → 1 | 900 ms | `cubicBezier(.16,1,.3,1)` | once, on intersection (threshold 0.08) |
| Retrieval trace steps | translateY, opacity | 14 → 0 px, 0 → 1 | 420 ms | `cubicBezier(.16,1,.3,1)` | `stagger(150)` |
| Citation resolves | scale | 1 → 1.28 → 1 | 420 ms | `cubicBezier(.16,1,.3,1)` | `stagger(90)` |
| Dashboard headline | counted value | 0 → 0.0041 | 1100 ms | `cubicBezier(.16,1,.3,1)` | - |
| Stage bars | width | 0% → target | 760 ms | `cubicBezier(.16,1,.3,1)` | `stagger(70)` |
| Chips, toggles, tabs | transform, background | knob 0 → 14 px | 160 ms | `cubicBezier(.16,1,.3,1)` | - |
| Hero copy entrance | translateY, opacity | 22 → 0 px, 0 → 1 | 800–900 ms | `cubicBezier(.16,1,.3,1)` | 0 / 80 / 180 / 300 / 380 / 460 ms |
| Primary button hover | transform, box-shadow | y 0 → -1 px, glow 0.34 → 0.50 | 180 ms | `cubicBezier(.16,1,.3,1)` | - |
| Hero blooms (CSS) | transform, opacity | scale 1 → 1.08, drift ±4% | 17–26 s loop | `ease-in-out` | 0 / 3 s offsets |
| Atlas hotspot rings (canvas) | radius, alpha | r 10 → 54 px, 0.42 → 0 | 1.8 s loop | linear | 0.33 phase offset each |

Constraints:

- `transform` and `opacity` only. The two width animations are on decorative bars that
  carry no text and never reflow their siblings.
- Every animation has a static end state. Under `prefers-reduced-motion` the end state is
  applied immediately and no timeline runs.
- Nothing decorative delays readable content. The citation pulse fires *after* the summary
  is on screen, not before it.
- Restrained overall. Precise and quick, not playful.

---

## 9. Component states

Designed, not assumed. Rendered in the states gallery of the redesign file.

| Panel | loading | empty | error | degraded / not reporting |
|---|---|---|---|---|
| Summary | 3-line skeleton + stage timeline | "No paper cleared the relevance floor" + broaden action | "Inference did not respond. Nothing was billed." + retry | - |
| Memory | inline skeleton | "No profile yet" | - | "Memory timed out at 300 ms. This answer is unpersonalized." |
| Atlas | skeleton + "Rendering Harvard-Oxford surface" | - | "Atlas unavailable" | - |
| Cost | skeleton bars | - | "TOKEN_LEDGER unreachable. Cost attribution suspended." | "Ledger reachable, no priced rows. Cost is unavailable, not zero." |
| Analyst | "Generating SQL" | - | "Cortex Analyst unavailable" | - |
| Health footer | grey dots | - | amber hollow dot + service name in amber | amber solid dot |

Degraded is never silent and never rendered as zero. "Unavailable" and "0.0000" are
different claims.

---

## 10. Non-negotiables

- The badge "Literature verification, not a diagnostic tool" appears in the hero header and
  the footer.
- Every anatomical view carries "Location reference from literature, not a diagnostic read."
- No copy implies diagnosis or that the tool knows what a patient has.
- Numbers carry units and sample sizes (`n = 148 requests · last 24 h`).
- Banned words: seamless, unlock, empower, cutting-edge, leverage, revolutionize, dive into.

---

## 11. Open items for Card 2B

- Wordmark: **Trace**. Lockup is a 26 px `--blue-500` square with a 6 px `--blue-500 / 0.14`
  halo ring, + Newsreader 600 at 23 px. 22 px variant on `/cost`.
- Median cost per query is the largest element on `/cost` and currently the only figure the
  backend contract does not supply. If the field is still missing at build time, render the
  "not yet reporting" state - do not compute a client-side median. That is a `Blockers.md`
  entry for Card 2A, not a backend edit.
- `animejs` added to `frontend/package.json`; log it in `Handoff-Log.md`.
