"""Recurring transactions: stored templates and their schedule.

A template is a row to enter again and again: an account, the row's text and amount, how often,
the date of the first one, and how many have been entered or skipped so far. The next date is
always worked out from the first date and that count, so "the 31st, monthly" gives Jan 31,
Feb 28, Mar 31 without drifting. Nothing here writes: the ledger enters rows only when asked.
"""
import calendar
import datetime

from . import formats

# how often -> (unit, step)
EVERY = {"week": ("days", 7), "2weeks": ("days", 14), "month": ("months", 1), "quarter": ("months", 3),
         "year": ("months", 12)}
KEYS = ("id", "account", "description", "category", "amount", "note", "every", "start", "count")
MAX_TEMPLATES = 200
MAX_DUE = 60  # most occurrences of one template entered at once (five years of monthly)


def occurrence(template, k):
    """The date of occurrence number k (0 is the start date)."""
    start = datetime.date.fromisoformat(template["start"])
    unit, step = EVERY[template["every"]]
    if unit == "days":
        return start + datetime.timedelta(days=step * k)
    months = start.year * 12 + start.month - 1 + step * k
    year, month = divmod(months, 12)
    return datetime.date(year, month + 1, min(start.day, calendar.monthrange(year, month + 1)[1]))


def next_date(template):
    """The next date to enter, or None once it would be past the last year the register holds."""
    try:
        d = occurrence(template, template["count"])
    except (OverflowError, ValueError):
        return None
    return d if d.year <= formats.MAX_YEAR else None


def due(template, today):
    """The dates up to today that have not been entered or skipped, oldest first (at most MAX_DUE)."""
    dates, t = [], dict(template)
    while len(dates) < MAX_DUE:
        d = next_date(t)
        if d is None or d > today:
            break
        dates.append(d)
        t["count"] += 1
    return dates


def validate(t):
    """A stored template in canonical form, or FieldError."""
    if not isinstance(t, dict) or tuple(t) != KEYS:
        raise formats.FieldError("recurring", "must have exactly the fields %s, in order" % ", ".join(KEYS))
    if not isinstance(t["id"], str) or not formats.LINK_ID.match(t["id"]):
        raise formats.FieldError("id", "invalid id")
    if not isinstance(t["account"], str):
        raise formats.FieldError("account", "must be text")
    out = dict(t)
    for f in ("description", "category", "note"):
        out[f] = formats.check_text(t[f], f, formats.TEXT_LIMITS[f])
    if not out["description"].strip():
        raise formats.FieldError("description", "is required")
    if not formats.check_amount(t["amount"]):
        raise formats.FieldError("amount", "is required")
    if t["every"] not in EVERY:
        raise formats.FieldError("every", "must be one of %s" % ", ".join(EVERY))
    if not formats.check_date(t["start"], "start"):
        raise formats.FieldError("start", "is required")
    if not isinstance(t["count"], int) or isinstance(t["count"], bool) or not 0 <= t["count"] <= 100000:
        raise formats.FieldError("count", "must be a whole number")
    if out != t:
        raise formats.FieldError("recurring", "is not in canonical form")
    return out
