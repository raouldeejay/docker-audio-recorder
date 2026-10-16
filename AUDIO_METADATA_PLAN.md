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
| 3 | Waveform display of finished recordings | M (≈2 d) | – |
| 4 | Discogs search + release import (tags, tracklist) | M (≈2–3 d) | 1, 2 |
| 5 | Manual split points + split action | M (≈2 d) | 3 |
| 6 | Split by Discogs track times | M/L (≈2 d) | 3, 4, 5 |
| 7 | Kid3-style post-hoc tag editor / bulk rename | L (≈4–5 d) | 1, 2, 4 |

Phases 1–3 are independent of Discogs and can ship first; 3 can be done in parallel with 1–2.

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

### 3. Waveform display (M)
- Backend: `GET /api/recordings` (list files under the recordings root) and `GET /api/waveform?file=…&points=2000` returning min/max peaks. Generate with FFmpeg to raw s16 mono (`ffmpeg -i f -ac 1 -f s16le -`), reduce in Python/`array`; cache as `<file>.peaks.json`. Path via `resolve_recording_path()` only.
- Frontend: canvas renderer (no new JS deps), click-to-seek, zoom, region markers (reused by phases 5/6). Optional `<audio>` playback via a `Range`-capable `/api/recording/<path>` route.
- Verify: long file (>1 h) renders in a few seconds; peak cache invalidated on mtime change.

### 4. Discogs integration (M)
- Backend proxy (keeps the token server-side): `GET /api/discogs/search?q=` → `https://api.discogs.com/database/search`, `GET /api/discogs/release/<id>` → `/releases/{id}`. Env `DISCOGS_TOKEN`, custom `User-Agent` (required by Discogs), respect 60 req/min limit, short in-memory cache, timeouts, graceful offline error.
- Map release → tags: artists (join, strip ` (2)` disambiguation), title, year, label, catno, genres/styles, per-track `position` (e.g. `A1`, `2-3`), `title`, `duration` (`mm:ss`). Vinyl positions map to track numbers via a configurable rule.
- UI: search box + result list; choosing a release fills the metadata panel and the track list, then picks the current track for the template.
- Add `requests` (or use stdlib `urllib`) — check advisory DB if adding a dependency.
- Verify: mock responses for unit-level checks; one live manual search.

### 5. Manual split (M)
- `POST /api/split` `{file, points:[seconds…], format?}` → FFmpeg `-ss/-to -c copy` per segment (re-encode only for MP3/AAC if frame-accurate cuts are required); outputs named through the phase 1 template; original kept unless `delete_original`.
- UI: add/move/remove markers on the waveform; "Split" button; progress/result list. Run in a background thread, never inside `engine_lock` (must not disturb the live monitor).
- Verify: concatenated segments equal original duration (±1 sample for WAV/AIFF/FLAC).

### 6. Split by Discogs track times (M/L)
- Convert tracklist durations to cumulative boundaries: `start[n] = Σ duration[0..n-1]`, plus optional per-side offset (silence/lead-in) and a global start offset.
- Durations from Discogs are often missing or rounded to seconds → snap each boundary to the nearest silence within ±N s (FFmpeg `silencedetect` or minimum-RMS from peaks data) and show the result as editable markers for confirmation before splitting.
- Side/vinyl handling: if positions restart (`A1…`, `B1…`), allow one recording per side and match the tracklist subset.
- Reuse `/api/split`; tags per segment come from the matching Discogs track (phase 2 writer, applied during split via `-metadata`).
- Verify: drift report (sum of Discogs times vs. real length); user must be able to override every marker.

### 7. Kid3-style tag editor & bulk rename (L)
- Edit tags on existing files (mutagen or FFmpeg remux), multi-file selection, Filename→Tags (parse with a reversed template), Tags→Filename/Folder rename, cover art (Discogs `images[0]`) embed, playlist/`.m3u` generation.
- Do last; evaluate whether mutagen is worth the new dependency versus FFmpeg-only rewrites.

## Cross-cutting
- Keep the API additive; `/api/start` behaviour without new fields is unchanged.
- Update `AGENTS.md` API table and `README.md` as each phase lands.
- New env vars: `DISCOGS_TOKEN`; compose needs outbound network (default bridge is fine).
- Security: all paths through `resolve_recording_path()`, escape metadata in the UI (`textContent`, not `innerHTML`), never log the token.

## Suggested delivery order
1 → 2 (tagged, templated recordings) → 3 (waveform) → 4 (Discogs) → 5 → 6 → 7.
