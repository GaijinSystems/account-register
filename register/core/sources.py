"""Read import sources (.ods or .xlsx workbooks, or CSV files) into a common grid of cells.

A grid is {(row, col): Cell} with 0-based row/col. Workbook cells carry the stored value (exact
number, ISO date); CSV cells are always text. The file type is detected from the content, not
the name (detect()).
"""
import codecs
import csv
import datetime
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import namedtuple
from decimal import Decimal
from pathlib import Path

Cell = namedtuple("Cell", "kind value date text formula", defaults=(None,))  # kind: string/float/currency/date/...

MAX_CONTENT_BYTES = 200 * 1024 * 1024
MAX_REPEAT = 10000  # repeated rows/cells with content beyond this are an error
MAX_CELLS = 2_000_000  # non-empty cells per workbook
MAX_SPACES = 10000  # text:s run length
MAX_CELL_TEXT = 100_000  # characters of text in one cell
MAX_WORKBOOK_TEXT = 20_000_000  # characters of cell text in the whole workbook
MAX_ROWS, MAX_COLS = 1048576, 16384  # LibreOffice's sheet size; content beyond it is refused
MAX_CSV_BYTES = 100 * 1024 * 1024
MAX_PARTS_BYTES = 400 * 1024 * 1024  # all XML parts read from one .xlsx, uncompressed

_NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
}


def _q(prefix, name):
    return "{%s}%s" % (_NS[prefix], name)


_CELL, _COVERED, _ROW = _q("table", "table-cell"), _q("table", "covered-table-cell"), _q("table", "table-row")
_ROW_GROUPS = {_q("table", n) for n in ("table-header-rows", "table-row-group", "table-rows")}


class SourceError(ValueError):
    pass


class Grid(dict):
    """{(row, col): Cell}. For CSV, widths[row] is the number of fields on that line, so the
    importer can check that the rows it reads all have the same shape."""
    widths = None


def parse_ref(ref):
    """'H1' -> (0, 7)."""
    m = re.fullmatch(r"([A-Z]{1,3})([1-9]\d{0,6})", ref or "")
    if not m:
        raise SourceError("not a cell reference: %r" % ref)
    return int(m.group(2)) - 1, col_index(m.group(1))


def col_index(letters):
    if not re.fullmatch(r"[A-Z]{1,3}", letters or ""):
        raise SourceError("not a column letter: %r" % letters)
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def col_letter(i):
    s, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def ref(row, col):
    return "%s%d" % (col_letter(col), row + 1)


# ---------------------------------------------------------------- .ods

def _count(el, attr):
    """A positive-integer repeat attribute (default 1); SourceError if malformed."""
    raw = el.get(attr, "1")
    if not re.fullmatch(r"[0-9]{1,9}", raw) or int(raw) < 1:
        raise SourceError("invalid repeat count %r" % raw)
    return int(raw)


def _para_text(el, budget):
    """Text of a paragraph element. budget is [characters left in this cell, characters left in
    the workbook], shared by all paragraphs, so expansion stops before it gets large."""
    out = []

    def add(s):
        budget[0] -= len(s)
        budget[1] -= len(s)
        if budget[0] < 0:
            raise SourceError("a cell has more than %d characters of text" % MAX_CELL_TEXT)
        if budget[1] < 0:
            raise SourceError("the workbook has more than %d characters of text" % MAX_WORKBOOK_TEXT)
        out.append(s)

    add(el.text or "")
    for child in el:
        if child.tag == _q("text", "s"):
            n = _count(child, _q("text", "c"))
            if n > MAX_SPACES:
                raise SourceError("too many repeated spaces in a cell")
            add(" " * n)
        elif child.tag == _q("text", "tab"):
            add("\t")
        elif child.tag == _q("text", "line-break"):
            add("\n")
        elif child.tag != _q("office", "annotation"):
            out.append(_para_text(child, budget))  # already counted against the budget
        add(child.tail or "")
    return "".join(out)


def _rows(el):
    for child in el:
        if child.tag == _ROW:
            yield child
        elif child.tag in _ROW_GROUPS:
            yield from _rows(child)


def read_ods(path):
    """Return {sheet name: grid} for every sheet in the workbook."""
    try:
        with zipfile.ZipFile(path) as z:
            if _encrypted_ods(z):
                raise SourceError("%s is password-protected. Save a copy without the password "
                                  "(File > Save As, untick \"Save with password\") and import that" % path)
            if z.getinfo("content.xml").file_size > MAX_CONTENT_BYTES:
                raise SourceError("%s: content.xml is unexpectedly large" % path)
            root = _xml(z.read("content.xml"), path)
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
        raise SourceError("%s is not a readable .ods workbook (%s)" % (path, e)) from None
    body = root.find(".//" + _q("office", "spreadsheet"))
    if body is None:
        raise SourceError("%s is not a spreadsheet (no office:spreadsheet element)" % path)
    try:
        return _read_tables(body)
    except RecursionError:
        raise SourceError("%s: content.xml is nested too deeply" % path) from None


def _read_tables(body):
    sheets, total, text_left = {}, 0, MAX_WORKBOOK_TEXT
    for table in body.findall(_q("table", "table")):
        grid, r = Grid(), 0
        for row in _rows(table):
            rrep = _count(row, _q("table", "number-rows-repeated"))
            cells, c = [], 0
            for cell in row:
                if cell.tag not in (_CELL, _COVERED):
                    continue
                crep = _count(cell, _q("table", "number-columns-repeated"))
                if cell.tag == _CELL:
                    paras = cell.findall(_q("text", "p"))
                    budget = [MAX_CELL_TEXT - max(len(paras) - 1, 0), text_left - max(len(paras) - 1, 0)]
                    if min(budget) < 0:  # the newlines joining the paragraphs count too
                        raise SourceError("too many paragraphs in a cell")
                    text = "\n".join(_para_text(p, budget) for p in paras)
                    text_left = budget[1]
                    kind = cell.get(_q("office", "value-type"))
                    if kind or text:
                        if crep > MAX_REPEAT:
                            raise SourceError("too many repeated cells at %s" % ref(r, c))
                        if r + min(rrep, MAX_REPEAT) > MAX_ROWS or c + crep > MAX_COLS:
                            raise SourceError("content beyond row %d or column %s (at row %d)"
                                              % (MAX_ROWS, col_letter(MAX_COLS - 1), r + 1))
                        if total + (len(cells) + crep) * min(rrep, MAX_REPEAT) > MAX_CELLS:
                            raise SourceError("workbook has more than %d non-empty cells" % MAX_CELLS)
                        text_left -= len(text) * (crep * min(rrep, MAX_REPEAT) - 1)  # once is counted
                        if text_left < 0:
                            raise SourceError("the workbook has more than %d characters of text"
                                              % MAX_WORKBOOK_TEXT)
                        value = cell.get(_q("office", "value"))
                        if kind == "boolean":
                            value = cell.get(_q("office", "boolean-value"))
                        for k in range(crep):
                            cells.append((c + k, Cell(kind or "string", value,
                                                      cell.get(_q("office", "date-value")), text,
                                                      cell.get(_q("table", "formula")))))
                c += crep
            if cells:
                if rrep > MAX_REPEAT:
                    raise SourceError("too many repeated rows at row %d" % (r + 1))
                total += rrep * len(cells)  # within MAX_CELLS: checked before each cell was added
                for k in range(rrep):
                    for cc, cell in cells:
                        grid[(r + k, cc)] = cell
            r += rrep
        sheets[table.get(_q("table", "name"))] = grid
    return sheets


def _encrypted_ods(z):
    try:
        info = z.getinfo("META-INF/manifest.xml")
        return info.file_size < MAX_CONTENT_BYTES and b"encryption-data" in z.read(info)
    except (KeyError, zipfile.BadZipFile, OSError):
        return False


def _xml(data, path):
    """Parse an XML part. Spreadsheet files never declare a DOCTYPE; refusing one rules out
    entity tricks whatever the XML library does with them."""
    if b"<!DOCTYPE" in data:  # entities need one; XML keywords are case-sensitive
        raise SourceError("%s: unexpected DOCTYPE/ENTITY declaration in the workbook" % path)
    try:
        return ET.fromstring(data)
    except ET.ParseError as e:
        raise SourceError("%s is not a readable workbook (%s)" % (path, e)) from None


# ---------------------------------------------------------------- .xlsx (also what Google Sheets downloads)

# Built-in number formats that show a date (14-17, 22, and the East Asian ones); 18-21 and 45-47
# are times of day only.
_XLSX_DATE_FORMATS = set(range(14, 18)) | {22} | set(range(27, 37)) | set(range(50, 59))
_XLSX_REF = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,6})")
# A cell reference in a formula, optionally with a sheet: H2, $H$2, 'My sheet'!H2, Sheet1!H2
_FORMULA_REF = re.compile(r"(?<![A-Za-z0-9_.$'!\"])((?:'(?:[^']|'')+'|[A-Za-z_][A-Za-z0-9_.]*)!)?"
                          r"(\$?)([A-Z]{1,3})(\$?)([0-9]{1,7})(?![0-9A-Za-z_(!])")
_ESCAPE = re.compile(r"_x([0-9A-Fa-f]{4})_")


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _child(el, name):
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _rel_id(el):
    for k, v in el.attrib.items():
        if k.endswith("}id"):
            return v
    return None


def _unescape(text):
    """Excel writes characters it cannot put in XML (like a carriage return) as _x000D_."""
    return _ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)


def _string_item(el):
    """Text of a shared string or inline string: plain <t>, or rich-text runs <r><t>; the
    phonetic guides (<rPh>) are not part of the text."""
    out = []
    for c in el:
        name = _local(c.tag)
        if name == "t":
            out.append(c.text or "")
        elif name == "r":
            t = _child(c, "t")
            out.append("" if t is None else t.text or "")
    return _unescape("".join(out))


def _shift_formula(text, dr, dc):
    """A shared formula as seen from a cell dr rows and dc columns away from where it is written:
    references without $ move along."""
    def move(m):
        col, row = m.group(3), int(m.group(5))
        if not m.group(2):
            col = col_letter(col_index(col) + dc)
        if not m.group(4):
            row += dr
        return "%s%s%s%s%d" % (m.group(1) or "", m.group(2), col, m.group(4), row)
    return _FORMULA_REF.sub(move, text)


def odf_formula(text):
    """An .xlsx formula (H2-D3+F3) written the way .ods stores it (of:=[.H2]-[.D3]+[.F3]), so
    one formula reader serves both."""
    def ref(m):
        sheet = (m.group(1) or "")[:-1]
        return "[%s.%s%s%s%s]" % ("$" + sheet if sheet else "", m.group(2), m.group(3), m.group(4), m.group(5))
    return "of:=" + _FORMULA_REF.sub(ref, text)


def _is_date_format(code):
    code = re.sub(r'"[^"]*"|\\.|\[[^\]]*\]|_.|\*.', "", code)  # quoted text, escapes, [colors], padding
    return bool(re.search(r"[dDyY]", code))


def _serial_date(number, date1904):
    """An Excel date serial number -> ISO date, or None if it is not a real date."""
    days = int(number // 1)
    if date1904:
        base = datetime.date(1904, 1, 1)
    elif days < 1 or days == 60:  # day 0, and 1900-02-29, which Excel counts but never existed
        return None
    else:
        base = datetime.date(1899, 12, 31) if days < 60 else datetime.date(1899, 12, 30)
    try:
        return (base + datetime.timedelta(days=days)).isoformat()
    except OverflowError:
        return None


def _xlsx_number(text):
    """A stored number, cut to the 15 significant digits Excel works with, so binary noise like
    84.120000000000005 reads as 84.12."""
    try:
        f = float(text)
    except ValueError:
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return Decimal("%.15g" % f)


class _Parts:
    """Reads parts of one .xlsx package within the size limits."""

    def __init__(self, z, path):
        self.z, self.path, self.total = z, path, 0
        self.names = set(z.namelist())

    def read(self, name):
        try:
            info = self.z.getinfo(name)
        except KeyError:
            raise SourceError("%s is not a readable .xlsx workbook (%s is missing)" % (self.path, name)) from None
        self.total += info.file_size
        if info.file_size > MAX_CONTENT_BYTES or self.total > MAX_PARTS_BYTES:
            raise SourceError("%s: %s is unexpectedly large" % (self.path, name))
        return self.z.read(name)

    def xml(self, name):
        return _xml(self.read(name), self.path)

    def rels(self, part):
        """{id: (type, target part name)} for a part's relationships."""
        folder, _, base = part.rpartition("/")
        name = (folder + "/" if folder else "") + "_rels/" + base + ".rels"
        out = {}
        if name not in self.names:
            return out
        for rel in self.xml(name):
            target = rel.get("Target", "")
            if rel.get("TargetMode") == "External":
                continue
            if target.startswith("/"):
                full = target[1:]
            else:
                parts = (folder.split("/") if folder else []) + target.split("/")
                stack = []
                for p in parts:
                    if p == "..":
                        if stack:
                            stack.pop()
                    elif p not in ("", "."):
                        stack.append(p)
                full = "/".join(stack)
            out[rel.get("Id")] = (rel.get("Type", "").rsplit("/", 1)[-1], full)
        return out


def read_xlsx(path):
    """Return {sheet name: grid} for every worksheet of an .xlsx (or .xlsm) workbook. Formulas are
    never evaluated: cells hold the values Excel or Google Sheets last calculated and saved."""
    try:
        with zipfile.ZipFile(path) as z:
            parts = _Parts(z, path)
            book = next((part for t, part in parts.rels("").values() if t == "officeDocument"),
                        "xl/workbook.xml")
            wb = parts.xml(book)
            rels = parts.rels(book)
            date1904 = False
            sheets = []
            for el in wb.iter():
                name = _local(el.tag)
                if name == "workbookPr":
                    date1904 = el.get("date1904", "").lower() in ("1", "true")
                elif name == "sheet":
                    sheets.append((el.get("name", ""), rels.get(_rel_id(el))))
            by_type = {}
            for t, part in rels.values():
                by_type.setdefault(t, part)
            strings = []
            if "sharedStrings" in by_type:
                for si in parts.xml(by_type["sharedStrings"]):
                    if _local(si.tag) == "si":
                        text = _string_item(si)
                        if len(text) > MAX_CELL_TEXT:
                            raise SourceError("a cell has more than %d characters of text" % MAX_CELL_TEXT)
                        strings.append(text)
            dates = _date_styles(parts.xml(by_type["styles"])) if "styles" in by_type else []
            out, state = {}, {"cells": 0, "text": MAX_WORKBOOK_TEXT}
            for name, rel in sheets:
                if rel is None or rel[0] != "worksheet":
                    continue  # chart sheets and the like hold no cells
                out[name] = _read_xlsx_sheet(parts.read(rel[1]), strings, dates, date1904, state, path)
            return out
    except zipfile.BadZipFile as e:
        raise SourceError("%s is not a readable .xlsx workbook (%s)" % (path, e)) from None
    except RecursionError:
        raise SourceError("%s: the workbook XML is nested too deeply" % path) from None


def _date_styles(styles):
    """For each cell style index: does its number format show a date?"""
    custom = {}
    xfs = []
    for el in styles:
        name = _local(el.tag)
        if name == "numFmts":
            for f in el:
                if f.get("numFmtId", "").isdigit():
                    custom[int(f.get("numFmtId"))] = f.get("formatCode", "")
        elif name == "cellXfs":
            xfs = [x.get("numFmtId", "0") for x in el if _local(x.tag) == "xf"]
    out = []
    for fid in xfs:
        n = int(fid) if fid.isdigit() else 0
        out.append(_is_date_format(custom[n]) if n in custom else n in _XLSX_DATE_FORMATS)
    return out


def _read_xlsx_sheet(data, strings, dates, date1904, state, path):
    if b"<!DOCTYPE" in data:
        raise SourceError("%s: unexpected DOCTYPE/ENTITY declaration in the workbook" % path)
    grid, shared = Grid(), {}
    row = next_row = col = 0
    try:
        for event, el in ET.iterparse(io.BytesIO(data), events=("start", "end")):
            name = _local(el.tag)
            if event == "start":
                if name == "row":
                    r = el.get("r", "")
                    row = int(r) - 1 if r.isdigit() and 0 < int(r) <= MAX_ROWS else next_row
                    col = 0
                continue
            if name == "row":
                next_row = row + 1
                el.clear()
            elif name == "c":
                m = _XLSX_REF.fullmatch(el.get("r", ""))
                r, c = (int(m.group(2)) - 1, col_index(m.group(1))) if m else (row, col)
                col = c + 1
                cell = _xlsx_cell(el, r, c, strings, dates, date1904, shared)
                el.clear()
                if cell is None:
                    continue
                if r >= MAX_ROWS or c >= MAX_COLS:
                    raise SourceError("content beyond row %d or column %s (at %s)"
                                      % (MAX_ROWS, col_letter(MAX_COLS - 1), ref(r, c)))
                state["cells"] += 1
                state["text"] -= len(cell.text)
                if state["cells"] > MAX_CELLS:
                    raise SourceError("workbook has more than %d non-empty cells" % MAX_CELLS)
                if state["text"] < 0:
                    raise SourceError("the workbook has more than %d characters of text" % MAX_WORKBOOK_TEXT)
                grid[(r, c)] = cell
    except ET.ParseError as e:
        raise SourceError("%s is not a readable .xlsx workbook (%s)" % (path, e)) from None
    return grid


def _xlsx_cell(el, r, c, strings, dates, date1904, shared):
    t = el.get("t", "n")
    v = _child(el, "v")
    v = None if v is None else v.text
    f = _child(el, "f")
    formula = None
    if f is not None:
        if f.get("t") == "shared" and f.get("si") is not None:
            if f.text:
                shared[f.get("si")] = (r, c, f.text)
                formula = f.text
            elif f.get("si") in shared:
                r0, c0, text = shared[f.get("si")]
                formula = _shift_formula(text, r - r0, c - c0)
        else:
            formula = f.text
    formula = odf_formula(formula) if formula else None
    if t == "s":
        try:
            text = strings[int(v)]
        except (TypeError, ValueError, IndexError):
            raise SourceError("cell %s refers to a missing shared string" % ref(r, c)) from None
        return Cell("string", None, None, text, formula) if text else None
    if t == "inlineStr":
        item = _child(el, "is")
        text = "" if item is None else _string_item(item)
        if len(text) > MAX_CELL_TEXT:
            raise SourceError("a cell has more than %d characters of text" % MAX_CELL_TEXT)
        return Cell("string", None, None, text, formula) if text else None
    if v is None:
        return None  # empty (perhaps only formatted), or a formula never calculated
    if t in ("str", "e"):
        return Cell("string", None, None, _unescape(v), formula)
    if t == "b":
        value = "true" if v.strip() in ("1", "true") else "false"
        return Cell("boolean", value, None, value.upper(), formula)
    if t == "d":  # ISO 8601 date, rarely used
        return Cell("date", None, v.strip()[:10], v.strip(), formula)
    number = _xlsx_number(v)
    if number is None:
        return Cell("string", None, None, v, formula)
    s_idx = el.get("s", "0")
    if s_idx.isdigit() and int(s_idx) < len(dates) and dates[int(s_idx)]:
        iso = _serial_date(number, date1904)
        if iso:
            return Cell("date", None, iso, iso, formula)
    text = format(number, "f") if abs(number) < Decimal("1e15") else str(number)
    return Cell("float", str(number), None, text, formula)


# ---------------------------------------------------------------- detection

_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_RESAVE = "Open it in LibreOffice, Excel or Google Sheets and save or download it as .xlsx, .ods or CSV"


def detect(path):
    """'ods', 'xlsx' or 'csv', from the file's content. Anything else is a SourceError that says
    what the file seems to be and what to do instead."""
    try:
        with open(path, "rb") as f:
            head = f.read(8192)
    except OSError as e:
        raise SourceError("cannot read %s: %s" % (path, e)) from None
    if head.startswith(_OLE2):
        raise SourceError("%s is an old-style Excel workbook (.xls), or a password-protected Excel "
                          "file. %s (without a password), then import that." % (path, _RESAVE))
    if head.startswith(b"PK\x03\x04") or zipfile.is_zipfile(path):
        return _detect_zip(path)
    lower = head.lstrip(codecs.BOM_UTF8 + b" \t\r\n").lower()
    if lower.startswith(b"%pdf"):
        raise SourceError("%s is a PDF; it has no spreadsheet data to read. Download the "
                          "statement as CSV or a spreadsheet instead." % path)
    if lower.startswith(b"<?xml") and b"urn:schemas-microsoft-com:office:spreadsheet" in lower:
        raise SourceError("%s is an Excel 2003 XML spreadsheet. %s, then import that." % (path, _RESAVE))
    if lower.startswith((b"<html", b"<!doctype html", b"<table")) or (lower.startswith(b"<?xml") and b"<html" in lower):
        raise SourceError("%s is a web page, not a spreadsheet (some banks' \"Excel\" downloads are). "
                          "%s, or download CSV instead." % (path, _RESAVE))
    if b"\x00" in head and not head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        raise SourceError("%s is a binary file, not a spreadsheet or CSV text" % path)
    return "csv"


def _detect_zip(path):
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            mimetype = ""
            if "mimetype" in names and z.getinfo("mimetype").file_size < 200:
                mimetype = z.read("mimetype").decode("ascii", "replace").strip()
    except (zipfile.BadZipFile, OSError) as e:
        raise SourceError("%s looks like a zip file but cannot be read (%s)" % (path, e)) from None
    if mimetype == "application/vnd.oasis.opendocument.spreadsheet" or (not mimetype and "content.xml" in names):
        return "ods"
    if mimetype.startswith("application/vnd.oasis.opendocument"):
        raise SourceError("%s is an OpenDocument %s, not a spreadsheet" % (path, mimetype.rsplit(".", 1)[-1]))
    if any(n.startswith("xl/") and n.endswith(".bin") and "workbook" in n for n in names):
        raise SourceError("%s is a binary Excel workbook (.xlsb). %s, then import that." % (path, _RESAVE))
    if "xl/workbook.xml" in names or (any(n.startswith("xl/worksheets/") for n in names)):
        return "xlsx"
    if "word/document.xml" in names or "ppt/presentation.xml" in names:
        raise SourceError("%s is a Word or PowerPoint file, not a spreadsheet" % path)
    if any(n.startswith("Index/") and n.endswith(".iwa") for n in names):
        raise SourceError("%s is an Apple Numbers file. Export it as Excel or CSV (File > Export To), "
                          "then import that." % path)
    raise SourceError("%s is a zip archive, not a spreadsheet" % path)


def read_source(path, csv_options=None):
    """Read any supported file: ({sheet name: grid}, info). info["kind"] is ods, xlsx or csv; for
    CSV it also has the encoding and delimiter used. A CSV file is one sheet, named after the file."""
    kind = detect(path)
    if kind == "ods":
        return read_ods(path), {"kind": kind}
    if kind == "xlsx":
        return read_xlsx(path), {"kind": kind}
    grid, info = read_csv(path, **(csv_options or {}))
    return {Path(path).stem: grid}, dict(info, kind=kind)


# ---------------------------------------------------------------- CSV

DELIMITERS = (",", ";", "\t", "|")
SNIFF_LINES = 500


def decode(raw, encoding=None):
    """Bytes -> (text, encoding used). Without an encoding: a byte-order mark if there is one,
    else UTF-8, else Windows-1252 (what Excel on Windows writes), else Latin-1."""
    if encoding:
        try:
            return raw.decode(encoding), encoding
        except LookupError:
            raise SourceError("unknown encoding %r" % encoding) from None
        except UnicodeDecodeError as e:
            raise SourceError("the file is not valid %s (%s)" % (encoding, e.reason)) from None
    for bom, enc in ((codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF16_LE, "utf-16"),
                     (codecs.BOM_UTF16_BE, "utf-16")):
        if raw.startswith(bom):
            return decode(raw, enc)
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue


def sniff_delimiter(text):
    """The delimiter that splits the most lines into the same number (more than one) of fields;
    a comma when nothing splits."""
    lines = [line for line in text.splitlines()[:SNIFF_LINES] if line.strip()]
    best = (0, 0, ",")
    for d in DELIMITERS:
        try:
            widths = [len(r) for r in csv.reader(lines, delimiter=d)]
        except csv.Error:
            continue
        counts = {}
        for w in widths:
            if w > 1:
                counts[w] = counts.get(w, 0) + 1
        if counts:
            width, n = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
            if (n, width) > best[:2]:
                best = (n, width, d)
    return best[2]


def _rejoin_split_money(fields):
    """Rejoin unquoted amounts split at thousands separators: '$68', '344.98' -> '$68,344.98'."""
    out, i = [], 0
    while i < len(fields):
        f = fields[i]
        if re.fullmatch(r"-?\$\d{1,3}", f):
            j = i + 1
            while j < len(fields) and re.fullmatch(r"\d{3}", fields[j]):
                f += "," + fields[j]
                j += 1
            if j < len(fields) and re.fullmatch(r"\d{3}\.\d\d", fields[j]):
                f += "," + fields[j]
                j += 1
            out.append(f)
            i = j
        else:
            out.append(f)
            i += 1
    return out


def read_csv(path, repair_split_money=False, encoding=None, delimiter=None):
    """Return (grid, info) for a CSV file. info: {"encoding", "delimiter"} as used.

    The grid records each line's number of fields (grid.widths); checking that the data rows
    agree is left to the caller, which knows where the data starts (lines above it, like a
    bank's account summary, may have any shape)."""
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_CSV_BYTES + 1)
    except OSError as e:
        raise SourceError("cannot read %s: %s" % (path, e)) from None
    if len(raw) > MAX_CSV_BYTES:
        raise SourceError("%s is larger than %d MB" % (path, MAX_CSV_BYTES // (1024 * 1024)))
    text, used = decode(raw, encoding)
    first, _, rest = text.partition("\n")
    hint = re.fullmatch(r'"?sep=(.)"?\r?', first)  # Excel's "sep=;" first line
    if hint:
        text = rest
    if delimiter is None:
        delimiter = hint.group(1) if hint else sniff_delimiter(text)
    elif delimiter not in DELIMITERS:
        raise SourceError("delimiter must be one of , ; tab |")
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True))
    except csv.Error as e:
        raise SourceError("%s is not valid CSV (%s)" % (path, e)) from None
    if repair_split_money:
        rows = [_rejoin_split_money(r) for r in rows]
    if len(rows) > MAX_ROWS:
        raise SourceError("%s has more than %d lines" % (path, MAX_ROWS))
    offset = 1 if hint else 0  # grid rows match the file's line numbers
    grid, cells = Grid(), 0
    grid.widths = {}
    for r, row in enumerate(rows):
        grid.widths[r + offset] = len(row)
        for c, value in enumerate(row):
            if value != "":
                if c >= MAX_COLS:
                    raise SourceError("%s line %d has more than %d fields" % (path, r + offset + 1, MAX_COLS))
                if len(value) > MAX_CELL_TEXT:
                    raise SourceError("%s line %d: a field has more than %d characters"
                                      % (path, r + offset + 1, MAX_CELL_TEXT))
                cells += 1
                if cells > MAX_CELLS:
                    raise SourceError("%s has more than %d non-empty fields" % (path, MAX_CELLS))
                grid[(r + offset, c)] = Cell("string", None, None, value)
    return grid, {"encoding": used, "delimiter": delimiter}


def check_widths(grid, first_row, repaired=False):
    """CSV only: every non-blank line from first_row (0-based) on must have the same number of
    fields. Lines with extra empty fields at the end are fine. Returns an error message or None."""
    widths = getattr(grid, "widths", None)
    if not widths:
        return None
    filled = {}
    for (r, c) in grid:
        if r >= first_row:
            filled[r] = max(filled.get(r, 0), c + 1)
    rows = sorted(filled)
    if not rows:
        return None

    def odd_rows(w):
        return [r for r in rows if widths[r] != w and not (widths[r] > w and filled[r] <= w)]

    counts = {}
    for r in rows:
        counts[widths[r]] = counts.get(widths[r], 0) + 1
    likely = sorted(sorted(counts, key=lambda w: -counts[w])[:5])  # a hostile file may have many
    common = min(likely, key=lambda w: len(odd_rows(w)))
    odd = odd_rows(common)
    if not odd:
        return None
    hint = "" if repaired else (" If amounts like $1,234.56 are unquoted in the file, set "
                                "\"repair_split_money\": true.")
    return ("rows have different numbers of fields (most have %d; line(s) %s%s differ).%s"
            % (common, ", ".join(str(r + 1) for r in odd[:10]), " ..." if len(odd) > 10 else "", hint))
