import tempfile
import unittest
from pathlib import Path

from audio_recorder.hardware import (
    find_input_selector,
    parse_capture_cards,
    parse_input_source,
    read_capture_hwcaps,
)
from audio_recorder.recording import (
    AiffRecorder,
    WavRecorder,
    clean_recording_filename,
    resolve_recording_path,
)


class RecordingFilenameTests(unittest.TestCase):
    def test_filename_sanitizes_directories_and_extension(self):
        filename, file_format = clean_recording_filename(
            "../Artist Name/01 - Track.wav", "aiff"
        )

        self.assertEqual(filename, "Artist_Name/01_-_Track.wav")
        self.assertEqual(file_format, "wav")

    def test_path_resolution_avoids_overwriting_existing_recordings(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "album").mkdir()
            Path(root, "album", "track.wav").touch()

            filename = resolve_recording_path("album/track.wav", root)

            self.assertEqual(filename, "album/track_1.wav")


class PcmRecorderTests(unittest.TestCase):
    def test_wav_recorder_finalizes_header_and_pcm(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "recording.wav")
            recorder = WavRecorder(path, 48000, 2, 16)
            recorder.write(b"\x01\x00\x02\x00")

            self.assertEqual(recorder.close(), 4)
            self.assertEqual(path.read_bytes()[44:], b"\x01\x00\x02\x00")
            self.assertEqual(int.from_bytes(path.read_bytes()[40:44], "little"), 4)

    def test_aiff_recorder_converts_pcm_to_big_endian(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "recording.aiff")
            recorder = AiffRecorder(path, 48000, 1, 16)
            recorder.write(b"\x34\x12")

            self.assertEqual(recorder.close(), 2)
            self.assertEqual(path.read_bytes()[-2:], b"\x12\x34")


class HardwareParsingTests(unittest.TestCase):
    def test_parses_capture_cards_and_input_selector(self):
        cards = parse_capture_cards(
            "card 2: USB [USB Audio], device 1: USB Audio [USB Audio]\n"
        )

        self.assertEqual(cards, [{"card": 2, "device": 1, "name": "USB Audio"}])
        self.assertEqual(
            find_input_selector(
                "numid=7,iface=MIXER,name='Capture'\n; type=ENUMERATED\n; items=Line IEC958 In"
            ),
            7,
        )

    def test_reads_capture_capabilities_and_input_source(self):
        with tempfile.TemporaryDirectory() as root:
            card = Path(root, "card1")
            card.mkdir()
            (card / "id").write_text("USB\n")
            (card / "stream0").write_text(
                "Playback:\nignored\nCapture:\n"
                "  Channels: 2\n  Bits: 16\n  Bits: 24\n  Rates: 44100, 48000\n"
            )

            caps = read_capture_hwcaps(1, root)

        self.assertEqual(caps["name"], "USB")
        self.assertEqual(caps["bitdepths"], [16, 24])
        self.assertEqual(caps["samplerates"], [44100, 48000])
        self.assertEqual(caps["channels"], 2)
        self.assertEqual(parse_input_source(": values=1"), "IEC958 In")


if __name__ == "__main__":
    unittest.main()
