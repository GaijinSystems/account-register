#!/usr/bin/env python3
"""Take the README screenshots from a demo home (tools/demo_home.py), and check the Security
dialog in a real browser on the way. Needs Playwright for Python with Chromium, and the
app's own requirements; the app must not already be running on port 8000.

    python3 tools/screenshots.py OUTDIR
"""
import datetime
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

import demo_app
from demo_app import URL, login


def check(ok):
    assert ok


def open_tab(page, tab, selector="#grid tbody td.c-cat"):
    """Go to a sheet by its address (account/year, or account/- for a single tab), freshly loaded."""
    page.goto(URL + "/#" + tab)
    page.reload()
    page.wait_for_selector(selector)


def main(argv):
    if len(argv) != 2:
        sys.exit(__doc__.strip())
    out = Path(argv[1])
    out.mkdir(parents=True, exist_ok=True)
    with demo_app.running():
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_context(viewport={"width": 1180, "height": 760}, device_scale_factor=2).new_page()
            login(page)
            open_tab(page, "everyday/2026", "#grid tr")
            page.screenshot(path=out / "register.png")
            page.click("#btn-security")
            page.screenshot(path=out / "security.png")

            # typing into Category, and into Num and X on a savings tab, saves
            page.keyboard.press("Escape")
            cell = page.locator("#grid tbody tr:not(.carried) td.c-cat").first
            cell.click()
            page.keyboard.type("Home")
            page.keyboard.press("Enter")
            open_tab(page, "rainy-day/-", "#grid tbody td.c-num")
            page.locator("#grid tbody td.c-num").first.click()
            page.keyboard.type("77")
            page.keyboard.press("Tab")
            page.locator("#grid tbody td.c-x").first.click()
            page.keyboard.press("Space")
            page.keyboard.press("ArrowDown")
            page.locator("#save-status", has_text="Saved").wait_for()
            page.reload()
            page.wait_for_selector("#grid tbody td.c-num")
            assert page.locator("#grid tbody td.c-num").first.inner_text() == "77"
            assert page.locator("#grid tbody td.c-x").first.inner_text().strip() != ""
            open_tab(page, "everyday/2026")
            assert page.locator("#grid tbody tr:not(.carried) td.c-cat").first.inner_text() == "Home"

            # autocomplete: an earlier description brings its amount and category
            page.locator("#grid tbody tr.newrow td.c-desc").first.click()
            page.keyboard.type("corn")
            page.locator("#suggest li", has_text="Corner Market").wait_for()
            page.screenshot(path=out / "autocomplete.png")
            page.keyboard.press("ArrowDown")
            page.keyboard.press("Enter")
            page.locator("#save-status", has_text="Saved").wait_for()
            last = page.locator("#grid tbody tr:not(.newrow)").last
            assert last.locator("td.c-desc").inner_text() == "Corner Market"
            assert last.locator("td.c-cat").inner_text() == "Food"
            assert last.locator("td.c-amt").inner_text() == "-$106.82", last.locator("td.c-amt").inner_text()

            # search across the years, then jump to a hit
            page.keyboard.press("Control+f")
            page.fill("#form-search input[name=text]", "bookshop")
            page.fill("#form-search input[name=start]", "1/1/2025")
            page.fill("#form-search input[name=end]", "6/30/2025")
            page.click("#search-submit")
            page.locator("#search-count", has_text="6 rows").wait_for()
            page.screenshot(path=out / "search.png")
            page.locator("#search-rows tr").nth(2).click()
            page.locator("#sheet-title", has_text="2025").wait_for()
            assert page.locator("#cell-value").inner_text() == "Bookshop"
            assert page.locator("#grid td.sel").locator("xpath=../td[contains(@class,'c-date')]").inner_text() \
                == "03/27/2025"
            open_tab(page, "everyday/2026")

            # reconcile: tick the uncleared February rows until the difference is zero
            page.click("#btn-reconcile")
            page.fill("#form-reconcile input[name=balance]", "7,232.29")
            page.click("#form-reconcile button[type=submit]")
            page.locator("#recon-diff.off").wait_for()
            page.locator("#grid tbody tr:not(.carried) td.c-desc").first.dblclick()
            page.locator("#save-status", has_text="Finish or cancel reconciling first.").wait_for()
            xs = page.locator("#grid tbody tr:not(.newrow):not(.carried) td.c-x")
            for i in range(12, xs.count()):
                if i % 2:
                    xs.nth(i).click()  # one click ticks
                else:
                    xs.nth(i).dblclick()  # and so does a double-click: once, not twice
                if i == 14:
                    page.screenshot(path=out / "reconcile.png")
            page.locator("#recon-diff.zero").wait_for()
            assert page.locator("#recon-diff").inner_text() == "$0.00"
            page.click("#recon-finish")
            page.locator("#save-status", has_text="Reconciled.").wait_for()
            page.reload()
            page.wait_for_selector("#grid tbody td.c-x")
            assert all(t.strip() == "x" for t in xs.all_inner_texts()), xs.all_inner_texts()

            # recurring: add one that started last month, enter what is due, then delete it
            page.click("#btn-recurring")
            # (in the savings account, which has no year tabs, so this works in any year)
            first = (datetime.date.today().replace(day=1) - datetime.timedelta(days=1)).replace(day=1)
            due = 2  # the 1st of last month and the 1st of this one
            page.select_option("#form-recurring select[name=account]", "rainy-day")
            page.fill("#form-recurring input[name=description]", "Gym")
            page.fill("#form-recurring input[name=amount]", "-30")
            page.fill("#form-recurring input[name=start]", first.strftime("%m/%d/%Y"))
            page.click("#recurring-add")
            page.locator("#recurring-enter", has_text="due").wait_for()
            page.screenshot(path=out / "recurring.png")
            assert str(due) in page.inner_text("#recurring-enter"), page.inner_text("#recurring-enter")
            page.click("#recurring-enter")
            page.locator("#save-status", has_text="Entered").wait_for()
            assert page.locator("#btn-recurring").inner_text() == "Recurring\u2026"
            page.once("dialog", lambda d: d.accept())
            page.locator("#recurring-rows button", has_text="Delete").click()
            page.locator("#recurring-none:not(.hidden)").wait_for()
            page.click("#recurring-close")
            open_tab(page, "rainy-day/-")
            assert page.locator("#grid tbody td.c-desc", has_text="Gym").count() == due
            open_tab(page, "everyday/2026")

            # on paper: no toolbars, tabs or blank rows; a heading instead
            page.emulate_media(media="print")
            assert page.locator(".toolbar").is_hidden() and page.locator("#tabs").is_hidden()
            assert page.locator("#grid tr.newrow").first.is_hidden()
            assert page.locator("#grid thead tr.hdr-letters").is_hidden()
            assert page.locator("#print-title").inner_text().startswith("Everyday")
            assert page.locator("#grid tbody td.c-desc").first.is_visible()
            page.screenshot(path=out / "print.png", full_page=True)
            page.emulate_media(media="screen")

            # history: the recurring entry cannot be undone here; the category edit can
            page.click("#btn-history")
            page.locator("#history-list > li").first.wait_for()
            page.screenshot(path=out / "history.png")
            edit = page.locator("#history-list > li", has_text="Edit row 1 (category)")
            assert "Housing" in edit.inner_text() and "Home" in edit.inner_text()
            edit.locator("button", has_text="Undo").click()
            page.locator("#save-status", has_text="Undone.").wait_for()
            page.click("#history-close")
            assert page.locator("#grid tbody tr:not(.carried) td.c-cat").first.inner_text() == "Housing"

            # the keyboard reaches the buttons: F6 leaves the grid, Tab walks them, Esc comes back
            focused = "() => document.activeElement.id"
            page.locator("#grid tbody tr:not(.carried) td.c-desc").first.click()
            page.keyboard.press("F6")
            assert page.evaluate(focused) == "btn-insert", page.evaluate(focused)
            page.keyboard.press("Tab")
            assert page.evaluate(focused) == "btn-delete", page.evaluate(focused)
            page.keyboard.press("F6")  # on to the sheet tabs
            assert page.evaluate("() => document.activeElement.closest('#tabs') !== null")
            assert page.evaluate("() => document.activeElement.textContent") == "2026"
            page.keyboard.press("F6")  # and round to the grid again
            assert page.evaluate(focused) == "grid", page.evaluate(focused)
            page.keyboard.press("F6")
            page.keyboard.press("Escape")
            assert page.evaluate(focused) == "grid", page.evaluate(focused)
            page.keyboard.press("F1")  # help from anywhere
            page.locator("#dlg-help[open]").wait_for()
            page.keyboard.press("Escape")
            page.locator("#grid tbody tr:not(.carried) td.c-desc").first.click()
            page.keyboard.press("Tab")  # in the grid, Tab moves the cell
            assert page.inner_text("#cell-ref") == "D1", page.inner_text("#cell-ref")

            # a copied deposit pastes as a deposit, although debit mode is on
            paste = """(text) => {
                const data = new DataTransfer();
                data.setData("text/plain", text);
                document.dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, cancelable: true }));
            }"""
            page.locator("#grid tbody tr.newrow td.c-amt").first.click()
            page.evaluate(paste, "1385.20")
            assert page.locator("#grid tbody tr.newrow td.c-amt").first.inner_text() == "$1,385.20"
            page.keyboard.press("Delete")  # take it out again: nothing to save
            # switching debit mode in the middle of typing keeps the keyboard in the cell
            page.keyboard.type("12")
            page.click("#btn-sign")
            page.keyboard.type(".50")
            page.keyboard.press("Tab")
            assert page.locator("#grid tbody tr.newrow td.c-amt").first.inner_text() == "$12.50"
            page.click("#btn-sign")
            page.locator("#grid tbody tr.newrow td.c-amt").first.click()
            page.keyboard.press("Delete")
            page.locator(".tab-label", has_text="Everyday").click()  # a click on the label: keys still reach the grid
            page.keyboard.press("ArrowLeft")
            assert page.inner_text("#cell-ref").startswith("D"), page.inner_text("#cell-ref")
            page.locator("#grid tbody tr.newrow td.c-amt").first.click()
            assert page.locator("#grid tbody tr.newrow td.c-amt").first.inner_text() == ""
            page.click("#btn-help")  # and nothing is pasted into the grid behind a dialog
            page.evaluate(paste, "99.00")
            page.keyboard.press("Escape")
            assert page.locator("#grid tbody tr.newrow td.c-amt").first.inner_text() == ""

            # keyboard help
            page.keyboard.press("?")
            page.locator("#dlg-help[open]").wait_for()
            page.keyboard.press("Escape")

            # a phone-sized window: a list of rows, and a form to edit one
            page.set_viewport_size({"width": 390, "height": 760})
            page.locator("#rows button.tap").first.wait_for()
            assert page.locator("#grid").is_hidden()
            page.screenshot(path=out / "phone.png")
            page.locator("#rows button.tap", has_text="Harbor Electric").first.click()
            assert page.input_value("#form-row input[name=amount]") == "-80.72"
            page.fill("#form-row input[name=note]", "from the phone")
            page.screenshot(path=out / "phone-form.png")
            page.click("#row-save")
            page.locator("#rows li", has_text="from the phone").wait_for()
            # the sheet changes in another browser while a row's form is open: the save is refused,
            # and a second try cannot land on whatever row now sits in that place
            page.locator("#rows button.tap", has_text="Car insurance").first.click()
            desk = browser.new_context(viewport={"width": 1180, "height": 760}).new_page()
            login(desk)
            open_tab(desk, "everyday/2026")
            desk.locator("#grid tbody tr:not(.carried) td.c-desc").first.click()
            desk.keyboard.press("Control+Enter")  # a row inserted above everything
            desk.locator("#save-status", has_text="saved").wait_for()
            desk.context.close()
            page.fill("#form-row input[name=note]", "must not be saved")
            page.click("#row-save")
            page.locator("#save-status", has_text="changed elsewhere").wait_for()
            assert page.locator("#dlg-row").is_hidden()
            assert page.locator("#rows li", has_text="must not be saved").count() == 0
            page.locator("#rows button.tap").first.click()  # the inserted blank row: remove it again
            assert page.input_value("#form-row input[name=description]") == ""
            page.once("dialog", lambda d: d.accept())
            page.click("#row-delete")
            page.locator("#save-status", has_text="Deleted.").wait_for()

            page.click("#btn-add-row")
            page.fill("#form-row input[name=date]", "3/1/2026")
            page.fill("#form-row input[name=description]", "Parking")
            page.fill("#form-row input[name=amount]", "6")
            # a new row survives a change made elsewhere: the form stays, and a second Save adds it
            desk = browser.new_context(viewport={"width": 1180, "height": 760}).new_page()
            login(desk)
            open_tab(desk, "everyday/2026")
            desk.locator("#grid tbody tr:not(.carried) td.c-note").first.click()
            desk.keyboard.type("seen from the desk")
            desk.keyboard.press("Enter")
            desk.locator("#save-status", has_text="saved").wait_for()
            desk.context.close()
            page.click("#row-save")
            page.locator("#row-error", has_text="Press Save again").wait_for()
            assert page.input_value("#form-row input[name=description]") == "Parking"
            page.click("#row-save")
            page.locator("#rows li", has_text="Parking").wait_for()
            added = page.locator("#rows button.tap").last
            assert "Parking" in added.inner_text() and "-$6.00" in added.inner_text(), added.inner_text()
            added.click()
            page.once("dialog", lambda d: d.accept())
            page.click("#row-delete")
            page.locator("#save-status", has_text="Deleted.").wait_for()
            assert page.locator("#rows li", has_text="Parking").count() == 0
            page.set_viewport_size({"width": 1180, "height": 760})
            page.locator("#grid tbody td.c-cat").first.wait_for()

            # reports: totals, the month chart with its table, and the two rankings
            page.click("#btn-reports")
            page.locator("#report-months path.bar").first.wait_for()
            assert page.locator("#report-year").input_value() == "2026"
            assert page.locator("#report-months path.bar").count() >= 4  # January and February at least
            page.screenshot(path=out / "reports.png")
            page.select_option("#report-year", "2025")
            page.locator("#report-months path.bar").nth(23).wait_for()
            assert page.locator("#report-months path.bar").count() == 24
            assert page.locator("#report-categories dt").first.inner_text() == "Housing"
            page.locator("#report-months path.bar").first.hover()
            assert "Income, Jan" in page.inner_text("#tip") and page.locator("#tip").is_visible()
            page.screenshot(path=out / "reports-2025.png")
            page.click("#dlg-reports summary")
            assert page.locator("#report-month-rows tr").count() == 12
            page.select_option("#report-account", "visa")  # no card rows in 2025
            page.locator("#report-categories .report-empty").wait_for()
            page.click("#reports-close")

            # a credit card shows what is owed as positive, and is typed that way
            open_tab(page, "visa/2026")
            amounts = page.locator("#grid tbody tr:not(.newrow) td.c-amt")
            balances = page.locator("#grid tbody tr:not(.newrow) td.c-bal")
            assert amounts.all_inner_texts() == ["$212.40", "$23.90", "$64.00", "-$212.40", "$38.15"]
            assert balances.last.inner_text() == "$126.05"
            assert page.locator("#btn-sign").is_hidden()
            balances.last.click()  # the bar above the grid shows what the cell shows
            assert page.inner_text("#cell-value") == "126.05", page.inner_text("#cell-value")
            assert page.locator("#grid thead th.c-bal").inner_text().strip().startswith("Owed")
            page.screenshot(path=out / "card.png")
            page.locator("#grid tbody tr.newrow td.c-amt").first.click()
            page.keyboard.type("12.50")
            page.keyboard.press("Enter")
            page.locator("#save-status", has_text="Saved").wait_for()
            assert amounts.last.inner_text() == "$12.50" and balances.last.inner_text() == "$138.55"
            page.locator(".tab-label", has_text="Visa").dblclick()
            page.uncheck("#form-settings input[name=owed]")
            page.click("#form-settings button[type=submit]")
            page.locator("#btn-sign:not(.hidden)").wait_for()
            assert amounts.last.inner_text() == "-$12.50" and balances.last.inner_text() == "-$138.55"
            # a new loan: the amount owed is typed as a plain number
            page.click(".tab-add-account")
            page.fill("#form-account input[name=name]", "Car loan")
            page.select_option("#form-account select[name=type]", "loan")
            assert page.inner_text("#opening-text") == "Amount owed now"
            assert page.locator("#year-label").is_hidden()
            page.fill("#form-account input[name=opening]", "9,000")
            page.click("#form-account button[type=submit]")
            page.locator("#sheet-title", has_text="Car loan").wait_for()
            assert balances.first.inner_text() == "$9,000.00"
            open_tab(page, "everyday/2026")

            # a transfer: one row in each account, and deleting one side deletes both
            page.click("#btn-transfer")
            page.screenshot(path=out / "transfer.png")
            page.fill("#form-transfer input[name=amount]", "25")
            page.fill("#form-transfer input[name=date]", "2/28/2026")
            page.click("#transfer-submit")
            page.locator("td.xfer", has_text="Transfer to Rainy Day").last.wait_for()
            assert page.locator("#grid tbody td.c-amt", has_text="-$25.00").count() == 1
            page.goto(URL + "/#rainy-day/-")
            page.reload()
            page.locator("td.xfer", has_text="Transfer from Everyday").wait_for()
            page.locator("td.xfer").click()
            page.once("dialog", lambda d: (check("other account is deleted too" in d.message), d.accept()))
            page.click("#btn-delete")
            page.locator("#save-status", has_text="Saved").wait_for()
            assert page.locator("td.xfer").count() == 0
            open_tab(page, "everyday/2026")
            assert page.locator("td.xfer").count() == 0
            page.click("#btn-security")

            # the Security dialog, for real: mismatch, wrong password, change, log out everywhere
            page.fill("input[name=current]", "demo")
            page.fill("input[name=fresh]", "demo two")
            page.fill("input[name=repeat]", "demo 2")
            page.click("#password-submit")
            assert "do not match" in page.inner_text("#password-error")
            page.fill("input[name=repeat]", "demo two")
            page.fill("input[name=current]", "wrong")
            page.click("#password-submit")
            page.locator("#password-error", has_text="wrong password").wait_for()
            page.fill("input[name=current]", "demo")
            page.click("#password-submit")
            page.wait_for_url(URL + "/login?changed")
            assert "Password changed." in page.inner_text("body")
            other = browser.new_context().new_page()
            login(other, "demo two")
            login(page, "demo two")
            page.click("#btn-security")
            page.click("#logout-all")
            page.wait_for_url(URL + "/login")
            other.goto(URL + "/")
            assert other.url == URL + "/login", other.url
            page.fill("input[name=password]", "demo")
            page.click("button[type=submit]")
            assert "Wrong password." in page.inner_text("body")
            browser.close()
    print("screenshots written to %s; the Security dialog checks passed" % out)


if __name__ == "__main__":
    main(sys.argv)
