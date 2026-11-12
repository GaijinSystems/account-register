"""Read bank downloads: OFX / QFX (both the old SGML form and XML) and QIF.

read(path, ...) gives a Statement: rows ready to append to one account (importer.run_append),
with the stored sign the register uses everywhere (money out is negative, also on a credit
card), which is how both formats write amounts. Standard library only; nothing is executed or
fetched, and XML is never handed to a parser (the tags are picked out with regular expressions,
which also copes with SGML's unclosed tags).
"""
import codecs
import datetime
import hashlib
import html
import io
import itertools
import re
import unicodedata
from decimal import Decimal

from . import formats, money, sources

MAX_BYTES = 50 * 1024 * 1024
MAX_ROWS = 100000


class BankFileError(ValueError):
    pass


class Statement:
    def __init__(self, kind, account_type, rows, notes):
        self.kind = kind                  # "ofx" or "qif", as sniff() says
        self.account_type = account_type  # the register type the file suggests, for a new account
        self.rows = rows                  # complete row dicts, in file order
        self.notes = notes                # report lines


def sniff(path):
    """'ofx', 'qif' or None, from the start of the file."""
    try:
        with open(path, "rb") as f:
            return _kind(f.read(4096))
    except OSError:
        return None


def _kind(head):
    if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        text = head[:len(head) // 2 * 2].decode("utf-16", "ignore").lstrip("\ufeff \t\r\n")
    else:
        text = head.lstrip(codecs.BOM_UTF8 + b" \t\r\n").decode("latin-1")
    if text.upper().startswith("OFXHEADER") or re.search(r"<OFX>", text, re.I) or "<?OFX" in text.upper():
        return "ofx"
    if re.match(r"!(Type|Account|Option|Clear):?", text, re.I):
        return "qif"
    return None


def read(path, statement=None, date_format=None):
    """statement: which account of a file holding several (the end of an OFX account number, or
    a QIF account name). date_format: strptime format for QIF dates, when the guess is wrong."""
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_BYTES + 1)
    except OSError as e:
        raise BankFileError("cannot read %s: %s" % (path, e)) from None
    if len(raw) > MAX_BYTES:
        raise BankFileError("%s is larger than %d MB" % (path, MAX_BYTES // (1024 * 1024)))
    try:
        text, _ = sources.decode(raw)
    except sources.SourceError as e:
        raise BankFileError("%s: %s" % (path, e)) from None
    kind = _kind(raw[:4096])
    if kind == "ofx":
        return _read_ofx(text, statement)
    if kind == "qif":
        return _read_qif(text, statement, date_format)
    raise BankFileError("%s is not an OFX, QFX or QIF file" % path)


# ---------------------------------------------------------------- shared

def _clean(text, field):
    """Text as the register stores it: no control or invisible characters, single spaces, cut to
    the field's limit."""
    text = "".join(" " if unicodedata.category(ch) in ("Cc", "Zl", "Zp") else ch for ch in text)  # line breaks, tabs
    return " ".join(formats.strip_forbidden(text).split())[:formats.TEXT_LIMITS[field]].strip()


def _texts(name, memo):
    """{description, note} from a payee name and a memo: the memo stands in for a missing name,
    and is the note unless it only repeats the name."""
    return {"description": name or memo[:formats.TEXT_LIMITS["description"]].strip(),
            "note": memo if name and memo.casefold() != name.casefold() else ""}


def _amount(text, decimal, where):
    try:
        value = money.parse_loose(text, decimal)
        if value is None:
            raise ValueError("no amount")
        return money.fmt(Decimal(value))  # refuses more than two decimals: no silent rounding
    except (ValueError, ArithmeticError) as e:
        raise BankFileError("%s: cannot read the amount %r (%s)" % (where, text[:40], e)) from None


def _decimal_mark(amounts):
    """',' if the amounts are written 1.234,56, else '.'."""
    comma = sum(bool(re.search(r",\d{1,2}$", a.strip())) for a in amounts)
    point = sum(bool(re.search(r"\.\d{1,2}$", a.strip())) for a in amounts)
    return "," if comma > point else "."


def _row(**fields):
    return dict(formats.FORMATS["checking"].blank_row(), **fields)


def _pick(found, statement, hint):
    """One of several accounts in a file. found: [(label, value)]; returns the chosen pair."""
    if not found:
        raise BankFileError("the file has no transactions the register can read")
    if statement is None:
        if len(found) == 1:
            return found[0]
        raise BankFileError("the file holds %d accounts (%s); pick one with --sheet %s"
                            % (len(found), ", ".join(label for label, _ in found), hint))
    chosen = [pair for pair in found if pair[0] == statement] \
        or [pair for pair in found if pair[0].endswith(statement)]  # the whole name, else its end
    if len(chosen) != 1:
        raise BankFileError("--sheet %s matches %s of the file's accounts (%s)"
                            % (statement, "none" if not chosen else "several",
                               ", ".join(label for label, _ in found)))
    return chosen[0]


# ---------------------------------------------------------------- OFX / QFX

_OFX_TYPES = {"SAVINGS": "savings", "MONEYMRKT": "savings", "CD": "savings", "CREDITLINE": "loan"}
_ENTITY = re.compile(r"&(amp|lt|gt|quot|apos|#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6});")


def _blocks(text, names):
    """[(tag name, upper case; the text between <NAME> and </NAME>)] for each block, in order.
    One pass over the text, so a file full of unclosed tags costs no more than any other."""
    out, opened = [], None
    for m in re.finditer(r"<(/?)(%s)>" % "|".join(names), text, re.I):
        name = m.group(2).upper()
        if not m.group(1):
            if opened is not None and opened[0] == name:  # else the second one would vanish into the first
                raise BankFileError("the file is damaged: <%s> opens again before it is closed" % name)
            if opened is None:
                opened = (name, m.end())
        elif opened and opened[0] == name:
            out.append((name, text[opened[1]:m.start()]))
            opened = None
            if len(out) > MAX_ROWS:
                raise BankFileError("more than %d transactions" % MAX_ROWS)
    if opened:
        raise BankFileError("the file is cut off: <%s> is never closed" % opened[0])
    return out


def _tag(block, name):
    """The value after <NAME>: up to the next tag or line end, or the content of a CDATA section.
    Only the XML entities are decoded."""
    m = re.search(r"<%s>[ \t\r\n]*(?:<!\[CDATA\[(.*?)\]\]>|([^<\r\n]*))" % name, block, re.I | re.S)
    if not m:
        return ""
    if m.group(1) is not None:
        return m.group(1).strip()
    return _ENTITY.sub(lambda e: html.unescape(e.group(0)), m.group(2)).strip()


def _read_ofx(text, statement):
    found = []
    for kind, body in _blocks(text, ("STMTRS", "CCSTMTRS")):
        account = _clean(_tag(body, "ACCTID"), "description")  # cleaned: it is shown on the terminal
        found.append(("…" + account[-4:] if account else "account %d" % (len(found) + 1), (kind, body)))
    if not found and re.search(r"<INVSTMTRS>", text, re.I):
        raise BankFileError("this is an investment statement; only bank and credit card statements are read")
    label, (kind, body) = _pick(found, statement, "LAST4")
    blocks = [block for _, block in _blocks(body, ("STMTTRN",))]
    decimal = _decimal_mark([_tag(b, "TRNAMT") for b in blocks])
    rows = []
    for n, block in enumerate(blocks, 1):
        where = "transaction %d" % n
        posted = re.match(r"(\d{4})(\d{2})(\d{2})", _tag(block, "DTPOSTED"))
        try:
            date = datetime.date(*map(int, posted.groups())).isoformat()
        except (AttributeError, ValueError):
            raise BankFileError("%s: cannot read the date %r" % (where, _tag(block, "DTPOSTED"))) from None
        name = _clean(_tag(block, "NAME"), "description")  # also finds the NAME inside a PAYEE block
        memo = _clean(_tag(block, "MEMO"), "note")
        bank_id = formats.strip_forbidden(_tag(block, "FITID"))
        if len(bank_id) > formats.TEXT_LIMITS["bank_id"]:
            bank_id = "sha256:" + hashlib.sha256(bank_id.encode("utf-8")).hexdigest()
        rows.append(_row(date=date, amount=_amount(_tag(block, "TRNAMT"), decimal, where), **_texts(name, memo),
                         num=_clean(_tag(block, "CHECKNUM"), "num"), bank_id=bank_id))
    account_type = "card" if kind == "CCSTMTRS" else _OFX_TYPES.get(_tag(body, "ACCTTYPE").upper(), "checking")
    notes = ["OFX %s statement, account %s: %d transactions"
             % ("credit card" if kind == "CCSTMTRS" else "bank", label, len(rows))]
    missing = sum(not r["bank_id"] for r in rows)
    if missing:
        notes.append("%d transactions have no bank id; they are compared by date, amount and description" % missing)
    return Statement("ofx", account_type, rows, notes)


# ---------------------------------------------------------------- QIF

_QIF_TYPES = {"bank": "checking", "cash": "cash", "ccard": "card", "oth a": "savings", "oth l": "loan"}
_QIF_DATES = ("%m/%d/%Y", "%d/%m/%Y", "%m-%d-%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%Y/%m/%d")


def _qif_date(text, fmt):
    """Quicken writes 1/ 5'25 for 2025 and 1/5/99 for 1999, and a file can mix two- and four-digit
    years, so each date may use either year form of the format."""
    text = re.sub(r"\s*([/.-])\s*", r"\1", text.replace("'", "/")).strip()  # "1/ 5'25" -> "1/5/25"
    # (a two-digit year is only read at the end: 01-05-25 is not the year 2001)
    for f in [fmt] + ([fmt.replace("%Y", "%y")] if fmt.endswith("%Y") else []):
        try:
            d = datetime.datetime.strptime(text, f).date()
        except ValueError:
            continue
        if formats.MIN_YEAR <= d.year <= formats.MAX_YEAR:
            return d
    raise ValueError("not a date: %r" % text)


def _fits(dates, fmt):
    try:
        return all(_qif_date(d, fmt) for d in dates)
    except ValueError:
        return False


def _read_qif(text, statement, date_format):
    groups, order = {}, []          # account name -> (type, [record])
    account, kind, record, skipped = "", None, {}, set()
    in_account_header, count = False, 0
    # (newline=None: lines may end in LF, CR LF or a lone CR; read one at a time, with no second
    # copy of a large file)
    for line in itertools.chain(io.StringIO(text, newline=None), ["^"]):  # a last record without ^ still counts
        line = line.strip()
        if not line:
            continue
        if line.startswith("!"):
            head = line[1:].strip().lower()
            record, in_account_header = {}, head == "account"
            if in_account_header:
                kind = None  # an account list: its entries are not transactions of the section before
            if head.startswith("type:"):
                kind = _QIF_TYPES.get(head[5:].strip())
                if kind is None:
                    skipped.add(_clean(line[6:], "description"))
            elif not in_account_header:
                kind = None  # !Option, !Clear and the like: no transactions follow
            continue
        if line == "^":
            if in_account_header:
                account = _clean(record.get("N", ""), "description")
                in_account_header = False
            elif kind and record:
                if account not in groups:
                    groups[account] = (kind, [])
                    order.append(account)
                groups[account][1].append(record)
                count += 1
                if count > MAX_ROWS:
                    raise BankFileError("more than %d transactions" % MAX_ROWS)
            record = {}
            continue
        if in_account_header or kind:
            code, value = line[0], line[1:].strip()
            record.setdefault(code, value)  # split lines (S, E, $) repeat; the total is T
    _, (account_type, records) = _pick([(name or "(unnamed)", groups[name]) for name in order], statement, "NAME")
    amounts = [r.get("T") or r.get("U", "") for r in records]
    dates = [r.get("D", "") for r in records]
    if not all(dates):
        raise BankFileError("transaction %d has no date" % (dates.index("") + 1))
    notes = ["QIF %s register%s: %d transactions"
             % (account_type, " \"%s\"" % statement if statement else "", len(records))]
    fits = [f for f in _QIF_DATES if _fits(dates, f)]
    if date_format:
        if not _fits(dates, date_format):
            raise BankFileError("the dates do not all read as %s (the first is %r)" % (date_format, dates[0]))
        fmt = date_format
    elif not fits:
        odd = next((d for d in dates if not any(_fits([d], f) for f in _QIF_DATES)), dates[0])
        raise BankFileError("cannot read the dates (for example %r); say how they are written with "
                            "--date-format, like DD/MM/YYYY" % odd[:40])
    else:
        fmt = fits[0]
        for other in fits[1:]:  # any other reading that gives different dates
            differs = next((d for d in dates if _qif_date(d, fmt) != _qif_date(d, other)), None)
            if differs:
                notes.append("AMBIGUOUS dates: %r was read as %s, but could also be %s. CHECK the dates in the "
                             "preview; to read them another way, add --date-format, like DD/MM/YYYY"
                             % (differs, _qif_date(differs, fmt).isoformat(), _qif_date(differs, other).isoformat()))
                break
    decimal = _decimal_mark(amounts)
    rows = []
    for n, r in enumerate(records, 1):
        category = re.sub(r"/.*", "", r.get("L", "")).strip().strip("[]")
        payee, memo = _clean(r.get("P", ""), "description"), _clean(r.get("M", ""), "note")
        rows.append(_row(date=_qif_date(r["D"], fmt).isoformat(),
                         amount=_amount(amounts[n - 1], decimal, "transaction %d" % n), **_texts(payee, memo),
                         num=_clean(r.get("N", ""), "num"), category=_clean(category, "category"),
                         cleared=r.get("C", "").strip().lower() in ("x", "*", "c", "r")))
    if skipped:
        notes.append("skipped sections that are not transactions: %s" % ", ".join(sorted(skipped)))
    return Statement("qif", account_type, rows, notes)
