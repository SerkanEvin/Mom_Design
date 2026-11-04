"""SQLite storage. One connection per thread, WAL mode, small explicit schema."""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time

from .config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  slug TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('owner','member')),
  unlimited INTEGER NOT NULL DEFAULT 0,
  key_hash TEXT UNIQUE,
  disabled INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  kind TEXT NOT NULL CHECK (kind IN ('grant','charge')),
  status TEXT NOT NULL CHECK (status IN ('reserved','settled','refunded')),
  amount_usd REAL NOT NULL,
  job_id TEXT, step TEXT, endpoint_id TEXT, fal_request_id TEXT, note TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_user ON ledger(user_id, kind, status);
CREATE TABLE IF NOT EXISTS assets (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  name TEXT NOT NULL,
  origin TEXT NOT NULL,
  thumb TEXT,
  deleted INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES assets(id),
  n INTEGER NOT NULL,
  parent_id TEXT,
  source TEXT NOT NULL,
  model_file TEXT NOT NULL,
  meta TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  UNIQUE (asset_id, n)
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  module TEXT NOT NULL,
  asset_id TEXT, version_id TEXT,
  status TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT '',
  input TEXT NOT NULL DEFAULT '{}',
  ctx TEXT NOT NULL DEFAULT '{}',
  log TEXT NOT NULL DEFAULT '[]',
  est_usd REAL NOT NULL DEFAULT 0,
  error TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY,
  level TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
  delivered INTEGER NOT NULL DEFAULT 0, error TEXT,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS drawings (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  name TEXT NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  kind TEXT NOT NULL CHECK (kind IN ('model','drawing')),
  asset_id TEXT, version_id TEXT, drawing_id TEXT,
  text TEXT NOT NULL,
  pos TEXT NOT NULL DEFAULT 'null',
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','sent','done')),
  job_id TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS notes_asset ON notes(asset_id, status);
CREATE INDEX IF NOT EXISTS notes_drawing ON notes(drawing_id, status);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS llm_calls (user_id INTEGER NOT NULL, at REAL NOT NULL);
"""

_local = threading.local()
_init_lock = threading.Lock()
_initialized: set[str] = set()


def conn() -> sqlite3.Connection:
    path = str(get_settings().db_path)
    c = getattr(_local, "conn", None)
    if c is None or getattr(_local, "path", None) != path:
        c = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=30000")
        _local.conn, _local.path = c, path
        with _init_lock:
            if path not in _initialized:
                c.executescript(SCHEMA)
                _initialized.add(path)
    return c


def new_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time()):x}{secrets.token_hex(5)}"


def now() -> float:
    return time.time()


def row(sql: str, *args) -> sqlite3.Row | None:
    return conn().execute(sql, args).fetchone()


def rows(sql: str, *args) -> list[sqlite3.Row]:
    return conn().execute(sql, args).fetchall()


def execute(sql: str, *args) -> sqlite3.Cursor:
    return conn().execute(sql, args)


class tx:
    """BEGIN IMMEDIATE ... COMMIT; serialises money checks across threads."""

    def __enter__(self):
        conn().execute("BEGIN IMMEDIATE")
        return conn()

    def __exit__(self, exc_type, exc, tb):
        conn().execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def kv_get(k: str, default=None):
    r = row("SELECT v FROM kv WHERE k=?", k)
    return json.loads(r["v"]) if r else default


def kv_set(k: str, v) -> None:
    execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", k, json.dumps(v))


def reset_for_tests() -> None:
    _local.__dict__.clear()
    _initialized.clear()
