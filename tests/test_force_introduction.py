"""Admin-only TN VED override for first-time FTS introduction submission."""

import pytest

from conftest import advance, client, sign_all
from test_logistics import introduction_body, prepare_atks


def blocked_introduction(env):
    app, service, remote = env
    doc_ids, logist, admin = prepare_atks(env)
    for row in service.store.all("SELECT cis FROM codes WHERE doc_id=?", (doc_ids[0],)):
        remote.codes[row["cis"]]["tnVedEaes"] = "1234567890"
    body = introduction_body(logist, doc_ids[0])
    response = logist.post(
        f"/api/logistics/documents/{doc_ids[0]}/introduce", json=body
    )
    assert response.status_code == 200, response.text
    op_id = response.json()["id"]
    sign_all(service)
    app.state.worker.tick()
    op = service.store.one("SELECT * FROM operations WHERE id=?", (op_id,))
    assert op["state"] == "preflight_blocked"
    assert op["error"] == "ГИС МТ не подтвердила полный ТН ВЭД каждого КМ"
    assert op["external_id"] is None and op["signature"]
    return app, service, remote, doc_ids, admin, op


def introduction_submissions(remote):
    return [
        call for call in remote.calls
        if call[1].endswith("/lk/documents/create")
        and call[2]["type"] == "LP_FTS_INTRODUCE"
    ]


def test_admin_can_force_tnved_mismatch_without_changing_signed_data(env):
    app, service, remote, doc_ids, admin, op = blocked_introduction(env)
    url = f"/api/admin/operations/{op['id']}/force-introduce"
    code_snapshot = service.store.all(
        "SELECT raw,quantity,tnved FROM codes ORDER BY id"
    )
    request = {"confirm": True, "reason": "Проверено расхождение ТН ВЭД в ЧЗ"}
    assert not introduction_submissions(remote)
    assert client(app).post(url, json=request).status_code == 403
    assert admin.post(url, json={**request, "confirm": False}).status_code == 422
    assert admin.post(url, json={**request, "reason": "   "}).status_code == 409
    assert admin.post(url, json=request).status_code == 200
    assert admin.post(url, json=request).status_code == 409

    permission = service.store.one(
        "SELECT * FROM introduction_force_authorizations WHERE op_id=?",
        (op["id"],),
    )
    assert permission["payload_sha"] == op["sha256"]
    assert permission["reason"] == request["reason"]
    assert service.store.one(
        "SELECT id FROM audit WHERE entity=? AND action LIKE '%Принудительный ввод%'",
        (op["id"],),
    )

    advance(app)
    finished = service.store.one("SELECT * FROM operations WHERE id=?", (op["id"],))
    assert finished["state"] == "done" and finished["external_id"]
    assert finished["payload"] == op["payload"]
    assert finished["signature"] == op["signature"]
    assert finished["sha256"] == op["sha256"]
    assert service.store.all("SELECT raw,quantity,tnved FROM codes ORDER BY id") == code_snapshot
    assert service.get(doc_ids[0], "alice")["status"] == "archive"
    assert service.get(doc_ids[1], "bob")["status"] == "done"
    assert len(introduction_submissions(remote)) == 1
    assert admin.post(url, json=request).status_code == 409


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "EMITTED"),
        ("ownerInn", "0000000000"),
        ("parent", "another-aggregate"),
        ("emissionType", "LOCAL"),
        ("statusEx", "FTS_REJECTED"),
    ],
)
def test_force_keeps_other_remote_guards(env, field, value):
    app, service, remote, doc_ids, admin, op = blocked_introduction(env)
    assert admin.post(
        f"/api/admin/operations/{op['id']}/force-introduce",
        json={"confirm": True, "reason": "Ручная сверка ТН ВЭД"},
    ).status_code == 200
    code = service.store.one("SELECT cis FROM codes WHERE doc_id=?", (doc_ids[0],))
    remote.codes[code["cis"]][field] = value
    advance(app, 2)
    pending = service.store.one("SELECT * FROM operations WHERE id=?", (op["id"],))
    assert pending["state"] == "preflight_blocked"
    assert pending["external_id"] is None
    assert not introduction_submissions(remote)
    assert service.get(doc_ids[0], "alice")["status"] == "done"


def test_force_permission_requires_matching_payload_hash(env):
    app, service, remote, _, admin, op = blocked_introduction(env)
    url = f"/api/admin/operations/{op['id']}/force-introduce"
    assert admin.post(url, json={"confirm": True, "reason": "Сверено с ЛК"}).status_code == 200
    # The signed operation SHA is immutable in the DB. Simulate a stale
    # authorization record instead: the override must no longer match.
    with service.store.transaction() as db:
        db.execute(
            "UPDATE introduction_force_authorizations SET payload_sha=? WHERE op_id=?",
            ("0" * 64, op["id"]),
        )
    advance(app, 2)
    pending = service.store.one("SELECT * FROM operations WHERE id=?", (op["id"],))
    assert pending["state"] == "preflight_blocked" and "ТН ВЭД" in pending["error"]
    assert not introduction_submissions(remote)


def test_force_refuses_unrelated_preflight_error(env):
    app, service, remote = env
    doc_ids, logist, admin = prepare_atks(env)
    for row in service.store.all("SELECT cis FROM codes WHERE doc_id=?", (doc_ids[0],)):
        remote.codes[row["cis"]]["ownerInn"] = "0000000000"
    response = logist.post(
        f"/api/logistics/documents/{doc_ids[0]}/introduce",
        json=introduction_body(logist, doc_ids[0]),
    )
    assert response.status_code == 200
    sign_all(service)
    app.state.worker.tick()
    op_id = response.json()["id"]
    op = service.store.one("SELECT * FROM operations WHERE id=?", (op_id,))
    assert op["state"] == "preflight_blocked" and "собственник" in op["error"]
    url = f"/api/admin/operations/{op_id}/force-introduce"
    assert admin.post(
        url, json={"confirm": True, "reason": "Прошу игнорировать статус"},
    ).status_code == 409
    assert not service.store.one(
        "SELECT op_id FROM introduction_force_authorizations WHERE op_id=?", (op_id,)
    )


def test_force_refuses_wrong_operation_kind(env):
    app, service, _, _, admin, op = blocked_introduction(env)
    report = service.store.one("SELECT * FROM operations WHERE kind='report'")
    url = f"/api/admin/operations/{report['id']}/force-introduce"
    assert admin.post(
        url, json={"confirm": True, "reason": "Проверено вручную"}
    ).status_code == 409
