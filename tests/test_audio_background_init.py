"""麥克風元件改到背景準備（v2.32.3）。

背景（2026-10-06 實測）：sounddevice 一 import 就跑 PortAudio 初始化，向系統查每個音訊裝置的屬性。
macOS 27 上 App 的麥克風模式是「語音突顯」時，App 身分每查一次要 0.05～0.3 秒，import 要 9.8～15.4 秒、
每次插拔重掃要 9.5 秒；改成「標準」只要 0.2 秒 / 0.01 秒。使用者想開語音突顯又要快，所以：
  (a) Mac：sounddevice 不在 import 時載入，視窗出現後在背景載入；Windows 照舊
  (b) 還沒準備好、或正在重掃時按錄音：提示「準備中」，畫面不能卡住、不能去開麥克風
  (c) 插拔麥克風的重掃改到背景；重掃中又有變動，結束後只再掃一次
  (d) 準備好之前的插拔先記著，準備好之後補做
  (e) 設定視窗不再當場重掃；麥克風還沒準備好時，不能把使用者選的麥克風洗掉
  (f) 防呆：準備好時記下花了幾秒與麥克風模式
"""

import threading
import time
import types
from unittest.mock import MagicMock

import pytest

import recorder as rec


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


def _win(monkeypatch, *, ready=True, failed=False):
    import gui
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
                              "_device_name": None}),
        cfg=types.SimpleNamespace(input_device="MacBook Pro的麥克風", apple_streaming=False,
                                  ollama_enabled=False, auto_paste=True),
        after=lambda ms, fn, *a: fn(*a),
    )
    _NoRunThread.started = []
    monkeypatch.setattr(gui.threading, "Thread", _NoRunThread)
    return gui, win, toasts


# ── (b) 還沒準備好／重掃中按錄音 ──

@pytest.mark.parametrize("ready,refreshing", [(False, False), (True, True)])
def test_準備中按錄音_提示而且不去開麥克風(monkeypatch, ready, refreshing):
    gui, win, toasts = _win(monkeypatch, ready=ready)
    win._audio_refreshing = refreshing
    gui.AppWindow._transition_to_recording(win)
    assert win._state == "idle"
    win.recorder.start.assert_not_called()   # start() 要拿鎖，重掃中會把畫面卡住
    assert toasts and "準備" in toasts[0]


def test_載入失敗時照舊走麥克風打不開的提示(monkeypatch):
    """不是「準備中」——永遠不會好，要讓使用者知道是麥克風的問題。"""
    gui, win, toasts = _win(monkeypatch, ready=False, failed=True)
    win._status_slot_show_banner = lambda *a, **k: None
    gui.AppWindow._transition_to_recording(win)
    win.recorder.start.assert_called_once()
    assert toasts and "麥克風無法啟動" in toasts[0]


# ── (c)(d) 插拔麥克風 ──

def test_插拔時的重掃不在主執行緒做(monkeypatch):
    gui, win, _ = _win(monkeypatch)
    gui.AppWindow._handle_device_change(win)
    win.recorder.refresh_portaudio.assert_not_called()
    assert len(_NoRunThread.started) == 1 and win._audio_refreshing


def test_重掃中又有變動_結束後只再掃一次(monkeypatch):
    gui, win, _ = _win(monkeypatch)
    gui.AppWindow._handle_device_change(win)
    gui.AppWindow._handle_device_change(win)
    gui.AppWindow._handle_device_change(win)
    assert len(_NoRunThread.started) == 1, "重掃中的變動不能疊著一直開新的"
    win.recorder.refresh_portaudio.return_value = [{"id": 0, "name": "MacBook Pro的麥克風"}]
    t = _NoRunThread.started[0]
    t.target(*t.args)                          # 第一次重掃跑完
    assert len(_NoRunThread.started) == 2, "結束後要補掃一次，才拿得到最新清單"
    assert not win._audio_refresh_again


def test_重掃完_使用中的麥克風被拔掉就退回系統預設(monkeypatch):
    gui, win, toasts = _win(monkeypatch)
    win.recorder._device_name = "AirPods"
    win.recorder.refresh_portaudio.return_value = [{"id": 0, "name": "MacBook Pro的麥克風"}]
    gui.AppWindow._handle_device_change(win)
    t = _NoRunThread.started[0]
    t.target(*t.args)
    assert win.recorder._device_name is None
    assert toasts and "AirPods" in toasts[0]
    assert not win._audio_refreshing


def test_還沒準備好時的插拔_先記著(monkeypatch):
    gui, win, _ = _win(monkeypatch, ready=False)
    gui.AppWindow._handle_device_change(win)
    assert _NoRunThread.started == [] and win._audio_change_pending


def test_準備好之後套用設定的麥克風_補做先前的插拔_記下花多久(monkeypatch):
    gui, win, _ = _win(monkeypatch)
    win._audio_change_pending = True
    events = []
    monkeypatch.setattr(gui, "_pipe_event", lambda name, **k: events.append((name, k)))
    gui.AppWindow._on_audio_ready(win, True, 12.3, 2)
    win.recorder.set_device_by_name.assert_called_once_with("MacBook Pro的麥克風")
    assert len(_NoRunThread.started) == 1, "準備期間的插拔要補掃"
    assert ("audio_ready", {"ok": True, "seconds": 12.3, "mic_mode": 2, "settle_s": 0.0}) in events


def test_啟動時在背景準備麥克風(monkeypatch):
    gui, win, _ = _win(monkeypatch, ready=False)
    gui.AppWindow._start_audio(win)
    assert len(_NoRunThread.started) == 1
    win.recorder.set_device_by_name.assert_not_called()
    win.recorder.start_device_monitor.assert_not_called()   # 語音突顯時在主執行緒登記花 13.7 秒


def test_背景載入完才登記熱插拔監聽(monkeypatch):
    gui, win, _ = _win(monkeypatch, ready=False)
    monkeypatch.setattr(rec, "ensure_sounddevice", lambda: True)
    monkeypatch.setattr(rec, "read_microphone_mode", lambda: 2)
    win.after = lambda ms, fn, *a: None
    gui.AppWindow._load_audio_in_background(win)
    win.recorder.start_device_monitor.assert_called_once()


def test_載入失敗就不登記熱插拔監聽(monkeypatch):
    gui, win, _ = _win(monkeypatch, ready=False)
    monkeypatch.setattr(rec, "ensure_sounddevice", lambda: False)
    monkeypatch.setattr(rec, "read_microphone_mode", lambda: None)
    win.after = lambda ms, fn, *a: None
    gui.AppWindow._load_audio_in_background(win)
    win.recorder.start_device_monitor.assert_not_called()


def test_已經準備好就直接套用設定的麥克風(monkeypatch):
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



# ── 第二輪（審查後）──

def test_準備中按錄音_錄音小窗也要提示(monkeypatch):
    """使用者多半在別的 App 打字，主視窗的提示看不到；不提示就像熱鍵壞了。"""
    gui, win, toasts = _win(monkeypatch, ready=False)
    shown = []
    win._mini_window = types.SimpleNamespace(show_warning=lambda m: shown.append(m))
    gui.AppWindow._transition_to_recording(win)
    assert shown and "準備" in shown[0]


def test_元件載好但還沒套用設定的麥克風_也要擋(monkeypatch):
    """中間那一下按熱鍵會用系統預設麥克風錄，不是使用者選的那支。"""
    gui, win, toasts = _win(monkeypatch, ready=True)
    win._audio_ready = False
    gui.AppWindow._transition_to_recording(win)
    win.recorder.start.assert_not_called()


def test_重掃卡太久_換一個說法(monkeypatch):
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


def test_背景準備等主迴圈開始才起跑(monkeypatch):
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


def test_重掃中查裝置_回上一份清單不等(monkeypatch):
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



# ── 系統調整期（語音突顯時掃完裝置後約 17 秒，開麥克風要 3～7 秒）──

def _settle(seq, **kw):
    from platform_util import IS_MAC
    if not IS_MAC:
        pytest.skip("只有 macOS 需要等")
    it = iter(seq)
    calls = []
    waited = rec.wait_until_settled(_probe=lambda: calls.append(1) or next(it), _sleep=lambda s: None, **kw)
    return len(calls), waited


def test_系統調整好之前一直等_連續兩次快才算好():
    n, _ = _settle([150, 90, 3, 200, 2, 1, 1])
    assert n == 6, "中間又慢一次要重新算"


def test_標準模式第一次就快_幾乎不用等():
    n, waited = _settle([1, 1])
    assert n == 2 and waited < 0.1


def test_連續問不到才放棄():
    """可能根本沒有麥克風；但也可能預設麥克風剛被拔、系統還在換，第一次問不到不能就放行。"""
    n, _ = _settle([None, None, None, 1, 1])
    assert n == 3


def test_偶爾問不到_之後問得到就照常等():
    n, _ = _settle([None, 150, 1, 1])
    assert n == 4


def test_一直慢_等到上限就放行():
    from platform_util import IS_MAC
    if not IS_MAC:
        pytest.skip("只有 macOS 需要等")
    clock = [0.0]
    monkey_time = types.SimpleNamespace(perf_counter=lambda: clock[0])
    import recorder
    orig = recorder.time
    recorder.time = monkey_time
    try:
        def slow():
            clock[0] += 1.0
            return 150
        waited = recorder.wait_until_settled(timeout=5, _probe=slow, _sleep=lambda s: None)
    finally:
        recorder.time = orig
    assert waited == 5.0


def test_背景載入完要等系統調整好才回報準備好(monkeypatch):
    gui, win, _ = _win(monkeypatch, ready=False)
    order = []
    monkeypatch.setattr(rec, "ensure_sounddevice", lambda: order.append("load") or True)
    monkeypatch.setattr(rec, "wait_until_settled", lambda: order.append("settle") or 17.0)
    monkeypatch.setattr(rec, "read_microphone_mode", lambda: 2)
    win.recorder.snapshot_coreaudio.side_effect = lambda: order.append("snapshot")
    win.recorder.device_change_affects_us.return_value = False
    win.recorder.portaudio_view_consistent.return_value = True
    win.after = lambda ms, fn, *a: order.append(("ready", a))
    gui.AppWindow._load_audio_in_background(win)
    assert order[:3] == ["load", "snapshot", "settle"], "掃完馬上記狀態，不能等調整好再記"
    assert order[-1][0] == "ready" and order[-1][1][-2:] == (17.0, False)


def test_重掃後也要等系統調整好才放行(monkeypatch):
    gui, win, _ = _win(monkeypatch)
    order = []
    win.recorder.refresh_portaudio.side_effect = lambda: order.append("refresh") or []
    monkeypatch.setattr(rec, "wait_until_settled", lambda: order.append("settle") or 17.0)
    gui.AppWindow._handle_device_change(win)
    t = _NoRunThread.started[0]
    t.target(*t.args)
    assert order == ["refresh", "settle"] and not win._audio_refreshing


def test_只有一個地方會登記熱插拔監聽():
    """結構檢查（視窗初始化要開真的 Tk 視窗，測不到）：語音突顯時這一步在主執行緒會卡 13.7 秒，
    只能在 _start_device_monitor 裡做；它只會從「已載入」與「背景載入完」兩條路被呼叫。"""
    import inspect
    import gui
    src = inspect.getsource(gui.AppWindow)
    assert src.count("self.recorder.start_device_monitor(") == 1
    assert "self.recorder.start_device_monitor(" in inspect.getsource(gui.AppWindow._start_device_monitor)
    init_src = inspect.getsource(gui.AppWindow.__init__)
    assert "_start_device_monitor()" not in init_src


# ── 只有影響到正在用的麥克風才重掃 ──

def _snap_rec(monkeypatch, device_name, *, alive=True, current=None, default_now=147):
    monkeypatch.setattr(rec, "IS_MAC", True)
    r = rec.AudioRecorder()
    r._device_name = device_name
    r._ca_snap = {"default": 147, "ids": {"MacBook Pro的麥克風": 147, "JerryCheniPhone麥克風": 183}}
    monkeypatch.setattr(rec, "_ca_alive", lambda dev: alive)
    monkeypatch.setattr(rec, "_ca_input_devices", lambda: current if current is not None else {})
    monkeypatch.setattr(rec, "_ca_default_input", lambda: default_now)
    return r


def test_指定的麥克風還在_不用重掃(monkeypatch):
    """iPhone 麥克風自己上下線這種，跟正在用的那支無關。"""
    assert _snap_rec(monkeypatch, "MacBook Pro的麥克風", alive=True).device_change_affects_us() is False


def test_指定的麥克風被拔掉_要重掃(monkeypatch):
    assert _snap_rec(monkeypatch, "MacBook Pro的麥克風", alive=False).device_change_affects_us() is True


def test_指定的麥克風上次不在_這次插回來_要重掃(monkeypatch):
    r = _snap_rec(monkeypatch, "AirPods", current={"AirPods": 300})
    assert r.device_change_affects_us() is True


def test_指定的麥克風上次不在_這次也不在_不用重掃(monkeypatch):
    r = _snap_rec(monkeypatch, "AirPods", current={"MacBook Pro的麥克風": 147})
    assert r.device_change_affects_us() is False


@pytest.mark.parametrize("default_now,期望", [(147, False), (300, True)])
def test_用系統預設時_只看預設有沒有換(monkeypatch, default_now, 期望):
    assert _snap_rec(monkeypatch, None, default_now=default_now).device_change_affects_us() is 期望


def test_沒有記下狀態或問不到_照舊重掃(monkeypatch):
    r = _snap_rec(monkeypatch, None)
    r._ca_snap = None
    assert r.device_change_affects_us() is True
    r = _snap_rec(monkeypatch, None)
    monkeypatch.setattr(rec, "_ca_default_input", lambda: (_ for _ in ()).throw(OSError("問不到")))
    assert r.device_change_affects_us() is True, "不能因為省這一次而錄錯麥克風"


def test_Windows_一律照舊重掃(monkeypatch):
    r = _snap_rec(monkeypatch, "MacBook Pro的麥克風")
    monkeypatch.setattr(rec, "IS_MAC", False)
    assert r.device_change_affects_us() is True


def test_不受影響就不重掃而且很快放行(monkeypatch):
    gui, win, _ = _win(monkeypatch)
    win._devices_dirty = False
    win.recorder.device_change_affects_us.return_value = False
    settled = []
    monkeypatch.setattr(rec, "wait_until_settled", lambda: settled.append(1) or 0.05)
    gui.AppWindow._handle_device_change(win)
    t = _NoRunThread.started[0]
    t.target(*t.args)
    win.recorder.refresh_portaudio.assert_not_called()
    assert not win._audio_refreshing and win._devices_dirty
    assert settled == [1], "裝置上線時系統可能又在調整，不重掃也要等它好"


def test_受影響才重掃_而且重新記下狀態(monkeypatch):
    gui, win, _ = _win(monkeypatch)
    win._devices_dirty = True
    win.recorder.device_change_affects_us.return_value = True
    win.recorder.refresh_portaudio.return_value = [{"id": 0, "name": "MacBook Pro的麥克風"}]
    monkeypatch.setattr(rec, "wait_until_settled", lambda: 0.0)
    gui.AppWindow._handle_device_change(win)
    t = _NoRunThread.started[0]
    t.target(*t.args)
    win.recorder.refresh_portaudio.assert_called_once()
    win.recorder.snapshot_coreaudio.assert_called_once()
    assert not win._devices_dirty


def test_強制重掃不先判斷(monkeypatch):
    """設定視窗要最新清單：不管有沒有影響到正在用的那支都要重掃。"""
    gui, win, _ = _win(monkeypatch)
    win.recorder.device_change_affects_us.return_value = False
    monkeypatch.setattr(rec, "wait_until_settled", lambda: 0.0)
    gui.AppWindow._handle_device_change(win, force=True)
    t = _NoRunThread.started[0]
    t.target(*t.args)
    win.recorder.refresh_portaudio.assert_called_once()


def test_重掃中又被要求強制重掃_結束後強制補一次(monkeypatch):
    gui, win, _ = _win(monkeypatch)
    win.recorder.device_change_affects_us.return_value = False
    gui.AppWindow._handle_device_change(win)              # 一般的（會被判定不影響）
    gui.AppWindow._handle_device_change(win, force=True)  # 進行中又來一個強制的
    assert win._audio_refresh_again == "force"
    monkeypatch.setattr(rec, "wait_until_settled", lambda: 0.0)
    t = _NoRunThread.started[0]
    t.target(*t.args)                                     # 第一個跑完（跳過重掃）
    t2 = _NoRunThread.started[1]
    t2.target(*t2.args)                                   # 補的那個要真的重掃
    win.recorder.refresh_portaudio.assert_called_once()


def test_背景載入完記下狀態(monkeypatch):
    gui, win, _ = _win(monkeypatch, ready=False)
    monkeypatch.setattr(rec, "ensure_sounddevice", lambda: True)
    monkeypatch.setattr(rec, "wait_until_settled", lambda: 0.0)
    monkeypatch.setattr(rec, "read_microphone_mode", lambda: 0)
    win.after = lambda ms, fn, *a: None
    gui.AppWindow._load_audio_in_background(win)
    win.recorder.snapshot_coreaudio.assert_called_once()



# ── 第二輪審查（只重掃受影響）後修正 ──

def test_啟動後清單已過時_直接強制重掃而且只核對一層(monkeypatch):
    gui, win, _ = _win(monkeypatch)
    calls = []
    win._handle_device_change = lambda force=False, recheck=True: calls.append((force, recheck))
    monkeypatch.setattr(gui, "_pipe_event", lambda *a, **k: None)
    gui.AppWindow._on_audio_ready(win, True, 12.0, 2, 17.0, True)
    assert calls == [(True, False)]


def test_重掃後清單已過時_再強制掃一次(monkeypatch):
    gui, win, _ = _win(monkeypatch)
    monkeypatch.setattr(rec, "wait_until_settled", lambda: 0.0)
    win.recorder.refresh_portaudio.return_value = []
    win.recorder.portaudio_view_consistent.return_value = False
    gui.AppWindow._handle_device_change(win, force=True)
    t = _NoRunThread.started[0]
    t.target(*t.args)
    assert len(_NoRunThread.started) == 2, "對不上要再掃"
    t2 = _NoRunThread.started[1]
    win.recorder.portaudio_view_consistent.reset_mock()
    t2.target(*t2.args)
    win.recorder.portaudio_view_consistent.assert_not_called()
    assert len(_NoRunThread.started) == 2, "第二次不再核對，免得對不上的機器一直重掃"


def test_重掃時掃完馬上記狀態_再等調整(monkeypatch):
    gui, win, _ = _win(monkeypatch)
    order = []
    win.recorder.refresh_portaudio.side_effect = lambda: order.append("refresh") or []
    win.recorder.snapshot_coreaudio.side_effect = lambda: order.append("snapshot")
    win.recorder.portaudio_view_consistent.return_value = True
    monkeypatch.setattr(rec, "wait_until_settled", lambda: order.append("settle") or 0.0)
    gui.AppWindow._handle_device_change(win, force=True)
    t = _NoRunThread.started[0]
    t.target(*t.args)
    assert order == ["refresh", "snapshot", "settle"]


@pytest.mark.parametrize("name,pa,ca,pa_default,ca_default,期望", [
    ("MacBook Pro的麥克風", {"MacBook Pro的麥克風"}, {"MacBook Pro的麥克風": 147}, None, None, True),
    ("AirPods", {"MacBook Pro的麥克風"}, {"AirPods": 300, "MacBook Pro的麥克風": 147}, None, None, False),
    ("AirPods", {"AirPods"}, {"MacBook Pro的麥克風": 147}, None, None, False),
    (None, set(), {"MacBook Pro的麥克風": 147, "AirPods": 300}, "MacBook Pro的麥克風", 147, True),
    (None, set(), {"MacBook Pro的麥克風": 147, "AirPods": 300}, "MacBook Pro的麥克風", 300, False),
])
def test_PortAudio清單跟系統一致嗎(monkeypatch, name, pa, ca, pa_default, ca_default, 期望):
    monkeypatch.setattr(rec, "IS_MAC", True)
    r = rec.AudioRecorder()
    r._device_name = name
    monkeypatch.setattr(rec, "_ca_input_devices", lambda: ca)
    monkeypatch.setattr(rec, "_ca_default_input", lambda: ca_default)
    monkeypatch.setattr(rec.AudioRecorder, "list_devices", staticmethod(lambda: [{"id": 0, "name": n} for n in pa]))
    monkeypatch.setattr(rec, "sd", types.SimpleNamespace(query_devices=lambda kind=None: {"name": pa_default}))
    assert r.portaudio_view_consistent() is 期望


def test_重掃之後舊的狀態作廢(monkeypatch):
    r = rec.AudioRecorder()
    r._ca_snap = {"default": 1, "ids": {}}
    monkeypatch.setattr(rec, "sd", types.SimpleNamespace(_terminate=lambda: None, _initialize=lambda: None,
                                                         query_devices=lambda: []))
    r.refresh_portaudio()
    assert r._ca_snap is None, "備援重掃後沒重新記的話，下一次變動要當作受影響"


def test_關掉之後不准再登記監聽():
    from platform_util import IS_MAC
    if not IS_MAC:
        pytest.skip("只有 macOS 用 CoreAudio 監聽")
    r = rec.AudioRecorder()
    r.stop_device_monitor()
    assert r.start_device_monitor(lambda: None) is False


def test_儲存時選了還沒掃到的麥克風_背景重掃而且記住名字(monkeypatch):
    import gui
    win = gui.AppWindow.__new__(gui.AppWindow)
    calls = []
    win._handle_device_change = lambda force=False, recheck=True: calls.append(force)
    r = rec.AudioRecorder()
    monkeypatch.setattr(rec.AudioRecorder, "list_devices", staticmethod(lambda: [{"id": 0, "name": "MacBook Pro的麥克風"}]))
    monkeypatch.setattr(rec, "coreaudio_input_names", lambda: {"MacBook Pro的麥克風", "AirPods"})
    win.recorder = r
    gui.AppWindow._apply_input_device_setting(win, "AirPods")
    assert r._device_name == "AirPods" and calls == [True]


def test_改回系統預設_舊名字要清掉(monkeypatch):
    import gui
    win = gui.AppWindow.__new__(gui.AppWindow)
    r = rec.AudioRecorder()
    r._device_name, r._device_index = "MacBook Pro的麥克風", 3
    win.recorder = r
    gui.AppWindow._apply_input_device_setting(win, None)
    assert r._device_name is None and r._device_index is None


def test_設定視窗補上系統現在的麥克風名字():
    import gui
    sw = gui.SettingsWindow.__new__(gui.SettingsWindow)
    sw._device_values = ["（系統預設）", "MacBook Pro的麥克風"]
    got = {}
    sw._device_menu = types.SimpleNamespace(configure=lambda **k: got.update(k))
    gui.SettingsWindow._merge_live_device_names(sw, {"MacBook Pro的麥克風", "AirPods"})
    assert got["values"] == ["（系統預設）", "MacBook Pro的麥克風", "AirPods"]


def test_重掃中不開音量表_也不標成壞掉(monkeypatch):
    import gui
    sw = gui.SettingsWindow.__new__(gui.SettingsWindow)
    sw.master = types.SimpleNamespace(_audio_ready=True, _audio_refreshing=True)
    sw._level_stream_failed = False
    gui.SettingsWindow._open_level_stream(sw)
    assert sw._level_stream_failed is False, "重掃完下一輪要能再試"
