# Docker Audio Recorder

A containerized audio recording application designed for Raspberry Pi with a web interface for easy recording management.

## Features

- 🎤 **Audio Recording**: Record audio from connected audio interfaces on Raspberry Pi
- 🌐 **Web Interface**: User-friendly web UI to start/stop recordings and manage files
- 🐳 **Docker Container**: Fully containerized for easy deployment
- 📁 **File Management**: List and delete recordings directly from the web interface
- ⚙️ **Configurable**: Easy to customize audio settings and metadata

## Requirements

- Docker & Docker Compose
- Raspberry Pi (or any Linux system with audio interface)
- Audio input device connected to the system

## Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/raouldeejay/docker-audio-recorder.git
cd docker-audio-recorder
```

### 2. Build and Run

```bash
docker-compose up --build
```

The web interface will be available at `http://localhost:5000`

### 3. Access the Web Interface

Open your browser and navigate to:
- Local: `http://localhost:5000`
- Remote (Raspberry Pi): `http://<raspberry-pi-ip>:5000`

## Usage

1. **Enter a filename** for your recording (optional - auto-generated if left blank)
2. **Click "Start Recording"** to begin capturing audio
3. **Click "Stop Recording"** to finish
4. **Manage recordings** in the "Recordings" section:
   - View file size and creation time
   - Delete recordings you no longer need

## Configuration

Edit `docker-compose.yml` to customize:

```yaml
environment:
  - AUDIO_DEVICE=default  # Change to specific audio device if needed
  - FLASK_ENV=production  # Set to 'development' for debug mode
```

## Audio Device Selection

To find available audio devices on your Raspberry Pi:

```bash
arecord -l  # List recording devices
```

Update the `AUDIO_DEVICE` in `docker-compose.yml` with your device (e.g., `hw:0,0`)

## Audio Settings

Modify audio recording settings in `app.py`:

```python
CHUNK = 1024          # Buffer size
FORMAT = pyaudio.paFloat32  # Audio format
CHANNELS = 2          # Number of channels (mono: 1, stereo: 2)
RATE = 44100          # Sample rate (Hz)
```

## Recordings Directory

Recordings are saved to `./recordings/` on the host machine. This directory is mounted as a volume in the container.

## Troubleshooting

### No Audio Input
- Verify audio device is connected
- Check device permissions: `ls -la /dev/snd/`
- Test recording with `arecord -D hw:0,0 test.wav`

### Permission Denied
- Ensure Docker has access to audio devices
- Add user to `audio` group: `sudo usermod -aG audio $USER`

### Web Interface Not Accessible
- Check if container is running: `docker-compose ps`
- Verify port binding: `docker-compose logs`

## Future Enhancements

- [ ] Metadata support (title, artist, description)
- [ ] Audio format conversion (MP3, FLAC)
- [ ] Recording scheduling
- [ ] Audio playback in web interface
- [ ] Cloud storage integration
- [ ] Advanced audio filters and effects

## License

MIT License

## Author

raouldeejay
