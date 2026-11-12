"""Shared test helpers: temporary register homes and synthetic .ods workbooks."""
import contextlib
import io
import os
import tempfile
import zipfile
from pathlib import Path
from unittest import mock
from xml.sax.saxutils import escape, quoteattr

from register import __main__ as cli
from register.core import admin, gitrepo
from register.core.store import Home

PASSWORD = "correct horse battery"
FAST_N = 2 ** 10  # cheap scrypt for tests; production uses 2^17


def make_home(test, password=PASSWORD):
    """Create an initialized, encrypted home in a temp dir removed after the test.
    home.key is the unlocked key for `password`."""
    tmp = tempfile.TemporaryDirectory(prefix="register-test-")
    test.addCleanup(tmp.cleanup)
    home = Home(Path(tmp.name) / "home")
    admin.init_home(home, password, "test", "test@localhost", n=FAST_N)
    home.key = home.unlock(password)
    return home


def run_import_cli(home, *argv, tty=False, answer=None):
    """Run `register import --home HOME ...` in-process with the test password.
    Returns (exit code, stdout + exit message, whether the y/N question was asked)."""
    out = io.StringIO()
    argv = ["import", "--home", str(home.root)] + [str(a) for a in argv]
    with mock.patch.dict(os.environ, {"REGISTER_PASSWORD": PASSWORD}), contextlib.redirect_stdout(out), \
            mock.patch("sys.stdin") as stdin, mock.patch("builtins.input", return_value=answer) as asked:
        stdin.isatty.return_value = tty
        try:
            cli.main(argv)
            code = 0
        except SystemExit as e:
            code, _ = e.code, out.write(str(e.code))
    return code, out.getvalue(), asked.called


def git_log(home):
    return gitrepo._run(home.data, "log", "--format=%s").splitlines()


# ---------------------------------------------------------------- .ods builder

_NS = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
       'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
       'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"')


def s(text):
    return ("string", text)


def cur(value, shown=None, formula=None):
    return ("currency", value, shown, formula)


def num(value):
    return ("float", value)


def date(iso, shown):
    return ("date", iso, shown)


def _cell(c):
    if c is None:
        return "<table:table-cell/>"
    kind = c[0]
    if kind == "string":
        return '<table:table-cell office:value-type="string"><text:p>%s</text:p></table:table-cell>' % escape(c[1])
    if kind == "date":
        return ('<table:table-cell office:value-type="date" office:date-value="%s"><text:p>%s</text:p>'
                '</table:table-cell>' % (c[1], c[2]))
    if kind == "float":
        return ('<table:table-cell office:value-type="float" office:value="%s"><text:p>%s</text:p>'
                '</table:table-cell>' % (c[1], c[1]))
    value, shown, formula = c[1], c[2], c[3]
    shown = shown or "${:,.2f}".format(float(value)).replace("$-", "-$")
    f = " table:formula=%s" % quoteattr(formula) if formula else ""
    return ('<table:table-cell office:value-type="currency" office:currency="USD" office:value="%s"%s>'
            '<text:p>%s</text:p></table:table-cell>' % (value, f, escape(shown)))


def write_ods(path, sheets):
    """sheets: {name: [row, ...]} where row is a list of cells (None = empty)."""
    tables = []
    for name, rows in sheets.items():
        body = "".join("<table:table-row>%s<table:table-cell table:number-columns-repeated=\"1000\"/>"
                       "</table:table-row>" % "".join(_cell(c) for c in row) for row in rows)
        body += '<table:table-row table:number-rows-repeated="1048000"><table:table-cell/></table:table-row>'
        tables.append("<table:table table:name=%s>%s</table:table>" % (quoteattr(name), body))
    content = ('<?xml version="1.0" encoding="UTF-8"?><office:document-content %s><office:body>'
               '<office:spreadsheet>%s</office:spreadsheet></office:body></office:document-content>'
               % (_NS, "".join(tables)))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet", compress_type=zipfile.ZIP_STORED)
        z.writestr("content.xml", content)


def savings_rows(opening, entries):
    """Build savings rows: opening balance in C1, then entries of (date_iso, amount, desc)
    with a running balance formula in C."""
    rows = [[None, None, cur(opening)]]
    bal = float(opening)
    for n, (iso, amount, desc) in enumerate(entries):
        bal = round(bal + float(amount), 2)
        y, m, d = iso.split("-")
        rows.append([date(iso, "%s/%s/%s" % (m, d, y[2:])), cur(amount),
                     cur("%.2f" % bal, formula="of:=[.C%d]+[.B%d]" % (n + 1, n + 2)), s(desc)])
    return rows


def checking_rows(opening, entries, header=True):
    """Build checking rows in the workbook's layout: header row with opening balance in H1,
    a blank row, then entries of (num, date_iso, desc, debit, x, credit, note_i, note_j)
    with a running balance formula in H."""
    rows = [[s("NUMBER"), s("DATE"), s("TRANSACTION DESCRIPTION"), s("AMOUNT (-)"), s("(X)"),
             s("AMOUNT (+)"), None, cur(opening)]] if header else []
    rows.append([])
    bal = float(opening)
    for n, (no, iso, desc, debit, x, credit, note_i, note_j) in enumerate(entries):
        r = n + 3
        bal = round(bal - float(debit or 0) + float(credit or 0), 2)
        y, m, d = iso.split("-")
        rows.append([
            num(no) if no else None, date(iso, "%s/%s/%s" % (m, d, y[2:])), s(desc),
            cur(debit) if debit else None, s(x) if x else None, cur(credit) if credit else None, None,
            cur("%.2f" % bal, formula="of:=([.H%d]-[.D%d]+[.F%d])" % (r - 2 if n == 0 else r - 1, r, r)),
            note_i, note_j,
        ])
    return rows


# ---------------------------------------------------------------- .xlsx builder

_XLSX_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_XLSX_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def _xlsx_formula(odf):
    """'of:=([.H1]-[.D3])' -> '(H1-D3)'; "[$'2025'.H5]" -> "'2025'!H5"."""
    import re
    text = odf[4:] if odf.startswith("of:=") else odf
    return re.sub(r"\[\$?([^\].]*)\.([^\]]+)\]", lambda m: (m.group(1) + "!" if m.group(1) else "") + m.group(2),
                  text)


def _serial(iso):
    import datetime
    return (datetime.date.fromisoformat(iso) - datetime.date(1899, 12, 30)).days


def write_xlsx(path, sheets, shared_strings=True, extra_parts=None):
    """The same rows as write_ods (s/cur/num/date cells), as an Excel workbook: text as shared
    strings (or inline), dates as serial numbers with a date style, formulas in Excel syntax."""
    strings, index = [], {}

    def col(i):
        out, i = "", i + 1
        while i:
            i, r = divmod(i - 1, 26)
            out = chr(65 + r) + out
        return out

    sheet_xml = []
    for rows in sheets.values():
        body = []
        for r, row in enumerate(rows, 1):
            cells = []
            for c, cell in enumerate(row):
                if cell is None:
                    continue
                ref = "%s%d" % (col(c), r)
                kind = cell[0]
                if kind == "string":
                    if shared_strings:
                        n = index.setdefault(cell[1], len(strings))
                        if n == len(strings):
                            strings.append(cell[1])
                        cells.append('<c r="%s" t="s"><v>%d</v></c>' % (ref, n))
                    else:
                        cells.append('<c r="%s" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>'
                                     % (ref, escape(cell[1])))
                elif kind == "date":
                    cells.append('<c r="%s" s="1"><v>%d</v></c>' % (ref, _serial(cell[1])))
                elif kind == "float":
                    cells.append('<c r="%s"><v>%s</v></c>' % (ref, cell[1]))
                else:
                    f = "<f>%s</f>" % escape(_xlsx_formula(cell[3])) if cell[3] else ""
                    cells.append('<c r="%s" s="2">%s<v>%s</v></c>' % (ref, f, cell[1]))
            body.append('<row r="%d">%s</row>' % (r, "".join(cells)))
        sheet_xml.append('<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="%s"><sheetData>%s'
                         '</sheetData></worksheet>' % (_XLSX_MAIN, "".join(body)))
    names = list(sheets)
    workbook = ('<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="%s" xmlns:r="%s"><sheets>%s</sheets>'
                '</workbook>' % (_XLSX_MAIN, _XLSX_REL, "".join(
                    '<sheet name=%s sheetId="%d" r:id="rId%d"/>' % (quoteattr(n), i + 1, i + 1)
                    for i, n in enumerate(names))))
    rels = ['<Relationship Id="rId%d" Type="%s/worksheet" Target="worksheets/sheet%d.xml"/>' % (i + 1, _XLSX_REL, i + 1)
            for i in range(len(names))]
    rels.append('<Relationship Id="rIdS" Type="%s/styles" Target="styles.xml"/>' % _XLSX_REL)
    if strings:
        rels.append('<Relationship Id="rIdT" Type="%s/sharedStrings" Target="sharedStrings.xml"/>' % _XLSX_REL)
    styles = ('<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="%s"><numFmts count="1">'
              '<numFmt numFmtId="164" formatCode="&quot;$&quot;#,##0.00"/></numFmts><cellXfs count="3">'
              '<xf numFmtId="0"/><xf numFmtId="14"/><xf numFmtId="164"/></cellXfs></styleSheet>' % _XLSX_MAIN)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/content-types"/>')
        z.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="%s"><Relationship Id="rId1" '
                   'Type="%s/officeDocument" Target="xl/workbook.xml"/></Relationships>' % (_PKG_REL, _XLSX_REL))
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0"?><Relationships xmlns="%s">%s'
                   '</Relationships>' % (_PKG_REL, "".join(rels)))
        z.writestr("xl/styles.xml", styles)
        if strings:
            z.writestr("xl/sharedStrings.xml", '<?xml version="1.0"?><sst xmlns="%s">%s</sst>' % (
                _XLSX_MAIN, "".join('<si><t xml:space="preserve">%s</t></si>' % escape(t) for t in strings)))
        for i, xml in enumerate(sheet_xml):
            z.writestr("xl/worksheets/sheet%d.xml" % (i + 1), xml)
        for name, data in (extra_parts or {}).items():
            z.writestr(name, data)
