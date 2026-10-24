function getThemeMode() {
  try {
    return localStorage.getItem("themeMode")
      || (localStorage.getItem("theme") === "dark" ? "dark" : "system");
  } catch (e) {
    return "system";
  }
}

function applyTheme(mode) {
  const dark = mode === "dark"
    || (mode === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("sl-theme-dark", dark);
  document.documentElement.classList.toggle("sl-theme-light", !dark);
}

export function initTheme() {
  const themeMode = document.getElementById("themeMode");
  themeMode.value = getThemeMode();
  themeMode.addEventListener("change", event => {
    const mode = event.target.value;
    applyTheme(mode);
    try {
      localStorage.setItem("themeMode", mode);
      localStorage.removeItem("theme");
    } catch (e) {}
  });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if (getThemeMode() === "system") applyTheme("system");
  });
}
