"""Historical pack imports keep already emitted CIS and never generate new labels."""
from io import BytesIO
import json

import xlsxwriter
from pypdf import PdfWriter

from conftest import client
from marking_bm.codes import parse
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
