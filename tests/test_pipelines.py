"""End-to-end pipeline runs with fal + LLM replaced by fakes (no money spent)."""
import io
import json
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image, ImageDraw

from conftest import login

H = {"X-Studio": "1"}


def photo_bytes(color=(200, 40, 40)):
    im = Image.new("RGB", (400, 400), (240, 240, 240))
    ImageDraw.Draw(im).ellipse((100, 80, 300, 330), fill=color)
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def cutout_png(path: Path):
    im = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    ImageDraw.Draw(im).ellipse((100, 80, 300, 330), fill=(200, 40, 40, 255))
    im.save(path)


def two_colour_glb(path: Path):
    box = trimesh.creation.box((40, 10, 40))
    box.apply_translation([0, 5, 0])
    sph = trimesh.creation.icosphere(subdivisions=3, radius=12)
    sph.apply_translation([0, 20, 0])
    m = trimesh.boolean.union([box, sph], engine="manifold")
    rgb = np.where((m.triangles_center[:, 1] > 11)[:, None], [220, 30, 30], [30, 30, 220]).astype(np.uint8)
    v = m.vertices[m.faces].reshape(-1, 3)
    f = np.arange(len(v)).reshape(-1, 3)
    vc = np.repeat(np.c_[rgb, np.full(len(rgb), 255)], 3, axis=0).astype(np.uint8)
    trimesh.Trimesh(v, f, vertex_colors=vc, process=False).export(path, file_type="glb")


class FakeFal:
    def __init__(self, tmp):
        self.tmp = Path(tmp)
        self.calls = []

    def run(self, endpoint_id, arguments, *, user_id, job_id, step, est_usd, on_submitted=None, existing=None, **kw):
        from studio import ledger
        lid = ledger.reserve(user_id, est_usd, job_id=job_id, step=step, endpoint_id=endpoint_id)
        if on_submitted:
            on_submitted({"request_id": f"req-{len(self.calls)}", "ledger_id": lid})
        ledger.settle(lid)
        self.calls.append(endpoint_id)
        if "rembg" in endpoint_id:
            return {"image": {"url": "fake://cutout.png"}}
        if "hidream" in endpoint_id:
            return {"images": [{"url": "fake://view.png"}]}
        if "trellis" in endpoint_id:
            return {"model_mesh": {"url": "fake://model.glb"}}
        raise AssertionError(endpoint_id)

    def upload(self, data, content_type, name):
        return f"fake://upload/{name}"

    def download(self, url, dest, **kw):
        dest.parent.mkdir(parents=True, exist_ok=True)
        if url.endswith(".glb"):
            two_colour_glb(dest)
        elif url.endswith("view.png"):
            Image.open(io.BytesIO(photo_bytes())).save(dest)
        else:
            cutout_png(dest)
        return dest


def fake_llm(system, content, **kw):
    if "spec for multi-view" in system:
        return {"object": "red egg", "name_tr": "Kırmızı yumurta", "colors": ["red"]}
    if "plan camera views" in system:
        return {v: f"{v} view of a red egg" for v in ("front", "right", "back", "left", "top")}
    if "generated views" in system:
        return {"right": {"ok": True}, "back": {"ok": False, "reason": "wrong side"}, "left": {"ok": True}}
    if "ONE object" in system:
        return {"same_object": True, "usable": [True, True], "issues": ""}
    if "beginner" in system:
        return {"summary": "Basılabilir.", "tips": ["Destek gerekmez."]}
    raise AssertionError(system[:40])


def patch(monkeypatch, tmp_path):
    from studio import falclient, llm
    fake = FakeFal(tmp_path)
    monkeypatch.setattr(falclient, "run", fake.run)
    monkeypatch.setattr(falclient, "upload_bytes", fake.upload)
    monkeypatch.setattr(falclient, "download", fake.download)
    monkeypatch.setattr(llm, "chat_json", fake_llm)
    return fake


def run_queued():
    from studio import db, jobs
    while True:
        r = jobs._claim()
        if not r:
            return
        jobs.run_one(r)


def test_mod01_full_flow_then_print(client, users, monkeypatch, tmp_path):
    fake = patch(monkeypatch, tmp_path)
    login(client, users["asli"][1])
    r = client.post("/api/jobs/mod01", files={"image": ("cat.png", photo_bytes(), "image/png")},
                    data={"prompt": "Bunu biblo yap", "include_top": "0"}, headers=H)
    assert r.status_code == 200, r.text
    jid = r.json()["id"]
    assert r.json()["est_usd"] > 0
    run_queued()
    j = client.get(f"/api/jobs/{jid}").json()
    assert j["status"] == "done", j
    # 4 rembg(front + 3 views) + 3 views + 1 redo (view+rembg) + recon
    assert fake.calls.count("fal-ai/hidream-o1-image/dev/edit") == 4
    assert fake.calls.count("fal-ai/trellis/multi") == 1
    assert j["cost_usd"] > 0
    aid = j["result"]["asset_id"]
    a = client.get(f"/api/assets/{aid}").json()
    assert a["name"] == "Kırmızı yumurta" and len(a["versions"]) == 1
    assert client.get(a["latest"]["url"]).status_code == 200
    bal = client.get("/api/me").json()["balance"]
    assert bal["spent"] == j["cost_usd"]

    # retrying a finished job's paid steps never pays twice
    n_calls = len(fake.calls)
    from studio import jobs, db
    db.execute("UPDATE jobs SET status='queued' WHERE id=?", jid)
    run_queued()
    assert len(fake.calls) == n_calls

    # MOD-04
    r = client.post("/api/jobs/prepare", json={"version_id": a["latest"]["id"], "height_mm": 60, "colors": 3}, headers=H)
    pid = r.json()["id"]
    run_queued()
    p = client.get(f"/api/jobs/{pid}").json()
    assert p["status"] == "done", p
    rep = p["result"]["report"]
    assert rep["watertight"] and rep["is_printable"]
    assert abs(rep["size_mm"][2] - 60) < 0.5
    assert len(p["result"]["palette"]) == 2
    arrays = client.get(f"/api/jobs/{pid}/files/arrays.json").json()
    assert len(arrays["labels"]) == p["result"]["faces"]
    assert client.get(f"/api/jobs/{pid}/files/preview.glb").status_code == 200

    labels = arrays["labels"]
    labels[0] = 2   # user painted a region with slot 3
    r = client.post("/api/jobs/export", json={"prep_job": pid, "labels": labels, "palette": ["#ff0000", "#0000ff", "#00ff00"],
                                              "mode": "painted", "name": "Kırmızı başlı figür"}, headers=H)
    eid = r.json()["id"]
    run_queued()
    e = client.get(f"/api/jobs/{eid}").json()
    assert e["status"] == "done", e
    f = e["result"]["files"][0]
    assert f == "Kırmızı_başlı_figür_boyali.3mf"
    resp = client.get(f"/api/jobs/{eid}/files/{f}")
    assert resp.status_code == 200
    data = resp.content
    from studio.threemf import read_painted_labels
    codes = read_painted_labels(data)
    assert len(codes) == len(labels) and codes[0] == "0C" and "8" in codes

    # parts mode gives closed solids
    r = client.post("/api/jobs/export", json={"prep_job": pid, "labels": arrays["labels"], "palette": ["#f00", "#00f"],
                                              "mode": "parts"}, headers=H)
    run_queued()
    e2 = client.get(f"/api/jobs/{r.json()['id']}").json()
    assert e2["status"] == "done" and len(e2["result"]["files"]) == 2

    # Serkan cannot be impersonated: Aslı's export of a wrong-length label list fails cleanly
    r = client.post("/api/jobs/export", json={"prep_job": pid, "labels": [0, 1], "mode": "painted"}, headers=H)
    run_queued()
    assert client.get(f"/api/jobs/{r.json()['id']}").json()["status"] == "failed"


def test_mod02_and_import(client, users, monkeypatch, tmp_path):
    patch(monkeypatch, tmp_path)
    login(client, users["serkan"][1])
    r = client.post("/api/jobs/mod02", files=[("images", ("a.png", photo_bytes(), "image/png")),
                                              ("images", ("b.png", photo_bytes((40, 200, 40)), "image/png"))],
                    data={"name": "Vazo"}, headers=H)
    jid = r.json()["id"]
    run_queued()
    j = client.get(f"/api/jobs/{jid}").json()
    assert j["status"] == "done", j

    stl = trimesh.creation.cylinder(radius=10, height=30).export(file_type="stl")
    r = client.post("/api/jobs/import", files={"file": ("kup.stl", stl, "application/octet-stream")}, headers=H)
    iid = r.json()["id"]
    run_queued()
    i = client.get(f"/api/jobs/{iid}").json()
    assert i["status"] == "done", i
    a = client.get(f"/api/assets/{i['result']['asset_id']}").json()
    # STL is Z-up; stored GLB is Y-up, so prepared height must be the cylinder's 30 mm axis scaled
    r = client.post("/api/jobs/prepare", json={"version_id": a["latest"]["id"], "height_mm": 30, "colors": 1}, headers=H)
    run_queued()
    p = client.get(f"/api/jobs/{r.json()['id']}").json()
    assert p["status"] == "done", p
    sx, sy, sz = p["result"]["report"]["size_mm"]
    assert abs(sz - 30) < 0.5 and abs(sx - 20) < 0.5


def test_bad_uploads(client, users):
    login(client, users["asli"][1])
    r = client.post("/api/jobs/mod01", files={"image": ("x.png", b"not an image", "image/png")}, headers=H)
    assert r.status_code == 400
    r = client.post("/api/jobs/import", files={"file": ("x.exe", b"MZ", "application/octet-stream")}, headers=H)
    assert r.status_code == 400


def test_blender_ops_validation():
    import pytest
    from studio.blender import BlenderError, validate_ops
    ok = validate_ops([{"op": "boolean_difference", "shape": "cylinder", "size_mm": [4, 4, 20], "center_mm": [0, 0, 70]},
                       {"op": "boolean_difference", "shape": "cylinder", "size_mm": [4, 4, 20],
                        "anchor": {"view": "top", "u": 50, "v": 40}, "depth_into_mm": 8},
                       {"op": "emboss_text", "text": "ASLI", "anchor": {"view": "front", "u": 50, "v": 80}, "size_mm": 8},
                       {"op": "remove_loose_parts"},
                       {"op": "drop_to_bed"}])
    assert ok[0]["rotation_deg"] == [0, 0, 0] and ok[1]["anchor"]["view"] == "top" and ok[2]["depth_mm"] == 1.2
    for bad in ([{"op": "run_python", "code": "import os"}], [{"op": "scale", "factor": [100, 1, 1]}],
                [{"op": "boolean_union", "shape": "teapot", "size_mm": [1, 1, 1], "center_mm": [0, 0, 0]}],
                [{"op": "emboss_text", "text": "A", "center_mm": [0, 0, 0]}],           # text needs an anchor
                [{"op": "emboss_text", "text": "A", "anchor": {"view": "side", "u": 1, "v": 1}}],
                [{"op": "remesh_voxel", "voxel_mm": 0.1}],
                [{"op": "remesh_voxel", "voxel_mm": 0.5}, {"op": "remesh_voxel", "voxel_mm": 0.5}],
                [{"op": "drop_to_bed"}] * 11):
        with pytest.raises(BlenderError):
            validate_ops(bad)


def test_grid_overlay(tmp_path):
    from PIL import Image
    from studio.blender import grid_overlay
    Image.new("RGB", (448, 448), (230, 230, 230)).save(tmp_path / "front.png")
    out = grid_overlay(tmp_path / "front.png", tmp_path / "g.png", "front")
    assert Image.open(out).size == (448, 448)
