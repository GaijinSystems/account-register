import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import checking_rows, cur, make_home, s, savings_rows, write_ods
from register.core import importer, sources, template
from register.core.ledger import Ledger

ROOT = Path(__file__).resolve().parent.parent


class LimitsTest(unittest.TestCase):
    def test_running_balance_search_is_bounded(self):
        cols = list(range(template.MAX_VALUE_COLUMNS + 1))
        with mock.patch.object(template, "_num", side_effect=AssertionError("searched")):
            self.assertIsNone(template._balance_by_values({}, list(range(20)), cols))
        # at the column limit: each cell of the sampled rows is parsed once
        cols = list(range(template.MAX_VALUE_COLUMNS))
        rows = list(range(template.MAX_VALUE_ROWS + 500))
        with mock.patch.object(template, "_num", return_value=None) as num:
            self.assertIsNone(template._balance_by_values({}, rows, cols))
        self.assertEqual(num.call_count, len(cols) * template.MAX_VALUE_ROWS)


class TemplateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_ods_workbook(self):
        entries = [(None, "2025-01-03", "Electric", "84.12", "x", None, None, None),
                   (1066, "2025-01-05", "Rent", "1000.00", "x", None, cur("6531.76"), s("Plus 5?")),
                   (None, "2025-01-06", "Pay", None, None, "2150.00", None, None)]
        mm = savings_rows("500.00", [("2024-01-02", "250.00", "dep"), ("2024-02-01", "-50.00", "wd")])
        y26 = checking_rows("1165.88", [(None, "2026-01-02", "Gas", "40.00", "x", None, None, None)])
        y26[0][7] = cur("1165.88", formula="of:=[$'2025'.H5]")  # carried by a cross-sheet formula
        write_ods(self.dir / "wb.ods", {"2025": checking_rows("100.00", entries), "2026": y26, "Money Market": mm})
        config, report = template.build_template(self.dir / "wb.ods", self.dir)
        sheets = {e.get("sheet"): e for e in config["sheets"]}
        self.assertEqual(sheets["2025"], {
            "source": "wb.ods", "sheet": "2025", "account": "checking", "year": 2025, "start_row": 3, "opening": "H1",
            "columns": {"num": "A", "date": "B", "description": "C", "debit": "D", "cleared": "E", "credit": "F",
                        "balance": "H", "note": ["I", "J"]}})
        self.assertEqual(sheets["2026"]["opening"], "H1")
        self.assertEqual(sheets["Money Market"], {
            "source": "wb.ods", "sheet": "Money Market", "account": "money-market", "start_row": 2, "opening": "C1",
            "columns": {"date": "A", "description": "D", "amount": "B", "balance": "C"}})
        self.assertEqual([a["id"] for a in config["accounts"]], ["checking", "money-market"])

        # the generated config imports cleanly
        home = make_home(self)
        (self.dir / "import.json").write_text(template.dumps_config(config))
        importer.run_import(home, self.dir / "import.json", home.key, out=lambda m: None)
        self.assertEqual(Ledger(home, home.key).get_sheet("checking", 2026)["opening"], "1165.88")

    def test_a_stray_far_away_cell_does_not_stop_the_guess(self):
        rows = checking_rows("100.00", [(None, "2025-01-03", "Gas", "4.00", "x", None, None, None),
                                        (None, "2025-01-04", "Pay", None, None, "9.00", None, None)])
        write_ods(self.dir / "wb.ods", {"2025": rows})
        grid = sources.read_ods(self.dir / "wb.ods")["2025"]
        plain = template.guess_sheet("2025", grid)
        grid[(0, sources.MAX_COLS - 1)] = sources.Cell("string", None, None, "printed 2025-02-01")
        grid[(sources.MAX_ROWS - 1, 0)] = sources.Cell("float", "1", None, "1")  # below everything
        with mock.patch.object(template, "MAX_GUESS_AREA", 100):
            stray = template.guess_sheet("2025", grid)
        self.assertEqual((stray.columns, stray.start_row, stray.opening),
                         (plain.columns, plain.start_row, plain.opening))
        self.assertEqual(plain.columns["balance"], 7)

    def test_csv_without_header_or_formulas(self):
        (self.dir / "mm.csv").write_text(
            ",,\"$1,000.00\",\n"
            "12/26/23,\"$250.00\",\"$1,250.00\",deposit\n"
            "01/12/24,-$50.00,\"$1,200.00\",fee\n"
            "02/01/24,$10.00,\"$1,210.00\",interest\n")
        config, report = template.build_template(self.dir / "mm.csv", self.dir)
        entry = config["sheets"][0]
        self.assertEqual(entry["columns"], {"date": "A", "description": "D", "amount": "B", "balance": "C"})
        self.assertEqual((entry["start_row"], entry["opening"], entry["date_format"]), (2, "C1", "%m/%d/%y"))
        self.assertEqual(config["accounts"][0]["type"], "savings")
        self.assertIn("adds up as a running balance", "\n".join(report))

    def test_cli_refuses_to_overwrite(self):
        write_ods(self.dir / "wb.ods", {"2025": checking_rows("1.00", [
            (None, "2025-01-03", "x", "0.50", "x", None, None, None)])})
        out = self.dir / "import.json"
        out.write_text("{}")
        cmd = [sys.executable, "-m", "register", "import-template", str(self.dir / "wb.ods"), "--output", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("already exists", r.stderr)
        r = subprocess.run(cmd + ["--force"], capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(out.read_text())["sheets"][0]["source"], "wb.ods")


if __name__ == "__main__":
    unittest.main()
