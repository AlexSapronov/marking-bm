"""Durable, idempotent delivery to the warehouse; independent of CRPT writes."""

import hashlib
import json
import time

import httpx

from .db import dumps, now, uid

SCHEMA = """CREATE TABLE IF NOT EXISTS receipt_deliveries (
    doc_id TEXT PRIMARY KEY REFERENCES documents(id), revision INTEGER NOT NULL,
    fingerprint TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
    warehouse_id TEXT, attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL DEFAULT 0, error TEXT, updated TEXT NOT NULL)"""


def enqueue(db, config, documents, codes):
    """Run in the same transaction that saves the completed ATK exports."""
    row = db.execute(
        "SELECT value FROM settings WHERE key='receipt_bridge_instance'"
    ).fetchone()
    instance = json.loads(row["value"]) if row else uid()
    if not row:
        db.execute(
            "INSERT INTO settings VALUES (?,?)",
            ("receipt_bridge_instance", dumps(instance)),
        )
    for document in documents:
        supplier = db.execute(
            "SELECT name FROM suppliers WHERE id=?", (document["supplier_id"],)
        ).fetchone()["name"]
        items = []
        for code in sorted(codes, key=lambda c: c["cis"]):
            if code["doc_id"] != document["id"]:
                continue
            atk = db.execute(
                "SELECT code FROM atks WHERE id=?", (code["atk_id"],)
            ).fetchone()
            items.append(
                {
                    "raw": code["raw"],
                    "pn": code["pn"],
                    "quantity": code["quantity"],
                    "atk": atk["code"],
                }
            )
        body = {
            "source_instance": instance,
            "source_document": document["id"],
            "number": document["number"],
            "date": document["created"][:10],
            "org_inn": config["inn"],
            "supplier": supplier,
            "codes": items,
        }
        fingerprint = hashlib.sha256(dumps(body).encode()).hexdigest()
        previous = db.execute(
            "SELECT * FROM receipt_deliveries WHERE doc_id=?", (document["id"],)
        ).fetchone()
        if previous and previous["fingerprint"] == fingerprint:
            continue
        body["revision"] = previous["revision"] + 1 if previous else 1
        db.execute(
            """INSERT INTO receipt_deliveries
            (doc_id,revision,fingerprint,payload,state,updated) VALUES (?,?,?,?,'pending',?)
            ON CONFLICT(doc_id) DO UPDATE SET revision=excluded.revision,
            fingerprint=excluded.fingerprint,payload=excluded.payload,state='pending',
            attempts=0,next_attempt=0,error=NULL,updated=excluded.updated""",
            (document["id"], body["revision"], fingerprint, dumps(body), now()),
        )


def deliver(service, transport=None, limit=10):
    settings, store = service.settings, service.store
    if not settings.receipt_bridge_url:
        return
    jobs = store.all(
        "SELECT r.* FROM receipt_deliveries r WHERE r.state!='delivered' AND r.next_attempt<=? "
        "AND NOT EXISTS (SELECT 1 FROM document_stops s WHERE s.doc_id=r.doc_id AND s.resumed_at IS NULL) "
        "AND NOT EXISTS (SELECT 1 FROM codes c JOIN atks a ON a.id=c.atk_id "
        "JOIN group_stops h ON h.group_id=a.batch_id||':'||a.supplier_id "
        "JOIN document_stops s ON s.doc_id=h.doc_id WHERE c.doc_id=r.doc_id AND s.resumed_at IS NULL) "
        "ORDER BY r.updated LIMIT ?",
        (time.time(), limit),
    )
    with httpx.Client(
        timeout=15, transport=transport, trust_env=False, follow_redirects=False
    ) as client:
        for job in jobs:
            with store.read() as db:
                if service.document_stopped(db, job["doc_id"]):
                    continue
                groups = db.execute(
                    "SELECT DISTINCT a.batch_id,a.supplier_id FROM codes c JOIN atks a ON a.id=c.atk_id WHERE c.doc_id=?",
                    (job["doc_id"],),
                ).fetchall()
                if any(
                    service.group_stopped(db, g["batch_id"] + ":" + g["supplier_id"])
                    for g in groups
                ):
                    continue
            error, warehouse_id = None, job["warehouse_id"]
            try:
                response = client.post(
                    settings.receipt_bridge_url
                    + "/api/integrations/marking-bm/receipts",
                    headers={
                        "X-Receipt-Bridge-Key": settings.receipt_bridge_key,
                        "Content-Type": "application/json",
                    },
                    content=job["payload"].encode(),
                )
                if response.status_code != 200:
                    # Only local controlled messages; never persist an HTML proxy page or request data.
                    messages = {
                        403: "Проверьте ключ подключения",
                        409: "Связанная приёмка уже запущена, изменена, удалена или в архиве; требуется разбор в gis-trueapi",
                        422: "Проверьте ИНН организации, PN и состав КМ",
                        413: "Состав превышает допустимый размер",
                    }
                    raise ValueError(
                        messages.get(
                            response.status_code, "gis-trueapi временно недоступен"
                        )
                        + f" (HTTP {response.status_code})"
                    )
                result = response.json()
                if (
                    result.get("revision") != job["revision"]
                    or not isinstance(result.get("document_id"), str)
                    or not result["document_id"]
                    or result.get("status") not in ("created", "updated", "unchanged")
                ):
                    raise ValueError(
                        "gis-trueapi не подтвердил сохранение нужной версии"
                    )
                warehouse_id = result["document_id"]
            except httpx.HTTPError:
                error = "Нет связи с gis-trueapi. Передача повторится автоматически"
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                error = (
                    str(exc)
                    if isinstance(exc, ValueError)
                    and not isinstance(exc, json.JSONDecodeError)
                    else "Некорректный ответ gis-trueapi"
                )
            with store.transaction() as db:
                db.execute(
                    """UPDATE receipt_deliveries SET state=?,warehouse_id=?,attempts=attempts+1,
                    next_attempt=?,error=?,updated=? WHERE doc_id=? AND revision=?""",
                    (
                        "error" if error else "delivered",
                        warehouse_id,
                        time.time() + min(1800, 60 * 2 ** min(job["attempts"], 5)),
                        error,
                        now(),
                        job["doc_id"],
                        job["revision"],
                    ),
                )
