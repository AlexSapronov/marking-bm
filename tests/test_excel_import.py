"""Import regression tests: inputs are generated locally; no operations sent to GIS MT."""

from io import BytesIO
import json
import warnings
import openpyxl
import pytest
import xlsxwriter
from marking_bm.excel import HEADERS, FIELDS, import_file, validate_rows, template, xlsx
from conftest import GTIN, TN, client

MAPPING = {"pn": 0, "gtin": 1, "quantity": 2, "labels": 3, "tnved": 4}


def test_original_user_cell_types_no_longer_fail():
    # Same mixed storage as the attached file, using public test identifiers only.
    content = xlsx(
        {
            "Товары": [
                HEADERS,
                ["P", GTIN, "1000", "2", int(TN)],
                ["P", GTIN, "500", "2", int(TN)],
                ["P", GTIN, "100", "5", int(TN)],
            ]
        }
    )
    result = import_file(content)
    assert not result["requires_mapping"] and not result["errors"]
    assert [l["quantity"] for l in result["lines"]] == [1000, 500, 100]
    assert all(l["tnved"] == TN and type(l["labels"]) is int for l in result["lines"])
    assert sum(l["quantity"] * l["labels"] for l in result["lines"]) == 3500


@pytest.mark.parametrize(
    "value",
    [
        1000,
        1000.0,
        "1000",
        " 1000 ",
        "1 000",
        "1\u00a0000",
        "1\u202f000",
        "1000.000",
        "1000,00",
        "1e3",
    ],
)
def test_integer_quantities_are_parsed_by_meaning(value):
    result = validate_rows([["P", GTIN, value, "2", int(TN)]])
    assert not result["errors"]
    assert result["lines"][0]["quantity"] == 1000


@pytest.mark.parametrize(
    "value",
    [
        True,
        None,
        "",
        "1.5",
        "1,5",
        -1,
        0,
        "1 00",
        "many",
        "NaN",
        float("inf"),
        "1e999999",
    ],
)
def test_invalid_quantities_are_never_rounded_or_guessed(value):
    assert validate_rows([["P", GTIN, value, 2, TN]])["errors"]


def test_identifiers_preserve_text_zeros_and_recover_numeric_zeros():
    result = validate_rows([[123, int(GTIN), "5.0", 2, 123456789]])
    assert not result["errors"]
    line = result["lines"][0]
    assert (
        line["pn"] == "123" and line["gtin"] == GTIN and line["tnved"] == "0123456789"
    )
    assert result["warnings"]
    assert validate_rows([["P", GTIN + "1", 5, 2, TN]])["errors"]
    assert validate_rows([["P", GTIN, 5, 2, "12345678901"]])["errors"]


def test_aliases_order_extra_columns_and_title_rows_autodetect():
    content = xlsx(
        {
            "Data": [
                ["Invoice export"],
                [
                    "Other",
                    "HS Code",
                    "Label count",
                    "Part number",
                    "Qty per pack",
                    "EAN",
                ],
                ["ignored", TN, "2", "PN-A", "5", GTIN],
            ]
        }
    )
    result = import_file(content)
    assert not result["requires_mapping"] and not result["errors"]
    assert result["header_row"] == 2 and result["mapping"] == {
        "pn": 3,
        "gtin": 5,
        "quantity": 4,
        "labels": 2,
        "tnved": 1,
    }
    assert result["lines"][0]["pn"] == "PN-A"


def test_unknown_or_duplicate_headers_require_explicit_mapping():
    content = xlsx(
        {
            "Custom": [
                ["Article", "Code", "Amount", "Boxes", "Customs"],
                ["P", GTIN, "5", "3", int(TN)],
            ]
        }
    )
    initial = import_file(content)
    assert initial["requires_mapping"] and initial["lines"] == []
    assert set(initial["fields"]) == set(FIELDS)
    assert initial["sheets"][0]["preview"][1]["values"][0] == "P"
    result = import_file(content, mapping=MAPPING, sheet_name="Custom", header_row=1)
    assert not result["errors"] and result["lines"][0]["labels"] == 3
    duplicate = xlsx(
        {
            "Duplicate": [
                ["PN", "GTIN", "GTIN", *HEADERS[2:]],
                ["P", GTIN, GTIN, 5, 2, TN],
            ]
        }
    )
    assert import_file(duplicate)["requires_mapping"]


def test_sheet_selection_and_no_header_row():
    content = xlsx(
        {"A": [HEADERS, ["P", GTIN, 5, 2, TN]], "B": [HEADERS, ["Q", GTIN, 7, 3, TN]]}
    )
    assert import_file(content)["requires_mapping"]
    chosen = import_file(content, MAPPING, "B", 1)
    assert chosen["lines"][0]["pn"] == "Q"
    headerless = xlsx({"Plain": [["P", GTIN, "5", "2", int(TN)]]})
    assert import_file(headerless)["requires_mapping"]
    chosen = import_file(headerless, MAPPING, "Plain", 0)
    assert chosen["lines"][0]["pn"] == "P"


@pytest.mark.parametrize(
    "mapping",
    [
        {"pn": 0},
        dict(MAPPING, quantity=0),
        dict(MAPPING, quantity=True),
        dict(MAPPING, quantity=999),
        dict(MAPPING, quantity="2"),
        dict(MAPPING, unexpected=3),
        [],
    ],
)
def test_mapping_must_be_complete_distinct_and_in_range(mapping):
    content = xlsx({"Data": [HEADERS, ["P", GTIN, 5, 2, TN]]})
    with pytest.raises(ValueError):
        import_file(content, mapping, "Data", 1)


def test_physical_row_numbers_and_blank_unmapped_columns():
    content = xlsx(
        {
            "Data": [
                ["Title"],
                [
                    "PN",
                    "GTIN",
                    "Кол-во в 1 этикетке",
                    "Кол-во этикеток",
                    "Код ТН ВЭД",
                    "Notes",
                ],
                [None, None, None, None, None, "Ignore this note"],
                ["P", GTIN, "1.5", "3", int(TN)],
            ]
        }
    )
    result = import_file(content)
    assert [e["row"] for e in result["errors"]] == [4]


def test_number_display_styles_do_not_change_values_and_formulas_stay_errors():
    stream = BytesIO()
    wb = xlsxwriter.Workbook(stream, {"in_memory": True})
    ws = wb.add_worksheet("Data")
    date_format = wb.add_format({"num_format": "yyyy-mm-dd"})
    for i, head in enumerate(HEADERS):
        ws.write_string(0, i, head)
    ws.write_string(1, 0, "P")
    ws.write_number(1, 1, int(GTIN), date_format)
    ws.write_number(1, 2, 5, date_format)
    ws.write_number(1, 3, 2, date_format)
    ws.write_number(1, 4, int(TN), date_format)
    ws.write_string(2, 0, "Q")
    ws.write_string(2, 1, GTIN)
    ws.write_formula(2, 2, "=2+3", None, 5)
    ws.write_number(2, 3, 2)
    ws.write_string(2, 4, TN)
    wb.close()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        result = import_file(stream.getvalue())
    assert (
        result["lines"][0]["gtin"] == GTIN
        and result["lines"][0]["quantity"] == 5
        and result["lines"][0]["tnved"] == TN
    )
    assert (
        len(result["errors"]) == 1
        and result["errors"][0]["row"] == 3
        and "Формула" in result["errors"][0]["message"]
    )


def test_template_assigns_numeric_quantity_and_text_identifier_columns():
    book = openpyxl.load_workbook(BytesIO(template()))
    ws = book.active

    def effective_format(index):
        return next(
            d.number_format
            for d in ws.column_dimensions.values()
            if d.min <= index <= d.max
        )

    assert [effective_format(i) for i in range(1, 6)] == ["@", "@", "0", "0", "@"]


def test_mapping_api_keeps_one_original_file_and_draft(env):
    app, s, r = env
    doc = s.create("alice", "IMPORT", "Supplier")
    c = client(app)
    content = xlsx(
        {
            "Custom": [
                ["Model", "Product", "Amount", "Labels", "Customs"],
                ["P", GTIN, "5", "3", int(TN)],
            ]
        }
    )
    files = {
        "file": (
            "custom.xlsx",
            content,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    }
    initial = c.post("/api/documents/" + doc + "/import", files=files)
    assert initial.status_code == 200 and initial.json()["requires_mapping"]
    response = c.post(
        "/api/documents/" + doc + "/import",
        files=files,
        data={"mapping": json.dumps(MAPPING), "sheet": "Custom", "header_row": "1"},
    )
    assert response.status_code == 200, response.text
    parsed = response.json()
    assert not parsed["errors"] and len(parsed["lines"]) == 1
    assert parsed["source_id"] == initial.json()["source_id"]
    assert (
        s.store.one(
            "SELECT COUNT(*) n FROM artifacts WHERE doc_id=? AND kind='source'", (doc,)
        )["n"]
        == 1
    )
    current = s.get(doc, "alice")
    assert current["status"] == "draft" and current["lines"] == [] and not r.orders
    assert (
        c.post(
            "/api/documents/" + doc + "/import", files=files, data={"mapping": "{bad"}
        ).status_code
        == 422
    )
    assert (
        client(app, "bob")
        .post("/api/documents/" + doc + "/import", files=files)
        .status_code
        == 403
    )
    saved = c.post(
        "/api/documents/" + doc + "/lines",
        json={
            "rows": [
                [l["pn"], l["gtin"], l["quantity"], l["labels"], l["tnved"]]
                for l in parsed["lines"]
            ],
            "revision": 0,
        },
    )
    assert saved.status_code == 200 and saved.json()["units"] == 15
