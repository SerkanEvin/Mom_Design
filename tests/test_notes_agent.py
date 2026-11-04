"""Notes on models/drawings, the design agent (drawing -> relief / figure, model -> Blender edit), effort, usage."""
import io
import json
import shutil
from pathlib import Path

import numpy as np
import pytest
import trimesh
from PIL import Image, ImageDraw

from conftest import login
from test_pipelines import H, FakeFal, fake_llm, patch, photo_bytes, run_queued, two_colour_glb


def drawing_bytes():
    im = Image.new("RGB", (800, 600), (250, 248, 240))
    d = ImageDraw.Draw(im)
    d.ellipse((200, 100, 600, 500), outline=(20, 20, 200), width=14)
    d.line((300, 250, 500, 350), fill=(20, 20, 200), width=12)
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def make_drawing(client):
    r = client.post("/api/drawings", files={"image": ("d.png", drawing_bytes(), "image/png")},
                    data={"name": "Kedi"}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def agent_llm(route, seen):
    def f(system, content, **kw):
        seen.append((system, content))
        if "2D drawing" in system:
            if route == "relief":
                return {"route": "relief", "name_tr": "Kedi anahtarlık", "relief": {"kind": "keychain", "width_mm": 45},
                        "explanation_tr": "Çizimini anahtarlık yaptım.", "tips_tr": ["Destek gerekmez."]}
            return {"route": "figure", "name_tr": "Kedi figür", "figure": {"photo_prompt_en": "a cat toy",
                    "request_tr": "kedi figürü", "height_mm": 50}, "colors": 2, "explanation_tr": "Figür yaptım."}
        if "left numbered notes on a 3D model" in system:
            return {"directive": "Add a round base. At pin 1 engrave ASLI.", "height_mm": 60, "colors": 2,
                    "explanation_tr": "Taban ekleyip yazıyı yazacağım.", "tips_tr": ["Tabanı tablaya koy."]}
        if "edit a 3D-printable figure" in system:
            return {"ops": [{"op": "add_base", "shape": "round", "height_mm": 4}], "explanation_tr": "Taban."}
        if "check an edited 3D figure" in system:
            return {"done": False, "missing": ["text"], "summary_tr": "Yazı eksik."}
        return fake_llm(system, content, **kw)
    return f


def test_drawing_and_notes_access(client, users):
    login(client, users["serkan"][1])
    sid = make_drawing(client)
    client.post("/api/logout", headers=H)
    login(client, users["asli"][1])
    assert client.get(f"/api/drawings/{sid}").status_code == 404
    assert client.post("/api/notes", json={"kind": "drawing", "drawing_id": sid, "text": "x"}, headers=H).status_code == 404
    did = make_drawing(client)
    assert client.get(f"/api/drawings/{did}/image").status_code == 200
    r = client.post("/api/notes", json={"kind": "drawing", "drawing_id": did, "text": "Kulaklar sivri", "pos": [30, 20]},
                    headers=H)
    assert r.status_code == 200 and r.json()["pos"] == [30, 20] and r.json()["status"] == "open"
    nid = r.json()["id"]
    for bad in ({"kind": "drawing", "drawing_id": did, "text": ""},
                {"kind": "drawing", "drawing_id": did, "text": "a", "pos": [120, 3]},
                {"kind": "drawing", "drawing_id": did, "text": "a", "pos": [1, 2, 3]},
                {"kind": "other", "text": "a"}):
        assert client.post("/api/notes", json=bad, headers=H).status_code == 400, bad
    r = client.post(f"/api/notes/{nid}/update", json={"text": "Kulaklar daha sivri"}, headers=H)
    assert r.json()["text"] == "Kulaklar daha sivri"
    notes = client.get(f"/api/drawings/{did}").json()["notes"]
    assert [n["id"] for n in notes] == [nid]
    assert client.post("/api/agent", json={"drawing_id": did, "note_ids": ["nope"]}, headers=H).status_code == 400
    assert client.post("/api/agent", json={"drawing_id": did, "effort": "turbo"}, headers=H).status_code == 400


def test_agent_drawing_relief_is_free_and_printable(client, users, monkeypatch, tmp_path):
    fake = patch(monkeypatch, tmp_path)
    from studio import llm
    seen = []
    monkeypatch.setattr(llm, "chat_json", agent_llm("relief", seen))
    login(client, users["asli"][1])
    did = make_drawing(client)
    client.post("/api/notes", json={"kind": "drawing", "drawing_id": did, "text": "Anahtarlık olsun", "pos": [50, 10]},
                headers=H)
    r = client.post("/api/agent", json={"drawing_id": did, "effort": "mid"}, headers=H)
    assert r.status_code == 200, r.text
    jid = r.json()["id"]
    assert client.get(f"/api/drawings/{did}").json()["notes"][0]["status"] == "sent"
    run_queued()
    j = client.get(f"/api/jobs/{jid}").json()
    assert j["status"] == "done", j
    assert fake.calls == [] and j["cost_usd"] == 0
    res = j["result"]
    assert res["route"] == "relief" and res["explanation_tr"] and res["tips_tr"]
    p = client.get(f"/api/jobs/{res['prep_job']}").json()
    assert p["status"] == "done", (p["error"], p["log"])
    rep = p["result"]["report"]
    assert rep["watertight"] and rep["is_printable"] and abs(rep["size_mm"][2] - 4.2) < 0.3
    assert 40 < max(rep["size_mm"][:2]) < 60
    assert len(p["result"]["palette"]) == 2
    a = client.get(f"/api/assets/{res['asset_id']}").json()
    assert a["name"] == "Kedi anahtarlık" and a["latest"]["meta"]["drawing_id"] == did
    assert client.get(f"/api/drawings/{did}").json()["notes"][0]["status"] == "done"
    # the planner saw the drawing with the pin on it
    assert any(isinstance(c, list) and c[0]["type"] == "image" for s, c in seen)


def test_agent_drawing_figure_runs_mod01(client, users, monkeypatch, tmp_path):
    fake = patch(monkeypatch, tmp_path)
    from studio import llm
    monkeypatch.setattr(llm, "chat_json", agent_llm("figure", []))
    login(client, users["asli"][1])
    did = make_drawing(client)
    client.post("/api/notes", json={"kind": "drawing", "drawing_id": did, "text": "Oyuncak kedi"}, headers=H)
    est = client.get("/api/estimate?module=agent&target=drawing&effort=high").json()["usd"]
    r = client.post("/api/agent", json={"drawing_id": did}, headers=H)
    jid = r.json()["id"]
    assert r.json()["est_usd"] == est > 0
    run_queued()
    j = client.get(f"/api/jobs/{jid}").json()
    assert j["status"] == "done", j
    # sketch -> photo (1) + 3 views + 1 QA redo
    assert fake.calls.count("fal-ai/hidream-o1-image/dev/edit") == 5
    assert fake.calls.count("fal-ai/trellis/multi") == 1
    assert client.get(f"/api/jobs/{j['result']['prep_job']}").json()["status"] == "done"


def fake_blender(calls):
    def run(job, workdir, timeout_s=420):
        calls.append(job)
        out = Path(job["output"])
        src = Path(job["input"])
        if job.get("height_mm"):
            from studio import meshio
            m = meshio.to_print_frame(meshio.load(src), "y", job["height_mm"])
            meshio.export_glb_with_colors(m, out, up="z")
        else:
            shutil.copyfile(src, out)
        shutil.copyfile(out, str(out)[:-4] + ".ref.glb")
        rd = Path(job["render_dir"])
        rd.mkdir(parents=True, exist_ok=True)
        renders = []
        for v in ("front", "right", "back", "top"):
            Image.new("RGB", (448, 448), (230, 230, 230)).save(rd / f"{v}.png")
            renders.append(str(rd / f"{v}.png"))
        return {"ok": True, "stats": {"size_mm": [40, 10, 64]}, "renders": renders, "notes": [], "failed_ops": [],
                "own_colors": True, "frame": {"c": [0, 0, 32], "size": 64 * 1.35}, "ref": str(out)[:-4] + ".ref.glb"}
    return run


@pytest.mark.parametrize("eff,rounds", [("mid", 2), ("extra", 5)])
def test_agent_model_edit_uses_pins_and_effort(client, users, monkeypatch, tmp_path, eff, rounds):
    from studio import blender, jobs, llm
    seen, bl = [], []
    monkeypatch.setattr(llm, "chat_json", agent_llm("relief", seen))
    monkeypatch.setattr(blender, "run_blender", fake_blender(bl))
    login(client, users["asli"][1])
    p = tmp_path / "m.glb"
    two_colour_glb(p)                     # glTF Y-up: box x ±20, y 0..10, z ±20; sphere on top
    aid = jobs.create_asset(users["asli"][0], "Figür", "upload")
    vid = jobs.add_version(aid, "upload", p, {"up": "y"})
    # pin on the box face at glTF z=+20: after Y-up -> Z-up that face looks toward the FRONT camera (-Y)
    r = client.post("/api/notes", json={"kind": "model", "version_id": vid, "text": "Buraya ASLI yaz", "pos": [0, 5, 20]},
                    headers=H)
    assert r.status_code == 200, r.text
    client.post("/api/notes", json={"kind": "model", "version_id": vid, "text": "Taban ekle"}, headers=H)
    r = client.post("/api/agent", json={"version_id": vid, "effort": eff}, headers=H)
    assert r.status_code == 200, r.text
    jid = r.json()["id"]
    run_queued()
    j = client.get(f"/api/jobs/{jid}").json()
    assert j["status"] == "done", j
    planner = [c for s, c in seen if "edit a 3D-printable figure" in s]
    assert len(planner) == 2        # round 2 repeats add_base, which is dropped -> nothing left to do
    info = json.loads(planner[0][-1]["text"])
    assert [n["n"] for n in info["notes"]] == [1, 2]
    assert info["notes"][0]["pinned"] and "front" in info["notes"][0]["visible_at"]
    assert (info["careful"] is not None) == (eff == "extra")
    # after the base moved the model, pins are passed as text only
    assert json.loads(planner[1][-1]["text"])["notes"][0]["visible_at"] == {}
    a = client.get(f"/api/assets/{aid}").json()
    assert a["latest"]["source"] == "agent" and a["latest"]["meta"]["effort"] == eff
    assert client.get(f"/api/jobs/{j['result']['prep_job']}").json()["status"] == "done"
    notes = client.get(f"/api/notes?asset_id={aid}").json()["notes"]
    assert {n["status"] for n in notes} == {"done"}


def test_edit_rounds_follow_effort(env, users, monkeypatch, tmp_path):
    from studio import blender, db, jobs, llm
    bl, plans = [], []

    def chat(system, content, **kw):
        if "edit a 3D-printable figure" in system:
            plans.append(1)
            return {"ops": [{"op": "smooth", "iterations": 1}], "explanation_tr": "x"}
        return {"done": False, "missing": ["more"]}
    monkeypatch.setattr(llm, "chat_json", chat)
    monkeypatch.setattr(blender, "run_blender", fake_blender(bl))
    p = tmp_path / "m.glb"
    two_colour_glb(p)
    aid = jobs.create_asset(users["asli"][0], "x", "upload")
    vid = jobs.add_version(aid, "upload", p, {"up": "y"})
    for eff, n in (("mid", 2), ("high", 3), ("extra", 5)):
        plans.clear()
        jid = jobs.create_job(users["asli"][0], "edit", {"version_id": vid, "directive": "smooth it", "effort": eff})
        db.execute("UPDATE jobs SET status='running' WHERE id=?", jid)
        jobs.run_one(db.row("SELECT * FROM jobs WHERE id=?", jid))
        assert db.row("SELECT status FROM jobs WHERE id=?", jid)["status"] == "done"
        assert len(plans) == n, eff


def test_effort_estimates_and_usage(client, users):
    login(client, users["asli"][1])
    e = {k: client.get(f"/api/estimate?module=mod01&effort={k}").json()["usd"] for k in ("mid", "high", "extra")}
    assert e["mid"] < e["high"] < e["extra"]
    me = client.get("/api/me").json()
    assert [x["name"] for x in me["efforts"]] == ["mid", "high", "extra"]
    u = me["usage"]
    assert u["credit"]["pct"] == 0 and u["credit"]["granted"] == 5
    assert u["ai_hour"]["limit"] == 60 and u["today"]["cap"] == 3
    client.post("/api/logout", headers=H)
    login(client, users["serkan"][1])
    u = client.get("/api/me").json()["usage"]
    assert u["credit"]["pct"] is None and u["ai_hour"]["limit"] is None


def test_project_pins_visibility():
    from studio.blender import project_pins
    box = trimesh.creation.box((20, 20, 20))
    box.apply_translation([0, 0, 10])
    pins = [{"n": 1, "mm": [0, -10, 10]}]      # centre of the face the FRONT camera sees (front looks along +Y)
    m = project_pins(pins, {"c": [0, 0, 10], "size": 27}, box)
    assert m["front"] == [{"n": 1, "u": 50.0, "v": 50.0}]
    assert "back" not in m                     # hidden behind the box
    assert "top" not in m                      # side face is edge-on from above: the top face is hit first


def test_relief_kinds(tmp_path):
    from studio import relief
    p = tmp_path / "d.png"
    p.write_bytes(drawing_bytes())
    for kind in relief.KINDS:
        m = relief.build(p, kind=kind, width_mm=50)
        assert m.is_watertight and m.is_volume, kind
        assert abs(max(m.extents[:2]) - 50) < 12 and m.bounds[0][2] == pytest.approx(0, abs=1e-6)
    blank = tmp_path / "b.png"
    Image.new("RGB", (300, 300), (255, 255, 255)).save(blank)
    with pytest.raises(relief.ReliefError):
        relief.build(blank)
