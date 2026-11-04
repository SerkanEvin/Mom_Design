"""MOD-03: agentic Blender editor with a fixed, whitelisted operation set (no arbitrary Python).

Each round: the planner (syn:large:vision) sees grid-labelled renders + stats and returns JSON ops ->
`blender_runner.py` applies them headless and renders the result -> colours are copied from the untouched
reference onto the new surface -> a separate check decides whether every request is satisfied.
Units are millimetres in the print frame (Z up, standing on z=0).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from . import llm, meshio
from .config import effort, get_settings
from .jobs import Job, JobFailed, add_version, version_path

RUNNER = Path(__file__).with_name("blender_runner.py")
MAX_OPS = 10
VIEWS = ("front", "right", "back", "top")


class BlenderError(Exception):
    pass


def _num(v, lo, hi, name):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise BlenderError(f"{name} sayı olmalı")
    if not lo <= x <= hi:
        raise BlenderError(f"{name} {lo}..{hi} aralığında olmalı")
    return x


def _vec(v, lo, hi, name, n=3):
    if not isinstance(v, (list, tuple)) or len(v) != n:
        raise BlenderError(f"{name} {n} sayılık liste olmalı")
    return [_num(x, lo, hi, name) for x in v]


def _anchor(a):
    if not isinstance(a, dict) or a.get("view") not in VIEWS:
        raise BlenderError("anchor: {view: front|right|back|top, u: 0..100, v: 0..100}")
    return {"view": a["view"], "u": _num(a.get("u"), 0, 100, "u"), "v": _num(a.get("v"), 0, 100, "v")}


PRIMS = {"cube", "cylinder", "sphere", "cone", "torus"}


def validate_ops(ops, internal: bool = False, pins: dict | None = None) -> list[dict]:
    """`pins` ({note number: [x, y, z] mm}) enables {"op": "erase_note", "note": n, "radius_mm": r}: the eraser
    at the exact spot the user pinned (becomes erase_sphere)."""
    if not isinstance(ops, list) or len(ops) > MAX_OPS:
        raise BlenderError(f"en fazla {MAX_OPS} işlem")
    out = []
    remesh = 0
    for o in ops:
        if not isinstance(o, dict):
            raise BlenderError("işlem nesne olmalı")
        op = o.get("op")
        if op == "erase_note":
            try:
                n = int(o.get("note"))
            except (TypeError, ValueError):
                raise BlenderError("erase_note: note numarası gerekli")
            if not pins or n not in pins:
                raise BlenderError(f"erase_note: {n} numaralı notun işaretli yeri yok (ya da model taşındı)")
            out.append({"op": "erase_sphere", "center_mm": [round(float(x), 3) for x in pins[n]],
                        "radius_mm": _num(o.get("radius_mm", 4), 1, 25, "radius_mm"), "note": n})
        elif op == "scale":
            out.append({"op": op, "factor": _vec(o.get("factor"), 0.05, 20, "factor")})
        elif op == "rotate":
            out.append({"op": op, "degrees": _vec(o.get("degrees"), -360, 360, "degrees")})
        elif op == "translate":
            out.append({"op": op, "mm": _vec(o.get("mm"), -500, 500, "mm")})
        elif op in ("boolean_difference", "boolean_union", "add_primitive"):
            prim = o.get("shape")
            if prim not in PRIMS:
                raise BlenderError("shape: " + ", ".join(sorted(PRIMS)))
            d = {"op": op, "shape": prim, "size_mm": _vec(o.get("size_mm"), 0.2, 500, "size_mm"),
                 "rotation_deg": _vec(o.get("rotation_deg", [0, 0, 0]), -360, 360, "rotation_deg")}
            if o.get("anchor") is not None:
                d["anchor"] = _anchor(o["anchor"])
                d["depth_into_mm"] = _num(o.get("depth_into_mm", 0), -200, 200, "depth_into_mm")
            elif internal:
                d["center_mm"] = _vec(o.get("center_mm"), -500, 500, "center_mm")
            else:
                raise BlenderError(f"{op} bir anchor ile verilmeli (taban için add_base kullan)")
            out.append(d)
        elif op == "remesh_voxel":
            remesh += 1
            if remesh > 1:
                raise BlenderError("remesh_voxel bir planda en fazla bir kez")
            out.append({"op": op, "voxel_mm": _num(o.get("voxel_mm"), 0.3, 5, "voxel_mm")})
        elif op == "decimate":
            out.append({"op": op, "ratio": _num(o.get("ratio"), 0.1, 1, "ratio")})
        elif op == "subdivide":
            out.append({"op": op, "levels": int(_num(o.get("levels"), 1, 2, "levels"))})
        elif op == "smooth":
            out.append({"op": op, "iterations": int(_num(o.get("iterations"), 1, 10, "iterations")),
                        "factor": _num(o.get("factor", 0.5), 0, 1, "factor")})
        elif op == "solidify":
            out.append({"op": op, "thickness_mm": _num(o.get("thickness_mm"), 0.4, 20, "thickness_mm")})
        elif op == "mirror":
            if o.get("axis") not in ("x", "y", "z"):
                raise BlenderError("axis x|y|z")
            out.append({"op": op, "axis": o["axis"]})
        elif op == "emboss_text":
            text = str(o.get("text", "")).strip()[:60]
            if not text:
                raise BlenderError("text boş")
            d = {"op": op, "text": text, "anchor": _anchor(o.get("anchor")),
                 "size_mm": _num(o.get("size_mm", 6), 2, 60, "size_mm"),
                 "depth_mm": _num(o.get("depth_mm", 1.2), -5, 5, "depth_mm")}
            if o.get("max_width_mm") is not None:
                d["max_width_mm"] = _num(o["max_width_mm"], 5, 300, "max_width_mm")
            out.append(d)
        elif op == "add_base":
            if o.get("shape", "round") not in ("round", "square"):
                raise BlenderError("add_base shape round|square")
            out.append({"op": op, "shape": o.get("shape", "round"),
                        "height_mm": _num(o.get("height_mm", 4), 2, 30, "height_mm"),
                        "margin_mm": _num(o.get("margin_mm", 4), 0, 30, "margin_mm")})
        elif op == "base_text":
            text = str(o.get("text", "")).strip()[:60]
            if not text:
                raise BlenderError("text boş")
            out.append({"op": op, "text": text, "size_mm": _num(o.get("size_mm", 5), 3, 20, "size_mm"),
                        "depth_mm": _num(o.get("depth_mm", 1.0), -3, 3, "depth_mm")})
        elif op == "erase_sphere" and internal:
            out.append({"op": op, "center_mm": _vec(o.get("center_mm"), -500, 500, "center_mm"),
                        "radius_mm": _num(o.get("radius_mm"), 0.5, 25, "radius_mm")})
        elif op == "remove_loose_parts":
            out.append({"op": op, "min_fraction": _num(o.get("min_fraction", 0.02), 0.0001, 0.5, "min_fraction")})
        elif op == "drop_to_bed":
            out.append({"op": op})
        else:
            raise BlenderError(f"bilinmeyen işlem: {op}")
    return out


def run_blender(job: dict, workdir: Path, timeout_s: int = 420) -> dict:
    s = get_settings()
    if not Path(s.blender_bin).exists():
        raise BlenderError("Blender bu sunucuda kurulu değil.")
    spec = workdir / "blender_job.json"
    result = workdir / "blender_result.json"
    result.unlink(missing_ok=True)
    spec.write_text(json.dumps(job))
    env = {**os.environ, "OMP_NUM_THREADS": "4"}
    try:
        p = subprocess.run([s.blender_bin, "-b", "--factory-startup", "-noaudio", "--python", str(RUNNER), "--",
                            str(spec), str(result)], capture_output=True, text=True, timeout=timeout_s, env=env)
    except subprocess.TimeoutExpired:
        raise BlenderError("Blender zaman aşımına uğradı.")
    if not result.exists():
        tail = (p.stderr or p.stdout or "").strip().splitlines()
        raise BlenderError("Blender çalışmadı: " + (tail[-1] if tail else "bilinmeyen hata"))
    out = json.loads(result.read_text())
    if not out.get("ok"):
        raise BlenderError(out.get("error", "Blender hatası"))
    return out


def grid_overlay(src: Path, dst: Path, view: str, marks: list[dict] | None = None) -> Path:
    """Label the render with a 0..100 grid so the planner can point at places (u right, v down).
    `marks` are the user's numbered note pins visible in this view ({n, u, v})."""
    im = Image.open(src).convert("RGB")
    w, h = im.size
    d = ImageDraw.Draw(im, "RGBA")
    for k in range(10, 100, 10):
        x, y = w * k / 100, h * k / 100
        col = (255, 60, 60, 110) if k == 50 else (60, 60, 60, 60)
        d.line([(x, 0), (x, h)], fill=col, width=1)
        d.line([(0, y), (w, y)], fill=col, width=1)
        d.text((x + 2, 2), str(k), fill=(200, 30, 30, 255))
        d.text((2, y + 2), str(k), fill=(30, 30, 200, 255))
    for m in marks or []:
        x, y, r = w * m["u"] / 100, h * m["v"] / 100, max(7, w // 50)
        d.ellipse([x - r, y - r, x + r, y + r], fill=(214, 40, 160, 210), outline=(255, 255, 255, 255), width=2)
        d.text((x - 3 * len(str(m["n"])), y - 6), str(m["n"]), fill=(255, 255, 255, 255))
    d.text((w - 70, h - 14), view.upper(), fill=(0, 0, 0, 255))
    im.save(dst)
    return dst


def _views_content(paths: list[str], workdir: Path, tag: str, marks: dict | None = None) -> list[dict]:
    content = []
    for p in paths:
        view = Path(p).stem
        g = grid_overlay(Path(p), workdir / f"{tag}_{view}_grid.png", view, (marks or {}).get(view))
        content.append({"type": "text", "text": f"{view.upper()} view:"})
        content.append(llm.image_block(g, 448))
    return content


# camera axes of each render, as in blender_runner.VIEWS (image right, image up, viewing direction)
VIEW_AXES = {
    "front": ((1, 0, 0), (0, 0, 1), (0, 1, 0)),
    "right": ((0, 1, 0), (0, 0, 1), (-1, 0, 0)),
    "back": ((-1, 0, 0), (0, 0, 1), (0, -1, 0)),
    "top": ((1, 0, 0), (0, 1, 0), (0, 0, -1)),
}


MOVING_OPS = {"add_base", "scale", "rotate", "translate", "mirror", "drop_to_bed", "remesh_voxel"}


def to_print_mm(version_file: Path, up: str, height: float, points) -> tuple[np.ndarray, "object"]:
    """Viewer points (glTF coords of the version file) -> print-frame mm, same transform as Blender's normalise.
    Also returns the version mesh in that frame (for visibility tests)."""
    import trimesh
    pts = np.asarray(points, dtype=float).reshape(-1, 3)
    m = meshio.geometry_only(version_file)
    v = m.vertices.copy()
    if up == "y":
        R = meshio.Y_TO_Z[:3, :3]
        v, pts = v @ R.T, pts @ R.T
    lo, hi = v.min(axis=0), v.max(axis=0)
    s = height / (hi[2] - lo[2])
    v, pts = v * s, pts * s
    lo, hi = v.min(axis=0), v.max(axis=0)
    off = np.array([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])
    return pts + off, trimesh.Trimesh(v + off, m.faces, process=False)


def project_pins(pins: list[dict], frame: dict, mesh=None) -> dict:
    """Where each pin ({n, mm}) appears in each render (u, v in 0..100), keeping only views where it is not
    hidden behind other surface. `frame` is the render framing {c, size} reported by the runner."""
    c, size = np.asarray(frame["c"], dtype=float), float(frame["size"])
    out: dict[str, list] = {}
    for view, (right, upv, dirv) in VIEW_AXES.items():
        right, upv, dirv = map(np.asarray, (right, upv, dirv))
        for p in pins:
            q = np.asarray(p["mm"], dtype=float) - c
            u, v = (q @ right / size + 0.5) * 100, (0.5 - q @ upv / size) * 100
            if not (0 <= u <= 100 and 0 <= v <= 100):
                continue
            if mesh is not None:
                origin = np.asarray(p["mm"], dtype=float) - dirv * size * 2
                locs, _, _ = mesh.ray.intersects_location([origin], [dirv], multiple_hits=False)
                if len(locs):
                    first = float(np.linalg.norm(locs[0] - origin))
                    if first < size * 2 - max(1.0, 0.03 * size):
                        continue              # something is in front of the pin in this view
            out.setdefault(view, []).append({"n": p["n"], "u": round(float(u), 1), "v": round(float(v), 1)})
    return out


PLANNER_SYSTEM = """You edit a 3D-printable figure in Blender by returning JSON operations.
Units: millimetres. Z is up, the model stands on z=0 and is centred on x/y.
You get 4 renders (FRONT looks along +Y, RIGHT looks along -X, BACK looks along -Y, TOP looks down). Each has a
0..100 grid: u goes left->right, v goes top->bottom. To place something ON the surface, give an anchor
{"view": "front", "u": 50, "v": 30} pointing at the exact spot in that image; the program finds the real surface.

Operations (prefer the first ones; the program computes their geometry, you only give parameters):
- {"op":"erase_note","note":2,"radius_mm":4}  erase material in a ball around the exact spot of the user's note
  pin n (only for pinned notes; radius = about half the size of the thing to remove). Use it whenever a note
  says something at its pin should be removed. Never cut a cube/cylinder into the figure to remove something.
- {"op":"add_base","shape":"round|square","height_mm":4,"margin_mm":4}  base sized from the figure's feet,
  joined under it and dropped on the bed. If a name goes on the base, height_mm >= size_mm + 3.
- {"op":"base_text","text":"...","size_mm":5,"depth_mm":1}  name on the FRONT side of the base (needs a base,
  can be in the same plan right after add_base). Bends around round bases.
- {"op":"remove_loose_parts","min_fraction":0.02}  delete small floating pieces.
- {"op":"emboss_text","text":"...","anchor":{...},"size_mm":6,"depth_mm":1.2,"max_width_mm":optional}
  raised text (negative depth = engraved) facing that view, auto-shrunk to fit. Use "\\n" for two lines.
  Put it on a flat, wide, visible area (e.g. a base front, a banner, a chest), never across thin parts.
- {"op":"boolean_union"|"boolean_difference"|"add_primitive","shape":"cube|cylinder|sphere|cone|torus",
   "size_mm":[x,y,z],"rotation_deg":[x,y,z],"anchor":{...},"depth_into_mm":n}  (anchor is required)
  e.g. a keychain hole: boolean_difference cylinder at an anchor, depth_into_mm = half its length.
  Thin ATTACHED strings/straps are hard to hit from a picture: do NOT try to cut them; say so in
  explanation_tr ("ipleri Silgi ile işaretleyip silebilirsin"). Never drill through the figure.
- {"op":"remesh_voxel","voxel_mm":0.4}  LAST RESORT: rebuilds the surface and blurs detail. Do not use it to
  "clean" a model; use remove_loose_parts. Never below 0.3 mm, at most once.
- {"op":"smooth","iterations":1..10,"factor":0..1}, {"op":"scale","factor":[x,y,z]}, {"op":"rotate","degrees":[x,y,z]},
  {"op":"translate","mm":[x,y,z]}, {"op":"mirror","axis":"x"}, {"op":"solidify","thickness_mm":n},
  {"op":"decimate","ratio":0..1}, {"op":"drop_to_bed"}
User notes: the request may list numbered notes. Each note's pin is drawn as a numbered magenta circle on the
renders, and "notes" gives its u/v in every view where it is visible. When a note asks for a change AT its pin,
use that view and u/v as the anchor. A note without a pin applies to the whole model.
Placement rules:
- Anchors point at what is visible in THESE renders. For a base and a name on it use add_base + base_text
  (both can be in one plan). Anything else you add is not in the images yet: place things on it next round.
- Text must be at least 4 mm tall to print well. Text bends onto curved surfaces automatically.
- Text on the SIDE of a base needs a base at least size_mm + 3 mm tall (e.g. 8-10 mm for 5 mm text); otherwise
  put it on the base's top surface in front of the figure (anchor on the TOP view) or on a flat body area.
- A base is a cylinder/cube unioned under the figure that overlaps the feet by ~1 mm, then drop_to_bed.
Rules:
- LOOK at the renders before planning: they show the model as it is NOW, including everything earlier rounds
  added. Never add something that is already visible (a base, a text, a symbol). "rounds_so_far" lists what was
  applied; a round marked "rejected" was undone (the model does not contain it) and says why it was bad: do it
  differently or not at all.
- If the model already stands on a base/plinth, never add another base. Never scale, rotate, mirror or move the
  whole model unless the request literally asks for it.
- Text uses a normal font: put each word or symbol ONCE, in one emboss_text.
- If an op failed, read its error and fix that op only. Only do what the request asks. Keep the figure's details.
- If something in the request is impossible with these operations (e.g. reshaping or resizing one body part,
  changing a printed picture/colours), do not fake it with other ops: leave it out and say so in explanation_tr.
Return JSON {"ops": [...], "explanation_tr": one short Turkish sentence}."""

CHECK_SYSTEM = """You review ONE editing round of a 3D figure. You get the request, the operations of this round,
and BEFORE and AFTER renders of the same views (0..100 grid), plus close-ups around the user's numbered notes
when available. First compare BEFORE and AFTER carefully and describe what actually changed. Then judge:
- verdict "good": the change does what the request asked, in the right place, and damaged nothing;
- verdict "bad": it made the model worse than BEFORE: wrong place, holes or box-shaped cuts in the figure, broken
  or missing parts, something added twice or duplicated, wrong or extra symbols/letters, a second base;
- verdict "no_change": nothing visible changed.
- done: true only if EVERY part of the request is satisfied in AFTER.
Return JSON {"changed": short English description of the difference, "verdict": "good"|"bad"|"no_change",
"problems": [short English items, what is wrong and where], "done": bool, "missing": [short English items
still to do, with where], "missing_tr": [the same in short, simple Turkish], "summary_tr": one short Turkish
sentence for the user}"""


def _accepted(history: list[dict]) -> list[dict]:
    """Rounds whose changes are in the model now (not failed, not rolled back)."""
    return [h for h in history if not h.get("rejected") and not h.get("failed")]


def _drop_repeats(ops: list[dict], history: list[dict], j: Job) -> list[dict]:
    """Never apply twice what is already on the model: the same text, a second base, or new text/symbols right
    where earlier text was embossed (the planner tends to "redo" a symbol next to the first one)."""
    done = [o for h in _accepted(history) for o in h["ops"]]
    done_texts = {o["text"].lower() for o in done if o["op"] in ("emboss_text", "base_text")}
    done_chars = {c for t in done_texts for c in t if not c.isspace()}
    spots = [(o["anchor"]["view"], o["anchor"]["u"], o["anchor"]["v"]) for o in done
             if o["op"] == "emboss_text" and o.get("anchor")]
    has_base = any(o["op"] == "add_base" for o in done)
    out = []
    for o in ops:
        if o["op"] in ("emboss_text", "base_text"):
            t = o["text"].lower()
            chars = {c for c in t if not c.isspace()}
            near = o["op"] == "emboss_text" and any(
                v == o["anchor"]["view"] and abs(u - o["anchor"]["u"]) < 15 and abs(w - o["anchor"]["v"]) < 10
                for v, u, w in spots)
            if t in done_texts or (near and chars and chars <= done_chars):
                j.log(f"Tekrarlanan yazı atlandı: {o['text']}")
                continue
        if o["op"] == "add_base" and has_base:
            j.log("İkinci taban eklenmedi")
            continue
        out.append(o)
    return out


def _erase_ops(j: Job, version_file: Path, up: str, height: float) -> list[dict]:
    """Viewer tap points (glTF coords of the version file) -> print-frame mm, same transform as Blender."""
    erase = j.input.get("erase", [])
    if not erase:
        return []
    pts, _ = to_print_mm(version_file, up, height, [e["p"] for e in erase])
    ops = [{"op": "erase_sphere", "center_mm": p.round(3).tolist(), "radius_mm": float(e.get("r_mm", 3))}
           for p, e in zip(pts, erase)]
    ops.append({"op": "remove_loose_parts", "min_fraction": 0.02})
    return validate_ops(ops, internal=True) if len(ops) <= MAX_OPS else \
        validate_ops(ops[:MAX_OPS - 1], internal=True) + validate_ops(ops[-1:], internal=True)


def note_pins(notes: list[dict], version_file: Path, up: str, height: float):
    """Pinned notes -> [{n, mm}] in the print frame + the version mesh there (for visibility tests)."""
    pinned = [n for n in notes or [] if n.get("p")]
    if not pinned:
        return [], None
    pts, mesh = to_print_mm(version_file, up, height, [n["p"] for n in pinned])
    return [{"n": n["n"], "mm": p.round(2).tolist()} for n, p in zip(pinned, pts)], mesh


def start_edit(j: Job, path: Path, height: float) -> None:
    """First Blender run: the model scaled into the print frame, coloured, rendered (ctx current/renders)."""
    if "current" in j.ctx:
        return
    work = j.dir
    j.state("MULTI_MODAL_ANALYSIS", "Model inceleniyor")
    first = run_blender({"input": str(path), "height_mm": height, "ops": [],
                         "render_dir": str(work / "r0"), "output": str(work / "start.glb")}, work)
    # start.glb is the model scaled to the print frame; colour it from its own reference copy
    meshio.transfer_colors(work / "start.ref.glb", work / "start.glb", work / "cur0.glb")
    j.ctx.update({"current": str(work / "cur0.glb"), "renders": first["renders"], "stats": first["stats"],
                  "frame": first.get("frame"), "history": [], "missing": None})
    j.save()


DETAIL_ORDER = ("front", "right", "back", "top")


def _details(renders: list[str], marks: dict, workdir: Path, tag: str, limit: int = 3) -> list[tuple[int, str, Path]]:
    """Close-ups (x2.5) around each pinned note, from the first view where it is visible."""
    by_view = {Path(p).stem: Path(p) for p in renders}
    out = []
    seen = set()
    for view in DETAIL_ORDER:
        for m in marks.get(view, []):
            if m["n"] in seen or view not in by_view or len(out) >= limit:
                continue
            seen.add(m["n"])
            im = Image.open(by_view[view]).convert("RGB")
            w, h = im.size
            half = 0.14
            box = (max(0, (m["u"] / 100 - half) * w), max(0, (m["v"] / 100 - half) * h),
                   min(w, (m["u"] / 100 + half) * w), min(h, (m["v"] / 100 + half) * h))
            crop = im.crop(tuple(int(x) for x in box))
            crop = crop.resize((int(crop.width * 2.5), int(crop.height * 2.5)), Image.LANCZOS)
            dst = workdir / f"{tag}_note{m['n']}_{view}.png"
            crop.save(dst)
            out.append((m["n"], view, dst))
    return out


def _review(j: Job, directive: str, note_list: list, ops: list, before: list[str], after: list[str],
            marks: dict, rnd: int) -> dict:
    """BEFORE vs AFTER review of one round. Returns {} when the reviewer could not answer."""
    work = j.dir
    content: list = []
    for b, a in zip(before, after):
        view = Path(a).stem
        content.append({"type": "text", "text": f"{view.upper()} BEFORE:"})
        content.append(llm.image_block(grid_overlay(Path(b), work / f"c{rnd}_{view}_before.png", view, marks.get(view)), 384))
        content.append({"type": "text", "text": f"{view.upper()} AFTER:"})
        content.append(llm.image_block(grid_overlay(Path(a), work / f"c{rnd}_{view}_grid.png", view, marks.get(view)), 384))
    db_, da_ = _details(before, marks, work, f"c{rnd}b"), _details(after, marks, work, f"c{rnd}a")
    for (n, view, pb), (_, _, pa) in zip(db_, da_):
        content.append({"type": "text", "text": f"Close-up around note {n} ({view.upper()}), BEFORE then AFTER:"})
        content.append(llm.image_block(pb, 384))
        content.append(llm.image_block(pa, 384))
    content.append({"type": "text", "text": json.dumps({"request": directive, "notes": note_list,
                                                         "this_round_ops": ops}, ensure_ascii=False)})
    try:
        out = llm.chat_json(CHECK_SYSTEM, content, vision=True, user_id=j.user_id, max_tokens=4000)
        return out if isinstance(out, dict) else {}
    except llm.LLMError as e:
        j.log(f"Kontrol yapılamadı ({e})")
        return {}


def edit_loop(j: Job, *, version_id: str | None = None, directive: str | None = None,
              height: float | None = None, notes: list[dict] | None = None, eff: dict | None = None,
              source: str = "mod03") -> None:
    """Plan -> Blender -> review rounds. Every round is compared BEFORE vs AFTER; a round that made the model
    worse is rolled back and the planner is told why. `notes` are the user's notes ({n, text, p: glTF point |
    None}); pinned ones are drawn on the renders and can be erased exactly with erase_note."""
    directive = ((j.input.get("directive") or "") if directive is None else directive).strip()
    notes = notes or []
    if not directive and not j.input.get("erase") and not notes:
        raise JobFailed("Ne değişeceğini yaz ya da silinecek yerleri işaretle.")
    eff = eff or effort(j.input.get("effort"))
    max_rounds = int(eff["edit_rounds"])
    height = float(j.input.get("height_mm", 80) if height is None else height)
    path, ver, meta = version_path(version_id or j.input["version_id"])
    j.asset_id = ver["asset_id"]
    work = j.dir
    about = meta.get("spec", {}).get("description") or meta.get("prompt") or ""

    pins, pin_mesh = note_pins(notes, path, meta.get("up", "y"), height)
    note_list = [{"n": n["n"], "text": n["text"], "pinned": bool(n.get("p"))} for n in notes]
    start_edit(j, path, height)

    history = j.ctx["history"]
    if j.input.get("erase") and not j.ctx.get("erased"):
        j.state("MCP_EXECUTION", "İşaretlenen yerler siliniyor")
        ops = _erase_ops(j, path, meta.get("up", "y"), height)
        res = run_blender({"input": j.ctx["current"], "height_mm": None, "ops": ops,
                           "render_dir": str(work / "re"), "output": str(work / "erase.glb")}, work)
        meshio.transfer_colors(Path(res["ref"]), work / "erase.glb", work / "cur_e.glb",
                               own_colors=res.get("own_colors", False))
        for n in res.get("notes", []):
            j.log("Not: " + n)
        history.append({"ops": ops, "note": f"{len(j.input['erase'])} işaretli yer silindi", "stats": res["stats"]})
        j.ctx.update({"current": str(work / "cur_e.glb"), "renders": res["renders"], "stats": res["stats"],
                      "frame": res.get("frame") or j.ctx.get("frame"), "changed": True, "erased": True})
        j.save()

    def pin_state():
        # pins belong to the model as it came in; once something moved it, they are given as text only
        moved = any(o["op"] in MOVING_OPS for h in _accepted(history) for o in h["ops"])
        if not pins or moved or not j.ctx.get("frame"):
            return {}, {}
        return project_pins(pins, j.ctx["frame"], pin_mesh), {p["n"]: p["mm"] for p in pins}

    while (directive or notes) and len(history) < max_rounds + (1 if j.ctx.get("erased") else 0):
        rnd = len(history)
        j.state("MULTI_MODAL_ANALYSIS", f"Plan hazırlanıyor (tur {rnd + 1}/{max_rounds})")
        marks, pin_mm = pin_state()
        content = _views_content(j.ctx["renders"], work, f"p{rnd}", marks)
        where = {p["n"]: {v: {"u": m["u"], "v": m["v"]} for v, ms in marks.items() for m in ms if m["n"] == p["n"]}
                 for p in pins}
        content.append({"type": "text", "text": json.dumps({
            "request": directive, "model": about, "stats_mm": j.ctx["stats"],
            "notes": [{**n, "visible_at": where.get(n["n"], {}),
                       "erase_note_available": n["n"] in pin_mm} for n in note_list],
            "careful": "Extra effort: before answering, re-check every anchor against the images and every size "
                       "against stats_mm." if eff.get("careful") else None,
            "rounds_so_far": [{"applied_ops": [] if h.get("rejected") else h["ops"],
                               "rejected": h.get("rejected", False), "rejected_ops": h["ops"] if h.get("rejected") else [],
                               "why_rejected": h.get("problems", []), "note": h.get("note"),
                               "failed_ops": h.get("failed_ops", []), "runner_notes": h.get("notes", [])}
                              for h in history],
            "still_missing": j.ctx.get("missing")}, ensure_ascii=False)})
        plan = llm.chat_json(PLANNER_SYSTEM, content, vision=True, user_id=j.user_id, max_tokens=6000)
        try:
            ops = _drop_repeats(validate_ops(plan.get("ops") or [], pins=pin_mm), history, j)
        except BlenderError as e:
            history.append({"ops": [], "note": f"geçersiz plan: {e}", "failed": True})
            j.log(f"Plan reddedildi: {e}")
            j.save()
            continue
        if not ops:
            if plan.get("explanation_tr"):
                j.ctx["last_word"] = str(plan["explanation_tr"])
            j.log("Yapılacak başka işlem yok.")
            break
        if any(o["op"] == "erase_sphere" for o in ops) and not any(o["op"] == "remove_loose_parts" for o in ops) \
                and len(ops) < MAX_OPS:
            ops.append({"op": "remove_loose_parts", "min_fraction": 0.02})

        j.state("MCP_EXECUTION", plan.get("explanation_tr") or "Blender'da uygulanıyor")
        out_glb = work / f"edit_{rnd}.glb"
        try:
            res = run_blender({"input": j.ctx["current"], "height_mm": None, "ops": ops,
                               "render_dir": str(work / f"r{rnd + 1}"), "output": str(out_glb)}, work)
        except BlenderError as e:
            history.append({"ops": ops, "note": f"uygulanamadı: {e}", "failed": True})
            j.log(f"Bu adım uygulanamadı: {e}")
            j.save()
            continue
        failed = res.get("failed_ops") or []
        applied = [o for i, o in enumerate(ops) if i not in {f["index"] for f in failed}]
        for f in failed:
            j.log(f"Atlanan adım ({f['op']}): {f['error']}")
        for n in res.get("notes", []):
            j.log("Not: " + n)
        if not applied:
            history.append({"ops": [], "note": "hiçbir adım uygulanamadı", "failed_ops": failed, "failed": True})
            j.save()
            continue
        colored = work / f"cur{rnd + 1}.glb"
        tr = meshio.transfer_colors(Path(res["ref"]), out_glb, colored, own_colors=res.get("own_colors", False))

        j.state("REVIEW", "Sonuç önceki hâliyle karşılaştırılıyor")
        moved_now = any(o["op"] in MOVING_OPS for o in applied)     # pins no longer line up in AFTER
        check = _review(j, directive, note_list, applied, j.ctx["renders"], res["renders"],
                        {} if moved_now else marks, rnd)
        entry = {"ops": applied, "note": plan.get("explanation_tr"), "stats": res["stats"], "colours": tr,
                 "failed_ops": failed, "notes": res.get("notes", []), "changed": check.get("changed"),
                 "verdict": check.get("verdict")}
        if check.get("verdict") == "bad":
            # undo: keep the model, renders and frame from before this round
            entry.update(rejected=True, problems=[str(p) for p in (check.get("problems") or [])][:6])
            history.append(entry)
            j.log("Bu adım modeli bozdu, geri alındı: " + "; ".join(entry["problems"])[:300])
            j.save()
            continue
        history.append(entry)
        j.ctx.update({"current": str(colored), "renders": res["renders"], "stats": res["stats"],
                      "frame": res.get("frame") or j.ctx.get("frame"), "changed": True,
                      "missing": check.get("missing"), "missing_tr": check.get("missing_tr") or [],
                      "reviewed_done": bool(check.get("done"))})
        if check.get("summary_tr"):
            j.log(check["summary_tr"])
        j.save()
        if check.get("done"):
            break

    if not j.ctx.get("changed"):
        reasons = [p for h in history for p in h.get("problems", [])] or [h.get("note") for h in history if h.get("note")]
        raise JobFailed("Model değiştirilemedi" + (": " + "; ".join(map(str, reasons))[:300] if reasons else
                                                 ". İsteği daha basit adımlarla yazmayı dene."))
    j.state("VERSIONING_AND_RENDER", "Yeni sürüm kaydediliyor")
    for p in j.ctx["renders"]:
        shutil.copyfile(p, work / f"final_{Path(p).stem}.png")
    final = Path(j.ctx["current"])
    kept = _accepted(history)
    j.version_id = add_version(j.asset_id, source, final,
                               {"up": "y", "directive": directive, "units": "mm", "height_mm": height,
                                "effort": eff["name"], "notes": [n["text"] for n in notes],
                                "ops": [h["ops"] for h in kept]}, parent_id=ver["id"])
    missing_tr = [] if j.ctx.get("reviewed_done") else [str(x) for x in (j.ctx.get("missing_tr") or [])][:6]
    j.ctx["result"] = {"asset_id": j.asset_id, "version_id": j.version_id,
                       "notes": [h.get("note") for h in kept if h.get("note")],
                       "undone": sum(1 for h in history if h.get("rejected")),
                       "missing_tr": missing_tr, "last_word": j.ctx.get("last_word")}
    j.save()
