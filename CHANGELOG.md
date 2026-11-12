# Changelog

## 1.0.0

The first public release.

- A spreadsheet-style register in the browser: sheet tabs, keyboard navigation, a computed
  running balance, row-by-row saving, dark and light modes, and a list layout for phones.
- Accounts: checking and credit card (one tab per year), savings, cash and loan (one tab).
  Credit cards and loans can show what is owed as positive.
- Transfers between accounts, categories, autocomplete, search, reconciliation against a
  statement, recurring transactions, and yearly reports with charts.
- Every change is one git commit, made by a separate recorder process. A per-sheet history
  with undo.
- All data encrypted at rest with a key derived from the password (scrypt, AES-256-GCM).
  The password can be changed from the page or the command line.
- Imports from `.ods`, `.xlsx`, CSV, OFX/QFX and QIF, each with a preview, and duplicate
  detection when adding a bank's download to an account.
- Exports to CSV (restores exactly), JSON, QIF and OFX.
- Runs on Python 3.9 to 3.13, on Linux.
- Data format 4. Homes made by earlier, unpublished versions are moved by exporting with the
  version that made them and importing into a new home.
