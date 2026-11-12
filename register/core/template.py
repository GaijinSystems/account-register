"""Generate an import config by inspecting a workbook (.ods, .xlsx) or CSV file.

Guesses, most reliable first:
  1. the running-balance formula (=H2-D3+F3: H balance, D debit, F credit; =C1+B2: B amount),
     whose first occurrence also points at the opening-balance cell;
  2. header text (DATE, AMOUNT (-), (X), NOTE, ...);
  3. cell types and values (dates, x marks, money, text), including checking which columns
     actually add up as a running balance when there are no formulas (CSV).

Text cells also tell the decimal mark (1.234,56) and the date format (including whether 03/04
could be day/month as well as month/day). The result is a plain import config to review and
edit; `import FILE` uses it directly and shows a preview before anything is written.
"""
import json
import os
import re
from pathlib import Path

from . import formats, importer, money, sources
from .ledger import slugify
from .store import ACCOUNT_ID, NAME_LIMIT

MAX_HEADER_SCAN = 10
MAX_GUESS_AREA = 5_000_000  # rows x columns; larger sheets are not inspected cell by cell
MAX_VALUE_COLUMNS = 10  # beyond this many numeric columns, don't search for a running balance
MAX_VALUE_ROWS = 1000  # the search looks at this many rows at most
# Tried in this order; with numbers only, month/day comes before day/month (the warning says so).
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%d/%m/%y", "%d.%m.%Y", "%d.%m.%y",
                "%Y/%m/%d", "%Y.%m.%d", "%m-%d-%Y", "%m-%d-%y", "%d-%m-%Y", "%d-%m-%y",
                "%d %b %Y", "%d-%b-%Y", "%d-%b-%y", "%d %b %y", "%b %d, %Y", "%b %d %Y",
                "%d %B %Y", "%B %d, %Y", "%B %d %Y")
_HUMAN = (("%Y", "YYYY"), ("%y", "YY"), ("%m", "MM"), ("%d", "DD"), ("%B", "MONTH"), ("%b", "MON"))
_DATE_TEXT = re.compile(r"\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}|\d{1,2}[ -][A-Za-z]{3,9}\.?[ -]\d{2,4}"
                        r"|[A-Za-z]{3,9}\.? \d{1,2},? \d{4}")
_DECIMAL_COMMA = re.compile(r"[-+(]?\s*\D?\s*-?(\d{1,3}(\.\d{3})+|\d+),\d{1,2}\s*\D?\)?-?")
_DECIMAL_POINT = re.compile(r"[-+(]?\s*\D?\s*-?(\d{1,3}(,\d{3})+|\d+)\.\d{1,2}\s*\D?\)?-?")

# header text -> field. Matched after upper-casing and collapsing whitespace.
HEADER_FIELDS = [
    (re.compile(r"^(CHECK ?)?(NUMBER|NUM|NO\.?|#|CK ?#?|CHECK)$"), "num"),
    (re.compile(r"\bDATE\b"), "date"),
    (re.compile(r"\b(DESCRIPTION|PAYEE|TRANSACTION)\b(?!.*AMOUNT)"), "description"),
    (re.compile(r"\(-\)|\bDEBIT|\bWITHDRAW|\bPAYMENTS?\b|^-$"), "debit"),
    (re.compile(r"\(\+\)|\bCREDIT|\bDEPOSIT|^\+$"), "credit"),
    (re.compile(r"^\(?X\)?$|\bCLEARED\b|^CLR$|^R$"), "cleared"),
    (re.compile(r"\bBALANCE\b|\bRUNNING\b|^TOTAL$"), "balance"),
    (re.compile(r"\bAMOUNT\b"), "amount"),
    (re.compile(r"\b(NOTES?|COMMENTS?|MEMO|REMARKS?)\b"), "note"),
    (re.compile(r"\bCATEGOR(Y|IES)\b|^CAT\.?$"), "category"),
]
_REF = re.compile(r"([+-]?)\s*\[(\$?'?[^.\]']*'?)\.\$?([A-Z]{1,3})\$?(\d+)\]")  # sign, sheet, col, row
_SINGLE = re.compile(r"^\s*[xX✓✔*]\s*$")


class TemplateError(ValueError):
    pass


# ---------------------------------------------------------------- cell helpers

def _num(cell, decimal="."):
    """Decimal value of a numeric or money-looking cell (as the importer reads it), else None."""
    try:
        return importer._amount(cell, "", decimal)
    except importer.CellError:
        return None


def _is_text(cell, decimal="."):
    return cell is not None and cell.kind == "string" and cell.text.strip() != "" \
        and _num(cell, decimal) is None and not _SINGLE.match(cell.text)


def _date_format(values):
    """(format, alternative): the first strptime format that parses every value, and another
    that also does but reads some of them as different dates (03/04: March 4 or April 3?).
    (None, None) if no format fits."""
    found = []
    for fmt in DATE_FORMATS:
        try:
            parsed = [importer.parse_date(v, fmt) for v in values]
        except ValueError:
            continue
        if not found:
            found.append((fmt, parsed))
        elif parsed != found[0][1]:
            return found[0][0], fmt
    return (found[0][0], None) if found else (None, None)


def _looks_date(cell):
    if cell is None:
        return False
    if cell.kind == "date":
        return True
    return cell.kind == "string" and bool(_DATE_TEXT.fullmatch(importer._TIME.sub("", cell.text.strip())))


def human_date_format(fmt):
    """'%d/%m/%Y' -> 'DD/MM/YYYY'."""
    for code, word in _HUMAN:
        fmt = fmt.replace(code, word)
    return fmt


def date_format_option(text):
    """--date-format: 'DD/MM/YYYY' (or a strptime format like '%d/%m/%Y') -> strptime format."""
    if "%" in text:
        return text
    out = text.upper()
    for code, word in _HUMAN:
        out = out.replace(word, code)
    if not re.search(r"%[dbB]", out) or not re.search(r"%[mbB]", out) or not re.search(r"%[Yy]", out):
        raise TemplateError("--date-format needs day, month and year, like DD/MM/YYYY, MM/DD/YY or DD MON YYYY")
    return out


def guess_decimal(grid):
    """"," if amounts written as text mostly use a decimal comma (1.234,56 or 84,12), else "."."""
    votes = {",": 0, ".": 0}
    for cell in grid.values():
        if cell.kind == "string" and len(cell.text) < 40:
            t = cell.text.strip()
            if _DECIMAL_COMMA.fullmatch(t):
                votes[","] += 1
            elif _DECIMAL_POINT.fullmatch(t):
                votes["."] += 1
    return "," if votes[","] > votes["."] else "."


# ---------------------------------------------------------------- one sheet

class Guess:
    def __init__(self, name):
        self.name = name
        self.columns = {}       # field -> column index, or list for note
        self.header_row = None  # 0-based
        self.start_row = None   # 0-based
        self.opening = None     # "H1"
        self.date_format = None
        self.decimal = "."
        self.notes = []


def _find_header(grid, columns):
    best = (0, None, {})
    for r in range(MAX_HEADER_SCAN):
        found = {}
        for c in columns:
            cell = grid.get((r, c))
            if cell is None or cell.kind != "string":
                continue
            label = " ".join(cell.text.upper().split())
            for pattern, field in HEADER_FIELDS:
                if pattern.search(label) and field not in found.values():
                    found[c] = field
                    break
        if len(found) > best[0]:
            best = (len(found), r, found)
    return (best[1], best[2]) if best[0] >= 2 else (None, {})


def _balance_formula(grid, rows, columns):
    """Find the column whose cells add/subtract other columns to their own previous value.

    Returns (balance_col, minus_cols, plus_cols, first_row, opening_ref) or None.
    """
    votes = {}
    for r in rows:
        for c in columns:
            cell = grid.get((r, c))
            if cell is None or not cell.formula:
                continue
            found = _REF.findall(cell.formula)
            if any(sheet for _, sheet, _, _ in found):
                continue  # references another sheet (e.g. a carried-forward opening balance)
            refs = [(sign, sources.col_index(col), int(row) - 1) for sign, _, col, row in found]
            prev = [x for x in refs if x[1] == c and x[2] < r and x[0] in ("", "+")]
            if len(prev) != 1:
                continue
            others = [x for x in refs if x is not prev[0]]
            if not others or any(x[2] != r for x in others):
                continue
            minus = tuple(sorted(x[1] for x in others if x[0] == "-"))
            plus = tuple(sorted(x[1] for x in others if x[0] in ("", "+")))
            key = (c, minus, plus)
            v = votes.setdefault(key, [0, r, prev[0]])
            v[0] += 1
            if r < v[1]:
                v[1], v[2] = r, prev[0]
    if not votes:
        return None
    (c, minus, plus), (n, first, prev) = max(votes.items(), key=lambda kv: kv[1][0])
    return c, minus, plus, first, sources.ref(prev[2], prev[1]) if prev[2] < first else None


def _balance_by_values(grid, rows, money_cols, decimal="."):
    """Without formulas: find a column that equals previous + one column (or - one + another)."""
    if len(money_cols) > MAX_VALUE_COLUMNS:
        return None  # the search is cubic in the number of columns; fall back to the layout guess
    rows = rows[:MAX_VALUE_ROWS]
    nums = {c: [_num(grid.get((r, c)), decimal) for r in rows] for c in money_cols}
    best = None
    for b in money_cols:
        others = [c for c in money_cols if c != b]
        candidates = [((), (a,)) for a in others] + [((d,), (cr,)) for d in others for cr in others if d != cr]
        for minus, plus in candidates:
            hits = total = 0
            prev = None
            for k in range(len(rows)):
                bal = nums[b][k]
                if bal is None:
                    continue
                if prev is not None:
                    total += 1
                    delta = sum((nums[c][k] or 0) for c in plus) - sum((nums[c][k] or 0) for c in minus)
                    if money.round_cents(prev + delta) == money.round_cents(bal):
                        hits += 1
                prev = bal
            if total and hits / total >= 0.8 and (best is None or hits > best[0]):
                best = (hits, b, minus, plus)
    return best[1:] if best else None


def _balance_by_density(grid, rows, cols, columns, g):
    """Last resort: the rightmost money column filled on nearly every row is the balance;
    money columns to its left (not already mapped) are the amounts."""
    dated = [r for r in rows if _looks_date(grid.get((r, cols["date"])))]
    filled = {c: sum(_num(grid.get((r, c)), g.decimal) is not None for r in dated) for c in columns}
    dense = [c for c, n in filled.items() if dated and n >= 0.95 * len(dated) and c not in cols.values()]
    if not dense:
        g.notes.append("could not identify the balance column; edit the config by hand")
        return None, (), ()
    b = max(dense)
    g.notes.append("GUESSED balance column %s from layout only (the values do not add up); CHECK the mapping"
                   % sources.col_letter(b))
    if any(f in cols for f in ("debit", "credit", "amount")):
        return b, (), ()  # keep the amount columns named by the header
    mapped = set(cols.values())
    amounts = [c for c in columns if c < b and c not in mapped and filled[c] > 0]
    if len(amounts) == 1:
        return b, (), (amounts[0],)
    if len(amounts) == 2:
        g.notes.append("GUESSED %s as debit and %s as credit" % tuple(sources.col_letter(c) for c in amounts))
        return b, (amounts[0],), (amounts[1],)
    g.notes.append("could not tell which columns hold the amounts; edit the config by hand")
    return b, (), ()


def guess_sheet(name, grid, decimal="."):
    g = Guess(name)
    g.decimal = decimal
    if not grid:
        g.notes.append("empty sheet; skipped")
        return g
    # Only rows and columns with content are looked at, so a stray cell far away costs nothing.
    occupied = sorted({r for r, _ in grid})
    columns = sorted({c for _, c in grid})
    if len(occupied) * len(columns) > MAX_GUESS_AREA:
        g.notes.append("too large to inspect (%d rows x %d columns with content); skipped"
                       % (len(occupied), len(columns)))
        return g
    max_row = occupied[-1]
    g.header_row, header = _find_header(grid, columns)
    cols = {}
    for c, field in header.items():
        cols.setdefault(field, c)

    # first data row: first row after the header with a date in it
    date_col = cols.get("date")
    if date_col is None:
        counts = {c: sum(_looks_date(grid.get((r, c))) for r in occupied) for c in columns}
        date_col = max(counts, key=counts.get)
        if counts[date_col] == 0:
            g.notes.append("no date column found; skipped")
            return g
    cols["date"] = date_col
    first_data = (g.header_row + 1) if g.header_row is not None else 0
    while first_data <= max_row and not _looks_date(grid.get((first_data, date_col))):
        first_data += 1
    g.start_row = first_data
    rows = [r for r in occupied if r >= first_data]

    found = _balance_formula(grid, rows, columns)
    if found:
        b, minus, plus, first, opening = found
        g.notes.append("balance formula in column %s" % sources.col_letter(b))
        g.opening = opening
    else:
        money_cols = [c for c in columns
                      if sum(_num(grid.get((r, c)), decimal) is not None for r in rows) >= max(1, len(rows) // 4)]
        found = _balance_by_values(grid, rows, money_cols, decimal)
        if found:
            b, minus, plus = found
            g.notes.append("no balance formulas; column %s adds up as a running balance" % sources.col_letter(b))
        elif "balance" in cols:
            b, minus, plus = cols["balance"], (), ()
            g.notes.append("balance column taken from the header only (the values do not add up); "
                           "CHECK the mapping")
        else:
            b, minus, plus = _balance_by_density(grid, rows, cols, columns, g)
    if b is not None:
        if minus or plus:  # the formula/values are more reliable than header text
            for f in ("debit", "credit", "amount"):
                cols.pop(f, None)
        cols["balance"] = b
        if len(minus) == 1 and len(plus) == 1:
            cols["debit"], cols["credit"] = minus[0], plus[0]
        elif len(plus) == 1 and not minus:
            cols["amount"] = plus[0]
        elif len(minus) == 1 and not plus:
            cols["debit"] = minus[0]
        elif minus or plus:
            g.notes.append("balance formula combines several columns; check the amount mapping")
        if g.opening is None:
            for r in range(first_data - 1, -1, -1):
                if _num(grid.get((r, b)), decimal) is not None:
                    g.opening = sources.ref(r, b)
                    break

    # cleared marks: a column of mostly single "x" characters
    if "cleared" not in cols:
        for c in columns:
            vals = [grid[(r, c)].text for r in rows if (r, c) in grid and grid[(r, c)].text.strip()]
            if vals and sum(bool(_SINGLE.match(v)) for v in vals) >= 0.8 * len(vals):
                cols["cleared"] = c
                break

    # everything else that has content: first text column is the description, the rest are notes
    used = {c for f, c in cols.items() if f != "note"}
    notes = [cols["note"]] if "note" in cols else []
    for c in columns:
        if c in used or c in notes or not any((r, c) in grid and grid[(r, c)].text.strip() for r in rows):
            continue
        if "description" not in cols and sum(_is_text(grid.get((r, c)), decimal) for r in rows) >= len(rows) // 4:
            cols["description"] = c
        else:
            notes.append(c)
    if notes:
        cols["note"] = sorted(notes)

    if any(c.kind == "string" for c in (grid.get((r, date_col)) for r in rows) if c):
        values = [grid[(r, date_col)].text for r in rows if (r, date_col) in grid and grid[(r, date_col)].text.strip()]
        g.date_format, other = _date_format(values)
        if not g.date_format:
            g.notes.append("dates are text in an unrecognized format; set date_format by hand")
        elif other:
            g.notes.append("AMBIGUOUS dates: they read as %s but also as %s; assumed %s. CHECK the dates in the "
                           "preview; to read them the other way, add --date-format %s"
                           % tuple(map(human_date_format, (g.date_format, other, g.date_format, other))))
        elif g.date_format != "%Y-%m-%d":
            g.notes.append("dates read as %s" % human_date_format(g.date_format))
    if decimal == ",":
        g.notes.append("amounts use a decimal comma (1.234,56)")
    g.columns = cols
    return g


# ---------------------------------------------------------------- whole config

def _main_year(grid, g):
    """The most common year among a sheet's dates."""
    years = {}
    c = g.columns["date"]
    for (r, cc), cell in grid.items():
        if cc != c or r < g.start_row:
            continue
        y = None
        if cell.kind == "date" and cell.date:
            y = int(cell.date[:4])
        elif cell.kind == "string" and g.date_format:
            try:
                y = int(importer.parse_date(cell.text, g.date_format)[:4])
            except ValueError:
                pass
        if y:
            years[y] = years.get(y, 0) + 1
    return max(years, key=years.get) if years else None


def _letters(spec):
    return [sources.col_letter(c) for c in spec] if isinstance(spec, list) else sources.col_letter(spec)


class TemplateResult:
    """A guessed config, the report lines, and for each sheet entry the year its dates mostly
    fall in (None if unknown); `choose` uses that when a sheet becomes a checking year."""

    def __init__(self, config, report, years):
        self.config, self.report, self.years = config, report, years


def build_template(source, config_dir=None):
    """Return (config dict, list of report lines) for a .ods, .xlsx or .csv source."""
    result = guess(source, config_dir)
    return result.config, result.report


def _read(source):
    """({name: grid}, csv options to record, info); for CSV, retry with split amounts rejoined
    when the rows do not have one shape."""
    sheets, info = sources.read_source(source)
    if info["kind"] != "csv":
        return sheets, None, info
    opts = {}  # recorded only when not the usual, so the config stays short
    if info["encoding"] not in ("utf-8", "utf-8-sig"):
        opts["encoding"] = info["encoding"]
    if info["delimiter"] != ",":
        opts["delimiter"] = info["delimiter"]
    name, grid = next(iter(sheets.items()))
    g = guess_sheet(name, grid, guess_decimal(grid))
    if g.start_row is not None and sources.check_widths(grid, g.start_row):
        fixed, _ = sources.read_csv(source, True, info["encoding"], info["delimiter"])
        if not sources.check_widths(fixed, g.start_row):
            return {name: fixed}, dict(opts, repair_split_money=True), info
    return sheets, opts, info


def guess(source, config_dir=None, need_balance=True):
    source = Path(source)
    if not source.is_file():
        raise TemplateError("%s does not exist" % source)
    rel = str(source.resolve())
    if config_dir:  # relative only when the source sits under the config's folder
        relative = os.path.relpath(source.resolve(), Path(config_dir).resolve())
        if not relative.startswith(".."):
            rel = relative
    try:
        sheets, csv_opts, info = _read(source)
    except (OSError, sources.SourceError) as e:
        raise TemplateError(str(e)) from None

    accounts, entries, report, years = {}, [], [], []
    if info["kind"] == "csv":
        report.append("%s: CSV, %s, separated by %s%s" % (
            source.name, {"utf-8": "UTF-8", "utf-8-sig": "UTF-8", "utf-16": "UTF-16"}.get(info["encoding"], info["encoding"]),
            {",": "commas", ";": "semicolons", "\t": "tabs", "|": "bars"}[info["delimiter"]],
            "; amounts split at their thousands commas were rejoined" if csv_opts.get("repair_split_money") else ""))
        if info["encoding"] in ("cp1252", "latin-1"):
            report.append("%s: not UTF-8; read as %s. CHECK accented letters in the preview"
                          % (source.name, "Windows-1252" if info["encoding"] == "cp1252" else "Latin-1"))
    for name, grid in sheets.items():
        decimal = guess_decimal(grid)
        g = guess_sheet(name, grid, decimal)
        shape = sources.check_widths(grid, g.start_row) if g.start_row is not None else None
        if shape:
            g.notes.append(shape.split(".")[0])
        report.append("%s: %s" % (name, "; ".join(g.notes) or "ok"))
        if g.start_row is None or shape:
            continue
        if "balance" not in g.columns and (need_balance or not {"amount", "debit", "credit"} & set(g.columns)):
            if not need_balance:
                report.append("%s: no amount column found (name it in a header row); skipped" % name)
            continue
        main_year = _main_year(grid, g)
        year = int(name) if re.fullmatch(r"(19|20|21)\d\d", name.strip()) else None
        if year is None and ("cleared" in g.columns or "num" in g.columns):
            year = main_year  # looks like a checking register: year from its dates
            if year:
                report.append("%s: has check numbers / an X column, so imported as checking year %d"
                              % (name, year))
        if year is not None:
            account = {"id": "checking", "name": "Checking", "type": "checking"}
        else:
            account = {"id": slugify(name), "name": name.strip()[:NAME_LIMIT] or "Savings", "type": "savings"}
        accounts.setdefault(account["id"], account)
        entry = {"source": rel}
        if info["kind"] != "csv":
            entry["sheet"] = name
        elif csv_opts:
            entry["csv"] = csv_opts
        entry["account"] = account["id"]
        if year is not None:
            entry["year"] = year
        entry["start_row"] = g.start_row + 1
        if g.opening:
            entry["opening"] = g.opening
        if g.date_format and g.date_format != "%Y-%m-%d":
            entry["date_format"] = g.date_format
        if decimal != ".":
            entry["decimal"] = decimal
        entry["columns"] = {f: _letters(g.columns[f]) for f in ORDER if f in g.columns}
        report.extend(_keep_columns(entry, account["type"]))
        report.append("%s: %s" % (name, describe(entry)))
        entries.append(entry)
        years.append(main_year)
    if not entries:
        raise TemplateError("no importable sheets found:\n  " + "\n  ".join(report))
    config = {"accounts": list(accounts.values()), "balance_check": "strict", "sheets": entries}
    return TemplateResult(config, report, years)


ORDER = ["num", "date", "description", "category", "debit", "cleared", "credit", "amount", "balance", "note"]


def _keep_columns(entry, account_type):
    """Drop column fields a sheet of this type does not have; report lines for what was dropped."""
    allowed = importer.column_fields(formats.FORMATS[account_type])
    dropped = [f for f in entry["columns"] if f not in allowed]
    for f in dropped:
        del entry["columns"][f]
    name = entry.get("sheet") or Path(entry["source"]).name
    return ["%s: dropped %s (not part of a %s sheet)" % (name, ", ".join(dropped), account_type)] if dropped else []


def describe(entry):
    """One line on how a sheet is read: data start, opening balance and column mapping."""
    cols = " ".join("%s=%s" % (f, "+".join(v) if isinstance(v, list) else v) for f, v in entry["columns"].items())
    return "data from row %d; opening balance %s; columns %s" % (
        entry["start_row"], entry.get("opening", "from the first row's balance"), cols)


def choose(result, existing, sheets=None, account=None, name=None, account_type=None, year=None,
           date_format=None):
    """Apply the import command's choices to a guessed config, in place. existing: {id: account}
    already in the register home. Returns report lines; raises TemplateError for a choice that
    cannot apply."""
    config, report = result.config, []
    pairs = list(zip(config["sheets"], result.years))
    if sheets:
        if any("sheet" not in e for e, _ in pairs):
            raise TemplateError("--sheet is for workbooks; a CSV file has no sheets")
        available = [e["sheet"] for e, _ in pairs]
        missing = [s for s in sheets if s not in available]
        if missing:
            raise TemplateError("no importable sheet named %s (importable: %s)"
                                % (", ".join(map(repr, missing)), ", ".join(available)))
        pairs = [(e, y) for e, y in pairs if e["sheet"] in sheets]
    guessed = {a["id"]: a for a in config["accounts"]}
    if account or name or account_type or year:
        if len(pairs) != 1:
            raise TemplateError("--account, --name, --type and --year apply to one sheet; pick it with --sheet "
                                "(importable: %s)" % ", ".join(e["sheet"] for e, _ in pairs))
        entry, hint = pairs[0]
        old = guessed[entry["account"]]
        acc_id = account or entry["account"]
        if acc_id in existing:
            acct = existing[acc_id]
            if account_type and account_type != acct["type"]:
                raise TemplateError("account %s already exists as a %s account" % (acc_id, acct["type"]))
            if name:
                raise TemplateError("account %s already exists; --name only names a new account" % acc_id)
        else:
            if not ACCOUNT_ID.match(acc_id):
                raise TemplateError("account id %r: use lower-case letters, digits and hyphens" % acc_id)
            same = acc_id == entry["account"]
            new_name = name or (old["name"] if same else acc_id.replace("-", " ").capitalize())
            if not new_name.strip() or len(new_name) > NAME_LIMIT:
                raise TemplateError("account name must be 1 to %d characters" % NAME_LIMIT)
            acct = {"id": acc_id, "name": new_name.strip(), "type": account_type or old["type"]}
        guessed[acc_id] = acct
        entry["account"] = acc_id
        if formats.FORMATS[acct["type"]].yearly:
            entry["year"] = year or entry.get("year") or hint
            if not entry["year"]:
                raise TemplateError("%s accounts keep one sheet per year; say which with --year" % acct["type"])
        else:
            if year:
                raise TemplateError("%s is a %s account, which has no years" % (acc_id, acct["type"]))
            entry.pop("year", None)
        report.extend(_keep_columns(entry, acct["type"]))
        if "amount" not in entry["columns"] and not ({"debit", "credit"} & set(entry["columns"])):
            raise TemplateError("no amount column left for a %s sheet" % acct["type"])
    config["sheets"] = [e for e, _ in pairs]
    if date_format:
        for e in config["sheets"]:
            e["date_format"] = date_format_option(date_format)
        report.append("dates read as %s (--date-format)" % human_date_format(date_format_option(date_format)))
    used = []
    for e in config["sheets"]:
        if e["account"] not in used:
            used.append(e["account"])
    config["accounts"] = [existing.get(i) or guessed[i] for i in used]
    for e in config["sheets"]:
        a = existing.get(e["account"])
        if a and guessed[e["account"]]["type"] != a["type"] and not account:
            raise TemplateError("%s would go into the existing %s account %s; choose another with --account"
                                % (e.get("sheet") or Path(e["source"]).name, a["type"], a["id"]))
    return report


def dumps_config(config):
    """JSON with each sheet's column map on one line, for easy reading and editing."""
    text = json.dumps(config, indent=2, ensure_ascii=False)
    for entry in config["sheets"]:
        pretty = json.dumps(entry["columns"], indent=2, ensure_ascii=False).replace("\n", "\n      ")
        text = text.replace(pretty, json.dumps(entry["columns"], ensure_ascii=False), 1)
    return text + "\n"
