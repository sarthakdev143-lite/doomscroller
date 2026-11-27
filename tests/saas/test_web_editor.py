"""The clip editor page (saas.web.editor_routes) and its browser modules (saas/web/static/js/editor).

The page is a shell: who may open it, what it renders and the hooks its modules rely on are checked here.
The modules are checked with node: every file parses as an ES module, every relative import resolves, and
the pure parts (word-edge snapping, caption lines, the spec the page posts, undo history) behave.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "saas" / "web"
EDITOR_JS = WEB / "static" / "js" / "editor"
EDIT_PATH = "/app/clips/{clip_id}/edit"
NODE = shutil.which("node")


class Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.missing_track: list[str] = []
        self.inline_scripts = 0
        self.style_attrs = 0
        self.scripts: list[str] = []
        self.styles: list[str] = []
        self.current_nav: list[str] = []
        self.ids: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        if "style" in a:
            self.style_attrs += 1
        if a.get("id"):
            self.ids.append(a["id"])
        if tag in ("a", "button", "summary", "select") and not a.get("data-track"):
            self.missing_track.append(f"<{tag}> {a}")
        if tag in ("section", "article") and not a.get("data-track-section"):
            self.missing_track.append(f"<{tag}> {a}")
        if tag == "script":
            if a.get("src"):
                self.scripts.append(a["src"])
            else:
                self.inline_scripts += 1
        if tag == "link" and a.get("rel") == "stylesheet":
            self.styles.append(a.get("href", ""))
        if tag == "a" and a.get("aria-current") == "page":
            self.current_nav.append(a.get("href", ""))

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def parse(html: str) -> Page:
    p = Page()
    p.feed(html)
    return p


def _app():
    """The SaaS app with the editor router, whether or not the integrator has wired it in yet."""
    from saas.main import create_app
    from saas.web.editor_routes import router

    app = create_app()
    if not any(getattr(r, "path", None) == EDIT_PATH for r in app.routes):
        app.include_router(router)
    return app


def _user(db, email: str, code: str):
    from saas.common import utcnow
    from saas.models.core import User

    user = User(email=email, email_canonical=email, name=email.split("@")[0].title(), referral_code=code,
                email_verified_at=utcnow())
    db.add(user)
    db.commit()
    return user


def _clip(db, user, *, title: str = "The moment it clicked", idx: int = 1, deleted: bool = False):
    from saas.common import utcnow
    from saas.models.jobs import Clip, Job

    job = Job(user_id=user.id, source_kind="url", source_url="https://www.youtube.com/watch?v=abc", status="done")
    db.add(job)
    db.flush()
    clip = Clip(job_id=job.id, user_id=user.id, idx=idx, path=f"{user.id}/{job.id}/clip_{idx}.mp4", duration_s=24.0,
                title=title, deleted_at=utcnow() if deleted else None)
    db.add(clip)
    db.commit()
    return clip


@pytest.fixture()
def editor_client(saas_db):
    """Signed in as Asha through the same auth dependency the page uses."""
    from fastapi.testclient import TestClient

    from saas.auth.deps import optional_user, require_user

    user = _user(saas_db, "asha@example.com", "refasha01")
    app = _app()
    app.dependency_overrides[require_user] = lambda: user
    app.dependency_overrides[optional_user] = lambda: user
    with TestClient(app) as c:
        c.user = user
        c.db = saas_db
        yield c


# ---- who may open it --------------------------------------------------------------------------------

def test_signed_out_visitors_are_sent_to_sign_in(saas_db):
    from fastapi.testclient import TestClient

    with TestClient(_app()) as c:
        r = c.get("/app/clips/clp_abc/edit", follow_redirects=False, headers={"Accept": "text/html"})
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2Fapp%2Fclips%2Fclp_abc%2Fedit"


def test_owner_gets_the_editor(editor_client):
    clip = _clip(editor_client.db, editor_client.user, idx=1)
    r = editor_client.get(f"/app/clips/{clip.id}/edit")
    assert r.status_code == 200, r.text[:300]
    page = parse(r.text)
    assert page.title.strip().startswith("Edit clip 2")
    assert f'data-clip="{clip.id}"' in r.text and f'data-job="{clip.job_id}"' in r.text
    assert f'href="/app/jobs/{clip.job_id}#clip-{clip.id}"' in r.text, "the way back lands on this clip"
    assert "The moment it clicked" in r.text


@pytest.mark.parametrize("case", ["another_account", "deleted", "unknown", "overlong"])
def test_clips_you_cannot_edit_are_not_found(editor_client, case):
    db = editor_client.db
    if case == "another_account":
        other = _user(db, "ravi@example.com", "refravi01")
        clip_id = _clip(db, other, title="Ravi's secret clip").id
    elif case == "deleted":
        clip_id = _clip(db, editor_client.user, deleted=True).id
    elif case == "unknown":
        clip_id = "clp_doesnotexist"
    else:
        clip_id = "clp_" + "x" * 200
    r = editor_client.get(f"/app/clips/{clip_id}/edit", headers={"Accept": "text/html"})
    assert r.status_code == 404
    assert "text/html" in r.headers["content-type"], "a page answers with the HTML 404, not JSON or a 500"
    assert "secret clip" not in r.text and "data-editor" not in r.text


# ---- what it renders --------------------------------------------------------------------------------

def test_page_follows_the_app_page_rules(editor_client, monkeypatch):
    from saas.templating import templates

    monkeypatch.setitem(templates.env.globals, "brand_name", "Zebracut")
    clip = _clip(editor_client.db, editor_client.user)
    r = editor_client.get(f"/app/clips/{clip.id}/edit")
    page = parse(r.text)
    assert not page.missing_track, page.missing_track[:3]
    assert page.inline_scripts == 0 and page.style_attrs == 0, "CSP: no inline scripts or style attributes"
    assert len(page.ids) == len(set(page.ids)), "ids are unique"
    assert "/app/clips" in page.current_nav
    assert '<meta name="robots" content="noindex, nofollow">' in r.text
    assert any(s.startswith("/static/js/editor/main.js?v=") for s in page.scripts), page.scripts
    assert any(s.startswith("/static/css/editor.css?v=") for s in page.styles), page.styles
    for leak in ("{{", "{%", "Undefined"):
        assert leak not in r.text
    text = re.sub(r"<[^>]+>", " ", r.text)
    assert "!" not in text.replace("<!doctype", ""), "no exclamation marks in the copy"
    assert page.title.strip() == "Edit clip 2 | Zebracut"
    assert "doomscroller" not in r.text.lower(), "the brand name comes from settings"
    for f in [*EDITOR_JS.glob("*.js"), WEB / "static" / "css" / "editor.css", WEB / "templates" / "app" / "editor.html"]:
        assert "doomscroller" not in f.read_text(encoding="utf-8").lower(), f


def test_page_carries_every_hook_the_modules_use(editor_client):
    clip = _clip(editor_client.db, editor_client.user)
    html = editor_client.get(f"/app/clips/{clip.id}/edit").text
    hooks = set()
    for js in EDITOR_JS.glob("*.js"):
        hooks |= set(re.findall(r"""\[(data-[a-z-]+)(?:[=\]])""", js.read_text(encoding="utf-8")))
    # Attributes the modules put on nodes they build themselves (timeline memes, caption words, meme rows).
    created = {"data-k", "data-i", "data-id", "data-line", "data-grip", "data-meme-start", "data-meme-length",
               "data-meme-sound", "data-meme-remove", "data-meme-select"}
    missing = sorted(h for h in hooks - created if f"{h}" not in html)
    assert not missing, missing
    assert "Caption fixes appear after Render changes" in html
    assert 'role="slider"' in html and 'aria-label="Trim start"' in html and 'aria-label="Trim end"' in html
    for tab in ("captions", "hook", "logo", "memes", "sound"):
        assert f'id="ed-tab-{tab}"' in html and f'id="ed-panel-{tab}"' in html


def test_hostile_clip_titles_are_escaped(editor_client):
    clip = _clip(editor_client.db, editor_client.user, title='<script>alert(1)</script>"><img src=x>')
    html = editor_client.get(f"/app/clips/{clip.id}/edit").text
    assert "<script>alert(1)</script>" not in html and "<img src=x>" not in html


def test_assets_are_served_with_the_right_types(editor_client):
    for url, kind in (("/static/js/editor/main.js", "javascript"), ("/static/js/editor/util.js", "javascript"),
                      ("/static/css/editor.css", "text/css")):
        r = editor_client.get(url)
        assert r.status_code == 200, url
        assert kind in r.headers["content-type"], (url, r.headers["content-type"])


# ---- the modules ------------------------------------------------------------------------------------

def _modules() -> list[Path]:
    files = sorted(EDITOR_JS.glob("*.js"))
    assert {"main.js", "state.js", "util.js"} <= {f.name for f in files}
    return files


def test_every_relative_import_resolves():
    spec = re.compile(r"""\b(?:from|import)\s*\(?\s*["'](\.{1,2}/[\w./-]+\.js)["']""")
    for js in _modules():
        for target in spec.findall(js.read_text(encoding="utf-8")):
            assert (js.parent / target).resolve().is_file(), f"{js.name} imports missing {target}"


def test_modules_stay_inside_the_design_rules():
    for js in _modules():
        text = js.read_text(encoding="utf-8")
        assert "innerHTML" not in text, f"{js.name}: build nodes, never parse strings into HTML"
        assert ".setAttribute(\"style\"" not in text and "cssText" not in text, f"{js.name}: CSP forbids style attributes"
        assert "localStorage" not in text or "try" in text, js.name


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_modules_parse_as_es_modules(tmp_path):
    # Copied to .mjs so every node version parses them as modules (CI runs node 20).
    for js in _modules():
        target = tmp_path / (js.stem + ".mjs")
        target.write_text(js.read_text(encoding="utf-8"), encoding="utf-8")
        r = subprocess.run([NODE, "--check", str(target)], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=60)
        assert r.returncode == 0, f"{js.name}: {r.stderr}"


NODE_SPEC = textwrap.dedent("""
    import assert from "node:assert/strict";
    import * as u from "./util.js";
    import * as s from "./state.js";

    const words = [
      { i: 0, text: "so", start_s: 0.4, end_s: 0.6 },
      { i: 1, text: "this", start_s: 0.7, end_s: 0.9 },
      { i: 2, text: "is", start_s: 1.0, end_s: 1.1 },
      { i: 3, text: "it.", start_s: 1.2, end_s: 1.5 },
      { i: 4, text: "Next", start_s: 3.0, end_s: 3.3 },
      { i: 5, text: "part", start_s: 3.4, end_s: 3.9 },
    ];
    const duration = 10;

    // Trim handles land on word edges, keep the minimum length, and step word by word.
    let trim = { start_s: 0, end_s: 10 };
    assert.equal(u.placeTrim(trim, "start", 0.82, { words, duration }).start_s, 0.7);
    assert.equal(u.placeTrim(trim, "start", 0.82, { words, duration, free: true }).start_s, 0.82);
    assert.equal(u.placeTrim(trim, "end", 3.7, { words, duration }).end_s, 3.9);
    assert.equal(u.placeTrim({ start_s: 0, end_s: 3.3 }, "start", 2.9, { words, duration, minLen: 3 }).start_s, 0);
    assert.equal(u.stepTrim(trim, "start", 1, { words, duration }).start_s, 0.4);
    assert.equal(u.stepTrim({ start_s: 0.4, end_s: 10 }, "start", 1, { words, duration }).start_s, 0.7);
    assert.equal(u.stepTrim({ start_s: 0.7, end_s: 10 }, "start", -1, { words, duration }).start_s, 0.4);
    assert.equal(u.stepTrim(trim, "end", -1, { words, duration }).end_s, 3.9);
    assert.equal(u.stepTrim({ start_s: 0.4, end_s: 10 }, "start", 1, { words, duration, fine: 0.1 }).start_s, 0.5);
    assert.deepEqual(u.stepTrim({ start_s: 3.0, end_s: 6.0 }, "start", 1, { words, duration, minLen: 3 }),
                     { start_s: 3.0, end_s: 6.0 }, "no edge left that keeps 3 s");

    // No words (a clip cut on movement, or made without captions): handles move on a 0.1 s grid, arrows by 0.5 s.
    assert.equal(u.placeTrim(trim, "start", 2.04, { words: [], duration }).start_s, 2.0);
    assert.equal(u.placeTrim(trim, "end", 7.36, { words: [], duration }).end_s, 7.4);
    assert.equal(u.stepTrim(trim, "start", 1, { words: [], duration }).start_s, 0.5);
    assert.equal(u.stepTrim(trim, "end", -1, { words: [], duration, fine: 0.1 }).end_s, 9.9);

    // Caption lines break after a sentence and after a pause; the playing word is found by time.
    const lines = u.captionLines(words);
    assert.deepEqual(lines.map((l) => l.words.map((w) => w.i)), [[0, 1, 2, 3], [4, 5]]);
    assert.equal(u.wordAt(words, 1.25), 3);
    assert.equal(u.wordAt(words, 2.0), -1);
    assert.equal(u.stamp(64.25), "1:04.3");
    assert.equal(u.cleanText("  Ravi\\u0000  Kumar\\n"), "Ravi Kumar");

    // The state fills every key; the spec it posts only carries what changed from the first render.
    // The server's limits (saas/jobs/editor.py): ranges as [lo, hi], re-renders as free_left.
    const served = s.readLimits({ free_left: 0, credit_cost_after_free: 1, balance: 40, min_clip_s: 3,
                                  meme_duration_s: [0.5, 4.0], logo_scale: [0.06, 0.25], music_volume_db: [-30, -6] });
    assert.deepEqual([served.freeRerendersLeft, served.creditCost, served.balance], [0, 1, 40]);
    assert.deepEqual([served.memeMin, served.memeMax, served.logoMin, served.logoMax, served.dbMin, served.dbMax],
                     [0.5, 4, 0.06, 0.25, -30, -6]);
    assert.equal(s.readLimits({}).freeRerendersLeft, null, "unknown quota shows nothing rather than a guess");
    const limits = s.readLimits({ free_rerenders_left: 2 });
    const ctx = { duration, words, limits, logoAvailable: false, defaults: null, memes: null };
    const base = s.effective({}, ctx);
    assert.equal(limits.freeRerendersLeft, 2);
    assert.deepEqual(base.trim, { start_s: 0, end_s: 10 });
    assert.equal(base.logo.enabled, false);
    const serverKeys = new Set();
    assert.deepEqual(s.toSpec(base, { base, serverKeys, ctx }), {}, "untouched means as first made");

    const st = s.clone(base);
    st.trim = { start_s: 0.7, end_s: 3.9 };
    s.setEdit(st, words[1], "  This ", limits.captionChars);
    s.setEdit(st, words[0], "so", limits.captionChars);  // same as the original: no fix recorded
    st.memes.push(s.fitMeme({ id: "giphy-1", start_s: 9.9, duration_s: 9, sound: true }, ctx));
    st.music.enabled = true;
    const spec = s.toSpec(st, { base, serverKeys, ctx });
    assert.deepEqual(Object.keys(spec).sort(), ["captions", "memes", "music", "trim"]);
    assert.deepEqual(spec.captions, { enabled: true, edits: [{ i: 1, text: "This" }] });
    assert.deepEqual(spec.memes, [{ id: "giphy-1", start_s: 6, duration_s: 4, sound: true }], "kept inside the clip");
    assert.deepEqual(spec.music, { enabled: true }, "volume stays the clip's own until it is moved");
    for (const key of Object.keys(spec)) assert.ok(s.KEYS.includes(key));

    // A stored spec round-trips: its keys are always sent back, even unchanged.
    const stored = { trim: { start_s: 0.7, end_s: 3.9 }, captions: { edits: { "1": "This" } }, logo: { enabled: false } };
    const back = s.effective(stored, ctx);
    assert.deepEqual(back.captions.edits, [{ i: 1, text: "This" }]);
    assert.deepEqual(Object.keys(s.toSpec(back, { base, serverKeys: new Set(Object.keys(stored)), ctx })).sort(),
                     ["captions", "logo", "trim"]);
    // Without a logo, the only logo setting ever sent is "off" (the engine refuses anything else).
    const withLogo = s.clone(base);
    withLogo.logo = { enabled: true, position: "top-left", scale: 0.1 };
    assert.deepEqual(s.toSpec(withLogo, { base, serverKeys, ctx }).logo, { enabled: false });
    const ctxLogo = { ...ctx, logoAvailable: true };
    const baseLogo = s.effective({}, ctxLogo);
    assert.deepEqual(s.toSpec(withLogo, { base: baseLogo, serverKeys, ctx: ctxLogo }).logo,
                     { enabled: true, position: "top-left", scale: 0.1 });
    assert.ok(s.same({ memes: [{ id: "b", start_s: 2 }, { id: "a", start_s: 1 }] },
                     { memes: [{ id: "a", start_s: 1 }, { id: "b", start_s: 2 }] }), "meme order is not a change");

    // Memes share one corner (the engine's rule): an earlier one is cut where the next starts, and two that
    // start under half a second apart can't render.
    const spans = s.memeSpans([{ start_s: 5, duration_s: 4 }, { start_s: 7, duration_s: 1 }, { start_s: 7.3, duration_s: 1 }]);
    assert.deepEqual(spans.map((x) => [x.start, x.end, x.cut, x.clash]),
                     [[5, 7, true, false], [7, 7.3, true, true], [7.3, 8.3, false, true]]);
    assert.equal(s.clearStart([{ start_s: 5 }, { start_s: 5.5 }], 5.2), 6, "a new meme lands clear of the others");
    assert.equal(s.clearStart([{ start_s: 5 }], 9), 9);

    // History: one step per change, bursts on one control merge, a drag is one step.
    let now = 0;
    const store = new s.Store(base, { now: () => now });
    store.change((x) => { x.hook.text = "W"; }, { key: "hook" });
    now += 200;
    store.change((x) => { x.hook.text = "Wa"; }, { key: "hook" });
    now += 5000;
    store.change((x) => { x.sfx.enabled = false; });
    assert.equal(store.past.length, 2);
    store.begin();
    store.change((x) => { x.trim.start_s = 1; });
    store.change((x) => { x.trim.start_s = 2; });
    store.end();
    assert.equal(store.past.length, 3);
    assert.ok(store.undo());
    assert.equal(store.state.trim.start_s, 0);
    assert.ok(store.undo());
    assert.equal(store.state.sfx.enabled, true);
    assert.ok(store.undo());
    assert.equal(store.state.hook.text, "");
    assert.ok(!store.undo());
    assert.ok(store.redo() && store.redo());
    assert.equal(store.state.sfx.enabled, false);
    assert.equal(store.change((x) => { x.sfx.enabled = false; }), false, "a no-op is not a step");
    store.change((x) => { x.broll.enabled = false; });
    assert.ok(!store.canRedo, "a new change drops the redo branch");
    console.log("ok");
""")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_pure_modules_behave(tmp_path):
    for name in ("util.js", "state.js"):
        shutil.copyfile(EDITOR_JS / name, tmp_path / name)
    (tmp_path / "package.json").write_text(json.dumps({"type": "module"}), encoding="utf-8")
    (tmp_path / "spec.js").write_text(NODE_SPEC, encoding="utf-8")
    r = subprocess.run([NODE, str(tmp_path / "spec.js")], capture_output=True, text=True, cwd=tmp_path,
                       stdin=subprocess.DEVNULL, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr or r.stdout
