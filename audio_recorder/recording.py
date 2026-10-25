"""Recording filenames, PCM containers, and format helpers."""

import datetime
import math
import os
import re
import struct
import subprocess
import threading

from werkzeug.utils import secure_filename


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
        ext = "aiff" if file_format == "aiff" else file_format
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
    elif lower.endswith(".flac"):
        filename = filename[:-5]
        file_format = "flac"

    ext = "aiff" if file_format == "aiff" else file_format
    parts = [secure_filename(part) for part in re.split(r"[\\/]+", filename)]
    parts = [part for part in parts if part]
    if not parts:
        raise ValueError("Filename must contain at least one letter or number")
    if len(parts) > 8:
        raise ValueError("Too many folder levels (maximum 7)")
    if any(len(part) > 255 - len(ext) - 1 for part in parts):
        raise ValueError("Filename is too long (maximum 255 characters per part)")

    return "/".join(parts[:-1] + [f"{parts[-1]}.{ext}"]), file_format


def resolve_recording_path(relname, recordings_root):
    """Create parent folders under recordings_root and return a collision-free name."""
    root = os.path.realpath(recordings_root)
    parts = relname.split("/")
    current = root
    resolved = []
    for part in parts[:-1]:
        candidate, suffix = part, 0
        while os.path.exists(os.path.join(current, candidate)) and not os.path.isdir(
            os.path.join(current, candidate)
        ):
            suffix += 1
            candidate = f"{part}_{suffix}"
        current = os.path.join(current, candidate)
        resolved.append(candidate)
    if os.path.commonpath([root, os.path.realpath(current)]) != root:
        raise ValueError("Invalid recording path")
    os.makedirs(current, exist_ok=True)

    stem, ext = os.path.splitext(parts[-1])
    candidate, suffix = parts[-1], 0
    while os.path.exists(os.path.join(current, candidate)):
        suffix += 1
        candidate = f"{stem}_{suffix}{ext}"
    resolved.append(candidate)
    return "/".join(resolved)


def get_audio_formats(bitdepth):
    formats = {
        8: ("U8", "u8"),
        16: ("S16_LE", "s16le"),
        24: ("S24_3LE", "s24le"),
        32: ("S32_LE", "s32le"),
    }
    try:
        return formats[bitdepth]
    except KeyError as exc:
        raise ValueError(f"Unsupported bit depth: {bitdepth}") from exc


def normalize_file_format(fmt):
    fmt = (fmt or "aiff").strip().lower().lstrip(".")
    return fmt if fmt in {"aiff", "wav", "flac"} else "aiff"


def wav_header(rate, channels, depth, data_bytes=0x7FFFF000):
    """Classic PCM WAV header (fmt=1)."""
    block_align = channels * (depth // 8)
    byte_rate = rate * block_align
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_bytes,
        b"WAVE",
        b"fmt ",
        16,
        1,
        channels,
        rate,
        byte_rate,
        block_align,
        depth,
        b"data",
        data_bytes,
    )


def _pcm_le_to_be(pcm, depth):
    """Byte-swap little-endian capture PCM to big-endian AIFF PCM."""
    width = max(1, int(depth) // 8)
    if width == 1 or not pcm:
        return pcm
    out = bytearray(len(pcm))
    aligned = (len(pcm) // width) * width
    for lane in range(width):
        out[lane:aligned:width] = pcm[width - 1 - lane:aligned:width]
    if aligned < len(pcm):
        out[aligned:] = pcm[aligned:]
    return bytes(out)


def _aiff_rate_extended(sample_rate):
    """80-bit SANE/AIFF extended float for sample rate (big-endian)."""
    rate = float(sample_rate)
    if rate <= 0:
        return b"\x00" * 10
    exponent = int(math.floor(math.log(rate, 2)))
    mantissa = int(rate * (2.0 ** (63 - exponent)))
    exponent += 16383
    return struct.pack(">HQ", exponent & 0xFFFF, mantissa & 0xFFFFFFFFFFFFFFFF)


def _aiff_header(rate, channels, depth, data_bytes=0):
    """Classic AIFF FORM/COMM/SSND header for big-endian PCM."""
    channels = int(channels)
    depth = int(depth)
    block_align = channels * (depth // 8)
    frames = data_bytes // block_align if block_align else 0
    comm = struct.pack(">LHLH", 18, channels, frames, depth) + _aiff_rate_extended(rate)
    ssnd_data_size = 8 + data_bytes
    form_size = 4 + (4 + len(comm)) + (8 + ssnd_data_size)
    return (
        b"FORM"
        + struct.pack(">I", form_size)
        + b"AIFF"
        + b"COMM"
        + comm
        + b"SSND"
        + struct.pack(">III", ssnd_data_size, 0, 0)
    )


class WavRecorder:
    """Write capture PCM into a classic PCM WAV (fmt=1)."""

    format_name = "wav"

    def __init__(self, path, rate, channels, depth):
        self.path = path
        self.rate = int(rate)
        self.channels = int(channels)
        self.depth = int(depth)
        self.data_bytes = 0
        self._lock = threading.Lock()
        self._fh = open(path, "wb", buffering=1024 * 1024)
        self._fh.write(wav_header(self.rate, self.channels, self.depth, data_bytes=0))

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
                wav_header(self.rate, self.channels, self.depth, data_bytes=self.data_bytes)
            )
            self._fh.flush()
            self._fh.close()
            return self.data_bytes


class AiffRecorder:
    """Write capture PCM as classic AIFF (big-endian PCM)."""

    format_name = "aiff"

    def __init__(self, path, rate, channels, depth):
        self.path = path
        self.rate = int(rate)
        self.channels = int(channels)
        self.depth = int(depth)
        self.data_bytes = 0
        self._lock = threading.Lock()
        self.metadata = {}
        self._fh = open(path, "wb", buffering=1024 * 1024)
        self._fh.write(_aiff_header(self.rate, self.channels, self.depth, data_bytes=0))

    def write(self, pcm):
        if not pcm:
            return
        converted = _pcm_le_to_be(pcm, self.depth)
        with self._lock:
            self._fh.write(converted)
            self.data_bytes += len(converted)

    def close(self):
        with self._lock:
            if self._fh.closed:
                return self.data_bytes
            self._fh.seek(0)
            self._fh.write(
                _aiff_header(self.rate, self.channels, self.depth, data_bytes=self.data_bytes)
            )
            self._fh.flush()
            self._fh.close()
            return self.data_bytes


class FlacRecorder:
    """Encode native capture PCM to FLAC through FFmpeg."""

    format_name = "flac"

    def __init__(self, path, rate, channels, depth):
        self.path = path
        self.rate = int(rate)
        self.channels = int(channels)
        self.depth = int(depth)
        self.data_bytes = 0
        self._lock = threading.Lock()
        pcm_format = {8: "u8", 16: "s16le", 24: "s24le", 32: "s32le"}.get(self.depth)
        if pcm_format is None:
            raise ValueError(f"Unsupported bit depth: {self.depth}")
        self._proc = subprocess.Popen(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                pcm_format,
                "-ar",
                str(self.rate),
                "-ac",
                str(self.channels),
                "-i",
                "pipe:0",
                "-c:a",
                "flac",
                "-y",
                path,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

    def write(self, pcm):
        if not pcm:
            return
        with self._lock:
            if self._proc.poll() is not None:
                raise RuntimeError("FLAC encoder stopped unexpectedly")
            self._proc.stdin.write(pcm)
            self.data_bytes += len(pcm)

    def close(self):
        with self._lock:
            if self._proc.stdin and not self._proc.stdin.closed:
                self._proc.stdin.close()
            return_code = self._proc.wait(timeout=30)
            error = ""
            if self._proc.stderr:
                if return_code:
                    error = self._proc.stderr.read().decode("utf-8", errors="replace").strip()
                self._proc.stderr.close()
            if return_code:
                raise RuntimeError(f"FLAC encoder failed: {error or return_code}")
            return self.data_bytes
