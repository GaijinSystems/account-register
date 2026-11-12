import json
import re
import unittest
from unittest import mock

from helpers import git_log, make_home
from register.core import formats, gitrepo, store
from register.core.ledger import Ledger
from register.core.store import StoreError, locked


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.home = make_home(self)
        self.ledger = Ledger(self.home, self.home.key)
        with locked(self.home):
            self.ledger.add_account("Checking", "checking", "100.00", 2025)

    def sheet(self, year=2025):
        return self.ledger.get_sheet("checking", year)

    def test_add_account_creates_opening_row(self):
        s = self.sheet()
        self.assertEqual(s["rows"][0]["description"], "Opening balance")
        self.assertEqual(s["rows"][0]["balance"], "100.00")
        self.assertEqual(git_log(self.home)[0], "add account checking (checking)")
        self.assertTrue(self.ledger.status()["repo_clean"])

    def test_an_edit_decrypts_each_year_once(self):
        with locked(self.home):
            self.ledger.add_year("checking")  # 2026
            self.ledger.add_year("checking")  # 2027
        s = self.sheet(2026)
        with mock.patch.object(store, "decode_rows", wraps=store.decode_rows) as decode:
            s = self.ledger.insert_row("checking", 2026, s["rev"], 0, {"amount": "-5.00"})
        read = [c.args[2] for c in decode.call_args_list]
        # 2025 (carried in) and 2027 (carried into) once each. 2026 four times: loading it, reusing
        # its unchanged ciphertext lines, the read-back check of the written file, the new view.
        self.assertEqual(sorted(read), ["checking/2025.jsonl"] + ["checking/2026.jsonl"] * 4
                         + ["checking/2027.jsonl"])
        self.assertEqual((s["opening"], s["rows"][0]["balance"]), ("100.00", "95.00"))
        self.assertIsNone(self.ledger.store._sheets)  # nothing decrypted is kept

    def test_a_damaged_later_year_blocks_earlier_edits_and_says_why(self):
        with locked(self.home):
            self.ledger.add_year("checking")  # 2026
        path = self.home.data / "checking" / "2026.jsonl"
        path.write_text(path.read_text() + "not an encrypted row\n")
        gitrepo._run(self.home.data, "commit", "-qam", "damaged")
        s = self.sheet(2025)
        with self.assertRaises(StoreError) as cm:
            self.ledger.insert_row("checking", 2025, s["rev"], 1, {"amount": "1.00"})
        self.assertEqual(cm.exception.code, "corrupt")
        self.assertIn("edit refused: checking/2026.jsonl line", cm.exception.message)
        self.assertIn("Damaged sheet files", cm.exception.message)
        self.assertEqual(self.sheet(2025)["rev"], s["rev"])  # nothing written

    def test_cached_reads_are_copies(self):
        st = self.ledger.store
        account = st.account("checking")
        with st.cached():
            rows, _ = st.read_sheet(account, 2025)
            rows[0]["amount"] = "1.00"
            rows.append(rows[0])
            self.assertEqual(st.read_sheet(account, 2025)[0][0]["amount"], "100.00")
            self.assertEqual(len(st.read_sheet(account, 2025)[0]), 1)

    def test_edits_commit_and_compute_balances(self):
        s = self.sheet()
        s = self.ledger.insert_row("checking", 2025, s["rev"], 1, {"date": "2025-01-03", "amount": "-84.12"})
        s = self.ledger.update_row("checking", 2025, s["rev"], 1, {"description": "Electric, Co", "cleared": True})
        self.assertEqual([r["balance"] for r in s["rows"]], ["100.00", "15.88"])
        log = git_log(self.home)
        self.assertEqual(log[0], "checking/2025: edit row 2 (cleared, description)")
        self.assertEqual(log[1], "checking/2025: insert row 2")
        s = self.ledger.delete_row("checking", 2025, s["rev"], 1)
        self.assertEqual(len(s["rows"]), 1)
        self.assertEqual(git_log(self.home)[0], "checking/2025: delete row 2")

    def test_file_format_is_encrypted_canonical_rows(self):
        s = self.sheet()
        self.ledger.update_row("checking", 2025, s["rev"], 0, {"note": 'say "hi", ok'})
        rel = "checking/2025.jsonl"
        raw = (self.home.data / rel).read_bytes()
        self.assertNotIn(b"Opening", raw)
        self.assertNotIn(b"100.00", raw)
        for line in raw.decode("ascii").splitlines():
            self.assertEqual(list(json.loads(line)), ["k", "n", "c"])
            self.assertEqual(json.loads(line)["k"], self.home.key.key_id)
        rows, lines = store.decode_rows(formats.FORMATS["checking"], raw, rel, self.home.key, with_lines=True)
        self.assertEqual(lines[0][0], '{"num":"","date":"","description":"Opening balance","category":"",'
                                      '"amount":"100.00","cleared":false,"note":"say \\"hi\\", ok",'
                                      '"transfer":"","bank_id":""}')
        for row in rows:
            self.assertNotIn("balance", row)
            self.assertTrue(re.fullmatch(r"-?\d+\.\d{2}|", row["amount"]))

    def test_link_fields_cannot_be_edited(self):
        s = self.sheet()
        for fields in ({"transfer": "0123456789abcdef"}, {"bank_id": "x"}, {"note": "ok", "transfer": ""}):
            for op in (lambda: self.ledger.update_row("checking", 2025, s["rev"], 0, fields),
                       lambda: self.ledger.insert_row("checking", 2025, s["rev"], 0, fields)):
                with self.assertRaises(StoreError) as cm:
                    op()
                self.assertEqual((cm.exception.code, cm.exception.field), ("invalid", next(iter(
                    f for f in fields if f in formats.LINK_FIELDS))))
        self.assertEqual(len(git_log(self.home)), 2)  # init and the account; nothing since

    def test_balance_cannot_be_written(self):
        s = self.sheet()
        for op in (lambda: self.ledger.update_row("checking", 2025, s["rev"], 0, {"balance": "1.00"}),
                   lambda: self.ledger.insert_row("checking", 2025, s["rev"], 0, {"balance": "1.00"})):
            with self.assertRaises(StoreError) as cm:
                op()
            self.assertEqual(cm.exception.code, "balance_readonly")

    def test_no_op_write_makes_no_commit(self):
        s = self.sheet()
        n = len(git_log(self.home))
        s2 = self.ledger.update_row("checking", 2025, s["rev"], 0, {"description": "Opening balance"})
        self.ledger.rename_account("checking", "Checking")
        self.assertEqual((len(git_log(self.home)), s2["rev"]), (n, s["rev"]))

    def test_sheet_reports_last_saved_time(self):
        s = self.sheet()
        self.assertRegex(s["modified"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:Z|[+-]\d\d:\d\d)$")  # git >= 2.41 writes Z for UTC
        self.assertEqual(s["modified"], gitrepo._run(self.home.data, "log", "-1", "--format=%cI").strip())
        res = self.ledger.add_year("checking")
        self.assertIsNotNone(self.ledger.get_sheet("checking", res["year"])["modified"])

    def test_stale_rev_rejected(self):
        s = self.sheet()
        self.ledger.update_row("checking", 2025, s["rev"], 0, {"note": "a"})
        with self.assertRaises(StoreError) as cm:
            self.ledger.update_row("checking", 2025, s["rev"], 0, {"note": "b"})
        self.assertEqual(cm.exception.code, "stale")

    def test_dirty_repo_refuses_writes_until_committed(self):
        (self.home.data / "stray.txt").write_text("x")
        self.assertFalse(self.ledger.status()["repo_clean"])
        before = (self.home.data / "checking" / "2025.jsonl").read_bytes()
        s = self.sheet()
        self.assertFalse(s["repo_clean"])
        with self.assertRaises(StoreError) as cm:
            self.ledger.update_row("checking", 2025, s["rev"], 0, {"note": "x"})
        self.assertEqual(cm.exception.code, "repo_dirty")
        self.assertEqual((self.home.data / "checking" / "2025.jsonl").read_bytes(), before)
        gitrepo.add(self.home.data, ["stray.txt"])
        gitrepo.commit(self.home.data, "manual")
        self.assertTrue(self.ledger.status()["repo_clean"])
        self.ledger.update_row("checking", 2025, s["rev"], 0, {"note": "x"})

    def test_failed_commit_rolls_back(self):
        s = self.sheet()
        path = self.home.data / "checking" / "2025.jsonl"
        before = path.read_bytes()
        with mock.patch.object(gitrepo, "commit", side_effect=gitrepo.GitError("boom")):
            with self.assertRaises(StoreError) as cm:
                self.ledger.update_row("checking", 2025, s["rev"], 0, {"note": "x"})
        self.assertEqual(cm.exception.code, "write_failed")
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(self.ledger.status()["repo_clean"])

    def test_balance_cannot_exceed_the_storable_range(self):
        big = "999999999899.99"  # with the 100.00 opening row: exactly the maximum
        s = self.ledger.insert_row("checking", 2025, self.sheet()["rev"], 1, {"amount": big})
        with self.assertRaises(StoreError) as cm:
            self.ledger.insert_row("checking", 2025, s["rev"], 2, {"amount": "1.00"})
        self.assertEqual(cm.exception.code, "invalid")
        self.assertIn("checking/2025 row 3", cm.exception.message)
        self.assertEqual(len(self.sheet()["rows"]), 2)  # nothing written
        # an edit in an earlier year may not push a later year out of range either
        with locked(self.home):
            self.ledger.add_year("checking")
        s26 = self.ledger.get_sheet("checking", 2026)
        self.ledger.insert_row("checking", 2026, s26["rev"], 0, {"amount": "-999999999000.00"})
        self.ledger.update_row("checking", 2025, self.sheet()["rev"], 1, {"amount": "-100.00"})
        with self.assertRaises(StoreError) as cm:  # 2025 ends at -1000.00, 2026 would reach -1000000000000.00
            self.ledger.update_row("checking", 2025, self.sheet()["rev"], 1, {"amount": "-1100.00"})
        self.assertIn("checking/2026 row 1", cm.exception.message)

    def test_too_large_stored_balance_still_displays(self):
        # data written before the check existed must still open, so it can be fixed in the UI
        fmt = formats.FORMATS["checking"]
        rows = [dict(fmt.blank_row(), amount="999999999999.99") for _ in range(2)]
        self.ledger.store.write_files({"checking/2025.jsonl": (fmt, rows)}, "big")
        s = self.sheet()
        self.assertEqual(s["rows"][-1]["balance"], "1999999999999.98")
        # edits that don't make it worse are allowed; making it worse is not
        s = self.ledger.update_row("checking", 2025, s["rev"], 0, {"cleared": True, "note": "fixing"})
        with self.assertRaises(StoreError):
            self.ledger.insert_row("checking", 2025, s["rev"], 2, {"amount": "1.00"})
        s = self.ledger.update_row("checking", 2025, s["rev"], 1, {"amount": "999999999999.00"})  # smaller
        self.ledger.delete_row("checking", 2025, s["rev"], 1)  # and the fix is allowed
        self.assertEqual(self.sheet()["rows"][-1]["balance"], "999999999999.99")

    def test_out_of_range_rules_for_old_data(self):
        fmt = formats.FORMATS["checking"]
        big = dict(fmt.blank_row(), amount="999999999999.99")
        self.ledger.store.write_files({"checking/2025.jsonl": (fmt, [big, big])}, "old data")  # ends ~2T
        with locked(self.home):
            self.ledger.add_year("checking")
        s = self.ledger.get_sheet("checking", 2026)
        # carried in out of range: a withdrawal in the new year moves toward range, so it's allowed
        s = self.ledger.insert_row("checking", 2026, s["rev"], 0, {"amount": "-1.00"})
        with self.assertRaises(StoreError):  # a deposit taking it past the old peak goes further out
            self.ledger.insert_row("checking", 2026, s["rev"], 1, {"amount": "2.00"})
        # no new out-of-range balance of the other sign
        s = self.ledger.update_row("checking", 2026, s["rev"], 0, {"amount": "-999999999999.99"})
        self.assertEqual(s["rows"][-1]["balance"], "999999999999.99")  # back in range
        s = self.ledger.insert_row("checking", 2026, s["rev"], 1, {"amount": "-999999999999.99"})
        s = self.ledger.insert_row("checking", 2026, s["rev"], 2, {"amount": "-999999999999.99"})
        self.assertEqual(s["rows"][-1]["balance"], "-999999999999.99")  # exactly -MAX is fine
        with self.assertRaises(StoreError):  # but not beyond: the old +2T peak doesn't allow -2T
            self.ledger.insert_row("checking", 2026, s["rev"], 3, {"amount": "-0.01"})

    def test_new_account_check_reads_nothing(self):
        account = {"id": "fresh", "name": "Fresh", "type": "checking"}
        fmt = formats.FORMATS["checking"]
        with mock.patch.object(self.ledger.store, "read_sheet", side_effect=AssertionError("read")), \
                mock.patch.object(self.ledger.store, "years", side_effect=AssertionError("listed")):
            self.ledger.store.check_balances(account, {2025: [fmt.opening_row("5.00")]}, new_account=True)
            with self.assertRaises(StoreError):
                self.ledger.store.check_balances(
                    account, {2025: [fmt.opening_row("999999999999.99"), fmt.opening_row("1.00")]},
                    new_account=True)

    def test_failed_new_account_leaves_no_directory(self):
        with mock.patch.object(gitrepo, "commit", side_effect=gitrepo.GitError("boom")):
            with self.assertRaises(StoreError):
                self.ledger.add_account("Savings", "savings", "", None)
        self.assertFalse((self.home.data / "savings").exists())
        self.assertTrue(self.ledger.status()["repo_clean"])
        self.ledger.add_account("Savings", "savings", "", None)  # a retry is not blocked

    def test_failed_read_back_changes_nothing(self):
        s = self.sheet()
        path = self.home.data / "checking" / "2025.jsonl"
        before = path.read_bytes()
        real = store.decode_rows
        calls = []

        def corrupt_read_back(fmt, data, rel, key, with_lines=False):
            calls.append(with_lines)
            if len(calls) == 3:  # 1st: load, 2nd: ciphertext reuse map, 3rd: read-back
                return []
            return real(fmt, data, rel, key, with_lines)

        with mock.patch("register.core.store.decode_rows", side_effect=corrupt_read_back):
            with self.assertRaises(StoreError) as cm:
                self.ledger.update_row("checking", 2025, s["rev"], 0, {"note": "x"})
        self.assertEqual(len(calls), 3)
        self.assertIn("read-back", cm.exception.message)
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(self.ledger.status()["repo_clean"])
        self.assertEqual(list(path.parent.glob(".tmp-*")), [])

    def test_years_chain_and_add_year(self):
        s = self.sheet()
        self.ledger.insert_row("checking", 2025, s["rev"], 1, {"amount": "-30.00"})
        res = self.ledger.add_year("checking")
        self.assertEqual(res["year"], 2026)
        s26 = self.ledger.get_sheet("checking", 2026)
        self.assertEqual((s26["carried_from"], s26["opening"], s26["rows"]), (2025, "70.00", []))
        s26 = self.ledger.insert_row("checking", 2026, s26["rev"], 0, {"amount": "5.00"})
        self.assertEqual(s26["rows"][0]["balance"], "75.00")
        self.assertEqual(self.ledger.add_year("checking")["year"], 2027)

    def test_savings_and_rename(self):
        res = self.ledger.add_account("Money Market", "savings", "", None)
        self.assertEqual(res["account"], "money-market")
        s = self.ledger.get_sheet("money-market", None)
        self.assertEqual((s["fields"], s["rows"][0]["amount"]), (list(formats.FIELDS), "0.00"))
        s = self.ledger.update_row("money-market", None, s["rev"], 0, {"num": "7", "cleared": True, "category": "Start"})
        self.assertEqual([s["rows"][0][f] for f in ("num", "cleared", "category")], ["7", True, "Start"])
        with self.assertRaises(StoreError):
            self.ledger.get_sheet("money-market", 2025)
        with self.assertRaises(StoreError):
            self.ledger.add_year("money-market")
        self.ledger.rename_account("money-market", "Ally HYSA")
        self.assertEqual([a["name"] for a in self.ledger.list_tabs()["accounts"]], ["Checking", "Ally HYSA"])
        self.assertEqual(self.ledger.add_account("Checking", "checking", "", 2025)["account"], "checking-2")

    def test_account_types_and_the_owed_setting(self):
        led = self.ledger
        led.add_account("Visa", "card", "-250.00", 2025)
        led.add_account("Wallet", "cash", "40.00")
        led.add_account("Car loan", "loan", "-9000.00")
        tabs = {a["id"]: a for a in led.list_tabs()["accounts"]}
        self.assertEqual({k: (a["type"], a["owed_positive"], a["years"]) for k, a in tabs.items()}, {
            "checking": ("checking", False, [2025]), "visa": ("card", True, [2025]),
            "wallet": ("cash", False, None), "car-loan": ("loan", True, None)})
        # amounts are stored as their effect on what you have, whatever the display setting
        s = led.get_sheet("visa", 2025)
        self.assertEqual((s["owed_positive"], s["rows"][0]["amount"], s["rows"][0]["balance"]),
                         (True, "-250.00", "-250.00"))
        s = led.insert_row("visa", 2025, s["rev"], 1, {"amount": "-30.00", "description": "Coffee"})
        self.assertEqual(s["rows"][1]["balance"], "-280.00")
        self.assertEqual(led.add_year("visa")["year"], 2026)
        with self.assertRaises(StoreError):
            led.add_year("car-loan")
        with self.assertRaises(StoreError) as cm:
            led.add_account("X", "mortgage", "")
        self.assertIn("checking, savings, card, cash, loan", cm.exception.message)
        self.assertTrue(led.add_account("Old card", "card", "", 2025, owed_positive=False)["account"])
        self.assertFalse(led.get_sheet("old-card", 2025)["owed_positive"])
        # the setting is changed with the name; the rows are untouched
        before = (self.home.data / "visa" / "2025.jsonl").read_bytes()
        tabs = led.rename_account("visa", "Visa", owed_positive=False)
        self.assertFalse(next(a for a in tabs["accounts"] if a["id"] == "visa")["owed_positive"])
        self.assertEqual(git_log(self.home)[0], "change the settings of account visa")
        self.assertEqual((self.home.data / "visa" / "2025.jsonl").read_bytes(), before)
        led.rename_account("visa", "Visa Gold")  # a plain rename keeps the setting
        self.assertEqual(git_log(self.home)[0], "rename account visa")
        self.assertFalse(led.get_sheet("visa", 2025)["owed_positive"])
        led.add_transfer("checking", "visa", "2025-02-01", "100.00")  # paying the card
        self.assertEqual(led.get_sheet("visa", 2025)["rows"][-1]["balance"], "-180.00")

    def test_account_types_are_listed_once(self):
        from register import __main__ as cli
        self.assertEqual(cli.ACCOUNT_TYPES, list(formats.FORMATS))  # the CLI's copy (formats is imported lazily)
        types = {t["id"]: t for t in self.ledger.list_tabs()["types"]}
        self.assertEqual(list(types), list(formats.FORMATS))
        self.assertEqual(types["card"], {"id": "card", "label": "Credit card", "yearly": True,
                                         "owed_positive": True, "debit_mode": True})
        self.assertEqual((types["savings"]["yearly"], types["loan"]["owed_positive"], types["cash"]["debit_mode"]),
                         (False, True, False))

    def test_bad_ids_and_indexes(self):
        s = self.sheet()
        for account in ("../etc", "Checking", "", "a/b"):
            with self.assertRaises(StoreError):
                self.ledger.get_sheet(account, 2025)
        for index in (-1, 5, True, "0"):
            with self.assertRaises(StoreError):
                self.ledger.update_row("checking", 2025, s["rev"], index, {"note": "x"})

    def test_corrupt_file_detected(self):
        path = self.home.data / "checking" / "2025.jsonl"
        path.write_text('{"date":"","num":"","description":"","amount":"1.00","cleared":false,"note":""}\n')
        gitrepo.add(self.home.data, ["checking/2025.jsonl"])
        gitrepo.commit(self.home.data, "hand edit")
        with self.assertRaises(StoreError) as cm:
            self.sheet()
        self.assertEqual(cm.exception.code, "corrupt")


if __name__ == "__main__":
    unittest.main()
