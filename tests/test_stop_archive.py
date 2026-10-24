"""Durable stop fences, in-flight responses and administrator-only restoration."""

import base64
import json
from datetime import date
import pytest
import httpx
from marking_bm.db import Store
from conftest import add_doc, advance, client, sign_all


def make_stage(env, stage):
    app, s, remote = env
    doc = add_doc(s, number="STOP-001")
    if stage == "draft":
        return doc
    if stage == "unknown":
        remote.timeout_order = True
    if stage == "partial":
        remote.partial = 1
    s.order(doc, "alice")
    if stage == "signing":
        return doc
    if stage == "leased":
        c = client(app)
        c.post(
            "/api/signer/next",
            json={},
            headers={"Authorization": "Bearer separate-signer-test-token"},
        )
        return doc
    sign_all(s)
    if stage == "queued":
        return doc
    if stage == "unknown":
        app.state.worker.tick()
        return doc
    if stage == "partial":
        for _ in range(3):
            app.state.worker.tick()
        assert len(s.get(doc, "alice")["codes"]) < 3
        return doc
    advance(app)
    assert s.get(doc, "alice")["status"] == "ready"
    if stage == "ready":
        return doc
    s.transferred(doc, "alice", 1)
    if stage == "transferred":
        return doc
    s.batch("alice", [doc], "2026-01-01")
    if stage == "report_signing":
        return doc
    sign_all(s)
    app.state.worker.tick()
    if stage == "report_submitted":
        return doc
    app.state.worker.tick()
    assert s.store.one("SELECT * FROM operations WHERE kind='atk'")
    if stage == "atk_signing":
        return doc
    sign_all(s)
    app.state.worker.tick()
    if stage == "atk_submitted":
        return doc
    advance(app)
    assert s.get(doc, "alice")["status"] == "done"
    if stage == "archive":
        with s.store.transaction() as db:
            db.execute("UPDATE documents SET status='archive' WHERE id=?", (doc,))
    return doc


@pytest.mark.parametrize(
    "stage",
    [
        "draft",
        "signing",
        "leased",
        "queued",
        "partial",
        "ready",
        "transferred",
        "report_signing",
        "report_submitted",
        "atk_signing",
        "atk_submitted",
        "done",
        "archive",
        "unknown",
    ],
)
def test_stop_from_every_stage_survives_ticks_and_restart(env, stage):
    app, s, remote = env
    doc = make_stage(env, stage)
    payloads = s.store.all("SELECT id,payload,sha256 FROM operations ORDER BY id")
    artifacts = s.store.all("SELECT id,sha256 FROM artifacts ORDER BY id")
    c = client(app)
    assert (
        c.post("/api/documents/" + doc + "/stop", json={"number": "wrong"}).status_code
        == 409
    )
    response = c.post("/api/documents/" + doc + "/stop", json={"number": "STOP-001"})
    assert response.status_code == 200, response.text
    assert (
        c.post(
            "/api/documents/" + doc + "/stop", json={"number": "STOP-001"}
        ).status_code
        == 200
    )
    calls = len(remote.calls)
    Store(s.store.path)  # Additive migration is safe on an existing database.
    app.state.worker.recover()
    advance(app, 5)
    assert len(remote.calls) == calls
    saved = s.get(doc, "alice")
    assert saved["status"] == "archive" and saved["stopped"]
    assert (
        s.store.all("SELECT id,payload,sha256 FROM operations ORDER BY id") == payloads
    )
    assert s.store.all("SELECT id,sha256 FROM artifacts ORDER BY id") == artifacts
    assert not s.store.one(
        "SELECT id FROM operations WHERE state IN ('leased','signing','queued','dispatching','checking')"
    )
    assert any(d["id"] == doc for d in c.get("/api/documents?archive=true").json())
    assert c.post("/api/documents/" + doc + "/order", json={}).status_code == 409
    admin = client(app, "administrator", True)
    for op in saved["operations"]:
        assert (
            admin.post(
                "/api/admin/operations/" + op["id"] + "/check", json={}
            ).status_code
            == 409
        )
    assert len(remote.calls) == calls


def test_stop_is_owner_scoped_csrf_protected_and_restore_is_admin_only(env):
    app, s, _ = env
    doc = add_doc(s, number="Exact-001")
    alice, bob, admin = (
        client(app),
        client(app, "bob"),
        client(app, "administrator", True),
    )
    route = "/api/documents/" + doc + "/stop"
    assert bob.post(route, json={"number": "Exact-001"}).status_code == 403
    assert alice.post(route, json={"number": "exact-001"}).status_code == 409
    assert alice.post(route, json={"number": "Exact-001 "}).status_code == 409
    assert (
        alice.post(
            route, json={"number": "Exact-001"}, headers={"X-CSRF-Token": ""}
        ).status_code
        == 403
    )
    admin.post("/api/admin/users", json={"username": "logist", "role": "logistics"})
    logist = client(app, "logist")
    assert logist.post(route, json={"number": "Exact-001"}).status_code == 403
    assert alice.post(route, json={"number": "Exact-001"}).status_code == 200
    restore = "/api/admin/documents/" + doc + "/restore"
    for ordinary in [alice, bob, logist]:
        assert ordinary.post(restore, json={"reason": "please"}).status_code == 403
    assert admin.post(restore, json={"reason": "  "}).status_code == 422
    assert (
        admin.post(restore, json={"reason": "Закупка подтверждена"}).status_code == 200
    )
    assert (
        s.get(doc, "alice")["status"] == "draft" and not s.get(doc, "alice")["stopped"]
    )
    assert admin.post(restore, json={"reason": "again"}).status_code == 409
    # Repeated stop/restore cycles keep the fence active only while stopped.
    assert admin.post(route, json={"number": "Exact-001"}).status_code == 200
    assert s.get(doc, "alice")["stopped"]
    assert (
        len(
            s.store.all(
                "SELECT * FROM audit WHERE entity=? AND action LIKE '%возвращена%';",
                (doc,),
            )
        )
        == 1
    )


def test_stop_revokes_signer_lease_and_restore_requires_a_new_signature(env):
    app, s, remote = env
    doc = add_doc(s, number="LEASE")
    s.order(doc, "alice")
    c = client(app)
    headers = {"Authorization": "Bearer separate-signer-test-token"}
    job = c.post("/api/signer/next", json={}, headers=headers).json()
    s.stop_document(doc, "LEASE", "alice")
    signed = {
        **{k: job[k] for k in ("id", "lease", "sha256", "thumbprint")},
        "signature": base64.b64encode(b"x" * 100).decode(),
    }
    assert c.post("/api/signer/result", json=signed, headers=headers).status_code == 409
    assert c.post("/api/signer/next", json={}, headers=headers).status_code == 204
    admin = client(app, "administrator", True)
    assert (
        admin.post(
            "/api/admin/documents/" + doc + "/restore", json={"reason": "Нужна закупка"}
        ).status_code
        == 200
    )
    new_job = c.post("/api/signer/next", json={}, headers=headers).json()
    assert new_job["id"] == job["id"] and new_job["lease"] != job["lease"]
    assert not remote.orders


@pytest.mark.parametrize("kind", ["order", "fetch"])
def test_in_flight_mutation_response_is_preserved_without_follow_up(env, kind):
    app, s, remote = env
    doc = add_doc(s, number="IN-FLIGHT")
    s.order(doc, "alice")
    if kind == "fetch":
        sign_all(s)
        app.state.worker.tick()
        app.state.worker.tick()
        assert s.store.one("SELECT * FROM operations WHERE kind='fetch'")
    original = remote.handle
    invoked = []

    def stop_during_response(request):
        response = original(request)
        expected = "/order" if kind == "order" else "/codes"
        if request.url.path.endswith(expected):
            s.stop_document(doc, "IN-FLIGHT", "alice")
            with pytest.raises(ValueError, match="Дождитесь"):
                s.restore_document(doc, "administrator", "too early")
            invoked.append(True)
        return response

    s.gateway.transport = httpx.MockTransport(stop_during_response)
    sign_all(s)
    app.state.worker.tick()
    assert invoked
    op = s.store.one("SELECT * FROM operations WHERE kind=?", (kind,))
    assert op["external_id"] and op["state"] == "submitted" and op["result"]
    if kind == "fetch":
        assert len(json.loads(op["result"])["receipt"]["codes"]) == 3
        assert not s.get(doc, "alice")["codes"]
    calls = len(remote.calls)
    advance(app, 3)
    assert len(remote.calls) == calls
    assert s.get(doc, "alice")["status"] == "archive"
    s.restore_document(doc, "administrator", "Сверить ответ ЧЗ")
    assert (
        s.store.one("SELECT state FROM operations WHERE id=?", (op["id"],))["state"]
        == "blocked"
    )
    advance(app, 2)
    assert len(remote.calls) == calls
    admin = client(app, "administrator", True)
    assert (
        admin.post("/api/admin/operations/" + op["id"] + "/check", json={}).status_code
        == 200
    )
    advance(app, 8)
    assert len(remote.orders) == 1
    if kind == "fetch":
        assert len(remote.blocks) == 1 and len(s.get(doc, "alice")["codes"]) == 3


def test_stop_during_report_preflight_prevents_first_post(env):
    app, s, remote = env
    doc = make_stage(env, "report_signing")
    original = s.gateway.verify

    def stop_before_post(codes, status, **kwargs):
        result = original(codes, status, **kwargs)
        s.stop_document(doc, "STOP-001", "alice")
        return result

    s.gateway.verify = stop_before_post
    sign_all(s)
    app.state.worker.tick()
    assert not remote.reports
    assert s.get(doc, "alice")["status"] == "archive"


def test_shared_group_waits_for_all_stopped_docs_and_other_supplier_continues(env):
    app, s, remote = env
    ids = [
        add_doc(s, number="GROUP-A"),
        add_doc(s, number="GROUP-B"),
        add_doc(s, supplier="Other", number="OTHER"),
    ]
    for doc in ids:
        s.order(doc, "alice")
    advance(app)
    for doc in ids:
        s.transferred(doc, "alice", 1)
    s.batch("alice", ids, "2026-01-01")
    s.stop_document(ids[0], "GROUP-A", "alice")
    advance(app)
    assert s.get(ids[0], "alice")["status"] == "archive"
    assert s.get(ids[1], "alice")["status"] == "reported"
    assert s.get(ids[1], "alice")["group_stop"]
    assert s.get(ids[2], "alice")["status"] == "done"
    s.restore_document(ids[0], "administrator", "Вернуть в общую партию")
    advance(app)
    assert all(s.get(doc, "alice")["status"] == "done" for doc in ids)
    assert len(remote.reports) == 3
    # Pause the existing shared aggregate twice; restoring one invoice must not
    # remove the other invoice's independent hold.
    s.stop_document(ids[0], "GROUP-A", "alice")
    s.stop_document(ids[1], "GROUP-B", "alice")
    s.restore_document(ids[0], "administrator", "first")
    assert s.get(ids[0], "alice")["group_stop"]
    s.restore_document(ids[1], "administrator", "second")
    assert not s.get(ids[0], "alice")["group_stop"]


def test_normal_archive_restore_does_not_immediately_auto_archive_again(env):
    _, s, _ = env
    doc = make_stage(env, "archive")
    with s.store.transaction() as db:
        db.execute(
            "UPDATE documents SET completed='2020-01-01T00:00:00+00:00' WHERE id=?",
            (doc,),
        )
    s.restore_document(doc, "administrator", "Вернуть завершённый документ")
    s.archive(date.today())
    assert s.get(doc, "alice")["status"] == "done"


def test_restart_after_stopped_read_check_allows_admin_restoration(env):
    app, s, _ = env
    doc = make_stage(env, "report_submitted")
    op = s.store.one("SELECT * FROM operations WHERE kind='report'")
    with s.store.transaction() as db:
        db.execute("UPDATE operations SET state='checking' WHERE id=?", (op["id"],))
    s.stop_document(doc, "STOP-001", "alice")
    app.state.worker.recover()
    s.restore_document(doc, "administrator", "После перезапуска")
    assert (
        s.store.one("SELECT state FROM operations WHERE id=?", (op["id"],))["state"]
        == "blocked"
    )


@pytest.mark.parametrize("stage", ["report_submitted", "atk_submitted"])
def test_stop_during_read_confirmation_preserves_receipt_without_reactivation(
    env, stage
):
    app, s, remote = env
    doc = make_stage(env, stage)
    original = s.gateway.info

    def archive_when_checking_codes(cises):
        answer = original(cises)
        s.stop_document(doc, "STOP-001", "alice")
        return answer

    s.gateway.info = archive_when_checking_codes
    app.state.worker.tick()
    kind = "report" if stage == "report_submitted" else "atk"
    report = s.store.one("SELECT * FROM operations WHERE kind=?", (kind,))
    assert json.loads(report["result"])["receipt"]
    assert s.get(doc, "alice")["status"] == "archive"
    assert not s.store.one("SELECT * FROM artifacts WHERE kind IN ('atk','result')")
    calls = len(remote.calls)
    advance(app, 3)
    assert len(remote.calls) == calls


def test_unexpected_preflight_error_cannot_undo_stop(env):
    app, s, _ = env
    doc = make_stage(env, "queued")

    def stop_then_fail(op):
        s.stop_document(doc, "STOP-001", "alice")
        raise RuntimeError("test only")

    app.state.worker.preflight = stop_then_fail
    app.state.worker.tick()
    op = s.store.one("SELECT * FROM operations WHERE kind='order'")
    assert op["state"] == "cancelled"
    s.restore_document(doc, "administrator", "Возобновить после проверки")
    assert (
        s.store.one("SELECT state FROM operations WHERE id=?", (op["id"],))["state"]
        == "signing"
    )


def test_fetch_recovery_and_reconciliation_are_blocked_until_restore(env):
    app, s, remote = env
    doc = add_doc(s, number="RECOVERY")
    remote.timeout_codes = True
    s.order(doc, "alice")
    advance(app, 4)
    fetch = s.store.one("SELECT * FROM operations WHERE kind='fetch'")
    assert fetch["state"] == "unknown"
    s.stop_document(doc, "RECOVERY", "alice")
    calls = len(remote.calls)
    admin = client(app, "administrator", True)
    prefix = "/api/admin/operations/" + fetch["id"]
    assert admin.get(prefix + "/blocks").status_code == 409
    assert (
        admin.post(
            prefix + "/recover-block", json={"block_id": "unused", "reason": "test"}
        ).status_code
        == 409
    )
    assert len(remote.calls) == calls


def test_stop_during_label_check_error_does_not_change_archive_status(env):
    app, s, _ = env
    doc = add_doc(s, number="PDF-CHECK")
    s.order(doc, "alice")
    sign_all(s)
    app.state.worker.tick()
    app.state.worker.tick()
    original = s.gateway.verify

    def failed_check(codes, status, **kwargs):
        original(codes, status, **kwargs)
        s.stop_document(doc, "PDF-CHECK", "alice")
        raise ValueError("test failure after stop")

    s.gateway.verify = failed_check
    app.state.worker.tick()
    assert s.get(doc, "alice")["status"] == "archive"
    assert not s.store.one(
        "SELECT * FROM artifacts WHERE doc_id=? AND kind='labels'", (doc,)
    )


def test_stop_during_excel_inspection_prevents_late_source_save(env, monkeypatch):
    import main
    from marking_bm.excel import xlsx

    app, s, _ = env
    doc = add_doc(s, number="IMPORT")
    original = main.import_file

    def stop_while_inspecting(content, **kwargs):
        result = original(content, **kwargs)
        s.stop_document(doc, "IMPORT", "alice")
        return result

    monkeypatch.setattr(main, "import_file", stop_while_inspecting)
    response = client(app).post(
        "/api/documents/" + doc + "/import",
        files={
            "file": (
                "mapping.xlsx",
                xlsx({"Unknown": [["A", "B"], ["1", "2"]]}),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert response.status_code == 409
    assert not s.store.one(
        "SELECT id FROM artifacts WHERE doc_id=? AND kind='source'", (doc,)
    )
    assert s.get(doc, "alice")["status"] == "archive"
