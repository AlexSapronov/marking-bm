#!/usr/bin/env python3
"""Read-only diagnosis of a previously submitted ATK document in True API.

Queries the local SQLite database and two True API read endpoints.
Never sends or retries ATK documents; does not print full KM, INN or tokens.
"""
from collections import Counter
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from marking_bm.config import Settings  # noqa: E402
from marking_bm.gateway import Gateway, RemoteError  # noqa: E402


def read_config():
    config = Path.home() / ".config/marking-bm/local.env"
    if not config.is_file() or config.is_symlink():
        raise ValueError("Не найдена локальная конфигурация marking-bm в WSL")
    if config.stat().st_mode & 0o077:
        raise ValueError("Права local.env слишком широкие; выполните chmod 600")
    for line in config.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if k.startswith("BM_"):
                os.environ[k] = v


def load_atk(db_path, external_id):
    if not db_path.is_file():
        raise ValueError("База SQLite не найдена")
    with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        if external_id:
            if not re.fullmatch(r"[0-9a-fA-F-]{36}", external_id):
                raise ValueError("Укажите UUID внешнего документа (36 символов)")
            row = db.execute(
                "SELECT external_id,state,error,payload FROM operations "
                "WHERE kind='atk' AND external_id=?", (external_id,)
            ).fetchone()
        else:
            row = db.execute(
                "SELECT external_id,state,error,payload FROM operations "
                "WHERE kind='atk' AND external_id IS NOT NULL "
                "ORDER BY created DESC LIMIT 1"
            ).fetchone()
    if row is None:
        raise ValueError("Операция АТК с внешним ID в локальной БД не найдена")
    return dict(row)


def parent_label(p):
    if p is None or p == "":
        return "(пусто)"
    if not isinstance(p, str):
        return "(не строка)"
    return "длина=" + str(len(p)) + ", окончание=***" + p[-4:]


def main():
    if len(sys.argv) > 2:
        raise ValueError("Использование: scripts/diagnose_atk.py [внешний UUID]")
    read_config()
    settings = Settings()
    op = load_atk(settings.data_dir / "marking-bm.sqlite3",
                  sys.argv[1] if len(sys.argv) == 2 else None)
    payload = json.loads(op["payload"])
    codes = [item["ki"] for item in payload["products_list"]]
    if not codes or len(codes) != len(set(codes)):
        raise ValueError("Пустой список КИ или повторяющиеся КИ в операции")
    print("=== АТК: безопасная диагностика (только чтение) ===")
    print("ID документа:", op["external_id"])
    print("Состояние в marking-bm:", op["state"])
    print("Количество КИ:", len(codes))
    print("Сообщение приложения:", op["error"] or "(нет)")

    gateway = Gateway(settings, lambda: {})
    doc_list = gateway.request(
        "GET", settings.true_url + "/api/v4/true-api/doc/" + op["external_id"] + "/info",
        params={"pg": "radio", "body": "true", "content": "true"},
    )
    if not isinstance(doc_list, list) or len(doc_list) != 1:
        raise ValueError("ЧЗ вернул неожиданный формат документа")
    doc = doc_list[0]
    if not isinstance(doc, dict) or doc.get("number") != op["external_id"]:
        raise ValueError("Ответ ЧЗ относится к другому документу")
    status = doc.get("status")
    print("Статус документа ЧЗ:", status)
    for field in ("errors", "commonErrors"):
        value = doc.get(field)
        print(field + ":", len(value) if isinstance(value, list) else
              ("есть" if value else 0))

    # POST cises/info is a read-only lookup, NOT a mutating document submission.
    info = gateway.info(codes)
    statuses = Counter(str(ci.get("status", "(нет)")) for ci in info.values())
    print("Статусы КИ:", dict(statuses))
    parents = Counter(ci.get("parent") if isinstance(ci.get("parent"), (str, type(None)))
                      else "(не строка)" for ci in info.values())
    print("Различных parent:", len(parents))
    for value, count in parents.most_common(12):
        print("  parent:", parent_label(value), "| КИ:", count)
    if len(parents) > 12:
        print("  ... ещё", len(parents) - 12, "вариантов")
    relation_fields = sorted({
        field for ci in info.values() for field in ci
        if re.search("parent|atk|aggreg|pack", field, re.I)
    })
    print("Поля связей в cisInfo:", ", ".join(relation_fields) or "(нет)")
    if status != "CHECKED_OK":
        print("ВЫВОД: документ ещё не подтверждён как CHECKED_OK; смотрите документ в ЛК ЧЗ.")
    elif len(parents) == 1 and isinstance(next(iter(parents)), str) and re.fullmatch(r"[0-9]{20}[0-9A-Za-z]{5}", next(iter(parents))):
        print("ВЫВОД: один 25-значный parent найден. Повторите только проверку состояния.")
    elif all(not p for p in parents):
        print("ВЫВОД: CHECKED_OK, но parent у всех КИ пуст. Сверьте наличие АТК в ЛК ЧЗ.")
    else:
        print("ВЫВОД: CHECKED_OK, но поле parent не равно одному 25-значному АТК.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RemoteError, sqlite3.Error, OSError, KeyError, TypeError) as exc:
        print("Диагностика не завершена:", str(exc), file=sys.stderr)
        sys.exit(1)
