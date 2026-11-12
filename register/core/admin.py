"""Admin operations behind the command line: init, change-password, show, history, textconv.

Each command proves the password itself; none of them borrows the running recorder's key.
"""
import difflib
import json
from pathlib import Path

from . import crypto, formats, gitrepo
from .store import ACCOUNTS, ACCOUNTS_FILE, CRYPTO, LIST_KINDS, RECURRING, RECURRING_FILE, SAVINGS_FILE, YEAR_FILE, \
    Store, StoreError, decode_rows, locked


def is_initialized(home):
    return (home.data / crypto.CRYPTO_FILE).exists() or (home.data / ACCOUNTS_FILE).exists()


def init_home(home, password, git_name, git_email, n=crypto.DEFAULT_N):
    """Create a new encrypted home: git repo, crypto.json and an empty account list.

    The key is derived before anything is created, so a failure (e.g. a missing dependency)
    leaves nothing behind. A home left half-created by an earlier failure (an empty data/
    repo) is completed rather than refused.
    """
    if is_initialized(home):
        raise StoreError("invalid", "%s is already initialized" % home.root)
    if home.data.is_dir() and any(p.name != ".git" for p in home.data.iterdir()):
        raise StoreError("invalid", "%s already exists and is not empty" % home.data)
    key, doc = crypto.new_key(password, n)
    home.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    home.run.mkdir(mode=0o700, exist_ok=True)
    home.data.mkdir(mode=0o700, exist_ok=True)
    gitrepo.init(home.data, git_name, git_email)
    Store(home, key).write_files({crypto.CRYPTO_FILE: (CRYPTO, doc), ACCOUNTS_FILE: (ACCOUNTS, [])},
                                 "initialize register (encrypted)")


def change_password(home, old_password, new_password, n=crypto.DEFAULT_N):
    """Re-encrypt every current file under a key from the new password, in one commit."""
    home.check()
    with locked(home):
        old = Store(home, home.unlock(old_password))
        if new_password == old_password:
            raise StoreError("invalid", "the new password is the same as the current one", "new_password")
        accounts = old.accounts()
        sheets = {}
        for a in accounts:
            for y in old.sheets(a):
                sheets[Store.relpath(a, y)] = (formats.FORMATS[a["type"]], old.read_sheet(a, y)[0])
        templates = old.recurring()
        key, doc = crypto.new_key(new_password, n)
        new = Store(home, key)
        changes = {crypto.CRYPTO_FILE: (CRYPTO, doc), ACCOUNTS_FILE: (ACCOUNTS, accounts), **sheets}
        if (home.data / RECURRING_FILE).exists():
            changes[RECURRING_FILE] = (RECURRING, templates)
        new.write_files(changes, "change password (re-encrypt all data)")
    return len(sheets)


# ---------------------------------------------------------------- reading history

class KeyRing:
    """Keys for the password eras met while reading history. ask(prompt) supplies passwords;
    a password given once is tried against every later era before asking again."""

    def __init__(self, ask):
        self.ask = ask
        self.keys = {}
        self.passwords = []

    def key_for(self, doc, when):
        key_id = doc["key_id"]
        if key_id in self.keys:
            return self.keys[key_id]
        for pw in self.passwords:
            try:
                self.keys[key_id] = crypto.unlock(pw, doc)
                return self.keys[key_id]
            except crypto.CryptoError:
                pass
        for attempt in range(3):
            pw = self.ask("Password in use at %s (key %s): " % (when, key_id))
            try:
                self.keys[key_id] = crypto.unlock(pw, doc)
                self.passwords.append(pw)
                return self.keys[key_id]
            except crypto.CryptoError:
                print("wrong password for that period")
        raise StoreError("bad_password", "no valid password for data from %s" % when)


def _kind(rel):
    """How a data file is decoded: ACCOUNTS, or the sheet Format guessed from its name."""
    name = Path(rel).name
    if rel == ACCOUNTS_FILE:
        return ACCOUNTS
    if rel == RECURRING_FILE:
        return RECURRING
    if YEAR_FILE.match(name):
        return formats.FORMATS["checking"]
    if name == SAVINGS_FILE:
        return formats.FORMATS["savings"]
    raise StoreError("invalid", "%s is not a register data file" % rel)


def _lines(rel, data, key):
    """Readable lines of one file version: one JSON object per row (or per account)."""
    kind = _kind(rel)
    if kind in LIST_KINDS:
        return [json.dumps(item, ensure_ascii=False) for item in LIST_KINDS[kind][0](data, key)]
    return [json.dumps(r, ensure_ascii=False) for r in decode_rows(kind, data, rel, key)]


def _version(home, ring, rev, rel, when):
    data = gitrepo.show(home.data, rev, rel)
    if data is None:
        return None
    doc_bytes = gitrepo.show(home.data, rev, crypto.CRYPTO_FILE)
    if doc_bytes is None:
        raise StoreError("corrupt", "no crypto.json at %s" % rev[:10])
    doc = crypto.loads_doc(doc_bytes)
    return _lines(rel, data, ring.key_for(doc, when))


def show(home, rel, rev, ask):
    home.check()
    if rev is None:
        key = KeyRing(ask).key_for(home.crypto_doc(), "present")
        path = home.data / rel
        if not path.is_file():
            raise StoreError("not_found", "%s does not exist" % rel)
        return _lines(rel, path.read_bytes(), key)
    lines = _version(home, KeyRing(ask), rev, rel, rev[:10])
    if lines is None:
        raise StoreError("not_found", "%s does not exist at %s" % (rel, rev))
    return lines


def history(home, rel, limit, ask):
    """[(hash, date, subject, diff lines)] for the newest `limit` commits touching rel."""
    home.check()
    _kind(rel)
    ring = KeyRing(ask)
    out = []
    for commit, date, subject in gitrepo.log(home.data, rel, limit):
        after = _version(home, ring, commit, rel, date) or []
        before = _version(home, ring, commit + "^", rel, "before " + date) or []
        diff = list(difflib.unified_diff(before, after, "before", "after", n=0, lineterm=""))[2:]
        out.append((commit, date, subject, diff))
    return out


def textconv(home, path, password):
    """git diff driver: print a file version decrypted with the current key.

    git passes either a working-tree path or a temp file named XXXXXX_<basename>; the real
    path (needed to verify each row) is found among the working tree's data files.
    """
    data = Path(path).read_bytes()
    if not password:
        return ["[account-register: set REGISTER_PASSWORD to see decrypted rows]"] + \
            data.decode("ascii", "replace").splitlines()
    key = crypto.unlock(password, home.crypto_doc())
    base = Path(path).name
    candidates = [str(p.relative_to(home.data)) for p in home.data.rglob("*")
                  if p.is_file() and ".git" not in p.parts and (p.name == base or base.endswith("_" + p.name))]
    for rel in candidates:
        try:
            return _lines(rel, data, key)
        except StoreError as e:
            if e.code == "wrong_key":
                return ["[account-register: encrypted under an older password]"]
    return ["[account-register: cannot decrypt this version with the current password]"]
