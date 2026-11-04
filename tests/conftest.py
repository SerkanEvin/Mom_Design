import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("STUDIO_COOKIE_SECURE", "0")
    monkeypatch.setenv("FAL_KEY", "test-key")
    monkeypatch.setenv("SYN_API_KEY", "test-key")
    monkeypatch.setenv("STUDIO_WORKERS", "0")
    from studio import config, db
    db.reset_for_tests()
    config.reset_settings(None)
    s = config.get_settings()
    sent = []
    from studio import notify
    monkeypatch.setattr(notify, "_deliver", lambda nid: sent.append(nid))
    yield s, sent
    db.reset_for_tests()
    config.reset_settings(None)


@pytest.fixture()
def users(env):
    from studio import auth, db, ledger
    db.conn()
    ks = auth.create_user("serkan", "Serkan", "owner", True)
    ka = auth.create_user("asli", "Aslı", "member", False)
    aid = db.row("SELECT id FROM users WHERE slug='asli'")["id"]
    sid = db.row("SELECT id FROM users WHERE slug='serkan'")["id"]
    ledger.grant(aid, 5.0, "başlangıç")
    return {"serkan": (sid, ks), "asli": (aid, ka)}


@pytest.fixture()
def client(users):
    from fastapi.testclient import TestClient
    from studio.app import app
    return TestClient(app)


def login(client, key):
    r = client.post("/api/login", json={"key": key})
    assert r.status_code == 200, r.text
    return client
