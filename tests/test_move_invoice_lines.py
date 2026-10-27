"""Moving intact invoice lines and exact allocated KMs before reports."""
import json
import zipfile
from collections import Counter
from io import BytesIO

import pytest
from conftest import GTIN, TN, add_doc, advance, client
from marking_bm.service import Conflict


def prepared(env, number, *, owner="alice", supplier="Supplier A", labels=2, qty=5):
    app, service, _ = env
    d = add_doc(service, owner=owner, supplier=supplier,
                number=number, labels=labels, quantity=qty)
    service.order(d, owner)
    advance(app)
    result = service.get(d, owner)
    assert result["status"] == "ready"
    assert result["current_version"] == 1
    return d


def test_move_selected_line_with_codes_rebuilds_both_label_versions(env):
    app, service, remote = env
    source = add_doc(service, number="FROM", labels=3)
    raw = [["PN-A", GTIN, 5, 2, TN], ["PN-A", GTIN, 9, 1, TN]]
    preview = service.preview(source, "alice", raw)
    service.save_lines(source, "alice", raw, 1, preview["fingerprint"])
    service.order(source, "alice")
    advance(app)
    target = prepared(env, "TO", labels=2)
    service.transferred(source, "alice", 1)
    service.transferred(target, "alice", 1)
    before = service.get(source, "alice")
    selected = next(l for l in before["lines"] if l["quantity"] == 9)
    old_code_ids = {c["id"] for c in before["codes"]}
    old_target = {c["id"] for c in service.get(target, "alice")["codes"]}
    original_labels = service.store.one(
        "SELECT id FROM artifacts WHERE doc_id=? AND kind='labels' AND version=1",
        (source,),
    )["id"]
    manager = client(app, "alice")
    candidates = manager.get(f"/api/documents/{source}/move-targets")
    assert candidates.status_code == 200
    assert target in [c["id"] for c in candidates.json()]
    params = {"target_id": target, "line_ids": [selected["id"]]}
    plan = manager.post(f"/api/documents/{source}/move-lines/preview", json=params)
    assert plan.status_code == 200, plan.text
    assert plan.json()["codes"] == 1
    assert plan.json()["units"] == 9
    result = manager.post(
        f"/api/documents/{source}/move-lines",
        json={**params, "fingerprint": plan.json()["fingerprint"]},
    )
    assert result.status_code == 200, result.text
    src, dst = service.get(source, "alice"), service.get(target, "alice")
    assert src["status"] == dst["status"] == "ready"
    assert src["current_version"] == dst["current_version"] == 2
    assert src["transferred_version"] is None
    assert dst["transferred_version"] is None
    assert src["revision"] == 3 and dst["revision"] == 2
    assert [(l["quantity"], l["labels"]) for l in src["lines"]] == [(5, 2)]
    assert Counter((l["quantity"], l["labels"]) for l in dst["lines"]) == Counter(
        [(5, 2), (9, 1)]
    )
    assert {c["id"] for c in src["codes"]} <= old_code_ids
    assert {c["id"] for c in dst["codes"]} == old_target | (old_code_ids - {c["id"] for c in src["codes"]})
    assert service.store.one("SELECT COUNT(*) n FROM codes")["n"] == 5
    assert service.store.one("SELECT COUNT(*) n FROM assignments WHERE active=1")["n"] == 5
    for d in (src, dst):
        new_zip = service.store.one(
            "SELECT content,snapshot FROM artifacts WHERE doc_id=? "
            "AND kind='labels' AND version=2", (d["id"],)
        )
        assignments = json.loads(new_zip["snapshot"])
        assert len(assignments) == d["required"]
        assert sorted(a["quantity"] for a in assignments) == sorted(
            c["quantity"] for c in d["codes"]
        )
        with zipfile.ZipFile(BytesIO(new_zip["content"])) as z:
            assert all(z.read(name).startswith(b"%PDF-") for name in z.namelist())
    assert service.store.one("SELECT id FROM artifacts WHERE id=?", (original_labels,))
    assert service.store.one(
        "SELECT COUNT(*) n FROM audit WHERE entity=? AND action=?",
        (source, "Строки и КМ переданы в другую накладную"),
    )["n"] == 1
    assert not remote.reports
    assert len(remote.orders) == 1


def test_move_all_lines_to_empty_draft_source_becomes_draft(env):
    app, service, registry = env
    source = prepared(env, "M-ALL")
    target = service.create("alice", "M-EMPTY", "Supplier A")
    line = service.get(source, "alice")["lines"][0]
    plan = service.move_lines_preview(source, target, [line["id"]], "alice")
    assert plan["codes"] == 2
    service.move_lines(source, target, [line["id"]], plan["fingerprint"], "alice")
    src, dst = service.get(source, "alice"), service.get(target, "alice")
    assert src["status"] == "draft" and not src["lines"] and not src["codes"]
    assert src["required"] == 0
    assert src["current_version"] == 1
    assert dst["status"] == "ready" and dst["current_version"] == 1
    assert len(dst["codes"]) == 2
    assert service.store.one("SELECT COUNT(*) n FROM codes")["n"] == 2
    # Old ZIP stays in historical artifacts, but must not be shown as current.
    assert service.store.one(
        "SELECT COUNT(*) n FROM artifacts WHERE doc_id=? AND kind='labels'",
        (source,),
    )["n"] == 1


def test_move_rejects_other_supplier_and_other_owner(env):
    app, service, registry = env
    source = prepared(env, "SOURCE")
    other_supplier = service.create("alice", "WRONG-SUPPLIER", "Supplier B")
    another_owner = service.create("bob", "OTHER-OWNER", "Supplier A")
    line_id = service.get(source, "alice")["lines"][0]["id"]
    with pytest.raises(Conflict, match="одного поставщика"):
        service.move_lines_preview(source, other_supplier, [line_id], "alice")
    with pytest.raises(PermissionError):
        service.move_lines_preview(source, another_owner, [line_id], "alice")
    assert another_owner not in [
        d["id"] for d in service.move_line_targets(source, "alice")
    ]
    assert another_owner in [
        d["id"] for d in service.move_line_targets(source, "administrator", True)
    ]
    assert service.move_lines_preview(
        source, another_owner, [line_id], "administrator", True,
    )["codes"] == 2
    logist = client(app, "bob")
    with service.store.transaction() as db:
        db.execute("UPDATE users SET role='logistics' WHERE username='bob'")
    # Employee B no longer has manager privileges.
    assert logist.post(
        f"/api/documents/{source}/move-lines/preview",
        json={"target_id": another_owner, "line_ids": [line_id]},
    ).status_code == 403


def test_move_blocks_after_report_and_does_not_touch_ownership(env):
    app, service, registry = env
    source = prepared(env, "WITH-REPORT")
    target = prepared(env, "RECEIVER")
    service.transferred(source, "alice", 1)
    service.batch("alice", [source], "2026-01-01")
    ids = {x["id"] for x in service.get(source, "alice")["codes"]}
    line = service.get(source, "alice")["lines"][0]
    with pytest.raises(Conflict):
        service.move_lines_preview(source, target, [line["id"]], "alice")
    assert {x["id"] for x in service.get(source, "alice")["codes"]} == ids
    assert service.store.one("SELECT COUNT(*) n FROM assignments WHERE active=1")["n"] == 4


def test_move_requires_preview_and_refuses_stale_revisions_or_unverified_codes(env):
    app, service, registry = env
    source = prepared(env, "BEFORE")
    target = service.create("alice", "AFTER", "Supplier A")
    line = service.get(source, "alice")["lines"][0]
    admin = client(app, "administrator", True)
    params = {"target_id": target, "line_ids": [line["id"]]}
    assert admin.post(f"/api/documents/{source}/move-lines", json=params).status_code == 422
    preview = admin.post(
        f"/api/documents/{source}/move-lines/preview", json=params
    ).json()
    assert admin.post(
        f"/api/documents/{source}/move-lines",
        json={**params, "fingerprint": "invalid"},
    ).status_code == 409
    registry.codes[service.get(source, "alice")["codes"][0]["cis"]]["status"] = "APPLIED"
    bad = admin.post(
        f"/api/documents/{source}/move-lines",
        json={**params, "fingerprint": preview["fingerprint"]},
    )
    assert bad.status_code in (422, 503)
    assert service.get(source, "alice")["codes"]
    assert not service.get(target, "alice")["codes"]
    registry.codes[service.get(source, "alice")["codes"][0]["cis"]]["status"] = "EMITTED"
    with service.store.transaction() as db:
        db.execute("UPDATE documents SET revision=revision+1 WHERE id=?", (target,))
    assert admin.post(
        f"/api/documents/{source}/move-lines",
        json={**params, "fingerprint": preview["fingerprint"]},
    ).status_code == 409


def test_move_target_nonempty_draft_rejected_and_duplicate_rows_rejected(env):
    app, service, registry = env
    source = prepared(env, "TRANS-SRC")
    destination = add_doc(service, number="TRANS-DRAFT")
    line_id = service.get(source, "alice")["lines"][0]["id"]
    with pytest.raises(Conflict, match="пустым"):
        service.move_lines_preview(source, destination, [line_id], "alice")
    with pytest.raises(ValueError, match="уникаль"):
        service.move_lines_preview(source, source, [line_id], "alice")
    with pytest.raises(ValueError, match="Выберите"):
        service.move_lines_preview(source, destination, [], "alice")
