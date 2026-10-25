const list = document.getElementById("recordingList");
const status = document.getElementById("libraryStatus");
const breadcrumb = document.getElementById("libraryBreadcrumb");
const refreshButton = document.getElementById("refreshLibrary");
const player = new Audio();
const PLAY_ICON = ["M8 5v14l11-7z"];
const STOP_ICON = ["M7 7h10v10H7z"];

let recordings = [];
let currentFolder = "";
let activePlayer = null;
let activeRecordingPath = null;
let activePlayButton = null;
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

function setButtonIcon(button, paths) {
  const icon = button.querySelector("svg");
  if (!icon) return;
  icon.replaceChildren();
  paths.forEach(d => {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", d);
    icon.append(path);
  });
}

function relativeEntries() {
  const prefix = currentFolder ? `${currentFolder}/` : "";
  const folders = new Map();
  const files = [];

  recordings.forEach(recording => {
    if (!recording.path.startsWith(prefix)) return;
    const remainder = recording.path.slice(prefix.length);
    const [segment, ...children] = remainder.split("/");
    if (children.length) {
      const folder = folders.get(segment) || { name: segment, path: `${prefix}${segment}`, count: 0 };
      folder.count++;
      folders.set(segment, folder);
    } else {
      files.push(recording);
    }
  });

  return {
    folders: [...folders.values()].sort((a, b) => a.name.localeCompare(b.name)),
    files: files.sort((a, b) => a.path.localeCompare(b.path)),
  };
}

function openFolder(path) {
  currentFolder = path;
  renderExplorer();
}

function renderBreadcrumb() {
  breadcrumb.replaceChildren();
  const parts = currentFolder ? currentFolder.split("/") : [];
  const trail = [{ label: "Library", path: "" }];
  parts.forEach((part, index) => {
    trail.push({ label: part, path: parts.slice(0, index + 1).join("/") });
  });

  trail.forEach((item, index) => {
    if (index) {
      const separator = document.createElement("span");
      separator.textContent = "/";
      separator.setAttribute("aria-hidden", "true");
      breadcrumb.append(separator);
    }
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = item.label;
    button.className = item.path === currentFolder ? "is-current" : "";
    button.disabled = item.path === currentFolder;
    button.addEventListener("click", () => openFolder(item.path));
    breadcrumb.append(button);
  });
}

function restoreMonitorIfIdle() {
  if (activePlayer || !suspendedMonitor) return;
  const monitor = suspendedMonitor;
  suspendedMonitor = null;
  window.resumeMonitorAfterLibraryPlayback?.(monitor);
}

function setPlaybackButton(button, active) {
  if (!button) return;
  button.classList.toggle("is-playing", active);
  button.setAttribute("aria-label", active ? "Stop playback" : "Play recording");
  button.title = active ? "Stop playback" : "Play recording";
  setButtonIcon(button, active ? STOP_ICON : PLAY_ICON);
}

function startPlayback() {
  activePlayer = player;
  if (!suspendedMonitor) {
    suspendedMonitor = window.suspendMonitorForLibraryPlayback?.() || null;
  }
  setPlaybackButton(activePlayButton, true);
}

function finishPlayback() {
  queueMicrotask(() => {
    if (!player.paused) return;
    setPlaybackButton(activePlayButton, false);
    activePlayer = null;
    activeRecordingPath = null;
    activePlayButton = null;
    restoreMonitorIfIdle();
  });
}

function stopPlayback() {
  if (!activePlayer) return;
  setPlaybackButton(activePlayButton, false);
  player.pause();
  player.currentTime = 0;
}

function togglePlayback(recording, button) {
  if (activeRecordingPath === recording.path && !player.paused) {
    stopPlayback();
    return;
  }
  setPlaybackButton(activePlayButton, false);
  activeRecordingPath = recording.path;
  activePlayButton = button;
  player.src = fileUrl(recording.path);
  player.play().catch(error => {
    setStatus(error.message || "Playback failed", true);
    finishPlayback();
  });
}

async function deleteRecording(recording) {
  if (!window.confirm(`Delete "${recording.path}"? This cannot be undone.`)) return;
  if (activeRecordingPath === recording.path) stopPlayback();

  const response = await fetch("/api/recordings", {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: recording.path }),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "Failed to delete recording");
  await loadRecordings();
}

async function renameRecording(recording) {
  const currentName = recording.path.split("/").pop();
  const filename = window.prompt("Rename recording", currentName);
  if (filename === null) return;
  if (activeRecordingPath === recording.path) stopPlayback();

  const response = await fetch("/api/recordings", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: recording.path, filename }),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "Failed to rename recording");
  await loadRecordings();
}

function createFolderEntry(folder) {
  const entry = document.createElement("li");
  entry.className = "explorer-entry explorer-folder";
  const button = document.createElement("button");
  button.type = "button";
  button.className = "explorer-open";
  button.append(createIcon(["M3 6a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"]));
  const name = document.createElement("span");
  name.textContent = folder.name;
  button.append(name);
  button.addEventListener("click", () => openFolder(folder.path));
  const count = document.createElement("span");
  count.className = "explorer-count";
  count.textContent = `${folder.count} item${folder.count === 1 ? "" : "s"}`;
  entry.append(button, count);
  return entry;
}

function createFileMenu(recording) {
  const menu = document.createElement("details");
  menu.className = "explorer-menu";
  const summary = document.createElement("summary");
  summary.setAttribute("aria-label", `Actions for ${recording.path}`);
  summary.title = `Actions for ${recording.path}`;
  summary.textContent = "•••";

  const actions = document.createElement("div");
  actions.className = "explorer-menu-actions";
  const download = document.createElement("a");
  download.href = fileUrl(recording.path, true);
  download.append(
    createIcon(["M12 3v12m0 0 4-4m-4 4-4-4", "M4 17v3h16v-3"]),
    document.createTextNode("Download"),
  );
  const rename = document.createElement("button");
  rename.type = "button";
  rename.append(
    createIcon(["M4 20h4l10.5-10.5-4-4L4 16v4", "m12.5 6.5 4 4"]),
    document.createTextNode("Rename"),
  );
  rename.addEventListener("click", async () => {
    try {
      await renameRecording(recording);
    } catch (error) {
      setStatus(error.message || "Failed to rename recording", true);
    }
  });
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "explorer-delete";
  remove.append(
    createIcon(["M4 7h16M10 11v6m4-6v6M9 7l1-3h4l1 3M6 7l1 14h10l1-14"]),
    document.createTextNode("Delete"),
  );
  remove.addEventListener("click", async () => {
    try {
      await deleteRecording(recording);
    } catch (error) {
      setStatus(error.message || "Failed to delete recording", true);
    }
  });
  actions.append(download, rename, remove);
  menu.append(summary, actions);
  return menu;
}

function createFileEntry(recording) {
  const entry = document.createElement("li");
  entry.className = "explorer-entry explorer-file";
  const play = createIconButton("Play recording", "explorer-play", PLAY_ICON, () => {
    togglePlayback(recording, play);
  });
  if (activeRecordingPath === recording.path && !player.paused) {
    setPlaybackButton(play, true);
    activePlayButton = play;
  }
  const name = document.createElement("span");
  name.className = "explorer-name";
  name.textContent = recording.path.split("/").pop();
  name.title = recording.path;
  const size = document.createElement("span");
  size.className = "explorer-size";
  size.textContent = formatSize(recording.size_bytes);
  entry.append(play, name, size, createFileMenu(recording));
  return entry;
}

function renderExplorer() {
  renderBreadcrumb();
  list.replaceChildren();
  const { folders, files } = relativeEntries();
  const count = folders.length + files.length;
  if (!count) {
    setStatus(currentFolder ? "This folder is empty." : "No recordings yet.");
    return;
  }
  setStatus(`${folders.length} folder${folders.length === 1 ? "" : "s"} · ${files.length} file${files.length === 1 ? "" : "s"}`);
  folders.forEach(folder => list.append(createFolderEntry(folder)));
  files.forEach(recording => list.append(createFileEntry(recording)));
}

async function loadRecordings() {
  refreshButton.disabled = true;
  setStatus("Loading recordings…");
  try {
    const response = await fetch("/api/recordings", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Failed to load recordings");
    recordings = payload.recordings;
    if (currentFolder && !recordings.some(recording => recording.path.startsWith(`${currentFolder}/`))) {
      currentFolder = "";
    }
    renderExplorer();
  } catch (error) {
    list.replaceChildren();
    setStatus(error.message || "Failed to load recordings", true);
  } finally {
    refreshButton.disabled = false;
  }
}

export function initLibrary() {
  player.preload = "metadata";
  player.addEventListener("play", startPlayback);
  player.addEventListener("pause", finishPlayback);
  refreshButton.addEventListener("click", loadRecordings);
  window.reloadLibrary = loadRecordings;
  loadRecordings();
}
