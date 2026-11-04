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


def validate_ops(ops) -> list[dict]:
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
            else:
                d["center_mm"] = _vec(o.get("center_mm"), -500, 500, "center_mm")
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

Operations:
- {"op":"emboss_text","text":"...","anchor":{...},"size_mm":6,"depth_mm":1.2,"max_width_mm":optional}
  raised text (negative depth = engraved) facing that view, auto-shrunk to fit. Use "\\n" for two lines.
  Put it on a flat, wide, visible area (e.g. a base front, a banner, a chest), never across thin parts.
- {"op":"boolean_union"|"boolean_difference"|"add_primitive","shape":"cube|cylinder|sphere|cone|torus",
   "size_mm":[x,y,z],"rotation_deg":[x,y,z], and EITHER "center_mm":[x,y,z] OR "anchor":{...},"depth_into_mm":n}
  e.g. a round base under the figure: boolean_union cylinder size [d,d,3], center [0,0,-1.5] then drop_to_bed.
  A hole: boolean_difference cylinder at an anchor with depth_into_mm = half its length.
- {"op":"remove_loose_parts","min_fraction":0.02}  delete small floating pieces (strings, specks).
- {"op":"remesh_voxel","voxel_mm":0.4}  LAST RESORT: rebuilds the surface and blurs detail. Do not use it to
  "clean" a model; use remove_loose_parts. Never below 0.3 mm, at most once.
- {"op":"smooth","iterations":1..10,"factor":0..1}, {"op":"scale","factor":[x,y,z]}, {"op":"rotate","degrees":[x,y,z]},
  {"op":"translate","mm":[x,y,z]}, {"op":"mirror","axis":"x"}, {"op":"solidify","thickness_mm":n},
  {"op":"decimate","ratio":0..1}, {"op":"drop_to_bed"}
Rules: operations already applied are permanent; NEVER repeat them. Only do what the request asks. Keep the
figure's details. Return JSON {"ops": [...], "explanation_tr": one short Turkish sentence}."""

CHECK_SYSTEM = """You check an edited 3D figure against the user's request. You get the request, the operations
already applied, and 4 renders of the result (FRONT, RIGHT, BACK, TOP with a 0..100 grid). For each part of the
request decide if it is done and visible in the right place. Be concrete.
Return JSON {"done": bool (true only if every part is satisfied), "missing": [short English items still to do,
with where], "summary_tr": one short Turkish sentence for the user}."""


def edit_loop(j: Job) -> None:
    directive = (j.input.get("directive") or "").strip()
    if not directive:
        raise JobFailed("Ne değişeceğini yaz.")
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
    while len(history) < MAX_ROUNDS:
        rnd = len(history)
        j.state("MULTI_MODAL_ANALYSIS", f"Plan hazırlanıyor (tur {rnd + 1})")
        content = _views_content(j.ctx["renders"], work, f"p{rnd}")
        content.append({"type": "text", "text": json.dumps({
            "request": directive, "model": about, "stats_mm": j.ctx["stats"],
            "rounds_so_far": [{"ops": h["ops"], "note": h.get("note"), "failed": h.get("failed", False)}
                              for h in history],
            "still_missing": j.ctx.get("missing")}, ensure_ascii=False)})
        plan = llm.chat_json(PLANNER_SYSTEM, content, vision=True, user_id=j.user_id, max_tokens=3000)
        try:
            ops = validate_ops(plan.get("ops") or [])
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
        colored = work / f"cur{rnd + 1}.glb"
        tr = meshio.transfer_colors(Path(res["ref"]), out_glb, colored)
        for n in res.get("notes", []):
            j.log("Not: " + n)
        history.append({"ops": ops, "note": plan.get("explanation_tr"), "stats": res["stats"], "colours": tr})
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
