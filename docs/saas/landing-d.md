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
  endorsement. Record URL + licence in `saas/web/static/media/d/SOURCES.md`.
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
