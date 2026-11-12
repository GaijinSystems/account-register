"""Money amounts as Decimal, with one canonical text form for storage.

Canonical form: optional '-', integer part without leading zeros, '.', two
decimals ("-84.12", "0.50", "30886.14"). An empty string means "blank".
"""
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN

CENT = Decimal("0.01")
MAX_ABS = Decimal("999999999999.99")
_CANONICAL = re.compile(r"^-?(0|[1-9][0-9]{0,11})\.[0-9]{2}\Z")  # \Z: "$" would let a final newline through


def is_canonical(text):
    return text == "" or (bool(_CANONICAL.match(text)) and text != "-0.00")


def to_decimal(text):
    """Canonical text -> Decimal ("" -> 0). Raises ValueError otherwise."""
    if not isinstance(text, str) or not is_canonical(text):
        raise ValueError("not a canonical amount: %r" % (text,))
    return Decimal(text) if text else Decimal(0)


def fmt(value):
    """Decimal -> canonical text. Values with more than two decimals are rejected."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("not a finite Decimal: %r" % (value,))
    try:
        q = value.quantize(CENT, rounding=ROUND_HALF_EVEN)
    except InvalidOperation:
        raise ValueError("amount too large: %s" % value) from None
    if q != value:
        raise ValueError("more than two decimal places: %s" % value)
    if abs(q) > MAX_ABS:
        raise ValueError("amount too large: %s" % value)
    if q == 0:
        q = abs(q)  # never "-0.00"
    return format(q, "f")


def round_cents(value):
    """Round a Decimal to cents (used for spreadsheet-computed balances).
    Raises ValueError for values that are not finite or too large to round."""
    if not value.is_finite():
        raise ValueError("not a finite number: %s" % value)
    try:
        return value.quantize(CENT, rounding=ROUND_HALF_EVEN)
    except InvalidOperation:
        raise ValueError("number too large: %s" % value) from None


def fmt_balance(value):
    """Decimal balance -> text with two decimals, like fmt but with no upper limit, so that a
    stored sheet whose balance grew too large still displays (and can be fixed)."""
    q = round_cents(value)
    if q == 0:
        q = abs(q)  # never "-0.00"
    return format(q, "f")


SYMBOLS = "$€£¥₹"
_SPACES = str.maketrans({"\u00a0": " ", "\u202f": " ", "\u2009": " ", "\u2212": "-"})
_BODIES = {}


def _body(decimal):
    """The number pattern for a decimal mark: digits, optionally in thousands groups separated by
    the other mark, a space or an apostrophe (one kind per number), then optional decimals."""
    if decimal not in _BODIES:
        if decimal not in (".", ","):
            raise ValueError("decimal mark must be . or ,")
        sep = "[%s ']" % ("," if decimal == "." else ".")
        d = re.escape(decimal)
        _BODIES[decimal] = re.compile(r"(?P<int>\d{1,3}(?P<sep>%s)\d{3}(?:(?P=sep)\d{3})*|\d+)?"
                                      r"(?:%s(?P<frac>\d+))?" % (sep, d))
    return _BODIES[decimal]


def parse_loose(text, decimal="."):
    """Parse a human/spreadsheet amount: '$1,234.56', '-$83.10', '(84.12)', '12', '84.12-',
    and with decimal=",": '1.234,56', '1 234,56 €'. Allowed around the number: parentheses for
    negative, one sign (+ or -, before or after) and one currency symbol (before or after).

    Returns a Decimal, or None for a blank. Raises ValueError if unparseable.
    """
    s = text.translate(_SPACES).strip()
    if s == "":
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1].strip()
    sign = symbol = False
    while s:
        if s[0] in "+-" and not sign:
            sign, neg, s = True, neg ^ (s[0] == "-"), s[1:].strip()
        elif s[-1] in "+-" and not sign:
            sign, neg, s = True, neg ^ (s[-1] == "-"), s[:-1].strip()
        elif s[0] in SYMBOLS and not symbol:
            symbol, s = True, s[1:].strip()
        elif s[-1] in SYMBOLS and not symbol:
            symbol, s = True, s[:-1].strip()
        else:
            break
    m = _body(decimal).fullmatch(s)
    if not s or not m or not (m.group("int") or m.group("frac")):
        raise ValueError("not an amount: %r" % text)
    whole = (m.group("int") or "0").replace(m.group("sep") or " ", "")
    d = Decimal(whole + "." + m.group("frac") if m.group("frac") else whole)
    return -d if neg else d
