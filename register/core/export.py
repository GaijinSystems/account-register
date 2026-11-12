"""Export every sheet to CSV, plus an import config that restores them exactly.

The CSV files use the "exact" format, shared with import's exact mode: a header row with the
row fields in order, then one line per stored row with the stored values as they are (ISO dates,
canonical amounts like -84.12, "x" or blank for cleared, text unchanged). Reading a file back
gives exactly the rows that were written; export checks this before writing anything.

Reading accepts a header that names only some of the fields: the others come in blank. That is
how an export from an older data format (fewer fields) is imported.

The output is decrypted data. The CLI writes it into a new directory readable only by the user,
never inside the register home; the web UI gets it from the recorder and sends it as a zip,
writing nothing on the server.
"""
import csv
import datetime
import hashlib
import io
import json
import os
import re
from pathlib import Path

from . import formats, money
from .store import FORMAT_VERSION, Store, locked, sum_amounts

CONFIG_NAME = "import.json"


class ExportError(Exception):
    pass


# ---------------------------------------------------------------- the exact CSV format

def dumps(fmt, rows):
    """Stored rows -> CSV text in the exact format."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(fmt.fields)
    for row in rows:
        w.writerow([("x" if row[f] else "") if f == "cleared" else row[f] for f in fmt.fields])
    return buf.getvalue()


def loads(fmt, text, name="file"):
    """CSV text in the exact format -> (rows, errors). Every row is validated as stored data;
    nothing is trimmed, normalized or inferred."""
    rows, errors = [], []
    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        header = next(reader, None)
        if not header or len(set(header)) != len(header) or set(header) - set(fmt.fields):
            return [], ["%s: the first line must name the row fields, each at most once (%s)"
                        % (name, ",".join(fmt.fields))]
        for values in reader:
            where = "%s line %d" % (name, reader.line_num)
            if len(values) != len(header):
                errors.append("%s: expected %d values, found %d" % (where, len(header), len(values)))
                continue
            row = dict(fmt.blank_row(), **dict(zip(header, values)))
            if "cleared" in header:
                if row["cleared"] not in ("x", ""):
                    errors.append("%s: cleared must be x or blank (found %r)" % (where, row["cleared"]))
                    continue
                row["cleared"] = row["cleared"] == "x"
            try:
                rows.append(formats.validate_row(fmt, row))
            except formats.FieldError as e:
                errors.append("%s: %s" % (where, e))
    except csv.Error as e:
        errors.append("%s: not valid CSV (%s)" % (name, e))
    return rows, errors


# ---------------------------------------------------------------- export

def check_outdir(home, outdir):
    """The export directory: new or empty, and outside the register home."""
    out = Path(outdir).resolve()
    root = home.root.resolve()
    if out == root or root in out.parents:
        raise ExportError("%s is inside the register home %s; choose a directory outside it" % (out, root))
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ExportError("%s already exists and is not an empty directory" % out)
    return out


def sheet_file(account, year):
    return "%s/%d.csv" % (account["id"], year) if year is not None else "%s.csv" % account["id"]


KINDS = ("csv", "json", "qif", "ofx")


def export(home, key, outdir, kind="csv", currency="USD"):  # currency: for ofx
    """Write every sheet to outdir in one of KINDS. Returns (outdir, accounts, sheets, rows).
    Only "csv" (with its import config) restores a register exactly; the others are for other
    programs."""
    out = check_outdir(home, outdir)
    if kind == "csv":
        files, accounts, sheets, rows = collect(home, key)
    else:
        files, accounts, sheets, rows = collect_for_other_tools(home, key, kind, currency)
    _write_private(out, files)
    return out, accounts, sheets, rows


def _snapshot(home, key):
    """([(account, [(year, rows)])], recurring templates): a consistent copy of everything."""
    store = Store(home, key)
    home.check()
    with locked(home, exclusive=False):  # the recorder may keep running
        store.require_clean()
        accounts = store.accounts()
        if not accounts:
            raise ExportError("there are no accounts to export")
        data = [(a, [(y, store.read_sheet(a, y)[0]) for y in store.sheets(a)]) for a in accounts]
        return data, store.recurring()


def collect(home, key):
    """Every sheet and the import config, in memory: ({relative path: text}, accounts, sheets, rows)."""
    data, templates = _snapshot(home, key)
    files, sheets, total = {}, [], 0
    for a, years in data:
        fmt = formats.FORMATS[a["type"]]
        for year, rows in years:
            rel = sheet_file(a, year)
            files[rel] = dumps(fmt, rows)
            back, errors = loads(fmt, files[rel], rel)
            if errors or back != rows:  # never hand out a file that would not restore exactly
                raise ExportError("%s would not import back exactly; nothing was exported (%s)"
                                  % (rel, "; ".join(errors) or "rows differ"))
            entry = {"source": rel, "account": a["id"]}
            if year is not None:
                entry["year"] = year
            sheets.append(entry)
            total += len(rows)
    config = {"exact": True, "accounts": [a for a, _ in data], "sheets": sheets}
    if templates:
        config["recurring"] = templates
    files[CONFIG_NAME] = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
    return files, len(data), len(sheets), total


# ---------------------------------------------------------------- for other programs
# Amounts keep the stored sign in all of these: money spent or owed is negative, also on a
# credit card or loan, which is what QIF and OFX expect.

def collect_for_other_tools(home, key, kind, currency="USD"):
    """JSON (one file with everything), or QIF / OFX (one file per account)."""
    if kind not in KINDS[1:]:
        raise ExportError("unknown export format %r (use one of %s)" % (kind, ", ".join(KINDS)))
    if kind == "ofx" and not re.fullmatch(r"[A-Z]{3}", currency or ""):
        raise ExportError("the currency must be a three-letter code like USD or EUR")
    data, templates = _snapshot(home, key)
    sheets = sum(len(years) for _, years in data)
    total = sum(len(rows) for _, years in data for _, rows in years)
    if kind == "json":
        doc = {"format_version": FORMAT_VERSION,
               "accounts": [dict(a, sheets=[{"year": y, "rows": rows} for y, rows in years]) for a, years in data],
               "recurring": templates}
        return {"register.json": json.dumps(doc, indent=2, ensure_ascii=False) + "\n"}, len(data), sheets, total
    writer = _qif if kind == "qif" else lambda a, rows: _ofx(a, rows, currency)
    files = {"%s.%s" % (a["id"], kind): writer(a, _dated([r for _, rows in years for r in rows]))
             for a, years in data}
    return files, len(data), sheets, total


def _dated(rows):
    """[(date, row)] for every row. QIF and OFX need a date on each transaction: a row without one
    (an opening balance, usually) takes the date of the row before it, or of the first dated row."""
    first = next((r["date"] for r in rows if r["date"]), datetime.date.today().isoformat())
    out, last = [], first
    for r in rows:
        last = r["date"] or last
        out.append((datetime.date.fromisoformat(last), r))
    return out


_QIF_TYPE = {"checking": "Bank", "savings": "Bank", "card": "CCard", "cash": "Cash", "loan": "Oth L"}


def _qif(account, dated):
    lines = ["!Type:%s" % _QIF_TYPE[account["type"]]]
    for date, r in dated:
        lines += ["D%s" % date.strftime("%m/%d/%Y"), "T%s" % (r["amount"] or "0.00")]
        if r["cleared"]:
            lines.append("C*")
        lines += [code + r[field] for code, field in (("N", "num"), ("P", "description"), ("M", "note")) if r[field]]
        if r["category"]:  # in QIF a "/" starts a class, so one inside a category is written as "-"
            lines.append("L" + r["category"].replace("/", "-"))
        lines.append("^")
    return "\n".join(lines) + "\n"


def _sgml(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _ofx(account, dated, currency):
    """OFX 1.02 (the SGML form, which every reader accepts)."""
    card = account["type"] == "card"
    kind = {"savings": "SAVINGS", "loan": "CREDITLINE"}.get(account["type"], "CHECKING")
    stamp = "%Y%m%d"
    today = datetime.date.today()  # only for an account without rows
    start, end = min((d for d, _ in dated), default=today), max((d for d, _ in dated), default=today)
    balance = sum_amounts(r for _, r in dated)
    seen, used = {}, set()
    transactions = []
    for date, r in dated:
        amount = r["amount"] or "0.00"
        fitid = r["bank_id"]
        if not fitid or fitid in used:  # none, or one the bank used twice: readers drop a repeated id
            # made from the row, so it is the same every time the row is exported: rows added
            # above it, a tick in X or a new note do not change it
            plain = json.dumps([account["id"], date.isoformat(), amount, r["description"], r["num"], r["bank_id"]],
                               ensure_ascii=False)
            while not fitid or fitid in used:  # (a made id can also be one a row already carries)
                seen[plain] = seen.get(plain, 0) + 1
                fitid = "R" + hashlib.sha256(("%s#%d" % (plain, seen[plain])).encode("utf-8")).hexdigest()[:24]
        used.add(fitid)
        name = r["description"] or "(no description)"
        memo = " ".join(x for x in (name if len(name) > 32 else "", r["note"]) if x)[:255]
        parts = ["<STMTTRN>", "<TRNTYPE>%s" % ("CHECK" if r["num"] else "DEBIT" if amount.startswith("-") else "CREDIT"),
                 "<DTPOSTED>%s" % date.strftime(stamp), "<TRNAMT>%s" % amount, "<FITID>%s" % _sgml(fitid)]
        if r["num"]:
            parts.append("<CHECKNUM>%s" % _sgml(r["num"]))
        parts.append("<NAME>%s" % _sgml(name[:32]))
        if memo:
            parts.append("<MEMO>%s" % _sgml(memo))
        transactions.append("\n".join(parts + ["</STMTTRN>"]))
    source = "<CCACCTFROM>\n<ACCTID>%s\n</CCACCTFROM>" % account["id"] if card else \
        "<BANKACCTFROM>\n<BANKID>0\n<ACCTID>%s\n<ACCTTYPE>%s\n</BANKACCTFROM>" % (account["id"], kind)
    outer, inner = ("CREDITCARDMSGSRSV1", "CCSTMT") if card else ("BANKMSGSRSV1", "STMT")
    status = "<STATUS>\n<CODE>0\n<SEVERITY>INFO\n</STATUS>"
    body = [
        "OFXHEADER:100", "DATA:OFXSGML", "VERSION:102", "SECURITY:NONE", "ENCODING:UTF-8", "CHARSET:NONE",
        "COMPRESSION:NONE", "OLDFILEUID:NONE", "NEWFILEUID:NONE", "",
        "<OFX>", "<SIGNONMSGSRSV1>", "<SONRS>", status, "<DTSERVER>%s" % end.strftime(stamp), "<LANGUAGE>ENG",
        "</SONRS>", "</SIGNONMSGSRSV1>",
        "<%s>" % outer, "<%sTRNRS>" % inner, "<TRNUID>0", status, "<%sRS>" % inner, "<CURDEF>%s" % currency, source,
        "<BANKTRANLIST>", "<DTSTART>%s" % start.strftime(stamp), "<DTEND>%s" % end.strftime(stamp),
        *transactions, "</BANKTRANLIST>",
        "<LEDGERBAL>", "<BALAMT>%s" % money.fmt_balance(balance), "<DTASOF>%s" % end.strftime(stamp), "</LEDGERBAL>",
        "</%sRS>" % inner, "</%sTRNRS>" % inner, "</%s>" % outer, "</OFX>"]
    return "\n".join(body) + "\n"


def _write_private(out, files):
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    for rel, text in files.items():
        path = out / rel
        if path.parent != out:
            path.parent.mkdir(mode=0o700, exist_ok=True)
            os.chmod(path.parent, 0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
