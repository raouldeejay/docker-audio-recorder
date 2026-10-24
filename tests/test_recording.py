import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import app as recorder_app
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


class RecordingLibraryApiTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.original_root = recorder_app.RECORDINGS_ROOT
        self.original_recorder = recorder_app.wav_recorder
        self.original_direct_path = recorder_app.direct_record_path
        recorder_app.RECORDINGS_ROOT = self.root.name
        recorder_app.wav_recorder = None
        recorder_app.direct_record_path = None
        self.client = recorder_app.app.test_client()

    def tearDown(self):
        recorder_app.RECORDINGS_ROOT = self.original_root
        recorder_app.wav_recorder = self.original_recorder
        recorder_app.direct_record_path = self.original_direct_path
        self.root.cleanup()

    def test_lists_and_serves_supported_recordings(self):
        nested = Path(self.root.name, "album")
        nested.mkdir()
        recording = nested / "track.wav"
        recording.write_bytes(b"RIFF")
        Path(self.root.name, "notes.txt").write_text("not audio")

        listing = self.client.get("/api/recordings")

        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json["recordings"][0]["path"], "album/track.wav")
        self.assertEqual(listing.json["recordings"][0]["media_type"], "audio/wav")

        response = self.client.get("/api/recordings/file?path=album%2Ftrack.wav")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"RIFF")
        self.assertEqual(response.mimetype, "audio/wav")
        response.close()

    def test_rejects_path_traversal_and_symlinks(self):
        Path(self.root.name, "track.wav").write_bytes(b"RIFF")
        Path(self.root.name, "shortcut.wav").symlink_to(Path(self.root.name, "track.wav"))

        traversal = self.client.get("/api/recordings/file?path=..%2Fetc%2Fpasswd")
        symlink = self.client.get("/api/recordings/file?path=shortcut.wav")

        self.assertEqual(traversal.status_code, 400)
        self.assertEqual(symlink.status_code, 400)

    def test_deletes_completed_but_not_active_recordings(self):
        recording = Path(self.root.name, "track.aiff")
        recording.write_bytes(b"FORM")

        recorder_app.wav_recorder = SimpleNamespace(path=str(recording))
        active = self.client.delete("/api/recordings", json={"path": "track.aiff"})

        self.assertEqual(active.status_code, 409)
        self.assertTrue(recording.exists())

        recorder_app.wav_recorder = None
        deleted = self.client.delete("/api/recordings", json={"path": "track.aiff"})

        self.assertEqual(deleted.status_code, 200)
        self.assertFalse(recording.exists())

    def test_renames_completed_recording_in_its_existing_folder(self):
        album = Path(self.root.name, "album")
        album.mkdir()
        recording = album / "track.wav"
        recording.write_bytes(b"RIFF")

        renamed = self.client.patch(
            "/api/recordings",
            json={"path": "album/track.wav", "filename": "new title.wav"},
        )

        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json["path"], "album/new_title.wav")
        self.assertFalse(recording.exists())
        self.assertTrue((album / "new_title.wav").exists())

    def test_rejects_unsafe_conflicting_or_active_renames(self):
        recording = Path(self.root.name, "track.aiff")
        recording.write_bytes(b"FORM")
        Path(self.root.name, "other.aiff").write_bytes(b"FORM")

        traversal = self.client.patch(
            "/api/recordings",
            json={"path": "track.aiff", "filename": "../escape.aiff"},
        )
        extension = self.client.patch(
            "/api/recordings",
            json={"path": "track.aiff", "filename": "track.wav"},
        )
        conflict = self.client.patch(
            "/api/recordings",
            json={"path": "track.aiff", "filename": "other.aiff"},
        )

        self.assertEqual(traversal.status_code, 400)
        self.assertEqual(extension.status_code, 400)
        self.assertEqual(conflict.status_code, 409)

        recorder_app.wav_recorder = SimpleNamespace(path=str(recording))
        active = self.client.patch(
            "/api/recordings",
            json={"path": "track.aiff", "filename": "renamed.aiff"},
        )

        self.assertEqual(active.status_code, 409)
        self.assertTrue(recording.exists())


if __name__ == "__main__":
    unittest.main()
