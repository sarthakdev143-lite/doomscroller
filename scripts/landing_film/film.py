# scripts/landing_film/film.py
# The opening film of landing direction D (scenes 1-3), rendered offline in Blender Cycles on GitHub Actions
# (.github/workflows/landing-film.yml). Assets for this project's own website only, never customer work.
#
# A long strip of dark glass tiles floats over a lacquered black floor in faint haze. Every tile carries a few
# seconds of the real source (the NASA working copy, via prep.py's atlases) as emitted light, so the strip is the
# whole video laid out in time. The camera travels along it; hairlines of light come up in the gaps where the
# sentences end; the two stretches the pipeline chose warm up and the rest dims. A red blade runs down the four
# gaps where the pipeline really cut; the strip parts there, the unchosen pieces drop to the floor and go dark, and
# each chosen stretch slides together into a deck (its thickness is its length), rolls upright into 9:16 and turns
# into the vertical picture the clipper makes from it.
#
#   blender -b -P scripts/landing_film/film.py -- --inputs prep_out/ --camera desk --start 1 --end 32 --out frames/
#       [--stills 1,48,128] [--res 1280x720] [--samples 48] [--look '{"emit": 1.6}'] [--quick] [--dry]
#
# Every animated value is a pure function of the frame, baked as one key per frame, and the footage is a pure
# function of the frame too: any frame range renders exactly as it would inside a full run.
import argparse
import json
import math
import os
import sys

import bmesh
import bpy
from mathutils import Vector

HERE = os.path.dirname(os.path.abspath(__file__))
argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
ap = argparse.ArgumentParser()
ap.add_argument("--config", default=os.path.join(HERE, "film.json"))
ap.add_argument("--inputs", default="", help="prep.py's output folder (cells.json, atlas/, faces/)")
ap.add_argument("--camera", default="desk", choices=["desk", "phone"])
ap.add_argument("--start", type=int, default=1)
ap.add_argument("--end", type=int, default=0)
ap.add_argument("--stills", default="")
ap.add_argument("--res", default="")
ap.add_argument("--samples", type=int, default=0)
ap.add_argument("--out", default="/tmp/film")
ap.add_argument("--look", default="{}")
ap.add_argument("--quick", action="store_true", help="tiny, noisy, no haze: a smoke test")
ap.add_argument("--dry", action="store_true", help="build and report, render nothing")
args = ap.parse_args(argv)

CFG = json.load(open(args.config, encoding="utf-8"))
LOOK = {
    "exposure": 0.0,
    "emit": 2.3,          # footage light, W/m2-ish
    "haze": 0.016,        # volume density (also a little aerial perspective down the strip)
    "aniso": 0.45,        # haze scatters forward
    "top": 900.0,         # softbox over the strip: lights only the glass (light linking), for its top edges
    "rim": 900.0,         # behind and above, glass only: outlines the tile edges
    "sheen": 160.0,       # a big dim softbox behind the camera, seen only as a reflection in the glass faces
    "deck": 110.0,        # two strip lights that find the decks' layered edges in scene 3
    "floor_rough": 0.13,  # black lacquer: the strip reflects in it
    "glass_rough": 0.08,
    "fstop": 2.0,
    "glare": -0.5,
    "vignette": 0.62,
    "hair": 2.0,          # sentence hairlines
    "air": 6000.0,        # soft spots that light only the haze, from behind toward the camera (forward scatter)
    "blade": 140.0,
    "warm": [1.0, 0.86, 0.7],
}
LOOK.update(CFG.get("look") or {})
LOOK.update(json.loads(args.look or "{}"))
print("look:", json.dumps(LOOK), flush=True)

FILM = CFG["film"]
F = FILM["frames"]
FPS = FILM["fps"]
B = FILM["beats"]
FACE_FROM = FILM["face_from"]

if args.inputs and os.path.isfile(os.path.join(args.inputs, "cells.json")):
    LAYOUT = json.load(open(os.path.join(args.inputs, "cells.json"), encoding="utf-8"))
else:  # smoke test without prep: the same layout rule, no pictures
    sys.path.insert(0, HERE)
    from layout import COLS, cells_from  # noqa: E402

    cells_ = cells_from(CFG)
    LAYOUT = {"cols": COLS, "rows": math.ceil(len(cells_) / COLS), "tile": [512, 288], "frames": F,
              "cells": cells_, "clips": CFG["clips"], "face_from": FACE_FROM}
CELLS = LAYOUT["cells"]
N = len(CELLS)

# ── Geometry ───────────────────────────────────────────────────────────────────────────────────────
W, H, D = 1.6, 0.9, 0.05          # a tile: 16:9 face, 5 cm of glass
GAP = 0.035
PITCH = W + GAP
Z0 = 1.3                          # strip centre height; the floor is z = 0
STACK = D + 0.004                 # deck spacing


def cell_x(i):
    return i * PITCH + W / 2


def gap_x(i):                     # the gap after cell i
    return (i + 1) * PITCH - GAP / 2


# Pieces: runs of tiles between the cuts. A piece is chosen when its tiles belong to a kept clip.
cut_after = [c["i"] for c in CELLS if c["gap_after"] == "cut"]
pieces, start = [], 0
for g in cut_after + [N - 1]:
    tiles = list(range(start, g + 1))
    clip = CELLS[tiles[0]]["clip"]
    pieces.append({"tiles": tiles, "clip": clip if all(CELLS[t]["clip"] == clip for t in tiles) else -1})
    start = g + 1
CHOSEN = [p for p in pieces if p["clip"] >= 0]
for p in pieces:
    p["x0"] = (cell_x(p["tiles"][0]) + cell_x(p["tiles"][-1])) / 2
print(f"tiles {N}; pieces {[ (p['tiles'][0], p['tiles'][-1], p['clip']) for p in pieces]}", flush=True)
assert len(CHOSEN) == len(CFG["clips"]) == 2, "the film is choreographed for two kept clips"

# ── Time helpers ───────────────────────────────────────────────────────────────────────────────────


def clamp01(t):
    return max(0.0, min(1.0, t))


def lin(f, a, b):
    return clamp01((f - a) / (b - a)) if b != a else float(f >= b)


def smooth(t):
    t = clamp01(t)
    return t * t * (3 - 2 * t)


def ease(t):                      # cubic in-out
    t = clamp01(t)
    return 4 * t * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def ease_out(t):
    t = clamp01(t)
    return 1 - (1 - t) ** 3


def settle(t, over=0.06):         # eases out past the mark and comes back: weight, not bounce
    t = clamp01(t)
    s = 1 - (1 - t) ** 3
    return s + over * math.sin(math.pi * t) * (1 - t) * 2.2


def mix(a, b, t):
    return a + (b - a) * t


def vmix(a, b, t):
    return Vector(a).lerp(Vector(b), t)


# ── Choreography (frames) ──────────────────────────────────────────────────────────────────────────
BLADE0 = B["cut"][0]              # first blade
BLADE_LEN = 6                     # frames per blade
# The blades fall far to near: the camera is pulling back as they fall, so the nearest gap comes into frame last,
# and the last blade lands closest to the lens. (cuts stays in strip order: piece k sits between cut k-1 and cut k.)
FALL_ORDER = sorted(cut_after, reverse=True)
cuts = [{"gap": g, "f0": BLADE0 + FALL_ORDER.index(g) * BLADE_LEN, "x": gap_x(g)} for g in cut_after]
for c in cuts:
    c["mid"] = c["f0"] + BLADE_LEN / 2
WARM = (96, 116)                  # the chosen stretches warm up
HAIR = (62, 112)                  # hairlines come up along the strip
DROP = {0: 166, 2: 170, 4: 168}   # piece index -> frame its fall starts
FALL = 12
LIFT = (162, 182)
GATHER_Y = (180, 189)
GATHER_X = (188, 212)
ROLL = (210, 233)
FACE = (228, 231)                 # the face changes inside a brief dip of the screen's light
DIP = (223, 229, 236)
GLIDE = (172, 236)
CENTRE = 24.0
DECK_Z = 1.08                     # a deck's centre: its foot hangs 28 cm over the lacquer, close enough to reflect
FINAL = {0: Vector((CENTRE - 0.84, -3.0, DECK_Z)), 1: Vector((CENTRE + 0.84, -3.0, DECK_Z))}
YAW = {0: math.radians(13), 1: math.radians(-13)}


def piece_transform(p_index, f):
    """World location and rotation of a piece's root at frame f."""
    p = pieces[p_index]
    loc = Vector((p["x0"], 0.0, Z0))
    rot = Vector((0.0, 0.0, 0.0))
    # the cuts push neighbours apart
    for k, c in enumerate(cuts):
        push = 0.13 * settle(lin(f, c["mid"], c["mid"] + 10))
        if p_index == k:
            loc.x -= push
        elif p_index == k + 1:
            loc.x += push
    if p["clip"] < 0:             # dropped: falls to the floor, lands with a small give
        f0 = DROP.get(p_index, 168)
        t = lin(f, f0, f0 + FALL)
        z = mix(Z0, H / 2, t * t)
        if f > f0 + FALL:
            z = H / 2 + 0.022 * math.sin(math.pi * lin(f, f0 + FALL, f0 + FALL + 7))
        loc.z = z
        loc.y += 2.2 * ease(lin(f, f0 + FALL + 6, f0 + FALL + 40))
        return loc, rot
    k = p["clip"]
    lift = ease(lin(f, *LIFT))
    loc.z += 0.22 * lift
    loc.y -= 0.55 * lift
    g = ease(lin(f, *GLIDE))
    loc = loc.lerp(FINAL[k], g)
    rot.y = -math.pi / 2 * settle(lin(f, *ROLL), 0.035)
    rot.z = YAW[k] * ease(lin(f, ROLL[0] + 4, ROLL[1] + 3))
    return loc, rot


def tile_local(p, j, f):
    """A tile's place inside its piece: in a row, then stepped back and slid together into a deck."""
    n = len(p["tiles"])
    x0 = (j - (n - 1) / 2) * PITCH
    if p["clip"] < 0:
        return Vector((x0, 0, 0))
    depth = (n - 1 - j) * STACK   # the last tile ends in front
    y = depth * ease(lin(f, *GATHER_Y))
    x = x0 * (1 - ease(lin(f, *GATHER_X)))
    return Vector((x, y, 0))


def power_on(i, f):
    """The intro: the strip lights up tile by tile, the start of the video first."""
    on = -3.0 + i * 0.95
    return smooth((f - on) / 7.0)


def tile_state(i, f):
    """(power, warm, saturation, face) for tile i at frame f."""
    c = CELLS[i]
    pw = power_on(i, f)
    w = smooth(lin(f, *WARM))
    if c["clip"] >= 0:
        pw *= mix(1.0, 1.28, w)
        warm = w
        sat = 1.0
    else:
        pw *= mix(1.0, 0.3, w)
        warm = 0.0
        sat = mix(1.0, 0.4, w)
        p = next(k for k, q in enumerate(pieces) if i in q["tiles"])
        f0 = DROP.get(p, 168)
        pw *= mix(1.0, 0.16, smooth(lin(f, f0 + FALL + 1, f0 + FALL + 16)))
    face = 0.0
    for p in CHOSEN:
        if i == p["tiles"][-1]:
            face = smooth(lin(f, *FACE))
            dip = smooth(lin(f, DIP[0], DIP[1])) * (1 - smooth(lin(f, DIP[1], DIP[2])))
            pw *= (1 - 0.8 * dip) * mix(1.0, 1.35, face)  # a finished Short glows a little brighter
    return pw, warm, sat, face


# Hairlines: one in every gap where a sentence ends.
hairs = [{"gap": c["i"], "cut": c["gap_after"] == "cut", "x": gap_x(c["i"])} for c in CELLS if c["gap_after"]]
xs = [h["x"] for h in hairs]
for h in hairs:
    h["on"] = mix(HAIR[0], HAIR[1], (h["x"] - min(xs)) / (max(xs) - min(xs)))
    h["piece"] = next(k for k, p in enumerate(pieces) if h["gap"] in p["tiles"][:-1]) if not h["cut"] else -1


def hair_state(h, f):
    """(glow, red) for a hairline at frame f."""
    glow = smooth((f - h["on"]) / 6.0)
    w = smooth(lin(f, *WARM))
    if h["cut"]:
        glow *= mix(1.0, 1.25, w)
        c = next(c for c in cuts if c["gap"] == h["gap"])
        hit = smooth(lin(f, c["mid"] - 1, c["mid"] + 1))
        glow = mix(glow, 3.0, hit) * (1 - smooth(lin(f, c["mid"] + 2, c["mid"] + 18)))
        return glow, hit
    glow *= mix(1.0, 0.32, w)
    p = pieces[h["piece"]]
    end = (DROP.get(h["piece"], 168) + 6) if p["clip"] < 0 else GATHER_Y[0]
    glow *= 1 - smooth(lin(f, end - 8, end))
    return glow, 0.0


# ── Cameras ────────────────────────────────────────────────────────────────────────────────────────
# Keys: (frame, position, target, lens mm). Between two keys of a plateau (a beat's rest) the camera drifts at a
# steady speed; between the others it eases, matching the drift speeds at either end, so it never stops dead.
RIGS = {
    "desk": [
        (1, (-2.5, -3.7, 0.72), (4.6, 0.0, 1.3), 28),
        (B["s1"][0], (-1.35, -2.75, 0.92), (5.4, 0.0, 1.28), 30),
        (B["s1"][1], (-1.05, -2.6, 0.95), (5.8, 0.0, 1.28), 30),
        (88, (2.0, -3.7, 1.15), (10.5, 0.0, 1.25), 34),
        (B["s2"][0], (6.6, -3.5, 1.32), (16.2, 0.0, 1.24), 35),
        (B["s2"][1], (6.95, -3.55, 1.33), (16.6, 0.0, 1.24), 35),
        (162, (2.8, -7.0, 1.65), (16.8, 0.0, 1.15), 40),
        (190, (13.0, -8.8, 1.95), (22.8, -1.6, 1.3), 40),
        (214, (20.6, -9.6, 1.6), (23.7, -2.6, 1.05), 38),
        (B["s3"][0], (23.6, -9.3, 1.3), (23.75, -3.0, 0.92), 36),
        (B["s3"][1], (23.62, -8.9, 1.28), (23.75, -3.0, 0.92), 36),
    ],
    "phone": [
        (1, (-1.6, -3.2, 0.6), (4.0, 0.0, 1.35), 22),
        (B["s1"][0], (-0.9, -2.45, 0.82), (4.4, 0.0, 1.3), 23),
        (B["s1"][1], (-0.7, -2.3, 0.85), (4.7, 0.0, 1.3), 23),
        (88, (2.4, -3.4, 1.2), (10.0, 0.0, 1.25), 24),
        (B["s2"][0], (8.2, -2.05, 1.0), (21.0, 0.0, 1.32), 28),
        (B["s2"][1], (8.5, -2.0, 1.02), (21.4, 0.0, 1.32), 28),
        (162, (2.6, -5.2, 1.5), (18.6, 0.0, 1.2), 24),
        (190, (13.5, -9.0, 2.1), (23.0, -1.6, 1.3), 30),
        (214, (21.0, -9.6, 1.7), (23.9, -2.6, 1.1), 30),
        (B["s3"][0], (24.0, -7.9, 1.3), (24.0, -3.0, 0.98), 30),
        (B["s3"][1], (24.0, -7.6, 1.28), (24.0, -3.0, 0.98), 30),
    ],
}
PLATEAUS = [tuple(B["s1"]), tuple(B["s2"]), tuple(B["s3"])]


def rig_at(keys, f):
    """Hermite through the keys: drift segments are linear, the moves between them ease to match."""
    ts = [k[0] for k in keys]
    vals = [[*k[1], *k[2], k[3]] for k in keys]
    if f <= ts[0]:
        v = vals[0]
    elif f >= ts[-1]:
        v = vals[-1]
    else:
        def is_plateau(a, b):
            return (a, b) in PLATEAUS

        def slope(k):
            outs = []
            if k > 0 and is_plateau(ts[k - 1], ts[k]):
                outs.append([(vals[k][d] - vals[k - 1][d]) / (ts[k] - ts[k - 1]) for d in range(7)])
            if k < len(ts) - 1 and is_plateau(ts[k], ts[k + 1]):
                outs.append([(vals[k + 1][d] - vals[k][d]) / (ts[k + 1] - ts[k]) for d in range(7)])
            if outs:
                return [sum(o[d] for o in outs) / len(outs) for d in range(7)]
            if k == 0 or k == len(ts) - 1:
                return [0.0] * 7
            return [(vals[k + 1][d] - vals[k - 1][d]) / (ts[k + 1] - ts[k - 1]) for d in range(7)]

        k = max(i for i in range(len(ts) - 1) if ts[i] <= f)
        a, b = ts[k], ts[k + 1]
        if is_plateau(a, b):
            v = [mix(vals[k][d], vals[k + 1][d], (f - a) / (b - a)) for d in range(7)]
        else:
            t = (f - a) / (b - a)
            h00, h10, h01, h11 = 2 * t**3 - 3 * t**2 + 1, t**3 - 2 * t**2 + t, -2 * t**3 + 3 * t**2, t**3 - t**2
            m0, m1 = slope(k), slope(k + 1)
            v = [h00 * vals[k][d] + h10 * (b - a) * m0[d] + h01 * vals[k + 1][d] + h11 * (b - a) * m1[d]
                 for d in range(7)]
    return Vector(v[0:3]), Vector(v[3:6]), v[6]


# ── Scene ──────────────────────────────────────────────────────────────────────────────────────────
bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene
sc.render.engine = "CYCLES"
sc.cycles.device = "CPU"
sc.cycles.samples = args.samples or CFG["render"]["samples"]
sc.cycles.use_adaptive_sampling = True
sc.cycles.adaptive_threshold = 0.02
sc.cycles.use_denoising = True
sc.cycles.denoiser = "OPENIMAGEDENOISE"
sc.cycles.max_bounces = 6
sc.cycles.diffuse_bounces = 2
sc.cycles.glossy_bounces = 3
sc.cycles.transmission_bounces = 2
sc.cycles.volume_bounces = 0
sc.cycles.transparent_max_bounces = 8
sc.cycles.caustics_reflective = False
sc.cycles.caustics_refractive = False
sc.cycles.sample_clamp_indirect = 4.0
sc.cycles.seed = 7
res = args.res or CFG["render"]["res"][args.camera]
w_, h_ = (int(x) for x in res.split("x"))
sc.render.resolution_x, sc.render.resolution_y = w_, h_
sc.render.resolution_percentage = 100
sc.render.fps = FPS
sc.frame_start, sc.frame_end = 1, F
sc.render.image_settings.file_format = "PNG"
sc.render.image_settings.color_mode = "RGB"
sc.render.image_settings.color_depth = "8"
sc.render.dither_intensity = 1.0
sc.view_settings.view_transform = "AgX"
sc.view_settings.look = "AgX - Medium High Contrast"
sc.view_settings.exposure = LOOK["exposure"]
sc.render.film_transparent = False
sc.render.use_motion_blur = True
sc.render.motion_blur_shutter = 0.45
vl = bpy.context.view_layer
for name, kind in (("nrm", "COLOR"), ("gls", "VALUE")):
    aov = vl.aovs.add()
    aov.name, aov.type = name, kind

world = bpy.data.worlds.new("Stage")
sc.world = world
world.use_nodes = True
world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.0005, 0.0005, 0.0006, 1)


def sock(node, ident):
    return next(s for s in node.inputs if s.identifier == ident)


def osock(node, ident):
    return next(s for s in node.outputs if s.identifier == ident)


def aov_outputs(nt, gloss):
    """Camera-space normal and a glossiness weight, written beside the picture: the page's pointer light uses them
    to put a highlight where a real light would catch the glass."""
    geo = nt.nodes.new("ShaderNodeNewGeometry")
    vt = nt.nodes.new("ShaderNodeVectorTransform")
    vt.vector_type, vt.convert_from, vt.convert_to = "NORMAL", "WORLD", "CAMERA"
    nt.links.new(geo.outputs["Normal"], vt.inputs["Vector"])
    ma = nt.nodes.new("ShaderNodeVectorMath")
    ma.operation = "MULTIPLY_ADD"
    ma.inputs[1].default_value = (0.5, 0.5, 0.5)
    ma.inputs[2].default_value = (0.5, 0.5, 0.5)
    nt.links.new(vt.outputs["Vector"], ma.inputs[0])
    o1 = nt.nodes.new("ShaderNodeOutputAOV")
    o1.aov_name = "nrm"
    nt.links.new(ma.outputs["Vector"], o1.inputs["Color"])
    o2 = nt.nodes.new("ShaderNodeOutputAOV")
    o2.aov_name = "gls"
    o2.inputs["Value"].default_value = gloss


def seq_image(path, start, count):
    img = bpy.data.images.load(path)
    img.source = "SEQUENCE"
    img.colorspace_settings.name = "sRGB"
    return img, start, count


atlas_dir = os.path.join(args.inputs, "atlas") if args.inputs else ""
# Blender puts the frame number into whichever file of the sequence is loaded, so any one will do (a stills run
# only has the atlases of its stills).
atlas_files = sorted(x for x in os.listdir(atlas_dir) if x.startswith("a") and x.endswith(".jpg"))     if atlas_dir and os.path.isdir(atlas_dir) else []
if atlas_files:
    ATLAS = seq_image(os.path.join(atlas_dir, atlas_files[0]), 1, F)
else:
    g = bpy.data.images.new("grid", 1024, 1024)
    g.generated_type = "COLOR_GRID"
    ATLAS = (g, 1, 1)
FACES = []
for ci in range(len(CFG["clips"])):
    fp = os.path.join(args.inputs, "faces", f"c{ci}", "v0001.jpg") if args.inputs else ""
    if fp and os.path.isfile(fp):
        FACES.append(seq_image(fp, FACE_FROM, F - FACE_FROM + 1))
    else:
        g = bpy.data.images.new(f"face{ci}", 540, 960)
        g.generated_type = "UV_GRID"
        FACES.append((g, 1, 1))


def tex_node(nt, spec, uv_out=None):
    img, start, count = spec
    t = nt.nodes.new("ShaderNodeTexImage")
    t.image = img
    t.interpolation = "Cubic"
    t.extension = "EXTEND"
    if img.source == "SEQUENCE":
        t.image_user.frame_start = start
        t.image_user.frame_duration = count
        t.image_user.frame_offset = 0
        t.image_user.use_auto_refresh = True
        t.image_user.use_cyclic = False
    if uv_out is not None:
        nt.links.new(uv_out, t.inputs["Vector"])
    return t


def screen_mat(name, face=None):
    """The front of a tile: the footage as emitted light behind polished glass. Object colour carries the tile's
    state: R power/2, G warmth, B saturation, A how far the vertical face has replaced it."""
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    pb = nt.nodes["Principled BSDF"]
    pb.inputs["Base Color"].default_value = (0.0, 0.0, 0.0, 1)
    pb.inputs["Roughness"].default_value = 0.06
    pb.inputs["Specular IOR Level"].default_value = 0.5
    info = nt.nodes.new("ShaderNodeObjectInfo")
    sep = nt.nodes.new("ShaderNodeSeparateColor")
    nt.links.new(info.outputs["Color"], sep.inputs["Color"])
    uv = nt.nodes.new("ShaderNodeUVMap")
    uv.uv_map = "atlas"
    tex = tex_node(nt, ATLAS, uv.outputs["UV"])
    col = tex.outputs["Color"]
    if face is not None:
        luv = nt.nodes.new("ShaderNodeUVMap")
        luv.uv_map = "local"
        sx = nt.nodes.new("ShaderNodeSeparateXYZ")
        nt.links.new(luv.outputs["UV"], sx.inputs["Vector"])
        inv = nt.nodes.new("ShaderNodeMath")
        inv.operation = "SUBTRACT"
        inv.inputs[0].default_value = 1.0
        nt.links.new(sx.outputs["Y"], inv.inputs[1])
        cx = nt.nodes.new("ShaderNodeCombineXYZ")
        nt.links.new(inv.outputs[0], cx.inputs["X"])       # rolled upright: picture x = 1 - v
        nt.links.new(sx.outputs["X"], cx.inputs["Y"])      # picture y = u
        ftex = tex_node(nt, face, cx.outputs["Vector"])
        fm = nt.nodes.new("ShaderNodeMix")
        fm.data_type = "RGBA"
        nt.links.new(info.outputs["Alpha"], sock(fm, "Factor_Float"))
        nt.links.new(col, sock(fm, "A_Color"))
        nt.links.new(ftex.outputs["Color"], sock(fm, "B_Color"))
        col = osock(fm, "Result_Color")
    hs = nt.nodes.new("ShaderNodeHueSaturation")
    nt.links.new(col, hs.inputs["Color"])
    nt.links.new(sep.outputs["Blue"], hs.inputs["Saturation"])
    tint = nt.nodes.new("ShaderNodeMix")
    tint.data_type = "RGBA"
    tint.blend_type = "MULTIPLY"
    nt.links.new(sep.outputs["Green"], sock(tint, "Factor_Float"))
    nt.links.new(hs.outputs["Color"], sock(tint, "A_Color"))
    sock(tint, "B_Color").default_value = (*LOOK["warm"], 1)
    nt.links.new(osock(tint, "Result_Color"), pb.inputs["Emission Color"])
    st = nt.nodes.new("ShaderNodeMath")
    st.operation = "MULTIPLY"
    st.inputs[1].default_value = 2.0 * LOOK["emit"]
    nt.links.new(sep.outputs["Red"], st.inputs[0])
    nt.links.new(st.outputs[0], pb.inputs["Emission Strength"])
    aov_outputs(nt, 0.55)
    return m


def glass_mat():
    m = bpy.data.materials.new("Glass")
    m.use_nodes = True
    nt = m.node_tree
    pb = nt.nodes["Principled BSDF"]
    pb.inputs["Base Color"].default_value = (0.006, 0.006, 0.007, 1)
    pb.inputs["Roughness"].default_value = LOOK["glass_rough"]
    pb.inputs["Coat Weight"].default_value = 0.6
    pb.inputs["Coat Roughness"].default_value = 0.03
    aov_outputs(nt, 1.0)
    return m


def floor_mat():
    """Black lacquer with a little life in its roughness, so reflections soften unevenly, as real ones do."""
    m = bpy.data.materials.new("Floor")
    m.use_nodes = True
    nt = m.node_tree
    pb = nt.nodes["Principled BSDF"]
    pb.inputs["Base Color"].default_value = (0.008, 0.008, 0.009, 1)
    nz = nt.nodes.new("ShaderNodeTexNoise")
    nz.inputs["Scale"].default_value = 1.6
    nz.inputs["Detail"].default_value = 6.0
    mr = nt.nodes.new("ShaderNodeMapRange")
    mr.inputs["To Min"].default_value = LOOK["floor_rough"] - 0.07
    mr.inputs["To Max"].default_value = LOOK["floor_rough"] + 0.09
    nt.links.new(nz.outputs["Fac"], mr.inputs["Value"])
    nt.links.new(mr.outputs["Result"], pb.inputs["Roughness"])
    bn = nt.nodes.new("ShaderNodeTexNoise")
    bn.inputs["Scale"].default_value = 70.0
    bump = nt.nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.025
    nt.links.new(bn.outputs["Fac"], bump.inputs["Height"])
    nt.links.new(bump.outputs["Normal"], pb.inputs["Normal"])
    aov_outputs(nt, 0.35)
    return m


def emit_mat(name, color_from_object, strength, base, red=(1.0, 0.06, 0.04)):
    """Hairlines (object colour R = glow/4, G = how red) and the blade."""
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    em = nt.nodes.new("ShaderNodeEmission")
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(em.outputs["Emission"], out.inputs["Surface"])
    if color_from_object:
        info = nt.nodes.new("ShaderNodeObjectInfo")
        sep = nt.nodes.new("ShaderNodeSeparateColor")
        nt.links.new(info.outputs["Color"], sep.inputs["Color"])
        mx = nt.nodes.new("ShaderNodeMix")
        mx.data_type = "RGBA"
        nt.links.new(sep.outputs["Green"], sock(mx, "Factor_Float"))
        sock(mx, "A_Color").default_value = (*base, 1)
        sock(mx, "B_Color").default_value = (*red, 1)
        nt.links.new(osock(mx, "Result_Color"), em.inputs["Color"])
        st = nt.nodes.new("ShaderNodeMath")
        st.operation = "MULTIPLY"
        st.inputs[1].default_value = 4.0 * strength
        nt.links.new(sep.outputs["Red"], st.inputs[0])
        nt.links.new(st.outputs[0], em.inputs["Strength"])
    else:
        em.inputs["Color"].default_value = (*red, 1)
        em.inputs["Strength"].default_value = strength
    aov_outputs(nt, 0.0)
    return m


GLASS = glass_mat()
INK = (1.0, 0.93, 0.84)
HAIR_M = emit_mat("Hairline", True, LOOK["hair"], INK)
BLADE_M = emit_mat("Blade", False, LOOK["blade"], INK)
SCREEN = screen_mat("Screen")
HERO = {p["tiles"][-1]: screen_mat(f"Hero{p['clip']}", FACES[p["clip"]]) for p in CHOSEN}


def tile_mesh(i):
    """A 16:9 slab, face toward -Y. Two UV maps on the face: its tile in the atlas, and 0..1 across the face."""
    cols, rows = LAYOUT["cols"], LAYOUT["rows"]
    col, row = i % cols, i // cols
    u0, u1 = col / cols, (col + 1) / cols
    v0, v1 = 1 - (row + 1) / rows, 1 - row / rows
    bm = bmesh.new()
    hw, hd, hh = W / 2, D / 2, H / 2
    v = [bm.verts.new(p) for p in [(-hw, -hd, -hh), (hw, -hd, -hh), (hw, -hd, hh), (-hw, -hd, hh),
                                   (-hw, hd, -hh), (hw, hd, -hh), (hw, hd, hh), (-hw, hd, hh)]]
    faces = [
        ((0, 1, 2, 3), 0),        # front (-Y)
        ((5, 4, 7, 6), 1),        # back
        ((4, 0, 3, 7), 1),        # left
        ((1, 5, 6, 2), 1),        # right
        ((3, 2, 6, 7), 1),        # top
        ((4, 5, 1, 0), 1),        # bottom
    ]
    uv_a = bm.loops.layers.uv.new("atlas")
    uv_l = bm.loops.layers.uv.new("local")
    for idx, mat in faces:
        fc = bm.faces.new([v[k] for k in idx])
        fc.material_index = mat
        fc.smooth = True
        if mat == 0:
            for loop, (a, b) in zip(fc.loops, [(u0, v0), (u1, v0), (u1, v1), (u0, v1)]):
                loop[uv_a].uv = (a, b)
            for loop, (a, b) in zip(fc.loops, [(0, 0), (1, 0), (1, 1), (0, 1)]):
                loop[uv_l].uv = (a, b)
    me = bpy.data.meshes.new(f"tile{i}")
    bm.to_mesh(me)
    bm.free()
    return me


def new_object(name, data=None):
    ob = bpy.data.objects.new(name, data)
    sc.collection.objects.link(ob)
    return ob


roots, tiles, hair_obs, blades = [], {}, [], []
GLASS_SET = bpy.data.collections.new("GlassOnly")   # what the edge lights may touch
for k, p in enumerate(pieces):
    root = new_object(f"piece{k}")
    root.location = (p["x0"], 0, Z0)
    roots.append(root)
    for j, i in enumerate(p["tiles"]):
        me = tile_mesh(i)
        me.materials.append(HERO.get(i, SCREEN))
        me.materials.append(GLASS)
        ob = new_object(f"tile{i}", me)
        ob.parent = root
        bev = ob.modifiers.new("Bevel", "BEVEL")
        bev.width = 0.012
        bev.segments = 3
        bev.limit_method = "ANGLE"
        bev.material = 1
        bev.harden_normals = True
        tiles[i] = (ob, k, j)
        GLASS_SET.objects.link(ob)

hair_me = bpy.data.meshes.new("hair")
bm = bmesh.new()
bmesh.ops.create_cube(bm, size=1.0)
bm.to_mesh(hair_me)
bm.free()
hair_me.materials.append(HAIR_M)
for h in hairs:
    ob = new_object(f"hair{h['gap']}", hair_me)
    ob.scale = (0.0045, 0.01, H + 0.07)
    h["ob"] = ob
    if h["cut"]:
        ob.location = (h["x"], 0, Z0)
    else:
        p = pieces[h["piece"]]
        ob.parent = roots[h["piece"]]
        ob.location = (h["x"] - p["x0"], 0, 0)
    hair_obs.append(ob)

blade_me = bpy.data.meshes.new("blade")
bm = bmesh.new()
bmesh.ops.create_cube(bm, size=1.0)
bm.to_mesh(blade_me)
bm.free()
blade_me.materials.append(BLADE_M)
for c in cuts:
    ob = new_object(f"blade{c['gap']}", blade_me)
    ob.scale = (0.006, 0.04, 1.3)
    lamp = bpy.data.lights.new(f"bladelight{c['gap']}", "POINT")
    lamp.color = (1.0, 0.1, 0.06)
    lamp.shadow_soft_size = 0.05
    lamp.volume_factor = 0.15   # it reddens the glass and the floor; in the air it only tints (a full red fog read cheap)
    lo = new_object(f"bladelight{c['gap']}", lamp)
    lo.parent = ob
    c["ob"], c["lamp"] = ob, lamp

# The floor, lacquered black, and the air: a faint haze the light can be seen in.
bpy.ops.mesh.primitive_plane_add(size=400, location=(25, 0, 0))
floor = bpy.context.object
floor.data.materials.append(floor_mat())
if not args.quick:
    bpy.ops.mesh.primitive_cube_add(size=1, location=(25, -6, 3.0))
    haze = bpy.context.object
    haze.scale = (110, 60, 6.2)
    hm = bpy.data.materials.new("Haze")
    hm.use_nodes = True
    nt = hm.node_tree
    nt.nodes.clear()
    vol = nt.nodes.new("ShaderNodeVolumePrincipled")
    vol.inputs["Color"].default_value = (0.92, 0.92, 0.92, 1)
    vol.inputs["Density"].default_value = LOOK["haze"]
    vol.inputs["Anisotropy"].default_value = LOOK["aniso"]
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(vol.outputs["Volume"], out.inputs["Volume"])
    haze.data.materials.append(hm)
    for attr in ("visible_shadow",):
        setattr(haze, attr, False)
    AIR = []
    HAZE_SET = bpy.data.collections.new("AirOnly")
    HAZE_SET.objects.link(haze)
    # Soft light in the air only (light linking): columns of glow above and behind the strip, which the camera
    # passes as it moves, so the dark has depth. They light nothing solid.
    for k, x in enumerate((7.0, 22.0, 37.0)):
        L = bpy.data.lights.new(f"Air{k}", "SPOT")
        L.energy = LOOK["air"]
        L.spot_size = math.radians(30)
        L.spot_blend = 1.0
        L.shadow_soft_size = 0.8
        L.color = (1.0, 0.9, 0.78)
        ob = new_object(f"Air{k}", L)
        ob.location = (x, 7.0, 8.5)
        ob.rotation_euler = (Vector((x + 1.0, -2.5, 0.0)) - ob.location).to_track_quat("-Z", "Y").to_euler()
        ob.light_linking.receiver_collection = HAZE_SET
        AIR.append(L)


def area(name, energy, size, size_y, loc, aim, color):
    """An edge light for the glass alone: light linking keeps it off the floor and out of the haze, so the stage
    stays black and only the tiles' bevels catch it."""
    L = bpy.data.lights.new(name, "AREA")
    L.energy = energy
    L.shape = "RECTANGLE"
    L.size, L.size_y = size, size_y
    L.color = color
    ob = new_object(name, L)
    ob.location = loc
    ob.rotation_euler = (Vector(aim) - Vector(loc)).to_track_quat("-Z", "Y").to_euler()
    ob.visible_volume_scatter = False
    ob.visible_camera = False
    ob.light_linking.receiver_collection = GLASS_SET
    return ob


WARMWHITE = (1.0, 0.9, 0.8)
area("Top", LOOK["top"], 56, 1.2, (24, 0.9, 4.6), (24, 0.0, 1.3), WARMWHITE)
area("Rim", LOOK["rim"], 56, 0.8, (24, 4.5, 2.6), (24, 0.0, 1.3), (1.0, 0.95, 0.9))
# Seen only in the glass: a long dim softbox behind the cameras, so the faces carry a soft sheen as real glass does.
area("Sheen", LOOK["sheen"], 70, 5.0, (24, -16, 3.6), (24, 0.0, 1.0), WARMWHITE)
# Scene 3: two tall strips either side of the decks, so their stacked edges (a tile per few seconds) read as layers.
DECK_LIGHTS = [area(f"Deck{k}", 0.0, 0.9, 2.6, (CENTRE + side * 2.3, -1.7, DECK_Z + 0.3), (CENTRE + side * 0.84, -3.0, DECK_Z),
                    WARMWHITE) for k, side in ((0, -1), (1, 1))]

# Cameras: the one asked for is the active one.
cam_data = bpy.data.cameras.new("Cam")
cam_data.sensor_width = 36
cam_data.sensor_fit = "AUTO"
cam_data.dof.use_dof = True
cam_data.dof.aperture_fstop = LOOK["fstop"]
cam_data.dof.aperture_blades = 7
cam = new_object("Cam", cam_data)
target = new_object("Look")
trk = cam.constraints.new("TRACK_TO")
trk.target = target
trk.track_axis = "TRACK_NEGATIVE_Z"
trk.up_axis = "UP_Y"
cam_data.dof.focus_object = target
sc.camera = cam


# ── Bake: one key per frame (plus one either side, for motion blur) ────────────────────────────────
def key(ob, path, f):
    ob.keyframe_insert(path, frame=f)


for f in range(0, F + 2):
    pos, tgt, lens = rig_at(RIGS[args.camera], f)
    cam.location = pos
    target.location = tgt
    cam_data.lens = lens
    key(cam, "location", f)
    key(target, "location", f)
    cam_data.keyframe_insert("lens", frame=f)
    for k, root in enumerate(roots):
        loc, rot = piece_transform(k, f)
        root.location = loc
        root.rotation_euler = rot
        key(root, "location", f)
        key(root, "rotation_euler", f)
    for i, (ob, k, j) in tiles.items():
        ob.location = tile_local(pieces[k], j, f)
        key(ob, "location", f)
        pw, warm, sat, face = tile_state(i, f)
        ob.color = (pw / 2, warm, sat, face)
        key(ob, "color", f)
    for h in hairs:
        glow, red = hair_state(h, f)
        h["ob"].color = (glow / 4, red, 0, 1)
        key(h["ob"], "color", f)
    if args.camera == "phone" and not args.quick:
        # a tall frame looks up into the air behind the decks: ease the glow down as they form
        for L in AIR:
            L.energy = LOOK["air"] * mix(1.0, 0.55, smooth(lin(f, 150, 205)))
            L.keyframe_insert("energy", frame=f)
    for dl in DECK_LIGHTS:
        dl.data.energy = LOOK["deck"] * smooth(lin(f, GATHER_X[0], ROLL[1]))
        dl.data.keyframe_insert("energy", frame=f)
    for c in cuts:
        t = (f - c["f0"]) / BLADE_LEN
        live = 0.0 <= t <= 1.0
        c["ob"].location = (c["x"], 0, mix(Z0 + 2.2, Z0 - 2.0, clamp01(t)))
        c["ob"].scale = (0.006, 0.04, 1.3) if live else (0.0, 0.0, 0.0)
        key(c["ob"], "location", f)
        key(c["ob"], "scale", f)
        c["lamp"].energy = 900.0 * math.sin(math.pi * clamp01(t)) if live else 0.0
        c["lamp"].keyframe_insert("energy", frame=f)

animated = list(sc.objects) + [cam_data] + [c["lamp"] for c in cuts] + [dl.data for dl in DECK_LIGHTS]
for ob in animated + (AIR if not args.quick else []):
    ad = getattr(ob, "animation_data", None)
    if ad and ad.action:
        for fc in ad.action.fcurves:
            for kp in fc.keyframe_points:
                kp.interpolation = "LINEAR"
for c in cuts:                     # a blade is there or it isn't
    for fc in c["ob"].animation_data.action.fcurves:
        if fc.data_path == "scale":
            for kp in fc.keyframe_points:
                kp.interpolation = "CONSTANT"

# ── Compositing: a little bloom, a lens vignette, and the light pass written beside the picture ───
sc.use_nodes = True
ct = sc.node_tree
ct.nodes.clear()
rl = ct.nodes.new("CompositorNodeRLayers")
glare = ct.nodes.new("CompositorNodeGlare")
glare.glare_type = "FOG_GLOW"
glare.quality = "HIGH"
glare.threshold = 1.0
glare.size = 8
glare.mix = LOOK["glare"]
ct.links.new(rl.outputs["Image"], glare.inputs["Image"])
vig = ct.nodes.new("CompositorNodeEllipseMask")
vig.width, vig.height = 0.94, 0.9
vblur = ct.nodes.new("CompositorNodeBlur")
vblur.use_relative = True
vblur.factor_x = vblur.factor_y = 40.0
vblur.filter_type = "GAUSS"
vmix = ct.nodes.new("CompositorNodeMixRGB")
vmix.blend_type = "MULTIPLY"
vmix.inputs[0].default_value = LOOK["vignette"]
ct.links.new(vig.outputs["Mask"], vblur.inputs["Image"])
ct.links.new(glare.outputs["Image"], vmix.inputs[1])
ct.links.new(vblur.outputs["Image"], vmix.inputs[2])
comp = ct.nodes.new("CompositorNodeComposite")
ct.links.new(vmix.outputs["Image"], comp.inputs["Image"])
sepn = ct.nodes.new("CompositorNodeSeparateColor")
ct.links.new(rl.outputs["nrm"], sepn.inputs["Image"])
comb = ct.nodes.new("CompositorNodeCombineColor")
ct.links.new(sepn.outputs["Red"], comb.inputs["Red"])
ct.links.new(sepn.outputs["Green"], comb.inputs["Green"])
ct.links.new(rl.outputs["gls"], comb.inputs["Blue"])
fo = ct.nodes.new("CompositorNodeOutputFile")
fo.base_path = os.path.join(args.out, "data")
fo.format.file_format = "PNG"
fo.format.color_mode = "RGB"
fo.format.color_depth = "8"
fo.file_slots[0].path = "d"
ct.links.new(comb.outputs["Image"], fo.inputs[0])
# The light pass is data, not a picture: written raw (linear), or failing that through plain sRGB. encoding.txt
# says which, so the page decodes it right.
DATA_ENCODING = "scene"
try:
    fo.format.color_management = "OVERRIDE"
    for view in ("Raw", "Standard"):
        try:
            fo.format.view_settings.view_transform = view
            DATA_ENCODING = "linear" if view == "Raw" else "srgb"
            break
        except TypeError:
            continue
    fo.format.view_settings.look = "None"
except (AttributeError, TypeError) as exc:
    print("note: light pass uses the scene view transform:", exc, flush=True)
print("light pass encoding:", DATA_ENCODING, flush=True)

if args.quick:
    sc.cycles.samples = 6
    sc.cycles.use_denoising = False
    sc.render.resolution_x, sc.render.resolution_y = (384, 216) if args.camera == "desk" else (216, 384)

if args.dry:
    from bpy_extras.object_utils import world_to_camera_view
    for c in cuts:  # is every blade in frame while it falls? (x, y in 0..1 means on screen)
        for f in (c["f0"], c["mid"], c["f0"] + BLADE_LEN):
            sc.frame_set(int(round(f)))
            v = world_to_camera_view(sc, cam, Vector((c["x"], 0.0, Z0)))
            print(f"cut gap {c['gap']} frame {f}: screen x {v.x:.3f} y {v.y:.3f}", flush=True)
    for f in (1, B["s1"][0], B["s2"][0], cuts[0]["mid"], 200, 230, F):
        sc.frame_set(int(f))
        print(f"frame {int(f)}: cam {tuple(round(x, 2) for x in cam.matrix_world.translation)}; "
              + "; ".join(f"piece{k} {tuple(round(x, 2) for x in r.matrix_world.translation)}" for k, r in enumerate(roots)),
              flush=True)
    sys.exit(0)

os.makedirs(os.path.join(args.out, "data"), exist_ok=True)
with open(os.path.join(args.out, "data", "encoding.txt"), "w") as fh:
    fh.write(DATA_ENCODING + "\n")
frames = [int(x) for x in args.stills.split(",") if x] or list(range(args.start, (args.end or F) + 1))
import time  # noqa: E402

for f in frames:
    t0 = time.time()
    sc.frame_set(f)
    sc.render.filepath = os.path.join(args.out, f"f{f:04d}.png")
    bpy.ops.render.render(write_still=True)
    print(f"rendered frame {f} ({args.camera}, {sc.render.resolution_x}x{sc.render.resolution_y}, "
          f"{sc.cycles.samples} samples) in {time.time() - t0:.1f} s", flush=True)
