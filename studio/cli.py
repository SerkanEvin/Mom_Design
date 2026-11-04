"""Admin CLI (run inside the container):

  python -m studio.cli init                 # creates serkan (owner, unlimited) + asli (member, 5 $)
  python -m studio.cli rekey asli           # new key for a user, old sessions end
  python -m studio.cli grant asli 5         # add credit
  python -m studio.cli balance
"""
from __future__ import annotations

import sys

from . import auth, db, ledger
from .config import get_settings


def _link(key: str) -> str:
    return f"{get_settings().public_url}/#key={key}"


def main(argv: list[str]) -> int:
    db.conn()
    cmd = argv[0] if argv else "help"
    if cmd == "init":
        for slug, name, role, unlimited, credit in (("serkan", "Serkan", "owner", True, 0),
                                                    ("asli", "Aslı", "member", False, 5.0)):
            if db.row("SELECT 1 FROM users WHERE slug=?", slug):
                print(f"{slug}: zaten var (yeni anahtar için: rekey {slug})")
                continue
            key = auth.create_user(slug, name, role, unlimited)
            uid = db.row("SELECT id FROM users WHERE slug=?", slug)["id"]
            if credit:
                ledger.grant(uid, credit, "başlangıç kredisi")
            print(f"{slug}: giriş bağlantısı (bir kez gösterilir) -> {_link(key)}")
        return 0
    if cmd == "rekey" and len(argv) == 2:
        u = db.row("SELECT * FROM users WHERE slug=?", argv[1])
        if not u:
            print("kullanıcı yok")
            return 1
        key = auth.create_user(u["slug"], u["name"], u["role"], bool(u["unlimited"]))
        print(f"{u['slug']}: yeni giriş bağlantısı -> {_link(key)}")
        return 0
    if cmd == "grant" and len(argv) == 3:
        u = db.row("SELECT * FROM users WHERE slug=?", argv[1])
        ledger.grant(u["id"], float(argv[2]), "CLI yükleme")
        print(ledger.balance(u["id"]))
        return 0
    if cmd == "balance":
        for u in db.rows("SELECT * FROM users"):
            print(u["slug"], ledger.balance(u["id"]))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
