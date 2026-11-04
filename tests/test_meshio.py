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


def test_color_scaling():
    a16 = np.array([[65535, 0, 32896]], dtype=np.uint16)
    assert np.allclose(meshio._to_255(a16), [[255, 0, 128]], atol=0.6)
    assert np.allclose(meshio._to_255(np.array([[1.0, 0.0, 0.5]])), [[255, 0, 127.5]])
    assert np.allclose(meshio._to_255(np.array([[200, 10, 0]], dtype=np.uint8)), [[200, 10, 0]])


def test_vertex_colours_roundtrip_srgb(tmp_path):
    """Stored COLOR_0 is linear (glTF spec); reading it back gives the same sRGB colour."""
    import trimesh as tm
    m = tm.creation.box((10, 10, 10))
    m.metadata["face_rgb"] = np.tile([200, 40, 40], (len(m.faces), 1)).astype(np.uint8)
    p = meshio.export_glb_with_colors(m, tmp_path / "c.glb", up="z")
    raw = tm.load(p, force="mesh").visual.vertex_colors[0][:3]
    assert raw[0] < 160          # linear value written, not the sRGB 200
    back = meshio.face_rgb(meshio.load(p))
    assert np.allclose(back[0], [200, 40, 40], atol=4)
    s = meshio.ColorSampler(p).sample(np.array([[0.0, 5.01, 0.0]]))[0][0]
    assert np.allclose(s, [200, 40, 40], atol=4)
