"""Blender work handed to the aicp agent studio-editor through the exchange folder (agent replaced by a fake)."""
import json
import shutil
from pathlib import Path

import pytest

from conftest import login
from test_pipelines import H, run_queued, two_colour_glb


@pytest.fixture()
def hook(env, monkeypatch, tmp_path):
    """Studio hook configured; the fake agent answers each message with the next scripted result."""
    from studio import aicp_agent, config
    ex = tmp_path / "exchange"
    ex.mkdir()
    s = config.get_settings()
    s.studio_hook_url, s.exchange_dir, s.agent_jobs_dir = "http://hook", str(ex), "/srv/aicp-work/studio-editor/jobs"
    monkeypatch.setenv("AICP_STUDIO_TOKEN", "t")
    script, sent = [], []

    def send(text, session=None):
        sent.append({"text": text, "session": session})
        jid = text.split("/jobs/")[1].split("/")[0].split(",")[0].split(" ")[0].rstrip(".")
        d = ex / jid
        job = json.loads((d / "job.json").read_text())
        res = script.pop(0)(d, job) if script else {"status": "failed", "message_tr": "yok"}
        (d / "result.json").write_text(json.dumps(res))
        return {"message_id": f"m{len(sent)}", "session": session or "ses1", "status": "dispatched"}
    monkeypatch.setattr(aicp_agent, "send", send)
    monkeypatch.setattr(aicp_agent, "status", lambda mid: {"status": "done"})
    monkeypatch.setattr(aicp_agent.time, "sleep", lambda s: None)
    return script, sent, ex


def model(users, tmp_path):
    from studio import jobs
    p = tmp_path / "m.glb"
    two_colour_glb(p)
    aid = jobs.create_asset(users["asli"][0], "Nilda", "upload")
    return aid, jobs.add_version(aid, "upload", p, {"up": "y"})


def done(d, job, **kw):
    shutil.copyfile(d / "input.glb", d / "result.glb")
    return {"status": "done", "praise_tr": "Bayrağı ne güzel seçmişsin!", "message_tr": "Hilal ve yıldızı düzelttim.",
            "missing_tr": ["Tek ayağı küçültemedim."], "tips_tr": ["Destek gerekmez."], "height_mm": 80, "colors": 3,
            **kw}


def test_agent_job_goes_to_studio_editor(client, users, hook, monkeypatch, tmp_path):
    script, sent, ex = hook
    seen = {}

    def ask(d, job):
        seen["job"] = job
        return {"status": "question", "question_tr": "Hilal sola mı?", "options_tr": ["Sola", "Sağa"],
                "praise_tr": "Ne güzel heykel!"}
    script += [ask, lambda d, job: (seen.update(job2=job), done(d, job))[1]]
    login(client, users["asli"][1])
    aid, vid = model(users, tmp_path)
    client.post("/api/notes", json={"kind": "model", "version_id": vid, "text": "Bayrağı düzelt", "pos": [0, 5, 20]},
                headers=H)
    jid = client.post("/api/agent", json={"version_id": vid, "effort": "extra"}, headers=H).json()["id"]
    run_queued()
    j = client.get(f"/api/jobs/{jid}").json()
    assert j["status"] == "waiting" and j["ask"]["text"] == "Hilal sola mı?" and j["ask"]["options"] == ["Sola", "Sağa"]
    job = seen["job"]
    assert job["notes"][0]["text"] == "Bayrağı düzelt" and len(job["notes"][0]["pin_mm"]) == 3
    assert job["effort"] == "extra" and job["confirm_plan"] is False and (ex / jid / "input.glb").exists()
    assert sent[0]["session"] is None and f"/srv/aicp-work/studio-editor/jobs/{jid}/job.json" in sent[0]["text"]

    client.post(f"/api/jobs/{jid}/answer", json={"text": "Sola"}, headers=H)
    run_queued()
    j = client.get(f"/api/jobs/{jid}").json()
    assert j["status"] == "done", j
    assert sent[1]["session"] == "ses1" and "Sola" in sent[1]["text"]          # same agent session
    assert seen["job2"]["dialog"][-1]["answer"] == "Sola"
    r = j["result"]
    assert r["praise_tr"] == "Bayrağı ne güzel seçmişsin!" and r["missing_tr"] == ["Tek ayağı küçültemedim."]
    a = client.get(f"/api/assets/{aid}").json()
    assert a["latest"]["source"] == "agent" and a["latest"]["n"] == 2
    assert client.get(f"/api/jobs/{r['prep_job']}").json()["status"] == "done"
    assert (ex / jid / "result_0.json").exists() and (ex / jid / "result_1.json").exists()


def test_plan_confirmation_goes_to_the_agent(client, users, hook, tmp_path):
    script, sent, ex = hook
    from studio import config
    config.get_settings().agent_confirm_after = 0
    flags = []
    script += [lambda d, job: (flags.append(job["confirm_plan"]),
                               {"status": "confirm", "plan_tr": "Hilali sola koyacağım.", "est_usd": 0})[1],
               lambda d, job: (flags.append(job["confirm_plan"]), done(d, job))[1]]
    login(client, users["asli"][1])
    aid, vid = model(users, tmp_path)
    client.post("/api/notes", json={"kind": "model", "version_id": vid, "text": "Bayrak"}, headers=H)
    jid = client.post("/api/agent", json={"version_id": vid}, headers=H).json()["id"]
    run_queued()
    assert client.get(f"/api/jobs/{jid}").json()["ask"]["kind"] == "confirm"
    client.post(f"/api/jobs/{jid}/answer", json={"ok": True}, headers=H)
    run_queued()
    assert client.get(f"/api/jobs/{jid}").json()["status"] == "done"
    assert flags == [True, False] and "onayladı" in sent[1]["text"]


def test_blender_page_edit_goes_to_agent_and_failure_is_kind(client, users, hook, tmp_path):
    script, sent, ex = hook
    seen = {}
    script += [lambda d, job: (seen.update(job=job), done(d, job))[1],
               lambda d, job: {"status": "failed", "message_tr": "Bunu yapamadım, üzgünüm."}]
    login(client, users["asli"][1])
    aid, vid = model(users, tmp_path)
    jid = client.post("/api/jobs/edit", json={"version_id": vid, "directive": "Taban ekle",
                                              "erase": [{"p": [0, 5, 20], "r_mm": 3}]}, headers=H).json()["id"]
    run_queued()
    j = client.get(f"/api/jobs/{jid}").json()
    assert j["status"] == "done", j
    assert [n["text"] for n in seen["job"]["notes"]] == ["Burayı sil (yaklaşık 3 mm yarıçapında)", "Taban ekle"]
    assert client.get(f"/api/assets/{aid}").json()["latest"]["source"] == "mod03"
    jid2 = client.post("/api/jobs/edit", json={"version_id": vid, "directive": "x"}, headers=H).json()["id"]
    run_queued()
    j2 = client.get(f"/api/jobs/{jid2}").json()
    assert j2["status"] == "failed" and j2["error"] == "Bunu yapamadım, üzgünüm."


def test_builtin_loop_without_token(env):
    from studio import aicp_agent
    assert not aicp_agent.enabled()
