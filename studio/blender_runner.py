"""Runs INSIDE Blender: blender -b --factory-startup --python blender_runner.py -- job.json result.json

job.json: {"input": path, "input_up": "y"|"z", "height_mm": float|null, "ops": [...validated...],
           "render_dir": path|null, "output": path|null}
Only the operations below exist; there is no way to run other code from a job file.
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


def bbox(obj):
    pts = [obj.matrix_world @ v.co for v in obj.data.vertices]
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    return lo, hi


def normalise(obj, height_mm):
    lo, hi = bbox(obj)
    h = hi.z - lo.z
    if h <= 0:
        raise RuntimeError("zero height")
    s = height_mm / h
    obj.scale = (s, s, s)
    bpy.ops.object.transform_apply(scale=True)
    drop(obj, center=True)


def drop(obj, center=False):
    lo, hi = bbox(obj)
    off = Vector((-(lo.x + hi.x) / 2 if center else 0, -(lo.y + hi.y) / 2 if center else 0, -lo.z))
    obj.location = obj.location + off
    bpy.ops.object.transform_apply(location=True)


def apply_mod(obj, mod):
    select_only(obj)
    bpy.ops.object.modifier_apply(modifier=mod.name)


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


def text_mesh(text, size, depth, center, rot):
    bpy.ops.object.text_add()
    t = active()
    t.data.body = text
    t.data.size = size
    t.data.extrude = max(abs(depth) / 2, 0.05)
    t.data.align_x = "CENTER"
    t.data.align_y = "CENTER"
    t.rotation_euler = [math.radians(a) for a in rot]
    t.location = center
    bpy.ops.object.convert(target="MESH")
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    return active()


def run_ops(obj, ops):
    for o in ops:
        op = o["op"]
        select_only(obj)
        if op == "scale":
            obj.scale = o["factor"]
            bpy.ops.object.transform_apply(scale=True)
        elif op == "rotate":
            obj.rotation_euler = [math.radians(a) for a in o["degrees"]]
            bpy.ops.object.transform_apply(rotation=True)
        elif op == "translate":
            obj.location = o["mm"]
            bpy.ops.object.transform_apply(location=True)
        elif op in ("boolean_difference", "boolean_union", "add_primitive"):
            tool = primitive(o["shape"], o["size_mm"], o["center_mm"], o["rotation_deg"])
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
            m = obj.modifiers.new("mirror", "MIRROR")
            m.use_axis = [o["axis"] == "x", o["axis"] == "y", o["axis"] == "z"]
            apply_mod(obj, m)
        elif op == "emboss_text":
            t = text_mesh(o["text"], o["size_mm"], o["depth_mm"], o["center_mm"], o["rotation_deg"])
            boolean(obj, t, "UNION" if o["depth_mm"] >= 0 else "DIFFERENCE")
        elif op == "drop_to_bed":
            drop(obj)
        obj = active()
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
    scene.cycles.samples = 12
    try:
        scene.cycles.use_denoising = False
    except Exception:
        pass
    scene.render.resolution_x = scene.render.resolution_y = 384
    scene.render.film_transparent = False
    world = bpy.data.worlds.new("w") if not scene.world else scene.world
    scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs[0].default_value = (0.9, 0.9, 0.9, 1)
        bg.inputs[1].default_value = 0.8
    mat = bpy.data.materials.new("preview")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs[0].default_value = (0.75, 0.62, 0.45, 1)
    keep = list(obj.data.materials)
    obj.data.materials.clear()
    obj.data.materials.append(mat)
    bpy.ops.object.light_add(type="SUN", location=(0, 0, 0))
    sun = active()
    sun.data.energy = 3.0
    sun.rotation_euler = (math.radians(50), math.radians(10), math.radians(30))
    lo, hi = bbox(obj)
    c = (lo + hi) / 2
    size = max(hi - lo) * 1.35
    bpy.ops.object.camera_add()
    cam = active()
    cam.data.type = "ORTHO"
    cam.data.ortho_scale = size
    scene.camera = cam
    d = size * 3
    views = {"front": ((c.x, c.y - d, c.z), (90, 0, 0)), "right": ((c.x + d, c.y, c.z), (90, 0, 90)),
             "back": ((c.x, c.y + d, c.z), (90, 0, 180)), "top": ((c.x, c.y, c.z + d), (0, 0, 0))}
    paths = []
    for name, (loc, rot) in views.items():
        cam.location = loc
        cam.rotation_euler = [math.radians(a) for a in rot]
        p = os.path.join(out_dir, f"{name}.png")
        scene.render.filepath = p
        bpy.ops.render.render(write_still=True)
        paths.append(p)
    obj.data.materials.clear()
    for m in keep:
        obj.data.materials.append(m)
    bpy.data.objects.remove(cam, do_unlink=True)
    bpy.data.objects.remove(sun, do_unlink=True)
    select_only(obj)
    return paths


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    obj = load(SPEC["input"])
    if SPEC.get("height_mm"):
        normalise(obj, float(SPEC["height_mm"]))
    obj = run_ops(obj, SPEC.get("ops") or [])
    out = {"ok": True, "stats": stats(obj)}
    if SPEC.get("render_dir"):
        out["renders"] = render(obj, SPEC["render_dir"])
    if SPEC.get("output"):
        select_only(obj)
        kw = dict(filepath=SPEC["output"], export_format="GLB", use_selection=True)
        try:   # keep vertex colours even when no material uses them (Blender 4.2+ option)
            bpy.ops.export_scene.gltf(**kw, export_vertex_color="ACTIVE")
        except TypeError:
            bpy.ops.export_scene.gltf(**kw, export_colors=True)
        out["output"] = SPEC["output"]
    return out


try:
    result = main()
except Exception as e:
    result = {"ok": False, "error": f"{type(e).__name__}: {e}"[:400], "trace": traceback.format_exc()[-1500:]}
with open(RESULT, "w") as f:
    json.dump(result, f)
