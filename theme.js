// Loaded synchronously in <head> so the saved theme applies before the first paint.
// Dark unless the user chose light (stored in this browser only).
"use strict";
(() => {
  let theme = "dark";
  try {
    if (localStorage.getItem("register-theme") === "light") theme = "light";
  } catch (e) { /* storage unavailable: keep the default */ }
  document.documentElement.dataset.theme = theme;
})();
