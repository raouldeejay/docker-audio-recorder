"""Pure parsing and sysfs helpers for ALSA capture hardware."""

import re
from pathlib import Path


def parse_capture_cards(output):
    """Parse ``arecord -l`` output into capture card dictionaries."""
    cards = []
    current_card = None
    for line in output.splitlines():
        card_match = re.search(r"card (\d+): ([^[]+)\[([^\]]+)\]", line)
        if card_match:
            current_card = {
                "card": int(card_match.group(1)),
                "name": card_match.group(3).strip(),
            }

        device_match = re.search(r"device (\d+): ([^[]+)\[([^\]]+)\]", line)
        if device_match and current_card:
            current_card["device"] = int(device_match.group(1))
            cards.append(current_card)
            current_card = None
    return cards


def read_capture_hwcaps(card_index, asound_root="/mnt/asound"):
    """Read non-exclusive capture capabilities from mounted ``/proc/asound``."""
    id_file = Path(asound_root, f"card{card_index}", "id")
    stream_file = Path(asound_root, f"card{card_index}", "stream0")
    caps = {
        "name": id_file.read_text().strip() if id_file.exists() else None,
        "bitdepths": None,
        "samplerates": None,
        "channels": None,
    }

    if not stream_file.exists():
        caps.update({"bitdepths": [16, 24], "samplerates": [48000], "channels": 2})
        return caps

    sections = re.split(r"^(Playback|Capture):", stream_file.read_text(), flags=re.MULTILINE)
    capture_block = ""
    current_mode = None
    for item in sections:
        if item in ("Playback", "Capture"):
            current_mode = item
        elif current_mode == "Capture":
            capture_block = item
            break

    if not capture_block:
        return caps

    bitdepths = sorted({int(value) for value in re.findall(r"Bits:\s*(\d+)", capture_block)})
    samplerates = []
    for rates in re.findall(r"Rates:\s*(.+)", capture_block):
        samplerates.extend(int(rate.strip()) for rate in rates.split(",") if rate.strip().isdigit())
    channels = [int(value) for value in re.findall(r"Channels:\s*(\d+)", capture_block)]
    caps.update(
        {
            "bitdepths": bitdepths or [16],
            "samplerates": sorted(set(samplerates)) or [48000],
            "channels": max(channels, default=2),
        }
    )
    return caps


def find_input_selector(amixer_contents):
    """Return the Line/IEC958 input-selector numid, if an interface exposes one."""
    for block in amixer_contents.split("numid=")[1:]:
        if "ENUMERATED" in block and "Line" in block and "IEC958 In" in block:
            try:
                return int(block.split(",", 1)[0])
            except ValueError:
                continue
    return None


def parse_input_source(amixer_contents):
    """Return the selected Line or IEC958 input from ``amixer cget`` output."""
    for line in amixer_contents.splitlines():
        value = line.strip()
        if value.startswith(": values="):
            return "Line" if value.split("=", 1)[1].split(",", 1)[0].strip() == "0" else "IEC958 In"
    return "Line"
