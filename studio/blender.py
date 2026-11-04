"""MOD-03: agentic Blender editor with a fixed, whitelisted operation set (no arbitrary Python).

The planner (syn:large:vision) sees renders + mesh stats and returns JSON ops; `blender_runner.py`
executes them headless inside a time/memory-limited Blender process. Units in ops are millimetres
in the print frame (Z up, standing on z=0).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from . import llm
from .config import get_settings
from .jobs import Job, JobFailed, add_version, version_path

RUNNER = Path(__file__).with_name("blender_runner.py")
MAX_ITER = 4
MAX_OPS = 12


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


PRIMS = {"cube", "cylinder", "sphere", "cone", "torus"}


def validate_ops(ops) -> list[dict]:
    if not isinstance(ops, list) or len(ops) > MAX_OPS:
        raise BlenderError(f"en fazla {MAX_OPS} işlem")
    out = []
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
            out.append({"op": op, "shape": prim, "size_mm": _vec(o.get("size_mm"), 0.2, 500, "size_mm"),
                        "center_mm": _vec(o.get("center_mm"), -500, 500, "center_mm"),
                        "rotation_deg": _vec(o.get("rotation_deg", [0, 0, 0]), -360, 360, "rotation_deg")})
        elif op == "remesh_voxel":
            out.append({"op": op, "voxel_mm": _num(o.get("voxel_mm"), 0.1, 10, "voxel_mm")})
        elif op == "decimate":
            out.append({"op": op, "ratio": _num(o.get("ratio"), 0.05, 1, "ratio")})
        elif op == "subdivide":
            out.append({"op": op, "levels": int(_num(o.get("levels"), 1, 2, "levels"))})
        elif op == "smooth":
            out.append({"op": op, "iterations": int(_num(o.get("iterations"), 1, 30, "iterations")),
                        "factor": _num(o.get("factor", 0.5), 0, 1, "factor")})
        elif op == "solidify":
            out.append({"op": op, "thickness_mm": _num(o.get("thickness_mm"), 0.4, 20, "thickness_mm")})
        elif op == "mirror":
            if o.get("axis") not in ("x", "y", "z"):
                raise BlenderError("axis x|y|z")
            out.append({"op": op, "axis": o["axis"]})
        elif op == "emboss_text":
            text = str(o.get("text", ""))[:40]
            if not text.strip():
                raise BlenderError("text boş")
            out.append({"op": op, "text": text, "size_mm": _num(o.get("size_mm"), 1, 200, "size_mm"),
                        "depth_mm": _num(o.get("depth_mm"), -20, 20, "depth_mm"),
                        "center_mm": _vec(o.get("center_mm"), -500, 500, "center_mm"),
                        "rotation_deg": _vec(o.get("rotation_deg", [90, 0, 0]), -360, 360, "rotation_deg")})
        elif op == "drop_to_bed":
            out.append({"op": op})
        else:
            raise BlenderError(f"bilinmeyen işlem: {op}")
    return out


def run_blender(job: dict, workdir: Path, timeout_s: int = 300) -> dict:
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
        tail = (p.stderr or p.stdout or "")[-600:]
        raise BlenderError("Blender çalışmadı: " + tail.strip().splitlines()[-1] if tail.strip() else "Blender çalışmadı.")
    out = json.loads(result.read_text())
    if not out.get("ok"):
        raise BlenderError(out.get("error", "Blender hatası"))
    return out


PLANNER_SYSTEM = (
    "You edit a 3D-printable mesh in Blender by returning JSON operations. Units are millimetres; Z is up and the "
    "model stands on z=0, centred on x/y. You see 4 renders (front -Y, right +X, back +Y, top) and stats. "
    "Allowed ops (JSON objects): "
    "{op:'scale', factor:[x,y,z]}, {op:'rotate', degrees:[x,y,z]}, {op:'translate', mm:[x,y,z]}, "
    "{op:'boolean_difference'|'boolean_union'|'add_primitive', shape:'cube'|'cylinder'|'sphere'|'cone'|'torus', "
    "size_mm:[x,y,z], center_mm:[x,y,z], rotation_deg:[x,y,z]}, {op:'remesh_voxel', voxel_mm:n}, "
    "{op:'decimate', ratio:0..1}, {op:'subdivide', levels:1|2}, {op:'smooth', iterations:n, factor:0..1}, "
    "{op:'solidify', thickness_mm:n}, {op:'mirror', axis:'x'|'y'|'z'}, "
    "{op:'emboss_text', text:str, size_mm:n, depth_mm:n (negative = engrave), center_mm:[x,y,z], rotation_deg:[x,y,z]}, "
    "{op:'drop_to_bed'}. "
    "Use the stats' bounding box to place things. Prefer few, precise ops. After booleans the result must stay "
    "one closed solid. Return JSON {\"done\": bool (true if the current model already satisfies the request), "
    "\"ops\": [..], \"explanation_tr\": short Turkish sentence for the user}.")


def edit_loop(j: Job) -> None:
    directive = (j.input.get("directive") or "").strip()
    if not directive:
        raise JobFailed("Ne değişeceğini yaz.")
    height = float(j.input.get("height_mm", 80))
    path, ver, meta = version_path(j.input["version_id"])
    j.asset_id = ver["asset_id"]
    work = j.dir
    current = j.ctx.get("current") or str(path)
    up = j.ctx.get("current_up") or meta.get("up", "y")
    history = j.ctx.setdefault("history", [])
    start_iter = len(history)
    for it in range(start_iter, MAX_ITER):
        j.state("MULTI_MODAL_ANALYSIS", f"Model inceleniyor (tur {it + 1})")
        rdir = work / f"it{it}"
        rdir.mkdir(exist_ok=True)
        info = run_blender({"input": current, "input_up": up, "height_mm": height if it == 0 else None,
                            "ops": [], "render_dir": str(rdir), "output": None}, work)
        content = [llm.image_block(Path(p), 512) for p in info["renders"]]
        content.append({"type": "text", "text": json.dumps({
            "request": directive, "stats": info["stats"],
            "previous_rounds": [{"ops": h["ops"], "note": h.get("note")} for h in history]}, ensure_ascii=False)})
        plan = llm.chat_json(PLANNER_SYSTEM, content, vision=True, user_id=j.user_id, max_tokens=2000)
        if plan.get("done") or not plan.get("ops"):
            history.append({"ops": [], "note": plan.get("explanation_tr", "tamam")})
            j.log(plan.get("explanation_tr") or "Değişiklik tamam.")
            break
        try:
            ops = validate_ops(plan["ops"])
        except BlenderError as e:
            history.append({"ops": [], "note": f"geçersiz plan: {e}"})
            j.log(f"Plan reddedildi: {e}")
            j.save()
            continue
        j.state("MCP_EXECUTION", plan.get("explanation_tr") or "Blender'da uygulanıyor")
        out_path = work / f"edit_{it}.glb"
        res = run_blender({"input": current, "input_up": up, "height_mm": height if it == 0 else None,
                           "ops": ops, "render_dir": None, "output": str(out_path)}, work)
        history.append({"ops": ops, "note": plan.get("explanation_tr"), "stats": res.get("stats")})
        current, up = str(out_path), "y"
        j.ctx.update({"current": current, "current_up": up})
        j.save()
    if not j.ctx.get("current"):
        raise JobFailed("Model değiştirilmedi (yapay zekâ bir değişiklik önermedi).")
    j.state("VERSIONING_AND_RENDER", "Yeni sürüm kaydediliyor")
    final = Path(j.ctx["current"])
    j.version_id = add_version(j.asset_id, "mod03", final,
                               {"up": "y", "directive": directive, "units": "mm", "height_mm": height,
                                "ops": [h["ops"] for h in history]}, parent_id=ver["id"])
    j.ctx["result"] = {"asset_id": j.asset_id, "version_id": j.version_id,
                       "notes": [h.get("note") for h in history if h.get("note")]}
    j.save()
