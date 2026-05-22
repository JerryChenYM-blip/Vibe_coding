"""
Whisper 語音轉文字封裝器。

後端自動偵測（啟動時判斷一次）：
  • Apple Silicon (arm64) + mlx_whisper 已安裝 → mlx-whisper（Metal GPU / Neural Engine）
  • 其他情況                                   → faster-whisper（CPU int8）

模型快取位置：~/.cache/huggingface/hub/（兩種後端共用同一目錄）

防護機制：
  • 靜音偵測：RMS < _MIN_RMS 直接跳過推論，避免空白音訊觸發幻覺
  • 幻覺過濾：比對已知幻覺字串列表，比對到就替換為「未偵測到語音」
"""

from __future__ import annotations

import gc
import os
import platform
import time
import threading
from dataclasses import dataclass, field
from typing import Optional

import prompts

from logger import get_logger, log_error

log = get_logger("transcriber")


# ── 後端偵測 ──────────────────────────────────────────────────────────────────

def _detect_backend() -> str:
    """偵測最佳推論後端：Apple Silicon + mlx 已安裝則用 mlx，否則用 ctranslate2。

    Returns:
        "mlx" 或 "ctranslate"。
    """
    if platform.machine() != "arm64":
        return "ctranslate"   # 非 Apple Silicon，只能跑 CPU 版
    try:
        import mlx_whisper  # noqa: F401 — 只測試能否 import
        return "mlx"
    except ImportError:
        return "ctranslate"   # mlx_whisper 未安裝，退而求其次


# 模組載入時偵測一次，整個 session 共用
BACKEND = _detect_backend()

# faster-whisper 模型名稱 → mlx-community HuggingFace 倉庫 ID 的映射
# 注意：tiny/base 的 `mlx-community/whisper-tiny`、`whisper-base` repo 已於
# 2026 年改名/下架，會回 401。全部統一使用 `-mlx-4bit` 量化版本；若 MLX 載入
# 失敗，transcribe() 會自動 fallback 到 CTranslate2 CPU。
_MLX_MODEL_MAP: dict[str, str] = {
    "tiny":           "mlx-community/whisper-tiny-mlx-4bit",
    "base":           "mlx-community/whisper-base-mlx-4bit",
    "small":          "mlx-community/whisper-small-mlx-4bit",
    "medium":         "mlx-community/whisper-medium-mlx-4bit",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "large-v3":       "mlx-community/whisper-large-v3-mlx-4bit",
}


# ── 靜音偵測與幻覺防護 ────────────────────────────────────────────────────────

# 最小 RMS 閾值：低於此值視為靜音，跳過推論。
# 0.002 是實測下限：可抓「嗯」「好」等短促輕音；再低就會把真靜音也送進去引發幻覺。
_MIN_RMS = 0.002

# 已知 Whisper 幻覺字串（模型在靜音或噪音上常輸出這些）
# 比對時不分大小寫、去除空白
_HALLUCINATIONS: tuple[str, ...] = (
    "作詞", "作曲", "李宗盛",          # 常見中文歌曲幻覺
    "字幕由", "amara",                  # 字幕工具幻覺
    "請訂閱", "訂閱頻道", "謝謝收看",   # YouTube 幻覺
    "敬請期待",
    "please subscribe", "thanks for watching",
    "subtitles by", "transcript by",
    "請原文保留英文單字", "技術術語",   # 把 initial_prompt 內容幻覺回來
)


def _is_silence(audio) -> bool:
    """判斷音訊 RMS 能量是否低於閾值（視為靜音）。"""
    import numpy as np
    if audio.size == 0:
        return True
    rms = float(np.sqrt(np.mean(audio ** 2)))
    return rms < _MIN_RMS


def _is_hallucination(text: str) -> bool:
    """判斷文字是否包含已知 Whisper 幻覺片段或重複 pattern。

    雙層防護：
      1. 重複 pattern：Whisper 在純噪音/合成音訊卡住時會輸出
         「我先記我先記我先記...」這類同一短 unit 重複 ≥80% 的文字。
         這類即使長度 ≥20 字也要當幻覺。
      2. 短句關鍵字：只對「< 20 字」才用子字串檢查訂閱／字幕／作詞等
         常見幻覺。長句即使含這些關鍵字也視為正當（避免誤殺）。
    """
    if not text:
        return True   # 空字串也算幻覺
    # 去除空白與換行後進行子字串比對
    lower = text.lower().replace(" ", "").replace("\n", "")

    # 第一層：偵測重複 pattern（卡迴圈型幻覺）
    if _is_repetitive(lower):
        return True

    # 第二層：長句（≥ 20 字）免檢關鍵字 — 幾乎不會有 20 字以上的純幻覺
    if len(lower) >= 20:
        return False
    return any(h.lower().replace(" ", "") in lower for h in _HALLUCINATIONS)


def _is_repetitive(text: str) -> bool:
    """偵測 Whisper 卡迴圈產生的重複 pattern。

    判定條件：存在某長度 2–6 字的 unit，重複 ≥ 8 次且佔總文字 ≥ 80%。
    短文字（< 16 字）跳過此檢查（短重複是正常的口語特徵）。
    """
    n = len(text)
    if n < 16:
        return False
    for unit_len in range(2, 7):
        # 從前 5 個位置取 unit 試算（不需 O(n²) 全掃）
        for start in range(min(5, n - unit_len)):
            unit = text[start:start + unit_len]
            if not unit.strip():
                continue
            count = text.count(unit)
            # 重複 ≥ 8 次且 unit × count 佔總長 ≥ 80% → 卡迴圈
            if count >= 8 and count * unit_len / n >= 0.8:
                return True
    return False


def _normalize_volume(audio, target_peak: float = 0.9, min_peak: float = 0.5):
    """若音訊峰值過小（<min_peak）則線性放大到 target_peak。

    避免：使用者小聲說話時峰值過低 → VAD 誤判為靜音 → 整段被丟。
    防爆：放大倍率上限 12x，避免把底噪放大到爆音。
    """
    import numpy as np
    if audio.size == 0:
        return audio
    peak = float(np.abs(audio).max())
    # 峰值太小（純噪音）或已足夠大（正常音量）就不動
    if peak <= 0.005 or peak >= min_peak:
        return audio
    scale = min(target_peak / peak, 12.0)
    return (audio * scale).astype(audio.dtype)


# ── 資料類別 ──────────────────────────────────────────────────────────────────

@dataclass
class TranscriptionResult:
    """單次轉錄的完整結果，包含文字、語言、耗時與 segment 詳情。"""
    text:             str            # 轉錄文字（主要輸出）
    language:         str            # 偵測到的語言代碼（例如 "zh"）
    duration_seconds: float          # 音訊長度（秒）
    elapsed_seconds:  float          # 推論耗時（秒）
    segments:         list[dict] = field(default_factory=list)  # 逐 segment 資訊


# ── 主要類別 ──────────────────────────────────────────────────────────────────

class Transcriber:
    """執行緒安全的 Whisper 轉錄器，支援 lazy model loading。

    自動選擇最佳後端（MLX Metal GPU 或 CTranslate2 CPU int8）。
    模型在第一次 transcribe() 時載入；可透過 warmup() 提前預熱。

    個人字典整合：
        set_dictionary_terms() 更新術語列表，下次轉錄時自動注入
        到 Whisper 的 initial_prompt，提升專有名詞辨識準確度。
    """

    def __init__(self) -> None:
        self._model              = None              # faster-whisper 模型實例（CPU 後端）
        self._loaded_model_size: Optional[str] = None
        self._lock               = threading.Lock()  # 保護模型載入（防止並行載入兩次）
        self._transcription_lock = threading.Lock()  # 防止同時進行兩個轉錄任務
        self._dictionary_terms:  list[str] = []     # 個人字典術語列表

    def set_dictionary_terms(self, terms: list[str]) -> None:
        """更新個人字典術語列表，下次 transcribe() 時注入 initial_prompt。

        Args:
            terms: 術語字串列表，例如 ["Kubernetes", "Whisper Pro"]。
        """
        self._dictionary_terms = list(terms) if terms else []

    def _build_initial_prompt(self) -> str:
        """動態組合傳給 Whisper 的 initial_prompt（支援 prompts.py 熱重載）。

        每次轉錄前呼叫，確保 prompt_reloader 重載後的新 prompt 立即生效。
        """
        try:
            return prompts.format_whisper_prompt(self._dictionary_terms)
        except Exception:
            log_error("format_whisper_prompt_failed")
            return prompts.WHISPER_INITIAL_PROMPT   # 降級回基礎 prompt

    # ── 公開介面 ──────────────────────────────────────────────────────────────

    def transcribe(
        self,
        audio,                     # np.ndarray float32，16 kHz
        model_size: str = "base",
        language: Optional[str] = None,
    ) -> TranscriptionResult:
        """執行完整轉錄（阻塞呼叫，應在背景執行緒執行）。

        Args:
            audio:      float32 numpy 陣列，振幅 [-1.0, 1.0]，16 kHz 單聲道。
            model_size: Whisper 模型大小，例如 "large-v3-turbo"。
            language:   語言代碼（None 代表自動偵測）。

        Returns:
            TranscriptionResult 實例（保證不拋例外）。
        """
        import numpy as np

        # 空音訊：立即回傳錯誤訊息，不進行推論
        if audio is None or len(audio) == 0:
            log.error("WHISPER: Empty audio buffer received.")
            return TranscriptionResult(
                text="（沒有偵測到音訊，請確認麥克風是否正常運作）",
                language="", duration_seconds=0.0, elapsed_seconds=0.0,
            )

        # 確保 float32 格式，並正規化可能超出 [-1, 1] 的 int16 轉換結果
        audio = audio.astype(np.float32)
        if audio.max() > 1.0:
            audio = audio / 32768.0   # int16 最大值，還原到 [-1, 1]

        # 音量自動正規化：小聲說話時避免 VAD 把整段判為靜音
        pre_peak = float(np.abs(audio).max()) if audio.size else 0.0
        audio = _normalize_volume(audio)
        post_peak = float(np.abs(audio).max()) if audio.size else 0.0
        if post_peak > pre_peak * 1.5:
            log.info(
                f"WHISPER: Volume normalized (peak {pre_peak:.3f} → {post_peak:.3f})"
            )

        duration = len(audio) / 16_000
        t0 = time.perf_counter()
        log.info(
            f"WHISPER: Starting transcription. Backend={BACKEND}, Model={model_size}, "
            f"Lang={language}, AudioDuration={duration:.2f}s"
        )

        # 靜音防護：能量太低就直接跳過，避免觸發幻覺
        if _is_silence(audio):
            _peak = float(np.abs(audio).max()) if audio.size else 0.0
            _rms = float(np.sqrt(np.mean(audio ** 2))) if audio.size else 0.0
            log.info(
                f"WHISPER: Guard - Audio peak={_peak:.6f} rms={_rms:.6f} "
                f"below threshold={_MIN_RMS} (duration={duration:.2f}s), skipping inference."
            )
            return TranscriptionResult(
                text="（未偵測到語音內容）",
                language="", duration_seconds=duration,
                elapsed_seconds=time.perf_counter() - t0,
            )

        # 依後端分發推論
        if BACKEND == "mlx":
            try:
                result = self._transcribe_mlx(audio, model_size, language)
            except Exception:
                # MLX 失敗時降級到 CPU，確保功能不中斷
                log_error("mlx_backend_failed", model=model_size)
                log.warning("WHISPER: Falling back to CPU CTranslate2 backend.")
                result = self._transcribe_ctranslate(audio, model_size, language)
        else:
            result = self._transcribe_ctranslate(audio, model_size, language)

        # 填入實際的音訊長度與推論耗時（兩個後端的 _transcribe_* 不填這兩欄）
        result.duration_seconds = duration
        result.elapsed_seconds  = time.perf_counter() - t0
        rtf = (result.elapsed_seconds / duration) if duration > 0 else 0
        log.info(f"WHISPER: Inference finished in {result.elapsed_seconds:.2f}s. RTF={rtf:.3f}")

        # 幻覺過濾：優先逐段過濾（保留合法段），沒 segments 資訊才退回整段檢查
        if result.segments:
            kept = [
                s for s in result.segments
                if not _is_hallucination(s.get("text", "").strip())
            ]
            removed = len(result.segments) - len(kept)
            if removed > 0:
                log.warning(
                    f"WHISPER: Guard - Removed {removed}/{len(result.segments)} "
                    f"hallucination segment(s), kept {len(kept)}."
                )
                result.segments = kept
                if kept:
                    result.text = "".join(s["text"].strip() for s in kept).strip()
                else:
                    result.text = "（未偵測到語音內容）"
        elif _is_hallucination(result.text):
            log.warning(f"WHISPER: Guard - Detected hallucination: '{result.text[:50]}...'")
            result.text = "（未偵測到語音內容）"

        # 記錄轉錄結果（前 100 字，方便未來 debug hallucination / 準確度）
        preview = result.text[:100].replace("\n", " ")
        log.info(f"WHISPER: Result (lang={result.language}) text='{preview}'")

        return result

    def transcribe_fast(
        self,
        audio,
        language: Optional[str] = None,
    ) -> TranscriptionResult:
        """低延遲轉錄，針對短片段（≤ 8 秒）最佳化。

        使用 beam_size=1（貪婪解碼）和較小模型，速度優先於精準度。
        目前主要保留給中段串流（streaming transcription）使用。

        Args:
            audio:    float32 numpy 陣列，16 kHz 單聲道。
            language: 語言代碼（None 代表自動偵測）。
        """
        import numpy as np

        if audio is None or len(audio) == 0:
            return TranscriptionResult(
                text="（沒有偵測到音訊，請確認麥克風是否正常運作）",
                language="", duration_seconds=0.0, elapsed_seconds=0.0,
            )

        audio = audio.astype(np.float32)
        if audio.max() > 1.0:
            audio = audio / 32768.0

        # 音量正規化 — 和 transcribe() 同策略
        audio = _normalize_volume(audio)

        duration = len(audio) / 16_000
        t0 = time.perf_counter()

        # 靜音防護
        if _is_silence(audio):
            return TranscriptionResult(
                text="（未偵測到語音內容）", language="",
                duration_seconds=duration, elapsed_seconds=0.0,
            )

        # 用 "small" 模型做快速推論（在此場景下 small 的速度/品質比最佳）
        model = self._ensure_model("small")

        with self._transcription_lock:
            segments_iter, info = model.transcribe(
                audio,
                language=language,
                beam_size=1,                              # 貪婪解碼，最快
                initial_prompt=self._build_initial_prompt(),
                condition_on_previous_text=False,         # 不依賴前文，避免幻覺傳播
                vad_filter=True,                          # 內建 VAD 過濾靜音
                # 寬鬆 VAD：threshold 降低讓小聲也偵測到；silence 拉長避免句中停頓
                # 被切段；min_speech 降到 50ms 保留短詞。
                vad_parameters={
                    "threshold":              0.3,
                    "min_silence_duration_ms": 800,
                    "min_speech_duration_ms":  50,
                },
                word_timestamps=False,   # 不需要詞級時間戳，省時間
                temperature=0,           # temperature=0 = 確定性輸出
            )
            segments = []
            full_text_parts = []
            for seg in segments_iter:
                segments.append({"start": seg.start, "end": seg.end, "text": seg.text})
                full_text_parts.append(seg.text.strip())

        text = "".join(full_text_parts).strip()
        if not text or _is_hallucination(text):
            text = "（未偵測到語音內容）"

        return TranscriptionResult(
            text=text,
            language=info.language,
            duration_seconds=duration,
            elapsed_seconds=time.perf_counter() - t0,
            segments=segments,
        )

    def warmup(self, model_size: str) -> None:
        """預先載入模型並跑一次靜音片段，暖機完成後第一次錄音不會卡頓。

        Args:
            model_size: 要預熱的模型大小。
        """
        if BACKEND == "mlx":
            self._warmup_mlx(model_size)
        else:
            self._warmup_ctranslate(model_size)

    def unload(self) -> None:
        """釋放模型記憶體（CPU 與 MLX）。App 切換到省電模式時可呼叫。"""
        with self._lock:
            if self._model is not None:
                del self._model
                self._model = None
            self._loaded_model_size = None

        # MLX 後端需要手動清除 Metal GPU 快取
        if BACKEND == "mlx":
            try:
                import mlx.core as mx
                mx.metal.clear_cache()
            except Exception:
                pass

        gc.collect()   # 強制觸發 Python GC，確保記憶體立即釋放

    @staticmethod
    def active_backend() -> str:
        """回傳目前使用的後端名稱（"mlx" 或 "ctranslate"）。"""
        return BACKEND

    # ── MLX 後端（Apple Silicon Metal GPU）────────────────────────────────────

    def _warmup_mlx(self, model_size: str) -> None:
        """MLX 後端暖機：載入模型並跑一次 1.5s 低振幅 sine wave。

        Fix 11 / 2026-05-23 — 為什麼不是 silence：
          原本跑 0.2s zeros，但 mlx_whisper 內建 VAD 偵測到靜音會 fast-skip，
          Metal shader（encoder / decoder / cross-attention 等）**完全沒被編譯**，
          第一次正式錄音仍是 cold path。曾觀察：warmup 完成後立刻錄 1.6s 音檔，
          推論花 12 秒（RTF 7.5）；之後同樣 size 的音檔降到 0.2 秒（RTF 0.05）。
          診斷依據是 5/22 一整天 RTF 都 0.06-0.13，但 5/23 重啟 App 後第一次
          推論 RTF 7.5。

        修法：用 1.5s 440Hz sine wave at amplitude 0.1
          • 通過內建 VAD（有能量 → 不被 fast-skip）
          • 強制走完整 encoder + decoder pipeline → Metal shader 全部編譯
          • 加 beam_size=1 / temperature=0 / condition_on_previous_text=False
            斷掉 temperature fallback chain（避免 sine wave 觸發幻覺重試把
            warmup 自己拖到 10 秒以上）

        Warmup 耗時：~2s → ~5s，但之後第一次正式錄音 RTF 可以維持 ~0.06。
        """
        import mlx_whisper
        import numpy as np
        hf_repo = _MLX_MODEL_MAP.get(model_size, _MLX_MODEL_MAP["large-v3-turbo"])
        sr = 16_000
        t_axis = np.arange(int(sr * 1.5), dtype=np.float32) / sr
        audio  = (0.1 * np.sin(2 * np.pi * 440.0 * t_axis)).astype(np.float32)
        with self._transcription_lock:
            try:
                # Fix 14 / 2026-05-23：mlx_whisper 偵測到 beam_size kwarg 就試圖走
                # beam-search path（即使值是 1），但 mlx_whisper 該 path 還沒實作
                # → NotImplementedError("Beam search decoder is not yet implemented")。
                # 只用 temperature=0 + condition_on_previous_text=False 切斷 fallback chain；
                # 不要傳 beam_size。greedy decode 是 mlx_whisper 預設行為。
                try:
                    mlx_whisper.transcribe(
                        audio,
                        path_or_hf_repo=hf_repo,
                        language="zh",
                        verbose=False,
                        temperature=0,
                        condition_on_previous_text=False,
                    )
                except TypeError:
                    mlx_whisper.transcribe(
                        audio,
                        path_or_hf_repo=hf_repo,
                        language="zh",
                        verbose=False,
                    )
                log.info(f"WHISPER: Warmup MLX complete (repo={hf_repo}, sine-wave 1.5s)")
            except Exception:
                log_error("warmup_mlx_failed", model=model_size, repo=hf_repo)

    def _transcribe_mlx(
        self,
        audio,
        model_size: str,
        language: Optional[str],
    ) -> TranscriptionResult:
        """使用 mlx-whisper 進行推論（Metal GPU 加速）。

        Fix 12 / 2026-05-23 — 短音檔快速路徑（< 3s）：
          mlx_whisper 預設 temperature fallback chain = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)。
          對短音檔（1-3s）模型常因資訊量不足而 quality bad → 一路 fallback 到 1.0，
          每段都重跑 decode，觀察過 1.6s 音檔產生 51 個 hallucination segment、
          RTF 7.5。長音檔（≥ 3s）資訊足夠不會觸發 fallback，維持預設品質。

          修法：< 3s 強制 beam_size=1 / temperature=0 / condition_on_previous_text=False
          —— 切斷 fallback chain、抑制幻覺放大、保證單次 decode 完事。
          中英混講等真實短句的辨識率影響微乎其微（這類短語就算多 beam 也只能
          靠音訊本身的資訊），但 RTF 7.5 → ~0.3 是 25 倍加速。
        """
        import mlx_whisper

        hf_repo  = _MLX_MODEL_MAP.get(model_size, _MLX_MODEL_MAP["large-v3-turbo"])
        duration = len(audio) / 16_000
        is_short = duration < 3.0

        # 短音檔額外 kwargs；長音檔維持 mlx_whisper 預設（品質優先）
        # Fix 14 / 2026-05-23：不要傳 beam_size（mlx_whisper 偵測 kwarg 就試
        # beam-search path，該 path 還沒實作 → NotImplementedError）。
        # 只用 temperature=0 + condition_on_previous_text=False 切 fallback chain。
        short_kwargs: dict = {}
        if is_short:
            short_kwargs = {
                "temperature":               0,
                "condition_on_previous_text": False,
            }

        with self._transcription_lock:
            try:
                try:
                    output = mlx_whisper.transcribe(
                        audio,
                        path_or_hf_repo=hf_repo,
                        language=language,
                        initial_prompt=self._build_initial_prompt(),
                        verbose=False,
                        **short_kwargs,
                    )
                except TypeError:
                    # 安全降級：mlx_whisper 版本不支援這些 kwargs 時用預設參數
                    if is_short:
                        log.warning(
                            "WHISPER: short-audio fast-path kwargs unsupported, "
                            "falling back to default mlx_whisper params"
                        )
                    output = mlx_whisper.transcribe(
                        audio,
                        path_or_hf_repo=hf_repo,
                        language=language,
                        initial_prompt=self._build_initial_prompt(),
                        verbose=False,
                    )
            except Exception as e:
                raise RuntimeError(f"MLX transcription failed: {e}")

        # 從 mlx_whisper 的輸出格式中提取 segments 與文字
        segments_raw = output.get("segments", [])
        segments     = [
            {"start": s["start"], "end": s["end"], "text": s["text"]}
            for s in segments_raw
        ]
        full_text_parts = [s["text"].strip() for s in segments_raw]
        text            = "".join(full_text_parts).strip()
        detected_lang   = output.get("language", "") or ""

        return TranscriptionResult(
            text=text,
            language=detected_lang,
            duration_seconds=0.0,   # 由呼叫端 transcribe() 填入
            elapsed_seconds=0.0,
            segments=segments,
        )

    # ── CTranslate2 後端（CPU int8）──────────────────────────────────────────

    def _warmup_ctranslate(self, model_size: str) -> None:
        """CTranslate2 後端暖機：確保模型載入，並跑一次靜音。"""
        import numpy as np
        try:
            model   = self._ensure_model(model_size)
            silence = np.zeros(3200, dtype=np.float32)   # 0.2 秒靜音
            with self._transcription_lock:
                segs, _ = model.transcribe(silence, beam_size=1, temperature=0)
                list(segs)   # 必須迭代才會真正執行推論
            log.info(f"WHISPER: Warmup CTranslate complete (model={model_size})")
        except Exception:
            log_error("warmup_ctranslate_failed", model=model_size)

    def _transcribe_ctranslate(
        self,
        audio,
        model_size: str,
        language: Optional[str],
    ) -> TranscriptionResult:
        """使用 faster-whisper（CTranslate2）進行 CPU int8 推論。"""
        model = self._ensure_model(model_size)

        # 長音訊（≥ 30s）開啟 condition_on_previous_text，讓模型利用前文
        # 上下文，減少長錄音的斷裂與不一致。短音訊維持關閉，避免 bad
        # start 造成 cascading error。
        long_audio = len(audio) >= 30 * 16_000

        with self._transcription_lock:
            segments_iter, info = model.transcribe(
                audio,
                language=language,
                beam_size=5,               # beam 越大越準，但越慢
                repetition_penalty=1.1,    # 輕微懲罰重複，抑制幻覺循環
                initial_prompt=self._build_initial_prompt(),
                condition_on_previous_text=long_audio,   # 長音訊才依賴前文
                vad_filter=True,           # 內建 VAD，過濾靜音段落
                # 寬鬆 VAD — 避免漏字/漏段：
                #   threshold 0.5→0.3：更包容弱訊號
                #   silence 300→800ms：句中小停頓不切段
                #   speech 150→50ms：短詞（如「是」「對」）也保留
                vad_parameters={
                    "threshold":              0.3,
                    "min_silence_duration_ms": 800,
                    "min_speech_duration_ms":  50,
                },
                word_timestamps=False,
                temperature=0,
            )
            segments        = []
            full_text_parts = []
            for seg in segments_iter:
                segments.append({"start": seg.start, "end": seg.end, "text": seg.text})
                full_text_parts.append(seg.text.strip())

        text = "".join(full_text_parts).strip()

        return TranscriptionResult(
            text=text,
            language=info.language,
            duration_seconds=0.0,   # 由呼叫端 transcribe() 填入
            elapsed_seconds=0.0,
            segments=segments,
        )

    # ── 私有方法 ──────────────────────────────────────────────────────────────

    def _ensure_model(self, model_size: str):
        """Lazy-load faster-whisper 模型（CPU 後端）。

        若已載入且相同 model_size，直接回傳快取實例。
        model_size 不同時，先釋放舊模型記憶體再載入新的。

        Args:
            model_size: 模型大小字串（例如 "large-v3-turbo"）。

        Returns:
            WhisperModel 實例。

        Raises:
            RuntimeError: 模型載入失敗時。
        """
        with self._lock:
            # 若已有相同大小的模型就直接回傳
            if self._model is None or self._loaded_model_size != model_size:
                t_start = time.perf_counter()

                # 釋放舊模型記憶體
                if self._model is not None:
                    log.info(f"WHISPER: Unloading old model '{self._loaded_model_size}'...")
                    try:
                        del self._model
                    except Exception:
                        log_error("whisper_model_del_failed")
                    gc.collect()

                from faster_whisper import WhisperModel
                # CPU 執行緒數：不超過 4，避免與 UI / pynput 執行緒競爭
                cpu_threads = min(4, os.cpu_count() or 4)
                log.info(f"WHISPER: Loading CPU model '{model_size}' with {cpu_threads} threads...")

                try:
                    self._model = WhisperModel(
                        model_size,
                        device="cpu",
                        compute_type="int8",      # int8 量化，省記憶體且速度快
                        cpu_threads=cpu_threads,
                        num_workers=1,            # 單 worker，避免 thread pool 過多
                    )
                    self._loaded_model_size = model_size
                    elapsed = time.perf_counter() - t_start
                    log.info(f"WHISPER: Model '{model_size}' loaded successfully in {elapsed:.2f}s.")
                except Exception as e:
                    log_error("whisper_model_load_failed", model=model_size)
                    raise RuntimeError(f"無法載入 Whisper 模型 {model_size}: {e}")

            return self._model
