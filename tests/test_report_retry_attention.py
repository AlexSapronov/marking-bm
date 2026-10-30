"""Report rejection, safe retry and reversible admin attention handling."""
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


def test_admin_can_retry_only_known_local_failures(env):
    app, service, registry = env
    doc_id, old = _rejected_report(env)
    admin = client(app, "administrator", True)
    path = f"/api/admin/operations/{old['id']}/retry-local"
    # Explicit upstream rejections and unknown results cannot be blindly replayed.
    assert admin.post(path, json={}).status_code == 409
    with service.store.transaction() as db:
        db.execute("UPDATE operations SET state='blocked',external_id=NULL,error=? WHERE id=?",
                   ("ЧЗ: HTTP 403. Проверьте права", old["id"]))
    assert admin.post(path, json={}).status_code == 409
    with service.store.transaction() as db:
        db.execute("UPDATE operations SET error=? WHERE id=?",
                   ("Обновите доступ к СУЗ в админке", old["id"]))
    sent = admin.post(path, json={})
    assert sent.status_code == 200, sent.text
    assert sent.json()["state"] == "queued"
    record = service.store.one("SELECT state,external_id,error FROM operations WHERE id=?", (old["id"],))
    assert record == {"state": "queued", "external_id": None, "error": None}


def test_admin_can_retry_http400_order_only_after_verified_absence(env):
    from conftest import add_doc, sign_all
    app, service, registry = env
    doc_id = add_doc(service, number="HTTP400-VERIFIED")
    service.order(doc_id, "alice")
    sign_all(service)
    op = service.store.one(
        "SELECT * FROM operations WHERE kind='order' AND doc_id=?", (doc_id,)
    )
    assert op["signature"], "Original signed bytes must be available"
    # This simulates a rejected upstream request without forging an external ID.
    with service.store.transaction() as db:
        db.execute(
            "UPDATE operations SET state='blocked',error=? WHERE id=?",
            ("ЧЗ: HTTP 400. Проверьте права и состояние операции в ЛК.", op["id"]),
        )
    employee = client(app, "alice")
    admin = client(app, "administrator", True)
    base = f"/api/admin/operations/{op['id']}"
    assert employee.get(base + "/order-review").status_code == 403
    assert employee.post(base + "/retry-verified-order", json={
        "confirmed_absent": True, "reason": "Заказ отсутствует в СУЗ"
    }).status_code == 403
    preview = admin.get(base + "/order-review")
    assert preview.status_code == 200, preview.text
    assert preview.json()["document"] == "HTTP400-VERIFIED"
    assert preview.json()["products"][0]["gtin"]
    assert preview.json()["products"][0]["quantity"] == 3
    assert preview.json()["production_order_id"]
    path = base + "/retry-verified-order"
    assert admin.post(path, json={
        "confirmed_absent": False, "reason": "Заказ отсутствует в СУЗ"
    }).status_code == 422
    assert admin.post(path, json={
        "confirmed_absent": True, "reason": "коротко"
    }).status_code == 422
    sent = admin.post(path, json={
        "confirmed_absent": True, "reason": "Проверен личный кабинет СУЗ, заказа нет"
    })
    assert sent.status_code == 200, sent.text
    updated = service.store.one("SELECT * FROM operations WHERE id=?", (op["id"],))
    assert updated["state"] == "queued"
    assert updated["error"] is None
    assert updated["signature"] == op["signature"]
    assert updated["payload"] == op["payload"]
    assert updated["op_key"] == op["op_key"]
    assert admin.post(path, json={
        "confirmed_absent": True, "reason": "Проверен личный кабинет СУЗ, заказа нет"
    }).status_code == 409
    audit_items = service.store.all("SELECT detail FROM audit WHERE entity=?", (op["id"],))
    assert any("Проверен личный кабинет СУЗ" in a["detail"] for a in audit_items)
    app.state.worker.tick()
    assert len(registry.orders) == 1


def test_verified_order_retry_rejects_unknown_unrelated_and_unsigned(env):
    from conftest import add_doc, sign_all
    app, service, _ = env
    doc_id = add_doc(service, number="HTTP400-REJECT-SAFETY")
    service.order(doc_id, "alice")
    sign_all(service)
    op = service.store.one(
        "SELECT * FROM operations WHERE kind='order' AND doc_id=?", (doc_id,)
    )
    admin = client(app, "administrator", True)
    path = f"/api/admin/operations/{op['id']}/retry-verified-order"
    payload = {"confirmed_absent": True, "reason": "Проверено в ЛК СУЗ: заказа нет"}

    with service.store.transaction() as db:
        db.execute("UPDATE operations SET state='unknown',error=? WHERE id=?",
                   ("Нет подтверждения от ЧЗ", op["id"]))
    assert admin.post(path, json=payload).status_code == 409
    with service.store.transaction() as db:
        db.execute("UPDATE operations SET state='blocked',error=? WHERE id=?",
                   ("ЧЗ: HTTP 403. Проверьте права", op["id"]))
    assert admin.post(path, json=payload).status_code == 409
    with service.store.transaction() as db:
        db.execute("UPDATE operations SET error=?,external_id=? WHERE id=?",
                   ("ЧЗ: HTTP 400. Проверьте права и состояние операции в ЛК.", "external", op["id"]))
    assert admin.post(path, json=payload).status_code == 409
    with service.store.transaction() as db:
        db.execute("UPDATE operations SET external_id=NULL,signature=NULL WHERE id=?", (op["id"],))
    assert admin.post(path, json=payload).status_code == 409
    with service.store.transaction() as db:
        db.execute("UPDATE operations SET signature='test',state='blocked' WHERE id=?", (op["id"],))
        db.execute("UPDATE documents SET status='archive' WHERE id=?", (doc_id,))
    assert admin.post(path, json=payload).status_code == 409
