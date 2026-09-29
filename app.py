import os
import subprocess
import re
import datetime
import threading
import time
import socket
from pathlib import Path

from flask import Flask, render_template, request, jsonify, Response

app = Flask(__name__)

# Global recorder process
cards = []
arecord_process = None
RECORDINGS_ROOT = "/app/recordings/"
selected_card = None
selected_device = None
selected_name = None
cards = None
card = None
device = None
name = None
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
    value = 0 if source == "Line" else 1
    subprocess.check_call(
        ["amixer", "-c", str(card), "cset", f"numid={numid}", str(value)]
    )


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
# RECORDING USING ARECORD
# ------------------------------------------------------------

def start_arecord(filename, samplerate, bitdepth, card, device):
    """
    Launch arecord as a subprocess.
    """
    global arecord_process, hwcaps
    filepath = RECORDINGS_ROOT + filename
    # Map bit depth to ALSA format
    if bitdepth == 16:
        fmt = "S16_LE"
    elif bitdepth == 24:
        fmt = "S24_3LE"
    else:
        raise ValueError("Unsupported bit depth")

    channels = hwcaps["channels"] # detect_channel_count(card, device)
    
    device_string = f"hw:{card},{device}"

    cmd = [
        "arecord",
        "-D", device_string,
        "-f", fmt,
        "-r", str(samplerate),
        "-c", str(channels),
        str(filepath)
    ]

    print("Starting arecord:", " ".join(cmd))
    arecord_process = subprocess.Popen(cmd)


def stop_arecord():
    """
    Stop the arecord subprocess cleanly.
    """
    global arecord_process

    if arecord_process is not None:
        arecord_process.terminate()
        try:
            arecord_process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            arecord_process.kill()

        arecord_process = None

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

def init_continuous_audio_engine(samplerate=44100, bitdepth=16):
    global arecord_process, ffmpeg_stream_process
    
    # 1. Map bitdiepte naar ALSA en FFmpeg formats
    alsa_fmt = "S16_LE" if bitdepth == 16 else "S24_3LE"
    ffmpeg_fmt = "s16le" if bitdepth == 16 else "s24le"
    device_string = f"hw:{CARD},{DEVICE}"

    # 2. Arecord vangt pure PCM
    arecord_cmd = [
        "arecord", "-D", device_string, "-f", alsa_fmt,
        "-r", str(samplerate), "-c", str(CHANNELS), "-t", "raw", "-"
    ]
    arecord_process = subprocess.Popen(arecord_cmd, stdout=subprocess.PIPE)

    # 3. FFmpeg met 3 parallelle outputs via the TEE-muxer:
    # - Output 1: PipeWire (Pulse) -> Ongecomprimeerd
    # - Output 2: High Quality Monitor -> Stereo AAC op 256 kbps (naar pipe:1 / stdout)
    # - Output 3: Low Bandwidth Monitor -> Mono AAC op 64 kbps (naar pipe:3)
    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-f", ffmpeg_fmt, "-ar", str(samplerate), "-ac", str(CHANNELS),
        "-i", "pipe:0",
        "-f", "tee",
        "-map", "0:a",
        f"[f=pulse]default|"
        f"[f=mpegts:c:a=aac:b:a=256k]pipe:1|"
        f"[f=mpegts:c:a=aac:b:a=64k:ac=1]pipe:3" # :ac=1 forceert downmix naar mono voor extra besparing
    ]
    
    # Pass 'pass_fds=[3]' mee zodat Python toestaat dat FFmpeg file descriptor 3 gebruikt
    ffmpeg_stream_process = subprocess.Popen(
        ffmpeg_cmd, 
        stdin=arecord_process.stdout, 
        stdout=subprocess.PIPE,
        pass_fds=[3],
        stderr=subprocess.DEVNULL
    )
    
    # Open de extra descriptor 3 in Python om de low-bandwidth stream uit te lezen
    # In een Docker Debian omgeving linkt fd 3 direct naar /proc/self/fd/3
    import os
    global low_bandwidth_fd
    low_bandwidth_fd = os.fdopen(3, 'rb')

    print("🚀 Dual-Bandbreedte Audio Engine actief.")

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
    return jsonify({"recording": arecord_process is not None})


@app.route("/api/start", methods=["POST"])
def api_start():
    global arecord_process, card, device, name

    if arecord_process is not None:
        return jsonify({"error": "Already recording"}), 400

    data = request.json

    filename = data.get("filename", "").strip()
    if filename == "":
        filename = generate_filename()

    samplerate = int(data.get("samplerate", 44100))
    bitdepth = int(data.get("bitdepth", 16))

    if card is None:
        return jsonify({"error": "No capture card found"}), 400

    start_arecord(filename, samplerate, bitdepth, card, device)

    return jsonify({"status": "recording", "filename": filename})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    stop_arecord()
    return jsonify({"status": "stopped"})


@app.route("/api/input", methods=["GET"])
def api_get_input():
    card, device, name = detect_capture_card()
    if card is None:
        return jsonify({"error": "no capture card"}), 400

    numid = detect_input_selector(card)
    if numid is None:
        return jsonify({"source": None, "supported": False})

    return jsonify({
        "source": get_input_source(card, numid),
        "supported": True
    })


@app.route("/api/input", methods=["POST"])
def api_set_input():
    card, device, name = detect_capture_card()
    if card is None:
        return jsonify({"error": "no capture card"}), 400

    numid = detect_input_selector(card)
    if numid is None:
        return jsonify({"error": "input selector not supported"}), 400

    data = request.json
    source = data.get("source")
    if source not in ["Line", "IEC958 In"]:
        return jsonify({"error": "invalid source"}), 400

    set_input_source(card, numid, source)
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
    # init_continuous_audio_engine(samplerate=44100, bitdepth=16)
        
    app.run(host="0.0.0.0", port=5000)
