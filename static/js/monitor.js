function renderTransport(active) {
  const button = document.getElementById("monitorToggle");
  if (!button) return;
  button.textContent = active ? "Stop monitor" : "Play monitor";
  button.classList.toggle("active", active);
}

export function initMonitor() {
  const quality = document.getElementById("monitorQuality");
  const toggle = document.getElementById("monitorToggle");

  quality.addEventListener("change", () => {
    if (window.isMonitorActive()) window.switchQuality(quality.value);
  });
  toggle.addEventListener("click", () => {
    if (window.isMonitorActive()) {
      window.stopStream();
    } else {
      window.switchQuality(quality.value);
    }
  });
  window.renderMonitorTransport = renderTransport;
  renderTransport(false);
}
