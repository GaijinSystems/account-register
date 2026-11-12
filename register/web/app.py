"""WSGI application: login, the app shell, static files and the JSON API.

Every API route maps one-to-one onto a recorder operation. This module checks
sessions, CSRF, origin and request shape; the recorder re-validates everything.
"""
import datetime
import hmac
import html
import importlib.resources
import io
import json
import re
import string
import time
import urllib.parse
import zipfile
from http import HTTPStatus

from .. import __version__, protocol
from ..throttle import Throttle
from .auth import Sessions

MAX_BODY = 64 * 1024
ACCOUNT = r"[a-z0-9-]{1,40}"
YEAR = r"\d{4}|-"

SECURITY_HEADERS = [
    ("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
                                "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
                                "frame-ancestors 'none'"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "same-origin"),  # no-referrer would make form POSTs send "Origin: null"
    ("X-Frame-Options", "DENY"),
    ("Cache-Control", "no-store"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
]
STATIC = {"grid.js": "text/javascript; charset=utf-8", "grid.css": "text/css; charset=utf-8",
          "theme.js": "text/javascript; charset=utf-8"}

# recorder error code -> HTTP status
ERROR_STATUS = {
    "stale": 409, "repo_dirty": 423, "not_found": 404, "conflict": 422,
    "bad_request": 400, "invalid": 400, "unknown_field": 400, "balance_readonly": 400, "too_large": 413,
    "locked": 401, "throttled": 429,
    "bad_password": 403,  # export / change password asking again; 401 would mean "logged out"
}
# files the recorder may return from export (anything else is refused, never zipped)
EXPORT_FILE = re.compile(r"(?:%s(?:/\d{4})?\.csv|import\.json)\Z" % ACCOUNT)

SHEET = r"/api/sheet/(?P<account>%s)/(?P<year>%s)" % (ACCOUNT, YEAR)
ID = r"(?P<id>[0-9a-f]{16})"
# (method, path, recorder op). An op's arguments come from the path's named groups and the JSON
# body; which body keys are allowed and required follows from protocol.OPS.
ROUTES = [
    ("GET", r"/api/status", "status"),
    ("GET", r"/api/tabs", "list_tabs"),
    ("GET", r"/api/suggestions", "suggestions"),
    ("POST", r"/api/accounts", "add_account"),
    ("PATCH", r"/api/accounts/(?P<account>%s)" % ACCOUNT, "rename_account"),
    ("POST", r"/api/accounts/(?P<account>%s)/years" % ACCOUNT, "add_year"),
    ("GET", SHEET, "get_sheet"),
    ("POST", SHEET + r"/rows", "insert_row"),
    ("PUT", SHEET + r"/rows/(?P<index>\d{1,6})", "update_row"),
    ("DELETE", SHEET + r"/rows/(?P<index>\d{1,6})", "delete_row"),
    ("GET", SHEET + r"/history", "history"),
    ("POST", SHEET + r"/undo", "undo"),
    ("POST", SHEET + r"/reconcile", "reconcile"),
    ("GET", r"/api/recurring", "list_recurring"),
    ("POST", r"/api/recurring", "add_recurring"),
    ("POST", r"/api/recurring/enter", "enter_recurring"),
    ("POST", r"/api/recurring/%s/skip" % ID, "skip_recurring"),
    ("DELETE", r"/api/recurring/%s" % ID, "delete_recurring"),
    ("POST", r"/api/report", "report"),
    ("POST", r"/api/search", "search"),
    ("POST", r"/api/transfers", "add_transfer"),
    ("POST", r"/api/export", "export"),
    ("POST", r"/api/password", "change_password"),
]


def _route(method, path, op):
    """(method, compiled path, op, allowed body keys, required body keys)."""
    pattern = re.compile(path + r"\Z")
    spec = {k: required for k, (_, required) in protocol.OPS[op].items() if k not in pattern.groupindex}
    return method, pattern, op, set(spec), {k for k, required in spec.items() if required}


ROUTES = [_route(*r) for r in ROUTES]


class HTTPError(Exception):
    def __init__(self, status, code, message, field=None):
        super().__init__(message)
        self.status, self.code, self.message, self.field = status, code, message, field


def _resource(name):
    return importlib.resources.files(__package__).joinpath(name).read_bytes()


def _has_balance(obj):
    """True if 'balance' appears as a key anywhere in a JSON value."""
    if isinstance(obj, dict):
        return "balance" in obj or any(_has_balance(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_balance(v) for v in obj)
    return False


class App:
    def __init__(self, config, call):
        """config: dict from config.json; call(op, **args): send an op to the recorder."""
        self.config = config
        self.call = call
        self.sessions = Sessions(config["session_idle_minutes"] * 60, config["session_max_hours"] * 3600)
        self.throttle = Throttle()
        self.cookie_name = "__Host-register_session" if config["cookie_secure"] else "register_session"
        self.static = {name: _resource("static/" + name) for name in STATIC}
        self.templates = {name: string.Template(_resource("templates/%s.html" % name).decode("utf-8"))
                          for name in ("index", "login")}

    # ------------------------------------------------------------ plumbing

    def __call__(self, environ, start_response):
        try:
            status, headers, body = self.handle(environ)
        except HTTPError as e:
            status, headers, body = self.json_error(e)
        except Exception:
            import traceback
            traceback.print_exc()
            status, headers, body = self.json_error(HTTPError(500, "internal", "internal error"))
        headers = headers + SECURITY_HEADERS + [("Content-Length", str(len(body)))]
        start_response("%d %s" % (status, HTTPStatus(status).phrase), headers)
        return [body] if environ.get("REQUEST_METHOD") != "HEAD" else [b""]

    @staticmethod
    def json_response(status, obj, headers=()):
        body = json.dumps(obj, separators=(",", ":")).encode("ascii")  # escaped: no text can fail to encode
        return status, [("Content-Type", "application/json; charset=utf-8")] + list(headers), body

    def json_error(self, e):
        obj = {"error": e.code, "message": e.message}
        if e.field:
            obj["field"] = e.field
        return self.json_response(e.status, obj)

    @staticmethod
    def html_response(status, text, headers=()):
        return status, [("Content-Type", "text/html; charset=utf-8")] + list(headers), text.encode("utf-8")

    @staticmethod
    def redirect(location, headers=()):
        return 303, [("Location", location)] + list(headers), b""

    def cookie(self, value, max_age=None):
        parts = ["%s=%s" % (self.cookie_name, value), "Path=/", "HttpOnly", "SameSite=Strict"]
        if self.config["cookie_secure"]:
            parts.append("Secure")
        if max_age is not None:
            parts.append("Max-Age=%d" % max_age)
        return ("Set-Cookie", "; ".join(parts))

    def session(self, environ):
        cookies = {}
        for part in environ.get("HTTP_COOKIE", "").split(";"):
            k, _, v = part.strip().partition("=")
            cookies.setdefault(k, v)
        token = cookies.get(self.cookie_name)
        s = self.sessions.get(token)
        return (token, s) if s else (None, None)

    def origin_ok(self, environ):
        origin = environ.get("HTTP_ORIGIN")
        if not origin:
            return False
        expected = self.config.get("public_origin")
        if expected:
            return origin.rstrip("/") == expected.rstrip("/")
        return urllib.parse.urlsplit(origin).netloc == environ.get("HTTP_HOST", "")

    @staticmethod
    def read_body(environ):
        raw = environ.get("CONTENT_LENGTH") or "0"
        if not re.fullmatch(r"[0-9]{1,18}", raw):  # no sign, spaces or underscores
            raise HTTPError(400, "bad_request", "invalid Content-Length")
        length = int(raw)
        if length > MAX_BODY:
            raise HTTPError(413, "too_large", "request body too large")
        return environ["wsgi.input"].read(length) if length else b""

    # ------------------------------------------------------------ routing

    def handle(self, environ):
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "/")
        if method == "HEAD":
            method = "GET"
        if path == "/login":
            return self.login(environ, method)
        if path.startswith("/static/") and method == "GET":
            name = path[len("/static/"):]
            if name not in STATIC:
                raise HTTPError(404, "not_found", "not found")
            return 200, [("Content-Type", STATIC[name])], self.static[name]
        token, session = self.session(environ)
        if path == "/":
            if method != "GET":
                raise HTTPError(405, "method_not_allowed", "method not allowed")
            if not session:
                return self.redirect("/login")
            page = self.templates["index"].substitute(csrf=html.escape(session["csrf"]),
                                                      version=html.escape(__version__))
            return self.html_response(200, page)
        if path in ("/logout", "/logout/all"):
            if method != "POST":
                raise HTTPError(405, "method_not_allowed", "method not allowed")
            if session:
                self.check_write(environ, session)
                if path == "/logout/all":
                    self.sessions.clear()
                else:
                    self.sessions.delete(token)
                try:
                    self.call("lock")  # logging out locks the data again
                except (protocol.RecorderError, protocol.RecorderUnavailable):
                    pass
            return self.json_response(200, {"ok": True}, [self.cookie("", max_age=0)])
        if path.startswith("/api/"):
            if not session:
                raise HTTPError(401, "unauthorized", "not logged in")
            return self.api(environ, method, path, session, token)
        raise HTTPError(404, "not_found", "not found")

    def check_write(self, environ, session):
        if not self.origin_ok(environ):
            raise HTTPError(403, "bad_origin", "request origin not allowed")
        sent = environ.get("HTTP_X_CSRF_TOKEN", "")
        if not hmac.compare_digest(sent.encode(), session["csrf"].encode()):
            raise HTTPError(403, "bad_csrf", "missing or invalid CSRF token")

    def api(self, environ, method, path, session, token):
        matched_path = False
        for m, pattern, op, allowed, required in ROUTES:
            match = pattern.match(path)
            if not match:
                continue
            matched_path = True
            if m != method:
                continue
            args = {}
            if method != "GET":
                self.check_write(environ, session)
                ctype = environ.get("CONTENT_TYPE", "").split(";")[0].strip().lower()
                raw = self.read_body(environ)
                if raw or allowed:
                    if ctype != "application/json":
                        raise HTTPError(415, "bad_request", "Content-Type must be application/json")
                    try:
                        body = json.loads(raw.decode("utf-8")) if raw else {}
                    except (UnicodeDecodeError, ValueError, RecursionError):
                        raise HTTPError(400, "bad_request", "body is not valid JSON") from None
                    if not isinstance(body, dict):
                        raise HTTPError(400, "bad_request", "body must be a JSON object")
                    try:  # before any message can quote a key: text a JSON escape smuggled in
                        if not protocol.valid_text(body):
                            raise HTTPError(400, "bad_request", "the request contains text that is not valid Unicode")
                        refused = _has_balance(body)
                    except RecursionError:
                        raise HTTPError(400, "bad_request", "body is nested too deeply") from None
                    if refused:
                        raise HTTPError(400, "balance_readonly", "the balance is computed and cannot be written",
                                        "balance")
                    extra = set(body) - allowed
                    if extra:
                        raise HTTPError(400, "bad_request", "unexpected field: %s" % sorted(extra)[0])
                    missing = required - set(body)
                    if missing:
                        raise HTTPError(400, "bad_request", "missing field: %s" % sorted(missing)[0])
                    args.update(body)
            for key, value in match.groupdict().items():
                if key == "year":
                    args["year"] = None if value == "-" else int(value)
                elif key == "index":
                    args["index"] = int(value)
                else:
                    args[key] = value
            try:
                result = self.call(op, **args)
            except protocol.ProtocolError as e:
                raise HTTPError(400, "bad_request", str(e), e.field) from None
            except protocol.RecorderUnavailable:
                if op == "change_password":  # it may have gone through (a slow re-encryption): assume it did
                    self.sessions.clear()
                    raise HTTPError(503, "recorder_unavailable", "no answer from the recorder: the password may or "
                                    "may not have been changed. Log in with the new one; if that fails, the old one"
                                    ) from None
                raise HTTPError(503, "recorder_unavailable", "the recorder process is not running") from None
            except protocol.RecorderError as e:
                if e.code == "locked":  # auto-lock, restart or password change: log in again
                    self.sessions.delete(token)
                raise HTTPError(ERROR_STATUS.get(e.code, 500), e.code, e.message, e.field) from None
            if op == "status" and result.get("locked"):
                self.sessions.delete(token)
                raise HTTPError(401, "locked", "the register is locked; log in to unlock it")
            if op == "export":
                return self.export_response(result)
            if op == "change_password":  # every session belonged to the old password
                self.sessions.clear()
                return self.json_response(200, {"ok": True}, [self.cookie("", max_age=0)])
            return self.json_response(200, result)
        if matched_path:
            raise HTTPError(405, "method_not_allowed", "method not allowed")
        raise HTTPError(404, "not_found", "not found")

    @staticmethod
    def export_response(result):
        """The recorder's export as a zip download, built in memory; nothing is written to disk."""
        files = result.get("files") if isinstance(result, dict) else None
        if not isinstance(files, dict) or not files or not all(
                isinstance(k, str) and EXPORT_FILE.match(k) and isinstance(v, str) for k, v in files.items()):
            raise HTTPError(500, "internal", "unexpected export from the recorder")
        folder = "register-export-%s" % datetime.date.today().isoformat()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for rel in sorted(files):
                info = zipfile.ZipInfo("%s/%s" % (folder, rel), date_time=time.localtime()[:6])
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16  # unzip restores owner-only permissions
                z.writestr(info, files[rel].encode("utf-8"))
        return 200, [("Content-Type", "application/zip"),
                     ("Content-Disposition", 'attachment; filename="%s.zip"' % folder)], buf.getvalue()

    # ------------------------------------------------------------ login

    def login(self, environ, method):
        if method == "GET":
            if self.session(environ)[1]:
                return self.redirect("/")
            if environ.get("QUERY_STRING") == "changed":
                return self.login_page(200, "Password changed. Log in with the new password.", "notice")
            return self.login_page(200, "")
        if method != "POST":
            raise HTTPError(405, "method_not_allowed", "method not allowed")
        if not self.origin_ok(environ):
            return self.login_page(403, "Request origin not allowed.")
        wait = self.throttle.wait_seconds()
        if wait:
            return self.login_page(429, "Too many failed attempts. Try again in %d seconds." % wait)
        raw = self.read_body(environ)
        if len(raw) > 4096:
            raise HTTPError(413, "too_large", "request body too large")
        form = urllib.parse.parse_qs(raw.decode("utf-8", "replace"), max_num_fields=5)
        password = (form.get("password") or [""])[0]
        if not password:
            return self.login_page(401, "Wrong password.")
        try:
            self.call("unlock", password=password)  # the recorder checks it against the data key
        except protocol.RecorderUnavailable:
            return self.login_page(503, "The recorder process is not running.")
        except protocol.RecorderError as e:
            if e.code == "bad_password":
                self.throttle.fail()
                return self.login_page(401, "Wrong password.")
            if e.code == "throttled":
                return self.login_page(429, e.message[:1].upper() + e.message[1:] + ".")
            return self.login_page(500, "Cannot unlock: %s" % e.message)
        self.throttle.success()
        token, _ = self.sessions.create()
        return self.redirect("/", [self.cookie(token)])

    def login_page(self, status, error, kind="error"):
        page = self.templates["login"].substitute(
            error=html.escape(error), error_class=kind if error else "error hidden",
            version=html.escape(__version__))
        return self.html_response(status, page)
