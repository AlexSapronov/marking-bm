from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
import zipfile
import pytest
from marking_bm.service import Conflict
from marking_bm.excel import validate_rows, import_file, template, xlsx, HEADERS
from conftest import GTIN, TN, add_doc, advance, sign_all, client


def test_import_merges_and_preserves_text():
    r = validate_rows(
        [["P", GTIN, 5, 2, TN], ["P", GTIN, 5, 3, TN], ["P", GTIN, 10, 1, TN]]
    )
    assert not r["errors"] and len(r["lines"]) == 2 and r["lines"][0]["labels"] == 5
    assert r["lines"][0]["gtin"] == GTIN
    other = "04640638349711"
    assert validate_rows([["P", GTIN, 5, 2, TN], ["P", other, 5, 1, TN]])["errors"]
    assert validate_rows([["P", GTIN, 5, 2, TN], ["Q", GTIN, 5, 1, "1234567890"]])[
        "errors"
    ]
    numeric = validate_rows([["P", int(GTIN), 5, 2, int(TN)]])
    assert not numeric["errors"] and numeric["lines"][0]["gtin"] == GTIN
    assert import_file(template())["errors"]
    result = import_file(xlsx({"Товары": [HEADERS, ["P", GTIN, 5, 3, TN]]}))
    assert not result["errors"]


def test_global_tnved_and_new_pn_confirmation(env):
    app, s, r = env
    add_doc(s)
    b = s.create("bob", "other", "Supplier B")
    p = s.preview(b, "bob", [["other", GTIN, 5, 2, "1234567890"]])
    assert p["errors"]
    raw = [["other", GTIN, 5, 2, TN]]
    with pytest.raises(Conflict):
        s.save_lines(b, "bob", raw, 0)
    p = s.preview(b, "bob", raw)
    assert p["warnings"]
    s.save_lines(b, "bob", raw, 0, p["fingerprint"])


def test_repacking_global_pool_and_latest_ai30(env):
    app, s, r = env
    a = add_doc(s, labels=10, quantity=5)
    s.order(a, "alice")
    advance(app)
    before = s.get(a, "alice")
    assert before["status"] == "ready" and len(before["codes"]) == 10
    first_ids = {c["id"] for c in before["codes"]}
    s.transferred(a, "alice", 1)
    raw = [["PN-A", GTIN, 10, 5, TN]]
    p = s.preview(a, "alice", raw)
    assert not p["changes"]  # total remains 50
    s.save_lines(a, "alice", raw, before["revision"], p["fingerprint"])
    d = s.get(a, "alice")
    assert (
        d["status"] == "ready"
        and d["current_version"] == 2
        and d["transferred_version"] is None
    )
    assert {c["id"] for c in d["codes"]} <= first_ids and all(
        c["quantity"] == 10 for c in d["codes"]
    )
    assert len(s.store.all("SELECT * FROM codes WHERE local_state='free'")) == 5
    b = add_doc(s, "bob", "Supplier B", labels=5, quantity=7, pn="OTHER")
    s.order(b, "bob")
    advance(app)
    assert len(r.orders) == 1  # second supplier uses released, paid-for codes
    second = s.get(b, "bob")
    assert second["status"] == "ready" and all(
        c["pn"] == "OTHER" for c in second["codes"]
    )
    assert s.store.one("SELECT COUNT(*) n FROM assignments WHERE active=1")["n"] == 10


def test_quantity_confirmation_sums_all_splits(env):
    _, s, _ = env
    a = add_doc(s, labels=2, quantity=5)
    raw = [["PN-A", GTIN, 5, 1, TN], ["PN-A", GTIN, 10, 1, TN]]
    p = s.preview(a, "alice", raw)
    assert p["changes"] == [{"pn": "PN-A", "before": 10, "after": 15, "difference": 5}]
    with pytest.raises(Conflict):
        s.save_lines(a, "alice", raw, 1)
    s.save_lines(a, "alice", raw, 1, p["fingerprint"])


def test_partial_codes_pdf_blocked_no_reemission(env):
    app, s, r = env
    r.partial = 2
    a = add_doc(s, labels=3)
    s.order(a, "alice")
    advance(app, 3)
    d = s.get(a, "alice")
    assert (
        d["status"] == "receiving" and len(d["codes"]) == 2 and not d["current_version"]
    )
    s.order(a, "alice")
    assert len(r.orders) == 1
    advance(app)
    assert s.get(a, "alice")["status"] == "ready"
    assert len(r.orders) == 1


def test_non_emitted_prevents_reallocation(env):
    app, s, r = env
    a = add_doc(s)
    s.order(a, "alice")
    advance(app)
    c = s.get(a, "alice")["codes"][0]
    r.codes[c["cis"]]["status"] = "APPLIED"
    raw = [["PN-A", GTIN, 15, 1, TN]]
    with pytest.raises(ValueError):
        s.save_lines(a, "alice", raw, 1)
    assert s.get(a, "alice")["current_version"] == 1


def test_race_and_double_click(env):
    app, s, r = env
    a = add_doc(s, labels=10)
    s.order(a, "alice")
    advance(app)
    raw = [["PN-A", GTIN, 50, 1, TN]]
    s.save_lines(a, "alice", raw, 1)
    assert (
        s.store.one("SELECT COUNT(*) n FROM codes WHERE local_state='free'")["n"] == 9
    )
    b = add_doc(s, "bob", "Supplier B", 9, 5, number="B")
    c = add_doc(s, "alice", "Supplier C", 9, 5, number="C")
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda args: s.order(*args), [(b, "bob"), (c, "alice")]))
    active = s.store.all(
        "SELECT code_id,COUNT(*) n FROM assignments WHERE active=1 GROUP BY code_id"
    )
    assert all(x["n"] == 1 for x in active)
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: s.order(c, "alice"), range(2)))
    assert (
        s.store.one(
            "SELECT COUNT(*) n FROM operations WHERE kind='order' AND doc_id=?", (c,)
        )["n"]
        <= 1
    )


def test_report_atk_and_exports_roundtrip(env):
    import openpyxl

    app, s, r = env
    ids = [
        add_doc(s, number="A1"),
        add_doc(s, number="A2"),
        add_doc(s, "bob", "Supplier B", number="B1"),
    ]
    for i, owner in zip(ids, ["alice", "alice", "bob"]):
        s.order(i, owner)
    advance(app)
    for i, owner in zip(ids, ["alice", "alice", "bob"]):
        s.transferred(i, owner, 1)
    batch = s.batch("administrator", ids, "2026-01-01", admin=True)
    assert s.batch("administrator", ids, "2026-01-01", admin=True) == batch
    advance(app)
    assert len(r.reports) == 3 and len(r.atks) == 2
    assert all(
        s.get(i, owner)["status"] == "done"
        for i, owner in zip(ids, ["alice", "alice", "bob"])
    )
    exports = s.store.all("SELECT * FROM artifacts WHERE kind='result'")
    assert len(exports) == 2
    for a in exports:
        wb = openpyxl.load_workbook(BytesIO(a["content"]))
        data = list(wb["Данные"].values)
        assert all(row[2] == 5 for row in data[1:])
        # OOXML GS encoding preserves full payload. Raw XML stores _x001D_.
        with zipfile.ZipFile(BytesIO(a["content"])) as z:
            assert b"_x001D_" in z.read("xl/sharedStrings.xml")
    a = s.get(ids[0], "alice")
    with pytest.raises(Conflict):
        s.save_lines(ids[0], "alice", [["PN-A", GTIN, 1, 1, TN]], 1, a["revision"])
    before = len(r.reports)
    advance(app)
    assert len(r.reports) == before


def test_atk_code_accepts_real_mixed_case_suffix_and_rejects_bad_formats():
    from marking_bm.worker import valid_atk_code

    assert valid_atk_code("12345678901226042028K0KvG")
    assert valid_atk_code("1234567890122604202800000")
    assert not valid_atk_code("12345678901226042028K0Kv")  # only 24 chars
    assert not valid_atk_code("12345678901226042028K0KvG0")  # 26 chars
    assert not valid_atk_code("A2345678901226042028K0KvG")  # prefix must be 20 digits
    assert not valid_atk_code("12345678901226042028K0K-G")  # punctuation is not valid
    assert not valid_atk_code(None)


def test_atk_existing_blocked_operation_recovers_without_second_document(env):
    # This matches a real CHECKED_OK document with 11 APPLIED KIs, a shared
    # mixed-case parent, and a previous local failure to confirm the ATK.
    app, service, remote = env
    doc_id = add_doc(service, labels=11)
    service.order(doc_id, "alice")
    advance(app)
    service.transferred(doc_id, "alice", 1)
    service.batch("alice", [doc_id], "2026-01-01", "123456789")

    for _ in range(24):
        sign_all(service)
        app.state.worker.tick()
        op = service.store.one("SELECT * FROM operations WHERE kind='atk'")
        if op and op["state"] == "submitted":
            break
    assert op is not None and op["state"] == "submitted"
    atk = next(iter(remote.atks))
    assert len(atk) == 25 and "K0KvG" in atk

    for ci in remote.codes.values():
        if ci.get("parent") == atk:
            ci["parent"] = None
    app.state.worker.tick()
    op = service.store.one("SELECT * FROM operations WHERE id=?", (op["id"],))
    assert op["state"] == "blocked"
    assert not service.store.one("SELECT code FROM atks WHERE op_id=?", (op["id"],))["code"]

    # The check route must only re-poll the same external document ID.
    for ci in remote.codes.values():
        if ci["status"] == "APPLIED":
            ci["parent"] = atk
    admin = client(app, "administrator", True)
    response = admin.post("/api/admin/operations/" + op["id"] + "/check", json={})
    assert response.status_code == 200, response.text
    app.state.worker.tick()
    saved = service.store.one("SELECT * FROM operations WHERE id=?", (op["id"],))
    assert saved["state"] == "done"
    assert saved["external_id"] == op["external_id"]
    assert service.store.one("SELECT code FROM atks WHERE op_id=?", (op["id"],))["code"] == atk
    assert service.get(doc_id, "alice")["status"] == "done"
    assert sum(path.endswith("/lk/documents/create") for _, path, _, _ in remote.calls) == 1


def test_supplier_error_isolation(env):
    app, s, r = env
    a = add_doc(s)
    b = add_doc(s, "bob", "Supplier B")
    for i, u in [(a, "alice"), (b, "bob")]:
        s.order(i, u)
    advance(app)
    for i, u in [(a, "alice"), (b, "bob")]:
        s.transferred(i, u, 1)
    s.batch("administrator", [a, b], "2026-01-01", admin=True)
    sign_all(s)
    app.state.worker.tick()
    op = s.store.one("SELECT * FROM operations WHERE kind='report' AND doc_id=?", (a,))
    r.fail_report.add(op["external_id"])
    advance(app)
    assert s.get(a, "alice")["status"] == "reporting"
    assert s.get(b, "bob")["status"] == "done"
    assert len(r.atks) == 1
    assert s.store.one("SELECT state FROM operations WHERE id=?", (op["id"],))["state"] == "rejected"
    r.fail_report.clear()
    c = client(app, "administrator", True)
    # A rejected report ID cannot become successful: a new signed report is required.
    assert c.post("/api/admin/operations/" + op["id"] + "/check", json={}).status_code == 409
    retried = c.post("/api/admin/operations/" + op["id"] + "/retry", json={})
    assert retried.status_code == 200, retried.text
    assert retried.json()["id"] != op["id"]
    advance(app)
    assert s.get(a, "alice")["status"] == "done" and len(r.reports) == 3


def test_timeout_never_reposts_and_restart_unknown(env):
    app, s, r = env
    r.timeout_order = True
    a = add_doc(s)
    s.order(a, "alice")
    advance(app)
    op = s.store.one("SELECT * FROM operations WHERE kind='order'")
    assert op["state"] == "unknown"
    app.state.worker.recover()
    advance(app)
    assert len(r.orders) == 1
    s.order(a, "alice")
    assert s.store.one("SELECT COUNT(*) n FROM operations WHERE kind='order'")["n"] == 1
    with s.store.transaction() as db:
        db.execute("UPDATE operations SET state='dispatching' WHERE id=?", (op["id"],))
    app.state.worker.recover()
    assert (
        s.store.one("SELECT state FROM operations WHERE id=?", (op["id"],))["state"]
        == "unknown"
    )


def test_consuming_get_timeout_stops_fetch(env):
    app, s, r = env
    r.timeout_codes = True
    a = add_doc(s)
    s.order(a, "alice")
    advance(app)
    assert (
        s.store.one("SELECT state FROM operations WHERE kind='fetch'")["state"]
        == "unknown"
    )
    n = len([c for c in r.calls if c[1].endswith("/codes")])
    advance(app)
    assert n == len([c for c in r.calls if c[1].endswith("/codes")]) == 1


def test_transformation_requires_two_decisions(env):
    app, s, r = env
    a = add_doc(s)
    s.order(a, "alice")
    advance(app)
    s.transferred(a, "alice", 1)
    s.batch("alice", [a], "2026-01-01")
    advance(app)
    atk = s.get(a, "alice")["atks"][0]
    code = s.get(a, "alice")["codes"][0]
    t = s.transform(atk["id"], "REMOVING", [code["id"]], "alice")
    advance(app)
    assert len(r.documents) == 1  # no submission before approval
    s.review(t, True, "Confirmed", "administrator")
    advance(app)
    c = s.store.one("SELECT * FROM codes WHERE id=?", (code["id"],))
    assert (
        c["external_status"] == "APPLIED"
        and c["local_state"] == "removed_review"
        and not c["reuse_allowed"]
    )
    with pytest.raises(Conflict):
        s.transform(atk["id"], "ADDING", [code["id"]], "alice")
    admin = client(app, "administrator", True)
    assert (
        admin.post(
            "/api/admin/codes/" + c["id"] + "/allow-reuse",
            json={"approve": True, "reason": "Checked removed label"},
        ).status_code
        == 200
    )
    t2 = s.transform(atk["id"], "ADDING", [code["id"]], "alice")
    advance(app)
    assert (
        s.store.one("SELECT atk_id FROM codes WHERE id=?", (code["id"],))["atk_id"]
        is None
    )
    s.review(t2, True, "Second separate approval", "administrator")
    advance(app)
    assert (
        s.store.one("SELECT atk_id FROM codes WHERE id=?", (code["id"],))["atk_id"]
        == atk["id"]
    )
    assert (
        s.store.one("SELECT version FROM atks WHERE id=?", (atk["id"],))["version"] == 3
    )


def test_archive_and_backup_restore(env, tmp_path):
    import subprocess, sys, sqlite3
    from datetime import date

    app, s, r = env
    a = add_doc(s)
    s.order(a, "alice")
    advance(app)
    s.transferred(a, "alice", 1)
    s.batch("alice", [a], "2026-01-01")
    advance(app)
    with s.store.transaction() as db:
        db.execute(
            "UPDATE documents SET completed=? WHERE id=?",
            ("2026-04-30T12:00:00+00:00", a),
        )
    s.archive(date(2026, 10, 29))
    assert s.get(a, "alice")["status"] == "done"
    s.archive(date(2026, 10, 30))
    assert s.get(a, "alice")["status"] == "archive"
    backup = tmp_path / "backup.sqlite3"
    restored = tmp_path / "restored.sqlite3"
    for src, dst, extra in [
        (s.store.path, backup, []),
        (backup, restored, ["--restore"]),
    ]:
        subprocess.run(
            [sys.executable, "scripts/backup.py", str(src), str(dst), *extra],
            check=True,
            capture_output=True,
        )
    with sqlite3.connect(restored) as db:
        assert (
            db.execute("SELECT status FROM documents WHERE id=?", (a,)).fetchone()[0]
            == "archive"
        )
        assert db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] >= 3


def test_recover_specific_consumed_block_without_second_fetch(env):
    app, s, r = env
    r.timeout_codes = True
    a = add_doc(s)
    s.order(a, "alice")
    advance(app)
    op = s.store.one("SELECT * FROM operations WHERE kind='fetch'")
    assert op["state"] == "unknown"
    admin = client(app, "administrator", True)
    candidates = admin.get("/api/admin/operations/" + op["id"] + "/blocks").json()
    assert len(candidates) == 1
    response = admin.post(
        "/api/admin/operations/" + op["id"] + "/recover-block",
        json={
            "block_id": candidates[0]["blockId"],
            "reason": "Matched order, quantity and time",
        },
    )
    assert response.status_code == 200, response.text
    advance(app)
    assert s.get(a, "alice")["status"] == "ready"
    assert len([c for c in r.calls if c[1].endswith("/api/v3/codes")]) == 1
    assert len(r.orders) == 1


def test_supplement_report_waits_for_approved_addition(env):
    app, s, r = env
    a = add_doc(s)
    s.order(a, "alice")
    advance(app)
    s.transferred(a, "alice", 1)
    s.batch("alice", [a], "2026-01-01")
    advance(app)
    parent = s.get(a, "alice")
    atk = parent["atks"][0]
    b = s.create("alice", "SUPPLEMENT", "ignored", parent_id=a)
    raw = [["PN-A", GTIN, 7, 2, TN]]
    s.save_lines(b, "alice", raw, 0)
    s.order(b, "alice")
    advance(app)
    s.transferred(b, "alice", 1)
    s.batch("alice", [b], "2026-01-01")
    advance(app)
    assert s.get(b, "alice")["status"] == "reported" and len(r.atks) == 1
    candidates = s.candidate_codes(atk["id"], "alice")
    assert len(candidates) == 2
    t = s.transform(atk["id"], "ADDING", [c["id"] for c in candidates], "alice")
    s.review(t, True, "Supplement applied", "administrator")
    advance(app)
    assert s.get(b, "alice")["status"] == "done"
    latest = s.store.one(
        "SELECT * FROM artifacts WHERE kind='result' ORDER BY created DESC LIMIT 1"
    )
    assert b in json.loads(latest["snapshot"])["documents"]
    assert "SUPPLEMENT" in latest["name"] and len(r.atks) == 1


def test_pdf_failure_preserves_received_codes(env):
    app, s, r = env
    a = add_doc(s, pn="W" * 120)
    s.order(a, "alice")
    advance(app)
    d = s.get(a, "alice")
    assert d["status"] == "label_error" and len(d["codes"]) == 3 and d["last_error"]
    assert (
        s.store.one("SELECT state FROM operations WHERE kind='fetch'")["state"]
        == "done"
    )
    raw = [["SHORT", GTIN, 5, 3, TN]]
    p = s.preview(a, "alice", raw)
    s.save_lines(a, "alice", raw, d["revision"], p["fingerprint"])
    assert s.get(a, "alice")["status"] == "ready" and len(r.orders) == 1


def test_two_full_tnved_create_distinct_atks_even_same_prefix(env):
    app, s, r = env
    other_gtin = "04640638349711"
    other_tn = "8536699009"
    a = s.create("alice", "TWO-TNVED", "Supplier")
    s.save_lines(
        a, "alice", [["P", GTIN, 2, 2, TN], ["Q", other_gtin, 7, 3, other_tn]], 0
    )
    s.order(a, "alice")
    advance(app)
    for ci in r.codes.values():
        if ci["gtin"] == other_gtin:
            ci["tnVedEaes"] = other_tn
    s.transferred(a, "alice", 1)
    s.batch("alice", [a], "2026-01-01")
    advance(app)
    d = s.get(a, "alice")
    assert d["status"] == "done" and len(d["atks"]) == 2
    assert {x["tnved"] for x in d["atks"]} == {TN, other_tn}
    # Two ATKs in the same supplier group share one ATK/RESULT workbook pair.
    with zipfile.ZipFile(
        BytesIO(s.logistics_download([a["id"] for a in d["atks"]]))
    ) as bundle:
        assert len(bundle.namelist()) == 2


def test_supplier_merge_preserves_finished_atks_and_files(env):
    app, s, r = env
    a = add_doc(s)
    s.order(a, "alice")
    advance(app)
    s.transferred(a, "alice", 1)
    s.batch("alice", [a], "2026-01-01")
    advance(app)
    b = add_doc(s, "bob", "Supplier B")
    old = s.get(a, "alice")
    target = s.get(b, "bob")["supplier_id"]
    admin = client(app, "administrator", True)
    response = admin.post(
        "/api/admin/suppliers",
        json={"id": old["supplier_id"], "name": "Supplier A", "merge_into": target},
    )
    assert response.status_code == 200, response.text
    new = s.get(a, "alice")
    assert (
        new["supplier_id"] == target
        and new["atks"][0]["code"] == old["atks"][0]["code"]
    )
    alice = client(app)
    assert alice.get("/api/files/" + old["exports"][0]["id"]).status_code == 200
    assert new["exports"] == old["exports"]


def test_renewal_and_signer_absence_preserve_queue(env):
    app, s, r = env
    a = add_doc(s)
    s.order(a, "alice")
    app.state.worker.tick()
    assert (
        s.store.one("SELECT state FROM operations WHERE kind='order'")["state"]
        == "signing"
    )
    assert not r.orders
    app.state.worker.recover()
    assert (
        s.store.one("SELECT state FROM operations WHERE kind='order'")["state"]
        == "signing"
    )
    # Token renewal only prepares signed authorization jobs.
    s.settings.save_token("true", "expired", "2000-01-01T00:00:00+00:00")
    app.state.worker.ensure_tokens()
    assert (
        s.store.one("SELECT state FROM operations WHERE kind='auth_true'")["state"]
        == "signing"
    )


def test_applied_code_database_guards_and_filename_collisions(env):
    from marking_bm.service import safe_name

    app, s, r = env
    a = add_doc(s)
    s.order(a, "alice")
    advance(app)
    s.transferred(a, "alice", 1)
    s.batch("alice", [a], "2026-01-01")
    advance(app)
    with pytest.raises(Exception):
        with s.store.transaction() as db:
            db.execute("UPDATE codes SET quantity=999 WHERE doc_id=?", (a,))
    assert len(safe_name("я" * 300).encode()) <= 170
    assert "/" not in safe_name("A/B")


def test_report_uses_admin_location_and_preserves_snapshot(env):
    from marking_bm.db import dumps

    app, s, registry = env
    doc_id = add_doc(s)
    s.order(doc_id, "alice")
    advance(app)
    s.transferred(doc_id, "alice", 1)
    fias = "11111111-2222-4333-8444-555555555555"
    with s.store.transaction() as db:
        for key, value in {"kpp": "987654321", "fias_id": fias}.items():
            db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, dumps(value)))
    c = client(app)
    # Old or modified clients cannot override the administrator's location.
    response = c.post("/api/batches", json={
        "documents": [doc_id], "production_date": "2026-01-01",
        "kpp": "111111111", "fias_id": "untrusted-client-value",
    })
    assert response.status_code == 200, response.text
    payload = json.loads(s.store.one("SELECT payload FROM operations WHERE kind='report'")["payload"])
    assert payload["attributes"] == {
        "productionDate": "2026-01-01", "kpp": "987654321", "fiasId": fias,
    }
    saved = s.get(doc_id, "alice")
    assert saved["kpp"] == "987654321" and saved["fias_id"] == fias
    with s.store.transaction() as db:
        db.execute("UPDATE settings SET value=? WHERE key='kpp'", (dumps("123456789"),))
    # Retry only needs the date; it does not rebuild the existing signed payload.
    retry = c.post("/api/batches", json={"documents": [doc_id], "production_date": "2026-01-01"})
    assert retry.status_code == 200 and retry.json() == response.json()
    advance(app)
    assert next(iter(registry.reports.values()))["attributes"] == payload["attributes"]


@pytest.mark.parametrize("kpp,fias", [("", ""), ("invalid", ""), ("123456789", "invalid")])
def test_report_requires_valid_admin_location_without_creating_operations(env, kpp, fias):
    from marking_bm.db import dumps

    app, s, _ = env
    doc_id = add_doc(s)
    s.order(doc_id, "alice")
    advance(app)
    s.transferred(doc_id, "alice", 1)
    with s.store.transaction() as db:
        for key, value in {"kpp": kpp, "fias_id": fias}.items():
            db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, dumps(value)))
    response = client(app).post("/api/batches", json={
        "documents": [doc_id], "production_date": "2026-01-01",
    })
    assert response.status_code == 422
    assert "админ" in response.json()["detail"].lower()
    assert s.store.one("SELECT COUNT(*) n FROM batches")["n"] == 0
    assert s.store.one("SELECT COUNT(*) n FROM operations WHERE kind='report'")["n"] == 0
    assert s.get(doc_id, "alice")["status"] == "transferred"


def test_report_date_only_request_allows_optional_fias_to_be_empty(env):
    app, s, _ = env
    doc_id = add_doc(s)
    s.order(doc_id, "alice")
    advance(app)
    s.transferred(doc_id, "alice", 1)
    response = client(app).post("/api/batches", json={
        "documents": [doc_id], "production_date": "2026-01-01",
    })
    assert response.status_code == 200, response.text
    payload = json.loads(s.store.one("SELECT payload FROM operations WHERE kind='report'")["payload"])
    assert payload["attributes"] == {"productionDate": "2026-01-01", "kpp": "123456789"}
