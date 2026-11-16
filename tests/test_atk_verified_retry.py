"""Verified re-submission of an ATK after repeated True API status 404."""

import json

from conftest import add_doc, advance, sign_all, client


def pending_atk(env):
    app, service, remote = env
    doc = add_doc(service, labels=3)
    service.order(doc, "alice")
    advance(app)
    service.transferred(doc, "alice", 1)
    service.batch("alice", [doc], "2026-01-01", "123456789")
    for _ in range(24):
        sign_all(service)
        app.state.worker.tick()
        op = service.store.one("SELECT * FROM operations WHERE kind='atk'")
        if op and op["state"] == "submitted":
            return op
    raise AssertionError("ATK did not reach submitted state")


def test_atk_retry_requires_admin_and_verified_absence(env):
    app, service, remote = env
    original = pending_atk(env)
    ext = original["external_id"]
    with service.store.transaction() as db:
        db.execute("UPDATE operations SET error=? WHERE id=?",
                   ("ЧЗ: HTTP 404. Проверьте права и состояние операции в ЛК.", original["id"]))
    employee = client(app, "alice")
    admin = client(app, "administrator", True)
    prefix = f"/api/admin/operations/{original['id']}"
    assert employee.get(prefix + "/atk-review").status_code == 403
    assert employee.post(prefix + "/retry-verified-atk", json={
        "external_id": ext, "confirmed_absent": True,
        "reason": "Сверено в ЛК ЧЗ, АТК не найден",
    }).status_code == 403
    review = admin.get(prefix + "/atk-review")
    assert review.status_code == 200, review.text
    assert review.json()["external_id"] == ext
    assert review.json()["count"] == 3
    payload = {"external_id": ext, "confirmed_absent": True,
               "reason": "Проверены документ и КМ в ЛК ЧЗ; АТК отсутствует"}
    assert admin.post(prefix + "/retry-verified-atk", json={
        **payload, "confirmed_absent": False,
    }).status_code == 422
    assert admin.post(prefix + "/retry-verified-atk", json={
        **payload, "external_id": "other-uuid",
    }).status_code == 409
    # Even a missing source document is NOT enough: registry already shows a parent.
    assert admin.post(prefix + "/retry-verified-atk", json=payload).status_code == 409
    assert service.store.one("SELECT COUNT(*) n FROM operations WHERE kind='atk'")["n"] == 1

    # Simulate definitive independent absence: no parent on any CIS.
    for ci in remote.codes.values():
        ci["parent"] = None
    remote.atks.clear()
    remote.documents.pop(ext, None)
    r = admin.post(prefix + "/retry-verified-atk", json=payload)
    assert r.status_code == 200, r.text
    new_id = r.json()["id"]
    assert new_id != original["id"]
    old = service.store.one("SELECT * FROM operations WHERE id=?", (original["id"],))
    new = service.store.one("SELECT * FROM operations WHERE id=?", (new_id,))
    assert old["external_id"] == ext
    assert old["state"] == "superseded"
    assert new["state"] == "signing"
    assert new["external_id"] is None
    assert json.loads(new["payload"]) == json.loads(original["payload"])
    assert service.store.one("SELECT op_id FROM atks")["op_id"] == new_id
    assert admin.post(prefix + "/retry-verified-atk", json=payload).status_code == 409
    assert service.store.one("SELECT COUNT(*) n FROM operations WHERE kind='atk'")["n"] == 2
    audits = service.store.all("SELECT * FROM audit WHERE entity=?", (original["id"],))
    assert any("Проверенный повтор АТК" in a["action"] for a in audits)

    sign_all(service)
    app.state.worker.tick()
    app.state.worker.tick()
    assert service.store.one("SELECT state FROM operations WHERE id=?", (new_id,))["state"] == "done"
    assert service.store.one("SELECT state,code FROM atks")["state"] == "done"


def test_atk_retry_rejects_other_errors_and_completed_operation(env):
    app, service, remote = env
    original = pending_atk(env)
    admin = client(app, "administrator", True)
    path = f"/api/admin/operations/{original['id']}/retry-verified-atk"
    payload = {"external_id": original["external_id"], "confirmed_absent": True,
               "reason": "В ЛК ЧЗ проверено отсутствие документа"}
    assert admin.post(path, json=payload).status_code == 409
    # The existing submitted operation can still complete, so no replay is allowed.
    app.state.worker.tick()
    assert service.store.one("SELECT state FROM operations WHERE id=?", (original["id"],))["state"] == "done"
    with service.store.transaction() as db:
        db.execute("UPDATE operations SET error=? WHERE id=?",
                   ("ЧЗ: HTTP 404. Статус временно недоступен", original["id"]))
    assert admin.post(path, json=payload).status_code == 409
