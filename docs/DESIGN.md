# Design

How account-register is put together, and why. [README.md](../README.md) says how to use it;
[SECURITY.md](../SECURITY.md) says what it protects and what it doesn't.

## Aims

- A register that feels like the spreadsheet it replaces: a grid, keyboard navigation, sheet
  tabs, and a running balance that is always right because it is computed, never stored.
- Every change is recorded and can be traced. Nothing the web page can do rewrites history.
- The data is unreadable at rest without the password.
- One file to install, with as few dependencies as possible: the Python standard library,
  `git`, and the `cryptography` package. No build step for the browser side.
- One user. No accounts, roles or sharing.

## Two processes

```
 browser ──HTTPS──▶ reverse proxy ──HTTP──▶ [web process]
                                                  │  a fixed set of JSON operations,
                                                  ▼  over a Unix socket
                                           [recorder process] ──▶ data/ (encrypted files + git)
```

- **The recorder** (`register/core/recorder.py`) is the only code that reads or writes the
  data directory or runs git. It accepts the operations listed in `register/protocol.py` and
  nothing else: no file paths, no git refs, no commit messages. It validates every argument
  itself and does not trust the web process. It handles one request at a time.
- **The web process** (`register/web/`) handles login, sessions and the page, and forwards
  each API call to the recorder. It never imports `register.core`; a test enforces that.
  Each route maps onto one operation, and the allowed request fields are derived from the
  operation table.
- **Why split them:** a bug in the web layer, or misuse of the HTTP API, can at worst send
  legitimate operations, and each of those is a visible commit. The split does not protect
  against code execution in the web process, because both run as the same OS user.
- **The command line** (`register/__main__.py`) uses the same core code directly. Every
  command that reads or writes data proves the password itself; none borrows the running
  recorder's key. A file lock (`run/write.lock`) serialises writers, so commands are safe
  while the app is running.

`Ledger` (`register/core/ledger.py`) implements the operations on top of `Store`
(`register/core/store.py`), which owns the files, the balance rules and the write procedure.

## Data

```
home/
  config.json                 web settings; no secrets
  run/                        the socket and the lock file
  data/                       a git repository
    crypto.json               key parameters: salt, key id, a password check (not secret)
    accounts.json             the account list, one encrypted item
    recurring.json            recurring transactions, one encrypted item
    everyday/2025.jsonl       one encrypted row per line
    rainy-day/register.jsonl
```

- **One row layout for every account type** (data format 4):
  `num, date, description, category, amount, cleared, note, transfer, bank_id`.
  - `transfer` is an id shared by the two rows of a transfer between accounts.
  - `bank_id` is a bank's own transaction id, kept so a later import can skip the row.
  - Both are set by the app; row edits refuse them.
- **Account types** differ only in whether they keep one sheet per year (checking, credit
  card) or a single sheet (savings, cash, loan), and in display defaults. They are defined
  once, in `register/core/formats.py`, and sent to the browser from there.
- **Amounts are decimal strings** matching `-?\d+\.\d{2}`, never floats. They always mean the
  same thing: negative is money spent or owed. A credit card or loan can be *shown* with the
  sign flipped ("owed as positive"); that is a per-account display setting applied in the
  browser only. The two sides of a transfer therefore always cancel.
- **The balance is never stored.** It is the sum of the rows above, plus what earlier years
  carry forward. A stored total could go stale; a computed one cannot.
- **Rows are kept in the order entered,** like a paper register. Nothing sorts them.
- **Year tabs form one unbroken run** that starts with the opening balance. A dated row that
  the app places itself (a transfer, a recurring entry, an imported row) goes into an
  existing year or the year after the last one, which is then created; anything else is
  refused (`store.year_tab`).
- **Text is restricted and normalised:** no control or invisible characters (they can make a
  line look different from what is stored), and Unicode NFC.

## Encryption

- **Key:** `scrypt(password, salt, n=2^17, r=8, p=1)`, 256 bits, held only in memory. There
  is no stored or wrapped key and no recovery code.
- **Cipher:** AES-256-GCM with a fresh nonce per encryption. Each row is encrypted on its
  own and bound, as associated data, to its file path and key id, so a row moved to another
  sheet fails to decrypt. Row position is not bound; that would mean re-encrypting every
  following row on each insert.
- **Padding:** plaintext is padded to 64-byte steps, so length reveals only a rough size.
- **Unchanged rows keep their exact ciphertext line.** Git diffs stay one line per changed
  row, and reading a sheet's history decrypts each distinct line once.
- **Changing the password** re-encrypts every current file in one commit under a new salt
  and key. Older commits stay encrypted under the password of their time; history is not
  rewritten.
- **What stays visible** without the password: account ids (folder names), years, row
  counts, which rows changed and when, and rough entry sizes.

## Writes

Every write goes through `Store.write_files`:

1. Refuse if the repository has uncommitted changes. The page then goes read-only until they
   are committed or discarded with git.
2. Check the sheet revision the caller saw (a hash of the file). A stale one is refused.
3. Check running balances for every sheet that changes, and for later years they carry into.
4. Write each file to a temporary file, read it back, decrypt and re-validate it, and compare
   it with what was meant to be written.
5. Move the files into place and make one commit. If anything fails, restore the previous
   contents.

- **One user action is one commit,** whatever it touches: a transfer writes two sheets, an
  import many.
- **Git is append-only here.** The wrapper (`register/core/gitrepo.py`) can init, add,
  commit, and read log and file contents. There is no reset, checkout, amend, rebase or push
  anywhere in the app.
- **Commit messages are not encrypted,** so they never contain row contents, amounts, names
  or file names: `everyday/2026: edit row 2 (amount, description)`,
  `import 12 row(s) into everyday from an OFX file`.
- **Undo is a new commit** that puts old values back. It is offered only for a change that
  touched one sheet, and only while the rows involved are still as that change left them;
  rows are found by content, so unrelated edits elsewhere in the sheet don't matter.

## Locking and sessions

- The recorder starts locked. Logging in on the page sends the password to the recorder,
  which derives the key; that is the only password check there is.
- It locks again on logout, after 30 minutes without activity (the page's status polls don't
  count), on restart, and when the password changes.
- Wrong passwords are slowed down, in the web process and again in the recorder. Exporting
  and changing the password ask for the password again and share that backoff.
- Sessions are random tokens held in the web process's memory. Writes need a CSRF token and
  a matching `Origin`.

## The page

- Plain JavaScript in one file (`register/web/static/grid.js`), no framework and no build
  step, served with a strict Content Security Policy: no inline scripts or styles, no
  outside resources.
- Edits are staged in the browser and saved a row at a time: when the selection leaves the
  row, when the page is hidden or closed, or after 30 seconds without typing. A refused save
  keeps the edits visible until Retry or Discard.
- Everything from the data is put into the page as text, never as markup.
- Below 700 pixels wide the grid becomes a list of rows with a form; it is the same page.
- Charts in the reports are SVG drawn by hand. There is no chart library.

## Imports and exports

- **Imports are command line only.** Files are untrusted: the readers for `.ods`, `.xlsx`,
  CSV, OFX/QFX and QIF use only the standard library, limit sizes, never hand XML with a
  DOCTYPE to a parser, and read OFX with single-pass matching, so a damaged file fails fast.
- **Every import previews first.** The real run goes ahead only if it would produce exactly
  the report that was shown.
- **A spreadsheet import fills whole sheets;** the layout is guessed from balance formulas,
  then headers, then values, and can be saved as a config.
- **A bank file is appended** to one account. Rows already there are skipped: by the bank's
  id together with date and amount; by date and amount against a hand-typed row, which then
  receives the id; or by date, amount and description when there is no id. One existing row
  accounts for at most one imported row.
- **The CSV export restores exactly** through an "exact" import that takes rows as written.
  A new data format is adopted by exporting with the old version and importing into a new
  home; there are no in-place migrations.

## Testing

- `python3 -m unittest discover -s tests`: the core, the recorder over a real socket, the
  web app against a fake recorder, the importers, the built `register.pyz`, and a check that
  the web package never imports the core.
- `tools/screenshots.py` drives the real app in headless Chromium on made-up data and checks
  the interface end to end; `tools/a11y_audit.py` runs the axe-core accessibility rules on
  every page and dialog.

## Decided against

- A second factor at login, user accounts, or a list of sessions: it is for one person on a
  private network.
- A backup command or automatic pushes: the recorder holds the key and gets no network
  access. A backup is a `git push` of the encrypted repository.
- Prebuilt releases, a container image, an installable web app, locale settings.
- Split transactions (one payment across several categories). Adding them would need a new
  data format.
- A third "reconciled" state for rows: reconciling ticks the existing X.

## Known limits

- **Tested on Linux only,** and by automated accessibility checks, not with a screen reader.
- **Bank-file readers were tested against fixtures written from the format descriptions.**
  Investment statements and QIF split lines are not read.
- **A transaction changed after import comes in again** on the next overlapping file, with a
  warning: an id reused for a different date or amount is treated as a new transaction.
- **Reading a sheet decrypts every row,** and a row save reads the sheet several times. That
  is fine for a personal register and would not scale far beyond it.
- **Recurring transactions can't be edited,** only deleted and added again; there are no
  recurring transfers or end dates.
- **Reports cover one calendar year at a time.**
