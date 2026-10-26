"""Report rejection, safe retry and reversible admin attention handling."""
import json

import pytest

from conftest import add_doc, advance, client, sign_all
from marking_bm.gateway import report_rejection_message


def _rejected_report(env):
    app, service, registry = env
    doc_id = add_doc(service, owner="alice", number="NK-NOT-READY")
    service.order(doc_id, "alice")
    advance(app)
    service.transferred(doc_id, "alice", 1)
    service.batch("alice", [doc_id], "2026-01-01")
    sign_all(service)
    app.state.worker.tick()
    op = service.store.one("SELECT * FROM operations WHERE kind='report' AND doc_id=?", (doc_id,))
    assert op and op["external_id"]
    registry.fail_report.add(op["external_id"])
    app.state.worker.tick()
    rejected = service.store.one("SELECT * FROM operations WHERE id=?", (op["id"],))
    assert rejected["state"] == "rejected"
    assert service.get(doc_id, "alice")["status"] == "reporting"
    return doc_id, rejected


def test_nk_card_error_is_friendly_without_leaking_upstream_payload():
    message = (
        "Буфер для GTIN = '04640638341173' содержит ошибку: "
        "Для кода товара 04640638341173 не найдена карточка товара или нет прав."
    )
    result = report_rejection_message({"reportStatus": "REJECTED", "errors": [message]})
    assert "Карточка в Национальном каталоге ещё не готова" in result
    assert "попробуйте сформировать отчёт позже" in result.lower()
    assert "04640638341173" in result
    assert "Буфер для GTIN" not in result
    assert "Проверьте причину" in report_rejection_message({"reportStatus": "REJECTED"})


def test_employee_retries_only_own_confirmed_rejection_preserving_payload(env):
    app, service, registry = env
    doc_id, old = _rejected_report(env)
    alice = client(app, "alice")
    bob = client(app, "bob")
    path = f"/api/documents/{doc_id}/report/{old['id']}/retry"
    assert bob.post(path, json={}).status_code == 403
    assert alice.post(f"/api/documents/{doc_id}/report/bad/retry", json={}).status_code == 409

    response = alice.post(path, json={})
    assert response.status_code == 200, response.text
    newer = service.store.one("SELECT * FROM operations WHERE id=?", (response.json()["id"],))
    assert newer["state"] == "signing"
    assert newer["external_id"] is None
    assert newer["payload"] == old["payload"]
    assert newer["group_id"] == old["group_id"]
    assert newer["doc_id"] == old["doc_id"]
    assert service.store.one("SELECT state FROM operations WHERE id=?", (old["id"],))["state"] == "rejected"
    assert alice.post(path, json={}).status_code == 409
    assert service.store.one("SELECT COUNT(*) n FROM operations WHERE kind='report' AND doc_id=?", (doc_id,))["n"] == 2
    registry.fail_report.clear()
    advance(app)
    assert service.get(doc_id, "alice")["status"] == "done"
    assert len(registry.reports) == 2


def test_uncertain_report_cannot_be_resent_and_attention_does_not_change_state(env):
    app, service, registry = env
    doc_id, old = _rejected_report(env)
    admin = client(app, "administrator", True)
    alice = client(app, "alice")
    with service.store.transaction() as db:
        db.execute("UPDATE operations SET state='unknown' WHERE id=?", (old["id"],))
    path = f"/api/admin/operations/{old['id']}"
    assert admin.post(path + "/retry", json={}).status_code == 409
    assert alice.post(path + "/attention", json={"resolved": True}).status_code == 403
    assert admin.post(path + "/attention", json={"resolved": True, "reason": "Сверено в ЛК"}).status_code == 200
    op = service.store.one("SELECT state FROM operations WHERE id=?", (old["id"],))
    assert op["state"] == "unknown"
    seen = next(o for o in admin.get("/api/admin/overview").json()["operations"] if o["id"] == old["id"])
    assert seen["attention_resolved_at"]
    assert admin.post(path + "/attention", json={"resolved": False}).status_code == 200
    again = next(o for o in admin.get("/api/admin/overview").json()["operations"] if o["id"] == old["id"])
    assert again["attention_resolved_at"] is None
    records = service.store.all("SELECT action,detail FROM audit WHERE entity=? ORDER BY id", (old["id"],))
    assert any(x["action"] == "Предупреждение скрыто" for x in records)
    assert any(x["action"] == "Предупреждение возвращено" for x in records)


def test_admin_retry_creates_one_new_signed_operation(env):
    app, service, registry = env
    doc_id, old = _rejected_report(env)
    admin = client(app, "administrator", True)
    assert admin.post(f"/api/admin/operations/{old['id']}/check", json={}).status_code == 409
    r = admin.post(f"/api/admin/operations/{old['id']}/retry", json={})
    assert r.status_code == 200, r.text
    assert r.json()["id"] != old["id"]
    assert admin.post(f"/api/admin/operations/{old['id']}/retry", json={}).status_code == 409
    assert service.store.one("SELECT COUNT(*) n FROM operations WHERE kind='report' AND doc_id=?", (doc_id,))["n"] == 2
