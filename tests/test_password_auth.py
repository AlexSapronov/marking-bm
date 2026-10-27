"""Password authentication and administrator-issued one-time enrollment."""
import time

from fastapi.testclient import TestClient

from conftest import client
from marking_bm.auth import verify_password


def anonymous(app):
    c = TestClient(app)
    c.headers["X-Requested-With"] = "marking-bm"
    return c


def test_user_cannot_log_in_without_password_or_take_over_setup(env):
    app, service, _ = env
    c = anonymous(app)
    assert c.post("/api/session", json={"username": "alice"}).status_code == 403
    assert c.post(
        "/api/session", json={"username": "alice", "password": "wrong"}
    ).status_code == 403
    assert c.post(
        "/api/users/set-password",
        json={
            "username": "alice", "code": "made-up", "password": "abcdefgh",
            "confirmation": "abcdefgh",
        },
    ).status_code == 403
    assert c.get("/api/documents").status_code == 401


def test_admin_creates_user_and_employee_sets_password_with_confirmation(env):
    app, service, _ = env
    admin = client(app, "administrator", True)
    created = admin.post(
        "/api/admin/users", json={"username": "new-person", "role": "logistics"}
    )
    assert created.status_code == 200, created.text
    code = created.json()["setup"]["code"]
    assert len(code) >= 24
    overview = admin.get("/api/admin/overview").json()["users"]
    registered = next(u for u in overview if u["username"] == "new-person")
    assert registered["password_set"] == 0
    assert registered["setup_pending"] == 1
    assert "password_hash" not in registered
    assert "setup_token_hash" not in registered
    assert "setup_expires" not in registered

    employee = anonymous(app)
    assert employee.post(
        "/api/session", json={"username": "new-person", "password": code}
    ).status_code == 403
    body = {
        "username": "new-person", "code": code,
        "password": "пароль123", "confirmation": "другое123",
    }
    assert employee.post("/api/users/set-password", json=body).status_code == 422
    assert employee.post(
        "/api/users/set-password",
        json={**body, "password": "abcdefg", "confirmation": "abcdefg"},
    ).status_code == 422
    done = employee.post(
        "/api/users/set-password",
        json={**body, "confirmation": "пароль123"},
    )
    assert done.status_code == 200, done.text
    assert employee.post(
        "/api/users/set-password",
        json={**body, "confirmation": "пароль123"},
    ).status_code == 403
    assert employee.post(
        "/api/session", json={"username": "new-person", "password": "wrongpass"}
    ).status_code == 403
    signed = employee.post(
        "/api/session", json={"username": "new-person", "password": "пароль123"}
    )
    assert signed.status_code == 200, signed.text
    assert signed.json()["role"] == "logistics"
    employee.headers["X-CSRF-Token"] = signed.json()["csrf"]
    assert employee.get("/api/logistics/atks").status_code == 200
    assert employee.get("/api/admin/overview").status_code == 403
    record = service.store.one("SELECT * FROM users WHERE username='new-person'")
    assert verify_password("пароль123", record["password_hash"])
    assert record["setup_token_hash"] is None
    assert record["setup_expires"] is None


def test_admin_reset_revokes_sessions_then_user_sets_new_password(env):
    app, service, _ = env
    alice = client(app, "alice")
    admin = client(app, "administrator", True)
    assert alice.get("/api/session").status_code == 200
    reset = admin.post("/api/admin/users/alice/reset-password", json={})
    assert reset.status_code == 200
    code = reset.json()["code"]
    assert alice.get("/api/session").status_code == 401
    anonymous_user = anonymous(app)
    assert anonymous_user.post(
        "/api/session",
        json={"username": "alice", "password": "test-password-long"},
    ).status_code == 403
    assert anonymous_user.post(
        "/api/users/set-password",
        json={
            "username": "alice", "code": code,
            "password": "abcdefgh", "confirmation": "abcdefgh",
        },
    ).status_code == 200
    assert anonymous_user.post(
        "/api/session",
        json={"username": "alice", "password": "abcdefgh"},
    ).status_code == 200
    # No case, digit or special-character composition rules.
    assert verify_password(
        "abcdefgh", service.store.one(
            "SELECT password_hash FROM users WHERE username='alice'"
        )["password_hash"]
    )
    # Users cannot reset somebody else's password or list setup codes.
    assert anonymous_user.post(
        "/api/admin/users/bob/reset-password", json={},
    ).status_code == 403


def test_employee_change_password_checks_old_and_confirmation(env):
    app, service, _ = env
    alice = client(app, "alice")
    old = {
        "current_password": "wrong",
        "password": "abcdefgh", "confirmation": "abcdefgh",
    }
    assert alice.post("/api/users/change-password", json=old).status_code == 403
    assert alice.post(
        "/api/users/change-password",
        json={**old, "current_password": "test-password-long",
              "confirmation": "abcdefgh1"},
    ).status_code == 422
    assert alice.post(
        "/api/users/change-password",
        json={**old, "current_password": "test-password-long"},
    ).status_code == 200
    fresh = anonymous(app)
    assert fresh.post(
        "/api/session", json={"username": "alice", "password": "test-password-long"}
    ).status_code == 403
    assert fresh.post(
        "/api/session", json={"username": "alice", "password": "abcdefgh"}
    ).status_code == 200


def test_reset_code_expiry_and_disabled_users_are_rejected(env):
    app, service, _ = env
    admin = client(app, "administrator", True)
    result = admin.post("/api/admin/users/bob/reset-password", json={})
    code = result.json()["code"]
    with service.store.transaction() as db:
        db.execute(
            "UPDATE users SET setup_expires=? WHERE username='bob'",
            (time.time() - 60,),
        )
    attempt = anonymous(app).post(
        "/api/users/set-password",
        json={"username": "bob", "code": code,
              "password": "abcdefgh", "confirmation": "abcdefgh"},
    )
    assert attempt.status_code == 403
    second = admin.post("/api/admin/users/bob/reset-password", json={})
    code = second.json()["code"]
    admin.post("/api/admin/users", json={"username": "bob", "enabled": False})
    assert anonymous(app).post(
        "/api/users/set-password",
        json={"username": "bob", "code": code,
              "password": "abcdefgh", "confirmation": "abcdefgh"},
    ).status_code == 403


def test_five_failed_employee_logins_rate_limited(env):
    app, service, _ = env
    c = anonymous(app)
    for _ in range(5):
        assert c.post(
            "/api/session",
            json={"username": "alice", "password": "wrongpass"},
        ).status_code == 403
    assert c.post(
        "/api/session",
        json={"username": "alice", "password": "test-password-long"},
    ).status_code == 429
    assert service.store.one(
        "SELECT failures FROM login_attempts WHERE ip=?", ("employee:testclient",)
    )["failures"] == 5
