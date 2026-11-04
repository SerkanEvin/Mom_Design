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
from .config import get_settings
from .jobs import Job, JobFailed, add_version, version_path

RUNNER = Path(__file__).with_name("blender_runner.py")
MAX_ROUNDS = 3
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


def validate_ops(ops, internal: bool = False) -> list[dict]:
    if not isinstance(ops, list) or len(ops) > MAX_OPS:
        raise BlenderError(f"en fazla {MAX_OPS} işlem")
    out = []
    remesh = 0
    for o in ops:
        if not isinstance(o, dict):
            raise BlenderError("işlem nesne olmalı")
        op = o.get("op")
        if op == "scale":
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


def grid_overlay(src: Path, dst: Path, view: str) -> Path:
    """Label the render with a 0..100 grid so the planner can point at places (u right, v down)."""
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
    d.text((w - 70, h - 14), view.upper(), fill=(0, 0, 0, 255))
    im.save(dst)
    return dst


def _views_content(paths: list[str], workdir: Path, tag: str) -> list[dict]:
    content = []
    for p in paths:
        view = Path(p).stem
        g = grid_overlay(Path(p), workdir / f"{tag}_{view}_grid.png", view)
        content.append({"type": "text", "text": f"{view.upper()} view:"})
        content.append(llm.image_block(g, 448))
    return content


PLANNER_SYSTEM = """You edit a 3D-printable figure in Blender by returning JSON operations.
Units: millimetres. Z is up, the model stands on z=0 and is centred on x/y.
You get 4 renders (FRONT looks along +Y, RIGHT looks along -X, BACK looks along -Y, TOP looks down). Each has a
0..100 grid: u goes left->right, v goes top->bottom. To place something ON the surface, give an anchor
{"view": "front", "u": 50, "v": 30} pointing at the exact spot in that image; the program finds the real surface.

Operations (prefer the first three; the program computes their geometry, you only give parameters):
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
Placement rules:
- Anchors point at what is visible in THESE renders. For a base and a name on it use add_base + base_text
  (both can be in one plan). Anything else you add is not in the images yet: place things on it next round.
- Text must be at least 4 mm tall to print well. Text bends onto curved surfaces automatically.
- Text on the SIDE of a base needs a base at least size_mm + 3 mm tall (e.g. 8-10 mm for 5 mm text); otherwise
  put it on the base's top surface in front of the figure (anchor on the TOP view) or on a flat body area.
- A base is a cylinder/cube unioned under the figure that overlaps the feet by ~1 mm, then drop_to_bed.
Rules: operations already applied are permanent; NEVER repeat them (see rounds_so_far). If an op failed, read
its error and fix that op only. Only do what the request asks. Keep the figure's details.
Return JSON {"ops": [...], "explanation_tr": one short Turkish sentence}."""

CHECK_SYSTEM = """You check an edited 3D figure against the user's request. You get the request, the operations
already applied, and 4 renders of the result (FRONT, RIGHT, BACK, TOP with a 0..100 grid). For each part of the
request decide if it is done and visible in the right place. Be concrete.
Return JSON {"done": bool (true only if every part is satisfied), "missing": [short English items still to do,
with where], "summary_tr": one short Turkish sentence for the user}."""


def _drop_repeats(ops: list[dict], history: list[dict], j: Job) -> list[dict]:
    """The planner sometimes re-adds a base or re-embosses the same text; never apply those twice."""
    done_texts = {o["text"].lower() for h in history for o in h["ops"] if o["op"] in ("emboss_text", "base_text")}
    has_base = any(o["op"] == "add_base" for h in history for o in h["ops"])
    out = []
    for o in ops:
        if o["op"] in ("emboss_text", "base_text") and o["text"].lower() in done_texts:
            j.log(f"Tekrarlanan yazı atlandı: {o['text']}")
            continue
        if o["op"] == "add_base" and has_base:
            j.log("İkinci taban eklenmedi")
            continue
        out.append(o)
    return out


def _erase_ops(j: Job, version_file: Path, up: str, height: float) -> list[dict]:
    """Viewer tap points (glTF coords of the version file) -> print-frame mm, same transform as Blender."""
    pts = np.array([e["p"] for e in j.input.get("erase", [])], dtype=float)
    if not len(pts):
        return []
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
    pts = pts + off
    ops = [{"op": "erase_sphere", "center_mm": p.round(3).tolist(), "radius_mm": float(e.get("r_mm", 3))}
           for p, e in zip(pts, j.input["erase"])]
    ops.append({"op": "remove_loose_parts", "min_fraction": 0.02})
    return validate_ops(ops, internal=True) if len(ops) <= MAX_OPS else \
        validate_ops(ops[:MAX_OPS - 1], internal=True) + validate_ops(ops[-1:], internal=True)


def edit_loop(j: Job) -> None:
    directive = (j.input.get("directive") or "").strip()
    if not directive and not j.input.get("erase"):
        raise JobFailed("Ne değişeceğini yaz ya da silinecek yerleri işaretle.")
    height = float(j.input.get("height_mm", 80))
    path, ver, meta = version_path(j.input["version_id"])
    j.asset_id = ver["asset_id"]
    work = j.dir
    about = meta.get("spec", {}).get("description") or meta.get("prompt") or ""

    if "current" not in j.ctx:
        j.state("MULTI_MODAL_ANALYSIS", "Model inceleniyor")
        first = run_blender({"input": str(path), "height_mm": height, "ops": [],
                             "render_dir": str(work / "r0"), "output": str(work / "start.glb")}, work)
        # start.glb is the model scaled to the print frame; colour it from its own reference copy
        meshio.transfer_colors(work / "start.ref.glb", work / "start.glb", work / "cur0.glb")
        j.ctx.update({"current": str(work / "cur0.glb"), "renders": first["renders"], "stats": first["stats"],
                      "history": [], "missing": None})
        j.save()

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
                      "changed": True, "erased": True})
        j.save()
    while directive and len(history) < MAX_ROUNDS + (1 if j.ctx.get("erased") else 0):
        rnd = len(history)
        j.state("MULTI_MODAL_ANALYSIS", f"Plan hazırlanıyor (tur {rnd + 1})")
        content = _views_content(j.ctx["renders"], work, f"p{rnd}")
        content.append({"type": "text", "text": json.dumps({
            "request": directive, "model": about, "stats_mm": j.ctx["stats"],
            "rounds_so_far": [{"applied_ops": h["ops"], "note": h.get("note"),
                               "failed_ops": h.get("failed_ops", []), "runner_notes": h.get("notes", [])}
                              for h in history],
            "still_missing": j.ctx.get("missing")}, ensure_ascii=False)})
        plan = llm.chat_json(PLANNER_SYSTEM, content, vision=True, user_id=j.user_id, max_tokens=3000)
        try:
            ops = _drop_repeats(validate_ops(plan.get("ops") or []), history, j)
        except BlenderError as e:
            history.append({"ops": [], "note": f"geçersiz plan: {e}"})
            j.log(f"Plan reddedildi: {e}")
            j.save()
            continue
        if not ops:
            j.log("Yapılacak başka işlem yok.")
            break

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
        history.append({"ops": applied, "note": plan.get("explanation_tr"), "stats": res["stats"], "colours": tr,
                        "failed_ops": failed, "notes": res.get("notes", [])})
        j.ctx.update({"current": str(colored), "renders": res["renders"], "stats": res["stats"], "changed": True})
        j.save()

        j.state("REVIEW", "Sonuç kontrol ediliyor")
        check = {}
        try:
            content = _views_content(res["renders"], work, f"c{rnd}")
            content.append({"type": "text", "text": json.dumps({"request": directive,
                                                                 "applied": [h["ops"] for h in history]},
                                                                ensure_ascii=False)})
            check = llm.chat_json(CHECK_SYSTEM, content, vision=True, user_id=j.user_id, max_tokens=1500)
        except llm.LLMError as e:
            j.log(f"Kontrol yapılamadı ({e})")
        if check.get("summary_tr"):
            j.log(check["summary_tr"])
        j.ctx["missing"] = check.get("missing")
        j.save()
        if check.get("done", True):
            break

    if not j.ctx.get("changed"):
        raise JobFailed("Model değiştirilemedi. İsteği daha basit adımlarla yazmayı dene.")
    j.state("VERSIONING_AND_RENDER", "Yeni sürüm kaydediliyor")
    for p in j.ctx["renders"]:
        shutil.copyfile(p, work / f"final_{Path(p).stem}.png")
    final = Path(j.ctx["current"])
    j.version_id = add_version(j.asset_id, "mod03", final,
                               {"up": "y", "directive": directive, "units": "mm", "height_mm": height,
                                "ops": [h["ops"] for h in history]}, parent_id=ver["id"])
    j.ctx["result"] = {"asset_id": j.asset_id, "version_id": j.version_id,
                       "notes": [h.get("note") for h in history if h.get("note")]}
    j.save()
