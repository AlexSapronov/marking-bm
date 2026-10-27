"""Only administrators may reassign invoice ownership, preserving history and files."""
import json
from pathlib import Path

from conftest import add_doc, advance, client


def test_admin_reassignment_moves_access_without_changing_artifacts(env):
    app, service, registry = env
    doc_id = add_doc(service, owner="alice", number="MOVE-42")
    service.order(doc_id, "alice")
    advance(app)
    before = service.get(doc_id, "alice")
    assert before["artifacts"] and before["codes"]
    file_id = before["artifacts"][0]["id"]
    original_codes = [x["id"] for x in before["codes"]]

    admin = client(app, "administrator", True)
    alice = client(app, "alice")
    bob = client(app, "bob")
    assert any(x["id"] == doc_id for x in admin.get("/api/admin/documents?q=MOVE-42").json())
    assert alice.get("/api/admin/documents").status_code == 403
    assert alice.put("/api/admin/documents/" + doc_id + "/owner", json={"owner": "bob"}).status_code == 403
    assert bob.get("/api/documents/" + doc_id).status_code == 403

    changed = admin.put("/api/admin/documents/" + doc_id + "/owner", json={"owner": "bob"})
    assert changed.status_code == 200, changed.text
    assert changed.json()["previous_owner"] == "alice"
    assert changed.json()["owner"] == "bob"
    assert alice.get("/api/documents/" + doc_id).status_code == 403
    assert alice.get("/api/files/" + file_id).status_code == 403
    assert bob.get("/api/documents/" + doc_id).status_code == 200
    assert bob.get("/api/files/" + file_id).status_code == 200
    after = service.get(doc_id, "bob")
    assert [x["id"] for x in after["codes"]] == original_codes
    assert [x["id"] for x in after["artifacts"]] == [x["id"] for x in before["artifacts"]]
    assert after["revision"] == before["revision"] + 1
    audits = service.store.all("SELECT * FROM audit WHERE entity=? AND action=?", (
        doc_id, "Переназначен ответственный за накладную"))
    assert len(audits) == 1
    assert audits[0]["actor"] == "administrator"
    assert json.loads(audits[0]["detail"])["previous_owner"] == "alice"
    assert json.loads(audits[0]["detail"])["new_owner"] == "bob"


def test_reassignment_guards_invalid_users_and_supports_archive(env):
    app, service, registry = env
    doc_id = add_doc(service, owner="alice", number="ARCH-23")
    admin = client(app, "administrator", True)
    url = "/api/admin/documents/" + doc_id + "/owner"
    assert admin.put(url, json={"owner": "alice"}).status_code == 409
    assert admin.put(url, json={"owner": "missing"}).status_code == 422
    assert admin.put("/api/admin/documents/missing/owner", json={"owner": "bob"}).status_code == 422

    assert admin.post("/api/admin/users", json={"username": "bob", "role": "logistics", "enabled": True}).status_code == 200
    assert admin.put(url, json={"owner": "bob"}).status_code == 422
    assert admin.post("/api/admin/users", json={"username": "bob", "role": "manager", "enabled": False}).status_code == 200
    assert admin.put(url, json={"owner": "bob"}).status_code == 422
    assert admin.post("/api/admin/users", json={"username": "bob", "role": "manager", "enabled": True}).status_code == 200

    with service.store.transaction() as db:
        db.execute("UPDATE documents SET status='archive' WHERE id=?", (doc_id,))
    result = admin.put(url, json={"owner": "bob"})
    assert result.status_code == 200, result.text
    assert any(x["id"] == doc_id for x in admin.get("/api/admin/documents?q=ARCH-23").json())
    assert client(app, "bob").get("/api/documents/" + doc_id).status_code == 200
    assert service.store.one("SELECT status FROM documents WHERE id=?", (doc_id,))["status"] == "archive"


def test_zip_download_is_secondary_to_next_workflow_action():
    js = Path("static/app.js").read_text()
    css = Path("static/style.css").read_text()
    workspace = Path("static/workspace.js").read_text()
    assert '<a class="button" href="/api/files/${e(currentFile.id)}">' in js
    assert '<button class="primary" data-action="report">Подтвердить нанесение</button>' in js
    assert ".button.primary" in css
    assert 'documents: ["Документы"' in workspace
