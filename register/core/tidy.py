"""Merging spellings of the same name (`register.pyz tidy-names`).

The same payee or category ends up in the register as "DD - Pizza Hut", "dd - pizza hut" and
"DD -- pizza hut". This module finds such groups and rewrites the chosen rows; the command in
__main__.py holds the conversation. Nothing here asks anything or decides anything: every
merge is chosen by the person running the command.

Exact groups are names whose keys (key()) are equal. Candidate groups, only on request, are
looser: CANDIDATE_KINDS. Transfer rows and opening-balance rows are never read or changed.
"""
import dataclasses
import difflib
import re
import unicodedata
from dataclasses import dataclass, field

from . import formats
from .importer import _same_text
from .ledger import OPENING
from .store import RECURRING, RECURRING_FILE

FIELDS = ("description", "category")
CANDIDATE_KINDS = ("numbers", "order", "contains", "typos")  # the order they are offered in
DEFAULT_THRESHOLD = 0.88
MIN_THRESHOLD, MAX_THRESHOLD = 0.70, 0.99
TYPO_MIN_LENGTH = 5  # shorter keys are never matched by "typos": one letter is too large a share
TYPO_WORD_RATIO = 0.6  # at least this share of every word's letters must line up with the other name

_NOT_LETTER_OR_DIGIT = re.compile(r"[\W_]+")


def key(text):
    """What two spellings of one name have in common: case, spacing and punctuation ignored.
    "DD - Pizza Hut", "dd - pizza hut" and "DD -- pizza hut" all give "dd pizza hut"."""
    text = unicodedata.normalize("NFKC", text).casefold()
    return _NOT_LETTER_OR_DIGIT.sub(" ", text).strip()


def eligible(row):
    """Rows whose names may be merged: not one half of a transfer (its description is written by
    the app and names the other account), and not a sheet's opening balance."""
    return not row["transfer"] and row["description"].strip() != OPENING


@dataclass
class Name:
    """One spelling as it is used: how many rows, between which dates, in which accounts."""
    text: str
    rows: int = 0
    first: str = ""
    last: str = ""
    accounts: set = field(default_factory=set)

    def add(self, row, account_id):
        self.rows += 1
        self.accounts.add(account_id)
        if row["date"]:
            self.first = min(self.first, row["date"]) if self.first else row["date"]
            self.last = max(self.last, row["date"])

    def merge(self, other):
        self.rows += other.rows
        self.accounts |= other.accounts
        dates = [d for d in (self.first, other.first) if d]
        self.first = min(dates) if dates else ""
        self.last = max(self.last, other.last)


@dataclass
class Group:
    names: list           # Name, most rows first
    kind: str = "exact"   # or one of CANDIDATE_KINDS

    @property
    def rows(self):
        return sum(n.rows for n in self.names)

    @property
    def accounts(self):
        return set().union(*(n.accounts for n in self.names))


# ---------------------------------------------------------------- reading

def read_names(store, name_field):
    """{spelling: Name} for every non-blank value of name_field in the eligible rows of every
    sheet. Spellings whose key is empty (punctuation only) are left out: they match nothing."""
    names = {}
    with store.cached():
        for account, _, _, row in store.rows(store.accounts()):
            text = row[name_field]
            if text and eligible(row) and key(text):
                names.setdefault(text, Name(text)).add(row, account["id"])
    return names


# ---------------------------------------------------------------- grouping

def count(n, word):
    """ "1 row", "2 rows"."""
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def _ordered(names):
    return sorted(names, key=lambda n: (-n.rows, n.text))


def _largest_first(groups):
    return sorted(groups, key=lambda g: (-g.rows, g.names[0].text))


def _buckets(items, keyfunc):
    """Lists of the items whose keyfunc values are equal (and not empty), two or more."""
    buckets = {}
    for item in items:
        k = keyfunc(item)
        if k:
            buckets.setdefault(k, []).append(item)
    return [b for b in buckets.values() if len(b) > 1]


def exact_groups(names):
    """Spellings with the same key."""
    return _largest_first(Group(_ordered(b)) for b in _buckets(names.values(), lambda n: key(n.text)))


def _without_numbers(k):
    return " ".join(w for w in k.split() if not w.isdigit())


def _word_order(k):
    return " ".join(sorted(k.split()))


def _contains(keys):
    """Each group: a key, and the longer keys that begin with all its words."""
    words = {k: k.split() for k in keys}
    starting = {}  # the first words of a longer key -> those keys, in keys order
    for k in keys:
        for m in range(1, len(words[k])):
            starting.setdefault(tuple(words[k][:m]), []).append(k)
    taken, groups = set(), []
    for root in sorted(keys, key=lambda k: (len(words[k]), k)):
        if root in taken:
            continue
        longer = [k for k in starting.get(tuple(words[root]), ()) if k not in taken]
        if longer:
            groups.append([root] + longer)
            taken.update(groups[-1])
    return groups


def _misspelt(a, b, blocks):
    """Whether keys that are close overall differ only by misspellings within words: most letters
    of every word, on both sides, are part of the alignment difflib made (blocks, from
    get_matching_blocks). "piza hut" / "pizza hut" and "wal mart" / "walmart" are; "mo income
    tax" / "us income tax" (a word replaced) and "walgreens pharmacy" / "walgreens us pharmacy"
    (a word added) are not."""
    kept_a, kept_b = [False] * len(a), [False] * len(b)
    for i, j, n in blocks:
        kept_a[i:i + n] = [True] * n
        kept_b[j:j + n] = [True] * n

    def every_word_kept(text, kept):
        return all(sum(kept[m.start():m.end()]) >= TYPO_WORD_RATIO * len(m.group())
                   for m in re.finditer(r"\S+", text))
    return every_word_kept(a, kept_a) and every_word_kept(b, kept_b)


def _typos(keys, threshold):
    """Groups of keys that are nearly equal letter for letter (difflib's ratio at least
    threshold) and differ only within words (_misspelt), joined through each other: membership
    is chosen by hand afterwards."""
    keys = sorted((k for k in keys if len(k) >= TYPO_MIN_LENGTH), key=len)
    parent = {k: k for k in keys}

    def root(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    # quick_ratio is symmetric, and SequenceMatcher keeps its analysis of seq2: the sieve holds
    # a as seq2 for the whole inner loop. ratio() is not symmetric, so it runs the original way.
    sieve = difflib.SequenceMatcher(None, autojunk=False)
    for i, a in enumerate(keys):
        sieve.set_seq2(a)
        matcher = difflib.SequenceMatcher(None, a, autojunk=False)
        for b in keys[i + 1:]:
            if 2 * len(a) < threshold * (len(a) + len(b)):
                break  # sorted by length: the ratio cannot reach the threshold from here on
            sieve.set_seq1(b)
            if sieve.quick_ratio() >= threshold:
                matcher.set_seq2(b)
                if matcher.ratio() >= threshold and _misspelt(a, b, matcher.get_matching_blocks()):
                    parent[root(b)] = root(a)
    components = {}
    for k in keys:
        components.setdefault(root(k), []).append(k)
    return [c for c in components.values() if len(c) > 1]


def candidate_groups(names, kind, threshold=DEFAULT_THRESHOLD, offered=None):
    """Looser groups of one kind, from the names as they are now (after the merges chosen so
    far). offered: a set of frozenset({key, key}) pairs already put to the user; it is
    extended, and a group whose every pair was offered before is not offered again."""
    if kind not in CANDIDATE_KINDS:
        raise ValueError("unknown kind: %s" % kind)
    offered = set() if offered is None else offered
    by_key = {}
    for n in names.values():
        by_key.setdefault(key(n.text), []).append(n)
    if kind == "numbers":
        key_groups = _buckets(by_key, _without_numbers)
    elif kind == "order":
        key_groups = _buckets(by_key, _word_order)
    elif kind == "contains":
        key_groups = _contains(list(by_key))
    else:
        key_groups = _typos(list(by_key), threshold)
    groups = []
    for keys in key_groups:
        pairs = {frozenset((a, b)) for i, a in enumerate(keys) for b in keys[i + 1:]}
        if pairs <= offered:
            continue
        offered |= pairs
        groups.append(Group(_ordered([n for k in keys for n in by_key[k]]), kind))
    return _largest_first(groups)


def candidate_rounds(names, kinds, threshold=DEFAULT_THRESHOLD):
    """The candidate groups of each kind in turn, each built when it is reached (so from the
    names as they are after the answers to the rounds before), no pair offered twice."""
    offered = set()
    for kind in kinds:
        yield candidate_groups(names, kind, threshold, offered)


def search(names, text, threshold=None):
    """Names for a hand-made merge: those whose key contains the key of text, most rows first;
    then, with a threshold (when typos are matched), those nearly equal to it, best first."""
    wanted = key(text)
    if not wanted:
        return []
    keyed = [(key(n.text), n) for n in names.values()]
    found = _ordered([n for k, n in keyed if wanted in k])
    if threshold is not None:
        scored = []
        for k, n in keyed:
            if wanted not in k:
                ratio = difflib.SequenceMatcher(None, wanted, k, autojunk=False).ratio()
                if ratio >= threshold:
                    scored.append((-ratio, -n.rows, n.text, n))
        found += [n for *_, n in sorted(scored)]
    return found


# ---------------------------------------------------------------- choosing

def check_spelling(name_field, text):
    """A spelling typed by hand, validated as the page validates the field, or FieldError."""
    text = formats.check_text(text.strip(), name_field, formats.TEXT_LIMITS[name_field])
    if not key(text):
        raise formats.FieldError(name_field, "needs at least one letter or digit")
    if name_field == "description" and text == OPENING:
        raise formats.FieldError(name_field, "is the text of opening balances, which are left out")
    return text


def is_transfer_category(text):
    """Reports leave out rows with this category (whatever the case and spacing)."""
    return text.strip().casefold() == formats.TRANSFER_CATEGORY.casefold()


def import_still_matches(old, new):
    """Whether an appending import (importer.run_append) still finds a row renamed from old to
    new when the bank file has no transaction ids: it compares descriptions ignoring case and
    runs of spaces."""
    return _same_text(old) == _same_text(new)


class Plan:
    """The merges chosen so far: into is {spelling in the register: the spelling it becomes},
    and names the names as they would be after them (for building the next groups)."""

    def __init__(self, names):
        self.names = {t: dataclasses.replace(n, accounts=set(n.accounts)) for t, n in names.items()}
        self.stored = set(names)  # the spellings in the register: the only ones that can change
        self.into = {}

    def merge(self, spellings, keep):
        """Every spelling in spellings becomes keep (which may be new). ValueError if keep is a
        spelling this plan already merges into another."""
        if keep in self.into:
            raise ValueError("%s is already being merged into %s" % (keep, self.into[keep]))
        target = self.names.setdefault(keep, Name(keep))
        for s in spellings:
            if s == keep:
                continue
            target.merge(self.names.pop(s))
            for old, new in self.into.items():
                if new == s:
                    self.into[old] = keep
            if s in self.stored:  # a spelling typed earlier in this run is in no row
                self.into[s] = keep

    def __bool__(self):
        return bool(self.into)


# ---------------------------------------------------------------- writing

@dataclass
class Result:
    names: int        # spellings that changed
    rows: int         # rows that changed
    sheets: int       # sheet files that changed
    templates: int    # recurring templates that changed

    @property
    def message(self):
        return "Tidy names: %s merged, %s in %s" % (
            count(self.names, "name"), count(self.rows, "row"), count(self.sheets, "sheet"))


def changes(store, name_field, into):
    """({relpath: (kind, content)}, Result) for the replacements into: every eligible row, and
    every recurring template, whose name_field is a key of into gets the value. Nothing is
    written; the caller passes the files to store.write_files with result.message."""
    files, rows_changed, sheets = {}, 0, 0
    with store.cached():
        for account in store.accounts():
            fmt = formats.FORMATS[account["type"]]
            for year in store.sheets(account):
                rows, _ = store.read_sheet(account, year)
                n = 0
                for r in rows:
                    if eligible(r) and r[name_field] in into:
                        r[name_field] = into[r[name_field]]
                        n += 1
                if n:
                    files[store.relpath(account, year)] = (fmt, rows)
                    rows_changed += n
                    sheets += 1
    templates, t_changed = store.recurring(), 0
    for t in templates:
        if t[name_field] in into:
            t[name_field] = into[t[name_field]]
            t_changed += 1
    if t_changed:
        files[RECURRING_FILE] = (RECURRING, templates)
    return files, Result(len(into), rows_changed, sheets, t_changed)
