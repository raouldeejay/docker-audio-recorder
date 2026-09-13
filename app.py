import os
import subprocess
import re
import wave
import threading
import time

from flask import Flask, render_template, request, jsonify
import pyaudio

app = Flask(__name__)

recording_thread = None
recording_active = False


# ------------------------------------------------------------
# ALSA / CARD DETECTION
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
    """
    Pick the first input-capable card.
    """
    cards = list_capture_cards()
    if not cards:
        return None, None, None
    c = cards[0]
    return c["card"], c["device"], c["name"]


# ------------------------------------------------------------
# INPUT SELECTOR (LINE / SPDIF) – DYNAMIC
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
# RECORDING
# ------------------------------------------------------------

def record_audio(filename, sample_rate=44100):
    global recording_active

    card, device, name = detect_capture_card()
    if card is None:
        print("ERROR: no capture card found")
        return

    device_string = f"hw:{card},{device}"
    print(f"Recording from {device_string} ({name})")

    audio = pyaudio.PyAudio()

    stream = audio.open(
        format=pyaudio.paInt16,
        channels=2,
        rate=sample_rate,
        input=True,
        input_device_index=None,
        frames_per_buffer=1024
    )

    wf = wave.open(filename, "wb")
    wf.setnchannels(2)
    wf.setsampwidth(audio.get_sample_size(pyaudio.paInt16))
    wf.setframerate(sample_rate)

    recording_active = True

    while recording_active:
        data = stream.read(1024, exception_on_overflow=False)
        wf.writeframes(data)

    stream.stop_stream()
    stream.close()
    audio.terminate()
    wf.close()


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


@app.route("/api/cards", methods=["GET"])
def api_cards():
    return jsonify(list_capture_cards())


@app.route("/api/start", methods=["POST"])
def api_start():
    global recording_thread, recording_active

    if recording_active:
        return jsonify({"error": "Already recording"}), 400

    filename = "recording.wav"
    recording_thread = threading.Thread(target=record_audio, args=(filename,))
    recording_thread.start()

    return jsonify({"status": "recording"})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    global recording_active
    recording_active = False
    time.sleep(0.5)
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
