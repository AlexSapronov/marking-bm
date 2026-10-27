"""SUZ terminal report states and partial per-CIS re-submission.

All remote services are MockTransport stubs; no external writes in tests.
"""
import json

from conftest import add_doc, advance, client, sign_all
from marking_bm.gateway import report_rejection_message


def test_partially_processed_report_retries_only_emitted_codes(env):
    app, service, registry = env
    doc_id = add_doc(service, labels=3, number="PARTIAL-SUZ", owner="alice")
    service.order(doc_id, "alice")
    advance(app)
    service.transferred(doc_id, "alice", 1)
    service.batch("alice", [doc_id], "2026-01-01")
    sign_all(service)
    app.state.worker.tick()
    original = service.store.one(
        "SELECT * FROM operations WHERE kind='report' AND doc_id=?", (doc_id,)
    )
    assert original["external_id"]
    original_payload = json.loads(original["payload"])
    all_codes = [x[:38] for x in original_payload["sntins"]]
    assert len(all_codes) == 3
    registry.partial_report[original["external_id"]] = {all_codes[0]}
    app.state.worker.tick()
    state = service.store.one("SELECT state,error FROM operations WHERE id=?", (original["id"],))
    assert state["state"] == "partial"
    assert "частично" in state["error"]
    assert service.get(doc_id, "alice")["status"] == "reporting"

    bob = client(app, "bob")
    alice = client(app, "alice")
    url = f"/api/documents/{doc_id}/report/{original['id']}/retry"
    assert bob.post(url, json={}).status_code == 403
    response = alice.post(url, json={})
    assert response.status_code == 200, response.text
    info = response.json()
    assert info["codes_already_applied"] == 1
    assert info["codes_for_retry"] == 2
    assert alice.post(url, json={}).status_code == 409
    retried = service.store.one("SELECT * FROM operations WHERE id=?", (info["id"],))
    new_payload = json.loads(retried["payload"])
    assert [raw[:38] for raw in new_payload["sntins"]] == all_codes[1:]
    assert new_payload["attributes"] == original_payload["attributes"]
    assert service.store.one("SELECT COUNT(*) n FROM codes WHERE doc_id=? AND external_status='APPLIED'",
                             (doc_id,))["n"] == 1

    registry.partial_report.clear()
    advance(app)
    assert service.get(doc_id, "alice")["status"] == "done"
    assert service.store.one("SELECT COUNT(*) n FROM codes WHERE doc_id=? AND external_status='APPLIED'",
                             (doc_id,))["n"] == 3
    assert len(registry.reports) == 2
    assert len(registry.reports[original["external_id"]]["sntins"]) == 3


def test_partial_reconciliation_refuses_uncertain_registry_states(env):
    app, service, registry = env
    doc_id = add_doc(service, labels=3, number="PARTIAL-UNSAFE")
    service.order(doc_id, "alice")
    advance(app)
    service.transferred(doc_id, "alice", 1)
    service.batch("alice", [doc_id], "2026-01-01")
    sign_all(service)
    app.state.worker.tick()
    op = service.store.one("SELECT * FROM operations WHERE kind='report' AND doc_id=?", (doc_id,))
    items = json.loads(op["payload"])["sntins"]
    registry.partial_report[op["external_id"]] = {items[0][:38]}
    app.state.worker.tick()
    registry.codes[items[1][:38]]["status"] = "IN_CIRCULATION"
    admin = client(app, "administrator", True)
    r = admin.post(f"/api/admin/operations/{op['id']}/retry", json={})
    assert r.status_code == 409, r.text
    assert service.store.one("SELECT COUNT(*) n FROM operations WHERE kind='report' AND doc_id=?",
                             (doc_id,))["n"] == 1


def test_missing_nk_card_wording_matches_suz_ui():
    response = {"reportStatus": "ERROR", "errors": [
        "12: Отсутствует карточка товара (GTIN) в НК. Код товара 04670553907321"
    ]}
    msg = report_rejection_message(response)
    assert "Карточка в Национальном каталоге ещё не готова" in msg
    assert "04670553907321" in msg
