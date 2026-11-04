"""Offline self-test inside the container (no fal / LLM spend):  python -m studio.selftest

Builds a two-colour test model, runs Blender ops (boolean hole + embossed text + renders),
then the print audit, colour regions and the painted 3MF export. Prints one JSON summary.
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
    m.metadata["face_rgb"] = np.where((m.triangles_center[:, 2] > 11)[:, None], [220, 30, 30], [30, 30, 220]).astype(np.uint8)
    src = meshio.export_glb_with_colors(m, work / "in.glb", up="z")

    t = time.time()
    try:
        ops = blender.validate_ops([
            {"op": "boolean_difference", "shape": "cylinder", "size_mm": [6, 6, 80], "center_mm": [0, 0, 40]},
            {"op": "emboss_text", "text": "ASLI", "size_mm": 7, "depth_mm": 1.2, "center_mm": [0, -20.5, 5],
             "rotation_deg": [90, 0, 0]},
        ])
        res = blender.run_blender({"input": str(src), "input_up": "y", "height_mm": 60, "ops": ops,
                                   "render_dir": str(work / "renders"), "output": str(work / "edit.glb")}, work)
        out["blender"] = {"ok": True, "seconds": round(time.time() - t, 1), "stats": res["stats"],
                          "renders": len(res.get("renders", []))}
        edited = meshio.load(work / "edit.glb")
    except Exception as e:
        out["blender"] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:400]}
        edited = meshio.load(src)

    t = time.time()
    p = meshio.to_print_frame(edited, "y", 60)
    p, notes = audit.basic_repair(p)
    if not p.is_watertight:
        p = audit.voxel_remesh(p)
        notes.append("voxel remesh")
    rep, _ = audit.audit(p)
    seg = segment.segment(p, 3)
    data = threemf.painted_3mf(p, seg["labels"], seg["palette"], "selftest")
    out["colours_kept_after_blender"] = len(seg["palette"]) >= 2
    out["print"] = {"seconds": round(time.time() - t, 1), "watertight": rep["watertight"],
                    "is_printable": rep["is_printable"], "size_mm": rep["size_mm"], "repairs": notes,
                    "palette": seg["palette"], "3mf_bytes": len(data),
                    "painted_faces": sum(1 for c in threemf.read_painted_labels(data) if c)}
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["blender"].get("ok") and rep["watertight"] else 1


if __name__ == "__main__":
    sys.exit(main())
