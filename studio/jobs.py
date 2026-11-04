"""Job queue + resumable step context, assets and versions."""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import traceback
from pathlib import Path

from . import db
from .config import get_settings

log = logging.getLogger("studio.jobs")

ACTIVE = ("queued", "running", "waiting")    # waiting = paused until the user answers the agent


class JobFailed(Exception):
    """Ends a job with a user-facing (Turkish) message."""


class WaitForUser(Exception):
    """Pauses a job until the user answers (the question is in ctx["ask"]); /answer re-queues it."""


# ---------------------------------------------------------------- assets / versions

def create_asset(user_id: int, name: str, origin: str) -> str:
    aid = db.new_id("ast")
    t = db.now()
    db.execute("INSERT INTO assets(id,user_id,name,origin,created_at,updated_at) VALUES(?,?,?,?,?,?)",
               aid, user_id, (name or "Model")[:80], origin, t, t)
    return aid


def asset_dir(asset_id: str) -> Path:
    a = db.row("SELECT a.id, u.slug FROM assets a JOIN users u ON u.id=a.user_id WHERE a.id=?", asset_id)
    return get_settings().files_dir / a["slug"] / a["id"]


def add_version(asset_id: str, source: str, model_path: Path, meta: dict, parent_id: str | None = None,
                extra_files: list[Path] = ()) -> str:
    with db.tx():
        n = db.row("SELECT COALESCE(MAX(n),0)+1 AS n FROM versions WHERE asset_id=?", asset_id)["n"]
        vid = db.new_id("ver")
        vdir = asset_dir(asset_id) / f"v{n}"
        vdir.mkdir(parents=True, exist_ok=True)
        dest = vdir / ("model" + model_path.suffix.lower())
        shutil.copyfile(model_path, dest)
        for f in extra_files:
            shutil.copyfile(f, vdir / f.name)
        db.execute("INSERT INTO versions(id,asset_id,n,parent_id,source,model_file,meta,created_at)"
                   " VALUES(?,?,?,?,?,?,?,?)", vid, asset_id, n, parent_id, source, dest.name,
                   json.dumps(meta), db.now())
        db.execute("UPDATE assets SET updated_at=? WHERE id=?", db.now(), asset_id)
    return vid


def version_path(version_id: str) -> tuple[Path, dict, dict]:
    v = db.row("SELECT * FROM versions WHERE id=?", version_id)
    if not v:
        raise JobFailed("Sürüm bulunamadı.")
    p = asset_dir(v["asset_id"]) / f"v{v['n']}" / v["model_file"]
    return p, dict(v), json.loads(v["meta"])


# ---------------------------------------------------------------- jobs

def create_job(user_id: int, module: str, inp: dict, *, asset_id: str | None = None,
               version_id: str | None = None, est_usd: float = 0.0) -> str:
    jid = db.new_id("job")
    t = db.now()
    db.execute("INSERT INTO jobs(id,user_id,module,asset_id,version_id,status,input,est_usd,created_at,updated_at)"
               " VALUES(?,?,?,?,?,?,?,?,?,?)", jid, user_id, module, asset_id, version_id, "queued",
               json.dumps(inp), est_usd, t, t)
    job_dir(jid).mkdir(parents=True, exist_ok=True)
    return jid


def job_dir(jid: str) -> Path:
    return get_settings().jobs_dir / jid


class Job:
    def __init__(self, r):
        self.id = r["id"]
        self.user_id = r["user_id"]
        self.module = r["module"]
        self.asset_id = r["asset_id"]
        self.version_id = r["version_id"]
        self.input = json.loads(r["input"])
        self.ctx = json.loads(r["ctx"])
        self._log = json.loads(r["log"])
        self.dir = job_dir(self.id)
        self.dir.mkdir(parents=True, exist_ok=True)

    def save(self) -> None:
        db.execute("UPDATE jobs SET ctx=?, log=?, asset_id=?, version_id=?, updated_at=? WHERE id=?",
                   json.dumps(self.ctx), json.dumps(self._log[-200:]), self.asset_id, self.version_id,
                   db.now(), self.id)
        self._cancel_check()

    def state(self, name: str, label: str) -> None:
        db.execute("UPDATE jobs SET state=?, updated_at=? WHERE id=?", name, db.now(), self.id)
        self.log(label)

    def log(self, msg: str) -> None:
        self._log.append({"t": round(time.time(), 1), "m": msg[:500]})
        self.save()

    def _cancel_check(self) -> None:
        r = db.row("SELECT status FROM jobs WHERE id=?", self.id)
        if r and r["status"] == "cancelled":
            raise JobFailed("İş iptal edildi.")

    def fal(self, key: str, endpoint_id: str, args: dict, est: float) -> dict:
        """Resumable paid call: the request id is stored before waiting, so a restart never pays twice."""
        from . import falclient
        calls = self.ctx.setdefault("fal", {})
        done = calls.get(key, {})
        if "result" in done:
            return done["result"]

        def submitted(info):
            calls[key] = info
            self.save()

        res = falclient.run(endpoint_id, args, user_id=self.user_id, job_id=self.id, step=key, est_usd=est,
                            on_submitted=submitted, existing=done or None)
        calls[key] = {**calls.get(key, {}), "result": res}
        self.save()
        return res

    def forget_fal(self, key: str) -> None:
        self.ctx.get("fal", {}).pop(key, None)
        self.save()


_blender_lock = threading.Lock()


def blender_lock() -> threading.Lock:
    return _blender_lock


def _claim():
    with db.tx():
        r = db.row("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1")
        if not r:
            return None
        db.execute("UPDATE jobs SET status='running', updated_at=? WHERE id=?", db.now(), r["id"])
    return db.row("SELECT * FROM jobs WHERE id=?", r["id"])


def run_one(r) -> None:
    from .pipelines import MODULES
    j = Job(r)
    fn = MODULES.get(j.module)
    try:
        if fn is None:
            raise JobFailed(f"Bilinmeyen iş türü: {j.module}")
        fn(j)
        db.execute("UPDATE jobs SET status='done', state='DONE', updated_at=? WHERE id=? AND status='running'",
                   db.now(), j.id)
    except WaitForUser:
        j.save()
        db.execute("UPDATE jobs SET status='waiting', updated_at=? WHERE id=? AND status='running'", db.now(), j.id)
    except JobFailed as e:
        _fail(j, str(e))
    except Exception as e:  # message types from our modules are already Turkish
        msg = str(e) if type(e).__name__ in ("FalError", "LLMError", "MeshError", "ImageError", "BudgetError",
                                              "BlenderError") else f"Beklenmeyen hata ({type(e).__name__})."
        log.error("job %s failed: %s", j.id, traceback.format_exc())
        _fail(j, msg)


def _fail(j: Job, msg: str) -> None:
    j._log.append({"t": round(time.time(), 1), "m": "Hata: " + msg})
    db.execute("UPDATE jobs SET status=CASE WHEN status='cancelled' THEN 'cancelled' ELSE 'failed' END,"
               " error=?, log=?, updated_at=? WHERE id=?", msg[:500], json.dumps(j._log[-200:]), db.now(), j.id)


def worker_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        r = None
        try:
            r = _claim()
        except Exception:
            log.exception("claim failed")
        if r is None:
            stop.wait(1.5)
            continue
        run_one(r)


def recover_after_restart() -> None:
    """Jobs cut off by a restart continue from their saved context."""
    db.execute("UPDATE jobs SET status='queued' WHERE status='running'")


def retry(jid: str) -> None:
    db.execute("UPDATE jobs SET status='queued', error=NULL, updated_at=? WHERE id=? AND status='failed'",
               db.now(), jid)


def public_job(r) -> dict:
    return {"id": r["id"], "module": r["module"], "status": r["status"], "state": r["state"],
            "asset_id": r["asset_id"], "version_id": r["version_id"], "error": r["error"],
            "est_usd": r["est_usd"], "log": json.loads(r["log"]), "result": json.loads(r["ctx"]).get("result"),
            "ask": json.loads(r["ctx"]).get("ask") if r["status"] == "waiting" else None,
            "created_at": r["created_at"], "updated_at": r["updated_at"],
            "input": {k: v for k, v in json.loads(r["input"]).items() if k != "labels"}}
