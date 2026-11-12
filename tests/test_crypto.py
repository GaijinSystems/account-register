"""Encryption at rest: crypto primitives, file format, password change, history tools."""
import json
import subprocess
import unittest
from pathlib import Path

from helpers import FAST_N, PASSWORD, git_log, make_home
from register.core import admin, crypto, formats, gitrepo, store
from register.core.ledger import Ledger
from register.core.store import StoreError, locked

CHECKING = formats.FORMATS["checking"]
REL = "checking/2025.jsonl"


class CryptoTest(unittest.TestCase):
    def test_round_trip_and_padding(self):
        key, doc = crypto.new_key("pw", n=FAST_N)
        item = key.encrypt("a.jsonl", b'{"x":1}')
        self.assertEqual(len(crypto._unb64(item["c"])) - 16, 64)  # padded to 64 bytes (+16 tag)
        self.assertEqual(key.decrypt("a.jsonl", item), b'{"x":1}')
        self.assertEqual(crypto.unlock("pw", doc).key_id, key.key_id)

    def test_wrong_password(self):
        _, doc = crypto.new_key("pw", n=FAST_N)
        with self.assertRaises(crypto.CryptoError) as cm:
            crypto.unlock("nope", doc)
        self.assertEqual(cm.exception.code, "bad_password")

    def test_tamper_and_move_detected(self):
        key, _ = crypto.new_key("pw", n=FAST_N)
        item = key.encrypt("checking/2025.jsonl", b"row")
        with self.assertRaises(crypto.CryptoError):
            key.decrypt("checking/2026.jsonl", item)  # moved to another file
        c = bytearray(crypto._unb64(item["c"]))
        c[0] ^= 1
        with self.assertRaises(crypto.CryptoError):
            key.decrypt("checking/2025.jsonl", dict(item, c=crypto._b64(bytes(c))))

    def test_new_password_new_key(self):
        a, doc_a = crypto.new_key("pw", n=FAST_N)
        b, doc_b = crypto.new_key("pw", n=FAST_N)  # same password, fresh salt: different key
        self.assertNotEqual(doc_a["key_id"], doc_b["key_id"])
        with self.assertRaises(crypto.CryptoError) as cm:
            b.decrypt("x", a.encrypt("x", b"data"))
        self.assertEqual(cm.exception.code, "wrong_key")


class FormatLabelTest(unittest.TestCase):
    def test_labels_are_frozen(self):
        # Part of the on-disk format (4). Changing them makes every home and its history undecryptable.
        self.assertEqual(crypto.FORMAT_VERSION, 4)
        self.assertEqual(crypto.CHECK_TEXT, b"key check")
        self.assertEqual(crypto.aad("checking/2026.jsonl", "0123456789abcdef"),
                         b"v4|checking/2026.jsonl|0123456789abcdef")

    def test_no_project_name_in_the_code(self):
        root = Path(__file__).resolve().parent.parent / "register"
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in (".py", ".js", ".css", ".html"):
                self.assertNotIn("register-mini", path.read_text(encoding="utf-8"), path)

    def test_older_formats_get_a_clear_message(self):
        home = make_home(self)
        doc = json.loads((home.data / "crypto.json").read_text())
        for version in (1, 2, 3):
            (home.data / "crypto.json").write_text(json.dumps(dict(doc, format_version=version)))
            with self.assertRaises(StoreError) as cm:
                home.unlock(PASSWORD)
            self.assertEqual(cm.exception.code, "old_format")
            self.assertIn("data format %d, from an older version" % version, cm.exception.message)
            self.assertIn("export", cm.exception.message)


class InitTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = store.Home(Path(tmp.name) / "h")

    def test_completes_a_half_created_home(self):
        # what a failed init used to leave: an empty git repo and run/
        self.home.run.mkdir(parents=True)
        self.home.data.mkdir()
        gitrepo.init(self.home.data, "t", "t@x")
        admin.init_home(self.home, PASSWORD, "t", "t@x", n=FAST_N)
        self.assertEqual(self.home.unlock(PASSWORD).key_id, self.home.crypto_doc()["key_id"])
        with self.assertRaises(StoreError):
            admin.init_home(self.home, PASSWORD, "t", "t@x", n=FAST_N)  # now really initialized

    def test_refuses_a_non_empty_data_dir(self):
        self.home.data.mkdir(parents=True)
        (self.home.data / "something.txt").write_text("x")
        with self.assertRaises(StoreError):
            admin.init_home(self.home, PASSWORD, "t", "t@x", n=FAST_N)

    def test_missing_cryptography_is_a_clear_error_before_any_prompt(self):
        code = ("import sys\n"
                "for m in ('cryptography', 'cryptography.exceptions', "
                "'cryptography.hazmat.primitives.ciphers.aead'): sys.modules[m] = None\n"
                "from register.__main__ import main\nmain(sys.argv[1:])\n")
        root = str(store.Path(__file__).resolve().parents[1])
        run = lambda *a: subprocess.run(["python3", "-c", code, *a], capture_output=True, text=True,
                                        stdin=subprocess.DEVNULL, cwd=root)
        r = run()
        self.assertEqual(r.returncode, 0)
        self.assertIn("python3-cryptography", r.stdout)
        r = run("init", "--home", str(self.home.root))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("python3-cryptography", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse(self.home.root.exists())  # nothing created


class EncryptedStoreTest(unittest.TestCase):
    def setUp(self):
        self.home = make_home(self)
        self.ledger = Ledger(self.home, self.home.key)
        with locked(self.home):
            self.ledger.add_account("Checking", "checking", "100.00", 2025)
            s = self.ledger.get_sheet("checking", 2025)
            for i in range(3):
                s = self.ledger.insert_row("checking", 2025, s["rev"], len(s["rows"]),
                                           {"description": "Secret payee %d" % i, "amount": "-1%d.00" % i})

    def lines(self):
        return (self.home.data / REL).read_text().splitlines()

    def test_no_plaintext_anywhere_in_git(self):
        self.ledger.rename_account("checking", "Personal Checking")
        everything = gitrepo._run(self.home.data, "log", "-p", "--all")
        for secret in ("Secret payee", "-10.00", "100.00", "Opening balance", "Personal Checking"):
            self.assertNotIn(secret, everything)
        self.assertNotIn("Checking", "\n".join(git_log(self.home)).replace("checking", ""))

    def test_unchanged_rows_keep_ciphertext(self):
        before = self.lines()
        s = self.ledger.get_sheet("checking", 2025)
        self.ledger.update_row("checking", 2025, s["rev"], 2, {"note": "x"})
        after = self.lines()
        self.assertEqual([i for i in range(len(after)) if after[i] != before[i]], [2])
        diff = gitrepo._run(self.home.data, "diff", "HEAD~1", "--numstat")
        self.assertTrue(diff.startswith("1\t1\t"))

    def test_mixed_key_lines_refused(self):
        other, _ = crypto.new_key("someone else", n=FAST_N)
        foreign = crypto.dumps_item(other.encrypt(REL, store.dumps_row(CHECKING, CHECKING.blank_row()).encode()))
        (self.home.data / REL).write_text("\n".join(self.lines() + [foreign]) + "\n")
        with self.assertRaises(StoreError) as cm:
            self.ledger.get_sheet("checking", 2025)
        self.assertEqual(cm.exception.code, "wrong_key")

    def test_locked_store_refuses(self):
        locked_ledger = Ledger(self.home)
        with self.assertRaises(StoreError) as cm:
            locked_ledger.list_tabs()
        self.assertEqual(cm.exception.code, "locked")

    def test_change_password(self):
        old_key_id = self.home.key.key_id
        before = self.lines()
        admin.change_password(self.home, PASSWORD, "brand new", n=FAST_N)
        self.assertEqual(git_log(self.home)[0], "change password (re-encrypt all data)")
        after = self.lines()
        self.assertTrue(all(a != b for a, b in zip(before, after)))  # everything re-encrypted
        self.assertTrue(all(json.loads(line)["k"] != old_key_id for line in after))
        with self.assertRaises(StoreError):
            self.home.unlock(PASSWORD)
        new = Ledger(self.home, self.home.unlock("brand new"))
        self.assertEqual([r["description"] for r in new.get_sheet("checking", 2025)["rows"]][1:],
                         ["Secret payee 0", "Secret payee 1", "Secret payee 2"])
        with self.assertRaises(StoreError):  # the stale key notices and gives up
            self.ledger.list_tabs()
        with self.assertRaises(StoreError):
            admin.change_password(self.home, PASSWORD, "x", n=FAST_N)

    def test_change_to_the_same_password_is_refused(self):
        home = make_home(self)
        before = len(git_log(home))
        with self.assertRaises(store.StoreError) as cm:      # the rule the command line and the page share
            admin.change_password(home, PASSWORD, PASSWORD, n=FAST_N)
        self.assertEqual((cm.exception.code, cm.exception.field), ("invalid", "new_password"))
        with self.assertRaises(store.StoreError) as cm:      # a wrong current password is still reported first
            admin.change_password(home, "wrong", "wrong", n=FAST_N)
        self.assertEqual(cm.exception.code, "bad_password")
        self.assertEqual(len(git_log(home)), before)

    def test_show_and_history_across_a_password_change(self):
        s = self.ledger.get_sheet("checking", 2025)
        self.ledger.update_row("checking", 2025, s["rev"], 1, {"note": "before change"})
        admin.change_password(self.home, PASSWORD, "brand new", n=FAST_N)
        new = Ledger(self.home, self.home.unlock("brand new"))
        s = new.get_sheet("checking", 2025)
        new.update_row("checking", 2025, s["rev"], 1, {"note": "after change"})
        asked = []

        def ask(prompt):
            asked.append(prompt)
            return "brand new" if len(asked) == 1 else PASSWORD

        entries = admin.history(self.home, REL, 3, ask)
        self.assertEqual([e[2] for e in entries], ["checking/2025: edit row 2 (note)",
                                                   "change password (re-encrypt all data)",
                                                   "checking/2025: edit row 2 (note)"])
        self.assertEqual(len(asked), 2)  # once per password era
        self.assertTrue(any('"note": "after change"' in line for line in entries[0][3]))
        self.assertEqual(entries[1][3], [])  # re-encryption changes no row
        current = admin.show(self.home, REL, None, lambda p: "brand new")
        self.assertIn('"note": "after change"', current[1])
        old_rev = gitrepo.log(self.home.data, REL, 3)[2][0]
        old = admin.show(self.home, REL, old_rev, lambda p: PASSWORD)
        self.assertIn('"note": "before change"', old[1])

    def test_textconv(self):
        path = self.home.data / REL
        out = admin.textconv(self.home, path, PASSWORD)
        self.assertIn('"description": "Secret payee 0"', out[1])
        tmp = self.home.root / "Ab12Cd_2025.jsonl"  # how git names its temp copies
        tmp.write_bytes(path.read_bytes())
        self.assertEqual(admin.textconv(self.home, tmp, PASSWORD), out)
        no_pw = admin.textconv(self.home, path, None)
        self.assertIn("REGISTER_PASSWORD", no_pw[0])
        self.assertFalse(any("Secret" in line for line in no_pw))

    def test_cli_history_uses_env_password(self):
        r = subprocess.run(["python3", "-m", "register", "history", "--home", str(self.home.root), REL, "-n", "2"],
                           capture_output=True, text=True, env={"REGISTER_PASSWORD": PASSWORD, "PATH": "/usr/bin:/bin",
                                                                "PYTHONPATH": str(store.Path(__file__).resolve().parents[1])})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("insert row 4", r.stdout)
        self.assertIn('"description": "Secret payee 2"', r.stdout)


if __name__ == "__main__":
    unittest.main()
