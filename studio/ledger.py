"""Credit ledger for fal.ai spend.

balance = sum(grants) - sum(settled charges) - sum(reserved charges)

* Owner (unlimited): charges are recorded, never blocked by balance.
* Member: when a reservation does not fit, the account is topped up automatically by
  `auto_topup_usd` (Aslı never pays) and Serkan is notified. Serkan is also warned when
  80 % of the granted credit is used.
* Global guards for everyone: a daily cap across users and a warning as the real fal balance
  (set by Serkan) runs low.
"""
from __future__ import annotations

import time

from . import db
from .config import get_settings
from .notify import notify


class BudgetError(Exception):
    """A reservation was refused (daily cap). Message is user-facing Turkish."""


def balance(user_id: int) -> dict:
    r = db.row(
        "SELECT "
        " COALESCE(SUM(CASE WHEN kind='grant' THEN amount_usd END),0) AS granted,"
        " COALESCE(SUM(CASE WHEN kind='charge' AND status='settled' THEN amount_usd END),0) AS spent,"
        " COALESCE(SUM(CASE WHEN kind='charge' AND status='reserved' THEN amount_usd END),0) AS reserved"
        " FROM ledger WHERE user_id=?", user_id)
    u = db.row("SELECT unlimited FROM users WHERE id=?", user_id)
    granted, spent, reserved = r["granted"], r["spent"], r["reserved"]
    return {
        "unlimited": bool(u and u["unlimited"]),
        "granted": round(granted, 4),
        "spent": round(spent, 4),
        "reserved": round(reserved, 4),
        "available": round(granted - spent - reserved, 4),
    }


def grant(user_id: int, amount: float, note: str) -> None:
    t = db.now()
    db.execute("INSERT INTO ledger(user_id,kind,status,amount_usd,note,created_at,updated_at)"
               " VALUES(?,?,?,?,?,?,?)", user_id, "grant", "settled", amount, note, t, t)
    db.kv_set(f"warned80:{user_id}", False)


def _today_spend() -> float:
    start = time.time() - 86400
    r = db.row("SELECT COALESCE(SUM(amount_usd),0) AS s FROM ledger WHERE kind='charge'"
               " AND status IN ('reserved','settled') AND created_at>?", start)
    return r["s"]


def _total_spend_since_balance_set() -> float:
    since = db.kv_get("fal_balance_set_at", 0)
    r = db.row("SELECT COALESCE(SUM(amount_usd),0) AS s FROM ledger WHERE kind='charge'"
               " AND status IN ('reserved','settled') AND created_at>?", since)
    return r["s"]


def reserve(user_id: int, amount: float, *, job_id: str, step: str, endpoint_id: str) -> int:
    """Set money aside before a fal call. Returns the ledger row id."""
    s = get_settings()
    amount = round(max(amount, 0.0), 6)
    topups: list[float] = []
    capped = False
    with db.tx():
        capped = _today_spend() + amount > s.daily_cap_usd
        u = db.row("SELECT * FROM users WHERE id=?", user_id)
        if not capped and not u["unlimited"]:
            for _ in range(5):
                if balance(user_id)["available"] >= amount:
                    break
                t = db.now()
                db.execute("INSERT INTO ledger(user_id,kind,status,amount_usd,note,created_at,updated_at)"
                           " VALUES(?,?,?,?,?,?,?)", user_id, "grant", "settled", s.auto_topup_usd,
                           "otomatik yükleme", t, t)
                topups.append(s.auto_topup_usd)
            if topups:
                db.kv_set(f"warned80:{user_id}", False)
        rid = None
        if not capped:
            t = db.now()
            cur = db.execute(
            "INSERT INTO ledger(user_id,kind,status,amount_usd,job_id,step,endpoint_id,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)", user_id, "charge", "reserved", amount, job_id, step, endpoint_id, t, t)
            rid = cur.lastrowid
    if capped:
        if db.kv_get("daily_cap_alert_day") != time.strftime("%Y-%m-%d"):
            db.kv_set("daily_cap_alert_day", time.strftime("%Y-%m-%d"))
            notify("Mom_Design: günlük fal sınırı",
                   f"Son 24 saatte fal harcaması {s.daily_cap_usd:.2f} $ sınırına ulaştı; yeni işler durduruldu.",
                   "urgent")
        raise BudgetError("Bugünkü üretim sınırına ulaşıldı. Serkan'a haber verildi, yarın tekrar dene.")
    if topups:
        b = balance(user_id)
        notify(f"Mom_Design: {u['name']} kredisi otomatik yüklendi",
               f"{u['name']} için +{sum(topups):.2f} $ eklendi (toplam verilen {b['granted']:.2f} $, "
               f"harcanan {b['spent']:.2f} $). Ödemeyi sen yapıyorsun; fal bakiyeni kontrol et.", "warn")
    _check_fal_balance()
    return rid


def settle(ledger_id: int, actual_usd: float | None = None, fal_request_id: str | None = None) -> None:
    r = db.row("SELECT * FROM ledger WHERE id=?", ledger_id)
    if not r or r["status"] != "reserved":
        return
    amount = r["amount_usd"] if actual_usd is None else round(max(actual_usd, 0.0), 6)
    db.execute("UPDATE ledger SET status='settled', amount_usd=?, fal_request_id=COALESCE(?,fal_request_id),"
               " updated_at=? WHERE id=?", amount, fal_request_id, db.now(), ledger_id)
    _check_warn(r["user_id"])


def refund(ledger_id: int, note: str = "") -> None:
    db.execute("UPDATE ledger SET status='refunded', note=?, updated_at=? WHERE id=? AND status='reserved'",
               note[:200], db.now(), ledger_id)


def attach_request(ledger_id: int, fal_request_id: str) -> None:
    db.execute("UPDATE ledger SET fal_request_id=?, updated_at=? WHERE id=?", fal_request_id, db.now(), ledger_id)


def _check_warn(user_id: int) -> None:
    s = get_settings()
    u = db.row("SELECT * FROM users WHERE id=?", user_id)
    if u["unlimited"]:
        return
    b = balance(user_id)
    if b["granted"] > 0 and b["spent"] >= s.warn_ratio * b["granted"] and not db.kv_get(f"warned80:{user_id}"):
        db.kv_set(f"warned80:{user_id}", True)
        notify(f"Mom_Design: {u['name']} kredisinin %{int(s.warn_ratio * 100)}'ini kullandı",
               f"Harcanan {b['spent']:.2f} $ / verilen {b['granted']:.2f} $. Bitince otomatik "
               f"+{s.auto_topup_usd:.2f} $ yüklenecek.", "info")


def fal_balance_amount() -> float:
    return float(db.kv_get("fal_balance_amount", get_settings().fal_balance_usd))


def _check_fal_balance() -> None:
    bal = fal_balance_amount()
    spent = _total_spend_since_balance_set()
    level = None
    if spent >= bal:
        level = "urgent"
    elif spent >= 0.8 * bal:
        level = "warn"
    if level and db.kv_get("fal_balance_alert") != level:
        db.kv_set("fal_balance_alert", level)
        notify("Mom_Design: fal bakiyesi azalıyor",
               f"Bakiye {bal:.2f} $ girildiğinden beri tahmini harcama {spent:.2f} $. "
               "fal.ai'ye yükleme yapıp uygulamada yeni bakiyeyi gir.", level)


def set_fal_balance(amount: float) -> None:
    db.kv_set("fal_balance_set_at", time.time())
    db.kv_set("fal_balance_amount", amount)
    db.kv_set("fal_balance_alert", None)


def recover_stale_reservations(max_age_s: int = 6 * 3600) -> None:
    """Reservations left by a crash with no fal request attached are refunded. Ones that did reach fal
    but whose job ended (failed/cancelled) more than a day ago are counted as spent: fal may have billed."""
    db.execute("UPDATE ledger SET status='settled', note='zaman aşımı: harcandı sayıldı', updated_at=?"
               " WHERE kind='charge' AND status='reserved' AND fal_request_id IS NOT NULL AND created_at<?"
               " AND job_id IN (SELECT id FROM jobs WHERE status IN ('failed','cancelled'))",
               db.now(), db.now() - 86400)
    db.execute("UPDATE ledger SET status='refunded', note='yarım kalan ayırma', updated_at=?"
               " WHERE kind='charge' AND status='reserved' AND fal_request_id IS NULL AND created_at<?",
               db.now(), db.now() - max_age_s)


def usage(user_id: int) -> dict:
    """What the header gauge shows: credit use, this hour's AI calls (members are limited), today's spend."""
    s = get_settings()
    u = db.row("SELECT role, unlimited FROM users WHERE id=?", user_id)
    b = balance(user_id)
    mine_today = db.row("SELECT COALESCE(SUM(amount_usd),0) AS s FROM ledger WHERE user_id=? AND kind='charge'"
                        " AND status IN ('reserved','settled') AND created_at>?", user_id, time.time() - 86400)["s"]
    ai_used = db.row("SELECT COUNT(*) AS n FROM llm_calls WHERE user_id=? AND at>?", user_id, time.time() - 3600)["n"]
    member = bool(u) and u["role"] != "owner"
    used = b["spent"] + b["reserved"]
    return {
        "credit": {"unlimited": b["unlimited"], "used": round(used, 4), "granted": b["granted"],
                   "pct": None if b["unlimited"] or b["granted"] <= 0 else round(min(1.0, used / b["granted"]), 4)},
        "ai_hour": {"used": int(ai_used), "limit": s.member_llm_per_hour if member else None},
        "today": {"mine": round(mine_today, 4), "all": round(_today_spend(), 4), "cap": s.daily_cap_usd},
    }
