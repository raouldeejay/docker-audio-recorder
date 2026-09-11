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

# Audio settings
CHUNK = 1024
FORMAT = pyaudio.paFloat32
CHANNELS = 2
RATE = 44100

# Global recording state
recording_state = {
    'is_recording': False,
    'current_file': None,
    'current_filename': None
}

def record_audio(filename, duration=None):
    """Record audio to file"""
    filepath = RECORDINGS_DIR / filename
    
    try:
        p = pyaudio.PyAudio()
        
        stream = p.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=RATE,
            input=True,
            frames_per_buffer=CHUNK
        )
        
        frames = []
        
        while recording_state['is_recording']:
            try:
                data = stream.read(CHUNK, exception_on_overflow=False)
                frames.append(data)
            except Exception as e:
                print(f"Error reading audio: {e}")
                break
        
        stream.stop_stream()
        stream.close()
        p.terminate()
        
        # Write WAV file
        with wave.open(str(filepath), 'wb') as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(p.get_sample_size(FORMAT))
            wf.setframerate(RATE)
            wf.writeframes(b''.join(frames))
        
        print(f"Recording saved: {filepath}")
        return True
        
    except Exception as e:
        print(f"Recording error: {e}")
        return False

@app.route('/')
def index():
    """Serve the web interface"""
    return render_template('index.html')

@app.route('/api/start', methods=['POST'])
def start_recording():
    """Start recording audio"""
    data = request.json
    filename = data.get('filename', f"recording_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav")
    
    if recording_state['is_recording']:
        return jsonify({'success': False, 'error': 'Already recording'}), 400
    
    recording_state['is_recording'] = True
    recording_state['current_filename'] = filename
    
    # Start recording in a separate thread
    thread = threading.Thread(target=record_audio, args=(filename,))
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
        'current_file': recording_state['current_filename']
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