"""Durable outbox. Any interruption during a consuming/mutating call stops retries."""

import json
import re
import threading
import time
from datetime import datetime, timezone, timedelta
from .db import now, uid, dumps, audit
from .service import one, rows, Conflict
from .codes import parse
from .gateway import RemoteError


class Worker:
    def __init__(self, service):
        self.s = service
        self.db = service.store
        self.g = service.gateway
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.last_token_check = 0

    def recover(self):
        with self.db.transaction() as db:
            db.execute(
                "UPDATE operations SET state='unknown',error='Работа прервана при внешнем запросе. Нужна сверка результата',updated=? WHERE state='dispatching'",
                (now(),),
            )
            db.execute(
                "UPDATE operations SET state='signing',lease=NULL WHERE state='leased'"
            )

    def run(self):
        self.recover()
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                # Keep the durable worker alive without logging payloads or secrets.
                with self.db.transaction() as db:
                    db.execute(
                        "UPDATE operations SET state='unknown',error='Обработка прервана. Требуется сверка',updated=? WHERE state='dispatching'",
                        (now(),),
                    )
                    db.execute(
                        "INSERT OR REPLACE INTO settings VALUES(?,?)",
                        (
                            "worker_error",
                            dumps(
                                {
                                    "time": now(),
                                    "message": "Фоновая обработка прервана; проверьте операции",
                                }
                            ),
                        ),
                    )
            self.stop.wait(5)

    def tick(self):
        if not self.lock.acquire(blocking=False):
            return
        try:
            if time.time() - self.last_token_check > 60:
                self.last_token_check = time.time()
                try:
                    self.ensure_tokens()
                except (ValueError, RemoteError):
                    pass
            for op in self.db.all(
                "SELECT * FROM operations WHERE state IN ('queued','submitted','checking') ORDER BY created LIMIT 50"
            ):
                try:
                    if op["state"] == "queued":
                        self.dispatch(op)
                    else:
                        self.poll(op)
                except (RemoteError, ValueError, Conflict) as e:
                    with self.db.transaction() as db:
                        current = one(
                            db, "SELECT state FROM operations WHERE id=?", (op["id"],)
                        )
                        # Read-only transport failures are recoverable; validation failures stop.
                        state = (
                            "submitted"
                            if isinstance(e, RemoteError)
                            and not e.uncertain
                            and current["state"] in ("submitted", "checking")
                            else "blocked"
                        )
                        db.execute(
                            "UPDATE operations SET state=?,error=?,updated=? WHERE id=?",
                            (state, str(e), now(), op["id"]),
                        )
                except Exception:
                    # Do not log request data or secrets. Preserve uncertainty for a dispatched request.
                    with self.db.transaction() as db:
                        db.execute(
                            "UPDATE operations SET state=CASE WHEN state='dispatching' THEN 'unknown' ELSE 'blocked' END,error='Внутренняя ошибка. Требуется проверка администратором',updated=? WHERE id=?",
                            (now(), op["id"]),
                        )
            for d in self.db.all("SELECT id FROM documents WHERE status='receiving'"):
                codes = self.db.all("SELECT * FROM codes WHERE doc_id=?", (d["id"],))
                needed = self.db.one(
                    "SELECT SUM(labels) n FROM lines WHERE doc_id=?", (d["id"],)
                )["n"]
                if not needed or len(codes) != needed:
                    continue
                try:
                    self.g.verify(codes, "EMITTED")
                    with self.db.transaction() as db:
                        self.s.prepare_labels(db, d["id"])
                except RemoteError as e:
                    with self.db.transaction() as db:
                        db.execute(
                            "UPDATE documents SET last_error=? WHERE id=?",
                            (str(e), d["id"]),
                        )
                except ValueError as e:
                    with self.db.transaction() as db:
                        db.execute(
                            "UPDATE documents SET status='label_error',last_error=?,updated=? WHERE id=?",
                            (str(e), now(), d["id"]),
                        )
            with self.db.transaction() as db:
                self.s.create_atks(db)
            for d in self.db.all(
                "SELECT id,owner FROM documents WHERE status='repack'"
            ):
                try:
                    self.s.order(d["id"], d["owner"], repack=True)
                except (ValueError, RemoteError):
                    pass
            self.s.archive()
        finally:
            self.lock.release()

    def ensure_tokens(self):
        config = self.s.config()
        if not all(config.get(k) for k in ("inn", "oms_id", "oms_connection")):
            return
        tokens = self.s.settings.secrets()
        for kind in ("true", "oms"):
            token = tokens.get(kind, {})
            expires = token.get("expires")
            acquired = token.get("acquired")
            try:
                limit = (
                    datetime.fromisoformat(expires.replace("Z", "+00:00"))
                    if expires
                    else datetime.fromisoformat(acquired) + timedelta(hours=9)
                )
                if limit > datetime.now(timezone.utc) + timedelta(minutes=5):
                    continue
            except (ValueError, TypeError, AttributeError):
                pass
            if self.db.one(
                "SELECT id FROM operations WHERE kind=? AND state IN ('signing','leased','queued','dispatching','unknown')",
                ("auth_" + kind,),
            ):
                continue
            challenge = self.g.challenge()
            with self.db.transaction() as db:
                if not one(
                    db,
                    "SELECT id FROM operations WHERE kind=? AND state IN ('signing','leased','queued','dispatching','unknown')",
                    ("auth_" + kind,),
                ):
                    self.s.queue(
                        db, "auth_" + kind, "auth:" + kind + ":" + uid(), challenge
                    )

    def preflight(self, op):
        if op["kind"] not in ("report", "atk", "transform"):
            return
        payload = json.loads(op["payload"])
        cises = (
            [parse(s).cis for s in payload["sntins"]]
            if op["kind"] == "report"
            else [x["ki"] for x in payload["products_list"]]
        )
        codes = []
        for cis in cises:
            c = self.db.one("SELECT * FROM codes WHERE cis=?", (cis,))
            if not c:
                raise ValueError("Неизвестный КМ в операции")
            codes.append(c)
        parent = (
            payload.get("atk")
            if payload.get("transformation_type") == "REMOVING"
            else None
        )
        self.g.verify(
            codes,
            "EMITTED" if op["kind"] == "report" else "APPLIED",
            aggregate=op["kind"] != "report",
            parent=parent,
        )
        if op["kind"] == "transform":
            ci = self.g.info([payload["atk"]])[payload["atk"]]
            if (
                ci.get("status") != "APPLIED"
                or ci.get("statusEx") not in (None, "EMPTY", "FTS_RESPOND_NOT_OK")
                or ci.get("ownerInn") != self.s.config()["inn"]
            ):
                raise ValueError("Фактическое состояние АТК не допускает трансформацию")

    def dispatch(self, op):
        if not self.s.settings.send_enabled:
            return  # Queue survives until the deployment administrator enables writes.
        try:
            if op["kind"] == "fetch":
                baseline = self.g.blocks(json.loads(op["payload"]))
                with self.db.transaction() as db:
                    db.execute(
                        "UPDATE operations SET result=? WHERE id=? AND state='queued'",
                        (dumps({"baseline": baseline}), op["id"]),
                    )
            self.preflight(op)
        except (ValueError, RemoteError) as e:
            with self.db.transaction() as db:
                db.execute(
                    "UPDATE operations SET state='preflight_blocked',error=?,updated=? WHERE id=? AND state='queued'",
                    (str(e), now(), op["id"]),
                )
            return
        with self.db.transaction() as db:
            if not db.execute(
                "UPDATE operations SET state='dispatching',updated=? WHERE id=? AND state='queued'",
                (now(), op["id"]),
            ).rowcount:
                return
        try:
            r = self.g.submit(op)
        except (RemoteError, ValueError) as e:
            with self.db.transaction() as db:
                db.execute(
                    "UPDATE operations SET state=?,error=?,updated=? WHERE id=?",
                    (
                        "unknown"
                        if isinstance(e, RemoteError) and e.uncertain
                        else "blocked",
                        str(e),
                        now(),
                        op["id"],
                    ),
                )
            return
        # Persist response before parsing/importing or doing any further HTTP request.
        with self.db.transaction() as db:
            db.execute(
                "UPDATE operations SET external_id=?,state='submitted',result=?,updated=? WHERE id=?",
                (r["id"], dumps(r), now(), op["id"]),
            )
        if r.get("complete"):
            self.complete(
                self.db.one("SELECT * FROM operations WHERE id=?", (op["id"],)), r
            )

    def poll(self, op):
        if op["kind"] in ("fetch", "auth_true", "auth_oms"):
            return self.complete(op, json.loads(op["result"]))
        r = self.g.poll(op)
        if op["kind"] == "order":
            return self.order_buffers(op, r)
        if r.get("complete"):
            self.complete(op, r)
        else:
            with self.db.transaction() as db:
                db.execute(
                    "UPDATE operations SET state='submitted',error=NULL,updated=? WHERE id=?",
                    (now(), op["id"]),
                )

    def order_buffers(self, op, result):
        payload = json.loads(op["payload"])
        product = payload["products"][0]
        gtin = product["gtin"]
        total = product["quantity"]
        buffers = result.get("buffers", [])
        with self.db.transaction() as db:
            children = rows(
                db,
                "SELECT * FROM operations WHERE kind='fetch' AND op_key LIKE ?",
                ("fetch:" + op["id"] + ":%",),
            )
            received = sum(
                len(json.loads(c["result"])["receipt"]["codes"])
                for c in children
                if c["state"] == "done"
            )
            if received == total:
                db.execute(
                    "UPDATE operations SET state='done',error=NULL,updated=? WHERE id=?",
                    (now(), op["id"]),
                )
                return
            if received > total:
                raise Conflict("СУЗ вернула больше КМ, чем заказано")
            if any(c["state"] != "done" for c in children):
                return
            matching = [b for b in buffers if b.get("gtin") == gtin]
            if len(matching) != 1:
                return
            b = matching[0]
            n = b.get("availableCodes", 0)
            if b.get("bufferStatus") != "ACTIVE" or type(n) is not int or n < 1:
                return
            self.s.queue(
                db,
                "fetch",
                f"fetch:{op['id']}:{len(children)}",
                {
                    "orderId": op["external_id"],
                    "gtin": gtin,
                    "quantity": min(total - received, n),
                },
                op["doc_id"],
                op["id"],
            )

    def complete(self, op, result):
        kind = op["kind"]
        payload = json.loads(op["payload"])
        receipt = result.get("receipt")
        # Actual external status/parent is verified before completing a report or ATK.
        registry = None
        if kind in ("report", "atk", "transform"):
            cises = (
                [parse(s).cis for s in payload["sntins"]]
                if kind == "report"
                else [x["ki"] for x in payload["products_list"]]
            )
            registry = self.g.info(cises)
            if any(
                ci.get("status") != "APPLIED"
                or ci.get("ownerInn") != self.s.config()["inn"]
                for ci in registry.values()
            ):
                raise ValueError(
                    "ЧЗ ещё не подтвердил нанесение всех КМ; нужна повторная проверка"
                )
        with self.db.transaction() as db:
            current = one(db, "SELECT * FROM operations WHERE id=?", (op["id"],))
            if current["state"] == "done":
                return
            if receipt is None:
                raise ValueError("Нет квитанции")
            if kind == "fetch":
                requested = payload["quantity"]
                received = receipt.get("codes", [])
                if not received or len(received) > requested:
                    raise ValueError("Неверное количество КМ в блоке")
                if len(received) != len(set(received)):
                    raise ValueError("СУЗ вернула повторные КМ")
                for raw in received:
                    code = parse(raw)
                    if code.quantity is not None or code.gtin != payload["gtin"]:
                        raise ValueError(
                            "СУЗ вернула КМ другого GTIN или с заполненным количеством"
                        )
                    if one(db, "SELECT id FROM codes WHERE cis=?", (code.cis,)):
                        raise Conflict("Повторное получение КМ. Нужна сверка блока")
                    db.execute(
                        "INSERT INTO codes(id,raw,cis,gtin,serial,source_op,external_status,checked,local_state,doc_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            uid(),
                            raw,
                            code.cis,
                            code.gtin,
                            code.cis[18:38],
                            op["id"],
                            "EMITTED",
                            now(),
                            "reserved",
                            op["doc_id"],
                        ),
                    )
                # Code block is committed independently of PDF generation below.
            elif kind == "report":
                db.execute(
                    "UPDATE codes SET external_status='APPLIED',local_state='applied',checked=? WHERE doc_id=?",
                    (now(), op["doc_id"]),
                )
                db.execute(
                    "UPDATE documents SET status='reported',updated=? WHERE id=?",
                    (now(), op["doc_id"]),
                )
            elif kind == "atk":
                parents = {ci.get("parent") for ci in registry.values()}
                if len(parents) != 1 or not re.fullmatch(
                    "[0-9]{25}", next(iter(parents)) or ""
                ):
                    raise ValueError(
                        "ЧЗ не подтвердил единый 25-значный АТК для всех КМ"
                    )
                code = next(iter(parents))
                a = one(db, "SELECT * FROM atks WHERE op_id=?", (op["id"],))
                db.execute(
                    "UPDATE atks SET code=?,state='done' WHERE id=?", (code, a["id"])
                )
                for cis in registry:
                    db.execute(
                        "UPDATE codes SET atk_id=?,local_state='aggregated' WHERE cis=?",
                        (a["id"], cis),
                    )
                self.s.exports(db, a["batch_id"], a["supplier_id"])
            elif kind == "transform":
                adding = payload["transformation_type"] == "ADDING"
                if any(
                    (
                        ci.get("parent") != payload["atk"]
                        if adding
                        else ci.get("parent") not in (None, "")
                    )
                    for ci in registry.values()
                ):
                    raise ValueError("ЧЗ ещё не подтвердил новый состав АТК")
                t = one(db, "SELECT * FROM transformations WHERE op_id=?", (op["id"],))
                a = one(db, "SELECT * FROM atks WHERE id=?", (t["atk_id"],))
                composition = set(
                    json.loads(
                        one(
                            db,
                            "SELECT composition FROM atk_versions WHERE atk_id=? AND version=?",
                            (a["id"], a["version"]),
                        )["composition"]
                    )
                )
                for i in json.loads(t["code_ids"]):
                    if adding:
                        composition.add(i)
                        db.execute(
                            "UPDATE codes SET atk_id=?,local_state='aggregated' WHERE id=?",
                            (a["id"], i),
                        )
                    else:
                        composition.remove(i)
                        db.execute(
                            "UPDATE codes SET atk_id=NULL,local_state='removed_review',reuse_allowed=0 WHERE id=?",
                            (i,),
                        )
                db.execute("UPDATE atks SET version=version+1 WHERE id=?", (a["id"],))
                db.execute(
                    "INSERT INTO atk_versions VALUES(?,?,?,?)",
                    (a["id"], a["version"] + 1, dumps(sorted(composition)), now()),
                )
                db.execute(
                    "UPDATE transformations SET state='done' WHERE id=?", (t["id"],)
                )
                audit(
                    db,
                    t["reviewer"],
                    "Подтверждена трансформация ЧЗ",
                    a["id"],
                    {"request": t["id"], "version": a["version"] + 1},
                )
                self.s.exports(db, a["batch_id"], a["supplier_id"])
            db.execute(
                "INSERT INTO receipts(op_id,body,created) VALUES(?,?,?)",
                (op["id"], dumps(receipt), now()),
            )
            db.execute(
                "UPDATE operations SET state='done',result=?,error=NULL,updated=? WHERE id=?",
                (dumps(result), now(), op["id"]),
            )
            audit(
                db,
                "system",
                "Операция подтверждена",
                op["doc_id"] or op["group_id"] or op["id"],
                {"operation": op["id"], "kind": kind, "external_id": op["external_id"]},
            )
