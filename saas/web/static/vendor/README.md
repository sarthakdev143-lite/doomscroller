# Vendored browser libraries

Self-hosted so no page loads code from a CDN at run time. Used by landing preview C (`static/js/c/`), imported by
relative path, so `scripts/saas_export_static.py` versions every import with `?v=<hash>` like the site's own modules.
This README is not exported (the exporter skips `.md`); each folder's licence file is.

| folder | package | version | licence | files |
|---|---|---|---|---|
| `three/` | `three` (npm) | 0.187.0 | MIT, `three/LICENSE` | `three.core.min.js`, `three.module.min.js` |
| `gsap/` | `gsap` (npm) | 3.15.1 | Standard "No Charge" GSAP License, `gsap/LICENSE.txt` | `gsap-core.js`, `CSSPlugin.js`, `index.js`, `Observer.js`, `ScrollTrigger.js` |
| `lenis/` | `lenis` (npm) | 1.3.29 | MIT, `lenis/LICENSE` | `lenis.min.js` |
| `google-signin/` | Google's Sign in with Google asset pack | 2026-11-28 | Google brand asset, `google-signin/NOTICE.txt` | `{light,dark,neutral}-{1,2,3,4}x.png` (unmodified PNGs; the sign-in pages use `light`) |

## How they were made

Each file is the package's own ES module, minified one file at a time (no bundling, licence comments kept) with the
esbuild that `edge/node_modules` already has:

```bash
npm pack three@0.187.0 gsap@3.15.1 lenis@1.3.29   # in an empty scratch folder, then untar each
ESB="node edge/node_modules/esbuild/bin/esbuild"
$ESB package/build/three.core.js   --minify --format=esm --legal-comments=inline --outfile=three/three.core.min.js
$ESB package/build/three.module.js --minify --format=esm --legal-comments=inline --outfile=three/three.module.min.js
#   then in three.module.min.js: from"./three.core.js"  ->  from"./three.core.min.js"
for f in gsap-core CSSPlugin index Observer ScrollTrigger; do
  $ESB package/$f.js --minify --format=esm --legal-comments=inline --outfile=gsap/$f.js
done
$ESB package/dist/lenis.mjs --minify --format=esm --outfile=lenis/lenis.min.js
#   then prepend the one-line MIT banner (lenis.mjs ships without one)
```

three.js 0.187 no longer publishes `*.min.js` builds; minifying the two ES modules gives the same pair it used to
ship. It needs WebGL 2 (`static/js/c/main.js` checks for it and otherwise shows the still version).
