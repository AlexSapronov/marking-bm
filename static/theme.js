/* Shared preference and palette with gis-trueapi and km-split. */
window.GisTheme = (() => {
  let chosen;
  const media = window.matchMedia?.("(prefers-color-scheme: dark)");
  try {
    chosen = localStorage.getItem("gis_theme");
  } catch (_) {}
  function refresh() {
    const theme = ["light", "dark"].includes(chosen)
      ? chosen
      : media?.matches
        ? "dark"
        : "light";
    document.documentElement.dataset.theme = theme;
    document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
      const label = theme === "dark" ? "Светлая тема" : "Тёмная тема";
      button.textContent = theme === "dark" ? "☀" : "☾";
      button.title = label;
      button.setAttribute("aria-label", label);
    });
  }
  document.addEventListener("click", (event) => {
    if (!event.target.closest("[data-theme-toggle]")) return;
    chosen =
      document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    try {
      localStorage.setItem("gis_theme", chosen);
    } catch (_) {}
    refresh();
  });
  window.addEventListener("storage", (event) => {
    if (event.key === "gis_theme") {
      chosen = event.newValue;
      refresh();
    }
  });
  media?.addEventListener?.("change", refresh);
  refresh();
  return { refresh };
})();
