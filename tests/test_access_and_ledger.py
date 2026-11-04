import pytest

from conftest import login

H = {"X-Studio": "1"}


def test_login_and_me(client, users):
    assert client.get("/api/me").status_code == 401
    assert client.post("/api/login", json={"key": "wrong"}).status_code == 401
    login(client, users["asli"][1])
    me = client.get("/api/me").json()
    assert me["user"]["slug"] == "asli" and me["user"]["role"] == "member"
    assert me["balance"]["available"] == 5.0
    assert "fal_balance" not in me


def test_member_cannot_use_admin(client, users):
    login(client, users["asli"][1])
    for path in ("/api/admin/overview",):
        assert client.get(path).status_code == 403
    for path in ("/api/admin/grant", "/api/admin/fal_balance", "/api/admin/test_notify"):
        assert client.post(path, json={"slug": "asli", "amount": 50}, headers=H).status_code == 403


def test_post_without_header_is_refused(client, users):
    login(client, users["asli"][1])
    assert client.post("/api/jobs/prepare", json={}).status_code == 403


def test_member_cannot_see_other_users_assets(client, users):
    from studio import jobs
    sid = users["serkan"][0]
    aid = jobs.create_asset(sid, "Serkan'ın modeli", "upload")
    login(client, users["asli"][1])
    assert client.get(f"/api/assets/{aid}").status_code == 404
    assert client.get("/api/assets?all=1").json()["assets"] == []
    assert client.get(f"/api/thumbs/{aid}").status_code == 404
    assert client.get(f"/api/files/{aid}/1/model.glb").status_code == 404


def test_owner_sees_everything(client, users):
    from studio import jobs
    jobs.create_asset(users["asli"][0], "Aslı'nın modeli", "upload")
    login(client, users["serkan"][1])
    assert len(client.get("/api/assets?all=1").json()["assets"]) == 1
    o = client.get("/api/admin/overview").json()
    assert {u["slug"] for u in o["users"]} == {"serkan", "asli"}


def test_path_traversal_blocked(client, users):
    login(client, users["serkan"][1])
    from studio import jobs
    jid = jobs.create_job(users["serkan"][0], "prepare", {})
    assert client.get(f"/api/jobs/{jid}/files/..%2F..%2Fstudio.sqlite").status_code in (400, 404)


def test_auto_topup_and_notification(env, users):
    s, sent = env
    from studio import db, ledger
    aid = users["asli"][0]
    # spend 4.9 of 5
    lid = ledger.reserve(aid, 2.9, job_id="j", step="a", endpoint_id="x")
    ledger.settle(lid)
    s.daily_cap_usd = 100
    lid = ledger.reserve(aid, 2.0, job_id="j", step="b", endpoint_id="x")
    ledger.settle(lid)
    titles = [n["title"] for n in db.rows("SELECT title FROM notifications")]
    assert any("%80" in t for t in titles)
    b = ledger.balance(aid)
    assert b["granted"] == 5.0 and abs(b["available"] - 0.1) < 1e-9
    # next reservation does not fit -> automatic +5, Serkan notified, Aslı never blocked
    lid = ledger.reserve(aid, 0.5, job_id="j", step="c", endpoint_id="x")
    b = ledger.balance(aid)
    assert b["granted"] == 10.0
    assert any("otomatik yüklendi" in n["title"] for n in db.rows("SELECT title FROM notifications"))
    ledger.refund(lid)
    assert ledger.balance(aid)["reserved"] == 0


def test_owner_unlimited_never_topped_up(env, users):
    from studio import ledger
    sid = users["serkan"][0]
    env[0].daily_cap_usd = 100
    ledger.settle(ledger.reserve(sid, 20, job_id="j", step="a", endpoint_id="x"))
    b = ledger.balance(sid)
    assert b["unlimited"] and b["granted"] == 0 and b["spent"] == 20


def test_daily_cap_blocks_everyone(env, users):
    from studio import ledger
    env[0].daily_cap_usd = 1.0
    ledger.settle(ledger.reserve(users["serkan"][0], 0.9, job_id="j", step="a", endpoint_id="x"))
    with pytest.raises(ledger.BudgetError):
        ledger.reserve(users["asli"][0], 0.2, job_id="j", step="b", endpoint_id="x")


def test_fal_balance_warning(env, users):
    from studio import db, ledger
    env[0].daily_cap_usd = 100
    ledger.set_fal_balance(1.0)
    ledger.settle(ledger.reserve(users["serkan"][0], 0.85, job_id="j", step="a", endpoint_id="x"))
    assert any("fal bakiyesi" in n["title"] for n in db.rows("SELECT title FROM notifications"))


def test_cli_init_prints_links(env, capsys):
    from studio import cli
    assert cli.main(["init"]) == 0
    out = capsys.readouterr().out
    assert out.count("#key=md_") == 2
    from studio import db, ledger
    asli = db.row("SELECT id FROM users WHERE slug='asli'")
    assert ledger.balance(asli["id"])["granted"] == 5.0


def test_owner_rekey_member(client, users):
    login(client, users["serkan"][1])
    r = client.post("/api/admin/users/asli/rekey", headers=H)
    link = r.json()["link"]
    assert "#key=md_" in link
    from fastapi.testclient import TestClient
    from studio.app import app
    other = TestClient(app)
    assert other.post("/api/login", json={"key": users["asli"][1]}).status_code == 401   # old key dead
    assert other.post("/api/login", json={"key": link.split("#key=")[1]}).status_code == 200


def test_owner_saves_fal_key(client, users, monkeypatch):
    monkeypatch.delenv("FAL_KEY", raising=False)
    from studio.config import secret
    assert secret("fal") == ""
    login(client, users["serkan"][1])
    assert client.post("/api/admin/fal_key", json={"key": "short"}, headers=H).status_code == 400
    assert client.post("/api/admin/fal_key", json={"key": "abc123:" + "x" * 30}, headers=H).status_code == 200
    assert secret("fal").startswith("abc123:")
    assert client.get("/api/admin/overview").json()["fal"]["key_configured"]


def test_stale_reservations(env, users):
    from studio import db, ledger, jobs
    env[0].daily_cap_usd = 100
    aid = users["asli"][0]
    jid = jobs.create_job(aid, "mod01", {})
    a = ledger.reserve(aid, 0.3, job_id=jid, step="x", endpoint_id="e")            # never reached fal
    b = ledger.reserve(aid, 0.2, job_id=jid, step="y", endpoint_id="e")
    ledger.attach_request(b, "req-1")                                                # reached fal
    db.execute("UPDATE ledger SET created_at=created_at-200000")
    db.execute("UPDATE jobs SET status='failed' WHERE id=?", jid)
    ledger.recover_stale_reservations()
    st = {r["id"]: r["status"] for r in db.rows("SELECT id,status FROM ledger WHERE kind='charge'")}
    assert st[a] == "refunded" and st[b] == "settled"
