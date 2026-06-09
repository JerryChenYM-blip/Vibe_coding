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

from logger import get_logger, log_error

log = get_logger("recorder")


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

        # 熱插拔偵測（v2.21）：
        # PortAudio 在初始化時把麥克風清單「拍快照」，之後 query_devices() 都回舊快照。
        # 插/拔麥克風時快照不更新 → 必須 sd._terminate()/_initialize() 重新初始化才看得到。
        #
        # _device_name 存「目前選定裝置的名字」（不是 index）——index 在 re-init 後會變，
        # 名字才能穩定比對。set_device_by_name() 成功時記下，is_active_device_present() 用它比對。
        self._device_name: Optional[str] = None
        # CoreAudio 裝置變動監聽相關 handle（用 ctypes 直呼 CoreAudio.framework）
        self._ca_lib = None          # CoreAudio.framework 的 CDLL handle
        self._ca_listener = None     # CFUNCTYPE callback 物件——必須存成 attribute 防 GC（否則 segfault）
        self._ca_addr = None         # AudioObjectPropertyAddress 實例（remove 時要傳回去）
        self._ca_on_change = None    # 上層傳入的 on_change callable

        # D2-S1（v2.9.0）：generation counter 防 stale callback。
        # 流程：每次 start() bump +1；stop() 也 bump +1。callback 在進入時帶
        # 入「當時的 gen」snapshot，寫 frame 前比對 self._capture_gen 是否相符；
        # 不符就直接 drop（PortAudio in-flight callback 在 stop() 還沒 drain
        # 完時可能還會 fire 一次，舊 callback 不能把上一段 frames 寫進下一段
        # 已重置的 self._frames）。bump 不需要 lock 保護（int 賦值原子）。
        self._capture_gen: int = 0

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
                self._device_name = dev["name"]   # 記下名字供 is_active_device_present() 比對
                return True
        self._device_index = None   # 找不到就回退到系統預設
        self._device_name = None    # 系統預設沒有固定名字
        return False

    def start(self) -> bool:
        """開啟麥克風串流並開始收集音訊幀。

        Returns:
            True 代表串流成功開啟，False 代表失敗（sounddevice 不可用或裝置錯誤）。
        """
        if sd is None:
            log.error("RECORD: sounddevice is not available.")
            return False

        with self._lock:
            if self._is_recording:
                # 避免重複呼叫 start()（例如快捷鍵連按）
                log.warning("RECORD: Already recording, ignoring start request.")
                return True

            # 重置所有狀態，確保每次錄音都是乾淨開始
            self._frames = []
            self._rms_level = 0.0
            # D2-S1：bump generation；舊段 in-flight callback 拿到的 my_gen 立刻 stale
            self._capture_gen += 1
            my_gen = self._capture_gen
            # 每個 callback 接收的 sample 數 = 採樣率 × 時間塊
            blocksize = int(BLOCK_MS / 1000 * SAMPLE_RATE)

            # D2-S1：closure 帶入當前 gen snapshot，callback 只在 gen 仍匹配時寫
            def _open_stream(device):
                def _cb(indata, frames, time_info, status, _gen=my_gen):
                    self._audio_callback(indata, frames, time_info, status, _gen)
                stream = sd.InputStream(
                    samplerate=SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype=DTYPE,
                    blocksize=blocksize,
                    device=device,
                    callback=_cb,                    # PortAudio 執行緒呼叫此函式
                )
                stream.start()
                return stream

            # v2.21.4：標記本次是否退回系統預設裝置（gui 端據此提示使用者）
            self._started_with_fallback = False
            try:
                log.info(
                    f"RECORD: Attempting to start microphone. "
                    f"Device={self._device_index}, Samplerate={SAMPLE_RATE}, "
                    f"Blocksize={blocksize}, gen={my_gen}"
                )
                self._stream = _open_stream(self._device_index)
                self._is_recording = True
                log.info(
                    f"RECORD: Stream active. Latency={self._stream.latency:.4f}s, "
                    f"CPU Load={self._stream.cpu_load:.2%}"
                )
                return True
            except Exception:
                log_error("recorder_init_failed", device=self._device_index)
                self._stream = None
                self._is_recording = False
                # v2.21.4：指定裝置開串流失敗（AirPods/藍牙耳機剛喚醒或沒連時、
                #   _device_index 指到的裝置還沒 ready、sd.InputStream 直接 throw）
                #   → 退回系統預設麥克風重試一次，避免使用者按了快捷鍵整段錄音作廢。
                #   注意這跟 v2.21 的「錄音中拔線」熱插拔不同——這是錄音開始那一刻
                #   裝置就還沒 ready。fallback 成功時設旗標、gui 端 toast 告知改用內建。
                if self._device_index is not None:
                    try:
                        log.warning(
                            "RECORD: 指定裝置開串流失敗、改用系統預設麥克風重試"
                        )
                        self._stream = _open_stream(None)
                        self._is_recording = True
                        self._started_with_fallback = True
                        log.info("RECORD: Stream active（已退回系統預設麥克風）。")
                        return True
                    except Exception:
                        log_error("recorder_init_failed_fallback")
                        self._stream = None
                        self._is_recording = False
                        return False
                return False

    def stop(self) -> np.ndarray:
        """停止錄音，回傳完整錄音資料。

        Returns:
            float32 一維 numpy 陣列（所有幀 concatenate 後）。
            若沒有捕捉到任何音訊則回傳空陣列。
        """
        with self._lock:
            if not self._is_recording:
                log.debug("RECORD: stop() called while already idle.")
                return np.zeros(0, dtype=np.float32)

            log.info("RECORD: Stopping audio stream...")
            self._is_recording = False
            # D2-S1：bump generation；任何還沒 drain 的 in-flight callback 立刻 stale
            self._capture_gen += 1

            # 關閉 PortAudio 串流，釋放硬體資源
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                    log.info("RECORD: PortAudio stream closed cleanly.")
                except Exception:
                    log_error("recorder_close_failed")
                self._stream = None

            # 複製 frames list 後立即清空，釋放記憶體
            frames = list(self._frames)
            self._frames = []

        if not frames:
            log.warning("RECORD: No audio data captured.")
            return np.zeros(0, dtype=np.float32)

        # 將所有音訊塊串接成單一一維陣列
        full_audio = np.concatenate(frames, axis=0).flatten()
        log.info(
            f"RECORD: Session complete. Total samples={len(full_audio)}, "
            f"Duration={len(full_audio)/SAMPLE_RATE:.2f}s"
        )
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
        except Exception:
            log_error("query_audio_devices_failed")
            return []

    # ── 麥克風熱插拔偵測（v2.21）────────────────────────────────────────────────

    def refresh_portaudio(self) -> list[dict]:
        """重新初始化 PortAudio、回最新輸入裝置清單。

        為什麼要 re-init：PortAudio 在啟動時把裝置清單拍成快照，之後 query_devices()
        都回舊快照；插/拔麥克風後唯一拿到新清單的方法就是 _terminate() + _initialize()。

        關鍵安全限制：正在錄音時 re-init 會打斷串流（硬體資源被釋放重抓），
        所以錄音中一律「跳過」re-init，直接回目前的 list_devices()（仍是舊快照）。
        """
        if sd is None:
            return []
        with self._lock:
            if self._is_recording:
                log.info("RECORD: refresh_portaudio skipped (recording in progress)")
                return self.list_devices()
            try:
                sd._terminate()
                sd._initialize()
                log.info("RECORD: PortAudio re-initialized for device refresh")
            except Exception:
                log_error("portaudio_reinit_failed")
        return self.list_devices()

    def is_active_device_present(self) -> bool:
        """檢查目前選定的裝置是否還在（插著）。

        _device_name 為 None（系統預設麥克風）時一律回 True——系統預設永遠存在，
        macOS 會自動 fallback 到當下可用的內建/外接麥克風。

        有指定名字時，用名字比對 list_devices() 結果（不用 index，因為 re-init 後 index 會變）。
        注意：本方法只讀目前快照；要拿到拔除後的真實狀態，呼叫端應先 refresh_portaudio()。
        """
        if self._device_name is None:
            return True
        names = {d["name"] for d in self.list_devices()}
        return self._device_name in names

    def start_device_monitor(self, on_change) -> bool:
        """註冊 CoreAudio 裝置變動監聽。

        on_change 是無參數 callable，裝置插/拔時被呼叫。
        注意：callback 在 CoreAudio 自己的執行緒被呼叫，呼叫端自己負責 marshal
        到主執行緒（例如用 tkinter 的 master.after(0, ...)）。

        回 True = 註冊成功；失敗（非 macOS / ctypes 載入失敗）回 False、不拋例外。
        """
        try:
            import ctypes.util

            # ── 找到並載入 CoreAudio.framework ──
            lib_path = ctypes.util.find_library("CoreAudio")
            if not lib_path:
                log.info("RECORD: CoreAudio not found (not macOS?), device monitor disabled")
                return False
            ca = ctypes.CDLL(lib_path)

            # ── AudioObjectPropertyAddress struct ──
            # CoreAudio 用這個 3 欄結構描述「要監聽哪個屬性」
            class AudioObjectPropertyAddress(ctypes.Structure):
                _fields_ = [
                    ("mSelector", ctypes.c_uint32),
                    ("mScope", ctypes.c_uint32),
                    ("mElement", ctypes.c_uint32),
                ]

            # ── CoreAudio 四字碼常數（FourCharCode）──
            # CoreAudio 的常數其實是 4 個 ASCII 字元打包成 uint32（big-endian）。
            # 例：'dev#' = 0x64657623。用 int.from_bytes(b'dev#', 'big') 算出來。
            kAudioObjectSystemObject = 1
            kAudioHardwarePropertyDevices = int.from_bytes(b"dev#", "big")
            kAudioObjectPropertyScopeGlobal = int.from_bytes(b"glob", "big")
            kAudioObjectPropertyElementMain = 0

            addr = AudioObjectPropertyAddress(
                kAudioHardwarePropertyDevices,
                kAudioObjectPropertyScopeGlobal,
                kAudioObjectPropertyElementMain,
            )

            # ── callback 型別 + 實體 ──
            # 簽章對應 AudioObjectPropertyListenerProc：
            #   OSStatus (AudioObjectID, UInt32 numAddresses, const AudioObjectPropertyAddress*, void* clientData)
            CALLBACK_TYPE = ctypes.CFUNCTYPE(
                ctypes.c_int32,    # OSStatus 回傳值
                ctypes.c_uint32,   # inObjectID
                ctypes.c_uint32,   # inNumberAddresses
                ctypes.c_void_p,   # inAddresses（const AudioObjectPropertyAddress*）
                ctypes.c_void_p,   # inClientData
            )

            def _ca_callback(obj_id, n_addr, addrs, client_data):
                # CoreAudio 執行緒呼叫——這裡拋例外會直接炸進程，全部吞掉。
                try:
                    on_change()
                except Exception:
                    # 連 log_error 都包進 try，極端情況下 logger 也可能在收尾時不可用
                    try:
                        log_error("device_monitor_callback_failed")
                    except Exception:
                        pass
                return 0   # OSStatus noErr

            cb = CALLBACK_TYPE(_ca_callback)

            # ── 註冊監聽 ──
            status = ca.AudioObjectAddPropertyListener(
                ctypes.c_uint32(kAudioObjectSystemObject),
                ctypes.byref(addr),
                cb,
                None,
            )
            if status != 0:
                log.warning(f"RECORD: AudioObjectAddPropertyListener failed, status={status}")
                return False

            # ── 存 handle ──
            # cb（CFUNCTYPE 實體）必須存成 instance attribute，否則離開本函式後被 GC 回收，
            # CoreAudio 之後回呼一個已釋放的位址 → segfault。addr 同理（remove 時要傳回去）。
            self._ca_lib = ca
            self._ca_listener = cb
            self._ca_addr = addr
            self._ca_on_change = on_change
            log.info("RECORD: CoreAudio device monitor registered")
            return True
        except Exception:
            log_error("start_device_monitor_failed")
            return False

    def stop_device_monitor(self) -> None:
        """移除 CoreAudio 裝置變動監聽（on_close 時呼叫）。包 try/except，失敗只記錄。"""
        if self._ca_lib is None or self._ca_listener is None or self._ca_addr is None:
            return
        try:
            kAudioObjectSystemObject = 1
            self._ca_lib.AudioObjectRemovePropertyListener(
                ctypes.c_uint32(kAudioObjectSystemObject),
                ctypes.byref(self._ca_addr),
                self._ca_listener,
                None,
            )
            log.info("RECORD: CoreAudio device monitor removed")
        except Exception:
            log_error("stop_device_monitor_failed")
        finally:
            # 清掉 handle，防止重複 remove；callback 物件此時可安全被 GC
            self._ca_lib = None
            self._ca_listener = None
            self._ca_addr = None
            self._ca_on_change = None

    # ── 私有方法 ──────────────────────────────────────────────────────────────

    def _audio_callback(
        self,
        indata: np.ndarray,    # 本次 callback 的音訊資料（shape: [blocksize, channels]）
        frames: int,           # 本次 callback 的 sample 數（= blocksize）
        time_info,             # PortAudio 時間資訊（目前未使用）
        status: sd.CallbackFlags,
        gen: int = 0,          # D2-S1：start() 時 closure capture 的 generation
    ) -> None:
        """PortAudio 執行緒呼叫的音訊回調函式——必須極快，不可阻塞。

        此函式在 PortAudio 的即時執行緒中執行，任何耗時操作（I/O、大量計算）
        都會造成錄音中斷（buffer underflow）。

        D2-S1（v2.9.0）：`gen` 是 start() 時 closure capture 的 snapshot；
        stop() / 下次 start() 都會 bump `self._capture_gen`，gen 不符就直接 drop，
        防止第一段尾巴的 in-flight callback 把 frames 寫進第二段已重置的 buffer。
        """
        if status:
            # PortAudio 回報的狀態警告（如 input overflow），記錄但不中止
            log.warning(f"SD STATUS: {status}")

        # D2-S1：generation 比對在 lock 外做（int 讀取原子），不符直接丟、
        # 不更新 RMS（避免上一段尾巴的音量殘留 UI）
        if gen != self._capture_gen:
            return

        try:
            chunk = indata.copy()   # 複製一份，避免 PortAudio 回收緩衝區後資料消失
            with self._lock:
                # D2-S1：lock 內再 double-check（lock acquire 期間 stop() 可能 bump）
                if not (self._is_recording and gen == self._capture_gen):
                    return
                self._frames.append(chunk)   # 加入幀列表
            # 計算 RMS 振幅（不在鎖內，因為 float 賦值是原子操作）
            self._rms_level = float(np.sqrt(np.mean(chunk ** 2)))
        except Exception:
            # callback 中任何例外都不能往上拋（會讓 PortAudio 崩潰），只能記錄
            log_error("audio_callback_failed")
            self._is_recording = False
