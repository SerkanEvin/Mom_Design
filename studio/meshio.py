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


def _to_255(a: np.ndarray) -> np.ndarray:
    """glTF COLOR_0 may be uint8, uint16 (Blender) or float 0..1."""
    if a.dtype == np.uint16:
        return a.astype(float) / 257.0
    if np.issubdtype(a.dtype, np.floating):
        return a.astype(float) * (255.0 if a.max() <= 1.0 + 1e-6 else 1.0)
    return a.astype(float)


def srgb_to_linear255(c: np.ndarray) -> np.ndarray:
    x = np.asarray(c, dtype=float) / 255.0
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4) * 255.0


def linear_to_srgb255(c: np.ndarray) -> np.ndarray:
    x = np.clip(np.asarray(c, dtype=float) / 255.0, 0, 1)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * x ** (1 / 2.4) - 0.055) * 255.0


def _face_colors(geom: trimesh.Trimesh, linear: bool = False) -> np.ndarray:
    """sRGB per face (uint8). Textures are sampled at vertex UVs. `linear`: vertex colours are glTF COLOR_0,
    which the glTF spec defines as linear, so they are converted back to sRGB."""
    n = len(geom.faces)
    fix = linear_to_srgb255 if linear else (lambda a: a)
    vis = geom.visual
    try:
        if isinstance(vis, trimesh.visual.texture.TextureVisuals):
            mat = vis.material
            has_tex = getattr(mat, "image", None) is not None or getattr(mat, "baseColorTexture", None) is not None
            if vis.uv is not None and has_tex:
                cv = vis.to_color()
                vc = np.asarray(cv.vertex_colors)[:, :3].astype(float)
                return vc[geom.faces].mean(axis=1).astype(np.uint8)
            # glTF COLOR_0 next to a plain material (e.g. Blender export) lands here
            attr = getattr(vis, "vertex_attributes", {}).get("color")
            if attr is not None and len(attr) == len(geom.vertices):
                vc = _to_255(np.asarray(attr)[:, :3])
                return np.clip(fix(vc[geom.faces].mean(axis=1)), 0, 255).astype(np.uint8)
            base = getattr(mat, "baseColorFactor", None)
            if base is None:
                base = getattr(mat, "main_color", None)
            if base is not None:
                return np.tile(np.asarray(base)[:3].astype(np.uint8), (n, 1))
        elif isinstance(vis, trimesh.visual.color.ColorVisuals):
            if vis.kind == "vertex":
                vc = np.asarray(vis.vertex_colors)[:, :3].astype(float)
                return np.clip(fix(vc[geom.faces].mean(axis=1)), 0, 255).astype(np.uint8)
            if vis.kind == "face":
                return np.clip(fix(np.asarray(vis.face_colors)[:, :3]), 0, 255).astype(np.uint8)
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
    linear = ext in (".glb", ".gltf")
    if ext in (".step", ".stp"):
        loaded = _load_step(path)
    else:
        try:
            loaded = trimesh.load(path, force=None, process=False)
        except Exception as e:
            raise MeshError(f"Model okunamadı ({type(e).__name__}).") from e
    geoms: list[trimesh.Trimesh] = []
    colors: list[np.ndarray] = []
    if isinstance(loaded, trimesh.Scene):
        for node in loaded.graph.nodes_geometry:
            transform, gname = loaded.graph[node]
            g = loaded.geometry[gname]
            if isinstance(g, trimesh.Trimesh) and len(g.faces):
                colors.append(_face_colors(g, linear))   # before copy(): copy drops glTF vertex attributes
                g = trimesh.Trimesh(g.vertices.copy(), g.faces.copy(), process=False)
                g.apply_transform(transform)
                geoms.append(g)
    elif isinstance(loaded, trimesh.Trimesh):
        geoms = [loaded]
        colors = [_face_colors(loaded, linear)]
    if not geoms:
        raise MeshError("Dosyada üçgen ağ (mesh) yok.")
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
    lin = np.clip(np.round(srgb_to_linear255(rgb)), 0, 255)       # glTF COLOR_0 is linear
    vc = np.repeat(np.c_[lin, np.full(len(lin), 255)], 3, axis=0).astype(np.uint8)
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


# ------------------------------------------------------------------ colour transfer (after Blender edits)

def _scene_parts(path: Path):
    loaded = trimesh.load(path, process=False)
    out = []
    if isinstance(loaded, trimesh.Scene):
        for node in loaded.graph.nodes_geometry:
            transform, gname = loaded.graph[node]
            g = loaded.geometry[gname]
            if isinstance(g, trimesh.Trimesh) and len(g.faces):
                out.append((g, transform))
    elif isinstance(loaded, trimesh.Trimesh):
        out.append((loaded, np.eye(4)))
    return out


def geometry_only(path: Path) -> trimesh.Trimesh:
    parts = []
    for g, tf in _scene_parts(path):
        m = trimesh.Trimesh(g.vertices.copy(), g.faces.copy(), process=False)
        m.apply_transform(tf)
        parts.append(m)
    if not parts:
        raise MeshError("Dosyada üçgen ağ (mesh) yok.")
    return trimesh.util.concatenate(parts)


class ColorSampler:
    """Colour of the reference surface nearest to any point: texture sampled at the exact spot when the
    reference is textured, otherwise its face colour."""

    def __init__(self, path: Path):
        meshes, rgbs, self.textured = [], [], []
        offset = 0
        for g, tf in _scene_parts(path):
            rgb = _face_colors(g, linear=True)
            m = trimesh.Trimesh(g.vertices.copy(), g.faces.copy(), process=False)
            m.apply_transform(tf)
            vis = g.visual
            mat = getattr(vis, "material", None)
            img = getattr(mat, "baseColorTexture", None) if mat is not None else None
            if img is None and mat is not None:
                img = getattr(mat, "image", None)
            uv = getattr(vis, "uv", None)
            if img is not None and uv is not None and len(uv) == len(g.vertices):
                self.textured.append((offset, offset + len(g.faces), np.asarray(uv), g.faces.copy(), img))
            meshes.append(m)
            rgbs.append(rgb)
            offset += len(g.faces)
        if not meshes:
            raise MeshError("Renk kaynağı boş.")
        self.mesh = trimesh.util.concatenate(meshes)
        self.rgb = np.concatenate(rgbs).astype(float)

    def sample(self, points: np.ndarray, chunk: int = 20000) -> tuple[np.ndarray, np.ndarray]:
        rgb_out = np.zeros((len(points), 3))
        dist_out = np.zeros(len(points))
        for s in range(0, len(points), chunk):
            pts = points[s:s + chunk]
            closest, dist, tri = trimesh.proximity.closest_point(self.mesh, pts)
            rgb = self.rgb[tri].copy()
            for lo, hi, uv, faces, img in self.textured:
                sel = (tri >= lo) & (tri < hi)
                if not sel.any():
                    continue
                local = tri[sel] - lo
                bary = trimesh.triangles.points_to_barycentric(self.mesh.triangles[tri[sel]], closest[sel])
                uvp = (bary[:, :, None] * uv[faces[local]]).sum(axis=1)
                rgb[sel] = trimesh.visual.color.uv_to_color(uvp, img)[:, :3]
            rgb_out[s:s + chunk] = rgb
            dist_out[s:s + chunk] = dist
        return rgb_out, dist_out


def transfer_colors(ref_path: Path, result_path: Path, out_path: Path, tol: float = 0.5,
                    new_rgb=(255, 255, 255)) -> dict:
    """Paint the edited mesh with the reference's colours; faces farther than `tol` (new parts: text,
    base, added shapes) get `new_rgb` so they can get their own filament later."""
    result = geometry_only(result_path)
    rgb, dist = ColorSampler(ref_path).sample(result.triangles_center)
    new = dist > tol
    rgb[new] = new_rgb
    result.metadata["face_rgb"] = np.clip(rgb, 0, 255).astype(np.uint8)
    export_glb_with_colors(result, out_path, up="y")
    return {"faces": int(len(result.faces)), "new_faces": int(new.sum())}
