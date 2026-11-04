"""HTTP app: the designer page + JSON API. Members see only their own things; the owner sees all."""
from __future__ import annotations

import base64
import json
import logging
import re
import threading
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import auth, db, imaging, jobs, ledger, meshio, notify
from .config import EFFORTS, effort, get_settings, save_secret, secret
from .pipelines import estimate

log = logging.getLogger("studio")
WEB = Path(__file__).with_name("web")
SAFE_NAME = re.compile(r"^[\w.\-]{1,120}$")
MAX_IMAGE = 20 * 1024 * 1024
MAX_MESH = 150 * 1024 * 1024

limiter = auth.LoginLimiter()
_stop = threading.Event()


# ------------------------------------------------------------------ lifecycle

def _startup() -> None:
    db.conn()
    jobs.recover_after_restart()
    ledger.recover_stale_reservations()
    for i in range(max(1, get_settings().workers)):
        threading.Thread(target=jobs.worker_loop, args=(_stop,), name=f"worker-{i}", daemon=True).start()

    def housekeeping():
        while not _stop.wait(300):
            try:
                notify.retry_undelivered()
                ledger.recover_stale_reservations()
            except Exception:
                log.exception("housekeeping")
    threading.Thread(target=housekeeping, daemon=True).start()


@asynccontextmanager
async def lifespan(_app):
    _startup()
    yield
    _stop.set()


app = FastAPI(title="Mom_Design", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB), name="static")


# ------------------------------------------------------------------ auth helpers

def client_ip(req: Request) -> str:
    xff = req.headers.get("x-forwarded-for", "")
    return xff.split(",")[0].strip() if xff else (req.client.host if req.client else "?")


def current_user(req: Request):
    u = auth.user_by_session(req.cookies.get(auth.SESSION_COOKIE))
    if not u:
        raise HTTPException(401, "Giriş gerekli")
    if req.method not in ("GET", "HEAD") and req.headers.get("x-studio") != "1":
        raise HTTPException(403, "İstek reddedildi")
    return u


def owner_only(u=Depends(current_user)):
    if u["role"] != "owner":
        raise HTTPException(403, "Yalnızca Serkan")
    return u


def _asset_for(u, asset_id: str):
    a = db.row("SELECT * FROM assets WHERE id=? AND deleted=0", asset_id)
    if not a or (u["role"] != "owner" and a["user_id"] != u["id"]):
        raise HTTPException(404, "Bulunamadı")
    return a


def _version_for(u, version_id: str):
    v = db.row("SELECT * FROM versions WHERE id=?", version_id)
    if not v:
        raise HTTPException(404, "Sürüm bulunamadı")
    _asset_for(u, v["asset_id"])
    return v


def _job_for(u, job_id: str):
    r = db.row("SELECT * FROM jobs WHERE id=?", job_id)
    if not r or (u["role"] != "owner" and r["user_id"] != u["id"]):
        raise HTTPException(404, "İş bulunamadı")
    return r


async def _read_limited(f: UploadFile, limit: int) -> bytes:
    data = await f.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"Dosya çok büyük (en fazla {limit // (1024 * 1024)} MB)")
    return data


# ------------------------------------------------------------------ pages

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((WEB / "index.html").read_text(), headers={
        "Cache-Control": "no-cache", "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin"})


@app.get("/healthz")
def healthz():
    db.row("SELECT 1")
    return {"ok": True}


@app.post("/api/login")
async def login(req: Request, resp: Response):
    ip = client_ip(req)
    if limiter.blocked(ip):
        raise HTTPException(429, "Çok fazla hatalı deneme, 10 dakika sonra tekrar dene.")
    try:
        body = await req.json()
    except Exception:
        body = {}
    u = auth.user_by_key(str(body.get("key", "")))
    if not u:
        limiter.fail(ip)
        raise HTTPException(401, "Anahtar geçersiz")
    token = auth.new_session(u["id"])
    resp.set_cookie(auth.SESSION_COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True,
                    secure=get_settings().cookie_secure, samesite="lax", path="/")
    return {"user": auth.public_user(u)}


@app.post("/api/logout")
def logout(req: Request, resp: Response):
    auth.end_session(req.cookies.get(auth.SESSION_COOKIE))
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(u=Depends(current_user)):
    out = {"user": auth.public_user(u), "balance": ledger.balance(u["id"]), "usage": ledger.usage(u["id"]),
           "efforts": [{"name": k, "label": v["label"]} for k, v in EFFORTS.items()]}
    if u["role"] == "owner":
        out["fal_balance"] = ledger.fal_balance_amount()
    return out


# ------------------------------------------------------------------ assets

def _asset_json(a, with_versions=False) -> dict:
    owner = db.row("SELECT slug, name FROM users WHERE id=?", a["user_id"])
    d = {"id": a["id"], "name": a["name"], "origin": a["origin"], "has_thumb": bool(a["thumb"]),
         "owner": owner["name"], "created_at": a["created_at"], "updated_at": a["updated_at"]}
    vs = db.rows("SELECT * FROM versions WHERE asset_id=? ORDER BY n DESC", a["id"])
    d["latest"] = _version_json(vs[0]) if vs else None
    if with_versions:
        d["versions"] = [_version_json(v) for v in vs]
    return d


def _version_json(v) -> dict:
    meta = json.loads(v["meta"])
    return {"id": v["id"], "n": v["n"], "source": v["source"], "parent_id": v["parent_id"],
            "url": f"/api/files/{v['asset_id']}/{v['n']}/{v['model_file']}", "created_at": v["created_at"],
            "meta": {k: meta[k] for k in ("prompt", "directive", "views", "images", "original", "height_mm",
                                          "effort", "notes", "drawing_id", "relief")
                     if k in meta}}


@app.get("/api/assets")
def list_assets(all: int = 0, u=Depends(current_user)):
    if u["role"] == "owner" and all:
        rs = db.rows("SELECT * FROM assets WHERE deleted=0 ORDER BY updated_at DESC LIMIT 300")
    else:
        rs = db.rows("SELECT * FROM assets WHERE deleted=0 AND user_id=? ORDER BY updated_at DESC LIMIT 300", u["id"])
    return {"assets": [_asset_json(a) for a in rs]}


@app.get("/api/assets/{asset_id}")
def get_asset(asset_id: str, u=Depends(current_user)):
    a = _asset_for(u, asset_id)
    d = _asset_json(a, with_versions=True)
    js = db.rows("SELECT * FROM jobs WHERE asset_id=? ORDER BY created_at DESC LIMIT 30", asset_id)
    d["jobs"] = [_job_json(j) for j in js]
    return d


@app.post("/api/assets/{asset_id}/rename")
async def rename_asset(asset_id: str, req: Request, u=Depends(current_user)):
    _asset_for(u, asset_id)
    name = str((await req.json()).get("name", "")).strip()[:80]
    if not name:
        raise HTTPException(400, "Ad boş olamaz")
    db.execute("UPDATE assets SET name=?, updated_at=? WHERE id=?", name, db.now(), asset_id)
    return {"ok": True}


@app.post("/api/assets/{asset_id}/delete")
def delete_asset(asset_id: str, u=Depends(current_user)):
    _asset_for(u, asset_id)
    db.execute("UPDATE assets SET deleted=1, updated_at=? WHERE id=?", db.now(), asset_id)
    return {"ok": True}


@app.post("/api/assets/{asset_id}/thumb")
async def set_thumb(asset_id: str, req: Request, u=Depends(current_user)):
    _asset_for(u, asset_id)
    data_url = str((await req.json()).get("png", ""))
    if not data_url.startswith("data:image/png;base64,") or len(data_url) > 2_000_000:
        raise HTTPException(400, "Geçersiz görsel")
    path = jobs.asset_dir(asset_id) / "thumb.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(data_url.split(",", 1)[1]))
    db.execute("UPDATE assets SET thumb=? WHERE id=?", "thumb.png", asset_id)
    return {"ok": True}


@app.get("/api/thumbs/{asset_id}")
def thumb(asset_id: str, u=Depends(current_user)):
    a = _asset_for(u, asset_id)
    p = jobs.asset_dir(asset_id) / "thumb.png"
    if a["thumb"] and p.exists():
        return FileResponse(p, media_type="image/png", headers={"Cache-Control": "private, max-age=60"})
    vs = db.row("SELECT n FROM versions WHERE asset_id=? ORDER BY n LIMIT 1", asset_id)
    if vs:
        src = jobs.asset_dir(asset_id) / f"v{vs['n']}" / "source.png"
        if src.exists():
            return FileResponse(src, media_type="image/png")
    raise HTTPException(404, "Küçük resim yok")


@app.get("/api/files/{asset_id}/{n}/{name}")
def asset_file(asset_id: str, n: int, name: str, u=Depends(current_user)):
    _asset_for(u, asset_id)
    if not SAFE_NAME.match(name):
        raise HTTPException(400, "Geçersiz ad")
    p = jobs.asset_dir(asset_id) / f"v{n}" / name
    if not p.is_file():
        raise HTTPException(404, "Dosya yok")
    return FileResponse(p, filename=name if p.suffix in (".glb", ".3mf", ".stl", ".zip") else None)


# ------------------------------------------------------------------ jobs

def _job_json(r) -> dict:
    d = jobs.public_job(r)
    c = db.row("SELECT COALESCE(SUM(amount_usd),0) AS s FROM ledger WHERE job_id=? AND kind='charge'"
               " AND status IN ('reserved','settled')", r["id"])
    d["cost_usd"] = round(c["s"], 4)
    return d


def _new_job(u, module: str, inp: dict, files: dict[str, bytes] | None = None, **kw) -> dict:
    est = estimate(module, inp)
    jid = jobs.create_job(u["id"], module, inp, est_usd=est, **kw)
    d = jobs.job_dir(jid)
    for name, data in (files or {}).items():
        (d / name).write_bytes(data)
    return _job_json(db.row("SELECT * FROM jobs WHERE id=?", jid))


def _effort_name(v) -> str:
    v = str(v or "")
    if v and v not in EFFORTS:
        raise HTTPException(400, "Çaba seviyesi: orta, yüksek ya da ekstra")
    return effort(v)["name"]


@app.get("/api/estimate")
def get_estimate(module: str, include_top: int = 0, count: int = 1, effort: str = "", target: str = "",
                 u=Depends(current_user)):
    return {"usd": estimate(module, {"include_top": bool(include_top), "count": count,
                                     "effort": _effort_name(effort), "target": target}),
            "balance": ledger.balance(u["id"])}


@app.post("/api/jobs/mod01")
async def start_mod01(image: UploadFile = File(...), prompt: str = Form(""), include_top: int = Form(0),
                      effort: str = Form(""), u=Depends(current_user)):
    eff = _effort_name(effort)
    try:
        im = imaging.load_upload(await _read_limited(image, MAX_IMAGE))
    except imaging.ImageError as e:
        raise HTTPException(400, str(e))
    return _new_job(u, "mod01", {"prompt": prompt[:1000], "include_top": bool(include_top), "effort": eff},
                    {"input_0.png": imaging.png_bytes(im)})


@app.post("/api/jobs/mod02")
async def start_mod02(images: list[UploadFile] = File(...), name: str = Form(""), u=Depends(current_user)):
    if not 1 <= len(images) <= 6:
        raise HTTPException(400, "1 ile 6 arasında görsel yükle")
    files = {}
    for i, f in enumerate(images):
        try:
            files[f"input_{i}.png"] = imaging.png_bytes(imaging.load_upload(await _read_limited(f, MAX_IMAGE)))
        except imaging.ImageError as e:
            raise HTTPException(400, f"{f.filename}: {e}")
    return _new_job(u, "mod02", {"count": len(files), "name": name[:80]}, files)


@app.post("/api/jobs/import")
async def start_import(file: UploadFile = File(...), name: str = Form(""), u=Depends(current_user)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in meshio.MESH_EXTS:
        raise HTTPException(400, "GLB, OBJ, STL, PLY, 3MF veya STEP yükle")
    data = await _read_limited(file, MAX_MESH)
    return _new_job(u, "import", {"file": "upload" + ext, "name": (name or Path(file.filename).stem)[:80]},
                    {"upload" + ext: data})


@app.post("/api/jobs/prepare")
async def start_prepare(req: Request, u=Depends(current_user)):
    b = await req.json()
    v = _version_for(u, str(b.get("version_id", "")))
    return _new_job(u, "prepare", {"version_id": v["id"], "height_mm": float(b.get("height_mm", 80)),
                                   "colors": max(1, min(int(b.get("colors", 3)), 4)), "remesh": bool(b.get("remesh"))},
                    asset_id=v["asset_id"], version_id=v["id"])


@app.post("/api/jobs/resegment")
async def start_resegment(req: Request, u=Depends(current_user)):
    b = await req.json()
    prep = _job_for(u, str(b.get("prep_job", "")))
    return _new_job(u, "resegment", {"prep_job": prep["id"], "colors": max(1, min(int(b.get("colors", 3)), 8)),
                                     "seed": int(b.get("seed", 0))}, asset_id=prep["asset_id"])


@app.post("/api/jobs/export")
async def start_export(req: Request, u=Depends(current_user)):
    b = await req.json()
    prep = _job_for(u, str(b.get("prep_job", "")))
    labels = b.get("labels")
    if not isinstance(labels, list) or len(labels) > 3_000_000:
        raise HTTPException(400, "Renk listesi eksik")
    mode = b.get("mode", "painted")
    if mode not in ("painted", "parts", "stl"):
        raise HTTPException(400, "Bilinmeyen çıktı")
    return _new_job(u, "export", {"prep_job": prep["id"], "labels": labels, "palette": b.get("palette") or [],
                                  "mode": mode, "name": str(b.get("name", "model"))[:40]}, asset_id=prep["asset_id"])


@app.post("/api/jobs/edit")
async def start_edit(req: Request, u=Depends(current_user)):
    b = await req.json()
    v = _version_for(u, str(b.get("version_id", "")))
    directive = str(b.get("directive", "")).strip()[:1000]
    erase = []
    for e in (b.get("erase") or [])[:30]:
        try:
            p = [float(x) for x in e["p"]][:3]
            r = float(e.get("r_mm", 3))
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "Silgi noktası hatalı")
        if len(p) != 3 or not 0.5 <= r <= 25:
            raise HTTPException(400, "Silgi noktası hatalı")
        erase.append({"p": p, "r_mm": r})
    if not directive and not erase:
        raise HTTPException(400, "Ne değişeceğini yaz ya da silinecek yerleri işaretle")
    return _new_job(u, "edit", {"version_id": v["id"], "directive": directive, "erase": erase,
                                "height_mm": float(b.get("height_mm", 80)), "effort": _effort_name(b.get("effort"))},
                    asset_id=v["asset_id"], version_id=v["id"])


@app.get("/api/jobs")
def list_jobs(active: int = 0, u=Depends(current_user)):
    q = "SELECT * FROM jobs WHERE user_id=?" + (" AND status IN ('queued','running','waiting')" if active else "") + \
        " ORDER BY created_at DESC LIMIT 50"
    return {"jobs": [_job_json(r) for r in db.rows(q, u["id"])]}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, u=Depends(current_user)):
    r = _job_for(u, job_id)
    d = _job_json(r)
    if r["status"] == "queued":
        d["queue_position"] = db.row("SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued','running')"
                                     " AND created_at<?", r["created_at"])["n"]
    return d


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, u=Depends(current_user)):
    _job_for(u, job_id)
    db.execute("UPDATE jobs SET status='cancelled', updated_at=? WHERE id=? AND status IN ('queued','running','waiting')",
               db.now(), job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/answer")
async def answer_job(job_id: str, req: Request, u=Depends(current_user)):
    """Her answer to the agent's question or plan ("ok": true = Tamam). The job continues from where it paused."""
    from . import agent
    r = _job_for(u, job_id)
    if r["status"] != "waiting" or r["module"] != "agent":
        raise HTTPException(409, "Bu iş bir cevap beklemiyor")
    b = await req.json()
    text = str(b.get("text", "")).strip()[:600]
    j = jobs.Job(r)
    agent.answer(j, text, bool(b.get("ok")))
    j.save()
    db.execute("UPDATE jobs SET status='queued', updated_at=? WHERE id=? AND status='waiting'", db.now(), job_id)
    return _job_json(db.row("SELECT * FROM jobs WHERE id=?", job_id))


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str, u=Depends(current_user)):
    _job_for(u, job_id)
    jobs.retry(job_id)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/files/{name}")
def job_file(job_id: str, name: str, u=Depends(current_user)):
    _job_for(u, job_id)
    if not SAFE_NAME.match(name):
        raise HTTPException(400, "Geçersiz ad")
    p = jobs.job_dir(job_id) / name
    if not p.is_file():
        raise HTTPException(404, "Dosya yok")
    dl = p.suffix in (".3mf", ".stl", ".zip")
    return FileResponse(p, filename=name if dl else None,
                        media_type="application/json" if p.suffix == ".json" else None)


# ------------------------------------------------------------------ drawings (2D)

def _drawing_for(u, drawing_id: str):
    d = db.row("SELECT * FROM drawings WHERE id=? AND deleted=0", drawing_id)
    if not d or (u["role"] != "owner" and d["user_id"] != u["id"]):
        raise HTTPException(404, "Çizim bulunamadı")
    return d


def _drawing_file(d) -> Path:
    slug = db.row("SELECT slug FROM users WHERE id=?", d["user_id"])["slug"]
    return get_settings().files_dir / slug / "drawings" / f"{d['id']}.png"


def _drawing_json(d) -> dict:
    owner = db.row("SELECT name FROM users WHERE id=?", d["user_id"])["name"]
    return {"id": d["id"], "name": d["name"], "owner": owner, "created_at": d["created_at"],
            "updated_at": d["updated_at"], "image": f"/api/drawings/{d['id']}/image?t={int(d['updated_at'])}"}


async def _drawing_png(image: UploadFile) -> bytes:
    try:
        im = imaging.load_upload(await _read_limited(image, MAX_IMAGE))
    except imaging.ImageError as e:
        raise HTTPException(400, str(e))
    return imaging.png_bytes(im)


@app.get("/api/drawings")
def list_drawings(all: int = 0, u=Depends(current_user)):
    if u["role"] == "owner" and all:
        rs = db.rows("SELECT * FROM drawings WHERE deleted=0 ORDER BY updated_at DESC LIMIT 200")
    else:
        rs = db.rows("SELECT * FROM drawings WHERE deleted=0 AND user_id=? ORDER BY updated_at DESC LIMIT 200",
                     u["id"])
    return {"drawings": [_drawing_json(d) for d in rs]}


@app.post("/api/drawings")
async def create_drawing(image: UploadFile = File(...), name: str = Form(""), u=Depends(current_user)):
    data = await _drawing_png(image)
    did, t = db.new_id("drw"), db.now()
    db.execute("INSERT INTO drawings(id,user_id,name,created_at,updated_at) VALUES(?,?,?,?,?)",
               did, u["id"], (name.strip() or "Çizim")[:80], t, t)
    d = db.row("SELECT * FROM drawings WHERE id=?", did)
    p = _drawing_file(d)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return _drawing_json(d)


@app.get("/api/drawings/{drawing_id}")
def get_drawing(drawing_id: str, u=Depends(current_user)):
    d = _drawing_for(u, drawing_id)
    out = _drawing_json(d)
    js = [r for r in db.rows("SELECT * FROM jobs WHERE user_id=? AND module='agent' ORDER BY created_at DESC"
                             " LIMIT 200", d["user_id"]) if json.loads(r["input"]).get("drawing_id") == d["id"]]
    out["jobs"] = [_job_json(r) for r in js[:20]]
    out["notes"] = _notes_for("drawing_id", d["id"])
    return out


@app.post("/api/drawings/{drawing_id}/image")
async def replace_drawing(drawing_id: str, image: UploadFile = File(...), u=Depends(current_user)):
    d = _drawing_for(u, drawing_id)
    _drawing_file(d).write_bytes(await _drawing_png(image))
    db.execute("UPDATE drawings SET updated_at=? WHERE id=?", db.now(), d["id"])
    return _drawing_json(db.row("SELECT * FROM drawings WHERE id=?", d["id"]))


@app.get("/api/drawings/{drawing_id}/image")
def drawing_image(drawing_id: str, u=Depends(current_user)):
    p = _drawing_file(_drawing_for(u, drawing_id))
    if not p.exists():
        raise HTTPException(404, "Görsel yok")
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": "private, max-age=60"})


@app.post("/api/drawings/{drawing_id}/rename")
async def rename_drawing(drawing_id: str, req: Request, u=Depends(current_user)):
    _drawing_for(u, drawing_id)
    name = str((await req.json()).get("name", "")).strip()[:80]
    if not name:
        raise HTTPException(400, "Ad boş olamaz")
    db.execute("UPDATE drawings SET name=?, updated_at=? WHERE id=?", name, db.now(), drawing_id)
    return {"ok": True}


@app.post("/api/drawings/{drawing_id}/delete")
def delete_drawing(drawing_id: str, u=Depends(current_user)):
    _drawing_for(u, drawing_id)
    db.execute("UPDATE drawings SET deleted=1, updated_at=? WHERE id=?", db.now(), drawing_id)
    return {"ok": True}


# ------------------------------------------------------------------ notes (on a 3D model or a 2D drawing)

MAX_NOTE = 600


def _note_json(n) -> dict:
    status = n["status"]
    if status == "sent" and n["job_id"]:     # the agent run failed or was cancelled: the note is open again
        j = db.row("SELECT status FROM jobs WHERE id=?", n["job_id"])
        if not j or j["status"] in ("failed", "cancelled"):
            status = "open"
    return {"id": n["id"], "kind": n["kind"], "asset_id": n["asset_id"], "version_id": n["version_id"],
            "drawing_id": n["drawing_id"], "text": n["text"], "pos": json.loads(n["pos"]), "status": status,
            "job_id": n["job_id"], "created_at": n["created_at"]}


def _notes_for(col: str, value: str) -> list[dict]:
    assert col in ("asset_id", "drawing_id")
    return [_note_json(n) for n in db.rows(f"SELECT * FROM notes WHERE {col}=? ORDER BY created_at", value)]


def _note_for(u, note_id: str):
    n = db.row("SELECT * FROM notes WHERE id=?", note_id)
    if not n or (u["role"] != "owner" and n["user_id"] != u["id"]):
        raise HTTPException(404, "Not bulunamadı")
    return n


def _pos(kind: str, pos):
    if pos is None:
        return None
    try:
        p = [float(x) for x in pos]
    except (TypeError, ValueError):
        raise HTTPException(400, "Not konumu hatalı")
    if kind == "model" and (len(p) != 3 or any(abs(x) > 1e6 for x in p)):
        raise HTTPException(400, "Not konumu hatalı")
    if kind == "drawing" and (len(p) != 2 or not all(0 <= x <= 100 for x in p)):
        raise HTTPException(400, "Not konumu hatalı")
    return [round(x, 5) for x in p]


@app.get("/api/notes")
def list_notes(asset_id: str = "", drawing_id: str = "", u=Depends(current_user)):
    if asset_id:
        _asset_for(u, asset_id)
        return {"notes": _notes_for("asset_id", asset_id)}
    if drawing_id:
        _drawing_for(u, drawing_id)
        return {"notes": _notes_for("drawing_id", drawing_id)}
    raise HTTPException(400, "Model ya da çizim seç")


@app.post("/api/notes")
async def create_note(req: Request, u=Depends(current_user)):
    b = await req.json()
    text = str(b.get("text", "")).strip()[:MAX_NOTE]
    if not text:
        raise HTTPException(400, "Not boş olamaz")
    kind = b.get("kind")
    asset_id = version_id = drawing_id = None
    if kind == "model":
        v = _version_for(u, str(b.get("version_id", "")))
        asset_id, version_id = v["asset_id"], v["id"]
    elif kind == "drawing":
        drawing_id = _drawing_for(u, str(b.get("drawing_id", "")))["id"]
    else:
        raise HTTPException(400, "Not türü: model ya da çizim")
    if db.row("SELECT COUNT(*) AS n FROM notes WHERE (asset_id=? OR drawing_id=?) AND status='open'",
              asset_id, drawing_id)["n"] >= 40:
        raise HTTPException(400, "En fazla 40 açık not")
    nid, t = db.new_id("not"), db.now()
    db.execute("INSERT INTO notes(id,user_id,kind,asset_id,version_id,drawing_id,text,pos,created_at,updated_at)"
               " VALUES(?,?,?,?,?,?,?,?,?,?)", nid, u["id"], kind, asset_id, version_id, drawing_id, text,
               json.dumps(_pos(kind, b.get("pos"))), t, t)
    return _note_json(db.row("SELECT * FROM notes WHERE id=?", nid))


@app.post("/api/notes/{note_id}/update")
async def update_note(note_id: str, req: Request, u=Depends(current_user)):
    n = _note_for(u, note_id)
    text = str((await req.json()).get("text", "")).strip()[:MAX_NOTE]
    if not text:
        raise HTTPException(400, "Not boş olamaz")
    db.execute("UPDATE notes SET text=?, status=CASE WHEN status='done' THEN 'open' ELSE status END, updated_at=?"
               " WHERE id=?", text, db.now(), n["id"])
    return _note_json(db.row("SELECT * FROM notes WHERE id=?", n["id"]))


@app.post("/api/notes/{note_id}/delete")
def delete_note(note_id: str, u=Depends(current_user)):
    n = _note_for(u, note_id)
    db.execute("DELETE FROM notes WHERE id=?", n["id"])
    return {"ok": True}


# ------------------------------------------------------------------ design agent

@app.post("/api/agent")
async def send_to_agent(req: Request, u=Depends(current_user)):
    """Send open notes (all, or the chosen ones) to the design agent; it makes a print-ready version."""
    b = await req.json()
    eff = _effort_name(b.get("effort"))
    extra = str(b.get("extra", "")).strip()[:MAX_NOTE]
    height = b.get("height_mm")
    if height not in (None, ""):
        try:
            height = float(height)
        except (TypeError, ValueError):
            raise HTTPException(400, "Yükseklik hatalı")
        if not 5 <= height <= 250:
            raise HTTPException(400, "Yükseklik 5–250 mm arasında olmalı")
    else:
        height = None
    want = set(b.get("note_ids") or [])
    files: dict[str, bytes] = {}
    if b.get("drawing_id"):
        d = _drawing_for(u, str(b["drawing_id"]))
        rows = db.rows("SELECT * FROM notes WHERE drawing_id=? ORDER BY created_at", d["id"])
        inp = {"target": "drawing", "drawing_id": d["id"], "name": d["name"]}
        files["drawing.png"] = _drawing_file(d).read_bytes()
        kw = {}
    else:
        v = _version_for(u, str(b.get("version_id", "")))
        rows = db.rows("SELECT * FROM notes WHERE asset_id=? ORDER BY created_at", v["asset_id"])
        # pins only make sense on the version they were placed on; pinless notes apply to any version
        rows = [r for r in rows if r["version_id"] == v["id"] or json.loads(r["pos"]) is None]
        inp = {"target": "model", "version_id": v["id"]}
        kw = {"asset_id": v["asset_id"], "version_id": v["id"]}
    rows = [r for r in rows if _note_json(r)["status"] == "open" and (not want or r["id"] in want)]
    if not rows and not extra:
        raise HTTPException(400, "Gönderilecek not yok; önce bir not ekle")
    notes = [{"id": r["id"], "n": i + 1, "text": r["text"], "p": json.loads(r["pos"])} for i, r in enumerate(rows)]
    inp.update({"notes": notes, "extra": extra, "effort": eff, "height_mm": height})
    job = _new_job(u, "agent", inp, files, **kw)
    for r in rows:
        db.execute("UPDATE notes SET status='sent', job_id=?, updated_at=? WHERE id=?", job["id"], db.now(), r["id"])
    return job


# ------------------------------------------------------------------ owner

@app.get("/api/admin/overview")
def admin_overview(u=Depends(owner_only)):
    users = []
    for x in db.rows("SELECT * FROM users ORDER BY id"):
        users.append({**auth.public_user(x), "balance": ledger.balance(x["id"]), "disabled": bool(x["disabled"])})
    led = db.rows("SELECT l.*, u.slug FROM ledger l JOIN users u ON u.id=l.user_id ORDER BY l.id DESC LIMIT 100")
    notes = db.rows("SELECT * FROM notifications ORDER BY id DESC LIMIT 30")
    since = db.kv_get("fal_balance_set_at", 0)
    spent = db.row("SELECT COALESCE(SUM(amount_usd),0) AS s FROM ledger WHERE kind='charge' AND"
                   " status IN ('reserved','settled') AND created_at>?", since)["s"]
    s = get_settings()
    return {"users": users, "ledger": [dict(r) for r in led], "notifications": [dict(n) for n in notes],
            "fal": {"balance_set": ledger.fal_balance_amount(), "set_at": since, "spent_since": round(spent, 4),
                    "daily_cap": s.daily_cap_usd, "auto_topup": s.auto_topup_usd,
                    "key_configured": bool(secret("fal")), "llm_configured": bool(secret("syn")),
                    "notify_configured": bool(s.notify_url and secret("notify"))}}


@app.post("/api/admin/fal_key")
async def admin_fal_key(req: Request, u=Depends(owner_only)):
    key = str((await req.json()).get("key", "")).strip()
    if not 20 <= len(key) <= 200 or any(c.isspace() for c in key):
        raise HTTPException(400, "Anahtar biçimi hatalı")
    save_secret("fal", key)
    return {"ok": True}


@app.post("/api/admin/grant")
async def admin_grant(req: Request, u=Depends(owner_only)):
    b = await req.json()
    x = db.row("SELECT * FROM users WHERE slug=?", str(b.get("slug", "")))
    amount = float(b.get("amount", 0))
    if not x or not 0 < amount <= 100:
        raise HTTPException(400, "Kullanıcı veya tutar hatalı")
    ledger.grant(x["id"], amount, f"Serkan yükledi")
    return {"balance": ledger.balance(x["id"])}


@app.post("/api/admin/fal_balance")
async def admin_fal_balance(req: Request, u=Depends(owner_only)):
    amount = float((await req.json()).get("amount", 0))
    if not 0 <= amount <= 10000:
        raise HTTPException(400, "Tutar hatalı")
    ledger.set_fal_balance(amount)
    return {"ok": True}


@app.post("/api/admin/users/{slug}/rekey")
def admin_rekey(slug: str, u=Depends(owner_only)):
    x = db.row("SELECT * FROM users WHERE slug=?", slug)
    if not x:
        raise HTTPException(404, "Kullanıcı yok")
    key = auth.create_user(x["slug"], x["name"], x["role"], bool(x["unlimited"]))
    return {"link": f"{get_settings().public_url}/#key={key}"}


@app.post("/api/admin/test_notify")
def admin_test_notify(u=Depends(owner_only)):
    notify.notify("Mom_Design bildirim testi", "asli.serkan.work bildirimleri çalışıyor.", "warn")
    return {"ok": True}


@app.exception_handler(HTTPException)
def http_error(req: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
