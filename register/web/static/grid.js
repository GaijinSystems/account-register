// account-register: spreadsheet-style register UI. Plain JS, no dependencies.
"use strict";
(() => {
  const CSRF = document.querySelector('meta[name="csrf-token"]').content;
  const STATUS_POLL_MS = 30000;
  const IDLE_SAVE_MS = 30000; // save a row's staged edits after this long without typing
  const BUFFER_ROWS = 3;      // blank rows always shown below the last entry (never stored)

  // Every account type has the same columns, so switching tabs doesn't shift the layout.
  const COLUMNS = [
    { key: "num", w: 64, label: "Num", cls: "c-num" },
    { key: "date", w: 100, label: "Date", cls: "c-date" },
    { key: "description", w: 300, label: "Description", cls: "c-desc" },
    { key: "category", w: 130, label: "Category", cls: "c-cat" },
    { key: "amount", w: 120, label: "Amount", cls: "c-amt" },
    { key: "cleared", w: 34, label: "X", cls: "c-x" },
    { key: "balance", w: 130, label: "Balance", cls: "c-bal", readonly: true },
    { key: "note", w: 280, label: "Note", cls: "c-note" },
  ];

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  };

  const state = {
    tabs: [],          // accounts from /api/tabs
    types: [],         // the account types, from the same call
    sheet: null,       // current sheet from /api/sheet
    sel: { r: 0, c: 0 },
    editing: null,     // { r, c, input }
    dirty: false,      // repo has uncommitted changes
    recorderDown: false,
    pending: new Map(), // "r|key" -> value not yet saved (staged until the row is left)
    blocked: false,     // the last save attempt failed; pending edits wait for Retry
    busy: 0,            // save requests in flight
    queue: Promise.resolve(),
    cells: [],         // td elements [r][c]
    recurring: { recurring: [], due: 0 }, // from /api/recurring
    recon: null,       // while reconciling: { statement (cents), rev, marks: Map(row -> ticked) }
    hints: { categories: [], payees: [] }, // autocomplete: from /api/suggestions, kept up to date locally
  };

  // ------------------------------------------------------------------ API

  async function api(method, path, body, keepalive) {
    const opts = { method, headers: {}, credentials: "same-origin", keepalive: !!keepalive };
    if (method !== "GET") {
      opts.headers["X-CSRF-Token"] = CSRF;
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body || {});
    }
    let res;
    try {
      res = await fetch(path, opts);
    } catch (e) {
      throw { status: 0, error: "network", message: "Cannot reach the server." };
    }
    if (res.status === 401) {
      location.href = "/login";
      throw { status: 401, error: "unauthorized", message: "Logged out." };
    }
    let data = null;
    try { data = await res.json(); } catch (e) { /* empty body */ }
    if (!res.ok) {
      throw { status: res.status, error: (data && data.error) || "error",
              message: (data && data.message) || res.statusText, field: data && data.field };
    }
    return data;
  }

  // ------------------------------------------------------------------ formatting and parsing

  function fmtMoney(s) {
    if (s === "" || s === undefined || s === null) return "";
    const neg = s.startsWith("-");
    const [i, d] = (neg ? s.slice(1) : s).split(".");
    const grouped = i.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return (neg ? "-$" : "$") + grouped + "." + (d || "00");
  }

  function fmtDate(iso) {
    if (!iso) return "";
    const [y, m, d] = iso.split("-");
    return `${m}/${d}/${y}`;
  }

  // Accepts "", 1234.5, -1,234.56, $1,234.56, (84.12), -$84.12, +2150. Returns canonical "-1234.56".
  // In debit mode a number without an explicit sign is money out: 84.12 -> -84.12; +2150 stays positive.
  function parseAmount(text, debit) {
    let s = text.trim().replace(/\s+/g, "");
    if (s === "") return "";
    const plus = s.startsWith("+");
    if (plus) s = s.slice(1);
    const explicit = plus || s.startsWith("-") || s.startsWith("(");
    let neg = false;
    if (s.startsWith("(") && s.endsWith(")")) { neg = true; s = s.slice(1, -1); }
    if (s.startsWith("-")) { neg = !neg; s = s.slice(1); }
    if (s.startsWith("$")) s = s.slice(1);
    if (s.startsWith("-")) { neg = !neg; s = s.slice(1); }
    if (!/^(\d{1,3}(,\d{3})+|\d+)?(\.\d*)?$/.test(s) || s === "" || s === ".") {
      throw "Not an amount: " + text;
    }
    s = s.replace(/,/g, "");
    let [i, d = ""] = s.split(".");
    if (d.length > 2) throw "Use at most two decimal places.";
    i = i.replace(/^0+(?=\d)/, "") || "0";
    if (i.length > 12) throw "Amount too large.";
    d = (d + "00").slice(0, 2);
    if (debit && !explicit) neg = true;
    if (/^0+$/.test(i) && d === "00") neg = false;
    return (neg ? "-" : "") + i + "." + d;
  }

  // Accepts "", YYYY-MM-DD, M/D/YYYY, M/D/YY, M/D (sheet year). Returns ISO.
  function parseDate(text) {
    const s = text.trim();
    if (s === "") return "";
    let y, m, d, mm;
    if ((mm = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/))) {
      [y, m, d] = [+mm[1], +mm[2], +mm[3]];
    } else if ((mm = s.match(/^(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2}|\d{4}))?$/))) {
      [m, d] = [+mm[1], +mm[2]];
      if (mm[3] === undefined) y = (state.sheet && state.sheet.year) || new Date().getFullYear();
      else y = mm[3].length === 2 ? 2000 + +mm[3] : +mm[3];
    } else {
      throw "Not a date: " + text + " (use MM/DD/YYYY)";
    }
    const dt = new Date(Date.UTC(y, m - 1, d));
    if (dt.getUTCFullYear() !== y || dt.getUTCMonth() !== m - 1 || dt.getUTCDate() !== d) {
      throw "Not a real date: " + text;
    }
    if (y < 1900 || y > 2199) throw "Year out of range: " + y;
    return `${String(y).padStart(4, "0")}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
  }

  function parseValue(key, text) {
    if (key === "amount") return shown(parseAmount(text, debitMode())); // debit mode is off where the sign is flipped
    if (key === "date") return parseDate(text);
    return text.trim();
  }

  // ------------------------------------------------------------------ cell values

  function columns() { return state.sheet ? COLUMNS : []; }
  function rowCount() { return state.sheet ? state.sheet.rows.length + BUFFER_ROWS : 0; } // + blank buffer
  function pkey(r, key) { return r + "|" + key; }

  const blankValue = (key) => (key === "cleared" ? false : "");
  const colClass = (c) => c.cls + (c.readonly ? " ro" : "");
  const balanceAt = (r) => (state.sheet.rows[r] ? state.sheet.rows[r].balance : "");
  const nextYear = (a) => (a.years.length ? a.years[a.years.length - 1] : new Date().getFullYear() - 1) + 1;
  const yearPart = (y) => (y === null ? "-" : y);

  // "Owed shown positive" (credit cards, loans): amounts and balances are displayed, typed and
  // copied with the sign flipped. Stored values, and everything sent to the server, are not.
  const negated = (s) => (!s || /^0+\.00$/.test(s) ? s : s.startsWith("-") ? s.slice(1) : "-" + s);
  const flipsSign = (accountId) => {
    if (state.sheet && state.sheet.account === accountId) return flipped(); // fresher than the tab list
    const a = state.tabs.find((x) => x.id === accountId);
    return !!(a && a.owed_positive);
  };
  function flipped() { return !!(state.sheet && state.sheet.owed_positive); } // the open sheet's own setting
  const shown = (s, flip = flipped()) => (flip ? negated(s) : s);
  // what the server says about an account type: {id, label, yearly, owed_positive, debit_mode}
  const typeInfo = (id) => state.types.find((t) => t.id === id) || {};

  function rawValue(r, key) {
    if (key === "cleared" && state.recon && state.recon.marks.has(r)) return state.recon.marks.get(r);
    const p = state.pending.get(pkey(r, key));
    if (p !== undefined) return p;
    const row = state.sheet.rows[r];
    if (!row) return blankValue(key);
    return row[key];
  }

  function displayValue(r, key) {
    const v = rawValue(r, key);
    if (key === "amount" || key === "balance") return fmtMoney(shown(v));
    if (key === "date") return fmtDate(v);
    if (key === "cleared") return v ? "x" : "";
    return v === undefined || v === null ? "" : String(v);
  }

  function editText(r, key) {
    const v = rawValue(r, key);
    if (key === "date") return fmtDate(v);
    if (key === "amount") return shown(v);
    return v === undefined || v === null ? "" : String(v);
  }

  // ------------------------------------------------------------------ rendering

  function letter(i) {
    let s = "";
    for (i += 1; i; i = Math.floor((i - 1) / 26)) s = String.fromCharCode(65 + ((i - 1) % 26)) + s;
    return s;
  }

  function renderGrid() {
    const table = $("grid");
    table.replaceChildren();
    state.cells = [];
    if (!state.sheet) return;
    const cols = columns();
    const colgroup = el("colgroup");
    const rownum = el("col", "c-rownum");
    rownum.style.width = ROWNUM_W + "px";
    colgroup.append(rownum);
    state.colEls = {};
    cols.forEach((c) => {
      const col = el("col", c.cls);
      col.style.width = c.w + "px";
      state.colEls[c.key] = col;
      colgroup.append(col);
    });
    table.append(colgroup);

    const thead = el("thead");
    const letters = el("tr", "hdr-letters");
    letters.setAttribute("aria-hidden", "true"); // A, B, C: for the eye; the names below are the headers
    letters.append(el("td", "corner"));
    cols.forEach((c, i) => letters.append(el("th", "", letter(i))));
    const names = el("tr", "hdr-names");
    names.append(el("td", "corner"));
    // with "owed shown positive" the balance column says what it is
    cols.forEach((c) => {
      const th = el("th", colClass(c), c.key === "balance" && flipped() ? "Owed" : c.label);
      th.scope = "col";
      if (c.key === "cleared") th.setAttribute("aria-label", "Cleared");
      names.append(th);
    });
    thead.append(letters, names);
    table.append(thead);

    const tbody = el("tbody");
    if (state.sheet.carried_from !== null) {
      const tr = el("tr", "carried");
      tr.append(el("td", "rownum", ""));
      cols.forEach((c) => {
        let text = "";
        if (c.key === "description") text = "Carried forward from " + state.sheet.carried_from;
        if (c.key === "balance") text = fmtMoney(shown(state.sheet.opening));
        const td = el("td", c.cls + " ro", text);
        if (c.key === "balance" && shown(state.sheet.opening).startsWith("-")) td.classList.add("neg");
        tr.append(td);
      });
      tbody.append(tr);
    }
    for (let r = 0; r < rowCount(); r++) {
      const tr = el("tr", r >= state.sheet.rows.length ? "newrow" : "");
      const th = el("th", "rownum", String(r + 1));
      th.dataset.r = r;
      th.scope = "row";
      tr.append(th);
      const rowCells = [];
      cols.forEach((c, ci) => {
        const td = el("td", colClass(c));
        td.id = `cell-${r}-${ci}`;
        if (c.readonly) td.setAttribute("aria-readonly", "true");
        td.dataset.r = r;
        td.dataset.c = ci;
        fillCell(td, r, c);
        tr.append(td);
        rowCells.push(td);
      });
      state.cells.push(rowCells);
      tbody.append(tr);
    }
    table.append(tbody);
    clampSel();
    showSel(false);
    fitColumns();
    renderList();
    if (state.editing) reattachEditor();
  }

  // ------------------------------------------------------------------ column widths
  // Description and Note grow to fit their longest text, between their CSS width and whatever
  // the window leaves after the other columns; Note takes any space left over.

  const TEXT_COLS = ["description", "note"];
  const TEXT_KEYS = ["num", "description", "category", "note"]; // the fields that hold free text
  const ROWNUM_W = 44;
  const measure = document.createElement("canvas").getContext("2d");
  const widest = { description: 0, note: 0 }; // widest text seen this session on any tab (px), so
                                              // every tab, checking or savings, gets the same layout

  function fitColumns() {
    const s = state.sheet;
    if (!s || !state.colEls || !TEXT_COLS.every((k) => state.colEls[k])) return;
    const table = $("grid");
    const td = table.querySelector("tbody td");
    const cs = getComputedStyle(td || table); // not cs.font: its line-height part breaks canvas fonts
    measure.font = `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
    const pad = 6 + 6 + 1 + 4; // cell padding, border, a little slack
    for (const key of TEXT_COLS) {
      for (let r = 0; r < rowCount(); r++) {
        const w = measure.measureText(displayValue(r, key)).width + pad;
        if (w > widest[key]) widest[key] = w;
      }
    }
    if (s.carried_from !== null) {
      widest.description = Math.max(widest.description,
                                  measure.measureText("Carried forward from " + s.carried_from).width + pad);
    }
    // each column's base width (w) is its minimum; the other columns stay at their base width
    const min = {};
    let fixed = ROWNUM_W;
    for (const c of columns()) {
      if (TEXT_COLS.includes(c.key)) min[c.key] = c.w;
      else fixed += c.w;
    }
    const room = $("grid-wrap").clientWidth - fixed - 2;
    let d = Math.max(min.description, widest.description);
    let n = Math.max(min.note, widest.note);
    if (d + n > room) {
      const spare = room - min.description - min.note;
      if (spare <= 0) {
        d = min.description; // window too narrow: keep the defaults and scroll sideways
        n = min.note;
      } else { // share the spare room in proportion to how much each column wants
        const wd = d - min.description, wn = n - min.note;
        d = min.description + spare * wd / (wd + wn);
        n = min.note + spare * wn / (wd + wn);
      }
    } else {
      n = room - d; // Note takes the rest of the window
    }
    d = Math.floor(d);
    n = Math.floor(n);
    state.colEls.description.style.width = d + "px";
    state.colEls.note.style.width = n + "px";
    // table-layout: fixed only honours the column widths when the table has a width
    table.style.width = fixed + d + n + "px";
  }

  function fillCell(td, r, c) {
    const isNew = r >= state.sheet.rows.length;
    td.textContent = c.key === "balance" && isNew ? "" : displayValue(r, c.key);
    if (c.key === "description" || c.key === "category" || c.key === "note") td.title = td.textContent; // full text on hover
    if (c.key === "description") {
      const linked = !isNew && !!state.sheet.rows[r].transfer;
      td.classList.toggle("xfer", linked);
      if (linked) td.title += "\n(Transfer: its date and amount are shared with the row in the other account.)";
    }
    td.classList.toggle("unsaved", state.pending.has(pkey(r, c.key))
      || (c.key === "cleared" && !!state.recon && state.recon.marks.has(r)));
    const v = c.key === "balance" ? balanceAt(r) : rawValue(r, c.key);
    td.classList.toggle("neg", (c.key === "amount" || c.key === "balance") && typeof v === "string"
      && shown(v).startsWith("-"));
  }

  function refreshCell(r, ci) {
    const td = state.cells[r] && state.cells[r][ci];
    if (td) fillCell(td, r, columns()[ci]);
    if (narrow.matches && !listQueued) { // once for a burst of cell refreshes, not once per cell
      listQueued = true;
      queueMicrotask(() => { listQueued = false; renderList(); });
    }
  }

  // ------------------------------------------------------------------ narrow screens
  // Below 700px the grid gives way to a list of the same rows. Tapping one opens a form; each
  // save is one commit, as in the grid. Nothing here changes what is stored.

  const narrow = window.matchMedia("screen and (max-width: 700px)");
  let listQueued = false;

  function renderList() {
    const list = $("rows");
    if (!narrow.matches || !state.sheet) return list.replaceChildren();
    const s = state.sheet;
    const items = [];
    if (s.carried_from !== null) {
      const li = el("li", "carried");
      const line = el("div", "line");
      line.append(el("span", "desc", "Carried forward from " + s.carried_from),
                  el("span", "balance", fmtMoney(shown(s.opening))));
      li.append(line);
      items.push(li);
    }
    s.rows.forEach((row, r) => {
      const li = el("li");
      const button = el("button", "tap");
      button.type = "button";
      const amount = shown(rawValue(r, "amount"));
      const top = el("span", "line");
      top.append(el("span", "date", displayValue(r, "date")),
                 el("span", "desc", (row.transfer ? "\u21C4 " : "") + (row.description || "(no description)")),
                 el("span", "amount" + (amount.startsWith("-") ? " neg" : ""), fmtMoney(amount)));
      const sub = el("span", "line sub");
      const ticked = rawValue(r, "cleared");
      sub.append(el("span", "desc", [row.num && "#" + row.num, row.category, row.note].filter(Boolean).join(" \u00B7 ")),
                 el("span", "", ticked ? "x" : ""),
                 el("span", "balance", fmtMoney(shown(row.balance))));
      button.append(top, sub);
      button.classList.toggle("marked", !!state.recon && state.recon.marks.has(r));
      button.addEventListener("click", () => {
        if (state.recon) { state.sel.r = r; toggleCleared(); } else openRowDialog(r);
      });
      li.append(button);
      items.push(li);
    });
    list.replaceChildren(...items);
  }

  // The row form belongs to the sheet as it was when it opened: its saves carry that revision,
  // so if the sheet changes meanwhile they are refused instead of landing on another row.
  let rowForm = null; // { index, rev, row (null for a new row) }

  function openRowDialog(r) {
    if (!state.sheet) return;
    if (readonly()) return flash("Read-only right now.", true);
    if (reconciling()) return;
    const f = $("form-row");
    const isNew = r === null;
    const row = isNew ? null : state.sheet.rows[r];
    rowForm = { index: isNew ? state.sheet.rows.length : r, rev: state.sheet.rev, row };
    f.reset();
    $("row-title").textContent = isNew ? "New row" : "Row " + (r + 1);
    $("row-delete").classList.toggle("hidden", isNew);
    if (row) {
      for (const key of TEXT_KEYS) f[key].value = row[key];
      f.date.value = fmtDate(row.date);
      f.amount.value = shown(row.amount);
      if (debitMode() && row.amount && !row.amount.startsWith("-")) f.amount.value = "+" + row.amount;
      f.cleared.checked = row.cleared;
    }
    $("row-amount-hint").textContent = flipped() ? "A plain number is a charge; use \u2212 for a payment."
      : debitMode() ? "Debit mode: a plain number is money out; type + for a deposit."
      : "Use \u2212 for money out.";
    const options = (items) => items.slice(0, 300).map((text) => option("", text));
    $("list-payees").replaceChildren(...options(state.hints.payees.map((p) => p.description)));
    $("list-categories").replaceChildren(...options(state.hints.categories));
    $("row-error").classList.add("hidden");
    $("dlg-row").showModal();
    (isNew ? f.date : f.description).focus();
  }

  async function submitRow(e) {
    e.preventDefault();
    const f = $("form-row");
    const isNew = rowForm.row === null;
    const r = rowForm.index;
    const btn = $("row-save");
    btn.disabled = true;
    try {
      const text = (input) => input.value.trim().normalize("NFC"); // as the server stores it
      const typed = { date: parseDate(f.date.value), description: text(f.description),
                      category: text(f.category), amount: parseValue("amount", f.amount.value),
                      cleared: f.cleared.checked, num: text(f.num), note: text(f.note) };
      const stored = rowForm.row;
      const fields = {};
      for (const [key, value] of Object.entries(typed)) {
        if (isNew ? value !== blankValue(key) : value !== stored[key]) fields[key] = value;
      }
      if (Object.keys(fields).length) {
        const body = { rev: rowForm.rev, fields };
        rowForm.sent = typed; // the whole row as typed, blanks included
        setSheet(isNew ? await api("POST", sheetPath() + "/rows", Object.assign(body, { index: r }))
                       : await api("PUT", sheetPath() + "/rows/" + r, body));
        Object.keys(fields).forEach((key) => rememberHint(key, fields[key]));
        flash("Saved.");
      }
      $("dlg-row").close();
    } catch (x) {
      await rowFormFailed(x);
    } finally {
      btn.disabled = false;
    }
  }

  async function rowFormFailed(x) {
    if (!x || x.status !== 409) return dialogFailed("row-error", x);
    const form = rowForm;
    let fresh;
    try { fresh = await api("GET", sheetPath()); } catch (e) { return dialogFailed("row-error", e); }
    setSheet(fresh);
    if (rowForm !== form || !$("dlg-row").open) return; // closed, or another row opened, meanwhile
    if (rowForm.row === null) { // a new row goes at the end whatever else changed: keep what was typed
      const last = fresh.rows[fresh.rows.length - 1];
      // the same in every field, blank ones included: then it is this row, already stored
      if (last && rowForm.sent && Object.entries(rowForm.sent).every(([k, v]) => last[k] === v)) {
        $("dlg-row").close(); // it is there already: the earlier Save went through, only its answer was lost
        return flash("Saved.");
      }
      rowForm = { index: fresh.rows.length, rev: fresh.rev, row: null };
      return showDialogError("row-error", "The sheet changed elsewhere and was reloaded. Press Save again.");
    }
    $("dlg-row").close(); // what the form shows is no longer what is stored
    flash("The sheet changed elsewhere and was reloaded; nothing was saved. Open the row again.", true);
  }

  async function deleteRowFromForm() {
    if (!confirmDelete(rowForm.index, rowForm.row)) return;
    try {
      setSheet(await api("DELETE", sheetPath() + "/rows/" + rowForm.index, { rev: rowForm.rev }));
      $("dlg-row").close();
      flash("Deleted.");
    } catch (x) {
      await rowFormFailed(x);
    }
  }

  function clampSel() {
    const n = rowCount(), m = columns().length;
    state.sel.r = Math.max(0, Math.min(state.sel.r, n - 1));
    state.sel.c = Math.max(0, Math.min(state.sel.c, m - 1));
  }

  function showSel(scroll = true) {
    document.querySelectorAll(".grid .sel, .grid .sel-row, .grid .sel-col").forEach((e) => {
      e.classList.remove("sel", "sel-row", "sel-col");
      e.removeAttribute("aria-selected");
    });
    $("grid").removeAttribute("aria-activedescendant");
    if (!state.sheet || !state.cells.length) {
      $("cell-ref").textContent = "";
      $("cell-value").textContent = "";
      return;
    }
    const { r, c } = state.sel;
    const td = state.cells[r][c];
    td.classList.add("sel");
    td.setAttribute("aria-selected", "true");
    $("grid").setAttribute("aria-activedescendant", td.id); // a screen reader follows the selection
    td.parentElement.firstChild.classList.add("sel-row");
    const hdr = $("grid").querySelector(".hdr-letters").children[c + 1];
    if (hdr) hdr.classList.add("sel-col");
    if (scroll) td.scrollIntoView({ block: "nearest", inline: "nearest" });
    const col = columns()[c];
    $("cell-ref").textContent = letter(c) + (r + 1);
    $("cell-value").textContent = col.key === "balance"
      ? shown(balanceAt(r))
      : (col.key === "cleared" ? displayValue(r, "cleared") : editText(r, col.key));
  }

  function moveSel(dr, dc) {
    const from = state.sel.r;
    state.sel.r += dr;
    state.sel.c += dc;
    clampSel();
    showSel();
    if (state.sel.r !== from) flushRow(from);
    else if (dr > 0 && from >= state.sheet.rows.length && pendingRows().includes(from)) {
      // Enter/Down on the last blank row: add it, then continue on the first blank row
      flushRow(from, false, () => { state.sel = { r: state.sheet.rows.length, c: 0 }; showSel(); });
    }
  }

  function renderTabs() {
    const nav = $("tabs");
    nav.replaceChildren();
    const cur = state.sheet;
    for (const a of state.tabs) {
      const group = el("span", "tab-group");
      if (a.years) {
        const label = el("span", "tab-label", a.name + ":");
        label.title = "Double-click (or Enter) for the name and settings";
        label.tabIndex = 0;
        label.setAttribute("role", "button");
        label.addEventListener("dblclick", () => renameAccount(a));
        label.addEventListener("keydown", (e) => {
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); renameAccount(a); }
        });
        group.append(label);
        for (const y of a.years) {
          group.append(tabButton(String(y), a.id, y, cur && cur.account === a.id && cur.year === y));
        }
        const add = el("button", "tab-add", "+");
        add.type = "button";
        add.title = "Add year " + nextYear(a);
        add.addEventListener("click", () => addYear(a));
        add.disabled = readonly();
        group.append(add);
      } else {
        const b = tabButton(a.name, a.id, null, cur && cur.account === a.id);
        b.title = "Double-click (or F2) for the name and settings";
        b.addEventListener("dblclick", () => renameAccount(a));
        b.addEventListener("keydown", (e) => { if (e.key === "F2") { e.preventDefault(); renameAccount(a); } });
        group.append(b);
      }
      nav.append(group);
    }
    const addAcct = el("button", "tab-add tab-add-account", "+ Account");
    addAcct.type = "button";
    addAcct.disabled = readonly();
    addAcct.addEventListener("click", openAccountDialog);
    nav.append(addAcct);
    $("empty").classList.toggle("hidden", state.tabs.length > 0);
  }

  function tabButton(text, account, year, active) {
    const b = el("button", "tab" + (active ? " active" : ""), text);
    b.type = "button";
    b.addEventListener("click", () => openSheet(account, year));
    return b;
  }

  // When the current sheet was last saved: the time of the last commit that touched it.
  function renderModified() {
    const span = $("last-modified");
    const iso = state.sheet && state.sheet.modified;
    if (!iso) {
      span.textContent = "";
      span.title = "";
      return;
    }
    const when = new Date(iso);
    span.textContent = "Last saved " + when.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
    span.title = "Most recent change to this sheet: " + when.toString();
  }

  function renderTitle() {
    const s = state.sheet;
    $("sheet-title").textContent = s ? s.name + (s.year !== null ? " — " + s.year : "") : "";
    $("print-title").textContent = $("sheet-title").textContent; // the heading on paper
    document.title = s ? `Register — ${s.name}${s.year !== null ? " " + s.year : ""}` : "Register";
  }

  // ------------------------------------------------------------------ status, banner, save indicator

  function readonly() { return state.dirty || state.recorderDown; }

  function renderBanner() {
    const b = $("banner");
    let msg = "";
    if (state.recorderDown) msg = "Not saving: the recorder process is not running.";
    else if (state.dirty) {
      msg = "Read-only: the data repository has uncommitted changes. " +
            "Edits are disabled until they are committed or discarded with git.";
    }
    b.textContent = msg;
    b.classList.toggle("hidden", !msg);
    document.body.classList.toggle("readonly", readonly());
    $("btn-insert").disabled = readonly() || !state.sheet;
    $("btn-delete").disabled = readonly() || !state.sheet;
    renderTabs();
    renderSaveStatus();
  }

  function setStatus(dirty, recorderDown) {
    const wasReadonly = readonly();
    const changed = dirty !== state.dirty || recorderDown !== state.recorderDown;
    state.dirty = dirty;
    state.recorderDown = recorderDown;
    if (readonly() && state.editing) parkEdit();
    if (changed) renderBanner();
    if (wasReadonly && !readonly() && state.sheet) reloadSheet();
  }

  async function checkStatus() {
    try {
      const s = await api("GET", "/api/status");
      setStatus(!s.repo_clean, false);
    } catch (e) {
      if (e.status === 503) setStatus(state.dirty, true);
    }
  }

  // ---- helpers shared by the dialogs

  function showDialogError(id, x) {
    const err = $(id);
    err.textContent = typeof x === "string" ? x : (x && x.message) || "Error";
    err.classList.remove("hidden");
  }

  // A dialog's action failed: say so in the dialog; a server refusal may also change the banner.
  function dialogFailed(id, x) {
    showDialogError(id, x);
    if (x && x.status) handleError(x);
  }

  const UNSAVED = "Some edits are not saved yet. Save or discard them first.";
  async function requireSaved() {
    if (!(await saveEverything())) throw UNSAVED;
  }

  function option(text, value) {
    const o = el("option", "", text);
    o.value = value;
    return o;
  }
  const accountOptions = () => state.tabs.map((a) => option(a.name, a.id));
  const colIndex = (key) => columns().findIndex((c) => c.key === key);

  let flashTimer = null;
  let saveState = "";
  function flash(msg, isError) {
    const s = $("save-status");
    s.textContent = msg;
    s.className = "save-status" + (isError ? " error" : "");
    clearTimeout(flashTimer);
    flashTimer = setTimeout(renderSaveStatus, isError ? 6000 : 2500);
  }

  function renderSaveStatus() {
    const s = $("save-status");
    const n = state.pending.size;
    const rows = pendingRows();
    s.className = "save-status";
    document.body.classList.toggle("blocked", state.blocked);
    if (saveState === "saving") s.textContent = "Saving…";
    else if (n && (state.blocked || readonly())) {
      s.textContent = n + " unsaved edit" + (n > 1 ? "s" : "");
      s.classList.add("error");
    } else if (n) {
      s.textContent = "Row " + rows.map((r) => r + 1).join(", ") + " not saved yet (saves when you leave the row)";
    } else s.textContent = saveState === "saved" ? "All changes saved" : "";
    const showActions = n > 0 && saveState !== "saving" && (state.blocked || readonly());
    $("btn-retry").classList.toggle("hidden", !showActions);
    $("btn-discard").classList.toggle("hidden", !showActions);
    $("btn-retry").disabled = readonly();
  }

  function handleError(e) {
    if (e.status === 423 || e.error === "repo_dirty") setStatus(true, state.recorderDown);
    else if (e.status === 503) setStatus(state.dirty, true);
    else if (e.status === 409) {
      reloadSheet();
      flash("The sheet changed elsewhere and was reloaded. Your unsaved edits are kept; " +
            "check them and press Retry.", true);
      return;
    }
    if (e.status !== 423 && e.status !== 503 && e.status !== 401) flash(e.message || "Error", true);
  }

  // ------------------------------------------------------------------ loading sheets

  function sheetPath(s) {
    s = s || state.sheet;
    return `/api/sheet/${encodeURIComponent(s.account)}/${yearPart(s.year)}`;
  }

  async function loadTabs() {
    const t = await api("GET", "/api/tabs");
    state.tabs = t.accounts;
    state.types = t.types;
    setStatus(!t.repo_clean, false);
    renderTabs();
    return t;
  }

  async function openSheet(account, year, keepSel) {
    if (reconciling()) return;
    if (state.editing && !finishEdit(null)) return;
    if (state.pending.size) {
      flushAll();
      await state.queue;
      if (state.pending.size && !confirm("Some edits on this sheet could not be saved. Discard them?")) return;
    }
    state.pending.clear();
    state.blocked = false;
    try {
      const s = await api("GET", sheetPath({ account, year }));
      const same = state.sheet && state.sheet.account === s.account && state.sheet.year === s.year;
      setSheet(s);
      if (!same && !keepSel) {
        // a different sheet: select the first blank row and show the very bottom of the sheet
        state.sel = { r: s.rows.length, c: 0 };
        clampSel();
        showSel(false);
        const wrap = $("grid-wrap");
        wrap.scrollTop = wrap.scrollHeight;
        wrap.scrollLeft = 0;
      }
      history.replaceState(null, "", "#" + s.account + "/" + yearPart(s.year));
    } catch (e) {
      handleError(e);
    }
  }

  function setSheet(s) {
    if (state.recon && state.recon.rev !== s.rev) { // the sheet changed underneath: the ticks no longer line up
      state.recon = null;
      flash("The sheet changed, so reconciling was cancelled. Start it again.", true);
    }
    state.sheet = s;
    renderRecon();
    renderSignButton();
    renderModified();
    renderTitle();
    renderGrid();
    renderTabs();
    $("btn-insert").disabled = readonly();
    $("btn-delete").disabled = readonly();
    if (s.repo_clean !== undefined) setStatus(!s.repo_clean, false);
  }

  async function reloadSheet() {
    if (!state.sheet) return;
    try {
      const s = await api("GET", sheetPath());
      setSheet(s);
    } catch (e) {
      handleError(e);
    }
  }

  // ------------------------------------------------------------------ saving

  function enqueue(fn) {
    state.queue = state.queue.then(fn, fn);
    return state.queue;
  }

  // Cell edits are staged; a row is saved (one commit) when the selection leaves it, the page is
  // hidden or closed, the sheet changes, a row is inserted/deleted, or after IDLE_SAVE_MS.
  let idleTimer = null;

  function pendingRows() {
    return [...new Set([...state.pending.keys()].map((k) => +k.split("|")[0]))].sort((a, b) => a - b);
  }

  function stageCell(r, key, value) {
    if (r >= state.sheet.rows.length && (value === "" || value === false) && !state.pending.has(pkey(r, key))) {
      return; // nothing to add on a blank buffer row
    }
    const row = state.sheet.rows[r];
    const stored = row ? row[key] : blankValue(key);
    if (value === stored) state.pending.delete(pkey(r, key)); // edited back to what is saved
    else state.pending.set(pkey(r, key), value);
    refreshCell(r, colIndex(key));
    if (readonly()) flash("Read-only right now; the edit is kept as unsaved.", true);
    renderSaveStatus();
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => { if (!state.editing) flushAll(); }, IDLE_SAVE_MS);
    if (r !== state.sel.r) flushRow(r);
  }

  function flushAll(keepalive) {
    for (const r of pendingRows()) flushRow(r, keepalive);
  }

  function flushRow(r, keepalive, after) {
    if (!state.sheet || readonly() || state.blocked) return;
    const sheetAtEdit = state.sheet;
    const send = async () => {
      if (state.sheet.account !== sheetAtEdit.account || state.sheet.year !== sheetAtEdit.year) return;
      const fields = {};
      for (const [k, v] of state.pending) {
        const [kr, key] = k.split("|");
        if (+kr === r) fields[key] = v;
      }
      const keys = Object.keys(fields);
      if (!keys.length) return;
      const s = state.sheet;
      const isNew = r >= s.rows.length;
      if (isNew && keys.every((k) => fields[k] === "" || fields[k] === false)) {
        keys.forEach((k) => state.pending.delete(pkey(r, k))); // an emptied new row: nothing to save
        renderGrid();
        renderSaveStatus();
        return;
      }
      saveState = "saving";
      renderSaveStatus();
      state.busy++;
      try {
        const next = isNew
          ? await api("POST", sheetPath() + "/rows", { rev: s.rev, index: s.rows.length, fields }, keepalive)
          : await api("PUT", sheetPath() + "/rows/" + r, { rev: s.rev, fields }, keepalive);
        // drop only what was sent; anything typed into the row meanwhile stays staged
        keys.forEach((k) => { if (state.pending.get(pkey(r, k)) === fields[k]) state.pending.delete(pkey(r, k)); });
        state.blocked = false;
        saveState = "saved";
        setSheet(next);
        if (isNew) snapIntoBuffer();
        if (after) after();
      } catch (e) {
        saveState = "";
        if ([409, 423, 503, 0].includes(e.status)) {
          state.blocked = true; // keep the edits, marked unsaved, until Retry
        } else {
          keys.forEach((k) => state.pending.delete(pkey(r, k))); // rejected outright
          renderGrid();
        }
        handleError(e);
      } finally {
        state.busy--;
      }
      renderSaveStatus();
    };
    // When the page is closing there is no time to wait for the queue: send right away.
    if (keepalive && !state.busy) send();
    else enqueue(send);
  }

  // After a buffer row is saved it moves up to directly below the last entry. If the selection is
  // somewhere in the blank buffer (and nothing is being typed there), put it on the first blank row.
  function snapIntoBuffer() {
    const first = state.sheet.rows.length;
    if (state.editing || state.sel.r <= first || pendingRows().includes(state.sel.r)) return;
    state.sel.r = first;
    showSel();
  }

  function retryPending() {
    state.blocked = false;
    flushAll();
  }

  function discardPending() {
    state.pending.clear();
    state.blocked = false;
    saveState = "";
    renderGrid();
    renderSaveStatus();
  }

  // The page is being hidden or closed: commit any open editor and save everything now.
  function saveBeforeLeaving() {
    if (state.editing) finishEdit(null);
    clearTimeout(idleTimer);
    flushAll(true);
  }

  function rowOp(method, path, body, after) {
    if (readonly()) return flash("Read-only right now.", true);
    if (state.blocked && state.pending.size) return flash("Save or discard the unsaved edits first.", true);
    flushAll();
    saveState = "saving";
    renderSaveStatus();
    enqueue(async () => {
      try {
        const next = await api(method, sheetPath() + path, Object.assign({ rev: state.sheet.rev }, body));
        saveState = "saved";
        setSheet(next);
        if (after) after();
      } catch (e) {
        saveState = "";
        handleError(e);
      }
      renderSaveStatus();
    });
  }

  function insertRow() {
    if (!state.sheet || state.editing || reconciling()) return;
    const r = Math.min(state.sel.r, state.sheet.rows.length);
    rowOp("POST", "/rows", { index: r }, () => { state.sel.r = r; showSel(); });
  }

  function confirmDelete(r, row = state.sheet.rows[r]) {
    const both = row.transfer ? "\n\nThis is a transfer: the row in the other account is deleted too." : "";
    return confirm(`Delete row ${r + 1}?\n\n${rowText(row)}${both}`);
  }

  function deleteRow() {
    if (!state.sheet || state.editing || reconciling()) return;
    const r = state.sel.r;
    if (r >= state.sheet.rows.length) return;
    if (!confirmDelete(r)) return;
    rowOp("DELETE", "/rows/" + r, {});
  }

  // ------------------------------------------------------------------ editing

  function startEdit(initial) {
    if (!state.sheet || state.editing) return;
    const { r, c } = state.sel;
    const col = columns()[c];
    if (col.readonly) return flash("The balance is calculated and cannot be edited."); // hard rule
    if (readonly()) return flash("Read-only right now.", true);
    if (state.recon) {
      if (col.key === "cleared" && (initial === null || initial === " ")) toggleCleared();
      else reconciling();
      return;
    }
    if (col.key === "cleared") {
      if (initial === null || initial === " ") toggleCleared();
      else if (/^[xX]$/.test(initial)) stageCell(r, "cleared", true);
      return;
    }
    const td = state.cells[r][c];
    const input = el("input", "editor");
    input.type = "text";
    input.value = initial === null ? editText(r, col.key) : initial;
    if (col.key === "amount" && debitMode()) {
      // an existing deposit opens as "+2150.00" so confirming it unchanged keeps it positive
      if (initial === null && input.value && !input.value.startsWith("-")) input.value = "+" + input.value;
      const mark = () => td.classList.toggle("debit-entry", debitMode() && !/^\s*[+\-(]/.test(input.value));
      input.addEventListener("input", mark);
      mark();
    }
    input.spellcheck = false;
    input.autocomplete = "off";
    state.editing = { r, c, input };
    if (col.key === "description" || col.key === "category") input.addEventListener("input", showSuggest);
    td.classList.add("editing");
    td.replaceChildren(input);
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  }

  function clearEditorMarks(td) {
    td.classList.remove("editing", "debit-entry");
    hideSuggest();
  }

  // ------------------------------------------------------------------ autocomplete
  // While typing a description or category, a short list of earlier ones appears under the cell.
  // Down/Up pick from it, Enter or Tab take the highlighted one, Esc closes the list. Taking an
  // earlier description also fills in its last amount and category, where those are still blank.

  const suggest = { items: [], active: -1 };
  const SUGGEST_MAX = 8;

  function matches(key, text) {
    const q = text.trim().toLowerCase();
    if (!q) return [];
    const pool = key === "category"
      ? state.hints.categories.map((c) => ({ text: c }))
      : state.hints.payees.map((p) => ({ text: p.description, payee: p }));
    const starts = [], inside = [];
    for (const item of pool) {
      const t = item.text.toLowerCase();
      if (t === q) continue; // already typed in full
      if (t.startsWith(q)) starts.push(item);
      else if (t.includes(q)) inside.push(item);
      if (starts.length >= SUGGEST_MAX) break;
    }
    return starts.concat(inside).slice(0, SUGGEST_MAX);
  }

  function showSuggest() {
    const ed = state.editing;
    const box = $("suggest");
    suggest.items = ed ? matches(columns()[ed.c].key, ed.input.value) : [];
    suggest.active = -1;
    if (!suggest.items.length) return hideSuggest();
    box.replaceChildren(...suggest.items.map((item, i) => {
      const li = el("li", "", item.text);
      li.setAttribute("role", "option");
      if (item.payee && item.payee.amount) li.append(el("span", "hint", fmtMoney(shown(item.payee.amount))));
      li.addEventListener("mousedown", (e) => { e.preventDefault(); takeSuggestion(i); finishEdit(null); });
      return li;
    }));
    const rect = ed.input.getBoundingClientRect();
    box.style.left = rect.left + "px";
    box.style.minWidth = rect.width + "px";
    box.classList.remove("hidden");
    const below = rect.bottom + 2 + box.offsetHeight <= window.innerHeight;
    box.style.top = (below ? rect.bottom + 2 : rect.top - 2 - box.offsetHeight) + "px";
  }

  function hideSuggest() {
    suggest.items = [];
    suggest.active = -1;
    $("suggest").classList.add("hidden");
  }

  function moveSuggest(step) {
    const n = suggest.items.length;
    suggest.active = (suggest.active + step + n + (suggest.active < 0 && step < 0 ? 1 : 0)) % n;
    [...$("suggest").children].forEach((li, i) => li.classList.toggle("active", i === suggest.active));
  }

  function takeSuggestion(i) {
    const ed = state.editing;
    const item = suggest.items[i];
    ed.input.value = item.text;
    ed.fill = item.payee || null;
    hideSuggest();
  }

  // Keep the lists current without asking the server again.
  function rememberHint(key, value) {
    if (!value) return;
    if (key === "category" && !state.hints.categories.includes(value)) state.hints.categories.push(value);
    if (key === "description") {
      const i = state.hints.payees.findIndex((p) => p.description === value);
      const p = i >= 0 ? state.hints.payees.splice(i, 1)[0] : { description: value, amount: "", category: "" };
      state.hints.payees.unshift(p);
    }
  }

  async function loadHints() {
    try { state.hints = await api("GET", "/api/suggestions"); } catch (e) { /* typing works without them */ }
  }

  function reattachEditor() {
    const ed = state.editing;
    const td = state.cells[ed.r] && state.cells[ed.r][ed.c];
    if (!td) { state.editing = null; return; }
    td.classList.add("editing");
    td.replaceChildren(ed.input);
    ed.input.focus();
  }

  // Returns false (and keeps editing) if the value cannot be parsed.
  function finishEdit(move) {
    const ed = state.editing;
    if (!ed) return true;
    const key = columns()[ed.c].key;
    let value;
    try {
      value = parseValue(key, ed.input.value);
    } catch (msg) {
      flash(String(msg), true);
      ed.input.focus();
      return false;
    }
    state.editing = null;
    const td = state.cells[ed.r][ed.c];
    clearEditorMarks(td);
    refreshCell(ed.r, ed.c);
    if (value !== rawValue(ed.r, key)) stageCell(ed.r, key, value);
    $("grid").focus({ preventScroll: true });
    if (ed.fill && ed.fill.description === value) { // an earlier description: its amount and category too
      for (const k of ["amount", "category"]) {
        if (ed.fill[k] && rawValue(ed.r, k) === "") stageCell(ed.r, k, ed.fill[k]);
      }
    }
    rememberHint(key, value);
    if (move) moveSel(move[0], move[1]);
    else showSel(false);
    return true;
  }

  // The repo became read-only mid-edit: keep the typed value as an unsaved edit.
  function parkEdit() {
    const ed = state.editing;
    const key = columns()[ed.c].key;
    let value;
    try { value = parseValue(key, ed.input.value); } catch (msg) { value = undefined; }
    cancelEdit();
    if (value !== undefined && value !== rawValue(ed.r, key)) {
      stageCell(ed.r, key, value);
    }
  }

  function cancelEdit() {
    const ed = state.editing;
    if (!ed) return;
    state.editing = null;
    clearEditorMarks(state.cells[ed.r][ed.c]);
    refreshCell(ed.r, ed.c);
    showSel(false);
    $("grid").focus({ preventScroll: true });
  }

  function toggleCleared() {
    const { r } = state.sel;
    if (!state.recon) return stageCell(r, "cleared", !rawValue(r, "cleared"));
    if (r >= state.sheet.rows.length) return;
    const ticked = !rawValue(r, "cleared");
    if (ticked === state.sheet.rows[r].cleared) state.recon.marks.delete(r);
    else state.recon.marks.set(r, ticked);
    refreshCell(r, colIndex("cleared"));
    renderRecon();
  }

  function clearCell() {
    const { r, c } = state.sel;
    const col = columns()[c];
    if (col.readonly || (r >= state.sheet.rows.length && !state.pending.has(pkey(r, col.key)))) return;
    if (readonly()) return flash("Read-only right now.", true);
    if (reconciling()) return;
    const empty = blankValue(col.key);
    if (rawValue(r, col.key) !== empty) stageCell(r, col.key, empty);
  }

  // ------------------------------------------------------------------ input handling

  // a dialog is open, or the row list has replaced the grid (narrow screens)
  const gridHidden = () => !!document.querySelector("dialog[open]") || narrow.matches;

  function onKeyDown(e) {
    if (e.key === "F1") { // help, wherever the keyboard is; never the browser's own help page
      e.preventDefault();
      if (!document.querySelector("dialog[open]")) $("dlg-help").showModal();
      return;
    }
    if (gridHidden() || !state.sheet) return;
    const ed = state.editing;
    const ctrl = e.ctrlKey || e.metaKey;
    // Help: "?" too, except on a text cell, where it is something to type. (F1 is handled first.)
    if (!ed && !ctrl && !e.altKey && e.key === "?" && !TEXT_KEYS.includes(columns()[state.sel.c].key)) {
      e.preventDefault();
      return $("dlg-help").showModal();
    }
    if (ctrl && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "f") {
      e.preventDefault();
      if (!ed || finishEdit(null)) openSearchDialog();
      return;
    }
    if (ed) {
      if (suggest.items.length) {
        if (e.key === "ArrowDown" || e.key === "ArrowUp") {
          e.preventDefault();
          return moveSuggest(e.key === "ArrowDown" ? 1 : -1);
        }
        if (e.key === "Escape") { e.preventDefault(); return hideSuggest(); }
        if ((e.key === "Enter" || e.key === "Tab") && suggest.active >= 0) takeSuggestion(suggest.active);
      }
      if (e.key === "Enter") { e.preventDefault(); finishEdit([e.shiftKey ? -1 : 1, 0]); }
      else if (e.key === "Tab") { e.preventDefault(); finishEdit([0, e.shiftKey ? -1 : 1]); }
      else if (e.key === "Escape") { e.preventDefault(); cancelEdit(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); finishEdit([-1, 0]); }
      else if (e.key === "ArrowDown") { e.preventDefault(); finishEdit([1, 0]); }
      return;
    }
    if (e.target && (e.target.tagName === "INPUT" || e.target.tagName === "SELECT")) return;
    const grid = $("grid");
    const inGrid = e.target === document.body || grid.contains(e.target);
    if (e.key === "F6") { // round: the grid, the buttons above it, the sheet tabs below it. Tab walks each group
      e.preventDefault();
      const first = document.querySelector(".toolbar button:not(:disabled):not(.hidden)");
      const tab = $("tabs").querySelector(".tab.active") || $("tabs").querySelector("button:not(:disabled)");
      const next = inGrid ? first : $("tabs").contains(e.target) ? null : tab;
      return (next || grid).focus();
    }
    if (!inGrid) { // on a button, a tab or a link: the keys are theirs; Esc goes back to the grid
      if (e.key === "Escape") grid.focus();
      return;
    }
    const col = columns()[state.sel.c];
    switch (e.key) {
      case "ArrowUp": e.preventDefault(); return ctrl ? moveSel(-1e9, 0) : moveSel(-1, 0);
      case "ArrowDown": e.preventDefault(); return ctrl ? moveSel(1e9, 0) : moveSel(1, 0);
      case "ArrowLeft": e.preventDefault(); return moveSel(0, -1);
      case "ArrowRight": e.preventDefault(); return moveSel(0, 1);
      case "PageUp": e.preventDefault(); return moveSel(-20, 0);
      case "PageDown": e.preventDefault(); return moveSel(20, 0);
      case "Home": e.preventDefault(); return ctrl ? moveSel(-1e9, -1e9) : moveSel(0, -1e9);
      case "End": e.preventDefault(); return ctrl ? moveSel(1e9, 1e9) : moveSel(0, 1e9);
      case "Tab": e.preventDefault(); return moveSel(0, e.shiftKey ? -1 : 1);
      case "Enter":
        e.preventDefault();
        if (ctrl) return insertRow();
        return moveSel(e.shiftKey ? -1 : 1, 0);
      case "F2": e.preventDefault(); return startEdit(null);
      case "Delete":
      case "Backspace":
        e.preventDefault();
        if (ctrl && e.shiftKey && e.key === "Delete") return deleteRow();
        return clearCell();
      case " ":
        if (col.key === "cleared") { e.preventDefault(); return startEdit(" "); }
        break;
    }
    if (e.key.length === 1 && !ctrl && !e.altKey) {
      e.preventDefault();
      startEdit(e.key);
    }
  }

  function cellFromEvent(e) {
    const td = e.target.closest("td[data-r]");
    return td ? { r: +td.dataset.r, c: +td.dataset.c } : null;
  }

  function onGridMouseDown(e) {
    if (e.target.classList.contains("editor")) return;
    const pos = cellFromEvent(e);
    if (!pos) return;
    if (state.editing && !finishEdit(null)) { e.preventDefault(); return; }
    const from = state.sel.r;
    state.sel = pos;
    showSel(false);
    $("grid").focus({ preventScroll: true });
    e.preventDefault(); // no text selection by dragging; the cell is what is selected
    if (state.recon) { // reconciling: a click on X ticks it (a double-click counts once)
      if (columns()[pos.c].key === "cleared" && e.button === 0 && e.detail % 2 === 1) toggleCleared();
      return;
    }
    if (pos.r !== from) flushRow(from);
  }

  function onGridDblClick(e) {
    const pos = cellFromEvent(e);
    if (!pos || state.editing) return;
    if (state.recon && columns()[pos.c].key === "cleared") return; // the click ticked it
    startEdit(null);
  }

  function onCopy(e) {
    if (state.editing || !state.sheet || document.activeElement.tagName === "INPUT" || gridHidden()) return;
    if (window.getSelection && String(window.getSelection())) return; // normal text selection
    const { r, c } = state.sel;
    const key = columns()[c].key;
    let text;
    if (key === "balance") text = shown(balanceAt(r));
    else if (key === "amount") text = shown(rawValue(r, key));
    else text = displayValue(r, key);
    e.clipboardData.setData("text/plain", text);
    e.preventDefault();
  }

  function onPaste(e) {
    if (state.editing || !state.sheet || document.activeElement.tagName === "INPUT" || gridHidden()) return;
    e.preventDefault();
    const col = columns()[state.sel.c];
    if (col.readonly) return; // pasting into balance does nothing
    if (readonly()) return flash("Read-only right now.", true);
    if (reconciling()) return;
    const text = (e.clipboardData.getData("text/plain") || "").split(/\r?\n/)[0].split("\t")[0];
    try {
      // a pasted amount is taken as written (what Copy gives): debit mode is for typing
      const value = col.key === "cleared" ? /^\s*[xX]\s*$/.test(text)
        : col.key === "amount" ? shown(parseAmount(text, false)) : parseValue(col.key, text);
      if (value !== rawValue(state.sel.r, col.key)) stageCell(state.sel.r, col.key, value);
    } catch (msg) {
      flash(String(msg), true);
    }
  }

  // ------------------------------------------------------------------ tabs and accounts

  async function addYear(a) {
    if (readonly()) return;
    const next = nextYear(a);
    if (!confirm(`Add year ${next} to ${a.name}?`)) return;
    try {
      const res = await api("POST", `/api/accounts/${encodeURIComponent(a.id)}/years`, {});
      state.tabs = res.accounts;
      renderTabs();
      openSheet(a.id, res.year);
    } catch (e) {
      handleError(e);
    }
  }

  function renameAccount(a) {
    if (readonly()) return flash("Read-only right now.", true);
    if (reconciling()) return;
    const f = $("form-settings");
    f.reset();
    f.name.value = a.name;
    f.owed.checked = !!a.owed_positive;
    f.dataset.account = a.id;
    $("settings-error").classList.add("hidden");
    $("dlg-settings").showModal();
    f.name.select();
  }

  async function submitSettings(e) {
    e.preventDefault();
    const f = $("form-settings");
    const id = f.dataset.account;
    try {
      const name = f.name.value.trim();
      if (!name) throw "Enter a name.";
      await requireSaved();
      const res = await api("PATCH", `/api/accounts/${encodeURIComponent(id)}`, { name, owed_positive: f.owed.checked });
      $("dlg-settings").close();
      state.tabs = res.accounts;
      renderTabs();
      if (state.sheet && state.sheet.account === id) reloadSheet();
    } catch (x) {
      dialogFailed("settings-error", x);
    }
  }

  function accountTypeChanged() {
    const f = $("form-account");
    const type = typeInfo(f.type.value);
    $("year-label").classList.toggle("hidden", !type.yearly);
    $("opening-text").textContent = type.owed_positive ? "Amount owed now" : "Opening balance";
  }

  async function openAccountDialog() {
    if (readonly()) return;
    if (!state.types.length) { // the first load of the tabs failed: the types came with it
      try { await loadTabs(); } catch (e) { return handleError(e); }
      if ($("dlg-account").open) return; // clicked twice while that was loading
    }
    const f = $("form-account");
    f.type.replaceChildren(...state.types.map((t) =>
      option(`${t.label} (${t.yearly ? "one tab per year" : "single tab"})`, t.id)));
    f.reset();
    f.year.value = new Date().getFullYear();
    accountTypeChanged();
    $("account-error").classList.add("hidden");
    $("dlg-account").showModal();
    f.name.focus();
  }

  async function submitAccount(e) {
    e.preventDefault();
    const f = $("form-account");
    const err = $("account-error");
    const body = { name: f.name.value.trim(), type: f.type.value };
    try {
      // what is owed on a card or loan is stored as a negative balance
      body.opening_balance = shown(parseAmount(f.opening.value), !!typeInfo(body.type).owed_positive);
      if (typeInfo(body.type).yearly) {
        const y = +f.year.value;
        if (!/^\d{4}$/.test(f.year.value.trim()) || y < 1900 || y > 2199) throw "Enter a four-digit year.";
        body.year = y;
      }
      const res = await api("POST", "/api/accounts", body);
      $("dlg-account").close();
      state.tabs = res.accounts;
      renderTabs();
      const a = res.accounts.find((x) => x.id === res.account);
      openSheet(a.id, a.years ? a.years[0] : null);
    } catch (x) {
      dialogFailed("account-error", x);
    }
  }

  // ------------------------------------------------------------------ history and undo

  const FIELD_NAMES = { num: "Num", date: "Date", description: "Description", category: "Category", amount: "Amount",
                        cleared: "X", note: "Note", transfer: "Transfer link", bank_id: "Bank id" };

  function fieldText(key, value) {
    if (key === "amount") return value === "" ? "(blank)" : fmtMoney(shown(value));
    if (key === "date") return value === "" ? "(blank)" : fmtDate(value);
    if (key === "cleared") return value ? "x" : "(blank)";
    return value === "" ? "(blank)" : value;
  }

  const rowText = (row) => [fmtDate(row.date), row.description, row.amount === "" ? "" : fmtMoney(shown(row.amount))]
    .filter(Boolean).join("  \u00B7  ") || "(blank row)";

  function changeLine(ch) {
    const li = el("li");
    if (ch.kind === "added") li.textContent = `Row ${ch.row} added: ${rowText(ch.after)}`;
    else if (ch.kind === "deleted") li.textContent = `Row ${ch.row} deleted: ${rowText(ch.before)}`;
    else {
      li.append(`Row ${ch.row}: `);
      Object.keys(FIELD_NAMES).filter((k) => ch.before[k] !== ch.after[k]).forEach((k, i) => {
        if (i) li.append("; ");
        li.append(FIELD_NAMES[k] + " ", el("span", "old", fieldText(k, ch.before[k])), " \u2192 " + fieldText(k, ch.after[k]));
      });
    }
    return li;
  }

  function historyEntry(entry) {
    const li = el("li");
    const when = new Date(entry.date);
    const time = el("time", "", when.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }));
    time.dateTime = entry.date;
    li.append(time, el("span", "what", entry.summary.charAt(0).toUpperCase() + entry.summary.slice(1)));
    if (entry.undo) {
      const b = el("button", "", "Undo");
      b.type = "button";
      b.addEventListener("click", () => undoChange(entry, b));
      li.append(b);
    } else li.append(el("span"));
    if (entry.changes.length) {
      const list = el("ul");
      list.append(...entry.changes.map(changeLine));
      if (entry.more) list.append(el("li", "why", `and ${entry.more} more rows`));
      li.append(list);
    }
    if (!entry.undo && entry.why) li.append(el("span", "why", "Cannot be undone here: " + entry.why + "."));
    return li;
  }

  async function loadHistory() {
    $("history-error").classList.add("hidden");
    try {
      const res = await api("GET", sheetPath() + "/history");
      $("history-list").replaceChildren(...res.entries.map(historyEntry));
      $("history-more").textContent = res.more ? "Older changes are not listed here; `register.pyz history` shows them all." : "";
    } catch (x) {
      dialogFailed("history-error", x);
    }
  }

  async function openHistoryDialog() {
    if (!state.sheet || reconciling()) return;
    if (!(await saveEverything())) return flash(UNSAVED, true);
    $("history-title").textContent = "History of " + $("sheet-title").textContent;
    $("history-list").replaceChildren();
    $("dlg-history").showModal();
    loadHistory();
  }

  async function undoChange(entry, button) {
    if (readonly()) return showDialogError("history-error", "Read-only right now.");
    button.disabled = true;
    try {
      const s = await api("POST", sheetPath() + "/undo", { rev: state.sheet.rev, commit: entry.commit });
      setSheet(s);
      showSel(false);
      await loadHistory();
      flash("Undone.");
    } catch (x) {
      showDialogError("history-error", x);
      if (x && x.status && x.status !== 422) handleError(x);
      button.disabled = false;
    }
  }

  // ------------------------------------------------------------------ reports
  // One year's income and spending. The month chart is SVG drawn here by hand; the two rankings
  // are plain bars. Every number shown on hover is also in the table or printed beside its bar.

  const SVG = "http://www.w3.org/2000/svg";
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const RANKED_MAX = 12;

  function svgEl(tag, attrs, text) {
    const e = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
    if (text !== undefined) e.textContent = text;
    return e;
  }

  // The axis top: the smallest of 1, 2, 2.5, 5 x a power of ten that splits into 4 clean steps.
  function axisTop(max) {
    if (max <= 0) return 4;
    const power = Math.pow(10, Math.floor(Math.log10(max / 4)));
    for (const m of [1, 2, 2.5, 5, 10]) if (m * power * 4 >= max) return m * power * 4;
    return max;
  }

  const axisLabel = (v, top) => (top >= 10000 ? (v / 1000) + "K" : v.toLocaleString());

  function showTip(target, value, label) {
    const tip = $("tip");
    tip.replaceChildren(el("b", "", value), document.createTextNode(label));
    tip.classList.remove("hidden");
    const r = target.getBoundingClientRect();
    const left = Math.min(Math.max(4, r.left + r.width / 2 - tip.offsetWidth / 2), window.innerWidth - tip.offsetWidth - 4);
    tip.style.left = left + "px";
    tip.style.top = Math.max(4, r.top - tip.offsetHeight - 6) + "px";
  }
  const hideTip = () => $("tip").classList.add("hidden");

  function drawMonths(months) {
    const W = 840, H = 230, left = 52, right = 8, top = 10, bottom = 24;
    const plotW = W - left - right, plotH = H - top - bottom;
    const svg = $("report-months");
    svg.replaceChildren();
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    const peak = Math.max(...months.map((m) => Math.max(cents(m.income), cents(m.spending)))) / 100;
    const axis = axisTop(peak);
    const y = (v) => top + plotH - (v / axis) * plotH;
    for (let i = 0; i <= 4; i++) { // recessive hairlines, clean numbers
      const v = axis * i / 4;
      svg.append(svgEl("line", { x1: left, x2: W - right, y1: y(v), y2: y(v), class: i ? "grid-line" : "axis" }),
                 svgEl("text", { x: left - 8, y: y(v) + 4, "text-anchor": "end" }, axisLabel(v, axis)));
    }
    const band = plotW / 12, barW = Math.min(16, (band - 14) / 2), gap = 2;
    months.forEach((m, i) => {
      const mid = left + band * i + band / 2;
      svg.append(svgEl("text", { x: mid, y: H - 7, "text-anchor": "middle" }, MONTHS[i]));
      [["income", "s1", "Income"], ["spending", "s2", "Spending"]].forEach(([key, cls, name], k) => {
        const value = cents(m[key]) / 100;
        if (!value) return;
        const x = mid - barW - gap / 2 + k * (barW + gap), yTop = y(value), base = y(0);
        const r = Math.min(4, base - yTop, barW / 2); // rounded at the data end, square on the baseline
        const bar = svgEl("path", {
          d: `M${x},${base}V${yTop + r}Q${x},${yTop} ${x + r},${yTop}H${x + barW - r}Q${x + barW},${yTop} ${x + barW},${yTop + r}V${base}Z`,
          class: "bar " + cls, tabindex: "0", role: "img",
          "aria-label": `${name}, ${MONTHS[i]}: ${fmtMoney(m[key])}`,
        });
        const show = () => showTip(bar, fmtMoney(m[key]), `${name}, ${MONTHS[i]}`);
        // the hover area is the bar's whole slot, top to bottom, not only its painted pixels
        const hit = svgEl("rect", { x: x - gap / 2, y: top, width: barW + gap, height: plotH, class: "hit" });
        for (const target of [bar, hit]) {
          target.addEventListener("pointerenter", show);
          target.addEventListener("pointerleave", hideTip);
        }
        bar.addEventListener("focus", show);
        bar.addEventListener("blur", hideTip);
        svg.append(hit, bar);
      });
    });
  }

  function drawRanked(id, items, blank) {
    const list = $(id);
    const top = items.slice(0, RANKED_MAX);
    const rest = items.slice(RANKED_MAX);
    if (rest.length) { // the tail folds into one row, never more colours or an endless list
      const total = rest.reduce((sum, item) => sum + cents(item.total), 0);
      top.push({ name: `Other (${rest.length} more)`, total: fromCents(total), count: rest.reduce((n, item) => n + item.count, 0) });
    }
    if (!top.length) return list.replaceChildren(el("dt", "report-empty", "Nothing spent."), el("dd"));
    const peak = Math.max(...top.map((item) => cents(item.total)));
    list.replaceChildren(...top.flatMap((item) => {
      const name = el("dt", "", item.name || blank);
      name.title = item.name || blank;
      const bar = el("span", "bar s1");
      bar.style.width = `calc((100% - 150px) * ${peak ? cents(item.total) / peak : 0})`;
      const row = el("dd");
      row.append(bar, el("span", "amount", fmtMoney(item.total)),
                 el("span", "count", item.count === 1 ? "1 row" : item.count + " rows"));
      return [name, row];
    }));
  }

  async function loadReport() {
    const body = {};
    if ($("report-year").value) body.year = +$("report-year").value;
    if ($("report-account").value) body.account = $("report-account").value;
    $("report-error").classList.add("hidden");
    $("report-body").classList.add("stale"); // keep the last render in place while the new one loads
    try {
      const res = await api("POST", "/api/report", body);
      $("report-year").replaceChildren(...res.years.map((y) => option(String(y), y)));
      $("report-year").value = res.year;
      $("report-income").textContent = fmtMoney(res.income);
      $("report-spending").textContent = fmtMoney(res.spending);
      $("report-net").textContent = fmtMoney(res.net);
      drawMonths(res.months);
      $("report-month-rows").replaceChildren(...res.months.map((m, i) => {
        const tr = el("tr");
        tr.append(el("td", "", MONTHS[i]), el("td", "", fmtMoney(m.income)), el("td", "", fmtMoney(m.spending)),
                  el("td", "", fmtMoney(m.net)));
        return tr;
      }));
      drawRanked("report-categories", res.categories, "(no category)");
      drawRanked("report-payees", res.payees, "(no description)");
    } catch (x) {
      dialogFailed("report-error", x);
    } finally {
      $("report-body").classList.remove("stale");
    }
  }

  async function openReportsDialog() {
    if (reconciling()) return;
    if (!(await saveEverything())) return flash(UNSAVED, true);
    $("report-account").replaceChildren(option("All accounts", ""), ...accountOptions());
    $("report-year").replaceChildren();
    $("dlg-reports").showModal();
    loadReport();
  }

  // ------------------------------------------------------------------ recurring transactions
  // Stored templates. Nothing is entered on its own: the button shows how many are due, and
  // they become rows only when Enter is pressed.

  const EVERY_TEXT = { week: "week", "2weeks": "two weeks", month: "month", quarter: "three months", year: "year" };

  function setRecurring(res) {
    state.recurring = res;
    const b = $("btn-recurring");
    b.textContent = res.due ? `Recurring (${res.due} due)\u2026` : "Recurring\u2026";
    b.classList.toggle("due", res.due > 0);
    if (!$("dlg-recurring").open) return;
    $("recurring-rows").replaceChildren(...res.recurring.map(recurringRow));
    $("recurring-list").classList.toggle("hidden", !res.recurring.length);
    $("recurring-none").classList.toggle("hidden", res.recurring.length > 0);
    const all = $("recurring-enter");
    all.classList.toggle("hidden", !res.due);
    all.textContent = res.due === 1 ? "Enter the 1 due transaction" : `Enter all ${res.due} due transactions`;
  }

  async function loadRecurring() {
    try { setRecurring(await api("GET", "/api/recurring")); } catch (e) { /* the button just shows no count */ }
  }

  function recurringRow(t) {
    const tr = el("tr");
    const actions = el("td", "actions");
    const button = (label, title, run) => {
      const b = el("button", "", label);
      b.type = "button";
      b.title = title;
      b.addEventListener("click", run);
      actions.append(b);
    };
    if (t.due) button("Enter", `Add ${t.due} row(s) to ${t.name}`, () => recurringOp("POST", "/api/recurring/enter", { ids: [t.id] }, true));
    if (t.next) button("Skip", "Pass over " + fmtDate(t.next) + " without entering it",
                       () => recurringOp("POST", `/api/recurring/${t.id}/skip`, { count: t.count }));
    button("Delete", "Stop this recurring transaction (rows already entered stay)", () => {
      if (confirm(`Delete the recurring transaction "${t.description}"?\n\nRows already entered stay.`)) {
        recurringOp("DELETE", "/api/recurring/" + t.id, {});
      }
    });
    const seen = shown(t.amount, flipsSign(t.account));
    tr.append(el("td", "", t.description), el("td", "", t.name),
              el("td", "num" + (seen.startsWith("-") ? " neg" : ""), fmtMoney(seen)),
              el("td", "", EVERY_TEXT[t.every] || t.every), el("td", "", t.next ? fmtDate(t.next) : "ended"),
              el("td", "", t.due ? String(t.due) : ""), actions);
    return tr;
  }

  async function recurringOp(method, path, body, entersRows) {
    $("recurring-error").classList.add("hidden");
    const buttons = [...document.querySelectorAll("#recurring-rows button, #recurring-enter")];
    buttons.forEach((b) => { b.disabled = true; }); // one request at a time: a second click would be refused
    try {
      if (entersRows) await requireSaved();
      const res = await api(method, path, body);
      setRecurring(res);
      if (entersRows) {
        await reloadSheet();
        flash(res.entered === 1 ? "Entered 1 recurring transaction." : `Entered ${res.entered} recurring transactions.`);
      }
    } catch (x) {
      if (x && x.status === 409) { // already skipped or entered in another window
        showDialogError("recurring-error", x);
        return loadRecurring();
      }
      dialogFailed("recurring-error", x);
    } finally {
      buttons.forEach((b) => { b.disabled = false; }); // (those still in the list; a reload makes new ones)
    }
  }

  function openRecurringDialog() {
    if (reconciling()) return;
    const f = $("form-recurring");
    f.reset();
    f.account.replaceChildren(...accountOptions());
    if (state.sheet) f.account.value = state.sheet.account;
    recurringAmountHint();
    $("recurring-error").classList.add("hidden");
    $("dlg-recurring").showModal();
    setRecurring(state.recurring);
    loadRecurring();
  }

  function recurringAmountHint() {
    const f = $("form-recurring");
    f.amount.placeholder = flipsSign(f.account.value) ? "84.12 is a charge" : "-84.12 is money out";
  }

  async function submitRecurring(e) {
    e.preventDefault();
    const f = $("form-recurring");
    try {
      const body = { account: f.account.value, description: f.description.value.trim(),
                     category: f.category.value.trim(),
                     amount: shown(parseAmount(f.amount.value, false), flipsSign(f.account.value)),
                     every: f.every.value, start: parseDate(f.start.value), note: f.note.value.trim() };
      if (!body.amount) throw "Enter the amount.";
      if (!body.start) throw "Enter the first date.";
      $("recurring-error").classList.add("hidden");
      setRecurring(await api("POST", "/api/recurring", body));
      const account = f.account.value;
      f.reset();
      f.account.value = account;
      recurringAmountHint();
      f.description.focus();
    } catch (x) {
      dialogFailed("recurring-error", x);
    }
  }

  // ------------------------------------------------------------------ reconcile
  // Checking a sheet against a statement: the X ticks are held here, not saved row by row, and
  // written in one commit by Finish. Everything else in the grid is locked meanwhile.

  const cents = (s) => (s ? Math.round(parseFloat(s) * 100) : 0); // amounts fit a double exactly in cents
  const fromCents = (n) => (n < 0 ? "-" : "") + Math.floor(Math.abs(n) / 100) + "." + String(Math.abs(n) % 100).padStart(2, "0");

  const centsText = (n) => fmtMoney(shown(fromCents(n))); // as this account displays amounts

  function reconciling() {
    if (state.recon) flash("Finish or cancel reconciling first.", true);
    return !!state.recon;
  }

  function openReconcileDialog() {
    if (!state.sheet || state.recon) return;
    if (readonly()) return flash("Read-only right now.", true);
    $("form-reconcile").reset();
    $("reconcile-error").classList.add("hidden");
    $("dlg-reconcile").showModal();
    $("form-reconcile").balance.focus();
  }

  async function startReconcile(e) {
    e.preventDefault();
    try {
      const statement = parseAmount($("form-reconcile").balance.value, false);
      if (statement === "") throw "Enter the statement's ending balance.";
      await requireSaved();
      state.recon = { statement: cents(shown(statement)), rev: state.sheet.rev, marks: new Map() };
      $("dlg-reconcile").close();
      renderRecon();
    } catch (x) {
      showDialogError("reconcile-error", x);
    }
  }

  function reconDifference() {
    let cleared = cents(state.sheet.cleared_before);
    state.sheet.rows.forEach((row, r) => { if (rawValue(r, "cleared")) cleared += cents(row.amount); });
    return { cleared, diff: state.recon.statement - cleared };
  }

  function renderRecon() {
    const on = !!state.recon && !!state.sheet;
    $("reconbar").classList.toggle("hidden", !on);
    $("btn-reconcile").disabled = on;
    if (!on) return;
    const { cleared, diff } = reconDifference();
    $("recon-statement").textContent = centsText(state.recon.statement);
    $("recon-cleared").textContent = centsText(cleared);
    $("recon-diff").textContent = centsText(diff);
    $("recon-diff").className = diff === 0 ? "zero" : "off";
  }

  function cancelReconcile() {
    const rows = [...state.recon.marks.keys()];
    state.recon = null;
    rows.forEach((r) => refreshCell(r, colIndex("cleared")));
    renderRecon();
  }

  async function finishReconcile() {
    const { diff } = reconDifference();
    const marks = state.recon.marks;
    if (diff !== 0 && !confirm(`The difference is ${centsText(diff)}, not zero. Save the ticks anyway?`)) return;
    if (!marks.size) { cancelReconcile(); return flash(diff === 0 ? "Already reconciled: nothing to save." : "Nothing was ticked."); }
    const body = { rev: state.recon.rev, cleared: [], uncleared: [] };
    marks.forEach((ticked, r) => (ticked ? body.cleared : body.uncleared).push(r));
    $("recon-finish").disabled = true;
    try {
      const s = await api("POST", sheetPath() + "/reconcile", body);
      state.recon = null;
      setSheet(s);
      showSel(false);
      flash(diff === 0 ? "Reconciled." : "Ticks saved; the difference is " + centsText(diff) + ".");
    } catch (x) {
      handleError(x);
    } finally {
      $("recon-finish").disabled = false;
    }
  }

  // ------------------------------------------------------------------ search

  function openSearchDialog() {
    if (!state.sheet) return;
    $("search-error").classList.add("hidden");
    $("dlg-search").showModal();
    $("form-search").text.select();
  }

  async function submitSearch(e) {
    e.preventDefault();
    const f = $("form-search");
    const btn = $("search-submit");
    $("search-error").classList.add("hidden");
    btn.disabled = true;
    try {
      const size = (text) => parseAmount(text, false).replace(/^-/, "");
      const body = { text: f.text.value.trim(), amount_min: size(f.low.value), amount_max: size(f.high.value),
                     date_from: parseDate(f.start.value), date_to: parseDate(f.end.value) };
      if (f.scope.value === "one") body.account = state.sheet.account;
      await requireSaved();
      const res = await api("POST", "/api/search", body);
      $("search-count").textContent = res.results.length === 0 ? "Nothing found."
        : res.more ? `The first ${res.results.length} rows; narrow the search to see the rest.`
        : res.results.length === 1 ? "1 row" : res.results.length + " rows";
      $("search-rows").replaceChildren(...res.results.map(searchRow));
      $("search-results").classList.toggle("hidden", !res.results.length);
    } catch (x) {
      dialogFailed("search-error", x);
    } finally {
      btn.disabled = false;
    }
  }

  function searchRow(hit) {
    const tr = el("tr");
    tr.tabIndex = 0;
    tr.title = "Go to this row";
    const where = hit.name + (hit.year === null ? "" : " " + hit.year) + ", row " + (hit.index + 1);
    const seen = shown(hit.amount, flipsSign(hit.account));
    const amount = el("td", "num" + (seen.startsWith("-") ? " neg" : ""), fmtMoney(seen));
    tr.append(el("td", "", fmtDate(hit.date)), el("td", "", where), el("td", "", hit.description),
              el("td", "", hit.category), amount, el("td", "", hit.note));
    const go = async () => {
      $("dlg-search").close();
      await openSheet(hit.account, hit.year, true);
      if (!state.sheet || state.sheet.account !== hit.account || state.sheet.year !== hit.year) return;
      state.sel = { r: hit.index, c: colIndex("description") };
      clampSel();
      showSel();
    };
    tr.addEventListener("click", go);
    tr.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } });
    return tr;
  }

  // ------------------------------------------------------------------ transfer

  function openTransferDialog() {
    if (readonly()) return flash("Read-only right now.", true);
    if (reconciling()) return;
    if (state.tabs.length < 2) return flash("A transfer needs two accounts.", true);
    const f = $("form-transfer");
    f.reset();
    for (const sel of [f.source, f.target]) {
      sel.replaceChildren(...accountOptions());
    }
    const here = state.sheet ? state.sheet.account : state.tabs[0].id;
    f.source.value = here;
    f.target.value = state.tabs.find((a) => a.id !== here).id;
    f.date.value = fmtDate(new Date().toLocaleDateString("sv")); // today, local time, as ISO
    // the link id is made here, once per opening: pressing the button again after "cannot reach
    // the server" sends the same transfer, which is then not entered twice
    f.dataset.id = [...crypto.getRandomValues(new Uint8Array(8))].map((b) => b.toString(16).padStart(2, "0")).join("");
    $("transfer-error").classList.add("hidden");
    $("dlg-transfer").showModal();
    f.amount.focus();
  }

  async function submitTransfer(e) {
    e.preventDefault();
    const f = $("form-transfer");
    const btn = $("transfer-submit");
    $("transfer-error").classList.add("hidden");
    btn.disabled = true;
    try {
      if (f.source.value === f.target.value) throw "Choose two different accounts.";
      const body = { from_account: f.source.value, to_account: f.target.value,
                     date: parseDate(f.date.value), amount: parseAmount(f.amount.value, false),
                     description: f.description.value.trim(), note: f.note.value.trim(), id: f.dataset.id };
      if (!body.date) throw "Enter the date.";
      if (!body.amount || body.amount.startsWith("-") || body.amount === "0.00") throw "Enter an amount above zero.";
      await requireSaved();
      await api("POST", "/api/transfers", body);
      $("dlg-transfer").close();
      await reloadSheet();
      const name = (id) => state.tabs.find((a) => a.id === id).name;
      flash(`Transfer added to ${name(body.from_account)} and ${name(body.to_account)}.`);
    } catch (x) {
      dialogFailed("transfer-error", x);
      if (x && x.status === 422) reloadSheet(); // an earlier try did go through: show what is there
    } finally {
      btn.disabled = false;
    }
  }

  // ------------------------------------------------------------------ export

  function openExportDialog() {
    const f = $("form-export");
    f.reset();
    $("export-error").classList.add("hidden");
    $("dlg-export").showModal();
    f.password.focus();
  }

  async function submitExport(e) {
    e.preventDefault();
    const f = $("form-export");
    const err = $("export-error");
    const btn = $("export-submit");
    err.classList.add("hidden");
    btn.disabled = true;
    try {
      // edits still being saved belong in the export
      if (!(await saveEverything())) throw "Some edits are not saved yet. Save or discard them, then export.";
      const res = await fetch("/api/export", {
        method: "POST", credentials: "same-origin",
        headers: { "X-CSRF-Token": CSRF, "Content-Type": "application/json" },
        body: JSON.stringify({ password: f.password.value }),
      });
      if (res.status === 401) { location.href = "/login"; return; }
      if (!res.ok) {
        let data = null;
        try { data = await res.json(); } catch (x) { /* no JSON body */ }
        throw (data && data.message) || res.statusText || "Export failed.";
      }
      const disp = res.headers.get("Content-Disposition") || "";
      const name = (/filename="([^"]+)"/.exec(disp) || [])[1] || "register-export.zip";
      const url = URL.createObjectURL(await res.blob());
      const a = el("a");
      a.href = url;
      a.download = name;
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      f.reset();
      $("dlg-export").close();
      flash("Downloaded " + name + ". It is your data, decrypted: delete it when you are done.");
    } catch (x) {
      err.textContent = typeof x === "string" ? x : "Cannot reach the server.";
      err.classList.remove("hidden");
      f.password.select();
    } finally {
      btn.disabled = false;
    }
  }

  // ------------------------------------------------------------------ security (password, sessions)

  // Saves what can be saved. False if edits remain that could not be saved.
  async function saveEverything() {
    if (state.editing) finishEdit(null);
    flushAll();
    await state.queue;
    return !state.pending.size;
  }

  async function logout(path) {
    if (!(await saveEverything()) && !confirm("Some edits could not be saved. Log out anyway?")) return;
    try { await api("POST", path, {}); } catch (e) { /* ignore */ }
    location.href = "/login";
  }

  function openSecurityDialog() {
    $("form-password").reset();
    $("password-error").classList.add("hidden");
    $("dlg-security").showModal();
    $("form-password").current.focus();
  }

  async function submitPassword(e) {
    e.preventDefault();
    const f = $("form-password");
    const err = $("password-error");
    const btn = $("password-submit");
    const others = [$("logout-all"), $("security-close")];
    err.classList.add("hidden");
    let focus = f.current;
    try {
      if (f.fresh.value !== f.repeat.value) { focus = f.repeat; throw "The two new passwords do not match."; }
      btn.disabled = true;
      others.forEach((b) => { b.disabled = true; });
      btn.textContent = "Changing\u2026 (a few seconds)";
      await requireSaved();
      await api("POST", "/api/password", { password: f.current.value, new_password: f.fresh.value });
      location.href = "/login?changed";
    } catch (x) {
      if (x && x.field === "new_password") focus = f.fresh;
      showDialogError("password-error", x);
      btn.disabled = false;
      others.forEach((b) => { b.disabled = false; });
      btn.textContent = "Change password";
      focus.select();
    }
  }

  // ------------------------------------------------------------------ debit mode (per account)
  // On: amounts typed without a sign are money out. The default comes from the account type
  // (on for checking and credit cards); it never applies where the sign is flipped.
  // Remembered in this browser only; it is an input preference, not data.

  const SIGN_KEY = "register-debit-mode";

  function signPrefs() {
    try { return JSON.parse(localStorage.getItem(SIGN_KEY)) || {}; } catch (e) { return {}; }
  }

  function debitMode() {
    const s = state.sheet;
    if (!s) return false;
    const pref = signPrefs()[s.account];
    if (s.owed_positive) return false; // there a plain number is already a charge
    return pref === undefined ? !!typeInfo(s.type).debit_mode : pref;
  }

  function renderSignButton() {
    const b = $("btn-sign");
    b.classList.toggle("hidden", !state.sheet || flipped());
    const on = debitMode();
    b.classList.toggle("debit", on);
    b.textContent = on ? "\u2212 Debit mode" : "\u00B1 As typed";
    b.title = on ? "Amounts without a sign are money out (type + for a deposit). Click to enter amounts as typed."
                 : "Amounts are entered as typed. Click to make plain amounts money out.";
  }

  function toggleSign() {
    if (!state.sheet) return;
    const prefs = signPrefs();
    prefs[state.sheet.account] = !debitMode();
    try { localStorage.setItem(SIGN_KEY, JSON.stringify(prefs)); } catch (e) { /* not remembered */ }
    renderSignButton();
    const ed = state.editing;
    if (ed && columns()[ed.c].key === "amount") {
      ed.input.dispatchEvent(new Event("input"));
      ed.input.focus();
    }
    flash(debitMode() ? "Debit mode on for " + state.sheet.name + ": plain amounts are money out."
                      : "Amounts for " + state.sheet.name + " are entered as typed.");
  }

  // ------------------------------------------------------------------ theme (dark by default)

  function renderThemeButton() {
    const dark = document.documentElement.dataset.theme !== "light";
    $("btn-theme").textContent = dark ? "\u2600 Light mode" : "\u263E Dark mode";
  }

  function toggleTheme() {
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("register-theme", next); } catch (e) { /* not remembered */ }
    renderThemeButton();
  }

  // ------------------------------------------------------------------ startup

  function initialSheet() {
    const m = location.hash.match(/^#([a-z0-9-]+)\/(\d{4}|-)$/);
    if (m) {
      const a = state.tabs.find((x) => x.id === m[1]);
      const year = m[2] === "-" ? null : +m[2];
      if (a && (a.years ? a.years.includes(year) : year === null)) return [a.id, year];
    }
    const a = state.tabs[0];
    if (!a) return null;
    return [a.id, a.years ? a.years[a.years.length - 1] : null];
  }

  async function init() {
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("copy", onCopy);
    document.addEventListener("paste", onPaste);
    $("grid").addEventListener("mousedown", onGridMouseDown);
    $("grid").addEventListener("dblclick", onGridDblClick);
    $("btn-insert").addEventListener("click", insertRow);
    $("btn-delete").addEventListener("click", deleteRow);
    $("btn-retry").addEventListener("click", retryPending);
    $("btn-theme").addEventListener("click", toggleTheme);
    $("btn-sign").addEventListener("click", toggleSign);
    $("btn-sign").addEventListener("mousedown", (e) => e.preventDefault()); // keep the cell editor focused
    renderThemeButton();
    $("btn-discard").addEventListener("click", discardPending);
    $("btn-logout").addEventListener("click", () => logout("/logout"));
    $("btn-security").addEventListener("click", openSecurityDialog);
    $("form-password").addEventListener("submit", submitPassword);
    $("logout-all").addEventListener("click", () => logout("/logout/all"));
    $("security-close").addEventListener("click", () => $("dlg-security").close());
    $("dlg-security").addEventListener("cancel", (e) => { if ($("password-submit").disabled) e.preventDefault(); });
    narrow.addEventListener("change", () => { renderList(); fitColumns(); });
    $("btn-add-row").addEventListener("click", () => openRowDialog(null));
    $("form-row").addEventListener("submit", submitRow);
    $("row-cancel").addEventListener("click", () => $("dlg-row").close());
    $("row-delete").addEventListener("click", deleteRowFromForm);
    $("btn-help").addEventListener("click", () => $("dlg-help").showModal());
    $("help-close").addEventListener("click", () => $("dlg-help").close());
    document.querySelectorAll("dialog").forEach((d) => { // each dialog is named by its heading
      const h = d.querySelector("h2");
      if (!h) return;
      h.id = h.id || d.id + "-heading";
      d.setAttribute("aria-labelledby", h.id);
    });
    // After a dialog, and after a click on a button of the page, the keyboard goes back to the grid.
    const toGrid = () => {
      if (gridHidden()) return;
      if (state.editing) return state.editing.input.focus(); // mid-edit: back to the cell being typed in
      $("grid").focus({ preventScroll: true });
    };
    document.querySelectorAll("dialog").forEach((d) => d.addEventListener("close", toGrid));
    document.addEventListener("click", (e) => {
      if (e.detail && e.target.closest && e.target.closest("button, [role=button]")
          && !e.target.closest("dialog")) toGrid();
    });
    $("btn-history").addEventListener("click", openHistoryDialog);
    $("history-close").addEventListener("click", () => $("dlg-history").close());
    $("btn-reports").addEventListener("click", openReportsDialog);
    $("reports-close").addEventListener("click", () => $("dlg-reports").close());
    $("report-year").addEventListener("change", loadReport);
    $("report-account").addEventListener("change", loadReport);
    $("dlg-reports").addEventListener("close", hideTip);
    $("btn-recurring").addEventListener("click", openRecurringDialog);
    $("form-recurring").addEventListener("submit", submitRecurring);
    $("form-recurring").account.addEventListener("change", recurringAmountHint);
    $("recurring-close").addEventListener("click", () => $("dlg-recurring").close());
    $("recurring-enter").addEventListener("click", () => recurringOp("POST", "/api/recurring/enter", {}, true));
    $("btn-reconcile").addEventListener("click", openReconcileDialog);
    $("form-reconcile").addEventListener("submit", startReconcile);
    $("reconcile-cancel").addEventListener("click", () => $("dlg-reconcile").close());
    $("recon-cancel").addEventListener("click", cancelReconcile);
    $("recon-finish").addEventListener("click", finishReconcile);
    $("btn-search").addEventListener("click", openSearchDialog);
    $("form-search").addEventListener("submit", submitSearch);
    $("search-close").addEventListener("click", () => $("dlg-search").close());
    $("btn-transfer").addEventListener("click", openTransferDialog);
    $("form-transfer").addEventListener("submit", submitTransfer);
    $("transfer-cancel").addEventListener("click", () => $("dlg-transfer").close());
    $("btn-export").addEventListener("click", openExportDialog);
    $("form-export").addEventListener("submit", submitExport);
    $("export-cancel").addEventListener("click", () => $("dlg-export").close());
    $("form-account").addEventListener("submit", submitAccount);
    $("account-cancel").addEventListener("click", () => $("dlg-account").close());
    $("form-account").type.addEventListener("change", accountTypeChanged);
    $("form-settings").addEventListener("submit", submitSettings);
    $("settings-cancel").addEventListener("click", () => $("dlg-settings").close());
    window.addEventListener("focus", checkStatus);
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) saveBeforeLeaving();
      else checkStatus();
    });
    window.addEventListener("pagehide", saveBeforeLeaving);
    window.addEventListener("beforeunload", (e) => {
      saveBeforeLeaving();
      // only warn when the edits cannot be saved (read-only repo, recorder down, earlier failure)
      if (state.pending.size && (readonly() || state.blocked)) { e.preventDefault(); e.returnValue = ""; }
    });
    setInterval(checkStatus, STATUS_POLL_MS);
    let resizeTimer = null;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(fitColumns, 100);
    });
    $("grid-wrap").addEventListener("scroll", hideSuggest);
    try {
      await loadTabs();
      const first = initialSheet();
      if (first) await openSheet(first[0], first[1]);
      renderBanner();
    } catch (e) {
      handleError(e);
      renderBanner();
    }
    loadRecurring(); // after the first sheet is up: neither is needed to show it,
    loadHints();     // and the hints mean reading every sheet
  }

  init();
})();
