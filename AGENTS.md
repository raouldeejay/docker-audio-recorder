# AGENTS.md

Agent instructions for **docker-audio-recorder** (repo root). This project is small and single-package; this file applies to the whole repository.

## What this project is

A Flask web app that records audio from ALSA capture devices (typically on a Raspberry Pi) inside Docker. The UI in `templates/index.html` starts/stops recording, selects capture cards / input source, and monitors a live PCM stream.

Primary runtime surface: `app.py` + `templates/index.html`, packaged by `Dockerfile` / `docker-compose.yml`.

## Layout

| Path | Role |
|------|------|
| `app.py` | Flask app, ALSA discovery, recording, stream router |
| `templates/index.html` | Web UI + client JS |
| `Dockerfile` | Python 3.11-slim image with ALSA, FFmpeg, PortAudio |
| `docker-compose.yml` | Compose service (profiles `rpi4`, `rpi5`) |
| `requirements.txt` | Flask, Werkzeug, PyAudio |
| `AUDIO_*.md` | Design / blueprint notes — may diverge from current code |
| `recordings/` | Host-side recordings (gitignored); container path `/app/recordings` |

## Stack & conventions

- Python 3.11, Flask 3.x, ALSA via `arecord` / `amixer` subprocesses (not PyAudio for capture in current `app.py`).
- Hardware caps are read from sysfs mounted at `/mnt/asound` (host `/proc/asound`).
- Prefer small, focused changes in `app.py` / `templates/index.html`. Do not invent npm/test tooling — none exists.
- Design docs (`AUDIO_MONITORING_STREAM.md`, `AUDIO_LOUDNESS_RADAR.md`, `AUDIO_FILE_FORMAT_SUPPORT.md`) describe target architectures (FFmpeg multiplex, radar UI, multi-format encode). Treat them as blueprints; verify against live code before implementing.

## Current audio behavior (important)

1. On boot: `init_card()` → `init_continuous_audio_engine()` starts one `arecord` raw-PCM process.
2. A router thread fans PCM out to (a) monitor queues and (b) FFmpeg recorder stdin when recording.
3. Recording: `start_recording()` starts FFmpeg → WAV under `/app/recordings/`. Stopping closes FFmpeg only; `arecord` keeps running for monitor.
4. Monitor: the UI plays `/stream.wav` (capture-format PCM) and `/stream16.wav` (16-bit PCM, low bandwidth) through a Web Audio jitter buffer (AudioWorklet, ScriptProcessor fallback on plain HTTP). `/stream.aac` and `/low_stream.aac` (AAC-ADTS via FFmpeg) remain for legacy clients but are not used by the UI — `<audio>` buffering made them laggy.
5. Do not start a second `arecord` for recording — ALSA `hw:` is exclusive and would kill the monitor.

## Build & run

Requires Docker, ALSA devices, and typically a Raspberry Pi profile:

```bash
# From repo root — compose uses profiles; pick the matching one
docker compose --profile rpi5 up --build
# or
docker compose --profile rpi4 up --build
```

Web UI: `http://localhost:5000` (or `http://<pi-ip>:5000`).

Local (non-Docker) run only works where ALSA tools and devices exist:

```bash
pip install -r requirements.txt
python app.py
```

List host capture devices:

```bash
arecord -l
```

## Compose / deploy notes

- `docker-compose.yml` currently builds from the GitHub URL context, not the local tree — local edits may not appear until pushed, or until `build.context` is switched to `.`.
- Recordings volume default: `/mnt/data/audio-recorder` → `/app/recordings`.
- Service needs `/dev/snd`, `audio` group, and read-only `/proc/asound` → `/mnt/asound`.

## API surface (for UI / backend changes)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | UI |
| GET | `/api/status` | `{ recording: bool }` |
| POST | `/api/start` | Start WAV recording (`filename`, `samplerate`, `bitdepth`) |
| POST | `/api/stop` | Stop recording |
| GET/POST | `/api/input` | Line / IEC958 In selector |
| GET | `/api/caps` | Card bitdepths, rates, channels |
| GET | `/api/cards` | Capture card list |
| POST | `/api/select_card` | Select card/device |
| GET | `/api/logs` | Recent in-memory debug logs |
| GET | `/stream.wav`, `/stream16.wav` | Live PCM monitor streams (native / 16-bit) |
| GET | `/stream.aac`, `/low_stream.aac` | Legacy AAC monitor byte streams |

## Constraints

- No automated test suite, linter config, or CI in-repo — validate with container logs and manual UI checks.
- Do not commit recordings, secrets, or `.env` files.
- Avoid claiming multi-format FFmpeg recording or radar UI as implemented unless the code actually has it.
- Prefer updating design docs when intentionally changing architecture so they stay aligned with `app.py`.
