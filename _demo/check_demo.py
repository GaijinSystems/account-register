#!/usr/bin/env python3
"""Drive the built demo site (_demo/build_demo.py) in a real browser and check that the app's
page works against the in-memory stand-in, that nothing is stored in the browser, and that no
request leaves the page except for the site's own files.

    python3 _demo/check_demo.py SITE [SCREENSHOT.png]

Needs Playwright for Python with Chromium, Firefox and WebKit. The site is served from this process on localhost,
under a sub-path, as a static host would.
"""
import calendar
import datetime
import functools
import http.server
import json
import re
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

PREFIX = "/some/sub-path/"  # the demo must not care where it is hosted
API = """async ([method, path, body]) => {
  const res = await fetch(path, method === "GET" ? {} : { method, body: JSON.stringify(body) });
  return { status: res.status, body: await res.json() };
}"""


class Handler(http.server.SimpleHTTPRequestHandler):
    """The site under PREFIX and nothing else; every request is noted in self.server.seen."""

    def translate_path(self, path):
        self.server.seen.append(self.command + " " + path)
        if not path.startswith(PREFIX):
            return "/nonexistent"
        return super().translate_path("/" + path[len(PREFIX):])

    def log_message(self, *args):
        pass


def serve(site):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(site)))
    server.seen = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d" % server.server_address[1]


def cents(text):
    """'-$1,234.56' -> -123456"""
    digits = re.sub(r"[^0-9]", "", text)
    return (-1 if text.strip().startswith("-") else 1) * int(digits)


def api(page, method, path, body=None):
    return page.evaluate(API, [method, path, body])


def wait_until(page, test, what):
    for _ in range(100):
        if test():
            return
        page.wait_for_timeout(50)
    raise AssertionError("timed out waiting for: " + what)


def rows(page):
    return page.locator("#grid tbody tr:not(.newrow):not(.carried)")


def cell(row, cls):
    return row.locator("td." + cls).inner_text()


def said(page, text):
    page.locator("#save-status", has_text=text).wait_for()


def open_tab(page, account, year=None):
    group = page.locator("#tabs .tab-group", has_text=account)
    group.locator(".tab", has_text=account if year is None else str(year)).click()
    page.locator("#sheet-title", has_text=account if year is None else "%s — %d" % (account, year)).wait_for()


def snapshot(page):
    """Everything the stand-in holds, as the page would be given it (without the save times)."""
    tabs = api(page, "GET", "/api/tabs")["body"]
    sheets = {}
    for a in tabs["accounts"]:
        for y in a["years"] or [None]:
            sheet = api(page, "GET", "/api/sheet/%s/%s" % (a["id"], "-" if y is None else y))["body"]
            del sheet["modified"]
            sheets["%s/%s" % (a["id"], y)] = sheet
    return json.dumps([tabs, sheets, api(page, "GET", "/api/recurring")["body"],
                       api(page, "GET", "/api/suggestions")["body"]], sort_keys=True)


def browser_storage(context, page, origin):
    """What the browser itself holds for the origin, asked over the DevTools protocol: the page's
    own view of localStorage is the stand-in, so asking the page would prove nothing."""
    cdp = context.new_cdp_session(page)
    held = {}
    for name, local in (("localStorage", True), ("sessionStorage", False)):
        held[name] = cdp.send("DOMStorage.getDOMStorageItems",
                              {"storageId": {"securityOrigin": origin, "isLocalStorage": local}})["entries"]
    held["cookies"] = cdp.send("Network.getAllCookies")["cookies"] + context.cookies()
    held["indexedDB"] = cdp.send("IndexedDB.requestDatabaseNames", {"securityOrigin": origin})["databaseNames"]
    held["caches"] = cdp.send("CacheStorage.requestCacheNames", {"securityOrigin": origin})["caches"]
    usage = cdp.send("Storage.getUsageAndQuota", {"origin": origin})
    held["usage"] = [u for u in usage["usageBreakdown"] if u["usage"]]
    cdp.detach()
    return held


def check(page, context, base, today, shot):
    year = today.year
    # on 1 January the recurring row that is due goes into last year's tab, so it is not in this sheet
    jan1 = (today.month, today.day) == (1, 1)
    page.goto(base + PREFIX)
    page.wait_for_selector("#grid tbody td.c-cat")
    assert not page.locator("#demo-failed").is_visible()  # the "did not start" message
    assert page.locator("#demo-notice").is_visible() and "made up" in page.locator("#demo-notice").inner_text()
    assert page.locator("#sheet-title").inner_text() == "Everyday — %d" % year
    assert rows(page).count() >= 2 and page.locator("#grid tbody tr.carried").count() == 1
    assert page.locator("#btn-recurring").inner_text() == "Recurring (1 due)…"
    start = snapshot(page)
    start_rows = rows(page).count()

    # typing a row into the grid: saved when the row is left, and the balance follows
    before = cents(cell(rows(page).last, "c-bal"))
    page.locator("#grid tbody tr.newrow td.c-desc").first.click()
    page.keyboard.type("Test shop")
    page.keyboard.press("Tab")
    page.keyboard.type("Food")
    page.keyboard.press("Tab")
    page.keyboard.type("12.34")  # debit mode: a plain number is money out
    page.keyboard.press("Enter")
    said(page, "saved")
    wait_until(page, lambda: rows(page).count() == start_rows + 1, "the new row")
    last = rows(page).last
    assert (cell(last, "c-desc"), cell(last, "c-cat"), cell(last, "c-amt")) == ("Test shop", "Food", "-$12.34")
    assert cents(cell(last, "c-bal")) == before - 1234, cell(last, "c-bal")
    # autocomplete from the made-up data
    page.locator("#grid tbody tr.newrow td.c-desc").first.click()
    page.keyboard.type("corn")
    page.locator("#suggest li", has_text="Corner Market").wait_for()
    page.keyboard.press("Escape")
    page.keyboard.press("Escape")

    # a transfer: one linked row in each account; deleting one side deletes the other
    page.click("#btn-transfer")
    page.fill("#form-transfer input[name=amount]", "50")
    assert page.locator("#form-transfer select[name=target]").input_value() == "rainy-day"
    page.click("#transfer-submit")
    said(page, "Transfer added to Everyday and Rainy Day.")
    last = rows(page).last
    assert (cell(last, "c-desc"), cell(last, "c-amt")) == ("Transfer to Rainy Day", "-$50.00")
    assert "xfer" in last.locator("td.c-desc").get_attribute("class")
    assert cents(cell(last, "c-bal")) == before - 1234 - 5000
    open_tab(page, "Rainy Day")
    last = rows(page).last
    assert (cell(last, "c-desc"), cell(last, "c-amt"), cell(last, "c-date")) == \
        ("Transfer from Everyday", "$50.00", today.strftime("%m/%d/%Y"))
    # changing the amount on this side changes the other
    last.locator("td.c-amt").click()
    page.keyboard.type("75")
    page.keyboard.press("Enter")
    said(page, "saved")
    open_tab(page, "Everyday", year)
    assert cell(rows(page).last, "c-amt") == "-$75.00"
    open_tab(page, "Rainy Day")
    count = rows(page).count()
    rows(page).last.locator("td.c-desc").click()
    page.click("#btn-delete")  # the confirm is accepted by the dialog handler
    wait_until(page, lambda: rows(page).count() == count - 1, "the transfer row to go")
    open_tab(page, "Everyday", year)
    assert cell(rows(page).last, "c-desc") == "Test shop" and rows(page).count() == start_rows + 1

    # search across the years, then jump to a hit
    page.keyboard.press("Control+f")
    page.fill("#form-search input[name=text]", "bookshop")
    page.select_option("#form-search select[name=scope]", "all")
    page.click("#search-submit")
    page.locator("#search-count", has_text=re.compile(r"^\d+ rows$")).wait_for()
    assert page.locator("#search-rows tr", has_text="Visa").count() == 1
    page.locator("#search-rows tr").first.click()
    page.locator("#sheet-title", has_text=str(year - 1)).wait_for()
    assert page.locator("#cell-value").inner_text() == "Bookshop"
    page.click("#btn-search")
    page.fill("#form-search input[name=text]", "")
    page.click("#search-submit")
    page.locator("#search-error", has_text="enter something to search for").wait_for()  # the server's own words
    page.click("#search-close")
    open_tab(page, "Everyday", year)

    # reconcile: tick the last row against a statement, save the ticks
    page.click("#btn-reconcile")
    page.fill("#form-reconcile input[name=balance]", "100")
    page.click("#form-reconcile button[type=submit]")
    page.locator("#reconbar").wait_for()
    assert cell(rows(page).last, "c-x") == ""
    rows(page).last.locator("td.c-x").click()
    assert cell(rows(page).last, "c-x") == "x" and page.locator("#recon-diff").inner_text() != ""
    page.click("#recon-finish")  # the difference is not zero: the confirm is accepted
    said(page, "Ticks saved")
    assert cell(rows(page).last, "c-x") == "x" and not page.locator("#reconbar").is_visible()

    # recurring: enter the one that is due, add one, skip it, delete it
    page.click("#btn-recurring")
    assert page.locator("#recurring-rows tr").count() == 2
    page.click("#recurring-enter")
    said(page, "Entered 1 recurring transaction.")
    assert page.locator("#btn-recurring").inner_text() == "Recurring…"
    page.fill("#form-recurring input[name=description]", "Magazine")
    page.fill("#form-recurring input[name=amount]", "-9.50")
    page.fill("#form-recurring input[name=start]", "1/31/%d" % (year + 1))
    page.click("#recurring-add")
    magazine = page.locator("#recurring-rows tr", has_text="Magazine")
    magazine.wait_for()
    assert "01/31/%d" % (year + 1) in magazine.inner_text()
    magazine.locator("button", has_text="Skip").click()
    page.locator("#recurring-rows tr", has_text="02/%d/%d" % (29 if calendar.isleap(year + 1) else 28, year + 1)).wait_for()  # no 31st: the last day
    magazine.locator("button", has_text="Delete").click()
    wait_until(page, lambda: page.locator("#recurring-rows tr").count() == 2, "the template to go")
    page.click("#recurring-close")
    if not jan1:
        assert cell(rows(page).last, "c-desc") == "Gym membership"

    # reports
    page.click("#btn-reports")
    page.locator("#report-income", has_text="$").wait_for()
    assert page.locator("#report-months path.bar").count() > 0
    assert page.locator("#report-categories dt", has_text="Housing").count() == 1
    assert page.locator("#report-categories dt", has_text="Transfer").count() == 0
    page.select_option("#report-year", str(year - 1))
    page.locator("#report-income", has_text="$33,299.90").wait_for()  # 24 paychecks and four quarters of interest
    page.click("#reports-close")

    # history: what changed, and undo of the latest change that can be undone (the reconcile)
    page.click("#btn-history")
    page.locator("#history-list > li").first.wait_for()
    entries = page.locator("#history-list > li")
    assert entries.count() >= 6, entries.count()
    if not jan1:
        assert "Cannot be undone here" in entries.first.inner_text()  # entering a recurring row changed two files
    recon = entries.nth(0 if jan1 else 1)
    assert "Reconcile (1 rows)" in recon.inner_text()
    recon.locator("button", has_text="Undo").click()
    said(page, "Undone.")
    page.click("#history-close")
    assert cell(rows(page).nth(rows(page).count() - (1 if jan1 else 2)), "c-x") == ""

    # account settings, a new account, a new year
    page.locator("#tabs .tab-label", has_text="Visa").dblclick()
    page.fill("#form-settings input[name=name]", "Visa Card")
    page.uncheck("#form-settings input[name=owed]")
    page.click("#form-settings button[type=submit]")
    page.locator("#tabs .tab-label", has_text="Visa Card:").wait_for()
    open_tab(page, "Visa Card", (today - datetime.timedelta(days=6)).year)  # the tab holding the newest charge
    assert cell(rows(page).last, "c-amt").startswith("-$")  # owed no longer shown as positive
    page.click("#tabs .tab-add-account")
    page.fill("#form-account input[name=name]", "Wallet")
    page.select_option("#form-account select[name=type]", "cash")
    page.fill("#form-account input[name=opening]", "40")
    page.click("#form-account button[type=submit]")
    page.locator("#sheet-title", has_text="Wallet").wait_for()
    assert (cell(rows(page).first, "c-desc"), cell(rows(page).first, "c-bal")) == ("Opening balance", "$40.00")
    page.locator("#tabs .tab-group", has_text="Everyday").locator(".tab-add").click()  # confirm accepted
    page.locator("#sheet-title", has_text="Everyday — %d" % (year + 1)).wait_for()
    assert "Carried forward from %d" % year in page.locator("#grid tbody tr.carried").inner_text()

    # what the demo does not have says so
    # Export and Security... are there, but no password can be typed into this public page
    assert page.locator("input[type=password]").count() == 4
    assert page.locator("input[type=password]:not(:disabled)").count() == 0
    page.click("#btn-export")
    page.click("#export-submit")
    page.locator("#export-error", has_text="not available in the demo").wait_for()
    page.click("#export-cancel")
    page.click("#btn-security")
    page.click("#password-submit")
    page.locator("#password-error", has_text="no password in the demo").wait_for()
    page.click("#security-close")

    # the two preferences the real app keeps in localStorage: they work, for this page only
    open_tab(page, "Everyday", year)
    assert page.locator("#btn-sign").inner_text() == "− Debit mode"
    page.click("#btn-sign")
    assert page.locator("#btn-sign").inner_text() == "± As typed"
    page.click("#btn-theme")
    assert page.evaluate("document.documentElement.dataset.theme") == "light"

    # the phone layout
    page.set_viewport_size({"width": 400, "height": 760})
    page.locator("#rows li button").first.wait_for()
    page.set_viewport_size({"width": 1180, "height": 760})

    # the answers themselves: statuses and the {error, message, field} shape
    sheet = "/api/sheet/everyday/%d" % year
    rev = api(page, "GET", sheet)["body"]["rev"]
    stale = api(page, "PUT", sheet + "/rows/0", {"rev": "old", "fields": {"note": "x"}})
    assert stale["status"] == 409 and stale["body"]["error"] == "stale", stale
    bad = api(page, "PUT", sheet + "/rows/0", {"rev": rev, "fields": {"amount": "12"}})
    assert bad == {"status": 400, "body": {"error": "invalid", "field": "amount",
                                           "message": "must be an amount like -84.12 or 1234.00 (or blank)"}}, bad
    bad = api(page, "PUT", sheet + "/rows/0", {"rev": rev, "fields": {"balance": "1.00"}})
    assert bad["status"] == 400 and bad["body"]["error"] == "balance_readonly", bad
    bad = api(page, "PUT", sheet + "/rows/0", {"rev": rev, "fields": {"transfer": ""}})
    assert bad["body"] == {"error": "invalid", "field": "transfer", "message": "is set by the app and cannot be edited"}
    assert api(page, "POST", sheet + "/rows", {"rev": rev})["body"]["message"] == "missing field: index"
    assert api(page, "GET", "/api/sheet/everyday/1999")["status"] == 404
    assert api(page, "GET", "/api/nothing")["status"] == 404
    assert api(page, "PUT", "/api/tabs", {})["status"] == 405
    transfer = {"from_account": "everyday", "to_account": "rainy-day", "date": today.isoformat(), "amount": "5.00",
                "id": "0123456789abcdef"}
    first = api(page, "POST", "/api/transfers", transfer)
    assert first["status"] == 200 and first["body"]["to"] == {"account": "rainy-day", "year": None}, first
    assert api(page, "POST", "/api/transfers", transfer) == first  # sent twice: entered once
    assert api(page, "POST", "/api/transfers", dict(transfer, amount="6.00"))["status"] == 422
    far = api(page, "POST", "/api/transfers", dict(transfer, id="aaaaaaaaaaaaaaaa", date="%d-01-01" % (year + 5)))
    assert far["status"] == 400 and far["body"]["field"] == "date" and "Add the years in between first" in far["body"]["message"]
    refused = page.evaluate("fetch('https://example.com/').then(() => 'answered', (e) => e.message)")
    assert refused == "The demo makes no network requests.", refused

    # nothing was kept in the browser by any of that
    origin = base
    held = browser_storage(context, page, origin)
    assert not any(held.values()), held
    state = context.storage_state()
    assert state == {"cookies": [], "origins": []}, state
    assert page.evaluate("[localStorage.length, sessionStorage.length, document.cookie]") == [2, 0, ""]  # the stand-in

    # "Log out" starts over, and so does a reload: the starting data, the dark theme, debit mode
    assert snapshot(page) != start
    with page.expect_navigation():
        page.click("#btn-logout")
    page.wait_for_selector("#grid tbody td.c-cat")
    assert snapshot(page) == start
    page.click("#btn-theme")
    page.reload()
    page.wait_for_selector("#grid tbody td.c-cat")
    assert snapshot(page) == start and rows(page).count() == start_rows
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    assert page.locator("#btn-sign").inner_text() == "− Debit mode"
    assert page.evaluate("[localStorage.length, sessionStorage.length, document.cookie]") == [0, 0, ""]
    assert not any(browser_storage(context, page, origin).values())
    if shot:
        page.screenshot(path=shot)


def control(browser, base):
    """The storage check can see a write: a page of the same site without the stand-in makes one."""
    context = browser.new_context()
    page = context.new_page()
    page.goto(base + PREFIX + "demo.css")
    page.evaluate("localStorage.setItem('control', '1')")
    held = browser_storage(context, page, base)
    assert held["localStorage"] == [["control", "1"]], held
    context.close()


def nothing_stored(context, label):
    state = context.storage_state()
    assert not state["cookies"] and not any(o.get("localStorage") for o in state["origins"]), (label, state)


def fails_closed(browser, base):
    """If any of the three demo scripts does not load, the page's own script must never start: a
    page that says so, not one that quietly uses the browser's real storage or network. If the
    page's own script is what does not load, the page says so too."""
    for missing in ("demo-nostore.js", "demo-api.js", "demo-spec.js", "grid.js"):
        context = browser.new_context()
        context.route("**/" + missing, lambda route: route.abort())
        page = context.new_page()
        asked = []
        page.on("request", lambda r: asked.append(r.url.rsplit("/", 1)[-1]))
        page.goto(base + PREFIX)
        page.wait_for_timeout(500)
        page.locator("#demo-failed").wait_for(state="visible", timeout=5000)  # the visitor is told
        assert missing == "grid.js" or "grid.js" not in asked, "%s missing, and grid.js still ran" % missing
        assert page.locator("#grid tr").count() == 0, missing
        nothing_stored(context, missing)
        context.close()
    # a script that loads and then stops with an error counts the same
    context = browser.new_context()
    context.route("**/demo-spec.js", lambda route: route.fulfill(body="throw new Error('x')",
                                                                 content_type="text/javascript"))
    page = context.new_page()
    asked = []
    page.on("request", lambda r: asked.append(r.url.rsplit("/", 1)[-1]))
    page.goto(base + PREFIX)
    page.wait_for_timeout(500)
    assert "grid.js" not in asked and page.locator("#demo-failed").is_visible()
    context.close()
    # without demo.css the page still works, and does not say that it did not start
    context = browser.new_context()
    context.route("**/demo.css", lambda route: route.abort())
    page = context.new_page()
    page.goto(base + PREFIX)
    page.wait_for_selector("#grid tbody td.c-cat")
    assert not page.locator("#demo-failed").is_visible()
    context.close()


def other_browser(p, name, base):
    """The storage stand-in is a replaced property of window, which engines may treat differently:
    in Firefox and WebKit too, the page must work, keep its two preferences for as long as it is
    open, hold nothing in the browser, and forget them on a reload."""
    browser = getattr(p, name).launch()
    context = browser.new_context(viewport={"width": 1180, "height": 760})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    theme = "() => document.documentElement.dataset.theme"
    page.goto(base + PREFIX)
    page.wait_for_selector("#grid tbody td.c-cat")
    assert not page.locator("#demo-failed").is_visible(), name
    before = page.evaluate(theme)
    page.click("#btn-theme")
    page.click("#btn-sign")
    assert page.evaluate(theme) != before, name
    nothing_stored(context, name)
    page.reload()
    page.wait_for_selector("#grid tbody td.c-cat")
    assert page.evaluate(theme) == before, "%s kept the theme across a reload" % name
    assert not errors, (name, errors)
    browser.close()


def main(argv):
    if len(argv) not in (2, 3):
        sys.exit(__doc__.strip())
    site = Path(argv[1]).resolve()
    # the site's files: not .git or _demo/ when SITE is the gh-pages checkout (Pages publishes neither)
    files = sorted(p.name for p in site.iterdir() if p.is_file() and not p.name.startswith((".", "_")))
    server, base = serve(site)
    asked, console = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1180, "height": 760})
        page = context.new_page()
        page.on("request", lambda r: asked.append(r.method + " " + r.url))
        page.on("console", lambda m: console.append("%s: %s" % (m.type, m.text)) if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: console.append("exception: %s" % e))
        page.on("dialog", lambda d: d.accept())
        check(page, context, base, datetime.date.today(), argv[2] if len(argv) == 3 else None)
        context.close()
        control(browser, base)
        fails_closed(browser, base)
        browser.close()
        for name in ("firefox", "webkit"):
            other_browser(p, name, base)
    server.shutdown()
    assert not console, console
    # every request the page made, and every request the server saw, was for one of the site's files
    allowed = {"GET " + base + PREFIX} | {"GET " + base + PREFIX + name for name in files}
    assert set(asked) <= allowed, sorted(set(asked) - allowed)
    seen = {s for s in server.seen if s != "GET /favicon.ico"}  # the browser's own, not the page's
    assert seen <= {"GET " + PREFIX} | {"GET " + PREFIX + name for name in files}, sorted(seen)
    loaded = {name for name in files if name not in ("index.html", "CNAME")}  # CNAME is for the host, not the page
    assert {"GET " + PREFIX + name for name in loaded} <= seen, sorted(seen)
    print("ok: %d page requests, all for the site's %d files; nothing stored in the browser "
          "(Chromium in full; Firefox and WebKit for storage)" % (len(asked), len(files)))


if __name__ == "__main__":
    main(sys.argv)
