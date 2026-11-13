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
    next_attempt REAL NOT NULL DEFAULT 0, error TEXT, updated TEXT NOT NULL,
    historical INTEGER NOT NULL DEFAULT 0)"""


def init_schema(db):
    db.execute(SCHEMA)
    if "historical" not in {
        r[1] for r in db.execute("PRAGMA table_info(receipt_deliveries)")
    }:
        db.execute(
            "ALTER TABLE receipt_deliveries ADD COLUMN historical INTEGER NOT NULL DEFAULT 0"
        )


def backfill(service, limit=1):
    """Discover old confirmed compositions without rebuilding exports or CRPT writes."""
    if not service.settings.receipt_bridge_url:
        return
    config = service.config()
    with service.store.transaction() as db:
        candidates = list(
            db.execute(
                "SELECT d.* FROM documents d WHERE "
                "NOT EXISTS (SELECT 1 FROM receipt_deliveries r WHERE r.doc_id=d.id) "
                "AND NOT EXISTS (SELECT 1 FROM document_stops h WHERE h.doc_id=d.id AND h.resumed_at IS NULL) "
                "AND NOT EXISTS (SELECT 1 FROM introduction_documents i WHERE i.doc_id=d.id) "
                "AND EXISTS (SELECT 1 FROM codes c WHERE c.doc_id=d.id) "
                "AND NOT EXISTS (SELECT 1 FROM codes c LEFT JOIN atks a ON a.id=c.atk_id WHERE c.doc_id=d.id AND "
                "(c.external_status!='APPLIED' OR c.local_state!='aggregated' OR a.id IS NULL OR a.state!='done' OR a.code IS NULL)) "
                "AND NOT EXISTS (SELECT 1 FROM codes c JOIN atks a ON a.id=c.atk_id "
                "JOIN atks sibling ON sibling.batch_id=a.batch_id AND sibling.supplier_id=a.supplier_id "
                "WHERE c.doc_id=d.id AND sibling.state!='done') "
                "AND NOT EXISTS (SELECT 1 FROM codes c JOIN introduction_atks i ON i.atk_id=c.atk_id WHERE c.doc_id=d.id) "
                "AND NOT EXISTS (SELECT 1 FROM codes c JOIN transformations t ON t.atk_id=c.atk_id "
                "WHERE c.doc_id=d.id AND t.state IN ('approval','approved','processing')) "
                "AND NOT EXISTS (SELECT 1 FROM codes c JOIN atks a ON a.id=c.atk_id "
                "JOIN group_stops h ON h.group_id=a.batch_id||':'||a.supplier_id JOIN document_stops s ON s.doc_id=h.doc_id "
                "WHERE c.doc_id=d.id AND s.resumed_at IS NULL) ORDER BY d.created,d.id LIMIT ?",
                (limit,),
            )
        )
        for document in candidates:
            codes = [
                dict(c)
                for c in db.execute(
                    "SELECT * FROM codes WHERE doc_id=? ORDER BY cis", (document["id"],)
                )
            ]
            enqueue(db, config, [dict(document)], codes, historical=True)


def historical_status(service, job):
    """Recheck both local introduction races and actual registry state before HTTP delivery."""
    payload = json.loads(job["payload"])
    with service.store.read() as db:
        if db.execute(
            "SELECT 1 FROM introduction_documents WHERE doc_id=?", (job["doc_id"],)
        ).fetchone():
            return "Ввод в оборот уже начат; историческая загрузка пропущена"
        codes = [
            dict(c)
            for c in db.execute("SELECT * FROM codes WHERE doc_id=?", (job["doc_id"],))
        ]
        if any(c["external_status"] == "INTRODUCED" for c in codes):
            return "КМ уже введены в оборот; историческая загрузка пропущена"
        if any(
            c["external_status"] != "APPLIED" or c["local_state"] != "aggregated"
            for c in codes
        ):
            raise ValueError("Состав старой накладной изменился; требуется проверка")
    from .codes import parse

    expected = {parse(c["raw"]).cis: c for c in payload["codes"]}
    if set(expected) != {c["cis"] for c in codes}:
        raise ValueError("Состав старой накладной изменился; требуется проверка")
    atks = {c["atk"] for c in payload["codes"]}
    records = service.gateway.info(sorted(set(expected) | atks))
    if any(records[cis].get("status") == "INTRODUCED" for cis in set(expected) | atks):
        return "ЧЗ подтвердил ввод в оборот; историческая загрузка пропущена"
    if any(
        records[cis].get("status") != "APPLIED"
        or records[cis].get("ownerInn") != payload["org_inn"]
        or records[cis].get("parent") != item["atk"]
        for cis, item in expected.items()
    ):
        raise ValueError("ЧЗ не подтвердил актуальный состав старой накладной")
    if any(
        records[atk].get("status") != "APPLIED"
        or records[atk].get("ownerInn") != payload["org_inn"]
        for atk in atks
    ):
        raise ValueError("ЧЗ не подтвердил статус старого АТК")
    return None


def enqueue(db, config, documents, codes, historical=False):
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
            (doc_id,revision,fingerprint,payload,state,updated,historical) VALUES (?,?,?,?,'pending',?,?)
            ON CONFLICT(doc_id) DO UPDATE SET revision=excluded.revision,
            fingerprint=excluded.fingerprint,payload=excluded.payload,state='pending',
            attempts=0,next_attempt=0,error=NULL,updated=excluded.updated,historical=excluded.historical""",
            (
                document["id"],
                body["revision"],
                fingerprint,
                dumps(body),
                now(),
                int(historical),
            ),
        )


def enqueue_revoke(db, doc_id):
    job = db.execute("SELECT * FROM receipt_deliveries WHERE doc_id=?", (doc_id,)).fetchone()
    if not job:
        return
    previous = json.loads(job["payload"])
    body = {"source_instance": previous["source_instance"], "source_document": doc_id,
            "revision": job["revision"] + 1, "reason": "Расформирование АТК администратором",
            "action": "revoke", "receipt": previous.get("receipt", previous)}
    db.execute("UPDATE receipt_deliveries SET revision=?,payload=?,state='pending',historical=0,attempts=0,next_attempt=0,error=NULL,updated=? WHERE doc_id=?",
               (body["revision"], dumps(body), now(), doc_id))


def deliver(service, transport=None, limit=10):
    settings, store = service.settings, service.store
    if not settings.receipt_bridge_url:
        return
    jobs = store.all(
        "SELECT r.* FROM receipt_deliveries r WHERE r.state NOT IN ('delivered','matched','skipped','revoked') AND r.next_attempt<=? "
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
            revoking = json.loads(job["payload"]).get("action") == "revoke"
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
            delivery_state = "delivered"
            try:
                if job["historical"]:
                    from .gateway import RemoteError

                    try:
                        reason = historical_status(service, job)
                    except (RemoteError, ValueError):
                        raise ValueError(
                            "Не удалось подтвердить в ЧЗ статус и состав старого АТК. Проверка повторится"
                        ) from None
                    if reason:
                        with store.transaction() as db:
                            db.execute(
                                "UPDATE receipt_deliveries SET state='skipped',error=?,updated=? WHERE doc_id=? AND revision=?",
                                (reason, now(), job["doc_id"], job["revision"]),
                            )
                        continue
                response = client.post(
                    settings.receipt_bridge_url
                    + ("/api/integrations/marking-bm/receipts/revoke" if revoking else "/api/integrations/marking-bm/receipts"),
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
                        409: "Конфликт идентификатора или версии приёмки; проверьте обновление gis-trueapi и журнал интеграции",
                        422: "Проверьте ИНН организации, PN и состав КМ",
                        413: "Состав превышает допустимый размер",
                    }
                    if response.status_code == 409:
                        try:
                            if response.json().get("code") == "receipt_match_conflict":
                                messages[409] = (
                                    "Найдена существующая приёмка с несовпадающим составом или несколько кандидатов. Дубль не создан; нужен разбор"
                                )
                        except (ValueError, AttributeError):
                            pass
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
                    or result.get("status")
                    not in (("revoked",) if revoking else ("created", "updated", "unchanged", "matched"))
                ):
                    raise ValueError(
                        "gis-trueapi не подтвердил сохранение нужной версии"
                    )
                warehouse_id = result["document_id"]
                if revoking:
                    delivery_state = "revoked"
                if result["status"] == "matched":
                    delivery_state = "matched"
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
                        "error" if error else delivery_state,
                        warehouse_id,
                        time.time() + min(1800, 60 * 2 ** min(job["attempts"], 5)),
                        error,
                        now(),
                        job["doc_id"],
                        job["revision"],
                    ),
                )

            if not error and revoking:
                with store.transaction() as db:
                    group = db.execute("SELECT group_id FROM cargo_reworks WHERE doc_id=?", (job["doc_id"],)).fetchone()
                    if group:
                        a = db.execute("SELECT * FROM atks WHERE batch_id||':'||supplier_id=? LIMIT 1", (group["group_id"],)).fetchone()
                        if a:
                            service.finish_cargo_breakup(db, dict(a))
