"""Load any supported mesh into one trimesh with per-face colours; move it into the print frame.

Conventions
* Stored models (`model.glb`) are glTF, Y-up, any unit.
* The print frame is Z-up, millimetres, standing on z=0, centred on x/y (what Bambu Studio expects).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import trimesh

MESH_EXTS = {".glb", ".gltf", ".obj", ".stl", ".ply", ".3mf", ".off", ".step", ".stp"}
Z_UP_EXTS = {".stl", ".3mf", ".step", ".stp"}  # CAD/print formats are Z-up; glTF/OBJ/PLY are Y-up

# glTF Y-up -> print Z-up: rotate +90° about X (new z = old y)
Y_TO_Z = trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0])
Z_TO_Y = trimesh.transformations.rotation_matrix(-np.pi / 2, [1, 0, 0])


class MeshError(Exception):
    pass


def up_axis_for(path: Path) -> str:
    return "z" if path.suffix.lower() in Z_UP_EXTS else "y"


def _face_colors(geom: trimesh.Trimesh) -> np.ndarray:
    """RGB per face (uint8). Textures are sampled at vertex UVs."""
    n = len(geom.faces)
    vis = geom.visual
    try:
        if isinstance(vis, trimesh.visual.texture.TextureVisuals):
            if vis.uv is not None and getattr(vis.material, "image", None) is not None or \
                    getattr(vis.material, "baseColorTexture", None) is not None:
                cv = vis.to_color()
                vc = np.asarray(cv.vertex_colors)[:, :3].astype(float)
                return vc[geom.faces].mean(axis=1).astype(np.uint8)
            mat = vis.material
            base = getattr(mat, "baseColorFactor", None)
            if base is None:
                base = getattr(mat, "main_color", None)
            if base is not None:
                return np.tile(np.asarray(base)[:3].astype(np.uint8), (n, 1))
        elif isinstance(vis, trimesh.visual.color.ColorVisuals):
            if vis.kind == "vertex":
                vc = np.asarray(vis.vertex_colors)[:, :3].astype(float)
                return vc[geom.faces].mean(axis=1).astype(np.uint8)
            if vis.kind == "face":
                return np.asarray(vis.face_colors)[:, :3].astype(np.uint8)
    except Exception:
        pass
    return np.full((n, 3), 200, dtype=np.uint8)


def _load_step(path: Path) -> trimesh.Trimesh:
    try:
        from OCP.BRepMesh import BRepMesh_IncrementalMesh
        from OCP.STEPControl import STEPControl_Reader
        from OCP.StlAPI import StlAPI_Writer
    except ImportError as e:
        raise MeshError("STEP desteği bu kurulumda yok; STL veya GLB olarak yükle.") from e
    reader = STEPControl_Reader()
    if reader.ReadFile(str(path)) != 1:
        raise MeshError("STEP dosyası okunamadı.")
    reader.TransferRoots()
    shape = reader.OneShape()
    BRepMesh_IncrementalMesh(shape, 0.1, False, 0.3, True).Perform()
    out = path.with_suffix(".tmp.stl")
    StlAPI_Writer().Write(shape, str(out))
    m = trimesh.load_mesh(out, file_type="stl")
    out.unlink(missing_ok=True)
    return m


def load(path: Path) -> trimesh.Trimesh:
    """One mesh, transforms applied, `face_rgb` attribute set (N×3 uint8)."""
    ext = path.suffix.lower()
    if ext not in MESH_EXTS:
        raise MeshError("Desteklenmeyen dosya türü. GLB, OBJ, STL, PLY, 3MF veya STEP olmalı.")
    if ext in (".step", ".stp"):
        loaded = _load_step(path)
    else:
        try:
            loaded = trimesh.load(path, force=None, process=False)
        except Exception as e:
            raise MeshError(f"Model okunamadı ({type(e).__name__}).") from e
    geoms: list[trimesh.Trimesh] = []
    if isinstance(loaded, trimesh.Scene):
        for node in loaded.graph.nodes_geometry:
            transform, gname = loaded.graph[node]
            g = loaded.geometry[gname]
            if isinstance(g, trimesh.Trimesh) and len(g.faces):
                g = g.copy()
                g.apply_transform(transform)
                geoms.append(g)
    elif isinstance(loaded, trimesh.Trimesh):
        geoms = [loaded]
    if not geoms:
        raise MeshError("Dosyada üçgen ağ (mesh) yok.")
    colors = [_face_colors(g) for g in geoms]
    merged = trimesh.util.concatenate([trimesh.Trimesh(g.vertices, g.faces, process=False) for g in geoms])
    rgb = np.concatenate(colors, axis=0)
    merged.metadata["face_rgb"] = rgb
    if len(merged.faces) > 1_500_000:
        raise MeshError("Model çok yoğun (1,5 milyondan fazla üçgen).")
    return merged


def face_rgb(m: trimesh.Trimesh) -> np.ndarray:
    rgb = m.metadata.get("face_rgb")
    if rgb is None or len(rgb) != len(m.faces):
        return np.full((len(m.faces), 3), 200, dtype=np.uint8)
    return np.asarray(rgb, dtype=np.uint8)


def to_print_frame(m: trimesh.Trimesh, up: str, height_mm: float) -> trimesh.Trimesh:
    m = m.copy()
    rgb = face_rgb(m)
    if up == "y":
        m.apply_transform(Y_TO_Z)
    lo, hi = m.bounds
    h = float(hi[2] - lo[2])
    if h <= 0:
        raise MeshError("Modelin yüksekliği sıfır.")
    m.apply_scale(height_mm / h)
    lo, hi = m.bounds
    m.apply_translation([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])
    m.metadata["face_rgb"] = rgb
    return m


def export_preview_glb(m: trimesh.Trimesh, path: Path) -> Path:
    """Print-frame mesh back to Y-up glTF, geometry only, face order preserved."""
    g = trimesh.Trimesh(m.vertices.copy(), m.faces.copy(), process=False)
    g.apply_transform(Z_TO_Y)
    path.parent.mkdir(parents=True, exist_ok=True)
    g.export(path, file_type="glb")
    return path


def export_glb_with_colors(m: trimesh.Trimesh, path: Path, up: str = "y") -> Path:
    """Store an uploaded/edited mesh as model.glb (Y-up) keeping face colours as vertex colours."""
    rgb = face_rgb(m)
    v = m.vertices[m.faces].reshape(-1, 3)
    f = np.arange(len(v)).reshape(-1, 3)
    vc = np.repeat(np.c_[rgb, np.full(len(rgb), 255)], 3, axis=0).astype(np.uint8)
    g = trimesh.Trimesh(v, f, vertex_colors=vc, process=False)
    if up == "z":
        g.apply_transform(Z_TO_Y)
    path.parent.mkdir(parents=True, exist_ok=True)
    g.export(path, file_type="glb")
    return path


def save_npz(m: trimesh.Trimesh, path: Path, **extra) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, vertices=m.vertices, faces=m.faces, face_rgb=face_rgb(m), **extra)
    return path


def load_npz(path: Path) -> tuple[trimesh.Trimesh, dict]:
    d = np.load(path, allow_pickle=False)
    m = trimesh.Trimesh(d["vertices"], d["faces"], process=False)
    m.metadata["face_rgb"] = d["face_rgb"]
    extra = {k: d[k] for k in d.files if k not in ("vertices", "faces", "face_rgb")}
    return m, extra
