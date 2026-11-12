"""Transfers between accounts: two linked rows, written and changed together."""
import unittest

from helpers import git_log, make_home
from register.core import formats
from register.core.ledger import Ledger
from register.core.store import StoreError, locked


class TransferTest(unittest.TestCase):
    def setUp(self):
        self.home = make_home(self)
        self.led = Ledger(self.home, self.home.key)
        with locked(self.home):
            self.led.add_account("Everyday", "checking", "1000.00", 2025)
            self.led.add_year("everyday")
            self.led.add_account("Rainy Day", "savings", "50.00")
            self.led.add_account("Second", "checking", "0.00", 2026)
        self.commits = len(git_log(self.home))

    def rows(self, account, year=None):
        return self.led.get_sheet(account, year)["rows"]

    def transfer(self, **kw):
        args = dict(from_account="everyday", to_account="rainy-day", date="2025-03-15", amount="200.00")
        args.update(kw)
        return self.led.add_transfer(**args)

    def test_one_commit_two_linked_rows(self):
        res = self.transfer()
        self.assertEqual((res["from"], res["to"]), ({"account": "everyday", "year": 2025},
                                                    {"account": "rainy-day", "year": None}))
        out, back = self.rows("everyday", 2025)[-1], self.rows("rainy-day")[-1]
        self.assertEqual((out["amount"], out["description"], out["category"], out["balance"]),
                         ("-200.00", "Transfer to Rainy Day", "Transfer", "800.00"))
        self.assertEqual((back["amount"], back["description"], back["date"], back["balance"]),
                         ("200.00", "Transfer from Everyday", "2025-03-15", "250.00"))
        self.assertRegex(out["transfer"], r"^[0-9a-f]{16}$")
        self.assertEqual(out["transfer"], back["transfer"])
        log = git_log(self.home)
        self.assertEqual(len(log), self.commits + 1)
        self.assertEqual(log[0], "transfer from everyday/2025 to rainy-day")
        self.assertNotIn("200", log[0])

    def test_own_text_and_the_year_comes_from_the_date(self):
        self.transfer(to_account="second", date="2026-01-02", amount="5.00", description="Float", category="",
                      note="for the trip")
        out, back = self.rows("everyday", 2026)[-1], self.rows("second", 2026)[-1]
        self.assertEqual([(r["description"], r["category"], r["note"]) for r in (out, back)],
                         [("Float", "", "for the trip")] * 2)

    def test_the_next_year_tab_is_added_and_a_repeat_changes_nothing(self):
        link = "0123456789abcdef"
        self.transfer(to_account="second", date="2027-01-02", amount="5.00", id=link)   # both end in 2026
        self.assertEqual(self.led.store.years(self.led.store.account("everyday")), [2025, 2026, 2027])
        self.assertEqual([r["transfer"] for r in self.rows("second", 2027)], [link])
        before = len(git_log(self.home))
        res = self.transfer(to_account="second", date="2027-01-02", amount="5.00", id=link)  # sent again
        self.assertEqual(res["to"], {"account": "second", "year": 2027})
        self.assertEqual((len(git_log(self.home)), len(self.rows("everyday", 2027))), (before, 1))

    def test_an_id_already_in_use_is_refused_unless_it_is_the_same_transfer(self):
        link = "f" * 16
        self.transfer(id=link)                                        # everyday -> rainy-day, 2025-03-15, 200.00
        before = len(git_log(self.home))
        same = self.transfer(id=link)                                 # sent again: the same answer, nothing written
        self.assertEqual((same["from"], same["to"]), ({"account": "everyday", "year": 2025},
                                                      {"account": "rainy-day", "year": None}))
        for kw in ({"amount": "250.00"},                              # corrected after a failure that had gone through
                   {"to_account": "second", "date": "2026-03-15"},    # other accounts
                   {"date": "2026-03-15"},                            # another year
                   {"from_account": "second", "to_account": "rainy-day", "date": "2026-01-02"}):
            with self.assertRaises(StoreError, msg=kw) as cm:
                self.transfer(id=link, **kw)
            self.assertEqual((cm.exception.code, cm.exception.field), ("conflict", "id"), kw)
        self.assertEqual(len(git_log(self.home)), before)
        linked = [r for a, y in (("everyday", 2025), ("everyday", 2026), ("rainy-day", None), ("second", 2026))
                  for r in self.rows(a, y) if r["transfer"] == link]
        self.assertEqual(sorted(r["amount"] for r in linked), ["-200.00", "200.00"])   # still one pair

    def test_a_damaged_sheet_anywhere_stops_transfers_with_an_id(self):
        path = self.home.data / "second" / "2026.jsonl"
        good = path.read_bytes()
        path.write_bytes(good + b"not a row\n")
        from register.core import gitrepo
        gitrepo.add(self.home.data, ["second/2026.jsonl"])
        gitrepo.commit(self.home.data, "damaged by hand")
        with self.assertRaises(StoreError) as cm:
            self.transfer(id="a" * 16)                       # everyday -> rainy-day: neither is the damaged one
        self.assertEqual(cm.exception.code, "corrupt")
        self.assertIn("a transfer cannot be checked against the ones already entered", cm.exception.message)
        self.assertIn("second/2026.jsonl", cm.exception.message)

    def test_refusals_write_nothing(self):
        for kw, field in (({"to_account": "everyday"}, "to_account"), ({"to_account": "nope"}, "account"),
                          ({"amount": "0.00"}, "amount"), ({"amount": "-5.00"}, "amount"), ({"amount": "5"}, "amount"),
                          ({"amount": ""}, "amount"), ({"date": ""}, "date"), ({"date": "03/15/2025"}, "date"),
                          ({"date": "2028-01-01"}, "date"),                       # everyday ends in 2026: a gap
                          ({"to_account": "second", "date": "2025-01-01"}, "date"),  # before second's first year
                          ({"id": "nope"}, "id"),
                          ({"description": "a\tb"}, "description"),
                          ({"amount": "999999999999.99"}, "amount")):               # balance out of range
            with self.assertRaises(StoreError, msg=kw) as cm:
                self.transfer(**kw)
            self.assertEqual(cm.exception.field, field, kw)
        self.assertEqual(len(git_log(self.home)), self.commits)

    def test_editing_amount_or_date_changes_both_sides(self):
        self.transfer()
        s = self.led.get_sheet("rainy-day", None)
        s = self.led.update_row("rainy-day", None, s["rev"], 1, {"amount": "300.00", "date": "2025-03-16"})
        self.assertEqual(s["rows"][1]["balance"], "350.00")
        out = self.rows("everyday", 2025)[-1]
        self.assertEqual((out["amount"], out["date"], out["balance"]), ("-300.00", "2025-03-16", "700.00"))
        self.assertEqual(git_log(self.home)[0],
                         "rainy-day: edit row 2 (amount, date) and the other side of the transfer in everyday/2025")
        # from the other side, and a blank amount
        s = self.led.get_sheet("everyday", 2025)
        self.led.update_row("everyday", 2025, s["rev"], 1, {"amount": ""})
        self.assertEqual(self.rows("rainy-day")[1]["amount"], "")
        self.assertEqual(len(git_log(self.home)), self.commits + 3)

    def test_other_fields_stay_per_side(self):
        self.transfer()
        s = self.led.get_sheet("everyday", 2025)
        self.led.update_row("everyday", 2025, s["rev"], 1, {"description": "Savings", "cleared": True, "note": "n"})
        back = self.rows("rainy-day")[1]
        self.assertEqual((back["description"], back["cleared"], back["note"]), ("Transfer from Everyday", False, ""))
        self.assertEqual(git_log(self.home)[0], "everyday/2025: edit row 2 (cleared, description, note)")

    def test_deleting_one_side_deletes_both(self):
        self.transfer()
        self.transfer(amount="1.00")
        s = self.led.get_sheet("everyday", 2025)
        s = self.led.delete_row("everyday", 2025, s["rev"], 1)
        self.assertEqual([r["amount"] for r in s["rows"]], ["1000.00", "-1.00"])
        self.assertEqual([r["amount"] for r in self.rows("rainy-day")], ["50.00", "1.00"])
        self.assertEqual(git_log(self.home)[0],
                         "everyday/2025: delete row 2 and the other side of the transfer in rainy-day")

    def test_a_side_whose_partner_is_gone_edits_alone(self):
        self.transfer()
        store, acct = self.led.store, self.led.store.account("rainy-day")
        rows, _ = store.read_sheet(acct, None)
        with locked(self.home):  # as if removed by hand with git
            store.write_files({store.relpath(acct, None): (formats.FORMATS["savings"], rows[:1])}, "by hand")
        s = self.led.get_sheet("everyday", 2025)
        s = self.led.update_row("everyday", 2025, s["rev"], 1, {"amount": "-7.00"})
        self.assertEqual(git_log(self.home)[0], "everyday/2025: edit row 2 (amount)")
        self.led.delete_row("everyday", 2025, s["rev"], 1)
        self.assertEqual(len(self.rows("everyday", 2025)), 1)

    def test_suggestions(self):
        self.transfer()
        for i, (date, what, cat, amount) in enumerate((
                ("2025-01-05", "Corner Market", "Food", "-40.00"), ("2025-02-05", "Corner Market", "Food", "-55.10"),
                ("2025-01-09", "Bookshop", "Fun", "-9.00"), ("", "  ", "", ""))):
            s = self.led.get_sheet("everyday", 2025)
            self.led.insert_row("everyday", 2025, s["rev"], len(s["rows"]),
                                {"date": date, "description": what, "category": cat, "amount": amount})
        s = self.led.get_sheet("rainy-day", None)
        self.led.insert_row("rainy-day", None, s["rev"], 2, {"date": "2025-03-01", "description": "Interest",
                                                             "category": "Fun", "amount": "1.00"})
        got = self.led.suggestions()
        self.assertEqual(got["categories"], ["Food", "Fun", "Transfer"])  # most used first, then by name
        self.assertEqual(got["payees"], [  # most recent first; no transfers, blanks or opening balances
            {"description": "Interest", "amount": "1.00", "category": "Fun"},
            {"description": "Corner Market", "amount": "-55.10", "category": "Food"},
            {"description": "Bookshop", "amount": "-9.00", "category": "Fun"}])

    def test_search(self):
        self.transfer()
        for year, fields in ((2025, {"date": "2025-01-05", "description": "Corner Market", "category": "Food",
                                     "amount": "-40.00", "note": "Party"}),
                             (2026, {"date": "2026-02-05", "description": "CORNER market", "amount": "-55.10",
                                     "num": "1077"}),
                             (2026, {"description": "undated", "amount": "25.00"})):
            s = self.led.get_sheet("everyday", year)
            self.led.insert_row("everyday", year, s["rev"], len(s["rows"]), fields)

        def found(**kw):
            res = self.led.search(**kw)
            self.assertFalse(res["more"])
            return [(r["account"], r["year"], r["index"]) for r in res["results"]]

        self.assertEqual(found(account="everyday", text="corner m"), [("everyday", 2025, 2), ("everyday", 2026, 0)])
        self.assertEqual(found(account="everyday", text="party"), [("everyday", 2025, 2)])  # the note
        self.assertEqual(found(account="everyday", text="1077"), [("everyday", 2026, 0)])   # the number
        self.assertEqual(found(text="transfer"), [("everyday", 2025, 1), ("rainy-day", None, 1)])  # all accounts
        self.assertEqual(found(account="everyday", amount_min="25.00", amount_max="55.10"),      # sizes, either sign
                         [("everyday", 2025, 2), ("everyday", 2026, 0), ("everyday", 2026, 1)])
        self.assertEqual(found(account="everyday", date_from="2025-03-15", date_to="2026-02-05"),
                         [("everyday", 2025, 1), ("everyday", 2026, 0)])                           # undated rows left out
        self.assertEqual(found(account="everyday", text="corner", amount_min="50.00"), [("everyday", 2026, 0)])
        self.assertEqual(found(account="rainy-day", text="corner"), [])
        hit = self.led.search(text="party")["results"][0]
        self.assertEqual((hit["name"], hit["description"], hit["amount"], hit["category"]),
                         ("Everyday", "Corner Market", "-40.00", "Food"))
        for kw, field in (({}, "text"), ({"text": "  "}, "text"), ({"amount_min": "5"}, "amount_min"),
                          ({"date_to": "1/2/2025"}, "date_to"), ({"account": "nope", "text": "x"}, "account")):
            with self.assertRaises(StoreError, msg=kw) as cm:
                self.led.search(**kw)
            self.assertEqual(cm.exception.field, field, kw)

    def test_reconcile(self):
        for fields in ({"amount": "-40.00", "cleared": True}, {"amount": "-5.00"}, {"amount": "7.00"}):
            s = self.led.get_sheet("everyday", 2025)
            self.led.insert_row("everyday", 2025, s["rev"], len(s["rows"]), fields)
        s = self.led.get_sheet("everyday", 2025)
        self.assertEqual(s["cleared_before"], "0.00")
        before = len(git_log(self.home))
        s = self.led.reconcile("everyday", 2025, s["rev"], [0, 3], [1])
        self.assertEqual([r["cleared"] for r in s["rows"]], [True, False, False, True])
        self.assertEqual((len(git_log(self.home)), git_log(self.home)[0]),
                         (before + 1, "everyday/2025: reconcile (3 rows)"))
        # the next year starts from what was cleared before it: 1000.00 + 7.00
        self.assertEqual(self.led.get_sheet("everyday", 2026)["cleared_before"], "1007.00")
        self.assertEqual(self.led.get_sheet("rainy-day", None)["cleared_before"], "0.00")
        for cleared, uncleared, field in (([], [], "cleared"), ([9], [], "index"), ([0], [0], "index"),
                                          ([0, 0], [], "index"), (["0"], [], "index"), ([True], [], "index")):
            with self.assertRaises(StoreError, msg=(cleared, uncleared)) as cm:
                self.led.reconcile("everyday", 2025, s["rev"], cleared, uncleared)
            self.assertEqual(cm.exception.field, field)
        with self.assertRaises(StoreError) as cm:
            self.led.reconcile("everyday", 2025, "old", [1])
        self.assertEqual(cm.exception.code, "stale")
        self.assertEqual(len(git_log(self.home)), before + 1)

    def test_report(self):
        self.transfer()                                             # left out: a linked transfer
        rows = (("everyday", 2025, "2025-01-05", "Corner Market", "Food", "-40.00"),
                ("everyday", 2025, "2025-01-20", "Corner Market", "Food", "-60.10"),
                ("everyday", 2025, "2025-02-01", "Corner Market", "Food", "15.00"),      # a refund
                ("everyday", 2025, "2025-02-03", "Paycheck", "Income", "2000.00"),
                ("everyday", 2025, "2025-02-09", "Bookshop", "", "-9.00"),
                ("everyday", 2025, "2025-03-01", "To the card", "transfer", "-500.00"),  # left out: by category
                ("everyday", 2025, "", "Undated", "Food", "-1.00"),                      # left out: no date
                ("everyday", 2026, "2025-12-31", "Late entry", "Fees", "-5.00"),         # counts by its date
                ("everyday", 2026, "2026-01-02", "Corner Market", "Food", "-7.00"),
                ("rainy-day", None, "2025-02-28", "Interest", "Income", "1.25"))
        for account, year, date, what, cat, amount in rows:
            s = self.led.get_sheet(account, year)
            self.led.insert_row(account, year, s["rev"], len(s["rows"]),
                                {"date": date, "description": what, "category": cat, "amount": amount})
        got = self.led.report(2025)
        self.assertEqual((got["year"], got["years"], got["account"]), (2025, [2025, 2026], None))
        self.assertEqual((got["income"], got["spending"], got["net"]), ("2016.25", "114.10", "1902.15"))
        self.assertEqual(got["months"][0], {"month": "2025-01", "income": "0.00", "spending": "100.10", "net": "-100.10"})
        self.assertEqual(got["months"][1], {"month": "2025-02", "income": "2016.25", "spending": "9.00", "net": "2007.25"})
        self.assertEqual(got["months"][11]["spending"], "5.00")
        self.assertEqual(got["categories"], [{"name": "Food", "total": "85.10", "count": 3},   # net of the refund
                                             {"name": "", "total": "9.00", "count": 1},
                                             {"name": "Fees", "total": "5.00", "count": 1}])
        self.assertEqual([(p["name"], p["total"]) for p in got["payees"]],
                         [("Corner Market", "85.10"), ("Bookshop", "9.00"), ("Late entry", "5.00")])
        self.assertEqual(self.led.report()["year"], 2026)            # the latest year with rows
        one = self.led.report(2025, "rainy-day")
        self.assertEqual((one["income"], one["spending"], one["categories"]), ("1.25", "0.00", []))
        self.assertEqual(self.led.report(2031)["years"], [2025, 2026, 2031])
        for kw, field in (({"year": 1800}, "year"), ({"year": True}, "year"), ({"account": "nope"}, "account")):
            with self.assertRaises(StoreError) as cm:
                self.led.report(**kw)
            self.assertEqual(cm.exception.field, field)

    def test_a_refused_edit_changes_neither_side(self):
        self.transfer()
        s = self.led.get_sheet("everyday", 2025)
        with self.assertRaises(StoreError):  # the savings balance would be out of range
            self.led.update_row("everyday", 2025, s["rev"], 1, {"amount": "-999999999999.99"})
        self.assertEqual(self.rows("everyday", 2025)[1]["amount"], "-200.00")
        self.assertEqual(self.rows("rainy-day")[1]["amount"], "200.00")


if __name__ == "__main__":
    unittest.main()
