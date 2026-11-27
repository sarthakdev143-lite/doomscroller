# Demo clip slot

The landing pages show the product as HTML/CSS (`partials/clip.html`, `static/js/landing.js`,
`static/js/redesign.js`): clip frames drawn to the renderer's own geometry and a scripted transcript. No
third-party video is committed here, and none is ever faked.

## Before/after section (landing previews A and B)

When the owner has three real clips to show, put all six files here. The section switches to them only when all
six exist (`saas/web/redesign.py` `demo_videos`); otherwise it keeps the drawn frames.

- `clip-1.mp4`, `clip-2.mp4`, `clip-3.mp4`: three finished clips made by the product from footage the owner owns
  outright (their own podcast or talk; never a creator's video, even with credit). 1080x1920, H.264, 15-60 s,
  each under 4 MB (`-crf 28 -preset slow -movflags +faststart`), with captions and the hook burned in by the
  product itself, not edited afterwards. Muted playback is what visitors see first, so the captions carry it.
- `clip-1.jpg`, `clip-2.jpg`, `clip-3.jpg`: each clip's first frame, 540x960, used as its `poster`.

The URLs go through `asset()`, so replacing a file changes its `?v=` (static files are cached as immutable).
Update `DEMO_CLIPS` in `saas/web/routes.py` with the real source length and in/out timecodes at the same time,
so the source timeline above the clips shows the real numbers instead of the scripted episode.
