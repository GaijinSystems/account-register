"""Bank downloads (OFX/QFX, QIF), and appending with duplicate detection."""
import tempfile
import unittest
from pathlib import Path

from helpers import git_log, make_home, run_import_cli
from register.core import bankfile, formats, importer
from register.core.ledger import Ledger
from register.core.store import locked, new_account

OFX_SGML = """OFXHEADER:100
DATA:OFXSGML
VERSION:102
CHARSET:1252

<OFX>
<BANKMSGSRSV1><STMTTRNRS><STMTRS>
<CURDEF>USD
<BANKACCTFROM><BANKID>1234<ACCTID>000111222<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST>
<DTSTART>20250101<DTEND>20250131
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20250105120000[-5:EST]<TRNAMT>-4.50<FITID>A1
<NAME>Coffee &amp; Co<MEMO>Card 1234</STMTTRN>
<STMTTRN><TRNTYPE>CHECK<DTPOSTED>20250103<TRNAMT>-1,000.00<FITID>A2<CHECKNUM>1066
<NAME>Check<MEMO>CHECK</STMTTRN>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20250115<TRNAMT>+2000<FITID>A3<MEMO>Payroll  ACME\tInc</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL><BALAMT>995.50<DTASOF>20250131</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
"""

OFX_XML = """<?xml version="1.0" encoding="UTF-8"?>
<?OFX OFXHEADER="200" VERSION="211"?>
<OFX><CREDITCARDMSGSRSV1><CCSTMTTRNRS><CCSTMTRS><CURDEF>EUR</CURDEF>
<CCACCTFROM><ACCTID>4111000000009876</ACCTID></CCACCTFROM>
<BANKTRANLIST>
<STMTTRN><TRNTYPE>DEBIT</TRNTYPE><DTPOSTED>20260209</DTPOSTED><TRNAMT>-38,15</TRNAMT><FITID>X-1</FITID>
  <PAYEE><NAME>Hardware store</NAME><CITY>Lyon</CITY></PAYEE></STMTTRN>
<STMTTRN><TRNTYPE>CREDIT</TRNTYPE><DTPOSTED>20260128</DTPOSTED><TRNAMT>212,40</TRNAMT><FITID>X-2</FITID>
  <NAME>Payment</NAME></STMTTRN>
</BANKTRANLIST></CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1>
<BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKACCTFROM><ACCTID>55550001</ACCTID><ACCTTYPE>SAVINGS</ACCTTYPE></BANKACCTFROM>
<BANKTRANLIST><STMTTRN><DTPOSTED>20260101</DTPOSTED><TRNAMT>1.00</TRNAMT><FITID>S1</FITID><NAME>Interest</NAME>
</STMTTRN></BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""

QIF = """!Account
NEveryday
TBank
^
!Type:Bank
D1/ 5'25
T-4.50
PCoffee
MCard 1234
LFood:Out/Trip
C*
^
D1/3/2025
U-1,000.00
T-1,000.00
N1066
PLandlord
L[Rainy Day]
SHousing
$-900.00
SUtilities
$-100.00
^
D1/15/2025
T2,000.00
MPayroll
CX
^
!Type:Cat
NFood
^
"""


def write(test, name, text):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    path = Path(tmp.name) / name
    path.write_text(text, encoding="utf-8")
    return path


def brief(rows):
    return [(r["date"], r["amount"], r["description"], r["num"], r["note"], r["bank_id"]) for r in rows]


class ReadTest(unittest.TestCase):
    def test_sniff(self):
        for name, text, want in (("a.qfx", OFX_SGML, "ofx"), ("a.ofx", OFX_XML, "ofx"), ("a.qif", QIF, "qif"),
                                 ("a.csv", "Date,Amount\n1/2/2025,5\n", None), ("a.txt", "", None)):
            self.assertEqual(bankfile.sniff(write(self, name, text)), want, name)

    def test_ofx_sgml(self):
        st = bankfile.read(write(self, "a.qfx", OFX_SGML))
        self.assertEqual((st.kind, st.account_type), ("ofx", "checking"))
        self.assertEqual(brief(st.rows), [
            ("2025-01-05", "-4.50", "Coffee & Co", "", "Card 1234", "A1"),
            ("2025-01-03", "-1000.00", "Check", "1066", "", "A2"),          # a memo that repeats the name is dropped
            ("2025-01-15", "2000.00", "Payroll ACME Inc", "", "", "A3")])   # no name: the memo is the description
        self.assertIn("account …1222: 3 transactions", st.notes[0])
        for row in st.rows:
            formats.validate_row(formats.FORMATS["checking"], row)

    def test_ofx_xml_with_several_accounts_and_decimal_commas(self):
        path = write(self, "a.ofx", OFX_XML)
        with self.assertRaises(bankfile.BankFileError) as cm:
            bankfile.read(path)
        self.assertIn("holds 2 accounts (…9876, …0001); pick one with --sheet LAST4", str(cm.exception))
        st = bankfile.read(path, "9876")
        self.assertEqual(st.account_type, "card")
        self.assertEqual(brief(st.rows), [("2026-02-09", "-38.15", "Hardware store", "", "", "X-1"),
                                          ("2026-01-28", "212.40", "Payment", "", "", "X-2")])
        self.assertEqual(bankfile.read(path, "0001").account_type, "savings")
        with self.assertRaises(bankfile.BankFileError):
            bankfile.read(path, "7777")

    def test_ofx_problems(self):
        for text, want in ((OFX_SGML.replace("20250105120000", "2025"), "cannot read the date"),
                           (OFX_SGML.replace("-4.50", "four"), "cannot read the amount"),
                           ("OFXHEADER:100\n<OFX><INVSTMTRS></INVSTMTRS></OFX>", "investment statement"),
                           ("OFXHEADER:100\n<OFX></OFX>", "no transactions")):
            with self.assertRaises(bankfile.BankFileError) as cm:
                bankfile.read(write(self, "a.ofx", text))
            self.assertIn(want, str(cm.exception))
        long_id = OFX_SGML.replace("<FITID>A1", "<FITID>" + "9" * 300)
        self.assertRegex(bankfile.read(write(self, "a.ofx", long_id)).rows[0]["bank_id"], r"^sha256:[0-9a-f]{64}$")

    def test_qif(self):
        st = bankfile.read(write(self, "a.qif", QIF))
        self.assertEqual((st.kind, st.account_type), ("qif", "checking"))
        self.assertEqual(brief(st.rows), [("2025-01-05", "-4.50", "Coffee", "", "Card 1234", ""),
                                          ("2025-01-03", "-1000.00", "Landlord", "1066", "", ""),  # the total, not the splits
                                          ("2025-01-15", "2000.00", "Payroll", "", "", "")])
        self.assertEqual([(r["category"], r["cleared"]) for r in st.rows],
                         [("Food:Out", True), ("Rainy Day", False), ("", True)])
        self.assertIn("skipped sections that are not transactions: Cat", st.notes)
        self.assertFalse(any("AMBIGUOUS" in n for n in st.notes))  # 1/15 can only be month/day
        unsure = write(self, "b.qif", QIF.replace("D1/15/2025", "D1/12/2025"))
        self.assertTrue(any("AMBIGUOUS" in n for n in bankfile.read(unsure).notes))
        other = bankfile.read(unsure, date_format="%d/%m/%Y")
        self.assertEqual([r["date"] for r in other.rows], ["2025-05-01", "2025-03-01", "2025-12-01"])
        with self.assertRaises(bankfile.BankFileError):
            bankfile.read(write(self, "a.qif", QIF), date_format="%d/%m/%Y")

    def test_hostile_or_cut_off_files_fail_fast(self):
        import time
        for body, why in (("<STMTRS>" * 40000, "opens again before it is closed"),
                          ("<STMTRS><BANKTRANLIST>" + "<STMTTRN>" * 40000, "the file is cut off"),
                          ("<STMTRS>" + "<STMTTRN>" * 40000 + "</STMTRS>", "opens again before it is closed"),
                          ("<STMTRS>" + "<ACCTID>1\n" * 40000, "the file is cut off"),
                          ("<STMTRS><STMTTRN>" + "<![CDATA[" * 40000 + "</STMTTRN>", "the file is cut off")):
            start = time.monotonic()
            with self.assertRaises(bankfile.BankFileError) as cm:
                bankfile.read(write(self, "a.ofx", "OFXHEADER:100\n<OFX>" + body))
            self.assertIn(why, str(cm.exception))
            self.assertLess(time.monotonic() - start, 5)   # was minutes: one pass now
        lost = OFX_SGML.replace("<NAME>Coffee &amp; Co<MEMO>Card 1234</STMTTRN>", "<NAME>Coffee")   # no closing tag
        with self.assertRaises(bankfile.BankFileError) as cm:      # was: the next transaction silently vanished
            bankfile.read(write(self, "a.ofx", lost))
        self.assertIn("<STMTTRN> opens again before it is closed", str(cm.exception))
        many = "!Type:Bank\n" + "D1/2/2025\nT1.00\n^\n" * (bankfile.MAX_ROWS + 1)
        with self.assertRaises(bankfile.BankFileError) as cm:
            bankfile.read(write(self, "a.qif", many))
        self.assertIn("more than", str(cm.exception))

    def test_ofx_xml_forms(self):
        xml = OFX_XML.replace("<NAME>Payment</NAME>", "<NAME><![CDATA[Bob & Co <ltd>]]></NAME>") \
                     .replace("<DTPOSTED>20260128</DTPOSTED>", "<DTPOSTED>\n    20260128\n  </DTPOSTED>") \
                     .replace("<NAME>Hardware store</NAME>", "<NAME>A&not B &amp; C&#233;</NAME>")
        rows = bankfile.read(write(self, "a.ofx", xml), "9876").rows
        self.assertEqual([(r["date"], r["description"]) for r in rows],
                         [("2026-02-09", "A&not B & Cé"), ("2026-01-28", "Bob & Co <ltd>")])

    def test_amounts_are_never_rounded_and_text_is_cleaned(self):
        with self.assertRaises(bankfile.BankFileError) as cm:
            bankfile.read(write(self, "a.ofx", OFX_SGML.replace("-4.50", "-4.505")))
        self.assertIn("transaction 1: cannot read the amount '-4.505'", str(cm.exception))
        odd = OFX_SGML.replace("Coffee &amp; Co", "Cafe\u200d\u0301").replace("<FITID>A1", "<FITID>A\u200d\u03011")
        row = bankfile.read(write(self, "a.ofx", odd)).rows[0]
        formats.validate_row(formats.FORMATS["checking"], row)   # was "not in canonical form"
        self.assertEqual(row["description"], "Caf\u00e9")
        last = bankfile.read(write(self, "a.qif", "!Type:Bank\nD1/20/2025\nT1.00\n^\nD1/21/2025\nT2.00\nPno caret"))
        self.assertEqual([r["amount"] for r in last.rows], ["1.00", "2.00"])

    def test_qif_dates_with_dashes(self):
        st = bankfile.read(write(self, "a.qif", "!Type:Bank\nD01-05-25\nT1.00\n^\nD01-12-25\nT2.00\n^\n"))
        self.assertEqual([r["date"] for r in st.rows], ["2025-01-05", "2025-01-12"])   # not the year 2001
        self.assertTrue(any("AMBIGUOUS" in n and "2025-05-01" in n for n in st.notes))
        iso = bankfile.read(write(self, "a.qif", "!Type:Bank\nD2025-01-05\nT1.00\n^\n"))
        self.assertEqual((iso.rows[0]["date"], [n for n in iso.notes if "AMBIGUOUS" in n]), ("2025-01-05", []))

    def test_text_shown_on_the_terminal_is_cleaned(self):
        esc = "\x1b[2J\x1b[H"
        st = bankfile.read(write(self, "a.ofx", OFX_SGML.replace("<ACCTID>000111222", "<ACCTID>0001" + esc + "22")))
        qif = bankfile.read(write(self, "a.qif", "!Type:Invst" + esc + "fake\nD1/2/2025\n^\n!Type:Bank\nD1/20/2025\nT1.00\n^\n"))
        two = "!Account\nNOne" + esc + "\n^\n!Type:Bank\nD1/20/2025\nT1.00\n^\n!Account\nNTwo\n^\n!Type:Bank\nD1/21/2025\nT2.00\n^\n"
        with self.assertRaises(bankfile.BankFileError) as cm:
            bankfile.read(write(self, "b.qif", two))
        for text in st.notes + qif.notes + [str(cm.exception)]:
            self.assertNotIn("\x1b", text)

    def test_line_endings_and_account_names(self):
        base = "!Type:Bank\nD1/20/2025\nT1.00\n^\nD1/21/2025\nT2.00\n^\n"
        for ending in ("\n", "\r\n", "\r"):                          # the last is a classic Mac file
            path = write(self, "a.qif", "x")
            path.write_bytes(base.replace("\n", ending).encode())
            self.assertEqual(len(bankfile.read(path).rows), 2, repr(ending))
        two = "!Account\nNChecking\n^\n!Type:Bank\nD1/20/2025\nT1.00\n^\n" \
              "!Account\nNJoint Checking\n^\n!Type:Bank\nD1/21/2025\nT2.00\n^\n"
        self.assertEqual(bankfile.read(write(self, "b.qif", two), "Checking").rows[0]["amount"], "1.00")
        self.assertEqual(bankfile.read(write(self, "b.qif", two), "Joint Checking").rows[0]["amount"], "2.00")

    def test_damaged_utf16_and_account_lists(self):
        path = write(self, "a.qif", "x")
        path.write_bytes(QIF.encode("utf-16") + b"\x00")          # an odd byte at the end
        with self.assertRaises(bankfile.BankFileError) as cm:
            bankfile.read(path)
        self.assertIn("not valid", str(cm.exception))
        listed = "!Type:Bank\nD1/20/2025\nT1.00\n^\n!Account\nNOne\nTBank\n^\nNTwo\nTBank\n^\n"
        self.assertEqual(len(bankfile.read(write(self, "c.qif", listed)).rows), 1)   # the list holds no transactions

    def test_utf16_files_and_dates_with_month_names(self):
        path = write(self, "a.ofx", "x")
        path.write_bytes(OFX_SGML.encode("utf-16"))
        self.assertEqual(bankfile.sniff(path), "ofx")
        self.assertEqual(len(bankfile.read(path).rows), 3)
        st = bankfile.read(write(self, "a.qif", "!Type:Bank\nD5 Jan 2025\nT1.00\n^\n"), date_format="%d %b %Y")
        self.assertEqual(st.rows[0]["date"], "2025-01-05")

    def test_qif_dates_and_types(self):
        st = bankfile.read(write(self, "a.qif", "!Type:CCard\nD31.12.2025\nT-1.234,56\nPShop\n^\nD01.01.2026\nT5,00\n^\n"))
        self.assertEqual(st.account_type, "card")
        self.assertEqual([(r["date"], r["amount"]) for r in st.rows], [("2025-12-31", "-1234.56"), ("2026-01-01", "5.00")])
        self.assertFalse(any("AMBIGUOUS" in n for n in st.notes))
        with self.assertRaises(bankfile.BankFileError) as cm:
            bankfile.read(write(self, "a.qif", "!Type:Bank\nDsoon\nT1.00\n^\n"))
        self.assertIn("--date-format", str(cm.exception))
        with self.assertRaises(bankfile.BankFileError):
            bankfile.read(write(self, "a.qif", "!Type:Bank\nD13/01/2025\nT1.00\n^\n"), date_format="%m/%d/%Y")
        two = "!Account\nNOne\n^\n!Type:Bank\nD1/2/2025\nT1.00\n^\n!Account\nNTwo\n^\n!Type:Cash\nD1/3/2025\nT2.00\n^\n"
        with self.assertRaises(bankfile.BankFileError) as cm:
            bankfile.read(write(self, "a.qif", two))
        self.assertIn("holds 2 accounts (One, Two)", str(cm.exception))
        self.assertEqual(bankfile.read(write(self, "a.qif", two), "Two").account_type, "cash")


class AppendTest(unittest.TestCase):
    def setUp(self):
        self.home = make_home(self)
        self.led = Ledger(self.home, self.home.key)
        with locked(self.home):
            self.led.add_account("Everyday", "checking", "1000.00", 2025)
        s = self.led.get_sheet("everyday", 2025)   # entered by hand, before the first download
        self.led.insert_row("everyday", 2025, s["rev"], 1, {"date": "2025-01-03", "amount": "-1000.00",
                                                            "description": "Rent", "num": "1066"})
        self.rows = bankfile.read(write(self, "a.qfx", OFX_SGML)).rows
        self.log = []

    def append(self, rows, account="everyday", **kw):
        return importer.run_append(self.home, self.home.key, account, rows, "a.qfx", out=self.log.append, **kw)

    def sheet(self, year=2025, account="everyday"):
        return self.led.get_sheet(account, year)["rows"]

    def test_first_import_links_the_hand_entered_row(self):
        before = len(git_log(self.home))
        self.append(self.rows)
        rows = self.sheet()
        self.assertEqual([(r["date"], r["description"], r["bank_id"]) for r in rows[1:]], [
            ("2025-01-03", "Rent", "A2"),                 # kept as typed; the bank id is recorded on it
            ("2025-01-05", "Coffee & Co", "A1"), ("2025-01-15", "Payroll ACME Inc", "A3")])  # oldest first
        self.assertEqual(rows[-1]["balance"], "1995.50")
        self.assertEqual((len(git_log(self.home)), git_log(self.home)[0]),
                         (before + 1, "import 2 row(s) into everyday from a file"))
        text = "\n".join(self.log)
        self.assertIn("2 new row(s) to add; 0 already there (skipped)", text)
        self.assertIn("1 row(s) entered by hand match by date and amount", text)

    def test_importing_again_adds_nothing(self):
        self.append(self.rows)
        before = len(git_log(self.home))
        self.append(self.rows + [dict(self.rows[0], bank_id="A9", date="2025-02-01")])
        self.assertEqual(len(self.sheet()), 5)
        self.assertEqual(len(git_log(self.home)), before + 1)
        self.log.clear()
        self.append(self.rows)
        self.assertIn("nothing to import: every row is already there", self.log)
        self.assertEqual(len(git_log(self.home)), before + 1)

    def test_rows_without_a_bank_id_compare_by_date_amount_and_description(self):
        plain = [dict(r, bank_id="") for r in self.rows]
        plain[1] = dict(plain[1], description="  rent ")           # the hand-entered one, differently spaced
        self.append(plain + [plain[0]])                             # the same coffee twice in one file: two rows
        self.assertEqual([r["description"] for r in self.sheet()[2:]], ["Coffee & Co", "Coffee & Co", "Payroll ACME Inc"])
        self.append(plain + [plain[0], plain[0]])                   # now three: one more
        self.assertEqual(len(self.sheet()), 6)

    def test_a_bank_id_used_again_for_another_transaction_is_not_dropped(self):
        self.append(self.rows)
        self.log.clear()
        again = [dict(self.rows[0]),                                                    # the same one: skipped
                 dict(self.rows[0], date="2025-02-05", amount="-9.00", description="Lunch"),   # the id, reused
                 dict(self.rows[0], bank_id="N1", date="2025-02-06", amount="-70.00", description="Groceries"),
                 dict(self.rows[0], bank_id="N1", date="2025-02-07", amount="-1.00", description="Gum")]
        self.append(again)
        self.assertEqual([r["description"] for r in self.sheet()[-3:]], ["Lunch", "Groceries", "Gum"])
        text = "\n".join(self.log)
        self.assertIn("3 new row(s) to add; 1 already there (skipped)", text)
        self.assertIn("WARNING: 2 row(s) carry a bank id that another transaction", text)
        before = len(git_log(self.home))
        self.append(again)                                                              # and again: nothing new
        self.assertEqual(len(git_log(self.home)), before)

    def test_one_existing_row_accounts_for_one_imported_row(self):
        s = self.led.get_sheet("everyday", 2025)
        self.led.insert_row("everyday", 2025, s["rev"], 2, {"date": "2025-01-05", "amount": "-4.50",
                                                            "description": "Coffee"})
        both = [dict(self.rows[0], bank_id="", description="Coffee"),            # matches the typed row by text
                dict(self.rows[0], bank_id="77", description="COFFEE SHOP 2")]   # a second coffee that day
        self.append(both)
        self.assertEqual([r["description"] for r in self.sheet() if r["date"] == "2025-01-05"],
                         ["Coffee", "COFFEE SHOP 2"])

    def test_identical_rows_in_one_file_are_all_kept(self):
        fares = [dict(self.rows[0], bank_id="F1", date="2025-03-01", amount="-2.75", description="Fare")] * 3
        self.append(fares)
        self.assertEqual(sum(r["description"] == "Fare" for r in self.sheet()), 3)
        self.assertIn("3 new row(s) to add; 0 already there (skipped)", "\n".join(self.log))
        before = len(git_log(self.home))
        self.append(fares + fares[:1])                     # all three again, and a fourth
        self.assertEqual(sum(r["description"] == "Fare" for r in self.sheet()), 4)
        self.append(fares)
        self.assertEqual(len(git_log(self.home)), before + 1)

    def test_an_id_match_uses_up_the_existing_row(self):
        self.append(self.rows)
        twin = dict(self.rows[0], bank_id="")              # the same coffee again, without an id: a second one
        self.append([self.rows[0], twin])
        self.assertEqual(sum(r["description"] == "Coffee & Co" for r in self.sheet()), 2)

    def test_the_warning_counts_only_rows_that_are_added(self):
        self.append(self.rows)                                   # A2 is now on the typed rent row
        s = self.led.get_sheet("everyday", 2025)
        self.led.insert_row("everyday", 2025, s["rev"], len(s["rows"]), {"date": "2025-01-20", "amount": "-1.00"})
        self.log.clear()
        self.append([dict(self.rows[0], bank_id="A2", date="2025-01-20", amount="-1.00")])   # A2 again; links
        text = "\n".join(self.log)
        self.assertIn("0 new row(s) to add", text)
        self.assertIn("1 row(s) entered by hand match", text)
        self.assertNotIn("WARNING", text)
        self.assertEqual(git_log(self.home)[0], "record bank ids on 1 row(s) of everyday, from a file")

    def test_a_new_account_is_checked_before_the_preview(self):
        for entry in (new_account("visa", "Vi\u200bsa", "card"), new_account("Bad Id", "Visa", "card")):
            with self.assertRaises(importer.ImportFailed):
                self.append(self.rows, entry, dry_run=True)
        with self.assertRaises(importer.ImportFailed) as cm:        # a gap in a new account's years
            self.append([dict(self.rows[0], date="2023-01-01", bank_id="g1"), dict(self.rows[0], date="2025-01-01", bank_id="g2")],
                        new_account("visa", "Visa", "card"))
        self.assertIn("without a gap", str(cm.exception))
        self.append([dict(self.rows[0], date="%d-01-01" % y, bank_id="n%d" % y) for y in (2023, 2024, 2025)],
                    new_account("visa", "Visa", "card"))
        self.assertEqual(self.led.store.years(self.led.store.account("visa")), [2023, 2024, 2025])
        self.assertEqual(self.sheet(2023, "visa")[0]["description"], "Opening balance")
        self.log.clear()
        self.append([])
        self.assertIn("nothing to import: the file has no transactions", self.log)

    def test_an_empty_file_creates_no_account(self):
        with self.assertRaises(importer.ImportFailed) as cm:
            self.append([], new_account("visa", "Visa", "card"))
        self.assertIn("was not created", str(cm.exception))

    def test_rows_outside_the_accounts_years_are_refused(self):
        before = len(git_log(self.home))
        for date, why in (("2023-05-01", "Everyday starts in 2025"), ("2027-05-01", "has no 2027 tab")):
            with self.assertRaises(importer.ImportFailed) as cm:
                self.append([dict(self.rows[0], date=date, bank_id="Y")])
            self.assertIn(why, str(cm.exception))
        self.assertEqual(len(git_log(self.home)), before)
        self.append([dict(self.rows[0], date="2026-01-01", bank_id="Y1"), dict(self.rows[0], date="2027-01-01", bank_id="Y2")])
        self.assertEqual(self.led.store.years(self.led.store.account("everyday")), [2025, 2026, 2027])

    def test_exact_matches_come_first_whatever_the_order(self):
        self.append(self.rows)
        self.log.clear()
        coffee = self.rows[0]
        self.append([dict(coffee, bank_id=""), coffee])      # the id-less twin first: it must not take the A1 row
        self.assertNotIn("WARNING", "\n".join(self.log))
        self.assertEqual(sum(r["description"] == "Coffee & Co" for r in self.sheet()), 2)

    def test_new_year_tab_and_new_account(self):
        self.append([dict(self.rows[0], date="2026-03-01", bank_id="B1")])
        self.assertEqual([r["bank_id"] for r in self.sheet(2026)], ["B1"])
        self.assertIn("  everyday/2026: 1 row(s) (new year tab)", self.log)
        self.log.clear()
        card = bankfile.read(write(self, "c.ofx", OFX_XML), "9876").rows
        self.append(card, new_account("visa", "Visa", "card"))
        rows = self.sheet(2026, "visa")
        self.assertEqual([(r["description"], r["amount"]) for r in rows],
                         [("Opening balance", "0.00"), ("Payment", "212.40"), ("Hardware store", "-38.15")])
        self.assertTrue(self.led.get_sheet("visa", 2026)["owed_positive"])
        self.assertIn("visa (new card account)  <-  a.qfx", self.log)
        with self.assertRaises(importer.ImportFailed):
            self.append(card, new_account("visa", "Visa", "card"))   # it exists now
        with self.assertRaises(importer.ImportFailed) as cm:
            self.append(card, "nope")
        self.assertIn("there is no account 'nope' (existing: everyday, visa)", str(cm.exception))

    def test_refusals_and_preview(self):
        before = len(git_log(self.home))
        for bad in (dict(self.rows[0], date=""), dict(self.rows[0], amount="5"), {"date": "2025-01-01"}):
            with self.assertRaises(importer.ImportFailed):
                self.append([bad])
        with self.assertRaises(importer.ImportFailed):
            self.append([dict(self.rows[0], amount="999999999999.99", bank_id=i) for i in "YZ"])
        preview = self.append(self.rows, dry_run=True)
        self.assertEqual(len(git_log(self.home)), before)
        s = self.led.get_sheet("everyday", 2025)
        self.led.insert_row("everyday", 2025, s["rev"], 2, {"date": "2025-01-05", "amount": "-4.50"})
        with self.assertRaises(importer.ImportFailed) as cm:   # that row now matches the coffee: a different result
            self.append(self.rows, expect=preview)
        self.assertIn("changed since the preview", str(cm.exception))


class BankCliTest(unittest.TestCase):
    def setUp(self):
        self.home = make_home(self)

    def cli(self, *argv, tty=False, answer=None):
        return run_import_cli(self.home, *argv, tty=tty, answer=answer)[:2]

    def rows(self, account, year=None):
        return Ledger(self.home, self.home.key).get_sheet(account, year)["rows"]

    def test_bank_file_into_a_new_account_then_again(self):
        path = write(self, "jan.qfx", OFX_SGML)
        code, out = self.cli(path)
        self.assertIn("say which account the rows go to with --account ID", out)
        code, out = self.cli(path, "--name", "Everyday")
        self.assertIn("there is no terminal to ask on", out)
        self.assertEqual(len(git_log(self.home)), 1)
        code, out = self.cli(path, "--name", "Everyday", tty=True, answer="y")
        self.assertEqual(code, 0, out)
        self.assertIn("everyday (new checking account)  <-  jan.qfx", out)
        self.assertEqual(len(self.rows("everyday", 2025)), 4)
        self.assertEqual(git_log(self.home)[0], "import 3 row(s) into everyday from an OFX file")  # never its name
        code, out = self.cli(path)   # the only account now; nothing new, so no question either
        self.assertEqual(code, 0, out)
        self.assertIn("nothing to import: every row is already there", out)
        self.assertEqual(len(git_log(self.home)), 2)
        for option in (["--replace"], ["--year", "2025"], ["--save-config", "x.json"]):
            code, out = self.cli(path, *option)
            self.assertIn("does not apply to a bank file", out)
        code, out = self.cli(path, "--account", "everyday", "--type", "savings")
        self.assertIn("--name and --type only describe a new account", out)

    def test_qif_date_format_option(self):
        path = write(self, "a.qif", QIF.replace("D1/15/2025", "D1/12/2025"))
        code, out = self.cli(path, "--account", "cash", "--type", "cash", "--date-format", "DD/MM/YYYY", "--yes")
        self.assertEqual(code, 0, out)
        self.assertEqual([r["date"] for r in self.rows("cash")[1:]], ["2025-03-01", "2025-05-01", "2025-12-01"])

    def test_csv_append_needs_no_balance_column(self):
        with locked(self.home):
            Ledger(self.home, self.home.key).add_account("Everyday", "checking", "100.00", 2025)
        first = write(self, "a.csv", "Date,Description,Amount\n01/03/2025,Coffee,-4.50\n01/05/2025,Pay,20.00\n")
        code, out = self.cli(first, "--append", "--account", "everyday", "--yes")
        self.assertEqual(code, 0, out)
        later = write(self, "b.csv", "Date,Description,Amount\n01/05/2025,Pay,20.00\n01/09/2025,Books,-9.00\n")
        code, out = self.cli(later, "--append", "--account", "everyday", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("1 new row(s) to add; 1 already there (skipped)", out)
        self.assertEqual(git_log(self.home)[0], "import 1 row(s) into everyday from a CSV file")
        self.assertEqual([(r["description"], r["balance"]) for r in self.rows("everyday", 2025)],
                         [("Opening balance", "100.00"), ("Coffee", "95.50"), ("Pay", "115.50"), ("Books", "106.50")])
        code, out = self.cli(later, "--account", "everyday", "--yes")   # without --append: the old rule
        self.assertNotEqual(code, 0)
        third = write(self, "c.csv", "Date,Description,Amount\n01/11/2025,Tea,-2.00\n")
        code, out = self.cli(third, "--append", "--yes")                # the only account: no --account needed
        self.assertEqual(code, 0, out)
        self.assertIn("everyday (existing checking account)", out)
        self.assertEqual(len(Ledger(self.home, self.home.key).list_tabs()["accounts"]), 1)
        code, out = self.cli(third, "--append", "--type", "savings", "--yes")   # --type: a new account is meant
        self.assertEqual(code, 0, out)
        self.assertIn("(new savings account)", out)
        code, out = self.cli(third, "--append", "--name", "Tea Money", "--yes")   # the id comes from the name
        self.assertEqual(code, 0, out)
        self.assertIn("tea-money (new savings account)", out)
        before = len(git_log(self.home))
        code, out = self.cli(third, "--append", "--yes")                # several accounts now: which one must be said
        self.assertIn("say which account the rows go to with --account ID (existing: everyday, c, tea-money)", out)
        for option in ("--replace", "--balance-check=warn"):
            code, out = self.cli(third, "--append", "--account", "everyday", option, "--yes")
            self.assertIn("does not apply with --append", out)
        code, out = self.cli(third, "--append", "--type", "savings", "--yes")   # the guessed id "c" exists now
        self.assertIn("account c already exists; --name and --type only describe a new account", out)
        self.assertEqual(len(git_log(self.home)), before)

    def test_bank_file_option_checks(self):
        path = write(self, "card.ofx", OFX_XML)
        with locked(self.home):
            Ledger(self.home, self.home.key).add_account("Everyday", "checking", "0.00", 2026)
        code, out = self.cli(path, "--sheet", "9876", "--sheet", "0001")
        self.assertIn("give --sheet once", out)
        code, out = self.cli(path, "--sheet", "9876", "--dry-run")      # a card statement into the checking account
        self.assertIn("WARNING: the file is a credit card statement, but everyday is a checking account", out)
        code, out = self.cli(path, "--sheet", "9876", "--account", "visa", "--name", "Vi\u200bsa", "--yes")
        self.assertIn("name: contains a control or invisible character", out)
        self.assertNotIn("Preview", out)                                # refused before anything is shown
        self.assertEqual(len(git_log(self.home)), 2)


if __name__ == "__main__":
    unittest.main()
