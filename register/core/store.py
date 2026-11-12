"""Home directory layout, account list, sheet files, balances and transactional writes.

All data is encrypted (see crypto.py): each sheet row is one encrypted line, accounts.json
is one encrypted item, crypto.json holds the (non-secret) key parameters. Unchanged rows keep
their exact ciphertext line, so git diffs stay one line per changed row.

Every write goes through Store.write_files(), which:
  1. refuses if the repository has uncommitted changes (repo_dirty),
  2. writes each file to a temp file, fsyncs it, reads it back and re-parses it,
     and checks it matches the intended content exactly,
  3. moves the files into place (os.replace) and makes one commit,
  4. on any failure, puts the previous contents back so nothing changes.
"""
import contextlib
import fcntl
import hashlib
import json
import os
import re
from decimal import Decimal
from pathlib import Path

from . import crypto, formats, gitrepo, money, recurring

FORMAT_VERSION = crypto.FORMAT_VERSION  # accounts.json and the sheets follow crypto.json
ACCOUNTS = "accounts"   # write_files kinds besides a sheet Format
CRYPTO = "crypto"
RECURRING = "recurring"
ACCOUNT_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?\Z")
YEAR_FILE = re.compile(r"^([0-9]{4})\.jsonl\Z")
SAVINGS_FILE = "register.jsonl"
ACCOUNTS_FILE = "accounts.json"
RECURRING_FILE = "recurring.json"  # recurring-transaction templates; absent until the first is added
NAME_LIMIT = 60
ACCOUNT_KEYS = ("id", "name", "type", "owed_positive")  # owed_positive: display amounts with the sign flipped


def year_tab(account, date, years):
    """The year tab a new row dated `date` goes in (None for an account with a single tab). One
    rule for transfers, recurring entries and imports: an existing year, or the year after
    the last one, which is then created. Anything else is refused, so an account's years stay
    in one unbroken run that starts with its opening balance.

    years: the account's year tabs, those on disk and any the operation is about to add; empty
    for an account that is being created. A year returned that is not among them is new."""
    if not formats.FORMATS[account["type"]].yearly:
        return None
    year = int(date[:4])
    if years and year not in years:
        if year < min(years):
            raise StoreError("invalid", "%s starts in %d; a row dated %s would come before its opening balance"
                             % (account["name"], min(years), date), "date")
        if year != max(years) + 1:
            raise StoreError("invalid", "%s has no %d tab: its last year is %d, and only the year after that is "
                             "added automatically. Add the years in between first"
                             % (account["name"], year, max(years)), "date")
    return year


def new_account(account_id, name, type, owed_positive=None):
    """An account entry; owed_positive defaults by type (on for credit cards and loans)."""
    return {"id": account_id, "name": name, "type": type,
            "owed_positive": formats.FORMATS[type].owed_positive if owed_positive is None else owed_positive}


def sum_amounts(rows):
    """Total of the rows' "amount" fields (blank counts as 0)."""
    return sum((money.to_decimal(r["amount"]) for r in rows), Decimal(0))


class StoreError(Exception):
    def __init__(self, code, message, field=None):
        super().__init__(message)
        self.code, self.message, self.field = code, message, field


class Home:
    """The --home directory: config.json, run/ (socket, lock) and data/ (git repo)."""

    def __init__(self, path):
        self.root = Path(path).expanduser().resolve()
        self.config = self.root / "config.json"
        self.run = self.root / "run"
        self.data = self.root / "data"
        self.lock_path = self.run / "write.lock"
        self.socket_path = self.run / "recorder.sock"

    def check(self):
        if not (self.data / ".git").is_dir() or not (self.data / ACCOUNTS_FILE).is_file():
            raise StoreError("not_initialized", "%s is not an initialized register home "
                             "(run: register.pyz init --home %s)" % (self.root, self.root))
        if not (self.data / crypto.CRYPTO_FILE).is_file():
            raise StoreError("not_initialized", "%s is an unencrypted home from an older version; "
                             "run init in a new directory and import again" % self.root)

    def crypto_doc(self):
        try:
            return crypto.loads_doc((self.data / crypto.CRYPTO_FILE).read_bytes())
        except crypto.CryptoError as e:
            if e.code == "old_format":
                raise StoreError(e.code, e.message) from None
            raise StoreError("corrupt", "cannot read crypto.json: %s" % e) from None
        except OSError as e:
            raise StoreError("corrupt", "cannot read crypto.json: %s" % e) from None

    def unlock(self, password):
        """Derive and verify the key for a password (raises StoreError bad_password)."""
        self.check()
        try:
            return crypto.unlock(password, self.crypto_doc())
        except crypto.CryptoError as e:
            raise StoreError(e.code, e.message) from None


@contextlib.contextmanager
def locked(home, exclusive=True):
    """Serialize writers (recorder and CLI) with a file lock in run/."""
    home.run.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(home.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ---------------------------------------------------------------- serialization

def _crypto_error(e):
    return StoreError("wrong_key" if e.code == "wrong_key" else "corrupt", e.message)


def dumps_row(fmt, row):
    """The canonical plaintext of one row (what gets encrypted)."""
    return json.dumps({f: row[f] for f in fmt.fields}, ensure_ascii=False, separators=(",", ":"))


def encode_rows(fmt, rows, rel, key, reuse=None):
    """Encrypt rows to file bytes. reuse maps canonical row text -> existing lines, so an
    unchanged row keeps its exact ciphertext."""
    reuse = {k: list(v) for k, v in (reuse or {}).items()}
    lines = []
    for r in rows:
        plain = dumps_row(fmt, r)
        if reuse.get(plain):
            lines.append(reuse[plain].pop(0))
        else:
            lines.append(crypto.dumps_item(key.encrypt(rel, plain.encode("utf-8"))))
    return "".join(line + "\n" for line in lines).encode("ascii")


def decode_rows(fmt, data, rel, key, with_lines=False, seen=None):
    """Decrypt and strictly validate a sheet file: one canonical row per line, keys in order.

    seen: a dict the caller keeps while decoding several versions of the same file. A line met
    before is not decrypted again (unchanged rows keep their exact line from version to
    version)."""
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise StoreError("corrupt", "%s is not an encrypted sheet file" % rel) from None
    if text and not text.endswith("\n"):
        raise StoreError("corrupt", "%s does not end with a newline" % rel)
    rows, lines = [], []
    for n, line in enumerate(text.split("\n")[:-1], 1):
        if seen is not None and line in seen:
            row, plain = seen[line]
            rows.append(dict(row))
            lines.append((plain, line))
            continue
        try:
            plain = key.decrypt(rel, crypto.loads_item(line, rel)).decode("utf-8")
            pairs = json.loads(plain, object_pairs_hook=list)
        except crypto.CryptoError as e:
            raise StoreError(_crypto_error(e).code, "%s line %d: %s" % (rel, n, e.message)) from None
        except (ValueError, UnicodeDecodeError):
            raise StoreError("corrupt", "%s line %d does not decrypt to a valid row" % (rel, n)) from None
        if not isinstance(pairs, list) or [k for k, _ in pairs] != list(fmt.fields):
            raise StoreError("corrupt", "%s line %d does not have the %s fields in order" % (rel, n, fmt.name))
        try:
            row = formats.validate_row(fmt, dict(pairs))
        except formats.FieldError as e:
            raise StoreError("corrupt", "%s line %d: %s" % (rel, n, e)) from None
        if dumps_row(fmt, row) != plain:
            raise StoreError("corrupt", "%s line %d is not in canonical form" % (rel, n))
        if seen is not None:
            seen[line] = (dict(row), plain)
        rows.append(row)
        lines.append((plain, line))
    return (rows, lines) if with_lines else rows


def _encode_list(rel, name, items, key):
    """A whole list as one encrypted item (accounts.json, recurring.json)."""
    doc = {"format_version": FORMAT_VERSION, name: items}
    plain = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    item = dict(key.encrypt(rel, plain), format_version=FORMAT_VERSION)
    return (crypto.dumps_item(item) + "\n").encode("ascii")


def _decode_list(rel, name, data, key):
    try:
        item = crypto.loads_item(data.decode("ascii"), rel)
        if item.get("format_version") != FORMAT_VERSION:
            raise StoreError("corrupt", "%s has an unsupported format_version" % rel)
        doc = json.loads(key.decrypt(rel, item).decode("utf-8"))
    except crypto.CryptoError as e:
        raise _crypto_error(e) from None
    except (ValueError, UnicodeDecodeError):
        raise StoreError("corrupt", "%s does not decrypt to a valid list" % rel) from None
    if not isinstance(doc, dict) or doc.get("format_version") != FORMAT_VERSION:
        raise StoreError("corrupt", "%s has an unsupported format_version" % rel)
    if not isinstance(doc.get(name, []), list):
        raise StoreError("corrupt", "%s does not decrypt to a valid list" % rel)
    return doc.get(name, [])


def encode_recurring(templates, key):
    return _encode_list(RECURRING_FILE, "recurring", [{k: t[k] for k in recurring.KEYS} for t in templates], key)


def decode_recurring(data, key):
    out, seen = [], set()
    for t in _decode_list(RECURRING_FILE, "recurring", data, key):
        try:
            t = recurring.validate(t)
        except formats.FieldError as e:
            raise StoreError("corrupt", "recurring.json has an invalid entry: %s" % e) from None
        if t["id"] in seen or len(out) >= recurring.MAX_TEMPLATES:
            raise StoreError("corrupt", "recurring.json has a repeated entry or too many entries")
        seen.add(t["id"])
        out.append(t)
    return out


def encode_accounts(accounts, key):
    return _encode_list(ACCOUNTS_FILE, "accounts",
                        [{k: a[k] for k in ACCOUNT_KEYS} for a in accounts], key)


def decode_accounts(data, key):
    accounts, seen = [], set()
    for a in _decode_list(ACCOUNTS_FILE, "accounts", data, key):
        if (not isinstance(a, dict) or set(a) != set(ACCOUNT_KEYS) or not isinstance(a["owed_positive"], bool)
                or not isinstance(a["id"], str) or not ACCOUNT_ID.match(a["id"])
                or a["type"] not in formats.FORMATS or a["id"] in seen):
            raise StoreError("corrupt", "accounts.json has an invalid account entry")
        try:
            formats.check_text(a["name"], "name", NAME_LIMIT)
        except formats.FieldError as e:
            raise StoreError("corrupt", "accounts.json has an invalid account name: %s" % e.message) from None
        seen.add(a["id"])
        accounts.append(a)
    return accounts


# whole-list files, each one encrypted item: kind -> (decode, encode)
LIST_KINDS = {ACCOUNTS: (decode_accounts, encode_accounts), RECURRING: (decode_recurring, encode_recurring)}


def rev_of(data):
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- store

class Store:
    def __init__(self, home, key=None):
        self.home = home
        self.data = home.data
        self.key = key  # crypto.Key, or None while locked
        self._sheets = None  # decrypted sheets inside cached(), else None

    def require_key(self):
        """The current key, or StoreError('locked'). Also detects a password change made
        elsewhere (crypto.json now names a different key): the stale key is dropped."""
        if self.key is None:
            raise StoreError("locked", "the register is locked; log in to unlock it")
        if self.home.crypto_doc()["key_id"] != self.key.key_id:
            self.key = None
            raise StoreError("locked", "the password was changed; log in again")
        return self.key

    # --- reading

    def accounts(self):
        return decode_accounts((self.data / ACCOUNTS_FILE).read_bytes(), self.require_key())

    def recurring(self):
        """The recurring-transaction templates ([] until the first one is saved)."""
        path = self.data / RECURRING_FILE
        return decode_recurring(path.read_bytes(), self.require_key()) if path.is_file() else []

    def account(self, account_id):
        if not isinstance(account_id, str) or not ACCOUNT_ID.match(account_id):
            raise StoreError("invalid", "invalid account id", "account")
        for a in self.accounts():
            if a["id"] == account_id:
                return a
        raise StoreError("not_found", "no such account: %s" % account_id, "account")

    def sheets(self, account):
        """The account's sheets as their `year` values: its years, or [None] for a single tab."""
        return self.years(account) if formats.FORMATS[account["type"]].yearly else [None]

    def years(self, account):
        d = self.data / account["id"]
        if not d.is_dir():
            return []
        return sorted(int(m.group(1)) for m in (YEAR_FILE.match(p.name) for p in d.iterdir()) if m)

    @staticmethod
    def label(account, year):
        """Short sheet name for commit messages and reports: "checking/2026" or "savings"."""
        return account["id"] if year is None else "%s/%d" % (account["id"], year)

    @staticmethod
    def relpath(account, year):
        if formats.FORMATS[account["type"]].yearly:
            return "%s/%04d.jsonl" % (account["id"], year)
        return "%s/%s" % (account["id"], SAVINGS_FILE)

    def check_sheet(self, account, year):
        """Validate that (account, year) names an existing sheet."""
        if formats.FORMATS[account["type"]].yearly:
            if not isinstance(year, int) or isinstance(year, bool) or year not in self.years(account):
                raise StoreError("not_found", "no such year for %s" % account["id"], "year")
        elif year is not None:
            raise StoreError("invalid", "%s has no year tabs" % account["id"], "year")

    @contextlib.contextmanager
    def cached(self):
        """Within the block, each sheet file is decrypted once: an edit checks the balances of
        the earlier and later years and then shows the sheet, which reads the earlier years
        again. Entries are keyed by the file's content hash, so a file written in between is
        read afresh; nothing decrypted is kept after the block. Blocks may nest."""
        outer = self._sheets is not None
        if not outer:
            self._sheets = {}
        try:
            yield
        finally:
            if not outer:
                self._sheets = None

    def read_sheet(self, account, year):
        self.check_sheet(account, year)
        rel = self.relpath(account, year)
        path = self.data / rel
        if not path.is_file():
            raise StoreError("not_found", "sheet file missing: %s" % rel)
        data = path.read_bytes()
        rev, key = rev_of(data), self.require_key()
        cache = self._sheets
        if cache is not None and (rel, rev, key.key_id) in cache:
            rows = cache[(rel, rev, key.key_id)]
        else:
            rows = decode_rows(formats.FORMATS[account["type"]], data, rel, key)
            if cache is not None:
                cache[(rel, rev, key.key_id)] = rows
        return [dict(r) for r in rows], rev  # copies: callers edit the rows they get

    def rows(self, accounts):
        """(account, year, index, row) for every row of the accounts' sheets, in order."""
        for a in accounts:
            for y in self.sheets(a):
                for i, r in enumerate(self.read_sheet(a, y)[0]):
                    yield a, y, i, r

    def opening(self, account, year):
        """Balance before the first row of a year: the sum of all earlier years (0 for the first).
        (The sheet view adds these up itself, together with the cleared total.)"""
        if year is None:
            return Decimal(0)
        return sum((sum_amounts(self.read_sheet(account, y)[0]) for y in self.years(account) if y < year),
                   Decimal(0))

    def check_balances(self, account, changed, new_account=False):
        """Raise StoreError if writing `changed` ({year: rows}, year None for savings) would put a
        running balance, in those sheets or in later years they carry into, beyond money.MAX_ABS.

        If balances are already out of range (data stored before this check), a write is still
        allowed as long as it creates no balance above the highest or below the lowest one already
        there (including the balance carried in), so the data can be fixed. For a new account
        nothing is read from disk.

        A damaged earlier or later sheet stops the edit (on purpose: the balance carried through it
        cannot be checked); the error says which file and where the README explains the repair."""
        try:
            self._check_balances(account, changed, new_account)
        except StoreError as e:
            if e.code != "corrupt":
                raise
            raise StoreError("corrupt", "edit refused: %s. Balances carry through every year of %s, so it "
                             "can't be checked until that file is repaired (README: \"Damaged sheet files\")"
                             % (e.message, account["id"])) from None

    def _check_balances(self, account, changed, new_account):
        if new_account:
            existing, opening = set(), Decimal(0)
        elif None in changed:
            existing, opening = {None} if (self.data / self.relpath(account, None)).is_file() else set(), Decimal(0)
        else:
            existing = set(self.years(account))
            opening = self.opening(account, min(changed))
        years = sorted(y for y in existing | set(changed) if y is None or y >= min(changed))

        def walk(sheets):
            """[(year, row index, running balance)] for every row, in order."""
            running, out = opening, []
            for y in years:
                for i, r in enumerate(sheets(y)):
                    running += money.to_decimal(r["amount"])
                    out.append((y, i, running))
            return out

        stored = {}

        def stored_rows(y):
            if y not in stored:
                stored[y] = self.read_sheet(account, y)[0] if y in existing else []
            return stored[y]

        after = walk(lambda y: changed[y] if y in changed else stored_rows(y))
        if all(abs(b) <= money.MAX_ABS for _, _, b in after):
            return
        before = [opening] + [b for _, _, b in walk(stored_rows)]
        hi, lo = max([money.MAX_ABS] + before), min([-money.MAX_ABS] + before)
        for y, i, b in after:
            if b > hi or b < lo:
                raise StoreError("invalid", "the balance at %s row %d would be larger than %s"
                                 % (self.label(account, y), i + 1, money.fmt(money.MAX_ABS)), "amount")

    def sheet_view(self, account_id, year):
        with self.cached():
            return self._sheet_view(account_id, year)

    def _sheet_view(self, account_id, year):
        account = self.account(account_id)
        rows, rev = self.read_sheet(account, year)
        # the earlier years once, for both the balance carried in and (for reconciling) what
        # their cleared rows add up to
        opening, cleared_before, carried_from = Decimal(0), Decimal(0), None
        for y in ([] if year is None else [y for y in self.years(account) if y < year]):
            earlier, _ = self.read_sheet(account, y)
            opening += sum_amounts(earlier)
            cleared_before += sum_amounts(r for r in earlier if r["cleared"])
            carried_from = y
        balance, out = opening, []
        for r in rows:
            balance += money.to_decimal(r["amount"])
            out.append(dict(r, balance=money.fmt_balance(balance)))
        fmt = formats.FORMATS[account["type"]]
        return {"account": account["id"], "name": account["name"], "type": account["type"],
                "owed_positive": account["owed_positive"],
                "year": year, "fields": list(fmt.fields), "carried_from": carried_from,
                "opening": money.fmt_balance(opening), "cleared_before": money.fmt_balance(cleared_before),
                "rows": out, "rev": rev,
                "modified": gitrepo.last_change(self.data, self.relpath(account, year))}

    # --- writing

    def is_clean(self):
        return gitrepo.is_clean(self.data)

    def require_clean(self):
        if not self.is_clean():
            raise StoreError("repo_dirty", "the data repository has uncommitted changes; "
                             "commit or discard them with git")

    def _encode(self, rel, kind, content, old):
        """File bytes for one change. Unchanged rows/accounts keep their existing ciphertext."""
        if kind == CRYPTO:
            return crypto.dumps_doc(content)
        key = self.key  # write_files checked it; not require_key(): change-password writes a new key
        if kind in LIST_KINDS:
            decode, encode = LIST_KINDS[kind]
            if old is not None:
                with contextlib.suppress(StoreError):
                    if decode(old, key) == content:
                        return old
            return encode(content, key)
        reuse = {}
        if old is not None:
            with contextlib.suppress(StoreError):  # e.g. old key after a password change: re-encrypt all
                for plain, line in decode_rows(kind, old, rel, key, with_lines=True)[1]:
                    reuse.setdefault(plain, []).append(line)
        return encode_rows(kind, content, rel, key, reuse)

    def _read_back(self, rel, kind, data):
        if kind == CRYPTO:
            return crypto.loads_doc(data)
        if kind in LIST_KINDS:
            return LIST_KINDS[kind][0](data, self.key)
        return decode_rows(kind, data, rel, self.key)

    def write_files(self, changes, message):
        """Atomically write {relpath: (kind, content)} and commit. kind is a sheet Format
        (content: rows), ACCOUNTS (content: account list), RECURRING (content: templates) or
        CRYPTO (content: crypto.json doc).

        Returns False (and commits nothing) if the content is identical to what is stored.
        """
        self.require_clean()
        if self.key is None:
            raise StoreError("locked", "the register is locked; log in to unlock it")
        rels = sorted(changes)
        backups, temps, new_dirs = {}, {}, []
        for rel in rels:
            path = self.data / rel
            backups[rel] = path.read_bytes() if path.exists() else None
        encoded = {rel: self._encode(rel, kind, content, backups[rel]) for rel, (kind, content) in changes.items()}
        if all(backups[rel] == encoded[rel] for rel in rels):
            return False
        try:
            for rel in rels:
                kind, content = changes[rel]
                data = encoded[rel]
                path = self.data / rel
                if not path.parent.exists():
                    path.parent.mkdir()
                    new_dirs.append(path.parent)
                tmp = path.with_name(".tmp-" + path.name)
                temps[rel] = tmp
                with open(tmp, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                back = tmp.read_bytes()
                reparsed = self._read_back(rel, kind, back)
                expected = content if kind == CRYPTO or kind in LIST_KINDS else [{k: r[k] for k in kind.fields} for r in content]
                if back != data or reparsed != expected:
                    raise StoreError("write_failed", "read-back check failed for %s" % rel)
            for rel in rels:
                os.replace(temps.pop(rel), self.data / rel)
                _fsync_dir((self.data / rel).parent)
            gitrepo.add(self.data, rels)
            gitrepo.commit(self.data, message)
            return True
        except Exception as e:
            self._rollback(rels, backups, temps, new_dirs)
            if isinstance(e, StoreError):
                raise
            raise StoreError("write_failed", "write failed and was rolled back: %s" % e) from e

    def _rollback(self, rels, backups, temps, new_dirs=()):
        for tmp in temps.values():
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()
        for rel in rels:
            path = self.data / rel
            if backups[rel] is None:
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
            else:
                path.write_bytes(backups[rel])
        for d in new_dirs:  # e.g. a new account's directory, which would block a retry
            with contextlib.suppress(OSError):
                d.rmdir()
        # Re-stage the restored paths so the index matches HEAD again.
        for rel in rels:
            with contextlib.suppress(gitrepo.GitError):
                gitrepo.add(self.data, [rel])


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
