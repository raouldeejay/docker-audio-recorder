import os
import subprocess
import re
import datetime
import threading
import time

from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

# Global recorder process
arecord_process = None
RECORDINGS_ROOT = "/app/recordings/"
selected_card = None
selected_device = None
selected_name = None

hwcaps = {
    "card": None,
    "device": None,
    "name": None,
    "raw": None,          # <-- raw hw params dump
    "bitdepths": None,
    "samplerates": None,
    "channels": None
}

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
    output = subprocess.check_output(["arecord", "-l"], text=True)
    cards = []

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

def dump_hw_params(card, device):
    device_string = f"hw:{card},{device}"
    try:
        output = subprocess.check_output(
            ["arecord", "-D", device_string, "--dump-hw-params"],
            text=True,
            stderr=subprocess.STDOUT
        )
        return output
    except subprocess.CalledProcessError as e:
        print("Failed to query hw params:", e.output)
        return ""


def detect_channel_count(card, device):
    """
    Returns the maximum supported channel count for the ALSA device.
    """
    global hwcaps
    # output = dump_hw_params(card, device)
    for line in hwcaps["raw"].splitlines():
        if "CHANNELS:" in line:
            line = line.replace("CHANNELS:", "").strip()

            # Case 1: single number, e.g. "2"
            if line.isdigit():
                return int(line)

            # Case 2: range, e.g. "[1 2]"
            m = re.search(r"\[(\d+)\s+(\d+)\]", line)
            if m:
                low = int(m.group(1))
                high = int(m.group(2))
                return high  # use max supported channels

    return 2  # fallback

def detect_bitdepths(card, device):
    """
    Returns list of supported bit depths, e.g. [16, 24].
    Combines FORMAT and SAMPLE_BITS, keeps entries unique.
    Ignores SUBFORMAT completely.
    """
    global hwcaps
    # output = dump_hw_params(card, device)
    bitdepths = set()

    for line in hwcaps["raw"].splitlines():

        # FORMAT: S16_LE S24_3LE S32_LE
        if "FORMAT:" in line:
            formats = line.replace("FORMAT:", "").strip().split()
            for fmt in formats:
                if "S16" in fmt:
                    bitdepths.add(16)
                elif "S24" in fmt:
                    bitdepths.add(24)
                elif "S32" in fmt:
                    bitdepths.add(32)

        # SAMPLE_BITS: [16 24]
        if "SAMPLE_BITS:" in line:
            # Range case
            m = re.search(r"\[(\d+)\s+(\d+)\]", line)
            if m:
                low = int(m.group(1))
                high = int(m.group(2))
                bitdepths.add(low)
                bitdepths.add(high)
            else:
                # Single value case: SAMPLE_BITS: 16
                val = line.replace("SAMPLE_BITS:", "").strip()
                if val.isdigit():
                    bitdepths.add(int(val))

        # SUBFORMAT is informational — never touch bitdepths

    # Fallback if ALSA reports nothing
    if not bitdepths:
        return [16]

    return sorted(bitdepths)


def detect_samplerates(card, device):
    """
    Returns list of supported samplerates based on RATE line.
    """
    global hwcaps
    # output = dump_hw_params(card, device)
    for line in hwcaps["raw"].splitlines():
        if "RATE:" in line:
            line = line.replace("RATE:", "").strip()

            # Case: "[8000 48000]"
            m = re.search(r"\[(\d+)\s+(\d+)\]", line)
            if m:
                low = int(m.group(1))
                high = int(m.group(2))
                common = [8000, 16000, 22050, 32000, 44100, 48000, 96000]
                return [r for r in common if low <= int(r) <= high]
                
            # Case: single number
            if line.isdigit():
                return [int(line)]

    return [44100, 48000]

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


# ------------------------------------------------------------
# RECORDING USING ARECORD
# ------------------------------------------------------------

def start_arecord(filename, samplerate, bitdepth, card, device):
    """
    Launch arecord as a subprocess.
    """
    global arecord_process
    filepath = RECORDINGS_ROOT + filename
    # Map bit depth to ALSA format
    if bitdepth == 16:
        fmt = "S16_LE"
    elif bitdepth == 24:
        fmt = "S24_3LE"
    else:
        raise ValueError("Unsupported bit depth")

    channels = detect_channel_count(card, device)
    
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
    raw = dump_hw_params(selected_card, selected_device)

    hwcaps["card"] = selected_card
    hwcaps["device"] = selected_device
    hwcaps["name"] = selected_name
    hwcaps["raw"] = raw

    # Now parse from cached raw dump
    hwcaps["bitdepths"] = detect_bitdepths(selected_card, selected_device)
    hwcaps["samplerates"] = detect_samplerates(selected_card, selected_device)
    hwcaps["channels"] = detect_channel_count(selected_card, selected_device)



# ------------------------------------------------------------
# FLASK ROUTES
# ------------------------------------------------------------

@app.route("/")
def index():
    cards = list_capture_cards()
    # init to 1st or selected card
    card, device, name = detect_capture_card()
    
    input_source = None
    selector_numid = None

    if card is not None:
        selector_numid = detect_input_selector(card)
        if selector_numid is not None:
            input_source = get_input_source(card, selector_numid)

    return render_template(
        "index.html",
        cards=cards,
        selected_card=card,
        selected_device=device,
        selected_name=name,
        input_source=input_source,
        selector_present=(selector_numid is not None)
    )


@app.route("/api/status")
def api_status():
    return jsonify({"recording": arecord_process is not None})


@app.route("/api/start", methods=["POST"])
def api_start():
    global arecord_process

    if arecord_process is not None:
        return jsonify({"error": "Already recording"}), 400

    data = request.json

    filename = data.get("filename", "").strip()
    if filename == "":
        filename = generate_filename()

    samplerate = int(data.get("samplerate", 44100))
    bitdepth = int(data.get("bitdepth", 16))

    card, device, name = detect_capture_card()
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
    selected_card = data.get("card")
    selected_device = data.get("device")

    # Store name
    for c in list_capture_cards():
        if c["card"] == selected_card and c["device"] == selected_device:
            selected_name = c["name"]

    set_card()

    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
