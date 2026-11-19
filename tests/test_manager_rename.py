"""Self-service invoice-number rename for brand-managers (OBM).

A brand-manager may change the number of their *own* invoice only before ATK
formation has begun for its supplier group. The gate is enforced server-side
(not by hiding the button): owner check, revision guard, status allow-list and
an explicit probe for an existing ATK / ATK operation on the group - all in a
single BEGIN IMMEDIATE transaction, so a worker tick that starts ATK formation
can never interleave with the number write.
"""

from concurrent.futures import ThreadPoolExecutor

from conftest import GTIN, TN, add_doc, advance, client
from test_stop_archive import make_stage
from marking_bm.db import uid
from marking_bm.service import Conflict


def reported_no_atk(env, number="REN-REPORTED", owner="alice"):
    """Drive an invoice to 'reported' with no ATK row/operation yet.

    create_atks is temporarily no-op'd so the report is confirmed and the
    invoice stays at 'reported' while the group has no ATK work at all.
    """
    app, s, _ = env
    doc = add_doc(s, owner=owner, number=number)
    real = s.create_atks
    s.create_atks = lambda db: None
    try:
        s.order(doc, owner)
        advance(app)
        s.transferred(doc, owner, 1)
        s.batch(owner, [doc], "2026-01-01")
        advance(app)
    finally:
        s.create_atks = real
    assert s.get(doc, owner)["status"] == "reported"
    batch = s.store.one(
        "SELECT batch_id,supplier_id FROM batch_docs WHERE doc_id=?", (doc,)
    )
    group = batch["batch_id"] + ":" + batch["supplier_id"]
    assert not s.store.one(
        "SELECT id FROM operations WHERE kind='atk' AND group_id=?", (group,)
    )
    assert not s.store.one(
        "SELECT id FROM atks WHERE batch_id=? AND supplier_id=?",
        (batch["batch_id"], batch["supplier_id"]),
    )
    return doc


def manager_route(doc):
    return "/api/documents/" + doc + "/rename"


def current_revision(s, doc):
    return s.get(doc, "alice")["revision"]


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


def test_manager_cannot_rename_supplement_when_parent_atk_exists(env):
    # A supplement never gets its own ATK (create_atks skips all-parent groups);
    # its codes join the ORIGINAL invoice's ATK via ADDING. Once that parent ATK
    # exists (or formation has begun), renaming the supplement must be refused
    # even though the supplement's own batch carries no ATK row.
    app, s, _ = env
    parent = add_doc(s, number="PARENT-INV")
    s.order(parent, "alice")
    advance(app)
    s.transferred(parent, "alice", 1)
    s.batch("alice", [parent], "2026-01-01")
    advance(app)
    assert s.get(parent, "alice")["status"] == "done"
    assert s.store.one("SELECT id FROM atks")  # the original ATK exists

    # Real supplement through the shipment mechanism (own batch, own report).
    supp = s.create("alice", "SUPPLEMENT-B", "ignored", parent_id=parent)
    s.save_lines(supp, "alice", [["PN-A", GTIN, 7, 2, TN]], current_revision(s, supp))
    # The parent's ATK already exists, so the supplement number is frozen even
    # before its report: the rename guard looks at the parent's group.
    c = client(app)
    r = c.post(manager_route(supp), json={"number": "RENAMED-EARLY", "revision": current_revision(s, supp)})
    assert r.status_code == 409, r.text
    assert s.get(supp, "alice")["number"] == "SUPPLEMENT-B"

    s.order(supp, "alice")
    advance(app)
    s.transferred(supp, "alice", 1)
    s.batch("alice", [supp], "2026-01-01")
    advance(app)
    sup = s.get(supp, "alice")
    assert sup["status"] == "reported"

    # The supplement lives in its own batch; only the parent batch has the ATK.
    supp_batch = s.store.one("SELECT batch_id FROM batch_docs WHERE doc_id=?", (supp,))["batch_id"]
    parent_batch = s.store.one("SELECT batch_id FROM batch_docs WHERE doc_id=?", (parent,))["batch_id"]
    assert supp_batch != parent_batch
    assert not s.store.one("SELECT id FROM atks WHERE batch_id=?", (supp_batch,))
    assert s.store.one("SELECT id FROM atks WHERE batch_id=?", (parent_batch,))

    # Renaming the reported supplement by its owner is refused (409) and the
    # number stays untouched, even though its own batch has no ATK row.
    r = c.post(manager_route(supp), json={"number": "RENAMED-SUPP", "revision": sup["revision"]})
    assert r.status_code == 409, r.text
    assert "АТК" in r.json()["detail"]
    assert s.get(supp, "alice")["number"] == "SUPPLEMENT-B"


def test_manager_cannot_rename_when_atk_operation_exists_without_atks_row(env):
    # Defence-in-depth: in the normal flow create_atks creates the ATK row and
    # its operation atomically, but the operation probe must hold even if an
    # ATK operation exists while no atks row for the group has been written.
    app, s, _ = env
    doc = reported_no_atk(env)
    with s.store.transaction() as db:
        batch = db.execute(
            "SELECT * FROM batch_docs WHERE doc_id=?", (doc,)
        ).fetchone()
        group = batch["batch_id"] + ":" + batch["supplier_id"]
        db.execute(
            "INSERT INTO operations(id,kind,doc_id,group_id,op_key,payload,sha256,"
            "state,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (uid(), "atk", None, group, "atk:" + group + ":8536699008",
             '{"products_list":[]}', "x" * 64, "queued", "2026-01-01T00:00:00+00:00",
             "2026-01-01T00:00:00+00:00"),
        )
    assert not s.store.one("SELECT id FROM atks")
    c = client(app)
    rev = s.get(doc, "alice")["revision"]
    r = c.post(manager_route(doc), json={"number": "OP-ONLY", "revision": rev})
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


def test_manager_cannot_rename_when_group_stopped(env):
    # The interface may still show the field, but the server must refuse a
    # rename once the invoice's own supplier group is paused by another member.
    app, s, _ = env
    first = add_doc(s, number="G1")
    second = add_doc(s, number="G2")
    for doc in (first, second):
        s.order(doc, "alice")
    advance(app)
    for doc in (first, second):
        s.transferred(doc, "alice", 1)
    s.batch("alice", [first, second], "2026-01-01")  # shared group
    s.stop_document(first, "G1", "alice")
    assert s.store.one("SELECT * FROM document_stops WHERE doc_id=? AND resumed_at IS NULL", (first,))
    c = client(app)
    rev = current_revision(s, second)
    assert s.get(second, "alice")["status"] in ("reporting", "reported")
    r = c.post(manager_route(second), json={"number": "G2-NEW", "revision": rev})
    assert r.status_code == 409, r.text
    assert "остановлен" in r.json()["detail"].lower()
    assert s.get(second, "alice")["number"] == "G2"


def test_manager_cannot_rename_when_intermediate_ancestor_group_stopped(env):
    # A grandchild supplement must inherit a group stop caused by a sibling
    # supplement in its immediate parent's batch, not only the root invoice.
    app, s, _ = env
    root = reported_no_atk(env, number="ROOT-REPORTED")
    middle = s.create("alice", "MIDDLE-SUPPLEMENT", "ignored", parent_id=root)
    sibling = s.create("alice", "SIBLING-SUPPLEMENT", "ignored", parent_id=root)
    for doc in (middle, sibling):
        s.save_lines(
            doc, "alice", [["PN-A", GTIN, 7, 2, TN]], current_revision(s, doc)
        )

    # Keep the root invoice at reported without an ATK while the intermediate
    # supplement group is processed, so only the intermediate group is stopped.
    real_create_atks = s.create_atks
    s.create_atks = lambda db: None
    try:
        for doc in (middle, sibling):
            s.order(doc, "alice")
        advance(app)
        for doc in (middle, sibling):
            s.transferred(doc, "alice", 1)
        s.batch("alice", [middle, sibling], "2026-01-01")
        advance(app)
    finally:
        s.create_atks = real_create_atks

    assert s.get(middle, "alice")["status"] == "reported"
    assert s.get(sibling, "alice")["status"] == "reported"
    middle_batch = s.store.one(
        "SELECT batch_id FROM batch_docs WHERE doc_id=?", (middle,)
    )["batch_id"]
    sibling_batch = s.store.one(
        "SELECT batch_id FROM batch_docs WHERE doc_id=?", (sibling,)
    )["batch_id"]
    assert middle_batch == sibling_batch
    assert not s.store.one("SELECT id FROM atks WHERE batch_id=?", (middle_batch,))

    child = s.create("alice", "CHILD-SUPPLEMENT", "ignored", parent_id=middle)
    s.stop_document(sibling, "SIBLING-SUPPLEMENT", "alice")
    assert not s.store.one(
        "SELECT doc_id FROM document_stops WHERE doc_id=? AND resumed_at IS NULL",
        (middle,),
    )

    detail = s.get(child, "alice")
    assert detail["group_stop"]
    assert detail["group_stop"]["number"] == "SIBLING-SUPPLEMENT"

    c = client(app)
    revision = current_revision(s, child)
    r = c.post(
        manager_route(child),
        json={"number": "CHILD-RENAMED", "revision": revision},
    )
    assert r.status_code == 409, r.text
    assert s.get(child, "alice")["number"] == "CHILD-SUPPLEMENT"

def test_nested_supplement_shows_root_atk_and_exports(env):
    # Detail views for a grandchild supplement must include the root invoice's
    # ATK and exports, not only the immediate parent's empty supplement batch.
    app, s, _ = env
    root = add_doc(s, number="ROOT-WITH-ATK")
    s.order(root, "alice")
    advance(app)
    s.transferred(root, "alice", 1)
    s.batch("alice", [root], "2026-01-01")
    advance(app)

    root_batch = s.store.one(
        "SELECT batch_id,supplier_id FROM batch_docs WHERE doc_id=?", (root,)
    )
    root_group = root_batch["batch_id"] + ":" + root_batch["supplier_id"]
    root_atk = s.store.one(
        "SELECT id FROM atks WHERE batch_id=? AND supplier_id=?",
        (root_batch["batch_id"], root_batch["supplier_id"]),
    )
    root_export = s.store.one(
        "SELECT id FROM artifacts WHERE group_id=? AND kind='atk'",
        (root_group,),
    )
    assert root_atk and root_export

    middle = s.create("alice", "MIDDLE-SUPPLEMENT", "ignored", parent_id=root)
    s.save_lines(
        middle, "alice", [["PN-A", GTIN, 7, 2, TN]], current_revision(s, middle)
    )
    s.order(middle, "alice")
    advance(app)
    s.transferred(middle, "alice", 1)
    s.batch("alice", [middle], "2026-01-01")
    advance(app)
    assert s.get(middle, "alice")["status"] == "reported"

    child = s.create("alice", "CHILD-SUPPLEMENT", "ignored", parent_id=middle)
    detail = s.get(child, "alice")
    assert any(a["id"] == root_atk["id"] for a in detail["atks"])
    assert any(f["id"] == root_export["id"] for f in detail["exports"])


def test_manager_cannot_rename_when_codes_have_pending_external_atk_transform(env):
    # Codes can be added to a same-supplier ATK outside their parent chain.
    # A pending ADDING request must therefore contribute its ATK group to the
    # rename guard before the worker applies the new code assignment.
    app, s, _ = env
    host = add_doc(s, number="HOST-ATK")
    s.order(host, "alice")
    advance(app)
    s.transferred(host, "alice", 1)
    s.batch("alice", [host], "2026-01-01")
    advance(app)

    host_batch = s.store.one(
        "SELECT batch_id,supplier_id FROM batch_docs WHERE doc_id=?", (host,)
    )
    host_atk = s.store.one(
        "SELECT * FROM atks WHERE batch_id=? AND supplier_id=?",
        (host_batch["batch_id"], host_batch["supplier_id"]),
    )
    assert host_atk and host_atk["state"] == "done"

    source = reported_no_atk(env, number="SOURCE-REPORTED")
    source_code = s.store.one("SELECT id FROM codes WHERE doc_id=?", (source,))
    candidates = {c["id"] for c in s.candidate_codes(host_atk["id"], "alice")}
    assert source_code["id"] in candidates

    request_id = s.transform(
        host_atk["id"], "ADDING", [source_code["id"]], "alice"
    )
    s.review(
        request_id,
        True,
        "КМ включается в АТК другой группы",
        "alice",
    )

    detail = s.get(source, "alice")
    assert any(a["id"] == host_atk["id"] for a in detail["atks"])
    revision = current_revision(s, source)
    c = client(app)
    r = c.post(
        manager_route(source),
        json={"number": "SOURCE-RENAMED", "revision": revision},
    )
    assert r.status_code == 409, r.text
    assert s.get(source, "alice")["number"] == "SOURCE-REPORTED"

    # Complete the ADDING transformation while suppressing unrelated ATK
    # creation for the reported source batch.
    real_create_atks = s.create_atks
    s.create_atks = lambda db: None
    try:
        advance(app)
    finally:
        s.create_atks = real_create_atks

    assert s.store.one(
        "SELECT state FROM transformations WHERE id=?", (request_id,)
    )["state"] == "done"
    assert s.store.one(
        "SELECT atk_id FROM codes WHERE id=?", (source_code["id"],)
    )["atk_id"] == host_atk["id"]
    detail = s.get(source, "alice")
    assert any(a["id"] == host_atk["id"] for a in detail["atks"])
    host_group = host_batch["batch_id"] + ":" + host_batch["supplier_id"]
    host_export_ids = {
        row["id"]
        for row in s.store.all(
            "SELECT id FROM artifacts WHERE group_id=?", (host_group,)
        )
    }
    assert host_export_ids.intersection({f["id"] for f in detail["exports"]})

    # The administrator can still rename a completed invoice, but must rebuild
    # exports for the external ATK group that now contains its codes as well.
    admin = client(app, "administrator", True)
    admin_revision = current_revision(s, source)
    response = admin.post(
        "/api/admin/documents/" + source + "/rename",
        json={"number": "SOURCE-ADMIN-RENAMED", "revision": admin_revision},
    )
    assert response.status_code == 200, response.text
    latest_host_export = s.store.one(
        "SELECT name FROM artifacts WHERE group_id=? AND kind='atk' "
        "ORDER BY version DESC LIMIT 1",
        (host_group,),
    )
    assert "SOURCE-ADMIN-RENAMED" in latest_host_export["name"]


def test_mixed_owner_external_atk_is_hidden_but_still_blocks_rename(env):
    # Admin-created mixed-owner transformations must still freeze the invoice,
    # but must not expose another owner's ATK details or export links to a manager.
    app, s, _ = env
    host = add_doc(s, owner="alice", number="ALICE-HOST")
    s.order(host, "alice")
    advance(app)
    s.transferred(host, "alice", 1)
    s.batch("alice", [host], "2026-01-01")
    advance(app)

    host_batch = s.store.one(
        "SELECT batch_id,supplier_id FROM batch_docs WHERE doc_id=?", (host,)
    )
    host_atk = s.store.one(
        "SELECT * FROM atks WHERE batch_id=? AND supplier_id=?",
        (host_batch["batch_id"], host_batch["supplier_id"]),
    )
    assert host_atk and host_atk["state"] == "done"

    source = reported_no_atk(env, number="BOB-REPORTED", owner="bob")
    source_code = s.store.one("SELECT id FROM codes WHERE doc_id=?", (source,))
    # Model an administrator-approved cross-owner ADDING result. The worker
    # normally updates atk_id and the ATK version atomically after ЧЗ confirms.
    with s.store.transaction() as db:
        db.execute(
            "UPDATE codes SET atk_id=?,local_state='aggregated' WHERE id=?",
            (host_atk["id"], source_code["id"]),
        )

    detail = s.get(source, "bob")
    assert not any(a["id"] == host_atk["id"] for a in detail["atks"])
    host_group = host_batch["batch_id"] + ":" + host_batch["supplier_id"]
    assert not any(
        f["id"] in {
            row["id"]
            for row in s.store.all(
                "SELECT id FROM artifacts WHERE group_id=?", (host_group,)
            )
        }
        for f in detail["exports"]
    )

    bob = client(app, "bob")
    revision = s.get(source, "bob")["revision"]
    response = bob.post(
        manager_route(source),
        json={"number": "BOB-RENAMED", "revision": revision},
    )
    assert response.status_code == 409, response.text
    assert s.get(source, "bob")["number"] == "BOB-REPORTED"


def test_manager_rename_returns_persisted_revision(env):
    # The API must return the revision that was persisted by the rename, not
    # the value supplied in the request.
    app, s, _ = env
    doc = add_doc(s, number="REV-OK")
    c = client(app)
    before = current_revision(s, doc)
    r = c.post(manager_route(doc), json={"number": "REV-NEW", "revision": before})
    assert r.status_code == 200, r.text
    persisted = s.get(doc, "alice")["revision"]
    assert persisted == before + 1
    assert r.json()["revision"] == persisted