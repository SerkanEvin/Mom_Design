"""Runs INSIDE Blender: blender -b --factory-startup --python blender_runner.py -- job.json result.json

job.json: {"input": path, "height_mm": float|null, "ops": [...validated...],
           "render_dir": path|null, "output": path|null}
Only the operations below exist; there is no way to run other code from a job file.

Coordinates: millimetres, Z up, model on z=0. Placement can be given as an *anchor* on one of the four
preview renders: {"view": "front"|"right"|"back"|"top", "u": 0..100, "v": 0..100} (u = left->right,
v = top->bottom of that image). The runner casts a ray from that view to find the real surface point.

A hidden reference copy ("ref") of the input follows every whole-object transform. It is exported next to
the result (<output>.ref.glb) so the caller can copy the original colours onto the edited surface.
"""
import json
import math
import os
import sys
import traceback

import bmesh
import bpy
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:]
SPEC = json.load(open(argv[0]))
RESULT = argv[1]
NOTES = []

# camera rotation (deg) per view; the image's right/up axes and the viewing direction follow from it
VIEWS = {
    "front": {"rot": (90, 0, 0), "right": Vector((1, 0, 0)), "up": Vector((0, 0, 1)), "dir": Vector((0, 1, 0))},
    "right": {"rot": (90, 0, 90), "right": Vector((0, 1, 0)), "up": Vector((0, 0, 1)), "dir": Vector((-1, 0, 0))},
    "back": {"rot": (90, 0, 180), "right": Vector((-1, 0, 0)), "up": Vector((0, 0, 1)), "dir": Vector((0, -1, 0))},
    "top": {"rot": (0, 0, 0), "right": Vector((1, 0, 0)), "up": Vector((0, 1, 0)), "dir": Vector((0, 0, -1))},
}


def active():
    return bpy.context.view_layer.objects.active


def select_only(obj):
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def load(path):
    ext = path.lower().rsplit(".", 1)[-1]
    if ext in ("glb", "gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == "stl":
        bpy.ops.wm.stl_import(filepath=path)
    elif ext == "obj":
        bpy.ops.wm.obj_import(filepath=path)
    elif ext == "ply":
        bpy.ops.wm.ply_import(filepath=path)
    else:
        raise RuntimeError("unsupported input " + ext)
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    if not meshes:
        raise RuntimeError("no mesh in file")
    for o in meshes:
        o.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    bpy.ops.object.parent_clear(type="CLEAR_KEEP_TRANSFORM")
    if len(meshes) > 1:
        bpy.ops.object.join()
    obj = active()
    for o in list(bpy.context.scene.objects):
        if o is not obj:
            bpy.data.objects.remove(o, do_unlink=True)
    select_only(obj)
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    return obj


def weld(obj, dist=1e-3):
    """Merge duplicate points (per-face colour files and texture seams split them); booleans need it."""
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    before = len(bm.verts)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=dist)
    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()
    return before - len(obj.data.vertices)


def bbox(obj):
    pts = [obj.matrix_world @ v.co for v in obj.data.vertices]
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    return lo, hi


def translate(obj, off):
    select_only(obj)
    obj.location = obj.location + off
    bpy.ops.object.transform_apply(location=True)


def normalise(obj, height_mm):
    lo, hi = bbox(obj)
    h = hi.z - lo.z
    if h <= 0:
        raise RuntimeError("zero height")
    s = height_mm / h
    select_only(obj)
    obj.scale = (s, s, s)
    bpy.ops.object.transform_apply(scale=True)
    lo, hi = bbox(obj)
    translate(obj, Vector((-(lo.x + hi.x) / 2, -(lo.y + hi.y) / 2, -lo.z)))


def apply_mod(obj, mod):
    select_only(obj)
    bpy.ops.object.modifier_apply(modifier=mod.name)


# ------------------------------------------------------------------ view frame + ray casting

def view_frame(obj):
    """Same camera framing as render(): centre and ortho size from the bbox."""
    lo, hi = bbox(obj)
    return (lo + hi) / 2, max(hi - lo) * 1.35


FRAME = {}


def ray_point(view, u, v):
    """Origin far outside the model on the image point (u, v in 0..100) and the viewing direction."""
    c, size = FRAME["c"], FRAME["size"]
    vw = VIEWS[view]
    p = c + vw["right"] * ((u / 100.0 - 0.5) * size) + vw["up"] * ((0.5 - v / 100.0) * size)
    return p - vw["dir"] * (size * 2), vw["dir"]


def cast(obj, origin, direction, max_dist=1e5):
    ok, loc, normal, _ = obj.ray_cast(origin, direction, distance=max_dist)
    return (loc, normal) if ok else (None, None)


def hit(obj, anchor):
    origin, d = ray_point(anchor["view"], anchor["u"], anchor["v"])
    loc, n = cast(obj, origin, d)
    if loc is None:
        raise RuntimeError(f"no surface at {anchor['view']} ({anchor['u']:.0f}, {anchor['v']:.0f})")
    return loc, n, d


def thickness_from(obj, loc, d):
    """Distance from an entry point to where the ray leaves the body again."""
    loc2, _ = cast(obj, loc + d * 0.05, d)
    return (loc2 - loc).length if loc2 is not None else 1e9


# ------------------------------------------------------------------ primitives, booleans, text

def primitive(shape, size, center, rot):
    if shape == "cube":
        bpy.ops.mesh.primitive_cube_add(size=1)
    elif shape == "cylinder":
        bpy.ops.mesh.primitive_cylinder_add(radius=0.5, depth=1, vertices=64)
    elif shape == "sphere":
        bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, segments=64, ring_count=32)
    elif shape == "cone":
        bpy.ops.mesh.primitive_cone_add(radius1=0.5, radius2=0, depth=1, vertices=64)
    elif shape == "torus":
        bpy.ops.mesh.primitive_torus_add(major_radius=0.35, minor_radius=0.15)
    tool = active()
    tool.scale = size
    tool.rotation_euler = [math.radians(a) for a in rot]
    tool.location = center
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    return tool


def boolean(target, tool, operation):
    mod = target.modifiers.new("bool", "BOOLEAN")
    mod.operation = operation
    mod.object = tool
    try:
        mod.solver = "EXACT"
    except Exception:
        pass
    apply_mod(target, mod)
    bpy.data.objects.remove(tool, do_unlink=True)
    select_only(target)


def join(target, tool):
    bpy.ops.object.select_all(action="DESELECT")
    tool.select_set(True)
    target.select_set(True)
    bpy.context.view_layer.objects.active = target
    bpy.ops.object.join()


def primitive_center(obj, o):
    if o.get("anchor"):
        loc, _n, d = hit(obj, o["anchor"])
        return loc + d * o.get("depth_into_mm", 0.0)   # positive = into the body along the view direction
    return Vector(o["center_mm"])


def text_object(text, size_mm, max_width_mm):
    bpy.ops.object.text_add()
    t = active()
    t.data.body = text.replace("\\n", "\n")
    t.data.size = size_mm
    t.data.align_x = "CENTER"
    t.data.align_y = "CENTER"
    t.data.extrude = 0.5
    bpy.ops.object.convert(target="MESH")
    t = active()
    w, h = t.dimensions.x, t.dimensions.y
    scale = 1.0
    if max_width_mm and w > max_width_mm:
        scale = max_width_mm / w
        NOTES.append(f"yazı {w:.0f} mm genişti, {max_width_mm:.0f} mm'ye sığdırıldı")
    # normalise: footprint centred on origin, depth from z=0 (back) to z=1 (front)
    select_only(t)
    t.scale = (scale, scale, 1.0)
    bpy.ops.object.transform_apply(scale=True)
    lo, hi = bbox(t)
    translate(t, Vector((-(lo.x + hi.x) / 2, -(lo.y + hi.y) / 2, -lo.z)))
    lo, hi = bbox(t)
    t.scale = (1, 1, 1.0 / max(hi.z - lo.z, 1e-6))
    bpy.ops.object.transform_apply(scale=True)
    return t, (hi.x - lo.x), (hi.y - lo.y)


def emboss_text(obj, o):
    """Text facing the chosen view, sized to fit, touching the real surface across its whole footprint."""
    view = o["anchor"]["view"]
    vw = VIEWS[view]
    lo, hi = bbox(obj)
    across = abs((hi - lo).dot(vw["right"]))
    max_w = o.get("max_width_mm") or across * 0.8
    t, w, h = text_object(o["text"], o["size_mm"], max_w)
    centre, _n, d = hit(obj, o["anchor"])
    # probe the footprint: where is the surface under each part of the text (along the view direction)?
    depths, exits = [], []
    origin0 = centre - d * (FRAME["size"] * 2)
    for fu in (-0.5, -0.25, 0.0, 0.25, 0.5):
        for fv in (-0.5, 0.0, 0.5):
            p = origin0 + vw["right"] * (fu * w) + vw["up"] * (fv * h)
            loc, _ = cast(obj, p, d)
            if loc is None:
                continue
            depths.append((loc - origin0).dot(d))
            exits.append((loc - origin0).dot(d) + thickness_from(obj, loc, d))
    if len(depths) < 6:
        NOTES.append("yazının bir kısmı yüzeyin dışına taşıyor")
    if not depths:
        raise RuntimeError("yazı için yüzey bulunamadı")
    t_out, t_in = min(depths), max(depths)       # outermost / innermost surface point under the text
    depth = o["depth_mm"]
    if depth >= 0:   # emboss: from depth mm in front of the outermost point to 0.8 mm inside the innermost
        front, back = t_out - depth, t_in + 0.8
        back = min(back, min(exits) - 0.6) if exits else back
        back = max(back, t_out + 0.3)
    else:            # engrave: from outside to |depth| below the mid surface
        front, back = t_out - 1.0, (t_out + t_in) / 2 + abs(depth)
    if t_in - t_out > 4:
        NOTES.append("yüzey yazının altında çok kavisli; yazı düz bir plaka gibi oturur")
    # orient: text +Z (front face) must point back toward the camera (-d); image right/up = text x/y
    select_only(t)
    t.scale = (1, 1, back - front)
    bpy.ops.object.transform_apply(scale=True)
    t.rotation_euler = [math.radians(a) for a in vw["rot"]]
    bpy.ops.object.transform_apply(rotation=True)
    # after rotation the text spans 0..(back-front) along -d; put its front face at depth `front`
    lo_t, hi_t = bbox(t)
    front_now = min((Vector((x, y, z)) - origin0).dot(d) for x in (lo_t.x, hi_t.x) for y in (lo_t.y, hi_t.y)
                    for z in (lo_t.z, hi_t.z))
    mid_now = (lo_t + hi_t) / 2
    in_plane = (mid_now - origin0) - d * (mid_now - origin0).dot(d)
    target_plane = (centre - origin0) - d * (centre - origin0).dot(d)
    translate(t, (target_plane - in_plane) + d * (front - front_now))
    boolean(obj, t, "UNION" if depth >= 0 else "DIFFERENCE")
    return active()


def remove_loose_parts(obj, min_fraction):
    select_only(obj)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.separate(type="LOOSE")
    bpy.ops.object.mode_set(mode="OBJECT")
    parts = [p for p in bpy.context.selected_objects if p.type == "MESH"]
    vols = []
    for p in parts:
        bm = bmesh.new()
        bm.from_mesh(p.data)
        vols.append(abs(bm.calc_volume()))
        bm.free()
    biggest = max(vols)
    keep = [p for p, v in zip(parts, vols) if v >= min_fraction * biggest]
    removed = 0
    for p, v in zip(parts, vols):
        if p not in keep:
            bpy.data.objects.remove(p, do_unlink=True)
            removed += 1
    bpy.ops.object.select_all(action="DESELECT")
    for p in keep:
        p.select_set(True)
    bpy.context.view_layer.objects.active = keep[0]
    if len(keep) > 1:
        bpy.ops.object.join()
    NOTES.append(f"{removed} küçük kopuk parça silindi")
    return active()


def whole_object(objs, fn):
    for ob in objs:
        if ob is not None:
            select_only(ob)
            fn(ob)


def run_ops(obj, ref, ops):
    for o in ops:
        op = o["op"]
        select_only(obj)
        if op == "scale":
            def f(ob):
                ob.scale = o["factor"]
                bpy.ops.object.transform_apply(scale=True)
            whole_object([obj, ref], f)
        elif op == "rotate":
            def f(ob):
                ob.rotation_euler = [math.radians(a) for a in o["degrees"]]
                bpy.ops.object.transform_apply(rotation=True)
            whole_object([obj, ref], f)
        elif op == "translate":
            for ob in (obj, ref):
                translate(ob, Vector(o["mm"]))
        elif op in ("boolean_difference", "boolean_union", "add_primitive"):
            tool = primitive(o["shape"], o["size_mm"], primitive_center(obj, o), o["rotation_deg"])
            if op == "add_primitive":
                join(obj, tool)
            else:
                boolean(obj, tool, "DIFFERENCE" if op == "boolean_difference" else "UNION")
        elif op == "remesh_voxel":
            m = obj.modifiers.new("remesh", "REMESH")
            m.mode = "VOXEL"
            m.voxel_size = o["voxel_mm"]
            apply_mod(obj, m)
        elif op == "decimate":
            m = obj.modifiers.new("dec", "DECIMATE")
            m.ratio = o["ratio"]
            apply_mod(obj, m)
        elif op == "subdivide":
            m = obj.modifiers.new("sub", "SUBSURF")
            m.levels = o["levels"]
            m.render_levels = o["levels"]
            apply_mod(obj, m)
        elif op == "smooth":
            m = obj.modifiers.new("smooth", "SMOOTH")
            m.iterations = o["iterations"]
            m.factor = o["factor"]
            apply_mod(obj, m)
        elif op == "solidify":
            m = obj.modifiers.new("solid", "SOLIDIFY")
            m.thickness = o["thickness_mm"]
            apply_mod(obj, m)
        elif op == "mirror":
            for ob in (obj, ref):
                m = ob.modifiers.new("mirror", "MIRROR")
                m.use_axis = [o["axis"] == "x", o["axis"] == "y", o["axis"] == "z"]
                apply_mod(ob, m)
        elif op == "emboss_text":
            obj = emboss_text(obj, o)
        elif op == "remove_loose_parts":
            obj = remove_loose_parts(obj, o["min_fraction"])
        elif op == "drop_to_bed":
            lo, _ = bbox(obj)
            for ob in (obj, ref):
                translate(ob, Vector((0, 0, -lo.z)))
        obj = active() if active() is not ref else obj
    return obj


def stats(obj):
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    nm = sum(1 for e in bm.edges if not e.is_manifold)
    try:
        vol = abs(bm.calc_volume())
    except Exception:
        vol = None
    faces = len(bm.faces)
    bm.free()
    lo, hi = bbox(obj)
    return {"bbox_min_mm": [round(x, 2) for x in lo], "bbox_max_mm": [round(x, 2) for x in hi],
            "size_mm": [round(h - l, 2) for l, h in zip(lo, hi)], "faces": faces,
            "nonmanifold_edges": nm, "watertight": nm == 0,
            "volume_cm3": round(vol / 1000, 2) if vol is not None else None}


def render(obj, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 16
    try:
        scene.cycles.use_denoising = False
    except Exception:
        pass
    scene.render.resolution_x = scene.render.resolution_y = 448
    world = bpy.data.worlds.new("w") if not scene.world else scene.world
    scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs[0].default_value = (0.9, 0.9, 0.9, 1)
        bg.inputs[1].default_value = 0.8
    # show the model's own colours: keep textured materials; otherwise a temporary vertex-colour / clay one
    keep = list(obj.data.materials)
    textured = any(m and m.use_nodes and any(n.type == "TEX_IMAGE" for n in m.node_tree.nodes) for m in keep)
    if not textured:
        mat = bpy.data.materials.new("preview")
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        if obj.data.color_attributes and bsdf:
            attr = mat.node_tree.nodes.new("ShaderNodeVertexColor")
            attr.layer_name = obj.data.color_attributes[0].name
            mat.node_tree.links.new(attr.outputs["Color"], bsdf.inputs["Base Color"])
        elif bsdf:
            bsdf.inputs[0].default_value = (0.75, 0.62, 0.45, 1)
        obj.data.materials.clear()
        obj.data.materials.append(mat)
    bpy.ops.object.light_add(type="SUN", location=(0, 0, 0))
    sun = active()
    sun.data.energy = 3.0
    sun.rotation_euler = (math.radians(50), math.radians(10), math.radians(30))
    c, size = FRAME["c"], FRAME["size"]
    bpy.ops.object.camera_add()
    cam = active()
    cam.data.type = "ORTHO"
    cam.data.ortho_scale = size
    scene.camera = cam
    paths = []
    for name, vw in VIEWS.items():
        cam.location = c - vw["dir"] * (size * 3)
        cam.rotation_euler = [math.radians(a) for a in vw["rot"]]
        p = os.path.join(out_dir, f"{name}.png")
        scene.render.filepath = p
        bpy.ops.render.render(write_still=True)
        paths.append(p)
    bpy.data.objects.remove(cam, do_unlink=True)
    bpy.data.objects.remove(sun, do_unlink=True)
    if not textured:
        obj.data.materials.clear()
        for m in keep:
            obj.data.materials.append(m)
    select_only(obj)
    return paths


def export(ob, path):
    select_only(ob)
    kw = dict(filepath=path, export_format="GLB", use_selection=True)
    try:   # keep vertex colours even when no material uses them (Blender 4.2+ option)
        bpy.ops.export_scene.gltf(**kw, export_vertex_color="ACTIVE")
    except TypeError:
        bpy.ops.export_scene.gltf(**kw, export_colors=True)


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    obj = load(SPEC["input"])
    if SPEC.get("height_mm"):
        normalise(obj, float(SPEC["height_mm"]))
    weld(obj)
    c, size = view_frame(obj)
    FRAME.update(c=c, size=size)           # anchors refer to the renders of the model as it came in
    ref = None
    if SPEC.get("output"):
        ref = obj.copy()
        ref.data = obj.data.copy()
        bpy.context.collection.objects.link(ref)
        ref.hide_render = True
    obj = run_ops(obj, ref, SPEC.get("ops") or [])
    out = {"ok": True, "stats": stats(obj), "notes": NOTES}
    if SPEC.get("render_dir"):
        lo, hi = bbox(obj)
        FRAME.update(c=(lo + hi) / 2, size=max(hi - lo) * 1.35)
        out["renders"] = render(obj, SPEC["render_dir"])
        out["frame"] = {"c": list(FRAME["c"]), "size": FRAME["size"]}
    if SPEC.get("output"):
        export(obj, SPEC["output"])
        export(ref, SPEC["output"][:-4] + ".ref.glb")
        out["output"] = SPEC["output"]
        out["ref"] = SPEC["output"][:-4] + ".ref.glb"
    return out


try:
    result = main()
except Exception as e:
    result = {"ok": False, "error": f"{type(e).__name__}: {e}"[:400], "trace": traceback.format_exc()[-1500:]}
with open(RESULT, "w") as f:
    json.dump(result, f)
