const VIEWS = new Set(["dashboard", "monitor", "library", "settings"]);
const STORAGE_KEY = "activeView";

function selectView(view) {
  if (!VIEWS.has(view)) return;

  document.querySelectorAll("[data-view]").forEach(panel => {
    const active = panel.dataset.view === view;
    panel.hidden = !active;
    panel.classList.toggle("is-active", active);
  });

  document.querySelectorAll("[data-view-target]").forEach(button => {
    const active = button.dataset.viewTarget === view;
    button.classList.toggle("is-active", active);
    if (active) {
      button.setAttribute("aria-current", "page");
    } else {
      button.removeAttribute("aria-current");
    }
  });

  try {
    localStorage.setItem(STORAGE_KEY, view);
  } catch (e) {}
}

export function initNavigation() {
  document.querySelectorAll("[data-view-target]").forEach(button => {
    button.addEventListener("click", () => selectView(button.dataset.viewTarget));
  });

  let initial = "dashboard";
  try {
    initial = localStorage.getItem(STORAGE_KEY) || initial;
  } catch (e) {}
  selectView(initial);
}
