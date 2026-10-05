"""蘋果辨識「邊錄邊辨識」的 Python 端（v2.32.0）。

背景：舊做法是錄音每 10～12 秒切一段、每段開一次 Swift 小程式、放開熱鍵後才轉最後一段。
2026-10-05 審查：放開後辨識等待中位數 468 ms，38 場錄音開了 337 次程式，9.4% 的切點硬切在
句子中間。改成一支常駐的 `whisperpro-apple-stt --serve`，錄音當下每 0.1 秒把聲音餵進去，
放開熱鍵只剩引擎收尾（合成語音實測中位數 170 ms，跟錄音長度無關；錯字率 1.4% vs 舊做法 1.7%）。

分工：
  AppleStreamEngine   管那支常駐程式（整個 App 共用一支；死了就重開）
  AppleStreamSession  一次錄音一個：錄音執行緒丟聲音進佇列，背景執行緒負責送出與收結果

任何一步失敗，session.finish() 回 None，呼叫端退回舊的「整段送一次」做法——使用者不會卡住。
"""

from __future__ import annotations

import base64
import collections
import json
import os
import queue
import subprocess
import threading
import time
from typing import Optional

import numpy as np

from logger import get_logger, log_error

log = get_logger("whisper_pro.apple_stream")


class _Channel:
    """跟一支常駐程式之間的連線。程式重開就換一條新的——舊 session 拿著舊連線，
    只會讀到「已關閉」然後結束，不會跑去讀新程式的事件。"""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self.events: queue.Queue = queue.Queue()
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        try:
            for line in self.proc.stdout:
                try:
                    self.events.put(json.loads(line))
                except Exception:
                    pass
        except Exception:
            pass
        self.events.put({"event": "_closed"})

    def _drain_stderr(self) -> None:
        # stderr 一定要讀走：管線塞滿時對方寫 stderr 會卡住，整支程式跟著停
        try:
            for line in self.proc.stderr:
                text = line.decode("utf-8", "replace").strip()
                if text:
                    log.info(f"APPLE_STREAM: helper: {text[:200]}")
        except Exception:
            pass

    def alive(self) -> bool:
        return self.proc.poll() is None

    def send(self, obj: dict) -> bool:
        try:
            self.proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
            return True
        except Exception:
            return False

    def wait(self, names: set, timeout: float) -> Optional[dict]:
        """等指定的事件；途中收到的其他事件（例如上一次逾時才回來的結果）直接略過。"""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                ev = self.events.get(timeout=remaining)
            except queue.Empty:
                return None
            name = ev.get("event")
            if name == "_closed":
                return None
            if name in names:
                return ev

    def kill(self) -> None:
        try:
            self.proc.kill()
        except Exception:
            pass


class AppleStreamEngine:
    """整個 App 共用的一支常駐程式。"""

    # 保險絲：最近 5 次裡失敗 3 次，就先停用一段時間。失敗本身不會丟字（會退回整段送一次），
    # 但「程式活著卻不回話」時每次錄音都要白等好幾秒才退回（2026-10-05 審查用假程式量到）——
    # 不停用的話，使用者每一句都多等這麼久。
    # 看「最近幾次」而不是「連續幾次」：審查實測，只有長錄音才卡住、短的正常時，連續計數會被
    # 短錄音的成功一直歸零，永遠不會停用（交替 8 次多等了 32 秒）。
    # 用冷卻時間而不是停到重開 App：使用者常常連開好幾天（最長 68 小時），偶發的問題不該
    # 讓他好幾天都用不到串流。冷卻完的第一次是「試用」：成功就清掉舊帳、回到正常；失敗就馬上
    # 再停。不清舊帳的話，冷卻前的失敗還留在最近 5 次裡，恢復正常後偶發一次失敗又停 10 分鐘
    # （第三輪審查）。
    RECENT_WINDOW = 5
    FAIL_LIMIT = 3
    COOLDOWN_S = 600.0

    def __init__(self, helper_path: str):
        self._path = helper_path
        self._channel: Optional[_Channel] = None
        self._lock = threading.Lock()
        self._recent: collections.deque = collections.deque(maxlen=self.RECENT_WINDOW)
        self._cooldown_until = 0.0
        self._probation = False

    def available(self) -> bool:
        """冷卻中回 False：呼叫端這次就不建 session，直接走舊做法。"""
        return time.monotonic() >= self._cooldown_until

    def note_result(self, ok: bool, reason: str = "") -> None:
        if self._probation:
            self._probation = False
            if ok:
                self._recent.clear()
            else:
                self._trip(f"failed again after cooldown ({reason})")
            return
        self._recent.append(ok)
        fails = list(self._recent).count(False)
        if not ok and fails >= self.FAIL_LIMIT:
            self._trip(f"{fails} of last {len(self._recent)} sessions failed (last={reason})")

    def _trip(self, why: str) -> None:
        self._cooldown_until = time.monotonic() + self.COOLDOWN_S
        self._probation = True
        log.warning(f"APPLE_STREAM: {why}, pausing streaming for {self.COOLDOWN_S:.0f}s")

    def channel(self) -> Optional[_Channel]:
        """拿到可用的連線；程式沒在跑就開一支（約 0.2 秒，App 暖機時先開好）。"""
        with self._lock:
            if self._channel is not None and self._channel.alive():
                return self._channel
            if not os.path.exists(self._path):
                return None
            try:
                proc = subprocess.Popen(
                    [self._path, "--serve"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    bufsize=0,   # 不緩衝：每一塊聲音寫進去就要立刻送到對面
                )
            except Exception:
                log_error("apple_stream_spawn_failed")
                return None
            ch = _Channel(proc)
            if ch.wait({"hello"}, timeout=5.0) is None:
                ch.kill()
                log_error("apple_stream_no_hello")
                return None
            self._channel = ch
            log.info("APPLE_STREAM: helper started")
            return ch

    def reset(self, ch: Optional[_Channel] = None) -> None:
        """出過錯就整支重開：避免對面還卡著上一次沒收完的狀態。

        ch：出錯的那一條。只砍它——如果引擎已經換了新的一支（下一次錄音拿去用了），新的不能
        一起砍掉（第三輪審查：舊 session 晚一步呼叫 reset，會把下一次錄音的程式砍掉）。
        不給就砍目前這支。"""
        with self._lock:
            target = ch if ch is not None else self._channel
            if target is not None:
                target.kill()
            if self._channel is target:
                self._channel = None


_END = object()
_ABORT = object()


class AppleStreamSession:
    """一次錄音。建立時就開始在背景準備引擎；錄音期間 push()，放開後 finish()。"""

    def __init__(self, engine: AppleStreamEngine, locale: str):
        self._engine = engine
        self._locale = locale
        self._q: queue.Queue = queue.Queue()
        self._done = threading.Event()
        self._result: Optional[dict] = None
        self.failure: Optional[str] = None
        self.prepare_ms: Optional[float] = None
        # 交出去幾個樣本、實際送出幾個：兩者差距 = 卡在佇列裡還沒送到對面的聲音
        self._pushed = 0
        self._sent = 0
        self._ch: Optional[_Channel] = None
        self._last_progress: Optional[float] = None   # 引擎準備好之後才開始算
        self._reported = False
        self._report_lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    def _report(self, ok: bool, reason: str = "") -> None:
        # 一次錄音只算一次：finish() 逾時後，背景執行緒稍後還會再失敗一次（連線被砍）
        with self._report_lock:
            if self._reported:
                return
            self._reported = True
        self._engine.note_result(ok, reason)

    def push(self, block) -> None:
        """錄音即時執行緒呼叫——只能丟佇列，任何會卡住的事都不能在這裡做。
        引擎還在準備時送進來的聲音也先排在佇列裡，準備好之後補送，一塊都不會漏。"""
        if not self._done.is_set():
            self._pushed += len(block)
            self._q.put_nowait(block)

    # 「卡住」= 引擎準備好之後，有超過 1 秒的聲音排著、卻已經這麼久一塊都送不出去。
    # 對面正常時，600 秒的聲音一口氣灌進去 1.2 秒就收完（第三輪審查實測），送不出去幾秒
    # 就是對面不讀了。錄音中用 8 秒（誤判的代價是這次放棄串流，寧可保守；審查實測對面停 5 秒
    # 還能自己恢復）；放開之後用 3 秒（再等下去本來就只是在等逾時）。
    # 準備好之前不算：那段聲音本來就會先排隊，真的卡住由準備逾時（5 秒）處理。
    STALL_RECORDING_S = 8.0
    STALL_FINISH_S = 3.0

    def _stalled(self, limit_s: float) -> bool:
        if self._last_progress is None:
            return False
        backlog_s = (self._pushed - self._sent) / 16_000
        return backlog_s > 1.0 and time.monotonic() - self._last_progress > limit_s

    def _mark_stalled(self) -> None:
        # 主執行緒呼叫，不能等：重開程式要拿引擎的鎖，別的執行緒可能正拿著它等程式開好（最多 5 秒）
        self.failure = self.failure or "stalled"
        log.warning("APPLE_STREAM: helper stopped taking audio, will fall back")
        self._report(False, "stalled")
        self._done.set()
        threading.Thread(target=self._engine.reset, args=(self._ch,), daemon=True).start()

    def check_alive(self) -> bool:
        """錄音中每秒問一次（主執行緒）：還能用就回 True。

        失敗過（程式掛了、準備失敗）回 False；程式活著但不收聲音也回 False 並標記失敗——
        這種情況程式不會自己報錯，不檢查的話要等到放開熱鍵、收尾逾時才發現
        （2026-10-05 審查：用假程式重現，佇列每秒長 10 塊、失敗旗標一直是空的）。
        """
        if self.failure is not None:
            return False
        if self._stalled(self.STALL_RECORDING_S):
            self._mark_stalled()
            return False
        return True

    def finish(self, timeout: float) -> Optional[dict]:
        """錄音結束：等引擎收尾、拿結果。回 None = 失敗或逾時，呼叫端要退回舊做法。

        等的時候順便看對面是不是卡住了：放開前幾秒才卡住的，錄音中還來不及發現，
        不看的話要白等到逾時（第三輪審查：最少 8 秒，30 分鐘的錄音要等更久）。"""
        self._q.put(_END)
        deadline = time.monotonic() + timeout
        while not self._done.wait(0.1):
            if self._stalled(self.STALL_FINISH_S):
                self._mark_stalled()
                return None
            if time.monotonic() >= deadline:
                self.failure = self.failure or "finish_timeout"
                self._report(False, self.failure)
                self._engine.reset(self._ch)
                return None
        return self._result

    def abort(self) -> None:
        """錄音被取消（麥克風打不開、App 關閉…）。"""
        self._q.put(_ABORT)

    def _fail(self, reason: str, *, reset: bool = True) -> None:
        # 第一個原因才是真的原因：例如判定卡住後砍掉程式，送聲音那邊接著也會失敗
        self.failure = self.failure or reason
        log.warning(f"APPLE_STREAM: session failed ({reason}), will fall back to one-shot")
        # 不算進保險絲的：使用者取消、一塊聲音都沒收到——都不是程式出問題
        if reason not in ("aborted", "end:empty_audio"):
            self._report(False, reason)
        if reset:
            self._engine.reset(self._ch)
        self._done.set()

    def _complete(self, ev: dict) -> bool:
        """對面收到的聲音跟這邊送出的一樣多。容許 0.25 秒（約一個字）誤差，欄位缺了就不擋。"""
        if (ev.get("bad_chunks") or 0) > 0:
            return False
        got = ev.get("audio_seconds")
        return got is None or float(got) >= self._sent / 16_000 - 0.25

    def _run(self) -> None:
        try:
            ch = self._engine.channel()
            if ch is None:
                self._fail("helper_unavailable", reset=False)
                return
            self._ch = ch
            if not ch.send({"cmd": "begin", "locale": self._locale}):
                self._fail("send_failed")
                return
            ev = ch.wait({"ready", "error"}, timeout=5.0)
            if ev is None or ev.get("event") != "ready":
                # asset_not_installed 這類錯誤不是程式壞掉，不必重開
                self._fail(f"begin:{(ev or {}).get('error', 'timeout')}",
                           reset=ev is None)
                return
            self.prepare_ms = ev.get("prepare_ms")
            self._last_progress = time.monotonic()
            while True:
                item = self._q.get()
                if item is _ABORT:
                    ch.send({"cmd": "cancel"})
                    ch.wait({"cancelled"}, timeout=3.0)
                    self._fail("aborted", reset=False)
                    return
                if item is _END:
                    if not ch.send({"cmd": "end"}):
                        self._fail("send_failed")
                        return
                    # 這裡的等待上限只是保險絲；真正的上限由 finish(timeout) 決定
                    ev = ch.wait({"result"}, timeout=120.0)
                    if ev is None:
                        self._fail("no_result")
                        return
                    if not ev.get("ok"):
                        self._fail(f"end:{ev.get('error')}", reset=False)
                        return
                    if not self._complete(ev):
                        # 對面有聲音沒收到（解碼或格式轉換失敗會被它跳過）卻回「成功」——用了會少字。
                        # 算成失敗：退回整段送一次，也讓保險絲知道這支程式有問題
                        # （之前是轉錄端才發現、丟掉結果，這裡卻已經回報成功，保險絲永遠不會跳）。
                        self._fail("incomplete", reset=False)
                        return
                    self._result = ev
                    self._report(True)
                    self._done.set()
                    return
                pcm = base64.b64encode(np.asarray(item, dtype=np.float32).tobytes()).decode("ascii")
                if not ch.send({"cmd": "audio", "pcm": pcm}):
                    self._fail("send_failed")
                    return
                self._sent += len(item)
                self._last_progress = time.monotonic()
        except Exception:
            log_error("apple_stream_session_crashed")
            self._fail("exception")
