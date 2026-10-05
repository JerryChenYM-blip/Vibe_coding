"""蘋果「邊錄邊辨識」的回歸測試（v2.32.0）。

用一支假的常駐程式（Python 寫、照同一套 stdin/stdout JSON 協定回應）代替真的 Swift helper，
所以這組測試不需要 macOS 26、不需要蘋果的模型，任何機器都跑得動。
真引擎的端到端實測數字記在 docs/工作紀錄/2026-10-05_v2.32.0-*.md。

重點：
  (a) 正常流程：聲音一塊不漏地送到對面、拿到結果
  (b) 引擎還在準備時就送進來的聲音不能掉（錄音一開始就在講話）
  (c) 任何失敗都要回 None——呼叫端靠這個退回舊做法，使用者不會卡住
  (d) 程式死掉之後下一次要自己重開
  (e) 轉錄端：拿到邊錄邊辨識的結果就不再呼叫 helper，但後續整理照樣走
  (f) App 端：邊錄邊辨識時不切段、錄音看門狗改看音量、對照組到期自動停
"""

import datetime
import os
import stat
import sys
import textwrap
import time
import types

import numpy as np
import pytest

import apple_stream as aps
import transcriber as tr


FAKE_HELPER = textwrap.dedent('''
    import base64, json, os, sys, time
    mode = os.environ.get("FAKE_MODE", "ok")
    def out(d):
        sys.stdout.write(json.dumps(d) + "\\n"); sys.stdout.flush()
    out({"event": "hello", "protocol": 1})
    frames = 0
    for line in sys.stdin:
        cmd = json.loads(line)
        c = cmd.get("cmd")
        if c == "begin":
            frames = 0
            if mode == "begin_error":
                out({"event": "error", "error": "asset_not_installed"}); continue
            if mode == "slow_ready":
                time.sleep(0.5)
            out({"event": "ready", "locale": "zh_TW", "prepare_ms": 1.0})
        elif c == "audio":
            frames += len(base64.b64decode(cmd["pcm"])) // 4
            if mode == "crash_on_audio":
                sys.exit(3)
            if mode == "stall_on_audio":
                time.sleep(60)          # 活著，但再也不讀 stdin
        elif c == "end":
            if frames == 0:             # 跟真的程式一樣：一塊聲音都沒收到就回 empty_audio
                out({"event": "result", "ok": False, "error": "empty_audio"}); continue
            if mode == "hang_on_end":
                time.sleep(30)
            bad = 1 if mode == "bad_chunks" else 0
            secs = frames / 16000 / (2 if mode == "short_audio" else 1)
            out({"event": "result", "ok": True, "parts": [f"收到{frames}個樣本"],
                 "tail_ms": 12.0, "audio_seconds": secs, "bad_chunks": bad})
        elif c == "cancel":
            out({"event": "cancelled"})
        elif c == "ping":
            out({"event": "pong"})
''')


@pytest.fixture
def fake_helper(tmp_path, monkeypatch):
    """回傳 (建立 engine 的函式)。可用 FAKE_MODE 環境變數切換假程式的行為。"""
    script = tmp_path / "fake_helper.py"
    script.write_text(FAKE_HELPER, encoding="utf-8")
    launcher = tmp_path / "fake_helper"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}"\n', encoding="utf-8")
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)

    def make(mode="ok"):
        monkeypatch.setenv("FAKE_MODE", mode)
        return aps.AppleStreamEngine(str(launcher))
    return make


def _blocks(n_blocks=5, size=1600):
    return [np.full(size, 0.1, dtype=np.float32) for _ in range(n_blocks)]


# ── (a)(b) 正常流程 ──

def test_聲音一塊不漏地送到對面(fake_helper):
    eng = fake_helper()
    s = aps.AppleStreamSession(eng, "zh-TW")
    for b in _blocks(5):
        s.push(b)
    r = s.finish(timeout=10)
    assert r is not None and r["ok"]
    assert r["parts"] == [f"收到{5 * 1600}個樣本"]
    assert s.failure is None


def test_引擎還在準備時送進來的聲音不會掉(fake_helper):
    """錄音一開始就在講話：準備要 0.5 秒，這段時間的聲音要先排隊、準備好再補送。"""
    eng = fake_helper("slow_ready")
    s = aps.AppleStreamSession(eng, "zh-TW")
    for b in _blocks(3):          # 引擎還沒 ready 就先推
        s.push(b)
    r = s.finish(timeout=10)
    assert r["parts"] == [f"收到{3 * 1600}個樣本"]


def test_同一支程式可以連續用好幾次(fake_helper):
    eng = fake_helper()
    for n in (2, 4):
        s = aps.AppleStreamSession(eng, "zh-TW")
        for b in _blocks(n):
            s.push(b)
        assert s.finish(timeout=10)["parts"] == [f"收到{n * 1600}個樣本"]


# ── (c) 任何失敗都回 None ──

def test_引擎準備失敗_回None而且有原因(fake_helper):
    s = aps.AppleStreamSession(fake_helper("begin_error"), "zh-TW")
    assert s.finish(timeout=10) is None
    assert s.failure == "begin:asset_not_installed"


def test_程式中途死掉_回None(fake_helper):
    s = aps.AppleStreamSession(fake_helper("crash_on_audio"), "zh-TW")
    for b in _blocks(3):
        s.push(b)
    assert s.finish(timeout=10) is None
    assert s.failure is not None


def test_收尾卡住_時間到就放棄(fake_helper):
    s = aps.AppleStreamSession(fake_helper("hang_on_end"), "zh-TW")
    s.push(_blocks(1)[0])
    t0 = time.monotonic()
    assert s.finish(timeout=1.0) is None
    assert time.monotonic() - t0 < 3, "不能一直等下去"
    assert s.failure == "finish_timeout"


def test_找不到程式_回None(tmp_path):
    s = aps.AppleStreamSession(aps.AppleStreamEngine(str(tmp_path / "不存在")), "zh-TW")
    assert s.finish(timeout=5) is None
    assert s.failure == "helper_unavailable"


def test_取消(fake_helper):
    s = aps.AppleStreamSession(fake_helper(), "zh-TW")
    s.push(_blocks(1)[0])
    s.abort()
    assert s.finish(timeout=5) is None
    assert s.failure == "aborted"


# ── (d) 死掉之後下一次自己重開 ──

def test_程式死掉之後下一次自己重開(fake_helper):
    eng = fake_helper()
    eng.channel().kill()
    time.sleep(0.2)
    s = aps.AppleStreamSession(eng, "zh-TW")
    s.push(_blocks(1)[0])
    assert s.finish(timeout=10) is not None


# ── (e) 轉錄端 ──

def test_有邊錄邊辨識的結果就不再呼叫_helper(monkeypatch):
    def boom(self, args, timeout):
        raise AssertionError("已經有結果了，不該再呼叫 helper")
    monkeypatch.setattr(tr.Transcriber, "_run_apple_helper", boom)
    payload = {"ok": True, "parts": ["我隻有 83 122"], "locale": "zh_TW", "tail_ms": 150.0}
    r = tr.Transcriber()._transcribe_apple(
        np.zeros(16000, dtype=np.float32), "apple-speech", None, None,
        chinese_variant="traditional_tw", precomputed=payload)
    # 後續整理照樣走：簡轉繁重整、數字逗號
    assert r.text == "我只有 83，122"
    assert r.apple_meta == {"streamed": True, "engine_ms": 150.0, "segments": 1}


def test_邊錄邊辨識的結果失敗時照樣報錯(monkeypatch):
    payload = {"ok": False, "error": "finalize_failed"}
    with pytest.raises(tr.AppleSTTError):
        tr.Transcriber()._transcribe_apple(
            np.zeros(16000, dtype=np.float32), "apple-speech", None, None, precomputed=payload)


def test_不是蘋果模型就不建_session():
    assert tr.Transcriber().start_apple_stream("qwen3-asr", None) is None


# ── (f) App 端 ──

def _win(monkeypatch, tail_rms):
    import gui
    win = gui.AppWindow.__new__(gui.AppWindow)
    win._state = "recording"
    win._stream_tick_id = None
    win._la_buffer = None
    win._last_voice_at = 0.0
    win._apple_session = types.SimpleNamespace(failure=None, check_alive=lambda: True)   # 活著的 session
    win.STREAM_TICK_MS = 1000
    win._recording_watchdog_check = lambda: None
    win.after = lambda ms, fn: "tick"
    win.recorder = types.SimpleNamespace(
        get_tail=lambda n: np.full(n, tail_rms, dtype=np.float32),
        get_buffer_snapshot=lambda: (_ for _ in ()).throw(AssertionError("邊錄邊辨識時不該切段")),
    )
    win._dispatch_stream_chunk = lambda chunk: (_ for _ in ()).throw(AssertionError("不該切段"))
    return gui, win


def test_邊錄邊辨識時不切段_有講話就刷新看門狗(monkeypatch):
    gui, win = _win(monkeypatch, tail_rms=0.03)
    gui.AppWindow._stream_tick(win)
    assert win._last_voice_at > 0.0, "有聲音要刷新，否則講 15 分鐘會被當成沒講話而自動停止"
    assert win._stream_tick_id == "tick"


def test_邊錄邊辨識時_安靜就不刷新看門狗(monkeypatch):
    gui, win = _win(monkeypatch, tail_rms=0.0005)
    gui.AppWindow._stream_tick(win)
    assert win._last_voice_at == 0.0, "真的沒講話時，看門狗要能發揮作用"


@pytest.mark.parametrize("until,期望", [
    ((datetime.date.today() + datetime.timedelta(days=3)).isoformat(), True),
    (datetime.date.today().isoformat(), True),                 # 到期日當天還跑
    ((datetime.date.today() - datetime.timedelta(days=1)).isoformat(), False),
    ("", False),
    ("不是日期", False),                                        # 寫錯就不跑，不拖累主流程
])
def test_對照組到期自動停(until, 期望):
    import gui
    win = gui.AppWindow.__new__(gui.AppWindow)
    win.cfg = types.SimpleNamespace(apple_shadow_until=until)
    assert gui.AppWindow._apple_shadow_active(win) is 期望


# ── (g) 審查後修正 ──

def test_失敗三次就先停用(fake_helper):
    """程式活著卻不回話時，每次錄音都要白等好幾秒才退回；常失敗就先別用。"""
    eng = fake_helper("begin_error")
    for _ in range(3):
        assert eng.available()
        aps.AppleStreamSession(eng, "zh-TW").finish(timeout=10)
    assert not eng.available()
    eng._cooldown_until = 0.0          # 冷卻時間到
    assert eng.available(), "冷卻完要再試，偶發的問題不能讓人好幾天都用不到"


def test_時好時壞也會停用(fake_helper):
    """只有長錄音才卡住、短的正常時，「連續」計數會一直被歸零（第二輪審查交替 8 次多等 32 秒）。"""
    eng = fake_helper()
    for ok in (False, True, False, True, False):
        eng.note_result(ok, "x")
    assert not eng.available()


def test_偶爾失敗一兩次不停用(fake_helper):
    eng = fake_helper()
    for ok in (False, True, True, False, True, True, True):
        eng.note_result(ok, "x")
    assert eng.available()


def test_很久以前的失敗不算(fake_helper):
    """只看最近 5 次：早期的失敗被後面的成功擠出去之後，就不該再算。"""
    eng = fake_helper()
    for ok in (False, False, True, True, True, True, True, False):
        eng.note_result(ok, "x")
    assert eng.available()


def _trip(eng):
    for _ in range(3):
        eng.note_result(False, "x")
    assert not eng.available()
    eng._cooldown_until = 0.0


def test_冷卻後第一次成功就清掉舊帳(fake_helper):
    """不清的話，冷卻前的失敗還在最近 5 次裡，恢復正常後偶發一次失敗又停 10 分鐘。"""
    eng = fake_helper()
    _trip(eng)
    eng.note_result(True)
    eng.note_result(False, "偶發")
    assert eng.available()


def test_冷卻後第一次又失敗就馬上再停(fake_helper):
    eng = fake_helper()
    _trip(eng)
    eng.note_result(False, "還是壞的")
    assert not eng.available()
    eng._cooldown_until = 0.0
    eng.note_result(True)
    assert eng.available()


def test_沒收到聲音不算程式失敗(fake_helper):
    """一塊聲音都沒有（按了馬上放）是輸入的問題，不是程式壞掉。"""
    eng = fake_helper()
    for _ in range(3):
        s = aps.AppleStreamSession(eng, "zh-TW")
        assert s.finish(timeout=10) is None
        assert s.failure == "end:empty_audio"
    assert list(eng._recent) == [] and eng.available()


@pytest.mark.parametrize("ev,期望", [
    ({"audio_seconds": 1.9, "bad_chunks": 0}, True),      # 差 0.1 秒：容許範圍內
    ({"audio_seconds": 1.7, "bad_chunks": 0}, False),     # 差 0.3 秒：算少收
    ({"bad_chunks": 0}, True),                            # 欄位缺了不擋
])
def test_少收的容許範圍(ev, 期望):
    s = aps.AppleStreamSession.__new__(aps.AppleStreamSession)
    s._sent = 32_000                   # 送了 2 秒
    assert s._complete(ev) is 期望


def test_使用者取消不算失敗(fake_helper):
    eng = fake_helper()
    for _ in range(4):
        s = aps.AppleStreamSession(eng, "zh-TW"); s.push(_blocks(1)[0]); s.abort()
        s.finish(timeout=5)
    assert eng.available()


def test_收尾逾時只算一次失敗(fake_helper):
    eng = fake_helper("hang_on_end")
    s = aps.AppleStreamSession(eng, "zh-TW"); s.push(_blocks(1)[0])
    assert s.finish(timeout=1.0) is None
    time.sleep(0.5)                    # 背景執行緒稍後會因為連線被砍再失敗一次
    assert list(eng._recent) == [False]


@pytest.mark.parametrize("mode", ["bad_chunks", "short_audio"])
def test_對面少收聲音就算失敗(fake_helper, mode):
    """對面跳過壞掉的聲音卻回成功：用了會少字。要算失敗，保險絲才看得到這支程式有問題。"""
    eng = fake_helper(mode)
    s = aps.AppleStreamSession(eng, "zh-TW")
    for b in _blocks(20):            # 2 秒；short_audio 回報只收到 1 秒，超過 0.25 秒的容許
        s.push(b)
    assert s.finish(timeout=10) is None
    assert s.failure == "incomplete"
    assert list(eng._recent) == [False]


def _wait_ready(s, timeout=10):
    deadline = time.monotonic() + timeout
    while s.prepare_ms is None and s.failure is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert s.prepare_ms is not None, "引擎沒準備好"


def _push_until_not_alive(s, seconds=6):
    """引擎準備好之後才開始送（準備期間排隊本來就正常，不能算卡住），每 0.1 秒送 2 秒的聲音。"""
    _wait_ready(s)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        s.push(_blocks(1, size=32_000)[0])
        if not s.check_alive():
            return False
        time.sleep(0.1)
    return True


def test_程式活著但不收聲音_錄音中就發現(fake_helper, monkeypatch):
    """卡住的程式不會自己報錯；不檢查的話要等放開熱鍵、收尾逾時才發現。"""
    monkeypatch.setattr(aps.AppleStreamSession, "STALL_RECORDING_S", 0.5)
    s = aps.AppleStreamSession(fake_helper("stall_on_audio"), "zh-TW")
    assert _push_until_not_alive(s) is False
    assert s.failure == "stalled"
    t0 = time.monotonic()
    assert s.finish(timeout=10) is None
    assert time.monotonic() - t0 < 1, "已經知道壞了，放開時不用再等"


def test_同樣的送法_正常程式不會被當成卡住(fake_helper, monkeypatch):
    """對照組：上一個測試的送法換成正常的程式，必須一路都是活的。"""
    monkeypatch.setattr(aps.AppleStreamSession, "STALL_RECORDING_S", 0.5)
    s = aps.AppleStreamSession(fake_helper(), "zh-TW")
    assert _push_until_not_alive(s, seconds=2) is True
    assert s.finish(timeout=10) is not None


def test_準備期間排隊不算卡住(fake_helper, monkeypatch):
    """引擎準備要 0.5 秒，期間積了好幾秒的聲音：不能誤判（準備真的卡住由準備逾時處理）。"""
    monkeypatch.setattr(aps.AppleStreamSession, "STALL_RECORDING_S", 0.1)
    s = aps.AppleStreamSession(fake_helper("slow_ready"), "zh-TW")
    for _ in range(3):
        s.push(_blocks(1, size=32_000)[0])
        assert s.check_alive()
    assert s.finish(timeout=10) is not None


def test_放開前才卡住_收尾時幾秒內就放棄(fake_helper, monkeypatch):
    """放開前幾秒才卡住，錄音中來不及發現；收尾時不能白等到逾時。"""
    monkeypatch.setattr(aps.AppleStreamSession, "STALL_FINISH_S", 0.5)
    s = aps.AppleStreamSession(fake_helper("stall_on_audio"), "zh-TW")
    _wait_ready(s)
    for _ in range(3):
        s.push(_blocks(1, size=32_000)[0])
    t0 = time.monotonic()
    assert s.finish(timeout=30) is None
    assert time.monotonic() - t0 < 3
    assert s.failure == "stalled"


def test_只砍自己那支程式(fake_helper):
    """舊 session 晚一步重開，不能把下一次錄音正在用的程式砍掉。"""
    eng = fake_helper()
    old = eng.channel()
    eng.reset(old)
    new = eng.channel()
    assert new is not old and not old.alive()
    eng.reset(old)                     # 舊的又被重開一次
    assert new.alive() and eng.channel() is new
    eng.reset()


def test_冷卻中不建_session(monkeypatch):
    monkeypatch.setattr(tr, "IS_MAC", True)
    monkeypatch.setattr(tr, "_get_apple_stream_engine",
                        lambda: types.SimpleNamespace(available=lambda: False))
    assert tr.Transcriber().start_apple_stream("apple-speech", None) is None


@pytest.mark.parametrize("payload,n,期望", [
    ({"ok": True, "audio_seconds": 2.0, "bad_chunks": 0}, 32_000, True),
    ({"ok": True, "audio_seconds": 1.0, "bad_chunks": 0}, 32_000, False),   # 少了 1 秒
    ({"ok": True, "audio_seconds": 2.0, "bad_chunks": 1}, 32_000, False),   # 有一塊壞掉被跳過
    ({"ok": True}, 32_000, True),                                           # 欄位缺了：不擋
    ({"ok": False, "error": "x"}, 32_000, True),                            # 失敗的交給後面報錯
])
def test_串流結果是否完整(payload, n, 期望):
    assert tr._stream_payload_complete(payload, n) is 期望


def test_串流結果少收聲音就整段重送(monkeypatch):
    sent = []
    monkeypatch.setattr(tr.Transcriber, "_run_apple_oneshot",
                        lambda self, audio, e, l, t: sent.append(len(audio)) or
                        {"ok": True, "parts": ["完整的"], "elapsed_ms": 300.0})
    incomplete = {"ok": True, "parts": ["少"], "tail_ms": 100.0, "audio_seconds": 0.5, "bad_chunks": 0}
    r = tr.Transcriber()._transcribe_apple(np.zeros(32_000, dtype=np.float32), "apple-speech", None, None,
                                           precomputed=incomplete)
    assert sent == [32_000]
    assert r.apple_meta["streamed"] is False


def test_錄音途中_session_掛了就恢復切段(monkeypatch):
    gui, win = _win(monkeypatch, tail_rms=0.03)
    # failure 還是空的、只有 check_alive 說不行（卡住）：App 要問 check_alive，不能只看 failure
    win._apple_session = types.SimpleNamespace(failure=None, check_alive=lambda: False)
    listener = []
    snap = []
    win.recorder.set_block_listener = lambda fn: listener.append(fn)
    win.recorder.get_buffer_snapshot = lambda: snap.append(1) or np.zeros(16_000, dtype=np.float32)
    win._stream_samples = 0
    win._stream_dispatched = 0
    win.STREAM_MIN_ENABLE_SAMPLES = 12 * 16_000
    gui.AppWindow._stream_tick(win)
    assert win._apple_session is None
    assert listener == [None], "不再把聲音丟給死掉的 session"
    assert snap, "要回到切段流程"


@pytest.mark.parametrize("cfg,期望", [
    (dict(apple_streaming=True, ollama_enabled=False, streaming_algo="fixed_chunk"), True),
    (dict(apple_streaming=False, ollama_enabled=False, streaming_algo="fixed_chunk"), False),
    (dict(apple_streaming=True, ollama_enabled=True, streaming_algo="fixed_chunk"), False),   # 開潤飾：切段邊講邊潤飾比較快
    (dict(apple_streaming=True, ollama_enabled=False, streaming_algo="local_agreement"), False),
])
def test_什麼時候用邊錄邊辨識(cfg, 期望):
    import gui
    win = gui.AppWindow.__new__(gui.AppWindow)
    win.cfg = types.SimpleNamespace(**cfg)
    assert gui.AppWindow._apple_streaming_wanted(win) is 期望


def test_補切段時每段最多12秒(monkeypatch):
    """錄音途中串流掛掉、從頭補切：不能把整段 25 分鐘切成一大段（放開後等不到就被丟掉）。"""
    gui, win = _win(monkeypatch, tail_rms=0.03)
    win._apple_session = types.SimpleNamespace(failure="send_failed", check_alive=lambda: False)
    backlog = np.full(16_000 * 60 * 25, 0.05, dtype=np.float32)
    backlog[16_000 * 20:16_000 * 24] = 0.0                  # 20～24 秒有停頓（12 秒窗外）
    backlog[-16_000 * 3:] = 0.0                             # 整段最後 3 秒也安靜
    win.recorder.set_block_listener = lambda fn: None
    win.recorder.get_buffer_snapshot = lambda: backlog
    win._stream_samples = 0
    win._stream_dispatched = 0
    win.STREAM_MIN_ENABLE_SAMPLES = 12 * 16_000
    win.cfg = types.SimpleNamespace(chunk_cut_mode="vad_aligned")
    chunks = []
    win._dispatch_stream_chunk = lambda chunk: chunks.append(len(chunk))
    monkeypatch.setattr(gui, "log_action", lambda *a, **k: None)
    gui.AppWindow._stream_tick(win)
    assert chunks == [gui.AppWindow.STREAM_HARD_CAP_SAMPLES], \
        "12 秒內沒有停頓就切在 12 秒；不能跑去找 20 秒或整段最後的停頓"
