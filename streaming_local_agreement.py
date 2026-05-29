"""
v2.19.x LocalAgreement-2 streaming ASR buffer。

設計動機
========
v2.16.0 的 fixed-chunk streaming 把 audio 切成固定 10s chunk、每段獨立轉、最後
concatenate。兩個根本問題：
  1. **chunk 邊界切碎**：一句話跨在 chunk N 與 chunk N+1 之間時、ASR 缺前後文，
     兩段 chunk 各自轉出殘缺結果，合併後語意斷裂或重複（例：「全自動、自行、
     自行。」chunk N 結尾「自行」、chunk N+1 開頭也跑出「自行」）。
  2. **句末殘缺**：chunk 結尾恰好在一句話中間時，VAD / decoder 不知道句子還沒
     講完，直接截斷（例：「呼叫大量的 sub agent，然後同步多頭的進行你的所有
     任務。然後你的所有任務全都全部同。」最後一個「同」是 chunk 結尾被截斷）。

LocalAgreement-2 演算法（Macháček et al. 2023, Whisper-Streaming）：

    每次新 audio 進來，**重轉「未 commit 的尾段」全部**，跟上一輪的 hypothesis
    取最長公共前綴（LCP）作為「兩輪 ASR 都同意」的穩定部分 → commit。
    未在 LCP 內的尾部 = 兩輪解碼分歧（通常因 ASR 還在猶豫句末詞）→ 留到下輪。

    錄音結束時 finalize：把所有未 commit 的尾段直接轉、不再等下一輪 LCP（因為
    沒有「下一輪」）。

優點：
  • Chunk 邊界自然消失——每次重轉都有完整 context 到「目前最尾端」
  • 句末殘缺自動被「等下一輪 LCP」修掉（再加幾秒 audio、ASR 解碼會更完整）
  • 重複病 (邊界 N-gram 重複) 因不再 concatenate 獨立 chunk 而消失

代價：
  • CPU 開銷比 fixed-chunk 大（每次重轉未 commit 尾段、不是只轉新增 audio）
  • 需要 backend 回 segments（時間戳）才能反推 audio 邊界——Qwen3-ASR 跟
    mlx_whisper 都支援

執行緒模型
==========
公開方法（add_audio / process_tick / finalize / reset）都用 self._lock 保護。
typical caller pattern（見 gui.py `_stream_tick_la`）：
  • UI 執行緒呼叫 add_audio（沒 ASR 推論、純 append）
  • 背景執行緒呼叫 process_tick（會跑 ASR、阻塞）
  • UI 執行緒呼叫 finalize（背景跑、要等所有 in-flight process_tick 完）

公開 API
========
  LocalAgreementBuffer(transcriber, language, model_size, chinese_variant, ...)
    add_audio(new_audio)              # 累積到 buffer，O(1)
    process_tick() -> str             # 重轉尾段、做 LCP、回傳「本輪新 commit」
    finalize() -> str                 # 強制 commit 所有 tail、回傳完整 commit
    reset()                           # 清空狀態給下次錄音用
    @property committed_text          # 目前已 commit 的全部文字（不含 tail）

模組級 helper：
  _longest_common_prefix(a, b) -> str       # char-level LCP，中英混講友好
  _audio_position_for_text(segments, prefix, duration_s) -> float
                                            # 用 segments 反推 prefix 結束的時間
"""
from __future__ import annotations

import threading
import time
from typing import Optional

import numpy as np

from logger import get_logger, log_error

log = get_logger("la_stream")

# audit_log 在 transcriber.py 是 _audit_log；這裡同樣 best-effort import
try:
    import audit_log as _audit_log
except Exception:
    _audit_log = None

# pipeline_id 取 current id 給 audit 用（noop on import failure）
try:
    import pipeline_id as _pipeline_id
except Exception:
    _pipeline_id = None


def _safe_pid() -> str:
    """安全取 current pipeline ID；模組缺席或拋例外都回空字串。"""
    if _pipeline_id is None:
        return ""
    try:
        return _pipeline_id.get_current() or ""
    except Exception:
        return ""


def _safe_event(event_type: str, **fields) -> None:
    """安全寫 audit event；失敗 silent。"""
    if _audit_log is None:
        return
    try:
        _audit_log.write_event(event_type, _safe_pid(), **fields)
    except Exception:
        try:
            log_error("la_audit_failed", event=event_type)
        except Exception:
            pass


# ── LCP helper ────────────────────────────────────────────────────────────────

def _longest_common_prefix(a: str, b: str) -> str:
    """Char-level LCP（中英混講友好——中文沒 word boundary、word-level LCP 無意義）。

    Example:
        _longest_common_prefix("我們在轉錄這段", "我們在轉錄那段") -> "我們在轉錄"
        _longest_common_prefix("hello world", "hello there")     -> "hello "
    """
    if not a or not b:
        return ""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return a[:i]


# ── audio position 反推 ───────────────────────────────────────────────────────

def _audio_position_for_text(
    segments: list[dict],
    text_prefix: str,
    duration_s: float,
) -> float:
    """用 segments 反推「text_prefix 結束」對應的 audio 時間（秒）。

    策略：
      1. 走 segments，累積每個 segment 的 text 長度；直到累積長度 ≥ len(text_prefix)
         就視為 prefix 落在這個 segment 結尾，回傳 segment["end"]。
      2. segments 為空 / 走完還沒滿足 → fallback 用「字元比例 × duration_s」估算。
      3. text_prefix 為空 → 回 0.0（什麼都還沒 commit）。

    注意：transcriber.transcribe() 回的 result.text 是 join(segments[*].text) 之後
    額外 .strip() 的結果，逐 segment 累積長度跟 result.text 的 prefix 長度未必
    1:1 對應（segment 之間可能有 strip 掉的 whitespace）。我們不為這幾個字
    精確算位置——LocalAgreement 只需要「足夠接近」的 audio 邊界，少切幾百 ms
    audio 不會炸（會在下輪 process_tick 重轉），所以策略 1 用 ≥ 寬鬆判斷。

    Args:
        segments: TranscriptionResult.segments，每個 dict 含 {start, end, text}
        text_prefix: 已 agreed / 已 commit 的文字
        duration_s: 整段 audio 長度（fallback 用）

    Returns:
        對應 audio 時間（秒）；越接近 prefix 真正結束位置越好、但保證 ≤ duration_s
    """
    if not text_prefix:
        return 0.0

    target_len = len(text_prefix)
    accumulated = 0

    # 策略 1：逐 segment 累積，直到 ≥ target_len
    if segments:
        for seg in segments:
            seg_text = seg.get("text", "") or ""
            # 跟 transcriber 一致用 .strip() 計長度（避免 leading/trailing whitespace
            # 跟 result.text 的計算方式不一致）
            accumulated += len(seg_text.strip())
            if accumulated >= target_len:
                end = float(seg.get("end", 0.0) or 0.0)
                # 邊界保護：segments 時間戳偶爾會超過實際 audio 長度（mlx_whisper
                # 內部 30s padding），clamp 到 duration_s
                return min(end, duration_s)

    # 策略 2：fallback 字元比例
    # 這只在 segments 為空（backend 沒回時間戳）或 prefix 比所有 segment 加起來
    # 還長（不該發生、但 defensive）時觸發
    # 取 result.text 總長近似估計
    return min(duration_s, duration_s)  # 全部 audio 都算「已對應」、防 underflow


# ── 主類別 ────────────────────────────────────────────────────────────────────

class LocalAgreementBuffer:
    """LocalAgreement-2 streaming ASR buffer。

    狀態：
      _audio_buffer：np.float32、累積整段錄音（add_audio 不斷 append）
      _committed_text：已 commit（兩輪 ASR LCP 同意）的文字
      _committed_audio_end_s：已 commit 對應的 audio 時間結束點（秒）
      _previous_hypothesis：上一輪 process_tick 對未 commit 尾段轉出來的結果
                            （注意：是「未 commit 尾段」的 transcription、不是整段
                            audio 的——所以隨著 commit 前進、起點會跟著前進）
    """

    def __init__(
        self,
        transcriber,
        language: Optional[str],
        model_size: str,
        chinese_variant: str = "off",
        min_chunk_samples: int = 5 * 16_000,     # 未 commit 尾段至少 5s 才重轉
        max_buffer_samples: int = 120 * 16_000,  # audio buffer 上限 120s
        sample_rate: int = 16_000,
    ) -> None:
        self._transcriber = transcriber
        self._language = language
        self._model_size = model_size
        self._chinese_variant = chinese_variant
        self._min_chunk_samples = int(min_chunk_samples)
        self._max_buffer_samples = int(max_buffer_samples)
        self._sample_rate = int(sample_rate)

        # 狀態（lock 保護）
        self._lock = threading.Lock()
        self._audio_buffer: list[np.ndarray] = []   # list of float32 chunks
        self._audio_buffer_samples: int = 0          # 快取長度避免每次 sum
        self._committed_text: str = ""
        self._committed_audio_end_s: float = 0.0
        self._previous_hypothesis: str = ""
        # 統計（觀測用）
        self._tick_count: int = 0
        self._commit_count: int = 0
        self._overflow_count: int = 0

    # ── 狀態查詢 ──────────────────────────────────────────────────────────────

    @property
    def committed_text(self) -> str:
        """目前已 commit 的全部文字（不含尚未 agreed 的 tail）。"""
        with self._lock:
            return self._committed_text

    @property
    def buffer_seconds(self) -> float:
        """累積 audio 長度（秒）——觀測用。"""
        with self._lock:
            return self._audio_buffer_samples / self._sample_rate

    # ── 公開 API ─────────────────────────────────────────────────────────────

    def add_audio(self, new_audio) -> None:
        """累積新 audio 到 buffer。O(1)、不跑 ASR。

        Args:
            new_audio: np.ndarray float32 / int16、16 kHz mono
        """
        if new_audio is None or len(new_audio) == 0:
            return
        arr = np.asarray(new_audio)
        if arr.dtype != np.float32:
            arr = arr.astype(np.float32)
            # int16 已被放大、還原到 [-1, 1]（跟 transcriber.transcribe 入口一致）
            if arr.size and arr.max() > 1.0:
                arr = arr / 32768.0
        with self._lock:
            self._audio_buffer.append(arr)
            self._audio_buffer_samples += len(arr)

    def process_tick(self) -> str:
        """重轉未 commit 尾段、做 LCP、commit、回傳「本輪新 commit」。

        關鍵流程：
          1. 取出未 commit 尾段（committed_audio_end_s 之後的 audio）
          2. 太短（< min_chunk_samples）→ 直接 return ""
          3. buffer 過大（> max_buffer_samples）→ force commit + 重置 hypothesis
             (避免 buffer 無限累積；user 講超過 2 分鐘就 force flush)
          4. transcribe() 跑完整尾段
          5. 跟上輪 hypothesis 取 LCP = agreed
          6. agreed 比上輪 hypothesis 在 commit 之外多出來的部分 = new_commit
          7. 用 segments 反推 agreed 對應的 audio 結束位置 → 更新 committed_audio_end_s
          8. previous_hypothesis = 本輪 hypothesis（給下輪用）

        Returns:
            本輪新 commit 的文字（空字串 = 沒新 commit、繼續等下輪）
        """
        with self._lock:
            self._tick_count += 1
            tick_num = self._tick_count
            buf_samples = self._audio_buffer_samples
            committed_end_s = self._committed_audio_end_s
            prev_hyp = self._previous_hypothesis
            already_committed = self._committed_text

            # 不夠長 → skip（避免短 audio 觸發短音檔閘把 result 變成「未偵測到」）
            committed_end_samples = int(committed_end_s * self._sample_rate)
            tail_samples = buf_samples - committed_end_samples
            if tail_samples < self._min_chunk_samples:
                return ""

            # buffer overflow → force commit 整段尾段、reset hypothesis
            overflow = buf_samples > self._max_buffer_samples
            # 拼接 audio buffer（持 lock 內做、避免併發 add_audio 改了 list）
            full_audio = np.concatenate(self._audio_buffer) if self._audio_buffer else np.zeros(0, dtype=np.float32)
            # 釋放 lock 跑 ASR——transcribe() 自己有 _transcription_lock 序列化、
            # 但我們不想持 self._lock 卡住 add_audio
        # lock 釋放後跑 ASR
        try:
            process_audio = full_audio[committed_end_samples:]
            duration_s = len(process_audio) / self._sample_rate
            t0 = time.perf_counter()
            r = self._transcriber.transcribe(
                process_audio,
                model_size=self._model_size,
                language=self._language,
                chinese_variant=self._chinese_variant,
            )
            elapsed_s = time.perf_counter() - t0
        except Exception as e:
            log_error("la_tick_transcribe_failed", tick=tick_num, error=str(e))
            return ""

        # transcribe 失敗 / gate 擋下 → text 開頭是「（」（例：「（未偵測到語音內容）」）
        # → 視為「沒新 hypothesis」、不更新 previous_hypothesis、跳出
        hyp = (r.text or "").strip()
        is_gate_msg = hyp.startswith("（")
        if is_gate_msg:
            hyp = ""

        # LCP：跟上輪 hypothesis 取最長公共前綴 = 兩輪都同意
        agreed = _longest_common_prefix(hyp, prev_hyp)

        new_commit = ""
        new_committed_audio_end_s = committed_end_s

        if overflow:
            # Force commit 全部尾段、reset hypothesis（下輪重新累積 prev_hyp）
            new_commit = hyp
            new_committed_audio_end_s = duration_s + committed_end_s
            with self._lock:
                self._overflow_count += 1
            _safe_event(
                "la_overflow",
                tick=tick_num,
                buffer_s=buf_samples / self._sample_rate,
                forced_commit_len=len(new_commit),
            )
            log.warning(
                f"LA: buffer overflow tick={tick_num} buf={buf_samples/self._sample_rate:.1f}s "
                f"force_commit_len={len(new_commit)}"
            )
        elif agreed:
            # agreed 長度若 ≤ already_committed_in_prev_hypothesis、表示 LCP 還沒
            # 超過上輪已 commit 的部分（不可能、因為上輪 commit 已從 prev_hyp 剝離）
            # 但保險絲：new_commit 為負時取空字串
            #
            # 注意 prev_hypothesis 是「對 committed 之後尾段的轉錄結果」（committed
            # 完成那刻當下的 tail），它在被 commit 出去之前還沒從 prev_hypothesis
            # 剝離；下一輪 process_tick 時、committed_end 已往後移、prev_hypothesis
            # 就重新 assign 為本輪的 hyp。所以「上輪 commit 已涵蓋的部分」並不在
            # prev_hypothesis 裡——直接拿 agreed 當 new_commit 即可。
            new_commit = agreed
            # 用 segments 反推 agreed 結束位置（相對 process_audio）
            audio_offset_s = _audio_position_for_text(
                r.segments or [], agreed, duration_s,
            )
            new_committed_audio_end_s = committed_end_s + audio_offset_s
        # else：兩輪沒有公共前綴 → 還沒同意、不 commit，留到下輪

        # 寫回狀態
        with self._lock:
            if new_commit:
                self._committed_text += new_commit
                self._committed_audio_end_s = new_committed_audio_end_s
                self._commit_count += 1
            # 更新 previous_hypothesis 給下輪用——關鍵 subtlety：
            #   commit 後 audio cursor 已往後移到 agreed 結尾位置；下輪 ASR 看到的是
            #   「agreed 之後的 audio」（= 本輪 hypothesis 在 agreed 之後的部分 +
            #   下輪 add_audio 進來的新 audio）。所以 prev_hypothesis 應該是「本輪
            #   hypothesis 扣掉 agreed 已 commit 的部分」、不是整段 hypothesis——
            #   否則 LCP 會把已 commit 的文字再 commit 一次（double commit bug）。
            if overflow:
                # overflow 已 force commit 全部 hyp、prev 必須清空
                self._previous_hypothesis = ""
            elif new_commit:
                # commit 過 → prev 取 hypothesis 在 agreed 之後的尾段
                self._previous_hypothesis = hyp[len(new_commit):]
            else:
                # 沒 commit → cursor 沒動、整段 hypothesis 留給下輪比 LCP
                self._previous_hypothesis = hyp

        # audit log
        _safe_event(
            "la_chunk_processed",
            tick=tick_num,
            buffer_s=buf_samples / self._sample_rate,
            tail_s=tail_samples / self._sample_rate,
            elapsed_s=round(elapsed_s, 3),
            hypothesis_len=len(hyp),
            prev_hypothesis_len=len(prev_hyp),
            agreed_len=len(agreed),
            new_commit_len=len(new_commit),
            gate_msg=is_gate_msg,
            overflow=overflow,
        )
        if new_commit:
            _safe_event(
                "la_commit",
                tick=tick_num,
                committed_text_len=len(already_committed) + len(new_commit),
                new_commit_len=len(new_commit),
                audio_position_s=round(new_committed_audio_end_s, 2),
            )
            log.info(
                f"LA: commit tick={tick_num} new={len(new_commit)} chars "
                f"total={len(already_committed) + len(new_commit)} "
                f"audio_pos={new_committed_audio_end_s:.1f}s"
            )

        return new_commit

    def finalize(self) -> str:
        """結束錄音、強制 commit 剩下未 commit 的所有 tail、回傳完整 committed_text。

        策略：把 committed_audio_end_s 之後的 audio 整段轉一次、直接 append 到
        committed_text（不再等下一輪 LCP——已沒有下一輪了）。

        Returns:
            完整 committed_text（含 finalize 補上的 tail）
        """
        with self._lock:
            buf_samples = self._audio_buffer_samples
            committed_end_samples = int(self._committed_audio_end_s * self._sample_rate)
            tail_samples = buf_samples - committed_end_samples
            # buffer 拼起來給 ASR——持 lock 內 concat 確保 add_audio race 安全
            full_audio = np.concatenate(self._audio_buffer) if self._audio_buffer else np.zeros(0, dtype=np.float32)
            already_committed = self._committed_text

        # 沒 tail（已全 commit）→ 直接回 committed_text
        if tail_samples <= 0:
            _safe_event(
                "la_finalize",
                total_committed_len=len(already_committed),
                tail_len=0,
                tail_s=0.0,
            )
            log.info(f"LA: finalize (no tail) total={len(already_committed)} chars")
            return already_committed

        # tail 太短（< 0.5s）跳過 ASR、避免短音檔閘把它變成「（未偵測到）」
        tail_s = tail_samples / self._sample_rate
        if tail_s < 0.5:
            _safe_event(
                "la_finalize",
                total_committed_len=len(already_committed),
                tail_len=0,
                tail_s=tail_s,
                tail_skipped="too_short",
            )
            log.info(
                f"LA: finalize (tail {tail_s:.2f}s < 0.5s, skipped) "
                f"total={len(already_committed)} chars"
            )
            return already_committed

        # 跑最後一次 ASR、結果整段當 commit 加上去
        tail_audio = full_audio[committed_end_samples:]
        try:
            r = self._transcriber.transcribe(
                tail_audio,
                model_size=self._model_size,
                language=self._language,
                chinese_variant=self._chinese_variant,
            )
            tail_text = (r.text or "").strip()
            # 排除 gate / hallucination message
            if tail_text.startswith("（"):
                tail_text = ""
        except Exception as e:
            log_error("la_finalize_transcribe_failed", error=str(e))
            tail_text = ""

        with self._lock:
            self._committed_text += tail_text
            # 把 audio_end 推到底（finalize 後沒人會再 process_tick）
            self._committed_audio_end_s = buf_samples / self._sample_rate
            final_text = self._committed_text

        _safe_event(
            "la_finalize",
            total_committed_len=len(final_text),
            tail_len=len(tail_text),
            tail_s=round(tail_s, 2),
        )
        log.info(
            f"LA: finalize tail_s={tail_s:.2f} tail_len={len(tail_text)} "
            f"total={len(final_text)} chars"
        )
        return final_text

    def reset(self) -> None:
        """清空所有狀態給下次錄音用。"""
        with self._lock:
            self._audio_buffer = []
            self._audio_buffer_samples = 0
            self._committed_text = ""
            self._committed_audio_end_s = 0.0
            self._previous_hypothesis = ""
            self._tick_count = 0
            self._commit_count = 0
            self._overflow_count = 0
        log.info("LA: reset")
