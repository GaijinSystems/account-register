import io
import json
import re
import unittest
import zipfile

from register import protocol
from register.web.app import App
from helpers import PASSWORD
from register.throttle import Throttle
from register.web.auth import Sessions

ORIGIN = "https://register.lan"


class FakeRecorder:
    """Stands in for the recorder. unlock/lock are recorded separately from data ops."""

    def __init__(self):
        self.calls = []
        self.auth_calls = []
        self.locked = True
        self.raise_next = None
        self.export_files = {"checking/2025.csv": "num,date,description,amount,cleared,note\n",
                             "money-market.csv": "date,description,amount,note\n", "import.json": "{}\n"}

    def __call__(self, op, **args):
        protocol.check_request({"op": op, "args": args})  # same validation as the real client
        if op == "unlock":
            self.auth_calls.append(op)
            if args["password"] != PASSWORD:
                raise protocol.RecorderError("bad_password", "wrong password")
            self.locked = False
            return {"locked": False, "repo_clean": True}
        if op == "lock":
            self.auth_calls.append(op)
            self.locked = True
            return {"locked": True}
        self.calls.append((op, args))
        if self.raise_next:
            e, self.raise_next = self.raise_next, None
            raise e
        if op == "export":
            if args["password"] != PASSWORD:
                raise protocol.RecorderError("bad_password", "wrong password")
            return {"files": self.export_files, "accounts": 2, "sheets": 2, "rows": 0}
        if op == "change_password":
            if args["password"] != PASSWORD:
                raise protocol.RecorderError("bad_password", "wrong password")
            self.locked = True
            return {"sheets": 2, "locked": True}
        return {"op": op, "args": args}


def config(**kw):
    cfg = {"bind": "127.0.0.1", "port": 8000, "cookie_secure": True,
           "public_origin": None, "session_idle_minutes": 60, "session_max_hours": 12}
    cfg.update(kw)
    return cfg


class WebTest(unittest.TestCase):
    def setUp(self):
        self.rec = FakeRecorder()
        self.app = App(config(), self.rec)
        self.cookie = None
        self.csrf = None

    def request(self, method, path, body=None, headers=None, raw=None, cookie=True):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
        env = {"REQUEST_METHOD": method, "PATH_INFO": path, "HTTP_HOST": "register.lan",
               "HTTP_ORIGIN": ORIGIN, "CONTENT_LENGTH": str(len(data)), "wsgi.input": io.BytesIO(data),
               "CONTENT_TYPE": "application/json" if body is not None else ""}
        if cookie and self.cookie:
            env["HTTP_COOKIE"] = self.cookie
        if self.csrf:
            env["HTTP_X_CSRF_TOKEN"] = self.csrf
        for k, v in (headers or {}).items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        out = {}

        def start_response(status, hdrs):
            out["status"] = int(status.split()[0])
            out["headers"] = hdrs

        body = b"".join(self.app(env, start_response))
        return out["status"], dict(out["headers"]), out["headers"], body

    def login(self):
        status, headers, all_headers, _ = self.request(
            "POST", "/login", raw=b"password=correct+horse+battery",
            headers={"CONTENT_TYPE": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 303)
        cookie = headers["Set-Cookie"]
        self.assertTrue(cookie.startswith("__Host-register_session="))
        for flag in ("HttpOnly", "SameSite=Strict", "Secure", "Path=/"):
            self.assertIn(flag, cookie)
        self.cookie = cookie.split(";")[0]
        page = self.request("GET", "/")[3].decode()
        self.csrf = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)

    # ------------------------------------------------------------ auth

    def test_login_is_unlock(self):
        self.assertTrue(self.rec.locked)
        self.login()
        self.assertEqual((self.rec.auth_calls, self.rec.locked), (["unlock"], False))

    def test_recorder_down_at_login(self):
        self.rec.raise_next = None

        def down(op, **args):
            raise protocol.RecorderUnavailable("down")
        self.app.call = down
        status, _, _, body = self.request("POST", "/login", raw=b"password=x",
                                          headers={"CONTENT_TYPE": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 503)
        self.assertIn(b"recorder process is not running", body)

    def test_locked_recorder_ends_the_session(self):
        self.login()
        self.rec.raise_next = protocol.RecorderError("locked", "the register is locked")
        self.assertEqual(self.request("GET", "/api/tabs")[0], 401)
        self.assertEqual(self.request("GET", "/api/status")[0], 401)  # session is gone

    def test_status_reporting_locked_ends_the_session(self):
        self.login()
        self.rec.calls.clear()
        real = self.rec.__call__
        self.app.call = lambda op, **a: {"locked": True, "repo_clean": True} if op == "status" else real(op, **a)
        self.assertEqual(self.request("GET", "/api/status")[0], 401)
        self.app.call = real
        self.assertEqual(self.request("GET", "/api/tabs")[0], 401)

    def test_requires_login(self):
        self.assertEqual(self.request("GET", "/")[0], 303)
        self.assertEqual(self.request("GET", "/api/tabs")[0], 401)
        self.assertEqual(self.rec.calls, [])

    def test_login_wrong_password_and_origin(self):
        status = self.request("POST", "/login", raw=b"password=nope",
                              headers={"CONTENT_TYPE": "application/x-www-form-urlencoded"})[0]
        self.assertEqual(status, 401)
        status = self.request("POST", "/login", raw=b"password=correct+horse+battery",
                              headers={"HTTP_ORIGIN": "https://evil.example"})[0]
        self.assertEqual(status, 403)
        status = self.request("POST", "/login", raw=b"password=correct+horse+battery",
                              headers={"HTTP_ORIGIN": None})[0]
        self.assertEqual(status, 403)

    def test_throttle(self):
        clock = [0.0]
        t = Throttle(clock=lambda: clock[0])
        for _ in range(4):
            t.fail()
        self.assertEqual(t.wait_seconds(), 0)
        t.fail()
        self.assertEqual(t.wait_seconds(), 1)
        t.fail()
        self.assertEqual(t.wait_seconds(), 2)
        clock[0] += 5
        self.assertEqual(t.wait_seconds(), 0)
        t.success()
        self.assertEqual(t.failures, 0)

    def test_sessions_expire_and_revoke(self):
        clock = [0.0]
        s = Sessions(idle_seconds=60, max_seconds=600, clock=lambda: clock[0])
        token, _ = s.create()
        clock[0] = 59
        self.assertIsNotNone(s.get(token))
        clock[0] = 120
        self.assertIsNone(s.get(token))           # idle timeout
        token, _ = s.create()
        for t in range(0, 651, 50):
            clock[0] = 120 + t
            alive = s.get(token)
        self.assertIsNone(alive)                   # absolute lifetime
        token, _ = s.create()
        s.delete(token)
        self.assertIsNone(s.get(token))

    def test_logout_revokes_and_locks(self):
        self.login()
        self.assertEqual(self.request("POST", "/logout", {})[0], 200)
        self.assertEqual(self.request("GET", "/api/tabs")[0], 401)
        self.assertEqual((self.rec.auth_calls, self.rec.locked), (["unlock", "lock"], True))

    def second_session(self):
        """Log in again, as another browser would; returns the first session's cookie."""
        first, self.cookie = self.cookie, None
        self.login()
        return first

    def test_logout_everywhere(self):
        self.login()
        first = self.second_session()
        self.assertEqual(self.request("POST", "/logout/all", {}, headers={"HTTP_X_CSRF_TOKEN": "x"})[0], 403)
        self.assertEqual(self.request("GET", "/logout/all")[0], 405)
        status, headers, _, _ = self.request("POST", "/logout/all", {})
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertTrue(self.rec.locked)
        self.assertEqual(self.request("GET", "/api/tabs")[0], 401)
        self.rec.locked = False  # even once someone logs in again, the other session stays dead
        self.cookie = first
        self.assertEqual(self.request("GET", "/api/tabs")[0], 401)

    def test_plain_logout_leaves_other_sessions(self):
        self.login()
        first = self.second_session()
        self.request("POST", "/logout", {})
        self.cookie = first
        self.assertEqual(self.request("GET", "/api/tabs")[0], 200)

    def test_change_password(self):
        body = {"password": PASSWORD, "new_password": "new password"}
        self.assertEqual(self.request("POST", "/api/password", body)[0], 401)  # no session
        self.login()
        first = self.second_session()
        self.assertEqual(self.request("POST", "/api/password", body, headers={"HTTP_X_CSRF_TOKEN": None})[0], 403)
        self.assertEqual(self.request("POST", "/api/password", {"password": PASSWORD})[0], 400)
        self.assertEqual(self.request("POST", "/api/password", dict(body, new_password=5))[0], 400)
        status, _, _, out = self.request("POST", "/api/password", dict(body, password="wrong"))
        self.assertEqual((status, json.loads(out)["error"]), (403, "bad_password"))
        self.assertEqual(self.request("GET", "/api/tabs")[0], 200)  # a wrong password doesn't log out
        self.rec.calls.clear()
        status, headers, _, out = self.request("POST", "/api/password", body)
        self.assertEqual((status, json.loads(out)), (200, {"ok": True}))
        self.assertEqual(self.rec.calls, [("change_password", body)])
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.rec.locked = False
        for cookie in (self.cookie, first):  # every session is gone
            self.cookie = cookie
            self.assertEqual(self.request("GET", "/api/tabs")[0], 401)
        page = self.request("GET", "/login", headers={"QUERY_STRING": "changed"})[3].decode()
        self.assertIn("Password changed.", page)
        self.assertIn('class="notice"', page)

    def test_forged_cookie(self):
        self.cookie = "__Host-register_session=forged"
        self.assertEqual(self.request("GET", "/api/tabs")[0], 401)

    # ------------------------------------------------------------ API

    def test_api_maps_to_ops(self):
        self.login()
        cases = [
            ("GET", "/api/status", None, ("status", {})),
            ("GET", "/api/tabs", None, ("list_tabs", {})),
            ("GET", "/api/suggestions", None, ("suggestions", {})),
            ("GET", "/api/sheet/checking/2026", None, ("get_sheet", {"account": "checking", "year": 2026})),
            ("GET", "/api/sheet/money-market/-", None, ("get_sheet", {"account": "money-market", "year": None})),
            ("POST", "/api/sheet/checking/2026/rows", {"rev": "r", "index": 3},
             ("insert_row", {"rev": "r", "index": 3, "account": "checking", "year": 2026})),
            ("PUT", "/api/sheet/checking/2026/rows/4", {"rev": "r", "fields": {"note": "x"}},
             ("update_row", {"rev": "r", "fields": {"note": "x"}, "account": "checking", "year": 2026, "index": 4})),
            ("DELETE", "/api/sheet/checking/2026/rows/4", {"rev": "r"},
             ("delete_row", {"rev": "r", "account": "checking", "year": 2026, "index": 4})),
            ("POST", "/api/accounts/checking/years", {}, ("add_year", {"account": "checking"})),
            ("POST", "/api/sheet/checking/2026/reconcile", {"rev": "r", "cleared": [1, 2]},
             ("reconcile", {"rev": "r", "cleared": [1, 2], "account": "checking", "year": 2026})),
            ("GET", "/api/recurring", None, ("list_recurring", {})),
            ("POST", "/api/recurring", {"account": "a", "description": "Rent", "amount": "-5.00", "every": "month",
                                        "start": "2026-01-01"},
             ("add_recurring", {"account": "a", "description": "Rent", "amount": "-5.00", "every": "month",
                                "start": "2026-01-01"})),
            ("POST", "/api/recurring/enter", {}, ("enter_recurring", {})),
            ("POST", "/api/recurring/enter", {"ids": ["0123456789abcdef"]},
             ("enter_recurring", {"ids": ["0123456789abcdef"]})),
            ("POST", "/api/recurring/0123456789abcdef/skip", {}, ("skip_recurring", {"id": "0123456789abcdef"})),
            ("DELETE", "/api/recurring/0123456789abcdef", {}, ("delete_recurring", {"id": "0123456789abcdef"})),
            ("POST", "/api/report", {"year": 2026, "account": "a"}, ("report", {"year": 2026, "account": "a"})),
            ("POST", "/api/report", {}, ("report", {})),
            ("GET", "/api/sheet/checking/2026/history", None, ("history", {"account": "checking", "year": 2026})),
            ("POST", "/api/sheet/checking/2026/undo", {"rev": "r", "commit": "c" * 40},
             ("undo", {"rev": "r", "commit": "c" * 40, "account": "checking", "year": 2026})),
            ("POST", "/api/search", {"account": "a", "text": "rent"}, ("search", {"account": "a", "text": "rent"})),
            ("POST", "/api/transfers", {"from_account": "a", "to_account": "b", "date": "2026-01-02", "amount": "5.00"},
             ("add_transfer", {"from_account": "a", "to_account": "b", "date": "2026-01-02", "amount": "5.00"})),
            ("PATCH", "/api/accounts/checking", {"name": "C"}, ("rename_account", {"name": "C", "account": "checking"})),
            ("PATCH", "/api/accounts/visa", {"name": "V", "owed_positive": False},
             ("rename_account", {"name": "V", "owed_positive": False, "account": "visa"})),
            ("POST", "/api/accounts", {"name": "A", "type": "savings", "opening_balance": ""},
             ("add_account", {"name": "A", "type": "savings", "opening_balance": ""})),
        ]
        for method, path, body, expected in cases:
            status = self.request(method, path, body)[0]
            self.assertEqual(status, 200, path)
            self.assertEqual(self.rec.calls[-1], expected, path)

    def test_writes_need_csrf_and_origin(self):
        self.login()
        csrf, self.csrf = self.csrf, None
        self.assertEqual(self.request("PUT", "/api/sheet/checking/2026/rows/1", {"rev": "r", "fields": {}})[0], 403)
        self.csrf = "wrong"
        self.assertEqual(self.request("PUT", "/api/sheet/checking/2026/rows/1", {"rev": "r", "fields": {}})[0], 403)
        self.csrf = csrf
        status = self.request("PUT", "/api/sheet/checking/2026/rows/1", {"rev": "r", "fields": {}},
                              headers={"HTTP_ORIGIN": "https://evil.example"})[0]
        self.assertEqual(status, 403)
        status = self.request("PUT", "/api/sheet/checking/2026/rows/1", {"rev": "r", "fields": {}},
                              headers={"CONTENT_TYPE": "text/plain"})[0]
        self.assertEqual(status, 415)
        self.assertEqual(self.rec.calls, [])

    def test_balance_rejected_before_recorder(self):
        self.login()
        for body in ({"rev": "r", "fields": {"balance": "1.00"}}, {"rev": "r", "balance": "1.00", "fields": {}}):
            status, _, _, resp = self.request("PUT", "/api/sheet/checking/2026/rows/1", body)
            self.assertEqual((status, json.loads(resp)["error"]), (400, "balance_readonly"))
        status = self.request("POST", "/api/sheet/checking/2026/rows", {"rev": "r", "index": 0,
                                                                         "fields": {"balance": "1"}})[0]
        self.assertEqual(status, 400)
        self.assertEqual(self.rec.calls, [])

    def test_broken_json_is_a_400_not_a_500(self):
        self.login()
        nested = b'{"text":' + b"[" * 2000 + b"]" * 2000 + b"}"
        for raw in (b"[" * 60000, nested, b'{"text": "\\ud83d"}', b'{"\\ud800": "x"}', b'{"text": ["\\ud800"]}'):
            status, headers, _, body = self.request("POST", "/api/search", raw=raw,
                                                    headers={"CONTENT_TYPE": "application/json"})
            self.assertEqual((status, json.loads(body)["error"]), (400, "bad_request"), raw[:24])
            self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(self.rec.calls, [])                       # none of it reached the recorder
        self.rec.raise_next = protocol.RecorderError("invalid", "bad \ud800 text", "x\ud800")
        status, _, _, body = self.request("GET", "/api/tabs")       # an error text that cannot be encoded
        self.assertEqual((status, json.loads(body)["error"]), (400, "invalid"))

    def test_a_password_change_with_an_unknown_outcome_ends_every_session(self):
        self.login()
        first = self.second_session()
        self.rec.raise_next = protocol.RecorderUnavailable("timed out")
        status = self.request("POST", "/api/password", {"password": PASSWORD, "new_password": "new password"})[0]
        self.assertEqual(status, 503)
        for cookie in (self.cookie, first):
            self.cookie = cookie
            self.assertEqual(self.request("GET", "/api/tabs")[0], 401)

    def test_shape_and_path_checks(self):
        self.login()
        bad = [
            ("PUT", "/api/sheet/checking/2026/rows/1", {"rev": "r", "fields": {}, "extra": 1}, 400),
            ("PUT", "/api/sheet/checking/2026/rows/1", {"fields": {}}, 400),
            ("PUT", "/api/sheet/checking/2026/rows/1", {"rev": 5, "fields": {}}, 400),
            ("GET", "/api/sheet/../etc/2026", None, 404),
            ("GET", "/api/sheet/Checking/2026", None, 404),
            ("GET", "/api/sheet/checking/26", None, 404),
            ("GET", "/static/../app.py", None, 404),
            ("GET", "/static/app.py", None, 404),
            ("POST", "/api/tabs", {}, 405),
        ]
        for method, path, body, want in bad:
            self.assertEqual(self.request(method, path, body)[0], want, (method, path, body))
        self.assertEqual(self.rec.calls, [])
        status = self.request("PUT", "/api/sheet/checking/2026/rows/1", raw=b"x" * 70000)[0]
        self.assertEqual(status, 413)
        # a negative length must not mean "read until the client closes the connection"
        for bad_length in ("-1", "+10", " 10", "1_0", "١٠"):
            status = self.request("POST", "/login", raw=b"password=x", headers={"CONTENT_LENGTH": bad_length})[0]
            self.assertEqual(status, 400, bad_length)
        self.assertEqual(self.rec.calls, [])

    def test_export_download(self):
        self.login()
        status, headers, _, body = self.request("POST", "/api/export", {"password": PASSWORD})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/zip")
        self.assertEqual(headers["Cache-Control"], "no-store")
        name = re.fullmatch(r'attachment; filename="(register-export-\d{4}-\d\d-\d\d)\.zip"',
                            headers["Content-Disposition"]).group(1)
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            self.assertEqual(sorted(z.namelist()), ["%s/%s" % (name, f) for f in sorted(self.rec.export_files)])
            for info in z.infolist():
                self.assertEqual(info.external_attr >> 16, 0o600)
                self.assertEqual(z.read(info).decode(), self.rec.export_files[info.filename.split("/", 1)[1]])

    def test_export_refusals(self):
        self.assertEqual(self.request("POST", "/api/export", {"password": PASSWORD})[0], 401)  # no session
        self.login()
        status, _, _, body = self.request("POST", "/api/export", {"password": "wrong"})
        self.assertEqual(status, 403)  # not 401: a wrong password here must not log the user out
        self.assertEqual(json.loads(body)["error"], "bad_password")
        self.assertEqual(self.request("GET", "/api/tabs")[0], 200)
        self.assertEqual(self.request("POST", "/api/export", {"password": PASSWORD},
                                      headers={"HTTP_X_CSRF_TOKEN": None})[0], 403)
        self.assertEqual(self.request("GET", "/api/export")[0], 405)
        self.assertEqual(self.request("POST", "/api/export", {})[0], 400)
        self.assertEqual(self.request("POST", "/api/export", {"password": PASSWORD, "x": 1})[0], 400)
        for bad in ("../x.csv", "/etc/passwd", "checking/2025.jsonl", "a/b/c.csv"):
            self.rec.export_files = {bad: "x", "import.json": "{}"}
            status, headers, _, _ = self.request("POST", "/api/export", {"password": PASSWORD})
            self.assertEqual(status, 500, bad)  # never zipped
            self.assertNotEqual(headers["Content-Type"], "application/zip")

    def test_recorder_errors_map_to_status(self):
        self.login()
        for err, want in ((protocol.RecorderError("stale", "s"), 409),
                          (protocol.RecorderError("repo_dirty", "d"), 423),
                          (protocol.RecorderError("not_found", "n"), 404),
                          (protocol.RecorderError("invalid", "i", "amount"), 400),
                          (protocol.RecorderError("write_failed", "w"), 500),
                          (protocol.RecorderUnavailable("down"), 503)):
            self.rec.raise_next = err
            status, _, _, body = self.request("GET", "/api/tabs")
            self.assertEqual(status, want, err)
        self.assertEqual(json.loads(body)["error"], "recorder_unavailable")

    def test_security_headers_everywhere(self):
        self.assertEqual(self.request("GET", "/static/theme.js")[0], 200)
        for path in ("/login", "/static/grid.js", "/api/tabs", "/nope"):
            headers = self.request("GET", path)[1]
            self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(headers["Cache-Control"], "no-store")

    def test_templates_escape(self):
        page = self.request("GET", "/login")[3].decode()
        self.assertNotIn("$error", page)
        self.assertNotIn("<script>", page)

    def test_insecure_cookie_mode_for_local_testing(self):
        app = App(config(cookie_secure=False), self.rec)
        self.app = app
        status, headers, _, _ = self.request(
            "POST", "/login", raw=b"password=correct+horse+battery",
            headers={"CONTENT_TYPE": "application/x-www-form-urlencoded"})
        self.assertTrue(headers["Set-Cookie"].startswith("register_session="))
        self.assertNotIn("Secure", headers["Set-Cookie"])


if __name__ == "__main__":
    unittest.main()
