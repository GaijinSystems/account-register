#!/usr/bin/env python3
"""Accessibility audit: run axe-core against every page and dialog, in both themes and at phone
width, on a demo home (tools/demo_home.py). Prints the violations; exits 1 if there are any.

    python3 tools/a11y_audit.py PATH/TO/axe.min.js

Needs Playwright for Python with Chromium, the app's own requirements, and a copy of axe-core's
axe.min.js (https://github.com/dequelabs/axe-core). The app must not already be on port 8000.
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

import demo_app
from demo_app import URL

if len(sys.argv) != 2:
    sys.exit(__doc__.strip())
AXE = Path(sys.argv[1]).read_text(encoding="utf-8")
FOUND = []
DIALOGS = (("#btn-transfer", "transfer"), ("#btn-search", "search"), ("#btn-recurring", "recurring"),
           ("#btn-reports", "reports"), ("#btn-history", "history"), ("#btn-security", "security"),
           ("#btn-export", "export"), ("#btn-help", "help"), (".tab-add-account", "account"),
           ("#btn-reconcile", "reconcile"))


def audit(page, label):
    page.evaluate(AXE)
    res = page.evaluate("axe.run(document, {resultTypes: ['violations']})")
    for v in res["violations"]:
        FOUND.append(v["id"])
        print("[%s] %s (%s): %s" % (label, v["id"], v["impact"], v["help"]))
        for n in v["nodes"][:4]:
            print("     ", n["target"], "|", (n.get("failureSummary") or "").replace("\n", " ")[:230])
    if not res["violations"]:
        print("[%s] no violations" % label)

with demo_app.running(quiet=True), sync_playwright() as p:
    browser = p.chromium.launch()
    for theme in ("dark", "light"):
        page = browser.new_context(viewport={"width": 1180, "height": 760}).new_page()
        page.goto(URL + "/login")
        if theme == "light":
            page.evaluate("localStorage.setItem('register-theme','light')")
            page.reload()
        audit(page, theme + " login")
        demo_app.login(page)
        page.wait_for_selector("#grid tbody td.c-cat")
        audit(page, theme + " grid")
        for button, dialog in DIALOGS:
            page.click(button)
            page.wait_for_selector("dialog[open]")
            page.wait_for_timeout(400)  # reports, history and recurring fill in after opening
            audit(page, "%s %s dialog" % (theme, dialog))
            page.keyboard.press("Escape")
        page.set_viewport_size({"width": 390, "height": 760})
        page.wait_for_selector("#rows button.tap")
        audit(page, theme + " phone list")
        page.locator("#rows button.tap").first.click()
        audit(page, theme + " phone form")
        page.context.close()
    browser.close()
sys.exit(1 if FOUND else 0)
