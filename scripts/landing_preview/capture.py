"""Record the landing-D opening for review: desktop (1440x900) and phone (390x844) scroll-throughs as MP4, key-beat
stills, the reduced-motion version, and measurements.

    python scripts/landing_preview/capture.py --base http://127.0.0.1:8080 --out preview-out/

Runs on GitHub Actions (.github/workflows/landing-preview.yml); the runner has no GPU, so WebGL runs on SwiftShader.
A real-time recording there would show the runner's lag, not the page's, so the scroll-throughs are frame-stepped:
the page's clock (requestAnimationFrame, performance.now, timers) is Playwright's fake clock, advanced 1/30 s per
video frame, and each frame waits for the film's decodes before the screenshot. The network is real (throttled
during the loader, so its count shows real progress). Frame timings in metrics.json come from a separate real-time
pass and are labelled software-GL: they measure the page's JavaScript cost, not a visitor's GPU.

Exits non-zero (after writing everything) on any console error, page error or CSP violation, or if the film did not
run (so a review never shows the fallback by mistake).
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ARGS = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist", "--enable-webgl"]
FPS = 30
STEP = 1000.0 / FPS
PATH = "/preview/d/"

WATCH = """
window.__csp = [];
document.addEventListener('securitypolicyviolation', e => window.__csp.push(e.violatedDirective + ' ' + e.blockedURI));
window.__lcp = 0; window.__cls = 0;
try {
  new PerformanceObserver(l => { for (const e of l.getEntries()) window.__lcp = e.startTime; })
    .observe({type: 'largest-contentful-paint', buffered: true});
  new PerformanceObserver(l => { for (const e of l.getEntries()) if (!e.hadRecentInput) window.__cls += e.value; })
    .observe({type: 'layout-shift', buffered: true});
} catch (e) {}
"""


def wait_js(page, expr: str, timeout: float = 60.0) -> None:
    """Poll a JavaScript expression until it is truthy. (page.wait_for_function evaluates its predicate with eval,
    which the production CSP rightly blocks.)"""
    end = time.time() + timeout
    while time.time() < end:
        if page.evaluate(expr):
            return
        time.sleep(0.1)
    raise TimeoutError(f"timed out waiting for {expr}")


def smooth(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


class Recorder:
    def __init__(self, page, folder: Path):
        self.page = page
        self.dir = folder
        self.dir.mkdir(parents=True, exist_ok=True)
        self.n = 0
        self.ms = 0.0  # fake time owed, so 1/30 s steps never drift
        self.stills: dict[str, str] = {}

    def state(self):
        return self.page.evaluate("window.__d && window.__d.snapshot ? window.__d.snapshot() : null")

    def frame(self, real_wait: float = 0.0):
        self.ms += STEP
        whole = int(self.ms)
        self.ms -= whole
        self.page.clock.run_for(whole)
        if real_wait:
            time.sleep(real_wait)
        t0 = time.time()
        while time.time() - t0 < 2.0:  # let the film's decodes land before the picture is taken
            s = self.state()
            if not s or s.get("phase") != "film" or not s.get("pending"):
                break
            time.sleep(0.02)
        self.page.evaluate("window.__d && window.__d.draw && window.__d.draw()")
        self.page.screenshot(path=str(self.dir / f"{self.n:05d}.png"))
        self.n += 1

    def still(self, name: str, out: Path):
        src = self.dir / f"{self.n - 1:05d}.png"
        shutil.copyfile(src, out / f"{name}.png")
        self.stills[name] = f"{name}.png"

    def hold(self, seconds: float):
        for _ in range(round(seconds * FPS)):
            self.frame()

    def scroll_to(self, y_to: float, seconds: float, mouse=None):
        y0 = self.page.evaluate("window.scrollY")
        frames = max(1, round(seconds * FPS))
        for i in range(1, frames + 1):
            y = y0 + (y_to - y0) * smooth(i / frames)
            self.page.evaluate(f"window.scrollTo(0, {y:.1f})")
            if mouse:
                mouse(i / frames)
            self.frame()


def scroll_for(page, p: float) -> float:
    """Scroll position for story progress p (0..1), as static/js/d/story.js measures it."""
    return page.evaluate(
        f"""(() => {{ const s = document.querySelector('[data-d-story]'); const r = s.getBoundingClientRect();
        const top = r.top + window.scrollY; return top + {p} * Math.max(1, s.offsetHeight - window.innerHeight); }})()""")


def record(pw, base: str, out: Path, kind: str, errors: list) -> dict:
    phone = kind == "phone"
    browser = pw.chromium.launch(args=ARGS)
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844} if phone else {"width": 1440, "height": 900},
        device_scale_factor=2 if phone else 1,
        is_mobile=phone,
        has_touch=phone,
        reduced_motion="no-preference",
    )
    page = ctx.new_page()
    page.add_init_script(WATCH)
    page.on("console", lambda m: errors.append(f"{kind} console {m.type}: {m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(f"{kind} pageerror: {e}"))
    cdp = ctx.new_cdp_session(page)
    cdp.send("Network.enable")
    # a realistic connection for the loader: its count shows real bytes arriving
    cdp.send("Network.emulateNetworkConditions", {"offline": False, "latency": 30,
                                                   "downloadThroughput": 160_000, "uploadThroughput": 100_000})
    start = 1_767_000_000_000
    page.clock.install(time=start)
    page.clock.pause_at(start + 1000)  # from here on, time moves only when the recorder steps it
    page.goto(base + PATH + "?capture=1", wait_until="commit")
    rec = Recorder(page, out / f"{kind}-frames")
    stills_dir = out / "stills"
    stills_dir.mkdir(parents=True, exist_ok=True)

    # 0. the loader, until the cut
    seen_half = False
    for _ in range(FPS * 60):
        rec.frame(real_wait=0.03)
        s = rec.state() or {}
        if s.get("mode") == "still":
            raise SystemExit(f"{kind}: the page fell back to stills ({s}); not recording the fallback as the film")
        if not seen_half:
            now = page.evaluate("Number(document.querySelector('[data-d-loader]').getAttribute('aria-valuenow'))")
            if now >= 50:
                rec.still(f"{kind}-0-loader", stills_dir)
                seen_half = True
        if s.get("phase") == "film":
            break
    else:
        raise SystemExit(f"{kind}: the loader never cut to the film")
    rec.still(f"{kind}-1-cut", stills_dir)
    cdp.send("Network.emulateNetworkConditions", {"offline": False, "latency": 0, "downloadThroughput": -1,
                                                   "uploadThroughput": -1})
    w, h = (390, 844) if phone else (1440, 900)

    # 1. the intro plays, then scene 1 rests (and breathes)
    rec.hold(2.2)
    rec.still(f"{kind}-2-scene1", stills_dir)
    rec.hold(2.4)
    if not phone:  # the pointer as a light, grazing the glass
        page.mouse.move(w * 0.12, h * 0.42)
        for i in range(round(2.4 * FPS)):
            t = smooth(i / (2.4 * FPS))
            page.mouse.move(w * (0.12 + 0.7 * t), h * (0.42 + 0.08 * math.sin(t * math.pi)))
            rec.frame()
            if i == round(1.2 * FPS):
                rec.still(f"{kind}-3-light", stills_dir)

    # 2. along the strip to scene 2, rest there
    rec.scroll_to(scroll_for(page, 0.36), 3.4)
    rec.hold(1.6)
    rec.still(f"{kind}-4-scene2", stills_dir)
    rec.hold(1.4)

    # drag to scrub: pull the strip left, hold, let go
    page.mouse.move(w * 0.7, h * 0.55)
    page.mouse.down()
    for i in range(round(1.2 * FPS)):
        page.mouse.move(w * (0.7 - 0.42 * smooth(i / (1.2 * FPS))), h * 0.55)
        rec.frame()
    rec.still(f"{kind}-5-drag", stills_dir)
    rec.hold(0.4)
    page.mouse.up()
    rec.hold(1.4)

    # 3. the cut, to scene 3
    rec.scroll_to(scroll_for(page, 0.47), 2.6)
    rec.still(f"{kind}-6-blade", stills_dir)
    rec.scroll_to(scroll_for(page, 0.72), 3.4)
    rec.still(f"{kind}-7-gather", stills_dir)
    rec.scroll_to(scroll_for(page, 0.95), 2.8)
    rec.hold(2.0)
    rec.still(f"{kind}-8-scene3", stills_dir)
    rec.hold(1.2)

    # scroll back: the strip re-joins; then forward again and on to the footer
    rec.scroll_to(scroll_for(page, 0.46), 3.0)
    rec.still(f"{kind}-9-rejoin", stills_dir)
    rec.scroll_to(scroll_for(page, 1.0), 2.4)
    rec.hold(0.8)
    rec.scroll_to(page.evaluate("document.documentElement.scrollHeight - window.innerHeight"), 1.4)
    rec.hold(0.8)
    rec.still(f"{kind}-10-footer", stills_dir)

    state = rec.state()
    csp = page.evaluate("window.__csp")
    errors.extend(f"{kind} csp: {c}" for c in csp)
    stats = page.evaluate("""(() => { const s = window.__d.story; if (!s) return null;
        return {work: s.stats.work.slice(), drawn: s.stats.frames}; })()""")
    browser.close()

    video = out / f"{kind}-scroll-through.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-framerate", str(FPS),
                    "-i", str(rec.dir / "%05d.png"), "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                    "-c:v", "libx264", "-preset", "slow", "-crf", "17", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", str(video)], check=True)
    work = (stats or {}).get("work") or [0]
    return {"frames": rec.n, "seconds": round(rec.n / FPS, 1), "video": video.name, "final_state": state,
            "stills": rec.stills,
            "js_ms_per_tick_stepped": {"p50": round(statistics.median(work), 2),
                                       "p95": round(sorted(work)[int(len(work) * 0.95) - 1], 2)}}


def measure(pw, base: str, kind: str, errors: list) -> dict:
    """Real time, no fake clock, no throttling: load, then a continuous scroll through the opening."""
    phone = kind == "phone"
    browser = pw.chromium.launch(args=ARGS)
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844} if phone else {"width": 1440, "height": 900},
        device_scale_factor=2 if phone else 1, is_mobile=phone, has_touch=phone)
    page = ctx.new_page()
    page.add_init_script(WATCH)
    page.on("console", lambda m: errors.append(f"{kind} rt console {m.type}: {m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(f"{kind} rt pageerror: {e}"))
    t0 = time.time()
    page.goto(base + PATH, wait_until="load")
    wait_js(page, "window.__d && window.__d.phase === 'film'")
    cut_wall = time.time() - t0
    wait_js(page, "window.__d.marks['film-loaded'] !== undefined")
    page.wait_for_timeout(2500)
    page.evaluate("window.__d.story.stats.intervals.length = 0; window.__d.story.stats.work.length = 0;")
    page.evaluate("""() => new Promise(done => {
        const s = document.querySelector('[data-d-story]');
        const end = s.offsetTop + s.offsetHeight - innerHeight; const t0 = performance.now(); const T = 9000;
        const go = (t) => { const k = Math.min(1, (t - t0) / T); scrollTo(0, end * (0.5 - 0.5 * Math.cos(Math.PI * k)));
          if (k < 1) requestAnimationFrame(go); else setTimeout(done, 600); };
        requestAnimationFrame(go); })""")
    data = page.evaluate("""(() => ({
        lcp: window.__lcp, cls: window.__cls, marks: window.__d.marks, bytes: window.__d.bytes,
        intervals: window.__d.story.stats.intervals.slice(), work: window.__d.story.stats.work.slice(),
        resources: performance.getEntriesByType('resource').map(r => [r.name.replace(location.origin, ''), r.encodedBodySize]),
        canvas: [document.querySelector('[data-d-canvas]').width, document.querySelector('[data-d-canvas]').height],
        csp: window.__csp }))()""")
    browser.close()
    errors.extend(f"{kind} rt csp: {c}" for c in data.pop("csp"))
    iv = sorted(data.pop("intervals") or [16.7])
    wk = sorted(data.pop("work") or [0])
    res = data.pop("resources")
    media = sum(b for n, b in res if "/media/d/" in n)
    data.update({
        "wall_seconds_to_cut": round(cut_wall, 2),
        "frame_interval_ms_software_gl": {"p50": round(iv[len(iv) // 2], 2), "p95": round(iv[int(len(iv) * 0.95) - 1], 2),
                                          "over_20ms_pct": round(100 * sum(1 for x in iv if x > 20) / len(iv), 1),
                                          "samples": len(iv)},
        "js_ms_per_tick": {"p50": round(wk[len(wk) // 2], 2), "p95": round(wk[int(len(wk) * 0.95) - 1], 2)},
        "media_bytes_loaded": media,
        "all_bytes_loaded": sum(b for _n, b in res),
        "requests": len(res),
    })
    return data


def reduced(pw, base: str, out: Path, kind: str, errors: list) -> dict:
    phone = kind == "phone"
    browser = pw.chromium.launch(args=ARGS)
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844} if phone else {"width": 1440, "height": 900},
        device_scale_factor=2 if phone else 1, is_mobile=phone, has_touch=phone, reduced_motion="reduce")
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(f"{kind} reduced pageerror: {e}"))
    page.goto(base + PATH, wait_until="load")
    mode = page.evaluate("document.documentElement.dataset.d")
    page.evaluate("document.querySelectorAll('img').forEach(i => i.loading = 'eager')")
    page.wait_for_timeout(1500)
    shot = out / "stills" / f"{kind}-reduced-motion.png"
    page.screenshot(path=str(shot), full_page=True)
    browser.close()
    return {"mode": mode, "still": shot.name}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="", help="desktop or phone")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    report: dict = {"note": "frame timings are from GitHub's CPU-only runner (SwiftShader WebGL); they show the "
                            "page's JavaScript cost per frame, not a visitor's GPU"}
    kinds = [args.only] if args.only else ["desktop", "phone"]
    try:
        with sync_playwright() as pw:
            for kind in kinds:
                report[kind] = {}
                for part, fn in (("recording", record), ("real_time", measure), ("reduced_motion", reduced)):
                    try:
                        report[kind][part] = fn(pw, args.base, out, kind, errors) if fn is not measure                             else fn(pw, args.base, kind, errors)
                    except Exception as exc:  # keep going: the other parts are still worth seeing
                        errors.append(f"{kind} {part} failed: {type(exc).__name__}: {exc}")
    finally:
        report["errors"] = errors
        (out / "metrics.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
        for kind in kinds:
            shutil.rmtree(out / f"{kind}-frames", ignore_errors=True)
    print(json.dumps(report, indent=1)[:6000])
    if errors:
        print("errors:\n" + "\n".join(errors), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
