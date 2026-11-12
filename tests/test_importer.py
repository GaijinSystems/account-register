import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from helpers import checking_rows, cur, date, git_log, make_home, s, savings_rows, write_ods
from register.core import importer, sources
from register.core.ledger import Ledger
from register.core.store import locked

CHECKING_COLS = {"num": "A", "date": "B", "description": "C", "debit": "D", "cleared": "E",
                 "credit": "F", "balance": "H", "note": ["I", "J"]}
ACCOUNTS = [{"id": "checking", "name": "Checking", "type": "checking"},
            {"id": "money-market", "name": "Money Market", "type": "savings"}]

Y2025 = [
    (None, "2025-01-03", "Electric, Co", "84.12", "x", None, None, None),
    (1066, "2025-01-05", "Rent", "1000.00", "X", None, cur("6531.76"), s("Plus 5?")),
    (None, "2025-01-06", "Paycheck", None, None, "2150.00", s("late"), None),
]
Y2026 = [(None, "2026-01-02", "Gas", "40.00", "x", None, None, None)]


class ImporterTest(unittest.TestCase):
    def setUp(self):
        self.home = make_home(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def workbook(self, y2025=None, y2026=None, opening2026="1165.88", mm=None):
        sheets = {"2025": checking_rows("100.00", y2025 or Y2025),
                  "2026": checking_rows(opening2026, y2026 or Y2026)}
        sheets["Money Market"] = mm or savings_rows("500.00", [("2024-01-02", "250.00", "deposit"),
                                                               ("2024-02-01", "-50.00", "withdrawal")])
        write_ods(self.dir / "register.ods", sheets)

    def config(self, sheets=None, **extra):
        if sheets is None:
            sheets = [{"source": "register.ods", "sheet": y, "account": "checking", "year": int(y),
                       "start_row": 3, "opening": "H1", "columns": CHECKING_COLS} for y in ("2025", "2026")]
            sheets.append({"source": "register.ods", "sheet": "Money Market", "account": "money-market",
                           "start_row": 2, "opening": "C1",
                           "columns": {"date": "A", "amount": "B", "balance": "C", "description": "D"}})
        cfg = {"accounts": ACCOUNTS, "sheets": sheets, **extra}
        path = self.dir / "import.json"
        path.write_text(json.dumps(cfg))
        return path

    def run_import(self, **kw):
        out = io.StringIO()
        importer.run_import(self.home, self.config(**kw.pop("cfg", {})), self.home.key,
                            out=lambda m: print(m, file=out), **kw)
        return out.getvalue()

    def test_full_workbook_import(self):
        self.workbook()
        self.run_import()
        ledger = Ledger(self.home, self.home.key)
        s25 = ledger.get_sheet("checking", 2025)
        rows = [{k: v for k, v in r.items()} for r in s25["rows"]]
        self.assertEqual(rows[0]["description"], "Opening balance")
        self.assertEqual(rows[0]["amount"], "100.00")
        self.assertEqual(rows[1], {"num": "", "date": "2025-01-03", "description": "Electric, Co", "category": "",
                                   "amount": "-84.12", "cleared": True, "note": "", "transfer": "", "bank_id": "",
                                   "balance": "15.88"})
        self.assertEqual(rows[2]["num"], "1066")                       # float cell -> "1066", not "1066.0"
        self.assertEqual(rows[2]["note"], "$6,531.76 Plus 5?")          # I+J joined, numbers kept as shown
        self.assertEqual(rows[3]["amount"], "2150.00")                   # credit -> positive
        self.assertFalse(rows[3]["cleared"])
        s26 = ledger.get_sheet("checking", 2026)
        self.assertEqual((s26["carried_from"], s26["opening"]), (2025, "1165.88"))
        self.assertEqual(s26["rows"][0]["description"], "Gas")          # no opening row after the first year
        mm = ledger.get_sheet("money-market", None)
        self.assertEqual([r["balance"] for r in mm["rows"]], ["500.00", "750.00", "700.00"])
        self.assertEqual(len(git_log(self.home)), 2)                     # init + one import commit
        self.assertTrue(git_log(self.home)[0].endswith(" from an .ods workbook"), git_log(self.home)[0])
        self.assertTrue(ledger.status()["repo_clean"])

    def test_workbook_passed_instead_of_config(self):
        self.workbook()
        with self.assertRaises(importer.ImportFailed) as cm:
            importer.run_import(self.home, self.dir / "register.ods", self.home.key, out=lambda m: None)
        self.assertIn("not an import config", str(cm.exception))

    def test_dry_run_writes_nothing(self):
        self.workbook()
        out = self.run_import(dry_run=True)
        self.assertIn("dry run: nothing was written", out)
        self.assertEqual(len(git_log(self.home)), 1)
        self.assertFalse((self.home.data / "checking").exists())

    def test_balance_mismatch_strict_and_warn(self):
        bad = Y2025[:2] + [(None, "2025-01-06", "Paycheck", None, None, "2150.00", None, None)]
        self.workbook(y2025=bad)
        # tamper one cached balance: H5 should be 1165.88
        rows = checking_rows("100.00", bad)
        rows[4][7] = cur("1165.89", formula="of:=([.H4]-[.D5]+[.F5])")
        write_ods(self.dir / "register.ods", {"2025": rows, "2026": checking_rows("1165.88", Y2026),
                                              "Money Market": savings_rows("0.00", [])})
        with self.assertRaises(importer.ImportFailed):
            self.run_import()
        self.assertEqual(len(git_log(self.home)), 1)
        out = self.run_import(balance_check="warn")
        self.assertIn("H5: computed 1165.88, source 1165.89", out)

    def test_carry_forward_mismatch(self):
        self.workbook(opening2026="1000.00")
        out = io.StringIO()
        with self.assertRaises(importer.ImportFailed):
            importer.run_import(self.home, self.config(), self.home.key, out=lambda m: print(m, file=out))
        self.assertIn("differs from the carried-forward 1165.88", out.getvalue())
        self.assertIn("closing balance 960.00", out.getvalue())  # 2026 still checked: 1000.00 - 40.00
        out = self.run_import(balance_check="warn")
        self.assertIn("differs from the carried-forward 1165.88", out)
        s26 = Ledger(self.home, self.home.key).get_sheet("checking", 2026)
        self.assertEqual(s26["rows"][0]["description"], "Opening balance adjustment (import)")
        self.assertEqual(s26["rows"][0]["amount"], "-165.88")
        self.assertEqual(s26["rows"][0]["balance"], "1000.00")

    def test_impossible_year_is_reported(self):
        mm = savings_rows("0.00", [("2025-01-01", "1.00", "ok")])
        mm[1][0] = date("0225-01-02", "01/02/25")
        self.workbook(mm=mm)
        out = io.StringIO()
        with self.assertRaises(importer.ImportFailed):
            importer.run_import(self.home, self.config(), self.home.key, out=lambda m: print(m, file=out))
        self.assertIn("A2: year is out of range (stored as 0225-01-02)", out.getvalue())

    def test_bad_cleared_mark(self):
        self.workbook(y2025=[(None, "2025-01-03", "x", "1.00", "net key", None, None, None)])
        out = io.StringIO()
        with self.assertRaises(importer.ImportFailed):
            importer.run_import(self.home, self.config(), self.home.key, out=lambda m: print(m, file=out))
        self.assertIn("E3: cleared mark must be x or blank (found 'net key')", out.getvalue())

    def test_repeated_errors_are_grouped(self):
        rows = [(None, "2025-01-%02d" % d, "x", "1.00", "junk %d" % d, None, None, None) for d in range(1, 26)]
        self.workbook(y2025=rows)
        out = io.StringIO()
        with self.assertRaises(importer.ImportFailed):
            importer.run_import(self.home, self.config(), self.home.key, out=lambda m: print(m, file=out))
        lines = [l for l in out.getvalue().splitlines() if "cleared mark" in l]
        self.assertEqual(lines, ["  ERROR: E3–E27 (25 cells): cleared mark must be x or blank "
                                 "(e.g. found 'junk 1' at E3)"])

    def test_replace_protection(self):
        self.workbook()
        self.run_import()
        with self.assertRaises(importer.ImportFailed):
            self.run_import()
        self.assertIn("nothing changed", self.run_import(replace=True))
        self.assertEqual(len(git_log(self.home)), 2)
        self.workbook(y2026=[(None, "2026-01-02", "Gas", "41.00", "x", None, None, None)])
        self.assertIn("imported and committed", self.run_import(replace=True))
        self.assertEqual(len(git_log(self.home)), 3)

    def test_huge_or_non_finite_numbers_are_cell_errors(self):
        (self.dir / "savings.csv").write_text(',,0.00,\n01/02/24,%s,1.00,big\n' % ("1" * 40))
        entry = {"source": "savings.csv", "csv": {"date_format": "%m/%d/%y"}, "account": "money-market",
                 "start_row": 2, "opening": "C1",
                 "columns": {"date": "A", "amount": "B", "balance": "C", "description": "D"}}
        out = io.StringIO()
        with self.assertRaises(importer.ImportFailed):
            importer.run_import(self.home, self.config(sheets=[entry]), self.home.key,
                                out=lambda m: print(m, file=out))
        self.assertIn("B2: number is too large or not finite", out.getvalue())
        for value in ("1e999999", "Infinity", "NaN", "sNaN", "1000000000000"):
            with self.assertRaises(importer.CellError, msg=value):
                importer._amount(sources.Cell("float", value, None, value, None), "A1")
        for value in ("Infinity", "1e2000000"):  # a check number that is not a whole number we can print
            self.assertEqual(importer._text([("A1", sources.Cell("float", value, None, "x", None))], "num"), "x")
        self.assertEqual(importer._text([("A1", sources.Cell("float", "1066", None, "1,066", None))], "num"), "1066")

    def test_balances_beyond_the_storable_range_are_errors(self):
        big = "999999999999.99"
        cases = [
            ("2.00", ',,0.00,\n01/02/24,1.00,5000000000000000,x\n', "C2"),  # balance cell
            ("5000000000000", ',,0.00,\n01/02/24,1.00,1.00,x\n', "opening"),  # opening setting
            ("C1", ',,5000000000000,\n01/02/24,1.00,1.00,x\n', "C1"),  # opening cell
            ("C1", ',,%s,\n01/02/24,%s,1.00,x\n' % (big, big), "amount out of range"),  # sum
        ]
        for opening, csv_text, expect in cases:
            (self.dir / "savings.csv").write_text(csv_text)
            entry = {"source": "savings.csv", "csv": {"date_format": "%m/%d/%y"}, "account": "money-market",
                     "start_row": 2, "opening": opening,
                     "columns": {"date": "A", "amount": "B", "balance": "C", "description": "D"}}
            out = io.StringIO()
            with self.assertRaises(importer.ImportFailed, msg=csv_text):
                importer.run_import(self.home, self.config(sheets=[entry]), self.home.key, dry_run=True,
                                    out=lambda m: print(m, file=out))
            self.assertIn(expect, out.getvalue())
            self.assertIn("ERROR", out.getvalue())

    def test_import_cannot_push_a_later_year_out_of_range(self):
        self.workbook()
        self.run_import()
        ledger = Ledger(self.home, self.home.key)
        s26 = ledger.get_sheet("checking", 2026)
        with locked(self.home):
            ledger.insert_row("checking", 2026, s26["rev"], len(s26["rows"]), {"amount": "999999990000.00"})
        self.workbook(y2025=Y2025 + [(None, "2025-02-01", "Big", None, None, "10000.00", None, None)])
        only_2025 = [{"source": "register.ods", "sheet": "2025", "account": "checking", "year": 2025,
                      "start_row": 3, "opening": "H1", "columns": CHECKING_COLS}]
        out = io.StringIO()
        with self.assertRaises(importer.ImportFailed):
            importer.run_import(self.home, self.config(sheets=only_2025), self.home.key, replace=True,
                                out=lambda m: print(m, file=out))
        self.assertIn("the balance at checking/2026 row", out.getvalue())

    def test_new_account_never_adopts_a_leftover_directory(self):
        self.workbook()
        (self.home.data / "money-market").mkdir()  # e.g. left over by hand; not in accounts.json
        with self.assertRaises(importer.ImportFailed) as cm:
            self.run_import()
        self.assertIn("a directory named money-market already exists", str(cm.exception))

    def test_malformed_ods_is_a_source_error(self):
        def ods(body):
            path = self.dir / "bad.ods"
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("content.xml", '<office:document-content '
                           'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
                           'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
                           'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
                           '<office:body>%s</office:body></office:document-content>' % body)
            return path

        def sheet(row):
            return '<office:spreadsheet><table:table table:name="S">%s</table:table></office:spreadsheet>' % row

        cell = '<table:table-cell office:value-type="string"><text:p>x</text:p></table:table-cell>'
        bad = [
            "",  # no office:spreadsheet
            sheet('<table:table-row table:number-rows-repeated="x">%s</table:table-row>' % cell),
            sheet('<table:table-row table:number-rows-repeated="-3">%s</table:table-row>' % cell),
            sheet('<table:table-row table:number-rows-repeated="²">%s</table:table-row>' % cell),
            sheet('<table:table-row table:number-rows-repeated="%s">%s</table:table-row>' % ("9" * 5000, cell)),
            sheet('<table:table-row><table:table-cell table:number-columns-repeated="1.5" '
                  'office:value-type="string"><text:p>x</text:p></table:table-cell></table:table-row>'),
            sheet('<table:table-row><table:table-cell office:value-type="string"><text:p>a'
                  '<text:s text:c="999999999"/>b</text:p></table:table-cell></table:table-row>'),
            # many small space runs in one cell add up
            sheet('<table:table-row><table:table-cell office:value-type="string"><text:p>%s</text:p>'
                  '</table:table-cell></table:table-row>' % ('<text:s text:c="10000"/>' * 11)),
            # content after an enormous empty gap, or beyond the last column
            sheet('<table:table-row table:number-rows-repeated="999999999"/><table:table-row>%s'
                  '</table:table-row>' % cell),
            sheet('<table:table-row><table:table-cell table:number-columns-repeated="16384"/>%s'
                  '</table:table-row>' % cell),
        ]
        for body in bad:
            with self.assertRaises(sources.SourceError, msg=body):
                sources.read_ods(ods(body))
        spaces = '<table:table-row><table:table-cell office:value-type="string"><text:p>%s</text:p>' \
                 '</table:table-cell></table:table-row>' % ('<text:s text:c="9999"/>' * 10)
        with mock.patch.object(sources, "MAX_WORKBOOK_TEXT", 500000):  # 5 such cells fit, 60 do not
            sources.read_ods(ods(sheet(spaces * 5)))
            with self.assertRaises(sources.SourceError) as cm:
                sources.read_ods(ods(sheet(spaces * 60)))
        self.assertIn("workbook", str(cm.exception))
        repeated = '<table:table-row table:number-rows-repeated="100"><table:table-cell ' \
                   'table:number-columns-repeated="100" office:value-type="string"><text:p>%s</text:p>' \
                   '</table:table-cell></table:table-row>' % ('<text:s text:c="9999"/>' * 10)
        with mock.patch.object(sources, "MAX_WORKBOOK_TEXT", 500000):  # one cell, 10000 copies
            with self.assertRaises(sources.SourceError) as cm:
                sources.read_ods(ods(sheet(repeated)))
        self.assertIn("workbook", str(cm.exception))
        many = '<table:table-row table:number-rows-repeated="10000"><table:table-cell ' \
               'table:number-columns-repeated="300" office:value-type="string"><text:p>x</text:p>' \
               '</table:table-cell></table:table-row>'
        with self.assertRaises(sources.SourceError):
            sources.read_ods(ods(sheet(many)))
        wide = '<table:table-row>%s</table:table-row>' % ('<table:table-cell table:number-columns-repeated='
                                                           '"10000" office:value-type="string"><text:p>x</text:p>'
                                                           '</table:table-cell>' * 600)
        with mock.patch.object(sources, "MAX_CELLS", 50000), \
                mock.patch.object(sources, "Cell", wraps=sources.Cell) as made:
            with self.assertRaises(sources.SourceError):  # refused before expanding the whole row
                sources.read_ods(ods(sheet(wide)))
        self.assertLessEqual(made.call_count, 50000)
        good = sources.read_ods(ods(sheet('<table:table-row table:number-rows-repeated="2">%s</table:table-row>'
                                          % cell)))
        self.assertEqual(sorted(good["S"]), [(0, 0), (1, 0)])
        # LibreOffice pads sheets with empty repeats up to the full size; that stays fine
        padded = sources.read_ods(ods(sheet(
            '<table:table-row>%s<table:table-cell table:number-columns-repeated="16383"/></table:table-row>'
            '<table:table-row table:number-rows-repeated="1048575"><table:table-cell '
            'table:number-columns-repeated="16384"/></table:table-row>' % cell)))
        self.assertEqual(sorted(padded["S"]), [(0, 0)])

    def test_csv_with_split_money(self):
        (self.dir / "savings.csv").write_text(
            ',,"$86,128.44",\n'
            '12/26/23,"$1,000.00","$87,128.44",stuff\n'
            '01/12/24,-$1,128.44,$86,000.00,more, stuff\n'.replace("more, stuff", '"more, stuff"'))
        entry = {"source": "savings.csv", "csv": {"date_format": "%m/%d/%y"}, "account": "money-market",
                 "start_row": 2, "opening": "C1",
                 "columns": {"date": "A", "amount": "B", "balance": "C", "description": "D"}}
        out = io.StringIO()
        with self.assertRaises(importer.ImportFailed):
            importer.run_import(self.home, self.config(sheets=[entry]), self.home.key, out=lambda m: print(m, file=out))
        self.assertIn("repair_split_money", out.getvalue())
        entry["csv"]["repair_split_money"] = True
        importer.run_import(self.home, self.config(sheets=[entry]), self.home.key, out=lambda m: None)
        rows = Ledger(self.home, self.home.key).get_sheet("money-market", None)["rows"]
        self.assertEqual([(r["date"], r["amount"], r["description"], r["balance"]) for r in rows], [
            ("", "86128.44", "Opening balance", "86128.44"),
            ("2023-12-26", "1000.00", "stuff", "87128.44"),
            ("2024-01-12", "-1128.44", "more, stuff", "86000.00"),
        ])


if __name__ == "__main__":
    unittest.main()
