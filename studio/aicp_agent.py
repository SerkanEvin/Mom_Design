"""Blender work done by the aicp agent `studio-editor` (Claude Code with its own Blender and vault memory).

The studio writes a job folder into the shared exchange directory (input model, notes with pins, request,
effort, dialog) and sends the agent a message through aicp's studio hook. The agent edits with free Blender
Python, checks its own renders and writes result.json (+ result.glb). Questions and plan confirmations come
back as result.json states; her answers go to the same agent session.

Used when a studio hook token is configured (secret "studio"); otherwise the built-in loop in blender.py runs.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import httpx

from . import db
from .config import get_settings, secret
from .jobs import Job, JobFailed, WaitForUser, add_version, version_path

PROTOCOL = {
    "input": "model file in this folder (glTF/GLB, Y-up); 'up' says its up axis; 'height_mm' = height to print at",
    "notes": "her numbered notes; pin_gltf = point in the input file's coordinates, pin_mm = the same point after "
             "scaling the model to height_mm, Z-up, standing on z=0, centred on x/y (null = whole model)",
    "dialog": "earlier questions/plans and her answers in this job (follow them)",
    "confirm_plan": "true: write status 'confirm' with plan_tr before changing anything, wait for her answer",
    "write": "result.glb (glTF, Y-up, millimetres, colours as COLOR_0) then result.json atomically "
             "(result.json.tmp -> rename). Optional: renders/front.png, right.png, back.png, top.png of the result",
    "result.json": {"status": "done|question|confirm|failed", "praise_tr": "str", "message_tr": "what you did, 1-3 "
                    "simple sentences", "question_tr": "str", "options_tr": ["2-4 answers"], "plan_tr": "str",
                    "est_usd": 0, "missing_tr": ["what could not be done"], "tips_tr": ["print tips"],
                    "height_mm": "final height", "colors": "1..4 filament colours"},
}


class AgentUnavailable(Exception):
    pass


def enabled() -> bool:
    s = get_settings()
    return bool(s.studio_hook_url and secret("studio") and Path(s.exchange_dir).is_dir())


def _call(method: str, path: str, **kw) -> dict:
    s = get_settings()
    url = s.studio_hook_url.rstrip("/") + path
    try:
        r = httpx.request(method, url, timeout=30, headers={"Authorization": f"Bearer {secret('studio')}"}, **kw)
    except httpx.HTTPError as e:
        raise AgentUnavailable(f"aicp'ye ulaşılamadı ({type(e).__name__})")
    if r.status_code != 200:
        raise AgentUnavailable(f"aicp cevap vermedi (HTTP {r.status_code})")
    return r.json()


def send(text: str, session: str | None = None) -> dict:
    return _call("POST", "/hook/studio/send", json={"text": text, "session": session, "new": session is None})


def status(message_id: str) -> dict:
    return _call("GET", "/hook/studio/status", params={"message_id": message_id})


def _open(p: Path, mode: int) -> None:
    try:
        os.chmod(p, mode)          # the agent runs as another user on the host
    except OSError:
        pass


def _job_folder(j: Job) -> tuple[Path, str]:
    s = get_settings()
    d = Path(s.exchange_dir) / j.id
    d.mkdir(parents=True, exist_ok=True)
    _open(d, 0o2777)
    return d, f"{s.agent_jobs_dir.rstrip('/')}/{j.id}"


def _write_job(j: Job, d: Path, payload: dict) -> None:
    tmp = d / "job.json.tmp"
    tmp.write_text(json.dumps({"protocol": PROTOCOL, **payload}, ensure_ascii=False, indent=1))
    tmp.replace(d / "job.json")
    _open(d / "job.json", 0o666)


def _timeout_s(eff: dict) -> int:
    return {"mid": 20 * 60, "high": 35 * 60, "extra": 60 * 60}.get(eff["name"], 35 * 60)


def _answers_text(dialog: list[dict]) -> str:
    last = dialog[-1]
    if last.get("kind") == "confirm_ok":
        return "Aslı planı onayladı: \"Tamam, yap.\" Şimdi uygula."
    if last.get("kind") == "change":
        return f"Aslı planda değişiklik istedi: \"{last.get('answer')}\". Planı buna göre yenile."
    return f"Aslı'nın cevabı: \"{last.get('answer')}\". Devam et."


def run_model_job(j: Job, *, notes: list[dict], extra: str, eff: dict, confirm_plan: bool, height: float,
                  source: str) -> dict:
    """Hand the job to studio-editor and wait for result.json. Returns the result dict on "done"
    (after saving the new version); raises WaitForUser for questions/plans, JobFailed on failure."""
    from .blender import to_print_mm
    path, ver, meta = version_path(j.input["version_id"])
    j.asset_id = ver["asset_id"]
    d, agent_dir = _job_folder(j)
    if not (d / "input.glb").exists():
        shutil.copyfile(path, d / "input.glb")
        _open(d / "input.glb", 0o666)

    pinned = [n for n in notes if n.get("p")]
    mm = to_print_mm(path, meta.get("up", "y"), height, [n["p"] for n in pinned])[0] if pinned else []
    pin_mm = {n["n"]: p.round(2).tolist() for n, p in zip(pinned, mm)}
    asset = db.row("SELECT name FROM assets WHERE id=?", ver["asset_id"])
    dialog = j.ctx.get("dialog", [])
    _write_job(j, d, {
        "job_id": j.id, "kind": "model", "input": "input.glb", "up": meta.get("up", "y"), "height_mm": height,
        "model_name": asset["name"] if asset else "", "about": meta.get("spec", {}).get("description")
        or meta.get("prompt") or "", "notes": [{"n": n["n"], "text": n["text"], "pin_gltf": n.get("p"),
                                                "pin_mm": pin_mm.get(n["n"])} for n in notes],
        "extra": extra, "effort": eff["name"], "edit_rounds_hint": eff["edit_rounds"],
        "confirm_plan": confirm_plan and not j.ctx.get("confirmed"), "dialog": dialog})

    a = j.ctx.setdefault("aicp", {})
    if not a.get("message_id"):
        j.state("AGENT_PLAN", "Tasarım ajanına gönderildi")
        r = send(f"Yeni iş: {agent_dir}/job.json oku ve protokole göre çalış. STUDIO_DIR = "
                 f"{str(Path(get_settings().agent_jobs_dir).parent)}. Aslı için, efor: {eff['label']}.")
        a.update(message_id=r["message_id"], session=r["session"], sent_at=time.time(), dialog_sent=len(dialog))
        j.save()
    elif a.get("dialog_sent", 0) < len(dialog):
        r = send(_answers_text(dialog) + f" (iş klasörü: {agent_dir}, job.json güncellendi)", session=a["session"])
        a.update(message_id=r["message_id"], sent_at=time.time(), dialog_sent=len(dialog))
        j.save()

    j.state("MCP_EXECUTION", "Tasarım ajanı Blender'da çalışıyor")
    res_path = d / "result.json"
    deadline = a["sent_at"] + _timeout_s(eff)
    last_check = 0.0
    while True:
        if res_path.exists() and res_path.stat().st_mtime >= a["sent_at"] - 1:
            try:
                res = json.loads(res_path.read_text())
                break
            except (OSError, json.JSONDecodeError):
                pass                                 # being written; try again
        if time.time() - last_check > 30:
            last_check = time.time()
            j.save()                                 # also notices a cancel
            try:
                st = status(a["message_id"])
            except AgentUnavailable as e:
                st = {"status": "unknown", "error": str(e)}
            if st.get("status") in ("done", "failed", "error") and not res_path.exists():
                time.sleep(3)
                if not res_path.exists():
                    raise JobFailed("Tasarım ajanı bir sonuç yazmadan bitti. Tekrar dene ya da notu biraz açarak yaz.")
        if time.time() > deadline:
            raise JobFailed("Tasarım ajanı zamanında bitiremedi. Biraz sonra tekrar dene.")
        time.sleep(3)

    n = len(list(d.glob("result_*.json")))
    res_path.replace(d / f"result_{n}.json")         # keep a history; the next answer starts clean
    st = res.get("status")
    if res.get("praise_tr"):
        j.log(str(res["praise_tr"]))
    if st in ("question", "confirm"):
        ask = {"kind": "question" if st == "question" else "confirm",
               "text": str(res.get("question_tr") if st == "question" else res.get("plan_tr") or ""),
               "praise": res.get("praise_tr"), "options": [str(o)[:60] for o in (res.get("options_tr") or [])][:4]}
        if st == "confirm":
            ask["est_usd"] = float(res.get("est_usd") or 0)
        j.ctx["ask"] = ask
        j.log(("Sana bir sorusu var: " if st == "question" else "Plan onayını bekliyor: ") + ask["text"][:200])
        raise WaitForUser()
    if st != "done":
        raise JobFailed(str(res.get("message_tr") or "Tasarım ajanı bu isteği yapamadı."))
    out = d / "result.glb"
    if not out.exists():
        raise JobFailed("Tasarım ajanı model dosyası yazmadı.")
    for v in ("front", "right", "back", "top"):
        r = d / "renders" / f"{v}.png"
        if r.exists():
            shutil.copyfile(r, j.dir / f"final_{v}.png")
    if res.get("message_tr"):
        j.log(str(res["message_tr"]))
    if not j.version_id or j.version_id == j.input.get("version_id"):
        j.version_id = add_version(j.asset_id, source, out,
                                   {"up": "y", "units": "mm", "height_mm": float(res.get("height_mm") or height),
                                    "effort": eff["name"], "notes": [x["text"] for x in notes], "agent": "studio-editor",
                                    "directive": extra}, parent_id=ver["id"])
    j.save()
    return res

