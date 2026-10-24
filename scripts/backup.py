#!/usr/bin/env python3
"""SQLite online backup includes all immutable files as BLOBs. No WAL file copying."""

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
from datetime import datetime, timezone

p = argparse.ArgumentParser()
p.add_argument("source")
p.add_argument("destination")
p.add_argument("--restore", action="store_true")
args = p.parse_args()
src, dst = Path(args.source).resolve(), Path(args.destination).resolve()
if not src.is_file():
    raise SystemExit("Исходная БД не найдена")
if dst.exists():
    raise SystemExit(
        "Целевой файл существует. Восстановление не перезаписывает рабочую БД"
    )
dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
try:
    with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as source:
        if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Повреждена исходная БД")
        with sqlite3.connect(dst) as target:
            source.backup(target)
    dst.chmod(0o600)
    with sqlite3.connect(dst) as target:
        if (
            target.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            or target.execute("PRAGMA foreign_key_check").fetchall()
        ):
            raise RuntimeError("Проверка БД не пройдена")
        for content, digest in target.execute("SELECT content,sha256 FROM artifacts"):
            if hashlib.sha256(content).hexdigest() != digest:
                raise RuntimeError("Контрольная сумма файла не совпала")
        meta = {
            "created": datetime.now(timezone.utc).isoformat(),
            "sha256": hashlib.sha256(dst.read_bytes()).hexdigest(),
            "documents": target.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
            "artifacts": target.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0],
        }
    if not args.restore:
        with sqlite3.connect(src) as db:
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES(?,?)",
                ("last_backup", json.dumps(meta)),
            )
    dst.with_suffix(dst.suffix + ".json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2)
    )
    dst.with_suffix(dst.suffix + ".json").chmod(0o600)
    print("OK: БД, связи и контрольные суммы файлов проверены")
except BaseException:
    if dst.exists():
        dst.unlink()
    raise
