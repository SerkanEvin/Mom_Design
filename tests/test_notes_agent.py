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
        if "review ONE editing round" in system:
            return {"verdict": "good", "done": False, "missing": ["text"], "missing_tr": ["Yazı eksik."],
                    "summary_tr": "Yazı eksik."}
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


def dialog_llm(seen, always_ask=False):
    """Asks once what the drawing is (or always, to test the cap), then plans a relief using her answer."""
    def f(system, content, **kw):
        if "Serkan's mother" not in system:      # print-check advice etc.
            return fake_llm(system, content, **kw)
        text = content[-1]["text"] if isinstance(content, list) else content
        seen.append(text)
        if always_ask or "Dialog with her" not in text:
            return {"question_tr": "Bu bir kedi mi, bir tavşan mı?", "options_tr": ["Kedi", "Tavşan"],
                    "praise_tr": "Ne güzel bir çizim!"}
        return {"route": "relief", "name_tr": "Kedi", "relief": {"kind": "plate"}, "praise_tr": "Kulaklar çok tatlı!",
                "plan_tr": "5 cm'lik düz bir kedi plakası yapacağım.", "explanation_tr": "Plaka yaptım."}
    return f


def send(client, did):
    r = client.post("/api/agent", json={"drawing_id": did}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_agent_asks_when_needed_then_continues(client, users, monkeypatch, tmp_path):
    patch(monkeypatch, tmp_path)
    from studio import llm
    seen = []
    monkeypatch.setattr(llm, "chat_json", dialog_llm(seen))
    login(client, users["asli"][1])
    did = make_drawing(client)
    client.post("/api/notes", json={"kind": "drawing", "drawing_id": did, "text": "Bunu bas"}, headers=H)
    jid = send(client, did)
    assert client.post(f"/api/jobs/{jid}/answer", json={"text": "x"}, headers=H).status_code == 409   # not waiting yet
    run_queued()
    j = client.get(f"/api/jobs/{jid}").json()
    assert j["status"] == "waiting" and j["ask"]["kind"] == "question"
    assert j["ask"]["options"] == ["Kedi", "Tavşan"] and j["ask"]["praise"] == "Ne güzel bir çizim!"
    assert any(x["status"] == "waiting" for x in client.get("/api/jobs?active=1").json()["jobs"])
    r = client.post(f"/api/jobs/{jid}/answer", json={"text": "Kedi"}, headers=H)
    assert r.status_code == 200 and r.json()["status"] == "queued"
    run_queued()
    j = client.get(f"/api/jobs/{jid}").json()
    assert j["status"] == "done", j
    assert '"answer": "Kedi"' in seen[-1]
    assert j["result"]["praise_tr"] == "Kulaklar çok tatlı!"


def test_agent_question_cap(client, users, monkeypatch, tmp_path):
    patch(monkeypatch, tmp_path)
    from studio import llm
    seen = []
    monkeypatch.setattr(llm, "chat_json", dialog_llm(seen, always_ask=True))
    login(client, users["asli"][1])
    did = make_drawing(client)
    client.post("/api/notes", json={"kind": "drawing", "drawing_id": did, "text": "Bunu bas"}, headers=H)
    jid = send(client, did)
    for _ in range(2):
        run_queued()
        assert client.get(f"/api/jobs/{jid}").json()["status"] == "waiting"
        client.post(f"/api/jobs/{jid}/answer", json={"text": ""}, headers=H)     # "Sen karar ver"
    run_queued()
    j = client.get(f"/api/jobs/{jid}").json()
    assert j["status"] == "done", j                  # no third question: it decided (relief fallback)
    assert "Do not ask anything more" in seen[-1]


def test_plan_shown_from_third_send(client, users, monkeypatch, tmp_path):
    patch(monkeypatch, tmp_path)
    from studio import llm
    seen = []
    monkeypatch.setattr(llm, "chat_json", dialog_llm(seen))
    login(client, users["asli"][1])
    did = make_drawing(client)

    def one(answer_with=None):
        client.post("/api/notes", json={"kind": "drawing", "drawing_id": did, "text": "Bir not daha"}, headers=H)
        jid = send(client, did)
        run_queued()
        client.post(f"/api/jobs/{jid}/answer", json={"text": "Kedi"}, headers=H)   # the drawing question
        run_queued()
        return jid

    for _ in range(2):   # 1st and 2nd send: no plan step
        assert client.get(f"/api/jobs/{one()}").json()["status"] == "done"
    jid = one()          # 3rd send: plan + cost, waits for Tamam
    j = client.get(f"/api/jobs/{jid}").json()
    assert j["status"] == "waiting" and j["ask"]["kind"] == "confirm"
    assert j["ask"]["text"] == "5 cm'lik düz bir kedi plakası yapacağım." and j["ask"]["est_usd"] == 0
    # she asks for a change -> re-plan with her words -> asked to confirm again
    client.post(f"/api/jobs/{jid}/answer", json={"text": "Biraz daha büyük olsun"}, headers=H)
    run_queued()
    j = client.get(f"/api/jobs/{jid}").json()
    assert j["status"] == "waiting" and j["ask"]["kind"] == "confirm"
    assert "Biraz daha büyük olsun" in seen[-1]
    client.post(f"/api/jobs/{jid}/answer", json={"ok": True}, headers=H)
    run_queued()
    assert client.get(f"/api/jobs/{jid}").json()["status"] == "done"


def test_cancel_waiting_reopens_notes(client, users, monkeypatch, tmp_path):
    patch(monkeypatch, tmp_path)
    from studio import llm
    monkeypatch.setattr(llm, "chat_json", dialog_llm([]))
    login(client, users["asli"][1])
    did = make_drawing(client)
    client.post("/api/notes", json={"kind": "drawing", "drawing_id": did, "text": "Bunu bas"}, headers=H)
    jid = send(client, did)
    run_queued()
    assert client.post(f"/api/jobs/{jid}/cancel", json={}, headers=H).status_code == 200
    assert client.get(f"/api/jobs/{jid}").json()["status"] == "cancelled"
    assert client.get(f"/api/drawings/{did}").json()["notes"][0]["status"] == "open"



# ------------------------------------------------------------------ edit loop: review, rollback, repeats, honesty

def scripted(plans, checks, seen):
    """Planner answers `plans` in order, reviewer answers `checks` in order; records what each one saw."""
    plans, checks = list(plans), list(checks)

    def chat(system, content, **kw):
        if "edit a 3D-printable figure" in system:
            seen.append(("plan", content))
            return plans.pop(0) if plans else {"ops": [], "explanation_tr": "Bitti."}
        if "review ONE editing round" in system:
            seen.append(("check", content))
            return checks.pop(0) if checks else {"verdict": "good", "done": True}
        return {"summary": "ok", "tips": []}
    return chat


def edit_job(users, tmp_path, inp):
    from studio import db, jobs
    p = tmp_path / "m.glb"
    two_colour_glb(p)
    aid = jobs.create_asset(users["asli"][0], "x", "upload")
    vid = jobs.add_version(aid, "upload", p, {"up": "y"})
    jid = jobs.create_job(users["asli"][0], "edit", {"version_id": vid, "effort": "high", **inp})
    db.execute("UPDATE jobs SET status='running' WHERE id=?", jid)
    jobs.run_one(db.row("SELECT * FROM jobs WHERE id=?", jid))
    r = db.row("SELECT * FROM jobs WHERE id=?", jid)
    return r, json.loads(r["ctx"])


TEXT = {"op": "emboss_text", "text": "☾ ★", "anchor": {"view": "front", "u": 51, "v": 32}, "size_mm": 8}


def test_bad_round_is_rolled_back_and_planner_told(env, users, monkeypatch, tmp_path):
    from studio import blender, llm
    seen, bl = [], []
    cut = {"op": "boolean_difference", "shape": "cube", "size_mm": [12, 14, 12],
           "anchor": {"view": "right", "u": 55, "v": 57}, "depth_into_mm": 4}
    monkeypatch.setattr(llm, "chat_json", scripted(
        [{"ops": [cut]}, {"ops": [TEXT]}],
        [{"verdict": "bad", "problems": ["box-shaped hole cut into the leg"], "done": False},
         {"verdict": "good", "done": True, "summary_tr": "Tamam."}], seen))
    monkeypatch.setattr(blender, "run_blender", fake_blender(bl))
    r, ctx = edit_job(users, tmp_path, {"directive": "remove the bump and put a crescent and star on the flag"})
    assert r["status"] == "done", r["error"]
    h = ctx["history"]
    assert h[0]["rejected"] and h[0]["problems"] == ["box-shaped hole cut into the leg"]
    # round 2 started from the model BEFORE the bad cut, and the planner was told why it was undone
    assert bl[2]["input"].endswith("cur0.glb")
    info = json.loads(seen[2][1][-1]["text"])
    assert info["rounds_so_far"][0]["rejected"] and info["rounds_so_far"][0]["applied_ops"] == []
    assert info["rounds_so_far"][0]["why_rejected"] == ["box-shaped hole cut into the leg"]
    assert ctx["current"].endswith("cur2.glb")
    assert ctx["result"]["undone"] == 1
    from studio import db
    meta = json.loads(db.row("SELECT meta FROM versions WHERE id=?", ctx["result"]["version_id"])["meta"])
    assert meta["ops"] == [h[1]["ops"]]          # the rolled-back cut is not recorded as applied


def test_reviewer_sees_before_and_after(env, users, monkeypatch, tmp_path):
    from studio import blender, llm
    seen = []
    monkeypatch.setattr(llm, "chat_json", scripted([{"ops": [TEXT]}], [{"verdict": "good", "done": True}], seen))
    monkeypatch.setattr(blender, "run_blender", fake_blender([]))
    edit_job(users, tmp_path, {"directive": "crescent and star on the flag"})
    check = [c for k, c in seen if k == "check"][0]
    labels = [b["text"] for b in check if b["type"] == "text"]
    assert "FRONT BEFORE:" in labels and "FRONT AFTER:" in labels and "TOP AFTER:" in labels
    assert sum(1 for b in check if b["type"] == "image") == 8


def test_symbol_redone_next_to_itself_is_dropped(env, users, monkeypatch, tmp_path):
    """The flag case: '☾ ★' then '☾' and '★' again beside it gave two crescents and two stars."""
    from studio import blender, llm
    seen, bl = [], []
    again = [{"op": "emboss_text", "text": "☾", "anchor": {"view": "front", "u": 44, "v": 31}, "size_mm": 9},
             {"op": "emboss_text", "text": "★", "anchor": {"view": "front", "u": 60, "v": 31}, "size_mm": 8}]
    monkeypatch.setattr(llm, "chat_json", scripted([{"ops": [TEXT]}, {"ops": again}],
                                                   [{"verdict": "good", "done": False, "missing": ["flag"]}], seen))
    monkeypatch.setattr(blender, "run_blender", fake_blender(bl))
    r, ctx = edit_job(users, tmp_path, {"directive": "crescent and star side by side on the flag"})
    assert r["status"] == "done"
    texts = [o["text"] for h in ctx["history"] for o in h["ops"] if o["op"] == "emboss_text"]
    assert texts == ["☾ ★"]
    assert len(bl) == 2                          # start render + one edit; the repeat never reached Blender
    log = [x["m"] for x in json.loads(r["log"])]
    assert any("Tekrarlanan yazı atlandı: ☾" in m for m in log)
    # a different text near it, or the same symbol elsewhere, is still allowed
    from studio.blender import _drop_repeats
    from studio import jobs
    j = jobs.Job(r)
    ok = _drop_repeats([{"op": "emboss_text", "text": "ASLI", "anchor": {"view": "front", "u": 50, "v": 33}},
                        {"op": "emboss_text", "text": "★", "anchor": {"view": "back", "u": 50, "v": 33}}],
                       ctx["history"], j)
    assert [o["text"] for o in ok] == ["ASLI", "★"]


def test_missing_items_are_reported_when_rounds_run_out(env, users, monkeypatch, tmp_path):
    from studio import blender, llm
    smooth = {"ops": [{"op": "smooth", "iterations": 1}]}
    monkeypatch.setattr(llm, "chat_json", scripted([smooth] * 3, [
        {"verdict": "good", "done": False, "missing": ["one star too many"], "missing_tr": ["Bir yıldız fazla."]}] * 3, []))
    monkeypatch.setattr(blender, "run_blender", fake_blender([]))
    r, ctx = edit_job(users, tmp_path, {"directive": "fix the flag"})
    assert r["status"] == "done"
    assert ctx["result"]["missing_tr"] == ["Bir yıldız fazla."]


def test_erase_note_hits_the_pin(env, users, monkeypatch, tmp_path):
    from studio import blender, llm
    seen, bl = [], []
    monkeypatch.setattr(llm, "chat_json", scripted([{"ops": [{"op": "erase_note", "note": 1, "radius_mm": 3}]}],
                                                   [{"verdict": "good", "done": True}], seen))
    monkeypatch.setattr(blender, "run_blender", fake_blender(bl))
    from studio import db, jobs
    p = tmp_path / "m.glb"
    two_colour_glb(p)
    aid = jobs.create_asset(users["asli"][0], "x", "upload")
    vid = jobs.add_version(aid, "upload", p, {"up": "y"})
    jid = jobs.create_job(users["asli"][0], "edit", {"version_id": vid, "effort": "high"})
    db.execute("UPDATE jobs SET status='running' WHERE id=?", jid)
    j = jobs.Job(db.row("SELECT * FROM jobs WHERE id=?", jid))
    blender.edit_loop(j, notes=[{"n": 1, "text": "burası silinmeli", "p": [0, 5, 20]}], height=64.0)
    ops = bl[1]["ops"]
    assert ops[0]["op"] == "erase_sphere" and ops[0]["radius_mm"] == 3
    expect, _ = blender.to_print_mm(p, "y", 64.0, [[0, 5, 20]])
    assert np.allclose(ops[0]["center_mm"], expect[0], atol=0.01)
    assert ops[-1]["op"] == "remove_loose_parts"
    info = json.loads(seen[0][1][-1]["text"])
    assert info["notes"][0]["erase_note_available"]
    import pytest as _pt
    with _pt.raises(blender.BlenderError):
        blender.validate_ops([{"op": "erase_note", "note": 2}], pins={1: [0, 0, 0]})


def test_agent_plans_while_looking_and_reports_what_it_cannot_do(client, users, monkeypatch, tmp_path):
    from studio import blender, jobs, llm
    seen, bl = [], []

    def chat(system, content, **kw):
        seen.append((system, content))
        if "left numbered notes on a 3D model" in system:
            return {"seen": "pin 1 is the left foot; the model has a base", "directive": "",
                    "cant_do_tr": ["Tek bir ayağı küçültemiyorum."], "praise_tr": "Ne güzel heykel!"}
        return fake_llm(system, content, **kw)
    monkeypatch.setattr(llm, "chat_json", chat)
    monkeypatch.setattr(blender, "run_blender", fake_blender(bl))
    login(client, users["asli"][1])
    p = tmp_path / "m.glb"
    two_colour_glb(p)
    aid = jobs.create_asset(users["asli"][0], "Figür", "upload")
    vid = jobs.add_version(aid, "upload", p, {"up": "y"})
    client.post("/api/notes", json={"kind": "model", "version_id": vid, "text": "iki ayak eşit olsun", "pos": [0, 5, 20]},
                headers=H)
    jid = client.post("/api/agent", json={"version_id": vid}, headers=H).json()["id"]
    run_queued()
    j = client.get(f"/api/jobs/{jid}").json()
    assert j["status"] == "failed" and "Tek bir ayağı küçültemiyorum." in j["error"]
    plan_content = seen[0][1]
    assert isinstance(plan_content, list) and any(b["type"] == "image" for b in plan_content)   # it looked
    assert client.get(f"/api/notes?asset_id={aid}").json()["notes"][0]["status"] == "open"


def test_empty_answer_retries_with_more_room(env, monkeypatch):
    from studio import llm
    sent = []

    class R:
        def __init__(self, text):
            self.status_code = 200
            self._t = text

        def json(self):
            return {"content": [{"type": "text", "text": self._t}]}

    def post(url, json=None, **kw):
        sent.append(json["max_tokens"])
        return R("" if len(sent) == 1 else "ok")
    monkeypatch.setattr(llm.httpx, "post", post)
    assert llm.chat("s", "hi", max_tokens=3000) == "ok"
    assert sent == [3000, 6000]


def test_done_note_can_be_reopened(client, users):
    from studio import db
    login(client, users["asli"][1])
    did = make_drawing(client)
    nid = client.post("/api/notes", json={"kind": "drawing", "drawing_id": did, "text": "x"}, headers=H).json()["id"]
    db.execute("UPDATE notes SET status='done', job_id='job_x' WHERE id=?", nid)
    r = client.post(f"/api/notes/{nid}/reopen", json={}, headers=H)
    assert r.status_code == 200 and r.json()["status"] == "open" and r.json()["job_id"] is None
