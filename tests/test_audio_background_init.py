"""麥克風元件改到背景準備（v2.32.3），Mac 部分在 v2.33.0 改成 AudioQueue＋暖機。

背景（2026-10-06 實測）：sounddevice 一 import 就跑 PortAudio 初始化，向系統查每個音訊裝置的屬性。
macOS 27 上 App 的麥克風模式是「語音突顯」時，App 身分每查一次要 0.05～0.3 秒，import 要 9.8～15.4 秒、
每次插拔重掃要 9.5 秒；改成「標準」只要 0.2 秒 / 0.01 秒。使用者想開語音突顯又要快，所以：
  (a) sounddevice 不在 import 時載入（Windows 沒有這個問題，照舊 import 時載入）；v2.33.0 起 Mac 完全不載入它
  (b) Windows：還沒準備好、或正在重掃時按錄音：提示「準備中」；Mac：只有暖機麥克風「開啟中」才提示
  (c) 插拔麥克風的重掃改到背景；重掃中又有變動，結束後只再掃一次
  (d) 準備好之前的插拔先記著，準備好之後補做
  (e) 設定視窗不再當場重掃；麥克風還沒準備好時，不能把使用者選的麥克風洗掉
  (f) 防呆：準備好時記下花了幾秒與麥克風模式

測試裡 gui.IS_MAC／recorder.IS_MAC 明確指定平台：Windows 的行為要照舊、Mac 走新做法，兩邊都要有測試。
"""

import threading
import time
import types
from unittest.mock import MagicMock

import numpy as np
import pytest

import recorder as rec
from tests.conftest import wait_for


# ── (a) 載入 ──

@pytest.fixture
def fresh_sd(monkeypatch):
    """讓 recorder 回到「還沒載入」的狀態，並換掉真正的載入動作。"""
    calls = []
    monkeypatch.setattr(rec, "sd", None)
    monkeypatch.setattr(rec, "_sd_failed", False)
    fake = types.SimpleNamespace(name="假的 sounddevice")

    def _import():
        calls.append(threading.current_thread().name)
        time.sleep(0.05)
        return fake
    monkeypatch.setattr(rec, "_import_sounddevice", _import)
    return calls, fake


def test_載入後才算準備好(fresh_sd):
    calls, fake = fresh_sd
    assert not rec.sounddevice_ready()
    assert rec.ensure_sounddevice() is True
    assert rec.sounddevice_ready() and rec.sd is fake


def test_同時好幾個地方要載入_只載入一次(fresh_sd):
    calls, _ = fresh_sd
    ts = [threading.Thread(target=rec.ensure_sounddevice) for _ in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(calls) == 1, "PortAudio 初始化很貴（語音突顯時十幾秒），只能做一次"


def test_載入失敗_明確回報而且不重試(fresh_sd, monkeypatch):
    calls, _ = fresh_sd

    def _boom():
        calls.append("x")
        raise OSError("找不到 libportaudio")
    monkeypatch.setattr(rec, "_import_sounddevice", _boom)
    assert rec.ensure_sounddevice() is False
    assert rec.ensure_sounddevice() is False
    assert calls == ["x"]
    assert rec.sounddevice_failed()


# ── App 端的假視窗 ──

class _NoRunThread:
    """記下要在背景跑什麼，但不跑——用來確認「沒有在主執行緒上做」。"""
    started = []

    def __init__(self, target=None, args=(), daemon=None, name=None):
        self.target, self.args = target, args

    def start(self):
        _NoRunThread.started.append(self)


def _win(monkeypatch, *, ready=True, failed=False, mac=False):
    """假視窗。mac=False（預設）= Windows 流程；mac=True = Mac 流程（recorder 是假的，不碰麥克風）。"""
    import gui
    monkeypatch.setattr(gui, "IS_MAC", mac)
    monkeypatch.setattr(rec, "sd", object() if ready else None)
    monkeypatch.setattr(rec, "_sd_failed", failed)
    win = gui.AppWindow.__new__(gui.AppWindow)
    toasts = []
    win.__dict__.update(
        _state="idle", _audio_refreshing=False, _audio_refresh_again=False,
        _audio_change_pending=False, _skeleton_mode=False, _audio_ready=ready and not failed,
        _audio_refresh_started=time.monotonic(), _mini_window=None,
        _show_toast=lambda msg, *a, **k: toasts.append(msg),
        recorder=MagicMock(**{"start.return_value": False, "is_recording.return_value": False,
                              "_device_name": None, "warm_state.return_value": "live"}),
        cfg=types.SimpleNamespace(input_device="MacBook Pro的麥克風", apple_streaming=False,
                                  ollama_enabled=False, auto_paste=True),
        after=lambda ms, fn, *a: fn(*a),
    )
    _NoRunThread.started = []
    monkeypatch.setattr(gui.threading, "Thread", _NoRunThread)
    return gui, win, toasts


# ── (b) 按錄音的擋門 ──

@pytest.mark.parametrize("ready,refreshing", [(False, False), (True, True)])
def test_Windows_準備中按錄音_提示而且不去開麥克風(monkeypatch, ready, refreshing):
    gui, win, toasts = _win(monkeypatch, ready=ready)
    win._audio_refreshing = refreshing
    gui.AppWindow._transition_to_recording(win)
    assert win._state == "idle"
    win.recorder.start.assert_not_called()   # start() 要拿鎖，重掃中會把畫面卡住
    assert toasts and "準備" in toasts[0]


def test_Windows_載入失敗時照舊走麥克風打不開的提示(monkeypatch):
    """不是「準備中」——永遠不會好，要讓使用者知道是麥克風的問題。"""
    gui, win, toasts = _win(monkeypatch, ready=False, failed=True)
    win._status_slot_show_banner = lambda *a, **k: None
    gui.AppWindow._transition_to_recording(win)
    win.recorder.start.assert_called_once()
    assert toasts and "麥克風無法啟動" in toasts[0]


def test_Mac_暖機開啟中按錄音_提示麥克風開啟中_不去開麥克風(monkeypatch):
    gui, win, toasts = _win(monkeypatch, mac=True)
    win.recorder.warm_state.return_value = "opening"
    shown = []
    win._mini_window = types.SimpleNamespace(show_warning=lambda m: shown.append(m))
    gui.AppWindow._transition_to_recording(win)
    assert win._state == "idle"
    win.recorder.start.assert_not_called()
    assert toasts and "麥克風開啟中" in toasts[0]
    assert shown and "開啟中" in shown[0], "使用者多半在別的 App 打字，錄音小窗要一起提示"


@pytest.mark.parametrize("state", ["live", "none", "failed"])
def test_Mac_暖機不在開啟中就不擋(monkeypatch, state):
    gui, win, toasts = _win(monkeypatch, mac=True)
    win.recorder.warm_state.return_value = state
    win._status_slot_show_banner = lambda *a, **k: None
    gui.AppWindow._transition_to_recording(win)
    win.recorder.start.assert_called_once()


def test_Mac_裝置清單重新列舉中也不擋錄音(monkeypatch):
    """列舉只讀系統、不碰錄音鎖，不該讓使用者按了沒反應。"""
    gui, win, toasts = _win(monkeypatch, mac=True)
    win._audio_refreshing = True
    win._status_slot_show_banner = lambda *a, **k: None
    gui.AppWindow._transition_to_recording(win)
    win.recorder.start.assert_called_once()
    assert not any("準備" in t for t in toasts)


# ── (c)(d) 插拔麥克風 ──

@pytest.mark.parametrize("mac", [True, False])
def test_插拔時的重掃不在主執行緒做(monkeypatch, mac):
    gui, win, _ = _win(monkeypatch, mac=mac)
    gui.AppWindow._handle_device_change(win)
    win.recorder.refresh_devices.assert_not_called()
    assert len(_NoRunThread.started) == 1 and win._audio_refreshing


@pytest.mark.parametrize("mac", [True, False])
def test_重掃中又有變動_結束後只再掃一次(monkeypatch, mac):
    gui, win, _ = _win(monkeypatch, mac=mac)
    gui.AppWindow._handle_device_change(win)
    gui.AppWindow._handle_device_change(win)
    gui.AppWindow._handle_device_change(win)
    assert len(_NoRunThread.started) == 1, "重掃中的變動不能疊著一直開新的"
    win.recorder.refresh_devices.return_value = [{"id": 0, "name": "MacBook Pro的麥克風"}]
    t = _NoRunThread.started[0]
    t.target(*t.args)                          # 第一次重掃跑完
    assert len(_NoRunThread.started) == 2, "結束後要補掃一次，才拿得到最新清單"
    assert not win._audio_refresh_again


@pytest.mark.parametrize("mac", [True, False])
def test_重掃完_使用中的麥克風被拔掉就退回系統預設(monkeypatch, mac):
    gui, win, toasts = _win(monkeypatch, mac=mac)
    win.recorder._device_name = "AirPods"
    win.recorder.refresh_devices.return_value = [{"id": 0, "name": "MacBook Pro的麥克風"}]
    gui.AppWindow._handle_device_change(win)
    t = _NoRunThread.started[0]
    t.target(*t.args)
    assert win.recorder._device_name is None
    assert toasts and "AirPods" in toasts[0]
    assert not win._audio_refreshing


def test_Mac_重掃後檢查暖機受不受影響(monkeypatch):
    """裝置變動：暖機用預設開的、預設換了 → 作廢；判斷在背景做（會問系統問題）。"""
    gui, win, _ = _win(monkeypatch, mac=True)
    devices = [{"id": "u1", "name": "MacBook Pro的麥克風"}]
    win.recorder.refresh_devices.return_value = devices
    gui.AppWindow._handle_device_change(win)
    win.recorder.release_warm_if_affected.assert_not_called()
    t = _NoRunThread.started[0]
    t.target(*t.args)
    win.recorder.release_warm_if_affected.assert_called_once_with(devices)


def test_Windows_重掃不碰暖機(monkeypatch):
    gui, win, _ = _win(monkeypatch, mac=False)
    win.recorder.refresh_devices.return_value = []
    gui.AppWindow._handle_device_change(win)
    t = _NoRunThread.started[0]
    t.target(*t.args)
    win.recorder.release_warm_if_affected.assert_not_called()


def test_Windows_還沒準備好時的插拔_先記著(monkeypatch):
    gui, win, _ = _win(monkeypatch, ready=False)
    gui.AppWindow._handle_device_change(win)
    assert _NoRunThread.started == [] and win._audio_change_pending


def test_Mac_一開始就能處理插拔_不用等載入(monkeypatch):
    """Mac 不載 sounddevice，沒有「還沒準備好」這回事（rec.sd 永遠是 None）。"""
    gui, win, _ = _win(monkeypatch, ready=False, mac=True)
    gui.AppWindow._handle_device_change(win)
    assert len(_NoRunThread.started) == 1 and not win._audio_change_pending


def test_準備好之後補做先前的插拔_記下花多久(monkeypatch):
    gui, win, _ = _win(monkeypatch)
    win._audio_change_pending = True
    events = []
    monkeypatch.setattr(gui, "_pipe_event", lambda name, **k: events.append((name, k)))
    gui.AppWindow._on_audio_ready(win, True, 2.4, 2)
    assert len(_NoRunThread.started) == 1, "準備期間的插拔要補掃"
    assert ("audio_ready", {"ok": True, "seconds": 2.4, "mic_mode": 2}) in events


def test_Windows_啟動時在背景準備麥克風(monkeypatch):
    gui, win, _ = _win(monkeypatch, ready=False)
    gui.AppWindow._start_audio(win)
    assert len(_NoRunThread.started) == 1
    win.recorder.set_device_by_name.assert_not_called()
    win.recorder.start_device_monitor.assert_not_called()   # 語音突顯時在主執行緒登記花 13.7 秒


def test_Mac_啟動時只記名字_立刻放行_不在主執行緒做任何系統呼叫(monkeypatch):
    gui, win, _ = _win(monkeypatch, ready=False, mac=True)
    later = []
    win.after = lambda ms, fn, *a: later.append(fn)
    win._audio_ready = False
    gui.AppWindow._start_audio(win)
    win.recorder.set_device_by_name.assert_called_once_with("MacBook Pro的麥克風")
    assert win._audio_ready is True, "不用等載入：暖機沒好時由「開啟中」那道門擋"
    win.recorder.start_device_monitor.assert_not_called()   # 登記監聽留給背景
    assert _NoRunThread.started == [] and later, "背景工作要等主迴圈開始才起跑"
    later[0]()
    assert len(_NoRunThread.started) == 1


def test_Mac_背景先登記監聽_再等暖機結果_記下從啟動算的秒數(monkeypatch):
    gui, win, _ = _win(monkeypatch, ready=False, mac=True)
    order, got = [], []
    win.recorder.start_device_monitor.side_effect = lambda cb: order.append("monitor")
    monkeypatch.setattr(rec, "warm_wait", lambda timeout: order.append("wait") or "live")
    monkeypatch.setattr(rec, "warm_info", lambda: {"state": "live", "seconds": 2.1, "error": ""})
    monkeypatch.setattr(rec, "read_microphone_mode", lambda: 2)
    win.after = lambda ms, fn, *a: got.append(a)
    gui.AppWindow._load_audio_in_background(win)
    assert order == ["monitor", "wait"]
    assert got == [(True, 2.1, 2, "")]


def test_Mac_暖機失敗_回報失敗原因_但不影響之後錄音(monkeypatch):
    gui, win, _ = _win(monkeypatch, ready=False, mac=True)
    got = []
    monkeypatch.setattr(rec, "warm_wait", lambda timeout: "failed")
    monkeypatch.setattr(rec, "warm_info", lambda: {"state": "failed", "seconds": None,
                                                   "error": "OSError: AudioQueueStart failed, status=-50"})
    monkeypatch.setattr(rec, "read_microphone_mode", lambda: 0)
    win.after = lambda ms, fn, *a: got.append(a)
    gui.AppWindow._load_audio_in_background(win)
    assert got[0][0] is False and "AudioQueueStart" in got[0][3]
    win._audio_ready = True
    gui.AppWindow._on_audio_ready(win, *got[0])
    assert win._audio_ready is True
    win._status_slot_show_banner = lambda *a, **k: None
    gui.AppWindow._transition_to_recording(win)
    win.recorder.start.assert_called_once()


def test_Mac_準備好的日誌記下秒數與麥克風模式(monkeypatch):
    gui, win, _ = _win(monkeypatch, mac=True)
    lines = []
    monkeypatch.setattr(gui.log, "info", lambda m, *a, **k: lines.append(m))
    monkeypatch.setattr(gui, "_pipe_event", lambda *a, **k: None)
    gui.AppWindow._on_audio_ready(win, True, 2.1, 2)
    assert any("AUDIO: ready=True in 2.10s since startup" in m and "語音突顯" in m for m in lines)


def test_Windows_背景載入完才登記熱插拔監聽(monkeypatch):
    gui, win, _ = _win(monkeypatch, ready=False)
    monkeypatch.setattr(rec, "ensure_sounddevice", lambda: True)
    monkeypatch.setattr(rec, "read_microphone_mode", lambda: 2)
    win.after = lambda ms, fn, *a: None
    gui.AppWindow._load_audio_in_background(win)
    win.recorder.start_device_monitor.assert_called_once()


def test_Windows_載入失敗就不登記熱插拔監聽(monkeypatch):
    gui, win, _ = _win(monkeypatch, ready=False)
    monkeypatch.setattr(rec, "ensure_sounddevice", lambda: False)
    monkeypatch.setattr(rec, "read_microphone_mode", lambda: None)
    win.after = lambda ms, fn, *a: None
    gui.AppWindow._load_audio_in_background(win)
    win.recorder.start_device_monitor.assert_not_called()


def test_Windows_已經準備好就直接套用設定的麥克風(monkeypatch):
    """Windows：import 時就載入了，維持原本在初始化當下設定麥克風。"""
    gui, win, _ = _win(monkeypatch, ready=True)
    gui.AppWindow._start_audio(win)
    assert _NoRunThread.started == []
    win.recorder.set_device_by_name.assert_called_once_with("MacBook Pro的麥克風")
    win.recorder.start_device_monitor.assert_called_once()


# ── (e) 設定視窗 ──

def test_設定視窗用目前清單():
    import gui
    vals, devs, initial = gui._settings_device_options(
        "MacBook Pro的麥克風", [{"id": 0, "name": "MacBook Pro的麥克風"}], ready=True)
    assert vals == ["（系統預設）", "MacBook Pro的麥克風"] and initial == "MacBook Pro的麥克風"


def test_麥克風還沒準備好_設定視窗保留而且預設選使用者那支():
    """清單是空的：選項只剩「系統預設」、或預設選「系統預設」，使用者一按儲存選好的麥克風就被洗掉。"""
    import gui
    vals, _, initial = gui._settings_device_options("MacBook Pro的麥克風", [], ready=False)
    assert "MacBook Pro的麥克風" in vals and initial == "MacBook Pro的麥克風"


def test_準備好了但設定的麥克風不在_照舊預設系統預設():
    """真的拔掉了就不該假裝它還在。"""
    import gui
    vals, _, initial = gui._settings_device_options(
        "AirPods", [{"id": 0, "name": "MacBook Pro的麥克風"}], ready=True)
    assert "AirPods" not in vals and initial == "（系統預設）"


# ── (f) 麥克風模式 ──

def test_麥克風模式讀不到時回空():
    assert rec.read_microphone_mode(_reader=lambda: (_ for _ in ()).throw(RuntimeError())) is None


def test_真的讀得到麥克風模式而且很快():
    """用 ctypes 直接問系統，不經過 PyObjC 的 loadBundle（那個會握著 GIL 讓畫面頓 1 秒）。"""
    from platform_util import IS_MAC
    if not IS_MAC:
        pytest.skip("只有 macOS 有麥克風模式")
    t0 = time.monotonic()
    mode = rec.read_microphone_mode()
    assert mode in (0, 1, 2) and time.monotonic() - t0 < 1


def test_啟動時的套件檢查不載入錄音元件(monkeypatch):
    """main.check_dependencies 在主執行緒、開視窗之前跑；在這裡載入就等於把十幾秒加回去。"""
    import builtins
    import main
    imported = []
    real_import = builtins.__import__

    def _spy(name, *a, **k):
        imported.append(name)
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", _spy)
    assert main.check_dependencies() == []
    assert "sounddevice" not in imported


def test_Mac_載入錄音模組時不初始化錄音元件():
    """用全新的程序檢查（這個測試程序裡 sounddevice 早就被 conftest 載入了）。
    Windows 照舊在 import 時載入。"""
    import subprocess
    import sys
    from platform_util import IS_MAC
    out = subprocess.run(
        [sys.executable, "-c", "import sys, recorder; print('sounddevice' in sys.modules)"],
        capture_output=True, text=True, timeout=60,
    ).stdout.strip().splitlines()[-1]
    assert out == ("False" if IS_MAC else "True")


def test_Mac_暖機與錄音全程不載入_sounddevice():
    """v2.33.0 的核心：Mac 錄音整條路都不碰 PortAudio（它一初始化就掃全部裝置，語音突顯時 10～15 秒；
    而且 AudioQueue 開著時 PortAudio 再開同一支會收不到聲音，兩套不能並用）。
    全新程序，假的 AudioQueue，跑完 暖機 → 錄音 → 停止，確認 sounddevice 從頭到尾沒被載入。"""
    import subprocess
    import sys
    from platform_util import IS_MAC
    if not IS_MAC:
        pytest.skip("只有 macOS 走 AudioQueue")
    code = r"""
import sys, time, threading
import numpy as np
import mac_audio_queue as m
class AQ:
    def __init__(self, sr, ch, bs, uid, cb): self.cb = cb
    def start(self): pass
    def close(self): pass
m.AQInputStream = AQ
m.default_input = lambda: (1, "Mic")
m.default_input_id = lambda: 1
m.input_devices = lambda: [{"id": "u", "name": "Mic"}]
import recorder
recorder.prewarm(None)
t = time.monotonic()
while recorder.warm_state() != "live" and time.monotonic() - t < 5: time.sleep(0.01)
r = recorder.AudioRecorder()
assert r.start()
r.stop()
recorder.release_warm()
print('sounddevice' in sys.modules)
"""
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip().splitlines()[-1] == "False", out.stderr


# ── 第二輪（審查後）──

def test_Windows_準備中按錄音_錄音小窗也要提示(monkeypatch):
    """使用者多半在別的 App 打字，主視窗的提示看不到；不提示就像熱鍵壞了。"""
    gui, win, toasts = _win(monkeypatch, ready=False)
    shown = []
    win._mini_window = types.SimpleNamespace(show_warning=lambda m: shown.append(m))
    gui.AppWindow._transition_to_recording(win)
    assert shown and "準備" in shown[0]


def test_Windows_元件載好但還沒套用設定的麥克風_也要擋(monkeypatch):
    """中間那一下按熱鍵會用系統預設麥克風錄，不是使用者選的那支。"""
    gui, win, toasts = _win(monkeypatch, ready=True)
    win._audio_ready = False
    gui.AppWindow._transition_to_recording(win)
    win.recorder.start.assert_not_called()


def test_Windows_重掃卡太久_換一個說法(monkeypatch):
    gui, win, toasts = _win(monkeypatch)
    win._audio_refreshing = True
    win._audio_refresh_started = time.monotonic() - 70
    gui.AppWindow._transition_to_recording(win)
    assert "重開" in toasts[0]


def test_錄音中的插拔_回到閒置補做(monkeypatch):
    gui, win, _ = _win(monkeypatch)
    win.recorder.is_recording.return_value = True
    gui.AppWindow._handle_device_change(win)
    assert _NoRunThread.started == [] and win._audio_change_pending
    # 錄完：只測補做這一段（_transition_to_idle 其餘的畫面工作不在這裡驗）
    win.recorder.is_recording.return_value = False
    win._audio_ready = True
    win._audio_change_pending = False
    win._audio_change_pending = True
    gui.AppWindow._replay_pending_device_change(win)
    assert len(_NoRunThread.started) == 1 and not win._audio_change_pending


def test_Windows_背景準備等主迴圈開始才起跑(monkeypatch):
    gui, win, _ = _win(monkeypatch, ready=False)
    later = []
    win.after = lambda ms, fn, *a: later.append(fn)
    gui.AppWindow._start_audio(win)
    assert _NoRunThread.started == [] and later, "要排進主迴圈，不能在 __init__ 裡直接開執行緒"
    later[0]()
    assert len(_NoRunThread.started) == 1


def test_重掃中開設定_保留使用者選的麥克風():
    import gui
    vals, _, initial = gui._settings_device_options("MacBook Pro的麥克風", [], ready=True)
    assert initial == "MacBook Pro的麥克風"


def test_Windows_重掃中查裝置_回上一份清單不等(monkeypatch):
    monkeypatch.setattr(rec, "IS_MAC", False)
    monkeypatch.setattr(rec, "_device_cache", [{"id": 3, "name": "MacBook Pro的麥克風"}])
    assert rec._pa_lock.acquire(blocking=False)
    try:
        t0 = time.monotonic()
        assert rec.AudioRecorder.list_devices() == [{"id": 3, "name": "MacBook Pro的麥克風"}]
        assert time.monotonic() - t0 < 0.1
    finally:
        rec._pa_lock.release()


def test_結束時背景還在用_PortAudio_直接結束程序不拆模組(monkeypatch):
    exits = []
    closed = []
    monkeypatch.setattr(rec, "_pa_busy", 1)
    monkeypatch.setattr(rec, "sd", types.SimpleNamespace(_exit_handler=lambda: closed.append(1)))
    monkeypatch.setattr(rec.os, "_exit", lambda code: exits.append(code))
    monkeypatch.setattr(rec.time, "sleep", lambda s: None)
    rec._on_exit()
    assert exits == [0]
    assert closed == [], "背景還在用時不能去關 PortAudio"


def test_結束時沒人在用_照原本關掉_PortAudio(monkeypatch):
    calls = []
    monkeypatch.setattr(rec, "_pa_busy", 0)
    monkeypatch.setattr(rec, "sd", types.SimpleNamespace(_exit_handler=lambda: calls.append("closed")))
    monkeypatch.setattr(rec.os, "_exit", lambda code: calls.append("exit"))
    rec._on_exit()
    assert calls == ["closed"]


def test_sounddevice_自己的收尾改由我們呼叫(fresh_sd, monkeypatch):
    """它比我們晚註冊、會先跑；那時背景重掃可能正在動 PortAudio。"""
    unregistered = []
    fake = types.SimpleNamespace(_exit_handler=lambda: None, query_devices=lambda: [])
    monkeypatch.setattr(rec, "_import_sounddevice", lambda: fake)
    monkeypatch.setattr(rec.atexit, "unregister", lambda fn: unregistered.append(fn))
    assert rec.ensure_sounddevice()
    assert unregistered == [fake._exit_handler]


def test_只有一個地方會登記熱插拔監聽():
    """結構檢查（視窗初始化要開真的 Tk 視窗，測不到）：語音突顯時這一步在主執行緒會卡 13.7 秒，
    只能在 _start_device_monitor 裡做；它只會從「已載入」與「背景載入」兩條路被呼叫。"""
    import inspect
    import gui
    src = inspect.getsource(gui.AppWindow)
    assert src.count("self.recorder.start_device_monitor(") == 1
    assert "self.recorder.start_device_monitor(" in inspect.getsource(gui.AppWindow._start_device_monitor)
    init_src = inspect.getsource(gui.AppWindow.__init__)
    assert "_start_device_monitor()" not in init_src


def test_關掉之後不准再登記監聽():
    from platform_util import IS_MAC
    if not IS_MAC:
        pytest.skip("只有 macOS 用 CoreAudio 監聽")
    r = rec.AudioRecorder()
    r.stop_device_monitor()
    assert r.start_device_monitor(lambda: None) is False


# ── 設定存檔套用麥克風 ──

def _apply_win(monkeypatch, *, mac=True, device_name=None):
    import gui
    monkeypatch.setattr(gui, "IS_MAC", mac)
    win = gui.AppWindow.__new__(gui.AppWindow)
    calls = {"changes": 0, "released": 0}
    win._handle_device_change = lambda: calls.__setitem__("changes", calls["changes"] + 1)
    r = rec.AudioRecorder()
    r._device_name = device_name
    r.release_warm = lambda: calls.__setitem__("released", calls["released"] + 1)
    win.recorder = r
    return gui, win, r, calls


# （審查後刪除「儲存時選了清單裡沒有的麥克風 → 記名字、背景重新列舉」：改成照舊版只記 warning，
#  見檔案最後 A4 的測試）


def test_Mac_儲存時換了麥克風_作廢暖機(monkeypatch):
    gui, win, r, calls = _apply_win(monkeypatch, device_name="MacBook Pro的麥克風")
    monkeypatch.setattr(rec, "_mac_devices", [{"id": "u1", "name": "MacBook Pro的麥克風"},
                                              {"id": "u2", "name": "EarPods"}])
    monkeypatch.setattr(rec, "_mac_devices_ready", True)
    gui.AppWindow._apply_input_device_setting(win, "EarPods")
    assert calls["released"] == 1 and r._device_index == "u2"


def test_Mac_儲存時麥克風沒換_不作廢暖機(monkeypatch):
    gui, win, r, calls = _apply_win(monkeypatch, device_name="MacBook Pro的麥克風")
    monkeypatch.setattr(rec, "_mac_devices", [{"id": "u1", "name": "MacBook Pro的麥克風"}])
    monkeypatch.setattr(rec, "_mac_devices_ready", True)
    gui.AppWindow._apply_input_device_setting(win, "MacBook Pro的麥克風")
    assert calls["released"] == 0


def test_Mac_改回系統預設_作廢暖機而且舊名字要清掉(monkeypatch):
    gui, win, r, calls = _apply_win(monkeypatch, device_name="MacBook Pro的麥克風")
    r._device_index = "u1"
    gui.AppWindow._apply_input_device_setting(win, None)
    assert r._device_name is None and r._device_index is None and calls["released"] == 1


def test_Windows_改回系統預設_舊名字要清掉(monkeypatch):
    gui, win, r, calls = _apply_win(monkeypatch, mac=False, device_name="MacBook Pro的麥克風")
    r._device_index = 3
    gui.AppWindow._apply_input_device_setting(win, None)
    assert r._device_name is None and r._device_index is None and calls["released"] == 0


def test_設定視窗補上系統現在的麥克風名字():
    import gui
    sw = gui.SettingsWindow.__new__(gui.SettingsWindow)
    sw._device_values = ["（系統預設）", "MacBook Pro的麥克風"]
    got = {}
    sw._device_menu = types.SimpleNamespace(configure=lambda **k: got.update(k))
    gui.SettingsWindow._merge_live_device_names(sw, {"MacBook Pro的麥克風", "AirPods"})
    assert got["values"] == ["（系統預設）", "MacBook Pro的麥克風", "AirPods"]


# ── 設定視窗的音量表 ──

def _settings(monkeypatch, *, mac):
    import gui
    monkeypatch.setattr(gui, "IS_MAC", mac)
    sw = gui.SettingsWindow.__new__(gui.SettingsWindow)
    sw.master = types.SimpleNamespace(_audio_ready=True, _audio_refreshing=False)
    sw._level_stream = None
    sw._level_stream_failed = False
    sw._level_rms = 0.0
    sw._level_lock = threading.Lock()
    sw._level_open_gen = 0
    sw._level_opening = False
    sw._device_var = types.SimpleNamespace(get=lambda: "（系統預設）")
    sw._available_devices = [{"id": "uid-mbp", "name": "MacBook Pro的麥克風"}]
    return gui, sw


def test_Windows_重掃中不開音量表_也不標成壞掉(monkeypatch):
    gui, sw = _settings(monkeypatch, mac=False)
    sw.master._audio_refreshing = True
    gui.SettingsWindow._open_level_stream(sw)
    assert sw._level_stream_failed is False, "重掃完下一輪要能再試"


def test_Mac_音量表在背景開_主執行緒不等(monkeypatch):
    """程序啟動後約 30 秒內開麥克風要 1～5 秒，在主執行緒開就是設定視窗卡住。"""
    gui, sw = _settings(monkeypatch, mac=True)
    monkeypatch.setattr(gui.threading, "Thread", _NoRunThread)
    _NoRunThread.started = []
    gui.SettingsWindow._open_level_stream(sw)
    assert len(_NoRunThread.started) == 1 and sw._level_stream is None and sw._level_opening
    gui.SettingsWindow._open_level_stream(sw)
    assert len(_NoRunThread.started) == 1, "上一次還在開，不能疊著再開"


def _fake_aq(monkeypatch, created, *, fail=False):
    import mac_audio_queue

    class _AQ:
        def __init__(self, sr, ch, bs, uid, cb):
            self.uid, self.cb, self.closed = uid, cb, False
            created.append(self)
            if fail:
                raise OSError("fake")

        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            self.closed = True
    monkeypatch.setattr(mac_audio_queue, "AQInputStream", _AQ)


def test_Mac_音量表用AudioQueue_開好就掛上(monkeypatch):
    gui, sw = _settings(monkeypatch, mac=True)
    sw._device_var = types.SimpleNamespace(get=lambda: "MacBook Pro的麥克風")
    created = []
    _fake_aq(monkeypatch, created)
    gui.SettingsWindow._level_open_worker(sw, "uid-mbp", sw._level_open_gen)
    assert sw._level_stream is created[0] and created[0].uid == "uid-mbp" and not sw._level_opening
    created[0].cb(np.full((1600, 1), 0.5, dtype=np.float32), 1600, None, None)
    assert abs(sw._level_rms - 0.5) < 1e-6


def test_Mac_音量表開到一半設定視窗關了_開好後自己關掉(monkeypatch):
    gui, sw = _settings(monkeypatch, mac=True)
    created = []
    _fake_aq(monkeypatch, created)
    gen = sw._level_open_gen
    gui.SettingsWindow._release_level_stream(sw)       # 視窗關閉／換頁 → 世代 +1
    gui.SettingsWindow._level_open_worker(sw, None, gen)
    assert created[0].closed is True and sw._level_stream is None, "不能裝上去，不然麥克風燈一直亮"
    assert sw._level_opening is False


def test_Mac_音量表開失敗_標成壞掉不重試(monkeypatch):
    gui, sw = _settings(monkeypatch, mac=True)
    created = []
    _fake_aq(monkeypatch, created, fail=True)
    gui.SettingsWindow._level_open_worker(sw, None, sw._level_open_gen)
    assert sw._level_stream is None and sw._level_stream_failed is True and not sw._level_opening


def test_Mac_音量表放掉時關掉串流(monkeypatch):
    gui, sw = _settings(monkeypatch, mac=True)
    created = []
    _fake_aq(monkeypatch, created)
    gui.SettingsWindow._level_open_worker(sw, None, sw._level_open_gen)
    gui.SettingsWindow._release_level_stream(sw)
    assert sw._level_stream is None
    assert wait_for(lambda: created[0].closed), "關串流在背景做（審查 A9），所以要等一下"


def test_Mac_音量表_主App錄音時立刻放掉麥克風_不跟正式錄音搶(monkeypatch):
    """安全鐵律（v2.28.0）保留：兩個 AudioQueue 雖然能同時開，仍維持主錄音優先。"""
    gui, sw = _settings(monkeypatch, mac=True)
    created = []
    _fake_aq(monkeypatch, created)
    gui.SettingsWindow._level_open_worker(sw, None, sw._level_open_gen)
    sw._active_category = "recording"
    sw._parent = types.SimpleNamespace(_state="recording")
    sw._level_canvas = MagicMock()
    sw._level_engine = MagicMock()
    sw._level_last_tick = sw._level_state_start = time.perf_counter()
    sw.winfo_exists = lambda: True
    sw.after = lambda ms, fn: "tick"
    monkeypatch.setattr(gui, "_draw_spectral_bars", lambda *a, **k: None)
    gui.SettingsWindow._level_tick(sw)
    assert sw._level_stream is None and wait_for(lambda: created[0].closed)


# ── 關閉 App ──

def _closing_win(monkeypatch, *, mac):
    gui, win, _ = _win(monkeypatch, mac=mac)
    win.hotkey_mgr = MagicMock()
    win._abort_apple_session = lambda: None
    win._destroy_mini_window = lambda: None
    win.recorder.is_recording.return_value = True
    return gui, win


def test_Mac_關閉視窗時錄完再作廢暖機_橘燈才會熄(monkeypatch):
    gui, win = _closing_win(monkeypatch, mac=True)
    gui.AppWindow.on_close(win)
    win.recorder.release_warm.assert_called_once()
    names = [c[0] for c in win.recorder.mock_calls]
    assert names.index("stop") < names.index("release_warm"), "先停錄音再作廢暖機（正在錄的不能被抽走）"


def test_Windows_關閉視窗不碰暖機(monkeypatch):
    gui, win = _closing_win(monkeypatch, mac=False)
    gui.AppWindow.on_close(win)
    win.recorder.release_warm.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
#  審查後修正（v2.33.0，Sonnet × 3 審查）：A 節行為修正的測試
# ═══════════════════════════════════════════════════════════════════════════════

def _bg_win(monkeypatch, *, state, devices_arrive):
    """跑 _load_audio_in_background（Mac）：warm_wait 回 state；wait_devices 回 devices_arrive。回傳 (win, 記錄)。"""
    gui, win, _ = _win(monkeypatch, ready=False, mac=True)
    seen = {"wait_devices": [], "warm_wait": [], "ready_args": []}
    monkeypatch.setattr(rec, "warm_wait", lambda timeout: seen["warm_wait"].append(timeout) or state)
    monkeypatch.setattr(rec, "warm_info", lambda: {"state": state, "seconds": 2.1 if state == "live" else None,
                                                   "error": "OSError: x" if state == "failed" else ""})
    monkeypatch.setattr(rec, "read_microphone_mode", lambda: 2)
    monkeypatch.setattr(rec, "wait_devices", lambda t: seen["wait_devices"].append(t) or devices_arrive,
                        raising=False)
    win.after = lambda ms, fn, *a: seen["ready_args"].append(a)
    gui.AppWindow._load_audio_in_background(win)
    return gui, win, seen


# ── A1：清單快取第一次填好後要對帳 ──

def test_A1_Mac_暖機開好後等清單填好_等得到就不自己列舉(monkeypatch):
    _, win, seen = _bg_win(monkeypatch, state="live", devices_arrive=True)
    assert len(seen["wait_devices"]) == 1 and 0 < seen["wait_devices"][0] <= 10, "等待要在 10 秒以內"
    win.recorder.refresh_devices.assert_not_called()
    assert seen["ready_args"], "對帳要在回主執行緒的 _on_audio_ready 做"


def test_A1_Mac_等不到清單_在這條背景執行緒自己列舉(monkeypatch):
    _, win, seen = _bg_win(monkeypatch, state="live", devices_arrive=False)
    win.recorder.refresh_devices.assert_called_once()
    assert seen["ready_args"]


@pytest.mark.parametrize("state", ["failed", "none"])
def test_A1_Mac_暖機失敗或被作廢_不白等_直接列舉(monkeypatch, state):
    """暖機失敗／被作廢時背景執行緒不會去填清單，等它只是白等。"""
    _, win, seen = _bg_win(monkeypatch, state=state, devices_arrive=False)
    assert seen["wait_devices"] == []
    win.recorder.refresh_devices.assert_called_once()


def test_A1_Mac_準備好時對帳_清單沒有設定的那支_清掉名字_不跳提示(monkeypatch, mac_env):
    gui, win, toasts = _win(monkeypatch, mac=True)
    win.recorder = rec.AudioRecorder()
    win.cfg.input_device = "USB Mic"
    win.recorder.set_device_by_name("USB Mic")        # 啟動當下清單還沒填：只記名字
    assert win.recorder._device_name == "USB Mic"
    rec._set_mac_devices(mac_env.devices)              # 之後清單填好了，裡面沒有 USB Mic
    monkeypatch.setattr(gui, "_pipe_event", lambda *a, **k: None)
    gui.AppWindow._on_audio_ready(win, True, 2.0, 2)
    assert win.recorder._device_name is None and win.recorder._device_index is None
    assert toasts == [], "跟舊版啟動時找不到的行為一致：只記 warning，不跳提示"


def test_A1_Mac_準備好時對帳_清單有設定的那支_對上UID(monkeypatch, mac_env):
    gui, win, toasts = _win(monkeypatch, mac=True)
    win.recorder = rec.AudioRecorder()
    win.cfg.input_device = "EarPods"
    win.recorder.set_device_by_name("EarPods")
    rec._set_mac_devices(mac_env.devices)
    monkeypatch.setattr(gui, "_pipe_event", lambda *a, **k: None)
    gui.AppWindow._on_audio_ready(win, True, 2.0, 2)
    assert (win.recorder._device_name, win.recorder._device_index) == ("EarPods", "uid-ear")


def test_A1_Mac_暖機失敗時也要對帳(monkeypatch, mac_env):
    """失敗的路徑照樣把清單填好；對帳不能只在 ok=True 時做。"""
    gui, win, toasts = _win(monkeypatch, mac=True)
    win.recorder = rec.AudioRecorder()
    win.cfg.input_device = "USB Mic"
    win.recorder.set_device_by_name("USB Mic")
    rec._set_mac_devices(mac_env.devices)
    monkeypatch.setattr(gui, "_pipe_event", lambda *a, **k: None)
    gui.AppWindow._on_audio_ready(win, False, 0.0, 2, "OSError: x")
    assert win.recorder._device_name is None


# ── A4：設定存檔 ──

def test_A4_Mac_沒改麥克風只改別的設定_不作廢暖機_不列舉_不跳提示(monkeypatch, mac_env):
    """設定的麥克風沒插著、runtime 早已退回系統預設（_device_name=None）：存檔不能拿它當『換了麥克風』。"""
    gui, win, r, calls = _apply_win(monkeypatch)
    toasts = []
    win._show_toast = lambda m, *a, **k: toasts.append(m)
    win.cfg = types.SimpleNamespace(input_device="EarPods")
    rec._set_mac_devices([d for d in mac_env.devices if d["name"] != "EarPods"])
    gui.AppWindow._apply_startup_device(win)          # 啟動：找不到 → 退回系統預設
    assert r._device_name is None
    gui.AppWindow._apply_input_device_setting(win, "EarPods")   # 之後存檔，麥克風欄位沒動
    assert calls["released"] == 0, "沒換麥克風就不該作廢暖機（下次按會變成 1～5 秒的冷開）"
    assert calls["changes"] == 0 and toasts == []


def test_A4_Mac_連續存檔兩次同一支麥克風_第二次不作廢暖機(monkeypatch, mac_env):
    """『上一次套用的設定』每次存檔都要更新，不能永遠拿啟動時的值比。"""
    gui, win, r, calls = _apply_win(monkeypatch)
    win.cfg = types.SimpleNamespace(input_device=None)
    rec._set_mac_devices(mac_env.devices)
    gui.AppWindow._apply_startup_device(win)                    # 啟動：系統預設
    gui.AppWindow._apply_input_device_setting(win, "EarPods")   # 第一次存檔：換成 EarPods
    assert calls["released"] == 1
    gui.AppWindow._apply_input_device_setting(win, "EarPods")   # 第二次存檔：沒動
    assert calls["released"] == 1, "第二次沒換麥克風，不該再作廢暖機"


def test_A4_Mac_換成清單裡沒有的麥克風_照舊版只記warning_不列舉不跳提示(monkeypatch, mac_env):
    gui, win, r, calls = _apply_win(monkeypatch, device_name="MacBook Pro的麥克風")
    win._applied_input_device = "MacBook Pro的麥克風"
    rec._set_mac_devices(mac_env.devices)
    gui.AppWindow._apply_input_device_setting(win, "AirPods")
    assert calls["released"] == 1, "換了麥克風 → 暖機作廢"
    assert calls["changes"] == 0, "清單會在每次裝置變動時更新，存檔不再特別重掃"
    assert r._device_name is None and r._device_index is None


def test_A4_Mac_清單還沒填好_存檔只記名字_等對帳(monkeypatch):
    gui, win, r, calls = _apply_win(monkeypatch)
    win._applied_input_device = None
    gui.AppWindow._apply_input_device_setting(win, "EarPods")
    assert r._device_name == "EarPods" and calls["changes"] == 0


# ── A5：列舉失敗 ──

def test_A5_Mac_重掃失敗_不去作廢暖機_也不套用清單(monkeypatch):
    gui, win, toasts = _win(monkeypatch, mac=True)
    win.recorder._device_name = "EarPods"
    win.recorder.refresh_devices.return_value = None
    gui.AppWindow._handle_device_change(win)
    t = _NoRunThread.started[0]
    t.target(*t.args)
    win.recorder.release_warm_if_affected.assert_not_called()
    assert win.recorder._device_name == "EarPods" and toasts == []
    assert not win._audio_refreshing


def test_A5_Mac_第一次列舉就失敗_使用者選的麥克風不會被當成已移除(monkeypatch, mac_env):
    gui, win, toasts = _win(monkeypatch, mac=True)
    win.recorder = rec.AudioRecorder()
    win.recorder.set_device_by_name("EarPods")
    mac_env.list_error = OSError("transient")
    gui.AppWindow._handle_device_change(win)
    t = _NoRunThread.started[0]
    t.target(*t.args)
    assert win.recorder._device_name == "EarPods" and toasts == []


# ── A6：_on_audio_ready 先套用設定的麥克風才放行 ──

def test_A6_Windows_準備好時先套用設定的麥克風_才放行(monkeypatch):
    gui, win, _ = _win(monkeypatch, ready=False)
    win._audio_ready = False
    ready_when_applied = []
    win.recorder.set_device_by_name.side_effect = lambda n: ready_when_applied.append(win._audio_ready) or True
    monkeypatch.setattr(gui, "_pipe_event", lambda *a, **k: None)
    gui.AppWindow._on_audio_ready(win, True, 1.0, 2)
    win.recorder.set_device_by_name.assert_called_once_with("MacBook Pro的麥克風")
    assert ready_when_applied == [False], "放行之前就要套用，不然這段空檔按錄音會錄到系統預設"
    assert win._audio_ready is True


def test_A6_Windows_載入失敗_不套用也不放行(monkeypatch):
    gui, win, _ = _win(monkeypatch, ready=False)
    win._audio_ready = False
    monkeypatch.setattr(gui, "_pipe_event", lambda *a, **k: None)
    gui.AppWindow._on_audio_ready(win, False, 0.0, 2)
    win.recorder.set_device_by_name.assert_not_called()
    assert win._audio_ready is False


def test_A6_Mac_準備好時也套用設定的麥克風(monkeypatch):
    gui, win, _ = _win(monkeypatch, mac=True)
    monkeypatch.setattr(gui, "_pipe_event", lambda *a, **k: None)
    gui.AppWindow._on_audio_ready(win, True, 2.0, 2)
    win.recorder.set_device_by_name.assert_called_once_with("MacBook Pro的麥克風")


# ── A8：暖機 60 秒還在開，日誌不能寫成準備好了 ──

def test_A8_Mac_等了60秒還在開_回報不是準備好_並記warning(monkeypatch):
    gui, win, _ = _win(monkeypatch, mac=True)
    warnings = []
    monkeypatch.setattr(gui.log, "warning", lambda m, *a, **k: warnings.append(m))
    _, win, seen = _bg_win(monkeypatch, state="opening", devices_arrive=False)
    (ok, seconds, mic_mode, detail), = seen["ready_args"]
    assert ok is False, "還在開 ≠ 準備好；不能記成 ready=True in 0.00s"
    assert "60" in detail
    assert any("60" in m and "麥克風" in m for m in warnings), "要留一行 warning 讓看日誌的人知道卡住了"
    assert seen["warm_wait"] == [60.0]


@pytest.mark.parametrize("state,expect_ok", [("none", True), ("live", True), ("failed", False), ("opening", False)])
def test_A8_Mac_四種暖機狀態_各自的ok判斷(monkeypatch, state, expect_ok):
    _, win, seen = _bg_win(monkeypatch, state=state, devices_arrive=True)
    assert seen["warm_wait"] == [60.0]
    assert seen["ready_args"][0][0] is expect_ok


def test_A8_Mac_擋門行為不變_仍是開啟中就提示(monkeypatch):
    gui, win, toasts = _win(monkeypatch, mac=True)
    win.recorder.warm_state.return_value = "opening"
    gui.AppWindow._transition_to_recording(win)
    win.recorder.start.assert_not_called()
    assert toasts and "開啟中" in toasts[0]


# ── A9：設定的音量表不在主執行緒關串流 ──

class _SlowCloseStream:
    def __init__(self):
        self.closed_in = None
        self.stopped_in = None

    def stop(self):
        self.stopped_in = threading.current_thread()

    def close(self):
        time.sleep(0.05)
        self.closed_in = threading.current_thread()


def test_A9_Mac_音量表放掉_關串流交給背景(monkeypatch):
    """開 App 30 秒內關一次實測 1～3 秒，在主執行緒關會卡設定視窗。"""
    gui, sw = _settings(monkeypatch, mac=True)
    s = _SlowCloseStream()
    sw._level_stream = s
    t0 = time.monotonic()
    gui.SettingsWindow._release_level_stream(sw)
    assert sw._level_stream is None
    assert time.monotonic() - t0 < 0.04, "呼叫端不能等串流關完"
    assert wait_for(lambda: s.closed_in is not None)
    assert s.closed_in is not threading.main_thread() and s.closed_in is not threading.current_thread()


def test_A9_Windows_音量表放掉_照舊當場關(monkeypatch):
    gui, sw = _settings(monkeypatch, mac=False)
    s = _SlowCloseStream()
    sw._level_stream = s
    gui.SettingsWindow._release_level_stream(sw)
    assert s.closed_in is threading.current_thread()


# ═══════════════════════════════════════════════════════════════════════════════
#  審查後補的測試漏洞（B 節）
# ═══════════════════════════════════════════════════════════════════════════════

# ── #10 主執行緒不能等暖機 ──

def test_B10_Mac_暖機還在開_start_audio立刻返回_不呼叫warm_wait(monkeypatch):
    """目標 1：視窗出現就能按。主執行緒若等暖機（2～30 秒），視窗會跟著凍住。"""
    gui, win, _ = _win(monkeypatch, ready=False, mac=True)
    w = rec._WarmState()
    w.state = "opening"
    w.done.clear()                                    # 真的還在開：warm_wait 會一直等
    monkeypatch.setattr(rec, "_warm", w)
    waited = []
    monkeypatch.setattr(rec, "warm_wait", lambda t: waited.append(t) or (_ for _ in ()).throw(
        AssertionError("主執行緒不能等暖機")))
    win.after = lambda ms, fn, *a: None
    win._audio_ready = False
    t0 = time.monotonic()
    gui.AppWindow._start_audio(win)
    assert time.monotonic() - t0 < 0.5 and waited == []
    assert win._audio_ready is True


# ── #11 音量表 _level_opening 的重設 ──

def test_B11_Mac_音量表開好後_opening旗標要重設(monkeypatch):
    """旗標沒重設的話 _open_level_stream_mac 會永遠 early return，音量表再也開不起來。"""
    gui, sw = _settings(monkeypatch, mac=True)
    created = []
    _fake_aq(monkeypatch, created)
    sw._level_opening = True                          # 真實流程：_open_level_stream_mac 開頭設的
    gui.SettingsWindow._level_open_worker(sw, None, sw._level_open_gen)
    assert sw._level_opening is False and sw._level_stream is created[0]


def test_B11_Mac_音量表開失敗_opening旗標也要重設(monkeypatch):
    gui, sw = _settings(monkeypatch, mac=True)
    _fake_aq(monkeypatch, [], fail=True)
    sw._level_opening = True
    gui.SettingsWindow._level_open_worker(sw, None, sw._level_open_gen)
    assert sw._level_opening is False


def test_B11_Mac_音量表開到一半被作廢_opening旗標也要重設(monkeypatch):
    gui, sw = _settings(monkeypatch, mac=True)
    created = []
    _fake_aq(monkeypatch, created)
    sw._level_opening = True
    gen = sw._level_open_gen
    sw._level_open_gen += 1
    gui.SettingsWindow._level_open_worker(sw, None, gen)
    assert sw._level_opening is False and created[0].closed


def test_B11_Mac_放掉之後下一輪能再開一條(monkeypatch):
    """走完整條路：_open_level_stream → 背景開好 → 放掉 → 再 _open_level_stream 要真的起新的背景執行緒。"""
    gui, sw = _settings(monkeypatch, mac=True)
    created = []
    _fake_aq(monkeypatch, created)
    monkeypatch.setattr(gui.threading, "Thread", _NoRunThread)
    _NoRunThread.started = []
    gui.SettingsWindow._open_level_stream(sw)
    t = _NoRunThread.started[0]
    t.target(*t.args)                                  # 背景開好
    assert sw._level_stream is created[0] and sw._level_opening is False
    gui.SettingsWindow._release_level_stream(sw)
    assert sw._level_stream is None
    gui.SettingsWindow._open_level_stream(sw)
    assert len([x for x in _NoRunThread.started if x.target == sw._level_open_worker]) == 2


# ── #12 設定視窗的 ready 判斷（Mac 看 devices_ready）──

def test_B12_設定視窗_Mac_快取沒填_保留使用者那支(monkeypatch):
    import gui
    monkeypatch.setattr(gui, "IS_MAC", True)
    monkeypatch.setattr(rec, "_mac_devices_ready", False)
    monkeypatch.setattr(rec, "_mac_devices", [])
    assert gui._settings_devices_ready(types.SimpleNamespace()) is False
    values, _, initial = gui._settings_device_options("EarPods", [], gui._settings_devices_ready(None))
    assert "EarPods" in values and initial == "EarPods", "不然一按儲存就被改成系統預設"


def test_B12_設定視窗_Mac_快取填好了_ready變True(monkeypatch):
    import gui
    monkeypatch.setattr(gui, "IS_MAC", True)
    monkeypatch.setattr(rec, "_mac_devices_ready", False)
    assert gui._settings_devices_ready(None) is False
    monkeypatch.setattr(rec, "_mac_devices_ready", True)
    assert gui._settings_devices_ready(None) is True


def test_B12_設定視窗_Windows_ready看元件與重掃(monkeypatch):
    import gui
    monkeypatch.setattr(gui, "IS_MAC", False)
    monkeypatch.setattr(rec, "_mac_devices_ready", True)    # 不能被 Mac 的旗標影響
    app = types.SimpleNamespace(_audio_ready=True, _audio_refreshing=False)
    assert gui._settings_devices_ready(app) is True
    app._audio_refreshing = True
    assert gui._settings_devices_ready(app) is False
    app._audio_refreshing, app._audio_ready = False, False
    assert gui._settings_devices_ready(app) is False


# ── #15 音量表用選單選的那支 ──

def test_B15_Mac_音量表用選單選的那支麥克風(monkeypatch):
    gui, sw = _settings(monkeypatch, mac=True)
    sw._available_devices = [{"id": "uid-mbp", "name": "MacBook Pro的麥克風"}, {"id": "uid-ear", "name": "EarPods"}]
    sw._device_var = types.SimpleNamespace(get=lambda: "EarPods")
    monkeypatch.setattr(gui.threading, "Thread", _NoRunThread)
    _NoRunThread.started = []
    gui.SettingsWindow._open_level_stream(sw)
    assert _NoRunThread.started[0].args[0] == "uid-ear", "否則設定畫面永遠顯示系統預設那支的音量"


def test_B15_Mac_選系統預設_UID是None(monkeypatch):
    gui, sw = _settings(monkeypatch, mac=True)
    monkeypatch.setattr(gui.threading, "Thread", _NoRunThread)
    _NoRunThread.started = []
    gui.SettingsWindow._open_level_stream(sw)
    assert _NoRunThread.started[0].args[0] is None


# ═══════════════════════════════════════════════════════════════════════════════
#  第三輪 A2（#concurrency0、#fixes1）：設定的麥克風被一次錯誤判斷就永久忘掉
# ═══════════════════════════════════════════════════════════════════════════════

def _restore_win(monkeypatch, mac_env, *, cfg_device="EarPods", mac=True):
    gui, win, toasts = _win(monkeypatch, mac=mac)
    win.recorder = rec.AudioRecorder()
    win.cfg = types.SimpleNamespace(input_device=cfg_device)
    actions = []
    monkeypatch.setattr(gui, "log_action", lambda *a, **k: actions.append((a, k)))
    win._actions = actions
    return gui, win, toasts


def _plug_in_ear(mac_env):
    """EarPods 插上：系統現在有它，快取要等背景列舉才更新（由測試自己呼叫 refresh_devices）。"""
    mac_env.devices = mac_env.devices + [{"id": "uid-ear", "name": "EarPods"}]


def test_第三輪A2_按錄音剛好在插上與清單更新之間_清單更新後恢復設定的麥克風(monkeypatch, mac_env):
    gui, win, toasts = _restore_win(monkeypatch, mac_env)
    mac_env.devices = [d for d in mac_env.devices if d["name"] != "EarPods"]
    r = win.recorder
    rec._set_mac_devices(mac_env.devices)
    r.set_device_by_name("EarPods")            # 清單沒有它 → 退回系統預設
    assert r._device_name is None
    _plug_in_ear(mac_env)
    assert r.start() is True and r._stream.uid is None   # 按錄音：快取還沒更新，這一次錄預設
    r.stop()
    assert r._device_name is None, "按錄音的判斷把名字清掉了"
    devices = r.refresh_devices()                         # 背景列舉完，清單裡有 EarPods 了
    gui.AppWindow._apply_device_list(win, devices)
    assert (r._device_name, r._device_index) == ("EarPods", "uid-ear"), "設定沒變，插著就要用回來"
    assert toasts == [], "靜默恢復，不跳提示"
    assert any(a[0][0] == "mic_device_restored" and a[1].get("device") == "EarPods" for a in win._actions)
    assert win.cfg.input_device == "EarPods", "恢復只動 runtime，使用者的設定不能被改掉"


def test_第三輪A2_設定存檔剛好在清單更新前_清單更新後恢復(monkeypatch, mac_env):
    gui, win, toasts = _restore_win(monkeypatch, mac_env)
    r = win.recorder
    mac_env.devices = [d for d in mac_env.devices if d["name"] != "EarPods"]
    rec._set_mac_devices(mac_env.devices)
    win._applied_input_device = None
    gui.AppWindow._apply_input_device_setting(win, "EarPods")   # 使用者剛插上、剛選、剛存；快取還是舊的
    assert r._device_name is None
    _plug_in_ear(mac_env)
    gui.AppWindow._apply_device_list(win, r.refresh_devices())
    assert (r._device_name, r._device_index) == ("EarPods", "uid-ear")


def test_第三輪A2_被拔掉又插回來_恢復用回原本設定的麥克風(monkeypatch, mac_env):
    gui, win, toasts = _restore_win(monkeypatch, mac_env)
    r = win.recorder
    rec._set_mac_devices(mac_env.devices)
    r.set_device_by_name("EarPods")
    assert r._device_name == "EarPods"
    gui.AppWindow._apply_device_list(win, [d for d in mac_env.devices if d["name"] != "EarPods"])
    assert r._device_name is None and toasts and "EarPods" in toasts[0]    # 拔掉：退回預設並提示
    toasts.clear()
    gui.AppWindow._apply_device_list(win, mac_env.devices)                  # 插回來
    assert (r._device_name, r._device_index) == ("EarPods", "uid-ear")
    assert toasts == []
    assert win.cfg.input_device == "EarPods"


def test_第三輪A2_設定的那支還是不在_不亂恢復(monkeypatch, mac_env):
    gui, win, toasts = _restore_win(monkeypatch, mac_env)
    r = win.recorder
    gui.AppWindow._apply_device_list(win, [d for d in mac_env.devices if d["name"] != "EarPods"])
    assert r._device_name is None
    assert not any(a[0][0] == "mic_device_restored" for a in win._actions)


def test_第三輪A2_設定是系統預設_不恢復任何東西(monkeypatch, mac_env):
    gui, win, toasts = _restore_win(monkeypatch, mac_env, cfg_device=None)
    gui.AppWindow._apply_device_list(win, mac_env.devices)
    assert win.recorder._device_name is None


def test_第三輪A2_現在已經有用中的麥克風_不被設定值覆蓋(monkeypatch, mac_env):
    """runtime 正在用別支（例如剛好另一條路徑設了）時，清單更新不去改它。"""
    gui, win, toasts = _restore_win(monkeypatch, mac_env, cfg_device="EarPods")
    r = win.recorder
    rec._set_mac_devices(mac_env.devices)
    r.set_device_by_name("BlackHole 16ch")
    gui.AppWindow._apply_device_list(win, mac_env.devices)
    assert r._device_name == "BlackHole 16ch"


def test_第三輪A2_Windows_不做恢復(monkeypatch, mac_env):
    gui, win, toasts = _restore_win(monkeypatch, mac_env, mac=False)
    win.recorder = MagicMock(**{"_device_name": None})
    gui.AppWindow._apply_device_list(win, mac_env.devices)
    win.recorder.set_device_by_name.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
#  第三輪 B：第二輪審查補的測試漏洞
# ═══════════════════════════════════════════════════════════════════════════════

def test_第三輪B3_設定視窗建麥克風選單時真的呼叫_settings_devices_ready():
    """B12 的測試只測了小工具本身；呼叫端被改回舊判斷時，Mac 啟動初期（快取還空）會把使用者選的麥克風
    洗成系統預設。結構測試：SettingsWindow 裡有呼叫它，而且結果有傳給 _settings_device_options。"""
    import ast
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "gui.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SettingsWindow")
    ready_vars = set()
    for node in ast.walk(cls):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "_settings_devices_ready"):
            # 傳進去的要是主視窗（_app）：Windows 靠它讀「載好了沒、在不在重掃」，傳 None 會把重掃中的舊清單當真
            args = node.value.args
            assert args and isinstance(args[0], ast.Name) and args[0].id == "_app", \
                "_settings_devices_ready 要拿到主視窗"
            ready_vars.update(t.id for t in node.targets if isinstance(t, ast.Name))
    assert ready_vars, "SettingsWindow 沒有呼叫 _settings_devices_ready"
    passed = [c for c in ast.walk(cls) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
              and c.func.id == "_settings_device_options"
              and any(isinstance(a, ast.Name) and a.id in ready_vars for a in c.args)]
    assert passed, "_settings_devices_ready 的結果沒有傳給 _settings_device_options"


def test_第三輪B4_Windows_音量表不走Mac的路_也不開LevelMeterOpen執行緒(monkeypatch):
    """Windows 上走到 AudioQueue 路徑會 import mac_audio_queue 並呼叫 ctypes，失敗後音量表被靜默標成壞掉。"""
    gui, sw = _settings(monkeypatch, mac=False)
    monkeypatch.setattr(rec, "sd", object())
    monkeypatch.setattr(rec, "_sd_failed", False)
    called = []
    sw._open_level_stream_mac = lambda: called.append(1)
    monkeypatch.setattr(gui.threading, "Thread", _NoRunThread)
    _NoRunThread.started = []
    # 讓 Windows 分支提早在 import sounddevice 失敗（測試不能真的開麥克風）：只要確認它沒有走 Mac 路徑
    monkeypatch.setitem(__import__("sys").modules, "sounddevice", None)
    gui.SettingsWindow._open_level_stream(sw)
    assert called == [], "Windows 不能走 _open_level_stream_mac"
    assert not any(getattr(t, "target", None) == getattr(sw, "_level_open_worker", object())
                   for t in _NoRunThread.started), "Windows 不能開 AudioQueue 的背景執行緒"
    assert _NoRunThread.started == []


def test_第三輪B7_Windows_warm_state是opening也不擋錄音(monkeypatch):
    """Windows 的 prewarm 是空操作，狀態永遠是 none；但萬一狀態不對，擋門也只該是 Mac 的行為。"""
    gui, win, toasts = _win(monkeypatch, mac=False)
    win.recorder.warm_state.return_value = "opening"
    win._status_slot_show_banner = lambda *a, **k: None
    gui.AppWindow._transition_to_recording(win)
    win.recorder.start.assert_called_once()
    assert not any("麥克風開啟中" in t for t in toasts)


def test_第三輪B5_測試期間真的sounddevice開串流會當場失敗_不會開到麥克風():
    """conftest 的第二道封鎖：沒注入假 sd 的測試一碰到 PortAudio 串流就丟例外。"""
    import sounddevice
    # 先確認是替身才呼叫：封鎖萬一失效，這個測試本身不能變成真的去開麥克風的那一個
    for name in ("InputStream", "RawInputStream", "Stream", "RawStream"):
        assert getattr(sounddevice, name).__name__ == "_BlockedStream", name
    with pytest.raises(OSError, match="must not open the real microphone"):
        sounddevice.InputStream(samplerate=16000, channels=1)
    with pytest.raises(OSError, match="must not open the real microphone"):
        sounddevice.RawInputStream(samplerate=16000, channels=1)


def test_第三輪B5_測試自己注入的假sd不受封鎖影響(monkeypatch):
    made = []

    class _Fake:
        def __init__(self, **k):
            made.append(k)
    monkeypatch.setattr(rec, "sd", types.SimpleNamespace(InputStream=_Fake))
    rec.sd.InputStream(samplerate=1)
    assert made == [{"samplerate": 1}]
