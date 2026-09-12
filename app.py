from flask import Flask, render_template, jsonify, request
import pyaudio
import wave
import os
import threading
from datetime import datetime
from pathlib import Path

app = Flask(__name__)

# Configuration
RECORDINGS_DIR = Path('/app/recordings')
RECORDINGS_DIR.mkdir(exist_ok=True)

# Audio format mappings
FORMAT_MAP = {
    pyaudio.paFloat32: {'name': 'Float32', 'bits': 32},
    pyaudio.paInt32: {'name': 'Int32', 'bits': 32},
    pyaudio.paInt24: {'name': 'Int24', 'bits': 24},
    pyaudio.paInt16: {'name': 'Int16', 'bits': 16},
    pyaudio.paInt8: {'name': 'Int8', 'bits': 8},
}

# Common sample rates to test
COMMON_SAMPLE_RATES = [8000, 16000, 22050, 44100, 48000, 96000, 192000]
COMMON_FORMATS = [pyaudio.paFloat32, pyaudio.paInt32, pyaudio.paInt16]

# Global recording state
recording_state = {
    'is_recording': False,
    'current_file': None,
    'current_filename': None,
    'selected_device': None,
    'sample_rate': 44100,
    'channels': 2,
    'format': pyaudio.paFloat32
}

def get_supported_sample_rates(device_index):
    """Get list of supported sample rates for a device"""
    p = pyaudio.PyAudio()
    supported_rates = []
    
    try:
        device_info = p.get_device_info_by_index(device_index)
        channels = int(device_info['maxInputChannels'])
        
        for rate in COMMON_SAMPLE_RATES:
            try:
                # Try to open a stream with this sample rate
                stream = p.open(
                    format=pyaudio.paFloat32,
                    channels=channels,
                    rate=rate,
                    input=True,
                    input_device_index=device_index,
                    frames_per_buffer=1024
                )
                stream.close()
                supported_rates.append(rate)
            except:
                pass
    finally:
        p.terminate()
    
    return supported_rates if supported_rates else [44100]

def get_supported_formats(device_index, sample_rate):
    """Get list of supported formats for a device"""
    p = pyaudio.PyAudio()
    supported_formats = []
    
    try:
        device_info = p.get_device_info_by_index(device_index)
        channels = int(device_info['maxInputChannels'])
        
        for fmt in COMMON_FORMATS:
            try:
                stream = p.open(
                    format=fmt,
                    channels=channels,
                    rate=sample_rate,
                    input=True,
                    input_device_index=device_index,
                    frames_per_buffer=1024
                )
                stream.close()
                format_info = FORMAT_MAP.get(fmt, {'name': 'Unknown', 'bits': 0})
                supported_formats.append({
                    'format': fmt,
                    'name': format_info['name'],
                    'bits': format_info['bits']
                })
            except:
                pass
    finally:
        p.terminate()
    
    return supported_formats if supported_formats else [{'format': pyaudio.paFloat32, 'name': 'Float32', 'bits': 32}]

def get_audio_devices():
    """Get list of available audio input devices with their capabilities"""
    p = pyaudio.PyAudio()
    devices = []
    
    for i in range(p.get_device_count()):
        device_info = p.get_device_info_by_index(i)
        # Only include devices that have input channels
        if device_info['maxInputChannels'] > 0:
            sample_rates = get_supported_sample_rates(i)
            formats = get_supported_formats(i, sample_rates[0] if sample_rates else 44100)
            
            devices.append({
                'index': i,
                'name': device_info['name'],
                'channels': device_info['maxInputChannels'],
                'defaultSampleRate': int(device_info['defaultSampleRate']),
                'supportedSampleRates': sample_rates,
                'supportedFormats': formats
            })
    
    p.terminate()
    return devices

def record_audio(filename, device_index=None, sample_rate=None, channels=None, audio_format=None):
    """Record audio to file from specified device"""
    filepath = RECORDINGS_DIR / filename
    
    if sample_rate is None:
        sample_rate = recording_state['sample_rate']
    if channels is None:
        channels = recording_state['channels']
    if audio_format is None:
        audio_format = recording_state['format']
    
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
        
        frames = []
        
        while recording_state['is_recording']:
            try:
                data = stream.read(1024, exception_on_overflow=False)
                frames.append(data)
            except Exception as e:
                print(f"Error reading audio: {e}")
                break
        
        stream.stop_stream()
        stream.close()
        
        # Write WAV file
        with wave.open(str(filepath), 'wb') as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(p.get_sample_size(audio_format))
            wf.setframerate(sample_rate)
            wf.writeframes(b''.join(frames))
        
        p.terminate()
        print(f"Recording saved: {filepath}")
        return True
        
    except Exception as e:
        print(f"Recording error: {e}")
        return False

@app.route('/')
def index():
    """Serve the web interface"""
    return render_template('index.html')

@app.route('/api/devices', methods=['GET'])
def get_devices():
    """Get list of available audio input devices"""
    try:
        devices = get_audio_devices()
        return jsonify({'success': True, 'devices': devices})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/device/<int:device_index>', methods=['GET'])
def get_device_info(device_index):
    """Get detailed info about a specific device"""
    try:
        devices = get_audio_devices()
        device = next((d for d in devices if d['index'] == device_index), None)
        
        if not device:
            return jsonify({'success': False, 'error': 'Device not found'}), 404
        
        return jsonify({'success': True, 'device': device})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/audio-settings', methods=['GET'])
def get_audio_settings():
    """Get current audio settings"""
    return jsonify({
        'success': True,
        'settings': {
            'sampleRate': recording_state['sample_rate'],
            'channels': recording_state['channels'],
            'format': recording_state['format']
        }
    })

@app.route('/api/audio-settings', methods=['POST'])
def set_audio_settings():
    """Set audio settings"""
    data = request.json
    
    if 'sampleRate' in data:
        recording_state['sample_rate'] = int(data['sampleRate'])
    if 'channels' in data:
        recording_state['channels'] = int(data['channels'])
    if 'format' in data:
        recording_state['format'] = int(data['format'])
    
    return jsonify({
        'success': True,
        'settings': {
            'sampleRate': recording_state['sample_rate'],
            'channels': recording_state['channels'],
            'format': recording_state['format']
        }
    })

@app.route('/api/start', methods=['POST'])
def start_recording():
    """Start recording audio"""
    data = request.json
    filename = data.get('filename', f"recording_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav")
    device_index = data.get('device_index')
    sample_rate = data.get('sample_rate', recording_state['sample_rate'])
    channels = data.get('channels', recording_state['channels'])
    audio_format = data.get('format', recording_state['format'])
    
    if device_index is None:
        return jsonify({'success': False, 'error': 'No audio device selected'}), 400
    
    if recording_state['is_recording']:
        return jsonify({'success': False, 'error': 'Already recording'}), 400
    
    recording_state['is_recording'] = True
    recording_state['current_filename'] = filename
    recording_state['selected_device'] = device_index
    recording_state['sample_rate'] = sample_rate
    recording_state['channels'] = channels
    recording_state['format'] = audio_format
    
    # Start recording in a separate thread
    thread = threading.Thread(
        target=record_audio,
        args=(filename, device_index, sample_rate, channels, audio_format)
    )
    thread.daemon = True
    thread.start()
    
    return jsonify({'success': True, 'message': f'Recording started: {filename}'})

@app.route('/api/stop', methods=['POST'])
def stop_recording():
    """Stop recording audio"""
    if not recording_state['is_recording']:
        return jsonify({'success': False, 'error': 'Not recording'}), 400
    
    recording_state['is_recording'] = False
    filename = recording_state['current_filename']
    
    return jsonify({'success': True, 'message': f'Recording stopped: {filename}'})

@app.route('/api/status', methods=['GET'])
def get_status():
    """Get current recording status"""
    return jsonify({
        'is_recording': recording_state['is_recording'],
        'current_file': recording_state['current_filename'],
        'settings': {
            'sampleRate': recording_state['sample_rate'],
            'channels': recording_state['channels'],
            'format': recording_state['format']
        }
    })

@app.route('/api/recordings', methods=['GET'])
def list_recordings():
    """List all recordings"""
    recordings = []
    for file in RECORDINGS_DIR.glob('*.wav'):
        recordings.append({
            'filename': file.name,
            'size': file.stat().st_size,
            'created': datetime.fromtimestamp(file.stat().st_ctime).isoformat()
        })
    return jsonify({'recordings': sorted(recordings, key=lambda x: x['created'], reverse=True)})

@app.route('/api/delete/<filename>', methods=['DELETE'])
def delete_recording(filename):
    """Delete a recording"""
    filepath = RECORDINGS_DIR / filename
    
    if not filepath.exists():
        return jsonify({'success': False, 'error': 'File not found'}), 404
    
    if not str(filepath).startswith(str(RECORDINGS_DIR)):
        return jsonify({'success': False, 'error': 'Invalid file path'}), 400
    
    try:
        filepath.unlink()
        return jsonify({'success': True, 'message': f'Deleted: {filename}'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
