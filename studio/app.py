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
from .config import get_settings, save_secret, secret
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
    out = {"user": auth.public_user(u), "balance": ledger.balance(u["id"])}
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
            "meta": {k: meta[k] for k in ("prompt", "directive", "views", "images", "original", "height_mm")
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


@app.get("/api/estimate")
def get_estimate(module: str, include_top: int = 0, count: int = 1, u=Depends(current_user)):
    return {"usd": estimate(module, {"include_top": bool(include_top), "count": count}),
            "balance": ledger.balance(u["id"])}


@app.post("/api/jobs/mod01")
async def start_mod01(image: UploadFile = File(...), prompt: str = Form(""), include_top: int = Form(0),
                      u=Depends(current_user)):
    try:
        im = imaging.load_upload(await _read_limited(image, MAX_IMAGE))
    except imaging.ImageError as e:
        raise HTTPException(400, str(e))
    return _new_job(u, "mod01", {"prompt": prompt[:1000], "include_top": bool(include_top)},
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
    if not directive:
        raise HTTPException(400, "Ne değişeceğini yaz")
    return _new_job(u, "edit", {"version_id": v["id"], "directive": directive,
                                "height_mm": float(b.get("height_mm", 80))}, asset_id=v["asset_id"], version_id=v["id"])


@app.get("/api/jobs")
def list_jobs(active: int = 0, u=Depends(current_user)):
    q = "SELECT * FROM jobs WHERE user_id=?" + (" AND status IN ('queued','running')" if active else "") + \
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
    db.execute("UPDATE jobs SET status='cancelled', updated_at=? WHERE id=? AND status IN ('queued','running')",
               db.now(), job_id)
    return {"ok": True}


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
