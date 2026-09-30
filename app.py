import os
import subprocess
import re
import datetime
import threading
import time
import socket

from pathlib import Path
from urllib.request import urlopen

from flask import Flask, render_template, request, jsonify, Response

app = Flask(__name__)

# Global recorder process
arecord_process = None
ffmpeg_stream_process = None
ffmpeg_recorder_process = None
low_bandwidth_fd = None

RECORDINGS_ROOT = "/app/recordings/"

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


hwcaps = {
    "card": None,
    "device": None,
    "name": None,
    "raw": None,          # <-- raw hw params dump
    "bitdepths": None,
    "samplerates": None,
    "channels": None
}

HOSTNAME = socket.gethostname()

# ------------------------------------------------------------
# ALSA CARD DETECTION
# ------------------------------------------------------------

def list_capture_cards():
    """
    Returns a list of dicts:
    [
        {"card": 1, "device": 0, "name": "U24XL"},
        ...
    ]
    """
    global cards
    
    if len(cards) == 0:
        output = subprocess.check_output(["arecord", "-l"], text=True)

        current_card = None

        for line in output.splitlines():
            m = re.search(r"card (\d+): ([^[]+)\[([^\]]+)\]", line)
            if m:
                current_card = {
                    "card": int(m.group(1)),
                    "name": m.group(3).strip()
                }

            d = re.search(r"device (\d+): ([^[]+)\[([^\]]+)\]", line)
            if d and current_card:
                current_card["device"] = int(d.group(1))
                cards.append(current_card)
                current_card = None

    return cards


def detect_capture_card():
    global selected_card, selected_device, selected_name
    if selected_card is None:
        #init
        cards = list_capture_cards()
        if not cards:
            return None, None, None
        c = cards[0]
        selected_card = c["card"]
        selected_device = c["device"]
        selected_name = c["name"]
        set_card()
        
    return selected_card, selected_device, selected_name

# ------------------------------------------------------------
# HW PARAMS (CHANNELS / FORMAT / RATE)
# ------------------------------------------------------------
def get_hwcaps_non_exclusive(card_index, device_index):

    global hwcaps
    # Paths for device name and streaming capabilities
    id_file = Path(f"/mnt/asound/card{card_index}/id")
    stream_file = Path(f"/mnt/asound/card{card_index}/stream0")
    
    # 1. Get the short name of the card (e.g., "U24XL")
    if id_file.exists():
        hwcaps["name"] = id_file.read_text().strip()
        
    # 2. Parse capabilities from stream0 safely
    if stream_file.exists():
        content = stream_file.read_text()
        
        # We only care about Capture for recording setups
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
            # Extract values
            bits_found = re.findall(r'Bits:\s*(\d+)', capture_block)
            rates_lines = re.findall(r'Rates:\s*(.+)', capture_block)
            channels_found = re.findall(r'Channels:\s*(\d+)', capture_block)
            
            # Process bit depths and sample rates
            bitdepths = sorted(list(set(int(b) for b in bits_found)))
            
            samplerates = []
            for line in rates_lines:
                samplerates.extend([int(r.strip()) for r in line.split(',')])
            samplerates = sorted(list(set(samplerates)))
            
            # Process channels (pick the maximum supported if multiple profiles exist)
            channels = max([int(c) for c in channels_found]) if channels_found else 2
            
            hwcaps["bitdepths"] = bitdepths
            hwcaps["samplerates"] = samplerates
            hwcaps["channels"] = channels
            
    return hwcaps

# ------------------------------------------------------------
# SPDIF / LINE SELECTOR (dynamic)
# ------------------------------------------------------------

def detect_input_selector(card):
    """
    Returns numid of an ENUMERATED control with items ['Line', 'IEC958 In']
    or None if not present.
    """
    output = subprocess.check_output(
        ["amixer", "-c", str(card), "contents"],
        text=True
    )

    blocks = output.split("numid=")[1:]
    for block in blocks:
        if "ENUMERATED" in block and "Line" in block and "IEC958 In" in block:
            numid = int(block.split(",")[0])
            return numid

    return None

def get_input_source(card, numid):
    output = subprocess.check_output(
        ["amixer", "-c", str(card), "cget", f"numid={numid}"],
        text=True
    )
    if "values=0" in output:
        return "Line"
    return "IEC958 In"


def set_input_source(card, numid, source):
    global input_source
    input_source = source
    value = 0 if source == "Line" else 1
    subprocess.check_call(
        ["amixer", "-c", str(card), "cset", f"numid={numid}", str(value)]
    )


# ------------------------------------------------------------
# FILENAME GENERATION
# ------------------------------------------------------------

def generate_filename():
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"recording_{ts}.aiff"

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
# RECORDING USING ARECORD
# ------------------------------------------------------------

def start_arecord(filename, fmt):
    """
    Launch arecord as a subprocess.
    """
    global ffmpeg_recorder_process
    filepath = RECORDINGS_ROOT + filename

    # Tap veilig in op de continu lopende HTTP-stream
    rec_cmd = [
        "ffmpeg", "-y",
        "-i", "http://127.0.0",
        # "-metadata", f"title={title}",
        # "-metadata", f"artist={artist}",
        "-f", fmt, filepath
    ]

    ffmpeg_recorder_process = subprocess.Popen(rec_cmd, stderr=subprocess.DEVNULL)
    return jsonify({"status": "success", "message": f"Recording started {fmt.upper()}", "path": filepath})

def set_card():
    global selected_card, selected_device, selected_name, hwcaps
    # Cache raw hw params ONCE
    if hwcaps["card"] != selected_card:
        #raw = dump_hw_params(selected_card, selected_device)
        
        # bitdepth and samplerate prefilled
        hwcaps = get_hwcaps_non_exclusive(selected_card, selected_device)
        hwcaps["card"] = selected_card
        hwcaps["device"] = selected_device
        hwcaps["name"] = selected_name
        #hwcaps["raw"] = raw



    print("RAW:", repr(hwcaps["raw"]))

def init_continuous_audio_engine(_samplerate=48000, _bitdepth=16):
    global arecord_process, ffmpeg_stream_process, card, device, samplerate, bitdepth, hwcaps, low_bandwidth_fd
    samplerate = _samplerate
    bitdepth = _bitdepth
    
    # 1. Map bitdiepte naar ALSA en FFmpeg formats
    alsa_fmt, ffmpeg_fmt = get_audio_formats(bitdepth)
    device_string = f"hw:{card},{device}"

    channels = hwcaps["channels"] # detect_channel_count(card, device)
    
    # 2. Arecord vangt pure PCM
    arecord_cmd = [
        "arecord", "-D", device_string, "-f", alsa_fmt,
        "-r", str(samplerate), "-c", str(channels), "-t", "raw", "-"
    ]
    arecord_process = subprocess.Popen(arecord_cmd, stdout=subprocess.PIPE)

    # 3. FFmpeg met 3 parallelle outputs via the TEE-muxer:
    # - Output 1: PipeWire (Pulse) -> Ongecomprimeerd
    # - Output 2: High Quality Monitor -> Stereo AAC op 256 kbps (naar pipe:1 / stdout)
    # - Output 3: Low Bandwidth Monitor -> Mono AAC op 64 kbps (naar pipe:3)
    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-f", ffmpeg_fmt, "-ar", str(samplerate), "-ac", str(channels),
        "-i", "pipe:0",
        "-f", "tee",
        "-map", "0:a",
        f"[f=pulse]default|"
        f"[f=adts:c:a=aac:b:a=512k]http://127.0.0.1:8081|"
        f"[f=adts:c:a=aac:b:a=256k:ac=1]http://127.0.0.1:8082" # :ac=1 forceert downmix naar mono voor extra besparing
    ]
    
    ffmpeg_stream_process = subprocess.Popen(
        ffmpeg_cmd, 
        stdin=arecord_process.stdout, 
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL
    )

    # Open de extra descriptor 3 in Python om de low-bandwidth stream uit te lezen
    # In een Docker Debian omgeving linkt fd 3 direct naar /proc/self/fd/3
    # low_bandwidth_fd = os.fdopen(3, 'rb')

    print("🚀 Dual-Bandbreedte Audio Engine actief.")

def stop_continuous_audio_engine():
    """Beëindigt de arecord- en FFmpeg-streamprocessen op een elegante manier."""
    global arecord_process, ffmpeg_stream_process, low_bandwidth_fd
    print("Stopping active audio engine components...")

    # Termineer arecord eerst (stopt de toevoer van nieuwe hardware bytes)
    if arecord_process:
        try:
            arecord_process.terminate()
            arecord_process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            arecord_process.kill()
        arecord_process = None

    # Laat FFmpeg zijn resterende buffers verwerken en sluiten
    if ffmpeg_stream_process:
        try:
            # .communicate() sluit stdin en wacht netjes tot het proces klaar is
            ffmpeg_stream_process.communicate(timeout=2)
        except (subprocess.TimeoutExpired, ValueError):
            ffmpeg_stream_process.kill()
        ffmpeg_stream_process = None

    # if low_bandwidth_fd:
    #    try:
    #        low_bandwidth_fd.close()
    #    except (subprocess.TimeoutExpired, ValueError):
    #        low_bandwidth_fd.kill()
    #    low_bandwidth_fd = None

def init_card():
    global cards, card, device, name, input_source, selector_numid
    cards = list_capture_cards()
    # init to 1st or selected card
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
        hostname=HOSTNAME
    )


@app.route("/api/status")
def api_status():
    return jsonify({"recording": ffmpeg_recorder_process is not None})

@app.route('/stream.aac')
def stream_audio():
    """Live monitor endpoint voor de browser (ondersteunt meerdere luisteraars)."""
    def generate():
    
        with urlopen('http://127.0.0.1:8081', timeout=5) as stream:
        # if ffmpeg_stream_process and ffmpeg_stream_process.stdout:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                yield chunk
    return Response(generate(), mimetype='audio/aac')

@app.route('/low_stream.aac')
def low_bandwidth_stream():
    """Lage kwaliteit monitor (Mono, 64 kbps AAC) geoptimaliseerd voor WiFi/4G."""
    def generate():
        # global low_bandwidth_fd
        # if low_bandwidth_fd:
        with  urlopen('htyp://127.0.0.1:8082', timeout=5) as stream:
            while True:
                chunk = stream.read(4096)
                if not chunk: break
                yield chunk
    return Response(generate(), mimetype='audio/aac')

@app.route("/api/start", methods=["POST"])
def api_start():
    global ffmpeg_recorder_process, card, device, name, samplerate, bitdepth

    if ffmpeg_recorder_process and ffmpeg_recorder_process.poll() is None:
        return jsonify({"error": "Already recording"}), 400

    data = request.json

    filename = data.get("filename", "").strip()
    if filename == "":
        filename = generate_filename()
    if (samplerate != int(data.get("samplerate")) or bitdepth != int(data.get("samplerate"))):
        samplerate = int(data.get("samplerate", 48000))
        bitdepth = int(data.get("bitdepth", 16))
        stop_continuous_audio_engine()
        init_continuous_audio_engine(samplerate, bitdepth)
    fmt = Path(data.get("filename", "").lower()).suffix
    if fmt not in ['wav', 'aiff']:
        fmt = 'aif'
        
    if card is None:
        return jsonify({"error": "No capture card found"}), 400

    start_arecord(filename, fmt)

    return jsonify({"status": "recording", "filename": filename})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    global ffmpeg_recorder_process
    if ffmpeg_recorder_process:
        ffmpeg_recorder_process.terminate()
        ffmpeg_recorder_process.wait()
        return jsonify({"status": "success", "message": "Recording saved"})
    return jsonify({"status": "error", "message": "No active recording found"}), 400



@app.route("/api/input", methods=["GET"])
def api_get_input():
    global card, device, name, selector_numid, input_source
    if card is None:
        return jsonify({"error": "no capture card"}), 400

    if selector_numid is None:
        return jsonify({"source": None, "supported": False})

    return jsonify({
        "source": input_source,
        "supported": True
    })


@app.route("/api/input", methods=["POST"])
def api_set_input():
    global card, device, name, selector_numid
    if card is None:
        return jsonify({"error": "no capture card"}), 400

    if selector_numid is None:
        return jsonify({"error": "input selector not supported"}), 400

    data = request.json
    source = data.get("source")
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
        "device": hwcaps["device"]
    })

@app.route("/api/cards")
def api_cards():
    global selected_card, selected_device, selected_name

    cards = list_capture_cards()

    # Auto-select if exactly one card is present
    if len(cards) == 1:
        c = cards[0]
        selected_card = c["card"]
        selected_device = c["device"]
        selected_name = c["name"]
        set_card()

    return jsonify(cards)

@app.route("/api/select_card", methods=["POST"])
def api_select_card():
    global selected_card, selected_device, selected_name

    data = request.json or {}
    if selected_card != date.get("card"):
        selected_card = data.get("card")
        selected_device = data.get("device")

        # Store name
        for c in list_capture_cards():
            if c["card"] == selected_card and c["device"] == selected_device:
                selected_name = c["name"]

        set_card()

    return jsonify({"status": "ok"})

if __name__ == "__main__":
    init_card()
    init_continuous_audio_engine(48000, 16)
        
    app.run(host="0.0.0.0", port=5000)
