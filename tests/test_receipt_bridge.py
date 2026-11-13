import json
import time

import httpx
import pytest
from conftest import add_doc, advance, client
from marking_bm.config import Settings
from marking_bm.db import Store
from marking_bm.receipt_bridge import deliver


def prepare(env):
    app, s, _ = env
    ids = [add_doc(s, number="ONE"), add_doc(s, number="TWO")]
    for doc in ids:
        s.order(doc, "alice")
    advance(app)
    for doc in ids:
        s.transferred(doc, "alice", 1)
    s.batch("administrator", ids, "2026-01-01", admin=True)
    advance(app)
    return ids


def test_completed_group_queues_one_payload_per_invoice_not_per_atk(env):
    app, s, _ = env
    ids = prepare(env)
    jobs = s.store.all("SELECT * FROM receipt_deliveries")
    assert len(jobs) == 2
    for job in jobs:
        body = json.loads(job["payload"])
        assert body["source_document"] in ids
        assert body["org_inn"] == "7700000000" and body["revision"] == 1
        assert (
            len(body["codes"]) == 3 and sum(c["quantity"] for c in body["codes"]) == 15
        )
        assert all("\x1d93" in c["raw"] and len(c["atk"]) == 25 for c in body["codes"])
    # Rebuilding exports does not requeue an unchanged composition.
    with s.store.transaction() as db:
        a = db.execute("SELECT * FROM atks LIMIT 1").fetchone()
        s.exports(db, a["batch_id"], a["supplier_id"])
    assert {
        j["revision"] for j in s.store.all("SELECT revision FROM receipt_deliveries")
    } == {1}
    assert len(Store(s.store.path).all("SELECT * FROM receipt_deliveries")) == 2
    overview = client(app, "administrator", True).get("/api/admin/overview").json()
    assert len(overview["receipt_deliveries"]) == 2
    assert "payload" not in overview["receipt_deliveries"][0]


def test_queue_not_created_before_atk_confirmation(env):
    app, s, _ = env
    doc = add_doc(s)
    s.order(doc, "alice")
    advance(app)
    s.transferred(doc, "alice", 1)
    s.batch("administrator", [doc], "2026-01-01", admin=True)
    assert s.store.all("SELECT * FROM receipt_deliveries") == []


def test_several_atks_of_one_invoice_make_one_complete_receipt(env):
    app, s, _ = env
    doc = add_doc(s)
    raw = [
        ["PN-A", "04640638349704", 5, 3, "8536699008"],
        ["PN-B", "04640638349711", 7, 2, "8504408300"],
    ]
    preview = s.preview(doc, "alice", raw)
    s.save_lines(doc, "alice", raw, 1, preview["fingerprint"])
    s.order(doc, "alice")
    advance(app)
    for c in env[2].codes.values():
        if c["gtin"] == "04640638349711":
            c["tnVedEaes"] = "8504408300"
    s.transferred(doc, "alice", s.get(doc, "alice")["current_version"])
    s.batch("administrator", [doc], "2026-01-01", admin=True)
    advance(app)
    jobs = s.store.all("SELECT * FROM receipt_deliveries")
    assert len(jobs) == 1, s.store.all(
        "SELECT kind,state,error FROM operations WHERE state!='done'"
    )
    body = json.loads(jobs[0]["payload"])
    assert len({c["atk"] for c in body["codes"]}) == 2
    assert len(body["codes"]) == 5 and sum(c["quantity"] for c in body["codes"]) == 29


def test_composition_change_increments_revision_and_old_ack_cannot_clear_it(env):
    from marking_bm.receipt_bridge import enqueue

    _, s, _ = env
    ids = prepare(env)
    s.settings.receipt_bridge_url = "http://localhost:8000"
    s.settings.receipt_bridge_key = "k" * 48
    original = s.store.one("SELECT * FROM receipt_deliveries WHERE doc_id=?", (ids[0],))

    def handle(request):
        body = json.loads(request.content)
        if body["source_document"] == ids[0]:
            with s.store.transaction() as db:
                docs = [
                    dict(
                        db.execute(
                            "SELECT * FROM documents WHERE id=?", (ids[0],)
                        ).fetchone()
                    )
                ]
                codes = [
                    dict(c)
                    for c in db.execute("SELECT * FROM codes WHERE doc_id=?", (ids[0],))
                ]
                enqueue(db, s.config(), docs, codes[:1])
        return httpx.Response(
            200,
            json={
                "document_id": "wh",
                "revision": body["revision"],
                "status": "created",
            },
        )

    deliver(s, httpx.MockTransport(handle))
    latest = s.store.one("SELECT * FROM receipt_deliveries WHERE doc_id=?", (ids[0],))
    assert (
        latest["revision"] == original["revision"] + 1 and latest["state"] == "pending"
    )
    assert len(json.loads(latest["payload"])["codes"]) == 1


def test_retry_lost_response_uses_exact_same_identity_and_payload(env):
    _, s, _ = env
    prepare(env)
    s.settings.receipt_bridge_url = "http://127.0.0.1:8000"
    s.settings.receipt_bridge_key = "k" * 48
    calls = []

    def handle(request):
        calls.append(request)
        assert request.headers["X-Receipt-Bridge-Key"] == "k" * 48
        raise httpx.ReadTimeout("response lost", request=request)

    deliver(s, httpx.MockTransport(handle))
    assert len(calls) == 2
    jobs = s.store.all("SELECT * FROM receipt_deliveries")
    assert all(j["state"] == "error" and j["next_attempt"] > time.time() for j in jobs)
    with s.store.transaction() as db:
        db.execute("UPDATE receipt_deliveries SET next_attempt=0")

    def success(request):
        assert request.content in {r.content for r in calls}
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "document_id": "warehouse-" + body["source_document"],
                "revision": body["revision"],
                "status": "unchanged",
            },
        )

    deliver(s, httpx.MockTransport(success))
    assert all(
        j["state"] == "delivered" and j["error"] is None
        for j in s.store.all("SELECT * FROM receipt_deliveries")
    )


def test_warehouse_conflict_and_wrong_ack_are_visible_and_not_success(env):
    _, s, _ = env
    prepare(env)
    s.settings.receipt_bridge_url = "http://localhost:8000"
    s.settings.receipt_bridge_key = "k" * 48
    deliver(s, httpx.MockTransport(lambda r: httpx.Response(409)))
    assert all(
        j["state"] == "error" and "HTTP 409" in j["error"]
        for j in s.store.all("SELECT * FROM receipt_deliveries")
    )
    with s.store.transaction() as db:
        db.execute("UPDATE receipt_deliveries SET next_attempt=0")
    deliver(
        s,
        httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"document_id": "x", "revision": 999, "status": "created"}
            )
        ),
    )
    assert all(
        j["state"] == "error" for j in s.store.all("SELECT * FROM receipt_deliveries")
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.example",
        "http://127.0.0.1:8000/path",
        "https://user:password@example.org",
        "https://example.org?q=1",
    ],
)
def test_connection_rejects_unsafe_urls(tmp_path, url):
    with pytest.raises(ValueError):
        Settings(data_dir=tmp_path, receipt_bridge_url=url, receipt_bridge_key="k" * 48)
