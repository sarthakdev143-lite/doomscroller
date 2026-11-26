# Demo clip slot

The landing page currently shows the product as HTML/CSS (`partials/clip.html`, `static/js/landing.js`): a
clip frame drawn to the renderer's own geometry and a scripted transcript. No third-party video is
committed here.

When the owner has a real before/after to show, drop it in this folder:

- `demo-clip.mp4`: one finished clip made by the product from footage the owner owns outright
  (their own podcast or talk; never a creator's video, even with credit). 1080x1920, H.264, 15-25 s,
  under 4 MB (`-crf 28 -preset slow -movflags +faststart`), with captions and the hook burned in by the
  product itself, not edited afterwards.
- `demo-clip.jpg`: its first frame, 540x960, used as the `poster` so the hero paints before the video loads.
- `demo-source.txt`: the source's title, length and the clip's in/out timecodes, so the timeline under the
  hero can show the real numbers instead of the scripted episode.

Wiring it in is a template change in `landing.html` (a muted, looping, `playsinline` `<video>` replacing the
live frame, kept behind the same Pause control and `prefers-reduced-motion` rule).
