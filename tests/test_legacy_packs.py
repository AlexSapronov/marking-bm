"""Historical pack imports keep already emitted CIS and never generate new labels."""
from io import BytesIO
import json

import xlsxwriter
from pypdf import PdfWriter

from conftest import client
from marking_bm.codes import GS, parse, with_quantity
from marking_bm.legacy import normalize_legacy_code


GTIN = "04670553900537"
TN = "8541410004"


def make_xlsx(rows):
    buf = BytesIO()
    with xlsxwriter.Workbook(buf, {"in_memory": True}) as book:
        sheet = book.add_worksheet("DataMatrix")
        for r, values in enumerate(rows):
            for c, value in enumerate(values):
                if isinstance(value, str):
                    sheet.write_string(r, c, value)
                else:
                    sheet.write_number(r, c, value)
    return buf.getvalue()


def legacy_raw(serial):
    # Simulate the original packs: 90-byte 91/92 DataMatrix with missing GS.
    assert len(serial) == 20
    return "01" + GTIN + "21" + serial + "91EE12" + "92" + "g" * 44


def make_rows(n=2):
    return [(legacy_raw(f"S{index:019d}"), GTIN, "PN-EXACT", 3000, 8541410004)
            for index in range(n)]


def register_legacy(registry, rows):
    for raw, gtin, _, _, tnved in rows:
        code = normalize_legacy_code(raw)
        registry.codes[code.cis] = {
            "cis": code.cis, "requestedCis": code.cis, "gtin": gtin,
            "status": "EMITTED", "ownerInn": "7700000000",
            "emissionType": "FOREIGN", "statusEx": None,
            "tnVedEaes": str(tnved), "parent": None,
        }


def test_admin_legacy_preview_import_and_future_report_without_pdf(env):
    app, service, registry = env
    admin = client(app, "administrator", True)
    alice = client(app, "alice")
    source_rows = make_rows(2)
    register_legacy(registry, source_rows)
    content = make_xlsx(source_rows)
    preview = admin.post("/api/admin/legacy/preview",
                         files={"file": ("result_TLCWB.xlsx", content)})
    assert preview.status_code == 200, preview.text
    assert preview.json()["number"] == "TLCWB"
    assert preview.json()["codes"] == 2
    assert preview.json()["units"] == 6000
    assert preview.json()["lines"][0]["labels"] == 2
    assert preview.json()["verification"]["allowed"] is True
    assert preview.json()["verification"]["status_counts"] == {"EMITTED": 2}
    assert preview.json()["verification"]["errors"] == []
    assert [p for p in registry.calls if "/cises/info" in p[1]]
    assert alice.post("/api/admin/legacy/preview",
                      files={"file": ("result_TLCWB.xlsx", content)}).status_code == 403

    result = admin.post("/api/admin/legacy/import",
                        files={"file": ("result_TLCWB.xlsx", content)},
                        data={"supplier": "Legacy Supplier", "owner": "alice"})
    assert result.status_code == 200, result.text
    doc_id = result.json()["id"]
    d = alice.get(f"/api/documents/{doc_id}").json()
    assert d["status"] == "transferred"
    assert d["owner"] == "alice"
    assert d["current_version"] == 0 and d["transferred_version"] == 0
    assert len(d["codes"]) == 2
    assert all(x["local_state"] == "reserved" for x in d["codes"])
    assert len([x for x in d["artifacts"] if x["kind"] == "source"]) == 1
    assert not [x for x in d["artifacts"] if x["kind"] == "labels"]
    assert service.store.one("SELECT COUNT(*) n FROM operations")["n"] == 0
    for x in service.store.all("SELECT raw,cis FROM codes WHERE doc_id=?", (doc_id,)):
        assert parse(x["raw"]).cis == x["cis"]
    assert admin.post("/api/admin/legacy/import",
                      files={"file": ("result_TLCWB.xlsx", content)},
                      data={"supplier": "Legacy Supplier", "owner": "alice"}).status_code == 409
    assert admin.post("/api/admin/legacy/import",
                      files={"file": ("result_DUPLICATE.xlsx", content)},
                      data={"supplier": "Legacy Supplier", "owner": "alice"}).status_code == 409

    batch = alice.post("/api/batches", json={
        "documents": [doc_id], "production_date": "2026-01-01"
    })
    assert batch.status_code == 200, batch.text
    op = service.store.one("SELECT payload,state FROM operations WHERE kind='report' AND doc_id=?",
                           (doc_id,))
    payload = json.loads(op["payload"])
    assert len(payload["sntins"]) == 2
    assert all(parse(x).quantity == 3000 for x in payload["sntins"])
    assert not service.store.one(
        "SELECT id FROM artifacts WHERE doc_id=? AND kind='labels'", (doc_id,)
    )


def test_legacy_file_validation_and_duplicate_codes(env):
    app, service, registry = env
    admin = client(app, "administrator", True)
    rows = make_rows()
    register_legacy(registry, rows)
    for bad_name in ("TLCWB.xlsx", "../result_bad.xlsx", "result_$.xlsx"):
        response = admin.post("/api/admin/legacy/preview",
                              files={"file": (bad_name, make_xlsx(rows))})
        if bad_name == "../result_bad.xlsx":
            # A POSIX browser path is normalized to the basename; accepted.
            assert response.status_code == 200
        else:
            assert response.status_code == 422
    duplicated = make_xlsx([rows[0], rows[0]])
    assert admin.post("/api/admin/legacy/preview",
                      files={"file": ("result_DUP.xlsx", duplicated)}).status_code == 422
    mismatch = make_xlsx([(rows[0][0], "04670553902487", *rows[0][2:])])
    response = admin.post("/api/admin/legacy/preview",
                          files={"file": ("result_BAD.xlsx", mismatch)})
    assert response.status_code == 422
    assert "GTIN" in response.json()["detail"]


def test_legacy_pdf_attachment_does_not_generate_label_zip(env):
    app, service, registry = env
    admin = client(app, "administrator", True)
    sample_rows = make_rows(1)
    register_legacy(registry, sample_rows)
    content = make_xlsx(sample_rows)
    imported = admin.post("/api/admin/legacy/import",
                          files={"file": ("result_OLD.pdfproof.xlsx", content)},
                          data={"supplier": "Supplier", "owner": "alice"})
    assert imported.status_code == 422  # filename restricted
    imported = admin.post("/api/admin/legacy/import",
                          files={"file": ("result_OLD.xlsx", content)},
                          data={"supplier": "Supplier", "owner": "alice"})
    assert imported.status_code == 200, imported.text
    doc_id = imported.json()["id"]
    pdf = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=168, height=113)
    writer.write(pdf)
    uploaded = admin.post(f"/api/admin/legacy/{doc_id}/pdf",
                          files={"file": ("old-labels.pdf", pdf.getvalue(), "application/pdf")})
    assert uploaded.status_code == 200, uploaded.text
    doc = service.get(doc_id, "administrator", admin=True)
    assert any(x["kind"] == "legacy_pdf" for x in doc["artifacts"])
    assert not any(x["kind"] == "labels" for x in doc["artifacts"])
    assert client(app, "alice").post(f"/api/admin/legacy/{doc_id}/pdf",
                                    files={"file": ("a.pdf", pdf.getvalue())}).status_code == 403


def test_editing_imported_document_enables_normal_label_generation(env):
    app, service, registry = env
    admin = client(app, "administrator", True)
    sample_rows = make_rows(1)
    register_legacy(registry, sample_rows)
    content = make_xlsx(sample_rows)
    created = admin.post("/api/admin/legacy/import",
                         files={"file": ("result_EDITME.xlsx", content)},
                         data={"supplier": "Supplier", "owner": "alice"})
    assert created.status_code == 200, created.text
    doc_id = created.json()["id"]
    stored = service.store.one("SELECT * FROM codes WHERE doc_id=?", (doc_id,))
    registry.codes[stored["cis"]] = {
        "cis": stored["cis"], "requestedCis": stored["cis"], "gtin": GTIN,
        "status": "EMITTED", "ownerInn": "7700000000",
        "emissionType": "FOREIGN", "statusEx": None, "tnVedEaes": TN, "parent": None,
    }
    assert not service.store.one(
        "SELECT id FROM artifacts WHERE kind='labels' AND doc_id=?", (doc_id,)
    )
    new_rows = [["PN-REVISED", GTIN, 3000, 1, TN]]
    preview = service.preview(doc_id, "alice", new_rows)
    after = service.save_lines(
        doc_id, "alice", new_rows, revision=1,
        confirmation=preview["fingerprint"],
    )
    assert after["status"] == "ready"
    assert after["current_version"] == 1
    labels = service.store.one(
        "SELECT name,kind FROM artifacts WHERE kind='labels' AND doc_id=?", (doc_id,)
    )
    assert labels and labels["name"].endswith("-v1.zip")
    assert not registry.orders  # Existing legacy KM reused instead of emission


def test_import_blocked_if_any_km_already_applied_or_introduced(env):
    app, service, registry = env
    admin = client(app, "administrator", True)
    sample_rows = make_rows(3)
    register_legacy(registry, sample_rows)
    registry.codes[normalize_legacy_code(sample_rows[1][0]).cis]["status"] = "APPLIED"
    registry.codes[normalize_legacy_code(sample_rows[2][0]).cis]["status"] = "INTRODUCED"
    content = make_xlsx(sample_rows)
    preview = admin.post("/api/admin/legacy/preview",
                         files={"file": ("result_STATUSES.xlsx", content)})
    assert preview.status_code == 200, preview.text
    check = preview.json()["verification"]
    assert not check["allowed"]
    assert check["checked"] == 3
    assert check["status_counts"] == {"EMITTED": 1, "APPLIED": 1, "INTRODUCED": 1}
    assert check["errors_count"] == 2
    assert "уже нанесён" in check["errors"][0]["reason"]
    assert "введён в оборот" in check["errors"][1]["reason"]
    resp = admin.post("/api/admin/legacy/import",
                      files={"file": ("result_STATUSES.xlsx", content)},
                      data={"supplier": "Supplier", "owner": "alice"})
    assert resp.status_code == 409, resp.text
    assert "Импорт запрещён" in resp.json()["detail"]
    assert not service.store.one("SELECT id FROM documents WHERE number='STATUSES'")
    assert not service.store.one("SELECT id FROM codes WHERE source_op LIKE 'legacy:%'")
    assert not service.store.one("SELECT id FROM artifacts WHERE name='result_STATUSES.xlsx'")


def test_import_checks_again_after_preview_and_refuses_changed_status(env):
    app, service, registry = env
    admin = client(app, "administrator", True)
    rows = make_rows(2)
    register_legacy(registry, rows)
    content = make_xlsx(rows)
    filename = "result_CHANGED.xlsx"
    preview = admin.post("/api/admin/legacy/preview",
                         files={"file": (filename, content)})
    assert preview.status_code == 200 and preview.json()["verification"]["allowed"]
    registry.codes[normalize_legacy_code(rows[0][0]).cis]["status"] = "APPLIED"
    denied = admin.post("/api/admin/legacy/import",
                        files={"file": (filename, content)},
                        data={"supplier": "Supplier", "owner": "alice"})
    assert denied.status_code == 409
    assert "уже нанесён" in denied.json()["detail"]
    assert not service.store.one("SELECT id FROM documents WHERE number='CHANGED'")
    assert sum(1 for call in registry.calls if "/cises/info" in call[1]) == 2


def test_unknown_or_foreign_legacy_km_must_fail_closed(env):
    app, service, registry = env
    admin = client(app, "administrator", True)
    rows = make_rows(2)
    content = make_xlsx(rows)
    filename = "result_FOREIGN.xlsx"
    register_legacy(registry, rows)
    cis0 = normalize_legacy_code(rows[0][0]).cis
    cis1 = normalize_legacy_code(rows[1][0]).cis
    registry.codes[cis0]["status"] = "UNKNOWN"
    registry.codes[cis1]["ownerInn"] = "7900000000"
    r = admin.post("/api/admin/legacy/preview",
                   files={"file": (filename, content)})
    assert r.status_code == 200, r.text
    result = r.json()["verification"]
    assert result["errors_count"] == 2 and not result["allowed"]
    assert "EMITTED" in result["errors"][0]["reason"]
    assert "организации" in result["errors"][1]["reason"]
    assert admin.post("/api/admin/legacy/import",
                      files={"file": (filename, content)},
                      data={"supplier": "Supplier", "owner": "alice"}).status_code == 409
    assert service.store.one("SELECT COUNT(*) n FROM documents")["n"] == 0


def test_introductory_extra_status_blocks_even_when_primary_emitted(env):
    app, service, registry = env
    admin = client(app, "administrator", True)
    rows = make_rows(1)
    register_legacy(registry, rows)
    cis = normalize_legacy_code(rows[0][0]).cis
    registry.codes[cis]["statusEx"] = "IN_CIRCULATION"
    r = admin.post("/api/admin/legacy/preview",
                   files={"file": ("result_EXTRA.xlsx", make_xlsx(rows))})
    assert r.status_code == 200
    check = r.json()["verification"]
    assert not check["allowed"]
    assert "введён в оборот" in check["errors"][0]["reason"]


def test_verified_import_records_status_check_time(env):
    app, service, registry = env
    admin = client(app, "administrator", True)
    rows = make_rows(1)
    register_legacy(registry, rows)
    result = admin.post("/api/admin/legacy/import",
                        files={"file": ("result_CHECKED.xlsx", make_xlsx(rows))},
                        data={"supplier": "Supplier", "owner": "alice"})
    assert result.status_code == 200, result.text
    record = service.store.one("SELECT checked,external_status FROM codes WHERE doc_id=?",
                               (result.json()["id"],))
    assert record["external_status"] == "EMITTED"
    assert record["checked"] is not None


def test_compact_legacy_ai30_is_validated_and_normalized_for_future_reports():
    # Mirrors the real result_7N5D8.xlsx rows 370-372: 90 compact characters
    # followed by 305000, with Excel column D containing 5000.
    original = legacy_raw("S0000000000000000001")
    compact = original + "305000"
    code = normalize_legacy_code(compact, 5000)
    assert code.quantity is None
    assert code.full == normalize_legacy_code(original).full
    assert code.full == original[:38] + GS + original[38:44] + GS + original[44:]
    assert parse(with_quantity(code.full, 5000)).quantity == 5000
    assert parse(with_quantity(code.full, 5000)).cis == code.cis
    # GS-formatted input with AI 30 must also be accepted and stripped.
    assert normalize_legacy_code(code.full + GS + "305000", 5000).full == code.full


def test_compact_legacy_ai30_quantity_must_match_excel(env):
    app, service, registry = env
    admin = client(app, "administrator", True)
    source = legacy_raw("S0000000000000000002")
    one_bad = [(source + "305000", GTIN, "PN-EXACT", 5001, 8541410004)]
    response = admin.post(
        "/api/admin/legacy/preview",
        files={"file": ("result_MISMATCH.xlsx", make_xlsx(one_bad))},
    )
    assert response.status_code == 422
    assert "Строка 1" in response.json()["detail"]
    assert "AI 30 = 5000" in response.json()["detail"]
    assert "5001" in response.json()["detail"]
    # Parsing happens before any True API calls or storage, fail closed.
    assert not registry.calls
    assert not service.store.one("SELECT id FROM documents WHERE number='MISMATCH'")


def test_mixed_legacy_file_with_ai30_and_no_ai30_passes_server_preview(env):
    app, service, registry = env
    admin = client(app, "administrator", True)
    rows = make_rows(5)
    rows[3] = (rows[3][0] + "305000", GTIN, "PN-EXACT", 5000, 8541410004)
    rows[4] = (rows[4][0] + "305000", GTIN, "PN-EXACT", 5000, 8541410004)
    register_legacy(registry, rows[:3])
    # Register the compact 96-char originals by canonical CIS, not the appended AI30.
    for original in rows[3:]:
        raw, gtin, pn, qty, tnved = original
        code = normalize_legacy_code(raw, qty)
        registry.codes[code.cis] = {
            "cis": code.cis, "requestedCis": code.cis, "gtin": gtin,
            "status": "EMITTED", "ownerInn": "7700000000",
            "emissionType": "FOREIGN", "statusEx": None,
            "tnVedEaes": str(tnved), "parent": None,
        }
    content = make_xlsx(rows)
    preview = admin.post(
        "/api/admin/legacy/preview",
        files={"file": ("result_MIXED.xlsx", content)},
    )
    assert preview.status_code == 200, preview.text
    data = preview.json()
    assert data["codes"] == 5
    assert data["verification"]["status_counts"] == {"EMITTED": 5}
    assert data["verification"]["allowed"]
    imported = admin.post(
        "/api/admin/legacy/import",
        files={"file": ("result_MIXED.xlsx", content)},
        data={"supplier": "Supplier", "owner": "alice"},
    )
    assert imported.status_code == 200, imported.text
    imported_codes = service.store.all(
        "SELECT raw,quantity FROM codes WHERE doc_id=?", (imported.json()["id"],)
    )
    assert len(imported_codes) == 5
    assert sum(c["quantity"] for c in imported_codes) == 19000
    assert all(parse(c["raw"]).quantity is None for c in imported_codes)
