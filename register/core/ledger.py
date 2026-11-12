"""The fixed set of register operations, shared by the recorder and the admin CLI.

Callers must hold the write lock (store.locked) around write operations.
"""
import datetime
import difflib
import re
import secrets
import unicodedata

from .. import protocol
from . import admin, crypto, export as exporter, formats, gitrepo, money, recurring
from .store import ACCOUNT_ID, ACCOUNTS, ACCOUNTS_FILE, NAME_LIMIT, RECURRING, RECURRING_FILE, Store, StoreError, \
    decode_rows, dumps_row, new_account, year_tab


ID_LIMIT = 40  # the longest account id (store.ACCOUNT_ID allows no more)


def slugify(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:ID_LIMIT].strip("-")
    return s or "account"


MAX_SUGGESTIONS = 1000
MAX_RESULTS = 500
MAX_REPORT_ROWS = 200
HISTORY_DEFAULT, HISTORY_MAX = 30, 60  # changes listed per request (each costs a read of the sheet's history)
UNREADABLE = "its data cannot be read: it is from before the password was changed, or damaged"
HISTORY_CHANGES = 40                   # rows shown per change (an import can replace hundreds)
SEARCH_FIELDS = ("description", "category", "note", "num")
OPENING = formats.FORMATS["checking"].opening_row("0.00")["description"]


def _negated(amount):
    return "" if amount == "" else money.fmt(-money.to_decimal(amount))


class Ledger:
    """Commit messages never contain row contents, amounts or names: they are not encrypted."""

    def __init__(self, home, key=None):
        self.home = home
        self.store = Store(home, key)
        self.today = datetime.date.today  # what "due" is measured against

    # ------------------------------------------------------------ locking

    @property
    def locked(self):
        return self.store.key is None

    def unlock(self, password):
        self.store.key = self.home.unlock(password)
        return {"repo_clean": self.store.is_clean(), "locked": False}

    def lock(self):
        self.store.key = None
        return {"locked": True}

    def change_password(self, password, new_password, n=crypto.DEFAULT_N):
        """Re-encrypt everything under a new password (admin.change_password: one commit, under
        its own exclusive lock), then lock: the key in memory belongs to the old password."""
        if not new_password:
            raise StoreError("invalid", "the new password cannot be empty", "new_password")
        if len(new_password) > protocol.MAX_PASSWORD:
            raise StoreError("invalid", "the new password is too long (at most %d characters)"
                             % protocol.MAX_PASSWORD, "new_password")
        sheets = admin.change_password(self.home, password, new_password, n)
        self.lock()
        return {"sheets": sheets, "locked": True}

    # ------------------------------------------------------------ reads

    def status(self):
        if self.store.key is not None:
            try:
                self.store.require_key()  # notices a password change made elsewhere
            except StoreError:
                pass
        return {"repo_clean": self.store.is_clean(), "locked": self.store.key is None}

    def list_tabs(self):
        out = []
        for a in self.store.accounts():
            yearly = formats.FORMATS[a["type"]].yearly
            out.append({"id": a["id"], "name": a["name"], "type": a["type"], "owed_positive": a["owed_positive"],
                        "years": self.store.years(a) if yearly else None})
        types = [{"id": f.name, "label": f.label, "yearly": f.yearly, "owed_positive": f.owed_positive,
                  "debit_mode": f.debit_mode} for f in formats.FORMATS.values()]
        return {"repo_clean": self.store.is_clean(), "accounts": out, "types": types}

    def export(self, password):
        """Every sheet as CSV plus the import config (export.collect), for a download. The
        password is checked again even though the register is unlocked: a decrypted copy of
        everything is worth a second confirmation."""
        key = self.home.unlock(password)
        try:
            files, accounts, sheets, rows = exporter.collect(self.home, key)
        except exporter.ExportError as e:
            raise StoreError("invalid", str(e)) from None
        return {"files": files, "accounts": accounts, "sheets": sheets, "rows": rows}

    def suggestions(self):
        """What the grid offers while typing: the categories in use (most used first) and, for
        each description, its most recent amount and category (most recent first). Transfers
        and opening balances are left out of the descriptions."""
        counts, payees = {}, {}
        with self.store.cached():
            for _, _, _, r in self.store.rows(self.store.accounts()):
                if r["category"]:
                    counts[r["category"]] = counts.get(r["category"], 0) + 1
                text = r["description"].strip()
                if not text or r["transfer"] or text == OPENING:
                    continue
                seen = payees.get(text)
                if seen is None or r["date"] >= seen["date"]:
                    payees[text] = {"description": text, "amount": r["amount"],
                                    "category": r["category"], "date": r["date"]}
        recent = sorted(payees.values(), key=lambda p: p["date"], reverse=True)[:MAX_SUGGESTIONS]
        return {"categories": sorted(counts, key=lambda c: (-counts[c], c))[:MAX_SUGGESTIONS],
                "payees": [{k: p[k] for k in ("description", "amount", "category")} for p in recent]}

    def search(self, account=None, text="", amount_min="", amount_max="", date_from="", date_to=""):
        """Rows matching every given condition, in one account (all its years) or, without an
        account, in all of them. text is looked for in the description, category, note and
        number, ignoring case. The amount range compares sizes, so 20 to 30 finds -25.00 too."""
        accounts = self.store.accounts() if account is None else [self.store.account(account)]
        try:
            low, high = (formats.check_amount(v, f) for v, f in ((amount_min, "amount_min"), (amount_max, "amount_max")))
            start, end = formats.check_date(date_from, "date_from"), formats.check_date(date_to, "date_to")
            needle = formats.check_text(text, "text", formats.TEXT_LIMITS["description"]).strip().casefold()
        except formats.FieldError as e:
            raise StoreError("invalid", e.message, e.field) from None
        if not (needle or low or high or start or end):
            raise StoreError("invalid", "enter something to search for", "text")
        low, high = (abs(money.to_decimal(v)) if v else None for v in (low, high))
        results, more = [], False
        with self.store.cached():
            for a, y, i, r in self.store.rows(accounts):
                size = abs(money.to_decimal(r["amount"]))
                if ((low is not None or high is not None) and not r["amount"]) \
                        or (low is not None and size < low) or (high is not None and size > high) \
                        or ((start or end) and not r["date"]) \
                        or (start and r["date"] < start) or (end and r["date"] > end) \
                        or (needle and not any(needle in r[f].casefold() for f in SEARCH_FIELDS)):
                    continue
                if len(results) == MAX_RESULTS:  # enough: the sheets after this one are not read
                    more = True
                    break
                results.append(dict(r, account=a["id"], name=a["name"], year=y, index=i))
        return {"results": results, "more": more}

    def report(self, year=None, account=None):
        """Income and spending for one calendar year, in one account or all of them: by month,
        and spending by category and by description. Rows count by their date's year. Left out:
        rows without a date or an amount, the two sides of a transfer, and rows whose category
        is "Transfer" (moving money between your own accounts is neither income nor spending).

        A category's or description's spending is the net of its rows, so a refund reduces it;
        only those that come out as money spent are listed."""
        accounts = self.store.accounts() if account is None else [self.store.account(account)]
        if year is not None and (not isinstance(year, int) or isinstance(year, bool)
                                 or not formats.MIN_YEAR <= year <= formats.MAX_YEAR):
            raise StoreError("invalid", "year out of range", "year")
        with self.store.cached():
            rows = [r for _, _, _, r in self.store.rows(accounts)
                    if r["date"] and r["amount"] and not r["transfer"]
                    and r["category"].strip().casefold() != formats.TRANSFER_CATEGORY.casefold()]
        years = sorted({int(r["date"][:4]) for r in rows})
        if year is None:
            year = years[-1] if years else self.today().year
        zero = money.to_decimal("")
        months = [{"income": zero, "spending": zero} for _ in range(12)]
        categories, payees = {}, {}
        for r in rows:
            if int(r["date"][:4]) != year:
                continue
            amount = money.to_decimal(r["amount"])
            months[int(r["date"][5:7]) - 1]["income" if amount > 0 else "spending"] += abs(amount)
            for totals, name in ((categories, r["category"].strip()), (payees, r["description"].strip())):
                entry = totals.setdefault(name, [zero, 0])
                entry[0] += amount
                entry[1] += 1

        def spent(totals):
            ranked = sorted(((name, -total, count) for name, (total, count) in totals.items() if total < 0),
                            key=lambda item: (-item[1], item[0]))
            return [{"name": name, "total": money.fmt_balance(total), "count": count}
                    for name, total, count in ranked[:MAX_REPORT_ROWS]]

        income, spending = (sum((m[k] for m in months), zero) for k in ("income", "spending"))
        return {"year": year, "years": sorted(set(years) | {year}), "account": account,
                "income": money.fmt_balance(income), "spending": money.fmt_balance(spending),
                "net": money.fmt_balance(income - spending),
                "months": [{"month": "%04d-%02d" % (year, i + 1), "income": money.fmt_balance(m["income"]),
                            "spending": money.fmt_balance(m["spending"]),
                            "net": money.fmt_balance(m["income"] - m["spending"])} for i, m in enumerate(months)],
                "categories": spent(categories), "payees": spent(payees)}

    def get_sheet(self, account, year):
        view = self.store.sheet_view(account, year)
        view["repo_clean"] = self.store.is_clean()
        return view

    # ------------------------------------------------------------ row edits

    def _load_for_write(self, account_id, year, rev):
        self.store.require_clean()
        account = self.store.account(account_id)
        rows, current = self.store.read_sheet(account, year)
        if rev != current:
            raise StoreError("stale", "the sheet changed since it was loaded; reload and try again")
        return account, formats.FORMATS[account["type"]], rows

    def _save(self, account, year, fmt, rows, message, other=None, view=True):
        """Write one sheet, and with it the sheet holding the other side of a transfer
        (other: (account, year, rows)), in one commit. Returns the sheet's new view."""
        with self.store.cached():  # the balance check and the new view share the decrypted years
            changes = {}
            for a, y, rs in [(account, year, rows)] + ([other] if other else []):
                self.store.check_balances(a, {y: rs})
                changes[self.store.relpath(a, y)] = (formats.FORMATS[a["type"]], rs)
            self.store.write_files(changes, message)
            return self.get_sheet(account["id"], year) if view else None

    def _other_side(self, row, account, change):
        """For a row that is one side of a transfer, apply change(rows, index) to the sheet
        holding the other side. Returns (what _save takes as `other`, the words to add to the
        commit message), or (None, "") if the row is not part of a transfer or its other side is
        gone (deleted by hand)."""
        if not row["transfer"]:
            return None, ""
        near = int(row["date"][:4]) if row["date"] else None
        sheets = [(a, y) for a in self.store.accounts() if a["id"] != account["id"] for y in self.store.sheets(a)]
        # the other side was entered with the same date: that year of every account first, then
        # the single tabs, then the other years, latest first
        for a, y in sorted(sheets, key=lambda s: (s[1] != near, s[1] is not None, -(s[1] or 0))):
            rows, _ = self.store.read_sheet(a, y)
            for i, r in enumerate(rows):
                if r["transfer"] == row["transfer"]:
                    change(rows, i)
                    return (a, y, rows), " and the other side of the transfer in %s" % Store.label(a, y)
        return None, ""

    @staticmethod
    def _index(index, rows, allow_end=False):
        limit = len(rows) + (1 if allow_end else 0)
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < limit:
            raise StoreError("invalid", "row index out of range", "index")
        return index

    def insert_row(self, account, year, rev, index, fields=None):
        account, fmt, rows = self._load_for_write(account, year, rev)
        index = self._index(index, rows, allow_end=True)
        row = fmt.blank_row()
        row.update(self._fields(fmt, fields or {}))
        rows.insert(index, row)
        msg = "%s: insert row %d" % (Store.label(account, year), index + 1)
        return self._save(account, year, fmt, rows, msg)

    def update_row(self, account, year, rev, index, fields):
        account, fmt, rows = self._load_for_write(account, year, rev)
        index = self._index(index, rows)
        changes = self._fields(fmt, fields)
        if not changes:
            raise StoreError("invalid", "no fields to change", "fields")
        before = rows[index]
        rows[index] = dict(before, **changes)
        msg = "%s: edit row %d (%s)" % (Store.label(account, year), index + 1, ", ".join(sorted(changes)))
        with self.store.cached():
            other, also = None, ""
            if "amount" in changes or "date" in changes:  # the two sides of a transfer stay in step
                def follow(other_rows, i):
                    other_rows[i] = dict(other_rows[i], date=rows[index]["date"],
                                         amount=_negated(rows[index]["amount"]))
                # the old row: its date says where the other side is
                other, also = self._other_side(dict(rows[index], date=before["date"]), account, follow)
            return self._save(account, year, fmt, rows, msg + also, other)

    def delete_row(self, account, year, rev, index):
        account, fmt, rows = self._load_for_write(account, year, rev)
        index = self._index(index, rows)
        row = rows.pop(index)
        msg = "%s: delete row %d" % (Store.label(account, year), index + 1)
        with self.store.cached():  # a transfer is deleted from both accounts
            other, also = self._other_side(row, account, lambda other_rows, i: other_rows.pop(i))
            return self._save(account, year, fmt, rows, msg + also, other)

    def reconcile(self, account, year, rev, cleared, uncleared=()):
        """Tick (cleared) and untick (uncleared) rows of one sheet, by index, in one commit: the
        result of checking the sheet against a statement."""
        account, fmt, rows = self._load_for_write(account, year, rev)
        marks = {}
        for value, indexes in ((True, cleared), (False, uncleared)):
            for i in indexes:
                if self._index(i, rows) in marks:
                    raise StoreError("invalid", "row %d is listed twice" % (i + 1), "index")
                marks[i] = value
        if not marks:
            raise StoreError("invalid", "no rows to mark", "cleared")
        for i, value in marks.items():
            rows[i] = dict(rows[i], cleared=value)
        return self._save(account, year, fmt, rows,
                          "%s: reconcile (%d rows)" % (Store.label(account, year), len(marks)))

    def add_transfer(self, from_account, to_account, date, amount, description="",
                     category=formats.TRANSFER_CATEGORY, note="", id=None):
        """A transfer between two accounts: one row appended to each, linked by a shared id, in
        one commit. amount is positive; it leaves from_account and arrives in to_account.

        id: the link id, chosen by the caller, so that sending the same request again (after a
        timeout, say) does not enter the transfer twice. An id already on other rows is refused."""
        self.store.require_clean()
        source, target = self.store.account(from_account), self.store.account(to_account)
        if source["id"] == target["id"]:
            raise StoreError("invalid", "choose two different accounts", "to_account")
        fields = self._fields(formats.FORMATS[source["type"]], {
            "date": date, "amount": amount, "description": description, "category": category, "note": note})
        if not fields["date"]:
            raise StoreError("invalid", "a transfer needs a date", "date")
        if not fields["amount"] or money.to_decimal(fields["amount"]) <= 0:
            raise StoreError("invalid", "the amount must be more than zero", "amount")
        if id is not None and not formats.LINK_ID.match(id):
            raise StoreError("invalid", "invalid transfer id", "id")
        link = id or secrets.token_hex(8)
        with self.store.cached():  # the search for the id, the two sheets and the save read each sheet once
            # A caller-chosen id must not already be on any row, unless those rows are this very
            # transfer (the request sent twice): then nothing is written and the same answer given.
            try:
                held = [(a["id"], y, r["date"], r["amount"]) for a, y, _, r in self.store.rows(self.store.accounts())
                        if r["transfer"] == link] if id is not None else []
            except StoreError as e:
                if e.code != "corrupt":
                    raise
                raise StoreError("corrupt", "%s. Until that file is repaired, a transfer cannot be checked against "
                                 "the ones already entered, so none is added (README: \"Damaged sheet files\")"
                                 % e.message) from None
            if held:
                want = sorted([(source["id"], fields["date"], _negated(fields["amount"])),
                               (target["id"], fields["date"], fields["amount"])])
                if sorted((i, d, amt) for i, _, d, amt in held) != want:
                    raise StoreError("conflict", "a transfer was already entered with this id, with a different date, "
                                     "amount or accounts; check both accounts before entering it again", "id")
                where = {i: y for i, y, _, _ in held}
                return {"repo_clean": self.store.is_clean(),
                        "from": {"account": source["id"], "year": where[source["id"]]},
                        "to": {"account": target["id"], "year": where[target["id"]]}}
            sides = []
            for a, other, word, value in ((source, target, "to", _negated(fields["amount"])),
                                          (target, source, "from", fields["amount"])):
                fmt = formats.FORMATS[a["type"]]
                existing = set(self.store.sheets(a))
                y = year_tab(a, fields["date"], existing)
                rows = self.store.read_sheet(a, y)[0] if y in existing else []  # else: next year's tab
                text = fields["description"] or ("Transfer %s %s" % (word, other["name"]))[:formats.TEXT_LIMITS["description"]]
                rows.append(dict(fmt.blank_row(), date=fields["date"], description=text, category=fields["category"],
                                 amount=value, note=fields["note"], transfer=link))
                sides.append((a, y, rows))
            (a, y, rows), other = sides
            self._save(a, y, formats.FORMATS[a["type"]], rows,
                       "transfer from %s to %s" % (Store.label(a, y), Store.label(other[0], other[1])), other,
                       view=False)
            return {"repo_clean": self.store.is_clean(),
                    "from": {"account": a["id"], "year": y}, "to": {"account": other[0]["id"], "year": other[1]}}

    @staticmethod
    def _fields(fmt, fields):
        try:
            for f in formats.LINK_FIELDS:
                if isinstance(fields, dict) and f in fields:
                    raise formats.FieldError(f, "is set by the app and cannot be edited")
            return formats.validate_partial(fmt, fields)
        except formats.FieldError as e:
            raise StoreError(e.code, e.message, e.field) from None

    # ------------------------------------------------------------ change log and undo
    # Read from git. Nothing in history is ever rewritten: an undo is a new commit that puts
    # the old values back, and only when that can be done without guessing.

    def _version(self, fmt, rel, rev, seen=None):
        """The sheet's rows at a commit: [] if it did not exist yet, None if that version cannot
        be read (it is from before a password change, or was damaged). seen: see
        store.decode_rows."""
        data = gitrepo.show(self.home.data, rev, rel)
        if data is None:
            return []
        try:
            return decode_rows(fmt, data, rel, self.store.require_key(), seen=seen)
        except StoreError as e:
            if e.code in ("wrong_key", "corrupt"):
                return None
            raise

    @staticmethod
    def _changes(fmt, before, after):
        """What turned `before` into `after`: [{kind, row, before, after}], kind one of changed,
        added, deleted; row counts from 1 (in `before` for a deleted row)."""
        a, b = [dumps_row(fmt, r) for r in before], [dumps_row(fmt, r) for r in after]
        out = []
        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
            if op == "equal":
                continue
            pairs = min(i2 - i1, j2 - j1)
            for k in range(pairs):
                out.append({"kind": "changed", "row": j1 + k + 1, "before": before[i1 + k], "after": after[j1 + k]})
            for i in range(i1 + pairs, i2):
                out.append({"kind": "deleted", "row": i + 1, "before": before[i], "after": None,
                            "above": before[i - 1] if i else None,
                            "below": before[i + 1] if i + 1 < len(before) else None})
            for j in range(j1 + pairs, j2):
                out.append({"kind": "added", "row": j + 1, "before": None, "after": after[j]})
        return out

    @staticmethod
    def _undone(rel, files, rows, changes):
        """`rows` with a commit's `changes` to the sheet `rel` taken back; `files` is every path
        that commit touched. StoreError('conflict') says why it cannot be done: the commit
        changed other files too, or a later change touched the same rows. Rows are found by
        their content, so rows added or removed elsewhere in the sheet since do not matter."""
        if not changes:
            raise StoreError("conflict", "nothing in this sheet changed")
        if files != [rel]:
            raise StoreError("conflict", "it changed more than this sheet (a transfer, an import or the like)")
        rows = list(rows)

        def find(row, near):
            hits = [i for i, r in enumerate(rows) if r == row]
            if not hits:
                raise StoreError("conflict", "a later change touched the same row; undo that one first, "
                                             "or put the old values back by hand")
            return min(hits, key=lambda i: abs(i - near))

        for ch in sorted(changes, key=lambda c: c["kind"] == "deleted"):  # deletions last: they shift rows
            if ch["kind"] == "changed":
                rows[find(ch["after"], ch["row"] - 1)] = ch["before"]
            elif ch["kind"] == "added":
                rows.pop(find(ch["after"], ch["row"] - 1))
            else:  # back between its old neighbours, wherever they are now: under the row it
                # followed or, if that one has changed since, above the row that followed it
                try:
                    at = 0 if ch["above"] is None else find(ch["above"], ch["row"] - 2) + 1
                except StoreError:
                    if ch["below"] is None:
                        at = len(rows)  # it was the last row
                    else:
                        at = find(ch["below"], ch["row"] - 1)  # neither neighbour is left: a conflict
                rows.insert(at, ch["before"])
        return rows

    def history(self, account, year, limit=HISTORY_DEFAULT):
        """The latest changes to one sheet, newest first, each with the rows it changed and
        whether it can be undone now."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= HISTORY_MAX:
            raise StoreError("invalid", "limit must be between 1 and %d" % HISTORY_MAX, "limit")
        account = self.store.account(account)
        self.store.check_sheet(account, year)
        rel, fmt = self.store.relpath(account, year), formats.FORMATS[account["type"]]
        # the sheet as it is now and its earlier versions share nearly all their lines: each line
        # is decrypted once
        seen = {}
        path = self.home.data / rel
        if not path.is_file():
            raise StoreError("not_found", "sheet file missing: %s" % rel)
        current = decode_rows(fmt, path.read_bytes(), rel, self.store.require_key(), seen=seen)
        commits = gitrepo.log_with_files(self.home.data, rel, limit + 1)
        versions = [self._version(fmt, rel, c[0], seen) for c in commits]
        label = Store.label(account, year) + ": "
        entries = []
        for i, (commit, date, subject, files) in enumerate(commits[:limit]):
            after = versions[i]
            before = versions[i + 1] if i + 1 < len(commits) else []
            entry = {"commit": commit, "date": date, "subject": subject,
                     "summary": subject[len(label):] if subject.startswith(label) else subject,
                     "changes": [], "more": 0, "undo": False}
            if after is None or before is None:
                entry["why"] = UNREADABLE
            else:
                changes = self._changes(fmt, before, after)
                entry["changes"] = [{k: c[k] for k in ("kind", "row", "before", "after")}
                                    for c in changes[:HISTORY_CHANGES]]
                entry["more"] = max(0, len(changes) - HISTORY_CHANGES)
                try:
                    self._undone(rel, files, current, changes)
                    entry["undo"] = True
                except StoreError as e:
                    entry["why"] = e.message
            entries.append(entry)
        return {"repo_clean": self.store.is_clean(), "entries": entries, "more": len(commits) > limit}

    def undo(self, account, year, rev, commit):
        """Take back one earlier change to a sheet, as a new commit."""
        account, fmt, rows = self._load_for_write(account, year, rev)
        rel = self.store.relpath(account, year)
        # only a change the history lists (so: a commit on this branch that touched this sheet);
        # `commit` reaches git only after it is found among git's own answers
        listed = {c[0]: c[3] for c in gitrepo.log_with_files(self.home.data, rel, HISTORY_MAX + 1)}
        if not isinstance(commit, str) or commit not in listed:
            raise StoreError("not_found", "no such change to this sheet", "commit")
        files = listed[commit]
        seen = {}  # the two versions differ by a few lines
        after, before = self._version(fmt, rel, commit, seen), self._version(fmt, rel, commit + "^", seen)
        if after is None or before is None:
            raise StoreError("invalid", "cannot undo: " + UNREADABLE, "commit")
        try:
            rows = self._undone(rel, files, rows, self._changes(fmt, before, after))
        except StoreError as e:
            raise StoreError("conflict", "cannot undo: " + e.message, "commit") from None
        return self._save(account, year, fmt, rows, "%s: undo %s" % (Store.label(account, year), commit[:10]))

    # ------------------------------------------------------------ recurring transactions
    # Templates are entered only when asked (enter_recurring); nothing is written on a timer.

    def list_recurring(self):
        today, names = self.today(), {a["id"]: a["name"] for a in self.store.accounts()}
        out = []
        for t in self.store.recurring():
            nxt = recurring.next_date(t)
            out.append(dict(t, name=names.get(t["account"], t["account"]), next=nxt.isoformat() if nxt else "",
                            due=len(recurring.due(t, today))))
        return {"repo_clean": self.store.is_clean(), "recurring": out, "due": sum(t["due"] for t in out)}

    def _write_recurring(self, templates, message, sheets=None):
        self.store.write_files({RECURRING_FILE: (RECURRING, templates), **(sheets or {})}, message)
        return self.list_recurring()

    def add_recurring(self, account, description, amount, every, start, category="", note=""):
        self.store.require_clean()
        templates = self.store.recurring()
        if len(templates) >= recurring.MAX_TEMPLATES:
            raise StoreError("invalid", "there are already %d recurring transactions" % recurring.MAX_TEMPLATES)
        try:
            text = {f: formats.check_text(v, f, formats.TEXT_LIMITS[f]).strip()  # normalized, as row edits are
                    for f, v in (("description", description), ("category", category), ("note", note))}
            new = recurring.validate({
                "id": secrets.token_hex(8), "account": self.store.account(account)["id"],
                "description": text["description"], "category": text["category"], "amount": amount,
                "note": text["note"], "every": every, "start": start, "count": 0})
        except formats.FieldError as e:
            raise StoreError("invalid", e.message, e.field) from None
        return self._write_recurring(templates + [new], "add a recurring transaction for %s" % new["account"])

    def _recurring(self, template_id):
        templates = self.store.recurring()
        for i, t in enumerate(templates):
            if t["id"] == template_id:
                return templates, i
        raise StoreError("not_found", "no such recurring transaction", "id")

    def delete_recurring(self, id):
        self.store.require_clean()
        templates, i = self._recurring(id)
        gone = templates.pop(i)
        return self._write_recurring(templates, "delete a recurring transaction for %s" % gone["account"])

    def skip_recurring(self, id, count=None):
        """Pass over the next date without entering it. count: how many dates the caller saw as
        entered or skipped so far; if that is no longer so (the request was sent twice, or
        another window got there first), nothing is skipped."""
        self.store.require_clean()
        templates, i = self._recurring(id)
        if count is not None and count != templates[i]["count"]:
            raise StoreError("stale", "that date was already entered or skipped; look at the list again", "count")
        if recurring.next_date(templates[i]) is None:
            raise StoreError("invalid", "this recurring transaction has no later date", "id")
        templates[i] = dict(templates[i], count=templates[i]["count"] + 1)
        return self._write_recurring(templates, "skip a recurring transaction for %s" % templates[i]["account"])

    def enter_recurring(self, ids=None):
        """Enter every due date of the given templates (all of them by default) as rows at the
        end of their accounts' sheets, in one commit."""
        self.store.require_clean()
        templates, today = self.store.recurring(), self.today()
        if ids is not None and (not all(isinstance(i, str) for i in ids)
                                or set(ids) - {t["id"] for t in templates}):
            raise StoreError("not_found", "no such recurring transaction", "ids")
        accounts = {a["id"]: a for a in self.store.accounts()}
        touched, on_disk, entered = {}, {}, 0  # account id -> {year: rows}; account id -> its sheets on disk
        with self.store.cached():
            for n, t in enumerate(templates):
                dates = recurring.due(t, today) if ids is None or t["id"] in ids else []
                if not dates:
                    continue
                account = accounts.get(t["account"])
                if account is None:
                    raise StoreError("not_found", "no such account: %s" % t["account"], "account")
                fmt, sheets = formats.FORMATS[account["type"]], touched.setdefault(account["id"], {})
                if account["id"] not in on_disk:
                    on_disk[account["id"]] = set(self.store.sheets(account))
                stored = on_disk[account["id"]]
                for d in dates:
                    try:
                        year = year_tab(account, d.isoformat(), stored | set(sheets))
                    except StoreError as e:
                        raise StoreError(e.code, "\"%s\": %s" % (t["description"], e.message), e.field) from None
                    if year not in sheets:
                        sheets[year] = self.store.read_sheet(account, year)[0] if year in stored else []
                    sheets[year].append(dict(fmt.blank_row(), date=d.isoformat(), description=t["description"],
                                             category=t["category"], amount=t["amount"], note=t["note"]))
                templates[n] = dict(t, count=t["count"] + len(dates))
                entered += len(dates)
            if not entered:
                raise StoreError("invalid", "nothing is due", "ids")
            files = {}
            for account_id, sheets in touched.items():
                account = accounts[account_id]
                self.store.check_balances(account, sheets)
                files.update({self.store.relpath(account, y): (formats.FORMATS[account["type"]], rows)
                              for y, rows in sheets.items()})
            return dict(self._write_recurring(templates, "enter %d recurring transaction(s)" % entered, files),
                        entered=entered)

    # ------------------------------------------------------------ tabs and accounts

    def add_year(self, account):
        self.store.require_clean()
        account = self.store.account(account)
        if not formats.FORMATS[account["type"]].yearly:
            raise StoreError("invalid", "%s has no year tabs" % account["id"], "account")
        years = self.store.years(account)
        year = years[-1] + 1 if years else datetime.date.today().year
        if year > formats.MAX_YEAR:
            raise StoreError("invalid", "year out of range", "year")
        self.store.write_files({self.store.relpath(account, year): (formats.FORMATS[account["type"]], [])},
                               "%s: add year %d" % (account["id"], year))
        return {"year": year, **self.list_tabs()}

    def add_account(self, name, type, opening_balance, year=None, account_id=None, owed_positive=None):
        """opening_balance is in the stored sign: negative for what is owed on a card or loan."""
        self.store.require_clean()
        name = self._name(name)
        if type not in formats.FORMATS:
            raise StoreError("invalid", "type must be one of %s" % ", ".join(formats.FORMATS), "type")
        fmt = formats.FORMATS[type]
        try:
            opening = formats.check_amount(opening_balance, "opening_balance") or "0.00"
        except formats.FieldError as e:
            raise StoreError("invalid", e.message, "opening_balance") from None
        if fmt.yearly:
            if year is None:
                year = datetime.date.today().year
            if not isinstance(year, int) or isinstance(year, bool) or not formats.MIN_YEAR <= year <= formats.MAX_YEAR:
                raise StoreError("invalid", "year out of range", "year")
        elif year is not None:
            raise StoreError("invalid", "%s accounts have no year tabs" % type, "year")
        accounts = self.store.accounts()
        taken = {a["id"] for a in accounts}
        if account_id is not None:
            if not isinstance(account_id, str) or not ACCOUNT_ID.match(account_id):
                raise StoreError("invalid", "invalid account id", "account")
            if account_id in taken:
                raise StoreError("invalid", "account id already exists", "account")
        else:
            base = account_id = slugify(name)
            n = 2
            while account_id in taken:
                suffix = "-%d" % n
                account_id = base[: ID_LIMIT - len(suffix)] + suffix
                n += 1
        if (self.home.data / account_id).exists():
            raise StoreError("invalid", "a directory named %s already exists in the data repo" % account_id)
        account = new_account(account_id, name, type, owed_positive)
        self.store.write_files({
            ACCOUNTS_FILE: (ACCOUNTS, accounts + [account]),
            self.store.relpath(account, year): (fmt, [fmt.opening_row(opening)]),
        }, "add account %s (%s)" % (account_id, type))
        return {"account": account_id, **self.list_tabs()}

    def rename_account(self, account, name, owed_positive=None):
        """Change an account's name and, if given, whether its amounts are shown with the sign
        flipped (what is owed as positive). Stored amounts never change."""
        self.store.require_clean()
        name = self._name(name)
        accounts = self.store.accounts()
        target = self.store.account(account)
        changed = dict(target, name=name)
        if owed_positive is not None:
            changed["owed_positive"] = owed_positive
        what = "rename account %s" if changed["owed_positive"] == target["owed_positive"] \
            else "change the settings of account %s"
        self.store.write_files({ACCOUNTS_FILE: (ACCOUNTS, [changed if a["id"] == target["id"] else a
                                                           for a in accounts])}, what % target["id"])
        return self.list_tabs()

    @staticmethod
    def _name(name):
        try:
            name = formats.check_text(name, "name", NAME_LIMIT).strip()
        except formats.FieldError as e:
            raise StoreError("invalid", e.message, "name") from None
        if not name:
            raise StoreError("invalid", "name is required", "name")
        return name
