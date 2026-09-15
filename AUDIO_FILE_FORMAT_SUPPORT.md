## Subprocess Audio Piping & Multi-Format Integration

This implementation updates process management variables and refactors `start_arecord` and `stop_arecord` to pipe raw PCM streams from `arecord` directly into `ffmpeg` for on-the-fly encoding into formats like AIFF, WAV, FLAC, MP3, and AAC without intermediate files.

### Key Implementation Details
* **Process Management:** Tracks both `arecord_process` and `ffmpeg_process` workers.
* **Format Mapping:** Configures codec mappings and links `subprocess.Popen` via OS pipes.
* **Buffer Stability:** Utilizes thread queue sizing to prevent inter-process audio buffering artifacts.
