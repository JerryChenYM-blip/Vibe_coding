"""
麥克風錄音模組（sounddevice 封裝）。

提供執行緒安全的音訊錄製介面，輸出 float32 numpy 陣列（16 kHz 單聲道）。

macOS 特殊處理：
  若 sounddevice 找不到 libportaudio（DYLD_LIBRARY_PATH 未設定），
  會嘗試從 Homebrew 安裝路徑動態載入，使用者無需手動設定環境變數。

匯出：
  AudioRecorder   主要錄音類別
  SAMPLE_RATE     16000（Hz，Whisper 要求）
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import threading
import time
from typing import Optional

import numpy as np


# ── Homebrew libportaudio 自動載入（macOS）────────────────────────────────────

def _load_portaudio() -> None:
    """嘗試從 Homebrew 前綴自動載入 libportaudio，解決 DYLD 路徑問題。

    正常 sounddevice 安裝後會自動找到 libportaudio；
    但某些 macOS 環境下（特別是透過 venv + Homebrew 組合）會找不到，
    此函式作為備援，直接用 ctypes 強制載入。
    """
    try:
        import sounddevice  # noqa: F401 — 若已可 import 就不需要手動載入
        return
    except OSError:
        pass   # 找不到 libportaudio，繼續往下嘗試 Homebrew 路徑

    try:
        # 支援 Apple Silicon（/opt/homebrew）與 Intel Mac（/usr/local）
        for brew_path in ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"]:
            if os.path.exists(brew_path):
                prefix = subprocess.check_output(
                    [brew_path, "--prefix", "portaudio"], text=True
                ).strip()
                lib = os.path.join(prefix, "lib", "libportaudio.2.dylib")
                if os.path.exists(lib):
                    ctypes.cdll.LoadLibrary(lib)   # 強制載入到進程
                    return
    except Exception:
        pass   # Homebrew 不存在或 portaudio 未安裝，讓 sounddevice import 自然失敗


# 模組載入時立即嘗試修復，確保後續 import sounddevice 能成功
_load_portaudio()

try:
    import sounddevice as sd
except ImportError:
    sd = None   # 沒有 sounddevice 時，所有錄音操作都回傳失敗/空陣列


# ── 錄音參數常數 ──────────────────────────────────────────────────────────────

SAMPLE_RATE = 16_000   # Hz — Whisper 要求 16 kHz
CHANNELS    = 1        # 單聲道，減少資料量
DTYPE       = "float32"  # 振幅範圍 [-1.0, 1.0]
BLOCK_MS    = 100      # sounddevice callback 每次呼叫的時間塊大小（毫秒）


class AudioRecorder:
    """執行緒安全的麥克風錄音器，輸出 float32 numpy 陣列。

    設計原則：
      • 所有共享狀態都由 _lock 保護，callback 函式從 PortAudio 執行緒呼叫
      • RMS 電平（_rms_level）不在鎖內讀寫（屬原子 float，race condition 無害）
      • stop() 一律等 stream 完全關閉再 concatenate，避免資料競爭
    """

    def __init__(self) -> None:
        self._frames: list[np.ndarray] = []    # 每次 callback 的音訊塊
        self._stream: Optional[sd.InputStream] = None
        self._is_recording = False
        self._rms_level: float = 0.0           # 最新一幀的 RMS 振幅（0~1）
        self._lock = threading.RLock()          # 可重入鎖，保護 frames 與 stream
        self._device_index: Optional[int] = None  # None = 系統預設麥克風
        self._monitor_thread: Optional[threading.Thread] = None

    # ── 公開介面 ──────────────────────────────────────────────────────────────

    def set_device_by_name(self, name: str) -> bool:
        """依名稱搜尋並設定錄音裝置。

        Args:
            name: 裝置完整名稱（由 list_devices() 回傳）。

        Returns:
            True 代表找到並設定成功，False 代表找不到（回退到系統預設）。
        """
        devices = self.list_devices()
        for dev in devices:
            if name == dev["name"]:
                self._device_index = dev["id"]
                return True
        self._device_index = None   # 找不到就回退到系統預設
        return False

    def start(self) -> bool:
        """開啟麥克風串流並開始收集音訊幀。

        Returns:
            True 代表串流成功開啟，False 代表失敗（sounddevice 不可用或裝置錯誤）。
        """
        if sd is None:
            print("RECORD: ERROR - sounddevice is not available.")
            return False

        with self._lock:
            if self._is_recording:
                # 避免重複呼叫 start()（例如快捷鍵連按）
                print("RECORD: Already recording, ignoring start request.")
                return True

            # 重置所有狀態，確保每次錄音都是乾淨開始
            self._frames = []
            self._rms_level = 0.0
            # 每個 callback 接收的 sample 數 = 採樣率 × 時間塊
            blocksize = int(BLOCK_MS / 1000 * SAMPLE_RATE)

            try:
                print(f"RECORD: Attempting to start microphone. Device={self._device_index}, Samplerate={SAMPLE_RATE}, Blocksize={blocksize}")
                self._stream = sd.InputStream(
                    samplerate=SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype=DTYPE,
                    blocksize=blocksize,
                    device=self._device_index,
                    callback=self._audio_callback,  # PortAudio 執行緒呼叫此函式
                )
                self._stream.start()
                self._is_recording = True
                print(f"RECORD: Stream active. Latency={self._stream.latency:.4f}s, CPU Load={self._stream.cpu_load:.2%}")
                return True
            except Exception as e:
                print(f"RECORD ERROR: Initialization failed. Exception: {str(e)}")
                import traceback
                traceback.print_exc()
                # 確保狀態乾淨，不讓下次 start() 誤判
                self._stream = None
                self._is_recording = False
                return False

    def stop(self) -> np.ndarray:
        """停止錄音，回傳完整錄音資料。

        Returns:
            float32 一維 numpy 陣列（所有幀 concatenate 後）。
            若沒有捕捉到任何音訊則回傳空陣列。
        """
        with self._lock:
            if not self._is_recording:
                print("RECORD: stop() called while already idle.")
                return np.zeros(0, dtype=np.float32)

            print("RECORD: Stopping audio stream...")
            self._is_recording = False

            # 關閉 PortAudio 串流，釋放硬體資源
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                    print("RECORD: PortAudio stream closed cleanly.")
                except Exception as e:
                    print(f"RECORD ERROR: Error while closing stream: {e}")
                self._stream = None

            # 複製 frames list 後立即清空，釋放記憶體
            frames = list(self._frames)
            self._frames = []

        if not frames:
            print("RECORD: Warning - No audio data captured.")
            return np.zeros(0, dtype=np.float32)

        # 將所有音訊塊串接成單一一維陣列
        full_audio = np.concatenate(frames, axis=0).flatten()
        print(f"RECORD: Session complete. Total samples={len(full_audio)}, Duration={len(full_audio)/SAMPLE_RATE:.2f}s")
        return full_audio

    def get_rms_level(self) -> float:
        """回傳最新一幀的 RMS 振幅（0.0~1.0）。任何執行緒可安全呼叫。"""
        return self._rms_level

    def get_buffer_snapshot(self) -> np.ndarray:
        """非破壞性地複製目前所有已錄音資料。錄音進行中也可安全呼叫。

        Returns:
            float32 一維 numpy 陣列（目前為止的全部音訊）。
        """
        with self._lock:
            if not self._frames:
                return np.zeros(0, dtype=np.float32)
            return np.concatenate(self._frames, axis=0).flatten()

    def get_recent_buffer(self, start_samples: int) -> np.ndarray:
        """非破壞性地取得從 start_samples 開始的音訊片段。

        Args:
            start_samples: 從第幾個 sample 開始取（0 代表從頭）。

        Returns:
            start_samples 之後的音訊片段，若超過緩衝區長度則回傳空陣列。
        """
        with self._lock:
            if not self._frames:
                return np.zeros(0, dtype=np.float32)
            # 一次 concatenate 再切片；效能可接受（每秒最多幾次呼叫）
            full_buffer = np.concatenate(self._frames, axis=0).flatten()
            if start_samples >= len(full_buffer):
                return np.zeros(0, dtype=np.float32)
            return full_buffer[start_samples:]

    def is_recording(self) -> bool:
        """回傳目前是否正在錄音。"""
        return self._is_recording

    @staticmethod
    def list_devices() -> list[dict]:
        """列出所有具有輸入能力的音訊裝置。

        Returns:
            [{"id": int, "name": str}, ...] 的列表，可傳給 set_device_by_name()。
        """
        if sd is None:
            return []
        try:
            devices = []
            for i, d in enumerate(sd.query_devices()):
                # 過濾只保留有麥克風輸入的裝置
                if d.get("max_input_channels", 0) > 0:
                    devices.append({"id": i, "name": d["name"]})
            return devices
        except Exception as e:
            print(f"WARNING: Cannot query audio devices: {e}")
            return []

    # ── 私有方法 ──────────────────────────────────────────────────────────────

    def _audio_callback(
        self,
        indata: np.ndarray,    # 本次 callback 的音訊資料（shape: [blocksize, channels]）
        frames: int,           # 本次 callback 的 sample 數（= blocksize）
        time_info,             # PortAudio 時間資訊（目前未使用）
        status: sd.CallbackFlags,
    ) -> None:
        """PortAudio 執行緒呼叫的音訊回調函式——必須極快，不可阻塞。

        此函式在 PortAudio 的即時執行緒中執行，任何耗時操作（I/O、大量計算）
        都會造成錄音中斷（buffer underflow）。
        """
        if status:
            # PortAudio 回報的狀態警告（如 input overflow），記錄但不中止
            print(f"SD STATUS: {status}")

        try:
            chunk = indata.copy()   # 複製一份，避免 PortAudio 回收緩衝區後資料消失
            with self._lock:
                if self._is_recording:
                    self._frames.append(chunk)   # 加入幀列表
            # 計算 RMS 振幅（不在鎖內，因為 float 賦值是原子操作）
            self._rms_level = float(np.sqrt(np.mean(chunk ** 2)))
        except Exception as e:
            # callback 中任何例外都不能往上拋（會讓 PortAudio 崩潰），只能記錄
            print(f"CALLBACK ERROR: {e}")
            self._is_recording = False
