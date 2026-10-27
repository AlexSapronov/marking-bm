"""Admin attention must remain visible regardless of recent operation volume."""
from conftest import client


def test_old_unresolved_warning_not_hidden_by_200_newer_operations(env):
    app, service, _ = env
    with service.store.transaction() as db:
        old_id = service.queue(db, "report", "ux:old:report", {"sntins": []})
        db.execute(
            "UPDATE operations SET state='rejected',external_id='suz:old' WHERE id=?",
            (old_id,),
        )
        for index in range(215):
            op_id = service.queue(db, "fetch", f"ux:recent:{index}", {"i": index})
            db.execute("UPDATE operations SET state='done' WHERE id=?", (op_id,))
    admin = client(app, "administrator", True)
    overview = admin.get("/api/admin/overview")
    assert overview.status_code == 200, overview.text
    items = overview.json()["operations"]
    assert len(items) == 201
    assert next(i for i in items if i["id"] == old_id)["state"] == "rejected"
    acknowledged = admin.post(
        f"/api/admin/operations/{old_id}/attention",
        json={"resolved": True, "reason": "Разобрано вручную"},
    )
    assert acknowledged.status_code == 200, acknowledged.text
    followup = admin.get("/api/admin/overview")
    assert followup.status_code == 200
    assert old_id not in {i["id"] for i in followup.json()["operations"]}


def test_unresolved_warning_is_not_duplicate_of_recent_operations(env):
    app, service, _ = env
    with service.store.transaction() as db:
        important = service.queue(db, "report", "ux:recent:warn", {"sntins": []})
        db.execute("UPDATE operations SET state='partial',external_id='suz:recent' WHERE id=?", (important,))
    result = client(app, "administrator", True).get("/api/admin/overview")
    assert result.status_code == 200, result.text
    assert [x["id"] for x in result.json()["operations"]].count(important) == 1
