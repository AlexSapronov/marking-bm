"""All external services are mocked ONLY in tests. Runtime has no simulation mode."""

import base64
import json
from uuid import uuid4
import httpx
import pytest
from fastapi.testclient import TestClient
from marking_bm.auth import hash_password
from marking_bm.config import Settings
from marking_bm.gateway import Gateway
from main import create_app

GTIN = "04640638349704"
TN = "8536699008"


class Registry:
    def __init__(self):
        self.orders = {}
        self.codes = {}
        self.blocks = {}
        self.reports = {}
        self.atks = {}
        self.documents = {}
        self.calls = []
        self.partial = None
        self.fail_report = set()
        self.timeout_order = False
        self.timeout_codes = False

    def handle(self, r):
        path = r.url.path
        q = dict(r.url.params)
        body = json.loads(r.content) if r.content else None
        self.calls.append((r.method, path, body, q))
        if path.endswith("/auth/key"):
            return httpx.Response(
                200, json={"uuid": str(uuid4()), "data": "challenge-ёж"}
            )
        if "/auth/simpleSignIn" in path:
            return httpx.Response(
                200,
                json={
                    "token": "oms-token",
                    "uuidToken": "true-token",
                    "expireDate": "2030-01-01T00:00:00Z",
                },
            )
        if path.endswith("/cises/info"):
            result = []
            for cis in body:
                if cis in self.atks:
                    ci = {
                        "cis": cis,
                        "requestedCis": cis,
                        "status": "APPLIED",
                        "ownerInn": "7700000000",
                        "statusEx": None,
                    }
                else:
                    ci = self.codes[cis]
                result.append({"cisInfo": ci})
            return httpx.Response(200, json=result)
        if path.endswith("/order") and r.method == "POST":
            ext = str(uuid4())
            self.orders[ext] = body["products"][0].copy()
            if self.timeout_order:
                raise httpx.ReadTimeout("response lost after mutation", request=r)
            return httpx.Response(200, json={"orderId": ext})
        if path.endswith("/order/status"):
            p = self.orders[q["orderId"]]
            n = p["quantity"] - p.get("taken", 0)
            if self.partial is not None:
                n = min(n, self.partial)
            return httpx.Response(
                200,
                json=[
                    {"gtin": p["gtin"], "bufferStatus": "ACTIVE", "availableCodes": n}
                ],
            )
        if path.endswith("/order/codes/blocks"):
            return httpx.Response(
                200,
                json={
                    "orderId": q["orderId"],
                    "gtin": q["gtin"],
                    "blocks": [
                        {"blockId": k, "quantity": len(v["codes"])}
                        for k, v in self.blocks.items()
                        if v["order"] == q["orderId"]
                    ],
                },
            )
        if path.endswith("/order/codes/retry"):
            return httpx.Response(
                200,
                json={
                    "blockId": q["blockId"],
                    "codes": self.blocks[q["blockId"]]["codes"],
                },
            )
        if path.endswith("/codes"):
            p = self.orders[q["orderId"]]
            n = int(q["quantity"])
            p["taken"] = p.get("taken", 0) + n
            codes = []
            for _ in range(n):
                cis = "01" + p["gtin"] + "21" + "5" + uuid4().hex[:19]
                raw = cis + "\x1d93dGVz"
                codes.append(raw)
                self.codes[cis] = {
                    "cis": cis,
                    "requestedCis": cis,
                    "gtin": p["gtin"],
                    "status": "EMITTED",
                    "ownerInn": "7700000000",
                    "emissionType": "FOREIGN",
                    "statusEx": None,
                    "tnVedEaes": TN,
                    "parent": None,
                }
            block = str(uuid4())
            self.blocks[block] = {"order": q["orderId"], "codes": codes}
            if self.timeout_codes:
                raise httpx.ReadTimeout("block response lost", request=r)
            return httpx.Response(200, json={"codes": codes, "blockId": block})
        if path.endswith("/utilisation"):
            ext = str(uuid4())
            self.reports[ext] = body
            return httpx.Response(200, json={"reportId": ext})
        if path.endswith("/report/info"):
            ext = q["reportId"]
            if ext in self.fail_report:
                return httpx.Response(
                    200, json={"reportId": ext, "reportStatus": "REJECTED"}
                )
            for raw in self.reports[ext]["sntins"]:
                self.codes[raw[:38]]["status"] = "APPLIED"
            return httpx.Response(
                200, json={"reportId": ext, "reportStatus": "SUCCESS"}
            )
        if path.endswith("/receipts/receipt"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "resultDocId": q["resultDocId"],
                            "sourceDocId": str(uuid4()),
                            "state": "SUCCESS",
                            "code": 0,
                        }
                    ]
                },
            )
        if path.endswith("/lk/documents/create"):
            payload = json.loads(base64.b64decode(body["product_document"]))
            ext = str(uuid4())
            if body["type"] == "ATK_AGGREGATION":
                # Use realistic ATK: 20 digits + 5 case-sensitive letters/digits.
                atk = "1" + str(len(self.atks) + 1).zfill(19) + "K0KvG"
                self.atks[atk] = payload["products_list"]
                for item in payload["products_list"]:
                    self.codes[item["ki"]]["parent"] = atk
            else:
                for item in payload["products_list"]:
                    self.codes[item["ki"]]["parent"] = (
                        payload["atk"]
                        if payload["transformation_type"] == "ADDING"
                        else None
                    )
            self.documents[ext] = {
                "number": ext,
                "status": "CHECKED_OK",
                "body": payload,
            }
            return httpx.Response(200, json=ext)
        if "/doc/" in path and path.endswith("/info"):
            return httpx.Response(200, json=[self.documents[path.split("/")[-2]]])
        raise AssertionError(f"Unexpected network request {r.method} {r.url}")


@pytest.fixture
def env(tmp_path):
    registry = Registry()
    settings = Settings(
        data_dir=tmp_path,
        admin_password_hash=hash_password("test-password-long"),
        signer_token="separate-signer-test-token",
        signer_thumbprint="AA11",
        secure_cookie=False,
        send_enabled=True,
        base_url="http://testserver",
    )
    settings.save_token("oms", "oms-token")
    settings.save_token("true", "true-token")
    app = create_app(
        settings,
        lambda settings, config: Gateway(
            settings, config, httpx.MockTransport(registry.handle)
        ),
        run_worker=False,
    )
    s = app.state.service
    with s.store.transaction() as db:
        from marking_bm.db import dumps

        for k, v in {
            "inn": "7700000000",
            "organization": "Test",
            "oms_id": str(uuid4()),
            "oms_connection": str(uuid4()),
            "template_id": 37,
            "kpp": "123456789",
        }.items():
            db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, dumps(v)))
        for u in ("alice", "bob"):
            db.execute("INSERT INTO users(username) VALUES(?)", (u,))
    return app, s, registry


def add_doc(
    s,
    owner="alice",
    supplier="Supplier A",
    labels=3,
    quantity=5,
    number="INV-1",
    pn="PN-A",
):
    i = s.create(owner, number, supplier)
    raw = [[pn, GTIN, quantity, labels, TN]]
    preview = s.preview(i, owner, raw)
    s.save_lines(i, owner, raw, 0, preview["fingerprint"])
    return i


def sign_all(s):
    with s.store.transaction() as db:
        db.execute(
            "UPDATE operations SET state='queued',signature='test-only-signature' WHERE state='signing'"
        )


def advance(app, steps=12):
    s = app.state.service
    for _ in range(steps):
        sign_all(s)
        app.state.worker.tick()


def client(app, username="alice", admin=False):
    c = TestClient(app)
    c.headers["X-Requested-With"] = "marking-bm"
    body = {
        "username": username,
        "admin": admin,
        "password": "test-password-long" if admin else "",
    }
    r = c.post("/api/session", json=body)
    assert r.status_code == 200, r.text
    c.headers["X-CSRF-Token"] = r.json()["csrf"]
    return c
