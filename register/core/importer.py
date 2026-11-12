"""Import sheets from .ods/.xlsx workbooks or CSV files, driven by a JSON config.

The file type is detected from the content (sources.detect). A config is usually written by
`import-template`, or made on the fly by `import FILE` (template.py), and can be edited.

A config with "exact": true (written by `export`) is different: each sheet entry is just
{"source", "account", "year"}, and the CSV files are in export's exact format. Rows are taken
exactly as written, with no opening or adjustment rows, no text clean-up and no balance checks
against the source; every row is still validated as stored data.

Example config (paths are relative to the config file):

{
  "accounts": [
    {"id": "checking", "name": "Checking", "type": "checking"},
    {"id": "money-market", "name": "Money Market", "type": "savings"}
  ],
  "balance_check": "strict",
  "sheets": [
    {"source": "register.ods", "sheet": "2022", "account": "checking", "year": 2022,
     "start_row": 3, "opening": "H1",
     "columns": {"num": "A", "date": "B", "description": "C", "debit": "D", "cleared": "E",
                 "credit": "F", "balance": "H", "note": ["I", "J"]}},
    {"source": "savings.csv", "csv": {"repair_split_money": true}, "date_format": "%m/%d/%y",
     "account": "money-market", "start_row": 2, "opening": "C1",
     "columns": {"date": "A", "amount": "B", "balance": "C", "description": "D"}}
  ]
}

Per sheet, optional: "sheet" (needed for a workbook with several sheets), "date_format" (strptime,
for dates stored as text; ISO dates need none), "decimal" ("." or ","; for amounts stored as
text), and for CSV files "csv": {"encoding", "delimiter", "repair_split_money"}; the encoding and
delimiter are detected when not given.
"""
import datetime
import re
import json
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import bankfile, export, formats, money, recurring, sources
from .store import ACCOUNT_ID, ACCOUNTS, ACCOUNTS_FILE, NAME_LIMIT, RECURRING, RECURRING_FILE, Store, StoreError, \
    locked, new_account, sum_amounts, year_tab

BALANCE_CHECKS = ("strict", "warn", "off")
TEXT_FIELDS = ("num", "description", "category", "note")
NUMERIC_KINDS = ("float", "currency", "percentage")
IMPORT_ONLY = {"debit", "credit", "balance"}  # column fields accepted on import besides the format's own
MAX_ERRORS = 20
CSV_OPTIONS = {"date_format", "repair_split_money", "encoding", "delimiter"}  # date_format: older configs
SHEET_KEYS = {"source", "sheet", "csv", "account", "year", "start_row", "opening", "columns", "date_format",
              "decimal"}
_TIME = re.compile(r"[ T]\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?( ?[AaPp][Mm])?Z?$")  # "2025-01-05 00:00:00"


class ImportFailed(Exception):
    pass


class CellError(ValueError):
    """A problem with one source cell. Errors with the same problem are reported as one line."""

    def __init__(self, where, problem, detail=None):
        super().__init__("%s: %s%s" % (where, problem, " (%s)" % detail if detail else ""))
        self.where, self.problem, self.detail = where, problem, detail


class _Errors:
    def __init__(self):
        self.messages = []
        self.by_problem = {}  # problem -> [(where, detail)], in first-seen order

    def add(self, msg):
        if len(self.messages) < MAX_ERRORS:
            self.messages.append(msg)
        elif len(self.messages) == MAX_ERRORS:
            self.messages.append("... (more errors not shown)")

    def add_cell(self, err):
        self.by_problem.setdefault(err.problem, []).append((err.where, err.detail))

    def __bool__(self):
        return bool(self.messages or self.by_problem)

    def __iter__(self):
        yield from self.messages
        for problem, cells in self.by_problem.items():
            where, detail = cells[0]
            if len(cells) == 1:
                yield "%s: %s%s" % (where, problem, " (%s)" % detail if detail else "")
            else:
                yield "%s (%d cells): %s%s" % (_span([w for w, _ in cells]), len(cells), problem,
                                               " (e.g. %s at %s)" % (detail, where) if detail else "")


def _span(wheres):
    """['E3', 'E4', ..., 'E321'] -> 'E3–E321' (same column); otherwise a short list."""
    try:
        refs = [sources.parse_ref(w) for w in wheres]
    except sources.SourceError:
        refs = None
    if refs and len({c for _, c in refs}) == 1:  # one column: first–last (the count shows any gaps)
        return "%s–%s" % (wheres[0], wheres[-1])
    return ", ".join(wheres[:5]) + (", ..." if len(wheres) > 5 else "")


# ---------------------------------------------------------------- config

def is_config(path):
    """Does this file look like an import config (JSON) rather than data to import?"""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return False
    return not zipfile.is_zipfile(path) and head.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"{")


def load_config(path):
    path = Path(path)
    if not is_config(path) and path.exists():
        raise ImportFailed("%s is a workbook/data file, not an import config. Import it directly with "
                           "`import --home DIR %s`, or pass a JSON config whose \"source\" entries "
                           "point at it." % (path, path))
    try:
        cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise ImportFailed("cannot read import config %s: %s" % (path, e)) from None
    return check_config(cfg, path.parent)


def check_config(cfg, base_dir):
    """Validate a config (from a file, or built by template.py); source paths are relative to
    base_dir. Returns the config, with each sheet's resolved "source_path" added."""
    if not isinstance(cfg, dict) or set(cfg) - {"accounts", "balance_check", "sheets", "exact", "recurring"}:
        raise ImportFailed("import config must be an object with accounts, balance_check, sheets "
                           "(and exact and recurring, in configs written by export)")
    if cfg.get("exact", False) not in (True, False):
        raise ImportFailed("exact must be true or false")
    if "recurring" in cfg:
        if not cfg.get("exact") or not isinstance(cfg["recurring"], list):
            raise ImportFailed("recurring must be a list, and only in an exact config written by export")
        try:
            cfg["recurring"] = [recurring.validate(t) for t in cfg["recurring"]]
        except formats.FieldError as e:
            raise ImportFailed("invalid recurring transaction in config: %s" % e) from None
    accounts = cfg.get("accounts", [])
    for a in accounts:
        if (not isinstance(a, dict) or set(a) - {"owed_positive"} != {"id", "name", "type"}
                or a.get("owed_positive", False) not in (True, False) or not isinstance(a["id"], str)
                or not ACCOUNT_ID.match(a["id"]) or a["type"] not in formats.FORMATS
                or not isinstance(a["name"], str) or not a["name"].strip() or len(a["name"]) > NAME_LIMIT):
            raise ImportFailed("invalid account entry in config: %r" % (a,))
    if cfg.get("balance_check", "strict") not in BALANCE_CHECKS:
        raise ImportFailed("balance_check must be one of %s" % ", ".join(BALANCE_CHECKS))
    sheets = cfg.get("sheets")
    if not isinstance(sheets, list) or not sheets:
        raise ImportFailed("config needs a non-empty \"sheets\" list")
    if cfg.get("exact"):
        allowed, required = {"source", "account", "year"}, ("source", "account")
    else:
        allowed, required = SHEET_KEYS, ("source", "account", "columns")
    for s in sheets:
        if not isinstance(s, dict) or set(s) - allowed:
            raise ImportFailed("unknown keys in sheet entry: %s"
                               % (sorted(set(s) - allowed) if isinstance(s, dict) else s))
        for key in required:
            if key not in s:
                raise ImportFailed("sheet entry is missing %r" % key)
        if not isinstance(s["source"], str):
            raise ImportFailed("a sheet's source must be a file name")
        if s.get("decimal", ".") not in (".", ","):
            raise ImportFailed("decimal must be \".\" or \",\"")
        opts = s.get("csv", {})
        if not isinstance(opts, dict) or set(opts) - CSV_OPTIONS:
            raise ImportFailed("csv options may only be %s" % ", ".join(sorted(CSV_OPTIONS)))
        for key in ("date_format", "encoding", "delimiter"):
            for where in (s, opts):
                if key in where and not isinstance(where[key], str):
                    raise ImportFailed("%s must be text" % key)
        s["source_path"] = (Path(base_dir) / s["source"]).resolve()
    return cfg


# ---------------------------------------------------------------- cell conversion

def _cells(grid, row, cols):
    return [(sources.ref(row, c), grid.get((row, c))) for c in cols]


def _text_of(cell):
    return "" if cell is None else cell.text


def _blank(cell):
    return cell is None or (cell.kind == "string" and cell.text.strip() == "")


def _amount(cell, where, decimal="."):
    """Cell -> Decimal or None (blank). decimal is the mark used in amounts stored as text."""
    if cell is None:
        return None
    if cell.kind in NUMERIC_KINDS and cell.value is not None:
        try:
            d = Decimal(cell.value)
        except InvalidOperation:
            raise CellError(where, "unreadable number") from None
    elif cell.kind == "string":
        try:
            d = money.parse_loose(cell.text, decimal)
        except ValueError:
            raise CellError(where, "not an amount", "found %r" % cell.text) from None
        if d is None:
            return None
    else:
        raise CellError(where, "expected an amount, found a %s cell" % cell.kind)
    if not d.is_finite() or abs(d) > money.MAX_ABS:
        raise CellError(where, "number is too large or not finite", "found %s" % d)
    return d


def _canonical(d, where):
    try:
        return money.fmt(d)
    except ValueError:
        raise CellError(where, "amount has more than two decimal places or is too large", "found %s" % d) from None


def _date(cell, where, date_format):
    if _blank(cell):
        return ""
    if cell.kind == "date" and cell.date:
        iso = cell.date[:10]
    elif cell.kind == "string":
        text = cell.text.strip()
        try:
            iso = parse_date(text, date_format)
        except ValueError:
            raise CellError(where, "date does not match %s" % (date_format or "YYYY-MM-DD (set date_format)"),
                            "found %r" % text) from None
    else:
        raise CellError(where, "expected a date, found a %s cell" % cell.kind)
    try:
        return formats.check_date(iso)
    except formats.FieldError as e:
        problem = "year is out of range" if "out of range" in e.message else e.message
        raise CellError(where, problem, "stored as %s" % (cell.date or cell.text)) from None


def parse_date(text, date_format=None):
    """A date written as text -> ISO. A time of day after it (00:00:00) is ignored."""
    text = _TIME.sub("", text.strip())
    if date_format:
        return datetime.datetime.strptime(text, date_format).date().isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):  # fromisoformat also takes 20250105
        raise ValueError(text)
    return datetime.date.fromisoformat(text).isoformat()


def _text(cells, field):
    parts = []
    for where, cell in cells:
        if cell is None:
            continue
        if field == "num" and cell.kind in NUMERIC_KINDS and cell.value is not None:
            try:
                d = Decimal(cell.value)
                whole = d.is_finite() and d.adjusted() < 18 and d == d.to_integral_value()
                t = str(int(d)) if whole else cell.text  # no int() of Infinity or 1e2000000
            except InvalidOperation:
                t = cell.text
        else:
            t = cell.text
        t = " ".join(t.replace("\r", " ").replace("\n", " ").replace("\t", " ").split())
        if t:
            parts.append(t)
    value = " ".join(parts)
    try:
        return formats.check_text(value, field, formats.TEXT_LIMITS[field])
    except formats.FieldError as e:
        raise CellError(cells[0][0] if len(cells) == 1 else "%s..%s" % (cells[0][0], cells[-1][0]),
                        e.message) from None


def _cleared(cell, where):
    t = _text_of(cell).strip()
    if t in ("x", "X"):
        return True
    if t == "":
        return False
    raise CellError(where, "cleared mark must be x or blank", "found %r" % t)


# ---------------------------------------------------------------- one sheet

class SheetPlan:
    """The converted content of one import sheet entry, plus its report."""

    def __init__(self, entry, account):
        self.entry, self.account = entry, account
        self.fmt = formats.FORMATS[account["type"]]
        self.year = entry.get("year")
        self.rows = []
        self.wheres = []           # source row number of each imported row
        self.balances = []         # (where, Decimal) per imported row, from the source
        self.opening_value = None  # Decimal from the "opening" setting
        self.errors = _Errors()
        self.warnings = []
        self.notes = []
        self.mismatches = []
        self.closing = None
        self.last_source_balance = None

    @property
    def label(self):
        return Store.label(self.account, self.year)


def column_fields(fmt):
    """The fields a config's "columns" may map: the visible row fields plus the import-only ones."""
    return (set(fmt.fields) - set(formats.LINK_FIELDS)) | IMPORT_ONLY


def _columns(entry, fmt, errors):
    cols = entry["columns"]
    if not isinstance(cols, dict):
        errors.add("columns must be an object")
        return {}
    allowed = column_fields(fmt)
    out = {}
    for key, spec in cols.items():
        if key not in allowed:
            errors.add("unknown column field %r for a %s sheet" % (key, fmt.name))
            continue
        letters = spec if isinstance(spec, list) else [spec]
        if len(letters) > 1 and key not in TEXT_FIELDS:
            errors.add("only text fields (%s) may join several columns" % ", ".join(TEXT_FIELDS))
            continue
        try:
            out[key] = [sources.col_index(x) for x in letters]
        except sources.SourceError as e:
            errors.add(str(e))
    if "amount" in out and ("debit" in out or "credit" in out):
        errors.add("map either amount or debit/credit, not both")
    if "amount" not in out and "debit" not in out and "credit" not in out:
        errors.add("map amount, or debit and/or credit")
    return out


def convert_sheet(plan, grid):
    entry, fmt, errors = plan.entry, plan.fmt, plan.errors
    cols = _columns(entry, fmt, errors)
    if errors:
        return
    date_format = entry.get("date_format") or (entry.get("csv") or {}).get("date_format")
    decimal = entry.get("decimal", ".")
    start = entry.get("start_row", 1)
    if not isinstance(start, int) or isinstance(start, bool) or start < 1:
        errors.add("start_row must be a positive integer")
        return
    shape = sources.check_widths(grid, start - 1, bool((entry.get("csv") or {}).get("repair_split_money")))
    if shape:
        errors.add("%s: %s" % (entry["source"], shape))
        return
    opening = entry.get("opening")
    if opening is not None:
        try:
            if isinstance(opening, str) and opening[:1].isalpha():
                r, c = sources.parse_ref(opening)
                plan.opening_value = _amount(grid.get((r, c)), opening, decimal)
                if plan.opening_value is None:
                    errors.add("opening cell %s is empty" % opening)
            else:
                plan.opening_value = money.parse_loose(str(opening), decimal)
            if plan.opening_value is not None:
                plan.opening_value = money.round_cents(plan.opening_value)
                if abs(plan.opening_value) > money.MAX_ABS:
                    raise ValueError("amount too large: %s" % plan.opening_value)
        except (ValueError, sources.SourceError) as e:
            errors.add("opening: %s" % e)

    content_cols = sorted({c for k, cs in cols.items() if k != "balance" for c in cs})
    last_row = max((r for r, _ in grid), default=-1)
    for r in range(start - 1, last_row + 1):
        if all(_blank(grid.get((r, c))) for c in content_cols):
            continue  # blank row
        row = fmt.blank_row()
        try:
            for field, cs in cols.items():
                cells = _cells(grid, r, cs)
                where, cell = cells[0]
                if field in TEXT_FIELDS:
                    row[field] = _text(cells, field)
                elif field == "date":
                    row["date"] = _date(cell, where, date_format)
                elif field == "cleared":
                    row["cleared"] = _cleared(cell, where)
                elif field == "amount":
                    d = _amount(cell, where, decimal)
                    row["amount"] = "" if d is None else _canonical(d, where)
            if "debit" in cols or "credit" in cols:
                parts = {}
                for field in ("debit", "credit"):
                    if field in cols:
                        where, cell = _cells(grid, r, cols[field])[0]
                        parts[field] = _amount(cell, where, decimal)
                if all(v is None for v in parts.values()):
                    row["amount"] = ""
                else:
                    total = (parts.get("credit") or 0) - (parts.get("debit") or 0)
                    row["amount"] = _canonical(Decimal(total), "row %d" % (r + 1))
            if "balance" in cols:
                where, cell = _cells(grid, r, cols["balance"])[0]
                b = _amount(cell, where, decimal)
                plan.balances.append((where, None if b is None else money.round_cents(b)))
            else:
                plan.balances.append((None, None))
            plan.rows.append(formats.validate_row(fmt, row))
            plan.wheres.append(r + 1)
        except CellError as e:
            errors.add_cell(e)
        except (ValueError, formats.FieldError) as e:
            errors.add(str(e))
    if plan.year is not None:
        other = [(n, row["date"]) for n, row in zip(plan.wheres, plan.rows)
                 if row["date"] and int(row["date"][:4]) != plan.year]
        if other:
            plan.warnings.append("%d row(s) are dated outside %d (first: %s on row %d); they are imported "
                                 "into the %d sheet as they are" % (len(other), plan.year, other[0][1],
                                                                   other[0][0], plan.year))


def convert_exact(plan):
    """Exact mode: the rows of a CSV file written by export, exactly as they are."""
    path = plan.entry["source_path"]
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        plan.errors.add("%s: %s" % (plan.entry["source"], e))
        return
    plan.rows, errors = export.loads(plan.fmt, text, plan.entry["source"])
    for e in errors:
        plan.errors.add(e)
    plan.balances = [(None, None)] * len(plan.rows)
    plan.notes.append("exact: rows taken as written")


def finish_sheet(plan, carried, is_first, balance_check):
    """Add the opening row (or carry-over adjustment) and verify running balances.
    A balance outside the storable range is a plan error (and leaves plan.closing None)."""
    try:
        _finish_sheet(plan, carried, is_first, balance_check)
    except ValueError as e:
        plan.closing, plan.mismatches = None, []
        plan.errors.add("amount out of range: %s" % e)


def _finish_sheet(plan, carried, is_first, balance_check):
    fmt = plan.fmt
    if is_first:
        start = plan.opening_value
        if start is None:
            where, first_bal = plan.balances[0] if plan.balances else (None, None)
            if first_bal is not None and plan.rows:
                start = first_bal - money.to_decimal(plan.rows[0]["amount"])
                plan.notes.append("opening balance %s inferred from %s" % (money.fmt(start), where))
            else:
                start = Decimal(0)
        plan.rows.insert(0, fmt.opening_row(money.fmt(start)))
        plan.balances.insert(0, (None, None))
        plan.notes.append("added an \"Opening balance\" row of %s" % money.fmt(start))
    else:
        start = carried
        plan.notes.append("carried forward %s from the previous year" % money.fmt(carried))
        if plan.opening_value is not None and plan.opening_value != carried:
            diff = plan.opening_value - carried
            msg = ("opening balance %s in the source differs from the carried-forward %s by %s"
                   % (money.fmt(plan.opening_value), money.fmt(carried), money.fmt(diff)))
            if balance_check == "strict":
                # still an error, but continue from the source's opening balance so this and the
                # later years are checked in the same run
                plan.errors.add(msg + " (use balance_check \"warn\" to import with an adjustment row)")
            else:
                plan.warnings.append(msg + "; added an adjustment row")
            row = fmt.blank_row()
            row.update(description="Opening balance adjustment (import)", amount=money.fmt(diff))
            plan.rows.insert(0, row)
            plan.balances.insert(0, (None, None))
    running = Decimal(0) if is_first else start
    for (where, expected), row in zip(plan.balances, plan.rows):
        running += money.to_decimal(row["amount"])
        if abs(running) > money.MAX_ABS:
            raise ValueError("running balance at %s is larger than %s" % (where or "the opening row",
                                                                         money.fmt(money.MAX_ABS)))
        if expected is not None:
            plan.last_source_balance = expected
            if expected != running and balance_check != "off":
                plan.mismatches.append((where, running, expected))
    plan.closing = running
    if plan.mismatches:
        where, got, exp = plan.mismatches[0]
        msg = ("%d running balance(s) differ from the source; first at %s: computed %s, source %s"
               % (len(plan.mismatches), where, money.fmt(got), money.fmt(exp)))
        if balance_check == "strict":
            plan.errors.add(msg + " (use balance_check \"warn\" to import anyway)")
        else:
            plan.warnings.append(msg)


# ---------------------------------------------------------------- whole import

def run_import(home, config, key, dry_run=False, replace=False, balance_check=None, out=print, expect=None):
    """Import what a config describes (a path, or a dict already passed through check_config),
    printing a report through out. Returns the report lines.

    expect: the report lines of an earlier dry run that the user confirmed. The import then
    goes ahead only if it would do exactly that (nothing changed in between), and the report is
    not printed again."""
    cfg = config if isinstance(config, dict) else load_config(config)
    exact = cfg.get("exact", False)
    balance_check = balance_check or cfg.get("balance_check", "strict")
    store = Store(home, key)
    home.check()
    with locked(home), store.cached():  # each existing sheet is decrypted once
        if not dry_run:
            store.require_clean()
        existing = {a["id"]: a for a in store.accounts()}
        new_accounts = []
        for a in cfg.get("accounts", []):
            if a["id"] in existing:
                if existing[a["id"]]["type"] != a["type"]:
                    raise ImportFailed("account %s already exists with type %s" % (a["id"], existing[a["id"]]["type"]))
            else:
                if (home.data / a["id"]).exists():  # as in Ledger.add_account: never adopt leftovers
                    raise ImportFailed("a directory named %s already exists in the data repo" % a["id"])
                acct = new_account(a["id"], formats.check_text(a["name"].strip(), "name", NAME_LIMIT), a["type"],
                                   a.get("owed_positive"))
                existing[a["id"]] = acct
                new_accounts.append(acct)

        grids, plans = {}, []
        for entry in cfg["sheets"]:
            account = existing.get(entry["account"])
            if account is None:
                raise ImportFailed("account %r is neither in the data repo nor in the config's "
                                   "accounts list" % entry["account"])
            plan = SheetPlan(entry, account)
            if plan.fmt.yearly:
                if not isinstance(plan.year, int) or not formats.MIN_YEAR <= plan.year <= formats.MAX_YEAR:
                    plan.errors.add("checking sheets need a \"year\"")
            elif plan.year is not None:
                plan.errors.add("savings sheets have no \"year\"")
            if not plan.errors:
                if exact:
                    convert_exact(plan)
                else:
                    grid = _load_grid(grids, entry, plan.errors)
                    if grid is not None:
                        convert_sheet(plan, grid)
            plans.append(plan)

        keys = [(p.account["id"], p.year) for p in plans]
        dupes = {k for k in keys if keys.count(k) > 1}
        for p in plans:
            if (p.account["id"], p.year) in dupes:
                p.errors.add("this sheet is imported more than once in the config")

        # Carry balances forward in year order, overlaying imported sheets on existing data.
        order = sorted(plans, key=lambda p: (p.account["id"], p.year or 0))
        for p in order:
            if p.errors:
                continue
            is_new_account = p.account in new_accounts
            existing_years = [] if is_new_account else store.years(p.account)
            rel = Store.relpath(p.account, p.year)
            if not is_new_account and (home.data / rel).exists() and not replace:
                p.errors.add("%s already exists (use --replace to overwrite it)" % rel)
                continue
            if p.fmt.yearly:
                imported = {q.year: q for q in plans if q.account["id"] == p.account["id"]}
                earlier = sorted({y for y in existing_years if y < p.year} | {y for y in imported if y < p.year})
                carried = Decimal(0)
                for y in earlier:
                    if y in imported:
                        if imported[y].closing is None:
                            p.errors.add("cannot carry the balance forward: %d has errors" % y)
                            break
                        carried += sum_amounts(imported[y].rows)
                    else:
                        rows, _ = store.read_sheet(p.account, y)
                        carried += sum_amounts(rows)
                if p.errors:
                    continue
                if exact:  # the rows are final as written; the closing balance is for the report
                    p.closing = carried + sum_amounts(p.rows)
                else:
                    finish_sheet(p, carried, not earlier, balance_check)
            elif exact:
                p.closing = sum_amounts(p.rows)
            else:
                finish_sheet(p, Decimal(0), True, balance_check)

        # The imported sheets must not push a later, existing year out of the storable range.
        by_account = {}
        for p in plans:
            by_account.setdefault(p.account["id"], []).append(p)
        for ps in by_account.values():
            if not any(p.errors for p in ps):
                try:
                    store.check_balances(ps[0].account, {p.year: p.rows for p in ps},
                                         new_account=ps[0].account in new_accounts)
                except StoreError as e:
                    ps[-1].errors.add(e.message)

        if exact and not any(p.errors for p in plans):
            _check_transfer_links(store, plans)
        lines = []
        for p in plans:
            _report(p, lines.append, new=p.account in new_accounts,
                    replaces=(home.data / Store.relpath(p.account, p.year)).exists())
        templates = store.recurring()
        have = {t["id"] for t in templates}
        added = [t for t in cfg.get("recurring", []) if t["id"] not in have]
        for t in added:
            if t["account"] not in existing:
                raise ImportFailed("a recurring transaction names the unknown account %r" % t["account"])
        if added:
            lines.append("%d recurring transaction(s)" % len(added))
            lines.append("")
        _show_or_compare(lines, expect, out)
        if any(p.errors for p in plans):
            raise ImportFailed("import failed; nothing was written")
        if dry_run:
            out("dry run: nothing was written")
            return lines
        changes = {}
        if new_accounts:
            changes[ACCOUNTS_FILE] = (ACCOUNTS, store.accounts() + new_accounts)
        for p in plans:
            changes[Store.relpath(p.account, p.year)] = (p.fmt, p.rows)
        if added:
            changes[RECURRING_FILE] = (RECURRING, templates + added)
        kinds = ["an export"] if exact else sorted({source_kind(path) for path in {p.entry["source_path"] for p in plans}})
        message = "import %s from %s" % (", ".join("%s (%d rows)" % (p.label, len(p.rows)) for p in plans),
                                         " and ".join(kinds))
        if store.write_files(changes, message):
            out("imported and committed: %s" % message)
        else:
            out("nothing changed: the data repo already has exactly this content")
        return lines


SOURCE_KINDS = {"csv": "a CSV file", "ods": "an .ods workbook", "xlsx": "an .xlsx workbook",
                "ofx": "an OFX file", "qif": "a QIF file"}


def source_kind(path):
    """What kind of file was imported, for the commit message. Never its name: commit messages
    are not encrypted, and a download is often named after a bank, a person or an account
    number."""
    try:
        return SOURCE_KINDS[bankfile.sniff(path) or sources.detect(path)]
    except (sources.SourceError, KeyError):
        return "a file"


def _check_transfer_links(store, plans):
    """An exact import brings transfer ids with it. Each id must end up on at most two rows, in
    two different accounts: editing one side of a transfer changes "the" other side."""
    where = {}  # id -> [account id]
    for p in plans:
        for r in p.rows:
            if r["transfer"]:
                where.setdefault(r["transfer"], []).append(p.account["id"])
    mine = set(where)
    if not mine:
        return  # nothing to check: the sheets already stored need not be read
    replaced = {(p.account["id"], p.year) for p in plans}
    for a, y, _, r in store.rows(store.accounts()):
        if r["transfer"] and (a["id"], y) not in replaced:
            where.setdefault(r["transfer"], []).append(a["id"])
    bad = sorted(i for i in mine if len(where[i]) > 2 or len(set(where[i])) < len(where[i]))
    if bad:
        raise ImportFailed("%d transfer link(s) would not join exactly two rows in two accounts (the first: "
                           "%s, in %s); nothing was written. Was this export already imported into another "
                           "account here?" % (len(bad), bad[0], ", ".join(sorted(set(where[bad[0]])))))


def _show_or_compare(lines, expect, out):
    """A first run prints its report. The run after the user's yes (expect: the previewed lines)
    prints nothing and goes ahead only if it would do exactly what was previewed."""
    if expect is None:
        for line in lines:
            out(line)
    elif lines != expect:
        raise ImportFailed("the data or the file changed since the preview; nothing was written. "
                           "Run the import again to see the new preview.")


# ---------------------------------------------------------------- appending

NOTHING_NEW = "nothing to import: every row is already there"
NO_ROWS = "nothing to import: the file has no transactions"


class Report(list):
    """run_append's report lines, plus whether there is anything to write."""
    changed = False


def _same_text(text):
    return " ".join(text.split()).casefold()


def rows_of_config(home, cfg, key):
    """For appending a spreadsheet or CSV: the rows of a (non-exact) config's single sheet as
    they would be stored, without an opening row. Returns (the account as run_append takes it:
    the id of an existing one, or the entry of one to create; rows; notes)."""
    if cfg.get("exact") or len(cfg["sheets"]) != 1:
        raise ImportFailed("appending reads one sheet at a time; pick it with --sheet")
    entry = cfg["sheets"][0]
    listed = {a["id"]: a for a in cfg.get("accounts", [])}
    with locked(home, exclusive=False):
        existing = {a["id"]: a for a in Store(home, key).accounts()}
    account = existing.get(entry["account"]) or listed.get(entry["account"])
    if account is None:
        raise ImportFailed("account %r is neither in the data repo nor in the config" % entry["account"])
    plan = SheetPlan(entry, account)
    grid = _load_grid({}, entry, plan.errors)
    if grid is not None:
        convert_sheet(plan, grid)
    if plan.errors:
        raise ImportFailed("cannot read %s:\n  %s" % (entry["source"], "\n  ".join(plan.errors)))
    target = account["id"] if account["id"] in existing else \
        new_account(account["id"], account["name"], account["type"], account.get("owed_positive"))
    return target, plan.rows, list(plan.notes)


def run_append(home, key, account, rows, source, dry_run=False, out=print, expect=None, notes=()):
    """Add rows to the end of an account's sheets, leaving out the ones already there.

    source: the file the rows came from. Its name is shown in the report; the commit message says
    only what kind of file it was (source_kind). account: an existing account's id, or a full
    entry (store.new_account) to create it. rows:
    complete row dicts, each with a date; they go to the sheet of their date's year (created if
    missing), oldest first. One commit, or nothing.

    Already there means:
    - a row with a bank id: an existing row has that id and the same date and amount. (Some
      banks use an id again for another transaction, or twice in one file; those rows are
      added, with a warning.)
      Failing that, an existing row without a bank id has the same date and amount (it was
      entered by hand): that row is kept and the bank id recorded on it, so the next import
      knows it.
    - a row without a bank id: an existing row has the same date, amount and description.
    Each existing row accounts for one imported row at most.

    Returns the report lines (a Report: .changed says whether there is anything to write);
    dry_run and expect work as in run_import."""
    store = Store(home, key)
    home.check()
    with locked(home), store.cached():
        if not dry_run:
            store.require_clean()
        accounts = store.accounts()
        is_new = isinstance(account, dict)
        if is_new:
            try:  # checked here, so every caller is covered and it fails before the preview
                account = new_account(account["id"], formats.check_text(account["name"].strip(), "name", NAME_LIMIT),
                                      account["type"], account.get("owed_positive"))
            except formats.FieldError as e:
                raise ImportFailed("the new account's %s" % e) from None
            if not ACCOUNT_ID.match(account["id"]) or not account["name"]:
                raise ImportFailed("the new account needs a name and an id of lower-case letters, digits and hyphens")
            if any(a["id"] == account["id"] for a in accounts) or (home.data / account["id"]).exists():
                raise ImportFailed("account %s already exists" % account["id"])
        else:
            found = [a for a in accounts if a["id"] == account]
            if not found:
                raise ImportFailed("there is no account %r (existing: %s); to create it, add --type"
                                   % (account, ", ".join(a["id"] for a in accounts) or "none"))
            account = found[0]
        fmt = formats.FORMATS[account["type"]]
        problems = []
        for n, row in enumerate(rows, 1):
            try:
                formats.validate_row(fmt, row)
                if not row["date"]:
                    raise formats.FieldError("date", "is missing; a row can only be appended with a date")
            except formats.FieldError as e:
                problems.append("row %d: %s" % (n, e))
        if problems:
            raise ImportFailed("cannot append:\n  " + "\n  ".join(problems[:MAX_ERRORS]))

        years = [] if is_new else store.sheets(account)
        sheets = {y: store.read_sheet(account, y)[0] for y in years}
        if is_new and not rows:
            raise ImportFailed("the file has no transactions; the account %s was not created" % account["id"])
        # existing rows, as [(year, index)], by what an imported row is compared with
        by_id, by_hand, by_text, known_ids = {}, {}, {}, set()
        for y in years:
            for i, r in enumerate(sheets[y]):
                by_text.setdefault((r["date"], r["amount"], _same_text(r["description"])), []).append((y, i))
                if r["bank_id"]:
                    by_id.setdefault((r["bank_id"], r["date"], r["amount"]), []).append((y, i))
                    known_ids.add(r["bank_id"])
                else:
                    by_hand.setdefault((r["date"], r["amount"]), []).append((y, i))
        used = set()  # existing rows that already account for an imported row

        def take(candidates):
            while candidates:
                where = candidates.pop(0)
                if where not in used:
                    used.add(where)
                    return where
            return None

        added, skipped, linked, reused, changed = {}, 0, 0, 0, set()
        rest, misplaced = [], []
        for row in sorted(rows, key=lambda r: r["date"]):  # stable: the file's order within a day
            # exact matches first, whatever the order in the file, so no looser match takes their row
            if row["bank_id"] and take(by_id.get((row["bank_id"], row["date"], row["amount"]), [])):
                skipped += 1
            else:
                rest.append(row)
        for row in rest:
            what = (row["date"], row["amount"])
            if row["bank_id"]:
                again = row["bank_id"] in known_ids  # the id again, on another transaction
                known_ids.add(row["bank_id"])
                where = take(by_hand.get(what, []))
                if where:
                    y, i = where
                    sheets[y][i] = dict(sheets[y][i], bank_id=row["bank_id"])
                    changed.add(y)
                    linked += 1
                    continue
                reused += again  # counted only when the row is added
            elif take(by_text.get(what + (_same_text(row["description"]),), [])):
                skipped += 1
                continue
            try:
                y = year_tab(account, row["date"], sheets.keys() | added.keys())
            except StoreError as e:
                misplaced.append(e.message)
                continue
            added.setdefault(y, []).append(row)
        if misplaced:
            raise ImportFailed("cannot append: %d row(s) have no year tab to go in. The first: %s%s"
                               % (len(misplaced), misplaced[0],
                                  " (a new account's years must follow one another without a gap)" if is_new else ""))
        new_tabs = set() if is_new else {y for y in added if y not in sheets}
        if is_new:
            first = min(added, default=datetime.date.today().year) if fmt.yearly else None
            sheets[first] = [fmt.opening_row("0.00")]
            changed.add(first)
        for y, new_rows in added.items():
            sheets.setdefault(y, []).extend(new_rows)
            changed.add(y)

        total = sum(len(v) for v in added.values())
        lines = Report(["%s (%s %s account)  <-  %s" % (account["id"], "new" if is_new else "existing",
                                                        account["type"], Path(source).name)])
        lines.changed = bool(changed)
        lines += ["  " + n for n in notes]
        lines.append("  %d new row(s) to add; %d already there (skipped)" % (total, skipped))
        if reused:
            lines.append("  WARNING: %d row(s) carry a bank id that another transaction already has; they "
                         "are added as new rows. CHECK them" % reused)
        if linked:
            lines.append("  %d row(s) entered by hand match by date and amount: kept, and the bank's id "
                         "is recorded on them" % linked)
        if is_new:
            lines.append("  the new account starts with an \"Opening balance\" row of 0.00; correct it in the grid")
        for y in sorted(added, key=lambda v: v or 0):
            lines.append("  %s: %d row(s)%s" % (Store.label(account, y), len(added[y]),
                                                " (new year tab)" if y in new_tabs else ""))
            lines += ["    " + t for t in _table(fmt, added[y])]
        lines.append("")
        try:
            if changed:
                store.check_balances(account, {y: sheets[y] for y in changed}, new_account=is_new)
        except StoreError as e:
            raise ImportFailed(e.message) from None
        _show_or_compare(lines, expect, out)
        if dry_run:
            out("dry run: nothing was written")
            return lines
        if not changed:
            out(NOTHING_NEW if rows else NO_ROWS)
            return lines
        changes = {Store.relpath(account, y): (fmt, sheets[y]) for y in changed}
        if is_new:
            changes[ACCOUNTS_FILE] = (ACCOUNTS, accounts + [account])
        kind = source_kind(source)
        message = "import %d row(s) into %s from %s" % (total, account["id"], kind) if total or is_new \
            else "record bank ids on %d row(s) of %s, from %s" % (linked, account["id"], kind)
        store.write_files(changes, message)
        out("imported and committed: %s" % message)
        return lines


def _load_grid(grids, entry, errors):
    path = entry["source_path"]
    opts = {k: v for k, v in (entry.get("csv") or {}).items() if k != "date_format"}
    key = (path, json.dumps(opts, sort_keys=True))
    try:
        if key not in grids:
            grids[key] = sources.read_source(path, opts)
        sheets, info = grids[key]
    except (OSError, sources.SourceError) as e:
        errors.add(str(e))
        return None
    if info["kind"] == "csv":
        if "sheet" in entry:
            errors.add("%s is a CSV file, which has no sheets; remove \"sheet\"" % path.name)
            return None
        return next(iter(sheets.values()))
    if "csv" in entry:
        errors.add("%s is a workbook, not a CSV file; remove \"csv\"" % path.name)
        return None
    if "sheet" not in entry:
        if len(sheets) == 1:
            return next(iter(sheets.values()))
        errors.add("%s has several sheets; say which with \"sheet\" (sheets: %s)" % (path.name, ", ".join(sheets)))
        return None
    if entry["sheet"] not in sheets:
        errors.add("no sheet %r in %s (sheets: %s)" % (entry["sheet"], path.name, ", ".join(sheets)))
        return None
    return sheets[entry["sheet"]]


def _report(p, out, new=False, replaces=False):
    src = Path(p.entry["source"]).name + (" [%s]" % p.entry["sheet"] if "sheet" in p.entry else "")
    target = "new %s account" % p.account["type"] if new else "replaces the existing sheet" if replaces \
        else "existing %s account" % p.account["type"]
    out("%s (%s)  <-  %s" % (p.label, target, src))
    for n in p.notes:
        out("  " + n)
    if p.closing is not None:
        out("  %d rows; closing balance %s%s" % (
            len(p.rows), money.fmt(p.closing),
            "" if p.last_source_balance is None else " (source's last balance %s)" % money.fmt(p.last_source_balance)))
    for w in p.warnings:
        out("  WARNING: " + w)
    for where, got, exp in p.mismatches[:5]:
        out("    %s: computed %s, source %s" % (where, money.fmt(got), money.fmt(exp)))
    if len(p.mismatches) > 5:
        out("    ... %d more" % (len(p.mismatches) - 5))
    for e in p.errors:
        out("  ERROR: " + e)
    if not p.errors and p.rows:
        for line in _table(p.fmt, p.rows):
            out("    " + line)
    out("")


def _table(fmt, rows, head=4, tail=2):
    """The first and last rows as a small text table, for checking the mapping by eye. Columns
    that are blank in every row (often category, and the link fields) are left out."""
    fields = [f for f in fmt.fields if f in ("date", "description", "amount") or any(r[f] for r in rows)]
    shown = rows if len(rows) <= head + tail + 1 else rows[:head] + [None] + rows[-tail:]
    cells = [fields]
    for row in shown:
        if row is None:
            cells.append(["..."] + [""] * (len(fields) - 1))
            continue
        line = []
        for f in fields:
            v = ("x" if row[f] else "") if f == "cleared" else row[f]
            line.append(v if len(v) <= 30 else v[:29] + "…")
        cells.append(line)
    widths = [max(len(c[i]) for c in cells) for i in range(len(fields))]
    right = {i for i, f in enumerate(fields) if f == "amount"}
    return ["  ".join(c.rjust(w) if i in right else c.ljust(w) for i, (c, w) in enumerate(zip(line, widths))).rstrip()
            for line in cells]
