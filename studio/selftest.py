"""Offline self-test inside the container (no fal / LLM spend):  python -m studio.selftest

Two-colour test model (blue block, red ball) + a small floating speck. Blender: remove the speck, emboss "ASLI"
on the block's front via an anchor, drill a hole through the top. Then colours are copied back and checked:
text must sit on the block front and be a new (white) colour; block stays blue, ball stays red.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import trimesh

from . import audit, blender, meshio, segment, threemf


def main() -> int:
    out: dict = {}
    work = Path(tempfile.mkdtemp(prefix="studio-selftest-"))
    box = trimesh.creation.box((40, 40, 10))
    box.apply_translation([0, 0, 5])
    sph = trimesh.creation.icosphere(subdivisions=3, radius=12)
    sph.apply_translation([0, 0, 20])
    m = trimesh.boolean.union([box, sph], engine="manifold")
    speck = trimesh.creation.icosphere(subdivisions=1, radius=0.6)
    speck.apply_translation([17, 0, 28])
    m = trimesh.util.concatenate([m, speck])
    m.metadata["face_rgb"] = np.where((m.triangles_center[:, 2] > 11)[:, None], [220, 30, 30], [30, 30, 220]).astype(np.uint8)
    src = meshio.export_glb_with_colors(m, work / "in.glb", up="z")

    t = time.time()
    ok = True
    try:
        first = blender.run_blender({"input": str(src), "height_mm": 60, "ops": [], "render_dir": str(work / "r0"),
                                    "output": str(work / "start.glb")}, work)
        meshio.transfer_colors(work / "start.ref.glb", work / "start.glb", work / "cur0.glb")
        ops = blender.validate_ops([
            {"op": "remove_loose_parts", "min_fraction": 0.01},
            {"op": "emboss_text", "text": "ASLI", "anchor": {"view": "front", "u": 50, "v": 72}, "size_mm": 9,
             "depth_mm": 1.5},
            {"op": "boolean_difference", "shape": "cylinder", "size_mm": [6, 6, 30],
             "anchor": {"view": "top", "u": 50, "v": 50}, "depth_into_mm": 10},
        ])
        res = blender.run_blender({"input": str(work / "cur0.glb"), "height_mm": None, "ops": ops,
                                   "render_dir": str(work / "r1"), "output": str(work / "edit.glb")}, work)
        tr = meshio.transfer_colors(Path(res["ref"]), work / "edit.glb", work / "cur1.glb")
        edited = meshio.load(work / "cur1.glb")
        c, rgb = edited.triangles_center, meshio.face_rgb(edited)
        # glTF is Y-up: model x = x, model z(up) = gltf y, model y = -gltf z
        mx, my, mz = c[:, 0], -c[:, 2], c[:, 1]
        text = my < -37.6
        white = rgb.min(axis=1) > 230
        block = (my > -37.4) & (mz < 15) & (np.abs(mx) < 30)
        ball = (mz > 30) & (np.hypot(mx, my) < 15) & (mz < 55)
        out["blender"] = {
            "ok": True, "seconds": round(time.time() - t, 1), "stats": res["stats"], "notes": res.get("notes"),
            "renders": len(res.get("renders", [])), "colour_transfer": tr,
            "text_faces": int(text.sum()),
            "text_z_range_mm": [round(float(mz[text].min()), 1), round(float(mz[text].max()), 1)] if text.any() else None,
            "text_is_white": round(float(white[text].mean()), 2) if text.any() else None,
            "block_is_blue": round(float((rgb[block][:, 2] > 150).mean()), 2),
            "ball_is_red": round(float((rgb[ball][:, 0] > 150).mean()), 2) if ball.any() else None,
        }
        # the case from Serkan's test: round base first, then a name on its curved side in the next run
        run2 = blender.validate_ops([{"op": "boolean_union", "shape": "cylinder", "size_mm": [90, 90, 10],
                                      "center_mm": [0, 0, -4]}, {"op": "drop_to_bed"}])
        r2 = blender.run_blender({"input": str(work / "cur1.glb"), "height_mm": None, "ops": run2,
                                  "render_dir": str(work / "r2"), "output": str(work / "edit2.glb")}, work)
        meshio.transfer_colors(Path(r2["ref"]), work / "edit2.glb", work / "cur2.glb")
        run3 = blender.validate_ops([{"op": "emboss_text", "text": "NILDA", "anchor": {"view": "front", "u": 50, "v": 74},
                                      "size_mm": 6, "depth_mm": 1.2}])
        r3 = blender.run_blender({"input": str(work / "cur2.glb"), "height_mm": None, "ops": run3,
                                  "render_dir": str(work / "r3"), "output": str(work / "edit3.glb")}, work)
        meshio.transfer_colors(Path(r3["ref"]), work / "edit3.glb", work / "cur3.glb")
        e3 = meshio.load(work / "cur3.glb")
        c3, rgb3 = e3.triangles_center, meshio.face_rgb(e3)
        x3, y3, z3 = c3[:, 0], -c3[:, 2], c3[:, 1]
        r = np.hypot(x3, y3)
        name = (r > 45.15) & (z3 < 12) & (y3 < 0)
        out["base_text"] = {
            "failed_ops": r2.get("failed_ops", []) + r3.get("failed_ops", []), "notes": r3.get("notes"),
            "text_faces": int(name.sum()),
            "z_range_mm": [round(float(z3[name].min()), 1), round(float(z3[name].max()), 1)] if name.any() else None,
            "max_protrusion_mm": round(float(r[name].max() - 45), 2) if name.any() else None,
            "x_span_mm": round(float(x3[name].max() - x3[name].min()), 1) if name.any() else None,
            "white": round(float((rgb3[name].min(axis=1) > 230).mean()), 2) if name.any() else None,
            "watertight": r3["stats"]["watertight"]}
        bt = out["base_text"]
        base_ok = (bt["text_faces"] > 50 and not bt["failed_ops"] and bt["max_protrusion_mm"] < 2.2
                   and 0.5 < bt["z_range_mm"][0] and bt["z_range_mm"][1] < 10 and bt["watertight"])
        out["base_text"]["pass"] = bool(base_ok)
        b = out["blender"]
        ok = base_ok and (b["text_faces"] > 50 and b["text_is_white"] > 0.6 and 2 < b["text_z_range_mm"][0] < b["text_z_range_mm"][1] < 18
              and b["block_is_blue"] > 0.8 and (b["ball_is_red"] or 0) > 0.8 and res["stats"]["watertight"])
        out["blender"]["pass"] = bool(ok)
    except Exception as e:
        ok = False
        out["blender"] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:400]}
        edited = meshio.load(src)

    p = meshio.to_print_frame(edited, "y", 60)
    p, notes = audit.basic_repair(p)
    rep, _ = audit.audit(p)
    seg = segment.segment(p, 3)
    data = threemf.painted_3mf(p, seg["labels"], seg["palette"], "selftest")
    out["print"] = {"watertight": rep["watertight"], "is_printable": rep["is_printable"], "parts": rep["parts"],
                    "palette": seg["palette"], "painted_faces": sum(1 for c in threemf.read_painted_labels(data) if c)}
    out["work"] = str(work)
    print(json.dumps(out, ensure_ascii=False))
    return 0 if ok and rep["watertight"] else 1


if __name__ == "__main__":
    sys.exit(main())
