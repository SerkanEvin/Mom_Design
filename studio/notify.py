"""Notifications to Serkan. Stored locally first (owner sees them in the app), then pushed to aicp."""
from __future__ import annotations

import logging
import threading

import httpx

from . import db
from .config import get_settings, secret

log = logging.getLogger("studio.notify")


def notify(title: str, body: str, level: str = "info") -> None:
    cur = db.execute("INSERT INTO notifications(level,title,body,created_at) VALUES(?,?,?,?)",
                     level, title[:120], body[:1000], db.now())
    nid = cur.lastrowid
    threading.Thread(target=_deliver, args=(nid,), daemon=True).start()


def _deliver(nid: int) -> None:
    s = get_settings()
    n = db.row("SELECT * FROM notifications WHERE id=?", nid)
    token = secret("notify")
    if not n or not s.notify_url or not token:
        if n:
            db.execute("UPDATE notifications SET error=? WHERE id=?", "aicp bildirim adresi ayarlı değil", nid)
        return
    try:
        r = httpx.post(s.notify_url, timeout=15, headers={"Authorization": f"Bearer {token}"},
                       json={"title": n["title"], "body": n["body"], "level": n["level"],
                             "source": "mom-design", "link": s.public_url})
        if r.status_code < 300:
            db.execute("UPDATE notifications SET delivered=1, error=NULL WHERE id=?", nid)
        else:
            db.execute("UPDATE notifications SET error=? WHERE id=?", f"HTTP {r.status_code}", nid)
    except httpx.HTTPError as e:
        db.execute("UPDATE notifications SET error=? WHERE id=?", type(e).__name__, nid)
        log.warning("notify failed: %s", type(e).__name__)


def retry_undelivered() -> None:
    for n in db.rows("SELECT id FROM notifications WHERE delivered=0 AND created_at>? ORDER BY id LIMIT 20",
                     db.now() - 86400):
        _deliver(n["id"])
