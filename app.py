import os
import struct
import subprocess
import signal
import re
import datetime
import threading
import time
import math
import json
import socket
import fcntl
import queue as queue_module
from collections import deque

from pathlib import Path

from flask import Flask, render_template, request, jsonify, Response
from werkzeug.utils import secure_filename

app = Flask(__name__)

# Global capture / recorder state
arecord_process = None
wav_recorder = None  # bit-perfect WavRecorder (not FFmpeg — avoids requantization)
# Disk writes run on a side thread so monitor/network never stalls arecord reads.
record_write_queue = None
record_writer_thread = None
record_writer_stop = threading.Event()
# Serializes capture (re)starts against recording start so a monitor request can
# never restart arecord underneath a recording.
engine_lock = threading.RLock()
# Temporary A/B: record with standalone `arecord -t wav` (no monitor pipe/fan-out).
RECORD_ONLY_MODE = False
direct_record_process = None
direct_record_path = None

RECORDINGS_ROOT = "/app/recordings/"

# Ensure directories are initialized immediately on boot
Path(RECORDINGS_ROOT).mkdir(parents=True, exist_ok=True)

cards = []
selected_card = None
selected_device = None
selected_name = None
card = None
device = None
name = None
bitdepth = None
samplerate = None
input_source = None
selector_numid = None
channels = None

hwcaps = {
    "card": None,
    "device": None,
    "name": None,
    "bitdepths": None,
    "samplerates": None,
    "channels": None,
}

HOSTNAME = socket.gethostname()

# Per-client monitor queues. A shared queue made multi-device listening warble
# (each consumer stole alternating PCM chunks).
monitor_listeners_lock = threading.Lock()
monitor_listeners = []  # list[deque]
# Raw bytes from arecord — dedicated reader thread; fan-out must not back-pressure ALSA.
# Unbounded: dropping here would glitch recordings; memory only grows if distributor stalls.
capture_byte_queue = queue_module.Queue()
audio_reader_thread = None
audio_router_thread = None
arecord_stderr_thread = None
is_recording = False
recorder_log = []
# Leftover PCM bytes when a read is not a multiple of the frame size (e.g. 24-bit).
pcm_read_leftover = b""
# ~25 ms chunks: 80 ≈ 2 s. A longer backlog turns a Wi‑Fi stall into seconds of lag.
MONITOR_QUEUE_MAXLEN = 80
# Straight `arecord -t wav file` never blocks on a slow consumer. Pipe+Python can —
# large pipe/ALSA buffer + a drain-only reader avoid overrun warble.
CAPTURE_PIPE_BYTES = 1024 * 1024
CAPTURE_READ_SIZE = 65536
ALSA_BUFFER_TIME_US = 500000
ALSA_PERIOD_TIME_US = 25000

# Low-latency local monitor → PipeWire sink (source rate/bit depth; 24-bit as s32).
PIPEWIRE_SINK = os.environ.get("PIPEWIRE_SINK", "input.web_monitor")
PIPEWIRE_LATENCY = os.environ.get("PIPEWIRE_LATENCY", "20ms")
SINK_QUEUE_MAXLEN = 8  # ~drop rather than add delay if pw-cat falls behind
sink_monitor_enabled = False
sink_pcm_queue = None
sink_monitor_proc = None
sink_feeder_thread = None
sink_monitor_stop = threading.Event()

# Low-rate peak meter for UI VU (updated on the router; served via SSE).
levels_lock = threading.Lock()
levels_state = {
    "peak_l": 0.0,
    "peak_r": 0.0,
    "hold_l": 0.0,
    "hold_r": 0.0,
    "peak_db_l": -120.0,
    "peak_db_r": -120.0,
    "hold_db_l": -120.0,
    "hold_db_r": -120.0,
    "clip_l": False,
    "clip_r": False,
}
LEVELS_HOLD_DECAY = 0.96  # per chunk toward current peak
LEVELS_CLIP_THRESH = 0.99
_levels_last_ts = 0.0
LEVELS_MIN_INTERVAL_S = 0.04  # ≥25 Hz is enough for VU; keep router light


def _log(msg):
    """Log to both console and in-memory buffer for debugging."""
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    log_msg = f"[{timestamp}] {msg}"
    print(log_msg)
    recorder_log.append(log_msg)
    if len(recorder_log) > 100:
        recorder_log.pop(0)


def _register_monitor_listener():
    """Create a private PCM queue for one HTTP stream client."""
    bucket = deque(maxlen=MONITOR_QUEUE_MAXLEN)
    with monitor_listeners_lock:
        monitor_listeners.append(bucket)
        n = len(monitor_listeners)
    _log(f"Monitor listener registered ({n} active)")
    return bucket


def _unregister_monitor_listener(bucket):
    with monitor_listeners_lock:
        try:
            monitor_listeners.remove(bucket)
        except ValueError:
            pass
        n = len(monitor_listeners)
    _log(f"Monitor listener removed ({n} active)")


def _flush_monitor_queues():
    global pcm_read_leftover
    with monitor_listeners_lock:
        for bucket in monitor_listeners:
            bucket.clear()
    pcm_read_leftover = b""
    if sink_pcm_queue is not None:
        sink_pcm_queue.clear()
    while True:
        try:
            capture_byte_queue.get_nowait()
        except queue_module.Empty:
            break


def _pcm_to_pipewire_raw(chunk, depth):
    """Map capture PCM to pw-cat --raw formats (s16 or s32 only)."""
    if not chunk:
        return chunk
    depth = int(depth or 16)
    if depth == 16:
        return chunk
    if depth == 32:
        return chunk
    if depth == 24:
        # S24_3LE → S32_LE with sign-extended high byte (source resolution preserved).
        n = len(chunk) // 3
        out = bytearray(n * 4)
        j = 0
        for i in range(0, n * 3, 3):
            b0, b1, b2 = chunk[i], chunk[i + 1], chunk[i + 2]
            out[j] = b0
            out[j + 1] = b1
            out[j + 2] = b2
            out[j + 3] = 0xFF if (b2 & 0x80) else 0x00
            j += 4
        return bytes(out)
    if depth == 8:
        out = bytearray(len(chunk) * 2)
        for i, b in enumerate(chunk):
            v = (b - 128) << 8
            out[i * 2] = v & 0xFF
            out[i * 2 + 1] = (v >> 8) & 0xFF
        return bytes(out)
    return chunk


def _sink_feeder_loop(proc, q, stop_event, depth):
    """Feed capture PCM into pw-cat stdin; never block the router."""
    while not stop_event.is_set() and proc.poll() is None:
        chunk = None
        if q:
            try:
                chunk = q.popleft()
            except IndexError:
                chunk = None
        if not chunk:
            time.sleep(0.001)
            continue
        try:
            proc.stdin.write(_pcm_to_pipewire_raw(chunk, depth))
        except (BrokenPipeError, ValueError, OSError):
            break
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
    except Exception:
        pass


def start_sink_monitor(target=None):
    """Play capture PCM to a PipeWire sink at source rate (low latency)."""
    global sink_monitor_enabled, sink_pcm_queue, sink_monitor_proc
    global sink_feeder_thread, sink_monitor_stop, samplerate, bitdepth

    if sink_monitor_enabled and sink_monitor_proc and sink_monitor_proc.poll() is None:
        return {
            "status": "ok",
            "enabled": True,
            "sink": target or PIPEWIRE_SINK,
            "message": "already running",
        }

    stop_sink_monitor()

    rate = int(samplerate or 48000)
    depth = int(bitdepth or 16)
    if not arecord_process or arecord_process.poll() is not None:
        init_continuous_audio_engine(rate, depth)
    if not arecord_process or arecord_process.poll() is not None:
        return {"status": "error", "message": "Audio engine not running"}

    rate = int(samplerate or rate)
    depth = int(bitdepth or depth)
    ch = _active_channel_count()
    sink = (target or PIPEWIRE_SINK or "").strip() or "input.web_monitor"
    pw_fmt = "s16" if depth <= 16 else "s32"

    cmd = [
        "pw-cat",
        "--playback",
        "--raw",
        "--format", pw_fmt,
        "--rate", str(rate),
        "--channels", str(ch),
        "--latency", PIPEWIRE_LATENCY,
        "--target", sink,
        "-",
    ]
    _log(f"PipeWire sink monitor: {' '.join(cmd)}")
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env={**os.environ, "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", "/run/user/1000")},
            bufsize=0,
        )
    except FileNotFoundError:
        return {"status": "error", "message": "pw-cat not found (install pipewire-bin)"}
    except Exception as e:
        return {"status": "error", "message": f"Failed to start pw-cat: {e}"}

    time.sleep(0.05)
    if proc.poll() is not None:
        err = b""
        try:
            err = proc.stderr.read() or b""
        except Exception:
            pass
        return {
            "status": "error",
            "message": f"pw-cat exited: {err.decode('utf-8', errors='replace').strip() or proc.returncode}",
        }

    threading.Thread(target=_arecord_stderr_loop, args=(proc,), daemon=True).start()

    sink_pcm_queue = deque(maxlen=SINK_QUEUE_MAXLEN)
    sink_monitor_stop = threading.Event()
    sink_monitor_proc = proc
    sink_feeder_thread = threading.Thread(
        target=_sink_feeder_loop,
        args=(proc, sink_pcm_queue, sink_monitor_stop, depth),
        daemon=True,
    )
    sink_feeder_thread.start()
    sink_monitor_enabled = True
    _log(f"PipeWire sink monitor started → {sink} ({pw_fmt}, {rate}Hz, {ch}ch, latency={PIPEWIRE_LATENCY})")
    return {
        "status": "ok",
        "enabled": True,
        "sink": sink,
        "format": pw_fmt,
        "samplerate": rate,
        "bitdepth": depth,
        "channels": ch,
        "latency": PIPEWIRE_LATENCY,
    }


def stop_sink_monitor():
    """Stop PipeWire sink playback."""
    global sink_monitor_enabled, sink_pcm_queue, sink_monitor_proc, sink_feeder_thread

    sink_monitor_enabled = False
    sink_monitor_stop.set()
    q = sink_pcm_queue
    sink_pcm_queue = None
    if q is not None:
        q.clear()

    proc = sink_monitor_proc
    sink_monitor_proc = None
    if proc and proc.poll() is None:
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=1)
            except Exception:
                pass

    thr = sink_feeder_thread
    sink_feeder_thread = None
    if thr is not None:
        thr.join(timeout=2)
    _log("PipeWire sink monitor stopped")
    return {"status": "ok", "enabled": False}


def _record_writer_loop(recorder, q, stop_event):
    """Drain PCM to disk off the arecord thread so I/O cannot overrun capture."""
    while True:
        if stop_event.is_set() and q.empty():
            break
        try:
            chunk = q.get(timeout=0.1)
        except queue_module.Empty:
            continue
        if chunk is None:
            break
        try:
            recorder.write(chunk)
        except Exception as e:
            _log(f"Recorder writer error: {e}")
            break


def _arecord_stderr_loop(proc):
    """Surface ALSA overruns — the usual cause of warble vs straight arecord→wav."""
    try:
        for line in iter(proc.stderr.readline, b""):
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            low = text.lower()
            if "overrun" in low or "error" in low or "fail" in low:
                _log(f"arecord: {text}")
    except Exception:
        pass


def _capture_reader_loop():
    """Drain arecord stdout as fast as possible — no fan-out, no sleep, no disk."""
    global arecord_process

    while True:
        proc = arecord_process
        if not proc or proc.poll() is not None or not proc.stdout:
            time.sleep(0.05)
            continue
        try:
            chunk = proc.stdout.read(CAPTURE_READ_SIZE)
        except Exception as e:
            _log(f"Capture read error: {e}")
            time.sleep(0.05)
            continue
        if proc is not arecord_process:
            continue
        if not chunk:
            time.sleep(0.05)
            continue
        capture_byte_queue.put(chunk)


def _audio_router_loop():
    """Align frames; recording prioritized, monitors best-effort."""
    global pcm_read_leftover

    while True:
        depth = bitdepth or 16
        ch = int(channels or hwcaps.get("channels") or 2)
        fb = max(1, ch * (depth // 8))

        try:
            raw = capture_byte_queue.get(timeout=0.2)
        except queue_module.Empty:
            continue

        chunk = raw
        if pcm_read_leftover:
            chunk = pcm_read_leftover + chunk
            pcm_read_leftover = b""

        if fb > 1 and len(chunk) % fb:
            keep = len(chunk) % fb
            pcm_read_leftover = chunk[-keep:]
            chunk = chunk[:-keep]

        if not chunk:
            continue

        if is_recording and record_write_queue is not None:
            try:
                record_write_queue.put(chunk)
            except Exception as e:
                _log(f"Recorder queue error: {e}")

        # Monitor work is best-effort: nothing here may ever stop the router (and
        # with it the recorder feed).
        try:
            q = sink_pcm_queue
            if sink_monitor_enabled and q is not None:
                q.append(chunk)

            with monitor_listeners_lock:
                listeners = list(monitor_listeners)
            for bucket in listeners:
                bucket.append(chunk)

            _update_levels_from_pcm(chunk, depth, ch)
        except Exception as e:
            _log(f"Monitor fan-out error (recording unaffected): {e}")


def _lin_to_db(x):
    if x <= 1e-9:
        return -120.0
    return 20.0 * math.log10(x)


def _update_levels_from_pcm(chunk, depth, ch):
    """Compute per-channel peak (0..1) + decaying hold for VU meters."""
    global _levels_last_ts
    if not chunk or ch < 1:
        return
    now = time.monotonic()
    if now - _levels_last_ts < LEVELS_MIN_INTERVAL_S:
        return
    _levels_last_ts = now

    depth = int(depth or 16)
    ch = int(ch)
    width = max(1, depth // 8)
    frame = width * ch
    if frame < 1 or len(chunk) < frame:
        return
    full_scale = float(1 << (depth - 1))
    peaks = [0] * ch

    # Coarse scan — VU only needs approximate peaks.
    nframes = len(chunk) // frame
    step = max(1, nframes // 128)
    for fi in range(0, nframes, step):
        base = fi * frame
        for c in range(min(ch, 2)):
            off = base + c * width
            if depth == 16:
                v = struct.unpack_from("<h", chunk, off)[0]
            elif depth == 24:
                b0, b1, b2 = chunk[off], chunk[off + 1], chunk[off + 2]
                v = b0 | (b1 << 8) | (b2 << 16)
                if v & 0x800000:
                    v -= 0x1000000
            elif depth == 32:
                v = struct.unpack_from("<i", chunk, off)[0]
            else:
                v = (chunk[off] - 128) << 8
            a = -v if v < 0 else v
            if a > peaks[c]:
                peaks[c] = a

    pl = min(1.0, peaks[0] / full_scale) if ch >= 1 else 0.0
    pr = min(1.0, peaks[1] / full_scale) if ch >= 2 else pl

    if not levels_lock.acquire(blocking=False):
        return
    try:
        hl = levels_state["hold_l"]
        hr = levels_state["hold_r"]
        if pl >= hl:
            hl = pl
        else:
            hl *= LEVELS_HOLD_DECAY
            if pl > hl:
                hl = pl
        if pr >= hr:
            hr = pr
        else:
            hr *= LEVELS_HOLD_DECAY
            if pr > hr:
                hr = pr
        levels_state["peak_l"] = pl
        levels_state["peak_r"] = pr
        levels_state["hold_l"] = hl
        levels_state["hold_r"] = hr
        levels_state["peak_db_l"] = round(_lin_to_db(pl), 1)
        levels_state["peak_db_r"] = round(_lin_to_db(pr), 1)
        levels_state["hold_db_l"] = round(_lin_to_db(hl), 1)
        levels_state["hold_db_r"] = round(_lin_to_db(hr), 1)
        levels_state["clip_l"] = pl >= LEVELS_CLIP_THRESH
        levels_state["clip_r"] = pr >= LEVELS_CLIP_THRESH
    finally:
        levels_lock.release()


# ------------------------------------------------------------
# ALSA CARD DETECTION
# ------------------------------------------------------------

def list_capture_cards():
    """Returns a list of dicts: [{"card": 1, "device": 0, "name": "U24XL"}, ...]."""
    global cards

    if not cards:
        try:
            output = subprocess.check_output(["arecord", "-l"], text=True)
        except subprocess.CalledProcessError as e:
            _log(f"Failed to list cards: {e}")
            return []

        current_card = None

        for line in output.splitlines():
            m = re.search(r"card (\d+): ([^[]+)\[([^\]]+)\]", line)
            if m:
                current_card = {"card": int(m.group(1)), "name": m.group(3).strip()}

            d = re.search(r"device (\d+): ([^[]+)\[([^\]]+)\]", line)
            if d and current_card:
                current_card["device"] = int(d.group(1))
                cards.append(current_card)
                current_card = None

    return cards


def detect_capture_card():
    global selected_card, selected_device, selected_name
    if selected_card is None:
        discovered_cards = list_capture_cards()
        if not discovered_cards:
            _log("No capture cards detected")
            return None, None, None
        c = discovered_cards[0]
        selected_card = c["card"]
        selected_device = c["device"]
        selected_name = c["name"]
        _log(f"Auto-selected card: {selected_name} (card {selected_card}, device {selected_device})")
        set_card()

    return selected_card, selected_device, selected_name


# ------------------------------------------------------------
# HW PARAMS (CHANNELS / FORMAT / RATE)
# ------------------------------------------------------------
def get_hwcaps_non_exclusive(card_index, device_index):
    global hwcaps, channels

    id_file = Path(f"/mnt/asound/card{card_index}/id")
    stream_file = Path(f"/mnt/asound/card{card_index}/stream0")

    if id_file.exists():
        hwcaps["name"] = id_file.read_text().strip()

    if stream_file.exists():
        content = stream_file.read_text()
        sections = re.split(r'^(Playback|Capture):', content, flags=re.MULTILINE)
        capture_block = ""
        current_mode = None

        for item in sections:
            if item in ["Playback", "Capture"]:
                current_mode = item
                continue
            if current_mode == "Capture":
                capture_block = item
                break

        if capture_block:
            bits_found = re.findall(r'Bits:\s*(\d+)', capture_block)
            rates_lines = re.findall(r'Rates:\s*(.+)', capture_block)
            channels_found = re.findall(r'Channels:\s*(\d+)', capture_block)

            bitdepths = sorted({int(b) for b in bits_found})
            samplerates = []
            for line in rates_lines:
                samplerates.extend(int(r.strip()) for r in line.split(',') if r.strip().isdigit())
            samplerates = sorted(set(samplerates))

            channels = max((int(c) for c in channels_found), default=2)
            hwcaps["bitdepths"] = bitdepths or [16]
            hwcaps["samplerates"] = samplerates or [48000]
            hwcaps["channels"] = channels
    else:
        # Fallback defaults if sysfs doesn't exist
        hwcaps["bitdepths"] = [16, 24]
        hwcaps["samplerates"] = [48000]
        hwcaps["channels"] = 2

    return hwcaps


def detect_input_selector(card):
    """Returns numid of an ENUMERATED control with items ['Line', 'IEC958 In'] or None if not present."""
    try:
        output = subprocess.check_output(["amixer", "-c", str(card), "contents"], text=True)
    except subprocess.CalledProcessError:
        return None

    blocks = output.split("numid=")[1:]
    for block in blocks:
        if "ENUMERATED" in block and "Line" in block and "IEC958 In" in block:
            try:
                numid = int(block.split(",")[0])
                return numid
            except (ValueError, IndexError):
                pass
    return None


def get_input_source(card, numid):
    """Read PCM Capture Source from amixer. Item #0=Line, Item #1=IEC958 In."""
    try:
        output = subprocess.check_output(
            ["amixer", "-c", str(card), "cget", f"numid={numid}"], text=True
        )
    except subprocess.CalledProcessError:
        return "Line"

    # Match the value line only — metadata also contains "values=1" (value count).
    for line in output.splitlines():
        s = line.strip()
        if s.startswith(": values="):
            raw = s.split("=", 1)[1].split(",")[0].strip()
            return "Line" if raw == "0" else "IEC958 In"
    return "Line"


def set_input_source(card, numid, source):
    global input_source
    value = 0 if source == "Line" else 1
    try:
        subprocess.check_call(
            ["amixer", "-c", str(card), "cset", f"numid={numid}", str(value)]
        )
        input_source = get_input_source(card, numid)
        _log(f"Set input source to {input_source} (requested {source})")
    except subprocess.CalledProcessError as e:
        _log(f"Failed to set input source: {e}")
        raise


# ------------------------------------------------------------
# FILENAME GENERATION
# ------------------------------------------------------------

def generate_filename(ext="aiff"):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    ext = (ext or "aiff").lstrip(".").lower()
    if ext == "aif":
        ext = "aiff"
    return f"recording_{ts}.{ext}"


def clean_recording_filename(filename, file_format):
    """Return a safe, canonical recording filename and its format."""
    filename = filename.strip()
    if not filename:
        ext = "aiff" if file_format == "aiff" else "wav"
        return generate_filename(ext), file_format

    lower = filename.lower()
    if lower.endswith(".aif"):
        filename = filename[:-4]
        file_format = "aiff"
    elif lower.endswith(".aiff"):
        filename = filename[:-5]
        file_format = "aiff"
    elif lower.endswith(".wav"):
        filename = filename[:-4]
        file_format = "wav"

    ext = "aiff" if file_format == "aiff" else "wav"
    # Each "/" separated part is sanitized on its own, so "." / ".." can never survive.
    parts = [secure_filename(p) for p in re.split(r"[\\/]+", filename)]
    parts = [p for p in parts if p]
    if not parts:
        raise ValueError("Filename must contain at least one letter or number")
    if len(parts) > 8:
        raise ValueError("Too many folder levels (maximum 7)")
    if any(len(p) > 255 - len(ext) - 1 for p in parts):
        raise ValueError("Filename is too long (maximum 255 characters per part)")

    return "/".join(parts[:-1] + [f"{parts[-1]}.{ext}"]), file_format


def resolve_recording_path(relname):
    """Create parent folders under RECORDINGS_ROOT and return a collision-free relative name.

    Existing files are never overwritten (suffix _1, _2, ...). A folder part that
    clashes with an existing file gets a suffix too.
    """
    root = os.path.realpath(RECORDINGS_ROOT)
    parts = relname.split("/")
    current = root
    resolved = []
    for part in parts[:-1]:
        candidate, n = part, 0
        while os.path.exists(os.path.join(current, candidate)) and not os.path.isdir(
            os.path.join(current, candidate)
        ):
            n += 1
            candidate = f"{part}_{n}"
        current = os.path.join(current, candidate)
        resolved.append(candidate)
    if os.path.commonpath([root, os.path.realpath(current)]) != root:
        raise ValueError("Invalid recording path")
    os.makedirs(current, exist_ok=True)

    stem, ext = os.path.splitext(parts[-1])
    candidate, n = parts[-1], 0
    while os.path.exists(os.path.join(current, candidate)):
        n += 1
        candidate = f"{stem}_{n}{ext}"
    resolved.append(candidate)
    return "/".join(resolved)


def get_audio_formats(bitdepth):
    if bitdepth == 8:
        return "U8", "u8"
    elif bitdepth == 16:
        return "S16_LE", "s16le"
    elif bitdepth == 24:
        return "S24_3LE", "s24le"
    elif bitdepth == 32:
        return "S32_LE", "s32le"
    else:
        raise ValueError(f"Unsupported bit depth: {bitdepth}")


def _normalize_file_format(fmt):
    fmt = (fmt or "aiff").strip().lower().lstrip(".")
    if fmt == "wav":
        return "wav"
    return "aiff"


# ------------------------------------------------------------
# RECORDING — PCM file from shared arecord (monitor stays alive)
# ------------------------------------------------------------
def _active_channel_count():
    return int(hwcaps.get("channels") or channels or 2)


def _wav_header(rate, ch, depth, data_bytes=0x7FFFF000):
    """Classic PCM WAV header (fmt=1). Large data_bytes keeps live HTTP streams open."""
    block_align = ch * (depth // 8)
    byte_rate = rate * block_align
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_bytes,
        b"WAVE",
        b"fmt ",
        16,  # PCM fmt chunk size
        1,   # PCM format
        ch,
        rate,
        byte_rate,
        block_align,
        depth,
        b"data",
        data_bytes,
    )


def _pcm_le_to_be(pcm, depth):
    """Byte-swap little-endian capture PCM to big-endian (AIFF)."""
    width = max(1, int(depth) // 8)
    if width == 1 or not pcm:
        return pcm
    out = bytearray(len(pcm))
    aligned = (len(pcm) // width) * width
    # Strided slice copy per byte lane: exact swap, no per-sample Python loop (GIL).
    for lane in range(width):
        out[lane:aligned:width] = pcm[width - 1 - lane:aligned:width]
    if aligned < len(pcm):
        out[aligned:] = pcm[aligned:]
    return bytes(out)


def _aiff_rate_extended(sample_rate):
    """80-bit SANE/AIFF extended float for sample rate (big-endian)."""
    import math

    rate = float(sample_rate)
    if rate <= 0:
        return b"\x00" * 10
    exp = int(math.floor(math.log(rate, 2)))
    mantissa = int(rate * (2.0 ** (63 - exp)))
    exp = exp + 16383
    return struct.pack(">HQ", exp & 0xFFFF, mantissa & 0xFFFFFFFFFFFFFFFF)


def _aiff_header(rate, ch, depth, data_bytes=0):
    """
    Classic AIFF (FORM/COMM/SSND). PCM is big-endian.
    Metadata chunks (NAME/AUTH/ANNO) can be inserted before SSND later.
    """
    ch = int(ch)
    depth = int(depth)
    block_align = ch * (depth // 8)
    frames = (data_bytes // block_align) if block_align else 0
    # COMM: ckSize(4) + numChannels(2) + numSampleFrames(4) + sampleSize(2) + rate(10)
    comm = struct.pack(">LHLH", 18, ch, frames, depth) + _aiff_rate_extended(rate)
    # SSND: chunk size includes 8 bytes offset/blockSize + PCM
    ssnd_data_size = 8 + data_bytes
    # FORM size = file size - 8
    form_size = 4 + (4 + len(comm)) + (8 + ssnd_data_size)
    return (
        b"FORM"
        + struct.pack(">I", form_size)
        + b"AIFF"
        + b"COMM"
        + comm
        + b"SSND"
        + struct.pack(">III", ssnd_data_size, 0, 0)  # size, offset, blockSize
    )


class WavRecorder:
    """Write capture PCM into a classic PCM WAV (fmt=1) with no codec conversion."""

    format_name = "wav"

    def __init__(self, path, rate, channels, depth):
        self.path = path
        self.rate = int(rate)
        self.channels = int(channels)
        self.depth = int(depth)
        self.data_bytes = 0
        self._lock = threading.Lock()
        # 1MiB buffer — writer thread batches; avoid tiny syscalls on the capture path.
        self._fh = open(path, "wb", buffering=1024 * 1024)
        # Placeholder sizes; patched in close().
        self._fh.write(_wav_header(self.rate, self.channels, self.depth, data_bytes=0))

    def write(self, pcm):
        if not pcm:
            return
        with self._lock:
            self._fh.write(pcm)
            self.data_bytes += len(pcm)

    def close(self):
        with self._lock:
            if self._fh.closed:
                return self.data_bytes
            self._fh.seek(0)
            self._fh.write(
                _wav_header(self.rate, self.channels, self.depth, data_bytes=self.data_bytes)
            )
            self._fh.flush()
            self._fh.close()
            return self.data_bytes


class AiffRecorder:
    """Write capture PCM as classic AIFF (big-endian PCM). Hook point for metadata later."""

    format_name = "aiff"

    def __init__(self, path, rate, channels, depth):
        self.path = path
        self.rate = int(rate)
        self.channels = int(channels)
        self.depth = int(depth)
        self.data_bytes = 0
        self._lock = threading.Lock()
        # Future: NAME / AUTH / ANNO / COMT chunks before SSND for tags.
        self.metadata = {}
        self._fh = open(path, "wb", buffering=1024 * 1024)
        self._fh.write(_aiff_header(self.rate, self.channels, self.depth, data_bytes=0))

    def write(self, pcm):
        if not pcm:
            return
        be = _pcm_le_to_be(pcm, self.depth)
        with self._lock:
            self._fh.write(be)
            self.data_bytes += len(be)

    def close(self):
        with self._lock:
            if self._fh.closed:
                return self.data_bytes
            # Rewrite header with final sizes. Metadata injection can splice chunks here later.
            self._fh.seek(0)
            header = _aiff_header(
                self.rate, self.channels, self.depth, data_bytes=self.data_bytes
            )
            self._fh.write(header)
            self._fh.flush()
            self._fh.close()
            return self.data_bytes


def _recording_active():
    if RECORD_ONLY_MODE:
        return bool(
            is_recording
            and direct_record_process is not None
            and direct_record_process.poll() is None
        )
    return bool(is_recording and wav_recorder is not None)


def _stop_capture_process_only():
    """Stop shared monitor arecord without touching an in-progress file recorder."""
    global arecord_process
    old = arecord_process
    arecord_process = None
    if old and old.poll() is None:
        try:
            old.terminate()
            old.wait(timeout=2)
        except subprocess.TimeoutExpired:
            old.kill()
            old.wait()
        except Exception as e:
            _log(f"Error stopping stream arecord: {e}")
    _flush_monitor_queues()


def start_recording(filename, file_format="aiff"):
    """Start PCM file recording from shared arecord (WAV or AIFF)."""
    global wav_recorder, samplerate, channels, is_recording, bitdepth
    global record_write_queue, record_writer_thread, record_writer_stop

    file_format = _normalize_file_format(file_format)

    # Direct arecord -t wav only supports WAV; AIFF always uses the shared path.
    if RECORD_ONLY_MODE and file_format == "wav":
        return _start_direct_arecord_wav(filename)

    if not arecord_process or arecord_process.poll() is not None:
        init_continuous_audio_engine(samplerate or 48000, bitdepth or 16)

    if not arecord_process or arecord_process.poll() is not None:
        return {"status": "error", "message": "Audio engine failed to start (no arecord)"}

    filepath = os.path.join(RECORDINGS_ROOT, filename)
    channels = _active_channel_count()

    try:
        if file_format == "aiff":
            wav_recorder = AiffRecorder(filepath, samplerate, channels, bitdepth)
        else:
            wav_recorder = WavRecorder(filepath, samplerate, channels, bitdepth)
    except Exception as e:
        wav_recorder = None
        _log(f"Failed to open recording file: {e}")
        return {"status": "error", "message": f"Failed to open file: {e}", "path": filepath}

    record_writer_stop = threading.Event()
    record_write_queue = queue_module.Queue()
    record_writer_thread = threading.Thread(
        target=_record_writer_loop,
        args=(wav_recorder, record_write_queue, record_writer_stop),
        daemon=True,
    )
    record_writer_thread.start()

    is_recording = True
    alsa_fmt, _ = get_audio_formats(bitdepth)
    label = "AIFF" if file_format == "aiff" else "WAV"
    _log(
        f"{label} recorder started ({alsa_fmt}, {samplerate}Hz, {channels}ch, {bitdepth}-bit) → {filepath}"
    )
    return {
        "status": "success",
        "message": f"Recording started ({label}, {alsa_fmt}, {samplerate}Hz, {channels}ch, {bitdepth}-bit)",
        "path": filepath,
        "format": file_format,
        "alsa_format": alsa_fmt,
        "samplerate": samplerate,
        "bitdepth": bitdepth,
        "channels": channels,
        "record_only": False,
    }


def _start_direct_arecord_wav(filename):
    """Standalone arecord→WAV (same as CLI). Stops monitor engine for exclusive hw:."""
    global direct_record_process, direct_record_path, is_recording
    global samplerate, bitdepth, channels

    if card is None or device is None:
        return {"status": "error", "message": "No capture card selected"}

    _log("RECORD_ONLY_MODE: stopping monitors, starting standalone arecord -t wav")
    _stop_capture_process_only()

    filepath = os.path.join(RECORDINGS_ROOT, filename)
    channels = _active_channel_count()
    rate = int(samplerate or 48000)
    depth = int(bitdepth or 16)
    alsa_fmt, _ = get_audio_formats(depth)
    device_string = f"hw:{card},{device}"

    arecord_cmd = [
        "arecord",
        "-D", device_string,
        "-f", alsa_fmt,
        "-r", str(rate),
        "-c", str(channels),
        "--buffer-time", str(ALSA_BUFFER_TIME_US),
        "--period-time", str(ALSA_PERIOD_TIME_US),
        "-t", "wav",
        filepath,
    ]
    _log(f"Direct record command: {' '.join(arecord_cmd)}")
    try:
        direct_record_process = subprocess.Popen(
            arecord_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except Exception as e:
        direct_record_process = None
        return {"status": "error", "message": f"Failed to start arecord: {e}"}

    threading.Thread(
        target=_arecord_stderr_loop, args=(direct_record_process,), daemon=True
    ).start()

    direct_record_path = filepath
    is_recording = True
    time.sleep(0.05)
    if direct_record_process.poll() is not None:
        is_recording = False
        direct_record_process = None
        return {"status": "error", "message": "arecord exited immediately", "path": filepath}

    _log(
        f"Direct arecord WAV started ({alsa_fmt}, {rate}Hz, {channels}ch, {depth}-bit) → {filepath}"
    )
    return {
        "status": "success",
        "message": "Recording started (direct arecord -t wav, monitors off)",
        "path": filepath,
        "alsa_format": alsa_fmt,
        "samplerate": rate,
        "bitdepth": depth,
        "channels": channels,
        "record_only": True,
    }


def stop_recording():
    """Stop WAV recording. RECORD_ONLY_MODE: SIGINT arecord so it finalizes the WAV."""
    global wav_recorder, is_recording
    global record_write_queue, record_writer_thread, record_writer_stop

    if RECORD_ONLY_MODE:
        return _stop_direct_arecord_wav()

    if not wav_recorder:
        return {"status": "error", "message": "No active recording found"}

    is_recording = False
    _log("Stopping recording (finalizing WAV)...")

    rec = wav_recorder
    q = record_write_queue
    thr = record_writer_thread
    stop_ev = record_writer_stop

    # Stop accepting new PCM, then drain the writer queue before closing the file.
    record_write_queue = None
    if stop_ev is not None:
        stop_ev.set()
    if q is not None:
        q.put(None)
    if thr is not None:
        thr.join(timeout=60)

    wav_recorder = None
    record_writer_thread = None
    record_writer_stop = threading.Event()

    try:
        nbytes = rec.close()
        _log(f"Recording saved ({nbytes} bytes PCM) → {rec.path}")
    except Exception as e:
        _log(f"Error finalizing recording: {e}")
        return {"status": "error", "message": f"Error saving recording: {e}"}

    _log("Recording stopped; monitor stream still active")
    return {"status": "success", "message": "Recording saved"}


def _stop_direct_arecord_wav():
    global direct_record_process, direct_record_path, is_recording

    if not direct_record_process and not is_recording:
        return {"status": "error", "message": "No active recording found"}

    is_recording = False
    proc = direct_record_process
    path = direct_record_path
    direct_record_process = None
    direct_record_path = None
    _log("Stopping direct arecord (finalizing WAV)...")

    if proc and proc.poll() is None:
        try:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        except Exception as e:
            _log(f"Error stopping direct arecord: {e}")
            try:
                proc.kill()
            except Exception:
                pass

    nbytes = 0
    if path and os.path.isfile(path):
        nbytes = max(0, os.path.getsize(path) - 44)
    _log(f"Direct recording saved ({nbytes} bytes PCM) → {path}")
    _log("Recording stopped; monitors remain off until you start a stream again")
    return {"status": "success", "message": "Recording saved (direct arecord)", "path": path}


def set_card():
    global selected_card, selected_device, selected_name, hwcaps
    if hwcaps["card"] != selected_card:
        hwcaps = get_hwcaps_non_exclusive(selected_card, selected_device)
        hwcaps["card"] = selected_card
        hwcaps["device"] = selected_device
        hwcaps["name"] = selected_name


def init_continuous_audio_engine(_samplerate=48000, _bitdepth=16):
    """Start arecord for live stream (shared source for monitor + recorder).

    While a file is being recorded the running capture is never restarted or
    reconfigured, whoever asks (monitor, warmup, sink, input change).
    """
    with engine_lock:
        if is_recording and wav_recorder is not None:
            if arecord_process and arecord_process.poll() is None:
                return
            # Capture died mid-recording: restart with the recording's own format.
            _samplerate = samplerate or _samplerate
            _bitdepth = bitdepth or _bitdepth
        _init_engine_unlocked(_samplerate, _bitdepth)


def _init_engine_unlocked(_samplerate=48000, _bitdepth=16):
    global arecord_process, card, device, samplerate, bitdepth, channels, hwcaps
    global audio_router_thread, audio_reader_thread, arecord_stderr_thread

    if card is None or device is None:
        _log("Cannot start audio engine: no card selected")
        return

    # Stop the old capture first and clear arecord_process so the router cannot
    # apply the new bit depth to leftover PCM from the previous format.
    old = arecord_process
    arecord_process = None
    if old and old.poll() is None:
        try:
            old.terminate()
            old.wait(timeout=2)
        except subprocess.TimeoutExpired:
            old.kill()
            old.wait()
        except Exception as e:
            _log(f"Error stopping previous arecord: {e}")

    _flush_monitor_queues()

    samplerate = _samplerate
    bitdepth = _bitdepth
    channel_count = int(hwcaps.get("channels") or channels or 2)
    channels = channel_count
    alsa_fmt, _ = get_audio_formats(bitdepth)
    device_string = f"hw:{card},{device}"

    # Single arecord: raw PCM on stdout for the reader thread to drain.
    # Larger ALSA buffer absorbs brief scheduling stalls (USB adaptive + Python).
    arecord_cmd = [
        "arecord",
        "-D", device_string,
        "-f", alsa_fmt,
        "-r", str(samplerate),
        "-c", str(channel_count),
        "--buffer-time", str(ALSA_BUFFER_TIME_US),
        "--period-time", str(ALSA_PERIOD_TIME_US),
        "-t", "raw",
        "-",
    ]

    _log(f"Stream arecord command: {' '.join(arecord_cmd)}")
    arecord_process = subprocess.Popen(
        arecord_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    try:
        fcntl.fcntl(arecord_process.stdout.fileno(), fcntl.F_SETPIPE_SZ, CAPTURE_PIPE_BYTES)
    except Exception as e:
        _log(f"Could not enlarge capture pipe: {e}")

    _log(f"Stream arecord started (PID {arecord_process.pid})")
    time.sleep(0.05)
    _log_capture_hw_params(expected_fmt=alsa_fmt, expected_rate=samplerate, expected_ch=channel_count)

    arecord_stderr_thread = threading.Thread(
        target=_arecord_stderr_loop, args=(arecord_process,), daemon=True
    )
    arecord_stderr_thread.start()

    if audio_reader_thread is None or not audio_reader_thread.is_alive():
        audio_reader_thread = threading.Thread(target=_capture_reader_loop, daemon=True)
        audio_reader_thread.start()
        _log("Capture reader thread started")

    if audio_router_thread is None or not audio_router_thread.is_alive():
        audio_router_thread = threading.Thread(target=_audio_router_loop, daemon=True)
        audio_router_thread.start()
        _log("Audio router thread started")

    _log("🚀 Audio engine initialized")


def _log_capture_hw_params(expected_fmt, expected_rate, expected_ch):
    """Confirm ALSA negotiated the requested format (S24_3LE = packed 24-bit LE)."""
    # Host /proc/asound is mounted at /mnt/asound in the container.
    path = f"/mnt/asound/card{card}/pcm{device}c/sub0/hw_params"
    try:
        text = Path(path).read_text()
    except Exception as e:
        _log(f"Could not read hw_params ({path}): {e}")
        return
    got = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            got[k.strip()] = v.strip()
    fmt = got.get("format", "?")
    rate = got.get("rate", "?")
    chs = got.get("channels", "?")
    _log(f"ALSA hw_params: format={fmt} rate={rate} channels={chs}")
    rate_num = rate.split()[0] if rate else ""
    if fmt != expected_fmt or rate_num != str(expected_rate) or chs != str(expected_ch):
        _log(
            f"WARNING: capture format mismatch — wanted {expected_fmt}/{expected_rate}Hz/{expected_ch}ch, "
            f"got {fmt}/{rate}/{chs}"
        )


def stop_continuous_audio_engine():
    """Stop the live stream arecord (and any active recorder)."""
    global arecord_process, is_recording

    _log("Stopping audio engine...")

    if sink_monitor_enabled:
        stop_sink_monitor()

    if _recording_active() or wav_recorder or direct_record_process:
        stop_recording()

    _stop_capture_process_only()
    is_recording = False
    _log("Audio engine stopped")


def init_card():
    global cards, card, device, name, input_source, selector_numid
    cards = list_capture_cards()
    card, device, name = detect_capture_card()

    input_source = None
    selector_numid = None

    if card is not None:
        selector_numid = detect_input_selector(card)
        if selector_numid is not None:
            input_source = get_input_source(card, selector_numid)


# ------------------------------------------------------------
# FLASK ROUTES
# ------------------------------------------------------------

@app.route("/")
def index():
    return render_template(
        "index.html",
        cards=list_capture_cards(),
        selected_card=card,
        selected_device=device,
        selected_name=name,
        input_source=input_source,
        selector_present=(selector_numid is not None),
        hostname=HOSTNAME,
    )


@app.route("/api/status")
def api_status():
    return jsonify({
        "recording": _recording_active(),
        "record_only": RECORD_ONLY_MODE,
        "sink_monitor": bool(
            sink_monitor_enabled
            and sink_monitor_proc is not None
            and sink_monitor_proc.poll() is None
        ),
        "sink": PIPEWIRE_SINK,
    })


def _ensure_monitor_engine(req_rate=None, req_depth=None):
    """Align capture with requested monitor/recording settings when not recording."""
    if _recording_active() or wav_recorder is not None:
        return

    rate = req_rate or samplerate or 48000
    depth = req_depth or bitdepth or 16
    running = arecord_process and arecord_process.poll() is None
    if not running or samplerate != rate or bitdepth != depth:
        init_continuous_audio_engine(rate, depth)


def _aac_stream_generator(bitrate_k, req_rate=None, req_depth=None):
    """Live AAC-ADTS monitor from capture PCM.

    Each HTTP client gets its own PCM queue so multiple devices can listen at once.
    FFmpeg ingests native capture format (e.g. s24le) — no Python downconvert in
    the hot path (that lagged feeders under multi-client load and caused warble).
    """
    if RECORD_ONLY_MODE and _recording_active():
        return
    _ensure_monitor_engine(req_rate, req_depth)
    depth = bitdepth or 16
    rate = samplerate or 48000
    ch = _active_channel_count()
    _, ffmpeg_fmt = get_audio_formats(depth)
    bytes_per_frame = max(1, ch * (depth // 8))
    bucket = _register_monitor_listener()

    # Low-latency live encode: skip probe/analyze, flush packets ASAP for mobile start.
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-fflags", "nobuffer",
        "-flags", "low_delay",
        "-probesize", "32",
        "-analyzeduration", "0",
        "-f", ffmpeg_fmt,
        "-ar", str(rate),
        "-ac", str(ch),
        "-i", "pipe:0",
        "-c:a", "aac",
        "-b:a", f"{int(bitrate_k)}k",
        "-profile:a", "aac_low",
        "-f", "adts",
        "-muxdelay", "0",
        "-muxpreload", "0",
        "-flush_packets", "1",
        "pipe:1",
    ]

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,  # PIPE can fill and stall FFmpeg
        bufsize=0,
    )
    stop = threading.Event()

    def _feed():
        idle = 0
        primed = False
        # Kick the AAC encoder immediately so the first ADTS frames leave sooner
        # (mobile players often wait on the first packet before starting).
        try:
            silence = b"\x00" * (bytes_per_frame * 1024)
            proc.stdin.write(silence)
            proc.stdin.flush()
            primed = True
        except (BrokenPipeError, ValueError, OSError):
            return
        while not stop.is_set() and proc.poll() is None:
            chunk = None
            if bucket:
                try:
                    chunk = bucket.popleft()
                except IndexError:
                    chunk = None
            if chunk:
                idle = 0
                try:
                    proc.stdin.write(chunk)
                    # Flush early packets so mobile gets ADTS quickly; then batch.
                    if primed:
                        proc.stdin.flush()
                        primed = False
                except (BrokenPipeError, ValueError, OSError):
                    break
            else:
                idle += 1
                # ~60s without PCM before tearing down (was ~10s — caused reconnect stalls).
                if idle > 6000:
                    break
                time.sleep(0.002 if idle < 50 else 0.01)
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:
            pass

    feeder = threading.Thread(target=_feed, daemon=True)
    feeder.start()
    try:
        # First reads smaller so we yield as soon as FFmpeg emits; then larger chunks.
        first = True
        while True:
            data = proc.stdout.read(1024 if first else 8192)
            if not data:
                break
            first = False
            yield data
    finally:
        stop.set()
        _unregister_monitor_listener(bucket)
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass
        feeder.join(timeout=2)


def _truncate_pcm_to_16(chunk, depth):
    """Keep the top 16 bits of each little-endian sample (byte slicing, no per-sample loop)."""
    if depth == 16:
        return chunk
    width = depth // 8
    if depth == 8:
        return chunk
    out = bytearray(len(chunk) // width * 2)
    out[0::2] = chunk[width - 2::width]
    out[1::2] = chunk[width - 1::width]
    return bytes(out)


def _wav_stream_generator(req_rate=None, req_depth=None, out_16bit=False):
    """Live PCM WAV monitor — same capture bytes as recording (bit-perfect).

    out_16bit sends the top 16 bits instead (~1.5 Mbps stereo @48k): no codec
    latency like AAC, but light enough for Wi-Fi/mobile.
    """
    if RECORD_ONLY_MODE and _recording_active():
        return
    _ensure_monitor_engine(req_rate, req_depth)
    depth = bitdepth or 16
    rate = samplerate or 48000
    ch = _active_channel_count()
    convert = out_16bit and depth in (24, 32)
    bucket = _register_monitor_listener()
    yield _wav_header(rate, ch, 16 if convert else depth, data_bytes=0x7FFFF000)
    idle = 0
    try:
        while True:
            chunk = None
            if bucket:
                try:
                    chunk = bucket.popleft()
                except IndexError:
                    chunk = None
            if chunk:
                idle = 0
                yield _truncate_pcm_to_16(chunk, depth) if convert else chunk
            else:
                idle += 1
                # Keep live WAV open through brief Wi‑Fi / scheduler gaps.
                if idle > 6000:
                    break
                time.sleep(0.002 if idle < 50 else 0.01)
    finally:
        _unregister_monitor_listener(bucket)


@app.route("/stream.wav")
def stream_audio_wav():
    """Studio monitor — raw PCM WAV (capture format)."""
    if RECORD_ONLY_MODE and _recording_active():
        return jsonify({"error": "Monitors disabled while recording (RECORD_ONLY_MODE)"}), 503
    req_rate = request.args.get("samplerate", type=int)
    req_depth = request.args.get("bitdepth", type=int)
    return Response(
        _wav_stream_generator(req_rate, req_depth),
        mimetype="audio/wav",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/stream16.wav")
def stream_audio_wav16():
    """Low-bandwidth monitor — 16-bit PCM WAV (no codec latency)."""
    if RECORD_ONLY_MODE and _recording_active():
        return jsonify({"error": "Monitors disabled while recording (RECORD_ONLY_MODE)"}), 503
    req_rate = request.args.get("samplerate", type=int)
    req_depth = request.args.get("bitdepth", type=int)
    return Response(
        _wav_stream_generator(req_rate, req_depth, out_16bit=True),
        mimetype="audio/wav",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/stream.aac")
def stream_audio_aac_legacy():
    """Legacy alias — low-bandwidth AAC (256 kbps). Prefer /low_stream.aac."""
    return low_bandwidth_stream()


@app.route("/low_stream.aac")
def low_bandwidth_stream():
    """Low-bandwidth monitor — AAC-ADTS at 256 kbps."""
    if RECORD_ONLY_MODE and _recording_active():
        return jsonify({"error": "Monitors disabled while recording (RECORD_ONLY_MODE)"}), 503
    req_rate = request.args.get("samplerate", type=int)
    req_depth = request.args.get("bitdepth", type=int)
    return Response(
        _aac_stream_generator(256, req_rate, req_depth),
        mimetype="audio/aac",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/logs")
def api_logs():
    """Return recent debug logs."""
    return jsonify({"logs": recorder_log[-50:]})


@app.route("/api/start", methods=["POST"])
def api_start():
    with engine_lock:
        return _api_start()


def _api_start():
    global card, device, name, samplerate, bitdepth, channels

    if _recording_active():
        return jsonify({"error": "Already recording"}), 400

    payload = request.get_json(silent=True) or {}

    if card is None:
        return jsonify({"error": "No capture card found"}), 400

    file_format = _normalize_file_format(payload.get("format", "aiff"))

    requested_filename = payload.get("filename", "")
    if requested_filename is None:
        requested_filename = ""
    if not isinstance(requested_filename, str):
        return jsonify({"error": "Filename must be a string"}), 400
    try:
        filename, file_format = clean_recording_filename(
            requested_filename, file_format
        )
        filename = resolve_recording_path(filename)
    except (ValueError, OSError) as e:
        return jsonify({"error": str(e)}), 400

    requested_samplerate = int(payload.get("samplerate", samplerate or 48000))
    requested_bitdepth = int(payload.get("bitdepth", bitdepth or 16))

    if RECORD_ONLY_MODE and file_format == "wav":
        samplerate = requested_samplerate
        bitdepth = requested_bitdepth
        channels = _active_channel_count()
    else:
        engine_running = arecord_process and arecord_process.poll() is None
        settings_changed = (
            samplerate != requested_samplerate or bitdepth != requested_bitdepth
        )
        samplerate = requested_samplerate
        bitdepth = requested_bitdepth
        channels = _active_channel_count()
        if settings_changed or not engine_running:
            stop_continuous_audio_engine()
            init_continuous_audio_engine(samplerate, bitdepth)

    result = start_recording(filename, file_format=file_format)
    status = "recording" if result["status"] == "success" else "error"
    code = 200 if status == "recording" else 500
    return jsonify({"status": status, "filename": filename, **result}), code


@app.route("/api/stop", methods=["POST"])
def api_stop():
    if not _recording_active() and wav_recorder is None and direct_record_process is None:
        return jsonify({"status": "error", "message": "No active recording found"}), 400

    result = stop_recording()
    code = 200 if result["status"] == "success" else 400
    return jsonify(result), code


@app.route("/api/input", methods=["GET"])
def api_get_input():
    global card, selector_numid, input_source
    if card is None:
        return jsonify({"error": "no capture card"}), 400

    if selector_numid is None:
        return jsonify({"source": None, "supported": False})

    # Always read live from the mixer — do not trust the in-memory cache alone.
    input_source = get_input_source(card, selector_numid)
    return jsonify({"source": input_source, "supported": True})


@app.route("/api/input", methods=["POST"])
def api_set_input():
    global card, selector_numid, input_source, samplerate, bitdepth
    if card is None:
        return jsonify({"error": "no capture card"}), 400

    if selector_numid is None:
        return jsonify({"error": "input selector not supported"}), 400

    if wav_recorder is not None:
        return jsonify({"error": "stop recording before changing input"}), 400

    payload = request.get_json(silent=True) or {}
    source = payload.get("source")
    if source not in ["Line", "IEC958 In"]:
        return jsonify({"error": "invalid source"}), 400

    try:
        set_input_source(card, selector_numid, source)
    except subprocess.CalledProcessError:
        return jsonify({"error": "failed to set mixer input source"}), 500

    # Restart arecord so the USB interface applies the new capture source.
    init_continuous_audio_engine(samplerate or 48000, bitdepth or 16)

    return jsonify({"status": "ok", "source": input_source})


@app.route("/api/caps")
def api_caps():
    global hwcaps

    if hwcaps["card"] is None:
        return jsonify({"error": "no card selected"}), 400

    return jsonify({
        "bitdepths": hwcaps["bitdepths"],
        "samplerates": hwcaps["samplerates"],
        "channels": hwcaps["channels"],
        "name": hwcaps["name"],
        "card": hwcaps["card"],
        "device": hwcaps["device"],
    })


@app.route("/api/warmup")
def api_warmup():
    """Pre-start capture at the UI's rate/depth so the first stream is not delayed."""
    if RECORD_ONLY_MODE and _recording_active():
        return jsonify({"ok": False, "reason": "recording"}), 409
    req_rate = request.args.get("samplerate", type=int) or 48000
    req_depth = request.args.get("bitdepth", type=int) or 24
    _ensure_monitor_engine(req_rate, req_depth)
    return jsonify({
        "ok": True,
        "samplerate": samplerate,
        "bitdepth": bitdepth,
        "running": bool(arecord_process and arecord_process.poll() is None),
    })


@app.route("/api/levels")
def api_levels():
    """
    Low-rate peak/hold meter stream (SSE) for UI VU meters.
    Not audio — JSON levels derived from capture PCM (~20 Hz).
    """
    # Keep capture alive while the meter page is open.
    if not (RECORD_ONLY_MODE and _recording_active()):
        _ensure_monitor_engine(samplerate or 48000, bitdepth or 24)

    def generate():
        while True:
            with levels_lock:
                payload = dict(levels_state)
            yield f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
            time.sleep(0.05)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/sink_monitor", methods=["GET", "POST"])
def api_sink_monitor():
    """Enable/disable low-latency PipeWire sink playback at capture resolution."""
    if request.method == "GET":
        running = bool(
            sink_monitor_enabled
            and sink_monitor_proc is not None
            and sink_monitor_proc.poll() is None
        )
        return jsonify({
            "enabled": running,
            "sink": PIPEWIRE_SINK,
            "latency": PIPEWIRE_LATENCY,
        })

    payload = request.get_json(silent=True) or {}
    enabled = payload.get("enabled")
    if enabled is None:
        return jsonify({"error": "missing enabled"}), 400
    target = payload.get("sink")
    if enabled:
        req_rate = req_depth = None
        try:
            if "samplerate" in payload:
                req_rate = int(payload["samplerate"])
            if "bitdepth" in payload:
                req_depth = int(payload["bitdepth"])
        except (TypeError, ValueError):
            return jsonify({"error": "invalid samplerate/bitdepth"}), 400
        if req_rate or req_depth:
            _ensure_monitor_engine(req_rate, req_depth)
        result = start_sink_monitor(target=target)
        code = 200 if result.get("status") == "ok" else 500
        return jsonify(result), code

    return jsonify(stop_sink_monitor())


@app.route("/api/cards")
def api_cards():
    global selected_card, selected_device, selected_name

    cards_list = list_capture_cards()

    if len(cards_list) == 1:
        c = cards_list[0]
        selected_card = c["card"]
        selected_device = c["device"]
        selected_name = c["name"]
        set_card()

    return jsonify(cards_list)


@app.route("/api/select_card", methods=["POST"])
def api_select_card():
    global selected_card, selected_device, selected_name

    if _recording_active() or wav_recorder is not None:
        return jsonify({"error": "stop recording before changing the capture card"}), 409

    payload = request.get_json(silent=True) or {}
    if selected_card != payload.get("card"):
        selected_card = payload.get("card")
        selected_device = payload.get("device")

        for c in list_capture_cards():
            if c["card"] == selected_card and c["device"] == selected_device:
                selected_name = c["name"]

        set_card()

    return jsonify({"status": "ok"})


if __name__ == "__main__":
    init_card()
    if card is not None and device is not None:
        # Prefer 24-bit so the first UI stream does not restart arecord (slow on mobile).
        if selected_card is None:
            selected_card, selected_device, selected_name = card, device, name
            set_card()
        boot_depth = 24 if 24 in (hwcaps.get("bitdepths") or []) else 16
        init_continuous_audio_engine(48000, boot_depth)
    # threaded: status polls / API must work while a monitor stream is open
    app.run(host="0.0.0.0", port=5000, threaded=True)
