const list = document.getElementById("recordingList");
const status = document.getElementById("libraryStatus");
const refreshButton = document.getElementById("refreshLibrary");
let activePlayer = null;
let suspendedMonitor = null;

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

function createIcon(paths) {
  const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  icon.setAttribute("viewBox", "0 0 24 24");
  icon.setAttribute("aria-hidden", "true");
  paths.forEach(d => {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", d);
    icon.append(path);
  });
  return icon;
}

function createIconButton(label, className, paths, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.setAttribute("aria-label", label);
  button.title = label;
  button.append(createIcon(paths));
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

function restoreMonitorIfIdle() {
  if (activePlayer || !suspendedMonitor) return;
  const monitor = suspendedMonitor;
  suspendedMonitor = null;
  window.resumeMonitorAfterLibraryPlayback?.(monitor);
}

function stopActivePlayer(except = null) {
  if (!activePlayer || activePlayer === except) return;
  activePlayer.pause();
  activePlayer.currentTime = 0;
}

function startPlayback(player) {
  stopActivePlayer(player);
  activePlayer = player;
  if (!suspendedMonitor) {
    suspendedMonitor = window.suspendMonitorForLibraryPlayback?.() || null;
  }
}

function finishPlayback(player) {
  queueMicrotask(() => {
    if (activePlayer !== player || !player.paused) return;
    activePlayer = null;
    restoreMonitorIfIdle();
  });
}

async function renameRecording(recording) {
  const currentName = recording.path.split("/").pop();
  const filename = window.prompt("Rename recording", currentName);
  if (filename === null) return;

  const response = await fetch("/api/recordings", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: recording.path, filename }),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "Failed to rename recording");
  await loadRecordings();
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
    play.addEventListener("play", () => startPlayback(play));
    play.addEventListener("pause", () => finishPlayback(play));
    const download = document.createElement("a");
    download.href = fileUrl(recording.path, true);
    download.className = "library-download";
    download.setAttribute("aria-label", `Download ${recording.path}`);
    download.title = `Download ${recording.path}`;
    download.append(createIcon([
      "M12 3v12m0 0 4-4m-4 4-4-4",
      "M4 17v3h16v-3",
    ]));
    const rename = createIconButton("Rename " + recording.path, "recording-rename", [
      "M4 20h4l10.5-10.5-4-4L4 16v4",
      "m12.5 6.5 4 4",
    ], async () => {
      rename.disabled = true;
      try {
        await renameRecording(recording);
      } catch (error) {
        setStatus(error.message || "Failed to rename recording", true);
      } finally {
        rename.disabled = false;
      }
    });
    const remove = createIconButton("Delete " + recording.path, "recording-delete", [
      "M4 7h16M10 11v6m4-6v6M9 7l1-3h4l1 3M6 7l1 14h10l1-14",
    ], async () => {
      remove.disabled = true;
      try {
        await deleteRecording(recording, entry);
      } catch (error) {
        setStatus(error.message || "Failed to delete recording", true);
        remove.disabled = false;
      }
    });
    actions.append(play, download, rename, remove);

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
  window.reloadLibrary = loadRecordings;
  loadRecordings();
}
