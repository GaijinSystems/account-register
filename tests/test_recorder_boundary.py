"""Recorder over a real Unix socket, plus checks on the web/recorder boundary.

The socket tests need permission to create Unix sockets; they are skipped if the
environment forbids it.
"""
import ast
import json
import os
import socket
import threading
import unittest
from pathlib import Path

from helpers import FAST_N, PASSWORD, git_log, make_home
from register import protocol
from register.core import admin
from register.core.ledger import Ledger
from register.core.recorder import RecorderServer
from register.core.store import locked

ROOT = Path(__file__).resolve().parent.parent / "register"


def _can_unix_socket():
    try:
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).close()
        return True
    except OSError:
        return False


@unittest.skipUnless(_can_unix_socket(), "Unix sockets not permitted here")
class RecorderTest(unittest.TestCase):
    def setUp(self):
        self.home = make_home(self)
        with locked(self.home):
            Ledger(self.home, self.home.key).add_account("Checking", "checking", "10.00", 2025)
        self.now = [1000.0]
        self.server = RecorderServer(self.home, auto_lock_minutes=30, clock=lambda: self.now[0])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def call(self, op, **args):
        return protocol.call(self.home.socket_path, op, **args)

    def raw(self, payload):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(str(self.home.socket_path))
            s.sendall(payload)
            s.shutdown(socket.SHUT_WR)
            return json.loads(b"".join(iter(lambda: s.recv(65536), b"")))

    def test_socket_permissions(self):
        self.assertEqual(os.stat(self.home.socket_path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.home.run).st_mode & 0o777, 0o700)

    def test_starts_locked_and_refuses_data_ops(self):
        self.assertEqual(self.call("status"), {"repo_clean": True, "locked": True})
        for op, args in (("list_tabs", {}), ("get_sheet", {"account": "checking", "year": 2025}),
                         ("add_year", {"account": "checking"})):
            with self.assertRaises(protocol.RecorderError) as cm:
                self.call(op, **args)
            self.assertEqual(cm.exception.code, "locked")

    def test_unlock_wrong_password_and_backoff(self):
        for _ in range(5):
            with self.assertRaises(protocol.RecorderError) as cm:
                self.call("unlock", password="wrong")
            self.assertEqual(cm.exception.code, "bad_password")
        with self.assertRaises(protocol.RecorderError) as cm:
            self.call("unlock", password=PASSWORD)  # even the right one waits out the backoff
        self.assertEqual(cm.exception.code, "throttled")
        self.now[0] += 2
        self.assertEqual(self.call("unlock", password=PASSWORD)["locked"], False)

    def test_export_needs_the_password_again(self):
        with self.assertRaises(protocol.RecorderError) as cm:
            self.call("export", password=PASSWORD)
        self.assertEqual(cm.exception.code, "locked")
        self.call("unlock", password=PASSWORD)
        result = self.call("export", password=PASSWORD)
        self.assertEqual(sorted(result["files"]), ["checking/2025.csv", "import.json"])
        self.assertEqual((result["accounts"], result["sheets"], result["rows"]), (1, 1, 1))
        for _ in range(5):  # wrong passwords share the unlock backoff
            with self.assertRaises(protocol.RecorderError) as cm:
                self.call("export", password="wrong")
            self.assertEqual(cm.exception.code, "bad_password")
        self.assertFalse(self.server.ledger.locked)  # a wrong export password doesn't lock
        with self.assertRaises(protocol.RecorderError) as cm:
            self.call("unlock", password=PASSWORD)
        self.assertEqual(cm.exception.code, "throttled")

    def test_lock_and_auto_lock(self):
        self.call("unlock", password=PASSWORD)
        self.call("list_tabs")
        self.assertTrue(self.call("lock")["locked"])
        with self.assertRaises(protocol.RecorderError):
            self.call("list_tabs")
        self.call("unlock", password=PASSWORD)
        for _ in range(4):  # status polls do not count as activity
            self.now[0] += 10 * 60
            self.call("status")
        self.assertTrue(self.call("status")["locked"])
        self.call("unlock", password=PASSWORD)
        self.now[0] += 29 * 60
        self.call("list_tabs")  # real activity keeps it unlocked
        self.now[0] += 29 * 60
        self.assertFalse(self.call("status")["locked"])
        self.now[0] += 2 * 60
        self.server.service_actions()  # the idle loop locks even without requests
        self.assertTrue(self.server.ledger.locked)

    def test_password_change_elsewhere_relocks(self):
        self.call("unlock", password=PASSWORD)
        admin.change_password(self.home, PASSWORD, "new password", n=FAST_N)
        with self.assertRaises(protocol.RecorderError) as cm:
            self.call("list_tabs")
        self.assertEqual(cm.exception.code, "locked")
        with self.assertRaises(protocol.RecorderError):
            self.call("unlock", password=PASSWORD)
        self.assertEqual(self.call("unlock", password="new password")["locked"], False)
        self.assertEqual([a["id"] for a in self.call("list_tabs")["accounts"]], ["checking"])

    def test_change_password_op(self):
        self.server.kdf_n = FAST_N
        new = "new password"
        with self.assertRaises(protocol.RecorderError) as cm:
            self.call("change_password", password=PASSWORD, new_password=new)
        self.assertEqual(cm.exception.code, "locked")  # only when logged in
        self.call("unlock", password=PASSWORD)
        for bad in ("", PASSWORD, "x" * (protocol.MAX_PASSWORD + 1)):
            with self.assertRaises(protocol.RecorderError) as cm:
                self.call("change_password", password=PASSWORD, new_password=bad)
            self.assertEqual((cm.exception.code, cm.exception.field), ("invalid", "new_password"))
        with self.assertRaises(protocol.RecorderError) as cm:
            self.call("change_password", password="wrong", new_password=new)
        self.assertEqual(cm.exception.code, "bad_password")
        self.assertEqual(self.server.throttle.failures, 1)  # shares the unlock backoff
        self.assertFalse(self.server.ledger.locked)
        self.assertEqual(self.call("change_password", password=PASSWORD, new_password=new),
                         {"sheets": 1, "locked": True})
        self.assertEqual(git_log(self.home)[0], "change password (re-encrypt all data)")
        self.assertTrue(self.call("status")["locked"])  # the old key is dropped
        with self.assertRaises(protocol.RecorderError) as cm:
            self.call("unlock", password=PASSWORD)
        self.assertEqual(cm.exception.code, "bad_password")
        self.call("unlock", password=new)
        self.assertEqual(self.call("get_sheet", account="checking", year=2025)["rows"][0]["amount"], "10.00")

    def test_change_password_refused_while_dirty_or_throttled(self):
        self.server.kdf_n = FAST_N
        self.call("unlock", password=PASSWORD)
        (self.home.data / "stray").write_text("x")
        with self.assertRaises(protocol.RecorderError) as cm:
            self.call("change_password", password=PASSWORD, new_password="new password")
        self.assertEqual(cm.exception.code, "repo_dirty")
        (self.home.data / "stray").unlink()
        for _ in range(5):
            with self.assertRaises(protocol.RecorderError):
                self.call("change_password", password="wrong", new_password="new password")
        with self.assertRaises(protocol.RecorderError) as cm:
            self.call("change_password", password=PASSWORD, new_password="new password")
        self.assertEqual(cm.exception.code, "throttled")
        self.assertEqual(self.call("list_tabs")["accounts"][0]["id"], "checking")  # still the old key

    def test_operations(self):
        self.call("unlock", password=PASSWORD)
        self.assertEqual(self.call("status"), {"repo_clean": True, "locked": False})
        sheet = self.call("get_sheet", account="checking", year=2025)
        sheet = self.call("insert_row", account="checking", year=2025, rev=sheet["rev"], index=1,
                          fields={"amount": "-4.00"})
        self.assertEqual(sheet["rows"][1]["balance"], "6.00")
        with self.assertRaises(protocol.RecorderError) as cm:
            self.call("update_row", account="checking", year=2025, rev=sheet["rev"], index=1,
                      fields={"balance": "100.00"})
        self.assertEqual(cm.exception.code, "balance_readonly")
        with self.assertRaises(protocol.RecorderError) as cm:
            self.call("update_row", account="checking", year=2025, rev="old", index=1, fields={"note": "x"})
        self.assertEqual(cm.exception.code, "stale")

    def test_malformed_requests_get_an_answer_and_no_password_is_logged(self):
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for payload in (b'{"op": [], "args": {}}\n', b'{"op": {"a": 1}}\n', b"[" * 50000 + b"\n",
                            b'{"op":"status","args":{"\\ud800":1}}\n'):
                self.assertEqual(self.raw(payload)["error"], "bad_request", payload[:30])
            # text that is not valid Unicode is refused as it arrives, whichever field it is in
            for payload in (b'{"op":"unlock","args":{"password":"secret-typo-\\ud800"}}\n',
                            b'{"op":"insert_row","args":{"account":"checking","year":2025,"rev":"x","index":0,'
                            b'"fields":{"note":"a\\ud800"}}}\n'):
                self.assertEqual(self.raw(payload)["error"], "bad_request")
            with self.assertRaises(protocol.ProtocolError):           # and the client refuses to send it
                self.call("unlock", password="secret-typo-\ud800")
            self.call("unlock", password=PASSWORD)
        self.assertNotIn("secret-typo", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        self.assertEqual(self.call("status")["locked"], False)        # still serving

    def test_rejects_unknown_ops_and_args(self):
        for req in ({"op": "git_reset", "args": {}}, {"op": "status", "args": {"path": "/etc"}},
                    {"op": "get_sheet", "args": {"account": "checking", "year": "2025"}},
                    {"op": "get_sheet", "args": {"account": "checking"}},
                    {"op": "status", "args": {}, "extra": 1}, [1, 2]):
            resp = self.raw((json.dumps(req) + "\n").encode())
            self.assertEqual((resp["ok"], resp["error"]), (False, "bad_request"), req)
        resp = self.raw(b"not json\n")
        self.assertEqual(resp["error"], "bad_request")
        self.call("unlock", password=PASSWORD)
        resp = self.raw(b'{"op":"get_sheet","args":{"account":"../../etc","year":2025}}\n')
        self.assertEqual(resp["error"], "invalid")

    def test_oversized_request(self):
        resp = self.raw(b"x" * (protocol.MAX_REQUEST + 10) + b"\n")
        self.assertEqual(resp["error"], "too_large")

    def test_second_recorder_refuses(self):
        with self.assertRaises(SystemExit):
            RecorderServer(self.home)


class BoundaryTest(unittest.TestCase):
    def test_web_never_imports_core(self):
        for path in (ROOT / "web").rglob("*.py"):
            tree = ast.parse(path.read_text(), str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    mod = ("." * node.level) + (node.module or "")
                    names = [a.name for a in node.names]
                    self.assertFalse("core" in mod.split(".") or (mod.strip(".") == "" and "core" in names),
                                     "%s imports %s" % (path.name, mod))
                if isinstance(node, ast.Import):
                    for a in node.names:
                        self.assertNotIn("core", a.name.split("."), path.name)

    def test_protocol_never_imports_core(self):
        tree = ast.parse((ROOT / "protocol.py").read_text())
        mods = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        self.assertFalse([m for m in mods if "core" in m])

    def test_git_wrapper_is_append_only(self):
        src = (ROOT / "core" / "gitrepo.py").read_text()
        code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith(("#", '"""')))
        calls = {n.args[1].value for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_run" and len(n.args) > 1
                 and isinstance(n.args[1], ast.Constant)}
        self.assertEqual(calls, {"init", "config", "status", "add", "commit", "log", "show"})
        for word in ("reset", "checkout", "restore", "rebase", "push", "--amend", "branch", "gc", "prune"):
            self.assertNotIn('"%s"' % word, code)
        for path in ROOT.rglob("*.py"):  # nothing else runs git
            if path.name != "gitrepo.py":
                self.assertNotIn('"git"', path.read_text(), path.name)

    def test_ops_match_ledger(self):
        for op, spec in protocol.OPS.items():
            fn = getattr(Ledger, op)
            params = fn.__code__.co_varnames[1:fn.__code__.co_argcount]
            self.assertTrue(set(spec) <= set(params), op)


if __name__ == "__main__":
    unittest.main()
