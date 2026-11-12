#!/usr/bin/env python3
"""Create a register home filled with made-up data, for trying the app and for screenshots.

    python3 tools/demo_home.py DIR        # DIR must not exist; the password is "demo"
    python3 -m register run --home DIR    # then open http://127.0.0.1:8000

The home allows plain HTTP on localhost (cookie_secure is off). Not for real data.
"""
import datetime
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from register.__main__ import DEFAULT_CONFIG, write_config  # noqa: E402
from register.core import admin, formats  # noqa: E402
from register.core.ledger import Ledger  # noqa: E402
from register.core.store import Home, locked  # noqa: E402

PASSWORD = "demo"
# (day of month, description, amount or (low, high))
MONTHLY = [
    (1, "Rent", "-1450.00"),
    (3, "Harbor Electric", (-140, -70)),
    (5, "Paycheck", "1385.20"),
    (8, "Car insurance", "-118.40"),
    (9, "Corner Market", (-190, -90)),
    (12, "City Water", (-60, -35)),
    (15, "Transfer to Rainy Day", "-200.00"),
    (17, "Internet", "-65.00"),
    (19, "Paycheck", "1385.20"),
    (22, "Corner Market", (-190, -90)),
    (24, "Phone", "-45.00"),
    (27, "Bookshop", (-45, -8)),
]
CATEGORIES = {"Rent": "Housing", "Harbor Electric": "Utilities", "Paycheck": "Income", "Car insurance": "Car",
              "Corner Market": "Food", "City Water": "Utilities", "Transfer to Rainy Day": "Transfer",
              "Internet": "Utilities", "Phone": "Utilities", "Bookshop": "Fun"}
NOTES = {(2025, 3, 27): "birthday present", (2025, 7, 3): "includes the late fee", (2026, 2, 22): "party supplies"}


def row(**fields):
    return dict(formats.FORMATS["checking"].blank_row(), **fields)  # every account type has the same fields


def checking_rows(rng, year, months, opening=None):
    rows, check = [], 1040 + (year - 2025) * 30
    if opening:
        rows.append(row(description="Opening balance", amount=opening, cleared=True))
    for month in months:
        for day, what, amount in MONTHLY:
            if not isinstance(amount, str):
                amount = "%.2f" % (rng.randint(amount[0] * 100, amount[1] * 100) / 100)
            num = ""
            if what in ("Rent", "City Water"):  # paid by check
                check += 1
                num = str(check)
            rows.append(row(num=num, date=datetime.date(year, month, day).isoformat(), description=what,
                            category=CATEGORIES[what], amount=amount, cleared=(year, month) < (2026, 2),
                            note=NOTES.get((year, month, day), "")))
    return rows


def savings_rows():
    rows = [row(description="Opening balance", amount="5000.00")]
    for year, months in ((2025, range(1, 13)), (2026, range(1, 3))):
        for month in months:
            rows.append(row(date=datetime.date(year, month, 15).isoformat(), description="Transfer from Everyday",
                            category="Transfer", amount="200.00"))
            if month % 3 == 0:
                rows.append(row(date=datetime.date(year, month, 28).isoformat(), description="Interest",
                                category="Interest", amount="%.2f" % (11 + month * 0.37)))
    rows.insert(9, row(date="2025-06-02", description="Car repair", category="Car", amount="-640.00",
                       note="new brakes"))
    return rows


def main(argv):
    if len(argv) != 2 or argv[1].startswith("-"):
        sys.exit(__doc__.strip())
    home = Home(argv[1])
    if home.root.exists():
        sys.exit("%s already exists" % home.root)
    admin.init_home(home, PASSWORD, "demo", "demo@localhost")
    write_config(home.config, dict(DEFAULT_CONFIG, cookie_secure=False))
    rng = random.Random(1)
    ledger = Ledger(home, home.unlock(PASSWORD))
    store = ledger.store
    with locked(home):
        ledger.add_account("Everyday", "checking", "1200.00", 2025)
        ledger.add_year("everyday")
        ledger.add_account("Rainy Day", "savings", "5000.00")
        ledger.add_account("Visa", "card", "-212.40", 2026)  # stored negative: owed; shown positive
        everyday, rainy = store.account("everyday"), store.account("rainy-day")
        fmt_c, fmt_s = formats.FORMATS["checking"], formats.FORMATS["savings"]
        store.write_files({
            store.relpath(everyday, 2025): (fmt_c, checking_rows(rng, 2025, range(1, 13), "1200.00")),
            store.relpath(everyday, 2026): (fmt_c, checking_rows(rng, 2026, range(1, 3))),
            store.relpath(rainy, None): (fmt_s, savings_rows()),
            store.relpath(store.account("visa"), 2026): (formats.FORMATS["card"], [
                row(description="Opening balance", amount="-212.40", cleared=True),
                row(date="2026-01-06", description="Bookshop", category="Fun", amount="-23.90", cleared=True),
                row(date="2026-01-14", description="Train tickets", category="Travel", amount="-64.00", cleared=True),
                row(date="2026-01-28", description="Payment, thank you", category="Transfer", amount="212.40",
                    cleared=True),
                row(date="2026-02-09", description="Hardware store", category="Home", amount="-38.15"),
            ]),
        }, "demo data")
    print("demo home created in %s (password: %s)\nrun: python3 -m register run --home %s"
          % (home.root, PASSWORD, home.root))


if __name__ == "__main__":
    main(sys.argv)
