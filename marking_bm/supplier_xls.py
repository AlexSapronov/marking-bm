"""Repeat the working codesOrder workbook, including OOXML inside .xls.

The template is the supplied workbook with all KMs and personal properties
removed. Only worksheet rows, its dimension/selection and shared strings change.
"""

from io import BytesIO
from pathlib import Path
import re
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

from .codes import parse


MAX_ROWS = 1048576
TEMPLATE = Path(__file__).with_name("supplier_codes_template.xls")


def _excel_text(value):
    # Escape literal Excel escape sequences first; real GS remains the same byte
    # when Excel decodes _x001D_, exactly as in the supplier's reference.
    value = re.sub(r"_x[0-9a-fA-F]{4}_", lambda m: "_x005F_" + m[0][1:], value)
    value = re.sub(r"[\x00-\x08\x0b-\x1f]", lambda m: f"_x{ord(m[0]):04X}_", value)
    return escape(value)


def codes_xls(items):
    """A=original full KM, B=GTIN, C=PN; Sheet1, strings, no headings/AI 30."""
    if not 1 <= len(items) <= MAX_ROWS:
        raise ValueError("В файле допустимо от 1 до 1 048 576 КМ на одном листе")
    values, indices = [], {}

    def intern(value):
        if value not in indices:
            indices[value] = len(values)
            values.append(value)
        return indices[value]

    rows = []
    for item in items:
        code = parse(item["raw"])
        if code.quantity is not None or code.gtin != item["gtin"]:
            raise ValueError("Для файла нужен исходный КМ СУЗ без AI 30 с соответствующим GTIN")
        rows.append([intern(code.full), intern(code.gtin), item["pn"]])
    for row in rows:
        row[2] = intern(row[2])
    strings = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
               '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
               f'count="{len(items) * 3}" uniqueCount="{len(values)}">'
               + ''.join(f'<si><t xml:space="preserve">{_excel_text(v)}</t></si>' for v in values)
               + '</sst>')
    sheet_data = '<sheetData>' + ''.join(
        f'<row r="{n}" spans="1:3" x14ac:dyDescent="0.25">'
        + ''.join(f'<c r="{col}{n}" t="s"><v>{i}</v></c>' for col, i in zip('ABC', row))
        + '</row>' for n, row in enumerate(rows, 1)
    ) + '</sheetData>'
    output = BytesIO()
    with ZipFile(TEMPLATE) as template, ZipFile(output, 'w', ZIP_DEFLATED) as book:
        for name in template.namelist():
            data = template.read(name)
            if name == 'xl/sharedStrings.xml':
                data = strings.encode('utf-8')
            elif name == 'xl/worksheets/sheet1.xml':
                sheet = data.decode('utf-8')
                sheet = re.sub(r'<sheetData\s*/>', lambda m: sheet_data, sheet)
                sheet = re.sub(r'<dimension ref="[^"]*"', f'<dimension ref="A1:C{len(rows)}"', sheet)
                sheet = re.sub(r'sqref="[^"]*"', f'sqref="C1:C{len(rows)}"', sheet)
                data = sheet.encode('utf-8')
            book.writestr(name, data)
    return output.getvalue()
