"""`import FILE`: guessed mapping, choices, preview, confirmation and one commit."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import PASSWORD, checking_rows, git_log, make_home, run_import_cli, s, savings_rows, write_xlsx
from register import __main__ as cli
from register.core.ledger import Ledger
from register.core.store import locked

BANK = ("Account,Everyday Checking\n"
        "Exported,2025-03-01\n"
        "\n"
        "Date,Description,Amount,Balance\n"
        "01/03/2025,Coffee,-4.50,995.50\n"
        "01/05/2025,\"Pay, Inc\",2000.00,2995.50\n"
        "12/30/2024,Late fee,-5.00,2990.50\n")


class ImportCliTest(unittest.TestCase):
    def setUp(self):
        self.home = make_home(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def cli(self, *argv, tty=False, answer=None):
        """Run the command; returns (exit code, stdout + stderr text)."""
        code, out, self.asked = run_import_cli(self.home, *argv, tty=tty, answer=answer)
        return code, out

    def sheet(self, account, year=None):
        return Ledger(self.home, self.home.key).get_sheet(account, year)

    def test_bank_csv_into_a_new_checking_account(self):
        path = self.dir / "export_2025-03.csv"
        path.write_text(BANK)
        code, out = self.cli(path, "--account", "everyday", "--type", "checking", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("How export_2025-03.csv was read", out)
        self.assertIn("data from row 5; opening balance from the first row's balance; "
                      "columns date=A description=B amount=C balance=D", out)
        self.assertIn("everyday/2025 (new checking account)  <-  export_2025-03.csv", out)
        self.assertNotIn("dry run", out)
        self.assertIn("1 row(s) are dated outside 2025 (first: 2024-12-30 on row 7)", out)
        self.assertIn("date        description       amount", out)
        self.assertIn("imported and committed", out)
        rows = self.sheet("everyday", 2025)["rows"]
        self.assertEqual([(r["description"], r["amount"], r["balance"]) for r in rows], [
            ("Opening balance", "1000.00", "1000.00"), ("Coffee", "-4.50", "995.50"),
            ("Pay, Inc", "2000.00", "2995.50"), ("Late fee", "-5.00", "2990.50")])
        self.assertEqual(len(git_log(self.home)), 2)

    def test_asks_before_writing(self):
        path = self.dir / "mm.csv"
        path.write_text(BANK)
        code, out = self.cli(path)
        self.assertNotEqual(code, 0)
        self.assertIn("run again with --yes", out)
        self.assertIn("Preview (nothing has been written yet)", out)
        code, out = self.cli(path, tty=True, answer="n")
        self.assertEqual((code, self.asked), (0, True))
        self.assertIn("nothing was written", out)
        code, out = self.cli(path, "--dry-run", tty=True)
        self.assertEqual((code, self.asked), (0, False))
        self.assertIn("dry run: nothing was written", out)
        self.assertEqual(len(git_log(self.home)), 1)
        code, out = self.cli(path, tty=True, answer="y")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.sheet("mm")["rows"][-1]["balance"], "2990.50")  # guessed: savings, named after the file

    def test_nothing_is_written_if_the_data_changed_after_the_preview(self):
        path = self.dir / "mm.csv"
        path.write_text(BANK)
        led = Ledger(self.home, self.home.key)

        def meanwhile(prompt):
            with locked(self.home):
                led.add_account("Mm", "checking", "1.00", 2025, account_id="mm")
            return "y"
        with mock.patch("builtins.input", side_effect=meanwhile), mock.patch("sys.stdin") as stdin, \
                mock.patch.dict(os.environ, {"REGISTER_PASSWORD": PASSWORD}), \
                contextlib.redirect_stdout(io.StringIO()):
            stdin.isatty.return_value = True
            with self.assertRaises(SystemExit) as cm:
                cli.main(["import", "--home", str(self.home.root), str(path)])
        self.assertIn("already exists with type checking", str(cm.exception.code))
        self.assertEqual(len(git_log(self.home)), 2)  # init and the add-account only

    def test_preview_must_still_hold_when_confirmed(self):
        led = Ledger(self.home, self.home.key)
        with locked(self.home):
            led.add_account("Checking", "checking", "100.00", 2025)
        path = self.dir / "2026.csv"
        path.write_text(BANK.replace("/2025", "/2026").replace("12/30/2024", "01/09/2026"))

        def meanwhile(prompt):  # an edit to 2025 changes the balance carried into 2026
            s = led.get_sheet("checking", 2025)
            with locked(self.home):
                led.insert_row("checking", 2025, s["rev"], 1, {"amount": "5.00"})
            return "y"
        with mock.patch("builtins.input", side_effect=meanwhile), mock.patch("sys.stdin") as stdin, \
                mock.patch.dict(os.environ, {"REGISTER_PASSWORD": PASSWORD}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            stdin.isatty.return_value = True
            with self.assertRaises(SystemExit) as cm:
                cli.main(["import", "--home", str(self.home.root), str(path), "--account", "checking",
                          "--balance-check", "warn"])
        self.assertIn("carried forward 100.00", out.getvalue())
        self.assertIn("changed since the preview; nothing was written", str(cm.exception.code))
        self.assertEqual(led.list_tabs()["accounts"][0]["years"], [2025])

    def test_european_csv(self):
        path = self.dir / "rekening.csv"
        path.write_bytes(("sep=;\r\nDatum;Omschrijving;Bedrag;Saldo\r\n"
                          "05.01.2025;Bäckerei;-4,50;1.995,50\r\n"
                          "13.01.2025;Salaris;2.000,00;3.995,50\r\n").encode("cp1252"))
        code, out = self.cli(path, "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("separated by semicolons", out)
        self.assertIn("read as Windows-1252", out)
        self.assertIn("amounts use a decimal comma", out)
        self.assertIn("dates read as DD.MM.YYYY", out)
        rows = self.sheet("rekening")["rows"]
        self.assertEqual([(r["date"], r["description"], r["amount"]) for r in rows], [
            ("", "Opening balance", "2000.00"), ("2025-01-05", "Bäckerei", "-4.50"),
            ("2025-01-13", "Salaris", "2000.00")])

    def test_ambiguous_dates_are_flagged(self):
        path = self.dir / "amb.csv"
        path.write_text("Date,Description,Amount,Balance\n03/04/2025,a,1.00,1.00\n05/06/2025,b,1.00,2.00\n")
        code, out = self.cli(path, "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("AMBIGUOUS dates: they read as MM/DD/YYYY but also as DD/MM/YYYY", out)
        self.assertIn("add --date-format DD/MM/YYYY", out)
        self.assertIn("2025-03-04", out)
        code, out = self.cli(path, "--dry-run", "--date-format", "dd/mm/yyyy")
        self.assertEqual(code, 0, out)
        self.assertIn("2025-04-03", out)
        code, out = self.cli(path, "--date-format", "MM/YYYY")
        self.assertIn("needs day, month and year", out)

    def test_xlsx_workbook_and_its_choices(self):
        rows = checking_rows("100.00", [(1066, "2025-01-05", "Rent", "1000.00", "x", None, None, s("note"))])
        write_xlsx(self.dir / "book.xlsx", {"2025": rows, "Money Market": savings_rows(
            "500.00", [("2024-01-02", "250.00", "deposit")])})
        code, out = self.cli(self.dir / "book.xlsx", "--account", "x")
        self.assertIn("apply to one sheet; pick it with --sheet (importable: 2025, Money Market)", out)
        code, out = self.cli(self.dir / "book.xlsx", "--sheet", "Nope")
        self.assertIn("no importable sheet named 'Nope'", out)
        code, out = self.cli(self.dir / "book.xlsx", "--sheet", "2025", "--type", "savings", "--yes")
        self.assertNotIn("dropped", out)  # savings sheets have check numbers and an X column too
        self.assertEqual(code, 0, out)
        row = self.sheet("checking")["rows"][1]
        self.assertEqual((row["amount"], row["num"], row["cleared"]), ("-1000.00", "1066", True))
        code, out = self.cli(self.dir / "book.xlsx", "--yes")  # checking now exists as savings
        self.assertIn("existing savings account checking; choose another with --account", out)
        code, out = self.cli(self.dir / "book.xlsx", "--sheet", "Money Market", "--yes")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.sheet("money-market")["rows"][-1]["balance"], "750.00")
        self.assertEqual(len(git_log(self.home)), 3)

    def test_saved_config_reads_the_next_file(self):
        first, second = self.dir / "jan.csv", self.dir / "feb.csv"
        first.write_text(BANK)
        second.write_text(BANK.replace("Coffee", "Tea"))
        cfg = self.dir / "bank.json"
        code, out = self.cli(first, "--account", "bank", "--save-config", cfg, "--dry-run")
        self.assertEqual(code, 0, out)
        saved = json.loads(cfg.read_text())
        self.assertEqual(saved["sheets"][0]["source"], "jan.csv")
        code, out = self.cli(first, "--save-config", cfg)
        self.assertIn("already exists", out)
        code, out = self.cli(cfg, "--account", "bank")
        self.assertIn("--account are for importing a spreadsheet directly", out)
        code, out = self.cli(cfg, "--source", second, "--yes")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.sheet("bank")["rows"][1]["description"], "Tea")

    def test_files_that_cannot_be_imported(self):
        (self.dir / "old.xls").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 100)
        code, out = self.cli(self.dir / "old.xls")
        self.assertIn("save or download it as .xlsx", out)
        (self.dir / "notes.csv").write_text("just,some\ntext,here\n")
        code, out = self.cli(self.dir / "notes.csv")
        self.assertIn("no importable sheets found", out)
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
