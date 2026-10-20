"use strict";

/* Applies the stored theme before first paint so the page never flashes the wrong palette.
   Kept in sync with the stylesheet's "auto" fallback (prefers-color-scheme). */
(function () {
  try {
    const mode = localStorage.getItem("vietlott-theme");
    if (mode === "light" || mode === "dark") document.documentElement.setAttribute("data-theme", mode);
  } catch (error) { /* storage unavailable */ }
})();
