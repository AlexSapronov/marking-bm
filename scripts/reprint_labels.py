#!/usr/bin/env python3
"""Re-render existing label PDFs with the current GS1 DataMatrix encoder.

Read-only SQLite access. Does not reissue KM, modify reports or overwrite
immutable ZIP artifacts. The new ZIP is a separate local export.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from marking_bm.codes import parse  # noqa: E402
from marking_bm.labels import render_label  # noqa: E402
from marking_bm.service import safe_name  # noqa: E402
from pypdf import PdfReader, PdfWriter  # noqa: E402

CONFIG = Path.home() / ".config" / "marking-bm" / "local.env"


def local_database() -> Path:
    if not CONFIG.is_file() or CONFIG.is_symlink() or CONFIG.stat().st_mode & 0o077:
        raise ValueError("Не найдена защищённая WSL-конфигурация local.env (права 600)")
    settings = dict(
        line.split("=", 1) for line in CONFIG.read_text(encoding="utf-8").splitlines()
        if line.startswith("BM_") and "=" in line
    )
    data = Path(settings["BM_DATA_DIR"]).resolve()
    if not data.is_relative_to(Path.home().resolve()):
        raise ValueError("Небезопасный путь к БД")
    return data / "marking-bm.sqlite3"


def open_readonly(database: Path):
    if not database.is_file():
        raise ValueError("SQLite-БД не найдена")
    db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def list_documents(db):
    return list(db.execute(
        "SELECT d.id,d.number,MAX(a.version) AS version "
        "FROM documents d JOIN artifacts a ON a.doc_id=d.id "
        "WHERE a.kind='labels' GROUP BY d.id,d.number "
        "ORDER BY d.created DESC LIMIT 50"
    ))


def get_snapshot(db, doc_id):
    row = db.execute(
        "SELECT d.id,d.number,a.version,a.snapshot FROM documents d "
        "JOIN artifacts a ON a.doc_id=d.id AND a.kind='labels' "
        "WHERE d.id=? ORDER BY a.version DESC LIMIT 1", (doc_id,)
    ).fetchone()
    if row is None:
        raise ValueError("Не найдена накладная с сохранённым ZIP этикеток")
    records = json.loads(row["snapshot"])
    if not isinstance(records, list) or not records or any(
        not isinstance(x, dict) for x in records
    ):
        raise ValueError("Неожиданный состав снимка этикеток")
    return dict(row), records


def render_snapshot_zip(records):
    groups = defaultdict(list)
    for item in records:
        full = item["full"]
        quantity = item["quantity"]
        pn = item["pn"]
        gtin = item["gtin"]
        code = parse(full)
        if (
            type(quantity) is not int
            or code.quantity != quantity
            or code.gtin != gtin
            or not isinstance(pn, str)
        ):
            raise ValueError("Снимок КМ и количество расходятся. Экспорт остановлен")
        groups[(pn, quantity, gtin)].append(full)
    archive = BytesIO()
    used = set()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for (pn, quantity, gtin), codes in groups.items():
            pages = PdfWriter()
            for raw in codes:
                pages.add_page(PdfReader(
                    BytesIO(render_label(raw, pn, quantity))
                ).pages[0])
            pdf = BytesIO()
            pages.write(pdf)
            name = safe_name(pn, 100) + f" - {quantity} - {gtin}.pdf"
            if name.casefold() in used:
                raise ValueError("Конфликт имён PDF; перепечатка остановлена")
            used.add(name.casefold())
            z.writestr(name, pdf.getvalue())
    return archive.getvalue()


def main():
    parser = argparse.ArgumentParser(description="Перепечатка PDF КМ без изменения БД и без ЧЗ")
    parser.add_argument("--list", action="store_true", help="Последние 50 накладных с PDF")
    parser.add_argument("--document", help="Внутренний UUID накладной из --list")
    parser.add_argument("--output", type=Path, help="Путь для нового ZIP (без перезаписи)")
    args = parser.parse_args()

    db_path = local_database()
    with open_readonly(db_path) as db:
        if args.list:
            for r in list_documents(db):
                print(f"{r['id']}  |  v{r['version']}  |  {r['number']}")
            return
        if not args.document:
            parser.error("Укажите --list или --document <id>")
        doc, records = get_snapshot(db, args.document)

    # All source bytes come from the immutable snapshot. No DB writes, HTTP
    # calls, reissuing KM, or changing historical print versions.
    content = render_snapshot_zip(records)
    destination = (args.output or (
        ROOT / "reprints" /
        (safe_name(doc["number"]) + f"-v{doc['version']}-GS-fixed.zip")
    )).expanduser().resolve()
    if destination.suffix.lower() != ".zip":
        raise ValueError("Файл вывода должен иметь расширение .zip")
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.umask(0o077)
    with destination.open("xb") as handle:
        handle.write(content)
    destination.chmod(0o600)
    print("Перепечатка готова:", destination)
    print("Кодов сохранено:", len(records), "; новых кодов заказано: 0")
    print("Исходные ZIP и база данных не изменялись.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, sqlite3.Error, TypeError, json.JSONDecodeError, IndexError) as exc:
        raise SystemExit(f"Ошибка: {exc}") from exc
