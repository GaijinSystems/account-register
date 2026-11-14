#!/usr/bin/env python3
"""Check that the demo's in-memory backend (_demo/api.js) still answers as the real one does:
send the same requests to the real app (register.web.app.App over a real Ledger, in process)
and to a built demo site in headless Chromium, and compare every answer. Run it after changing
register/core/ledger.py on main, or _demo/api.js.

    export REGISTER_SOURCE=PATH/TO/main
    python3 _demo/build_demo.py SITE
    python3 _demo/demo_differential.py SITE

Needs Playwright for Python with Chromium, and the app's own requirements.
"""
import datetime
import functools
import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from appsource import ROOT  # noqa: E402  (puts the app's checkout on sys.path)

assert (ROOT / "register").is_dir()
from playwright.sync_api import sync_playwright  # noqa: E402

from register import protocol  # noqa: E402
from register.__main__ import DEFAULT_CONFIG  # noqa: E402
from register.core import admin, formats, recurring as rec  # noqa: E402
from register.core.ledger import Ledger  # noqa: E402
from register.core.store import RECURRING, RECURRING_FILE, Home, StoreError, locked  # noqa: E402
from register.web.app import ROUTES, App, HTTPError  # noqa: E402

from check_demo import PREFIX, api, serve  # noqa: E402  (the same server, the same way of calling the page's fetch)

# What the demo does not have. Their steps go to the demo only, which must answer 501. (The real
# change_password also takes a lock of its own that this harness cannot give it.)
NOT_COMPARED = {"change_password", "export"}
TODAY = datetime.date.today()
Y = TODAY.year
D = TODAY.isoformat()


def make_real(tmp):
    home = Home(Path(tmp) / "home")
    admin.init_home(home, "demo", "demo", "demo@localhost")
    ledger = Ledger(home, home.unlock("demo"))

    def call(op, **args):
        op, args = protocol.check_request({"op": op, "args": args})
        try:
            with locked(home):
                return getattr(ledger, op)(**args)
        except (StoreError, formats.FieldError) as e:  # as the recorder answers them
            raise protocol.RecorderError(e.code, e.message, e.field) from None

    app = App(dict(DEFAULT_CONFIG, cookie_secure=False), call)

    def real(method, path, body=None):
        raw = json.dumps(body if body is not None else {}).encode() if method != "GET" else b""
        environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "HTTP_ORIGIN": "http://x", "HTTP_HOST": "x",
                   "HTTP_X_CSRF_TOKEN": "t", "CONTENT_TYPE": "application/json", "CONTENT_LENGTH": str(len(raw)),
                   "wsgi.input": io.BytesIO(raw)}
        try:
            status, _, out = app.api(environ, method, path, {"csrf": "t"}, "tok")
            return {"status": status, "body": json.loads(out)}
        except HTTPError as e:
            status, _, out = app.json_error(e)  # the app's own error body
            return {"status": status, "body": json.loads(out)}
    return home, ledger, real


def load(home, ledger, demo):
    """Put the demo's starting data into the real ledger."""
    tabs = demo("GET", "/api/tabs")["body"]
    with locked(home):
        files = {}
        for a in tabs["accounts"]:
            ys = a["years"]
            ledger.add_account(a["name"], a["type"], "0.00", ys[0] if ys else None, account_id=a["id"],
                               owed_positive=a["owed_positive"])
            for _ in (ys or [])[1:]:
                ledger.add_year(a["id"])
            acct = ledger.store.account(a["id"])
            for y in ys or [None]:
                sheet = demo("GET", "/api/sheet/%s/%s" % (a["id"], "-" if y is None else y))["body"]
                files[ledger.store.relpath(acct, y)] = (formats.FORMATS[a["type"]],
                                                        [{k: r[k] for k in formats.FIELDS} for r in sheet["rows"]])
        recs = demo("GET", "/api/recurring")["body"]["recurring"]
        files[RECURRING_FILE] = (RECURRING, [{k: t[k] for k in rec.KEYS} for t in recs])
        ledger.store.write_files(files, "demo data")


def resolve(call, method, path, body):
    body = dict(body) if isinstance(body, dict) else body
    m = re.match(r"(/api/sheet/[^/]+/[^/@]+)", path)
    if m:
        sheet = call("GET", m.group(1))["body"]
        if "@last" in path:
            path = path.replace("@last", str(len(sheet["rows"]) - 1))
        if isinstance(body, dict) and body.get("rev") == "@":
            body["rev"] = sheet["rev"]
        if isinstance(body, dict) and str(body.get("commit", "")).startswith("@"):
            entries = call("GET", m.group(1) + "/history")["body"]["entries"]
            body["commit"] = entries[int(body["commit"][1:])]["commit"]
    if "@rec" in path or (isinstance(body, dict) and "@rec" in json.dumps(body)):
        recs = call("GET", "/api/recurring")["body"]["recurring"]
        m2 = re.search(r"@rec(\d+)", path)
        if m2:
            t = recs[int(m2.group(1))]
            path = path.replace(m2.group(0), t["id"])
            if isinstance(body, dict) and body.get("count") == "@":
                body["count"] = t["count"]
        if isinstance(body, dict) and "ids" in body:
            body["ids"] = [recs[int(i[4:])]["id"] if isinstance(i, str) and i.startswith("@rec") else i for i in body["ids"]]
    return path, body


def norm(x):
    if isinstance(x, list):
        return [norm(v) for v in x]
    if isinstance(x, dict):
        # values that cannot match are blanked, not dropped: a key one side lacks is still a difference
        # (only where both sides can have a value: some text; anything else is compared as it is)
        def blank(v):
            return "*" if isinstance(v, str) and v else v
        out = {k: blank(v) if k in ("rev", "modified", "commit") else norm(v) for k, v in x.items()}
        if "every" in out and "id" in out:
            out["id"] = blank(out["id"])
        if "subject" in out:
            if "date" in out:
                out["date"] = blank(out["date"])
            for k in ("subject", "summary"):
                out[k] = re.sub(r"undo [0-9a-f]{10}", "undo X", out[k])
        return out
    return x


def steps():
    e, r, v = "/api/sheet/everyday/%d" % Y, "/api/sheet/rainy-day/-", "/api/sheet/visa/%d" % Y
    T = "/api/transfers"
    xfer = {"from_account": "everyday", "to_account": "rainy-day", "date": D, "amount": "25.00", "id": "1" * 16}
    R = "/api/recurring"
    month_ago = (TODAY - datetime.timedelta(days=30)).isoformat()
    out = [("GET", "/api/status"), ("GET", "/api/tabs"), ("GET", "/api/suggestions"), ("GET", R),
           ("GET", e), ("GET", "/api/sheet/everyday/%d" % (Y - 1)), ("GET", r), ("GET", v),
           ("GET", "/api/sheet/everyday/-"), ("GET", "/api/sheet/rainy-day/%d" % Y), ("GET", "/api/sheet/nope/-"),
           ("GET", "/api/sheet/UPPER/-"), ("GET", "/api/x"), ("DELETE", "/api/tabs", {}),
           ("POST", "/api/report", {}), ("POST", "/api/report", {"year": Y - 1}),
           ("POST", "/api/report", {"year": Y, "account": "everyday"}), ("POST", "/api/report", {"account": "visa"}),
           ("POST", "/api/report", {"account": "rainy-day", "year": Y - 1}),
           ("POST", "/api/report", {"year": 1800}), ("POST", "/api/report", {"account": "nope"}),
           ("POST", "/api/report", {"year": "2025"}), ("POST", "/api/report", {"year": 2030}),
           ("POST", "/api/search", {"text": "corner"}), ("POST", "/api/search", {"text": "MARKET", "account": "everyday"}),
           ("POST", "/api/search", {"amount_min": "20.00", "amount_max": "30.00"}),
           ("POST", "/api/search", {"amount_max": "0.00"}), ("POST", "/api/search", {"text": "108"}),
           ("POST", "/api/search", {"date_from": "%d-06-01" % (Y - 1), "date_to": "%d-06-30" % (Y - 1)}),
           ("POST", "/api/search", {"text": ""}), ("POST", "/api/search", {"amount_min": "x"}),
           ("POST", "/api/search", {"text": "\ud800"}), ("POST", "/api/search", {"a\udfff": 1}),  # half a surrogate pair
           ("POST", "/api/search", {"date_from": "2025-02-30"}), ("POST", "/api/search", {"text": "a​b"}),
           ("POST", "/api/search", {"text": "x" * 201}), ("POST", "/api/search", {"text": 5}),
           ("POST", "/api/search", {"nope": 1}), ("POST", "/api/search", {"text": "e", "balance": "1"}),
           # rows
           ("POST", e + "/rows", {"rev": "@", "index": 5, "fields": {"date": D, "description": " Café ", "amount": "-4.50",
                                                                   "category": "Food", "num": "7", "note": "n", "cleared": True}}),
           ("POST", e + "/rows", {"rev": "@", "index": 3}), ("POST", e + "/rows", {"rev": "@", "index": 99999}),
           ("POST", e + "/rows", {"rev": "@", "index": -1}), ("POST", e + "/rows", {"rev": "@", "index": 1.5}),
           ("POST", e + "/rows", {"rev": "@"}), ("POST", e + "/rows", {"rev": "@", "index": 0, "fields": []}),
           ("PUT", e + "/rows/3", {"rev": "@", "fields": {"description": " spaced ", "amount": "-12.00", "date": "%d-01-15" % Y}}),
           ("PUT", e + "/rows/3", {"rev": "@", "fields": {}}), ("PUT", e + "/rows/3", {"rev": "@", "fields": {"nope": 1}}),
           ("PUT", e + "/rows/3", {"rev": "@", "fields": {"cleared": "yes"}}),
           ("PUT", e + "/rows/3", {"rev": "@", "fields": {"date": "0000-01-01"}}),
           ("PUT", e + "/rows/3", {"rev": "@", "fields": {"date": "1899-12-31"}}),
           ("PUT", e + "/rows/3", {"rev": "@", "fields": {"date": "%d-2-3" % Y}}),
           ("PUT", e + "/rows/3", {"rev": "@", "fields": {"date": 5}}),
           ("PUT", e + "/rows/3", {"rev": "@", "fields": {"amount": "-0.00"}}),
           ("PUT", e + "/rows/3", {"rev": "@", "fields": {"amount": "1.5"}}),
           ("PUT", e + "/rows/3", {"rev": "@", "fields": {"amount": "999999999999.99"}}),
           ("PUT", e + "/rows/3", {"rev": "@", "fields": {"num": "x" * 21}}),
           ("PUT", e + "/rows/3", {"rev": "@", "fields": {"note": "tab\there"}}),
           ("PUT", e + "/rows/3", {"rev": "@", "fields": {"bank_id": "x"}}),
           ("PUT", e + "/rows/3", {"rev": "@", "fields": {"balance": "1.00"}}),
           ("PUT", e + "/rows/3", {"rev": "@", "fields": {"note": "same"}}),
           ("PUT", e + "/rows/3", {"rev": "@", "fields": {"note": "same"}}),  # no change: no commit
           ("PUT", e + "/rows/99999", {"rev": "@", "fields": {"note": "x"}}),
           ("PUT", e + "/rows/3", {"rev": "stale", "fields": {"note": "x"}}),
           ("PUT", e + "/rows/3", {"fields": {"note": "x"}}), ("PUT", e + "/rows/3", {"rev": 5, "fields": {"note": "x"}}),
           ("DELETE", e + "/rows/3", {"rev": "@"}), ("DELETE", e + "/rows/99999", {"rev": "@"}),
           # transfers
           ("POST", T, xfer), ("POST", T, xfer), ("POST", T, dict(xfer, amount="26.00")),
           ("POST", T, dict(xfer, to_account="everyday")), ("POST", T, dict(xfer, id="2" * 16, amount="0.00")),
           ("POST", T, dict(xfer, id="2" * 16, amount="-5.00")), ("POST", T, dict(xfer, id="2" * 16, date="")),
           ("POST", T, dict(xfer, id="xyz")), ("POST", T, dict(xfer, to_account="nope")),
           ("POST", T, {"from_account": "visa", "to_account": "everyday", "date": "%d-01-05" % (Y + 1), "amount": "10.00",
                        "id": "3" * 16, "description": "Refund", "note": "next year", "category": "Odd"}),
           ("POST", T, dict(xfer, id="4" * 16, date="%d-01-05" % (Y + 3))),
           ("POST", T, dict(xfer, id="4" * 16, date="%d-01-05" % (Y - 2))),
           ("GET", "/api/tabs"), ("GET", e), ("GET", r), ("GET", "/api/sheet/everyday/%d" % (Y + 1)),
           ("GET", "/api/sheet/visa/%d" % (Y + 1)),
           ("PUT", r + "/rows/@last", {"rev": "@", "fields": {"amount": "30.00", "date": (TODAY - datetime.timedelta(days=1)).isoformat()}}),
           ("GET", e),
           ("PUT", e + "/rows/@last", {"rev": "@", "fields": {"description": "Moved", "cleared": True}}), ("GET", r),
           ("PUT", e + "/rows/@last", {"rev": "@", "fields": {"amount": ""}}), ("GET", r),
           ("PUT", "/api/sheet/visa/%d/rows/@last" % (Y + 1), {"rev": "@", "fields": {"amount": "-11.00"}}),
           ("GET", "/api/sheet/everyday/%d" % (Y + 1)),
           ("DELETE", e + "/rows/@last", {"rev": "@"}), ("GET", r),
           ("DELETE", "/api/sheet/everyday/%d/rows/@last" % (Y + 1), {"rev": "@"}), ("GET", "/api/sheet/visa/%d" % (Y + 1)),
           # a seeded transfer: edit the Everyday side in last year, see Rainy Day follow
           ("POST", "/api/search", {"text": "Transfer to Rainy Day", "account": "everyday", "date_to": "%d-02-01" % (Y - 1)}),
           ("PUT", "/api/sheet/everyday/%d/rows/7" % (Y - 1), {"rev": "@", "fields": {"amount": "-250.00"}}), ("GET", r),
           # reconcile
           ("POST", e + "/reconcile", {"rev": "@", "cleared": [0, 1], "uncleared": [2]}),
           ("POST", e + "/reconcile", {"rev": "@", "cleared": [0, 0]}), ("POST", e + "/reconcile", {"rev": "@", "cleared": []}),
           ("POST", e + "/reconcile", {"rev": "@", "cleared": [99999]}), ("POST", e + "/reconcile", {"rev": "@", "cleared": ["1"]}),
           ("POST", e + "/reconcile", {"rev": "@", "cleared": [1], "uncleared": [1]}),
           ("POST", e + "/reconcile", {"rev": "@", "cleared": [0, 1]}),  # no change
           # history and undo
           ("GET", e + "/history", None, True), ("POST", e + "/undo", {"rev": "@", "commit": "@1"}),
           ("POST", e + "/undo", {"rev": "@", "commit": "@3"}), ("POST", e + "/undo", {"rev": "@", "commit": "f" * 40}),
           ("POST", e + "/undo", {"rev": "x", "commit": "@1"}), ("GET", e + "/history", None, True),
           ("POST", e + "/rows", {"rev": "@", "index": 2, "fields": {"description": "one"}}),
           ("POST", e + "/rows", {"rev": "@", "index": 3, "fields": {"description": "two"}}),
           ("DELETE", e + "/rows/2", {"rev": "@"}), ("GET", e + "/history", None, True),
           ("POST", e + "/undo", {"rev": "@", "commit": "@0"}), ("POST", e + "/undo", {"rev": "@", "commit": "@2"}),
           ("POST", e + "/undo", {"rev": "@", "commit": "@3"}), ("GET", e + "/history", None, True), ("GET", e),
           ("GET", r + "/history", None, True), ("GET", "/api/sheet/nope/-/history"),
           # recurring
           ("POST", R, {"account": "rainy-day", "description": " Weekly pocket ", "amount": "-10.00", "every": "week", "start": month_ago,
                        "category": " Fun ", "note": "w"}),
           ("POST", R, {"account": "everyday", "description": "x", "amount": "1.00", "every": "day", "start": D}),
           ("POST", R, {"account": "everyday", "description": "x", "amount": "", "every": "week", "start": D}),
           ("POST", R, {"account": "everyday", "description": "x", "amount": "1", "every": "week", "start": D}),
           ("POST", R, {"account": "everyday", "description": "  ", "amount": "1.00", "every": "week", "start": D}),
           ("POST", R, {"account": "everyday", "description": "x", "amount": "1.00", "every": "week", "start": ""}),
           ("POST", R, {"account": "everyday", "description": "x", "amount": "1.00", "every": "week", "start": "nope"}),
           ("POST", R, {"account": "nope", "description": "x", "amount": "1.00", "every": "week", "start": D}),
           ("POST", R, {"account": "everyday", "description": "x", "amount": "1.00", "every": "week"}),
           ("POST", R, {"account": "everyday", "description": "End of month", "amount": "-3.00", "every": "month",
                        "start": "%d-08-31" % (Y - 1)}),
           ("POST", R, {"account": "visa", "description": "Quarterly", "amount": "-7.00", "every": "quarter", "start": "%d-11-30" % (Y - 1)}),
           ("POST", R, {"account": "rainy-day", "description": "Yearly", "amount": "9.00", "every": "year", "start": "2024-02-29"}),
           ("POST", R, {"account": "rainy-day", "description": "Fortnight", "amount": "2.00", "every": "2weeks", "start": "%d-12-25" % (Y - 1)}),
           ("POST", R + "/@rec2/skip", {"count": "@"}), ("POST", R + "/@rec2/skip", {"count": 0}), ("POST", R + "/@rec2/skip", {}),
           ("POST", R + "/enter", {"ids": ["@rec0"]}), ("POST", R + "/enter", {"ids": ["@rec0"]}),
           ("POST", R + "/enter", {"ids": ["0" * 16]}), ("POST", R + "/enter", {"ids": [5]}),
           ("POST", R + "/enter", {}), ("POST", R + "/enter", {}), ("GET", e), ("GET", r), ("GET", v),
           ("GET", "/api/sheet/everyday/%d" % (Y - 1)), ("GET", e + "/history", None, True),
           ("POST", R, {"account": "visa", "description": "Too early", "amount": "-7.00", "every": "year", "start": "%d-03-01" % (Y - 4)}),
           ("POST", R + "/enter", {}), ("DELETE", R + "/@rec7", {}), ("DELETE", R + "/" + "0" * 16, {}),
           ("DELETE", R + "/nope", {}), ("GET", R),
           # tabs and accounts
           ("POST", "/api/accounts/everyday/years", {}), ("POST", "/api/accounts/rainy-day/years", {}),
           ("POST", "/api/accounts/nope/years", {}),
           ("POST", "/api/accounts", {"name": " Café Crème! ", "type": "cash", "opening_balance": "12.00"}),
           ("POST", "/api/accounts", {"name": "Cafe creme", "type": "loan", "opening_balance": "-900.00"}),
           ("POST", "/api/accounts", {"name": "日本", "type": "checking", "opening_balance": ""}),
           ("POST", "/api/accounts", {"name": "x" * 60, "type": "card", "opening_balance": "0.00", "year": 2001, "owed_positive": False}),
           ("POST", "/api/accounts", {"name": "x" * 60, "type": "savings", "opening_balance": "0.00"}),
           ("POST", "/api/accounts", {"name": "y", "type": "checking", "opening_balance": "0.00", "year": 1800}),
           ("POST", "/api/accounts", {"name": "y", "type": "savings", "opening_balance": "0.00", "year": 2020}),
           ("POST", "/api/accounts", {"name": "y", "type": "x", "opening_balance": "0.00"}),
           ("POST", "/api/accounts", {"name": "  ", "type": "cash", "opening_balance": "0.00"}),
           ("POST", "/api/accounts", {"name": "y" * 61, "type": "cash", "opening_balance": "0.00"}),
           ("POST", "/api/accounts", {"name": "y", "type": "cash", "opening_balance": "abc"}),
           ("POST", "/api/accounts", {"name": "y", "type": "cash", "opening_balance": "0.00", "owed_positive": "yes"}),
           ("POST", "/api/accounts", {"name": "y", "type": "cash"}),
           ("PATCH", "/api/accounts/visa", {"name": " Visa Card ", "owed_positive": False}),
           ("PATCH", "/api/accounts/visa", {"name": "Visa Card"}), ("PATCH", "/api/accounts/nope", {"name": "x"}),
           ("PATCH", "/api/accounts/visa", {"name": ""}), ("PATCH", "/api/accounts/visa", {}),
           ("GET", "/api/tabs"), ("GET", v), ("GET", "/api/sheet/cafe-creme/-"), ("GET", "/api/sheet/cafe-creme-2/-"),
           ("GET", "/api/sheet/account/%d" % Y), ("GET", "/api/suggestions"), ("POST", "/api/report", {}),
           ("POST", "/api/report", {"year": Y - 1}), ("GET", R),
           ("POST", "/api/search", {"text": "e"}),
           # more changes than History lists at once: nothing is cut here, so "more" is compared
           *[("PUT", r + "/rows/1", {"rev": "@", "fields": {"note": "n%d" % i}}) for i in range(31)],
           ("GET", r + "/history", None, True),
           ("POST", "/api/export", {"password": "x"}),
           ("POST", "/api/password", {"password": "x", "new_password": "y"})]
    return out


def ops_of(method, path):
    return {op for m, pattern, op, _, _ in ROUTES if m == method and pattern.match(path)}


def main(argv):
    server, base = serve(Path(argv[1]))
    bad, covered = 0, set()
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
        home, ledger, real = make_real(tmp)
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(base + PREFIX)
        page.wait_for_selector("#grid tbody td.c-cat")
        demo = functools.partial(api, page)
        load(home, ledger, demo)
        todo = steps()
        # what the demo's History holds for each sheet before any step: the entries its own data starts with
        start = {path: len(demo("GET", path)["body"]["entries"]) for path in {s[1] for s in todo if len(s) > 3}}
        for n, step in enumerate(todo):
            method, path, body, history = (step + (None, None))[:4]
            absent = ops_of(method, path) & NOT_COMPARED
            got = []
            for call in ((demo,) if absent else (real, demo)):
                p2, b2 = resolve(call, method, path, body)
                if not got:
                    covered |= ops_of(method, p2)
                got.append(call(method, p2, b2))
            if absent:
                ok = got[0]["status"] == 501
                bad += not ok
                print("%3d %s %-45s demo only: %s%s" % (n, method, path, got[0]["status"], "" if ok else "  DIFF: not 501"))
                continue
            if history and got[0]["status"] == got[1]["status"] == 200:
                theirs, ours = (g["body"] for g in got)
                subjects = [x["subject"] for x in theirs["entries"]]
                # the real side: up to where its own loading of the data begins
                cut = subjects.index("demo data") if "demo data" in subjects else len(subjects)
                whole = cut == len(subjects)  # "more" only means the same on both sides when nothing was cut
                # past the part compared, the demo may hold only what it started with (fewer if its
                # list was cut short at the limit): no change the real app does not have
                left = len(ours["entries"]) - cut
                if not whole and left != start[path] and not (ours["more"] and 0 <= left < start[path]):
                    bad += 1
                    print("%3d DIFF %s %s: the demo lists %d entries before the first one compared, "
                          "and started with %d" % (n, method, path, left, start[path]))
                for g in got:
                    g["body"] = {**g["body"], "entries": g["body"]["entries"][:cut],
                                 "more": g["body"]["more"] if whole else None}
            got = [norm(g) for g in got]
            print("%3d %s %-45s %s %s %d" % (n, method, path[:45], got[0]["status"], got[1]["status"], len(json.dumps(got[1]))), (got[1]["body"].get("message", "") if isinstance(got[1]["body"], dict) else "")[:90])
            if got[0] != got[1]:
                bad += 1
                a, b = json.dumps(got[0], sort_keys=True), json.dumps(got[1], sort_keys=True)
                i = len(os.path.commonprefix([a, b]))
                print("%3d DIFF %s %s %s\n    real: ...%s\n    demo: ...%s" % (n, method, path, json.dumps(body)[:150],
                                                                             a[max(0, i - 150):i + 250], b[max(0, i - 150):i + 250]))
        browser.close()
    missing = {op for _, _, op, _, _ in ROUTES} - covered  # every operation the app has must have a step
    if missing:
        print("no step for: %s" % ", ".join(sorted(missing)))
    print("%d steps, %d differ" % (len(todo), bad))
    sys.exit(1 if bad or missing else 0)


if __name__ == "__main__":
    main(sys.argv)
