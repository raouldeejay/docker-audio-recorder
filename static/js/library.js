const list = document.getElementById("recordingList");
const status = document.getElementById("libraryStatus");
const refreshButton = document.getElementById("refreshLibrary");

function fileUrl(path, download = false) {
  const params = new URLSearchParams({ path });
  if (download) params.set("download", "1");
  return `/api/recordings/file?${params}`;
}

function formatSize(size) {
  if (size < 1024) return `${size} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = size;
  let unit = -1;
  do {
    value /= 1024;
    unit++;
  } while (value >= 1024 && unit < units.length - 1);
  return `${value.toFixed(value >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function formatModified(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Unknown date" : date.toLocaleString();
}

function setStatus(message, error = false) {
  status.textContent = message;
  status.classList.toggle("error", error);
}

function createAction(label, className, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.addEventListener("click", onClick);
  return button;
}

async function deleteRecording(recording, entry) {
  if (!window.confirm(`Delete "${recording.path}"? This cannot be undone.`)) return;

  const response = await fetch("/api/recordings", {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: recording.path }),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "Failed to delete recording");

  entry.remove();
  const remaining = list.children.length;
  setStatus(remaining ? `${remaining} recording${remaining === 1 ? "" : "s"}` : "No recordings yet.");
}

function renderRecordings(recordings) {
  list.replaceChildren();
  if (!recordings.length) {
    setStatus("No recordings yet.");
    return;
  }

  setStatus(`${recordings.length} recording${recordings.length === 1 ? "" : "s"}`);
  recordings.forEach(recording => {
    const entry = document.createElement("li");
    entry.className = "recording-entry";

    const details = document.createElement("div");
    details.className = "recording-details";
    const name = document.createElement("span");
    name.className = "recording-name";
    name.textContent = recording.path;
    name.title = recording.path;
    const meta = document.createElement("span");
    meta.className = "recording-meta";
    meta.textContent = `${formatSize(recording.size_bytes)} · ${formatModified(recording.modified_at)}`;
    details.append(name, meta);

    const actions = document.createElement("div");
    actions.className = "recording-actions";
    const play = document.createElement("audio");
    play.controls = true;
    play.preload = "none";
    play.src = fileUrl(recording.path);
    play.setAttribute("aria-label", `Play ${recording.path}`);
    const download = document.createElement("a");
    download.href = fileUrl(recording.path, true);
    download.textContent = "Download";
    download.className = "library-download";
    const remove = createAction("Delete", "recording-delete", async () => {
      remove.disabled = true;
      try {
        await deleteRecording(recording, entry);
      } catch (error) {
        setStatus(error.message || "Failed to delete recording", true);
        remove.disabled = false;
      }
    });
    actions.append(play, download, remove);

    entry.append(details, actions);
    list.append(entry);
  });
}

async function loadRecordings() {
  refreshButton.disabled = true;
  setStatus("Loading recordings…");
  try {
    const response = await fetch("/api/recordings", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Failed to load recordings");
    renderRecordings(payload.recordings);
  } catch (error) {
    list.replaceChildren();
    setStatus(error.message || "Failed to load recordings", true);
  } finally {
    refreshButton.disabled = false;
  }
}

export function initLibrary() {
  refreshButton.addEventListener("click", loadRecordings);
  loadRecordings();
}
