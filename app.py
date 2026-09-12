from flask import Flask, render_template, jsonify, request
import pyaudio
import wave
import threading
from datetime import datetime
from pathlib import Path

app = Flask(__name__)

# Directory for recordings
RECORDINGS_DIR = Path('/app/recordings')
RECORDINGS_DIR.mkdir(exist_ok=True)

# Map PyAudio formats to human-readable names
FORMAT_MAP = {
    pyaudio.paInt16: {'name': '16-bit', 'bits': 16},
    pyaudio.paInt24: {'name': '24-bit', 'bits': 24},
}

# Global recording state
recording_state = {
    'is_recording': False,
    'current_filename': None,
    'selected_device': None,
    'sample_rate': None,
    'channels': None,
    'format': None,
    'last_error': None
}

# -------------------------------
# DEVICE CAPABILITY PROBING
# -------------------------------

def probe_device_capabilities(device_index):
    """Probe ALSA/PyAudio to determine real hardware capabilities."""
    p = pyaudio.PyAudio()
    info = p.get_device_info_by_index(device_index)

    channels = int(info['maxInputChannels'])

    # Probe sample rates
    supported_rates = []
    for rate in [8000, 16000, 22050, 44100, 48000]:
        try:
            stream = p.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=rate,
                input=True,
                input_device_index=device_index
            )
            stream.close()
            supported_rates.append(rate)
        except Exception:
            pass

    # Probe formats
    supported_formats = []
    for fmt in [pyaudio.paInt16, pyaudio.paInt24]:
        try:
            stream = p.open(
                format=fmt,
                channels=channels,
                rate=supported_rates[0] if supported_rates else 44100,
                input=True,
                input_device_index=device_index
            )
            stream.close()
            
            supported_formats.append({
                "format": fmt,
                "name": FORMAT_MAP[fmt]["name"],
                "bits": FORMAT_MAP[fmt]["bits"]
            })

        except Exception:
            pass

    p.terminate()

    return {
        "channels": channels,
        "sampleRates": supported_rates,
        "formats": supported_formats
    }


def get_audio_devices():
    """List all ALSA input devices with dynamic capabilities."""
    p = pyaudio.PyAudio()
    devices = []

    for i in range(p.get_device_count()):
        try:
            info = p.get_device_info_by_index(i)
            if info['maxInputChannels'] > 0:
                caps = probe_device_capabilities(i)
                devices.append({
                    'index': i,
                    'name': info['name'],
                    'channels': caps['channels'],
                    'supportedSampleRates': caps['sampleRates'],
                    'supportedFormats': caps['formats']
                })
        except Exception:
            pass

    p.terminate()
    return devices

# -------------------------------
# RECORDING ENGINE
# -------------------------------

def record_audio(filename, device_index, sample_rate, channels, audio_format):
    filepath = RECORDINGS_DIR / filename

    try:
        p = pyaudio.PyAudio()

        stream = p.open(
            format=audio_format,
            channels=channels,
            rate=sample_rate,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=1024
        )

        stream.start_stream()
        frames = []

        while recording_state['is_recording']:
            try:
                data = stream.read(1024, exception_on_overflow=False)
                frames.append(data)
            except Exception as e:
                recording_state['last_error'] = f"Audio read error: {e}"
                break

        stream.stop_stream()
        stream.close()
        p.terminate()

        if frames:
            with wave.open(str(filepath), 'wb') as wf:
                wf.setnchannels(channels)
                wf.setsampwidth(p.get_sample_size(audio_format))
                wf.setframerate(sample_rate)
                wf.writeframes(b''.join(frames))
        else:
            recording_state['last_error'] = "No audio frames captured"

    except Exception as e:
        recording_state['last_error'] = f"Recording error: {e}"

# -------------------------------
# API ENDPOINTS
# -------------------------------

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/devices')
def api_devices():
    return jsonify({'success': True, 'devices': get_audio_devices()})


@app.route('/api/device/<int:device_index>')
def api_device(device_index):
    devices = get_audio_devices()
    device = next((d for d in devices if d['index'] == device_index), None)

    if not device:
        return jsonify({'success': False, 'error': 'Device not found'}), 404

    return jsonify({'success': True, 'device': device})


@app.route('/api/start', methods=['POST'])
def api_start():
    data = request.json
    device_index = data.get('device_index')

    if device_index is None:
        return jsonify({'success': False, 'error': 'No device selected'}), 400

    devices = get_audio_devices()
    device = next((d for d in devices if d['index'] == device_index), None)

    if not device:
        return jsonify({'success': False, 'error': 'Invalid device index'}), 400

    # Dynamic capabilities
    caps = probe_device_capabilities(device_index)

    # Requested settings
    # Normalize incoming values
    requested_rate = data.get('sample_rate')
    if requested_rate is not None:
        requested_rate = int(requested_rate)

    requested_format = data.get('format')
    if isinstance(requested_format, dict):
        requested_format = requested_format.get("format")
    if requested_format is not None:
        requested_format = int(requested_format)

    # Enforce valid sample rate
    if requested_rate not in caps['sampleRates']:
        sample_rate = caps['sampleRates'][0]
    else:
        sample_rate = requested_rate

    # Enforce valid format
    if requested_format not in caps['formats']:
        audio_format = caps['formats'][0]
    else:
        audio_format = requested_format

    channels = caps['channels']

    filename = data.get(
        'filename',
        f"recording_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav"
    )

    recording_state.update({
        'is_recording': True,
        'current_filename': filename,
        'selected_device': device_index,
        'sample_rate': sample_rate,
        'channels': channels,
        'format': audio_format,
        'last_error': None
    })

    thread = threading.Thread(
        target=record_audio,
        args=(filename, device_index, sample_rate, channels, audio_format)
    )
    thread.daemon = True
    thread.start()

    return jsonify({'success': True, 'message': f"Recording started: {filename}"})


@app.route('/api/stop', methods=['POST'])
def api_stop():
    if not recording_state['is_recording']:
        return jsonify({'success': False, 'error': 'Not recording'}), 400

    recording_state['is_recording'] = False
    return jsonify({'success': True, 'message': f"Recording stopped: {recording_state['current_filename']}"})


@app.route('/api/status')
def api_status():
    return jsonify({
        'success': True,
        'is_recording': recording_state['is_recording'],
        'current_file': recording_state['current_filename'],
        'last_error': recording_state['last_error'],
        'settings': {
            'sampleRate': recording_state['sample_rate'],
            'channels': recording_state['channels'],
            'format': recording_state['format']
        }
    })


@app.route('/api/recordings')
def api_recordings():
    files = []
    for file in RECORDINGS_DIR.glob('*.wav'):
        files.append({
            'filename': file.name,
            'size': file.stat().st_size,
            'created': datetime.fromtimestamp(file.stat().st_ctime).isoformat()
        })
    return jsonify({'success': True, 'recordings': files})


@app.route('/api/delete/<filename>', methods=['DELETE'])
def api_delete(filename):
    filepath = RECORDINGS_DIR / filename
    if not filepath.exists():
        return jsonify({'success': False, 'error': 'File not found'}), 404

    filepath.unlink()
    return jsonify({'success': True, 'message': f"Deleted: {filename}"})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
