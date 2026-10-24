from collections import Counter, defaultdict
from datetime import date
from calendar import monthrange
from hashlib import sha256
from io import BytesIO
import json
import re
import zipfile
from pypdf import PdfReader, PdfWriter
from .db import now, uid, dumps, audit
from .excel import validate_rows, xlsx
from .codes import with_quantity
from .labels import render_label


class Conflict(ValueError):
    def __init__(self, message, detail=None):
        super().__init__(message)
        self.detail = detail


def safe_name(value, limit=170):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .") or "file"
    if len(s.encode()) > limit:
        end = "-" + sha256(value.encode()).hexdigest()[:12]
        s = s.encode()[: limit - len(end)].decode("utf-8", "ignore") + end
    return s


def rows(db, sql, args=()):
    return [dict(r) for r in db.execute(sql, args)]


def one(db, sql, args=()):
    r = db.execute(sql, args).fetchone()
    return dict(r) if r else None


class Service:
    def __init__(self, store, gateway, settings):
        self.store, self.gateway, self.settings = store, gateway, settings

    def config(self):
        result = {
            "organization": "",
            "inn": "",
            "oms_id": "",
            "oms_connection": "",
            "template_id": 36,
            "kpp": "",
            "fias_id": "",
        }
        result.update(
            {
                r["key"]: json.loads(r["value"])
                for r in self.store.all("SELECT * FROM settings")
            }
        )
        return result

    def document(self, db, doc_id, actor, admin=False):
        d = one(
            db,
            "SELECT d.*,s.name supplier FROM documents d JOIN suppliers s ON s.id=d.supplier_id WHERE d.id=?",
            (doc_id,),
        )
        if not d or (not admin and d["owner"] != actor):
            raise PermissionError("Документ недоступен")
        return d

    def get(self, doc_id, actor, admin=False):
        with self.store.transaction() as db:
            d = self.document(db, doc_id, actor, admin)
            d["lines"] = rows(
                db, "SELECT * FROM lines WHERE doc_id=? ORDER BY id", (doc_id,)
            )
            d["codes"] = rows(
                db,
                "SELECT id,cis,pn,quantity,tnved,external_status,local_state,atk_id FROM codes WHERE doc_id=?",
                (doc_id,),
            )
            d["operations"] = rows(
                db,
                "SELECT id,kind,state,external_id,error,created,updated FROM operations WHERE doc_id=? ORDER BY created",
                (doc_id,),
            )
            d["artifacts"] = rows(
                db,
                "SELECT id,name,kind,version,created FROM artifacts WHERE doc_id=? ORDER BY version DESC",
                (doc_id,),
            )
            d["history"] = rows(
                db,
                "SELECT actor,action,detail,created FROM audit WHERE entity=? ORDER BY id DESC LIMIT 100",
                (doc_id,),
            )
            d["atks"] = rows(
                db,
                "SELECT a.* FROM atks a JOIN batch_docs b ON b.batch_id=a.batch_id AND b.supplier_id=a.supplier_id WHERE b.doc_id IN (?,?)",
                (doc_id, d["parent_id"] or doc_id),
            )
            d["exports"] = rows(
                db,
                'SELECT f.id,f.name,f.kind,f.version,f.created FROM artifacts f JOIN batch_docs b ON b.batch_id||":"||b.supplier_id=f.group_id WHERE b.doc_id IN (?,?) ORDER BY f.created DESC',
                (doc_id, d["parent_id"] or doc_id),
            )
            d["required"] = sum(l["labels"] for l in d["lines"])
            d["units"] = sum(l["labels"] * l["quantity"] for l in d["lines"])
            return d

    def create(self, actor, number, supplier, parent_id=None, admin=False):
        if not isinstance(number, str) or not 1 <= len(number.strip()) <= 120:
            raise ValueError("Заполните номер накладной")
        if not isinstance(supplier, str) or not 1 <= len(supplier.strip()) <= 160:
            raise ValueError("Заполните поставщика")
        with self.store.transaction() as db:
            if parent_id:
                parent = self.document(db, parent_id, actor, admin)
                if parent["status"] not in ("done", "archive", "reported"):
                    raise ValueError("Дополнение создаётся к оформленному документу")
                supplier = parent["supplier"]
            s = one(db, "SELECT * FROM suppliers WHERE name=?", (supplier.strip(),))
            if s and s["merged_into"]:
                s = one(db, "SELECT * FROM suppliers WHERE id=?", (s["merged_into"],))
            if not s:
                s = {"id": uid()}
                db.execute(
                    "INSERT INTO suppliers(id,name) VALUES(?,?)",
                    (s["id"], supplier.strip()),
                )
            i = uid()
            db.execute(
                "INSERT INTO documents(id,number,supplier_id,owner,created,updated,parent_id) VALUES(?,?,?,?,?,?,?)",
                (i, number.strip(), s["id"], actor, now(), now(), parent_id),
            )
            audit(db, actor, "Создана накладная", i, {"parent": parent_id})
            return i

    def preview(self, doc_id, actor, raw_rows, admin=False):
        result = validate_rows(raw_rows)
        with self.store.transaction() as db:
            d = self.document(db, doc_id, actor, admin)
            old = Counter()
            for l in rows(db, "SELECT * FROM lines WHERE doc_id=?", (doc_id,)):
                old[l["pn"]] += l["quantity"] * l["labels"]
            new = Counter()
            for l in result["lines"]:
                new[l["pn"]] += l["quantity"] * l["labels"]
                cat = one(db, "SELECT tnved FROM catalog WHERE gtin=?", (l["gtin"],))
                if cat and cat["tnved"] != l["tnved"]:
                    result["errors"].append(
                        {
                            "row": 0,
                            "message": f"GTIN {l['gtin']}: в справочнике другой ТН ВЭД",
                        }
                    )
                known = rows(db, "SELECT pn FROM pn_map WHERE gtin=?", (l["gtin"],))
                if known and l["pn"] not in [x["pn"] for x in known]:
                    result["warnings"].append(f"GTIN {l['gtin']}: новый PN {l['pn']}")
            result["changes"] = (
                [
                    {
                        "pn": pn,
                        "before": old[pn],
                        "after": new[pn],
                        "difference": new[pn] - old[pn],
                    }
                    for pn in sorted(old.keys() | new.keys())
                    if old[pn] != new[pn]
                ]
                if old
                else []
            )
            result["revision"] = d["revision"]
            result["fingerprint"] = sha256(
                dumps(
                    {
                        "lines": result["lines"],
                        "changes": result["changes"],
                        "warnings": result["warnings"],
                        "revision": d["revision"],
                    }
                ).encode()
            ).hexdigest()
            return result

    def save_lines(
        self,
        doc_id,
        actor,
        raw_rows,
        revision,
        confirmation=None,
        admin=False,
        source=None,
    ):
        preview = self.preview(doc_id, actor, raw_rows, admin)
        if preview["errors"]:
            raise Conflict("Исправьте ошибки строк", preview)
        if (preview["changes"] or preview["warnings"]) and confirmation != preview[
            "fingerprint"
        ]:
            raise Conflict("Подтвердите изменения количества и предупреждения", preview)
        # Recheck all assigned/free candidates against True API before changing reservations.
        codes = self.store.all("SELECT * FROM codes WHERE doc_id=?", (doc_id,))
        if codes:
            self.gateway.verify(codes, "EMITTED")
        with self.store.transaction() as db:
            d = self.document(db, doc_id, actor, admin)
            if d["revision"] != revision or preview["revision"] != revision:
                raise Conflict("Документ изменился. Обновите страницу")
            if d["status"] not in ("draft", "ready", "transferred", "label_error"):
                raise Conflict("Изменение состава на этой стадии запрещено")
            if any(x["external_status"] != "EMITTED" for x in codes):
                raise Conflict("Нанесённые КМ изменять нельзя")
            db.execute("DELETE FROM lines WHERE doc_id=?", (doc_id,))
            for l in preview["lines"]:
                cat = one(db, "SELECT tnved FROM catalog WHERE gtin=?", (l["gtin"],))
                if cat and cat["tnved"] != l["tnved"]:
                    raise Conflict("Привязка ТН ВЭД изменилась")
                db.execute(
                    "INSERT OR IGNORE INTO catalog VALUES(?,?)", (l["gtin"], l["tnved"])
                )
                known = rows(db, "SELECT pn FROM pn_map WHERE gtin=?", (l["gtin"],))
                if (
                    known
                    and l["pn"] not in [x["pn"] for x in known]
                    and confirmation != preview["fingerprint"]
                ):
                    raise Conflict(
                        "Появилось новое соответствие PN; повторите подтверждение"
                    )
                db.execute(
                    "INSERT OR IGNORE INTO pn_map VALUES(?,?)", (l["gtin"], l["pn"])
                )
                db.execute(
                    "INSERT INTO lines(doc_id,pn,gtin,quantity,labels,tnved) VALUES(?,?,?,?,?,?)",
                    (
                        doc_id,
                        l["pn"],
                        l["gtin"],
                        l["quantity"],
                        l["labels"],
                        l["tnved"],
                    ),
                )
            db.execute(
                "UPDATE documents SET revision=revision+1,transferred_version=NULL,status=?,updated=? WHERE id=?",
                ("draft" if d["status"] == "draft" else "repack", now(), doc_id),
            )
            audit(
                db,
                actor,
                "Изменён состав",
                doc_id,
                {
                    "lines": preview["lines"],
                    "changes": preview["changes"],
                    "previous_version": d["current_version"],
                },
            )
            if source:
                self.artifact(
                    db,
                    "import.xlsx",
                    source,
                    "source",
                    doc_id=doc_id,
                    version=revision + 1,
                    snapshot=preview["lines"],
                )
            if d["status"] != "draft":
                self.release(db, doc_id)
        if d["status"] != "draft":
            self.order(doc_id, actor, admin, repack=True)
        return self.get(doc_id, actor, admin)

    def release(self, db, doc_id):
        assigned = rows(db, "SELECT * FROM codes WHERE doc_id=?", (doc_id,))
        if any(
            c["external_status"] != "EMITTED" or c["local_state"] != "reserved"
            for c in assigned
        ):
            raise Conflict("Нельзя освободить КМ")
        db.execute(
            "UPDATE assignments SET active=0,ended=? WHERE doc_id=? AND active=1",
            (now(), doc_id),
        )
        db.execute(
            "UPDATE codes SET doc_id=NULL,pn=NULL,quantity=NULL,local_state='free' WHERE doc_id=?",
            (doc_id,),
        )

    def queue(self, db, kind, key, payload, doc_id=None, group_id=None):
        existing = one(db, "SELECT * FROM operations WHERE op_key=?", (key,))
        if existing:
            return existing["id"]
        op = uid()
        body = dumps(payload)
        db.execute(
            "INSERT INTO operations(id,kind,doc_id,group_id,op_key,payload,sha256,state,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                op,
                kind,
                doc_id,
                group_id,
                key,
                body,
                sha256(body.encode()).hexdigest(),
                "queued" if kind == "fetch" else "signing",
                now(),
                now(),
            ),
        )
        return op

    def order(self, doc_id, actor, admin=False, repack=False):
        existing = self.get(doc_id, actor, admin)
        if existing["status"] in ("ordering", "receiving", "ready", "transferred"):
            return existing
        config = self.config()
        for key in ("inn", "oms_id", "oms_connection"):
            if not config[key]:
                raise ValueError(
                    "Администратору нужно настроить организацию и доступ к СУЗ"
                )
        # Verify candidates outside transaction; CAS in the transaction prevents double assignment.
        needed_gtins = {
            l["gtin"]
            for l in self.store.all("SELECT gtin FROM lines WHERE doc_id=?", (doc_id,))
        }
        candidates = [
            c
            for c in self.store.all(
                "SELECT * FROM codes WHERE doc_id IS NULL AND local_state='free' AND external_status='EMITTED'"
            )
            if c["gtin"] in needed_gtins
        ]
        verified = self.gateway.verify(candidates, "EMITTED") if candidates else {}
        with self.store.transaction() as db:
            d = self.document(db, doc_id, actor, admin)
            if d["status"] in ("ordering", "receiving", "ready", "transferred"):
                return d
            if d["status"] not in ("draft", "repack"):
                raise Conflict("Заказ на этой стадии запрещён")
            ls = rows(db, "SELECT * FROM lines WHERE doc_id=? ORDER BY id", (doc_id,))
            if not ls:
                raise ValueError("Сначала заполните состав")
            for l in ls:
                cat = one(db, "SELECT * FROM catalog WHERE gtin=?", (l["gtin"],))
                if cat["tnved"] != l["tnved"]:
                    raise Conflict("Изменился ТН ВЭД в справочнике. Проверьте состав")
            need = Counter()
            for l in ls:
                need[l["gtin"]] += l["labels"]
            for gtin, n in need.items():
                available = rows(
                    db,
                    "SELECT * FROM codes WHERE gtin=? AND doc_id IS NULL AND local_state='free' AND external_status='EMITTED' ORDER BY id",
                    (gtin,),
                )
                if any(c["cis"] not in verified for c in available):
                    raise Conflict(
                        "Пул КМ изменился. Повторите резервирование: новая эмиссия ещё не отправлена"
                    )
                allocated = 0
                for c in available:
                    if c["cis"] not in verified or allocated >= n:
                        continue
                    db.execute(
                        "UPDATE codes SET doc_id=?,local_state='reserved',checked=? WHERE id=? AND doc_id IS NULL",
                        (doc_id, now(), c["id"]),
                    )
                    allocated += 1
                deficit = n - allocated
                if deficit:
                    payload = {
                        "productGroup": "radio",
                        "products": [
                            {
                                "gtin": gtin,
                                "quantity": deficit,
                                "serialNumberType": "OPERATOR",
                                "templateId": config["template_id"],
                                "cisType": "UNIT",
                            }
                        ],
                        "attributes": {
                            "releaseMethodType": "IMPORT",
                            "paymentType": 1,
                            "productionOrderId": f"{doc_id}-{d['revision']}-{gtin}",
                        },
                    }
                    self.queue(
                        db,
                        "order",
                        f"order:{doc_id}:{d['revision']}:{gtin}",
                        payload,
                        doc_id,
                    )
            db.execute(
                "UPDATE documents SET status='receiving',updated=? WHERE id=?",
                (now(), doc_id),
            )
            audit(
                db,
                actor,
                "Заказаны КМ",
                doc_id,
                {"required": dict(need), "revision": d["revision"]},
            )
            self.prepare_labels(db, doc_id)
        return self.get(doc_id, actor, admin)

    def artifact(
        self,
        db,
        name,
        content,
        kind,
        *,
        doc_id=None,
        group_id=None,
        version=1,
        snapshot=None,
    ):
        i = uid()
        db.execute(
            "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                i,
                doc_id,
                group_id,
                version,
                kind,
                name,
                content,
                sha256(content).hexdigest(),
                dumps(snapshot or {}),
                now(),
            ),
        )
        return i

    def prepare_labels(self, db, doc_id):
        d = one(db, "SELECT * FROM documents WHERE id=?", (doc_id,))
        ls = rows(db, "SELECT * FROM lines WHERE doc_id=? ORDER BY id", (doc_id,))
        cs = rows(db, "SELECT * FROM codes WHERE doc_id=? ORDER BY gtin,id", (doc_id,))
        if len(cs) != sum(l["labels"] for l in ls):
            return
        if any(c["external_status"] != "EMITTED" for c in cs):
            raise Conflict("Статус КМ не EMITTED")
        buckets = defaultdict(list)
        for c in cs:
            buckets[c["gtin"]].append(c)
        snapshot = []
        files = {}
        used = set()
        for l in ls:
            writer = PdfWriter()
            for _ in range(l["labels"]):
                c = buckets[l["gtin"]].pop(0)
                full = with_quantity(c["raw"], l["quantity"])
                writer.add_page(
                    PdfReader(
                        BytesIO(render_label(full, l["pn"], l["quantity"]))
                    ).pages[0]
                )
                item = {
                    "id": c["id"],
                    "cis": c["cis"],
                    "full": full,
                    "pn": l["pn"],
                    "quantity": l["quantity"],
                    "tnved": l["tnved"],
                    "gtin": l["gtin"],
                }
                snapshot.append(item)
                db.execute(
                    "UPDATE codes SET pn=?,quantity=?,tnved=? WHERE id=?",
                    (l["pn"], l["quantity"], l["tnved"], c["id"]),
                )
                db.execute(
                    "INSERT INTO assignments(code_id,doc_id,pn,quantity,tnved,active,created) VALUES(?,?,?,?,?,1,?)",
                    (c["id"], doc_id, l["pn"], l["quantity"], l["tnved"], now()),
                )
            name = safe_name(l["pn"], 100) + f" - {l['quantity']} - {l['gtin']}.pdf"
            # Preserve required basename; isolate sanitization collisions in deterministic folders.
            path = (
                name
                if name.casefold() not in used
                else sha256(l["pn"].encode()).hexdigest()[:12] + "/" + name
            )
            used.add(name.casefold())
            buf = BytesIO()
            writer.write(buf)
            files[path] = buf.getvalue()
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name, content in files.items():
                z.writestr(name, content)
        version = d["current_version"] + 1
        self.artifact(
            db,
            safe_name(d["number"]) + f"-v{version}.zip",
            buf.getvalue(),
            "labels",
            doc_id=doc_id,
            version=version,
            snapshot=snapshot,
        )
        db.execute(
            "UPDATE documents SET status='ready',current_version=?,transferred_version=NULL,last_error=NULL,updated=? WHERE id=?",
            (version, now(), doc_id),
        )
        audit(
            db,
            "system",
            "Этикетки готовы",
            doc_id,
            {"version": version, "labels": len(snapshot)},
        )

    def transferred(self, doc_id, actor, version, admin=False):
        with self.store.transaction() as db:
            d = self.document(db, doc_id, actor, admin)
            if (
                d["status"] not in ("ready", "transferred")
                or version != d["current_version"]
            ):
                raise Conflict("Передайте актуальную версию этикеток")
            db.execute(
                "UPDATE documents SET status='transferred',transferred_version=?,updated=? WHERE id=?",
                (version, now(), doc_id),
            )
            audit(db, actor, "Передано поставщику", doc_id, {"version": version})

    def batch(self, actor, doc_ids, production_date, kpp, fias_id="", admin=False):
        if not doc_ids or len(doc_ids) != len(set(doc_ids)):
            raise ValueError("Выберите уникальные накладные")
        try:
            if date.fromisoformat(production_date) > date.today():
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError("Укажите фактическую дату производства не в будущем")
        if fias_id:
            from uuid import UUID

            UUID(fias_id)
        if not re.fullmatch("[0-9]{9}", kpp or ""):
            raise ValueError("Укажите КПП места деятельности (9 цифр)")
        with self.store.transaction() as db:
            docs = [self.document(db, i, actor, admin) for i in sorted(doc_ids)]
            if any(d["parent_id"] for d in docs) and not all(
                d["parent_id"] for d in docs
            ):
                raise Conflict("Обрабатывайте дополнения отдельно от обычных накладных")
            existing = rows(
                db,
                "SELECT * FROM batch_docs WHERE doc_id IN ("
                + ",".join("?" * len(doc_ids))
                + ")",
                doc_ids,
            )
            if existing:
                ids = {x["batch_id"] for x in existing}
                if len(existing) == len(doc_ids) and len(ids) == 1:
                    return next(iter(ids))
                raise Conflict("Часть документов уже обрабатывается в другой партии")
            if any(
                d["status"] != "transferred"
                or d["transferred_version"] != d["current_version"]
                for d in docs
            ):
                raise Conflict(
                    "Отметьте передачу актуальных этикеток для всех накладных"
                )
            batch = uid()
            db.execute("INSERT INTO batches VALUES(?,?,?)", (batch, now(), actor))
            for d in docs:
                db.execute(
                    "INSERT INTO batch_docs VALUES(?,?,?)",
                    (batch, d["id"], d["supplier_id"]),
                )
                art = one(
                    db,
                    "SELECT * FROM artifacts WHERE doc_id=? AND version=? AND kind='labels'",
                    (d["id"], d["current_version"]),
                )
                snapshot = json.loads(art["snapshot"])
                payload = {
                    "productGroup": "radio",
                    "sntins": [c["full"] for c in snapshot],
                    "attributes": {"productionDate": production_date, "kpp": kpp},
                }
                if fias_id:
                    payload["attributes"]["fiasId"] = fias_id
                self.queue(
                    db,
                    "report",
                    "report:" + d["id"],
                    payload,
                    d["id"],
                    batch + ":" + d["supplier_id"],
                )
                db.execute(
                    "UPDATE codes SET local_state='reporting' WHERE doc_id=?",
                    (d["id"],),
                )
                db.execute(
                    "UPDATE documents SET status='reporting',production_date=?,kpp=?,fias_id=?,updated=? WHERE id=?",
                    (production_date, kpp, fias_id, now(), d["id"]),
                )
                audit(
                    db,
                    actor,
                    "Подтверждено нанесение. Отчёт поставлен в очередь",
                    d["id"],
                    {"batch": batch, "version": d["current_version"]},
                )
            return batch

    def create_atks(self, db):
        groups = rows(db, "SELECT DISTINCT batch_id,supplier_id FROM batch_docs")
        for g in groups:
            ds = rows(
                db,
                "SELECT d.* FROM documents d JOIN batch_docs b ON b.doc_id=d.id WHERE b.batch_id=? AND b.supplier_id=?",
                (g["batch_id"], g["supplier_id"]),
            )
            if any(
                d["status"] not in ("reported", "aggregating", "done", "archive")
                for d in ds
            ):
                continue
            # Supplements await an approved ADDING transformation into the original ATK.
            if all(d["parent_id"] for d in ds):
                continue
            cs = rows(
                db,
                "SELECT c.* FROM codes c JOIN batch_docs b ON b.doc_id=c.doc_id WHERE b.batch_id=? AND b.supplier_id=?",
                (g["batch_id"], g["supplier_id"]),
            )
            buckets = defaultdict(list)
            for c in cs:
                buckets[c["tnved"]].append(c)
            for tnved, items in buckets.items():
                if one(
                    db,
                    "SELECT id FROM atks WHERE batch_id=? AND supplier_id=? AND tnved=?",
                    (g["batch_id"], g["supplier_id"], tnved),
                ):
                    continue
                group = g["batch_id"] + ":" + g["supplier_id"]
                payload = {
                    "trade_participant_inn": self.config()["inn"],
                    "products_list": [{"ki": c["cis"]} for c in items],
                }
                op = self.queue(
                    db, "atk", "atk:" + group + ":" + tnved, payload, group_id=group
                )
                atk = uid()
                db.execute(
                    "INSERT INTO atks(id,supplier_id,tnved,batch_id,op_id,state) VALUES(?,?,?,?,?,?)",
                    (atk, g["supplier_id"], tnved, g["batch_id"], op, "processing"),
                )
                db.execute(
                    "INSERT INTO atk_versions VALUES(?,?,?,?)",
                    (atk, 1, dumps([c["id"] for c in items]), now()),
                )
            for d in ds:
                if d["status"] == "reported":
                    db.execute(
                        "UPDATE documents SET status='aggregating',updated=? WHERE id=?",
                        (now(), d["id"]),
                    )

    def exports(self, db, batch, supplier):
        atks = rows(
            db,
            "SELECT * FROM atks WHERE batch_id=? AND supplier_id=?",
            (batch, supplier),
        )
        if not atks or any(a["state"] != "done" for a in atks):
            return
        docs = rows(
            db,
            "SELECT d.* FROM documents d JOIN batch_docs b ON b.doc_id=d.id WHERE b.batch_id=? AND b.supplier_id=? ORDER BY d.number,d.id",
            (batch, supplier),
        )
        s = one(
            db,
            "SELECT * FROM suppliers WHERE id=?",
            (self.canonical_supplier(db, supplier),),
        )
        group = batch + ":" + supplier
        ver = 1 + max(
            [
                x["version"]
                for x in rows(
                    db,
                    "SELECT version FROM artifacts WHERE group_id=? AND kind='atk'",
                    (group,),
                )
            ]
            or [0]
        )
        ids = []
        for a in atks:
            ids += json.loads(
                one(
                    db,
                    "SELECT composition FROM atk_versions WHERE atk_id=? AND version=?",
                    (a["id"], a["version"]),
                )["composition"]
            )
        codes = [one(db, "SELECT * FROM codes WHERE id=?", (i,)) for i in ids]
        extra_ids = {c["doc_id"] for c in codes} - {d["id"] for d in docs}
        docs.extend(
            one(db, "SELECT * FROM documents WHERE id=?", (i,))
            for i in sorted(extra_ids)
        )
        docs.sort(key=lambda d: (d["number"], d["id"]))
        title = safe_name(s["name"] + "-" + "-".join(d["number"] for d in docs))
        meta = [
            ["Поставщик", s["name"]],
            ["Номера накладных", ", ".join(d["number"] for d in docs)],
            ["Внутренние ID", ", ".join(d["id"] for d in docs)],
            ["Версия", ver],
        ]
        summary = [
            ["ТН ВЭД", "АТК", "Количество штук (сумма переменной характеристики)"]
        ]
        result = [
            ["Полный КМ с крипточастью", "PN", "Количество штук (AI 30)", "ТН ВЭД"]
        ]
        for a in atks:
            summary.append(
                [
                    a["tnved"],
                    a["code"],
                    sum(c["quantity"] for c in codes if c["atk_id"] == a["id"]),
                ]
            )
        for c in codes:
            result.append(
                [
                    with_quantity(c["raw"], c["quantity"]),
                    c["pn"],
                    c["quantity"],
                    c["tnved"],
                ]
            )
        for kind, prefix, data in [
            ("atk", "АТК", summary),
            ("result", "RESULT", result),
        ]:
            self.artifact(
                db,
                prefix + "-" + title + ".xlsx",
                xlsx({"Данные": data, "Накладные": meta}),
                kind,
                group_id=group,
                version=ver,
                snapshot={"documents": [d["id"] for d in docs], "codes": ids},
            )
        for d in docs:
            db.execute(
                "UPDATE documents SET status=CASE WHEN status='archive' THEN status ELSE 'done' END,completed=COALESCE(completed,?),updated=? WHERE id=?",
                (now(), now(), d["id"]),
            )
            audit(
                db,
                "system",
                "Сформированы итоговые файлы",
                d["id"],
                {"version": ver, "group": group},
            )

    def canonical_supplier(self, db, supplier_id):
        s = one(db, "SELECT * FROM suppliers WHERE id=?", (supplier_id,))
        return s["merged_into"] or s["id"]

    def candidate_codes(self, atk_id, actor, admin=False):
        with self.store.transaction() as db:
            a = self.atk_access(db, atk_id, actor, admin)
            canonical = self.canonical_supplier(db, a["supplier_id"])
            return rows(
                db,
                "SELECT c.id,c.cis,c.pn,c.quantity,c.tnved FROM codes c JOIN documents d ON d.id=c.doc_id WHERE d.supplier_id=? AND c.tnved=? AND c.external_status='APPLIED' AND c.atk_id IS NULL AND c.reuse_allowed=1 AND d.status IN ('reported','done','archive')",
                (canonical, a["tnved"]),
            )

    def atk_access(self, db, atk_id, actor, admin=False):
        a = one(db, "SELECT * FROM atks WHERE id=?", (atk_id,))
        if not a:
            raise PermissionError("АТК недоступен")
        if not admin and not one(
            db,
            "SELECT d.id FROM documents d JOIN batch_docs b ON b.doc_id=d.id WHERE b.batch_id=? AND b.supplier_id=? AND d.owner=?",
            (a["batch_id"], a["supplier_id"], actor),
        ):
            raise PermissionError("АТК недоступен")
        return a

    def transform(self, atk_id, kind, code_ids, actor, admin=False):
        if (
            kind not in ("ADDING", "REMOVING")
            or not code_ids
            or len(code_ids) != len(set(code_ids))
        ):
            raise ValueError("Неверная заявка")
        with self.store.transaction() as db:
            a = self.atk_access(db, atk_id, actor, admin)
            if a["state"] != "done":
                raise Conflict("АТК ещё не готов")
            for i in code_ids:
                c = one(
                    db,
                    "SELECT c.*,d.supplier_id FROM codes c JOIN documents d ON d.id=c.doc_id WHERE c.id=?",
                    (i,),
                )
                if (
                    not c
                    or self.canonical_supplier(db, c["supplier_id"])
                    != self.canonical_supplier(db, a["supplier_id"])
                    or c["tnved"] != a["tnved"]
                    or c["external_status"] != "APPLIED"
                ):
                    raise Conflict("КМ не подходит для этого АТК")
                if (
                    kind == "ADDING"
                    and (c["atk_id"] is not None or not c["reuse_allowed"])
                ) or (kind == "REMOVING" and c["atk_id"] != atk_id):
                    raise Conflict("Состав КМ не соответствует операции")
                # Do not allow the same code to be claimed by competing approved requests.
                for t in rows(
                    db,
                    "SELECT code_ids FROM transformations WHERE state IN ('approval','approved','processing')",
                ):
                    if i in json.loads(t["code_ids"]):
                        raise Conflict("КМ уже включён в другую заявку")
            if kind == "REMOVING":
                count = one(
                    db, "SELECT COUNT(*) n FROM codes WHERE atk_id=?", (atk_id,)
                )["n"]
                if len(code_ids) >= count:
                    raise Conflict(
                        "Нельзя изъять весь состав АТК. Расформирование не поддерживается"
                    )
            t = uid()
            db.execute(
                "INSERT INTO transformations(id,atk_id,kind,code_ids,actor,state,created) VALUES(?,?,?,?,?,?,?)",
                (t, atk_id, kind, dumps(code_ids), actor, "approval", now()),
            )
            audit(
                db,
                actor,
                "Заявка на трансформацию",
                atk_id,
                {"request": t, "kind": kind, "codes": code_ids},
            )
            return t

    def review(self, request_id, approve, reason, actor):
        if not reason or len(reason) > 1000:
            raise ValueError("Укажите причину решения")
        with self.store.transaction() as db:
            t = one(db, "SELECT * FROM transformations WHERE id=?", (request_id,))
            if not t:
                raise ValueError("Заявка не найдена")
            if t["state"] != "approval":
                return t
            a = one(db, "SELECT * FROM atks WHERE id=?", (t["atk_id"],))
            op = None
            if approve:
                items = [
                    one(db, "SELECT * FROM codes WHERE id=?", (i,))
                    for i in json.loads(t["code_ids"])
                ]
                if any(
                    c["external_status"] != "APPLIED"
                    or (
                        t["kind"] == "ADDING"
                        and (c["atk_id"] or not c["reuse_allowed"])
                    )
                    or (t["kind"] == "REMOVING" and c["atk_id"] != a["id"])
                    for c in items
                ):
                    raise Conflict("Изменился состав или разрешения КМ")
                payload = {
                    "trade_participant_inn": self.config()["inn"],
                    "atk": a["code"],
                    "transformation_type": t["kind"],
                    "products_list": [{"ki": c["cis"]} for c in items],
                }
                op = self.queue(
                    db,
                    "transform",
                    "transform:" + request_id,
                    payload,
                    group_id=a["batch_id"] + ":" + a["supplier_id"],
                )
            db.execute(
                "UPDATE transformations SET state=?,reason=?,reviewer=?,op_id=? WHERE id=?",
                ("approved" if approve else "rejected", reason, actor, op, request_id),
            )
            audit(
                db,
                actor,
                "Одобрена трансформация" if approve else "Отклонена трансформация",
                a["id"],
                {"request": request_id, "reason": reason},
            )

    def archive(self, today=None):
        today = today or date.today()
        with self.store.transaction() as db:
            for d in rows(db, "SELECT * FROM documents WHERE status='done'"):
                dt = date.fromisoformat(d["completed"][:10])
                m = dt.month + 6
                y = dt.year + (m - 1) // 12
                m = (m - 1) % 12 + 1
                due = date(y, m, min(dt.day, monthrange(y, m)[1]))
                if today >= due:
                    db.execute(
                        "UPDATE documents SET status='archive' WHERE id=?", (d["id"],)
                    )
                    audit(db, "system", "Перемещено в архив", d["id"])

    def logistics_atks(
        self,
        *,
        q="",
        supplier="",
        status="",
        tnved="",
        date_from="",
        date_to="",
        archive=False,
    ):
        """Cross-owner ATK registry. Deliberately omits invoice payloads and full crypto."""
        result = []
        with self.store.read() as db:
            atks = rows(
                db,
                "SELECT a.*,b.created created,o.state operation_state,o.error operation_error,o.updated updated FROM atks a JOIN batches b ON b.id=a.batch_id JOIN operations o ON o.id=a.op_id ORDER BY b.created DESC,a.id",
            )
            for a in atks:
                item = self._logistics_summary(db, a)
                if item["archived"] != archive:
                    continue
                if supplier and item["supplier_id"] != supplier:
                    continue
                if status and item["status"] != status:
                    continue
                if tnved and tnved not in item["tnved"]:
                    continue
                if date_from and item["created"][:10] < date_from:
                    continue
                if date_to and item["created"][:10] > date_to:
                    continue
                if (
                    q
                    and q.casefold()
                    not in " ".join([item["code"] or "", *item["invoices"]]).casefold()
                ):
                    continue
                result.append(item)
        return result

    def _logistics_summary(self, db, a):
        canonical = self.canonical_supplier(db, a["supplier_id"])
        supplier = one(db, "SELECT name FROM suppliers WHERE id=?", (canonical,))
        docs = rows(
            db,
            "SELECT d.id,d.number,d.owner,d.status FROM documents d JOIN batch_docs b ON b.doc_id=d.id WHERE b.batch_id=? AND b.supplier_id=? ORDER BY d.number,d.id",
            (a["batch_id"], a["supplier_id"]),
        )
        # A supplier export can also contain applied supplements from other batches.
        group = a["batch_id"] + ":" + a["supplier_id"]
        files = rows(
            db,
            "SELECT id,name,kind,version,created,snapshot FROM artifacts WHERE group_id=? AND kind IN ('atk','result') ORDER BY version DESC,kind",
            (group,),
        )
        latest = max([f["version"] for f in files] or [0])
        current = [
            {k: v for k, v in f.items() if k != "snapshot"}
            for f in files
            if f["version"] == latest
        ]
        known = {d["id"] for d in docs}
        if files:
            for i in json.loads(files[0]["snapshot"]).get("documents", []):
                if i not in known:
                    d = one(
                        db,
                        "SELECT id,number,owner,status FROM documents WHERE id=?",
                        (i,),
                    )
                    if d:
                        docs.append(d)
                        known.add(i)
        stats = one(
            db,
            "SELECT COUNT(*) labels,COALESCE(SUM(c.quantity),0) units FROM atk_versions v JOIN json_each(v.composition) j JOIN codes c ON c.id=j.value WHERE v.atk_id=? AND v.version=?",
            (a["id"], a["version"]),
        )
        op = one(
            db, "SELECT state,error,updated FROM operations WHERE id=?", (a["op_id"],)
        )
        transform = one(
            db,
            "SELECT t.state,t.created,o.state operation_state,o.error FROM transformations t LEFT JOIN operations o ON o.id=t.op_id WHERE t.atk_id=? AND t.state IN ('approval','approved','processing') ORDER BY t.created DESC LIMIT 1",
            (a["id"],),
        )
        error = (transform["error"] if transform else None) or op["error"]
        state = (
            "attention"
            if error
            else (
                "transforming"
                if transform
                else ("done" if a["state"] == "done" else "processing")
            )
        )
        batch = one(db, "SELECT created FROM batches WHERE id=?", (a["batch_id"],))
        return {
            "id": a["id"],
            "code": a["code"],
            "supplier_id": canonical,
            "supplier": supplier["name"],
            "tnved": a["tnved"],
            "version": a["version"],
            "status": state,
            "operation_state": (transform["operation_state"] or transform["state"])
            if transform
            else op["state"],
            "error": error,
            "created": batch["created"],
            "updated": op["updated"],
            "labels": stats["labels"],
            "units": stats["units"],
            "invoices": [d["number"] for d in docs],
            "owners": sorted({d["owner"] for d in docs}),
            "archived": bool(docs) and all(d["status"] == "archive" for d in docs),
            "files": current,
            "group_id": group,
            "download_ready": any(f["kind"] == "atk" for f in current)
            and any(f["kind"] == "result" for f in current),
        }

    def logistics_atk(self, atk_id):
        with self.store.read() as db:
            a = one(db, "SELECT * FROM atks WHERE id=?", (atk_id,))
            if not a:
                raise PermissionError("АТК недоступен")
            result = self._logistics_summary(db, a)
            result["codes"] = rows(
                db,
                "SELECT c.cis,c.pn,c.quantity,c.tnved FROM atk_versions v JOIN json_each(v.composition) j JOIN codes c ON c.id=j.value WHERE v.atk_id=? AND v.version=? ORDER BY c.pn,c.cis",
                (atk_id, a["version"]),
            )
            result["history_files"] = rows(
                db,
                "SELECT id,name,kind,version,created FROM artifacts WHERE group_id=? AND kind IN ('atk','result') ORDER BY version DESC,kind",
                (result["group_id"],),
            )
            return result

    def logistics_download(self, atk_ids):
        if not atk_ids or len(atk_ids) > 200:
            raise ValueError("Выберите от 1 до 200 АТК")
        groups = {}
        # Capture the complete file selection in one consistent DB snapshot.
        with self.store.read() as db:
            for atk_id in sorted(set(atk_ids)):
                a = one(db, "SELECT * FROM atks WHERE id=?", (atk_id,))
                if not a:
                    raise PermissionError("АТК недоступен")
                summary = self._logistics_summary(db, a)
                if not summary["download_ready"]:
                    raise Conflict("Не все выбранные АТК имеют готовый комплект файлов")
                group = summary["group_id"]
                if group not in groups:
                    groups[group] = [
                        (
                            f["name"],
                            one(
                                db,
                                "SELECT content FROM artifacts WHERE id=?",
                                (f["id"],),
                            )["content"],
                        )
                        for f in summary["files"]
                    ]
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
            for group, files in groups.items():
                # One supplier workbook may cover multiple ATKs: include each pair once.
                for name, content in files:
                    archive.writestr(safe_name(group) + "/" + name, content)
        return buf.getvalue()
