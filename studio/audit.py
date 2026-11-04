"""MOD-04 step 1: printability audit and repair on a print-frame mesh (Z-up, mm)."""
from __future__ import annotations

import numpy as np
import trimesh

from .meshio import face_rgb

BED_EPS_MM = 0.3


def basic_repair(m: trimesh.Trimesh) -> tuple[trimesh.Trimesh, list[str]]:
    """Cheap fixes that keep face colours aligned."""
    notes: list[str] = []
    rgb = face_rgb(m)
    m = m.copy()
    m.metadata["face_rgb"] = rgb
    before = len(m.faces)
    m.merge_vertices()
    keep = m.nondegenerate_faces() & m.unique_faces()
    if not keep.all():
        m.update_faces(keep)
        rgb = rgb[keep]
        notes.append(f"{int((~keep).sum())} bozuk/tekrarlı üçgen silindi")
    m.remove_unreferenced_vertices()
    trimesh.repair.fix_winding(m)
    trimesh.repair.fix_inversion(m)
    if not m.is_watertight:
        n0 = len(m.faces)
        trimesh.repair.fill_holes(m)
        added = len(m.faces) - n0
        if added > 0:
            fill = np.tile(rgb.mean(axis=0).astype(np.uint8), (added, 1))
            rgb = np.vstack([rgb, fill])
            notes.append(f"{added} üçgenle küçük delikler kapatıldı")
    m.metadata["face_rgb"] = rgb
    if len(m.faces) != before and not notes:
        notes.append("ağ temizlendi")
    return m, notes


def voxel_remesh(m: trimesh.Trimesh, max_voxels: int = 150) -> trimesh.Trimesh:
    """Rebuild a closed surface from a filled voxel grid; colours copied from the nearest old face."""
    ext = float(np.max(m.extents))
    pitch = max(ext / max_voxels, 0.15)
    vox = m.voxelized(pitch).fill()
    new = vox.marching_cubes
    new.apply_transform(vox.transform)
    # marching_cubes returns index space; vox.transform maps to world
    from .threemf import _decimate
    new = _decimate(trimesh.Trimesh(new.vertices, new.faces, process=True), 120000)
    trimesh.repair.fix_normals(new)
    from scipy.spatial import cKDTree
    _, tri = cKDTree(m.triangles_center).query(new.triangles_center, workers=-1)
    new.metadata["face_rgb"] = face_rgb(m)[tri]
    return new


def _edge_stats(m: trimesh.Trimesh) -> dict:
    edges = m.edges_sorted
    _, counts = np.unique(edges, axis=0, return_counts=True)
    return {"boundary_edges": int((counts == 1).sum()), "nonmanifold_edges": int((counts > 2).sum())}


def wall_thickness(m: trimesh.Trimesh, samples: int = 2500, min_mm: float = 0.8, seed: int = 0):
    """Cast rays inward from sampled surface points; distance to the opposite wall."""
    pts, fidx = trimesh.sample.sample_surface(m, samples, seed=seed)
    normals = m.face_normals[fidx]
    origins = pts - normals * 1e-3
    try:
        locs, ray_idx, _ = m.ray.intersects_location(origins, -normals, multiple_hits=False)
    except Exception:
        return {"checked": 0, "thin_fraction": None, "min_mm": None}, np.zeros(len(m.faces), bool)
    dist = np.full(len(pts), np.inf)
    if len(ray_idx):
        dist[ray_idx] = np.linalg.norm(locs - pts[ray_idx], axis=1)
    hit = np.isfinite(dist)
    thin = hit & (dist < min_mm)
    face_thin = np.zeros(len(m.faces), bool)
    face_thin[fidx[thin]] = True
    return {
        "checked": int(hit.sum()),
        "thin_fraction": float(thin.sum() / max(1, hit.sum())),
        "min_mm": float(dist[hit].min()) if hit.any() else None,
        "threshold_mm": min_mm,
    }, face_thin


def audit(m: trimesh.Trimesh, overhang_deg: float = 45.0, min_wall_mm: float = 0.8) -> tuple[dict, dict]:
    """Returns (report, per-face flag arrays)."""
    report: dict = {}
    es = _edge_stats(m)
    report.update(es)
    report["watertight"] = bool(m.is_watertight)
    report["winding_consistent"] = bool(m.is_winding_consistent)
    report["faces"] = int(len(m.faces))
    report["size_mm"] = [round(float(x), 2) for x in m.extents]
    report["volume_cm3"] = round(float(m.volume) / 1000, 2) if m.is_watertight else None

    # Overhangs: downward faces steeper than the limit, not lying on the bed.
    nz = m.face_normals[:, 2]
    zmin_face = m.vertices[m.faces][:, :, 2].min(axis=1)
    over = (nz < -np.cos(np.radians(overhang_deg))) & (zmin_face > BED_EPS_MM)
    area = m.area_faces
    report["overhang_deg"] = overhang_deg
    report["overhang_area_fraction"] = round(float(area[over].sum() / max(area.sum(), 1e-9)), 4)

    # Loose parts / floating islands.
    comps = m.split(only_watertight=False)
    vols = []
    floating = 0
    for c in comps:
        vols.append(abs(float(c.volume)) if c.is_watertight else float(c.area) * 0.1)
        if c.bounds[0][2] > BED_EPS_MM:
            floating += 1
    report["parts"] = len(comps)
    big = max(vols) if vols else 0
    report["tiny_parts"] = int(sum(1 for v in vols if big and v < 0.01 * big))
    report["floating_parts"] = int(floating)

    wt, thin = wall_thickness(m, min_mm=min_wall_mm)
    report["wall"] = wt

    problems = []
    if not report["watertight"]:
        problems.append("Model kapalı değil (su geçirmez değil); dilimleyici hata yapabilir.")
    if es["nonmanifold_edges"]:
        problems.append(f"{es['nonmanifold_edges']} kenar ikiden fazla yüze bağlı (non-manifold).")
    if report["overhang_area_fraction"] > 0.05:
        problems.append(f"Yüzeyin %{report['overhang_area_fraction'] * 100:.0f}'i {overhang_deg:.0f}°'den dik sarkıyor; destek gerekir.")
    if report["floating_parts"]:
        problems.append(f"{report['floating_parts']} parça tablaya değmiyor (havada).")
    if report["tiny_parts"]:
        problems.append(f"{report['tiny_parts']} çok küçük kopuk parça var.")
    if wt.get("thin_fraction") and wt["thin_fraction"] > 0.02:
        problems.append(f"Duvarların %{wt['thin_fraction'] * 100:.0f}'i {min_wall_mm} mm'den ince.")
    report["problems"] = problems
    # Printable = slicer can handle it; overhangs only need supports.
    report["is_printable"] = bool(report["watertight"] and not es["nonmanifold_edges"]
                                  and not report["floating_parts"]
                                  and not (wt.get("thin_fraction") or 0) > 0.15)
    return report, {"overhang": over, "thin": thin}
