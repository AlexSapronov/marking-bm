"""Durable outbox. Any interruption during a consuming/mutating call stops retries."""

import json
import re
import threading
import time
from datetime import datetime, timezone, timedelta
from .db import now, uid, dumps, audit
from .service import one, rows, Conflict
from .codes import parse
from .gateway import RemoteError, ReportRejected, ReportPartiallyProcessed


# The 25-character ATK comprises 20 decimal digits and five characters
# in [0-9A-Za-z]. The final five preserve case (e.g. "K0KvG").
# Never coerce ATK to int or assume a digits-only identifier.
def valid_atk_code(value):
    return (
        isinstance(value, str)
        and re.fullmatch(r"[0-9]{20}[0-9A-Za-z]{5}", value) is not None
    )


class Worker:
    def __init__(self, service):
        self.s = service
        self.db = service.store
        self.g = service.gateway
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.last_token_check = 0

    def held(self, op):
        with self.db.read() as db:
            return self.s.operation_stopped(db, op)

    def retain_held_result(self, db, op, result):
        if not self.s.operation_stopped(db, op):
            return False
        # Preserve data returned by a request already in flight, but do not apply
        # its business transitions, import a block or create any follow-up work.
        db.execute(
            "UPDATE operations SET result=?,state='submitted',updated=? WHERE id=? AND state IN ('submitted','checking','dispatching')",
            (dumps(result), now(), op["id"]),
        )
        return True

    def recover(self):
        with self.db.transaction() as db:
            db.execute(
                "UPDATE operations SET state='unknown',error='Работа прервана при внешнем запросе. Нужна сверка результата',updated=? WHERE state='dispatching'",
                (now(),),
            )
            db.execute(
                "UPDATE operations SET state='signing',lease=NULL WHERE state='leased'"
            )
            # A status check is read-only. After restart it is no longer in flight;
            # held operations remain excluded until the administrator restores them.
            db.execute(
                "UPDATE operations SET state='submitted',updated=? WHERE state='checking'",
                (now(),),
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
            # Reserve capacity for both new dispatches and status checks. A full
            # page of waiting orders must not starve authorization or later polls.
            scheduled = self.db.all(
                "SELECT o.* FROM operations o WHERE o.state='queued' AND NOT EXISTS (SELECT 1 FROM operation_stops h JOIN document_stops d ON d.doc_id=h.doc_id WHERE h.op_id=o.id AND d.resumed_at IS NULL) ORDER BY o.created,o.id LIMIT 25"
            ) + self.db.all(
                "SELECT o.* FROM operations o WHERE o.state IN ('submitted','checking') AND NOT EXISTS (SELECT 1 FROM operation_stops h JOIN document_stops d ON d.doc_id=h.doc_id WHERE h.op_id=o.id AND d.resumed_at IS NULL) ORDER BY o.updated,o.created,o.id LIMIT 25"
            )
            for op in scheduled:
                try:
                    if op["state"] == "queued":
                        self.dispatch(op)
                    else:
                        self.poll(op)
                except ReportPartiallyProcessed as e:
                    with self.db.transaction() as db:
                        db.execute(
                            "UPDATE operations SET state='partial',error=?,updated=? "
                            "WHERE id=? AND state IN ('submitted','checking')",
                            (str(e), now(), op["id"]),
                        )
                except ReportRejected as e:
                    # A report ID with explicit REJECTED status is terminal.
                    # It is never re-polled; retry must create a fresh signed job.
                    with self.db.transaction() as db:
                        db.execute(
                            "UPDATE operations SET state='rejected',error=?,updated=? "
                            "WHERE id=? AND state IN ('submitted','checking')",
                            (str(e), now(), op["id"]),
                        )
                except (RemoteError, ValueError, Conflict) as e:
                    with self.db.transaction() as db:
                        current = one(
                            db, "SELECT state FROM operations WHERE id=?", (op["id"],)
                        )
                        if current["state"] == "cancelled":
                            continue
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
                            "UPDATE operations SET state=CASE WHEN state='dispatching' THEN 'unknown' ELSE 'blocked' END,error='Внутренняя ошибка. Требуется проверка администратором',updated=? WHERE id=? AND state!='cancelled'",
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
                            "UPDATE documents SET status='label_error',last_error=?,updated=? WHERE id=? AND status='receiving'",
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
        if op["kind"] == "introduce":
            payload = json.loads(op["payload"])
            for product in payload["products_list"]:
                codes = [self.db.one("SELECT * FROM codes WHERE cis=?", (c["cis"],)) for c in product["children"]]
                if any(c is None for c in codes):
                    raise ValueError("Неизвестный КМ в документе ввода в оборот")
                if any(c["tnved"] != item.get("tn_ved") or not re.fullmatch(r"[0-9]{10}", item.get("tn_ved", ""))
                       for c, item in zip(codes, product["children"])):
                    raise ValueError("У каждого КМ должен быть явно указан ТН ВЭД строки накладной")
                info = self.g.verify(codes, "APPLIED")
                if any(info[c["cis"]].get("tnVedEaes") != c["tnved"] for c in codes):
                    raise ValueError("ГИС МТ не подтвердила полный ТН ВЭД каждого КМ")
                if any(ci.get("parent") != product["atk"] or ci.get("emissionType") != "FOREIGN" or
                       ci.get("statusEx") not in (None, "EMPTY", "FTS_RESPOND_NOT_OK", "FTS_CONTROL") for ci in info.values()):
                    raise ValueError("Состав или состояние КМ в ГИС МТ изменилось перед вводом в оборот")
                aggregate = self.g.info([product["atk"]])[product["atk"]]
                if aggregate.get("ownerInn") != self.s.config()["inn"] or aggregate.get("status") != "APPLIED":
                    raise ValueError("ГИС МТ не подтвердила состояние АТК перед вводом в оборот")
            return
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
        if self.held(op):
            return
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
            if self.s.operation_stopped(db, op):
                return
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
        with self.db.transaction() as db:
            if self.s.operation_stopped(db, op):
                return
            # Touch even pending orders with empty buffers, so subsequent ticks
            # rotate through the entire queue rather than the first page forever.
            db.execute(
                "UPDATE operations SET state='checking',updated=? WHERE id=? AND state IN ('submitted','checking')",
                (now(), op["id"]),
            )
        if op["kind"] in ("fetch", "auth_true", "auth_oms"):
            return self.complete(op, json.loads(op["result"]))
        r = self.g.poll(op)
        with self.db.transaction() as db:
            if self.retain_held_result(db, op, r):
                return
        if op["kind"] == "order":
            return self.order_buffers(op, r)
        if r.get("complete") and op["kind"] == "introduce":
            payload = json.loads(op["payload"])
            info = self.g.info([c["cis"] for a in payload["products_list"] for c in a["children"]])
            if any(ci.get("statusEx") == "FTS_RESPOND_NOT_OK" for ci in info.values()):
                raise ValueError("Отрицательное решение ФТС: товар не введён в оборот. Проверьте ДТ в ЛК ЧЗ")
            if any(ci.get("status") != "INTRODUCED" or ci.get("ownerInn") != self.s.config()["inn"] for ci in info.values()):
                # A successful document can still await the customs response.
                r["complete"] = False
            else:
                r["registry"] = info
        if r.get("complete"):
            self.complete(op, r)
        else:
            with self.db.transaction() as db:
                db.execute(
                    "UPDATE operations SET state='submitted',error=NULL,updated=? WHERE id=? AND state='checking'",
                    (now(), op["id"]),
                )

    def order_buffers(self, op, result):
        payload = json.loads(op["payload"])
        product = payload["products"][0]
        gtin = product["gtin"]
        total = product["quantity"]
        buffers = result.get("buffers", [])
        with self.db.transaction() as db:
            if self.retain_held_result(db, op, result):
                return
            current = one(db, "SELECT state FROM operations WHERE id=?", (op["id"],))
            if current["state"] not in ("submitted", "checking"):
                return
            db.execute(
                "UPDATE operations SET state='submitted',updated=? WHERE id=?",
                (now(), op["id"]),
            )
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
        with self.db.transaction() as db:
            if self.retain_held_result(db, op, result):
                return
            current = one(db, "SELECT state FROM operations WHERE id=?", (op["id"],))
            if current["state"] not in ("submitted", "checking"):
                return
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
            if self.retain_held_result(db, op, result):
                return
            if current["state"] not in ("submitted", "checking"):
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
            elif kind == "introduce":
                expected = {c["cis"] for a in payload["products_list"] for c in a["children"]}
                registry = result.get("registry", {})
                if set(registry) != expected or any(ci.get("status") != "INTRODUCED" or ci.get("ownerInn") != self.s.config()["inn"] for ci in registry.values()):
                    raise ValueError("Нет подтверждения ввода в оборот всех КМ")
                for cis in expected:
                    db.execute("UPDATE codes SET external_status='INTRODUCED',local_state='introduced',reuse_allowed=0,checked=? WHERE cis=?", (now(), cis))
                for d in rows(db, "SELECT doc_id FROM introduction_documents WHERE op_id=?", (op["id"],)):
                    db.execute("UPDATE documents SET status='archive',updated=? WHERE id=?", (now(), d["doc_id"]))
                    audit(db, "system", "Ввод в оборот подтверждён ГИС МТ. Накладная завершена", d["doc_id"], {"operation": op["id"], "external_id": op["external_id"]})
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
                if len(parents) != 1 or not valid_atk_code(next(iter(parents))):
                    raise ValueError(
                        "ЧЗ не подтвердил единый 25-символьный буквенно-цифровой АТК для всех КМ"
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
