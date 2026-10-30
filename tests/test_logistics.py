"""Logist registry and local invoice completion; runtime has no mock/demo mode."""

from datetime import date
from io import BytesIO
import sqlite3
import zipfile
import pytest
from marking_bm.db import Store
from conftest import add_doc, advance, client, TN


def prepare_atks(env):
    app, s, r = env
    ids = [
        add_doc(s, "alice", "Supplier A", number="AL-100"),
        add_doc(s, "bob", "Supplier B", number="BO-200"),
    ]
    for doc, owner in zip(ids, ["alice", "bob"]):
        s.order(doc, owner)
    advance(app)
    for doc, owner in zip(ids, ["alice", "bob"]):
        s.transferred(doc, owner, 1)
    s.batch("administrator", ids, "2026-01-01", admin=True)
    advance(app)
    admin = client(app, "administrator", True)
    response = admin.post(
        "/api/admin/users",
        json={"username": "logist", "role": "logistics", "enabled": True},
    )
    assert response.status_code == 200, response.text
    return ids, client(app, "logist"), admin


def test_logist_sees_all_owners_filters_and_downloads(env):
    app, s, _ = env
    ids, c, _ = prepare_atks(env)
    assert c.get("/api/session").json()["role"] == "logistics"
    items = c.get("/api/logistics/atks").json()
    assert len(items) == 2 and {owner for a in items for owner in a["owners"]} == {
        "alice",
        "bob",
    }
    first = next(a for a in items if a["invoices"] == ["AL-100"])
    assert first["units"] == 15 and first["labels"] == 3 and first["download_ready"]
    for query in ["q=AL-100", "q=" + first["code"], "supplier=" + first["supplier_id"]]:
        assert [a["id"] for a in c.get("/api/logistics/atks?" + query).json()] == [
            first["id"]
        ]
    assert len(c.get("/api/logistics/atks?status=done&tnved=" + TN).json()) == 2
    assert c.get("/api/logistics/atks?status=processing").json() == []
    assert c.get("/api/logistics/atks?date_from=2100-01-01").json() == []
    assert c.get("/api/logistics/atks?date_to=2000-01-01").json() == []
    detail = c.get("/api/logistics/atks/" + first["id"]).json()
    assert len(detail["codes"]) == 3 and "raw" not in detail["codes"][0]
    for f in first["files"]:
        response = c.get("/api/files/" + f["id"])
        assert response.status_code == 200
        assert zipfile.is_zipfile(BytesIO(response.content))  # XLSX
    response = c.post(
        "/api/logistics/download", json={"atks": [a["id"] for a in items]}
    )
    assert response.status_code == 200
    assert "AL-100-BO-200.zip" in response.headers["content-disposition"]
    with zipfile.ZipFile(BytesIO(response.content)) as z:
        names = z.namelist()
        assert len(names) == 4
        assert all(name.endswith(".xlsx") and "/" not in name for name in names)
        assert sum("AL-100" in name for name in names) == 2
        assert sum("BO-200" in name for name in names) == 2
        assert all(z.read(name) for name in names)
    # The same group cannot be downloaded twice when several of its ATKs
    # are selected. A single-invoice download keeps the invoice in its name.
    individual = c.post("/api/logistics/download", json={"atks": [first["id"]]})
    assert individual.status_code == 200
    assert "AL-100.zip" in individual.headers["content-disposition"]
    with zipfile.ZipFile(BytesIO(individual.content)) as z:
        assert len(z.namelist()) == 2
        assert all("/" not in name for name in z.namelist())


def test_supervisor_can_view_invoices_download_atks_but_not_complete(env):
    app, s, _ = env
    ids, _, admin = prepare_atks(env)
    changed = admin.post(
        "/api/admin/users",
        json={"username": "logist", "role": "supervisor"},
    )
    assert changed.status_code == 200, changed.text

    supervisor = client(app, "logist")
    assert supervisor.get("/api/session").json()["role"] == "supervisor"
    assert supervisor.get("/api/documents").status_code == 403
    listed = supervisor.get("/api/supervisor/documents").json()
    assert {d["id"] for d in listed} >= set(ids)
    assert supervisor.get("/api/supervisor/documents/" + ids[1]).status_code == 200

    items = supervisor.get("/api/logistics/atks").json()
    assert len(items) == 2
    for file in items[0]["files"]:
        response = supervisor.get("/api/files/" + file["id"])
        assert response.status_code == 200, response.text

    # Invoice-specific artifacts remain unavailable in the BM view.
    label = s.store.one(
        "SELECT id FROM artifacts WHERE doc_id=? AND kind='labels'", (ids[0],)
    )
    assert label is not None
    assert supervisor.get("/api/files/" + label["id"]).status_code == 403
    # Even a legacy document assigned to this username must not grant access
    # to invoice-specific files through the supervisor role.
    with s.store.transaction() as db:
        db.execute("UPDATE documents SET owner='logist' WHERE id=?", (ids[0],))
    assert supervisor.get("/api/files/" + label["id"]).status_code == 403

    # Downloading is allowed, but completing logistics work is not.
    download = supervisor.post(
        "/api/logistics/download", json={"atks": [a["id"] for a in items]}
    )
    assert download.status_code == 200, download.text
    assert supervisor.post(
        "/api/logistics/documents/" + ids[0] + "/complete", json={}
    ).status_code == 403

    assert supervisor.get("/api/admin/overview").status_code == 403
    assert supervisor.post(
        "/api/admin/users",
        json={"username": "blocked-user", "role": "manager"},
    ).status_code == 403


def test_logistics_export_preserves_colliding_filenames_flat(env, monkeypatch):
    app, service, _ = env
    _, logistics, _ = prepare_atks(env)
    atks = logistics.get("/api/logistics/atks").json()
    real_summary = service._logistics_summary

    def same_file_names(db, atk):
        item = real_summary(db, atk)
        for file in item["files"]:
            file["name"] = file["kind"].upper() + ".xlsx"
        return item

    monkeypatch.setattr(service, "_logistics_summary", same_file_names)
    result = logistics.post(
        "/api/logistics/download", json={"atks": [item["id"] for item in atks]}
    )
    assert result.status_code == 200, result.text
    with zipfile.ZipFile(BytesIO(result.content)) as bundle:
        assert sorted(bundle.namelist()) == [
            "ATK-2.xlsx", "ATK.xlsx", "RESULT-2.xlsx", "RESULT.xlsx"
        ]
        assert all(bundle.read(name) for name in bundle.namelist())


def test_logist_cannot_mutate_or_download_labels_or_source(env):
    app, s, _ = env
    ids, c, _ = prepare_atks(env)
    a = s.get(ids[0], "alice")["atks"][0]
    routes = [
        ("/api/documents", {"number": "BAD", "supplier": "Supplier A"}),
        ("/api/documents/" + ids[0] + "/order", {}),
        ("/api/documents/" + ids[0] + "/lines", {"rows": []}),
        ("/api/documents/" + ids[0] + "/preview", {"rows": []}),
        ("/api/documents/" + ids[0] + "/transferred", {"version": 1}),
        (
            "/api/batches",
            {"documents": ids, "production_date": "2026-01-01", "kpp": "123456789"},
        ),
        ("/api/atks/" + a["id"] + "/transform", {"kind": "REMOVING", "codes": []}),
        ("/api/admin/users", {"username": "hacker", "role": "manager"}),
    ]
    for route, body in routes:
        assert c.post(route, json=body).status_code == 403, route
    for route in [
        "/api/documents",
        "/api/documents/" + ids[0],
        "/api/template",
        "/api/atks/" + a["id"],
        "/api/atks/" + a["id"] + "/candidates",
        "/api/admin/overview",
    ]:
        assert c.get(route).status_code == 403, route
    label = s.store.one(
        "SELECT id FROM artifacts WHERE doc_id=? AND kind='labels'", (ids[0],)
    )
    assert c.get("/api/files/" + label["id"]).status_code == 403
    assert c.post("/api/signer/next", json={}).status_code == 403
    assert c.get("/api/logistics/atks/not-existing").status_code == 403


def test_manager_cannot_assign_role_or_access_logist_registry(env):
    app, s, _ = env
    c = client(app, "alice")
    for route in ["/api/logistics/atks", "/api/logistics/atks/unknown"]:
        assert c.get(route).status_code == 403
    assert (
        c.post("/api/logistics/download", json={"atks": ["unknown"]}).status_code == 403
    )
    assert (
        c.post(
            "/api/admin/users", json={"username": "alice", "role": "logistics"}
        ).status_code
        == 403
    )
    response = c.post(
        "/api/session",
        json={"username": "alice", "password": "test-password-long", "role": "logistics"},
    )
    assert response.status_code == 200 and response.json()["role"] == "manager"


def test_role_change_invalidates_sessions_and_toggle_preserves_role(env):
    app, s, _ = env
    admin = client(app, "administrator", True)
    alice = client(app)
    assert (
        admin.post(
            "/api/admin/users", json={"username": "alice", "role": "logistics"}
        ).status_code
        == 200
    )
    assert alice.get("/api/session").status_code == 401
    c = client(app)
    assert c.get("/api/session").json()["role"] == "logistics"
    assert (
        admin.post(
            "/api/admin/users", json={"username": "alice", "enabled": False}
        ).status_code
        == 200
    )
    assert (
        s.store.one("SELECT role FROM users WHERE username=?", ("alice",))["role"]
        == "logistics"
    )
    assert c.get("/api/session").status_code == 401
    assert (
        admin.post(
            "/api/admin/users", json={"username": "alice", "enabled": True}
        ).status_code
        == 200
    )
    assert client(app).get("/api/session").json()["role"] == "logistics"
    assert (
        admin.post(
            "/api/admin/users", json={"username": "alice", "role": "admin"}
        ).status_code
        == 422
    )


def test_legacy_database_migrates_roles_without_losing_users(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE users(username TEXT PRIMARY KEY,enabled INTEGER NOT NULL DEFAULT 1)"
        )
        db.execute("INSERT INTO users VALUES('old-manager',1)")
    for _ in range(2):
        store = Store(path)
        assert store.one("SELECT * FROM users")["role"] == "manager"
        assert store.one("SELECT MAX(version) v FROM schema_version")["v"] == 6
        assert store.one("SELECT password_hash FROM users WHERE username='old-manager'")["password_hash"] is None
        assert store.one("SELECT setup_token_hash FROM users WHERE username='old-manager'")["setup_token_hash"] is None
    with store.transaction() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE users SET role='admin'")


def test_registry_archive_and_pending_groups(env):
    app, s, _ = env
    ids, c, _ = prepare_atks(env)
    with s.store.transaction() as db:
        db.execute(
            "UPDATE documents SET completed=? WHERE id=?",
            ("2025-01-01T00:00:00+00:00", ids[0]),
        )
    s.archive(date(2026, 1, 1))
    assert len(c.get("/api/logistics/atks").json()) == 1
    archived = c.get("/api/logistics/atks?archive=true").json()
    assert len(archived) == 1 and archived[0]["invoices"] == ["AL-100"]
    assert c.get("/api/files/" + archived[0]["files"][0]["id"]).status_code == 200
    # Pending actual ATK records are visible but do not pretend to have downloadable files.
    d = add_doc(s, "bob", "Supplier C", number="PENDING")
    s.order(d, "bob")
    advance(app)
    s.transferred(d, "bob", 1)
    s.batch("bob", [d], "2026-01-01")
    for _ in range(3):
        from conftest import sign_all

        sign_all(s)
        app.state.worker.tick()
    # Freeze newly created ATK before the signed mutation is dispatched.
    with s.store.transaction() as db:
        db.execute(
            "UPDATE operations SET state='signing' WHERE kind='atk' AND state!='done' AND external_id IS NULL"
        )
    pending = s.store.one(
        "SELECT * FROM atks WHERE batch_id=(SELECT batch_id FROM batch_docs WHERE doc_id=?)",
        (d,),
    )
    assert pending and pending["state"] != "done"
    if pending:
        item = c.get("/api/logistics/atks/" + pending["id"]).json()
        assert not item["download_ready"]
        assert (
            c.post(
                "/api/logistics/download", json={"atks": [pending["id"]]}
            ).status_code
            == 409
        )


def introduction_body(logist, doc_id):
    scope = logist.get("/api/logistics/documents/" + doc_id + "/introduction")
    assert scope.status_code == 200, scope.text
    scope = scope.json()
    return {"declaration_number": "10000000/011026/0000001", "declaration_date": "2026-10-01",
            "countries": {l["id"]: "156" for l in scope["lines"]}, "snapshot": scope["snapshot"]}


def test_introduction_archives_only_after_gis_confirmation(env):
    app, s, remote = env
    ids, logist, _ = prepare_atks(env)
    body = introduction_body(logist, ids[0])
    endpoint = "/api/logistics/documents/" + ids[0] + "/introduce"
    response = logist.post(endpoint, json=body)
    assert response.status_code == 200, response.text
    assert s.get(ids[0], "alice")["status"] == "done"
    assert logist.post(endpoint, json=body).status_code == 409
    atk = s.get(ids[0], "alice")["atks"][0]
    code = s.store.one("SELECT id FROM codes WHERE doc_id=?", (ids[0],))
    with pytest.raises(ValueError, match="вводом в оборот"):
        s.transform(atk["id"], "REMOVING", [code["id"]], "alice")
    advance(app)
    assert s.get(ids[0], "alice")["status"] == "archive"
    assert s.get(ids[1], "bob")["status"] == "done"
    op = s.store.one("SELECT * FROM operations WHERE id=?", (response.json()["id"],))
    assert op["state"] == "done" and op["external_id"]
    assert all(c["external_status"] == "INTRODUCED" for c in s.store.all("SELECT * FROM codes WHERE doc_id=?", (ids[0],)))
    assert s.store.one("SELECT op_id FROM receipts WHERE op_id=?", (op["id"],))
    sent = [c[2] for c in remote.calls if c[1].endswith("/lk/documents/create")]
    assert sent[-1]["type"] == "LP_FTS_INTRODUCE"
    import base64
    import json
    payload = json.loads(base64.b64decode(sent[-1]["product_document"]))
    assert all(c["tn_ved"] == TN and c["production_country_code"] == "156" for a in payload["products_list"] for c in a["children"])
    assert logist.post("/api/logistics/documents/" + ids[1] + "/complete", json={}).status_code == 409


def test_only_logist_or_admin_can_introduce_and_csrf_is_required(env):
    app, s, _ = env
    ids, logist, admin = prepare_atks(env)
    endpoint = "/api/logistics/documents/" + ids[0] + "/introduce"
    body = introduction_body(logist, ids[0])
    assert client(app).post(endpoint, json=body).status_code == 403
    token = logist.headers.pop("X-CSRF-Token")
    assert logist.post(endpoint, json=body).status_code == 403
    logist.headers["X-CSRF-Token"] = token
    assert admin.post(endpoint, json=body).status_code == 200


@pytest.mark.parametrize("change", ["date", "number", "country", "missing_country", "snapshot"])
def test_introduction_validates_before_queueing(env, change):
    app, s, _ = env
    ids, logist, _ = prepare_atks(env)
    body = introduction_body(logist, ids[0])
    if change == "date": body["declaration_date"] = "2026-10-02"
    if change == "number": body["declaration_number"] = "bad"
    if change == "country": body["countries"] = {k: "CN" for k in body["countries"]}
    if change == "missing_country": body["countries"] = {}
    if change == "snapshot": body["snapshot"] = "0" * 64
    response = logist.post("/api/logistics/documents/" + ids[0] + "/introduce", json=body)
    assert response.status_code in (422, 409), response.text
    assert not s.store.all("SELECT id FROM operations WHERE kind='introduce'")


@pytest.mark.parametrize("block", ["draft", "pending_transform", "missing_files", "unfinished_atk", "unfinished_sibling_atk"])
def test_completion_rejects_unfinished_or_incomplete_invoice(env, block):
    app, s, remote = env
    ids, logist, _ = prepare_atks(env)
    target = ids[0]
    atk = s.get(target, "alice")["atks"][0]
    if block == "draft":
        target = add_doc(s, number="NOT-READY")
    elif block == "pending_transform":
        code = s.store.one("SELECT id FROM codes WHERE atk_id=?", (atk["id"],))
        s.transform(atk["id"], "REMOVING", [code["id"]], "alice")
    elif block == "missing_files":
        # Add a newer incomplete export version; historical files stay immutable.
        with s.store.transaction() as db:
            db.execute("INSERT INTO artifacts SELECT 'incomplete',doc_id,group_id,version+1,kind,name,content,sha256,snapshot,created FROM artifacts WHERE group_id=? AND kind='atk' LIMIT 1", (atk["batch_id"] + ":" + atk["supplier_id"],))
    elif block == "unfinished_sibling_atk":
        with s.store.transaction() as db:
            db.execute("INSERT INTO atks SELECT 'pending-sibling',NULL,supplier_id,'1234567890',batch_id,version,op_id,'processing' FROM atks WHERE id=?", (atk["id"],))
    else:
        with s.store.transaction() as db:
            db.execute("UPDATE atks SET state='processing' WHERE id=?", (atk["id"],))
    calls = len(remote.calls)
    response = logist.post("/api/logistics/documents/" + target + "/complete", json={})
    assert response.status_code == 409, response.text
    assert s.get(target, "alice")["status"] != "archive"
    assert len(remote.calls) == calls


def test_shared_atk_introduces_all_connected_invoices_together(env):
    app, s, _ = env
    _, logist, _ = prepare_atks(env)
    ids = [add_doc(s, number="SHARED-1", supplier="Shared"), add_doc(s, owner="bob", number="SHARED-2", supplier="Shared")]
    for doc_id, owner in zip(ids, ["alice", "bob"]):
        s.order(doc_id, owner)
    advance(app)
    for doc_id, owner in zip(ids, ["alice", "bob"]):
        s.transferred(doc_id, owner, 1)
    s.batch("administrator", ids, "2026-01-01", admin=True)
    advance(app)
    body = introduction_body(logist, ids[0])
    assert len(body["countries"]) == 2
    values = list(body["countries"])
    body["countries"][values[1]] = "158"
    response = logist.post("/api/logistics/documents/" + ids[0] + "/introduce", json=body)
    assert response.status_code == 200, response.text
    advance(app)
    assert all(s.store.one("SELECT status FROM documents WHERE id=?", (i,))["status"] == "archive" for i in ids)
    archived = logist.get("/api/logistics/atks?archive=true&q=SHARED").json()
    assert len(archived) == 1 and set(archived[0]["invoices"]) == {"SHARED-1", "SHARED-2"}


@pytest.mark.parametrize("outcome", ["waiting", "negative", "partial", "foreign_owner", "document_rejected"])
def test_fts_does_not_archive_without_every_code_confirmed(env, outcome):
    from conftest import sign_all
    app, s, remote = env
    ids, logist, _ = prepare_atks(env)
    body = introduction_body(logist, ids[0])
    response = logist.post("/api/logistics/documents/" + ids[0] + "/introduce", json=body)
    op_id = response.json()["id"]
    sign_all(s)
    app.state.worker.dispatch(s.store.one("SELECT * FROM operations WHERE id=?", (op_id,)))
    codes = s.store.all("SELECT cis FROM codes WHERE doc_id=?", (ids[0],))
    for index, code in enumerate(codes):
        ci = remote.codes[code["cis"]]
        if outcome in ("waiting", "negative") or outcome == "partial" and index == 0:
            ci["status"] = "APPLIED"
        if outcome == "negative": ci["statusEx"] = "FTS_RESPOND_NOT_OK"
        if outcome == "foreign_owner": ci["ownerInn"] = "other"
    op = s.store.one("SELECT * FROM operations WHERE id=?", (op_id,))
    if outcome == "document_rejected":
        remote.documents[op["external_id"]]["status"] = "CHECKED_NOT_OK"
    advance(app, 2)
    assert s.get(ids[0], "alice")["status"] == "done"
    assert s.store.one("SELECT state FROM operations WHERE id=?", (op_id,))["state"] != "done"
    assert not s.store.one("SELECT op_id FROM receipts WHERE op_id=?", (op_id,))


def test_lost_fts_response_is_never_sent_again(env):
    import httpx
    app, s, remote = env
    ids, logist, _ = prepare_atks(env)
    body = introduction_body(logist, ids[0])
    original = remote.handle
    def lost_response(request):
        result = original(request)
        if request.url.path.endswith("/lk/documents/create"):
            raise httpx.ReadTimeout("lost after acceptance", request=request)
        return result
    s.gateway.transport = httpx.MockTransport(lost_response)
    response = logist.post("/api/logistics/documents/" + ids[0] + "/introduce", json=body)
    calls = len([c for c in remote.calls if c[1].endswith("/lk/documents/create")])
    advance(app)
    assert s.store.one("SELECT state FROM operations WHERE id=?", (response.json()["id"],))["state"] == "unknown"
    assert len([c for c in remote.calls if c[1].endswith("/lk/documents/create")]) == calls + 1
    assert s.get(ids[0], "alice")["status"] == "done"
    assert logist.post("/api/logistics/documents/" + ids[0] + "/introduce", json=body).status_code == 409


@pytest.mark.parametrize("problem", ["missing_local", "different_registry"])
def test_fts_requires_explicit_matching_tnved_for_every_code(env, problem):
    app, s, remote = env
    ids, logist, _ = prepare_atks(env)
    body = introduction_body(logist, ids[0])
    if problem == "missing_local":
        # Production makes APPLIED assignments immutable; simulate a damaged
        # legacy line, not a legal edit to an already-applied code.
        with s.store.transaction() as db:
            db.execute("UPDATE lines SET tnved='' WHERE doc_id=?", (ids[0],))
        assert logist.post("/api/logistics/documents/" + ids[0] + "/introduce", json=body).status_code == 409
        assert not s.store.all("SELECT id FROM operations WHERE kind='introduce'")
    else:
        for c in s.store.all("SELECT cis FROM codes WHERE doc_id=?", (ids[0],)):
            remote.codes[c["cis"]]["tnVedEaes"] = "1234567890"
        response = logist.post("/api/logistics/documents/" + ids[0] + "/introduce", json=body)
        advance(app)
        op = s.store.one("SELECT * FROM operations WHERE id=?", (response.json()["id"],))
        assert op["state"] == "preflight_blocked"
        assert "ТН ВЭД" in op["error"] and not op["external_id"]
        assert s.get(ids[0], "alice")["status"] == "done"
