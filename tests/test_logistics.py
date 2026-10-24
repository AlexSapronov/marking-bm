"""Logist role is read-only across all owners; runtime has no mock/demo mode."""

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
    s.batch("administrator", ids, "2026-01-01", "123456789", admin=True)
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
    with zipfile.ZipFile(BytesIO(response.content)) as z:
        assert len(z.namelist()) == 4 and all(
            name.endswith(".xlsx") for name in z.namelist()
        )


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
    response = c.post("/api/session", json={"username": "alice", "role": "logistics"})
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
        assert store.one("SELECT MAX(version) v FROM schema_version")["v"] == 2
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
    s.batch("bob", [d], "2026-01-01", "123456789")
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
