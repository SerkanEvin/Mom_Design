"""Accounts: owner (Serkan) and member (Aslı). Login with a personal access key -> session cookie."""
from __future__ import annotations

import hashlib
import secrets
import time
from collections import defaultdict, deque

from . import db

SESSION_COOKIE = "md_session"
SESSION_DAYS = 180


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def create_user(slug: str, name: str, role: str, unlimited: bool) -> str:
    """Create (or re-key) a user. Returns the new access key, shown once."""
    if role not in ("owner", "member"):
        raise ValueError("role must be owner or member")
    key = "md_" + secrets.token_urlsafe(24)
    existing = db.row("SELECT id FROM users WHERE slug=?", slug)
    if existing:
        db.execute("UPDATE users SET name=?, role=?, unlimited=?, key_hash=?, disabled=0 WHERE id=?",
                   name, role, int(unlimited), _h(key), existing["id"])
        db.execute("DELETE FROM sessions WHERE user_id=?", existing["id"])
    else:
        db.execute("INSERT INTO users(slug,name,role,unlimited,key_hash,created_at) VALUES(?,?,?,?,?,?)",
                   slug, name, role, int(unlimited), _h(key), db.now())
    return key


def user_by_key(key: str):
    if not key or len(key) > 200:
        return None
    return db.row("SELECT * FROM users WHERE key_hash=? AND disabled=0", _h(key.strip()))


def new_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    t = db.now()
    db.execute("INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
               _h(token), user_id, t, t + SESSION_DAYS * 86400)
    return token


def user_by_session(token: str | None):
    if not token:
        return None
    return db.row(
        "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
        "WHERE s.token_hash=? AND s.expires_at>? AND u.disabled=0", _h(token), db.now())


def end_session(token: str | None) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash=?", _h(token))


class LoginLimiter:
    """At most `limit` failed logins per IP per window."""

    def __init__(self, limit: int = 8, window_s: int = 600):
        self.limit, self.window = limit, window_s
        self.fails: dict[str, deque] = defaultdict(deque)

    def blocked(self, ip: str) -> bool:
        q = self.fails[ip]
        cutoff = time.time() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        return len(q) >= self.limit

    def fail(self, ip: str) -> None:
        self.fails[ip].append(time.time())


def public_user(u) -> dict:
    return {"slug": u["slug"], "name": u["name"], "role": u["role"], "unlimited": bool(u["unlimited"])}
