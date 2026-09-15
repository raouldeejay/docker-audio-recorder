
## Live Browser Audio Monitoring & Dynamic Pipelines

This document details the decoupled architecture used to tap the live hardware audio stream. It allows users to start or stop browser-based monitoring independently of physical file recording.

## 1. Lifecycle Scenario Matrix

Because the data capture worker (`arecord`) and individual encoding workers (`ffmpeg`) are decoupled via a Python background multiplexer, monitoring client instances do not interfere with the active file writer pipeline.

You can execute the exact pipeline sequence detailed below

[User Action]            [Hardware Status]                 [Data Stream Routing Path]───────────────────────────────────────────────────────────────────────────────────────────────Start Monitoring  ──► arecord initialises          ──► Raw PCM ──► Python Queue ──► BrowserStart Recording   ──► Storage Encoder initialises  ──► Raw PCM ──► Split Path   ──► Browser & Local Storage FileStop Monitoring   ──► Browser client disconnects   ──► Raw PCM ──► Local Storage File OnlyStop Recording    ──► Storage Encoder flushes      ──► Pipeline teardown completely closed

## 2. Decoupled Routing Architecture

The data stream is multiplexed entirely in memory via raw RAM buffers. Instead of locking down the audio subsystem device to a single binary output process, data is dynamically distributed based on active consumer registration flags:

┌──► [Storage FFmpeg Process] ──► Local Target (.aiff, .wav, .flac)│     (Active if Recording == True)[arecord Process] ──────┼(Raw Hardware Stream)  │└──► [Thread-Safe Client Queues] ──► [Live FFmpeg Transcoder] ──► Web Client(Active if Listener Count > 0)
---

## 3. Implementation Blueprint (`app.py`)

Implement this state management logic within your Flask controller framework to gracefully register and unregister pipelines on the fly:

```python
import subprocess
import threading
import queue
from flask import Flask, Response, jsonify

# Shared engine execution pointers
arecord_process = None
storage_ffmpeg_process = None
connected_listeners = []
listeners_lock = threading.Lock()
multiplexer_thread = None

def ensure_arecord_running(card, device, samplerate, bitdepth):
    """
    Spins up the hardware receiver process if it isn't already active.
    This safely serves both initial monitoring requests or immediate recording tasks.
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
```

---

## 4. Operational Best Practices

* **Cache Busting Strategy:** Modern browsers heavily buffer standard HTTP streams. When implementing your frontend audio player element, always append a changing parameter tag to the endpoint source string (e.g., `/api/stream?t=17181920`) when triggering a play event. This forces the browser to fetch standard real-time audio segments instead of playing stale, cached information from a previous session.
* **Buffer Underflow Prevention:** The transcoder configuration pipes into browser streams via `pipe:1`. Setting the internal listener thread queue threshold size (`maxsize=200`) protects system memory targets from continuous inflation if network interruptions cause web client connection lag.

## 5. Frontend Implementatie & Latency Optimalisatie

Gebruik de onderstaande HTML- en JavaScript-code in je frontend (`index.html`). Deze code zorgt ervoor dat de browser de audiocache direct leeggooit bij het starten/stoppen en dwingt de mediaspeler om altijd zo dicht mogelijk op de live-tijd (real-time stream head) te spelen.

```html
<div class="audio-control-panel">
    <h3>Hardware Audio Monitor</h3>
    <button id="monitorBtn" onclick="toggleMonitor()">Luister Live</button>
    <audio id="livePlayer" style="display:none;" preload="none"></audio>
</div>

<script>
let isMonitoring = false;
let latencyInterval = null;

function toggleMonitor() {
    const player = document.getElementById('livePlayer');
    const btn = document.getElementById('monitorBtn');
    
    if (!isMonitoring) {
        // 1. Cache-busting timestamp om oude buffers te omzeilen
        player.src = "/api/stream?t=" + new Date().getTime();
        
        // 2. Configureer de audio-engine voor lage vertraging
        player.muted = false;
        player.play().then(() => {
            btn.innerText = "Stop Luisteren";
            btn.classList.add("monitoring-active");
            isMonitoring = true;
            
            // 3. Start actieve latency-controle (haal achterstand in)
            startLatencyTracker(player);
        }).catch(err => {
            console.error("Audio afspelen mislukt:", err);
        });
        
    } else {
        // Stop de monitor-stream op de juiste manier
        stopMonitoringGracefully(player, btn);
    }
}

function startLatencyTracker(player) {
    // Controleer elke seconde of de browser achterloopt op de live-stream
    latencyInterval = setInterval(() => {
        if (!player.buffered.length) return;
        
        const bufferedEnd = player.buffered.end(player.buffered.length - 1);
        const currentTime = player.currentTime;
        const latency = bufferedEnd - currentTime;
        
        // Als de browser meer dan 1.5 seconde achterloopt op de binnengekomen buffer,
        // spring dan direct naar de rand van de live-buffer (live edge).
        if (latency > 1.5) {
            console.log(`[Latency Control] Bufferloopt achter (${latency.toFixed(2)}s). Versnellen naar live edge...`);
            player.currentTime = bufferedEnd - 0.2; 
        }
    }, 1000);
}

function stopMonitoringGracefully(player, btn) {
    // Wis de actieve interval-timer
    if (latencyInterval) {
        clearInterval(latencyInterval);
        latencyInterval = null;
    }
    
    player.pause();
    player.src = ""; // Essentieel: Verbreekt direct de HTTP-verbinding met Flask
    player.load();   // Reset de HTML5 media-element status volledig
    
    btn.innerText = "Luister Live";
    btn.classList.remove("monitoring-active");
    isMonitoring = false;
}
</script>
```

### Waarom deze specifieke JavaScript-aanpak nodig is:
1. **`player.src = ""` & `player.load()`:** Als je alleen `.pause()` gebruikt, houdt de browser de HTTP-verbinding naar je Flask-container op de Raspberry Pi open op de achtergrond. Dit zorgt ervoor dat `ffmpeg` blijft transcoderen en data blijft sturen. Door de `src` leeg te maken en `.load()` aan te roepen, dwing je de browser de netwerkverbinding fysiek te verbreken.
2. **De Live Edge Tracker:** Wanneer het netwerk op de Pi of je wifi-netwerk even hapert, zal de HTML5-speler de audio vertragen in plaats van frames skippen. De `startLatencyTracker`-functie controleert continu het verschil tussen de afspeelkop (`currentTime`) en het einde van de binnengekomen data (`buffered.end`). Als dit gat te groot wordt, 'skipt' de code automatisch naar voren om de vertraging te herstellen naar < 200ms.
