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

# Audio format mappings - prioritize commonly supported formats
FORMAT_MAP = {
    pyaudio.paInt16: {'name': 'Int16', 'bits': 16},
    pyaudio.paInt24: {'name': 'Int24', 'bits': 24},
    pyaudio.paInt32: {'name': 'Int32', 'bits': 32},
    pyaudio.paFloat32: {'name': 'Float32', 'bits': 32},
}

# Common sample rates to test (most to least common)
COMMON_SAMPLE_RATES = [44100, 48000, 22050, 16000, 96000, 192000, 8000]

# Audio formats to test - prioritize 16-bit and 24-bit
COMMON_FORMATS = [
    pyaudio.paInt16,    # 16-bit (most common)
    pyaudio.paInt24,    # 24-bit (pro audio)
    pyaudio.paInt32,    # 32-bit
    pyaudio.paFloat32,  # Float32
]

# Global recording state
recording_state = {
    'is_recording': False,
    'current_file': None,
    'current_filename': None,
    'selected_device': None,
    'sample_rate': 44100,
    'channels': 2,
    'format': pyaudio.paInt16  # Default to 16-bit
}

def get_supported_sample_rates(device_index, is_input=True):
    """Get list of supported sample rates for a device (input or output)"""
    p = pyaudio.PyAudio()
    supported_rates = []
    
    try:
        device_info = p.get_device_info_by_index(device_index)
        channels = int(device_info['maxInputChannels']) if is_input else int(device_info['maxOutputChannels'])
        
        if channels == 0:
            return []
        
        # Try common sample rates
        for rate in COMMON_SAMPLE_RATES:
            try:
                # Try with Int16 first as it's most commonly supported
                if is_input:
                    stream = p.open(
                        format=pyaudio.paInt16,
                        channels=channels,
                        rate=rate,
                        input=True,
                        input_device_index=device_index,
                        frames_per_buffer=1024
                    )
                else:
                    stream = p.open(
                        format=pyaudio.paInt16,
                        channels=channels,
                        rate=rate,
                        output=True,
                        output_device_index=device_index,
                        frames_per_buffer=1024
                    )
                stream.close()
                supported_rates.append(rate)
            except Exception as e:
                pass
    finally:
        p.terminate()
    
    # Return sorted rates or default
    return sorted(supported_rates) if supported_rates else []

def get_supported_formats(device_index, sample_rate, is_input=True):
    """Get list of supported formats for a device (input or output)"""
    p = pyaudio.PyAudio()
    supported_formats = []
    
    try:
        device_info = p.get_device_info_by_index(device_index)
        channels = int(device_info['maxInputChannels']) if is_input else int(device_info['maxOutputChannels'])
        
        if channels == 0:
            return []
        
        # Try each format
        for fmt in COMMON_FORMATS:
            try:
                if is_input:
                    stream = p.open(
                        format=fmt,
                        channels=channels,
                        rate=sample_rate,
                        input=True,
                        input_device_index=device_index,
                        frames_per_buffer=1024
                    )
                else:
                    stream = p.open(
                        format=fmt,
                        channels=channels,
                        rate=sample_rate,
                        output=True,
                        output_device_index=device_index,
                        frames_per_buffer=1024
                    )
                stream.close()
                
                format_info = FORMAT_MAP.get(fmt, {'name': 'Unknown', 'bits': 0})
                supported_formats.append({
                    'format': fmt,
                    'name': format_info['name'],
                    'bits': format_info['bits']
                })
            except Exception as e:
                # Format not supported, skip it
                pass
    finally:
        p.terminate()
    
    return supported_formats

def merge_capabilities(input_rates, output_rates, input_formats, output_formats):
    """Merge input and output capabilities - return common/recommended settings"""
    # Get intersection of sample rates
    common_rates = sorted(set(input_rates) & set(output_rates)) if output_rates else input_rates
    
    # Get intersection of formats
    input_format_set = {f['format'] for f in input_formats}
    output_format_set = {f['format'] for f in output_formats} if output_formats else input_format_set
    common_format_codes = input_format_set & output_format_set
    
    # Map back to format info
    common_formats = [f for f in input_formats if f['format'] in common_format_codes]
    if not common_formats:
        common_formats = input_formats  # Fallback to input-only if no common formats
    
    return {
        'input_rates': input_rates,
        'output_rates': output_rates,
        'common_rates': common_rates,
        'input_formats': input_formats,
        'output_formats': output_formats,
        'common_formats': common_formats
    }

def get_audio_devices():
    """Get list of available audio input devices with their capabilities"""
    p = pyaudio.PyAudio()
    devices = []
    
    for i in range(p.get_device_count()):
        try:
            device_info = p.get_device_info_by_index(i)
            # Only include devices that have input channels
            if device_info['maxInputChannels'] > 0:
                # Get input capabilities
                input_sample_rates = get_supported_sample_rates(i, is_input=True)
                if not input_sample_rates:
                    continue  # Skip device if can't get input rates
                
                default_rate = int(device_info['defaultSampleRate'])
                probe_rate = default_rate if default_rate in input_sample_rates else input_sample_rates[0]
                
                input_formats = get_supported_formats(i, probe_rate, is_input=True)
                
                # Get output capabilities (if device supports output)
                output_sample_rates = []
                output_formats = []
                if device_info['maxOutputChannels'] > 0:
                    output_sample_rates = get_supported_sample_rates(i, is_input=False)
                    if output_sample_rates:
                        output_probe_rate = default_rate if default_rate in output_sample_rates else output_sample_rates[0]
                        output_formats = get_supported_formats(i, output_probe_rate, is_input=False)
                
                # Merge capabilities
                capabilities = merge_capabilities(input_sample_rates, output_sample_rates, input_formats, output_formats)
                
                devices.append({
                    'index': i,
                    'name': device_info['name'],
                    'channels': {
                        'input': int(device_info['maxInputChannels']),
                        'output': int(device_info['maxOutputChannels'])
                    },
                    'defaultSampleRate': default_rate,
                    'capabilities': capabilities
                })
        except Exception as e:
            print(f"Error probing device {i}: {e}")
            pass
    
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
        format_name = FORMAT_MAP.get(audio_format, {}).get('name', 'Unknown')
        print(f"Recording saved: {filepath} ({sample_rate}Hz, {format_name}, {channels}ch)")
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
