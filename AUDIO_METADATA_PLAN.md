# Metadata, Discogs, Waveform & Splitting — Implementation Plan

**Status: plan only.** Nothing here is implemented yet. Verify against `app.py` before starting any item.

Reference: [Kid3](https://kid3.kde.org/) — tag editor with a frame table, *Import from Discogs*, *Tags → Filename* / *Filename → Tags* format strings (`%{artist} - %{album}/%{track} %{title}`), and *Create Playlist* / *Rename Directory* actions.

## Current state (relevant facts)

- `/api/start` takes `filename`, `format`, `samplerate`, `bitdepth`; `clean_recording_filename()` already sanitises each path segment with `secure_filename` and `resolve_recording_path()` supports sub-folders under `/app/recordings`.
- Recording = FFmpeg fed from the router thread (`arecord` stays running for the monitor). No metadata is written; no post-processing step exists.
- No test suite, so each phase below includes a manual verification checklist.
- Container has no outbound-network requirement today; Discogs adds one (document in compose/README).

## Phases, sorted by effort (smallest first)

| # | Item | Effort | Depends on |
|---|------|--------|-----------|
| 1 | Filename/folder template engine | S (≈0.5–1 d) | – |
| 2 | Basic tag fields written at record time | S (≈1 d) | 1 |
| 3 | Recordings list, delete & playback (README "File Management") | S/M (≈1–1.5 d) | – |
| 4 | Waveform display (finished recordings; live input later) | M (≈2 d) | 3 |
| 5 | Discogs search + release import (tags, tracklist) | M (≈2–3 d) | 1, 2 |
| 6 | Manual split points + split action | M (≈2 d) | 4 |
| 7 | Split by Discogs track times | M/L (≈2 d) | 4, 5, 6 |
| 8 | Format conversion of finished files (MP3, FLAC) | M (≈1–2 d) | 3 |
| 9 | Marker export: Audacity label track, Ableton locators | M (≈1–2 d) | 4 (markers) |
| 10 | Click/pop detection with markers | M/L (≈2–3 d) | 4, 9 |
| 11 | Phase scope (stereo correlation / goniometer) in live monitor | M/L (≈2–3 d) | – |
| 12 | Kid3-style post-hoc tag editor / bulk rename | L (≈4–5 d) | 1, 2, 5 |
| 13 | Recording scheduling | L (≈3 d) | 1, 2 |
| 14 | Cloud storage upload, advanced filters/effects | L/XL (≈5+ d each) | 3 / 8 |

Phases 1–4 are independent of Discogs and can ship first; 3–4 can be done in parallel with 1–2.

### 1. Filename / folder template engine (S)
- New helper `render_template(fmt, tags)` in `app.py` supporting Kid3-like placeholders: `%{artist}`, `%{album}`, `%{year}`, `%{track}` (zero-padded `%{track:02}`), `%{title}`, `%{label}`, `%{catno}`, `%{date}`.
- Default `%{artist}/%{album} (%{year})/%{track} - %{title}`; output goes through existing `clean_recording_filename()` so sanitisation and traversal protection stay in one place. Empty placeholders collapse separators.
- `/api/start` accepts optional `tags` (dict) and `filename_template`; plain `filename` keeps working (backward compatible).
- UI: small collapsible "Metadata" panel in `templates/index.html` with a live filename preview.
- Verify: preview matches the path returned by `/api/start`; `../` in a tag value cannot escape `/app/recordings`.

### 2. Basic tag writing (S)
- Pass tags to FFmpeg as `-metadata key=value` (title, artist, album, date, track, genre, comment) in `start_recording()`. FLAC/MP3/AAC/AIFF are covered; plain WAV gets LIST/INFO only (documented limitation).
- Validate: strings only, length-capped, strip control characters/newlines.
- Verify with `ffprobe -show_format` on a short recording per format.

### 3. Recordings list, delete & playback (S/M)
- README lists "List and delete recordings" and "Audio playback in web interface"; neither exists in `app.py` yet. Add `GET /api/recordings` (name, size, mtime, duration via ffprobe), `DELETE /api/recordings/<path>`, and a `Range`-capable `GET /api/recording/<path>` for an `<audio>` player. All paths via `resolve_recording_path()`; delete needs a confirm in the UI and must refuse the active recording.
- This is the file browser that phases 4, 6–9 and 12 build on.

### 4. Waveform display (M)
- Backend: `GET /api/waveform?file=…&points=2000` returning min/max peaks. Generate with FFmpeg to raw s16 mono (`ffmpeg -i f -ac 1 -f s16le -`), reduce in Python/`array`; cache as `<file>.peaks.json`. Path via `resolve_recording_path()` only.
- Frontend: canvas renderer (no new JS deps), click-to-seek, zoom, region markers (reused by phases 6/7 and 9/10). Playback uses the phase 3 route.
- Live input waveform (README item): a rolling min/max strip fed from the existing `/api/levels` data or a router-thread tap; do after the finished-file view.
- Verify: long file (>1 h) renders in a few seconds; peak cache invalidated on mtime change.

### 5. Discogs integration (M)
- Backend proxy (keeps the token server-side): `GET /api/discogs/search?q=` → `https://api.discogs.com/database/search`, `GET /api/discogs/release/<id>` → `/releases/{id}`. Env `DISCOGS_TOKEN`, custom `User-Agent` (required by Discogs), respect 60 req/min limit, short in-memory cache, timeouts, graceful offline error.
- Map release → tags: artists (join, strip ` (2)` disambiguation), title, year, label, catno, genres/styles, per-track `position` (e.g. `A1`, `2-3`), `title`, `duration` (`mm:ss`). Vinyl positions map to track numbers via a configurable rule.
- UI: search box + result list; choosing a release fills the metadata panel and the track list, then picks the current track for the template.
- Add `requests` (or use stdlib `urllib`) — check advisory DB if adding a dependency.
- Verify: mock responses for unit-level checks; one live manual search.

### 6. Manual split (M)
- `POST /api/split` `{file, points:[seconds…], format?}` → FFmpeg `-ss/-to -c copy` per segment (re-encode only for MP3/AAC if frame-accurate cuts are required); outputs named through the phase 1 template; original kept unless `delete_original`.
- UI: add/move/remove markers on the waveform; "Split" button; progress/result list. Run in a background thread, never inside `engine_lock` (must not disturb the live monitor).
- Verify: concatenated segments equal original duration (±1 sample for WAV/AIFF/FLAC).

### 7. Split by Discogs track times (M/L)
- Convert tracklist durations to cumulative boundaries: `start[n] = Σ duration[0..n-1]`, plus optional per-side offset (silence/lead-in) and a global start offset.
- Durations from Discogs are often missing or rounded to seconds → snap each boundary to the nearest silence within ±N s (FFmpeg `silencedetect` or minimum-RMS from peaks data) and show the result as editable markers for confirmation before splitting.
- Side/vinyl handling: if positions restart (`A1…`, `B1…`), allow one recording per side and match the tracklist subset.
- Reuse `/api/split` (phase 6); tags per segment come from the matching Discogs track (phase 2 writer, applied during split via `-metadata`).
- Verify: drift report (sum of Discogs times vs. real length); user must be able to override every marker.

### 8. Format conversion (M)
- `POST /api/convert` `{file, format: flac|mp3|aac, bitrate?}` runs FFmpeg in a background thread, copies tags (phase 2), names output via phase 1 template. Complements record-time formats already handled by `_normalize_file_format()`.

### 9. Marker export (M)
- Export waveform/split/click markers as an Audacity label track (`start\tend\tlabel` text) and Ableton-friendly output (locators as CSV/`.asd`-free text or cue list; verify what Ableton actually imports before promising warp markers). Optional CUE sheet export doubles as split-point import.

### 10. Click and pop detection (M/L)
- Offline scan of a finished file for short, high-slope transients (e.g. derivative/median-filter threshold on peaks plus a short raw-sample pass); sensitivity slider; results appear as markers on the waveform and export via phase 9. Vinyl-specific, so tune on real needle-drop recordings; expect false positives on percussive music.

### 11. Phase scope (M/L)
- Live goniometer / correlation meter: send downsampled L/R pairs plus a correlation coefficient from the router thread (new endpoint or extend `/api/levels`); canvas rendering client-side. Stereo captures only. Design with `AUDIO_LOUDNESS_RADAR.md` so monitor visuals share one data feed.

### 12. Kid3-style tag editor & bulk rename (L)
- Edit tags on existing files (mutagen or FFmpeg remux), multi-file selection, Filename→Tags (parse with a reversed template), Tags→Filename/Folder rename, cover art (Discogs `images[0]`) embed, playlist/`.m3u` generation.
- Do last; evaluate whether mutagen is worth the new dependency versus FFmpeg-only rewrites.

### 13–14. Scheduling, cloud storage, filters (L+)
- Scheduling: persisted start/stop jobs honouring `engine_lock`, templated filenames. Cloud upload: optional rclone/S3 post-step with credentials via env only. Filters/effects: apply offline via FFmpeg on copies, never on the monitor path. Defer until the above is stable.

## Cross-cutting
- Keep the API additive; `/api/start` behaviour without new fields is unchanged.
- Update `AGENTS.md` API table and tick the matching `README.md` "Future Enhancements" item as each phase lands.
- New env vars: `DISCOGS_TOKEN`; compose needs outbound network (default bridge is fine).
- Security: all paths through `resolve_recording_path()`, escape metadata in the UI (`textContent`, not `innerHTML`), never log the token.

## Suggested delivery order
1 → 2 (tagged, templated recordings) → 3 (file list/playback) → 4 (waveform) → 5 (Discogs) → 6 → 7 → 8–11 as needed → 12 → 13–14.
