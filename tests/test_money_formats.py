import unittest
from decimal import Decimal

from register.core import formats, money

CHECKING = formats.FORMATS["checking"]


class MoneyTest(unittest.TestCase):
    def test_canonical(self):
        for ok in ("", "0.00", "-84.12", "30886.14", "999999999999.99"):
            self.assertTrue(money.is_canonical(ok), ok)
        for bad in ("-0.00", "1,234.00", "$1.00", "1.5", "01.00", "1.000", " 1.00", "(1.00)", "1e3"):
            self.assertFalse(money.is_canonical(bad), bad)

    def test_fmt(self):
        self.assertEqual(money.fmt(Decimal("5")), "5.00")
        self.assertEqual(money.fmt(Decimal("-84.1")), "-84.10")
        self.assertEqual(money.fmt(Decimal("-0")), "0.00")
        with self.assertRaises(ValueError):
            money.fmt(Decimal("1.005"))
        for huge in (Decimal("1e999999"), Decimal("1" * 40)):  # too many digits to quantize
            with self.assertRaises(ValueError):
                money.fmt(huge)
            with self.assertRaises(ValueError):
                money.round_cents(huge)
        for odd in ("Infinity", "-Infinity", "NaN", "sNaN"):
            with self.assertRaises(ValueError):
                money.round_cents(Decimal(odd))

    def test_parse_loose(self):
        cases = {"$1,234.56": "1234.56", "-$83,373.29": "-83373.29", "(84.12)": "-84.12",
                 "$-12": "-12", "12": "12", " 7.5 ": "7.5", ".5": "0.5"}
        for text, want in cases.items():
            self.assertEqual(money.parse_loose(text), Decimal(want), text)
        self.assertIsNone(money.parse_loose("  "))
        for bad in ("abc", "1,23.00", "$", "1.2.3", "12,3456"):
            with self.assertRaises(ValueError, msg=bad):
                money.parse_loose(bad)


class FormatsTest(unittest.TestCase):
    def test_balance_is_never_a_field(self):
        for fmt in formats.FORMATS.values():
            self.assertNotIn("balance", fmt.fields)
            with self.assertRaises(formats.FieldError) as cm:
                formats.validate_partial(fmt, {"balance": "1.00"})
            self.assertEqual(cm.exception.code, "balance_readonly")

    def test_text_rules(self):
        ok = 'Smith, John "JJ" `tick` \\ é =SUM(A1) -500 +5 @x'
        self.assertEqual(formats.check_text(ok, "note", 500), ok)
        for bad in ("a\nb", "a\tb", "a\x00b", "a\u202eb", "a\u200bb", "a\ue000b"):
            with self.assertRaises(formats.FieldError, msg=repr(bad)):
                formats.check_text(bad, "note", 500)
        self.assertEqual(formats.check_text("e\u0301", "note", 500), "\u00e9")  # NFC
        with self.assertRaises(formats.FieldError):
            formats.check_text("x" * 21, "num", 20)

    def test_dates(self):
        self.assertEqual(formats.check_date("2026-01-03"), "2026-01-03")
        self.assertEqual(formats.check_date(""), "")
        for bad in ("2026-02-30", "01/03/2026", "0225-01-02", "2026-1-3"):
            with self.assertRaises(formats.FieldError, msg=bad):
                formats.check_date(bad)

    def test_a_trailing_newline_is_not_canonical(self):
        from register.core import crypto, store
        self.assertFalse(money.is_canonical("5.00\n"))
        self.assertFalse(money.is_canonical("\u0665.00"))      # digits of another script
        for bad in ({"amount": "5.00\n"}, {"transfer": "0123456789abcdef\n"}, {"date": "2025-01-01\n"}):
            with self.assertRaises(formats.FieldError, msg=bad):
                formats.validate_partial(CHECKING, bad)
        for pattern, text in ((store.ACCOUNT_ID, "yy\n"), (store.YEAR_FILE, "2025.jsonl\n"),
                              (crypto._KEY_ID, "0123456789abcdef\n"), (formats.LINK_ID, "0123456789abcdef\n")):
            self.assertIsNone(pattern.match(text))

    def test_fields(self):
        self.assertEqual(formats.validate_partial(CHECKING, {"cleared": True, "amount": "-1.00"}),
                         {"cleared": True, "amount": "-1.00"})
        for bad in ({"cleared": "x"}, {"cleared": 1}, {"amount": -1}, {"amount": "1"}, {"bogus": ""}):
            with self.assertRaises(formats.FieldError, msg=bad):
                formats.validate_partial(CHECKING, bad)
        # every account type has the same row fields
        self.assertEqual(formats.FORMATS["savings"].fields, CHECKING.fields)
        self.assertEqual(CHECKING.fields, ("num", "date", "description", "category", "amount", "cleared", "note",
                                           "transfer", "bank_id"))
        self.assertEqual(formats.validate_partial(CHECKING, {"category": "Food", "transfer": "0123456789abcdef",
                                                             "bank_id": "FIT-1"}),
                         {"category": "Food", "transfer": "0123456789abcdef", "bank_id": "FIT-1"})
        for bad in ({"transfer": "xyz"}, {"transfer": "0123456789ABCDEF"}, {"transfer": 5},
                    {"category": "x" * 61}, {"bank_id": "x" * 256}, {"category": "a\u200bb"}):
            with self.assertRaises(formats.FieldError, msg=bad):
                formats.validate_partial(CHECKING, bad)


if __name__ == "__main__":
    unittest.main()
