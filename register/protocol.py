"""Wire protocol between the web process and the recorder process.

One JSON request line and one JSON response line per Unix-socket connection:
    request:  {"op": "<name>", "args": {...}}
    response: {"ok": true, "result": ...}
              {"ok": false, "error": "<code>", "message": "...", "field": "..."}

This module is shared by both sides and must not import register.core.
"""
import json
import socket

MAX_REQUEST = 256 * 1024
MAX_RESPONSE = 32 * 1024 * 1024
# A new password set through the web. The login form caps its request at 4096 bytes, so a
# longer password (URL-encoded, up to 12 bytes per character) could never be typed back in.
MAX_PASSWORD = 256
TIMEOUT = 15

# The complete set of operations. Each arg is (type, required).
# "year" is an int year or null (savings accounts have no year tabs).
YEAR = "year"
OPS = {
    "status": {},
    "unlock": {"password": (str, True)},
    "lock": {},
    "list_tabs": {},
    "search": {"account": (str, False), "text": (str, False), "amount_min": (str, False),
               "amount_max": (str, False), "date_from": (str, False), "date_to": (str, False)},
    "report": {"year": (int, False), "account": (str, False)},  # one year's income and spending
    "suggestions": {},  # categories and past descriptions, for autocomplete
    "get_sheet": {"account": (str, True), "year": (YEAR, True)},
    "insert_row": {"account": (str, True), "year": (YEAR, True), "rev": (str, True),
                   "index": (int, True), "fields": (dict, False)},
    "update_row": {"account": (str, True), "year": (YEAR, True), "rev": (str, True),
                   "index": (int, True), "fields": (dict, True)},
    "delete_row": {"account": (str, True), "year": (YEAR, True), "rev": (str, True),
                   "index": (int, True)},
    # the sheet's latest changes from git, and taking one back (a new commit)
    "history": {"account": (str, True), "year": (YEAR, True), "limit": (int, False)},
    "undo": {"account": (str, True), "year": (YEAR, True), "rev": (str, True), "commit": (str, True)},
    # tick / untick rows by index after checking them against a statement (one commit)
    "reconcile": {"account": (str, True), "year": (YEAR, True), "rev": (str, True),
                  "cleared": (list, True), "uncleared": (list, False)},
    # one row in each account, linked; amount is positive and leaves from_account
    "add_transfer": {"from_account": (str, True), "to_account": (str, True), "date": (str, True),
                     "amount": (str, True), "description": (str, False), "category": (str, False),
                     "note": (str, False), "id": (str, False)},
    # recurring transactions: templates, entered only when asked
    "list_recurring": {},
    "add_recurring": {"account": (str, True), "description": (str, True), "amount": (str, True),
                      "every": (str, True), "start": (str, True), "category": (str, False), "note": (str, False)},
    "delete_recurring": {"id": (str, True)},
    "skip_recurring": {"id": (str, True), "count": (int, False)},
    "enter_recurring": {"ids": (list, False)},
    "add_year": {"account": (str, True)},
    # opening_balance is in the stored sign (negative for what is owed)
    "add_account": {"name": (str, True), "type": (str, True), "opening_balance": (str, True),
                    "year": (YEAR, False), "owed_positive": (bool, False)},
    # the name and, optionally, the display setting
    "rename_account": {"account": (str, True), "name": (str, True), "owed_positive": (bool, False)},
    # every sheet as CSV plus an import config, in memory; asks for the password again
    "export": {"password": (str, True)},
    # re-encrypt everything under a new password (one commit), then lock
    "change_password": {"password": (str, True), "new_password": (str, True)},
}
WRITE_OPS = {"insert_row", "update_row", "delete_row", "undo", "reconcile", "add_transfer", "add_recurring", "delete_recurring",
             "skip_recurring", "enter_recurring", "add_year", "add_account", "rename_account"}


class ProtocolError(ValueError):
    def __init__(self, message, field=None):
        super().__init__(message)
        self.field = field


def check_request(req):
    """Validate a request's shape against OPS. Returns (op, args)."""
    if not isinstance(req, dict) or set(req) - {"op", "args"}:
        raise ProtocolError("request must be {op, args}")
    try:
        if not valid_text(req):
            raise ProtocolError("the request contains text that is not valid Unicode")
    except RecursionError:
        raise ProtocolError("the request is nested too deeply") from None
    op, args = req.get("op"), req.get("args", {})
    if not isinstance(op, str) or op not in OPS:
        raise ProtocolError("unknown operation")
    if not isinstance(args, dict):
        raise ProtocolError("args must be an object")
    spec = OPS[op]
    for key in args:
        if key not in spec:
            raise ProtocolError("unknown argument: %s" % key, key)
    for key, (kind, required) in spec.items():
        if key not in args:
            if required:
                raise ProtocolError("missing argument: %s" % key, key)
            continue
        value = args[key]
        if kind == YEAR:
            ok = value is None or (isinstance(value, int) and not isinstance(value, bool))
        elif kind is int:
            ok = isinstance(value, int) and not isinstance(value, bool)
        else:
            ok = isinstance(value, kind)
        if not ok:
            raise ProtocolError("wrong type for %s" % key, key)
    return op, args


def valid_text(value):
    """False if any string in a JSON value (keys included) cannot be encoded: half of a surrogate
    pair, which JSON's \\uD800 escapes let through. Nothing past this point has to cope with it."""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return False
    elif isinstance(value, dict):
        return all(valid_text(k) and valid_text(v) for k, v in value.items())
    elif isinstance(value, list):
        return all(valid_text(v) for v in value)
    return True


def encode(obj):
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class RecorderUnavailable(Exception):
    pass


class RecorderError(Exception):
    def __init__(self, code, message, field=None):
        super().__init__(message)
        self.code, self.message, self.field = code, message, field


def call(socket_path, op, **args):
    """Send one operation to the recorder and return its result (client side)."""
    op, args = check_request({"op": op, "args": args})
    payload = encode({"op": op, "args": args})
    if len(payload) > MAX_REQUEST:
        raise RecorderError("too_large", "request too large")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(TIMEOUT)
            s.connect(str(socket_path))
            s.sendall(payload)
            s.shutdown(socket.SHUT_WR)
            chunks, size = [], 0
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_RESPONSE:
                    raise RecorderUnavailable("response too large")
                chunks.append(chunk)
    except OSError as e:
        raise RecorderUnavailable(str(e)) from None
    try:
        resp = json.loads(b"".join(chunks).decode("utf-8"))
    except ValueError:
        raise RecorderUnavailable("malformed response from recorder") from None
    if not isinstance(resp, dict):
        raise RecorderUnavailable("malformed response from recorder")
    if resp.get("ok") is True:
        return resp.get("result")
    raise RecorderError(str(resp.get("error", "error")), str(resp.get("message", "")), resp.get("field"))
