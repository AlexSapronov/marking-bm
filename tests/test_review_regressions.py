"""Regression cases identified in the deployment-readiness code review."""

import json
import time
import httpx
import pytest
from conftest import GTIN, add_doc, advance, client, sign_all
from marking_bm.gateway import RemoteError


@pytest.mark.parametrize("kind", ["order", "report", "atk", "transform"])
def test_invalid_success_id_after_mutation_is_uncertain(env, kind):
    _, service, _ = env
    service.gateway.transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json="invalid-id" if kind in ("atk", "transform") else {"orderId" if kind == "order" else "reportId": "invalid-id"})
    )
    with pytest.raises(RemoteError) as caught:
        service.gateway.submit({"kind": kind, "payload": "{}", "signature": "test-only"})
    assert caught.value.uncertain


def test_dispatch_keeps_invalid_success_response_unknown_without_resending(env):
    app, service, remote = env
    doc = add_doc(service)
    service.order(doc, "alice")
    original = remote.handle

    def corrupted_response(request):
        response = original(request)
        if request.url.path.endswith("/order"):
            return httpx.Response(200, json={"orderId": "invalid-id"})
        return response

    service.gateway.transport = httpx.MockTransport(corrupted_response)
    sign_all(service)
    app.state.worker.tick()
    op = service.store.one("SELECT * FROM operations WHERE kind='order'")
    assert op["state"] == "unknown"
    advance(app)
    assert len(remote.orders) == 1


def test_old_pending_operations_do_not_starve_new_authorization(env):
    app, service, remote = env
    remote.partial = 0
    doc = add_doc(service)
    service.order(doc, "alice")
    sign_all(service)
    app.state.worker.tick()
    old = service.store.one("SELECT * FROM operations WHERE kind='order'")
    with service.store.transaction() as db:
        for index in range(55):
            op = service.queue(db, "order", f"pending-{index}", json.loads(old["payload"]), doc)
            db.execute("UPDATE operations SET state='submitted',external_id=? WHERE id=?", (old["external_id"], op))
        auth = service.queue(db, "auth_true", "review-auth", service.gateway.challenge())
    sign_all(service)
    worker = app.state.worker
    worker.last_token_check = time.time()
    worker.tick()
    assert service.store.one("SELECT state FROM operations WHERE id=?", (auth,))["state"] == "done"
    # Older pending status checks must also rotate, rather than always selecting the first 50.
    for _ in range(3):
        worker.tick()
    pending = service.store.all("SELECT updated,created FROM operations WHERE kind='order'")
    assert all(op["updated"] > op["created"] for op in pending)


def test_manager_cannot_open_mixed_owner_atk_created_by_admin(env):
    app, service, _ = env
    docs = [add_doc(service, number="MIX-A"), add_doc(service, owner="bob", number="MIX-B")]
    for doc, owner in zip(docs, ["alice", "bob"]):
        service.order(doc, owner)
    advance(app)
    for doc, owner in zip(docs, ["alice", "bob"]):
        service.transferred(doc, owner, 1)
    service.batch("administrator", docs, "2026-01-01", admin=True)
    advance(app)
    atk = service.get(docs[0], "alice")["atks"][0]
    alice = client(app)
    bob = client(app, "bob")
    admin = client(app, "administrator", True)
    for employee in [alice, bob]:
        assert employee.get("/api/atks/" + atk["id"]).status_code == 403
        assert employee.get("/api/atks/" + atk["id"] + "/candidates").status_code == 403
    assert admin.get("/api/atks/" + atk["id"]).status_code == 200


def test_manager_cannot_pick_another_owners_supplement_codes(env):
    app, service, _ = env
    bases = [add_doc(service, number="BASE-A"), add_doc(service, owner="bob", number="BASE-B")]
    for doc, owner in zip(bases, ["alice", "bob"]):
        service.order(doc, owner)
    advance(app)
    for doc, owner in zip(bases, ["alice", "bob"]):
        service.transferred(doc, owner, 1)
        service.batch(owner, [doc], "2026-01-01")
    advance(app)
    extra = service.create("bob", "EXTRA-B", "Supplier A", parent_id=bases[1])
    raw = [["PN-A", GTIN, 5, 2, "8536699008"]]
    preview = service.preview(extra, "bob", raw)
    service.save_lines(extra, "bob", raw, 0, preview["fingerprint"])
    service.order(extra, "bob")
    advance(app)
    service.transferred(extra, "bob", 1)
    service.batch("bob", [extra], "2026-01-01")
    advance(app)
    atk = service.get(bases[0], "alice")["atks"][0]
    code = service.store.one("SELECT id FROM codes WHERE doc_id=?", (extra,))
    alice = client(app)
    candidates = alice.get("/api/atks/" + atk["id"] + "/candidates")
    assert candidates.status_code == 200
    assert code["id"] not in {c["id"] for c in candidates.json()}
    response = alice.post("/api/atks/" + atk["id"] + "/transform", json={"kind": "ADDING", "codes": [code["id"]]})
    assert response.status_code == 403

    admin = client(app, "administrator", True)
    allowed = admin.get("/api/atks/" + atk["id"] + "/candidates").json()
    assert code["id"] in {c["id"] for c in allowed}
    requested = admin.post("/api/atks/" + atk["id"] + "/transform", json={"kind": "ADDING", "codes": [code["id"]]})
    assert requested.status_code == 200, requested.text
    assert admin.post("/api/admin/transformations/" + requested.json()["id"], json={"approve": True, "reason": "Mixed-owner group approved by administrator"}).status_code == 200
    advance(app)
    assert alice.get("/api/atks/" + atk["id"]).status_code == 403
    assert admin.get("/api/atks/" + atk["id"]).status_code == 200


def test_upstream_order_http_400_requires_review_not_automatic_replay(env):
    # Signing and external rejection are separate: a 400 means the request
    # reached the external service, not that the signer is offline.
    app, service, remote = env
    doc_id = add_doc(service, number="HTTP400-ORDER")
    service.order(doc_id, "alice")
    sign_all(service)
    seen = []
    original = remote.handle

    def reject_order(request):
        if request.url.path.endswith("/order"):
            seen.append(request.url.path)
            return httpx.Response(400, json={"message": "Upstream request rejected"})
        return original(request)

    service.gateway.transport = httpx.MockTransport(reject_order)
    worker = app.state.worker
    worker.tick()
    operation = service.store.one(
        "SELECT state,external_id,error FROM operations WHERE kind='order' AND doc_id=?",
        (doc_id,),
    )
    assert operation["state"] == "blocked"
    assert operation["external_id"] is None
    assert "HTTP 400" in operation["error"]
    worker.tick()
    worker.tick()
    assert len(seen) == 1, "Do not replay an upstream rejection automatically"


def test_expired_oms_access_token_pauses_signed_order_until_refresh(env):
    app, service, remote = env
    doc_id = add_doc(service, number="OMS-EXPIRED")
    service.order(doc_id, "alice")
    sign_all(service)
    service.settings.save_token("oms", "expired", "2000-01-01T00:00:00+00:00")
    worker = app.state.worker
    worker.last_token_check = 0
    worker.tick()
    order = service.store.one("SELECT state,error FROM operations WHERE kind='order' AND doc_id=?", (doc_id,))
    assert order == {"state": "queued", "error": None}
    assert service.get(doc_id, "alice")["waiting_for_oms_auth"]
    assert not remote.orders
    auth = service.store.one("SELECT state FROM operations WHERE kind='auth_oms'")
    assert auth and auth["state"] == "signing", "An unsigned refresh must remain queued for signer"
    # The order still cannot be sent without signing the separate auth job.
    worker.tick()
    assert not remote.orders
    sign_all(service)
    worker.tick()
    assert service.gateway.token_ready("oms")
    worker.tick()
    assert len(remote.orders) == 1
    assert not service.get(doc_id, "alice")["waiting_for_oms_auth"]


def test_oms_token_near_expiry_pauses_order_before_external_call(env):
    app, service, remote = env
    from datetime import datetime, timedelta, timezone
    service.settings.save_token(
        "oms", "nearly-expired",
        (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat(),
    )
    doc_id = add_doc(service, number="OMS-NEAR-EXPIRED")
    service.order(doc_id, "alice")
    sign_all(service)
    app.state.worker.tick()
    order = service.store.one("SELECT state FROM operations WHERE kind='order' AND doc_id=?", (doc_id,))
    assert order["state"] == "queued"
    assert not remote.orders
