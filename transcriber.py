"""
Whisper transcription wrapper.

Backend selection (auto-detected at startup):
  • Apple Silicon (arm64) + mlx installed → mlx-whisper  (Metal GPU / Neural Engine)
  • Everything else                        → faster-whisper (CPU int8)

Models are cached in ~/.cache/huggingface/hub/ (faster-whisper)
or ~/.cache/huggingface/hub/ (mlx-whisper, same HF cache).
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

# ── Backend detection ─────────────────────────────────────────────────────────

def _detect_backend() -> str:
    """Return 'mlx' if Apple Silicon + mlx available, else 'ctranslate'."""
    if platform.machine() != "arm64":
        return "ctranslate"
    try:
        import mlx_whisper  # noqa: F401
        return "mlx"
    except ImportError:
        return "ctranslate"


BACKEND = _detect_backend()

# MLX model IDs (mapped from faster-whisper model size names)
# 注意：tiny/base 的 `mlx-community/whisper-tiny`、`whisper-base` repo
# 已於 2026 年改名/下架，會回 401。全部統一使用 `-mlx-4bit` 量化版本，
# 若 MLX 載入失敗，transcribe() 會自動 fallback 到 CTranslate2 CPU。
_MLX_MODEL_MAP: dict[str, str] = {
    "tiny":           "mlx-community/whisper-tiny-mlx-4bit",
    "base":           "mlx-community/whisper-base-mlx-4bit",
    "small":          "mlx-community/whisper-small-mlx-4bit",
    "medium":         "mlx-community/whisper-medium-mlx-4bit",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "large-v3":       "mlx-community/whisper-large-v3-mlx-4bit",
}

# ── Silence & hallucination guard ────────────────────────────────────────────

# Minimum RMS energy to bother calling Whisper.
# Audio quieter than this is treated as silence.
_MIN_RMS = 0.004

# Known Whisper hallucination substrings (model outputs these on silence/noise).
# Matching is case-insensitive and strips whitespace.
_HALLUCINATIONS: tuple[str, ...] = (
    "作詞",
    "作曲",
    "李宗盛",
    "字幕由",
    "amara",
    "請訂閱",
    "訂閱頻道",
    "謝謝收看",
    "敬請期待",
    "please subscribe",
    "thanks for watching",
    "subtitles by",
    "transcript by",
    "請原文保留英文單字",
    "技術術語",
)


def _is_silence(audio) -> bool:
    """Return True if the audio energy is too low to contain real speech."""
    import numpy as np
    if audio.size == 0:
        return True
    rms = float(np.sqrt(np.mean(audio ** 2)))
    return rms < _MIN_RMS


def _is_hallucination(text: str) -> bool:
    """Return True if the text looks like a Whisper hallucination.

    寬容策略：只對「短句（< 20 字）」才用子字串檢查。長句子即使包含
    「訂閱」「感謝」等常見幻覺關鍵字，也視為正當句子保留 — 避免誤殺。
    """
    if not text:
        return True
    lower = text.lower().replace(" ", "").replace("\n", "")
    # 長句（≥ 20 字）免檢查 — 幾乎不會有 20 字以上的純幻覺
    if len(lower) >= 20:
        return False
    return any(h.lower().replace(" ", "") in lower for h in _HALLUCINATIONS)


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


@dataclass
class TranscriptionResult:
    text: str
    language: str
    duration_seconds: float
    elapsed_seconds: float
    segments: list[dict] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────

class Transcriber:
    """Thread-safe Whisper transcription with lazy model loading.

    Uses mlx-whisper (Metal GPU) on Apple Silicon when available,
    falls back to faster-whisper (CPU int8) otherwise.
    """

    def __init__(self) -> None:
        self._model = None                        # faster-whisper model (CPU)
        self._loaded_model_size: Optional[str] = None
        self._lock = threading.Lock()             # guards model loading
        self._transcription_lock = threading.Lock()  # prevents concurrent transcriptions

    # ── public API ────────────────────────────────────────────────────────────

    def transcribe(
        self,
        audio,           # np.ndarray float32 at 16 kHz
        model_size: str = "base",
        language: Optional[str] = None,
    ) -> TranscriptionResult:
        """
        Transcribe audio. Blocking — run in a background thread.
        """
        import numpy as np

        if audio is None or len(audio) == 0:
            print("WHISPER: ERROR - Empty audio buffer received.")
            return TranscriptionResult(
                text="（沒有偵測到音訊，請確認麥克風是否正常運作）",
                language="",
                duration_seconds=0.0,
                elapsed_seconds=0.0,
            )

        audio = audio.astype(np.float32)
        if audio.max() > 1.0:
            audio = audio / 32768.0

        # 音量自動正規化：小聲說話時避免 VAD 把整段判為靜音
        pre_peak = float(np.abs(audio).max()) if audio.size else 0.0
        audio = _normalize_volume(audio)
        post_peak = float(np.abs(audio).max()) if audio.size else 0.0
        if post_peak > pre_peak * 1.5:
            print(
                f"WHISPER: Volume normalized (peak {pre_peak:.3f} → {post_peak:.3f})"
            )

        duration = len(audio) / 16_000
        t0 = time.perf_counter()
        print(f"WHISPER: Starting transcription. Backend={BACKEND}, Model={model_size}, Lang={language}, AudioDuration={duration:.2f}s")

        # Guard: skip Whisper entirely if audio is silent
        if _is_silence(audio):
            print("WHISPER: Guard - Audio level below threshold, skipping inference.")
            return TranscriptionResult(
                text="（未偵測到語音內容）",
                language="",
                duration_seconds=duration,
                elapsed_seconds=time.perf_counter() - t0,
            )

        if BACKEND == "mlx":
            try:
                result = self._transcribe_mlx(audio, model_size, language)
            except Exception as e:
                print(f"WHISPER ERROR: MLX Backend failed: {e}. Falling back to CPU.")
                result = self._transcribe_ctranslate(audio, model_size, language)
        else:
            result = self._transcribe_ctranslate(audio, model_size, language)

        result.duration_seconds = duration
        result.elapsed_seconds = time.perf_counter() - t0
        print(f"WHISPER: Inference finished in {result.elapsed_seconds:.2f}s. RTF={(result.elapsed_seconds/duration) if duration>0 else 0:.3f}")

        # 幻覺過濾：優先逐段過濾（保留合法段），沒 segments 資訊才退回整段檢查
        if result.segments:
            kept = [
                s for s in result.segments
                if not _is_hallucination(s.get("text", "").strip())
            ]
            removed = len(result.segments) - len(kept)
            if removed > 0:
                print(
                    f"WHISPER: Guard - Removed {removed}/{len(result.segments)} "
                    f"hallucination segment(s), kept {len(kept)}."
                )
                result.segments = kept
                if kept:
                    result.text = "".join(s["text"].strip() for s in kept).strip()
                else:
                    result.text = "（未偵測到語音內容）"
        elif _is_hallucination(result.text):
            print(f"WHISPER: Guard - Detected hallucination in result: '{result.text[:50]}...'")
            result.text = "（未偵測到語音內容）"

        return result

    def transcribe_fast(
        self,
        audio,
        language: Optional[str] = None,
        model_size: str = "small",
    ) -> TranscriptionResult:
        """
        Low-latency transcription optimised for short clips (≤ 8 s) and
        streaming previews. Always uses the CTranslate2 CPU backend with
        greedy decoding. Default model is "small" to keep streaming fast;
        callers can override for higher accuracy.
        """
        import numpy as np

        if audio is None or len(audio) == 0:
            return TranscriptionResult(text="（沒有偵測到音訊，請確認麥克風是否正常運作）",
                                       language="", duration_seconds=0.0, elapsed_seconds=0.0)

        audio = audio.astype(np.float32)
        if audio.max() > 1.0:
            audio = audio / 32768.0

        # 音量正規化 — 和 transcribe() 同策略
        audio = _normalize_volume(audio)

        duration = len(audio) / 16_000
        t0 = time.perf_counter()

        # Guard: skip Whisper if audio is silent
        if _is_silence(audio):
            return TranscriptionResult(text="（未偵測到語音內容）", language="",
                                       duration_seconds=duration, elapsed_seconds=0.0)

        model = self._ensure_model(model_size)

        with self._transcription_lock:
            segments_iter, info = model.transcribe(
                audio,
                language=language,
                beam_size=1,               # greedy decoding — fastest
                initial_prompt=prompts.WHISPER_INITIAL_PROMPT,
                condition_on_previous_text=False,
                vad_filter=True,
                # 寬鬆 VAD：threshold 降低讓小聲也偵測到；silence 拉長避免句中停頓
                # 被切段；min_speech 降到 50ms 保留短詞。
                vad_parameters={
                    "threshold": 0.3,
                    "min_silence_duration_ms": 800,
                    "min_speech_duration_ms": 50,
                },
                word_timestamps=False,
                temperature=0,
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
        """Pre-load model weights and run a silent clip."""
        if BACKEND == "mlx":
            self._warmup_mlx(model_size)
        else:
            self._warmup_ctranslate(model_size)

    def _warmup_ctranslate(self, model_size: str) -> None:
        import numpy as np
        try:
            model = self._ensure_model(model_size)
            silence = np.zeros(3200, dtype=np.float32)   # 0.2 s
            with self._transcription_lock:
                segs, _ = model.transcribe(silence, beam_size=1, temperature=0)
                list(segs)
        except Exception as e:
            print(f"Warmup CTranslate failed: {e}")

    def unload(self) -> None:
        """Release models from memory (CPU and MLX)."""
        with self._lock:
            if self._model is not None:
                del self._model
                self._model = None
            self._loaded_model_size = None
        
        if BACKEND == "mlx":
            try:
                import mlx.core as mx
                mx.metal.clear_cache()
            except:
                pass
                
        gc.collect()

    @staticmethod
    def active_backend() -> str:
        return BACKEND

    # ── MLX backend (Apple Silicon Metal GPU) ────────────────────────────────

    def _warmup_mlx(self, model_size: str) -> None:
        import mlx_whisper
        import numpy as np
        hf_repo = _MLX_MODEL_MAP.get(model_size, _MLX_MODEL_MAP["large-v3-turbo"])
        silence = np.zeros(3200, dtype=np.float32)   # 0.2 s
        with self._transcription_lock:
            try:
                mlx_whisper.transcribe(
                    silence,
                    path_or_hf_repo=hf_repo,
                    language="zh",
                    verbose=False,
                )
            except Exception as e:
                print(f"Warmup MLX failed: {e}")

    def _transcribe_mlx(
        self,
        audio,
        model_size: str,
        language: Optional[str],
    ) -> TranscriptionResult:
        import mlx_whisper

        hf_repo = _MLX_MODEL_MAP.get(model_size, _MLX_MODEL_MAP["large-v3-turbo"])

        with self._transcription_lock:
            try:
                output = mlx_whisper.transcribe(
                    audio,
                    path_or_hf_repo=hf_repo,
                    language=language,
                    initial_prompt=prompts.WHISPER_INITIAL_PROMPT,
                    verbose=False,
                )
            except Exception as e:
                raise RuntimeError(f"MLX transcription failed: {e}")

        segments_raw = output.get("segments", [])
        segments = [
            {"start": s["start"], "end": s["end"], "text": s["text"]}
            for s in segments_raw
        ]
        full_text_parts = [s["text"].strip() for s in segments_raw]
        text = "".join(full_text_parts).strip()
        
        detected_lang = output.get("language", "") or ""

        return TranscriptionResult(
            text=text,
            language=detected_lang,
            duration_seconds=0.0,
            elapsed_seconds=0.0,
            segments=segments,
        )

    # ── CTranslate2 backend (CPU int8) ────────────────────────────────────────

    def _transcribe_ctranslate(
        self,
        audio,
        model_size: str,
        language: Optional[str],
    ) -> TranscriptionResult:
        model = self._ensure_model(model_size)

        # 長音訊（≥ 30s）開啟 condition_on_previous_text，讓模型利用前文
        # 上下文，減少長錄音的斷裂與不一致。短音訊維持關閉，避免 bad
        # start 造成 cascading error。
        long_audio = len(audio) >= 30 * 16_000
        condition = long_audio

        with self._transcription_lock:
            segments_iter, info = model.transcribe(
                audio,
                language=language,
                beam_size=5,
                repetition_penalty=1.1,
                initial_prompt=prompts.WHISPER_INITIAL_PROMPT,
                condition_on_previous_text=condition,
                vad_filter=True,
                # 寬鬆 VAD — 避免漏字/漏段：
                #   threshold 0.5→0.3：更包容弱訊號
                #   silence 300→800ms：句中小停頓不切段
                #   speech 150→50ms：短詞（如「是」「對」）也保留
                vad_parameters={
                    "threshold": 0.3,
                    "min_silence_duration_ms": 800,
                    "min_speech_duration_ms": 50,
                },
                word_timestamps=False,
                temperature=0,
            )

            segments = []
            full_text_parts = []
            for seg in segments_iter:
                segments.append({"start": seg.start, "end": seg.end, "text": seg.text})
                full_text_parts.append(seg.text.strip())

        text = "".join(full_text_parts).strip()

        return TranscriptionResult(
            text=text,
            language=info.language,
            duration_seconds=0.0,
            elapsed_seconds=0.0,
            segments=segments,
        )

    # ── private ───────────────────────────────────────────────────────────────

    def _ensure_model(self, model_size: str):
        """Lazy-load faster-whisper model (CPU)."""
        with self._lock:
            if self._model is None or self._loaded_model_size != model_size:
                t_start = time.perf_counter()
                if self._model is not None:
                    print(f"WHISPER: Unloading old model '{self._loaded_model_size}'...")
                    try:
                        del self._model
                    except: pass
                    gc.collect()
                    
                from faster_whisper import WhisperModel
                cpu_threads = min(4, os.cpu_count() or 4)
                print(f"WHISPER: Loading CPU model '{model_size}' with {cpu_threads} threads...")
                try:
                    self._model = WhisperModel(
                        model_size,
                        device="cpu",
                        compute_type="int8",
                        cpu_threads=cpu_threads,
                        num_workers=1,
                    )
                    self._loaded_model_size = model_size
                    elapsed = time.perf_counter() - t_start
                    print(f"WHISPER: Model '{model_size}' loaded successfully in {elapsed:.2f}s.")
                except Exception as e:
                    print(f"WHISPER ERROR: Failed to load model '{model_size}'. Detail: {e}")
                    raise RuntimeError(f"無法載入 Whisper 模型 {model_size}: {e}")
            return self._model
