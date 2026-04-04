"""
Microphone recording via sounddevice.

On macOS, if sounddevice cannot find libportaudio automatically, the library
path is resolved from Homebrew at module import time.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import threading
import time
from typing import Optional

import numpy as np

# ── PortAudio bootstrap (macOS Homebrew) ─────────────────────────────────────
def _load_portaudio() -> None:
    """Try to load libportaudio from Homebrew prefix if not found on DYLD path."""
    try:
        import sounddevice  # noqa: F401 — will succeed if already in path
        return
    except OSError:
        pass
    try:
        # Check for Homebrew path
        for brew_path in ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"]:
            if os.path.exists(brew_path):
                prefix = subprocess.check_output(
                    [brew_path, "--prefix", "portaudio"], text=True
                ).strip()
                lib = os.path.join(prefix, "lib", "libportaudio.2.dylib")
                if os.path.exists(lib):
                    ctypes.cdll.LoadLibrary(lib)
                    return
    except Exception:
        pass


_load_portaudio()
try:
    import sounddevice as sd
except ImportError:
    sd = None


# ─────────────────────────────────────────────────────────────────────────────

SAMPLE_RATE = 16_000   # Hz — Whisper expects 16 kHz
CHANNELS = 1
DTYPE = "float32"
BLOCK_MS = 100         # callback chunk size in milliseconds


class AudioRecorder:
    """Thread-safe microphone recorder that produces a float32 numpy array."""

    def __init__(self) -> None:
        self._frames: list[np.ndarray] = []
        self._stream: Optional[sd.InputStream] = None
        self._is_recording = False
        self._rms_level: float = 0.0
        self._lock = threading.RLock()
        self._device_index: Optional[int] = None
        self._monitor_thread: Optional[threading.Thread] = None

    # ── public API ────────────────────────────────────────────────────────────

    def set_device_by_name(self, name: str) -> bool:
        """Find device ID by partial name match and set it."""
        devices = self.list_devices()
        for dev in devices:
            if name == dev["name"]:
                self._device_index = dev["id"]
                return True
        self._device_index = None # Fallback to default
        return False

    def start(self) -> bool:
        """
        Open microphone stream and begin collecting audio frames.
        Returns True if successful, False otherwise.
        """
        if sd is None:
            print("RECORD: ERROR - sounddevice is not available.")
            return False

        with self._lock:
            if self._is_recording:
                print("RECORD: Already recording, ignoring start request.")
                return True
            
            self._frames = []
            self._rms_level = 0.0
            blocksize = int(BLOCK_MS / 1000 * SAMPLE_RATE)
            
            try:
                print(f"RECORD: Attempting to start microphone. Device={self._device_index}, Samplerate={SAMPLE_RATE}, Blocksize={blocksize}")
                self._stream = sd.InputStream(
                    samplerate=SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype=DTYPE,
                    blocksize=blocksize,
                    device=self._device_index,
                    callback=self._audio_callback,
                )
                self._stream.start()
                self._is_recording = True
                print(f"RECORD: Stream active. Latency={self._stream.latency:.4f}s, CPU Load={self._stream.cpu_load:.2%}")
                
                # _auto_stop_monitor is currently not implemented, skipping thread creation
                # if self._monitor_thread is None or not self._monitor_thread.is_alive():
                #     self._monitor_thread = threading.Thread(target=self._auto_stop_monitor, daemon=True)
                #     self._monitor_thread.start()
                
                return True
            except Exception as e:
                print(f"RECORD ERROR: Initialization failed. Exception: {str(e)}")
                import traceback
                traceback.print_exc()
                self._stream = None
                self._is_recording = False
                return False

    def stop(self) -> np.ndarray:
        """
        Stop recording and return the captured audio as a flat float32 array.
        Returns an empty array if nothing was recorded.
        """
        with self._lock:
            if not self._is_recording:
                print("RECORD: stop() called while already idle.")
                return np.zeros(0, dtype=np.float32)
            
            print("RECORD: Stopping audio stream...")
            self._is_recording = False
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                    print("RECORD: PortAudio stream closed cleanly.")
                except Exception as e:
                    print(f"RECORD ERROR: Error while closing stream: {e}")
                self._stream = None
            
            frames = list(self._frames)
            self._frames = [] # 徹底釋放記憶體

        if not frames:
            print("RECORD: Warning - No audio data captured.")
            return np.zeros(0, dtype=np.float32)
            
        full_audio = np.concatenate(frames, axis=0).flatten()
        print(f"RECORD: Session complete. Total samples={len(full_audio)}, Duration={len(full_audio)/SAMPLE_RATE:.2f}s")
        return full_audio

    def get_rms_level(self) -> float:
        """Return the latest RMS amplitude (0.0 – 1.0). Safe to call from any thread."""
        return self._rms_level

    def get_buffer_snapshot(self) -> np.ndarray:
        """Non-destructive copy of all recorded samples so far. Safe to call while recording."""
        with self._lock:
            if not self._frames:
                return np.zeros(0, dtype=np.float32)
            return np.concatenate(self._frames, axis=0).flatten()

    def get_recent_buffer(self, start_samples: int) -> np.ndarray:
        """Non-destructive copy of recorded samples starting from start_samples index."""
        with self._lock:
            if not self._frames:
                return np.zeros(0, dtype=np.float32)

            # Since _frames is a list of blocks, we can just concatenate the needed ones
            # For simplicity, we concatenate all and slice.
            # In a very high-performance scenario, we could slice before concatenation
            # but this is already much better than copying all and returning it all
            # if we only need the tail.
            full_buffer = np.concatenate(self._frames, axis=0).flatten()
            if start_samples >= len(full_buffer):
                return np.zeros(0, dtype=np.float32)
            return full_buffer[start_samples:]

    def is_recording(self) -> bool:
        return self._is_recording

    @staticmethod
    def list_devices() -> list[dict]:
        """Return input-capable audio devices."""
        if sd is None:
            return []
        try:
            devices = []
            for i, d in enumerate(sd.query_devices()):
                if d.get("max_input_channels", 0) > 0:
                    devices.append({"id": i, "name": d["name"]})
            return devices
        except Exception as e:
            print(f"WARNING: Cannot query audio devices: {e}")
            return []

    # ── private ───────────────────────────────────────────────────────────────

    def _audio_callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info,
        status: sd.CallbackFlags,
    ) -> None:
        """Called on PortAudio thread — must be fast, no blocking."""
        if status:
            print(f"SD STATUS: {status}")
        
        try:
            chunk = indata.copy()
            with self._lock:
                if self._is_recording:
                    self._frames.append(chunk)
            # RMS amplitude
            self._rms_level = float(np.sqrt(np.mean(chunk ** 2)))
        except Exception as e:
            print(f"CALLBACK ERROR: {e}")
            self._is_recording = False
