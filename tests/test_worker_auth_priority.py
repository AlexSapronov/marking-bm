"""Regression tests for durable outbox authorization scheduling."""

from uuid import uuid4


def test_signed_authorization_is_not_starved_by_25_old_token_blocked_orders(env, monkeypatch):
    app, service, registry = env
    service.settings.save_token("true", "expired-true", "2020-01-01T00:00:00Z")
    service.settings.save_token("oms", "expired-oms", "2020-01-01T00:00:00Z")

    with service.store.transaction() as db:
        for index in range(25):
            op_id = service.queue(
                db, "order", f"starvation:order:{index}", {"products": []}
            )
            db.execute(
                "UPDATE operations SET state='queued',signature='test-only-signature' WHERE id=?",
                (op_id,),
            )

        authorizations = {}
        for kind in ("true", "oms"):
            op_id = service.queue(
                db, f"auth_{kind}", f"starvation:auth:{kind}",
                {"uuid": str(uuid4()), "data": "signed-challenge"},
            )
            db.execute(
                "UPDATE operations SET state='queued',signature='test-only-signature' WHERE id=?",
                (op_id,),
            )
            authorizations[kind] = op_id

    worker = app.state.worker
    original_dispatch = worker.dispatch
    seen = []

    def dispatch(op):
        seen.append(op["kind"])
        # Old paid orders have no business payload in this test: keep them queued,
        # while processing auth through the real mocked HTTPS gateway.
        if op["kind"] == "order":
            return
        return original_dispatch(op)

    monkeypatch.setattr(worker, "dispatch", dispatch)
    worker.tick()

    assert seen[:2] == ["auth_true", "auth_oms"]
    for kind, op_id in authorizations.items():
        assert service.store.one(
            "SELECT state FROM operations WHERE id=?", (op_id,)
        )["state"] == "done"
        assert service.gateway.token_ready(kind)

    assert service.store.one(
        "SELECT COUNT(*) AS n FROM operations WHERE kind='order' AND state='queued'"
    )["n"] == 25
    assert len([call for call in registry.calls if "/auth/simpleSignIn" in call[1]]) == 2
