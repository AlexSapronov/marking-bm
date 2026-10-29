"""Supervisor role combines the BM and logistics workspaces without admin access."""
import sqlite3

from marking_bm.db import Store
from conftest import client


def anonymous(app):
    from fastapi.testclient import TestClient

    c = TestClient(app)
    c.headers["X-Requested-With"] = "marking-bm"
    return c


def test_supervisor_can_use_bm_and_logistics_but_not_admin(env):
    app, service, _ = env
    admin = client(app, "administrator", True)
    created = admin.post(
        "/api/admin/users",
        json={"username": "team-lead", "role": "supervisor"},
    )
    assert created.status_code == 200, created.text
    setup_code = created.json()["setup"]["code"]

    employee = anonymous(app)
    setup = employee.post(
        "/api/users/set-password",
        json={
            "username": "team-lead",
            "code": setup_code,
            "password": "leader-pass-8",
            "confirmation": "leader-pass-8",
        },
    )
    assert setup.status_code == 200, setup.text

    signed = employee.post(
        "/api/session",
        json={"username": "team-lead", "password": "leader-pass-8"},
    )
    assert signed.status_code == 200, signed.text
    assert signed.json()["role"] == "supervisor"
    employee.headers["X-CSRF-Token"] = signed.json()["csrf"]

    # The supervisor gets the same BM workspace/API as a manager and the
    # shared logistics registry/API, but is never elevated to administrator.
    assert employee.get("/api/documents").status_code == 200
    assert employee.get("/api/template").status_code == 200
    assert employee.get("/api/logistics/atks").status_code == 200
    assert employee.get("/api/admin/overview").status_code == 403
    assert employee.get("/api/admin/documents").status_code == 403
    assert employee.post(
        "/api/admin/users",
        json={"username": "attempted-admin-user", "role": "supervisor"},
    ).status_code == 403

    created_document = employee.post(
        "/api/documents",
        json={"number": "LEAD-001", "supplier": "Supervisor test supplier"},
    )
    assert created_document.status_code == 200, created_document.text
    documents = employee.get("/api/documents").json()
    assert any(d["number"] == "LEAD-001" for d in documents)
    assert service.store.one(
        "SELECT role FROM users WHERE username='team-lead'"
    )["role"] == "supervisor"


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
