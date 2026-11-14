// account-register demo: nothing is kept in the browser.
//
// Loaded first, before theme.js and grid.js. Those two remember two preferences (the theme and
// debit mode) in localStorage. In the demo they must not: this file puts a stand-in in place of
// localStorage and sessionStorage that has the same methods but keeps its values in a variable,
// which is gone when the page is reloaded or closed. The buttons still work for as long as the
// page is open; nothing reaches the browser's storage.
//
// This covers what the app's own scripts do. It is not a sandbox for other code.
"use strict";
(() => {
  class MemoryStorage {
    constructor() { this.items = new Map(); }
    get length() { return this.items.size; }
    key(n) { return [...this.items.keys()][n] ?? null; }
    getItem(key) { return this.items.has(String(key)) ? this.items.get(String(key)) : null; }
    setItem(key, value) { this.items.set(String(key), String(value)); }
    removeItem(key) { this.items.delete(String(key)); }
    clear() { this.items.clear(); }
  }

  const refuse = () => { throw new DOMException("The demo stores nothing in the browser.", "SecurityError"); };
  for (const name of ["localStorage", "sessionStorage"]) {
    const standIn = new MemoryStorage();
    try {
      // an own property of window hides the browser's accessor, whether that lives on window or
      // on its prototype. (Left configurable: some browsers refuse a non-configurable property
      // on window, and nothing in the page puts the real one back.)
      Object.defineProperty(window, name, { get: () => standIn, enumerable: true, configurable: true });
    } catch (e) {
      // The browser would not let the property be replaced. Make the real object refuse instead:
      // the app wraps every storage call in try/catch and carries on without it.
      Storage.prototype.setItem = refuse;
      Storage.prototype.getItem = refuse;
    }
  }
  // The app sets no cookies from script (the real one gets its session cookie from the server),
  // and the build stops if its scripts start to. This is a second line: a cookie written here
  // is dropped, not kept.
  try {
    Object.defineProperty(document, "cookie", { get: () => "", set() {}, enumerable: true, configurable: true });
  } catch (e) { /* left as it is: nothing writes it */ }
  // demo-api.js starts the page's own script only when it sees this: if this file did not
  // load, grid.js never starts, so nothing is written to the browser's real storage.
  window.REGISTER_DEMO_NOSTORE = true;
})();
