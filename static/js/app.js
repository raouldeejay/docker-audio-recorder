import { initLibrary } from "./library.js";
import { initMonitor } from "./monitor.js";
import { initNavigation } from "./navigation.js";
import { initTheme } from "./theme.js";

window.addEventListener("DOMContentLoaded", () => {
  initTheme();
  initNavigation();
  initMonitor();
  initLibrary();
});
