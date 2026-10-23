"""Mac 錄音改用 AudioQueue 與開 App 先暖機 30 秒（v2.33.0）。

背景（2026-10-08 實測，App 身分、macOS 27、麥克風模式「語音突顯」）：
  PortAudio 一初始化就掃全部裝置，10～15 秒；程序啟動後約 30 秒內每開一次麥克風要 1～5 秒，之後 0.06～0.08 秒。
  所以 Mac 不再載入 sounddevice，改用 AudioQueue 只開一支麥克風，並在程序最早期先開著撐到 30 秒才關。

全部用假的 AudioQueue（conftest 的 mac_env），不碰真的麥克風；conftest 另外把真正的系統呼叫整個擋掉。
"""

import pathlib
import threading
import time

import numpy as np
import pytest

import mac_audio_queue
import recorder as rec
from tests.conftest import wait_for


_MAIN_SRC = pathlib.Path(__file__).resolve().parent.parent / "main.py"


def _warm_live(env, name=None):
    rec.prewarm(name)
    assert wait_for(lambda: rec.warm_state() == "live"), "暖機應該要開好"
    return env.instances[0]


# ── 1. 接手 ──

def test_暖機開好且是同一支_start_直接接手_不開新串流(mac_env):
    warm = _warm_live(mac_env)
    r = rec.AudioRecorder()
    t0 = time.monotonic()
    assert r.start() is True
    assert time.monotonic() - t0 < 0.5
    assert len(mac_env.instances) == 1, "接手就是不碰硬體，不能再開一條"
    assert r._stream is warm and r.is_recording()
    warm.emit()
    assert r.sample_count() == 1600


def test_接手後_暖機串流在錄音開始前送來的聲音不進錄音(mac_env):
    warm = _warm_live(mac_env)
    warm.emit()                      # 還沒按錄音，聲音一直在進來
    warm.emit()
    r = rec.AudioRecorder()
    r.start()
    assert r.sample_count() == 0, "錄音開始之前的聲音不能混進來"
    warm.emit()
    assert r.sample_count() == 1600


# ── 2. 錄完還給暖機 ──

def test_暖機未到期時_stop_不關串流_再start又接手(mac_env):
    warm = _warm_live(mac_env)
    r = rec.AudioRecorder()
    r.start()
    warm.emit()
    audio = r.stop()
    assert len(audio) == 1600
    assert warm.closed is False and rec.warm_state() == "live"
    assert r.start() is True
    assert len(mac_env.instances) == 1 and r._stream is warm


# ── 3. 到期 ──

def test_到期時閒置_關掉(mac_env):
    warm = _warm_live(mac_env)
    rec._expire_warm(rec._warm)
    assert warm.closed is True and rec.warm_state() == "none"


def test_到期時正在錄_stop才關(mac_env):
    warm = _warm_live(mac_env)
    r = rec.AudioRecorder()
    r.start()
    rec._expire_warm(rec._warm)
    assert warm.closed is False, "正在錄音，不能把麥克風從底下抽走"
    assert r.is_recording()
    r.stop()
    assert wait_for(lambda: warm.closed), "stop() 把關閉交給背景，要等一下"


def test_真的等滿時間後自己關掉(mac_env):
    """背景執行緒的等待：hold_s 到了沒在錄就關。"""
    rec.prewarm(None, hold_s=0.3)
    assert wait_for(lambda: rec.warm_state() == "live")
    warm = mac_env.instances[0]
    assert wait_for(lambda: warm.closed, timeout=3)
    assert rec.warm_state() == "none"


def test_到期後再start_走一般流程開新串流(mac_env):
    warm = _warm_live(mac_env)
    rec._expire_warm(rec._warm)
    r = rec.AudioRecorder()
    assert r.start() is True
    assert len(mac_env.instances) == 2 and r._stream is not warm
    r.stop()
    assert r._stream is None
    assert wait_for(lambda: mac_env.instances[1].closed is True), "一般串流錄完就關（橘燈熄）；關閉交給背景，要等一下"


# ── 4. 開啟中 ──

def test_暖機開啟中_start回False_不等待_不開第二條(mac_env):
    mac_env.gate = threading.Event()
    rec.prewarm(None)
    assert rec.warm_state() == "opening"
    assert wait_for(lambda: len(mac_env.instances) == 1)   # 背景那條卡在 start()
    r = rec.AudioRecorder()
    t0 = time.monotonic()
    assert r.start() is False
    assert time.monotonic() - t0 < 0.5, "不能等暖機"
    assert len(mac_env.instances) == 1, "不能再開第二條"
    assert not r.is_recording()
    mac_env.gate.set()
    assert wait_for(lambda: rec.warm_state() == "live")
    assert r.start() is True    # 開好之後就能錄


# ── 5. 作廢（世代號碼）──

def test_開的途中被作廢_開好後自己關掉_不裝上去(mac_env):
    mac_env.gate = threading.Event()
    rec.prewarm(None)
    assert wait_for(lambda: len(mac_env.instances) == 1)
    warm = mac_env.instances[0]
    rec.release_warm()
    assert rec.warm_state() == "none"
    mac_env.gate.set()
    assert wait_for(lambda: warm.closed), "作廢後才開好的串流要自己關掉"
    assert rec._warm.stream is None and rec.warm_state() == "none"
    assert rec.warm_info()["seconds"] is None, "作廢後開好的不能先裝上去再關——中間那一下按錄音會接手到一條將死的串流"


def test_作廢閒置中的暖機_會關掉(mac_env):
    warm = _warm_live(mac_env)
    rec.release_warm()
    assert wait_for(lambda: warm.closed)
    assert rec.warm_state() == "none"


def test_作廢時正在錄_不抽走麥克風_stop才關(mac_env):
    warm = _warm_live(mac_env)
    r = rec.AudioRecorder()
    r.start()
    rec.release_warm()
    time.sleep(0.1)
    assert warm.closed is False and r.is_recording()
    warm.emit()
    assert r.sample_count() == 1600, "作廢不能讓正在進行的錄音收不到聲音"
    r.stop()
    assert wait_for(lambda: warm.closed), "stop() 把關閉交給背景，要等一下"


def test_作廢時正在錄_之後stop關這條很慢_也不能卡住呼叫端(mac_env):
    """第三輪審查實測：錄音中被作廢（裝置變動、設定換麥克風）的暖機串流，stop() 曾在主執行緒當場關、卡 0.8 秒。
    作廢可能發生在開 App 30 秒內，那時關一次要 1～3 秒，不能假設「到期了所以關得快」。"""
    warm = _warm_live(mac_env)
    r = rec.AudioRecorder()
    r.start()
    rec.release_warm()
    real_close = warm.close

    def slow_close():
        time.sleep(0.8)
        real_close()
    warm.close = slow_close
    warm.emit()
    t0 = time.monotonic()
    audio = r.stop()
    assert time.monotonic() - t0 < 0.3, "關硬體的 0.8 秒不能算在 stop() 頭上"
    assert len(audio) == 1600
    assert wait_for(lambda: warm.closed, timeout=3)


# ── 6. key 比對 ──

def test_設定換了麥克風_作廢暖機_用UID開新的(mac_env):
    warm = _warm_live(mac_env)                       # 開的是系統預設，key=None
    assert wait_for(lambda: rec._mac_devices_ready)  # 背景列舉完
    r = rec.AudioRecorder()
    assert r.set_device_by_name("EarPods") is True
    assert r._device_index == "uid-ear"
    assert r.start() is True
    assert r._stream is not warm and r._stream.uid == "uid-ear"
    assert wait_for(lambda: warm.closed), "不是這次要的那支，暖機要關掉"
    assert rec.warm_state() == "none"


def test_暖機是指定的那支_名字一樣就接手(mac_env):
    warm = _warm_live(mac_env, "EarPods")
    assert warm.uid == "uid-ear", "指定了預設以外的麥克風：要用它的 UID 開"
    r = rec.AudioRecorder()
    r.set_device_by_name("EarPods")      # 清單可能還沒填好：只記名字
    assert r.start() is True
    assert r._stream is warm and len(mac_env.instances) == 1
    assert rec._mac_devices_ready and rec.AudioRecorder.list_devices() == mac_env.devices   # 審查 #13


def test_指定的是預設那支_開預設不用列舉也能接手(mac_env):
    warm = _warm_live(mac_env, "MacBook Pro的麥克風")
    assert warm.uid is None, "名字等於預設麥克風：開預設，省掉一次 2.5 秒的列舉"
    r = rec.AudioRecorder()
    r.set_device_by_name("MacBook Pro的麥克風")
    assert r.start() is True and r._stream is warm


def test_指定的麥克風沒插著_暖機開預設_標成給系統預設用的(mac_env):
    mac_env.devices = [d for d in mac_env.devices if d["name"] != "EarPods"]
    warm = _warm_live(mac_env, "EarPods")
    assert warm.uid is None and rec._warm.key is None


def test_暖機失敗_狀態failed_之後按錄音照一般流程(mac_env):
    mac_env.fail_start = {None}
    rec.prewarm(None)
    assert wait_for(lambda: rec.warm_state() == "failed")
    assert "AudioQueueStart" in rec.warm_info()["error"]
    assert mac_env.instances[0].closed is True, "開失敗的串流要關掉，不佔著裝置"
    mac_env.fail_start = set()
    r = rec.AudioRecorder()
    assert r.start() is True and len(mac_env.instances) == 2


# ── 7. 鎖外關硬體 ──

def test_stop_在鎖外關硬體(mac_env):
    """真的 AudioQueueStop 會等 callback 跑完，而 callback 要拿錄音鎖——在鎖內關就死結。"""
    r = rec.AudioRecorder()
    mac_env.lock_probe = r._lock
    assert r.start() is True            # 沒有暖機：一般流程
    r.stop()
    assert wait_for(lambda: len(mac_env.close_lock_free) == 1), "stop() 把關閉交給背景，要等一下"
    assert mac_env.close_lock_free == [True], "關閉時不能握著錄音鎖"


def test_暖機到期後_stop_關串流時也不握鎖(mac_env):
    warm = _warm_live(mac_env)
    r = rec.AudioRecorder()
    mac_env.lock_probe = r._lock
    r.start()
    rec._expire_warm(rec._warm)
    r.stop()
    # stop() 一律把關閉交給背景（v2.33.0 總監終審改的）：要等一下，不能假設當場關完（原本這裡偶發失敗，2026-10-23）
    assert wait_for(lambda: warm.closed), "stop() 把關閉交給背景，要等一下"
    assert mac_env.close_lock_free == [True]


# ── 8. callback 世代 ──

def test_舊一段的callback晚到_不能寫進新一段(mac_env):
    warm = _warm_live(mac_env)
    r = rec.AudioRecorder()
    r.start()
    old_sink = rec._warm.sink            # 第一段錄音的接收者（綁第一段的世代）
    r.stop()
    r.start()                            # 第二段：接手同一條串流
    old_sink(np.ones((1600, 1), dtype=np.float32), 1600, None, None)
    assert r.sample_count() == 0, "第一段的 callback 不能把聲音寫進第二段"
    warm.emit()
    assert r.sample_count() == 1600


def test_一般串流_舊callback晚到也不能寫進新一段(mac_env):
    r = rec.AudioRecorder()
    r.start()
    first = mac_env.instances[0]
    r.stop()
    r.start()
    first.emit()                         # 已經關掉的第一條串流還有一塊在路上
    assert r.sample_count() == 0
    mac_env.instances[1].emit()
    assert r.sample_count() == 1600


class _BumpOnEnter:
    """模擬競爭：callback 通過第一道世代檢查之後、拿到鎖之前，stop()＋start() 剛好跑完。"""

    def __init__(self, recorder):
        self._r, self._real = recorder, recorder._lock

    def __enter__(self):
        self._r._capture_gen += 2
        return self._real.__enter__()

    def __exit__(self, *a):
        return self._real.__exit__(*a)


def test_callback等鎖的空檔世代變了_鎖內雙重檢查要丟掉(mac_env):
    r = rec.AudioRecorder()
    r.start()
    gen = r._capture_gen
    r._lock = _BumpOnEnter(r)
    r._audio_callback(np.ones((1600, 1), dtype=np.float32), 1600, None, None, gen)
    assert r._frames == [], "第一道檢查是鎖外讀的，等鎖的空檔世代可能已經變了"


def test_沒在錄音時進來的聲音一律丟掉(mac_env):
    warm = _warm_live(mac_env)
    r = rec.AudioRecorder()
    r.start(); r.stop()
    warm.emit()
    assert r.sample_count() == 0 and r.get_rms_level() == 0.0


# ── 9. 裝置清單快取 ──

def test_list_devices_是快取_不在呼叫端列舉(mac_env):
    r = rec.AudioRecorder()
    assert r.list_devices() == []
    assert mac_env.list_calls == 0, "主執行緒絕不列舉（語音突顯時 2～4 秒）"
    rec._set_mac_devices(mac_env.devices)
    assert rec.AudioRecorder.list_devices() == mac_env.devices
    assert mac_env.list_calls == 0


def test_set_device_by_name_清單還沒填_記名字_回True(mac_env):
    r = rec.AudioRecorder()
    assert r.set_device_by_name("EarPods") is True
    assert r._device_name == "EarPods" and r._device_index is None
    assert mac_env.list_calls == 0


def test_set_device_by_name_清單有_用UID(mac_env):
    rec._set_mac_devices(mac_env.devices)
    r = rec.AudioRecorder()
    assert r.set_device_by_name("EarPods") is True
    assert (r._device_name, r._device_index) == ("EarPods", "uid-ear")


def test_set_device_by_name_清單填好了但沒有這支_退回系統預設(mac_env):
    rec._set_mac_devices(mac_env.devices)
    r = rec.AudioRecorder()
    r._device_name, r._device_index = "EarPods", "uid-ear"
    assert r.set_device_by_name("AirPods") is False
    assert (r._device_name, r._device_index) == (None, None)


def test_refresh_devices_更新快取而且把啟動初期記的名字對上UID(mac_env):
    r = rec.AudioRecorder()
    r.set_device_by_name("EarPods")      # 清單還沒填
    assert r.refresh_devices() == mac_env.devices
    assert rec.devices_ready() and r._device_index == "uid-ear"


def test_refresh_devices_列舉失敗_回None不炸_快取不動(mac_env):
    # 審查後改：原本回舊快取，沒填過時就是空清單，會被當成「沒有任何麥克風」（見 A5 的測試）
    rec._set_mac_devices(mac_env.devices)
    mac_env.list_error = OSError("coreaudio down")
    assert rec.AudioRecorder().refresh_devices() is None
    assert rec.AudioRecorder.list_devices() == mac_env.devices


def test_refresh_devices_錄音中也能做_不拿錄音鎖(mac_env):
    r = rec.AudioRecorder()
    r.start()
    got = []
    with r._lock:                         # 模擬另一條執行緒握著錄音鎖
        t = threading.Thread(target=lambda: got.append(r.refresh_devices()))
        t.start(); t.join(2)
    assert got == [mac_env.devices], "列舉不需要錄音鎖"


def test_清單還沒填好時按錄音_當場解析名字_UID開(mac_env):
    r = rec.AudioRecorder()
    r.set_device_by_name("EarPods")
    assert r.start() is True
    assert r._stream.uid == "uid-ear"
    assert r._started_with_fallback is False


def test_指定的麥克風當場也找不到_退回系統預設並提示(mac_env):
    r = rec.AudioRecorder()
    r.set_device_by_name("AirPods")
    assert r.start() is True
    assert r._stream.uid is None
    assert r._started_with_fallback is True and r._fallback_label == "系統預設麥克風"


# ── 11. 裝置變動要不要作廢暖機 ──

def test_用預設開的_預設換了_作廢(mac_env):
    warm = _warm_live(mac_env)
    mac_env.default = (300, "AirPods")
    assert rec.release_warm_if_affected(mac_env.devices) is True
    assert wait_for(lambda: warm.closed) and rec.warm_state() == "none"


def test_用預設開的_預設沒換_不作廢(mac_env):
    warm = _warm_live(mac_env)
    assert rec.release_warm_if_affected(mac_env.devices) is False
    assert warm.closed is False and rec.warm_state() == "live"


def test_指定的那支不見了_作廢(mac_env):
    warm = _warm_live(mac_env, "EarPods")
    remaining = [d for d in mac_env.devices if d["name"] != "EarPods"]
    assert rec.release_warm_if_affected(remaining) is True
    assert wait_for(lambda: warm.closed)


def test_指定的那支還在_別的裝置上下線_不作廢(mac_env):
    warm = _warm_live(mac_env, "EarPods")
    devices = mac_env.devices + [{"id": "uid-iphone", "name": "JerryCheniPhone麥克風"}]
    assert rec.release_warm_if_affected(devices) is False
    assert warm.closed is False


def test_問不到預設是誰_當作受影響(mac_env, monkeypatch):
    _warm_live(mac_env)
    monkeypatch.setattr(mac_audio_queue, "default_input_id", lambda: (_ for _ in ()).throw(OSError("x")))
    assert rec.release_warm_if_affected(mac_env.devices) is True, "寧可多開一次，也不能錄錯麥克風"


def test_沒有暖機時裝置變動_什麼都不做(mac_env):
    assert rec.release_warm_if_affected(mac_env.devices) is False


# ── 13. Windows 不變 ──

def test_Windows_start_仍用_sounddevice_不碰AudioQueue(mac_env, monkeypatch):
    monkeypatch.setattr(rec, "IS_MAC", False)
    made = []

    class _S:
        latency = 0.01
        cpu_load = 0.0
        def start(self): pass
        def stop(self): pass
        def close(self): pass
    monkeypatch.setattr(rec.sd, "InputStream", lambda *a, **k: made.append(1) or _S())
    r = rec.AudioRecorder()
    assert r.start() is True
    r.stop()
    assert made == [1] and mac_env.instances == [], "Windows 不能走 AudioQueue"


def test_Windows_暖機不做事(mac_env, monkeypatch):
    monkeypatch.setattr(rec, "IS_MAC", False)
    assert rec.prewarm(None) is False and mac_env.instances == []


def test_Windows_list_devices_走PortAudio快取(mac_env, monkeypatch):
    monkeypatch.setattr(rec, "IS_MAC", False)
    monkeypatch.setattr(rec, "_device_cache", [{"id": 3, "name": "X"}])
    monkeypatch.setattr(rec, "_refresh_device_cache", lambda: None)
    assert rec.AudioRecorder.list_devices() == [{"id": 3, "name": "X"}]


def test_Windows_refresh_devices_就是_refresh_portaudio(mac_env, monkeypatch):
    monkeypatch.setattr(rec, "IS_MAC", False)
    r = rec.AudioRecorder()
    monkeypatch.setattr(rec.AudioRecorder, "refresh_portaudio", lambda self: [{"id": 1, "name": "W"}])
    assert r.refresh_devices() == [{"id": 1, "name": "W"}]
    assert mac_env.list_calls == 0


# ── 14. 結束時的清理 ──

class _Closable:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


def test_結束時有開著的AudioQueue_關掉(monkeypatch):
    a, b = _Closable(), _Closable()
    monkeypatch.setattr(mac_audio_queue, "_live", {a, b})
    monkeypatch.setattr(mac_audio_queue, "_busy", 0)
    exits = []
    monkeypatch.setattr(rec.os, "_exit", lambda code: exits.append(code))
    monkeypatch.setattr(rec, "_pa_busy", 0)
    monkeypatch.setattr(rec, "sd", None)
    rec._on_exit()
    assert (a.closed, b.closed) == (1, 1) and exits == []


def test_結束時背景還卡在AudioQueue裡_直接結束程序_不去關(monkeypatch):
    a = _Closable()
    monkeypatch.setattr(mac_audio_queue, "_live", {a})
    monkeypatch.setattr(mac_audio_queue, "_busy", 1)
    exits = []
    monkeypatch.setattr(rec.os, "_exit", lambda code: exits.append(code))
    monkeypatch.setattr(rec.time, "sleep", lambda s: None)
    monkeypatch.setattr(rec, "_pa_busy", 0)
    monkeypatch.setattr(rec, "sd", None)
    rec._on_exit()
    assert exits == [0]
    assert a.closed == 0, "背景還在 AudioQueue 裡，這時去關只會多一個人撞進去"


# ── 15. main.py 的順序 ──

def test_prewarm_排在_import_gui_之前():
    src = _MAIN_SRC.read_text(encoding="utf-8")
    i_warm = src.index("_recorder_early.prewarm(")
    i_gui = src.index("\nimport gui\n")
    assert i_warm < i_gui, "暖機要在 import gui 之前開始，不然白白晚 1 秒以上"


def test_prewarm_只在直接執行時做_測試_import_main_不會開麥克風():
    src = _MAIN_SRC.read_text(encoding="utf-8")
    block = src[src.index("if IS_MAC and __name__"):src.index("_recorder_early.prewarm(")]
    assert '__name__ == "__main__"' in block


def test_讀不到設定檔時_暖機當系統預設(monkeypatch, tmp_path):
    import main
    monkeypatch.setenv("HOME", str(tmp_path))
    assert main._early_input_device() is None
    (tmp_path / ".whisper_app").mkdir()
    (tmp_path / ".whisper_app" / "config.json").write_text('{"input_device": "EarPods"}', encoding="utf-8")
    assert main._early_input_device() == "EarPods"
    (tmp_path / ".whisper_app" / "config.json").write_text("{壞掉", encoding="utf-8")
    assert main._early_input_device() is None


def test_暖機是進程裡只有一條_重複呼叫不重複開(mac_env):
    _warm_live(mac_env)
    assert rec.prewarm(None) is False
    assert len(mac_env.instances) == 1


def test_暖機時間是30秒(mac_env):
    """30 秒是實測值（2026-10-08）：只撐 15 秒不夠，撐 30 秒之後每次開麥克風 0.06～0.08 秒。"""
    import inspect
    assert rec._WARM_HOLD_S == 30.0
    assert inspect.signature(rec.prewarm).parameters["hold_s"].default == 30.0


# ═══════════════════════════════════════════════════════════════════════════════
#  審查後修正（v2.33.0，Sonnet × 3 審查）：A 節行為修正的測試
# ═══════════════════════════════════════════════════════════════════════════════

# ── A1. 設定的麥克風沒插著就開 App ──

def test_A1_指定的麥克風沒插著_暖機開預設_按錄音直接接手_不再列舉(mac_env):
    """原本：暖機 key=None ≠ 設定的名字 → 判成不符、丟掉暖機、主執行緒再列舉一次 2～4 秒，每按一次都重來。"""
    mac_env.devices = [d for d in mac_env.devices if d["name"] != "EarPods"]
    r = rec.AudioRecorder()
    r.set_device_by_name("EarPods")             # 啟動初期清單還沒填：只記名字
    warm = _warm_live(mac_env, "EarPods")
    assert warm.uid is None and rec._warm.key is None and rec._warm.requested == "EarPods"
    lists_before = mac_env.list_calls           # 暖機背景自己列舉的那 1～2 次不算
    for _ in range(3):                          # 連按三次：每一次都不能再列舉
        assert r.start() is True
        assert r._stream is warm, "那支不在 → 錄系統預設，跟舊版『啟動時找不到就用預設』一致"
        assert r._started_with_fallback is False
        r.stop()
    assert mac_env.list_calls == lists_before, "主執行緒不能為了這件事列舉"
    assert len(mac_env.instances) == 1


def test_A1_暖機替沒插著的那支開了預設_那支後來插上了_作廢(mac_env):
    """要求的那支現在出現了，暖機卻還在錄預設——不作廢的話使用者會一直錄到錯的麥克風。"""
    mac_env.devices = [d for d in mac_env.devices if d["name"] != "EarPods"]
    warm = _warm_live(mac_env, "EarPods")
    assert rec.release_warm_if_affected(mac_env.devices) is False
    assert rec.release_warm_if_affected(
        mac_env.devices + [{"id": "uid-ear", "name": "EarPods"}]) is True
    assert wait_for(lambda: warm.closed)


def test_A1_清單已填好_設定的麥克風不在_按錄音不列舉_直接開預設並清掉名字(mac_env):
    rec._set_mac_devices([d for d in mac_env.devices if d["name"] != "EarPods"])
    r = rec.AudioRecorder()
    r._device_name, r._device_index = "EarPods", None   # 例如清單填好前就記下的名字
    assert r.start() is True
    assert mac_env.list_calls == 0, "清單已填好就代表那支不在，不要再列舉（語音突顯時 2～4 秒）"
    assert mac_env.opened == [None]
    assert r._device_name is None and r._device_index is None, "runtime 退回系統預設，之後不再重來"
    r.stop()
    assert r.start() is True and mac_env.list_calls == 0


def test_A1_清單還沒填好_仍走當場列舉的慢路徑(mac_env):
    """暖機失敗＋清單未填的極少數情況才會走到。"""
    r = rec.AudioRecorder()
    r._device_name = "EarPods"
    assert r.start() is True
    assert mac_env.list_calls == 1 and r._stream.uid == "uid-ear"


def test_A1_wait_devices_等到清單填好才回True_逾時回False(mac_env):
    assert rec.wait_devices(0.05) is False
    threading.Timer(0.1, lambda: rec._set_mac_devices(mac_env.devices)).start()
    t0 = time.monotonic()
    assert rec.wait_devices(3.0) is True
    assert time.monotonic() - t0 < 1.5
    assert rec.wait_devices(0.0) is True      # 已經填好就立刻回


# ── A2. 暖機是「指定的那支剛好是預設」而用預設開的 ──

def test_A2_指定的是預設那支_預設換了_作廢(mac_env):
    warm = _warm_live(mac_env, "MacBook Pro的麥克風")
    assert warm.uid is None and rec._warm.opened_default_id == 147
    mac_env.default = (300, "AirPods")
    assert rec.release_warm_if_affected(mac_env.devices) is True, "沒帶 UID 開的，預設換了錄到的就是別支"
    assert wait_for(lambda: warm.closed)


def test_A2_指定的是預設那支_預設沒換_那支還在_不作廢(mac_env):
    warm = _warm_live(mac_env, "MacBook Pro的麥克風")
    assert rec.release_warm_if_affected(mac_env.devices) is False
    assert warm.closed is False


def test_A2_指定的是預設那支_它從清單消失_作廢(mac_env):
    warm = _warm_live(mac_env, "MacBook Pro的麥克風")
    remaining = [d for d in mac_env.devices if d["name"] != "MacBook Pro的麥克風"]
    assert rec.release_warm_if_affected(remaining) is True


# ── A3. 開失敗的串流不能在錄音鎖內關 ──

def test_A3_開失敗的串流_不在錄音鎖內關(mac_env):
    """真的 AudioQueueStop 會等 callback 跑完，callback 要拿錄音鎖；在鎖內關失敗的串流就是死結。"""
    r = rec.AudioRecorder()
    mac_env.lock_probe = r._lock
    mac_env.fail_start = {None}              # 第一次（系統預設）和補救的『同一支再試』都開失敗
    assert r.start() is True                 # 最後內建麥克風開成功
    failed = [s for s in mac_env.instances if s.uid is None]
    assert len(failed) == 2
    assert wait_for(lambda: all(s.closed for s in failed)), "失敗的串流仍然要關掉"
    assert wait_for(lambda: len(mac_env.close_lock_free) == 2)
    assert mac_env.close_lock_free == [True, True], "關閉時不能握著錄音鎖"
    r.stop()


# ── A5. 列舉失敗不能被當成『沒有麥克風』 ──

def test_A5_Mac_列舉失敗_回None_快取不動(mac_env):
    r = rec.AudioRecorder()
    mac_env.list_error = OSError("coreaudio down")
    assert r.refresh_devices() is None, "回舊快取（可能是空的）會被呼叫端當成權威清單"
    assert rec.devices_ready() is False
    rec._set_mac_devices(mac_env.devices)
    assert r.refresh_devices() is None
    assert rec.AudioRecorder.list_devices() == mac_env.devices


def test_A5_補救流程碰到列舉失敗_照樣往下試(mac_env):
    """refresh_devices 回 None 時，補救流程用手上的快取繼續（改用內建），不能炸。"""
    rec._set_mac_devices(mac_env.devices)
    r = rec.AudioRecorder()
    mac_env.fail_start = {None}
    mac_env.list_error = OSError("coreaudio down")
    assert r.start() is True
    assert r._fallback_label == "內建麥克風" and r._stream.uid == "uid-mbp"


# ═══════════════════════════════════════════════════════════════════════════════
#  審查後補的測試漏洞（B 節）：審查用 mutation 證明原本測不到的防線
# ═══════════════════════════════════════════════════════════════════════════════

# ── #13 暖機為非預設麥克風列舉後要填進快取 ──

def test_B13_暖機為非預設麥克風列舉_結果要填進清單快取(mac_env):
    """列舉已經花了 2.5 秒，結果不填進快取，設定視窗與按錄音又要再付一次。"""
    _warm_live(mac_env, "EarPods")
    assert rec._mac_devices_ready and rec.AudioRecorder.list_devices() == mac_env.devices
    assert mac_env.list_calls == 1, "只列舉這一次（開完之後不再重複列舉）"


# ── #14 30 秒從 prewarm 那一刻算 ──

def test_B14_慢開的暖機_從prewarm那一刻起算_不是從開好那一刻(mac_env):
    """系統的 30 秒初期是從程序啟動算的；從開好那一刻才算會讓橘燈多亮、也不再保證 30 秒時關。"""
    mac_env.gate = threading.Event()
    threading.Timer(0.4, mac_env.gate.set).start()      # 開麥克風花 0.4 秒
    t0 = time.monotonic()
    rec.prewarm(None, hold_s=0.6)
    assert wait_for(lambda: rec.warm_state() == "live")
    warm = mac_env.instances[0]
    assert wait_for(lambda: warm.closed, timeout=3)
    elapsed = time.monotonic() - t0
    assert 0.55 <= elapsed < 0.85, f"應該約在 t0+0.6 秒關閉，實際 {elapsed:.2f}（從開好算會是 1.0）"


# ── #16 release_warm 要 set done／叫醒背景 ──

def test_B16_開啟中被作廢_warm_wait立刻返回(mac_env):
    """不 set done 的話，gui 的 AudioInit 背景執行緒要白等 60 秒才發現暖機已經沒了。"""
    mac_env.gate = threading.Event()
    rec.prewarm(None)
    assert wait_for(lambda: len(mac_env.instances) == 1)
    rec.release_warm()
    t0 = time.monotonic()
    assert rec.warm_wait(5.0) == "none"
    assert time.monotonic() - t0 < 0.5


def test_B16_作廢後_等30秒的背景執行緒立刻收工(mac_env):
    _warm_live(mac_env)
    rec.release_warm()
    assert wait_for(lambda: not any(t.name == "MicWarmup" and t.is_alive() for t in threading.enumerate()),
                    timeout=1.0), "作廢要叫醒等 30 秒的執行緒，不然它白睡到時間到"


# ── #18 連按兩次 start、暖機裡面各步驟的容錯 ──

def test_B18_Mac_連按兩次start_只開一條(mac_env):
    r = rec.AudioRecorder()
    assert r.start() is True
    assert r.start() is True, "第二次是『已經在錄』，照舊回 True"
    assert len(mac_env.instances) == 1, "不能再開第二條（第一條會漏掉、麥克風燈不熄）"
    r.stop()
    assert wait_for(lambda: mac_env.instances[0].closed)


def test_B18_暖機_問預設麥克風失敗_仍然開預設(mac_env, monkeypatch):
    def boom():
        raise OSError("no default input device")
    monkeypatch.setattr(mac_audio_queue, "default_input", boom)
    warm = _warm_live(mac_env)
    assert rec.warm_state() == "live" and warm.uid is None


def test_B18_暖機_列舉失敗_仍然開預設_不算失敗(mac_env):
    mac_env.list_error = OSError("coreaudio busy")
    warm = _warm_live(mac_env, "EarPods")        # 非預設：需要列舉才找得到 UID
    assert rec.warm_state() == "live" and warm.uid is None
    assert rec._warm.requested == "EarPods", "列舉失敗＝找不到，開預設並記下替誰開的"


def test_B18_暖機_開完之後列舉失敗_暖機照常_而且時間到照樣關(mac_env):
    """開完之後的列舉出例外時，背景執行緒不能死掉——死了就沒人在 30 秒時關麥克風，橘燈一直亮。"""
    mac_env.list_error = OSError("coreaudio busy")
    rec.prewarm(None, hold_s=0.3)                # 預設那支：開完才列舉
    assert wait_for(lambda: rec.warm_state() == "live")
    warm = mac_env.instances[0]
    assert rec.wait_devices(0.05) is False
    assert wait_for(lambda: warm.closed, timeout=3), "列舉失敗也要照樣到期關閉"


def test_B18_main_的prewarm包在try_except_Exception裡():
    """暖機起不來絕不能讓 App 開不了。結構測試：prewarm 那一行要在 try 裡、except 要接得住一般例外。"""
    import ast
    tree = ast.parse(_MAIN_SRC.read_text(encoding="utf-8"))
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "prewarm"]
    assert len(calls) == 1
    node, try_node = calls[0], None
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.Try):
            try_node = node
            break
    assert try_node is not None, "prewarm 沒有被 try 包住"
    assert any(isinstance(h.type, ast.Name) and h.type.id == "Exception" for h in try_node.handlers), \
        "except 要接 Exception，不然 RuntimeError／OSError 會讓 App 開不起來"


# ── #12 devices_ready（設定視窗『清單可信了嗎』）──

def test_B12_Mac_devices_ready_沒填是False_填了是True(mac_env):
    assert rec.devices_ready() is False
    rec._set_mac_devices([])
    assert rec.devices_ready() is True, "空清單也算填過：真的沒有任何麥克風"


def test_B12_Windows_devices_ready_看PortAudio載入了沒(mac_env, monkeypatch):
    monkeypatch.setattr(rec, "IS_MAC", False)
    monkeypatch.setattr(rec, "sd", object())
    assert rec.devices_ready() is True
    monkeypatch.setattr(rec, "sd", None)
    assert rec.devices_ready() is False


# ═══════════════════════════════════════════════════════════════════════════════
#  第二輪審查後修正（v2.33.0 第三輪）：A、B 節的測試
# ═══════════════════════════════════════════════════════════════════════════════

# ── A1（#fixes0）：用 requested 接手，只在那支「現在仍不在清單快取」才成立 ──

def test_第三輪A1_暖機開啟中那支插上了_對帳後按錄音要錄到那支_不能錄預設(mac_env):
    """審查 P1：暖機列舉時 EarPods 還沒插 → 開預設並標 requested；開啟中（2～5 秒）EarPods 插上。
    release_warm_if_affected 只看 live，這時回 False；之後對帳設定 UID，若仍用 requested 接手，
    使用者會在 30 秒內一直錄到內建麥克風，沒有提示也沒有日誌。"""
    mac_env.devices = [d for d in mac_env.devices if d["name"] != "EarPods"]
    mac_env.gate = threading.Event()
    rec.prewarm("EarPods")
    assert wait_for(lambda: len(mac_env.instances) == 1)          # 列舉過了，卡在 start()
    assert rec.warm_state() == "opening"
    mac_env.devices = mac_env.devices + [{"id": "uid-ear", "name": "EarPods"}]   # 插上
    r = rec.AudioRecorder()
    r.refresh_devices()
    assert rec.release_warm_if_affected(r.list_devices()) is False   # 還在開啟中，無從判斷
    mac_env.gate.set()
    assert wait_for(lambda: rec.warm_state() == "live")
    assert r.set_device_by_name("EarPods") is True
    assert r.start() is True
    assert r._stream.uid == "uid-ear", "要錄的是 EarPods，不是接手那條系統預設"
    r.stop()


def test_第三輪A1_暖機列舉失敗但那支其實插著_對帳後按錄音要錄到那支(mac_env):
    """審查 P5：暖機自己的列舉丟例外 → 找不到 UID → 開預設並標 requested；
    之後 gui 的背景列舉成功、清單裡有那支。不能因為 requested 相同就接手預設。"""
    mac_env.list_error = OSError("transient")
    rec.prewarm("EarPods")
    assert wait_for(lambda: rec.warm_state() == "live")
    assert rec._warm.requested == "EarPods" and rec._warm.stream.uid is None
    assert wait_for(lambda: mac_env.list_calls >= 2)       # 暖機開完之後的補列舉也失敗了
    mac_env.list_error = None
    r = rec.AudioRecorder()
    r.set_device_by_name("EarPods")
    assert r.refresh_devices() is not None                 # gui 那條背景執行緒的列舉成功
    assert r.start() is True
    assert r._stream.uid == "uid-ear"


def test_第三輪A1_清單沒填好_不冒險接手requested_走一般流程(mac_env):
    """不知道那支現在在不在，就不能假設它不在：寧可走一般流程（會當場列舉），也不默默錄到預設。"""
    mac_env.list_error = OSError("down")
    rec.prewarm("EarPods")
    assert wait_for(lambda: rec.warm_state() == "live")
    assert wait_for(lambda: mac_env.list_calls >= 2)
    assert rec.devices_ready() is False
    warm = rec._warm.stream
    r = rec.AudioRecorder()
    r._device_name = "EarPods"
    assert r.start() is True
    assert r._stream is not warm, "清單沒填好時 requested 不能當作『那支不在』的證據"


def test_第三輪A1_那支仍不在清單_照樣接手(mac_env):
    """對照：原本 A1 的好處要留著——清單填好、那支確實不在，連按都直接接手、不列舉。"""
    mac_env.devices = [d for d in mac_env.devices if d["name"] != "EarPods"]
    warm = _warm_live(mac_env, "EarPods")
    r = rec.AudioRecorder()
    r.set_device_by_name("EarPods")
    assert rec.devices_ready()
    assert r.start() is True and r._stream is warm


def test_第三輪A1_暖機替別支開的預設_不能拿來錄另一支不在的麥克風(mac_env):
    """requested 只認「暖機當初替它開預設的那個名字」：換成另一支同樣不在的，不能接手（審查 A1g）。"""
    mac_env.devices = [d for d in mac_env.devices if d["name"] != "EarPods"]
    warm = _warm_live(mac_env, "EarPods")
    r = rec.AudioRecorder()
    r._device_name, r._device_index = "Foo", None
    assert r.start() is True
    assert r._stream is not warm


# ── A3（#concurrency1 的一半）：stop() 關「非暖機」串流交給背景 ──

def test_第三輪A3_stop關一般串流很慢_不能卡住呼叫端(mac_env):
    """開 App 30 秒內關一次麥克風實測要 1～3 秒；stop() 是主執行緒呼叫的。frames 已在鎖內拿走，不影響回傳的錄音。"""
    r = rec.AudioRecorder()
    assert r.start() is True
    stream = r._stream
    done = []
    real_close = stream.close

    def slow_close():
        time.sleep(0.8)
        real_close()
        done.append(1)
    stream.close = slow_close
    stream.emit()
    t0 = time.monotonic()
    audio = r.stop()
    assert time.monotonic() - t0 < 0.3, "關硬體的 0.8 秒不能算在 stop() 頭上"
    assert len(audio) == 1600, "錄到的聲音照樣完整回傳"
    assert not done
    assert wait_for(lambda: stream.closed, timeout=3), "背景仍然要把它關掉"


def test_第三輪A3_stop背景關串流_也不握錄音鎖(mac_env):
    r = rec.AudioRecorder()
    mac_env.lock_probe = r._lock
    r.start()
    r.stop()
    assert wait_for(lambda: len(mac_env.close_lock_free) == 1)
    assert mac_env.close_lock_free == [True]


def test_第三輪A3_stop背景關_再start不受影響(mac_env):
    r = rec.AudioRecorder()
    r.start()
    first = r._stream
    r.stop()
    assert r.start() is True and r._stream is not first
    assert wait_for(lambda: first.closed)
    r.stop()


# ── B（第三輪）：第二輪審查用 mutation 證明測不到的防線 ──

def test_第三輪B0_暖機開好_warm_wait馬上返回live(mac_env):
    """開好卻沒 set done，gui 的 AudioInit 背景執行緒會白等 60 秒才做清單對帳。"""
    rec.prewarm(None)
    t0 = time.monotonic()
    assert rec.warm_wait(3.0) == "live"
    assert time.monotonic() - t0 < 1.0


def test_第三輪B1_暖機開失敗_warm_wait馬上返回failed(mac_env):
    mac_env.fail_start = {None}
    rec.prewarm(None)
    t0 = time.monotonic()
    assert rec.warm_wait(3.0) == "failed"
    assert time.monotonic() - t0 < 1.0, "失敗當下就知道了，不能讓等的人白等到逾時"


def test_第三輪B2_問不到預設是誰時暖機照開_之後裝置變動當作受影響(mac_env, monkeypatch):
    """default_input() 丟例外 → opened_default_id 與 key 都是 None，無從比對預設有沒有換：寧可作廢多開一次。"""
    def boom():
        raise OSError("no default input device")
    monkeypatch.setattr(mac_audio_queue, "default_input", boom)
    warm = _warm_live(mac_env)
    assert rec._warm.opened_default_id is None and rec._warm.key is None
    assert rec.release_warm_if_affected(mac_env.devices) is True
    assert wait_for(lambda: warm.closed)


def test_第三輪B6_清單沒填好_列舉本身也失敗_記成退回預設_名字不動(mac_env):
    """暖機失敗＋清單未填＋當場列舉丟例外：不確定那支在不在，保留名字（下次再試），但這次錄的是系統預設，要讓使用者知道。"""
    mac_env.list_error = OSError("coreaudio down")
    r = rec.AudioRecorder()
    r._device_name = "EarPods"
    assert r.start() is True
    assert r._stream.uid is None
    assert r._started_with_fallback is True and r._fallback_label == "系統預設麥克風"
    assert r._device_name == "EarPods"
    r.stop()
