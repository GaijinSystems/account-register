"""Recurring transactions: the schedule, the stored templates, and entering what is due."""
import datetime
import json
import tempfile
import unittest
from pathlib import Path

from helpers import FAST_N, PASSWORD, git_log, make_home
from register.core import admin, export, importer, recurring
from register.core.ledger import Ledger
from register.core.store import RECURRING_FILE, StoreError, locked


def template(every, start, count=0):
    return {"id": "0123456789abcdef", "account": "everyday", "description": "Rent", "category": "", "amount": "-5.00",
            "note": "", "every": every, "start": start, "count": count}


class ScheduleTest(unittest.TestCase):
    def dates(self, every, start, n):
        return [recurring.occurrence(template(every, start), k).isoformat() for k in range(n)]

    def test_months_keep_the_day_without_drifting(self):
        self.assertEqual(self.dates("month", "2025-01-31", 4), ["2025-01-31", "2025-02-28", "2025-03-31", "2025-04-30"])
        self.assertEqual(self.dates("month", "2023-12-15", 3), ["2023-12-15", "2024-01-15", "2024-02-15"])
        self.assertEqual(self.dates("quarter", "2025-11-30", 3), ["2025-11-30", "2026-02-28", "2026-05-30"])
        self.assertEqual(self.dates("year", "2024-02-29", 5)[1:], ["2025-02-28", "2026-02-28", "2027-02-28", "2028-02-29"])

    def test_weeks(self):
        self.assertEqual(self.dates("week", "2025-12-25", 3), ["2025-12-25", "2026-01-01", "2026-01-08"])
        self.assertEqual(self.dates("2weeks", "2025-02-21", 2), ["2025-02-21", "2025-03-07"])

    def test_due(self):
        t = template("month", "2025-01-31", count=1)
        today = datetime.date(2025, 4, 30)
        self.assertEqual([d.isoformat() for d in recurring.due(t, today)], ["2025-02-28", "2025-03-31", "2025-04-30"])
        self.assertEqual(recurring.due(t, datetime.date(2025, 2, 27)), [])
        self.assertEqual(len(recurring.due(template("week", "1990-01-01"), today)), recurring.MAX_DUE)
        self.assertIsNone(recurring.next_date(template("year", "2199-06-01", count=1)))  # past the last year

    def test_validate(self):
        good = template("month", "2025-01-31")
        self.assertEqual(recurring.validate(good), good)
        for change in ({"every": "day"}, {"start": ""}, {"start": "2025-02-30"}, {"amount": ""}, {"amount": "5"},
                       {"description": " "}, {"count": -1}, {"count": True}, {"id": "x"}, {"note": "a\tb"}):
            with self.assertRaises(Exception, msg=change):
                recurring.validate(dict(good, **change))
        with self.assertRaises(Exception):
            recurring.validate(dict(good, extra=1))


class RecurringTest(unittest.TestCase):
    def setUp(self):
        self.home = make_home(self)
        self.led = Ledger(self.home, self.home.key)
        self.led.today = lambda: datetime.date(2025, 3, 10)
        with locked(self.home):
            self.led.add_account("Everyday", "checking", "1000.00", 2025)
            self.led.add_account("Rainy Day", "savings", "0.00")
        self.rent = self.led.add_recurring("everyday", " Rent ", "-400.00", "month", "2025-02-01",
                                           category="Housing", note="flat 2")["recurring"][0]["id"]

    def test_text_is_normalized_like_row_edits(self):
        got = self.led.add_recurring("everyday", "Cafe\u0301 ", "-3.00", "week", "2025-03-01", category="Fu\u0308n")
        self.assertEqual([(t["description"], t["category"]) for t in got["recurring"]][-1], ("Caf\u00e9", "F\u00fcn"))

    def test_add_and_list(self):
        got = self.led.list_recurring()
        self.assertEqual(got["due"], 2)
        self.assertEqual(got["recurring"], [{
            "id": self.rent, "account": "everyday", "description": "Rent", "category": "Housing",
            "amount": "-400.00", "note": "flat 2", "every": "month", "start": "2025-02-01", "count": 0,
            "name": "Everyday", "next": "2025-02-01", "due": 2}])
        self.assertEqual(git_log(self.home)[0], "add a recurring transaction for everyday")
        raw = (self.home.data / RECURRING_FILE).read_text()
        self.assertNotIn("Rent", raw)  # encrypted like everything else
        for kw, field in (({"account": "nope"}, "account"), ({"amount": ""}, "amount"), ({"every": "daily"}, "every"),
                          ({"start": "soon"}, "start"), ({"description": "  "}, "description")):
            args = dict(account="everyday", description="x", amount="1.00", every="week", start="2025-01-01")
            args.update(kw)
            with self.assertRaises(StoreError, msg=kw) as cm:
                self.led.add_recurring(**args)
            self.assertEqual(cm.exception.field, field, kw)

    def test_enter_what_is_due_in_one_commit(self):
        self.led.add_recurring("rainy-day", "Interest", "1.50", "week", "2025-03-01")
        before = len(git_log(self.home))
        got = self.led.enter_recurring()
        self.assertEqual((got["entered"], got["due"]), (4, 0))
        self.assertEqual((len(git_log(self.home)), git_log(self.home)[0]), (before + 1, "enter 4 recurring transaction(s)"))
        rows = self.led.get_sheet("everyday", 2025)["rows"]
        self.assertEqual([(r["date"], r["description"], r["category"], r["amount"], r["note"], r["cleared"])
                          for r in rows[1:]],
                         [("2025-02-01", "Rent", "Housing", "-400.00", "flat 2", False),
                          ("2025-03-01", "Rent", "Housing", "-400.00", "flat 2", False)])
        self.assertEqual(rows[-1]["balance"], "200.00")
        self.assertEqual([r["date"] for r in self.led.get_sheet("rainy-day", None)["rows"][1:]],
                         ["2025-03-01", "2025-03-08"])
        self.assertEqual([t["next"] for t in got["recurring"]], ["2025-04-01", "2025-03-15"])
        with self.assertRaises(StoreError) as cm:
            self.led.enter_recurring()
        self.assertEqual(cm.exception.message, "nothing is due")
        self.assertEqual(len(git_log(self.home)), before + 1)  # nothing is ever entered twice

    def test_enter_only_some(self):
        other = self.led.add_recurring("rainy-day", "Interest", "1.50", "week", "2025-03-01")["recurring"][1]["id"]
        self.assertEqual(self.led.enter_recurring([other])["due"], 2)  # the rent is still due
        for bad in (["0" * 16], [5]):
            with self.assertRaises(StoreError):
                self.led.enter_recurring(bad)

    def test_year_tabs_are_added_in_order_and_never_before_the_first(self):
        self.led.today = lambda: datetime.date(2027, 1, 5)       # everyday has only 2025
        got = self.led.enter_recurring()
        self.assertEqual(got["entered"], 24)                     # Feb 2025 to Jan 2027
        account = self.led.store.account("everyday")
        self.assertEqual(self.led.store.years(account), [2025, 2026, 2027])
        self.assertEqual([len(self.led.get_sheet("everyday", y)["rows"]) for y in (2025, 2026, 2027)], [12, 12, 1])
        self.led.add_recurring("everyday", "Old", "-1.00", "year", "2024-06-01")
        before = len(git_log(self.home))
        with self.assertRaises(StoreError) as cm:
            self.led.enter_recurring()
        self.assertIn('"Old": Everyday starts in 2025; a row dated 2024-06-01 would come before', cm.exception.message)
        self.assertEqual(len(git_log(self.home)), before)

    def test_skip_and_delete(self):
        got = self.led.skip_recurring(self.rent, count=0)
        with self.assertRaises(StoreError) as cm:                # the same request again: not skipped twice
            self.led.skip_recurring(self.rent, count=0)
        self.assertEqual(cm.exception.code, "stale")
        self.assertEqual((got["recurring"][0]["next"], got["due"]), ("2025-03-01", 1))
        self.assertEqual(len(self.led.get_sheet("everyday", 2025)["rows"]), 1)  # skipping enters nothing
        self.assertEqual(self.led.delete_recurring(self.rent)["recurring"], [])
        self.assertEqual(git_log(self.home)[:2], ["delete a recurring transaction for everyday",
                                                  "skip a recurring transaction for everyday"])
        for op in (self.led.skip_recurring, self.led.delete_recurring):
            with self.assertRaises(StoreError) as cm:
                op(self.rent)
            self.assertEqual(cm.exception.code, "not_found")

    def test_an_out_of_range_balance_enters_nothing(self):
        self.led.add_recurring("everyday", "Too much", "-999999999999.00", "week", "2025-03-01")
        with self.assertRaises(StoreError):
            self.led.enter_recurring()
        self.assertEqual(len(self.led.get_sheet("everyday", 2025)["rows"]), 1)
        self.assertEqual(self.led.list_recurring()["due"], 4)

    def test_password_change_and_history_tools(self):
        admin.change_password(self.home, PASSWORD, "new password", n=FAST_N)
        led = Ledger(self.home, self.home.unlock("new password"))
        self.assertEqual(led.list_recurring()["recurring"][0]["description"], "Rent")
        lines = admin.show(self.home, RECURRING_FILE, None, lambda prompt: "new password")
        self.assertEqual(json.loads(lines[0])["amount"], "-400.00")

    def test_export_and_import_keep_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "export"
            export.export(self.home, self.home.key, out)
            config = json.loads((out / "import.json").read_text())
            self.assertEqual([t["description"] for t in config["recurring"]], ["Rent"])
            fresh = make_home(self)
            log = []
            importer.run_import(fresh, out / "import.json", fresh.key, out=log.append)
            self.assertIn("1 recurring transaction(s)", log)
            led = Ledger(fresh, fresh.key)
            self.assertEqual([{k: t[k] for k in recurring.KEYS} for t in led.list_recurring()["recurring"]],
                             [{k: t[k] for k in recurring.KEYS} for t in self.led.list_recurring()["recurring"]])
            before = len(git_log(fresh))  # importing the same export again adds nothing
            importer.run_import(fresh, out / "import.json", fresh.key, replace=True, out=log.append)
            self.assertEqual((len(git_log(fresh)), len(led.list_recurring()["recurring"])), (before, 1))
            config["recurring"][0].update(account="nope", id="f" * 16)
            (out / "import.json").write_text(json.dumps(config))
            with self.assertRaises(importer.ImportFailed):
                importer.run_import(fresh, out / "import.json", fresh.key, replace=True, out=log.append)


if __name__ == "__main__":
    unittest.main()
