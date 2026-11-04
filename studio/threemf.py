"""MOD-04 step 3: Bambu Studio compatible 3MF.

painted: one watertight body; each triangle carries the filament it is painted with, using the
  TriangleSelector bit-stream that Bambu Studio (`paint_color`) and PrusaSlicer/Orca
  (`slic3rpe:mmu_segmentation`) read. A whole triangle painted with extruder n encodes as:
  n=1 "4", n=2 "8", n=3 "0C", n=4 "1C", ... (2 bits split=0, then state bits, LSB first, nibbles
  written most-significant first).
parts: one closed solid per colour (from a labelled voxel grid), assembled as parts of one object;
  Metadata/model_settings.config gives each part its extruder.
"""
from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape

import numpy as np
import trimesh

from .meshio import face_rgb

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
 <Default Extension="config" ContentType="text/xml"/>
</Types>"""

RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>"""

MODEL_HEAD = ('<?xml version="1.0" encoding="UTF-8"?>\n'
              '<model unit="millimeter" xml:lang="en-US" '
              'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
              'xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06">\n'
              ' <metadata name="Application">Mom_Design</metadata>\n'
              ' <metadata name="Title">{title}</metadata>\n'
              ' <resources>\n')


def paint_code(extruder: int) -> str:
    """Serialised TriangleSelector state for an unsplit triangle painted with `extruder` (1..16)."""
    if extruder <= 0:
        return ""
    bits = [0, 0]                                  # split sides = 0 (leaf)
    if extruder < 3:
        bits += [extruder & 1, (extruder >> 1) & 1]
    else:
        n = extruder - 3
        bits += [1, 1] + [(n >> i) & 1 for i in range(4)]
    out = ""
    for off in range(0, len(bits), 4):
        nib = bits[off:off + 4] + [0] * (4 - len(bits[off:off + 4]))
        val = sum(b << i for i, b in enumerate(nib))
        out = "0123456789ABCDEF"[val] + out
    return out


def _mesh_xml(v: np.ndarray, f: np.ndarray, paint: list[str] | None = None) -> str:
    parts = ["   <mesh>\n    <vertices>\n"]
    parts.extend(f'     <vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>\n' for x, y, z in v)
    parts.append("    </vertices>\n    <triangles>\n")
    if paint is None:
        parts.extend(f'     <triangle v1="{a}" v2="{b}" v3="{c}"/>\n' for a, b, c in f)
    else:
        for (a, b, c), p in zip(f, paint):
            if p:
                parts.append(f'     <triangle v1="{a}" v2="{b}" v3="{c}" paint_color="{p}" '
                             f'slic3rpe:mmu_segmentation="{p}"/>\n')
            else:
                parts.append(f'     <triangle v1="{a}" v2="{b}" v3="{c}"/>\n')
    parts.append("    </triangles>\n   </mesh>\n")
    return "".join(parts)


def _settings_xml(obj_id: int, name: str, parts: list[tuple[int, str, int]], obj_extruder: int = 1) -> str:
    x = ['<?xml version="1.0" encoding="UTF-8"?>\n<config>\n', f' <object id="{obj_id}">\n',
         f'  <metadata key="name" value="{escape(name)}"/>\n', f'  <metadata key="extruder" value="{obj_extruder}"/>\n']
    for pid, pname, ext in parts:
        x += [f'  <part id="{pid}" subtype="normal_part">\n', f'   <metadata key="name" value="{escape(pname)}"/>\n',
              f'   <metadata key="extruder" value="{ext}"/>\n', '  </part>\n']
    x.append(" </object>\n</config>\n")
    return "".join(x)


def _zip(model_xml: str, settings_xml: str, palette: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("3D/3dmodel.model", model_xml)
        z.writestr("Metadata/model_settings.config", settings_xml)
        z.writestr("Metadata/mom_design_filaments.txt",
                   "".join(f"Filament {i + 1}: {c}\n" for i, c in enumerate(palette)))
    return buf.getvalue()


def painted_3mf(m: trimesh.Trimesh, labels: np.ndarray, palette: list[str], name: str = "model") -> bytes:
    """labels[i] = 0-based filament slot of face i."""
    paint = [paint_code(int(l) + 1) if l > 0 else "" for l in labels]
    xml = MODEL_HEAD.format(title=escape(name)) + '  <object id="1" type="model">\n' + \
        _mesh_xml(m.vertices, m.faces, paint) + "  </object>\n </resources>\n" + \
        ' <build>\n  <item objectid="1" printable="1"/>\n </build>\n</model>\n'
    return _zip(xml, _settings_xml(1, name, [(1, name, 1)]), palette)


def _decimate(m: trimesh.Trimesh, target: int = 60000) -> trimesh.Trimesh:
    """Marching cubes makes very dense staircases; smooth lightly and reduce."""
    try:
        trimesh.smoothing.filter_taubin(m, iterations=6)
        if len(m.faces) > target:
            m = m.simplify_quadric_decimation(face_count=target)
    except Exception:
        pass
    return m


def split_parts(m: trimesh.Trimesh, labels: np.ndarray, max_voxels: int = 128) -> list[tuple[int, trimesh.Trimesh]]:
    """Closed solid per label: fill a voxel grid, give each voxel the label of the nearest face."""
    ext = float(np.max(m.extents))
    pitch = max(ext / max_voxels, 0.2)
    vox = m.voxelized(pitch).fill()
    idx = vox.sparse_indices
    centers = vox.indices_to_points(idx)
    from scipy.spatial import cKDTree
    _, tri = cKDTree(m.triangles_center).query(centers, workers=-1)
    vlab = labels[tri]
    out = []
    for lab in np.unique(vlab):
        grid = np.zeros(vox.shape, bool)
        sel = idx[vlab == lab]
        grid[sel[:, 0], sel[:, 1], sel[:, 2]] = True
        part = trimesh.voxel.VoxelGrid(grid, transform=vox.transform).marching_cubes
        part.apply_transform(vox.transform)
        part = _decimate(trimesh.Trimesh(part.vertices, part.faces, process=True))
        trimesh.repair.fix_normals(part)
        if len(part.faces):
            out.append((int(lab), part))
    return out


def parts_3mf(parts: list[tuple[int, trimesh.Trimesh]], palette: list[str], name: str = "model") -> bytes:
    xml = [MODEL_HEAD.format(title=escape(name))]
    meta = []
    for i, (lab, p) in enumerate(parts, start=1):
        xml.append(f'  <object id="{i}" type="model">\n' + _mesh_xml(p.vertices, p.faces) + "  </object>\n")
        meta.append((i, f"renk {lab + 1}", lab + 1))
    asm = len(parts) + 1
    xml.append(f'  <object id="{asm}" type="model">\n   <components>\n')
    xml.extend(f'    <component objectid="{i}"/>\n' for i in range(1, len(parts) + 1))
    xml.append("   </components>\n  </object>\n </resources>\n")
    xml.append(f' <build>\n  <item objectid="{asm}" printable="1"/>\n </build>\n</model>\n')
    return _zip("".join(xml), _settings_xml(asm, name, meta), palette)


def parts_stl_zip(parts: list[tuple[int, trimesh.Trimesh]], palette: list[str], name: str = "model") -> bytes:
    """Fallback: one STL per colour. In Bambu Studio select all -> 'load as one object with parts'."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for lab, p in parts:
            z.writestr(f"{name}_renk{lab + 1}_{palette[lab].lstrip('#')}.stl", p.export(file_type="stl"))
    return buf.getvalue()


def stl_bytes(m: trimesh.Trimesh) -> bytes:
    return trimesh.Trimesh(m.vertices, m.faces, process=False).export(file_type="stl")


def read_painted_labels(data: bytes) -> list[str]:
    """Test helper: paint attributes of the first mesh, in order."""
    import re
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("3D/3dmodel.model").decode()
    return [m_.group(1) if m_ else "" for m_ in (re.search(r'paint_color="([0-9A-F]+)"', t)
                                                   for t in re.findall(r"<triangle [^>]*/>", xml))]


__all__ = ["paint_code", "painted_3mf", "split_parts", "parts_3mf", "parts_stl_zip", "stl_bytes", "face_rgb"]
