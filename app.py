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
    cards = list_capture_cards()
    if not cards:
        return None, None, None
    c = cards[0]
    return c["card"], c["device"], c["name"]

def detect_channel_count(card, device):
    """
    Returns the maximum supported channel count for the ALSA device.
    """
    device_string = f"hw:{card},{device}"

    try:
        output = subprocess.check_output(
            ["arecord", "-D", device_string, "--dump-hw-params"],
            text=True,
            stderr=subprocess.STDOUT
        )
    except subprocess.CalledProcessError as e:
        print("Failed to query hw params:", e.output)
        return 2  # safe fallback

    for line in output.splitlines():
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
        filename
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


# ------------------------------------------------------------
# FLASK ROUTES
# ------------------------------------------------------------

@app.route("/")
def index():
    cards = list_capture_cards()
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
