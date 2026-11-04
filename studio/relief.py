"""2D drawing -> flat, printable relief (keychain, plate, cookie cutter). Local, CPU only, no fal cost.

The drawing is turned into a voxel volume (0.3 mm), meshed with marching cubes (closed and manifold by
construction), and coloured in two filaments: the plate and the raised lines.
Output frame: millimetres, Z up, standing on z=0, centred on x/y.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import trimesh
from PIL import Image
from scipy import ndimage

PITCH_MM = 0.3
KINDS = ("keychain", "plate", "cookie_cutter")


class ReliefError(Exception):
    """User-facing (Turkish)."""


def _disk(r_px: float) -> np.ndarray:
    r = max(1, int(round(r_px)))
    y, x = np.ogrid[-r:r + 1, -r:r + 1]
    return x * x + y * y <= r * r


def _grow(mask: np.ndarray, mm: float) -> np.ndarray:
    return ndimage.binary_dilation(mask, structure=_disk(mm / PITCH_MM)) if mm > 0 else mask


def _shrink(mask: np.ndarray, mm: float) -> np.ndarray:
    return ndimage.binary_erosion(mask, structure=_disk(mm / PITCH_MM)) if mm > 0 else mask


def _ink(arr: np.ndarray) -> np.ndarray:
    # distance from paper white; paper photos are rarely pure white, so use the brightest 10 % as "paper"
    paper = np.percentile(arr.reshape(-1, 3), 90, axis=0)
    dist = np.linalg.norm(arr - paper, axis=2)
    ink = dist > max(60.0, 0.35 * dist.max())
    return ndimage.binary_opening(ink, structure=_disk(1))          # drop specks


def ink_mask(im: Image.Image, width_mm: float) -> tuple[np.ndarray, tuple[int, int, int]]:
    """Strokes of the drawing at PITCH_MM per pixel (the drawing's own width = width_mm), and their colour."""
    im = im.convert("RGBA")
    bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
    rgb = Image.alpha_composite(bg, im).convert("RGB")
    probe = rgb.copy()
    probe.thumbnail((600, 600))
    ink = _ink(np.asarray(probe).astype(float))
    if ink.sum() < 30:
        raise ReliefError("Çizimde çizgi bulunamadı; daha koyu bir kalemle çiz.")
    ys, xs = np.nonzero(ink)
    k = rgb.width / probe.width
    box = (max(0, int((xs.min() - 2) * k)), max(0, int((ys.min() - 2) * k)),
           min(rgb.width, int((xs.max() + 3) * k)), min(rgb.height, int((ys.max() + 3) * k)))
    rgb = rgb.crop(box)
    w_px = max(40, int(round(width_mm / PITCH_MM)))
    h_px = max(1, int(round(rgb.height * w_px / rgb.width)))
    if h_px > 600:                                                  # tall drawings: width_mm is the long side
        h_px, w_px = 600, max(1, int(round(rgb.width * 600 / rgb.height)))
    small = np.asarray(rgb.resize((w_px, h_px), Image.LANCZOS)).astype(float)
    ink = _ink(small)
    if ink.sum() < 30:
        raise ReliefError("Çizimde çizgi bulunamadı; daha koyu bir kalemle çiz.")
    col = np.median(small[ink], axis=0).astype(int)
    return ink, (int(col[0]), int(col[1]), int(col[2]))


def build(image_path: Path, *, kind: str = "keychain", width_mm: float = 50, base_mm: float = 3.0,
          line_mm: float = 1.2, hole: bool | None = None, line_rgb=None, base_rgb=(245, 245, 240)) -> trimesh.Trimesh:
    if kind not in KINDS:
        kind = "keychain"
    width_mm = float(np.clip(width_mm, 20, 180))
    base_mm = float(np.clip(base_mm, 1.5, 8))
    line_mm = float(np.clip(line_mm, 0.6, 4))
    ink, ink_rgb = ink_mask(Image.open(image_path), width_mm)
    line_rgb = tuple(line_rgb or ink_rgb)
    if sum(abs(a - b) for a, b in zip(line_rgb, base_rgb)) < 120:   # lines must contrast with the plate
        line_rgb = (30, 30, 32)

    pad = int(round(8 / PITCH_MM))
    ink = np.pad(ink, pad)
    # filled outline of everything drawn, plus a 1.5 mm rim so lines never sit on the edge
    sil = ndimage.binary_fill_holes(ndimage.binary_closing(ink, structure=_disk(2.0 / PITCH_MM)))
    sil = _grow(sil, 1.5)
    lab, n = ndimage.label(sil)
    if n > 1:   # drop crumbs (< 2 % of the biggest piece)
        sizes = ndimage.sum(sil, lab, range(1, n + 1))
        sil = np.isin(lab, 1 + np.flatnonzero(sizes >= 0.02 * sizes.max()))
    lines = _grow(ink, 0.3) & sil                                   # at least ~1 mm wide on the nozzle

    layers: list[tuple[np.ndarray, int]] = []                       # (mask, layer count)
    if kind == "cookie_cutter":
        wall = _grow(sil, 0.6) & ~_shrink(sil, 0.6)
        flange = _grow(sil, 3.0) & ~_shrink(sil, 0.6)
        layers = [(flange, round(2.0 / PITCH_MM)), (wall, round(12.0 / PITCH_MM))]
        hole = False
    else:
        if hole is None:
            hole = kind == "keychain"
        if hole:
            rows = np.flatnonzero(sil.any(axis=1))
            top = rows[0]
            cols = np.flatnonzero(sil[top + int(1 / PITCH_MM)] if top + 3 < sil.shape[0] else sil[top])
            cx = int(cols.mean()) if len(cols) else sil.shape[1] // 2
            r_tab, r_hole = 5.0 / PITCH_MM, 2.2 / PITCH_MM
            cy = top - int(2.0 / PITCH_MM)
            yy, xx = np.ogrid[:sil.shape[0], :sil.shape[1]]
            d2 = (yy - cy) ** 2 + (xx - cx) ** 2
            sil = (sil | (d2 <= r_tab ** 2)) & ~(d2 <= r_hole ** 2)
            lines &= ~(d2 <= (r_hole + 1.2 / PITCH_MM) ** 2)
        layers = [(sil, round(base_mm / PITCH_MM)), (lines, round(line_mm / PITCH_MM))]

    nz = sum(c for _, c in layers)
    vol = np.zeros((sil.shape[1] + 2, sil.shape[0] + 2, nz + 2), dtype=np.uint8)   # (x, y, z), padded
    z = 1
    for mask, count in layers:
        m = mask.T[:, ::-1]                                          # image rows -> +y up
        vol[1:-1, 1:-1, z:z + count] = m[:, :, None]
        z += count
    from skimage.measure import marching_cubes
    verts, faces, _n, _v = marching_cubes(vol.astype(np.float32), 0.5, spacing=(PITCH_MM,) * 3)
    mesh = trimesh.Trimesh(verts, faces, process=True)
    mesh.fix_normals()
    if not mesh.is_watertight:
        raise ReliefError("Çizimden kapalı bir model çıkmadı; çizgileri biraz daha kalın çiz.")
    lo, hi = mesh.bounds
    mesh.apply_translation([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])

    cz = mesh.triangles_center[:, 2]
    split = layers[0][1] * PITCH_MM + PITCH_MM * 0.5
    raised = cz > split
    rgb = np.where(raised[:, None], np.array(line_rgb), np.array(base_rgb)).astype(np.uint8)
    if kind == "cookie_cutter":
        rgb[:] = base_rgb
    mesh.metadata["face_rgb"] = rgb
    mesh.metadata["relief"] = {"kind": kind, "width_mm": round(float(hi[0] - lo[0]), 1),
                               "depth_mm": round(float(hi[1] - lo[1]), 1), "height_mm": round(float(hi[2] - lo[2]), 2),
                               "hole": bool(hole), "line_rgb": list(line_rgb), "base_rgb": list(base_rgb)}
    return mesh
