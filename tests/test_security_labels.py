import base64
from hashlib import sha256
from io import BytesIO
import json
import pytest
import fitz
import zxingcpp
from PIL import Image
from pypdf import PdfReader
from conftest import client, add_doc, GTIN, advance
from marking_bm.codes import parse, with_quantity
from marking_bm.labels import render_label
from marking_bm.auth import hash_password, verify_password


def test_ordinary_user_cannot_access_other_document_file_or_admin(env):
    app, s, r = env
    a = add_doc(s)
    s.order(a, "alice")
    advance(app)
    c = client(app, "bob")
    assert c.get("/api/documents/" + a).status_code == 403
    assert c.post("/api/documents/" + a + "/order", json={}).status_code == 403
    f = s.get(a, "alice")["artifacts"][0]
    assert c.get("/api/files/" + f["id"]).status_code == 403
    assert c.get("/api/admin/overview").status_code == 403
    assert c.post("/api/admin/users", json={"username": "evil"}).status_code == 403
    # An administrator's username without the separate password is an ordinary session.
    admin = client(app, "administrator", True)
    same_name = client(app, "administrator")
    assert same_name.get("/api/admin/overview").status_code == 403
    assert admin.get("/api/admin/overview").status_code == 200


def test_csrf_disabled_user_and_rate_limit(env):
    app, s, r = env
    c = client(app)
    assert (
        c.post(
            "/api/documents",
            headers={"X-CSRF-Token": ""},
            json={"number": "X", "supplier": "Y"},
        ).status_code
        == 403
    )
    assert (
        c.post(
            "/api/documents",
            headers={"Origin": "https://evil.example"},
            json={"number": "X", "supplier": "Y"},
        ).status_code
        == 403
    )
    a = client(app, "administrator", True)
    assert (
        a.post(
            "/api/admin/users", json={"username": "alice", "enabled": False}
        ).status_code
        == 200
    )
    assert c.get("/api/documents").status_code == 401
    for _ in range(5):
        assert (
            c.post(
                "/api/session", json={"admin": True, "password": "wrong"}
            ).status_code
            == 403
        )
    assert (
        c.post("/api/session", json={"admin": True, "password": "wrong"}).status_code
        == 429
    )


def test_signature_lease_exact_utf8_hash_replay_and_separate_auth(env):
    app, s, r = env
    a = add_doc(s)
    s.order(a, "alice")
    c = client(app)
    assert c.post("/api/signer/next", json={}).status_code == 403
    headers = {"Authorization": "Bearer separate-signer-test-token"}
    job = c.post("/api/signer/next", json={}, headers=headers).json()
    payload = base64.b64decode(job["payload_base64"])
    assert sha256(payload).hexdigest() == job["sha256"]
    assert json.loads(payload)["attributes"]["releaseMethodType"] == "IMPORT"
    assert json.loads(payload)["attributes"]["paymentType"] == 1
    body = {k: job[k] for k in ("id", "lease", "sha256", "thumbprint")}
    body["signature"] = base64.b64encode(b"TEST ONLY CMS MOCK " * 30).decode()
    wrong = {**body, "sha256": "0" * 64}
    assert c.post("/api/signer/result", json=wrong, headers=headers).status_code == 409
    assert c.post("/api/signer/result", json=body, headers=headers).status_code == 200
    assert c.post("/api/signer/result", json=body, headers=headers).status_code == 409
    assert c.post("/api/admin/auth/true", json={}).status_code == 403


@pytest.mark.parametrize(
    "crypto", ["\x1d93dGVz", "\x1d91EE12\x1d92" + "Ab+/_x001D_" * 4]
)
def test_vector_label_real_decode_and_dimensions(crypto):
    if crypto.startswith("\x1d91"):
        crypto = "\x1d91EE12\x1d92" + ("Ab+/_x001D_" * 5)[:44]
    raw = "01" + GTIN + "21" + '5pZpV1RbL3/uSA&!*"FB' + crypto
    assert len(raw[18:38]) == 20
    full = with_quantity(raw, 12345)
    assert parse(full).quantity == 12345 and parse(raw).cis == parse(full).cis
    content = render_label(full, "L-132XYD", 12345)
    page = PdfReader(BytesIO(content)).pages[0]
    assert abs(float(page.mediabox.width) - 58 * 72 / 25.4) < 0.01
    assert abs(float(page.mediabox.height) - 40 * 72 / 25.4) < 0.01
    pdf = fitz.open(stream=content, filetype="pdf")
    pix = pdf[0].get_pixmap(matrix=fitz.Matrix(4, 4), alpha=False)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    decoded = zxingcpp.read_barcodes(img)
    assert len(decoded) == 1
    assert decoded[0].bytes == full.encode()
    assert decoded[0].symbology_identifier == "]d2"
    assert raw[-10:] in full


def test_auth_hashes_and_history_immutable(env):
    _, s, _ = env
    h = hash_password("secret password")
    assert verify_password("secret password", h) and not verify_password("wrong", h)
    a = add_doc(s)
    with pytest.raises(Exception):
        with s.store.transaction() as db:
            db.execute("DELETE FROM audit WHERE entity=?", (a,))


def test_crypto_and_serial_keyboard_sequences_are_never_normalized():
    raw = (
        "01"
        + GTIN
        + "21"
        + '5^]91"[]\\_x001D_AbCd'
        + "\x1d91^]12\x1d92"
        + ('^]"_x001D_AbC+' * 4)[:44]
    )
    assert len(raw[18:38]) == 20
    assert parse(raw).full == raw
    assert with_quantity(raw, 17) == raw + "\x1d3017"
