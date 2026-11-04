"""fal.ai calls with the ledger in front of every one.

Each call: estimate -> reserve -> submit (request_id saved) -> wait -> settle/refund.
If the worker restarts mid-call, `resume` polls the saved request_id instead of paying again.
"""
from __future__ import annotations

import logging
import math
import time
from pathlib import Path

import fal_client
import httpx

from . import ledger
from .config import get_settings, secret

log = logging.getLogger("studio.fal")


class FalError(Exception):
    """User-facing message in Turkish."""


def _client() -> fal_client.SyncClient:
    key = secret("fal")
    if not key:
        raise FalError("fal.ai anahtarı ayarlı değil (Serkan'ın sunucuda FAL_KEY_FILE vermesi gerekiyor).")
    return fal_client.SyncClient(key=key, default_timeout=120)


def estimate(endpoint_id: str, *, images: int = 1, width: int = 1024, height: int = 1024) -> float:
    p = get_settings().prices.get(endpoint_id)
    if p is None:
        raise FalError(f"{endpoint_id} için fiyat tanımlı değil; önce fiyat tablosuna eklenmeli.")
    if "per_call" in p:
        return p["per_call"]
    if "per_mp" in p:
        mp = max(1, math.ceil(width * height / 1_000_000))   # conservative: started megapixels
        return p["per_mp"] * mp * images
    if "per_second" in p:
        return max(p.get("min_charge", 0.0), p["per_second"] * 3)
    raise FalError(f"{endpoint_id} fiyat birimi anlaşılamadı.")


def actual_cost(endpoint_id: str, est: float, metrics: dict | None) -> float:
    p = get_settings().prices.get(endpoint_id, {})
    if "per_second" in p and metrics and isinstance(metrics.get("inference_time"), (int, float)):
        return max(p["per_second"] * float(metrics["inference_time"]), 0.0001)
    return est


def upload_bytes(data: bytes, content_type: str, name: str) -> str:
    """Upload to fal's CDN (free) and return a URL usable as model input."""
    try:
        return _client().upload(data, content_type, file_name=name)
    except FalError:
        raise
    except Exception as e:  # network / auth: surface a short reason
        raise FalError(f"fal yükleme hatası: {type(e).__name__}") from e


def run(endpoint_id: str, arguments: dict, *, user_id: int, job_id: str, step: str, est_usd: float,
        on_submitted=None, existing: dict | None = None, timeout_s: int = 900) -> dict:
    """Run a model with money reserved first. `existing` = {"request_id", "ledger_id"} resumes a call."""
    client = _client()
    if existing and existing.get("request_id"):
        rid, lid = existing["request_id"], existing.get("ledger_id")
    else:
        lid = ledger.reserve(user_id, est_usd, job_id=job_id, step=step, endpoint_id=endpoint_id)
        try:
            handle = client.submit(endpoint_id, arguments)
        except Exception as e:
            ledger.refund(lid, f"submit failed: {type(e).__name__}")
            raise FalError(f"fal isteği gönderilemedi ({type(e).__name__}).") from e
        rid = handle.request_id
        ledger.attach_request(lid, rid)
        if on_submitted:
            on_submitted({"request_id": rid, "ledger_id": lid})

    handle = client.get_handle(endpoint_id, rid)
    deadline = time.time() + timeout_s
    status = None
    while time.time() < deadline:
        try:
            status = handle.status()
        except Exception as e:
            log.warning("status poll failed: %s", type(e).__name__)
            time.sleep(4)
            continue
        if isinstance(status, fal_client.Completed):
            break
        time.sleep(2.5)
    else:
        # Leave the reservation: fal may still finish and bill. Job can be resumed.
        raise FalError("fal işi zaman aşımına uğradı; iş tekrar denendiğinde kaldığı yerden sürer.")

    if status.error:
        if lid:
            ledger.refund(lid, f"fal error: {status.error_type or ''}")
        raise FalError(f"Model hata verdi: {str(status.error)[:200]}")
    try:
        result = handle.get()
    except Exception as e:
        if lid:
            ledger.refund(lid, f"result failed: {type(e).__name__}")
        raise FalError(f"fal sonucu alınamadı ({type(e).__name__}).") from e
    if lid:
        ledger.settle(lid, actual_cost(endpoint_id, est_usd, status.metrics), rid)
    return result


def download(url: str, dest: Path, max_bytes: int = 300 * 1024 * 1024) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", url, timeout=120, follow_redirects=True) as r:
        r.raise_for_status()
        size = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise FalError("İndirilen dosya çok büyük.")
                f.write(chunk)
    return dest
