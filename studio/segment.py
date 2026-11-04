"""MOD-04 step 2: colour regions for AMS filament slots (k <= 4 by default)."""
from __future__ import annotations

import numpy as np
import trimesh
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from .meshio import face_rgb


def rgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    c = rgb.astype(float) / 255.0
    c = np.where(c > 0.04045, ((c + 0.055) / 1.055) ** 2.4, c / 12.92)
    m = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]])
    xyz = c @ m.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[:, 1] - 16, 500 * (f[:, 0] - f[:, 1]), 200 * (f[:, 1] - f[:, 2])], axis=1)


def kmeans(x: np.ndarray, k: int, weights: np.ndarray, iters: int = 30, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    n = len(x)
    k = max(1, min(k, n))
    centers = [x[rng.choice(n, p=weights / weights.sum())]]
    for _ in range(1, k):
        d = np.min(((x[:, None, :] - np.array(centers)[None]) ** 2).sum(-1), axis=1) * weights
        if d.sum() <= 0:
            centers.append(x[rng.integers(n)])
        else:
            centers.append(x[rng.choice(n, p=d / d.sum())])
    centers = np.array(centers)
    labels = np.zeros(n, int)
    for _ in range(iters):
        d = ((x[:, None, :] - centers[None]) ** 2).sum(-1)
        new = d.argmin(1)
        if (new == labels).all() and _ > 0:
            break
        labels = new
        for j in range(k):
            w = weights[labels == j]
            if w.sum() > 0:
                centers[j] = (x[labels == j] * w[:, None]).sum(0) / w.sum()
    return labels, centers


def _adjacency(m: trimesh.Trimesh):
    adj = m.face_adjacency
    n = len(m.faces)
    return adj, n


def smooth_labels(m: trimesh.Trimesh, labels: np.ndarray, k: int, rounds: int = 3) -> np.ndarray:
    adj, n = _adjacency(m)
    labels = labels.copy()
    for _ in range(rounds):
        votes = np.zeros((n, k))
        votes[np.arange(n), labels] += 1.5          # a face's own colour counts a bit more
        np.add.at(votes, (adj[:, 0], labels[adj[:, 1]]), 1)
        np.add.at(votes, (adj[:, 1], labels[adj[:, 0]]), 1)
        labels = votes.argmax(1)
    return labels


def regions(m: trimesh.Trimesh, labels: np.ndarray) -> np.ndarray:
    """Connected patches of the same label -> region id per face."""
    adj, n = _adjacency(m)
    same = labels[adj[:, 0]] == labels[adj[:, 1]]
    a = adj[same]
    g = coo_matrix((np.ones(len(a)), (a[:, 0], a[:, 1])), shape=(n, n))
    _, reg = connected_components(g, directed=False)
    return reg


def merge_small(m: trimesh.Trimesh, labels: np.ndarray, min_fraction: float = 0.002, rounds: int = 3) -> np.ndarray:
    area = m.area_faces
    total = area.sum()
    adj, n = _adjacency(m)
    labels = labels.copy()
    for _ in range(rounds):
        reg = regions(m, labels)
        reg_area = np.bincount(reg, weights=area)
        small = reg_area[reg] < min_fraction * total
        if not small.any():
            break
        # for each small face pick the most common neighbouring label from a big region
        k = labels.max() + 1
        votes = np.zeros((n, k))
        ok0 = ~small[adj[:, 1]]
        ok1 = ~small[adj[:, 0]]
        np.add.at(votes, (adj[ok0, 0], labels[adj[ok0, 1]]), area[adj[ok0, 1]])
        np.add.at(votes, (adj[ok1, 1], labels[adj[ok1, 0]]), area[adj[ok1, 0]])
        has = votes.sum(1) > 0
        upd = small & has
        labels[upd] = votes[upd].argmax(1)
    return labels


def segment(m: trimesh.Trimesh, k: int = 3, seed: int = 0) -> dict:
    """k colour groups -> labels per face, palette (#rrggbb per slot), region ids."""
    rgb = face_rgb(m)
    lab = rgb_to_lab(rgb)
    area = m.area_faces + 1e-12
    if len(np.unique(rgb, axis=0)) <= 1:
        k = 1
    labels, _ = kmeans(lab, k, area, seed=seed)
    k_eff = labels.max() + 1
    labels = smooth_labels(m, labels, k_eff)
    labels = merge_small(m, labels)
    # compact label ids, largest area first (slot 1 = main colour)
    used = np.unique(labels)
    order = sorted(used, key=lambda j: -area[labels == j].sum())
    remap = {old: new for new, old in enumerate(order)}
    labels = np.vectorize(remap.get)(labels).astype(np.int32)
    palette = []
    for j in range(len(order)):
        sel = labels == j
        c = (rgb[sel] * area[sel, None]).sum(0) / area[sel].sum()
        palette.append("#%02x%02x%02x" % tuple(int(v) for v in c))
    return {"labels": labels, "palette": palette, "regions": regions(m, labels).astype(np.int32)}
