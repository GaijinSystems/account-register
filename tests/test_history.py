"""The change log read from git, and undo."""
import unittest

from helpers import FAST_N, PASSWORD, git_log, make_home
from register.core import admin
from register.core.ledger import Ledger
from register.core.store import StoreError, locked


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.home = make_home(self)
        self.led = Ledger(self.home, self.home.key)
        with locked(self.home):
            self.led.add_account("Everyday", "checking", "100.00", 2025)
            self.led.add_account("Rainy Day", "savings", "0.00")
        for what, amount in (("Coffee", "-4.00"), ("Books", "-9.00"), ("Pay", "50.00")):
            self.add(description=what, amount=amount, date="2025-01-05")

    def sheet(self):
        return self.led.get_sheet("everyday", 2025)

    def add(self, **fields):
        s = self.sheet()
        return self.led.insert_row("everyday", 2025, s["rev"], len(s["rows"]), fields)

    def edit(self, index, **fields):
        return self.led.update_row("everyday", 2025, self.sheet()["rev"], index, fields)

    def entries(self):
        return self.led.history("everyday", 2025)["entries"]

    def undo(self, entry):
        return self.led.undo("everyday", 2025, self.sheet()["rev"], entry["commit"])

    def what(self):
        return [(r["description"], r["amount"]) for r in self.sheet()["rows"]]

    def test_the_log_lists_each_change_with_its_rows(self):
        self.edit(1, amount="-4.50", note="large")
        self.led.delete_row("everyday", 2025, self.sheet()["rev"], 2)
        got = self.entries()
        self.assertEqual([e["subject"] for e in got], [
            "everyday/2025: delete row 3", "everyday/2025: edit row 2 (amount, note)", "everyday/2025: insert row 4",
            "everyday/2025: insert row 3", "everyday/2025: insert row 2", "add account everyday (checking)"])
        deleted, changed = got[0]["changes"][0], got[1]["changes"][0]
        self.assertEqual((deleted["kind"], deleted["row"], deleted["before"]["description"], deleted["after"]),
                         ("deleted", 3, "Books", None))
        self.assertEqual((changed["kind"], changed["row"], changed["before"]["amount"], changed["after"]["amount"],
                          changed["after"]["note"]), ("changed", 2, "-4.00", "-4.50", "large"))
        self.assertEqual((got[2]["changes"][0]["kind"], got[2]["changes"][0]["after"]["description"]), ("added", "Pay"))
        self.assertRegex(got[0]["date"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d([+-]\d\d:\d\d|Z)$")
        # the rows that the two older inserts added have since been edited and deleted
        self.assertEqual([e["undo"] for e in got[:5]], [True, True, True, False, False])
        self.assertEqual((got[5]["undo"], got[5]["why"]),
                         (False, "it changed more than this sheet (a transfer, an import or the like)"))
        self.assertEqual(len(self.led.history("everyday", 2025, 2)["entries"]), 2)
        self.assertTrue(self.led.history("everyday", 2025, 2)["more"])
        for bad in (0, 201, "5", True):
            with self.assertRaises(StoreError):
                self.led.history("everyday", 2025, bad)

    def test_undo_an_edit_a_delete_and_an_insert(self):
        self.edit(1, amount="-4.50")
        self.led.delete_row("everyday", 2025, self.sheet()["rev"], 2)
        before = len(git_log(self.home))
        got = self.entries()
        s = self.undo(got[1])                                   # the edit, although a delete came after it
        self.assertEqual(s["rows"][1]["amount"], "-4.00")
        self.assertEqual((len(git_log(self.home)), git_log(self.home)[0]),
                         (before + 1, "everyday/2025: undo %s" % got[1]["commit"][:10]))
        self.undo(got[0])                                       # the delete: back under the row it followed
        self.assertEqual(self.what(), [("Opening balance", "100.00"), ("Coffee", "-4.00"), ("Books", "-9.00"),
                                       ("Pay", "50.00")])
        self.undo(got[2])                                       # the insert of Pay
        self.assertEqual(self.what()[-1], ("Books", "-9.00"))
        self.assertEqual(self.sheet()["rows"][-1]["balance"], "87.00")
        redo = self.entries()[0]                                # an undo is a change like any other
        self.assertEqual(redo["changes"][0]["kind"], "deleted")
        self.undo(redo)
        self.assertEqual(self.what()[-1], ("Pay", "50.00"))

    def test_a_later_change_to_the_same_row_blocks_the_undo(self):
        self.edit(1, amount="-4.50")
        first = self.entries()[0]
        self.edit(1, note="again")
        entry = next(e for e in self.entries() if e["commit"] == first["commit"])
        self.assertFalse(entry["undo"])
        self.assertIn("a later change touched the same row", entry["why"])
        before = len(git_log(self.home))
        with self.assertRaises(StoreError) as cm:
            self.undo(entry)
        self.assertEqual(cm.exception.code, "conflict")
        self.assertEqual(len(git_log(self.home)), before)
        self.undo(self.entries()[0])     # take back the later one first...
        self.undo(first)                 # ...and then this works
        self.assertEqual(self.sheet()["rows"][1]["amount"], "-4.00")

    def test_a_deleted_row_goes_back_between_its_neighbours_or_not_at_all(self):
        self.led.delete_row("everyday", 2025, self.sheet()["rev"], 2)           # Books, between Coffee and Pay
        deleted = self.entries()[0]
        for n in range(3):                                                      # rows pile up above
            self.led.insert_row("everyday", 2025, self.sheet()["rev"], 1, {"description": "New %d" % n})
        self.edit(4, note="changed")                                            # Coffee, the row it followed
        entry = next(e for e in self.entries() if e["commit"] == deleted["commit"])
        self.assertTrue(entry["undo"])                                          # Pay, the row after it, is intact
        self.undo(entry)
        self.assertEqual([d for d, _ in self.what()], ["Opening balance", "New 2", "New 1", "New 0", "Coffee",
                                                       "Books", "Pay"])
        self.led.delete_row("everyday", 2025, self.sheet()["rev"], 5)           # Books again
        deleted = self.entries()[0]
        self.edit(4, note="again")
        self.edit(5, note="and Pay too")                                        # now both neighbours changed
        entry = next(e for e in self.entries() if e["commit"] == deleted["commit"])
        self.assertFalse(entry["undo"])
        with self.assertRaises(StoreError) as cm:
            self.undo(entry)
        self.assertEqual(cm.exception.code, "conflict")

    def test_a_missing_sheet_file_is_not_found(self):
        from register.core import gitrepo
        (self.home.data / "rainy-day" / "register.jsonl").unlink()
        gitrepo.add(self.home.data, ["rainy-day/register.jsonl"])
        gitrepo.commit(self.home.data, "removed by hand")
        with self.assertRaises(StoreError) as cm:
            self.led.history("rainy-day", None)
        self.assertEqual(cm.exception.code, "not_found")

    def test_a_damaged_old_version_does_not_break_the_list(self):
        from register.core import gitrepo
        path = self.home.data / "everyday" / "2025.jsonl"
        good = path.read_bytes()
        path.write_bytes(good + b"not a row\n")                                # committed by hand, then repaired
        gitrepo.add(self.home.data, ["everyday/2025.jsonl"])
        gitrepo.commit(self.home.data, "by hand")
        path.write_bytes(good)
        gitrepo.add(self.home.data, ["everyday/2025.jsonl"])
        gitrepo.commit(self.home.data, "repaired")
        got = self.entries()
        self.assertEqual([(e["subject"], e["undo"]) for e in got[:2]], [("repaired", False), ("by hand", False)])
        self.assertIn("damaged", got[0]["why"])
        self.assertTrue(got[2]["changes"])                                      # the older entries still show

    def test_rows_added_elsewhere_do_not_matter(self):
        self.edit(3, note="x")
        target = self.entries()[0]
        self.led.insert_row("everyday", 2025, self.sheet()["rev"], 1, {"description": "New at the top"})
        self.led.delete_row("everyday", 2025, self.sheet()["rev"], 2)   # Coffee, above the edited row
        self.undo(target)
        self.assertEqual([r["note"] for r in self.sheet()["rows"]], ["", "", "", ""])

    def test_transfers_and_other_sheets_are_not_undone_here(self):
        self.led.add_transfer("everyday", "rainy-day", "2025-02-01", "10.00")
        entry = self.entries()[0]
        self.assertEqual((entry["undo"], entry["changes"][0]["kind"]), (False, "added"))
        with self.assertRaises(StoreError) as cm:
            self.undo(entry)
        self.assertIn("it changed more than this sheet", cm.exception.message)
        for commit in ("nope", "0" * 40, self.led.history("rainy-day", None)["entries"][-1]["commit"]):
            with self.assertRaises(StoreError) as cm:
                self.led.undo("everyday", 2025, self.sheet()["rev"], commit)
            self.assertEqual(cm.exception.code, "not_found")
        with self.assertRaises(StoreError) as cm:
            self.led.undo("everyday", 2025, "old", entry["commit"])
        self.assertEqual(cm.exception.code, "stale")

    def test_changes_from_before_a_password_change_are_not_shown(self):
        admin.change_password(self.home, PASSWORD, "new password", n=FAST_N)
        led = Ledger(self.home, self.home.unlock("new password"))
        s = led.get_sheet("everyday", 2025)
        led.update_row("everyday", 2025, s["rev"], 1, {"note": "after"})
        got = led.history("everyday", 2025)["entries"]
        self.assertEqual((got[0]["undo"], got[0]["changes"][0]["after"]["note"]), (True, "after"))
        self.assertEqual(got[1]["subject"], "change password (re-encrypt all data)")
        for entry in got[1:]:
            self.assertEqual((entry["undo"], entry["changes"]), (False, []))
        self.assertIn("from before the password was changed", got[2]["why"])
        with self.assertRaises(StoreError):
            led.undo("everyday", 2025, led.get_sheet("everyday", 2025)["rev"], got[2]["commit"])


if __name__ == "__main__":
    unittest.main()
