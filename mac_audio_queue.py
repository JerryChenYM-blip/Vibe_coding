"""macOS 專用：用系統的 AudioQueue 收麥克風聲音（取代 Mac 上的 sounddevice／PortAudio）。

為什麼不用 PortAudio（2026-10-08 實測，App 身分、macOS 27、麥克風模式「語音突顯」）：
  • PortAudio 一初始化就掃全部音訊裝置，約 126 次向系統查屬性；語音突顯時每次 0.05～0.3 秒，
    合計 10～15 秒，而且它沒有辦法延後掃描
  • AudioQueue 只開「一支」麥克風：程序啟動後 1.7～2.6 秒就收到聲音
  • 兩個 AudioQueue 同時開同一支麥克風可以（兩個都收得到）；但 AudioQueue 開著時 PortAudio 再開同一支，
    3 秒內收不到聲音 → Mac 上不能兩套並用，整個 Mac 路徑都改走這裡

本模組只在 Mac 用；Windows 上 import 不能炸，所以 AudioToolbox／CoreAudio 都延到第一次使用才載入。

匯出：
  AQInputStream      模仿 sd.InputStream 被用到的那一小部分（start／stop／close／latency／active）
  default_input / default_input_id / input_devices   CoreAudio 查詢（不開麥克風）
  busy_count / close_all                             App 結束時的清理用（見 recorder._on_exit）
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import threading
from typing import Callable, Optional

import numpy as np

_UTF8 = 0x08000100   # kCFStringEncodingUTF8


def _cc(code: bytes) -> int:
    """CoreAudio 的常數是 4 個 ASCII 字元打包成 uint32（big-endian）。"""
    return int.from_bytes(code, "big")


# ── ctypes 結構與 callback 型別（純 Python 定義，不需要載入任何系統函式庫）──────────

class _ASBD(ctypes.Structure):
    _fields_ = [("mSampleRate", ctypes.c_double), ("mFormatID", ctypes.c_uint32),
                ("mFormatFlags", ctypes.c_uint32), ("mBytesPerPacket", ctypes.c_uint32),
                ("mFramesPerPacket", ctypes.c_uint32), ("mBytesPerFrame", ctypes.c_uint32),
                ("mChannelsPerFrame", ctypes.c_uint32), ("mBitsPerChannel", ctypes.c_uint32),
                ("mReserved", ctypes.c_uint32)]


class _AQBuf(ctypes.Structure):
    _fields_ = [("mAudioDataBytesCapacity", ctypes.c_uint32), ("mAudioData", ctypes.c_void_p),
                ("mAudioDataByteSize", ctypes.c_uint32), ("mUserData", ctypes.c_void_p),
                ("mPacketDescriptionCapacity", ctypes.c_uint32), ("mPacketDescriptions", ctypes.c_void_p),
                ("mPacketDescriptionCount", ctypes.c_uint32)]


class _Addr(ctypes.Structure):
    _fields_ = [("sel", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("elem", ctypes.c_uint32)]


# AudioQueueInputCallback(userData, queue, buffer, startTime, numPackets, packetDescs)
_CB_TYPE = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_AQBuf),
                            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p)

# 緩衝區數量。實驗用 3 個 50 ms 曾出現 0.25 秒的聲音間隔（系統忙時來不及補），所以改成 8 個、
# 每個 blocksize 大小（recorder 用 1600 樣本 = 100 ms，合計 0.8 秒的餘裕）
_N_BUFFERS = 8

_libs = None
_libs_lock = threading.Lock()


def _load():
    """第一次用才載入 AudioToolbox／CoreAudio／CoreFoundation，並設好函式簽章。"""
    global _libs
    with _libs_lock:
        if _libs is not None:
            return _libs
        at = ctypes.CDLL(ctypes.util.find_library("AudioToolbox"))
        ca = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))
        cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        at.AudioQueueNewInput.argtypes = [ctypes.POINTER(_ASBD), _CB_TYPE, ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
        at.AudioQueueSetProperty.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32]
        at.AudioQueueAllocateBuffer.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                                ctypes.POINTER(ctypes.POINTER(_AQBuf))]
        at.AudioQueueEnqueueBuffer.argtypes = [ctypes.c_void_p, ctypes.POINTER(_AQBuf), ctypes.c_uint32,
                                               ctypes.c_void_p]
        at.AudioQueueStart.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        at.AudioQueueStop.argtypes = [ctypes.c_void_p, ctypes.c_uint8]
        at.AudioQueueDispose.argtypes = [ctypes.c_void_p, ctypes.c_uint8]
        ca.AudioObjectGetPropertyDataSize.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Addr), ctypes.c_uint32,
                                                      ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        ca.AudioObjectGetPropertyData.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Addr), ctypes.c_uint32,
                                                  ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        _libs = (at, ca, cf)
        return _libs


# ── 結束時的清理用：開著的串流、正在 AudioQueue 呼叫裡的件數 ─────────────────────────
# AudioQueueStart／Stop／Dispose 可能卡住（驅動程式沒回應）。App 結束時 Python 會拆掉模組，背景執行緒
# 剛好還卡在裡面就會踩到已釋放的記憶體閃退（sounddevice 那邊審查模擬過，同樣的道理），
# 所以 recorder._on_exit 要能問「有沒有人還在裡面」並在結束時關掉所有還開著的串流。

_live: set = set()
_live_lock = threading.Lock()
_busy = 0
_busy_lock = threading.Lock()


@contextlib.contextmanager
def _aq_work():
    global _busy
    with _busy_lock:
        _busy += 1
    try:
        yield
    finally:
        with _busy_lock:
            _busy -= 1


def busy_count() -> int:
    return _busy


def close_all() -> int:
    """關掉所有還開著的串流（App 結束時用），回傳關了幾個。個別失敗不影響其他的。"""
    with _live_lock:
        streams = list(_live)
    for s in streams:
        try:
            s.close()
        except Exception:
            pass
    return len(streams)


class AQInputStream:
    """一條 AudioQueue 麥克風輸入。callback(indata, frames, time_info, status) 跟 sd.InputStream 同形：
    indata 是 float32、shape (frames, channels) 的新陣列（已複製，callback 可以留著用）。

    device_uid=None 用系統預設麥克風；給 UID 字串就指定那一支（kAudioQueueProperty_CurrentDevice）。"""

    latency = 0.0   # 不重要（recorder 只拿來寫日誌）；AudioQueue 沒有等價的即時數字

    def __init__(self, samplerate: int, channels: int, blocksize: int,
                 device_uid: Optional[str], callback: Callable) -> None:
        self._callback = callback
        self._channels = channels
        self._closing = False       # 關閉中：callback 不再把緩衝區放回去，也不再交資料出去
        self._closed = False
        self._started = False
        self._state_lock = threading.Lock()
        self._aq = ctypes.c_void_p()
        self._buffers: list = []
        # CFUNCTYPE 物件必須存在實例上：被 GC 回收後系統再回呼已釋放的位址會閃退
        self._cb = _CB_TYPE(self._on_buffer)
        at, _, cf = _load()

        fmt = _ASBD(float(samplerate), _cc(b"lpcm"), 1 | 8,   # float32、packed
                    4 * channels, 1, 4 * channels, channels, 32, 0)
        try:
            with _aq_work():
                st = at.AudioQueueNewInput(ctypes.byref(fmt), self._cb, None, None, None, 0,
                                           ctypes.byref(self._aq))
                if st != 0:
                    self._aq = ctypes.c_void_p()
                    raise OSError(f"AudioQueueNewInput failed, status={st}")
                if device_uid:
                    ref = cf.CFStringCreateWithCString(None, device_uid.encode("utf-8"), _UTF8)
                    try:
                        p = ctypes.c_void_p(ref)
                        st = at.AudioQueueSetProperty(self._aq, _cc(b"aqcd"), ctypes.byref(p), ctypes.sizeof(p))
                    finally:
                        if ref:
                            cf.CFRelease(ref)
                    if st != 0:
                        raise OSError(f"AudioQueueSetProperty(CurrentDevice) failed, status={st}")
                nbytes = max(1, blocksize) * 4 * channels
                for _ in range(_N_BUFFERS):
                    buf = ctypes.POINTER(_AQBuf)()
                    st = at.AudioQueueAllocateBuffer(self._aq, nbytes, ctypes.byref(buf))
                    if st != 0:
                        raise OSError(f"AudioQueueAllocateBuffer failed, status={st}")
                    st = at.AudioQueueEnqueueBuffer(self._aq, buf, 0, None)
                    if st != 0:
                        raise OSError(f"AudioQueueEnqueueBuffer failed, status={st}")
                    self._buffers.append(buf)
        except Exception:
            self._dispose()
            raise
        with _live_lock:
            _live.add(self)

    # ── 系統的執行緒呼叫 ──────────────────────────────────────────────────────

    def _on_buffer(self, _ud, aq, buf, _ts, _npackets, _pd) -> None:
        # 這裡的例外跑出 ctypes callback 會讓整個程序閃退，全部吞掉
        try:
            if self._closing:
                return
            b = buf.contents
            n = b.mAudioDataByteSize // 4
            if n:
                raw = (ctypes.c_float * n).from_address(b.mAudioData)
                data = np.frombuffer(raw, dtype=np.float32).copy()   # 複製：緩衝區馬上要還給系統
                frames = n // self._channels
                self._callback(data[: frames * self._channels].reshape(frames, self._channels),
                               frames, None, None)
        except Exception:
            pass
        try:
            if not self._closing:
                at, _, _ = _load()
                at.AudioQueueEnqueueBuffer(aq, buf, 0, None)
        except Exception:
            pass

    # ── 公開介面 ──────────────────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        return self._started and not self._closed

    def start(self) -> None:
        if self._closed:
            raise OSError("AudioQueue already closed")
        at, _, _ = _load()
        with _aq_work():
            st = at.AudioQueueStart(self._aq, None)
        if st != 0:
            raise OSError(f"AudioQueueStart failed, status={st}")
        self._started = True

    def stop(self) -> None:
        if self._closed or not self._started:
            return
        at, _, _ = _load()
        with _aq_work():
            at.AudioQueueStop(self._aq, 1)
        self._started = False

    def close(self) -> None:
        """停止並釋放。重複呼叫無害。

        AudioQueueStop(immediate) 會等正在跑的 callback 跑完——所以呼叫端（recorder）絕不能在
        callback 會用到的鎖裡面呼叫它，否則兩邊互等死結。"""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._closing = True
        try:
            self._dispose()
        finally:
            with _live_lock:
                _live.discard(self)

    def _dispose(self) -> None:
        self._closing = True
        if not self._aq:
            return
        at, _, _ = _load()
        aq, self._aq = self._aq, ctypes.c_void_p()
        with _aq_work():
            try:
                at.AudioQueueStop(aq, 1)
            except Exception:
                pass
            try:
                at.AudioQueueDispose(aq, 1)
            except Exception:
                pass
        self._started = False


# ── CoreAudio 查詢（只讀，不開麥克風）──────────────────────────────────────────────
# 語音突顯時每次查詢 0.05～0.3 秒，所以這些都只能在背景執行緒呼叫。

def _get_data(obj: int, sel: bytes, scope: bytes, ctype):
    ca = _load()[1]
    out, size = ctype(), ctypes.c_uint32(ctypes.sizeof(ctype))
    addr = _Addr(_cc(sel), _cc(scope), 0)
    if ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(out)) != 0:
        raise OSError(f"CoreAudio query {sel!r} failed")
    return out


def _get_string(obj: int, sel: bytes) -> Optional[str]:
    cf = _load()[2]
    try:
        ref = _get_data(obj, sel, b"glob", ctypes.c_void_p)
    except OSError:
        return None
    if not ref.value:
        return None
    buf = ctypes.create_string_buffer(512)
    ok = cf.CFStringGetCString(ref, buf, 512, _UTF8)
    cf.CFRelease(ref)   # 'lnam'／'uid ' 回來的 CFString 歸我們放
    return buf.value.decode("utf-8") if ok else None


def default_input_id() -> int:
    """系統預設麥克風的 AudioDeviceID；沒有預設麥克風就 raise OSError。"""
    dev = _get_data(1, b"dIn ", b"glob", ctypes.c_uint32).value
    if not dev:
        raise OSError("no default input device")
    return dev


def default_input() -> tuple:
    """(AudioDeviceID, 名字)。只有 2 次查詢（語音突顯時約 0.24 秒）。"""
    dev = default_input_id()
    return dev, (_get_string(dev, b"lnam") or "")


def input_devices() -> list:
    """能收音的裝置 → [{"id": UID 字串, "name": 名字}, ...]。

    用 UID 而不是 PortAudio 那種流水編號：UID 在裝置存在期間不變，重新列舉不會位移，
    也是 AudioQueue 指定裝置要的格式。語音突顯時列舉全部約 2.5 秒。"""
    ca = _load()[1]
    addr = _Addr(_cc(b"dev#"), _cc(b"glob"), 0)
    size = ctypes.c_uint32(0)
    if ca.AudioObjectGetPropertyDataSize(1, ctypes.byref(addr), 0, None, ctypes.byref(size)) != 0:
        raise OSError("device list query failed")
    ids = (ctypes.c_uint32 * (size.value // 4))()
    if ca.AudioObjectGetPropertyData(1, ctypes.byref(addr), 0, None, ctypes.byref(size), ids) != 0:
        raise OSError("device list query failed")
    out = []
    for dev in ids:
        a = _Addr(_cc(b"stm#"), _cc(b"inpt"), 0)
        sz = ctypes.c_uint32(0)
        if ca.AudioObjectGetPropertyDataSize(dev, ctypes.byref(a), 0, None, ctypes.byref(sz)) != 0 or sz.value == 0:
            continue   # 沒有輸入串流（純喇叭）
        name, uid = _get_string(dev, b"lnam"), _get_string(dev, b"uid ")
        if name and uid:
            out.append({"id": uid, "name": name})
    return out
