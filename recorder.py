# Mac/Windows 雙棲 — 移植自 macOS v2.21.6
"""
麥克風錄音模組（macOS：系統 AudioQueue；Windows：sounddevice 封裝）。

提供執行緒安全的音訊錄製介面，輸出 float32 numpy 陣列（16 kHz 單聲道）。

macOS 特殊處理（v2.33.0 起）：
  不再載入 sounddevice／PortAudio——它一初始化就掃全部裝置，麥克風模式是「語音突顯」時要 10～15 秒。
  改用 mac_audio_queue.AQInputStream 只開「一支」麥克風，並在 App 啟動最早期就先開著暖機
  （見 prewarm）；裝置清單改走 CoreAudio 查詢並快取，主執行緒絕不做列舉。
  （_load_portaudio 的 Homebrew 動態載入沒動，只是 Mac 的正式路徑已不走 sounddevice。）

Windows 特殊處理：
  PyPI 上的 sounddevice wheel 已內建打包 PortAudio DLL，不需要 Homebrew
  那套動態庫載入修復，_load_portaudio() 在 Windows 上直接 no-op。

匯出：
  AudioRecorder   主要錄音類別
  SAMPLE_RATE     16000（Hz，Whisper 要求）
"""

from __future__ import annotations

import atexit
import contextlib
import ctypes
import os
import subprocess
import threading
import time
from typing import Optional

import numpy as np

import mac_audio_queue as _mac
from platform_util import IS_MAC, IS_WINDOWS


# ── Homebrew libportaudio 自動載入（macOS）────────────────────────────────────

def _load_portaudio() -> None:
    """嘗試從 Homebrew 前綴自動載入 libportaudio，解決 DYLD 路徑問題。

    正常 sounddevice 安裝後會自動找到 libportaudio；
    但某些 macOS 環境下（特別是透過 venv + Homebrew 組合）會找不到，
    此函式作為備援，直接用 ctypes 強制載入。

    Windows：sounddevice 官方 wheel 已內建打包 PortAudio DLL，不需要這段
    修復邏輯，直接 no-op（不影響後續 import sounddevice）。
    """
    if not IS_MAC:
        return   # Windows/Linux：sounddevice wheel 自帶 PortAudio，無需修復

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


from logger import get_logger, log_error

log = get_logger("recorder")

# v2.32.3：sounddevice 改成「要用時才載入」。v2.33.0 起 Mac 完全不載入它（改用 AudioQueue，見檔案開頭）。
#
# 為什麼不在模組最上層 import：sounddevice 一 import 就跑 PortAudio 初始化，向系統查每個音訊裝置
# 的一堆屬性。macOS 27 上，App 的麥克風模式是「語音突顯」時，App 身分每查一次要 0.05～0.3 秒
# （系統每次都重新協商語音突顯處理）——9 個裝置的機器 import 要 9.8～15.4 秒、重掃一次 9.5 秒；
# 改「標準」只要 0.2 秒／0.01 秒（2026-10-06 用 App 身分實測）。
#
# Windows 沒有這個問題，維持原本「import 時就載入」。
sd = None            # 載入成功後才是 sounddevice 模組；None 時所有錄音操作回傳失敗／空陣列
_sd_failed = False   # 載入失敗過就不再重試（失敗通常是缺 libportaudio，重試也不會好）
_sd_lock = threading.Lock()

# 重掃（PortAudio terminate＋initialize）期間握著這把鎖。這段時間 PortAudio 是「未初始化」，
# 別的執行緒去查裝置會拿到空清單、甚至讀到已釋放的記憶體（審查用真的 PortAudio 重現：72% 拿到空清單，
# 偶爾 AssertionError／UnicodeDecodeError）。查裝置的一方不能等（主執行緒一等就是語音突顯時的 10 秒），
# 所以拿不到鎖就回上一份清單（_device_cache）。
_pa_lock = threading.Lock()
_device_cache: list[dict] = []

# 背景執行緒正在 PortAudio 裡（第一次載入或重掃）的件數。App 結束時 Python 會拆掉 sounddevice 模組、
# 卸載 PortAudio；背景執行緒剛好還在裡面就會踩到已釋放的記憶體閃退（審查模擬：80 次裡 34 次）。
_pa_busy = 0
_pa_busy_lock = threading.Lock()


@contextlib.contextmanager
def _portaudio_work():
    global _pa_busy
    with _pa_busy_lock:
        _pa_busy += 1
    try:
        yield
    finally:
        with _pa_busy_lock:
            _pa_busy -= 1


def _import_sounddevice():
    _load_portaudio()
    import sounddevice
    return sounddevice


def ensure_sounddevice() -> bool:
    """載入 sounddevice（第一次會做 PortAudio 初始化）。可重複呼叫、多執行緒同時呼叫只做一次。"""
    global sd, _sd_failed
    with _sd_lock:
        if sd is not None:
            return True
        if _sd_failed:
            return False
        try:
            with _portaudio_work():
                module = _import_sounddevice()
        except Exception:
            # 原本只接 ImportError，找不到 libportaudio 的 OSError 會讓整個 App 開不起來；
            # 現在可能在背景發生，一樣當成「不能錄音」，並留下紀錄
            log_error("sounddevice_load_failed")
            _sd_failed = True
            return False
        # sounddevice 自己註冊了「結束時關掉 PortAudio」，而且比我們晚註冊、會先跑——
        # 那時背景重掃可能正在 initialize，兩邊同時動 PortAudio。改由 _on_exit 在確認沒人在用之後才呼叫。
        try:
            atexit.unregister(module._exit_handler)
        except Exception:
            pass
        sd = module
        _refresh_device_cache()
        return True


def _refresh_device_cache() -> None:
    global _device_cache
    try:
        _device_cache = [
            {"id": i, "name": d["name"]}
            for i, d in enumerate(sd.query_devices())
            if d.get("max_input_channels", 0) > 0
        ]
    except Exception:
        log_error("query_audio_devices_failed")


@atexit.register
def _on_exit() -> None:
    """App 結束：背景執行緒還在 PortAudio／AudioQueue 裡就最多等 2 秒；還沒好就直接結束程序，
    不讓 Python 拆掉它正在用的模組（會閃退）。沒人在用才關掉還開著的 AudioQueue（暖機的、錄音中的），
    再照 sounddevice 原本的方式關掉 PortAudio。

    最早註冊（recorder 很早就被 import），所以最後才跑：pidfile 清理、啟動紀錄等其他收尾都已做完。"""
    deadline = time.monotonic() + 2.0
    while (_pa_busy or _mac.busy_count()) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _pa_busy or _mac.busy_count():
        try:
            log.warning("RECORD: exiting while PortAudio/AudioQueue work is in flight; skipping teardown")
        except Exception:
            pass
        os._exit(0)
        return   # 正式環境到不了這裡；測試把 os._exit 換掉時，不能接著真的去關 PortAudio
    try:
        _mac.close_all()   # 暖機中的、錄音中的都在登記表裡；關掉 = 選單列的橘色麥克風燈跟著熄
    except Exception:
        pass
    if sd is not None:
        try:
            sd._exit_handler()
        except Exception:
            pass


def sounddevice_ready() -> bool:
    return sd is not None


def sounddevice_failed() -> bool:
    return _sd_failed


if not IS_MAC:
    ensure_sounddevice()


_MIC_MODE_NAMES = {0: "標準", 1: "寬廣頻譜", 2: "語音突顯"}


def _preferred_microphone_mode_ctypes() -> int:
    """+[AVCaptureDevice preferredMicrophoneMode]，直接用 ctypes 呼叫 Objective-C runtime。

    不用 PyObjC 的 objc.loadBundle：它載入 AVFoundation 時整段握著 GIL，主執行緒（畫面）會頓
    0.75～1.4 秒（審查實測）；ctypes 呼叫期間會放掉 GIL。"""
    import ctypes.util
    ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AVFoundation.framework/AVFoundation")
    rt = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
    rt.objc_getClass.restype = ctypes.c_void_p
    rt.objc_getClass.argtypes = [ctypes.c_char_p]
    rt.sel_registerName.restype = ctypes.c_void_p
    rt.sel_registerName.argtypes = [ctypes.c_char_p]
    rt.objc_msgSend.restype = ctypes.c_long
    rt.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    cls = rt.objc_getClass(b"AVCaptureDevice")
    if not cls:
        raise RuntimeError("AVCaptureDevice not found")
    return int(rt.objc_msgSend(cls, rt.sel_registerName(b"preferredMicrophoneMode")))


def read_microphone_mode(_reader=_preferred_microphone_mode_ctypes) -> Optional[int]:
    """這個 App 在 macOS 的麥克風模式：0 標準、1 寬廣頻譜、2 語音突顯；讀不到回 None。

    只讀：App 不能改這個設定，只有使用者能在控制中心改（錄音中才會出現「麥克風模式」）。
    記進日誌是為了下次啟動變慢時一眼看得出原因（2026-10-06 查了一整天才找到）。"""
    if not IS_MAC:
        return None
    try:
        return int(_reader())
    except Exception:
        return None


# ── 錄音參數常數 ──────────────────────────────────────────────────────────────

SAMPLE_RATE = 16_000   # Hz — Whisper 要求 16 kHz
CHANNELS    = 1        # 單聲道，減少資料量
DTYPE       = "float32"  # 振幅範圍 [-1.0, 1.0]
BLOCK_MS    = 100      # sounddevice callback 每次呼叫的時間塊大小（毫秒）


# 第一次開麥克風花超過這個秒數才失敗，就判定是「硬體本身起不來」而不是「清單過期」。
# 2026-10-03 EarPods 實測：coreaudiod 回「Failed calling start_io on remote driver」，
# 每次都要等 5～8.5 秒才失敗；同一支再試一次只會讓使用者再白等一輪。
# 反過來，清單過期（拔掉耳機後問預設麥克風拿到 -1）是 4 毫秒內就失敗。
_SLOW_OPEN_FAIL_S = 1.0


def coreaudio_input_names() -> set:
    """系統現在所有能收音的裝置名字（直接問 CoreAudio，剛插的也看得到）。問不到回空集合。
    要列舉全部裝置，語音突顯時約 2.5 秒——只能在背景執行緒呼叫。"""
    if not IS_MAC:
        return set()
    try:
        return {d["name"] for d in _mac.input_devices()}
    except Exception:
        return set()


def builtin_input_device_name() -> Optional[str]:
    """向 macOS 查「內建、而且能收音」的麥克風叫什麼名字；查不到回 None。

    為什麼不用名字比對（例如找含「MacBook」的裝置）：名字會隨系統語言變
    （「MacBook Pro的麥克風」／「MacBook Pro Microphone」），而且這台機器上就有
    一個叫「MacBook Pro的揚聲器」、卻帶 1 個輸入聲道的虛擬裝置——光看名字會認錯。
    改問 CoreAudio 每個裝置的「連接方式」，只認 built-in。

    沿用 start_device_monitor 已經在用的 ctypes 直呼 CoreAudio 寫法（專案沒裝
    pyobjc 的 CoreAudio 綁定）。任何一步出錯都回 None，呼叫端就跳過這個退路。
    """
    if not IS_MAC:
        return None
    try:
        import ctypes.util

        ca = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))
        cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFRelease.argtypes = [ctypes.c_void_p]

        class _Addr(ctypes.Structure):
            _fields_ = [("mSelector", ctypes.c_uint32),
                        ("mScope", ctypes.c_uint32),
                        ("mElement", ctypes.c_uint32)]

        def _cc(code: bytes) -> int:      # CoreAudio 四字碼 → uint32
            return int.from_bytes(code, "big")

        glob, inpt = _cc(b"glob"), _cc(b"inpt")
        system_object = ctypes.c_uint32(1)

        # 所有音訊裝置的 ID
        addr = _Addr(_cc(b"dev#"), glob, 0)
        size = ctypes.c_uint32(0)
        if ca.AudioObjectGetPropertyDataSize(system_object, ctypes.byref(addr), 0, None, ctypes.byref(size)) != 0:
            return None
        ids = (ctypes.c_uint32 * (size.value // 4))()
        if ca.AudioObjectGetPropertyData(system_object, ctypes.byref(addr), 0, None, ctypes.byref(size), ids) != 0:
            return None

        for dev in ids:
            dev_id = ctypes.c_uint32(dev)
            # 連接方式：只要內建（'bltn'）
            transport, sz = ctypes.c_uint32(0), ctypes.c_uint32(4)
            addr = _Addr(_cc(b"tran"), glob, 0)
            if ca.AudioObjectGetPropertyData(dev_id, ctypes.byref(addr), 0, None, ctypes.byref(sz), ctypes.byref(transport)) != 0:
                continue
            if transport.value != _cc(b"bltn"):
                continue
            # 要有輸入串流（內建喇叭也是 built-in，但不能收音）
            addr, sz = _Addr(_cc(b"stm#"), inpt, 0), ctypes.c_uint32(0)
            if ca.AudioObjectGetPropertyDataSize(dev_id, ctypes.byref(addr), 0, None, ctypes.byref(sz)) != 0 or sz.value == 0:
                continue
            # 名字（CFString，用完要 CFRelease）
            ref, sz = ctypes.c_void_p(), ctypes.c_uint32(ctypes.sizeof(ctypes.c_void_p))
            addr = _Addr(_cc(b"lnam"), glob, 0)
            if ca.AudioObjectGetPropertyData(dev_id, ctypes.byref(addr), 0, None, ctypes.byref(sz), ctypes.byref(ref)) != 0 or not ref.value:
                continue
            buf = ctypes.create_string_buffer(512)
            ok = cf.CFStringGetCString(ref, buf, 512, 0x08000100)   # kCFStringEncodingUTF8
            cf.CFRelease(ref)
            if ok:
                return buf.value.decode("utf-8")
        return None
    except Exception:
        log_error("builtin_mic_lookup_failed")
        return None


# ── v2.33.0：Mac 裝置清單快取與麥克風「暖機」 ─────────────────────────────────────

# Mac 的輸入裝置清單：[{"id": UID 字串, "name": 名字}]。主執行緒只讀這份快取，絕不自己列舉
# （語音突顯時列舉全部裝置要 2～4 秒）；填它的是暖機背景執行緒與 AudioRecorder.refresh_devices()。
_mac_devices: list = []
_mac_devices_ready = False   # 至少填過一次（空清單也算：真的沒有任何麥克風）
_mac_devices_event = threading.Event()   # 第一次填好時 set，給 wait_devices 等


def _set_mac_devices(devices: list) -> None:
    global _mac_devices, _mac_devices_ready
    _mac_devices = list(devices)
    _mac_devices_ready = True
    _mac_devices_event.set()


def wait_devices(timeout: float) -> bool:
    """等裝置清單快取第一次填好（背景執行緒用）。填好回 True，逾時回 False。

    為什麼要等：啟動時設定的麥克風只記了名字（清單還沒填），清單一填好就要對帳——
    設定的那支沒插著就退回系統預設，不然每次按錄音都會為了找它而卡住。"""
    if _mac_devices_ready:
        return True
    _mac_devices_event.wait(timeout)
    return _mac_devices_ready


def devices_ready() -> bool:
    """裝置清單可以信了嗎？Mac：快取填過；Windows：PortAudio 已載入。設定視窗用它決定要不要保留使用者選的那支。"""
    return _mac_devices_ready if IS_MAC else sd is not None


# 暖機要撐多久。30 秒是實測值（2026-10-08，App 身分、macOS 27、麥克風模式「語音突顯」）：
#   • 程序啟動後約 30 秒內，每開一次麥克風要 1～5 秒；30 秒後降到 0.05～0.08 秒（跟用哪種 API 無關，
#     推斷是系統替這個 App 準備語音突顯的初始化期）
#   • 啟動就開著、撐到 30 秒才關 → 之後每次開 0.06～0.08 秒；只撐 15 秒不夠
#   • 實驗程式：bench_aq_hold.py／bench_aq_hold15.py／bench_aq_hold30.py（總監暫存區，未入版控）
# 使用者已同意：因此每次開 App，選單列的橘色麥克風燈會亮約 30 秒（聲音沒有被錄下來）。
# 刻意不做成設定項：這是系統行為的補償，不是使用者的偏好。
_WARM_HOLD_S = 30.0


class _WarmState:
    """暖機狀態。所有欄位都由 _warm_lock 保護；絕不在握著 _warm_lock 時呼叫 AudioQueue（可能卡住）。"""

    def __init__(self) -> None:
        self.gen = 0                  # 世代號碼：作廢（release_warm）就 +1，背景執行緒開好後發現不符就自己關掉
        self.state = "none"           # none / opening / live / failed
        self.stream = None
        self.key: Optional[str] = None            # 這條串流是給哪個 _device_name 用的（None = 系統預設）
        # 設定的那支不在（沒插著）而退回開系統預設時，記下「當初是替哪個名字開的」。
        # 錄音器的 _device_name 在清單對帳前還是那個名字，沒有這欄的話 key(None) ≠ 名字，
        # 每按一次都會判成不符、丟掉暖機、主執行緒再列舉一次（審查 #0/#3，2026-10-09）
        self.requested: Optional[str] = None
        self.opened_default_id: Optional[int] = None   # 開的是系統預設時，當時預設麥克風的 AudioDeviceID
        self.in_use = False           # 錄音器正在用它錄
        self.expired = False          # 30 秒到了但還在錄：等 stop() 時才關
        self.sink = None              # 錄音中把聲音交給誰（沒在錄 = None，聲音直接丟掉）
        self.t0 = 0.0                 # prewarm 被呼叫那一刻（time.monotonic）
        self.live_at: Optional[float] = None
        self.error = ""
        self.done = threading.Event()   # 開啟中結束（開好／失敗／被作廢）
        self.done.set()
        self.wake = threading.Event()   # 叫醒等待 30 秒的背景執行緒（作廢時）


_warm = _WarmState()
_warm_lock = threading.Lock()


def _close_quietly(stream) -> None:
    try:
        stream.close()
    except Exception:
        log_error("warm_stream_close_failed")


def _close_in_background(stream) -> None:
    """關閉交給背景：AudioQueueStop 要等 callback 跑完，不要讓呼叫端（可能是主執行緒）等。"""
    if stream is None:
        return
    threading.Thread(target=_close_quietly, args=(stream,), daemon=True, name="MicClose").start()


def warm_state() -> str:
    """none（沒有暖機）／opening（還在開）／live（開好了）／failed。"""
    return _warm.state


def warm_wait(timeout: float) -> str:
    """等暖機從「開啟中」結束（開好／失敗／被作廢），回傳當時的狀態。背景執行緒用。"""
    _warm.done.wait(timeout)
    return _warm.state


def warm_info() -> dict:
    """給啟動日誌：{"state", "seconds"（從 prewarm 被呼叫算到開好）, "error"}。"""
    w = _warm
    secs = round(w.live_at - w.t0, 2) if w.live_at is not None else None
    return {"state": w.state, "seconds": secs, "error": w.error}


def prewarm(device_name: Optional[str] = None, hold_s: float = _WARM_HOLD_S) -> bool:
    """App 啟動最早期就在背景開著麥克風，撐到 hold_s 秒才關（見 _WARM_HOLD_S 的實測說明）。

    主執行緒絕不等它：立刻回傳，開麥克風在背景做。開好之前按熱鍵，gui 會提示「麥克風開啟中」。
    Windows 不需要（回 False）。已經在開／開好了也不重複開。"""
    if not IS_MAC:
        return False
    with _warm_lock:
        w = _warm
        if w.state in ("opening", "live"):
            return False
        w.gen += 1
        gen = w.gen
        w.state, w.stream, w.key, w.opened_default_id = "opening", None, None, None
        w.requested = None
        w.in_use, w.expired, w.sink, w.error = False, False, None, ""
        w.t0, w.live_at = time.monotonic(), None
        w.done.clear()
        w.wake.clear()
    threading.Thread(target=_warm_worker, args=(w, gen, device_name, hold_s),
                     daemon=True, name="MicWarmup").start()
    return True


def _warm_worker(w: _WarmState, gen: int, device_name: Optional[str], hold_s: float) -> None:
    # 一律用傳進來的 w，不去讀全域 _warm：這條執行緒活很久（等 30 秒），期間全域可能被換掉
    stream = None
    devices_listed = False
    try:
        default_id, default_name = None, None
        try:
            default_id, default_name = _mac.default_input()
        except Exception:
            log_error("warm_default_input_failed")
        uid, key, requested = None, device_name, None
        if device_name is not None and device_name != default_name:
            # 指定了預設以外的麥克風：要列舉才找得到它的 UID（約 2.5 秒）。這是暖機期間要多花的時間，
            # 但列舉的結果順便填進清單快取，省掉開完之後再列舉一次
            try:
                devices = _mac.input_devices()
                _set_mac_devices(devices)
                devices_listed = True
                uid = next((d["id"] for d in devices if d["name"] == device_name), None)
            except Exception:
                log_error("warm_list_devices_failed")
            if uid is None:
                key, requested = None, device_name   # 找不到（沒插著）→ 開預設，標記成「給系統預設用的」
        blocksize = int(BLOCK_MS / 1000 * SAMPLE_RATE)

        def _dispatch(indata, frames, time_info, status, _w=w):
            sink = _w.sink
            if sink is not None:
                sink(indata, frames, time_info, status)

        stream = _mac.AQInputStream(SAMPLE_RATE, CHANNELS, blocksize, uid, _dispatch)
        stream.start()
    except Exception as exc:
        if stream is not None:
            _close_quietly(stream)
        log_error("mic_warmup_failed")
        with _warm_lock:
            if w.gen == gen:
                w.state, w.error = "failed", f"{type(exc).__name__}: {exc}"
                w.done.set()
        return
    with _warm_lock:
        superseded = w.gen != gen
        if not superseded:
            w.stream, w.key, w.requested = stream, key, requested
            w.opened_default_id = default_id if uid is None else None
            w.state, w.live_at = "live", time.monotonic()
            w.done.set()
    if superseded:
        # 開的途中被作廢了（設定換麥克風、App 要關了）：開好的這條不能裝上去，自己關掉
        _close_quietly(stream)
        return
    log.info(f"RECORD: warm microphone live in {w.live_at - w.t0:.2f}s since startup "
             f"(key={key!r}, hold {hold_s:.0f}s)")
    if not devices_listed:
        # 開麥克風之後才列舉：先列舉會拖慢暖機（實測 2.5 秒）
        try:
            _set_mac_devices(_mac.input_devices())
        except Exception:
            log_error("warm_list_devices_failed")
    w.wake.wait(max(0.0, w.t0 + hold_s - time.monotonic()))
    if w.gen == gen:
        _expire_warm(w)


def _expire_warm(w: _WarmState) -> None:
    """30 秒到：沒在錄就關掉（橘燈熄）；正在用它錄就標記到期，等 stop() 時才關。"""
    with _warm_lock:
        if w.state != "live":
            return
        if w.in_use:
            w.expired = True
            return
        stream, w.stream, w.state = w.stream, None, "none"
        w.wake.set()
    _close_quietly(stream)


def release_warm() -> None:
    """作廢暖機：設定換了麥克風、裝置變動影響到它、App 關閉。

    還在開的：世代號碼 +1，背景開好後發現不符會自己關掉、不裝上去。
    正在用它錄音的：不在這裡關（錄音器還在收聲音），只把它從暖機卸下，stop() 時會發現不是暖機而關掉。"""
    stream = None
    with _warm_lock:
        w = _warm
        w.gen += 1
        if w.state == "live" and not w.in_use:
            stream = w.stream
        w.stream = None
        w.state = "none"
        w.done.set()
        w.wake.set()   # 還在等 30 秒的背景執行緒立刻醒來收工（已經結束的也無害）
    _close_in_background(stream)


def release_warm_if_affected(devices: list) -> bool:
    """裝置變動後，暖機那支受影響就作廢，回傳有沒有作廢。在背景執行緒呼叫（會問系統一個問題）。

    受影響 = 沒帶 UID（用系統預設）開的、而預設麥克風換了；或指定的那支從最新清單消失了；
    或當初找不到而退回預設的那支現在插上了。問不到就當作受影響
    （寧可多開一次，也不能讓之後的錄音留在已經不對的麥克風上）。

    「沒帶 UID」不等於「key 是 None」：指定的那支剛好就是當時的預設時，也是開預設（省一次列舉），
    這時預設換了一樣要作廢（審查 #1/#5/#8）。"""
    with _warm_lock:
        w = _warm
        if w.state != "live":
            return False
        key, opened_default_id, requested = w.key, w.opened_default_id, w.requested
    try:
        names = {d["name"] for d in devices}
        affected = False
        if opened_default_id is not None:
            affected = _mac.default_input_id() != opened_default_id
        elif key is None:
            affected = True   # 開預設但當時沒問到預設是誰：無從比對，當作受影響
        if not affected and key is not None:
            affected = key not in names
        if not affected and requested is not None:
            affected = requested in names
    except Exception:
        affected = True
    if affected:
        log.info(f"RECORD: warm microphone invalidated by device change (key={key!r})")
        release_warm()
    return affected


def _requested_still_absent(key: Optional[str], requested: Optional[str]) -> bool:
    """暖機替「設定的那支（沒插著）」開了系統預設時，錄音器要錄的也是系統預設——但只在那支『現在仍確定不在』時成立。

    不能只比名字：暖機開啟中（2～5 秒）那支才插上、或暖機自己的列舉丟了例外而那支其實插著，
    這時只比名字會讓使用者一直錄到內建麥克風，而且沒有任何提示（第二輪審查 P1/P5，2026-10-09 重現）。
    清單還沒填好 = 不知道，也不成立：不知道就不冒險，走一般流程（會自己解析 UID）。
    不在 release_warm_if_affected 補這個判斷：暖機還在開啟中時它直接回 False，接手這一刻看快取最準。"""
    if key is None or key != requested:
        return False
    return _mac_devices_ready and all(d["name"] != key for d in _mac_devices)


def _take_warm(key: Optional[str], sink):
    """錄音開始時嘗試接手暖機串流。回傳 (結果, 串流)：
    took（接手了）／opening（還在開，不等也不開第二條）／mismatch（開好了但不是這次要的那支）／none（沒有可用的）。"""
    with _warm_lock:
        w = _warm
        if w.state == "opening":
            return "opening", None
        if w.state != "live" or w.stream is None or w.in_use or w.expired:
            return "none", None
        if w.key != key and not _requested_still_absent(key, w.requested):
            return "mismatch", None
        w.in_use = True
        w.sink = sink
        return "took", w.stream


def _return_warm(stream) -> bool:
    """錄完把暖機串流還回去。回 True = 繼續留著等下一次；False = 已作廢或到期，呼叫端要關掉它。"""
    with _warm_lock:
        w = _warm
        if w.stream is not stream:
            return False    # 錄音中被 release_warm 卸下了
        w.in_use = False
        w.sink = None
        if w.expired or w.state != "live":
            w.stream, w.state = None, "none"
            return False
        return True


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
        self._stream_warm = False               # v2.33.0（Mac）：self._stream 是接手來的暖機串流，stop() 時要還回去
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
        # CoreAudio 裝置變動監聽相關 handle（用 ctypes 直呼 CoreAudio.framework，macOS 專屬）
        self._ca_lib = None          # CoreAudio.framework 的 CDLL handle
        self._ca_listener = None     # CFUNCTYPE callback 物件——必須存成 attribute 防 GC（否則 segfault）
        self._ca_addr = None         # AudioObjectPropertyAddress 實例（remove 時要傳回去）
        self._ca_addr_default = None # v2.32.3：「預設麥克風換了」那個監聽
        self._monitor_lock = threading.Lock()
        self._monitor_closed = False  # stop_device_monitor 之後不准再登記（登記改在背景做）
        self._ca_on_change = None    # 上層傳入的 on_change callable

        # Windows 裝置變動監聽相關（輪詢法，見 start_device_monitor 的 IS_WINDOWS 分支）：
        # 沒有 CoreAudio 這種事件推播機制，改成 background thread 每隔幾秒
        # 呼叫 refresh_portaudio() 比對裝置清單是否變化。
        self._poll_stop_event: Optional[threading.Event] = None   # 通知輪詢 thread 結束

        # D2-S1（v2.9.0）：generation counter 防 stale callback。
        # 流程：每次 start() bump +1；stop() 也 bump +1。callback 在進入時帶
        # 入「當時的 gen」snapshot，寫 frame 前比對 self._capture_gen 是否相符；
        # 不符就直接 drop（PortAudio in-flight callback 在 stop() 還沒 drain
        # 完時可能還會 fire 一次，舊 callback 不能把上一段 frames 寫進下一段
        # 已重置的 self._frames）。bump 不需要 lock 保護（int 賦值原子）。
        self._capture_gen: int = 0

        # v2.32.0：錄音當下把每一塊聲音也交給蘋果常駐引擎（邊錄邊辨識）。
        # None = 沒有人要聽。listener 在 PortAudio 即時執行緒裡被呼叫，必須極快、
        # 不能阻塞——目前唯一的使用者是 AppleStreamSession.push，只做 queue.put_nowait。
        self._block_listener = None

    # ── 公開介面 ──────────────────────────────────────────────────────────────

    def set_device_by_name(self, name: str) -> bool:
        """依名稱搜尋並設定錄音裝置。

        Args:
            name: 裝置完整名稱（由 list_devices() 回傳）。

        Returns:
            True 代表找到並設定成功，False 代表找不到（回退到系統預設）。
        """
        if IS_MAC and not _mac_devices_ready:
            # v2.33.0：啟動初期清單還沒填好（暖機背景執行緒正在列舉）。主執行緒不能自己列舉
            # （語音突顯時 2～4 秒），先記名字；UID 等清單填好（refresh_devices）或開麥克風時再對
            self._device_index = None
            self._device_name = name
            return True
        devices = self.list_devices()
        for dev in devices:
            if name == dev["name"]:
                self._device_index = dev["id"]    # Mac：UID 字串；Windows：PortAudio 編號
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
        if IS_MAC:
            return self._start_mac()
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
                try:
                    stream.start()
                except Exception:
                    # v2.31.4：建好但啟動失敗的串流要關掉——失敗之後會重整 PortAudio
                    # 清單、換一支麥克風再試，留著半開的串流會佔住裝置。
                    try:
                        stream.close()
                    except Exception:
                        pass
                    raise
                return stream

            # v2.21.4：標記本次是否改用別支麥克風（gui 端據此提示使用者）；
            # v2.31.4：再記下改用的是哪一種，提示文字才講得清楚
            self._started_with_fallback = False
            self._fallback_label = ""
            target = self._device_index
            t_first = time.perf_counter()
            try:
                log.info(
                    f"RECORD: Attempting to start microphone. "
                    f"Device={target}, Samplerate={SAMPLE_RATE}, "
                    f"Blocksize={blocksize}, gen={my_gen}"
                )
                self._stream = _open_stream(target)
                self._is_recording = True
                log.info(
                    f"RECORD: Stream active. Latency={self._stream.latency:.4f}s, "
                    f"CPU Load={self._stream.cpu_load:.2%}"
                )
                return True
            except Exception:
                log_error("recorder_init_failed", device=target)
                self._stream = None
                self._is_recording = False
            first_slow = (time.perf_counter() - t_first) > _SLOW_OPEN_FAIL_S
            return self._open_with_fallbacks(_open_stream, target, first_slow)

    def _start_mac(self) -> bool:
        """Mac 版 start()：不碰 sounddevice，用 AudioQueue。見檔案開頭與 prewarm 的說明。

        優先接手暖機串流（不碰硬體，立刻開始）；暖機還在開就直接回 False（不等、不開第二條）；
        其餘照一般流程開。"""
        with self._lock:
            if self._is_recording:
                log.warning("RECORD: Already recording, ignoring start request.")
                return True

            self._frames = []
            self._rms_level = 0.0
            self._capture_gen += 1
            my_gen = self._capture_gen
            self._started_with_fallback = False
            self._fallback_label = ""
            t_begin = time.perf_counter()

            # 暖機串流的 callback 在錄音開始前就建好了，聲音一直在進來；所以聲音交給誰（sink）
            # 在這裡才掛上，而且綁定這一次錄音的世代號碼——沒掛上之前進來的聲音直接丟掉
            def _sink(indata, frames, time_info, status, _gen=my_gen):
                self._audio_callback(indata, frames, time_info, status, _gen)

            verdict, warm_stream = _take_warm(self._device_name, _sink)
            if verdict == "took":
                self._stream = warm_stream
                self._stream_warm = True
                self._is_recording = True
                log.info(f"RECORD: took over warm microphone in {(time.perf_counter() - t_begin) * 1000:.1f} ms "
                         f"(device={self._device_name!r}, gen={my_gen})")
                return True
            if verdict == "opening":
                # 暖機還在開（約 2～3 秒、最慢 30 秒）。這時再開第二條會讓兩邊都更慢；gui 在按下前就會先擋，
                # 這裡是防呆
                log.warning("RECORD: warm microphone still opening; start refused")
                return False
            if verdict == "mismatch":
                release_warm()   # 開著的不是這次要的那支（設定換了麥克風），關掉、照一般流程開

            target = self._device_index
            forced_label = ""
            if self._device_name and target is None:
                target = self._index_for_name(self._device_name)   # 只查快取，不列舉
                enumerated = False
                if target is None and not _mac_devices_ready:
                    # 清單還沒填好才走這條慢路徑：當場向系統找（語音突顯時 2～4 秒）。
                    # 現在只剩「暖機失敗＋清單未填」的極少數情況（gui 會等清單填好再對帳）
                    try:
                        devices = _mac.input_devices()
                        _set_mac_devices(devices)
                        enumerated = True
                        target = self._index_for_name(self._device_name)
                    except Exception:
                        log_error("recorder_resolve_device_failed", device=self._device_name)
                if target is not None:
                    self._device_index = target
                elif _mac_devices_ready or enumerated:
                    # 清單已填好、裡面沒有這支 = 它不在（沒插著）。不要再列舉：直接開預設，
                    # 並清掉 runtime 的名字，不然每按一次都重來一遍（審查 #0/#3）。
                    # 只動 runtime，config 不動（跟 _apply_device_list 一樣：重新插回來時仍會用回原裝置）
                    log.warning(f"RECORD: input_device '{self._device_name}' not in device list, "
                                f"falling back to the system default")
                    self._device_name = None
                    self._device_index = None
                    forced_label = "系統預設麥克風"   # 照舊版：退回系統預設並提示（名字清掉了，只會提示這一次）
                else:
                    forced_label = "系統預設麥克風"   # 列舉本身失敗：不確定，保留名字，下次再試
            blocksize = int(BLOCK_MS / 1000 * SAMPLE_RATE)

            def _open_stream(uid):
                def _cb(indata, frames, time_info, status, _gen=my_gen):
                    self._audio_callback(indata, frames, time_info, status, _gen)
                stream = _mac.AQInputStream(SAMPLE_RATE, CHANNELS, blocksize, uid, _cb)
                try:
                    stream.start()
                except Exception:
                    # 建好但啟動失敗的串流要關掉，不然留著半開的佔住裝置（v2.31.4 同樣的理由）。
                    # 交給背景關：我們正握著 self._lock，AudioQueueStop 會等 callback 跑完而 callback
                    # 要拿這把鎖——在鎖內關就是死結（審查 #2 用「start 失敗前 callback 已在跑」的假串流重現）
                    _close_in_background(stream)
                    raise
                return stream

            t_first = time.perf_counter()
            try:
                log.info(f"RECORD: Attempting to start microphone (AudioQueue). Device={target}, "
                         f"Samplerate={SAMPLE_RATE}, Blocksize={blocksize}, gen={my_gen}")
                self._stream = _open_stream(target)
                self._stream_warm = False
                self._is_recording = True
                if forced_label:
                    self._started_with_fallback = True
                    self._fallback_label = forced_label
                log.info(f"RECORD: Stream active in {time.perf_counter() - t_first:.2f}s "
                         f"(warm takeover: no, verdict={verdict})")
                return True
            except Exception:
                log_error("recorder_init_failed", device=target)
                self._stream = None
                self._is_recording = False
            first_slow = (time.perf_counter() - t_first) > _SLOW_OPEN_FAIL_S
            return self._open_with_fallbacks_mac(_open_stream, target, first_slow)

    def _open_with_fallbacks_mac(self, open_fn, target: Optional[str], first_slow: bool) -> bool:
        """Mac 版的開麥克風補救（v2.31.4 的順序與判斷原封不動，只是裝置身分從 PortAudio 編號換成 UID）。
        呼叫端（_start_mac）已持有 self._lock。

        順序：①重新列舉清單 ②同一支再試（只在瞬間失敗時）③內建麥克風 ④系統預設。
        UID 在裝置存在期間不變，所以不像 PortAudio 要擔心重掃後編號位移；①是為了拿到剛插拔後的新清單。
        """
        self.refresh_devices()   # 列舉失敗回 None、快取不動：不用它的回傳值，拿手上的快取繼續往下試
        if self._device_name is not None:
            self._device_index = self._index_for_name(self._device_name)

        candidates: list[tuple[Optional[str], str]] = []
        if not first_slow:
            if self._device_name is None:
                candidates.append((target, ""))
            else:
                uid = self._index_for_name(self._device_name)
                if uid is not None:
                    candidates.append((uid, ""))
        builtin = builtin_input_device_name()
        if builtin and not (first_slow and builtin == self._device_name):
            uid = self._index_for_name(builtin)
            if uid is not None:
                candidates.append((uid, "內建麥克風"))
        if target is not None:
            candidates.append((None, "系統預設麥克風"))

        tried: set = set()
        for dev, label in candidates:
            if dev in tried:
                continue
            tried.add(dev)
            try:
                log.warning(f"RECORD: retry device={dev} ({label or 'same device after refresh'})")
                self._stream = open_fn(dev)
                self._stream_warm = False
                self._is_recording = True
                self._started_with_fallback = bool(label)
                self._fallback_label = label
                log.info(f"RECORD: Stream active（{label or '重整清單後同一支'}）")
                return True
            except Exception:
                log_error("recorder_init_failed_fallback", device=dev, step=label or "retry_same")
                self._stream = None
                self._is_recording = False
        return False

    def _index_for_name(self, name: Optional[str]):
        """依裝置名字找目前清單裡的識別（Windows：PortAudio 編號；Mac：UID 字串）；找不到回 None。"""
        if not name:
            return None
        for dev in self.list_devices():
            if dev["name"] == name:
                return dev["id"]
        return None

    def _open_with_fallbacks(self, open_fn, target: Optional[int], first_slow: bool) -> bool:
        """第一次開麥克風失敗之後的補救。呼叫端（start）已持有 self._lock。

        v2.21.4 的舊邏輯只有一條退路：「指定裝置失敗 → 改系統預設」。2026-10-03／04
        使用者遇到兩種它救不了的狀況：
          • 系統預設本身就是壞掉的那支（EarPods 硬體起不來）——舊邏輯只在「有指定裝置」
            時才退，預設失敗就直接放棄
          • 拔掉耳機後 PortAudio 還記著舊清單，問預設麥克風拿到 -1（瞬間失敗）
            ——其實重整清單再試就好，舊邏輯從來不重整
        順序：①重整清單 ②同一支再試（只在瞬間失敗時）③內建麥克風 ④系統預設。
        內建排在系統預設前面：實際出事時，系統預設往往就是那支壞掉的外接裝置。
        """
        # ① 重整清單（refresh_portaudio 自己會拿 self._lock，RLock 可重入）
        self.refresh_portaudio()
        # v2.32.3：重整後所有編號都可能位移——不管下面走哪條路，都先把指定麥克風的編號對回來
        # （不在清單裡就清掉），不然舊編號會一直指錯，審查模擬出錄到 BlackHole、一片空白（2026-10-07）
        if self._device_name is not None:
            self._device_index = self._index_for_name(self._device_name)

        candidates: list[tuple[Optional[int], str]] = []
        # ② 同一支再試一次：只在第一次是瞬間失敗時（慢慢失敗 = 硬體卡住，再試只是白等）
        if not first_slow:
            if self._device_name is None:
                candidates.append((target, ""))
            else:
                idx = self._index_for_name(self._device_name)
                if idx is not None:
                    self._device_index = idx   # 重整後編號會變，順手更新給下次用
                    candidates.append((idx, ""))
        # ③ 內建麥克風（第一次慢慢失敗的就是內建本身時，不再試它）
        builtin = builtin_input_device_name()
        if builtin and not (first_slow and builtin == self._device_name):
            idx = self._index_for_name(builtin)
            if idx is not None:
                candidates.append((idx, "內建麥克風"))
        # ④ 系統預設：v2.21.4 原本的退路，留作最後一道
        if target is not None:
            candidates.append((None, "系統預設麥克風"))

        tried: set = set()
        for dev, label in candidates:
            if dev in tried:
                continue
            tried.add(dev)
            try:
                log.warning(f"RECORD: retry device={dev} ({label or 'same device after refresh'})")
                self._stream = open_fn(dev)
                self._is_recording = True
                self._started_with_fallback = bool(label)
                self._fallback_label = label
                log.info(f"RECORD: Stream active（{label or '重整清單後同一支'}）")
                return True
            except Exception:
                log_error("recorder_init_failed_fallback", device=dev, step=label or "retry_same")
                self._stream = None
                self._is_recording = False
        return False

    def set_block_listener(self, fn) -> None:
        """設定（或用 None 清除）「每收到一塊聲音就呼叫」的掛勾。fn 收到一維 float32 陣列。"""
        self._block_listener = fn

    def stop(self) -> np.ndarray:
        """停止錄音，回傳完整錄音資料。

        Returns:
            float32 一維 numpy 陣列（所有幀 concatenate 後）。
            若沒有捕捉到任何音訊則回傳空陣列。
        """
        stream, was_warm = None, False
        with self._lock:
            if not self._is_recording:
                log.debug("RECORD: stop() called while already idle.")
                return np.zeros(0, dtype=np.float32)

            log.info("RECORD: Stopping audio stream...")
            self._is_recording = False
            # D2-S1：bump generation；任何還沒 drain 的 in-flight callback 立刻 stale
            self._capture_gen += 1

            if IS_MAC:
                # 只在鎖內把串流「拆下來」，關硬體留到鎖外：AudioQueueStop(immediate) 會等正在跑的
                # callback 跑完，而 callback 要拿 self._lock——在鎖內關，兩邊互等就死結
                stream, was_warm = self._stream, self._stream_warm
                self._stream = None
                self._stream_warm = False
            # 關閉 PortAudio 串流，釋放硬體資源
            elif self._stream is not None:
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

        if stream is not None:
            if was_warm and _return_warm(stream):
                log.info("RECORD: warm microphone kept for next recording")
            else:
                # 一律交給背景關：開 App 30 秒內關一次實測要 1～3 秒（2026-10-08 實測，App 身分、語音突顯），
                # stop() 是主執行緒呼叫的，不能讓畫面卡住。暖機串流也一樣——錄音中被 release_warm 卸下的
                # （裝置變動、設定換麥克風）可能還在 30 秒內，不能假設「到期了所以關得快」（第三輪審查實測卡 0.8 秒）。
                # 錄到的 frames 已在鎖內拿走、callback 有世代檢查，晚關不影響回傳的錄音
                _close_in_background(stream)
                log.info("RECORD: AudioQueue stream close handed to background.")

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

    def sample_count(self) -> int:
        """目前已錄到幾個樣本（不複製任何資料）。"""
        with self._lock:
            return sum(len(f) for f in self._frames)

    def get_tail(self, n_samples: int) -> np.ndarray:
        """v2.32.0：只取最後 n_samples 個樣本。

        不用 get_buffer_snapshot()／get_recent_buffer() 的原因：它們每次都把整段錄音串起來，
        23 分鐘的錄音每秒要搬約 88 MB。這裡從尾巴往前只串夠用的那幾塊。
        """
        with self._lock:
            if not self._frames or n_samples <= 0:
                return np.zeros(0, dtype=np.float32)
            picked, total = [], 0
            for frame in reversed(self._frames):
                picked.append(frame)
                total += len(frame)
                if total >= n_samples:
                    break
            return np.concatenate(picked[::-1], axis=0).flatten()[-n_samples:]

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
            Mac（v2.33.0）：id 是 UID 字串，內容是快取——主執行緒絕不做列舉，
            快取由暖機背景執行緒與 refresh_devices() 填。
        """
        if IS_MAC:
            return list(_mac_devices)
        if sd is None:
            return []
        # v2.32.3：重掃進行中（PortAudio 未初始化）就回上一份清單，不等（見 _pa_lock）
        if not _pa_lock.acquire(blocking=False):
            return list(_device_cache)
        try:
            _refresh_device_cache()
            return list(_device_cache)
        finally:
            _pa_lock.release()

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
                with _pa_lock, _portaudio_work():
                    sd._terminate()
                    sd._initialize()
                    _refresh_device_cache()
                log.info("RECORD: PortAudio re-initialized for device refresh")
            except Exception:
                log_error("portaudio_reinit_failed")
        return self.list_devices()

    def refresh_devices(self) -> Optional[list]:
        """重新取得輸入裝置清單（跨平台）。要在背景執行緒呼叫，會等到列舉完才回傳。

        Mac：重新向 CoreAudio 列舉並更新快取（語音突顯時約 2.5 秒）。不需要拿錄音鎖、錄音中也可以做——
        它只讀系統、不碰正在錄音的串流（PortAudio 重掃會打斷串流，所以 Windows 那邊錄音中要跳過）。
        列舉失敗回 None、快取不動：回舊快取（尤其是還沒填過的空清單）會被呼叫端當成權威的
        「現在沒有任何麥克風」，把使用者選的那支當成已移除（審查 #6）。
        Windows：照舊呼叫 refresh_portaudio()。"""
        if not IS_MAC:
            return self.refresh_portaudio()
        try:
            devices = _mac.input_devices()
        except Exception:
            log_error("coreaudio_list_failed")
            return None
        _set_mac_devices(devices)
        # 啟動初期 set_device_by_name 只記了名字（當時清單是空的）：清單一到就把 UID 對上
        name = self._device_name
        if name and self._device_index is None:
            for d in devices:
                if d["name"] == name:
                    self._device_index = d["id"]
                    break
        log.info(f"RECORD: CoreAudio device list refreshed ({len(devices)} inputs)")
        return list(devices)

    def warm_state(self) -> str:
        return warm_state()

    def release_warm(self) -> None:
        release_warm()

    def release_warm_if_affected(self, devices: list) -> bool:
        return release_warm_if_affected(devices)

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
        """註冊麥克風裝置變動監聽（插拔偵測）。

        on_change 是無參數 callable，裝置插/拔時被呼叫。
        注意：callback 在背景執行緒被呼叫（macOS 是 CoreAudio 自己的執行緒、
        Windows 是本函式開的輪詢 thread），呼叫端自己負責 marshal 到主執行緒
        （例如用 tkinter 的 master.after(0, ...)）。

        macOS：直接 ctypes 呼叫 CoreAudio.framework 註冊 AudioObjectAddPropertyListener，
        事件驅動、插拔立即感知。
        Windows：CoreAudio 是 Apple 私有框架，沒有對應物，改用輪詢法——background
        thread 每隔幾秒呼叫 refresh_portaudio() 比對裝置清單是否變化，變了就觸發
        on_change。體感差異是「拔麥克風後最多等數秒才偵測到」而非「立即感知」，可接受。

        回 True = 註冊成功；失敗（ctypes 載入失敗等）回 False、不拋例外。
        """
        if IS_MAC:
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
                # v2.32.3：也監聽「預設麥克風換了」。用系統預設時，插耳機後系統可能晚一點才把預設換過去，
                # 只聽「裝置清單變了」的話，預設換了卻沒人通知——v2.33.0 暖機開的是「預設」那支，
                # 預設換了就要作廢暖機（release_warm_if_affected）
                addr_default = AudioObjectPropertyAddress(
                    int.from_bytes(b"dIn ", "big"),
                    kAudioObjectPropertyScopeGlobal,
                    kAudioObjectPropertyElementMain,
                )

                # ── 註冊監聽 ──
                # v2.32.3：登記改在背景做；跟 stop_device_monitor 用同一把鎖，App 關掉之後不能再登記
                # （不然監聽留在系統裡、指向已釋放的 callback，關閉時有閃退風險）
                with self._monitor_lock:
                    if self._monitor_closed:
                        return False
                    status = ca.AudioObjectAddPropertyListener(
                        ctypes.c_uint32(kAudioObjectSystemObject),
                        ctypes.byref(addr),
                        cb,
                        None,
                    )
                    if status != 0:
                        log.warning(f"RECORD: AudioObjectAddPropertyListener failed, status={status}")
                        return False
                    if ca.AudioObjectAddPropertyListener(
                        ctypes.c_uint32(kAudioObjectSystemObject), ctypes.byref(addr_default), cb, None,
                    ) != 0:
                        addr_default = None   # 只少了「預設換了」的通知，裝置清單的通知照樣有

                    # ── 存 handle ──
                    # cb（CFUNCTYPE 實體）必須存成 instance attribute，否則離開本函式後被 GC 回收，
                    # CoreAudio 之後回呼一個已釋放的位址 → segfault。addr 同理（remove 時要傳回去）。
                    self._ca_lib = ca
                    self._ca_listener = cb
                    self._ca_addr = addr
                    self._ca_addr_default = addr_default
                    self._ca_on_change = on_change
                log.info("RECORD: CoreAudio device monitor registered")
                return True
            except Exception:
                log_error("start_device_monitor_failed")
                return False
        elif IS_WINDOWS:
            # Windows：沒有 CoreAudio 事件推播，改用輪詢法。
            # 每 POLL_INTERVAL_SEC 秒呼叫 refresh_portaudio()（正在錄音時該函式
            # 內部會自動跳過 re-init，不影響錄音中的裝置）比對裝置名稱集合是否
            # 變化，變了才觸發 on_change；呼叫端 marshal 到主執行緒的責任不變。
            POLL_INTERVAL_SEC = 2.0
            try:
                stop_event = threading.Event()

                def _poll_loop():
                    try:
                        last_names = {d["name"] for d in self.list_devices()}
                    except Exception:
                        last_names = set()
                    while not stop_event.wait(POLL_INTERVAL_SEC):
                        try:
                            current_names = {d["name"] for d in self.refresh_portaudio()}
                        except Exception:
                            continue
                        if current_names != last_names:
                            last_names = current_names
                            try:
                                on_change()
                            except Exception:
                                try:
                                    log_error("device_monitor_callback_failed")
                                except Exception:
                                    pass

                thread = threading.Thread(
                    target=_poll_loop, daemon=True, name="DeviceMonitorPoll"
                )
                self._poll_stop_event = stop_event
                self._monitor_thread = thread
                self._ca_on_change = on_change   # 供 stop_device_monitor 判斷是否已註冊
                thread.start()
                log.info("RECORD: Windows device monitor (polling) started")
                return True
            except Exception:
                log_error("start_device_monitor_failed")
                return False
        else:
            log.info("RECORD: device monitor not supported on this platform")
            return False

    def stop_device_monitor(self) -> None:
        """移除裝置變動監聽（on_close 時呼叫）。包 try/except，失敗只記錄。

        macOS：移除 CoreAudio AudioObjectPropertyListener。
        Windows：通知輪詢 thread 結束（threading.Event set），不需要對應
        remove listener 的動作。
        """
        if IS_MAC:
            with self._monitor_lock:
                self._monitor_closed = True   # 背景還沒登記的話，之後也不准登記了
                if self._ca_lib is None or self._ca_listener is None or self._ca_addr is None:
                    return
                try:
                    kAudioObjectSystemObject = 1
                    for a in (self._ca_addr, getattr(self, "_ca_addr_default", None)):
                        if a is not None:
                            self._ca_lib.AudioObjectRemovePropertyListener(
                                ctypes.c_uint32(kAudioObjectSystemObject),
                                ctypes.byref(a),
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
                    self._ca_addr_default = None
                    self._ca_on_change = None
        elif IS_WINDOWS:
            if self._poll_stop_event is None:
                return
            try:
                self._poll_stop_event.set()   # 通知輪詢 thread 結束迴圈
                log.info("RECORD: Windows device monitor (polling) stopped")
            except Exception:
                log_error("stop_device_monitor_failed")
            finally:
                self._poll_stop_event = None
                self._monitor_thread = None
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
            # v2.32.0：交給邊錄邊辨識。先取一份本地參照，避免另一條執行緒剛好清成 None。
            # 錄音是單聲道（CHANNELS=1），攤平成一維；掛勾出錯絕不能影響錄音本身。
            listener = self._block_listener
            if listener is not None:
                try:
                    listener(chunk.reshape(-1))
                except Exception:
                    pass
        except Exception:
            # callback 中任何例外都不能往上拋（會讓 PortAudio 崩潰），只能記錄
            log_error("audio_callback_failed")
            self._is_recording = False
