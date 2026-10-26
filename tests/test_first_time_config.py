"""Initial organization setup must not deadlock behind an unsubmitted authorization."""
from uuid import uuid4

from conftest import client
from marking_bm.db import dumps


def _body():
    return {
        "organization": "Real company",
        "inn": "7800000000",
        "oms_id": str(uuid4()),
        "oms_connection": str(uuid4()),
        "template_id": 36,
        "kpp": "",
        "fias_id": "",
    }


def _pending(s, kind="auth_true", state="signing", external_id=None):
    with s.store.transaction() as db:
        op = s.queue(db, kind, f"setup:{uuid4()}", {"uuid": str(uuid4()), "data": "challenge"})
        if state != "signing" or external_id:
            db.execute("UPDATE operations SET state=?,external_id=? WHERE id=?",
                       (state, external_id, op))
    return op


def _clear_org(s):
    with s.store.transaction() as db:
        for key in ("inn", "oms_id", "oms_connection"):
            db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, dumps("")))


def test_first_setup_with_pending_auth_is_allowed(env):
    app, s, _ = env
    _clear_org(s)
    op = _pending(s, kind="auth_oms")
    c = client(app, "administrator", True)
    body = _body()
    r = c.put("/api/admin/config", json=body)
    assert r.status_code == 200, r.text
    assert s.config()["inn"] == body["inn"]
    assert s.store.one("SELECT state FROM operations WHERE id=?", (op,))["state"] == "signing"
    assert c.put("/api/admin/config", json={**body, "oms_connection": str(uuid4())}).status_code == 409


def test_first_setup_blocks_other_pending_operations(env):
    app, s, _ = env
    _clear_org(s)
    _pending(s, kind="order")
    c = client(app, "administrator", True)
    assert c.put("/api/admin/config", json=_body()).status_code == 409


def test_first_setup_blocks_dispatched_and_uncertain_auth(env):
    app, s, _ = env
    _clear_org(s)
    _pending(s, kind="auth_true", state="unknown")
    c = client(app, "administrator", True)
    assert c.put("/api/admin/config", json=_body()).status_code == 409
