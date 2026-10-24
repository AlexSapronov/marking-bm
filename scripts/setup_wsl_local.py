#!/usr/bin/env python3
"""Configure a *real*, single-PC marking-bm instance for WSL2, with no external writes by default."""
from __future__ import annotations

import getpass
import os
from pathlib import Path
import re
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from marking_bm.auth import hash_password  # noqa: E402

CONFIG = Path.home() / ".config" / "marking-bm" / "local.env"
DATA = Path.home() / ".local" / "share" / "marking-bm"

if sys.platform != "linux" or sys.version_info < (3, 12):
    raise SystemExit("Run inside Ubuntu WSL2 with Python 3.12+.")
if CONFIG.exists():
    raise SystemExit(f"Config already exists: {CONFIG}. Never overwrite an existing signer token automatically.")

password = getpass.getpass("Пароль администратора (от 12 символов): ")
if len(password) < 12:
    raise SystemExit("Пароль слишком короткий.")
if getpass.getpass("Повторите пароль: ") != password:
    raise SystemExit("Пароли не совпадают.")
thumbprint = input("Отпечаток сертификата CryptoPro (40 hex-символов): ").replace(" ", "").upper()
if not re.fullmatch(r"[A-F0-9]{40}", thumbprint):
    raise SystemExit("Неверный отпечаток сертификата. Он не должен быть токеном подключения.")

os.umask(0o077)
CONFIG.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
fields = {
    "BM_DATA_DIR": str(DATA),
    "BM_PUBLIC_URL": "http://localhost:8010",
    "BM_COOKIE_SECURE": "0",
    "BM_ADMIN_PASSWORD_HASH": hash_password(password),
    "BM_SIGNER_TOKEN": secrets.token_urlsafe(48),
    "BM_SIGNER_THUMBPRINT": thumbprint,
    "BM_TRUE_URL": "https://markirovka.crpt.ru",
    "BM_OMS_URL": "https://suzgrid.crpt.ru",
    "BM_ALLOW_EXTERNAL_WRITES": "0",
}
password = None
with CONFIG.open("x", encoding="utf-8") as output:
    for k, v in fields.items():
        output.write(f"{k}={v}\n")
CONFIG.chmod(0o600)
print(f"Local WSL configuration created: {CONFIG}")
print(f"Database directory: {DATA}")
print("External writes DISABLED. No token was printed.")
print("Next: import existing VPS database if needed, then run scripts/run_wsl_local.py.")
