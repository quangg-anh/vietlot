"use strict";

/* Theme controller: cycles light -> dark -> system, persists choice in localStorage. */
(function () {
  const STORAGE_KEY = "vietlott-theme";
  const root = document.documentElement;
  const button = document.getElementById("theme-toggle");
  const label = document.getElementById("theme-label");
  if (!button) return;

  const modes = ["light", "dark", "auto"];
  const labels = { light: "Sáng", dark: "Tối", auto: "Theo hệ thống" };

  function storedMode() {
    try {
      const value = localStorage.getItem(STORAGE_KEY);
      return modes.includes(value) ? value : "auto";
    } catch {
      return "auto";
    }
  }

  function apply(mode) {
    if (mode === "auto") {
      root.removeAttribute("data-theme");
    } else {
      root.setAttribute("data-theme", mode);
    }
    button.dataset.mode = mode;
    button.setAttribute("aria-label", `Chế độ giao diện: ${labels[mode]}. Nhấn để đổi.`);
    if (label) label.textContent = labels[mode];
  }

  function next(mode) {
    const index = modes.indexOf(mode);
    return modes[(index + 1) % modes.length];
  }

  const initial = storedMode();
  apply(initial);

  button.addEventListener("click", () => {
    const mode = next(button.dataset.mode || "auto");
    apply(mode);
    try { localStorage.setItem(STORAGE_KEY, mode); } catch { /* storage unavailable */ }
  });

  /* keep "auto" in sync when the OS theme changes */
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  media.addEventListener("change", () => {
    if ((button.dataset.mode || "auto") === "auto") apply("auto");
  });
})();
