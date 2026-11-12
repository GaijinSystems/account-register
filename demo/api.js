// account-register demo: the backend, in memory.
//
// The real app's page (grid.js, unchanged) talks to its server only through fetch() and JSON.
// This file replaces window.fetch with a function that answers those requests from JavaScript
// variables: a small port of register/core/ledger.py, store.py, formats.py and recurring.py.
// Nothing is sent anywhere and nothing is stored: a reload builds the starting data again.
//
// The routes, the argument types, the field names, the patterns and the limits are not repeated
// here. They come from window.REGISTER_DEMO_SPEC (demo-spec.js), which tools/build_demo.py
// writes from the app's own Python modules, so they cannot drift. What is ported by hand is the
// behaviour and the wording of the error messages; tools/demo_differential.py compares both
// with the real backend.
"use strict";
(() => {
  const SPEC = window.REGISTER_DEMO_SPEC;
  const FIELDS = SPEC.fields;
  const TYPES = new Map(SPEC.types.map((t) => [t.id, t]));
  const ACCOUNT_ID = new RegExp(SPEC.account_id);
  const LINK_ID = new RegExp(SPEC.link_id);
  const ACCOUNTS_FILE = SPEC.accounts_file;   // named in a change's list of files, as in the real
  const RECURRING_FILE = SPEC.recurring_file; // history; only the sheets have content here

  // What the real server answers with: {"error", "message", "field"} and a status by error code.
  class ApiError extends Error {
    constructor(code, message, field) {
      super(message);
      this.code = code;
      this.field = field;
    }
  }
  const fail = (code, message, field) => { throw new ApiError(code, message, field); };
  const UNAVAILABLE = "demo_unavailable"; // the one error code the real app does not have
  // the recorder's codes, as the web process maps them, and the two answers that are its own
  const STATUS = { ...SPEC.error_status, [UNAVAILABLE]: 501, method_not_allowed: 405 };

  // ---------------------------------------------------------------- money, dates, ids
  // Amounts are canonical text ("-84.12"); sums are done in whole cents, which a double holds
  // exactly far beyond the largest balance the register allows.

  const CANONICAL = new RegExp(SPEC.amount);
  const isCanonical = (s) => s === "" || (CANONICAL.test(s) && s !== "-0.00");

  function toCents(s) {
    if (!s) return 0;
    const neg = s.startsWith("-");
    const [whole, frac] = (neg ? s.slice(1) : s).split(".");
    const n = Number(whole) * 100 + Number(frac);
    return neg ? -n : n;
  }

  function fromCents(n) {
    const size = Math.abs(n);
    return (n < 0 ? "-" : "") + Math.floor(size / 100) + "." + String(size % 100).padStart(2, "0");
  }

  const MAX_ABS = toCents(SPEC.max_abs);
  const negated = (amount) => (amount === "" ? "" : fromCents(-toCents(amount)));
  const sumAmounts = (rows) => rows.reduce((sum, r) => sum + toCents(r.amount), 0);

  // Dates are ISO text; the arithmetic is done in UTC so that daylight saving never shifts a day.
  const pad = (n, width) => String(n).padStart(width, "0");
  const iso = (y, m, d) => `${pad(y, 4)}-${pad(m, 2)}-${pad(d, 2)}`;
  const dateParts = (s) => s.split("-").map(Number);
  const utc = (y, m, d) => { const t = new Date(0); t.setUTCFullYear(y, m - 1, d); return t; };
  const isoOf = (t) => iso(t.getUTCFullYear(), t.getUTCMonth() + 1, t.getUTCDate());
  const addDays = (s, n) => { const [y, m, d] = dateParts(s); return isoOf(utc(y, m, d + n)); };
  const daysIn = (y, m) => utc(y, m + 1, 0).getUTCDate();
  const yearOf = (s) => Number(s.slice(0, 4));
  function todayIso() { // the visitor's own date, as the real app uses the server's
    const t = new Date();
    return iso(t.getFullYear(), t.getMonth() + 1, t.getDate());
  }

  const hex = (bytes) => [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
  const randomId = (bytes) => hex(crypto.getRandomValues(new Uint8Array(bytes)));

  // ---------------------------------------------------------------- field validation (formats.py)

  // control, format, private use, surrogate and unassigned characters
  const FORBIDDEN = new RegExp("[" + SPEC.forbidden_categories.map((c) => `\\p{${c}}`).join("") + "]", "u");
  const ISO_DATE = new RegExp(SPEC.iso_date);
  const isObject = (v) => typeof v === "object" && v !== null && !Array.isArray(v);
  const isInt = (v) => typeof v === "number" && Number.isInteger(v);

  function checkText(value, field, limit) {
    if (typeof value !== "string") fail("invalid", "must be text", field);
    value = value.normalize("NFC");
    const bad = FORBIDDEN.exec(value);
    if (bad) {
      const code = bad[0].codePointAt(0).toString(16).toUpperCase().padStart(4, "0");
      fail("invalid", `contains a control or invisible character (U+${code})`, field);
    }
    if ([...value].length > limit) fail("invalid", `longer than ${limit} characters`, field);
    return value;
  }

  function checkDate(value, field = "date") {
    if (typeof value !== "string") fail("invalid", "must be text", field);
    if (value === "") return value;
    if (!ISO_DATE.test(value)) fail("invalid", "must be YYYY-MM-DD", field);
    const [y, m, d] = dateParts(value);
    if (y === 0 || isoOf(utc(y, m, d)) !== value) fail("invalid", "not a real date", field);
    if (y < SPEC.min_year || y > SPEC.max_year) fail("invalid", `year ${y} is out of range`, field);
    return value;
  }

  function checkAmount(value, field = "amount") {
    if (typeof value !== "string" || !isCanonical(value)) {
      fail("invalid", "must be an amount like -84.12 or 1234.00 (or blank)", field);
    }
    return value;
  }

  function validateField(type, field, value) {
    if (field === "balance") fail("balance_readonly", "the balance is computed and cannot be written", field);
    if (!FIELDS.includes(field)) fail("unknown_field", `unknown field for a ${type} sheet`, field);
    if (field === "date") return checkDate(value);
    if (field === "amount") return checkAmount(value);
    if (field === "cleared") {
      if (typeof value !== "boolean") fail("invalid", "must be true or false", field);
      return value;
    }
    if (field === "transfer") {
      if (typeof value !== "string" || (value && !LINK_ID.test(value))) {
        fail("invalid", "must be blank or a 16-character transfer id", field);
      }
      return value;
    }
    return checkText(value, field, SPEC.text_limits[field]);
  }

  // The fields of an edit, validated. The link fields are the app's own and are never typed in.
  function editFields(type, fields) {
    if (!isObject(fields)) fail("invalid", "must be an object", "fields");
    for (const f of SPEC.link_fields) {
      if (f in fields) fail("invalid", "is set by the app and cannot be edited", f);
    }
    const out = {};
    for (const [key, value] of Object.entries(fields)) out[key] = validateField(type, key, value);
    return out;
  }

  const blankRow = () => Object.fromEntries(FIELDS.map((f) => [f, f === "cleared" ? false : ""]));
  // One text per row, the same for equal rows: how rows are compared (history, undo, "no change").
  const rowText = (row) => JSON.stringify(FIELDS.map((f) => row[f]));

  // ---------------------------------------------------------------- the data (store.py)
  // Everything the demo knows. Rows and row lists are never changed in place: an edit makes new
  // ones, so the lists kept for the history stay as they were.

  const db = {
    accounts: [],       // {id, name, type, owed_positive}
    sheets: new Map(),  // sheet key -> {rows, rev, modified}
    recurring: [],      // templates: {id, account, description, category, amount, note, every, start, count}
    changes: [],        // one per write, oldest first: {id, date, subject, files, versions}
  };
  let clock = () => new Date(); // when a change is made (the starting data is dated a little earlier)
  let revCount = 0;

  const yearly = (account) => TYPES.get(account.type).yearly;
  // "everyday/2026" or "rainy-day": names a sheet here and in the change subjects, as Store.label
  const sheetKey = (account, year) => (year === null ? account.id : `${account.id}/${year}`);

  function findAccount(id) {
    if (typeof id !== "string" || !ACCOUNT_ID.test(id)) fail("invalid", "invalid account id", "account");
    const account = db.accounts.find((a) => a.id === id);
    if (!account) fail("not_found", "no such account: " + id, "account");
    return account;
  }

  function years(account) {
    const out = [];
    for (const key of db.sheets.keys()) {
      const m = /^(.*)\/([0-9]{4})$/.exec(key);
      if (m && m[1] === account.id) out.push(Number(m[2]));
    }
    return out.sort((a, b) => a - b);
  }

  // The account's sheets as their `year` values: its years, or [null] for a single tab.
  const sheetsOf = (account) => (yearly(account) ? years(account) : [null]);

  function readSheet(account, year) {
    if (yearly(account)) {
      if (!isInt(year) || !db.sheets.has(sheetKey(account, year))) {
        fail("not_found", "no such year for " + account.id, "year");
      }
    } else if (year !== null) fail("invalid", account.id + " has no year tabs", "year");
    const sheet = db.sheets.get(sheetKey(account, year));
    return { rows: [...sheet.rows], rev: sheet.rev, modified: sheet.modified }; // a copy: callers edit the list
  }

  // [account, year, index, row] for every row of the accounts' sheets, in order.
  function allRows(accounts) {
    const out = [];
    for (const a of accounts) {
      for (const y of sheetsOf(a)) readSheet(a, y).rows.forEach((r, i) => out.push([a, y, i, r]));
    }
    return out;
  }

  // The year tab a new row dated `date` goes in (store.year_tab): an existing year, or the year
  // after the last one, which is then created. Anything else is refused.
  function yearTab(account, date, existing) {
    if (!yearly(account)) return null;
    const year = yearOf(date);
    if (existing.length && !existing.includes(year)) {
      const first = Math.min(...existing), last = Math.max(...existing);
      if (year < first) {
        fail("invalid", `${account.name} starts in ${first}; a row dated ${date} would come before its opening balance`, "date");
      }
      if (year !== last + 1) {
        fail("invalid", `${account.name} has no ${year} tab: its last year is ${last}, and only the year after that ` +
             "is added automatically. Add the years in between first", "date");
      }
    }
    return year;
  }

  // Refuse a write that would put a running balance, in the changed sheets or the later years
  // they carry into, beyond the largest amount. changed: Map(year -> rows).
  function checkBalances(account, changed) {
    const single = changed.has(null);
    const first = single ? null : Math.min(...changed.keys());
    const stored = (y) => { const s = db.sheets.get(sheetKey(account, y)); return s ? s.rows : []; };
    const all = single ? [null] : [...new Set([...years(account), ...changed.keys()])].sort((a, b) => a - b);
    let opening = 0;
    for (const y of all) if (!single && y < first) opening += sumAmounts(stored(y));
    const later = all.filter((y) => single || y >= first);
    const walk = (rowsOf) => {
      let running = opening;
      const out = [];
      for (const y of later) {
        rowsOf(y).forEach((r, i) => { running += toCents(r.amount); out.push([y, i, running]); });
      }
      return out;
    };
    const after = walk((y) => (changed.has(y) ? changed.get(y) : stored(y)));
    if (after.every(([, , balance]) => Math.abs(balance) <= MAX_ABS)) return;
    // already out of range before: allowed as long as it gets no worse, so it can be put right
    const before = [opening, ...walk(stored).map(([, , balance]) => balance)];
    const high = Math.max(MAX_ABS, ...before), low = Math.min(-MAX_ABS, ...before);
    for (const [y, i, balance] of after) {
      if (balance > high || balance < low) {
        fail("invalid", `the balance at ${sheetKey(account, y)} row ${i + 1} would be larger than ${fromCents(MAX_ABS)}`, "amount");
      }
    }
  }

  // One write: the real app's commit. sheets: [[key, rows]]; others: the other files it changes.
  // Sheets whose rows are the same as before are left alone (same rev, nothing in the history).
  function commit(subject, sheets, others = []) {
    const changed = sheets.filter(([key, rows]) => {
      const old = db.sheets.get(key);
      return !old || old.rows.length !== rows.length || old.rows.some((r, i) => rowText(r) !== rowText(rows[i]));
    });
    if (!changed.length) return;
    const date = clock().toISOString();
    const versions = {};
    for (const [key, rows] of changed) {
      const old = db.sheets.get(key);
      versions[key] = { before: old ? old.rows : [], after: rows };
      db.sheets.set(key, { rows, rev: "demo-" + ++revCount, modified: date });
    }
    db.changes.push({ id: randomId(20), date, subject, files: [...others, ...changed.map(([key]) => key)], versions });
  }

  function sheetView(accountId, year) {
    const account = findAccount(accountId);
    const { rows, rev, modified } = readSheet(account, year);
    let opening = 0, clearedBefore = 0, carriedFrom = null;
    for (const y of year === null ? [] : years(account).filter((x) => x < year)) {
      const earlier = readSheet(account, y).rows;
      opening += sumAmounts(earlier);
      clearedBefore += sumAmounts(earlier.filter((r) => r.cleared));
      carriedFrom = y;
    }
    let balance = opening;
    const out = rows.map((r) => {
      balance += toCents(r.amount);
      return { ...r, balance: fromCents(balance) };
    });
    return { account: account.id, name: account.name, type: account.type, owed_positive: account.owed_positive,
             year, fields: [...FIELDS], carried_from: carriedFrom, opening: fromCents(opening),
             cleared_before: fromCents(clearedBefore), rows: out, rev, modified, repo_clean: true };
  }

  // ---------------------------------------------------------------- row edits (ledger.py)

  function loadForWrite(accountId, year, rev) {
    const account = findAccount(accountId);
    const sheet = readSheet(account, year);
    if (rev !== sheet.rev) fail("stale", "the sheet changed since it was loaded; reload and try again");
    return { account, rows: sheet.rows };
  }

  // Write one sheet and, with it, the sheet holding the other side of a transfer: one change.
  function save(account, year, rows, subject, other) {
    const sheets = [[account, year, rows]].concat(other ? [other] : []);
    for (const [a, y, rs] of sheets) checkBalances(a, new Map([[y, rs]]));
    commit(subject, sheets.map(([a, y, rs]) => [sheetKey(a, y), rs]));
    return sheetView(account.id, year);
  }

  // For a row that is one side of a transfer: apply change(rows, index) to the sheet holding the
  // other side. Nothing happens if the row is no transfer or its other side is gone.
  function otherSide(row, account, change) {
    if (!row.transfer) return { other: undefined, also: "" };
    const near = row.date ? yearOf(row.date) : null;
    const sheets = [];
    for (const a of db.accounts) if (a.id !== account.id) for (const y of sheetsOf(a)) sheets.push([a, y]);
    // entered with the same date: that year of every account first, then the single tabs, then
    // the other years, latest first
    const rank = ([, y]) => [y !== near ? 1 : 0, y !== null ? 1 : 0, -(y || 0)];
    sheets.sort((p, q) => {
      const a = rank(p), b = rank(q);
      return a[0] - b[0] || a[1] - b[1] || a[2] - b[2];
    });
    for (const [a, y] of sheets) {
      const rows = readSheet(a, y).rows;
      const i = rows.findIndex((r) => r.transfer === row.transfer);
      if (i >= 0) {
        change(rows, i);
        return { other: [a, y, rows], also: " and the other side of the transfer in " + sheetKey(a, y) };
      }
    }
    return { other: undefined, also: "" };
  }

  function rowIndex(index, rows, allowEnd) {
    if (!isInt(index) || index < 0 || index >= rows.length + (allowEnd ? 1 : 0)) {
      fail("invalid", "row index out of range", "index");
    }
    return index;
  }

  // ---------------------------------------------------------------- history and undo
  // The real app reads these from git. Here every write keeps the row lists before and after it.

  // What turned `before` into `after`: [{kind, row, before, after}], kind one of changed, added,
  // deleted; row counts from 1 (in `before` for a deleted row).
  function changesBetween(before, after) {
    const a = before.map(rowText), b = after.map(rowText);
    let head = 0;
    while (head < a.length && head < b.length && a[head] === b[head]) head++;
    let tail = 0;
    while (tail < a.length - head && tail < b.length - head && a[a.length - 1 - tail] === b[b.length - 1 - tail]) tail++;
    const n = a.length - head - tail, m = b.length - head - tail; // what is left differs at both ends
    const out = [];
    const run = (i1, i2, j1, j2) => { // rows i1..i2 of before became rows j1..j2 of after
      const pairs = Math.min(i2 - i1, j2 - j1);
      for (let k = 0; k < pairs; k++) {
        out.push({ kind: "changed", row: j1 + k + 1, before: before[i1 + k], after: after[j1 + k] });
      }
      for (let i = i1 + pairs; i < i2; i++) {
        out.push({ kind: "deleted", row: i + 1, before: before[i], after: null,
                   above: i ? before[i - 1] : null, below: i + 1 < before.length ? before[i + 1] : null });
      }
      for (let j = j1 + pairs; j < j2; j++) out.push({ kind: "added", row: j + 1, before: null, after: after[j] });
    };
    // longest common run of rows in the middle part: common[i][j] is its length from i, j onwards
    const common = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        common[i][j] = a[head + i] === b[head + j] ? common[i + 1][j + 1] + 1
          : Math.max(common[i + 1][j], common[i][j + 1]);
      }
    }
    const same = (i, j) => i < n && j < m && a[head + i] === b[head + j];
    let i = 0, j = 0;
    while (i < n || j < m) {
      if (same(i, j)) { i++; j++; continue; }
      const i1 = i, j1 = j;
      while ((i < n || j < m) && !same(i, j)) {
        if (j >= m || (i < n && common[i + 1][j] >= common[i][j + 1])) i++;
        else j++;
      }
      run(head + i1, head + i, head + j1, head + j);
    }
    return out;
  }

  // `rows` with one earlier change to the sheet `key` taken back, or a "conflict" that says why
  // not. Rows are found by their content, so rows added or removed elsewhere since do not matter.
  function undone(key, files, rows, changes) {
    if (!changes.length) fail("conflict", "nothing in this sheet changed");
    if (files.length !== 1 || files[0] !== key) {
      fail("conflict", "it changed more than this sheet (a transfer, an import or the like)");
    }
    rows = [...rows];
    const find = (row, near) => {
      const text = rowText(row);
      let best = -1;
      rows.forEach((r, i) => {
        if (rowText(r) === text && (best < 0 || Math.abs(i - near) < Math.abs(best - near))) best = i;
      });
      if (best < 0) {
        fail("conflict", "a later change touched the same row; undo that one first, or put the old values back by hand");
      }
      return best;
    };
    const ordered = [...changes.filter((c) => c.kind !== "deleted"), ...changes.filter((c) => c.kind === "deleted")];
    for (const ch of ordered) { // deletions last: putting a row back shifts the others
      if (ch.kind === "changed") rows[find(ch.after, ch.row - 1)] = ch.before;
      else if (ch.kind === "added") rows.splice(find(ch.after, ch.row - 1), 1);
      else { // back between its old neighbours, wherever they are now
        let at;
        try {
          at = ch.above === null ? 0 : find(ch.above, ch.row - 2) + 1;
        } catch (e) {
          if (!(e instanceof ApiError)) throw e;
          at = ch.below === null ? rows.length : find(ch.below, ch.row - 1);
        }
        rows.splice(at, 0, ch.before);
      }
    }
    return rows;
  }

  // The latest changes to one sheet, newest first.
  const changesTo = (key, limit) => db.changes.filter((c) => c.files.includes(key)).reverse().slice(0, limit);

  // ---------------------------------------------------------------- recurring (recurring.py)

  // The date of occurrence number k (0 is the start date), or null past the last year allowed.
  // Counted from the start date each time, so "the 31st, monthly" does not drift.
  function occurrence(template, k) {
    const [y, m, d] = dateParts(template.start);
    const [unit, step] = SPEC.recurring_every[template.every];
    let date;
    if (unit === "days") date = utc(y, m, d + step * k);
    else {
      const months = y * 12 + m - 1 + step * k;
      const year = Math.floor(months / 12), month = months % 12 + 1;
      date = utc(year, month, Math.min(d, daysIn(year, month)));
    }
    return date.getUTCFullYear() <= SPEC.max_year ? isoOf(date) : null;
  }

  const nextDate = (template) => occurrence(template, template.count);

  // The dates up to today that have not been entered or skipped, oldest first.
  function due(template, today) {
    const dates = [];
    while (dates.length < SPEC.recurring_max_due) {
      const d = occurrence(template, template.count + dates.length);
      if (d === null || d > today) break;
      dates.push(d);
    }
    return dates;
  }

  function listRecurring() {
    const today = todayIso();
    const names = new Map(db.accounts.map((a) => [a.id, a.name]));
    const out = db.recurring.map((t) => ({ ...t, name: names.get(t.account) || t.account,
                                           next: nextDate(t) || "", due: due(t, today).length }));
    return { repo_clean: true, recurring: out, due: out.reduce((n, t) => n + t.due, 0) };
  }

  function findRecurring(id) {
    const i = db.recurring.findIndex((t) => t.id === id);
    if (i < 0) fail("not_found", "no such recurring transaction", "id");
    return i;
  }

  // ---------------------------------------------------------------- the operations
  // One per recorder operation (register/protocol.py OPS), under the same name and with the
  // same arguments. An operation the page asks for that is missing here answers "not available".

  function listTabs() {
    const accounts = db.accounts.map((a) => ({ ...a, years: yearly(a) ? years(a) : null }));
    return { repo_clean: true, accounts, types: SPEC.types };
  }

  function accountName(name) {
    name = checkText(name, "name", SPEC.name_limit).trim();
    if (!name) fail("invalid", "name is required", "name");
    return name;
  }

  function slugify(name) {
    const ascii = name.normalize("NFKD").replace(/[^\x00-\x7f]/g, "").toLowerCase();
    return ascii.replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, SPEC.account_id_limit).replace(/^-+|-+$/g, "") || "account";
  }

  const validYear = (year) => isInt(year) && year >= SPEC.min_year && year <= SPEC.max_year;

  const ops = {
    status: () => ({ repo_clean: true, locked: false }),
    list_tabs: listTabs,
    get_sheet: ({ account, year }) => sheetView(account, year),

    // Categories in use (most used first) and, for each description, its latest amount and
    // category (most recent first). Transfers and opening balances are left out.
    suggestions() {
      const counts = new Map(), payees = new Map();
      for (const [, , , r] of allRows(db.accounts)) {
        if (r.category) counts.set(r.category, (counts.get(r.category) || 0) + 1);
        const text = r.description.trim();
        if (!text || r.transfer || text === SPEC.opening) continue;
        const seen = payees.get(text);
        if (!seen || r.date >= seen.date) {
          payees.set(text, { description: text, amount: r.amount, category: r.category, date: r.date });
        }
      }
      const byDate = (p, q) => (p.date < q.date ? 1 : p.date > q.date ? -1 : 0);
      const byUse = (p, q) => counts.get(q) - counts.get(p) || (p < q ? -1 : p > q ? 1 : 0);
      return { categories: [...counts.keys()].sort(byUse).slice(0, SPEC.max_suggestions),
               payees: [...payees.values()].sort(byDate).slice(0, SPEC.max_suggestions)
                 .map(({ description, amount, category }) => ({ description, amount, category })) };
    },

    // Rows matching every given condition, in one account (all its years) or in all of them.
    // The amount range compares sizes, so 20 to 30 finds -25.00 too.
    search({ account, text = "", amount_min = "", amount_max = "", date_from = "", date_to = "" }) {
      const accounts = account === undefined ? db.accounts : [findAccount(account)];
      const [low, high] = [[amount_min, "amount_min"], [amount_max, "amount_max"]]
        .map(([v, f]) => (checkAmount(v, f) ? Math.abs(toCents(v)) : null));
      const start = checkDate(date_from, "date_from"), end = checkDate(date_to, "date_to");
      const needle = checkText(text, "text", SPEC.text_limits.description).trim().toLowerCase();
      if (!needle && low === null && high === null && !start && !end) {
        fail("invalid", "enter something to search for", "text");
      }
      const results = [];
      let more = false;
      for (const [a, y, i, r] of allRows(accounts)) {
        const size = Math.abs(toCents(r.amount));
        if (((low !== null || high !== null) && !r.amount)
            || (low !== null && size < low) || (high !== null && size > high)
            || ((start || end) && !r.date) || (start && r.date < start) || (end && r.date > end)
            || (needle && !SPEC.search_fields.some((f) => r[f].toLowerCase().includes(needle)))) continue;
        if (results.length === SPEC.max_results) { more = true; break; }
        results.push({ ...r, account: a.id, name: a.name, year: y, index: i });
      }
      return { results, more };
    },

    // Income and spending for one calendar year: by month, and spending by category and by
    // description. Left out: rows without a date or an amount, and transfers.
    report({ year, account }) {
      const accounts = account === undefined ? db.accounts : [findAccount(account)];
      if (year !== undefined && !validYear(year)) fail("invalid", "year out of range", "year");
      const transfer = SPEC.transfer_category.toLowerCase();
      const rows = allRows(accounts).map(([, , , r]) => r)
        .filter((r) => r.date && r.amount && !r.transfer && r.category.trim().toLowerCase() !== transfer);
      const found = [...new Set(rows.map((r) => yearOf(r.date)))].sort((a, b) => a - b);
      if (year === undefined) year = found.length ? found[found.length - 1] : yearOf(todayIso());
      const months = Array.from({ length: 12 }, () => ({ income: 0, spending: 0 }));
      const categories = new Map(), payees = new Map();
      for (const r of rows) {
        if (yearOf(r.date) !== year) continue;
        const amount = toCents(r.amount);
        months[Number(r.date.slice(5, 7)) - 1][amount > 0 ? "income" : "spending"] += Math.abs(amount);
        for (const [totals, name] of [[categories, r.category.trim()], [payees, r.description.trim()]]) {
          const entry = totals.get(name) || { total: 0, count: 0 };
          entry.total += amount;
          entry.count += 1;
          totals.set(name, entry);
        }
      }
      // a refund reduces its category: only what comes out as money spent is listed
      const spent = (totals) => [...totals].filter(([, e]) => e.total < 0)
        .map(([name, e]) => ({ name, total: -e.total, count: e.count }))
        .sort((p, q) => q.total - p.total || (p.name < q.name ? -1 : p.name > q.name ? 1 : 0))
        .slice(0, SPEC.max_report_rows)
        .map((item) => ({ ...item, total: fromCents(item.total) }));
      const income = months.reduce((n, m) => n + m.income, 0), spending = months.reduce((n, m) => n + m.spending, 0);
      return { year, years: [...new Set([...found, year])].sort((a, b) => a - b), account: account === undefined ? null : account,
               income: fromCents(income), spending: fromCents(spending), net: fromCents(income - spending),
               months: months.map((m, i) => ({ month: `${pad(year, 4)}-${pad(i + 1, 2)}`, income: fromCents(m.income),
                                               spending: fromCents(m.spending), net: fromCents(m.income - m.spending) })),
               categories: spent(categories), payees: spent(payees) };
    },

    insert_row({ account: id, year, rev, index, fields = {} }) {
      const { account, rows } = loadForWrite(id, year, rev);
      rowIndex(index, rows, true);
      rows.splice(index, 0, { ...blankRow(), ...editFields(account.type, fields) });
      return save(account, year, rows, `${sheetKey(account, year)}: insert row ${index + 1}`);
    },

    update_row({ account: id, year, rev, index, fields }) {
      const { account, rows } = loadForWrite(id, year, rev);
      rowIndex(index, rows);
      const changes = editFields(account.type, fields);
      const names = Object.keys(changes).sort();
      if (!names.length) fail("invalid", "no fields to change", "fields");
      const before = rows[index];
      rows[index] = { ...before, ...changes };
      let other, also = "";
      if ("amount" in changes || "date" in changes) { // the two sides of a transfer stay in step
        const follow = (otherRows, i) => {
          otherRows[i] = { ...otherRows[i], date: rows[index].date, amount: negated(rows[index].amount) };
        };
        // the old date says where the other side is
        ({ other, also } = otherSide({ ...rows[index], date: before.date }, account, follow));
      }
      return save(account, year, rows, `${sheetKey(account, year)}: edit row ${index + 1} (${names.join(", ")})${also}`, other);
    },

    delete_row({ account: id, year, rev, index }) {
      const { account, rows } = loadForWrite(id, year, rev);
      rowIndex(index, rows);
      const [row] = rows.splice(index, 1);
      // a transfer is deleted from both accounts
      const { other, also } = otherSide(row, account, (otherRows, i) => otherRows.splice(i, 1));
      return save(account, year, rows, `${sheetKey(account, year)}: delete row ${index + 1}${also}`, other);
    },

    // Tick and untick rows of one sheet, by index, in one change.
    reconcile({ account: id, year, rev, cleared, uncleared = [] }) {
      const { account, rows } = loadForWrite(id, year, rev);
      const marks = new Map();
      for (const [value, indexes] of [[true, cleared], [false, uncleared]]) {
        for (const i of indexes) {
          if (marks.has(rowIndex(i, rows))) fail("invalid", `row ${i + 1} is listed twice`, "index");
          marks.set(i, value);
        }
      }
      if (!marks.size) fail("invalid", "no rows to mark", "cleared");
      for (const [i, value] of marks) rows[i] = { ...rows[i], cleared: value };
      return save(account, year, rows, `${sheetKey(account, year)}: reconcile (${marks.size} rows)`);
    },

    // One row appended to each account, linked by a shared id, in one change. amount is positive:
    // it leaves from_account. The id may be chosen by the page, so that the same request sent
    // twice does not enter the transfer twice.
    add_transfer({ from_account, to_account, date, amount, description = "", category = SPEC.transfer_category, note = "", id }) {
      const source = findAccount(from_account), target = findAccount(to_account);
      if (source.id === target.id) fail("invalid", "choose two different accounts", "to_account");
      const f = editFields(source.type, { date, amount, description, category, note });
      if (!f.date) fail("invalid", "a transfer needs a date", "date");
      if (!f.amount || toCents(f.amount) <= 0) fail("invalid", "the amount must be more than zero", "amount");
      if (id !== undefined && !LINK_ID.test(id)) fail("invalid", "invalid transfer id", "id");
      const link = id || randomId(8);
      const held = id === undefined ? [] : allRows(db.accounts).filter(([, , , r]) => r.transfer === link);
      if (held.length) { // this very transfer, sent again: nothing is written, the same answer given
        const sides = (list) => list.map((side) => side.join("|")).sort().join("\n");
        const want = [[source.id, f.date, negated(f.amount)], [target.id, f.date, f.amount]];
        if (sides(held.map(([a, , , r]) => [a.id, r.date, r.amount])) !== sides(want)) {
          fail("conflict", "a transfer was already entered with this id, with a different date, amount or accounts; " +
               "check both accounts before entering it again", "id");
        }
        const where = new Map(held.map(([a, y]) => [a.id, y]));
        return { repo_clean: true, from: { account: source.id, year: where.get(source.id) },
                 to: { account: target.id, year: where.get(target.id) } };
      }
      const sides = [[source, target, "to", negated(f.amount)], [target, source, "from", f.amount]].map(([a, other, word, value]) => {
        const existing = sheetsOf(a);
        const y = yearTab(a, f.date, existing);
        const rows = existing.includes(y) ? readSheet(a, y).rows : []; // else: next year's tab
        const text = f.description || [...`Transfer ${word} ${other.name}`].slice(0, SPEC.text_limits.description).join("");
        rows.push({ ...blankRow(), date: f.date, description: text, category: f.category, amount: value, note: f.note, transfer: link });
        return [a, y, rows];
      });
      const [[a, y, rows], other] = sides;
      save(a, y, rows, `transfer from ${sheetKey(a, y)} to ${sheetKey(other[0], other[1])}`, other);
      return { repo_clean: true, from: { account: a.id, year: y }, to: { account: other[0].id, year: other[1] } };
    },

    // Each change with the rows it changed and whether it can be undone now.
    history({ account: id, year, limit = SPEC.history_default }) {
      if (!isInt(limit) || limit < 1 || limit > SPEC.history_max) {
        fail("invalid", `limit must be between 1 and ${SPEC.history_max}`, "limit");
      }
      const account = findAccount(id);
      const current = readSheet(account, year).rows;
      const key = sheetKey(account, year), label = key + ": ";
      const listed = changesTo(key, limit + 1);
      const entries = listed.slice(0, limit).map((c) => {
        const changes = changesBetween(c.versions[key].before, c.versions[key].after);
        const entry = { commit: c.id, date: c.date, subject: c.subject,
                        summary: c.subject.startsWith(label) ? c.subject.slice(label.length) : c.subject,
                        changes: changes.slice(0, SPEC.history_changes).map(({ kind, row, before, after }) => ({ kind, row, before, after })),
                        more: Math.max(0, changes.length - SPEC.history_changes), undo: false };
        try {
          undone(key, c.files, current, changes);
          entry.undo = true;
        } catch (e) {
          if (!(e instanceof ApiError)) throw e;
          entry.why = e.message;
        }
        return entry;
      });
      return { repo_clean: true, entries, more: listed.length > limit };
    },

    // Take back one earlier change to a sheet, as a new change.
    undo({ account: id, year, rev, commit: changeId }) {
      const { account, rows } = loadForWrite(id, year, rev);
      const key = sheetKey(account, year);
      const change = changesTo(key, SPEC.history_max + 1).find((c) => c.id === changeId);
      if (!change) fail("not_found", "no such change to this sheet", "commit");
      let restored;
      try {
        restored = undone(key, change.files, rows, changesBetween(change.versions[key].before, change.versions[key].after));
      } catch (e) {
        if (!(e instanceof ApiError)) throw e;
        fail("conflict", "cannot undo: " + e.message, "commit");
      }
      return save(account, year, restored, `${key}: undo ${changeId.slice(0, 10)}`);
    },

    list_recurring: listRecurring,

    add_recurring({ account, description, amount, every, start, category = "", note = "" }) {
      if (db.recurring.length >= SPEC.recurring_max_templates) {
        fail("invalid", `there are already ${SPEC.recurring_max_templates} recurring transactions`);
      }
      const text = {};
      for (const [f, v] of [["description", description], ["category", category], ["note", note]]) {
        text[f] = checkText(v, f, SPEC.text_limits[f]).trim();
      }
      const target = findAccount(account);
      if (!text.description) fail("invalid", "is required", "description");
      if (!checkAmount(amount)) fail("invalid", "is required", "amount");
      if (!Object.keys(SPEC.recurring_every).includes(every)) {
        fail("invalid", "must be one of " + Object.keys(SPEC.recurring_every).join(", "), "every");
      }
      if (!checkDate(start, "start")) fail("invalid", "is required", "start");
      db.recurring = [...db.recurring, { id: randomId(8), account: target.id, description: text.description,
                                         category: text.category, amount, note: text.note, every, start, count: 0 }];
      return listRecurring();
    },

    delete_recurring({ id }) {
      const i = findRecurring(id);
      db.recurring = db.recurring.filter((t, n) => n !== i);
      return listRecurring();
    },

    // Pass over the next date without entering it. count: how many dates the page saw as entered
    // or skipped so far; if that is no longer so, nothing is skipped.
    skip_recurring({ id, count }) {
      const i = findRecurring(id);
      const t = db.recurring[i];
      if (count !== undefined && count !== t.count) {
        fail("stale", "that date was already entered or skipped; look at the list again", "count");
      }
      if (nextDate(t) === null) fail("invalid", "this recurring transaction has no later date", "id");
      db.recurring = db.recurring.map((x, n) => (n === i ? { ...x, count: x.count + 1 } : x));
      return listRecurring();
    },

    // Enter every due date of the given templates (all of them by default) as rows at the end of
    // their accounts' sheets, in one change.
    enter_recurring({ ids }) {
      const templates = [...db.recurring], today = todayIso();
      if (ids !== undefined && !ids.every((i) => typeof i === "string" && templates.some((t) => t.id === i))) {
        fail("not_found", "no such recurring transaction", "ids");
      }
      const touched = new Map(); // account id -> Map(year -> rows)
      let entered = 0;
      templates.forEach((t, n) => {
        const dates = ids === undefined || ids.includes(t.id) ? due(t, today) : [];
        if (!dates.length) return;
        const account = findAccount(t.account);
        if (!touched.has(account.id)) touched.set(account.id, new Map());
        const sheets = touched.get(account.id), stored = sheetsOf(account);
        for (const d of dates) {
          let year;
          try {
            year = yearTab(account, d, [...new Set([...stored, ...sheets.keys()])]);
          } catch (e) {
            if (!(e instanceof ApiError)) throw e;
            fail(e.code, `"${t.description}": ${e.message}`, e.field);
          }
          if (!sheets.has(year)) sheets.set(year, stored.includes(year) ? readSheet(account, year).rows : []);
          sheets.get(year).push({ ...blankRow(), date: d, description: t.description, category: t.category,
                                  amount: t.amount, note: t.note });
        }
        templates[n] = { ...t, count: t.count + dates.length };
        entered += dates.length;
      });
      if (!entered) fail("invalid", "nothing is due", "ids");
      const files = [];
      for (const [id, sheets] of touched) {
        const account = db.accounts.find((a) => a.id === id);
        checkBalances(account, sheets);
        for (const [y, rows] of sheets) files.push([sheetKey(account, y), rows]);
      }
      db.recurring = templates;
      commit(`enter ${entered} recurring transaction(s)`, files, [RECURRING_FILE]);
      return { ...listRecurring(), entered };
    },

    add_year({ account: id }) {
      const account = findAccount(id);
      if (!yearly(account)) fail("invalid", account.id + " has no year tabs", "account");
      const have = years(account);
      const year = have.length ? have[have.length - 1] + 1 : yearOf(todayIso());
      if (year > SPEC.max_year) fail("invalid", "year out of range", "year");
      commit(`${account.id}: add year ${year}`, [[sheetKey(account, year), []]]);
      return { year, ...listTabs() };
    },

    // opening_balance is in the stored sign: negative for what is owed on a card or loan.
    add_account({ name, type, opening_balance, year = null, owed_positive }) {
      name = accountName(name);
      if (!TYPES.has(type)) fail("invalid", "type must be one of " + [...TYPES.keys()].join(", "), "type");
      const opening = checkAmount(opening_balance, "opening_balance") || "0.00";
      if (TYPES.get(type).yearly) {
        if (year === null) year = yearOf(todayIso());
        if (!validYear(year)) fail("invalid", "year out of range", "year");
      } else if (year !== null) fail("invalid", type + " accounts have no year tabs", "year");
      const taken = new Set(db.accounts.map((a) => a.id));
      const base = slugify(name);
      let id = base;
      for (let n = 2; taken.has(id); n++) id = base.slice(0, SPEC.account_id_limit - `-${n}`.length) + `-${n}`;
      const account = { id, name, type, owed_positive: owed_positive === undefined ? TYPES.get(type).owed_positive : owed_positive };
      db.accounts = [...db.accounts, account];
      commit(`add account ${id} (${type})`, [[sheetKey(account, year), [{ ...blankRow(), description: SPEC.opening, amount: opening }]]],
             [ACCOUNTS_FILE]);
      return { account: id, ...listTabs() };
    },

    // The name and, if given, whether amounts are shown with the sign flipped. Stored amounts
    // never change.
    rename_account({ account: id, name, owed_positive }) {
      name = accountName(name);
      const target = findAccount(id);
      const changed = { ...target, name };
      if (owed_positive !== undefined) changed.owed_positive = owed_positive;
      db.accounts = db.accounts.map((a) => (a.id === target.id ? changed : a));
      return listTabs();
    },

    export: () => fail(UNAVAILABLE, "Export is not available in the demo: the data is made up and there is nothing to " +
                       "download. The real app downloads every sheet as CSV."),
    change_password: () => fail(UNAVAILABLE, "There is no password in the demo. The real app encrypts everything under " +
                                "a password and asks for it at login."),
  };

  // ---------------------------------------------------------------- requests (web/app.py)

  const ROUTES = SPEC.routes.map((r) => ({ ...r, pattern: new RegExp(r.path) }));
  const KINDS = {
    str: (v) => typeof v === "string", int: isInt, bool: (v) => typeof v === "boolean",
    dict: isObject, list: Array.isArray, year: (v) => v === null || isInt(v),
  };

  const hasBalance = (v) => (Array.isArray(v) ? v.some(hasBalance)
    : isObject(v) ? "balance" in v || Object.values(v).some(hasBalance) : false);

  // False if any string in the body, keys included, holds half of a surrogate pair (a \uD800
  // escape in the JSON): with the u flag a whole pair is one character, so only a lone half matches.
  const validText = (v) => (typeof v === "string" ? !/\p{Cs}/u.test(v)
    : Array.isArray(v) ? v.every(validText)
    : isObject(v) ? Object.entries(v).every(([k, x]) => validText(k) && validText(x)) : true);

  // The operation's arguments: the JSON body, checked as the web process checks it, and the
  // parts of the path; then their types, as the recorder checks them.
  function readArgs(route, match, method, text) {
    const args = {};
    if (method !== "GET" && (text || route.allowed.length)) {
      let body;
      try { body = text ? JSON.parse(text) : {}; } catch (e) { fail("bad_request", "body is not valid JSON"); }
      if (!isObject(body)) fail("bad_request", "body must be a JSON object");
      let refused;
      try {
        if (!validText(body)) fail("bad_request", "the request contains text that is not valid Unicode");
        refused = hasBalance(body);
      } catch (e) { // the browser's own limit; Python's is lower (see demo/README.md)
        // too deep for the browser's stack: a RangeError, or Firefox's InternalError
        if (e instanceof RangeError || e.name === "InternalError") fail("bad_request", "body is nested too deeply");
        throw e;
      }
      if (refused) fail("balance_readonly", "the balance is computed and cannot be written", "balance");
      const extra = Object.keys(body).filter((k) => !route.allowed.includes(k)).sort();
      if (extra.length) fail("bad_request", "unexpected field: " + extra[0]);
      const missing = route.required.filter((k) => !(k in body)).sort();
      if (missing.length) fail("bad_request", "missing field: " + missing[0]);
      Object.assign(args, body);
    }
    for (const [key, value] of Object.entries(match.groups || {})) {
      if (key === "year") args.year = value === "-" ? null : Number(value);
      else args[key] = key === "index" ? Number(value) : value;
    }
    for (const [key, [kind, required]] of Object.entries(SPEC.ops[route.op])) {
      if (!(key in args)) {
        if (required) fail("bad_request", "missing argument: " + key, key);
      } else if (!KINDS[kind](args[key])) fail("bad_request", "wrong type for " + key, key);
    }
    return args;
  }

  function answer(method, path, text) {
    if (!path.startsWith("/api/")) fail("not_found", "not found");
    let known = false;
    for (const route of ROUTES) {
      const match = route.pattern.exec(path);
      if (!match) continue;
      known = true;
      if (route.method !== method) continue;
      const args = readArgs(route, match, method, text);
      if (!ops[route.op]) fail(UNAVAILABLE, "This is not available in the demo.");
      return ops[route.op](args);
    }
    return known ? fail("method_not_allowed", "method not allowed") : fail("not_found", "not found");
  }

  const json = (status, body) => new Response(JSON.stringify(body), {
    status, headers: { "Content-Type": "application/json; charset=utf-8" } });

  // In place of the browser's fetch. It answers the app's own routes and refuses everything
  // else, so no request can leave the page through it.
  window.fetch = async (input, init = {}) => {
    const url = new URL(typeof input === "string" ? input : input.url || String(input), location.href);
    const method = (init.method || "GET").toUpperCase();
    if (url.origin !== location.origin) throw new TypeError("The demo makes no network requests.");
    if (url.pathname === "/logout" || url.pathname === "/logout/all") {
      // Nothing to log out of: start over. The answer never comes, so the page does not go on to
      // the login page, which the demo does not have.
      location.reload();
      return new Promise(() => {});
    }
    try {
      return json(200, answer(method, url.pathname, typeof init.body === "string" ? init.body : ""));
    } catch (e) {
      if (!(e instanceof ApiError)) {
        console.error(e); // a mistake in this file, not in what was asked
        return json(500, { error: "internal", message: "internal error" });
      }
      const body = { error: e.code, message: e.message };
      if (e.field) body.field = e.field;
      return json(STATUS[e.code] || 500, body);
    }
  };

  for (const route of ROUTES) { // the app gained a route this file does not know yet
    if (!ops[route.op]) console.warn("account-register demo: no in-memory version of " + route.op);
  }

  // ---------------------------------------------------------------- the starting data
  // The same kind of made-up household as tools/demo_home.py, but dated from today backwards
  // (last year and this year), so the demo never looks stale and a transfer dated today always
  // has a year tab to go in. The amounts come from a fixed seed: every reload gives the same data.

  // (day of month, description, category, amount or [low, high])
  const MONTHLY = [
    [1, "Rent", "Housing", "-1450.00"],
    [3, "Harbor Electric", "Utilities", [-140, -70]],
    [5, "Paycheck", "Income", "1385.20"],
    [8, "Car insurance", "Car", "-118.40"],
    [9, "Corner Market", "Food", [-190, -90]],
    [12, "City Water", "Utilities", [-60, -35]],
    [15, "Transfer to Rainy Day", "Transfer", "-200.00"],
    [17, "Internet", "Utilities", "-65.00"],
    [19, "Paycheck", "Income", "1385.20"],
    [22, "Corner Market", "Food", [-190, -90]],
    [24, "Phone", "Utilities", "-45.00"],
    [27, "Bookshop", "Fun", [-45, -8]],
  ];
  // year (0 is this year), month, day -> note
  const NOTES = { "-1/3/27": "birthday present", "-1/7/3": "includes the late fee", "0/2/22": "party supplies" };

  function seededRandom(seed) { // mulberry32: small, and the same numbers in every browser
    return () => {
      seed = (seed + 0x6D2B79F5) | 0;
      let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function seed() {
    const random = seededRandom(1);
    const between = (low, high) => low + Math.floor(random() * (high - low + 1));
    const linkId = () => hex(Array.from({ length: 8 }, () => between(0, 255)));
    const row = (fields) => ({ ...blankRow(), ...fields });
    const today = todayIso(), thisYear = yearOf(today);
    const clearedUntil = addDays(today, -35); // about the last month is not ticked yet: something to reconcile
    const cleared = (date) => date < clearedUntil;

    const everyday = [], rainy = [], visa = [];
    let check = 1040;
    for (const year of [thisYear - 1, thisYear]) {
      for (let month = 1; month <= 12; month++) {
        for (const [day, what, category, amount] of MONTHLY) {
          const date = iso(year, month, day);
          if (date > today) continue;
          const fields = { date, description: what, category, cleared: cleared(date),
                           amount: typeof amount === "string" ? amount : fromCents(between(amount[0] * 100, amount[1] * 100)),
                           note: NOTES[`${year - thisYear}/${month}/${day}`] || "" };
          if (what === "Rent" || what === "City Water") fields.num = String(++check); // paid by check
          if (what === "Transfer to Rainy Day") { // a real transfer: the two rows are linked
            fields.transfer = linkId();
            rainy.push(row({ date, description: "Transfer from Everyday", category: SPEC.transfer_category,
                             amount: "200.00", transfer: fields.transfer }));
          }
          everyday.push(row(fields));
        }
        const interest = iso(year, month, 28);
        if (month % 3 === 0 && interest <= today) {
          rainy.push(row({ date: interest, description: "Interest", category: "Interest", amount: (11 + month * 0.37).toFixed(2) }));
        }
      }
    }
    rainy.push(row({ date: iso(thisYear - 1, 6, 2), description: "Car repair", category: "Car", amount: "-640.00", note: "new brakes" }));

    // a recurring transaction with one date left to enter, and one that is not due yet
    const gym = { id: linkId(), account: "everyday", description: "Gym membership", category: "Health", amount: "-32.00",
                  note: "", every: "month", start: iso(thisYear - 1, 1, 2), count: 0 };
    const gymDates = due({ ...gym }, today);
    for (const date of gymDates.slice(0, -1)) {
      everyday.push(row({ date, description: gym.description, category: gym.category, amount: gym.amount, cleared: cleared(date) }));
    }
    gym.count = Math.max(0, gymDates.length - 1);
    const [y, m] = dateParts(today);
    const rent = { id: linkId(), account: "everyday", description: "Rent", category: "Housing", amount: "-1450.00",
                   note: "", every: "month", start: m === 12 ? iso(y + 1, 1, 1) : iso(y, m + 1, 1), count: 0 };
    db.recurring = [gym, rent];

    // the card: a few charges over the last six weeks, and last month's bill paid from Everyday
    const paid = addDays(today, -19), payment = linkId();
    visa.push(row({ date: addDays(today, -41), description: "Bookshop", category: "Fun", amount: "-23.90", cleared: true }),
              row({ date: addDays(today, -33), description: "Train tickets", category: "Travel", amount: "-64.00", cleared: true }),
              row({ date: paid, description: "Payment, thank you", category: SPEC.transfer_category, amount: "212.40",
                    cleared: true, transfer: payment }),
              row({ date: addDays(today, -6), description: "Hardware store", category: "Home", amount: "-38.15" }));
    everyday.push(row({ date: paid, description: "Transfer to Visa", category: SPEC.transfer_category, amount: "-212.40",
                        cleared: true, transfer: payment }));

    const loaded = new Date(Date.now() - 3 * 24 * 3600 * 1000).toISOString(); // "last saved" three days ago
    const byDate = (p, q) => (p.date < q.date ? -1 : p.date > q.date ? 1 : 0);
    const add = (id, name, type, opening, rows) => {
      const account = { id, name, type, owed_positive: TYPES.get(type).owed_positive };
      db.accounts.push(account);
      rows.sort(byDate); // stable: rows of one day stay in the order they were made
      const first = row({ description: SPEC.opening, amount: opening, cleared: type !== "savings" });
      if (!yearly(account)) {
        db.sheets.set(sheetKey(account, null), { rows: [first, ...rows], rev: "demo-" + ++revCount, modified: loaded });
        return;
      }
      for (let year = yearOf(rows[0].date); year <= thisYear; year++) { // an unbroken run of years
        const inYear = rows.filter((r) => yearOf(r.date) === year);
        db.sheets.set(sheetKey(account, year), { rows: year === yearOf(rows[0].date) ? [first, ...inYear] : inYear,
                                                 rev: "demo-" + ++revCount, modified: loaded });
      }
    };
    add("everyday", "Everyday", "checking", "1200.00", everyday);
    add("rainy-day", "Rainy Day", "savings", "5000.00", rainy);
    add("visa", "Visa", "card", "-212.40", visa); // stored negative: owed; shown positive

    // Two edits made through the operations above, a couple of hours ago, so that History has
    // something to show, and to undo, from the start.
    const earlier = Date.now() - 2 * 3600 * 1000;
    const sheet = () => db.sheets.get("everyday/" + thisYear);
    clock = () => new Date(earlier);
    ops.insert_row({ account: "everyday", year: thisYear, rev: sheet().rev, index: sheet().rows.length,
                     fields: { date: today, description: "Farmers market", category: "Food", amount: "-27.50" } });
    clock = () => new Date(earlier + 4 * 60 * 1000);
    ops.update_row({ account: "everyday", year: thisYear, rev: sheet().rev, index: sheet().rows.length - 1,
                     fields: { note: "cash" } });
    clock = () => new Date();
  }

  seed();

  // The page's own script starts only now, with both stand-ins in place. If either had failed
  // to load, grid.js would otherwise run against the browser's real storage and network.
  if (window.REGISTER_DEMO_NOSTORE) {
    // hides the "did not start" message; set here, before the body is drawn, so it never flashes
    document.documentElement.classList.add("demo-started");
    // with demo.css missing, the class hides nothing: hide the message itself, the moment the parser adds it
    const hideFailed = () => {
      const failed = document.getElementById("demo-failed");
      if (failed) failed.hidden = true;
      return failed;
    };
    const watch = new MutationObserver(() => { if (hideFailed()) watch.disconnect(); });
    watch.observe(document.documentElement, { childList: true, subtree: true });
    const start = () => {
      // Export and Security... stay in the page, but their password fields take no input: nobody
      // should type a password into a public page, or have a browser offer to save one for it.
      for (const input of document.querySelectorAll("input[type=password]")) {
        input.disabled = true;
        input.placeholder = "not used in the demo";
      }
      watch.disconnect();
      const failed = hideFailed();
      const script = document.createElement("script");
      script.src = "grid.js";
      script.onerror = () => { // the page's own script did not load: say so after all
        document.documentElement.classList.remove("demo-started");
        if (failed) failed.hidden = false;
      };
      document.head.append(script);
    };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
    else start();
  }
})();
