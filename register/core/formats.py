"""The sheet formats and the validation rules for every stored field.

Every account type stores the same row fields (data format 4); the types differ only in
whether they have one sheet per year."""
import datetime
import re
import unicodedata

from . import money

# Unicode categories never allowed in text: control, format (zero-width,
# bidi overrides), private use, surrogates, unassigned.
_FORBIDDEN_CATEGORIES = {"Cc", "Cf", "Co", "Cs", "Cn"}
_ISO_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
MIN_YEAR, MAX_YEAR = 1900, 2199
TEXT_LIMITS = {"num": 20, "description": 200, "category": 60, "note": 500, "bank_id": 255}
# The stored fields of every row, in order.
#   transfer: blank, or the id shared by the two rows of a transfer between accounts
#   bank_id:  blank, or the bank's own transaction id (set by bank-file imports)
FIELDS = ("num", "date", "description", "category", "amount", "cleared", "note", "transfer", "bank_id")
LINK_FIELDS = ("transfer", "bank_id")  # set by the app, never typed into the grid
LINK_ID = re.compile(r"^[0-9a-f]{16}\Z")  # transfer ids and recurring-template ids
TRANSFER_CATEGORY = "Transfer"           # given to transfers; reports leave this category out


class FieldError(ValueError):
    def __init__(self, field, message, code="invalid"):
        super().__init__("%s: %s" % (field, message))
        self.field, self.message, self.code = field, message, code


class Format:
    def __init__(self, name, label, yearly, owed_positive=False, debit_mode=False):
        self.name, self.label, self.fields, self.yearly = name, label, FIELDS, yearly
        # Display defaults only (stored amounts mean the same for every type: negative is money
        # spent or owed). owed_positive: shown with the sign flipped unless the account's own
        # setting says otherwise. debit_mode: the grid starts with plain numbers as money out.
        self.owed_positive, self.debit_mode = owed_positive, debit_mode

    def blank_row(self):
        return {f: (False if f == "cleared" else "") for f in self.fields}

    def opening_row(self, amount):
        """The first row of a new account or imported sheet."""
        row = self.blank_row()
        row.update(description="Opening balance", amount=amount)
        return row


FORMATS = {
    "checking": Format("checking", "Checking", yearly=True, debit_mode=True),
    "savings": Format("savings", "Savings", yearly=False),
    "card": Format("card", "Credit card", yearly=True, owed_positive=True, debit_mode=True),
    "cash": Format("cash", "Cash", yearly=False),
    "loan": Format("loan", "Loan", yearly=False, owed_positive=True),
}


def strip_forbidden(text):
    """Text without the characters check_text refuses, NFC-normalized (for imported text)."""
    kept = "".join(ch for ch in unicodedata.normalize("NFC", text)
                   if unicodedata.category(ch) not in _FORBIDDEN_CATEGORIES)
    return unicodedata.normalize("NFC", kept)  # removing a character can bring two together


def check_text(value, field, limit):
    """Validate a text field and return it NFC-normalized."""
    if not isinstance(value, str):
        raise FieldError(field, "must be text")
    value = unicodedata.normalize("NFC", value)
    for ch in value:
        if unicodedata.category(ch) in _FORBIDDEN_CATEGORIES:
            raise FieldError(field, "contains a control or invisible character (U+%04X)" % ord(ch))
    if len(value) > limit:
        raise FieldError(field, "longer than %d characters" % limit)
    return value


def check_date(value, field="date"):
    if not isinstance(value, str):
        raise FieldError(field, "must be text")
    if value == "":
        return value
    if not _ISO_DATE.match(value):
        raise FieldError(field, "must be YYYY-MM-DD")
    try:
        d = datetime.date.fromisoformat(value)
    except ValueError:
        raise FieldError(field, "not a real date") from None
    if not MIN_YEAR <= d.year <= MAX_YEAR:
        raise FieldError(field, "year %d is out of range" % d.year)
    return value


def check_amount(value, field="amount"):
    if not isinstance(value, str) or not money.is_canonical(value):
        raise FieldError(field, "must be an amount like -84.12 or 1234.00 (or blank)")
    return value


def validate_field(fmt, field, value):
    """Return the canonical value of one field, or raise FieldError."""
    if field == "balance":
        raise FieldError(field, "the balance is computed and cannot be written", code="balance_readonly")
    if field not in fmt.fields:
        raise FieldError(field, "unknown field for a %s sheet" % fmt.name, code="unknown_field")
    if field == "date":
        return check_date(value)
    if field == "amount":
        return check_amount(value)
    if field == "cleared":
        if not isinstance(value, bool):
            raise FieldError(field, "must be true or false")
        return value
    if field == "transfer":
        if not isinstance(value, str) or (value and not LINK_ID.match(value)):
            raise FieldError(field, "must be blank or a 16-character transfer id")
        return value
    return check_text(value, field, TEXT_LIMITS[field])


def validate_partial(fmt, fields):
    """Validate a dict of some fields (for edits). Returns canonical values."""
    if not isinstance(fields, dict):
        raise FieldError("fields", "must be an object")
    return {k: validate_field(fmt, k, v) for k, v in fields.items()}


def validate_row(fmt, row):
    """Validate a complete stored row: exactly the format's fields, in order."""
    if not isinstance(row, dict):
        raise FieldError("row", "must be an object")
    if "balance" in row:
        validate_field(fmt, "balance", row["balance"])
    if set(row) != set(fmt.fields):
        raise FieldError("row", "must have exactly the fields %s" % ", ".join(fmt.fields))
    out = {f: validate_field(fmt, f, row[f]) for f in fmt.fields}
    if out != row:
        raise FieldError("row", "is not in canonical form")
    return out
