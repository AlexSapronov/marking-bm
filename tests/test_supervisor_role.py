"""Supervisor role combines the BM and logistics workspaces without admin access."""
import sqlite3

from marking_bm.db import Store
from conftest import client


def anonymous(app):
    from fastapi.testclient import TestClient

    c = TestClient(app)
    c.headers["X-Requested-With"] = "marking-bm"
    return c


def test_supervisor_can_view_all_invoices_and_download_only(env):
    app, service, _ = env
    admin = client(app, "administrator", True)
    created = admin.post(
        "/api/admin/users",
        json={"username": "team-lead", "role": "supervisor"},
    )
    assert created.status_code == 200, created.text
    supervisor = client(app, "team-lead")

    alice_doc = add_doc(service, "alice", "Supplier A", number="INV-ALICE")
    bob_doc = add_doc(service, "bob", "Supplier B", number="INV-BOB")

    listed = supervisor.get("/api/supervisor/documents").json()
    assert {d["id"] for d in listed} >= {alice_doc, bob_doc}
    assert {d["owner"] for d in listed if d["id"] in {alice_doc, bob_doc}} == {
        "alice", "bob"
    }
    detail = supervisor.get("/api/supervisor/documents/" + bob_doc)
    assert detail.status_code == 200, detail.text
    assert detail.json()["owner"] == "bob"
    assert detail.json()["number"] == "INV-BOB"

    # Normal BM endpoints are not available to a supervisor, even by direct API.
    assert supervisor.get("/api/documents").status_code == 403
    assert supervisor.get("/api/documents/" + bob_doc).status_code == 403
    assert supervisor.get("/api/template").status_code == 403
    assert supervisor.post(
        "/api/documents",
        json={"number": "FORBIDDEN", "supplier": "Supplier C"},
    ).status_code == 403
    assert supervisor.post(
        "/api/documents/" + bob_doc + "/order", json={}
    ).status_code == 403

    assert supervisor.get("/api/logistics/atks").status_code == 200
    assert supervisor.get("/api/admin/overview").status_code == 403
    assert supervisor.post(
        "/api/admin/users",
        json={"username": "attempted-admin-user", "role": "supervisor"},
    ).status_code == 403


def test_store_migrates_existing_role_constraint_without_losing_users(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        # Simulate a deployed database created before the supervisor role.
        db.execute(
            "CREATE TABLE users("
            "username TEXT PRIMARY KEY,"
            "enabled INTEGER NOT NULL DEFAULT 1,"
            "role TEXT NOT NULL DEFAULT 'manager' "
            "CHECK(role IN ('manager','logistics')))"
        )
        db.execute(
            "INSERT INTO users(username, enabled, role) VALUES(?,?,?)",
            ("existing-manager", 1, "manager"),
        )
        db.execute(
            "INSERT INTO users(username, enabled, role) VALUES(?,?,?)",
            ("existing-logist", 1, "logistics"),
        )

    store = Store(path)
    with store.transaction() as db:
        db.execute(
            "INSERT INTO users(username, enabled, role) VALUES(?,?,?)",
            ("new-supervisor", 1, "supervisor"),
        )
        users = {
            row["username"]: row["role"]
            for row in db.execute("SELECT username, role FROM users")
        }
        assert users == {
            "existing-manager": "manager",
            "existing-logist": "logistics",
            "new-supervisor": "supervisor",
        }
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute(
            "SELECT 1 FROM schema_version WHERE version=5"
        ).fetchone()
