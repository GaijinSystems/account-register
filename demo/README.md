# Static demo

A folder of static files that shows the app's real page (`grid.js`, `grid.css` and `theme.js`
byte for byte; `index.html` with only the changes listed below) running entirely in the
visitor's browser tab, with made-up data and no
storage of any kind. An experiment: nothing else in the repository depends on it.

It is **not** the app's security model. The real app runs on your own server, encrypts everything
it stores and requires a login; the demo has no server, no encryption and no login, and says so in
a notice at the top of the page.

## Build and view

The build needs only Python and this repository (standard library only):

    python3 tools/build_demo.py OUTDIR

`OUTDIR` must be new, empty, or hold only files this build also writes (so the `CNAME` of an
earlier `--cname` build stops a build without `--cname`): the build stops if it holds anything
else, hidden files and subdirectories included (so build into a folder of its own, then
copy the files to where they are published). It then holds eight files with relative URLs only
(nine with `--cname DOMAIN`, which adds the `CNAME` file GitHub Pages wants). Put them on any
static file host, in the root or under a sub-path, or look at them locally:

    python3 -m http.server --directory OUTDIR 8000      # then open http://127.0.0.1:8000/

Opening `OUTDIR/index.html` straight from disk works too (checked in Chromium).

| File | From |
| :--- | :--- |
| `grid.js`, `grid.css`, `theme.js` | `register/web/static/`, byte for byte |
| `index.html` | `register/web/templates/index.html`, rendered as the server renders it, then: the `/static/` URLs of `theme.js` and `grid.css` made relative, the tag for `grid.js` removed (`demo-api.js` adds it, see below), the demo's files and a Content-Security-Policy added to the head, and `notice.html` put first in the body |
| `demo-nostore.js` | `demo/nostore.js`: stands in for `localStorage` and `sessionStorage`, and makes `document.cookie` inert |
| `demo-spec.js` | written by the build from the app's Python modules: the routes and their arguments, the row fields, the account types, the limits |
| `demo-api.js` | `demo/api.js`: replaces `window.fetch` with the backend, in memory, makes the starting data, disables the password fields, and then starts `grid.js` |
| `demo.css` | `demo/demo.css`: the notice, and the message shown if the demo did not start |

## Nothing is stored, nothing is sent

- **Data** lives in JavaScript variables in `demo-api.js`. A reload runs the script again and the
  starting data is back. "Log out" reloads the page.
- **`fetch`** is replaced before `grid.js` runs. The replacement answers the app's own routes,
  answers 404 for anything else on the site, and rejects every other address, so no request can
  leave the page through it.
- **The policy** in `index.html` has `connect-src 'none'`: the browser itself refuses any
  `fetch`, `XMLHttpRequest`, `WebSocket` or beacon, whatever a script tries. It also allows
  scripts and styles only from the site's own files (no inline ones, as in the real app), and no
  form may be submitted anywhere.
- **`localStorage`** is where the real app remembers two preferences (the theme and debit mode).
  `demo-nostore.js` runs first and defines `window.localStorage` and `window.sessionStorage` as
  objects with the same methods that keep their values in a `Map`. The two buttons work while the
  page is open; nothing reaches the browser's storage. `document.cookie` is made inert the same
  way (the app's scripts never write it).
- **The page's own script starts last, and only if all three demo scripts ran.** `index.html`
  has no tag for `grid.js`; `demo-api.js` adds it once its data is made and `demo-nostore.js` has
  run. If `demo-nostore.js`, `demo-spec.js` or `demo-api.js` fails to load or stops with an
  error, `grid.js` is never requested: the page says that the demo did not start, shows no data,
  and none of its buttons do anything. Nothing is written to the browser's storage and no
  request is made. (`theme.js` still runs; without the stand-in it reads the `register-theme`
  key from the real `localStorage` and writes nothing.) If `grid.js` itself does not load, the
  page says the same.
- **Export and Security…** open as in the real app, but their password fields are disabled
  (`demo-api.js` does this before the page's script starts): nobody should type a password
  into a public page or have a browser offer to save one. Submitting either answers that it
  is not available in the demo.
- **The build refuses** to copy `grid.js` or `theme.js` if they mention one of a list of other
  ways of keeping data or talking to a server (`NOT_COVERED` in `tools/build_demo.py`:
  `indexedDB`, `document.cookie`, `sessionStorage`, caches, service workers, `XMLHttpRequest`,
  `WebSocket`, workers and the like). It is a tripwire for later changes to the app, not a proof.

One thing does stay in the browser's hands: the address. The app puts the open sheet in the
URL fragment (`#everyday/2026`), so a reload opens the same tab, with the starting data. For an
account the visitor adds, that is an id made from the name they typed, and the browser may keep
the address in its history.

## What is and is not the real thing

The page is the real one. The backend is a port, by hand, of `register/core/ledger.py`,
`store.py`, `formats.py` and `recurring.py`, answering the routes in `register/web/app.py` with
the same JSON, the same error shape (`{"error", "message", "field"}`) and the same status codes.

Implemented: status, tabs, sheet views (running balance, carried-forward opening,
`cleared_before`), insert / update / delete row with the server's validation and messages, the
stale-`rev` refusal (409), add account, add year, account settings, transfers (linked rows, the
optional `id`, the year-tab rule), reconcile, recurring (list, add, delete, skip, enter), search,
report, suggestions, history and undo.

Different from the real app:

- **Export** and **change password** answer 501 and say why. **Log out**
  and **Log out everywhere** reload the page.
- **History** is kept in memory from the moment the page loads (the real one is read from git).
  The starting data itself is not listed; two edits made at load are, so there is something to undo.
- **`rev`** is a counter, not a hash of the file; **`modified`** and the history dates are the
  browser's clock; the starting data is "last saved" three days ago (except the sheet with the two edits made at load).
- **The starting data** is dated from today backwards (last year and this year), not fixed to
  2025 and 2026 as in `tools/demo_home.py`, so that it never looks stale and a transfer dated
  today always has a year tab to go in. It comes from a fixed seed: every reload on the same day
  gives the same rows.
- There is no "read-only: uncommitted changes" state, no auto-lock, no session, no CSRF or
  origin check, no request size limit and no `Content-Type` check: there is no server to protect.
- Text comparison uses the browser's Unicode tables and `toLowerCase()` where Python uses its own
  tables and `casefold()`; a few unusual characters may be treated differently.
- A whole number written as `2026.0` or `1e3` in a request is a number with a fraction to Python
  and refused; JavaScript cannot tell it from `2026` or `1000`. The page never sends one.
- Requests the page never sends: `NaN` or `Infinity` in a body is refused as invalid JSON
  (Python accepts them and then refuses the type); a body nested thousands of levels deep is
  refused as "nested too deeply" only at the browser's own limit, which is higher than Python's;
  and `HEAD` answers 405 where the real app answers as for `GET`.
- In History, rows are lined up with a longest-common-subsequence comparison, not Python's `difflib`.
  They agree where the rows around an edit differ from one another. Where identical rows sit
  next to the edit (two blank rows, say), the row number shown, and occasionally whether Undo is
  offered, can differ from the real app; so can a change that rewrites many rows at once.

## Keeping it in step

- `demo-spec.js` is generated, so routes, argument types, fields, limits and account types follow
  the app. A route the app gains that `api.js` has no operation for answers 501 and logs a
  warning in the console.
- The build stops if `index.html` no longer has the lines it rewrites, has a URL that is not
  relative, a link or a meta refresh that leaves the page, or an inline script, style or event
  handler; and if the scripts use storage or network calls
  the demo does not stand in for.
- `tests/test_demo_build.py` builds the site as part of the normal test suite, so those guards
  fire when the app changes, not only when someone next publishes the demo.
- `tools/check_demo.py SITE [SCREENSHOT.png]` drives the built site in Chromium (Playwright for
  Python): the grid, the main dialogs, a transfer seen from both accounts, the error answers, a
  reload; that the browser holds no localStorage, sessionStorage, cookies, IndexedDB or caches for
  the site (asked over the DevTools protocol, not through the page); and that every request was
  for one of the site's own files. It then checks in Firefox and WebKit that the page loads
  without errors, that the two preferences leave no cookie and nothing in localStorage, and that
  the theme is back to its default after a reload. In Chromium it also blocks each of the three
  demo scripts in turn and checks that `grid.js` is then never requested and nothing is stored,
  and blocks `grid.js` and checks that the page says it did not start.
- The behaviour in `api.js` is ported by hand. A change to `ledger.py` that changes an answer
  needs the same change there. `tools/demo_differential.py SITE` checks it: it sends the same
  requests to the real app and to the demo, compares the answers, and fails if they differ or
  if an operation of the app has no step. (Export and change password, which the demo does not
  have, are sent to the demo only, which must answer 501. The values of `rev`, `modified`, change and
  template ids and history dates are not compared, and History is compared down to where the
  starting data was loaded (its "more" flag only where the whole list is compared).)
