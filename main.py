from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import quote
import base64
from hashlib import sha256
import hmac
import json
import re
import secrets
import shutil
import threading
import time
from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form, Depends
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict
from marking_bm.config import Settings
from marking_bm.db import Store, now, uid, dumps, audit
from marking_bm.auth import verify_password, digest, new_session
from marking_bm.excel import import_file, template
from marking_bm.legacy import parse_legacy_pack
from marking_bm.service import safe_name
from marking_bm.gateway import Gateway, RemoteError
from marking_bm.service import Service, Conflict, one, rows
from marking_bm.worker import Worker

ROOT = Path(__file__).parent


def create_app(settings=None, gateway_factory=Gateway, run_worker=True):
    settings = settings or Settings()
    store = Store(settings.data_dir / "marking-bm.sqlite3")
    service = Service(store, None, settings)
    service.gateway = gateway_factory(settings, service.config)
    worker = Worker(service)

    @asynccontextmanager
    async def lifespan(app):
        thread = None
        if run_worker:
            thread = threading.Thread(target=worker.run, daemon=True)
            thread.start()
        yield
        worker.stop.set()
        if thread:
            thread.join(timeout=45)

    app = FastAPI(title="Маркировка · Бренд-менеджер", lifespan=lifespan)
    app.state.service, app.state.worker = service, worker
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.middleware("http")
    async def headers(req, call_next):
        if req.method not in ("GET", "HEAD", "OPTIONS"):
            if req.headers.get("origin") and req.headers["origin"] != settings.base_url:
                return JSONResponse({"detail": "Недопустимый источник запроса"}, 403)
            if (
                not req.url.path.startswith("/api/signer/")
                and req.headers.get("X-Requested-With") != "marking-bm"
            ):
                return JSONResponse({"detail": "Отсутствует защита запроса"}, 403)
        if int(req.headers.get("content-length", "0") or 0) > 11 * 1024 * 1024:
            return JSONResponse({"detail": "Размер запроса превышен"}, 413)
        response = await call_next(req)
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "same-origin",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'",
                "Cache-Control": "no-store",
            }
        )
        return response

    @app.exception_handler(Conflict)
    async def conflict(req, e):
        return JSONResponse({"detail": str(e), "preview": e.detail}, 409)

    @app.exception_handler(PermissionError)
    async def permission(req, e):
        return JSONResponse({"detail": str(e)}, 403)

    @app.exception_handler(ValueError)
    async def value_error(req, e):
        return JSONResponse({"detail": str(e)}, 422)

    @app.exception_handler(RemoteError)
    async def remote_error(req, e):
        return JSONResponse({"detail": str(e)}, 503)

    def session(req: Request):
        token = req.cookies.get("bm_session", "")
        s = store.one(
            "SELECT * FROM sessions WHERE token_hash=? AND expires>?",
            (digest(token), time.time()),
        )
        if not s:
            raise HTTPException(401, "Войдите в приложение")
        user = store.one("SELECT * FROM users WHERE username=?", (s["username"],))
        if not s["admin"] and (not user or not user["enabled"]):
            raise HTTPException(401, "Пользователь отключён")
        if req.method not in ("GET", "HEAD") and not hmac.compare_digest(
            req.headers.get("X-CSRF-Token", ""), s["csrf"]
        ):
            raise HTTPException(403, "Обновите страницу и повторите действие")
        s["role"] = "admin" if s["admin"] else user["role"]
        return s

    def manager(s=Depends(session)):
        if not s["admin"] and s["role"] != "manager":
            raise HTTPException(403, "Раздел доступен бренд-менеджерам")
        return s

    def logistics(s=Depends(session)):
        if not s["admin"] and s["role"] != "logistics":
            raise HTTPException(403, "Раздел доступен логистам")
        return s

    def admin(s=Depends(session)):
        if not s["admin"]:
            raise HTTPException(403, "Нужен отдельный вход администратора")
        return s

    def signer(req: Request):
        supplied = req.headers.get("Authorization", "").removeprefix("Bearer ")
        if not settings.signer_token or not hmac.compare_digest(
            supplied, settings.signer_token
        ):
            raise HTTPException(403, "Подписант не авторизован")
        if req.url.scheme != "https" and not req.client.host in (
            "testclient",
            "127.0.0.1",
            "::1",
        ):
            raise HTTPException(403, "Требуется HTTPS")

    def download(content, name, kind):
        return Response(
            content,
            media_type=kind,
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''" + quote(name)
            },
        )

    @app.get("/")
    def index():
        return FileResponse(ROOT / "static/index.html")

    @app.get("/admin")
    def admin_page():
        return FileResponse(ROOT / "static/index.html")

    @app.get("/health")
    def health():
        return {"ok": True}

    class Login(BaseModel):
        username: str = Field(default="", max_length=80)
        password: str = Field(default="", max_length=1024)
        admin: bool = False

    @app.post("/api/session")
    def login(body: Login, req: Request):
        ip = req.client.host
        with store.transaction() as db:
            if body.admin:
                rate = one(db, "SELECT * FROM login_attempts WHERE ip=?", (ip,))
                if rate and rate["failures"] >= 5 and rate["until"] > time.time():
                    raise HTTPException(
                        429, "Подождите 15 минут перед повторным входом"
                    )
                if not verify_password(body.password, settings.admin_password_hash):
                    count = (
                        rate["failures"] if rate and rate["until"] > time.time() else 0
                    ) + 1
                    db.execute(
                        "INSERT OR REPLACE INTO login_attempts VALUES(?,?,?)",
                        (ip, count, time.time() + 900),
                    )
                    # Return rather than raise so the failed attempt is committed.
                    return JSONResponse(
                        {"detail": "Неверный пароль администратора"}, 403
                    )
                db.execute("DELETE FROM login_attempts WHERE ip=?", (ip,))
                username = body.username or "administrator"
                db.execute(
                    "INSERT OR IGNORE INTO users(username) VALUES(?)", (username,)
                )
            else:
                username = body.username
                u = one(
                    db,
                    "SELECT * FROM users WHERE username=? AND enabled=1",
                    (username,),
                )
                if not u:
                    raise HTTPException(403, "Пользователь не найден или отключён")
            old = req.cookies.get("bm_session")
            if old:
                db.execute("DELETE FROM sessions WHERE token_hash=?", (digest(old),))
            token, csrf = new_session(db, username, body.admin)
            audit(
                db,
                username,
                "Вход администратора" if body.admin else "Вход по username",
                username,
            )
        role = "admin" if body.admin else u["role"]
        r = JSONResponse(
            {"username": username, "admin": body.admin, "role": role, "csrf": csrf}
        )
        r.set_cookie(
            "bm_session",
            token,
            max_age=28800,
            httponly=True,
            secure=settings.secure_cookie,
            samesite="strict",
        )
        return r

    @app.get("/api/session")
    def who(s=Depends(session)):
        return {k: s[k] for k in ("username", "admin", "role", "csrf")}

    @app.delete("/api/session")
    def logout(s=Depends(session)):
        with store.transaction() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (s["token_hash"],))
        r = JSONResponse({"ok": True})
        r.delete_cookie("bm_session")
        return r

    @app.get("/api/suppliers")
    def suppliers(s=Depends(session)):
        return store.all(
            "SELECT id,name FROM suppliers WHERE merged_into IS NULL ORDER BY name"
        )

    @app.get("/api/template")
    def excel_template(s=Depends(manager)):
        return download(
            template(),
            "Шаблон.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.get("/api/documents")
    def documents(
        q: str = "",
        status: str = "",
        supplier: str = "",
        date_from: str = "",
        date_to: str = "",
        archive: bool = False,
        s=Depends(manager),
    ):
        sql = (
            "SELECT d.*,EXISTS(SELECT 1 FROM document_stops h WHERE h.doc_id=d.id "
            "AND h.resumed_at IS NULL) stopped,p.name supplier,"
            "(SELECT SUM(quantity*labels) FROM lines WHERE doc_id=d.id) units,"
            "CASE WHEN d.status='reporting' AND NOT EXISTS("
            "SELECT 1 FROM document_stops h WHERE h.doc_id=d.id AND h.resumed_at IS NULL"
            ") AND (SELECT o.state FROM operations o WHERE o.doc_id=d.id "
            "AND o.kind='report' ORDER BY o.rowid DESC LIMIT 1) IN ('rejected','partial') "
            "THEN 'report_error' ELSE d.status END display_status "
            "FROM documents d JOIN suppliers p ON p.id=d.supplier_id WHERE 1=1"
        )
        args = []
        if not s["admin"]:
            sql += " AND owner=?"
            args.append(s["username"])
        sql += " AND status" + ("=" if archive else "!=") + "'archive'"
        for condition, value in [
            ("d.number LIKE ?", ("%" + q + "%") if q else ""),
            ("d.status=?", status),
            ("d.supplier_id=?", supplier),
            ("d.created>=?", date_from),
            ("substr(d.created,1,10)<=?", date_to),
        ]:
            if value:
                sql += " AND " + condition
                args.append(value)
        return store.all(sql + " ORDER BY d.created DESC", args)

    class Create(BaseModel):
        number: str
        supplier: str
        parent_id: str | None = None

    @app.post("/api/documents")
    def create(body: Create, s=Depends(manager)):
        return {
            "id": service.create(
                s["username"], body.number, body.supplier, body.parent_id, s["admin"]
            )
        }

    @app.get("/api/documents/{doc_id}")
    def get(doc_id: str, s=Depends(manager)):
        return service.get(doc_id, s["username"], s["admin"])

    class Lines(BaseModel):
        rows: list
        revision: int = 0
        confirmation: str | None = None

    @app.post("/api/documents/{doc_id}/preview")
    def preview(doc_id: str, body: Lines, s=Depends(manager)):
        return service.preview(doc_id, s["username"], body.rows, s["admin"])

    @app.post("/api/documents/{doc_id}/lines")
    def save(doc_id: str, body: Lines, s=Depends(manager)):
        return service.save_lines(
            doc_id,
            s["username"],
            body.rows,
            body.revision,
            body.confirmation,
            s["admin"],
        )

    @app.post("/api/documents/{doc_id}/import")
    async def excel_import(
        doc_id: str,
        file: UploadFile = File(...),
        mapping: str | None = Form(None),
        sheet: str | None = Form(None),
        header_row: int | None = Form(None),
        s=Depends(manager),
    ):
        doc = service.get(doc_id, s["username"], s["admin"])
        if doc["status"] not in ("draft", "ready", "transferred", "label_error"):
            raise Conflict("Импорт на этой стадии запрещён")
        content = await file.read(10 * 1024 * 1024 + 1)
        try:
            selected_mapping = json.loads(mapping) if mapping is not None else None
        except (ValueError, TypeError) as exc:
            raise ValueError("Некорректное сопоставление столбцов") from exc
        result = import_file(
            content, mapping=selected_mapping, sheet_name=sheet, header_row=header_row
        )
        if not result["requires_mapping"] and not result["errors"]:
            imported_warnings = result["warnings"]
            result.update(
                service.preview(
                    doc_id,
                    s["username"],
                    [
                        [l["pn"], l["gtin"], l["quantity"], l["labels"], l["tnved"]]
                        for l in result["lines"]
                    ],
                    s["admin"],
                )
            )
            result["warnings"] = list(
                dict.fromkeys(imported_warnings + result["warnings"])
            )
        with store.transaction() as db:
            current = service.document(db, doc_id, s["username"], s["admin"])
            if service.document_stopped(db, doc_id) or current["status"] not in (
                "draft",
                "ready",
                "transferred",
                "label_error",
            ):
                raise Conflict("Импорт остановлен: состояние накладной изменилось")
            # Re-uploading the same file while choosing a mapping keeps one source version.
            source = one(
                db,
                "SELECT id FROM artifacts WHERE doc_id=? AND kind='source' AND sha256=?",
                (doc_id, sha256(content).hexdigest()),
            )
            if not source:
                version = 1 + max(
                    [
                        a["version"]
                        for a in rows(
                            db,
                            "SELECT version FROM artifacts WHERE doc_id=? AND kind='source'",
                            (doc_id,),
                        )
                    ]
                    or [0]
                )
                source = {
                    "id": service.artifact(
                        db,
                        "Исходный-Excel.xlsx",
                        content,
                        "source",
                        doc_id=doc_id,
                        version=version,
                        snapshot={
                            "errors": result["errors"],
                            "requires_mapping": result["requires_mapping"],
                            "sheet": result.get("sheet"),
                            "header_row": result.get("header_row"),
                            "mapping": result.get("mapping"),
                        },
                    )
                }
            result["source_id"] = source["id"]
            if selected_mapping is not None:
                audit(
                    db,
                    s["username"],
                    "Сопоставлены столбцы Excel",
                    doc_id,
                    {
                        "source_id": source["id"],
                        "sheet": result.get("sheet"),
                        "header_row": result.get("header_row"),
                        "mapping": result.get("mapping"),
                    },
                )
        return result

    @app.post("/api/documents/{doc_id}/order")
    def order(doc_id: str, s=Depends(manager)):
        return service.order(doc_id, s["username"], s["admin"])

    class Transfer(BaseModel):
        version: int

    @app.post("/api/documents/{doc_id}/transferred")
    def transferred(doc_id: str, body: Transfer, s=Depends(manager)):
        service.transferred(doc_id, s["username"], body.version, s["admin"])
        return {"ok": True}

    class Batch(BaseModel):
        documents: list[str]
        production_date: str

    @app.post("/api/batches")
    def batch(body: Batch, s=Depends(manager)):
        return {
            "id": service.batch(
                s["username"],
                body.documents,
                body.production_date,
                admin=s["admin"],
            )
        }

    class StopDocument(BaseModel):
        number: str = Field(min_length=1, max_length=120)

    @app.post("/api/documents/{doc_id}/stop")
    def stop_document(doc_id: str, body: StopDocument, s=Depends(manager)):
        return service.stop_document(doc_id, body.number, s["username"], s["admin"])

    @app.post("/api/admin/legacy/preview")
    async def legacy_preview(file: UploadFile = File(...), s=Depends(admin)):
        content = await file.read(10 * 1024 * 1024 + 1)
        parsed = parse_legacy_pack(content, file.filename)
        conflict = store.one(
            "SELECT id FROM documents WHERE number=? COLLATE NOCASE", (parsed["number"],)
        )
        return {
            "number": parsed["number"], "codes": parsed["codes_count"],
            "units": parsed["units"], "lines": parsed["lines"],
            "duplicate_invoice": bool(conflict),
        }

    @app.post("/api/admin/legacy/import")
    async def legacy_import(
        file: UploadFile = File(...),
        supplier: str = Form(...),
        owner: str = Form(...),
        s=Depends(admin),
    ):
        content = await file.read(10 * 1024 * 1024 + 1)
        parsed = parse_legacy_pack(content, file.filename)
        return service.import_legacy_pack(
            s["username"], owner.strip(), supplier.strip(),
            safe_name(file.filename, 120), content, parsed,
        )

    @app.post("/api/admin/legacy/{doc_id}/pdf")
    async def legacy_pdf(
        doc_id: str, file: UploadFile = File(...), s=Depends(admin),
    ):
        content = await file.read(20 * 1024 * 1024 + 1)
        if not file.filename or not file.filename.lower().endswith(".pdf"):
            raise ValueError("Выберите PDF")
        return service.add_legacy_pdf(
            s["username"], doc_id, safe_name(file.filename), content
        )

    @app.get("/api/admin/documents")
    def admin_documents(q: str = "", s=Depends(admin)):
        term = q.strip()[:120]
        sql = (
            "SELECT d.id,d.number,d.owner,d.status,d.updated,p.name supplier "
            "FROM documents d JOIN suppliers p ON p.id=d.supplier_id"
        )
        args = []
        if term:
            sql += " WHERE d.number LIKE ? OR d.owner LIKE ? OR p.name LIKE ?"
            args = ["%" + term + "%"] * 3
        return store.all(sql + " ORDER BY d.updated DESC,d.id DESC LIMIT 100", args)

    class ReassignDocument(BaseModel):
        owner: str = Field(min_length=1, max_length=80)

    @app.put("/api/admin/documents/{doc_id}/owner")
    def admin_reassign_document(doc_id: str, body: ReassignDocument, s=Depends(admin)):
        return service.reassign_document(doc_id, body.owner, s["username"])

    class RestoreDocument(BaseModel):
        reason: str = Field(min_length=1, max_length=1000)

    @app.post("/api/admin/documents/{doc_id}/restore")
    def restore_document(doc_id: str, body: RestoreDocument, s=Depends(admin)):
        return service.restore_document(doc_id, s["username"], body.reason)

    @app.get("/api/files/{file_id}")
    def file_get(file_id: str, s=Depends(session)):
        a = store.one("SELECT * FROM artifacts WHERE id=?", (file_id,))
        if not a:
            raise HTTPException(404, "Файл не найден")
        with store.transaction() as db:
            if s["role"] == "logistics":
                if (
                    a["doc_id"]
                    or a["kind"] not in ("atk", "result")
                    or not a["group_id"]
                ):
                    raise PermissionError("Логисту доступны только файлы АТК и RESULT")
                batch_id, supplier_id = a["group_id"].split(":")
                if not one(
                    db,
                    "SELECT id FROM atks WHERE batch_id=? AND supplier_id=?",
                    (batch_id, supplier_id),
                ):
                    raise PermissionError("Файл не относится к АТК")
            elif a["doc_id"]:
                service.document(db, a["doc_id"], s["username"], s["admin"])
            elif not s["admin"]:
                batch_id, supplier = a["group_id"].split(":")
                docs = rows(
                    db,
                    "SELECT d.owner FROM documents d JOIN batch_docs b ON b.doc_id=d.id WHERE b.batch_id=? AND b.supplier_id=?",
                    (batch_id, supplier),
                )
                # Admin mixed-owner batches must not disclose other owners' data through group exports.
                ids = json.loads(a["snapshot"]).get("documents", [])
                extra = [
                    one(db, "SELECT owner FROM documents WHERE id=?", (i,)) for i in ids
                ]
                if (
                    not docs
                    or any(d["owner"] != s["username"] for d in docs)
                    or any(not d or d["owner"] != s["username"] for d in extra)
                ):
                    raise PermissionError("Файл доступен администратору группы")
        return download(
            a["content"],
            a["name"],
            "application/zip"
            if a["kind"] == "labels"
            else "application/pdf"
            if a["kind"] == "legacy_pdf"
            else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.get("/api/atks/{atk_id}/candidates")
    def candidates(atk_id: str, s=Depends(manager)):
        return service.candidate_codes(atk_id, s["username"], s["admin"])

    @app.get("/api/atks/{atk_id}")
    def atk_detail(atk_id: str, s=Depends(manager)):
        with store.transaction() as db:
            a = service.atk_access(db, atk_id, s["username"], s["admin"])
            a["stopped"] = bool(
                service.group_stopped(db, a["batch_id"] + ":" + a["supplier_id"])
            )
            a["codes"] = rows(
                db,
                "SELECT id,cis,pn,quantity,tnved FROM codes WHERE atk_id=?",
                (atk_id,),
            )
            a["transformations"] = rows(
                db,
                "SELECT * FROM transformations WHERE atk_id=? ORDER BY created DESC",
                (atk_id,),
            )
            return a

    class Transform(BaseModel):
        kind: str
        codes: list[str]

    @app.post("/api/atks/{atk_id}/transform")
    def transform(atk_id: str, body: Transform, s=Depends(manager)):
        return {
            "id": service.transform(
                atk_id, body.kind, body.codes, s["username"], s["admin"]
            )
        }

    @app.get("/api/logistics/atks")
    def logistics_atks(
        q: str = "",
        supplier: str = "",
        status: str = "",
        tnved: str = "",
        date_from: str = "",
        date_to: str = "",
        archive: bool = False,
        s=Depends(logistics),
    ):
        return service.logistics_atks(
            q=q,
            supplier=supplier,
            status=status,
            tnved=tnved,
            date_from=date_from,
            date_to=date_to,
            archive=archive,
        )

    @app.get("/api/logistics/atks/{atk_id}")
    def logistics_atk(atk_id: str, s=Depends(logistics)):
        return service.logistics_atk(atk_id)

    @app.post("/api/logistics/documents/{doc_id}/complete")
    def logistics_complete(doc_id: str, s=Depends(logistics)):
        return service.logistics_complete(doc_id, s["username"])

    class LogisticsDownload(BaseModel):
        atks: list[str] = Field(min_length=1, max_length=200)

    @app.post("/api/logistics/download")
    def logistics_download(body: LogisticsDownload, s=Depends(logistics)):
        # This POST only reads files and creates an in-memory archive. No GIS mutation.
        data = service.logistics_download(body.atks)
        with store.transaction() as db:
            audit(
                db,
                s["username"],
                "Скачан комплект АТК",
                "logistics",
                {"atks": sorted(set(body.atks))},
            )
        return download(data, "АТК-и-RESULT.zip", "application/zip")

    @app.get("/api/admin/overview")
    def overview(s=Depends(admin)):
        usage = shutil.disk_usage(settings.data_dir)
        secrets_config = settings.secrets()
        operations = store.all(
            "SELECT o.id,o.kind,o.doc_id,o.group_id,o.state,o.external_id,o.error,"
            "o.created,o.updated, a.resolved_at AS attention_resolved_at "
            "FROM operations o LEFT JOIN operation_attention a ON a.op_id=o.id "
            "ORDER BY o.created DESC LIMIT 200"
        )
        transformations = store.all(
            "SELECT * FROM transformations ORDER BY created DESC"
        )
        with store.read() as db:
            for operation in operations:
                operation["stopped"] = service.operation_stopped(db, operation)
            for request in transformations:
                a = one(db, "SELECT * FROM atks WHERE id=?", (request["atk_id"],))
                request["stopped"] = bool(
                    service.group_stopped(db, a["batch_id"] + ":" + a["supplier_id"])
                    or one(
                        db,
                        "SELECT h.doc_id FROM document_stops h JOIN codes c ON c.doc_id=h.doc_id JOIN json_each(?) j ON j.value=c.id WHERE h.resumed_at IS NULL LIMIT 1",
                        (request["code_ids"],),
                    )
                )
        return {
            "users": store.all("SELECT * FROM users ORDER BY username"),
            "suppliers": store.all("SELECT * FROM suppliers ORDER BY name"),
            "config": service.config(),
            "writes_enabled": settings.send_enabled,
            "signer_configured": bool(
                settings.signer_token and settings.signer_thumbprint
            ),
            "tokens": {
                k: {"configured": True, "expires": v.get("expires")}
                for k, v in secrets_config.items()
            },
            "disk": {"used": usage.used, "free": usage.free, "total": usage.total},
            "operations": operations,
            "transformations": transformations,
            "pool": store.all(
                "SELECT gtin,external_status,local_state,COUNT(*) count FROM codes GROUP BY gtin,external_status,local_state"
            ),
            "removed": store.all(
                "SELECT id,cis,pn,quantity FROM codes WHERE local_state='removed_review' AND reuse_allowed=0"
            ),
            "audit": store.all("SELECT * FROM audit ORDER BY id DESC LIMIT 200"),
            "heartbeat": store.one(
                "SELECT value FROM settings WHERE key='signer_heartbeat'"
            ),
            "last_backup": store.one(
                "SELECT value FROM settings WHERE key='last_backup'"
            ),
        }

    class UserBody(BaseModel):
        username: str
        enabled: bool = True
        role: Literal["manager", "logistics"] | None = None

    @app.post("/api/admin/users")
    def users(body: UserBody, s=Depends(admin)):
        if not re.fullmatch(r"[A-Za-zА-Яа-яЁё0-9_.@-]{1,80}", body.username):
            raise ValueError("Username: буквы, цифры и _.@-")
        with store.transaction() as db:
            previous = one(db, "SELECT * FROM users WHERE username=?", (body.username,))
            role = body.role or (previous["role"] if previous else "manager")
            db.execute(
                "INSERT INTO users(username,enabled,role) VALUES(?,?,?) ON CONFLICT(username) DO UPDATE SET enabled=excluded.enabled,role=excluded.role",
                (body.username, int(body.enabled), role),
            )
            if not body.enabled or (previous and previous["role"] != role):
                db.execute(
                    "DELETE FROM sessions WHERE username=? AND admin=0",
                    (body.username,),
                )
            audit(
                db,
                s["username"],
                "Изменён пользователь",
                body.username,
                {
                    "enabled": body.enabled,
                    "role": role,
                    "previous_role": previous["role"] if previous else None,
                },
            )
        return {"ok": True}

    class ConfigBody(BaseModel):
        model_config = ConfigDict(extra="forbid")
        organization: str = Field(max_length=160)
        inn: str
        oms_id: str
        oms_connection: str
        template_id: int = 36
        kpp: str = ""
        fias_id: str = ""

    @app.put("/api/admin/config")
    def configure(body: ConfigBody, s=Depends(admin)):
        from uuid import UUID

        if not re.fullmatch(
            "[0-9]{10}|[0-9]{12}", body.inn
        ) or body.template_id not in (36, 37):
            raise ValueError("Проверьте ИНН и шаблон этикетки")
        config_values = body.model_dump()
        for field, label in (
            ("oms_id", "OMS ID"),
            ("oms_connection", "omsConnection"),
            ("fias_id", "ФИАС места деятельности"),
        ):
            raw = config_values[field].strip()
            if field == "fias_id" and not raw:
                config_values[field] = ""
                continue
            try:
                config_values[field] = str(UUID(raw))
            except (ValueError, TypeError, AttributeError):
                raise ValueError(
                    f"{label}: неверный UUID. Проверьте латинские буквы, "
                    "36 символов и отсутствие посторонних знаков"
                ) from None
        if body.kpp and not re.fullmatch("[0-9]{9}", body.kpp):
            raise ValueError("КПП: 9 цифр")
        with store.transaction() as db:
            old = service.config()
            # One organization for life of existing documents; no silent reassignment of ownership.
            if (
                old["inn"]
                and old["inn"] != body.inn
                and one(db, "SELECT id FROM documents LIMIT 1")
            ):
                raise Conflict("Нельзя менять организацию при существующих документах")
            pending = rows(
                db,
                "SELECT kind,state,external_id FROM operations "
                "WHERE state NOT IN ('done','rejected','cancelled')",
            )
            # Initial setup may follow a premature authorization request.
            # An auth challenge is not organization-bound before it is dispatched.
            # Never relax this guard for issued/uncertain requests, other
            # operation kinds, or later changes to an established organization.
            initial_auth_only = (
                not old["inn"]
                and all(
                    op["kind"] in ("auth_true", "auth_oms")
                    and op["state"] in ("signing", "leased", "queued", "blocked")
                    and not op["external_id"]
                    for op in pending
                )
            )
            if pending and not initial_auth_only and any(
                old[k] != config_values[k]
                for k in ("inn", "oms_id", "oms_connection")
            ):
                raise Conflict(
                    "Дождитесь завершения текущих операций перед сменой подключения"
                )
            for k, v in config_values.items():
                db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, dumps(v)))
            audit(
                db, s["username"], "Настройки организации", "config", config_values
            )
        return {"ok": True}

    class SupplierEdit(BaseModel):
        id: str
        name: str = Field(min_length=1, max_length=160)
        merge_into: str | None = None

    @app.post("/api/admin/suppliers")
    def supplier_edit(body: SupplierEdit, s=Depends(admin)):
        with store.transaction() as db:
            if not one(db, "SELECT id FROM suppliers WHERE id=?", (body.id,)):
                raise ValueError("Поставщик не найден")
            if body.merge_into:
                target = one(
                    db,
                    "SELECT * FROM suppliers WHERE id=? AND merged_into IS NULL",
                    (body.merge_into,),
                )
                if not target or body.merge_into == body.id:
                    raise ValueError("Неверный поставщик для объединения")
                db.execute(
                    "UPDATE documents SET supplier_id=? WHERE supplier_id=?",
                    (body.merge_into, body.id),
                )
                db.execute(
                    "UPDATE suppliers SET merged_into=? WHERE id=? OR merged_into=?",
                    (body.merge_into, body.id, body.id),
                )
            else:
                db.execute(
                    "UPDATE suppliers SET name=? WHERE id=?",
                    (body.name.strip(), body.id),
                )
            audit(db, s["username"], "Изменён поставщик", body.id, body.model_dump())
        return {"ok": True}

    class CatalogFix(BaseModel):
        gtin: str
        tnved: str
        reason: str = Field(min_length=1, max_length=1000)

    @app.post("/api/admin/catalog")
    def catalog(body: CatalogFix, s=Depends(admin)):
        if not re.fullmatch("[0-9]{10}", body.tnved):
            raise ValueError("ТН ВЭД: 10 цифр")
        with store.transaction() as db:
            old = one(db, "SELECT * FROM catalog WHERE gtin=?", (body.gtin,))
            if not old:
                raise ValueError("GTIN не найден")
            db.execute(
                "UPDATE catalog SET tnved=? WHERE gtin=?", (body.tnved, body.gtin)
            )
            audit(
                db,
                s["username"],
                "Исправлена привязка ТН ВЭД",
                body.gtin,
                {"before": old["tnved"], "after": body.tnved, "reason": body.reason},
            )
        return {"ok": True}

    class Decision(BaseModel):
        approve: bool
        reason: str = Field(min_length=1, max_length=1000)

    @app.post("/api/admin/transformations/{request_id}")
    def review(request_id: str, body: Decision, s=Depends(admin)):
        service.review(request_id, body.approve, body.reason, s["username"])
        return {"ok": True}

    @app.post("/api/admin/codes/{code_id}/allow-reuse")
    def allow_reuse(code_id: str, body: Decision, s=Depends(admin)):
        if not body.approve:
            raise ValueError("Требуется явное разрешение")
        with store.transaction() as db:
            c = one(
                db,
                "SELECT * FROM codes WHERE id=? AND local_state='removed_review' AND external_status='APPLIED' AND atk_id IS NULL",
                (code_id,),
            )
            if not c:
                raise Conflict("КМ не ожидает разрешения")
            db.execute("UPDATE codes SET reuse_allowed=1 WHERE id=?", (code_id,))
            audit(
                db,
                s["username"],
                "Разрешено повторное включение КМ",
                code_id,
                {"reason": body.reason},
            )
        return {"ok": True}

    @app.post("/api/admin/auth/{kind}")
    def auth_start(kind: str, s=Depends(admin)):
        if kind not in ("true", "oms"):
            raise ValueError("Неверный тип доступа")
        challenge = service.gateway.challenge()
        with store.transaction() as db:
            if one(
                db,
                "SELECT id FROM operations WHERE kind=? AND state IN ('signing','leased','queued','dispatching')",
                ("auth_" + kind,),
            ):
                raise Conflict("Авторизация уже ожидает подписи")
            op = service.queue(
                db, "auth_" + kind, "auth:" + kind + ":" + uid(), challenge
            )
            audit(db, s["username"], "Запрошена авторизация ЧЗ", op)
        return {"id": op}

    @app.post("/api/documents/{doc_id}/report/{op_id}/retry")
    def employee_retry_report(doc_id: str, op_id: str, s=Depends(manager)):
        with store.read() as db:
            service.document(db, doc_id, s["username"], s["admin"])
            op = one(db, "SELECT doc_id FROM operations WHERE id=?", (op_id,))
            if not op or op["doc_id"] != doc_id:
                raise Conflict("Отчёт не относится к этой накладной")
        return service.retry_rejected_report(op_id, s["username"])

    @app.post("/api/admin/operations/{op_id}/retry-local")
    def retry_local_failure(op_id: str, s=Depends(admin)):
        # Only known LOCAL failures are safe to requeue without reconciliation.
        # In particular, unknown outcomes and HTTP failures are never replayed.
        with store.transaction() as db:
            op = one(db, "SELECT * FROM operations WHERE id=?", (op_id,))
            if not op or op["state"] != "blocked" or op["external_id"]:
                raise Conflict("Неизвестный либо отправленный результат нельзя повторять без сверки")
            service.require_operation_active(db, op)
            reason = op["error"] or ""
            if not reason.startswith(("Обновите доступ к ГИС МТ",
                                      "Обновите доступ к СУЗ",
                                      "Отправка операций отключена администратором сервера")):
                raise Conflict("Причина отказа не подтверждает отсутствие отправки. Проверьте ЛК ЧЗ")
            new_state = "queued" if op["signature"] or op["kind"] == "fetch" else "signing"
            db.execute("UPDATE operations SET state=?,error=NULL,updated=? WHERE id=?",
                       (new_state, now(), op_id))
            audit(db, s["username"], "Повторно запущена локально заблокированная операция",
                  op_id, {"previous_error": reason, "new_state": new_state})
        return {"ok": True, "state": new_state}

    @app.post("/api/admin/operations/{op_id}/retry")
    def admin_retry_report(op_id: str, s=Depends(admin)):
        return service.retry_rejected_report(op_id, s["username"], admin=True)

    class AttentionBody(BaseModel):
        resolved: bool
        reason: str = Field(default="", max_length=500)

    @app.post("/api/admin/operations/{op_id}/attention")
    def set_attention(op_id: str, body: AttentionBody, s=Depends(admin)):
        with store.transaction() as db:
            op = one(db, "SELECT id,kind,state FROM operations WHERE id=?", (op_id,))
            if not op:
                raise Conflict("Операция не найдена")
            if body.resolved:
                if op["state"] not in ("unknown", "blocked", "preflight_blocked", "rejected", "partial"):
                    raise Conflict("Эта операция не содержит активного предупреждения")
                db.execute(
                    "INSERT OR REPLACE INTO operation_attention(op_id,resolved_by,reason,resolved_at) "
                    "VALUES(?,?,?,?)",
                    (op_id, s["username"], body.reason.strip() or "Отмечено решённым вручную", now()),
                )
            else:
                db.execute("DELETE FROM operation_attention WHERE op_id=?", (op_id,))
            audit(db, s["username"],
                  "Предупреждение скрыто" if body.resolved else "Предупреждение возвращено",
                  op_id, {"reason": body.reason.strip(), "state": op["state"]})
        return {"ok": True, "resolved": body.resolved}

    @app.post("/api/admin/operations/{op_id}/check")
    def check(op_id: str, s=Depends(admin)):
        op = store.one("SELECT * FROM operations WHERE id=?", (op_id,))
        if op:
            with store.read() as db:
                service.require_operation_active(db, op)
        if op and op["kind"] == "report" and op["state"] in ("rejected", "partial"):
            raise Conflict("СУЗ обработала отчёт с ошибками. Используйте безопасную повторную отправку после сверки КМ")
        if op and op["state"] == "preflight_blocked":
            with store.transaction() as db:
                db.execute(
                    "UPDATE operations SET state='queued',error=NULL WHERE id=? AND state='preflight_blocked'",
                    (op_id,),
                )
                audit(db, s["username"], "Повторная проверка до первой отправки", op_id)
            return {"ok": True}
        if not op or not op["external_id"]:
            raise Conflict("Нет внешнего ID. Сначала сверьте операцию с ЛК ЧЗ")
        if op["state"] == "done":
            return {"ok": True}
        with store.transaction() as db:
            db.execute(
                "UPDATE operations SET state='submitted',error=NULL WHERE id=? AND state IN ('unknown','blocked')",
                (op_id,),
            )
            audit(db, s["username"], "Повторная проверка состояния", op_id)
        return {"ok": True}

    @app.get("/api/admin/operations/{op_id}/blocks")
    def lost_blocks(op_id: str, s=Depends(admin)):
        op = store.one("SELECT * FROM operations WHERE id=?", (op_id,))
        if not op or op["kind"] != "fetch" or op["state"] != "unknown":
            raise Conflict("Операция не ожидает восстановления блока")
        with store.read() as db:
            service.require_operation_active(db, op)
        baseline = json.loads(op["result"] or "{}").get("baseline")
        if baseline is None:
            raise Conflict("Нет снимка блоков до запроса. Нужна ручная сверка с ЧЗ")
        known = {x["blockId"] for x in baseline}
        return [
            b
            for b in service.gateway.blocks(json.loads(op["payload"]))
            if b["blockId"] not in known
        ]

    class BlockRecovery(BaseModel):
        block_id: str
        reason: str = Field(min_length=1, max_length=1000)

    @app.post("/api/admin/operations/{op_id}/recover-block")
    def recover_block(op_id: str, body: BlockRecovery, s=Depends(admin)):
        candidates = lost_blocks(op_id, s)
        op = store.one("SELECT * FROM operations WHERE id=?", (op_id,))
        payload = json.loads(op["payload"])
        matches = [
            b
            for b in candidates
            if b["blockId"] == body.block_id and b["quantity"] <= payload["quantity"]
        ]
        if len(matches) != 1:
            raise Conflict("Блок не подтверждён для этого запроса")
        recovered = service.gateway.recover_block(body.block_id)
        result = {"id": body.block_id, "complete": True, "receipt": recovered}
        with store.transaction() as db:
            service.require_operation_active(db, op)
            if not db.execute(
                "UPDATE operations SET external_id=?,result=?,state='submitted' WHERE id=? AND state='unknown'",
                (body.block_id, dumps(result), op_id),
            ).rowcount:
                raise Conflict("Операция уже обработана")
            audit(
                db,
                s["username"],
                "Восстановлен ранее полученный блок СУЗ",
                op_id,
                {"block": body.block_id, "reason": body.reason},
            )
        return {"ok": True}

    class Reconcile(BaseModel):
        external_id: str
        reason: str = Field(min_length=1, max_length=1000)

    @app.post("/api/admin/operations/{op_id}/reconcile")
    def reconcile(op_id: str, body: Reconcile, s=Depends(admin)):
        from uuid import UUID

        UUID(body.external_id)
        with store.transaction() as db:
            op = one(db, "SELECT * FROM operations WHERE id=?", (op_id,))
            if (
                not op
                or op["state"] != "unknown"
                or op["kind"] not in ("order", "report", "atk", "transform")
            ):
                raise Conflict("Операция не допускает привязку ID")
            service.require_operation_active(db, op)
            db.execute(
                "UPDATE operations SET external_id=?,state='submitted',error=NULL WHERE id=?",
                (body.external_id, op_id),
            )
            audit(
                db,
                s["username"],
                "Внешний ID установлен после сверки ЛК",
                op_id,
                body.model_dump(),
            )
        return {"ok": True}

    @app.post("/api/signer/next", dependencies=[Depends(signer)])
    def signer_next():
        if not settings.signer_thumbprint:
            raise HTTPException(409, "Настройте отпечаток сертификата на сервере")
        with store.transaction() as db:
            # Expired leases can be signed again: signing does not mutate GIS/OMS.
            for op in rows(db, "SELECT * FROM operations WHERE state='leased'"):
                if json.loads(op["lease"])["expires"] < time.time():
                    db.execute(
                        "UPDATE operations SET state='signing',lease=NULL WHERE id=?",
                        (op["id"],),
                    )
            op = one(
                db,
                "SELECT o.* FROM operations o WHERE o.state='signing' AND NOT EXISTS (SELECT 1 FROM operation_stops h JOIN document_stops d ON d.doc_id=h.doc_id WHERE h.op_id=o.id AND d.resumed_at IS NULL) ORDER BY o.created LIMIT 1",
            )
            if not op:
                return Response(status_code=204)
            if op["kind"] not in (
                "auth_true",
                "auth_oms",
                "order",
                "report",
                "atk",
                "transform",
            ):
                raise Conflict("Недопустимое задание подписи")
            service.require_operation_active(db, op)
            lease = secrets.token_urlsafe(32)
            db.execute(
                "UPDATE operations SET state='leased',lease=? WHERE id=?",
                (dumps({"token": lease, "expires": time.time() + 180}), op["id"]),
            )
            content = (
                json.loads(op["payload"])["data"].encode()
                if op["kind"].startswith("auth_")
                else op["payload"].encode()
            )
            return {
                "id": op["id"],
                "kind": op["kind"],
                "lease": lease,
                "payload_base64": base64.b64encode(content).decode(),
                "sha256": digest(content.decode()),
                "detached": not op["kind"].startswith("auth_"),
                "thumbprint": settings.signer_thumbprint,
                "inn": service.config()["inn"],
            }

    class Signed(BaseModel):
        id: str
        lease: str
        sha256: str
        signature: str = Field(max_length=1024 * 1024)
        thumbprint: str

    @app.post("/api/signer/result", dependencies=[Depends(signer)])
    def signer_result(body: Signed):
        try:
            signature = base64.b64decode(body.signature, validate=True)
            if not 64 <= len(signature) <= 750000:
                raise ValueError()
        except Exception:
            raise ValueError("Неверный формат подписи")
        if not hmac.compare_digest(
            body.thumbprint.upper().replace(" ", ""), settings.signer_thumbprint
        ):
            raise PermissionError("Другой сертификат")
        with store.transaction() as db:
            op = one(db, "SELECT * FROM operations WHERE id=?", (body.id,))
            if not op or op["state"] != "leased":
                raise Conflict("Задание уже обработано или отозвано")
            service.require_operation_active(db, op)
            lease = json.loads(op["lease"])
            if lease["expires"] < time.time() or not hmac.compare_digest(
                lease["token"], body.lease
            ):
                raise PermissionError("Истёк срок задания")
            payload = (
                json.loads(op["payload"])["data"]
                if op["kind"].startswith("auth_")
                else op["payload"]
            )
            if not hmac.compare_digest(digest(payload), body.sha256):
                raise Conflict("Подписаны другие байты")
            db.execute(
                "UPDATE operations SET state='queued',signature=?,lease=NULL,updated=? WHERE id=?",
                (body.signature, now(), body.id),
            )
            audit(
                db,
                "signer",
                "Получена подпись",
                body.id,
                {"sha256": body.sha256, "thumbprint": body.thumbprint},
            )
        return {"ok": True}

    class Heartbeat(BaseModel):
        thumbprint: str
        expires: str
        status: str = Field(max_length=120)

    @app.post("/api/signer/heartbeat", dependencies=[Depends(signer)])
    def heartbeat(body: Heartbeat):
        with store.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES(?,?)",
                ("signer_heartbeat", dumps({**body.model_dump(), "time": now()})),
            )
        return {"ok": True}

    return app


app = create_app()
