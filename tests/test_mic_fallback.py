"""麥克風打不開時的補救流程 unit tests（v2.31.4）。

2026-10-03／04 使用者遇到兩種舊邏輯救不了的狀況：
  • EarPods 硬體起不來：coreaudiod「Failed calling start_io」，每次等 5～8.5 秒才失敗，
    而它剛好是系統預設——舊邏輯只在「有指定裝置」時才退，預設失敗就直接放棄
  • 拔掉耳機後 PortAudio 清單過期：問預設麥克風拿到 -1，4 毫秒內失敗，重整就好

補救順序：①重整清單 ②同一支再試（只在瞬間失敗時）③內建麥克風 ④系統預設。

全部用假的 InputStream，不碰真的麥克風；唯一例外是最後一個測試，
它只「查詢」這台 Mac 的內建麥克風名字，不開任何錄音。
"""

import pytest

import recorder as _rec
from platform_util import IS_MAC

DEVICES = [
    {"id": 0, "name": "EarPods"},
    {"id": 1, "name": "MacBook Pro的麥克風"},
    {"id": 2, "name": "BlackHole 16ch"},
]


class _FakeStream:
    latency = 0.01
    cpu_load = 0.0

    def __init__(self, fail: bool, log: list, device):
        self._fail, self._log, self.device = fail, log, device
        self.closed = False

    def start(self):
        self._log.append(self.device)
        if self._fail:
            raise RuntimeError("Error starting stream")

    def stop(self):
        pass

    def close(self):
        self.closed = True


@pytest.fixture
def env(monkeypatch):
    """可設定「哪些裝置打得開」的假環境；回傳 (recorder, 狀態)。"""
    state = {"ok": set(), "opened": [], "streams": [], "refreshed": 0, "devices": list(DEVICES)}

    def fake_stream(*args, device=None, **kwargs):
        s = _FakeStream(device not in state["ok"], state["opened"], device)
        state["streams"].append(s)
        return s

    def fake_refresh(self):
        state["refreshed"] += 1
        return state["devices"]

    monkeypatch.setattr(_rec.sd, "InputStream", fake_stream)
    monkeypatch.setattr(_rec.AudioRecorder, "list_devices", staticmethod(lambda: state["devices"]))
    monkeypatch.setattr(_rec.AudioRecorder, "refresh_portaudio", fake_refresh)
    monkeypatch.setattr(_rec, "builtin_input_device_name", lambda: "MacBook Pro的麥克風")
    monkeypatch.setattr(_rec, "_SLOW_OPEN_FAIL_S", 999.0)   # 預設當「瞬間失敗」
    rec = _rec.AudioRecorder()
    yield rec, state
    if rec.is_recording():
        rec.stop()


def _slow(monkeypatch):
    """讓第一次失敗被判定成「硬體卡住、慢慢失敗」。"""
    monkeypatch.setattr(_rec, "_SLOW_OPEN_FAIL_S", -1.0)


def test_一次就成功不重整也不提示(env):
    rec, st = env
    st["ok"] = {None}
    assert rec.start() is True
    assert st["refreshed"] == 0
    assert rec._started_with_fallback is False


def test_拔掉耳機後清單過期_重整後同一支就好_不跳提示(env):
    """2026-10-04 00:56 的狀況：第一次瞬間失敗，重整清單後同樣用系統預設就成功。"""
    rec, st = env
    calls = {"n": 0}

    def flaky(*args, device=None, **kwargs):
        calls["n"] += 1
        s = _FakeStream(calls["n"] == 1, st["opened"], device)   # 只有第一次失敗
        return s

    _rec.sd.InputStream = flaky   # fixture 的 monkeypatch 會在結束時還原
    assert rec.start() is True
    assert st["refreshed"] == 1
    assert st["opened"] == [None, None], "應該重整後再試同一支，而不是跳去內建"
    assert rec._started_with_fallback is False, "同一支救回來不算換麥克風，不該跳提示"


def test_硬體卡住_不再試同一支_直接改內建(env, monkeypatch):
    """2026-10-03 EarPods 的狀況：慢慢失敗代表硬體起不來，再試同一支只會白等 5 秒。"""
    rec, st = env
    _slow(monkeypatch)
    st["ok"] = {1}
    assert rec.start() is True
    assert st["opened"] == [None, 1], "不該重試系統預設，直接跳內建"
    assert rec._fallback_label == "內建麥克風"
    assert rec._started_with_fallback is True


def test_系統預設打不開_改用內建(env):
    """舊邏輯（v2.21.4）在沒有指定裝置時，失敗就直接放棄——這是使用者這次卡住的原因。"""
    rec, st = env
    st["ok"] = {1}            # 只有內建打得開，系統預設（None）打不開
    rec._device_index = None
    assert rec.start() is True
    assert st["opened"][-1] == 1
    assert rec._fallback_label == "內建麥克風"


def test_指定裝置和內建都失敗_最後退到系統預設(env):
    rec, st = env
    rec.set_device_by_name("EarPods")
    st["ok"] = {None}
    assert rec.start() is True
    assert st["opened"] == [0, 0, 1, None], "順序：EarPods → 重整後 EarPods → 內建 → 系統預設"
    assert rec._fallback_label == "系統預設麥克風"


def test_第一次慢慢失敗的就是內建本身_不再試內建(env, monkeypatch):
    rec, st = env
    _slow(monkeypatch)
    rec.set_device_by_name("MacBook Pro的麥克風")
    st["ok"] = {None}
    assert rec.start() is True
    assert st["opened"] == [1, None], "內建已經卡過一次，不該再等它一輪"


def test_全部都打不開_回失敗而且沒有殘留(env):
    rec, st = env
    rec.set_device_by_name("EarPods")
    st["ok"] = set()
    assert rec.start() is False
    assert rec.is_recording() is False
    assert rec._stream is None


def test_啟動失敗的串流都有被關掉(env):
    """建好但啟動失敗的串流不關，會佔住裝置、讓接下來換麥克風也失敗。"""
    rec, st = env
    rec.set_device_by_name("EarPods")
    st["ok"] = {1}
    assert rec.start() is True
    failed = [s for s in st["streams"] if s._fail]
    assert failed and all(s.closed for s in failed)


def test_重整後編號變了_用新編號而且記下來(env):
    rec, st = env
    rec.set_device_by_name("EarPods")          # 一開始 EarPods 是 0 號

    def renumber(self):
        st["refreshed"] += 1
        st["devices"] = [{"id": 5, "name": "EarPods"}, {"id": 6, "name": "MacBook Pro的麥克風"}]
        return st["devices"]

    _rec.AudioRecorder.refresh_portaudio = renumber
    st["ok"] = {5}
    assert rec.start() is True
    assert st["opened"] == [0, 5]
    assert rec._device_index == 5, "下次錄音要直接用新編號"


def test_查不到內建麥克風就跳過這一步(env, monkeypatch):
    rec, st = env
    monkeypatch.setattr(_rec, "builtin_input_device_name", lambda: None)
    rec.set_device_by_name("EarPods")
    st["ok"] = {None}
    assert rec.start() is True
    assert st["opened"] == [0, 0, None]


def test_非_Mac_不查內建麥克風(monkeypatch):
    monkeypatch.setattr(_rec, "IS_MAC", False)
    assert _rec.builtin_input_device_name() is None


@pytest.mark.skipif(not IS_MAC, reason="只有 macOS 有內建麥克風可查")
def test_這台_Mac_查得到內建麥克風而且錄音程式認得():
    """真的去問 CoreAudio（只查名字，不開錄音、不需要麥克風權限）。"""
    name = _rec.builtin_input_device_name()
    if name is None:
        pytest.skip("這台 Mac 沒有內建麥克風（例如 Mac mini）")
    assert name in {d["name"] for d in _rec.AudioRecorder.list_devices()}
