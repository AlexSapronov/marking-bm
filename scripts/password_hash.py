#!/usr/bin/env python3
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from marking_bm.auth import hash_password

p = getpass.getpass("Новый пароль администратора (не менее 12 символов): ")
if len(p) < 12:
    raise SystemExit("Минимум 12 символов")
if p != getpass.getpass("Повторите пароль: "):
    raise SystemExit("Пароли не совпали")
print(hash_password(p))
