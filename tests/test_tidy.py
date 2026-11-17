"""tidy-names: merging spellings of one payee or category, and the command that asks about it."""
import collections
import contextlib
import socket
import unittest
from unittest import mock

from helpers import git_log, make_home, run_cli

from register import __main__ as cli
from register.core import store, tidy
from register.core.ledger import Ledger
from register.core.store import Store, locked

YEAR = 2025


def names_of(*spec):
    """{text: Name} from (text, rows) pairs, for the grouping tests."""
    return {t: tidy.Name(t, rows=n, first="2025-01-01", last="2025-02-01", accounts={"a"}) for t, n in spec}


def texts(groups):
    return [sorted(n.text for n in g.names) for g in groups]


class KeyTest(unittest.TestCase):
    def test_case_spaces_and_punctuation_are_ignored(self):
        for text in ("DD - Pizza Hut", "dd - pizza hut", "DD -- pizza hut", "  DD_Pizza*Hut. "):
            self.assertEqual(tidy.key(text), "dd pizza hut", text)

    def test_unicode(self):
        self.assertEqual(tidy.key("ＣＡＦÉ  Crème"), "café crème")   # full-width letters, accents kept
        self.assertEqual(tidy.key("Straße"), "strasse")               # casefold, not lower
        self.assertEqual(tidy.key("Store 12"), "store 12")            # digits stay

    def test_punctuation_only_has_no_key(self):
        self.assertEqual(tidy.key(" -- ** "), "")


class GroupingTest(unittest.TestCase):
    def test_exact_groups_need_two_spellings_and_are_largest_first(self):
        names = names_of(("DD - Pizza Hut", 36), ("dd - pizza hut", 4), ("Gas", 3), ("GAS", 9), ("Rent", 12))
        groups = tidy.exact_groups(names)
        self.assertEqual(texts(groups), [["DD - Pizza Hut", "dd - pizza hut"], ["GAS", "Gas"]])
        self.assertEqual([n.text for n in groups[0].names], ["DD - Pizza Hut", "dd - pizza hut"])  # most rows first

    def test_numbers(self):
        names = names_of(("Pizza Hut #4412", 3), ("Pizza Hut 0093", 2), ("Pizza Hut", 9), ("Store", 1))
        self.assertEqual(texts(tidy.candidate_groups(names, "numbers")),
                         [["Pizza Hut", "Pizza Hut #4412", "Pizza Hut 0093"]])

    def test_order(self):
        names = names_of(("Hut Pizza DD", 1), ("DD Pizza Hut", 5), ("Pizza", 2))
        self.assertEqual(texts(tidy.candidate_groups(names, "order")), [["DD Pizza Hut", "Hut Pizza DD"]])

    def test_contains_is_whole_words_at_the_start(self):
        names = names_of(("Shell", 4), ("Shell Oil", 3), ("Shell Oil Co", 1), ("Shellfish Shack", 2),
                         ("Oil Shell", 1))
        self.assertEqual(texts(tidy.candidate_groups(names, "contains")), [["Shell", "Shell Oil", "Shell Oil Co"]])

    def test_typos(self):
        names = names_of(("Walgreens", 51), ("Wallgreens", 6), ("Target", 3), ("Ford", 2), ("Fort", 1))
        self.assertEqual(texts(tidy.candidate_groups(names, "typos")), [["Walgreens", "Wallgreens"]])
        # four letters are too few to be matched, whatever the threshold
        self.assertEqual(texts(tidy.candidate_groups(names, "typos", 0.70)), [["Walgreens", "Wallgreens"]])
        self.assertEqual(tidy.candidate_groups(names, "typos", 0.99), [])
        # five are enough, and give the false matches the threshold is a trade-off against
        self.assertEqual(texts(tidy.candidate_groups(names_of(("Chase", 2), ("Chaser", 1)), "typos")),
                         [["Chase", "Chaser"]])

    def test_typos_are_misspellings_within_words(self):
        names = names_of(("MO income tax payment", 4), ("US income tax payment", 9),
                         ("US federal income tax payment", 2), ("MO federal incometax payment", 1),
                         ("Piza Hut", 1), ("Pizza Hut", 9), ("PizzaHut", 2),
                         ("Wallgreens Pharmacy", 1), ("Walgreens Pharmacy", 3), ("Walgreens US Pharmacy", 2),
                         ("Wal Mart Supercenter", 1), ("WalMart Super Center", 4))
        self.assertEqual(texts(tidy.candidate_groups(names, "typos")),
                         [["Piza Hut", "Pizza Hut", "PizzaHut"], ["Wal Mart Supercenter", "WalMart Super Center"],
                          ["Walgreens Pharmacy", "Wallgreens Pharmacy"]])
        # close overall, but a word replaced or added: not a typo, at any threshold
        for x, y in (("MO income tax payment", "US income tax payment"),
                     ("US federal income tax payment", "MO federal incometax payment"),
                     ("Walgreens Pharmacy", "Walgreens US Pharmacy")):
            self.assertGreaterEqual(tidy.difflib.SequenceMatcher(None, tidy.key(x), tidy.key(y)).ratio(), 0.88)
            for threshold in (0.70, 0.88):
                self.assertFalse(any({x, y} <= set(g) for g in texts(tidy.candidate_groups(names, "typos", threshold))),
                                 (x, y, threshold))

    def test_typos_join_through_each_other(self):
        names = names_of(("Walgreens", 5), ("Wallgreens", 2), ("Wallgreen", 1))
        self.assertEqual(texts(tidy.candidate_groups(names, "typos")), [["Walgreens", "Wallgreen", "Wallgreens"]])

    def test_a_pair_is_offered_once_across_kinds(self):
        names = names_of(("Pizza Hut", 9), ("Pizza Hut 12", 2))   # both "numbers" and "contains" find it
        offered = set()
        self.assertEqual(len(tidy.candidate_groups(names, "numbers", offered=offered)), 1)
        self.assertEqual(tidy.candidate_groups(names, "contains", offered=offered), [])

    def test_candidates_leave_exact_groups_alone(self):
        names = names_of(("Gas", 3), ("GAS", 9))   # one key: an exact group, not a candidate
        for kind in tidy.CANDIDATE_KINDS:
            self.assertEqual(tidy.candidate_groups(names, kind), [], kind)

    def test_search(self):
        names = names_of(("Pizza Hut #4412", 3), ("DD - Pizza Hut", 9), ("Pizzeria", 1), ("Piza Hut", 1))
        self.assertEqual([n.text for n in tidy.search(names, "pizza hut")], ["DD - Pizza Hut", "Pizza Hut #4412"])
        self.assertEqual([n.text for n in tidy.search(names, "pizza hut", 0.88)],
                         ["DD - Pizza Hut", "Pizza Hut #4412", "Piza Hut"])
        self.assertEqual(tidy.search(names, " -- "), [])


class PlanTest(unittest.TestCase):
    def test_merges_follow_each_other(self):
        plan = tidy.Plan(names_of(("A x", 1), ("a x", 2), ("B", 3)))
        plan.merge(["A x", "a x"], "a x")
        plan.merge(["a x", "B"], "B")
        self.assertEqual(plan.into, {"A x": "B", "a x": "B"})
        self.assertEqual(plan.names["B"].rows, 6)
        with self.assertRaises(ValueError):   # already merged away in this run
            plan.merge(["B"], "a x")

    def test_a_typed_spelling_merged_again(self):
        plan = tidy.Plan(names_of(("A", 1), ("a", 2), ("C", 3)))
        plan.merge(["A", "a"], "X")        # typed: in no row
        plan.merge(["X", "C"], "C")
        self.assertEqual(plan.into, {"A": "C", "a": "C"})
        self.assertEqual(plan.names["C"].rows, 6)

    def test_typed_spelling_is_validated(self):
        self.assertEqual(tidy.check_spelling("description", "  Pizza Hut "), "Pizza Hut")
        for bad in ("x" * 201, "Pizza​Hut", "--"):
            with self.assertRaises(Exception, msg=bad):
                tidy.check_spelling("description", bad)


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.home = make_home(self)
        led = self.led = Ledger(self.home, self.home.key)
        led.add_account("Everyday", "checking", "500.00", year=YEAR)
        led.add_account("Visa", "card", "0.00", year=YEAR)
        self.add("everyday", "2025-01-03", "DD - Pizza Hut", "-12.00", "Food")
        self.add("everyday", "2025-02-07", "dd - pizza hut", "-9.00", "food")
        self.add("visa", "2025-03-01", "DD -- pizza hut", "-11.00", "Food")
        self.add("everyday", "2025-03-02", "Walgreens", "-5.00", "Health")
        self.add("everyday", "2025-03-09", "Wallgreens", "-6.00", "Health")
        led.add_transfer("everyday", "visa", "2025-04-01", "50.00", description="dd - pizza hut")
        led.add_recurring("everyday", "dd - pizza hut", "-10.00", "month", "2026-01-15", category="food")
        self.commits = len(git_log(self.home))

    def add(self, account, date, description, amount, category=""):
        sheet = self.led.get_sheet(account, YEAR)
        self.led.insert_row(account, YEAR, sheet["rev"], len(sheet["rows"]),
                            {"date": date, "description": description, "amount": amount, "category": category})

    def run_cli(self, *argv, answers=(), tty=True):
        """Run the command with these answers typed, then the end of input (as Ctrl-D)."""
        typed = iter(answers)

        def answer(prompt=""):
            print(prompt, end="")
            try:
                return next(typed)
            except StopIteration:
                raise EOFError from None
        return run_cli(self.home, "tidy-names", *argv, tty=tty, input=answer)

    def rows(self, account, field="description"):
        return [r[field] for r in self.led.get_sheet(account, YEAR)["rows"]]

    def test_merging_one_group(self):
        code, out, _ = self.run_cli(answers=["1", "n", "y"])
        self.assertEqual(code, 0, out)
        self.assertIn("Group 1 of 1: 3 rows in 2 accounts", out)
        self.assertIn("dd - pizza hut -> DD - Pizza Hut   (1 row)", out)
        self.assertIn("2 rows in 2 sheets, and 1 recurring template, in one commit.", out)
        self.assertIn("Note: importing an old bank file again", out)
        everyday, visa = self.rows("everyday"), self.rows("visa")
        self.assertEqual(everyday.count("DD - Pizza Hut"), 1 + 1)
        self.assertEqual(visa.count("DD - Pizza Hut"), 1)
        self.assertIn("Opening balance", everyday)
        # the transfer's two rows keep their own text; the template follows the merge
        transfer_rows = [r for a in ("everyday", "visa") for r in self.led.get_sheet(a, YEAR)["rows"] if r["transfer"]]
        self.assertEqual({r["description"] for r in transfer_rows}, {"dd - pizza hut"})
        self.assertEqual([t["description"] for t in self.led.list_recurring()["recurring"]], ["DD - Pizza Hut"])
        log = git_log(self.home)
        self.assertEqual(len(log), self.commits + 1)
        self.assertEqual(log[0], "Tidy names: 2 names merged, 2 rows in 2 sheets")
        self.assertNotIn("pizza", log[0].casefold())

    def test_typos_are_offered_only_on_request(self):
        code, out, _ = self.run_cli(answers=["s"])
        self.assertNotIn("Walgreens", out)
        self.assertIn("Nothing to change; nothing was written.", out)
        code, out, _ = self.run_cli("--fuzzy", "typos", answers=["s", "a", "1", "n", "y"])
        self.assertEqual(code, 0, out)
        self.assertIn("Similar names (typos), 1 of 1: 2 rows in 1 account", out)
        self.assertEqual(self.rows("everyday").count("Walgreens"), 2)

    def test_a_candidate_group_merges_only_the_chosen_names(self):
        self.add("everyday", "2025-05-01", "Walgreens Pharmacy", "-3.00")
        self.add("everyday", "2025-05-02", "Walgreens Photo", "-4.00")
        # exact group: skip; "contains" offers Walgreens, Walgreens Pharmacy and Walgreens Photo:
        # the first two are the same, keep the first; "typos" offers Walgreens and Wallgreens: skip
        code, out, _ = self.run_cli("--fuzzy", "contains,typos", answers=["s", "1 2", "1", "s", "n", "y"])
        self.assertEqual(code, 0, out)
        self.assertIn("Similar names (contains), 1 of 1: 3 rows in 1 account", out)
        everyday = self.rows("everyday")
        self.assertEqual(everyday.count("Walgreens"), 2)
        self.assertNotIn("Walgreens Pharmacy", everyday)
        self.assertIn("Walgreens Photo", everyday)    # offered, not chosen
        self.assertIn("Wallgreens", everyday)         # skipped

    def test_typing_a_new_spelling(self):
        code, out, _ = self.run_cli(answers=["t", "Pizza Hut", "y", "n", "y"])
        self.assertEqual(code, 0, out)
        self.assertIn("that is a new name, not another spelling of these", out)
        self.assertEqual(self.rows("visa").count("Pizza Hut"), 1)

    def test_categories(self):
        code, out, _ = self.run_cli("--field", "category", answers=["1", "n", "y"])
        self.assertEqual(code, 0, out)
        self.assertNotIn("Note: importing", out)
        self.assertEqual(self.rows("everyday", "category").count("Food"), 2)
        self.assertEqual([t["category"] for t in self.led.list_recurring()["recurring"]], ["Food"])

    def test_merging_by_hand(self):
        code, out, _ = self.run_cli(answers=["s", "y", "green", "a", "2", "n", "y"])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.rows("everyday").count("Wallgreens"), 2)

    def test_nothing_is_written_unless_confirmed(self):
        for answers in (["1", "n"], ["1", ""], ["s", "n"], ["q"], []):   # [] is the end of input at once
            code, out, _ = self.run_cli(answers=answers)
            self.assertEqual(code, 0, out)
            self.assertIn("othing was written", out)
            self.assertEqual(len(git_log(self.home)), self.commits, answers)

    def test_dry_run_asks_nothing(self):
        code, out, asked = self.run_cli("--dry-run", "--fuzzy", "all", tty=False)
        self.assertEqual((code, asked), (0, 0), out)
        self.assertIn("Group 1 of 1", out)
        self.assertIn("Similar names (typos)", out)
        self.assertIn("dry run: nothing was written", out)
        self.assertEqual(len(git_log(self.home)), self.commits)

    def test_needs_a_terminal(self):
        code, out, _ = self.run_cli(tty=False)
        self.assertIn("needs a terminal", out)
        self.assertEqual(len(git_log(self.home)), self.commits)

    def test_options(self):
        for argv, message in ((["--fuzzy", "maybe"], "--fuzzy takes"), (["--fuzzy", "typos", "--threshold", "0.5"],
                              "between 0.70 and 0.99"), (["--threshold", "0.9"], "applies to --fuzzy typos")):
            code, out, _ = self.run_cli(*argv)
            self.assertIn(message, out, argv)

    def test_refuses_while_the_app_is_running(self):
        self.home.run.mkdir(mode=0o700, exist_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(str(self.home.socket_path))
        listener.listen(1)
        code, out, asked = self.run_cli(answers=["1", "n", "y"])
        self.assertIn("the app is running", out)
        self.assertEqual(asked, 0)
        self.assertEqual(len(git_log(self.home)), self.commits)
        listener.close()   # a socket file left by a crash: nothing answers, so it goes ahead
        code, out, _ = self.run_cli(answers=["1", "n", "y"])
        self.assertEqual(code, 0, out)
        self.assertEqual(len(git_log(self.home)), self.commits + 1)

    def test_each_sheet_is_decrypted_once(self):
        self.led.add_account("Savings", "savings", "100.00")   # a sheet nothing is merged in
        with mock.patch("register.core.store.decode_rows", wraps=store.decode_rows) as decode:
            code, out, _ = self.run_cli(answers=["1", "n", "y"])
        self.assertEqual(code, 0, out)
        reads = collections.Counter(c.args[2] for c in decode.call_args_list if not c.kwargs.get("with_lines"))
        # once for the run; write_files then reads each sheet it writes back once more (it also
        # decrypts it to reuse unchanged rows' ciphertext, not counted here)
        savings = Store.relpath({"id": "savings", "type": "savings"}, None)
        self.assertEqual(reads, {"everyday/2025.jsonl": 2, "visa/2025.jsonl": 2, savings: 1})

    def test_holds_the_lock_while_reading_and_writing(self):
        held, seen = [], []

        @contextlib.contextmanager
        def watched(home, exclusive=True):
            with locked(home, exclusive):
                held.append((exclusive, object()))
                try:
                    yield
                finally:
                    held.pop()

        def noting(name, real):
            def call(*args, **kwargs):
                seen.append((name, list(held)))
                return real(*args, **kwargs)
            return call
        with mock.patch("register.core.store.locked", watched), \
                mock.patch.object(tidy, "read_names", noting("read", tidy.read_names)), \
                mock.patch.object(Store, "write_files", autospec=True, side_effect=noting("write", Store.write_files)):
            code, out, _ = self.run_cli(answers=["1", "n", "y"])
        self.assertEqual(code, 0, out)
        self.assertEqual([(name, [e for e, _ in h]) for name, h in seen], [("read", [True]), ("write", [True])])
        self.assertIs(seen[0][1][0], seen[1][1][0])   # the same lock, never released in between

    def test_refuses_if_the_app_starts_during_the_password(self):
        with mock.patch.object(cli, "_app_running", side_effect=[False, True]):
            code, out, asked = self.run_cli(answers=["1", "n", "y"])
        self.assertIn("the app was started", out)
        self.assertEqual(asked, 0)
        self.assertEqual(len(git_log(self.home)), self.commits)

    def test_ctrl_c_writes_nothing(self):
        def interrupted(prompt=""):
            raise KeyboardInterrupt
        code, out, _ = run_cli(self.home, "tidy-names", tty=True, input=interrupted)
        self.assertIn("Interrupted; nothing was written.", out)
        self.assertEqual(len(git_log(self.home)), self.commits)

    def test_the_reimport_note_only_when_imports_would_no_longer_match(self):
        # merging case and spacing only: an import still finds the rows, so no note
        self.add("everyday", "2025-06-01", "Corner  Shop", "-1.00")
        self.add("everyday", "2025-06-02", "corner shop", "-1.00")
        code, out, _ = self.run_cli(answers=["s", "1", "n", "y"])
        self.assertEqual(code, 0, out)
        self.assertIn("Written:", out)
        self.assertNotIn("Note: importing", out)
        code, out, _ = self.run_cli(answers=["1", "n", "y"])   # "DD -- pizza hut" changes more than that
        self.assertIn("Note: importing", out)

    def test_a_typed_spelling_with_the_groups_own_key(self):
        self.add("everyday", "2025-06-01", "Shell Oil", "-1.00")
        self.add("everyday", "2025-06-02", "shell oil", "-1.00")
        # pizza: skip; Shell Oil: type a spelling with the group's own key (no question about it)
        code, out, _ = self.run_cli(answers=["s", "t", "SHELL OIL!", "n", "y"])
        self.assertEqual(code, 0, out)
        self.assertNotIn("already a name here", out)           # its key is this group's own
        self.assertEqual(self.rows("everyday").count("SHELL OIL!"), 2)

    def test_typing_the_spelling_of_another_group(self):
        self.add("everyday", "2025-06-01", "Shell Oil", "-1.00")
        self.add("everyday", "2025-06-02", "shell oil", "-1.00")
        self.add("everyday", "2025-06-03", "Shell", "-1.00")
        self.add("everyday", "2025-06-04", "SHELL", "-1.00")
        # pizza: skip; the Shell group: type "Shell oil!", which is another group's name: use its
        # spelling; then stop and write
        code, out, _ = self.run_cli(answers=["s", "t", "Shell oil!", "y", "q", "y"])
        self.assertEqual(code, 0, out)
        self.assertIn("Shell Oil is already a name here. Use that spelling?", out)
        self.assertEqual(self.rows("everyday").count("Shell Oil"), 3)

    def test_typing_another_name_exactly(self):
        self.add("everyday", "2025-06-01", "Shell Oil", "-1.00")
        self.add("everyday", "2025-06-02", "shell oil", "-1.00")
        self.add("everyday", "2025-06-03", "Shell", "-1.00")
        self.add("everyday", "2025-06-04", "SHELL", "-1.00")
        # the Shell group merged into "Shell Oil", then the Shell Oil group, which now holds them too
        code, out, _ = self.run_cli(answers=["s", "t", "Shell Oil", "y", "1", "n", "y"])
        self.assertEqual(code, 0, out)
        self.assertIn("Shell Oil is another name in the register. Merge into it?", out)
        self.assertNotIn("not another spelling", out)
        self.assertEqual(self.rows("everyday").count("Shell Oil"), 4)

    def test_ctrl_c_at_the_last_question_writes_nothing(self):
        answers = iter(["1", "n"])

        def answer(prompt=""):
            try:
                return next(answers)
            except StopIteration:
                raise KeyboardInterrupt from None
        code, out, _ = run_cli(self.home, "tidy-names", tty=True, input=answer)
        self.assertIn("Nothing was written.", out)
        self.assertEqual(len(git_log(self.home)), self.commits)

    def test_typing_the_opening_balance_text(self):
        code, out, _ = self.run_cli(answers=["t", "Opening balance", "", "s", "n"])
        self.assertIn("opening balances, which are left out", out)
        self.assertEqual(len(git_log(self.home)), self.commits)

    def test_the_transfer_category_is_flagged(self):
        self.add("everyday", "2025-06-01", "Bank", "-1.00", "Transfers")
        self.add("everyday", "2025-06-02", "Bank", "-1.00", "transfers")
        before = len(git_log(self.home))
        # Food: skip; Transfers: type "Transfer" (a new name: yes), then decline the report note
        code, out, _ = self.run_cli("--field", "category", answers=["s", "t", "Transfer", "y", "n", "s", "n"])
        self.assertIn("reports leave the Transfer category out", out)
        self.assertIn("Nothing to change; nothing was written.", out)
        self.assertEqual(len(git_log(self.home)), before)


if __name__ == "__main__":
    unittest.main()
