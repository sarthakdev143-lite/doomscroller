# Landing direction D — "the cut" (spec)

The owner rejected directions A, B and C (all: same yellow, text-heavy heroes, drawn mockups, HUD panels, a normal
page with 3D beside it). D is one cinematic, scroll-directed piece at the level of igloo.inc / lusion.co, the two
references the owner named. It must not look like a SaaS template, a reskin of A–C, or a clone of either reference.

## The owner's taste (from his own words; binding)
- **Wants:** studio-level, a story you move *through*, smooth transitions, moving backgrounds, realism, and
  *something to play with in every section*. He loved one thing: a real-looking cinematic intro behind a loader that
  counts 000→100 with real progress while a 1px frame draws itself around the screen edge by edge.
- **Rejects on sight:** walls of text / unasked information; split-hero SaaS layouts and card grids; HUD or console
  chrome, corner brackets, mono-transcript panels, custom reticle cursors; cartoon, flat-illustrated or blocky
  visuals; human faces as decoration; static backgrounds; fade-in-only reveals; the stacked "Awwwards kit" (particle
  morphs, letter-by-letter reveals, film grain, magnetic buttons); low contrast; lag; anything that reads as
  "vibe coded". Clever taglines instead of saying plainly what it does.

## Build order: a gated slice
Build the loader and the opening (scenes 0–3) first and stop: the owner reviews a desktop + phone scroll-through video
before anything else is built. Only then scenes 4–7.

**The opening must not be C again.** C was a 3D video ribbon on real-time planes, a light scanning a transcript,
pieces snapping into phones — rejected. The owner also called his own real-time three.js/Lenis/bloom portfolio build
"not even close to studio level", and loved only a *pre-rendered* Blender Cycles film (frames:
`content/site-previews/refs/portfolio-loved-film-cmp.jpg`; its script and GitHub Actions render pattern:
`D:\Sarthak\Coding\My Codes\Portfolio\portfolio\.claude\worktrees\update-8x\scripts\blender\intro.py`). So the hero
moment is preferably a pre-rendered, physically lit film of the real footage being cut — real materials, real light,
weight — scrubbed by scroll (image sequence or keyframe-dense video), with light real-time layers only where they add
play. Write down, before building, what makes D's opening different from C's. Heavy renders run on GitHub Actions (a
`workflow_dispatch` render job, as the portfolio does), never as a long local job on the owner's laptop.

## Story (one line of copy per screen, max ~8 words; no paragraphs above the fold)
0. **Loader.** Real asset-loading progress 000→100 (counter tied to actual bytes/textures loaded), a 1px frame
   drawing around the viewport edge by edge, tiny uppercase labels in two corners only (brand, "loading footage").
   Ends in a cut, not a fade.
1. **Open — "Long video in."** A long horizontal strip of the REAL source footage (playing, as a video texture)
   floats in near-black space with soft volumetric light. The camera drifts; the strip breathes.
   *Play:* the pointer is a light that grazes the strip; dragging scrubs the footage.
2. **"We find the moments worth posting."** Scroll travels along the strip; the real transcript's sentence breaks
   glow as hairlines across the footage; the chosen moments warm up.
   *Play:* drag along the strip to scrub; the moments respond.
3. **"Cut on the sentence."** A blade of light runs down the cut lines; the strip separates at exactly those
   sentence boundaries; each piece rotates upright into 9:16. Physical, weighty, not confetti.
   *Play:* the cut follows scroll velocity; scrolling back re-joins the strip.
4. **"Every word, captioned."** One finished Short fills the frame with its real burned-in captions, real audio
   available. *Play:* tap the sound toggle; tap the clip to pause/scrub.
5. **"Ready to post."** The finished Shorts stand in a row/stack in space, each playing, with title + "post at"
   time beneath in small type. *Play:* swipe/drag through them.
6. **"₹0.30 a minute."** *Play:* drag a single control for source length (1–120 min) → the price in ₹ and the
   number of Shorts, computed from `saas/billing/seed_catalog.json` (never hard-coded). One line under it naming
   the free tier.
7. **"Join the waitlist."** One email field + Turnstile + consent, nothing else on the screen. Uses the existing
   waitlist endpoint and form contract of the live site (see `edge/functions/api/waitlist*` and the A/B/C templates).

Footer: legal links only.

## Visual system
- **Not the portfolio's look** (deep navy + aqua + Archivo): this brand has its own.
- Near-black stage (#07070A-ish), warm off-white ink, ONE accent: a recording red (the REC dot) used sparingly — the
  live moment, the blade, the active cut. No yellow.
- Typography: one display face with real presence for the one-liners, one quiet text face; self-hosted woff2, OFL
  or similar licence; not Archivo, not Syne/Space Grotesk/Inter (template tells). Huge sizes, tight leading.
- Light, depth and motion carry the premium feel: real-time three.js (vendored at `saas/web/static/vendor/`), video
  textures from the real footage, soft bloom where it earns it, a spring-damped camera, Lenis smooth scroll. One easing
  family throughout. 60fps target on a mid laptop; adaptive quality on phones; a calm, beautiful static version under
  `prefers-reduced-motion` and without WebGL (stills from the real footage, not drawings).

## Footage (real, licensed for commercial use — never the owner's MrBeast download)
- Source: public-domain NASA video, or CC-BY with attribution — but first read NASA's media usage guidelines on
  recognisable people in commercial/advertising use. If that rules out identifiable people, use narrated footage
  with nobody identifiable on screen (which also fits the no-faces rule). No NASA insignia emphasis, no implied
  endorsement. Record URL + licence in `docs/saas/landing-d-sources.md`.
- The Shorts on the page are cut from that source by THIS product's pipeline (proof, not mockups). Cut more than
  three and keep only clips whose title matches their content, that carry no outro/housekeeping, and whose frames
  look right.
- Web budget: total media ≤ ~12 MB, lazy-loaded after the loader's critical set; H.264 MP4 + poster JPGs.

## Engineering constraints
- Template `saas/web/templates/redesign/d.html`, styles `saas/web/static/css/redesign-d.css`, scripts
  `saas/web/static/js/d/`, media `saas/web/static/media/d/`. Exported to `/preview/d/` by
  `scripts/saas_export_static.py --previews`. Never touches the live page until the owner approves.
- The CSP allows no third-party scripts or CDNs (vendored only). Brand name comes from config, never a literal
  (`tests/saas/test_web_pages.py` enforces it). Consent-gated analytics and Turnstile as in the existing templates.
- Accessibility: real text in the DOM for every line, keyboard reachable controls, captions/alt for media.

## The opening as built: how it differs from C (written before building)

C's opening was a real-time three.js ribbon of drawn podcast frames beside a normal text page: a split hero with a
paragraph and a form, a yellow underline, a transcript panel in mono with IN/OUT timecodes, a HUD bar (file name,
timecode, chapter, sound toggle), a light scanning words, and shards snapping into phone mockups. The owner rejected
all of it. D's opening keeps only the idea (a long video is cut on its sentences) and changes everything he saw:

| | C (rejected) | D (opening, scenes 0-3) |
|---|---|---|
| Picture | flat planes lit in real time; cartoon podcast hosts drawn in SVG | a pre-rendered Blender Cycles film: glass tiles with thickness and bevels, a dark lacquered floor with real reflections, haze, depth of field, motion blur |
| Footage | none (drawn frames) | the real NASA working copy, mapped as emission onto the tiles; every tile plays its own seconds of the source |
| Layout | page with 3D beside it, nav, paragraph, form above the fold | full-bleed film, nothing beside it; one line of copy per screen, no nav, no paragraph, no form in scenes 0-3 |
| The cut | shards fly into phone mockups | the strip parts at the four sentence boundaries where the pipeline really cut (run 46085200172); the unchosen pieces drop to the floor and switch off; each chosen stretch gathers into a deck whose thickness is its length and stands up into 9:16 |
| Chrome | HUD bar, timecodes, chapter labels, mono transcript, custom cursor, magnetic button | none: two tiny corner labels on the loader only; the sentence breaks are hairlines of light, never text or timecodes |
| Colour | yellow accent | near-black, warm off-white ink, one recording red used only for the blade; no yellow anywhere in scenes 0-3 (the Shorts' yellow word captions belong to scene 4 and are flagged there) |
| Real time | the whole scene | only what adds play: frame blending, the pointer as a light grazing the glass (from a rendered normal pass), drag to scrub, an idle playhead that breathes across each beat |

Checkable against stills: no nav, paragraph, form, timecode, chapter label, transcript text, drawn frame, mockup or
yellow appears anywhere in scenes 0-3.

What a pre-rendered film cannot do, said plainly: the footage on the tiles is baked into the frames, so it "plays"
only as the playhead moves (scroll, drag, the intro run and the idle breathing), and "the cut follows scroll velocity"
means the film advances at the speed you scroll and runs backwards (re-joining the strip) when you scroll back.

### Opening media budget (per visitor, one orientation)

| Set | Budget | Loaded |
|---|---|---|
| Critical (the loader counts these bytes) | <= 2.5 MB | before the cut: the intro frames plus every fourth frame of the rest, so scrubbing works at once |
| Rest of the film | <= 3.5 MB | after the cut, nearest the playhead first |
| Light pass (normals for the pointer light) | <= 0.6 MB | after the film, skipped on touch screens |
| Reduced motion / no WebGL | <= 0.6 MB | three stills instead of the film |

So the opening stays at or under ~6.5 MB for one orientation (desktop or phone frames, never both), leaving at least
5.5 MB of the page's ~12 MB for scenes 4-7. Frames ship as a few packed bundles with hashed names (not hundreds of
files), indexed by a manifest the template versions through `asset()`.

### The opening as built (for the next session)

- **Film:** `scripts/landing_film/` (`film.json` holds the cut data, beats and render config; `prep.py` builds the
  tile atlases and the captionless vertical faces from the showcase edit list; `film.py` is the Blender scene;
  `encode.py` packs the frames). `.github/workflows/landing-film.yml` renders it on Actions: set `render.mode` to
  `stills` to judge a look (one job per frame), `full` for the whole film (a few hundred frames, two cameras, about an
  hour), or `draft: true` for a small noisy whole film. To fix a few seconds without another hour, add
  `"patch": {"base_runs": [<full run id>], "ranges": {"desk": [a, b], "phone": [a, b]}}`: only those frames render,
  laid over the base run's frames at encode (base frames expire after 3 days). The shipped film is full run
  46100630797 plus patch 46105828943; a fresh `full` render of the scripts as committed reproduces it. Its `film-web` artifact is what ships in
  `saas/web/static/media/d/`.
- **Beats (film frames, 24 fps):** intro 1-40 runs on its own clock after the loader's cut; scene 1 rests on 40-58,
  scene 2 on 118-138, the blades fall on 138-162, the decks gather 180-212, roll upright 210-233, and scene 3 rests
  on 234-256. A rest is a slow camera drift, so the idle playhead can breathe across it.
- **Review:** `.github/workflows/landing-preview.yml` exports the preview, serves it under the production CSP and
  records desktop (1440x900) and phone (390x844) scroll-throughs frame by frame, plus stills and measurements
  (artifact `landing-preview`). `scripts/landing_preview/preview.json` can point it at a film run's frames instead of
  the committed ones.
- **Known limits:** LCP is the loader's number until the cut, then the first line once it rises; the base template
  still preloads the old Archivo and Plex fonts (about 105 KB D never uses); the NASA title cards inside the footage
  carry their own cyan numerals.
