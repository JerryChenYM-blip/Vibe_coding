"""mac_audio_queue.AQInputStream 的 ctypes 管線（v2.33.0）。

用假的 AudioToolbox／CoreFoundation 函式庫驗：呼叫順序、狀態碼處理、callback 資料轉換、關閉後不再放回緩衝區。
全程不碰真的系統呼叫（conftest 把 _load 擋掉了）；唯一例外是最後的「只讀」CoreAudio 查詢，
它只問裝置名字與 UID，不開任何麥克風。
"""

import ctypes
import subprocess
import sys

import numpy as np
import pytest

import mac_audio_queue as maq
from platform_util import IS_MAC


class _FakeAT:
    def __init__(self):
        self.calls = []
        self.fail = {}          # 函式名 -> 要回的非 0 狀態碼
        self.buffers = []       # 保住記憶體，不被 GC

    def _st(self, name):
        self.calls.append(name)
        return self.fail.get(name, 0)

    def AudioQueueNewInput(self, fmt, cb, ud, rl, rlm, flags, aq_ref):
        self.fmt = fmt._obj
        st = self._st("new")
        if st == 0:
            aq_ref._obj.value = 0x1234
        return st

    def AudioQueueSetProperty(self, aq, sel, ptr_ref, size):
        self.prop = (sel, ptr_ref._obj.value, size)
        return self._st("setprop")

    def AudioQueueAllocateBuffer(self, aq, nbytes, buf_ref):
        arr = (ctypes.c_float * (nbytes // 4))()
        buf = maq._AQBuf(nbytes, ctypes.addressof(arr), 0, None, 0, None, 0)
        buf_ref._obj.contents = buf
        self.buffers.append((buf, arr))
        self.nbytes = nbytes
        return self._st("alloc")

    def AudioQueueEnqueueBuffer(self, aq, buf, n, descs):
        return self._st("enqueue")

    def AudioQueueStart(self, aq, t):
        return self._st("start")

    def AudioQueueStop(self, aq, immediate):
        self.stop_immediate = immediate
        return self._st("stop")

    def AudioQueueDispose(self, aq, immediate):
        return self._st("dispose")


class _FakeCF:
    def __init__(self):
        self.released = []

    def CFStringCreateWithCString(self, alloc, data, enc):
        self.created = data
        return 0xBEEF

    def CFRelease(self, ref):
        self.released.append(ref)


@pytest.fixture
def libs(monkeypatch):
    at, cf = _FakeAT(), _FakeCF()
    monkeypatch.setattr(maq, "_load", lambda: (at, None, cf))
    monkeypatch.setattr(maq, "_live", set())
    monkeypatch.setattr(maq, "_busy", 0)
    return at, cf


def _make(uid=None, blocksize=1600, got=None):
    got = got if got is not None else []
    return maq.AQInputStream(16000, 1, blocksize, uid, lambda *a: got.append(a)), got


def test_建立_格式是16k單聲道float32_配8個緩衝區並放進佇列(libs):
    at, _ = libs
    s, _ = _make()
    assert (at.fmt.mSampleRate, at.fmt.mChannelsPerFrame, at.fmt.mBitsPerChannel) == (16000.0, 1, 32)
    assert at.fmt.mFormatFlags == (1 | 8), "float + packed"
    assert at.calls.count("alloc") == 8 and at.calls.count("enqueue") == 8
    assert at.nbytes == 1600 * 4, "每塊 blocksize 個樣本（100 ms）"
    assert "setprop" not in at.calls, "沒指定裝置就用系統預設，不設 CurrentDevice"
    assert s in maq._live


def test_指定UID_設CurrentDevice_並釋放CFString(libs):
    at, cf = libs
    _make(uid="BuiltInMicrophoneDevice")
    assert at.prop[0] == int.from_bytes(b"aqcd", "big") and at.prop[1] == 0xBEEF
    assert cf.created == b"BuiltInMicrophoneDevice"
    assert cf.released == [0xBEEF], "CFStringCreate 出來的要放掉，不然每開一次漏一個"


@pytest.mark.parametrize("failing", ["new", "setprop", "alloc", "enqueue"])
def test_建立時任何非0狀態碼_清乾淨後丟OSError(libs, failing):
    at, _ = libs
    at.fail[failing] = -50
    with pytest.raises(OSError):
        _make(uid="X")
    assert maq._live == set() and maq.busy_count() == 0
    if failing != "new":
        assert "dispose" in at.calls, "已經建好的佇列要釋放，不能漏"


def test_start失敗_丟OSError_訊息含狀態碼(libs):
    at, _ = libs
    s, _ = _make()
    at.fail["start"] = 1768845428
    with pytest.raises(OSError, match="1768845428"):
        s.start()
    assert s.active is False


def test_start成功_active_stop(libs):
    s, _ = _make()
    s.start()
    assert s.active is True
    s.stop()
    assert s.active is False


def test_callback_把緩衝區複製成_n乘1_float32_交給呼叫端_並放回佇列(libs):
    at, _ = libs
    s, got = _make()
    buf, arr = at.buffers[0]
    for i in range(1600):
        arr[i] = 0.25
    buf.mAudioDataByteSize = 1600 * 4
    at.calls.clear()
    s._on_buffer(None, 0x1234, ctypes.pointer(buf), None, 0, None)
    (indata, frames, ti, status), = got
    assert indata.shape == (1600, 1) and indata.dtype == np.float32 and frames == 1600
    assert float(indata[0, 0]) == 0.25 and (ti, status) == (None, None)
    arr[0] = 9.0
    assert float(indata[0, 0]) == 0.25, "要是複製：緩衝區馬上還給系統，之後會被覆寫"
    assert at.calls == ["enqueue"]


def test_空的緩衝區_不呼叫呼叫端_但照樣放回(libs):
    at, _ = libs
    s, got = _make()
    buf, _ = at.buffers[0]
    buf.mAudioDataByteSize = 0
    at.calls.clear()
    s._on_buffer(None, 0x1234, ctypes.pointer(buf), None, 0, None)
    assert got == [] and at.calls == ["enqueue"]


def test_呼叫端的callback丟例外_被吞掉_仍放回(libs):
    """例外跑出 ctypes callback 會讓整個程序閃退。"""
    at, _ = libs

    def boom(*a):
        raise RuntimeError("x")
    s = maq.AQInputStream(16000, 1, 1600, None, boom)
    buf, _ = at.buffers[0]
    buf.mAudioDataByteSize = 400
    at.calls.clear()
    s._on_buffer(None, 0x1234, ctypes.pointer(buf), None, 0, None)
    assert at.calls == ["enqueue"]


def test_關閉後_callback不再交資料_也不再放回(libs):
    at, _ = libs
    s, got = _make()
    s.start()
    s.close()
    buf, _ = at.buffers[0]
    buf.mAudioDataByteSize = 400
    at.calls.clear()
    s._on_buffer(None, 0x1234, ctypes.pointer(buf), None, 0, None)
    assert got == [] and at.calls == [], "關閉中還放回緩衝區，會對已釋放的佇列動手"


def test_callback處理到一半被關閉_不能再放回緩衝區(libs):
    """關閉可以發生在 callback 跑到一半（錄音器在別的執行緒 stop）；已經 Dispose 的佇列不能再被放回緩衝區。"""
    at, _ = libs
    holder = {}

    def closes_stream(*a):
        holder["s"].close()
    s = maq.AQInputStream(16000, 1, 1600, None, closes_stream)
    holder["s"] = s
    buf, _ = at.buffers[0]
    buf.mAudioDataByteSize = 400
    at.calls.clear()
    s._on_buffer(None, 0x1234, ctypes.pointer(buf), None, 0, None)
    assert "enqueue" not in at.calls


def test_close_停止再釋放_立即模式_重複呼叫無害(libs):
    at, _ = libs
    s, _ = _make()
    s.start()
    at.calls.clear()
    s.close()
    s.close()
    assert at.calls == ["stop", "dispose"] and at.stop_immediate == 1
    assert s not in maq._live and s.active is False
    with pytest.raises(OSError):
        s.start()


def test_close_all_關掉所有還開著的(libs):
    a, _ = _make()
    b, _ = _make()
    assert maq.close_all() == 2
    assert maq._live == set()


def test_busy_count_在AudioQueue呼叫期間大於0(libs):
    at, _ = libs
    seen = []
    orig = at.AudioQueueStart
    at.AudioQueueStart = lambda aq, t: seen.append(maq.busy_count()) or orig(aq, t)
    s, _ = _make()
    s.start()
    assert seen == [1] and maq.busy_count() == 0


def test_載入模組時不碰系統函式庫_Windows_import_不會炸():
    code = ("import ctypes.util\n"
            "def boom(name): raise RuntimeError('find_library called: ' + name)\n"
            "ctypes.util.find_library = boom\n"
            "import mac_audio_queue as m\n"
            "print(m._libs is None)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "True", out.stderr


@pytest.mark.skipif(not IS_MAC, reason="只有 macOS 有 CoreAudio")
def test_真的問CoreAudio_只讀_裝置清單有名字也有UID(allow_coreaudio_read):
    """只問名字與 UID，不開麥克風。"""
    devices = maq.input_devices()
    if not devices:
        pytest.skip("這台 Mac 沒有任何輸入裝置")
    assert all(d["id"] and d["name"] for d in devices)
    try:
        dev_id, name = maq.default_input()
    except OSError:
        pytest.skip("沒有預設麥克風")
    assert dev_id and name in {d["name"] for d in devices}


# ── 審查後補（#17）：close_all 個別失敗不影響其他；input_devices 的過濾 ──

def test_close_all_有一條關不掉_其他的照樣關(libs):
    """App 結束時一條失敗就停下來的話，後面的不會關，選單列橘燈一直亮到程序結束。"""
    class _Bad:
        def close(self):
            raise RuntimeError("AudioQueueStop hung")

    class _Good:
        closed = 0

        def close(self):
            self.closed += 1
    bad, good1, good2 = _Bad(), _Good(), _Good()
    maq._live.update({bad, good1, good2})
    assert maq.close_all() == 3
    assert good1.closed == 1 and good2.closed == 1


class _FakeCoreAudio:
    """假 CoreAudio：devs = {AudioDeviceID: {"in": 有沒有輸入串流, "name": 名字或 None, "uid": UID 或 None}}。"""

    def __init__(self, devs):
        self.devs = devs

    def AudioObjectGetPropertyDataSize(self, obj, addr_ref, q, qd, size_ref):
        a = addr_ref._obj
        if obj == 1 and a.sel == maq._cc(b"dev#"):
            size_ref._obj.value = 4 * len(self.devs)
            return 0
        if a.sel == maq._cc(b"stm#"):
            size_ref._obj.value = 16 if self.devs[obj]["in"] else 0
            return 0
        return -1

    def AudioObjectGetPropertyData(self, obj, addr_ref, q, qd, size_ref, out):
        a = addr_ref._obj
        if obj == 1 and a.sel == maq._cc(b"dev#"):
            for i, dev_id in enumerate(self.devs):
                out[i] = dev_id
            return 0
        key = {maq._cc(b"lnam"): "name", maq._cc(b"uid "): "uid"}.get(a.sel)
        value = self.devs[obj][key] if key else None
        if value is None:
            return -1                      # 查不到這個屬性
        out._obj.value = hash((obj, key)) & 0xFFFFFF or 1
        self.strings[out._obj.value] = value
        return 0


class _FakeCFStrings:
    def __init__(self, ca):
        self.ca = ca

    def CFStringGetCString(self, ref, buf, size, enc):
        buf.value = self.ca.strings[ref.value].encode("utf-8")
        return True

    def CFRelease(self, ref):
        pass


def test_input_devices_濾掉沒有輸入串流_缺名字_缺UID的裝置(monkeypatch):
    """喇叭（無輸入串流）不能出現在麥克風選單；沒名字或沒 UID 的裝置無法顯示也無法指定。"""
    ca = _FakeCoreAudio({
        10: {"in": True, "name": "EarPods", "uid": "uid-ear"},
        11: {"in": False, "name": "MacBook Pro的揚聲器", "uid": "uid-spk"},   # 純輸出
        12: {"in": True, "name": None, "uid": "uid-noname"},
        13: {"in": True, "name": "NoUID Mic", "uid": None},
        14: {"in": True, "name": "BlackHole 16ch", "uid": "uid-bh"},
    })
    ca.strings = {}
    monkeypatch.setattr(maq, "_load", lambda: (None, ca, _FakeCFStrings(ca)))
    assert maq.input_devices() == [{"id": "uid-ear", "name": "EarPods"},
                                   {"id": "uid-bh", "name": "BlackHole 16ch"}]
