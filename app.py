import os
import subprocess
import re
import datetime
import threading
import time
import socket
from collections import deque

from pathlib import Path

from flask import Flask, render_template, request, jsonify, Response

app = Flask(__name__)

# Global recorder process
arecord_process = None
ffmpeg_recorder_process = None

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

# Stream buffering: do not let any downstream consumer block the recorder.
stream_lock = threading.Lock()
stream_high_queue = deque(maxlen=200)
stream_low_queue = deque(maxlen=80)
audio_router_thread = None
is_recording = False
recorder_log = []


def _log(msg):
    """Log to both console and in-memory buffer for debugging."""
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    log_msg = f"[{timestamp}] {msg}"
    print(log_msg)
    recorder_log.append(log_msg)
    if len(recorder_log) > 100:
        recorder_log.pop(0)


def _enqueue_stream_chunk(chunk, *, low_quality=False):
    if not chunk:
        return

    bucket = stream_low_queue if low_quality else stream_high_queue
    try:
        bucket.append(chunk)
    except Exception:
        pass


def _audio_router_loop():
    """Runs in a background thread and duplicates raw PCM chunks to recording and live streams."""
    global arecord_process, ffmpeg_recorder_process, is_recording

    while True:
        if not arecord_process or arecord_process.poll() is not None:
            time.sleep(0.2)
            continue

        try:
            chunk = arecord_process.stdout.read(4096)
        except Exception as e:
            _log(f"Router read error: {e}")
            time.sleep(0.1)
            continue

        if not chunk:
            time.sleep(0.2)
            continue

        # Record to file if active — CRITICAL: write to recorder stdin
        if is_recording and ffmpeg_recorder_process:
            if ffmpeg_recorder_process.poll() is None:  # Process still alive
                try:
                    ffmpeg_recorder_process.stdin.write(chunk)
                    ffmpeg_recorder_process.stdin.flush()
                except (BrokenPipeError, ValueError, AttributeError) as e:
                    _log(f"Recorder write error: {e}")
                    ffmpeg_recorder_process = None
            else:
                exit_code = ffmpeg_recorder_process.returncode
                _log(f"Recorder process died (exit code: {exit_code})")
                ffmpeg_recorder_process = None

        # Feed the monitor streams without blocking the recorder.
        with stream_lock:
            _enqueue_stream_chunk(chunk, low_quality=False)
            _enqueue_stream_chunk(chunk[: max(1, len(chunk) // 2)], low_quality=True)

        time.sleep(0.001)


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
    try:
        output = subprocess.check_output(["amixer", "-c", str(card), "cget", f"numid={numid}"], text=True)
        if "values=0" in output:
            return "Line"
        return "IEC958 In"
    except subprocess.CalledProcessError:
        return "Line"


def set_input_source(card, numid, source):
    global input_source
    input_source = source
    value = 0 if source == "Line" else 1
    try:
        subprocess.check_call(["amixer", "-c", str(card), "cset", f"numid={numid}", str(value)])
        _log(f"Set input source to {source}")
    except subprocess.CalledProcessError as e:
        _log(f"Failed to set input source: {e}")


# ------------------------------------------------------------
# FILENAME GENERATION
# ------------------------------------------------------------

def generate_filename():
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"recording_{ts}.wav"


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


# ------------------------------------------------------------
# RECORDING — Direct arecord to file (minimal, proven approach)
# ------------------------------------------------------------
def start_arecord(filename, fmt):
    """Direct arecord capture to WAV file. No FFmpeg pipeline."""
    global arecord_process, samplerate, channels, is_recording

    filepath = os.path.join(RECORDINGS_ROOT, filename)
    alsa_fmt, _ = get_audio_formats(bitdepth)
    device_string = f"hw:{card},{device}"

    rec_cmd = [
        "arecord",
        "-D", device_string,
        "-f", alsa_fmt,
        "-r", str(samplerate),
        "-c", str(channels),
        "-t", "wav",
        filepath,
    ]

    _log(f"arecord command: {' '.join(rec_cmd)}")

    arecord_process = subprocess.Popen(
        rec_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    is_recording = True
    _log(f"Direct arecord recorder started (PID {arecord_process.pid}, writing to {filepath})")
    return {"status": "success", "message": f"Recording started (WAV, {samplerate}Hz, {channels}ch, {bitdepth}bit)", "path": filepath}


def set_card():
    global selected_card, selected_device, selected_name, hwcaps
    if hwcaps["card"] != selected_card:
        hwcaps = get_hwcaps_non_exclusive(selected_card, selected_device)
        hwcaps["card"] = selected_card
        hwcaps["device"] = selected_device
        hwcaps["name"] = selected_name


def init_continuous_audio_engine(_samplerate=48000, _bitdepth=16):
    """Start arecord for live stream only (no recording yet)."""
    global arecord_process, card, device, samplerate, bitdepth, hwcaps, audio_router_thread

    samplerate = _samplerate
    bitdepth = _bitdepth

    if card is None or device is None:
        _log("Cannot start audio engine: no card selected")
        return

    alsa_fmt, _ = get_audio_formats(bitdepth)
    device_string = f"hw:{card},{device}"
    channel_count = hwcaps.get("channels", 2)

    if arecord_process and arecord_process.poll() is None:
        arecord_process.terminate()
        try:
            arecord_process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            arecord_process.kill()

    # arecord for live streaming ONLY (not recording)
    arecord_cmd = [
        "arecord",
        "-D", device_string,
        "-f", alsa_fmt,
        "-r", str(samplerate),
        "-c", str(channel_count),
        "-t", "raw",
        "-",
    ]

    _log(f"Stream arecord command: {' '.join(arecord_cmd)}")
    arecord_process = subprocess.Popen(
        arecord_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0
    )
    _log(f"Stream arecord started (PID {arecord_process.pid})")

    if audio_router_thread is None or not audio_router_thread.is_alive():
        audio_router_thread = threading.Thread(target=_audio_router_loop, daemon=True)
        audio_router_thread.start()
        _log("Audio router thread started")

    _log("🚀 Audio engine initialized")


def stop_continuous_audio_engine():
    """Stop the live stream arecord."""
    global arecord_process, is_recording

    _log("Stopping audio engine...")

    if arecord_process:
        try:
            arecord_process.terminate()
            arecord_process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            arecord_process.kill()
            arecord_process.wait()
        except Exception as e:
            _log(f"Error stopping stream arecord: {e}")
        arecord_process = None

    is_recording = False
    stream_high_queue.clear()
    stream_low_queue.clear()
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
        cards=cards,
        selected_card=card,
        selected_device=device,
        selected_name=name,
        input_source=input_source,
        selector_present=(selector_numid is not None),
        hostname=HOSTNAME,
    )


@app.route("/api/status")
def api_status():
    # Check if the recorder arecord is still running
    recording = is_recording and arecord_process and arecord_process.poll() is None
    return jsonify({"recording": recording})


def _stream_generator(kind):
    while True:
        if kind == "high":
            bucket = stream_high_queue
        else:
            bucket = stream_low_queue

        try:
            with stream_lock:
                if bucket:
                    chunk = bucket.popleft()
                    yield chunk
                else:
                    time.sleep(0.05)
        except Exception:
            time.sleep(0.05)


@app.route('/stream.aac')
def stream_audio():
    """Live monitor endpoint using buffered PCM."""
    return Response(_stream_generator("high"), mimetype='audio/aac')


@app.route('/low_stream.aac')
def low_bandwidth_stream():
    """Low-quality monitor stream."""
    return Response(_stream_generator("low"), mimetype='audio/aac')


@app.route("/api/logs")
def api_logs():
    """Return recent debug logs."""
    return jsonify({"logs": recorder_log[-50:]})


@app.route("/api/start", methods=["POST"])
def api_start():
    global arecord_process, card, device, name, samplerate, bitdepth, is_recording

    if is_recording and arecord_process and arecord_process.poll() is None:
        return jsonify({"error": "Already recording"}), 400

    payload = request.get_json(silent=True) or {}

    if card is None:
        return jsonify({"error": "No capture card found"}), 400

    filename = str(payload.get("filename", "")).strip()
    if filename == "":
        filename = generate_filename()

    requested_samplerate = int(payload.get("samplerate", samplerate or 48000))
    requested_bitdepth = int(payload.get("bitdepth", bitdepth or 16))

    if samplerate != requested_samplerate or bitdepth != requested_bitdepth:
        samplerate = requested_samplerate
        bitdepth = requested_bitdepth
        # Restart live stream with new settings
        stop_continuous_audio_engine()
        init_continuous_audio_engine(samplerate, bitdepth)

    result = start_arecord(filename, "wav")
    return jsonify({"status": "recording" if result["status"] == "success" else "error", "filename": filename, **result})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    global arecord_process, is_recording

    if not is_recording or not arecord_process or arecord_process.poll() is not None:
        return jsonify({"status": "error", "message": "No active recording found"}), 400

    is_recording = False
    _log("Stopping recording...")

    try:
        arecord_process.terminate()
        arecord_process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        arecord_process.kill()
        arecord_process.wait()
    except Exception as e:
        _log(f"Error stopping recorder: {e}")
    finally:
        arecord_process = None

    _log("Recording stopped")
    return jsonify({"status": "success", "message": "Recording saved"})


@app.route("/api/input", methods=["GET"])
def api_get_input():
    global card, selector_numid, input_source
    if card is None:
        return jsonify({"error": "no capture card"}), 400

    if selector_numid is None:
        return jsonify({"source": None, "supported": False})

    return jsonify({"source": input_source, "supported": True})


@app.route("/api/input", methods=["POST"])
def api_set_input():
    global card, selector_numid
    if card is None:
        return jsonify({"error": "no capture card"}), 400

    if selector_numid is None:
        return jsonify({"error": "input selector not supported"}), 400

    payload = request.get_json(silent=True) or {}
    source = payload.get("source")
    if source not in ["Line", "IEC958 In"]:
        return jsonify({"error": "invalid source"}), 400

    set_input_source(card, selector_numid, source)
    return jsonify({"status": "ok", "source": source})


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
        init_continuous_audio_engine(48000, 16)
    app.run(host="0.0.0.0", port=5000)
