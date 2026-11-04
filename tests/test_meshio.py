import numpy as np
import trimesh

from studio import meshio


def test_color0_with_plain_material(tmp_path):
    """Blender exports COLOR_0 plus an untextured material; colours must survive loading."""
    b = trimesh.creation.box((10, 10, 10))
    v = b.vertices[b.faces].reshape(-1, 3)
    f = np.arange(len(v)).reshape(-1, 3)
    m = trimesh.Trimesh(v, f, process=False)
    red = np.tile([220, 30, 30, 255], (len(v) // 2, 1))
    blue = np.tile([30, 30, 220, 255], (len(v) - len(v) // 2, 1))
    m.visual = trimesh.visual.TextureVisuals(material=trimesh.visual.material.PBRMaterial(name="DefaultMaterial"))
    m.visual.vertex_attributes["color"] = np.vstack([red, blue]).astype(np.uint8)
    p = tmp_path / "x.glb"
    m.export(p, file_type="glb")
    loaded = meshio.load(p)
    cols = {tuple(c) for c in meshio.face_rgb(loaded)}
    assert len(cols) >= 2, cols
