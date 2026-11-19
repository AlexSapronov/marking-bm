"""Self-service invoice-number rename for brand-managers (OBM).

A brand-manager may change the number of their *own* invoice only before ATK
formation has begun for its supplier group. The gate is enforced server-side
(not by hiding the button): owner check, revision guard, status allow-list and
an explicit probe for an existing ATK / ATK operation on the group - all in a
single BEGIN IMMEDIATE transaction, so a worker tick that starts ATK formation
can never interleave with the number write.
"""

from concurrent.futures import ThreadPoolExecutor

from conftest import add_doc, advance, client
from test_stop_archive import make_stage
from marking_bm.db import uid
from marking_bm.service import Conflict


def reported_no_atk(env, number="REN-REPORTED"):
    """Drive an invoice to 'reported' with no ATK row/operation yet.

    create_atks is temporarily no-op'd so the report is confirmed and the
    invoice stays at 'reported' while the group has no ATK work at all.
    """
    app, s, _ = env
    doc = add_doc(s, number=number)
    real = s.create_atks
    s.create_atks = lambda db: None
    try:
        s.order(doc, "alice")
        advance(app)
        s.transferred(doc, "alice", 1)
        s.batch("alice", [doc], "2026-01-01")
        advance(app)
    finally:
        s.create_atks = real
    assert s.get(doc, "alice")["status"] == "reported"
    assert not s.store.one("SELECT id FROM operations WHERE kind='atk'")
    assert not s.store.one("SELECT id FROM atks")
    return doc


def manager_route(doc):
    return "/api/documents/" + doc + "/rename"


def test_manager_renames_own_draft(env):
    app, s, _ = env
    doc = add_doc(s, number="DRAFT-REN")
    c = client(app)
    r = c.post(manager_route(doc), json={"number": "DRAFT-NEW", "revision": s.get(doc, "alice")["revision"]})
    assert r.status_code == 200, r.text
    assert s.get(doc, "alice")["number"] == "DRAFT-NEW"


def test_manager_renames_own_ready(env):
    app, s, _ = env
    doc = make_stage(env, "ready")
    c = client(app)
    rev = s.get(doc, "alice")["revision"]
    r = c.post(manager_route(doc), json={"number": "READY-NEW", "revision": rev})
    assert r.status_code == 200, r.text
    assert s.get(doc, "alice")["number"] == "READY-NEW"


def test_manager_renames_own_transferred(env):
    app, s, _ = env
    doc = make_stage(env, "transferred")
    c = client(app)
    rev = s.get(doc, "alice")["revision"]
    r = c.post(manager_route(doc), json={"number": "TRANS-NEW", "revision": rev})
    assert r.status_code == 200, r.text
    assert s.get(doc, "alice")["number"] == "TRANS-NEW"


def test_manager_renames_own_reported_before_atk(env):
    app, s, _ = env
    doc = reported_no_atk(env)
    c = client(app)
    rev = s.get(doc, "alice")["revision"]
    r = c.post(manager_route(doc), json={"number": "REPORTED-NEW", "revision": rev})
    assert r.status_code == 200, r.text
    assert s.get(doc, "alice")["number"] == "REPORTED-NEW"
    # The later ATK formation builds exports with the new number.
    advance(app)
    assert s.get(doc, "alice")["status"] == "done"
    assert "REPORTED-NEW" in s.store.one(
        "SELECT name FROM artifacts WHERE kind='atk' ORDER BY version DESC LIMIT 1"
    )["name"]


def test_manager_cannot_rename_after_atk_started(env):
    app, s, _ = env
    # atk_submitted leaves the invoice at 'aggregating' with an ATK operation,
    # i.e. ATK formation already began (incomplete/unknown result).
    doc = make_stage(env, "atk_submitted")
    c = client(app)
    rev = s.get(doc, "alice")["revision"]
    assert s.get(doc, "alice")["status"] == "aggregating"
    r = c.post(manager_route(doc), json={"number": "LATE", "revision": rev})
    assert r.status_code == 409, r.text


def test_manager_cannot_rename_reported_when_atk_exists_for_group(env):
    app, s, _ = env
    doc = reported_no_atk(env)
    # Simulate the supplement edge case: invoice remains 'reported' while its
    # supplier group already carries an ATK (e.g. an unfinished ADDING step).
    # The status name is not enough - the operation/atk probe must refuse.
    with s.store.transaction() as db:
        batch = db.execute(
            "SELECT * FROM batch_docs WHERE doc_id=?", (doc,)
        ).fetchone()
        db.execute(
            "INSERT INTO atks(id,supplier_id,tnved,batch_id,op_id,state) "
            "VALUES(?,?,?,?,?,?)",
            (uid(), batch["supplier_id"], "8536699008", batch["batch_id"], uid(), "processing"),
        )
    c = client(app)
    rev = s.get(doc, "alice")["revision"]
    r = c.post(manager_route(doc), json={"number": "FORSED", "revision": rev})
    assert r.status_code == 409, r.text
    assert "АТК" in r.json()["detail"]
    assert s.get(doc, "alice")["number"] == "REN-REPORTED"


def test_manager_cannot_rename_others_invoice(env):
    app, s, _ = env
    doc = add_doc(s, number="ALICE-OWN")
    bob = client(app, "bob")
    r = bob.post(manager_route(doc), json={"number": "HIJACK", "revision": 0})
    assert r.status_code == 403, r.text
    assert s.get(doc, "alice")["number"] == "ALICE-OWN"


def test_logistics_cannot_rename(env):
    app, s, _ = env
    doc = add_doc(s, number="LOGISTICS-OK")
    admin = client(app, "administrator", True)
    assert (
        admin.post(
            "/api/admin/users", json={"username": "logist-r", "role": "logistics"}
        ).status_code
        == 200
    )
    logist = client(app, "logist-r")
    assert logist.post(manager_route(doc), json={"number": "NOPE", "revision": 0}).status_code == 403
    assert s.get(doc, "alice")["number"] == "LOGISTICS-OK"


def test_supervisor_cannot_rename(env):
    app, s, _ = env
    doc = add_doc(s, number="SUP-OK")
    admin = client(app, "administrator", True)
    assert (
        admin.post(
            "/api/admin/users", json={"username": "team-lead-r", "role": "supervisor"}
        ).status_code
        == 200
    )
    sup = client(app, "team-lead-r")
    assert sup.post(manager_route(doc), json={"number": "NOPE", "revision": 0}).status_code == 403
    assert s.get(doc, "alice")["number"] == "SUP-OK"


def test_admin_keeps_full_power_after_atk(env):
    app, s, remote = env
    doc = make_stage(env, "done")
    admin = client(app, "administrator", True)
    rev = s.get(doc, "alice")["revision"]
    # Admin flow (including after ATK formation) keeps working.
    r = admin.post(
        "/api/admin/documents/" + doc + "/rename",
        json={"number": "ADMIN-POST-ATK", "revision": rev},
    )
    assert r.status_code == 200, r.text
    assert s.get(doc, "alice")["number"] == "ADMIN-POST-ATK"
    # The regenerated exports use the admin-supplied number.
    assert "ADMIN-POST-ATK" in s.store.one(
        "SELECT name FROM artifacts WHERE kind='atk' ORDER BY version DESC LIMIT 1"
    )["name"]


def test_stale_revision_is_refused(env):
    app, s, _ = env
    doc = add_doc(s, number="STALE")
    c = client(app)
    r = c.post(manager_route(doc), json={"number": "NEW", "revision": 5})
    assert r.status_code == 409, r.text
    assert s.get(doc, "alice")["number"] == "STALE"


def test_invalid_numbers_are_refused(env):
    app, s, _ = env
    doc = add_doc(s, number="VALID-1")
    c = client(app)
    rev = s.get(doc, "alice")["revision"]
    # Empty value.
    assert c.post(manager_route(doc), json={"number": "", "revision": rev}).status_code == 422
    # Whitespace only (passes the JSON schema, fails server validation).
    assert c.post(manager_route(doc), json={"number": "   ", "revision": rev}).status_code == 422
    # Too long.
    assert c.post(manager_route(doc), json={"number": "X" * 121, "revision": rev}).status_code == 422
    assert s.get(doc, "alice")["number"] == "VALID-1"


def test_cannot_rename_into_existing_number(env):
    app, s, _ = env
    add_doc(s, number="TAKEN-1", pn="PN-A")
    second = add_doc(s, number="FREE-1", pn="PN-B")
    c = client(app)
    rev = s.get(second, "alice")["revision"]
    r = c.post(manager_route(second), json={"number": "TAKEN-1", "revision": rev})
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "Накладная с таким номером уже существует"
    assert s.get(second, "alice")["number"] == "FREE-1"
    # Admin flow is equally prevented from colliding.
    admin = client(app, "administrator", True)
    assert (
        admin.post(
            "/api/admin/documents/" + second + "/rename",
            json={"number": "TAKEN-1", "revision": rev},
        ).status_code
        == 409
    )
    # Keeping the invoice's own current number is a harmless no-op.
    assert c.post(manager_route(second), json={"number": "FREE-1", "revision": rev}).status_code == 200


def test_rename_records_audit_and_preserves_history(env):
    app, s, _ = env
    doc = make_stage(env, "ready")
    c = client(app)
    rev = s.get(doc, "alice")["revision"]
    before_artifacts = s.store.all("SELECT id,sha256 FROM artifacts ORDER BY id")
    before_ops = s.store.all("SELECT payload,sha256 FROM operations ORDER BY id")
    r = c.post(manager_route(doc), json={"number": "AUDITED-1", "revision": rev})
    assert r.status_code == 200, r.text
    row = s.store.one(
        "SELECT actor,detail FROM audit WHERE action='Изменён номер накладной' "
        "AND entity=? ORDER BY id DESC LIMIT 1",
        (doc,),
    )
    assert row["actor"] == "alice"
    assert row["detail"].index("STOP-001") >= 0 and row["detail"].index("AUDITED-1") >= 0
    # Immutable artifacts and immutable operation payloads are untouched.
    assert s.store.all("SELECT id,sha256 FROM artifacts ORDER BY id") == before_artifacts
    assert s.store.all("SELECT payload,sha256 FROM operations ORDER BY id") == before_ops


def test_new_number_reflects_in_register_and_card(env):
    app, s, _ = env
    doc = add_doc(s, number="REG-OLD")
    c = client(app)
    r = c.post(manager_route(doc), json={"number": "REG-NEW", "revision": s.get(doc, "alice")["revision"]})
    assert r.status_code == 200
    listed = {d["id"]: d for d in c.get("/api/documents").json()}
    assert listed[doc]["number"] == "REG-NEW"
    assert s.get(doc, "alice")["number"] == "REG-NEW"


def test_rename_and_atk_start_race_is_consistent(env):
    app, s, _ = env
    doc = reported_no_atk(env)
    rev = s.get(doc, "alice")["revision"]

    outcome = {}

    def do_rename():
        try:
            s.manager_rename_document(doc, "RACED-1", rev, "alice")
            outcome["rename"] = "ok"
        except Conflict:
            outcome["rename"] = "refused"

    def do_atks():
        # A fresh transaction, exactly like the worker tick.
        with s.store.transaction() as db:
            s.create_atks(db)
        outcome["atks"] = bool(
            s.store.one(
                "SELECT id FROM atks WHERE batch_id IN "
                "(SELECT batch_id FROM batch_docs WHERE doc_id=?)",
                (doc,),
            )
        )

    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(do_rename)
        f2 = ex.submit(do_atks)
        f1.result()
        f2.result()

    final = s.get(doc, "alice")
    assert outcome["atks"]  # ATK formation ran
    if outcome["rename"] == "ok":
        assert final["number"] == "RACED-1"
    else:
        assert final["number"] == "REN-REPORTED"
    # Whichever committed first, the invoice is either still reported (renamed)
    # or moved to aggregating by the ATK step - never a torn intermediate state.
    assert final["status"] in ("reported", "aggregating")
    if outcome["rename"] == "ok":
        # Rename committed before ATK: a later ATK build keeps the new number.
        assert final["status"] == "aggregating"