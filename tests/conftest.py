"""全部測試共用的前提。"""

import pytest

import mac_audio_queue

_REAL_LOAD = mac_audio_queue._load   # 在任何 fixture 換掉它之前先留一份（只讀查詢的測試要還原）


@pytest.fixture(scope="session", autouse=True)
def _load_sounddevice_once():
    """v2.32.3 起 Mac 上 sounddevice 不在 import recorder 時載入（改由 App 在背景載入）。
    舊測試都假設 import 完就能用 recorder.sd，這裡先載入一次恢復這個前提。
    測試是從終端機跑，沒有 App 身分，不受麥克風模式影響，載入約 0.2 秒。
    要測「還沒載入」的情況，各測試自己用 monkeypatch 把 recorder.sd 設回 None。"""
    import recorder
    recorder.ensure_sounddevice()
    yield


@pytest.fixture(autouse=True)
def _never_touch_real_audio(monkeypatch):
    """v2.33.0：Mac 錄音改走 AudioQueue。測試絕不能真的開麥克風（選單列橘燈會亮、還會跟使用者正在用的 App 搶）。

    mac_audio_queue._load 是所有真實系統呼叫（開 AudioQueue、問 CoreAudio）的唯一入口，擋在這裡：
    沒注入假串流的測試一碰到真的就會當場失敗，而不是默默開了麥克風。
    需要「只讀」問 CoreAudio 的測試用 allow_coreaudio_read；測 ctypes 管線的測試自己換成假函式庫。"""
    def _blocked():
        raise OSError("tests must not touch the real AudioToolbox/CoreAudio (inject a fake)")
    monkeypatch.setattr(mac_audio_queue, "_load", _blocked)


@pytest.fixture(autouse=True)
def _never_open_real_portaudio(monkeypatch):
    """第二道封鎖（第三輪）：Windows 路徑用 sounddevice（PortAudio），Mac 的 _never_touch_real_audio 擋不到它。
    實作與審查的 mutation 期間，曾有測試因為少了一道判斷而真的走到 sd.InputStream、短暫開了麥克風
    （實作期間 2 次、審查 mutation 1 次）。這裡把 sounddevice 的串流類別換成會丟例外的替身：
    測試自己注入的假 sd（recorder.sd = 假物件）不受影響，其餘任何路徑碰到真的都會當場失敗而不是開麥克風。"""
    try:
        import sounddevice
    except Exception:
        return

    class _BlockedStream:
        def __init__(self, *a, **k):
            raise OSError("tests must not open the real microphone via PortAudio (inject a fake sd)")
    for name in ("InputStream", "RawInputStream", "Stream", "RawStream"):
        if hasattr(sounddevice, name):
            monkeypatch.setattr(sounddevice, name, _BlockedStream)


@pytest.fixture
def allow_coreaudio_read(monkeypatch):
    """還原真的 _load——只給「只讀查詢」的測試（問裝置名字，不開麥克風）。"""
    monkeypatch.setattr(mac_audio_queue, "_load", _REAL_LOAD)


# ── v2.33.0：假的 AudioQueue 環境（Mac 錄音相關測試共用）──────────────────────────────

import threading
import time
import types

import numpy as np


def wait_for(cond, timeout: float = 3.0, step: float = 0.01) -> bool:
    """等條件成立（背景執行緒的結果）。逾時回 False，由呼叫端的 assert 報錯。"""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


class _FakeAQBase:
    """模仿 mac_audio_queue.AQInputStream；env 由 mac_env 注入。絕不碰真的 AudioToolbox。"""
    env = None

    def __init__(self, samplerate, channels, blocksize, device_uid, callback):
        env = self.env
        self.uid, self.callback, self.blocksize = device_uid, callback, blocksize
        self.started = False
        self.closed = False
        self.close_calls = 0
        if device_uid in env.fail_new:
            raise OSError("AudioQueueNewInput failed (fake)")
        env.instances.append(self)
        env.opened.append(device_uid)

    def start(self):
        env = self.env
        if env.gate is not None:
            env.gate.wait(10)
        if self.uid in env.fail_start:
            raise OSError("AudioQueueStart failed (fake)")
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        # 偵測「在錄音鎖裡面關硬體」：真的 AudioQueueStop 會等 callback 跑完，而 callback 要拿這把鎖。
        # RLock 同一條執行緒可重入，所以要換一條執行緒去搶——搶不到就代表呼叫端握著鎖
        env = self.env
        if env.lock_probe is not None:
            got = []

            def _probe():
                ok = env.lock_probe.acquire(timeout=0.5)
                got.append(ok)
                if ok:
                    env.lock_probe.release()
            t = threading.Thread(target=_probe)
            t.start()
            t.join()
            env.close_lock_free.append(bool(got and got[0]))
        self.closed = True
        self.close_calls += 1

    def emit(self, n: int = 1600, value: float = 0.1):
        """模擬系統送來一塊聲音（呼叫端的 callback，跟真的一樣在別條執行緒也可以）。"""
        self.callback(np.full((n, 1), value, dtype=np.float32), n, None, None)


@pytest.fixture
def mac_env(monkeypatch):
    """Mac 路徑的假環境：假 AudioQueue、假 CoreAudio 查詢、乾淨的暖機狀態與裝置快取。"""
    import recorder as rec

    env = types.SimpleNamespace(
        instances=[], opened=[], fail_start=set(), fail_new=set(), gate=None,
        lock_probe=None, close_lock_free=[],
        devices=[{"id": "uid-ear", "name": "EarPods"},
                 {"id": "uid-mbp", "name": "MacBook Pro的麥克風"},
                 {"id": "uid-bh", "name": "BlackHole 16ch"}],
        default=(147, "MacBook Pro的麥克風"), list_calls=0, list_error=None,
    )

    class _AQ(_FakeAQBase):
        pass
    _AQ.env = env

    def _input_devices():
        env.list_calls += 1
        if env.list_error is not None:
            raise env.list_error
        return [dict(d) for d in env.devices]

    monkeypatch.setattr(mac_audio_queue, "AQInputStream", _AQ)
    monkeypatch.setattr(mac_audio_queue, "default_input", lambda: env.default)
    monkeypatch.setattr(mac_audio_queue, "default_input_id", lambda: env.default[0])
    monkeypatch.setattr(mac_audio_queue, "input_devices", _input_devices)
    monkeypatch.setattr(rec, "IS_MAC", True)
    monkeypatch.setattr(rec, "_warm", rec._WarmState())
    monkeypatch.setattr(rec, "_mac_devices", [])
    monkeypatch.setattr(rec, "_mac_devices_ready", False)
    monkeypatch.setattr(rec, "_mac_devices_event", threading.Event())
    monkeypatch.setattr(rec, "builtin_input_device_name", lambda: "MacBook Pro的麥克風")
    monkeypatch.setattr(rec, "_SLOW_OPEN_FAIL_S", 999.0)   # 預設把失敗當「瞬間失敗」
    env.rec = rec
    yield env
    env.gate and env.gate.set()
    rec.release_warm()
    for t in list(threading.enumerate()):
        if t.name in ("MicWarmup", "MicClose"):
            t.join(2)
