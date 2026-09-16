# Live Audio Pipeline: Decoupled Multi-Format Recording & Clarity M Loudness Radar

This blueprint implements a high-performance, real-time audio ecosystem tailored for a Raspberry Pi 4 inside Docker. The backend captures raw hardware PCM streams using `arecord` and multiplexes the audio data completely in RAM. This data is fed simultaneously to a local multi-format storage encoder (`ffmpeg`) and custom browser-monitoring streams. 

The frontend implements the **ITU-R BS.1770 / EBU R128 specifications** via the client-side Web Audio API to deliver a real-time **Clarity M-style analytical toolkit** (including Momentary LUFS, Short-Term LUFS, and a Loudness Radar Display) without placing any processing load on the Raspberry Pi CPU.

---

## 1. System & Lifecycle Architecture

The audio capture engine (`arecord`) operates independently of individual consumer endpoints. This allows you to connect, disconnect, start, or stop the browser monitor at any time without creating audio drops, pops, or artifacts in the active file recording.

### Lifecycle Sequence Example

[User Action]            [Hardware Status]                 [Data Stream Routing Path]───────────────────────────────────────────────────────────────────────────────────────────────Start Monitoring  ──► arecord initialises          ──► Raw PCM ──► Python Queue ──► Browser StreamStart Recording   ──► Storage Encoder initialises  ──► Raw PCM ──► Split Path   ──► Browser & Storage FileStop Monitoring   ──► Browser client disconnects   ──► Raw PCM ──► Local Storage File OnlyStop Recording    ──► Storage Encoder flushes      ──► Pipeline teardown completely closed
### Stream Routing Layout
┌──► [Storage FFmpeg Process] ──► Target File (.aiff, .wav, .flac, .mp3)│     (Active if Recording == True)[arecord Process] ──────┼(Raw Hardware Stream)  │└──► [Thread-Safe Client Queues] ──► [Live FFmpeg Transcoder] ──► Web Client Monitor(Active if Listener Count > 0)
---

## 2. Backend Engine Integration (`app.py`)

Implement this thread-safe state management logic within your Flask framework. It automatically mounts and unmounts encoding pipelines dynamically and distributes raw binary buffers across active components.

```python
import subprocess
import threading
import queue
from flask import Flask, Response, jsonify

# Shared global execution pointers
arecord_process = None
storage_ffmpeg_process = None
connected_listeners = []
listeners_lock = threading.Lock()
multiplexer_thread = None

def ensure_arecord_running(card=0, device=0, samplerate=44100, bitdepth=16):
    """
    Spins up the hardware receiver process if it isn't already active.
    Serves both initial monitoring requests or immediate recording tasks.
    """
    global arecord_process, multiplexer_thread
    if arecord_process is not None and arecord_process.poll() is None:
        return
        
    fmt = "S16_LE" if bitdepth == 16 else "S24_3LE"
    cmd = [
        "arecord", "-D", f"hw:{card},{device}", 
        "-t", "raw", "-f", fmt, "-r", str(samplerate), "-c", "2"
    ]
    
    arecord_process = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    
    # Run the dynamic data router loop in a persistent background worker thread
    multiplexer_thread = threading.Thread(target=audio_multiplexer_loop, daemon=True)
    multiplexer_thread.start()

def audio_multiplexer_loop():
    """
    In-memory data splitter. Clones hardware bytes chunks across 
    all active storage endpoints and streaming consumer queues.
    """
    global arecord_process, storage_ffmpeg_process
    chunk_size = 2048
    
    while arecord_process and arecord_process.poll() is None:
        try:
            data = arecord_process.stdout.read(chunk_size)
            if not data: 
                break
                
            # Stream directly to local storage if writer is active
            if storage_ffmpeg_process and storage_ffmpeg_process.poll() is None:
                try: 
                    storage_ffmpeg_process.stdin.write(data)
                except IOError: 
                    pass
                    
            # Stream directly to web browser instances
            with listeners_lock:
                for q in connected_listeners:
                    try: 
                        q.put_nowait(data)
                    except queue.Full: 
                        pass # Drop chunk if browser buffer stalls to prevent UI lockup
                        
        except Exception: 
            break
            
    cleanup_audio_engine()

def cleanup_audio_engine():
    """
    Auto-teardown monitor. Kills the arecord device worker only when 
    BOTH local recording and remote network clients are disconnected.
    """
    global arecord_process, storage_ffmpeg_process
    with listeners_lock: 
        active_listeners = len(connected_listeners)
        
    if storage_ffmpeg_process is None and active_listeners == 0 and arecord_process:
        arecord_process.terminate()
        arecord_process = None

@app.route("/api/start", methods=["POST"])
def api_start():
    global storage_ffmpeg_process
    if storage_ffmpeg_process is not None:
        return jsonify({"error": "Recording already running"}), 400
        
    # Example format target configuration (e.g. AIFF, WAV, FLAC, MP3)
    format_choice = "aiff" 
    FORMAT_CONFIGS = {
        "aiff": {"ext": ".aiff", "codec": ["-c:a", "pcm_s16be"]}, # Native Big Endian PCM
        "wav":  {"ext": ".wav",  "codec": ["-c:a", "pcm_s16le"]}, # Native Little Endian PCM
        "flac": {"ext": ".flac", "codec": ["-c:a", "flac"]}       # Compressed Lossless
    }
    selected = FORMAT_CONFIGS.get(format_choice, FORMAT_CONFIGS["aiff"])
    
    ensure_arecord_running()
    
    # Map storage encoder to ingest the raw PCM stream from the multiplexer
    storage_ffmpeg_process = subprocess.Popen([
        "ffmpeg", "-y", "-thread_queue_size", "2048",
        "-f", "s16le", "-ar", "44100", "-ac", "2", "-i", "pipe:0"
    ] + selected["codec"] + [f"output/recording{selected['ext']}"], stdin=subprocess.PIPE)
    
    return jsonify({"status": "recording"})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    global storage_ffmpeg_process
    if storage_ffmpeg_process:
        storage_ffmpeg_process.stdin.close()
        storage_ffmpeg_process.wait()
        storage_ffmpeg_process = None
    cleanup_audio_engine()
    return jsonify({"status": "stopped"})

@app.route("/api/stream")
def live_audio_stream():
    """
    Flask route that serves an interactive live transcode of the audio
    stream directly to an HTML5 audio element.
    """
    ensure_arecord_running()
    
    def generate_browser_stream():
        q = queue.Queue(maxsize=200)
        with listeners_lock: 
            connected_listeners.append(q)
            
        # Transcode to high-quality streaming MP3 container for browser native playback
        transcoder = subprocess.Popen([
            "ffmpeg", "-f", "s16le", "-ar", "44100", "-ac", "2", "-i", "pipe:0",
            "-c:a", "libmp3lame", "-b:a", "192k", "-f", "mp3", "pipe:1"
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        
        # Async stream writer pipe
        threading.Thread(target=lambda: [transcoder.stdin.write(c) for c in iter(q.get, None) if c], daemon=True).start()
        
        try:
            while transcoder.poll() is None:
                out = transcoder.stdout.read(1024)
                if not out: break
                yield out
        finally:
            with listeners_lock:
                if q in connected_listeners: connected_listeners.remove(q)
            q.put(None)
            transcoder.terminate()
            cleanup_audio_engine()
            
    return Response(generate_browser_stream(), mimetype="audio/mpeg")
```

---

## 3. Frontend Clarity M Analytics Engine (`index.html`)

This comprehensive implementation uses an **HTML5 Canvas**, **BiquadFilterNodes** for K-Weighting equalization (BS.1770), and pool coordinate mapping to render an active **Clarity M Loudness Radar View** and True-Peak level meter.

### HTML Layout
```html
<div class="audio-control-panel">
    <h2>Broadcast Monitoring Desk</h2>
    <button id="monitorBtn" onclick="toggleMonitor()">Luister Live</button>
    <audio id="livePlayer" style="display:none;" preload="none" crossorigin="anonymous"></audio>

    <div class="clarity-suite" style="display: flex; gap: 20px; margin-top: 20px; background: #141414; padding: 20px; border-radius: 8px; border: 1px solid #2d2d2d; max-width: 800px;">
        <!-- Canvas Element: Loudness Radar -->
        <div>
            <label style="color:#888; font-family:sans-serif; display:block; margin-bottom:8px;">Loudness Radar History (Short-Term)</label>
            <canvas id="radarCanvas" width="300" height="300" style="background: #090909; border-radius: 50%; border: 2px solid #222;"></canvas>
        </div>

        <!-- Numeric and Linear VU Meter Display -->
        <div style="flex-grow: 1; display: flex; flex-direction: column; justify-content: center; font-family: monospace; color: #fff;">
            <div style="margin-bottom: 15px;">
                <span style="color: #666;">MOMENTARY:</span> 
                <span id="momentaryTxt" style="font-size: 24px; color: #2ecc71; font-weight: bold;">-70.0 LUFS</span>
            </div>
            <div style="margin-bottom: 25px;">
                <span style="color: #666;">SHORT-TERM:</span> 
                <span id="shortTermTxt" style="font-size: 24px; color: #3498db; font-weight: bold;">-70.0 LUFS</span>
            </div>

            <label style="color:#888; margin-bottom:5px; font-size:12px;">TRUE PEAK METER (dBFS)</label>
            <div style="background: #222; width: 100%; height: 25px; border-radius: 4px; overflow: hidden; border: 1px solid #333;">
                <div id="peakBar" style="background: linear-gradient(to right, #2ecc71 70%, #f1c40f 85%, #e74c3c 100%); width: 0%; height: 100%; transition: width 0.05s ease;"></div>
            </div>
            <span id="peakTxt" style="text-align: right; font-size: 11px; color: #aaa; margin-top: 4px;">Peak: -Inf dBFS</span>
        </div>
    </div>
</div>
```

### JavaScript Processing Core
```javascript
let isMonitoring = false;
let latencyInterval = null;
let animationFrameId = null;

// Web Audio Pipeline Architecture Nodes
let audioContext = null;
let audioSource = null;
let kWeightingFilter = null;
let lufsAnalyser = null;

// Radar Plot Configuration Tracking Vars
let radarAngle = 0;
let shortTermBuffer = [];

function toggleMonitor() {
    const player = document.getElementById('livePlayer');
    const btn = document.getElementById('monitorBtn');
    
    if (!isMonitoring) {
        player.src = "/api/stream?t=" + new Date().getTime();
        player.muted = false;
        
        player.play().then(() => {
            btn.innerText = "Stop Luisteren";
            isMonitoring = true;
            setupWebAudioProcessing(player);
            startLatencyTracker(player);
        }).catch(err => console.error("Playback Initialization Failed:", err));
    } else {
        stopMonitoringGracefully(player, btn);
    }
}

function setupWebAudioProcessing(player) {
    if (!audioContext) {
        audioContext = new (window.AudioContext || window.webkitAudioContext)();
    }
    if (!audioSource) {
        // CROSSORIGIN="ANONYMOUS" attribute on the audio tag is strictly required for CORS access!
        audioSource = audioContext.createMediaElementSource(player);
    }

    // --- BS.1770 STAGE 1: K-Weighting High-Shelf Pre-Filter ---
    const preFilter = audioContext.createBiquadFilter();
    preFilter.type = "highshelf";
    preFilter.frequency.value = 1500; 
    preFilter.Q.value = 1.0;
    preFilter.gain.value = 4.0; // +4dB Boost
    // --- BS.1770 STAGE 2: RLB High-Pass / Low-Cut Filter ---
    const rlbFilter = audioContext.createBiquadFilter();rlbFilter.type = "highpass";
    rlbFilter.frequency.value = 38;
    rlbFilter.Q.value = 0.5;// Connect the Filter Network Matrix
    audioSource.connect(preFilter);
    preFilter.connect(rlbFilter);// Create Analyser Node connected to the filtered stream 
    outputlufsAnalyser = audioContext.createAnalyser();
    lufsAnalyser.fftSize = 1024;
    rlbFilter.connect(lufsAnalyser);// Pass the unprocessed direct output stream to speakers for accurate clean monitoring
    audioSource.connect(audioContext.destination);// Initialize Canvas Grid Layout and trigger execution 
    loopinitRadarCanvas();
    executeAnalysisLoop();
}

function initRadarCanvas() {
    const canvas = document.getElementById('radarCanvas');
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);// Draw target visual grid ring markings
    ctx.strokeStyle = '#222';
    ctx.lineWidth = 1;
    for (let r = 0.25; r <= 1.0; r += 0.25) {
            ctx.beginPath();
            ctx.arc(canvas.width/2, canvas.height/2, (canvas.width/2 - 10) * r, 0, 2 * Math.PI);
            ctx.stroke();
    }
}

function executeAnalysisLoop() {
    if (!isMonitoring) 
        return;
    animationFrameId = requestAnimationFrame(executeAnalysisLoop);
    const bufferLength = lufsAnalyser.frequencyBinCount;
    const dataArray = new Float32Array(bufferLength);
    lufsAnalyser.getFloatTimeDomainData(dataArray);
    let sumOfSquares = 0;
    let peakValue = 0;
    for (let i = 0; i < bufferLength; i++) {
        const val = dataArray[i];
        sumOfSquares += val * val;
        if (Math.abs(val) > peakValue) 
            peakValue = Math.abs(val);
    }
    // 1. Calculate Real-time Peak Level (dBFS)
    let peakDB = 20 * Math.log10(peakValue);
    if (peakDB < -70 || peakValue === 0) 
        peakDB = -70;
    const peakPercent = Math.min(100, Math.max(0, (peakDB + 70) * (100 / 70)));
    document.getElementById('peakBar').style.width = peakPercent + "%";
    document.getElementById('peakTxt').innerText = peakDB === -70 ? "Peak: -Inf dBFS" : Peak: ${peakDB.toFixed(1)} dBFS;
    // 2. Calculate Momentary LUFS (400ms Windows Estimation)// The -0.691 constant is the specific BS.1770 Loudness Calibration offset factor
    let meanSquare = sumOfSquares / bufferLength;
    let momentaryLUFS = 10 * Math.log10(meanSquare) - 0.691;
    if (momentaryLUFS < -70) 
        momentaryLUFS = -70;
    document.getElementById('momentaryTxt').innerText = momentaryLUFS === -70 ? "-Inf LUFS" : ${momentaryLUFS.toFixed(1)} LUFS;
    document.getElementById('momentaryTxt').style.color = momentaryLUFS > -14 ? "#e74c3c" : "#2ecc71";
    // 3. Calculate Short-Term LUFS (Rolling 3-Second Average Buffer Window)
    shortTermBuffer.push(meanSquare);
    if (shortTermBuffer.length > 120) 
        shortTermBuffer.shift(); 
    // Keep trailing timeline data points
    let stMeanSquare = shortTermBuffer.reduce((a, b) => a + b, 0) / shortTermBuffer.length;
    let shortTermLUFS = 10 * Math.log10(stMeanSquare) - 0.691;
    if (shortTermLUFS < -70) 
        shortTermLUFS = -70;
    document.getElementById('shortTermTxt').innerText = shortTermLUFS === -70 ? "-Inf LUFS" : ${shortTermLUFS.toFixed(1)} LUFS;
    // 4. Render Dynamic Clarity M Radar Sweep Sweep Point
    renderRadarSweep(shortTermLUFS);
}

function renderRadarSweep(lufsValue) {
    const canvas = document.getElementById('radarCanvas');
    const ctx = canvas.getContext('2d');
    const centerX = canvas.width / 2;
    const centerY = canvas.height / 2;
    const maxRadius = centerX - 10;
    // Map LUFS value scope scale (-70dB to 0dB) linearly into radius space dimensions
    const normalizedLoudness = (lufsValue + 70) / 70; 
    // Map down range: 0.0 to 1.0
    const targetRadius = maxRadius * normalizedLoudness;
    // Resolve polar vectors array mapping layout coordinate markers
    const targetX = centerX + targetRadius * Math.cos(radarAngle);
    const targetY = centerY + targetRadius * Math.sin(radarAngle);
    // Render step sweeping point plot indicator marker
    ctx.fillStyle = lufsValue > -14 ? '#e74c3c' : '#3498db'; 
    // Turn point red if clipping target threshold bounds
    ctx.beginPath();
    ctx.arc(targetX, targetY, 2, 0, 2 * Math.PI);
    ctx.fill();
    // Advance angle parameter tracking indicators (approx 1 sweep lap revolution per minute)
    radarAngle += 0.005;
    if (radarAngle > 2 * Math.PI) {
        radarAngle = 0;
        // Fade out previous historical traces to prevent sweep accumulation cluttering
        ctx.fillStyle = 'rgba(9,9,9,0.85)';
        ctx.beginPath();
        ctx.arc(centerX, centerY, maxRadius + 5, 0, 2 * Math.PI);
        ctx.fill();
        initRadarCanvas();
    }
}

function startLatencyTracker(player) {
    latencyInterval = setInterval(() => {
        if (!player.buffered.length) 
            return;
        const bufferedEnd = player.buffered.end(player.buffered.length - 1);
        const latency = bufferedEnd - player.currentTime;
        // If client media element drops behind active pipeline index context by 1.5 seconds,
        // force buffer skip ahead to catch up up seamlessly to modern edge (<200ms)
        if (latency > 1.5) {
            player.currentTime = bufferedEnd - 0.2;
        }
    }, 1000);
}

function stopMonitoringGracefully(player, btn) {
    isMonitoring = false;
    if (animationFrameId) 
        cancelAnimationFrame(animationFrameId);
    if (latencyInterval) 
        clearInterval(latencyInterval);
    player.pause();
    player.src = ""; 
    // Sever active connection socket stream processing loops immediately
    player.load();    
    // Deallocate standard decoder context pipelines
    document.getElementById('peakBar').style.width = "0%";
    document.getElementById('peakTxt').innerText = "Peak: -Inf dBFS";
    document.getElementById('momentaryTxt').innerText = "-Inf LUFS";
    document.getElementById('shortTermTxt').innerText = "-Inf LUFS";
    btn.innerText = "Luister Live";
}

```
4. Key Considerations for Network & Processing Safety
 1. CORS Security Constraints: Because Web Audio API analyzes raw samples from an external HTTP source, your media stream endpoint must serve identical Origin parameters. The HTML5 audio element must include the crossorigin="anonymous" tag, and your Flask response header array must return Access-Control-Allow-Origin: 
 2. Buffer Overrun Prevention: If the network link between your web browser and the Raspberry Pi slows down, the streaming queue will begin dropping chunks. The q.put_nowait(data) clause inside audio_multiplexer_loop prevents delayed clients from locking up the memory of your Python app.
 3. RAM Cleaning Protocol: Simply pausing an HTML5 stream leaves the network socket connection open in the background, keeping the transcoding process alive. The player.src = "" and player.load() steps inside stopMonitoringGracefully are required to cleanly sever the socket link and reclaim processing cores on the Pi.

