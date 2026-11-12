"""export, and import's exact mode."""
import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import git_log, make_home
from register import __main__ as cli
from register.core import export, importer
from register.core.ledger import Ledger
from register.core.store import StoreError, locked


def populate(home):
    """A home with awkward content: quotes, commas, double spaces, Unicode, a formula-looking
    description, an all-blank row, an adjustment row, an empty year and a savings account."""
    led = Ledger(home, home.key)
    with locked(home):
        led.add_account("Checking", "checking", "100.00", 2025)
        led.add_account("Money Market", "savings", "5000.00")
        led.add_year("checking")  # 2026
        led.add_year("checking")  # 2027, left empty

    def add(account, year, fields):
        s = led.get_sheet(account, year)
        led.insert_row(account, year, s["rev"], len(s["rows"]), fields)

    add("checking", 2025, {"num": "1066", "date": "2025-01-05", "description": '  two  spaces, "quotes" ',
                           "amount": "-84.12", "cleared": True, "note": "Café ☕ — ünïcödé"})
    add("checking", 2025, {})
    add("checking", 2025, {"description": "Opening balance adjustment (import)", "amount": "12.00"})
    add("checking", 2026, {"date": "2026-03-01", "description": "=SUM(A1)", "amount": "1999.99",
                           "note": "comma, and; semicolon", "category": "Food/Out"})
    add("money-market", None, {"date": "2024-01-02", "description": "deposit", "amount": "250.00"})
    return led


def snapshot(led):
    """Everything a user can see: tabs in order, and every sheet's rows, balances and opening."""
    tabs = led.list_tabs()["accounts"]
    sheets = {}
    for a in tabs:
        for year in a["years"] or [None]:
            s = led.get_sheet(a["id"], year)
            sheets[(a["id"], year)] = (s["rows"], s["opening"], s["carried_from"])
    return [(a["id"], a["name"], a["type"], a["years"]) for a in tabs], sheets


HEAD = "num,date,description,category,amount,cleared,note,transfer,bank_id"


class ExportTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.src = make_home(self)
        self.led = populate(self.src)

    def run_import(self, home, config, **kw):
        log = io.StringIO()
        importer.run_import(home, config, home.key, out=lambda m: print(m, file=log), **kw)
        return log.getvalue()

    def test_round_trip_is_exact(self):
        out = self.tmp / "export"
        _, accounts, sheets, rows = export.export(self.src, self.src.key, out)
        self.assertEqual((accounts, sheets, rows), (2, 4, 7))  # 2025: 4, 2026: 1, 2027: 0, savings: 2
        dst = make_home(self)
        self.assertIn("imported and committed", self.run_import(dst, out / "import.json"))
        self.assertEqual(snapshot(Ledger(dst, dst.key)), snapshot(self.led))
        self.assertEqual(len(git_log(dst)), 2)  # init, then one commit for the whole import

    def test_files_are_plain_and_readable(self):
        out = self.tmp / "export"
        export.export(self.src, self.src.key, out)
        self.assertEqual(sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()),
                         ["checking/2025.csv", "checking/2026.csv", "checking/2027.csv",
                          "import.json", "money-market.csv"])
        lines = (out / "checking" / "2025.csv").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], HEAD)
        self.assertEqual(lines[2], '1066,2025-01-05,"  two  spaces, ""quotes"" ",,-84.12,x,Café ☕ — ünïcödé,,')
        self.assertEqual(lines[3], ",,,,,,,,")
        self.assertEqual((out / "checking" / "2027.csv").read_text(encoding="utf-8"), HEAD + "\n")
        self.assertEqual((out / "money-market.csv").read_text(encoding="utf-8").splitlines()[0], HEAD)
        config = json.loads((out / "import.json").read_text(encoding="utf-8"))
        self.assertTrue(config["exact"])
        self.assertEqual([a["id"] for a in config["accounts"]], ["checking", "money-market"])

    def test_formats_for_other_programs(self):
        from register.core import bankfile
        led = self.led
        s = led.get_sheet("checking", 2025)
        doc = json.loads(export.collect_for_other_tools(self.src, self.src.key, "json")[0]["register.json"])
        self.assertEqual([(a["id"], a["type"], [sh["year"] for sh in a["sheets"]]) for a in doc["accounts"]],
                         [("checking", "checking", [2025, 2026, 2027]), ("money-market", "savings", [None])])
        self.assertEqual(doc["accounts"][0]["sheets"][0]["rows"], [{k: v for k, v in r.items() if k != "balance"}
                                                                  for r in s["rows"]])
        for kind in ("qif", "ofx"):
            out = self.tmp / kind
            _, accounts, sheets, rows = export.export(self.src, self.src.key, out, kind, "EUR")
            self.assertEqual((accounts, sheets, rows), (2, 4, 7))
            self.assertEqual(sorted(p.name for p in out.iterdir()), ["checking.%s" % kind, "money-market.%s" % kind])
            back = bankfile.read(out / ("checking.%s" % kind)).rows   # our own reader takes them back
            self.assertEqual(len(back), 5)
            self.assertEqual([r["amount"] for r in back], [r["amount"] or "0.00" for r in
                                                         s["rows"] + led.get_sheet("checking", 2026)["rows"]])
            self.assertEqual(back[1]["num"], "1066")
            if kind == "qif":
                self.assertEqual(back[-1]["category"], "Food-Out")   # "/" would start a QIF class
            self.assertTrue(all(r["date"] for r in back))  # undated rows take a neighbour's date
        text = (self.tmp / "ofx" / "checking.ofx").read_text(encoding="utf-8")
        self.assertIn("<CURDEF>EUR", text)
        ids = [line for line in text.splitlines() if line.startswith("<FITID>")]
        self.assertEqual(len(set(ids)), 5)
        twice = [{"bank_id": "DUP", "date": "2025-01-0%d" % d, "amount": "-1.00", "description": "x", "num": "",
                  "note": "", "category": "", "cleared": False, "transfer": ""} for d in (1, 2)]
        ofx = export._ofx({"id": "a", "type": "checking"}, export._dated(twice), "USD")
        fitids = [line for line in ofx.splitlines() if line.startswith("<FITID>")]
        self.assertEqual((fitids[0], len(set(fitids))), ("<FITID>DUP", 2))   # a bank's repeated id is not repeated
        plain = dict(twice[0], bank_id="")
        made = [line for line in export._ofx({"id": "a", "type": "checking"}, export._dated([plain]), "USD").splitlines()
                if line.startswith("<FITID>")][0][len("<FITID>"):]
        again = export._ofx({"id": "a", "type": "checking"},                 # that id came back on the row; a twin follows
                            export._dated([dict(plain, bank_id=made), plain]), "USD")
        self.assertEqual(len({line for line in again.splitlines() if line.startswith("<FITID>")}), 2)
        again = export.collect_for_other_tools(self.src, self.src.key, "ofx", "EUR")[0]["checking.ofx"]
        self.assertEqual(again, text)  # the same ids every time
        for kind, currency in (("xml", "USD"), ("ofx", "usd"), ("ofx", "")):
            with self.assertRaises(export.ExportError):
                export.collect_for_other_tools(self.src, self.src.key, kind, currency)

    def test_ledger_export_matches_the_cli_files(self):
        out = self.tmp / "export"
        export.export(self.src, self.src.key, out)
        result = self.led.export("correct horse battery")
        self.assertEqual(result["files"], {str(p.relative_to(out)): p.read_text(encoding="utf-8")
                                           for p in out.rglob("*") if p.is_file()})
        with self.assertRaises(StoreError) as cm:
            self.led.export("wrong")
        self.assertEqual(cm.exception.code, "bad_password")

    def test_permissions(self):
        out = self.tmp / "export"
        export.export(self.src, self.src.key, out)
        for p in [out, *out.rglob("*")]:
            want = 0o700 if p.is_dir() else 0o600
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), want, p)

    def test_reimport_needs_replace_and_changes_nothing(self):
        out = self.tmp / "export"
        export.export(self.src, self.src.key, out)
        with self.assertRaises(importer.ImportFailed):
            self.run_import(self.src, out / "import.json")
        self.assertIn("nothing changed", self.run_import(self.src, out / "import.json", replace=True))

    def test_refusals(self):
        busy = self.tmp / "busy"
        busy.mkdir()
        (busy / "something").write_text("x")
        for bad in (busy, self.src.root, self.src.data, self.src.root / "export"):
            with self.assertRaises(export.ExportError, msg=bad):
                export.export(self.src, self.src.key, bad)
        (self.src.data / "stray.txt").write_text("uncommitted")
        out = self.tmp / "export"
        with self.assertRaises(StoreError) as cm:
            export.export(self.src, self.src.key, out)
        self.assertEqual(cm.exception.code, "repo_dirty")
        self.assertFalse(out.exists())

    def test_cli_needs_the_password(self):
        out = self.tmp / "export"
        argv = ["export", "--home", str(self.src.root), str(out)]
        with mock.patch.dict(os.environ, {"REGISTER_PASSWORD": "wrong"}):
            with self.assertRaises(SystemExit) as cm:
                cli.main(argv)
        self.assertIn("wrong password", str(cm.exception.code))
        self.assertFalse(out.exists())
        printed = io.StringIO()
        with mock.patch.dict(os.environ, {"REGISTER_PASSWORD": "correct horse battery"}), \
                contextlib.redirect_stdout(printed):
            cli.main(argv)
        self.assertIn("exported 2 account(s), 4 sheet(s), 7 row(s)", printed.getvalue())
        self.assertIn("decrypted", printed.getvalue())


class ExactImportTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.home = make_home(self)

    def config(self, csv_text, **entry):
        (self.dir / "c.csv").write_text(csv_text, encoding="utf-8")
        cfg = {"exact": True, "accounts": [{"id": "checking", "name": "Checking", "type": "checking"}],
               "sheets": [dict({"source": "c.csv", "account": "checking", "year": 2025}, **entry)]}
        (self.dir / "import.json").write_text(json.dumps(cfg))
        return self.dir / "import.json"

    def fails_with(self, csv_text):
        with self.assertRaises(importer.ImportFailed) as cm:
            importer.run_import(self.home, self.config(csv_text, year=2026), self.home.key, out=lambda m: None)
        return str(cm.exception)

    def fails(self, csv_text, **entry):
        log = io.StringIO()
        with self.assertRaises(importer.ImportFailed):
            importer.run_import(self.home, self.config(csv_text, **entry), self.home.key,
                                out=lambda m: print(m, file=log))
        self.assertEqual(len(git_log(self.home)), 1)  # nothing written
        return log.getvalue()

    def test_rows_are_validated_not_cleaned_up(self):
        head = "num,date,description,amount,cleared,note\n"
        for bad in ("date,description,total\n", "date,date,amount\n", "\n", ""):
            self.assertIn("first line must name the row fields, each at most once (%s)" % HEAD, self.fails(bad))
        self.assertIn("c.csv line 3: expected 6 values, found 5", self.fails(head + ",,,,,\n,,,,\n"))
        self.assertIn("c.csv line 2: cleared must be x or blank (found 'X')", self.fails(head + ",,,,X,\n"))
        self.assertIn("c.csv line 2: amount: must be an amount", self.fails(head + ",,,1.5,,\n"))
        self.assertIn("c.csv line 2: date: must be YYYY-MM-DD", self.fails(head + ",2025-1-5,,,,\n"))
        self.assertIn("c.csv line 2: description: contains a control or invisible character",
                      self.fails(head + ",,a​b,,,\n"))
        self.assertIn("not valid CSV", self.fails(head + ',,"unterminated,,,\n'))

    def test_config_shape(self):
        with self.assertRaises(importer.ImportFailed):
            importer.load_config(self.config("x\n", columns={"amount": "A"}))  # not an exact key
        path = self.dir / "import.json"
        path.write_text(json.dumps({"exact": "yes", "sheets": [{"source": "c.csv", "account": "x"}]}))
        with self.assertRaises(importer.ImportFailed):
            importer.load_config(path)

    def test_no_opening_row_is_added(self):
        log = io.StringIO()
        importer.run_import(self.home, self.config("num,date,description,amount,cleared,note\n"
                                                   ",2025-01-02,  as is  ,5.00,x,\n"),
                            self.home.key, out=lambda m: print(m, file=log))
        rows = Ledger(self.home, self.home.key).get_sheet("checking", 2025)["rows"]
        self.assertEqual(rows, [{"num": "", "date": "2025-01-02", "description": "  as is  ", "category": "",
                                 "amount": "5.00", "cleared": True, "note": "", "transfer": "", "bank_id": "",
                                 "balance": "5.00"}])
        self.assertIn("closing balance 5.00", log.getvalue())

    def test_format_3_export_imports(self):
        """An export made by the previous data format: fewer columns, and savings files without num
        or cleared. The missing fields come in blank."""
        (self.dir / "checking").mkdir()
        (self.dir / "checking" / "2025.csv").write_text(
            "num,date,description,amount,cleared,note\n,,Opening balance,100.00,,\n"
            '1066,2025-01-05,"Rent, etc",-40.00,x,a note\n', encoding="utf-8")
        (self.dir / "money-market.csv").write_text(
            "date,description,amount,note\n,Opening balance,500.00,\n2025-02-01,Interest,1.25,ünï\n",
            encoding="utf-8")
        (self.dir / "import.json").write_text(json.dumps({
            "exact": True,
            "accounts": [{"id": "checking", "name": "Checking", "type": "checking"},
                         {"id": "money-market", "name": "Money Market", "type": "savings"}],
            "sheets": [{"source": "checking/2025.csv", "account": "checking", "year": 2025},
                       {"source": "money-market.csv", "account": "money-market"}]}))
        importer.run_import(self.home, self.dir / "import.json", self.home.key, out=lambda m: None)
        led = Ledger(self.home, self.home.key)
        self.assertEqual(led.get_sheet("checking", 2025)["rows"][1], {
            "num": "1066", "date": "2025-01-05", "description": "Rent, etc", "category": "", "amount": "-40.00",
            "cleared": True, "note": "a note", "transfer": "", "bank_id": "", "balance": "60.00"})
        self.assertEqual(led.get_sheet("money-market", None)["rows"][1], {
            "num": "", "date": "2025-02-01", "description": "Interest", "category": "", "amount": "1.25",
            "cleared": False, "note": "ünï", "transfer": "", "bank_id": "", "balance": "501.25"})

    def test_account_settings_round_trip_and_default(self):
        led = Ledger(self.home, self.home.key)
        led.add_account("Visa", "card", "-5.00", 2025, owed_positive=False)
        led.add_account("Loan", "loan", "-9.00")
        out = self.dir / "out"
        export.export(self.home, self.home.key, out)
        config = json.loads((out / "import.json").read_text())
        self.assertEqual(config["accounts"], [
            {"id": "visa", "name": "Visa", "type": "card", "owed_positive": False},
            {"id": "loan", "name": "Loan", "type": "loan", "owed_positive": True}])
        self.assertEqual((out / "loan.csv").read_text().splitlines()[1], ",,Opening balance,,-9.00,,,,")  # stored sign
        del config["accounts"][1]["owed_positive"]  # as in a hand-written config: the type's default
        config["accounts"][0]["owed_positive"] = "yes"
        (out / "import.json").write_text(json.dumps(config))
        fresh = make_home(self)
        with self.assertRaises(importer.ImportFailed):
            importer.run_import(fresh, out / "import.json", fresh.key, out=lambda m: None)
        config["accounts"][0]["owed_positive"] = False
        (out / "import.json").write_text(json.dumps(config))
        importer.run_import(fresh, out / "import.json", fresh.key, out=lambda m: None)
        self.assertTrue(git_log(fresh)[0].endswith(" from an export"), git_log(fresh)[0])
        self.assertEqual([(a["id"], a["owed_positive"]) for a in Ledger(fresh, fresh.key).list_tabs()["accounts"]],
                         [("visa", False), ("loan", True)])

    def test_transfer_links_must_stay_in_pairs(self):
        link = "0123456789abcdef"
        row = HEAD + "\n,2025-01-02,To savings,Transfer,-5.00,,,%s,\n" % link
        importer.run_import(self.home, self.config(row), self.home.key, out=lambda m: None)   # one side: allowed
        led = Ledger(self.home, self.home.key)
        led.add_account("Other", "checking", "", 2025)
        led.add_account("Third", "checking", "", 2025)
        for account, ok in (("other", True), ("third", False)):          # a second side, then a third
            cfg = json.loads(self.config(row).read_text())
            cfg["accounts"] = []
            cfg["sheets"][0]["account"] = account
            (self.dir / "import.json").write_text(json.dumps(cfg))
            try:
                importer.run_import(self.home, self.dir / "import.json", self.home.key, replace=True, out=lambda m: None)
                self.assertTrue(ok)
            except importer.ImportFailed as e:
                self.assertFalse(ok)
                self.assertIn("1 transfer link(s) would not join exactly two rows in two accounts", str(e))
        twice = HEAD + "\n" + ",2025-01-02,a,,-5.00,,,%s,\n" % ("f" * 16) * 2            # both in one account
        self.assertIn("transfer link(s) would not join", self.fails_with(twice))

    def test_link_fields_round_trip(self):
        self.assertIn("transfer: must be blank or a 16-character transfer id",
                      self.fails(HEAD + "\n,,,,,,,nope,\n"))
        importer.run_import(self.home, self.config(
            HEAD + "\n,2025-01-02,To savings,Transfer,-5.00,,,0123456789abcdef,\n"
            ",2025-01-03,Coffee,Food,-2.00,x,,,FITID-77\n"), self.home.key, out=lambda m: None)
        rows = Ledger(self.home, self.home.key).get_sheet("checking", 2025)["rows"]
        self.assertEqual([(r["category"], r["transfer"], r["bank_id"]) for r in rows],
                         [("Transfer", "0123456789abcdef", ""), ("Food", "", "FITID-77")])


if __name__ == "__main__":
    unittest.main()
