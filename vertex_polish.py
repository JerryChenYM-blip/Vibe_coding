"""
v2.18.0 Vertex AI Gemini polish backend（雲端潤飾、本地 Ollama 的替代）。

為什麼：user 反映本地 Ollama polish 在 GPU thermal throttle 時慢、且閒置
後第一次 cold load。雲端方案 offload 整個 polish 到 Google Cloud、本地零
GPU 負擔、不會 cold load。

跟 OllamaClient 同 process/warmup/unload 介面（duck-type）、gui.py 內可
透過 polish_backend config 一鍵切換。

認證：Application Default Credentials（ADC）—— user 跑 `gcloud auth
application-default login` 一次即可。VertexPolishClient.warmup 偵測 ADC
失敗會 silent fail、log warning、user 仍可用本地 polish fallback。

API 端點：aiplatform.googleapis.com（Vertex AI / Gemini Enterprise Agent
Platform、2026/04/23 Google rebrand 後新名稱、endpoint 不變）。

模型：預設 gemini-2.5-flash（快、便宜、polish 品質夠）。可在設定改成
gemini-2.5-pro（慢、更貴、品質略好）。
"""

from __future__ import annotations

import json
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import prompts
from logger import get_logger, log_error

log = get_logger("vertex")


# ── Polish log 路徑（跟 Ollama 共用同檔、便於統計比較）──────────────────────
_POLISH_LOG_PATH = Path.home() / ".whisper_app" / "polish_log.jsonl"


@dataclass
class VertexConfig:
    """Vertex AI Gemini polish 設定。"""
    enabled:      bool = False
    project_id:   str  = ""                  # GCP project（必填、ADC 仍需指定）
    location:     str  = "us-central1"       # Vertex region
    model:        str  = "gemini-2.5-flash"  # gemini-2.5-flash / pro / lite
    timeout_seconds: int = 30
    log_enabled:  bool = True


@dataclass
class VertexResponse:
    """跟 OllamaResponse 同欄位、duck-type 兼容。"""
    text:   str
    model:  str
    done:   bool
    error:  Optional[str] = None
    elapsed_seconds: float = 0.0
    preset_name:     str   = "default"


# ─────────────────────────────────────────────────────────────────────────────


class VertexPolishClient:
    """Vertex AI Gemini polish client、duck-type 兼容 OllamaClient。

    執行緒安全：client / config 在 init 後不變、process() 可從多 thread 並行
    呼叫（Vertex API 本身是 HTTP、google-genai 套件內部處理連線 pool）。
    """

    def __init__(self, config: Optional[VertexConfig] = None) -> None:
        self.config = config or VertexConfig()
        self._client = None   # google.genai.Client、lazy init（避免 import 失敗炸 App 啟動）
        self._health_ok: Optional[bool] = None
        self._health_cached_at: float = 0.0
        self._health_lock = threading.Lock()

    # ── 設定同步（跟 OllamaClient 同介面）──────────────────────────────────

    def set_enabled(self, enabled: bool) -> None:
        self.config.enabled = enabled
        with self._health_lock:
            self._health_ok = None

    def apply_app_config(self, cfg) -> None:
        """從 app Config 物件同步 Vertex 參數。"""
        new_project = getattr(cfg, "vertex_project_id", "")
        new_location = getattr(cfg, "vertex_location", "us-central1")
        new_model = getattr(cfg, "vertex_model", "gemini-2.5-flash")
        new_enabled = getattr(cfg, "polish_backend", "local") == "vertex"

        changed = (
            self.config.project_id != new_project
            or self.config.location != new_location
            or self.config.model    != new_model
            or self.config.enabled  != new_enabled
        )
        self.config.project_id = new_project
        self.config.location   = new_location
        self.config.model      = new_model
        self.config.enabled    = new_enabled
        self.config.log_enabled = getattr(cfg, "polish_log_enabled", True)
        if changed:
            with self._health_lock:
                self._health_ok = None
            # client 設定變了、丟掉 cache、下次 process() 重 init
            self._client = None

    # ── Lazy client init（ADC 認證錯誤包在 try 裡、不炸 App）───────────────

    def _ensure_client(self):
        """Lazy load google.genai Client、ADC 不對時 raise。"""
        if self._client is not None:
            return self._client
        if not self.config.project_id:
            raise RuntimeError("Vertex project_id 未設定（設定 → AI 潤飾 → Vertex Project ID）")
        try:
            from google import genai
            self._client = genai.Client(
                vertexai=True,
                project=self.config.project_id,
                location=self.config.location,
            )
            return self._client
        except Exception as e:
            raise RuntimeError(f"無法初始化 Vertex client: {e}")

    # ── 健康檢查（跟 OllamaClient 同介面）──────────────────────────────────

    @property
    def health_ok(self) -> Optional[bool]:
        with self._health_lock:
            return self._health_ok

    def health_check_sync(self) -> bool:
        """嘗試 _ensure_client；成功視為健康。"""
        if not self.config.enabled:
            with self._health_lock:
                self._health_ok = False
                self._health_cached_at = time.monotonic()
            return False
        try:
            self._ensure_client()
            ok = True
        except Exception:
            ok = False
        with self._health_lock:
            self._health_ok = ok
            self._health_cached_at = time.monotonic()
        return ok

    def health_check_async(self, callback=None) -> None:
        """非同步 health check、callback(ok: bool) 在背景 thread 呼叫。"""
        def _run():
            ok = self.health_check_sync()
            if callback:
                try:
                    callback(ok)
                except Exception:
                    log_error("vertex_health_callback_failed")
        threading.Thread(target=_run, daemon=True).start()

    # ── Warmup / unload（跟 OllamaClient 同介面、no-op）───────────────────

    def warmup(self) -> bool:
        """雲端不需要 warmup（沒有 cold load 概念、Google 側自管）。
        但跑一次小 request 驗證 ADC + 網路、回 True/False 給 UI 顯示就緒狀態。
        """
        if not self.config.enabled or not self.config.project_id:
            return False
        t0 = time.time()
        try:
            client = self._ensure_client()
            from google.genai import types
            resp = client.models.generate_content(
                model=self.config.model,
                contents=".",
                config=types.GenerateContentConfig(
                    temperature=0,
                    max_output_tokens=1,
                ),
            )
            elapsed = time.time() - t0
            log.info(
                f"VERTEX: warmup complete (model={self.config.model}, "
                f"project={self.config.project_id}, {elapsed:.2f}s)"
            )
            return True
        except Exception as e:
            log.warning(f"VERTEX: warmup failed ({type(e).__name__}: {e})")
            return False

    def unload(self) -> bool:
        """雲端無 unload 概念（資源在 Google 側）；只 reset local cache。"""
        self._client = None
        return True

    def get_models(self) -> list[str]:
        """回常用 Gemini 模型列表（不打 API、避免每次都呼叫）。"""
        return [
            "gemini-2.5-flash",      # 快、便宜、推薦
            "gemini-2.5-pro",        # 慢、品質更好
            "gemini-2.5-flash-lite", # 最快、最便宜、品質略差
        ]

    # ── 核心 process（跟 OllamaClient 同介面）──────────────────────────────

    def process(
        self,
        text: str,
        prompt_template: str,
        dictionary_terms: Optional[Iterable[str]] = None,
        preset_name: str = "default",
    ) -> VertexResponse:
        """執行雲端 polish；介面跟 OllamaClient.process 一致。

        Args:
            text:            要 polish 的原文
            prompt_template: 含 {text} placeholder 的 prompt 模板
            dictionary_terms: 個人字典術語（沿用 format_polish_prompt 注入）
            preset_name:     preset 名稱（log 用）

        Returns:
            VertexResponse；error 非 None 時 text=原文（fallback）。
        """
        if not text or not text.strip():
            return VertexResponse(text=text, model=self.config.model, done=True,
                                  preset_name=preset_name)
        if not self.config.enabled:
            return VertexResponse(text=text, model=self.config.model, done=True,
                                  error="vertex_disabled", preset_name=preset_name)

        t0 = time.time()
        try:
            client = self._ensure_client()
            from google.genai import types

            # 拼 prompt（沿用既有 prompts.format_polish_prompt 邏輯）
            try:
                full_prompt = prompts.format_polish_prompt(
                    prompt_template, dictionary_terms
                ).format(text=text)
            except Exception:
                # fallback：直接 format
                full_prompt = prompt_template.format(text=text)

            system_instruction = getattr(prompts, "OLLAMA_POLISH_SYSTEM", None)

            cfg_kwargs = {
                "temperature": 0.2,
                "top_p":       0.9,
                "max_output_tokens": max(64, int(len(text) * 2.0)),
            }
            if system_instruction:
                cfg_kwargs["system_instruction"] = system_instruction

            resp = client.models.generate_content(
                model=self.config.model,
                contents=full_prompt,
                config=types.GenerateContentConfig(**cfg_kwargs),
            )
            elapsed = time.time() - t0
            out_text = (resp.text or "").strip()

            # log 落地
            if self.config.log_enabled:
                self._append_polish_log(
                    model=self.config.model, preset=preset_name,
                    elapsed_s=elapsed, len_in=len(text), len_out=len(out_text),
                    error=None,
                )
            # v2.19.0：pipeline event + session summary（觀測性、失敗 silent）
            _emit_polish_observability(
                backend="vertex", elapsed=elapsed,
                error=None, text_len=len(out_text),
            )

            return VertexResponse(
                text=out_text or text,
                model=self.config.model,
                done=True,
                elapsed_seconds=elapsed,
                preset_name=preset_name,
            )
        except Exception as e:
            elapsed = time.time() - t0
            err = f"{type(e).__name__}: {e}"
            log_error("vertex_process_failed", error=err)
            if self.config.log_enabled:
                self._append_polish_log(
                    model=self.config.model, preset=preset_name,
                    elapsed_s=elapsed, len_in=len(text), len_out=0, error=err,
                )
            _emit_polish_observability(
                backend="vertex", elapsed=elapsed,
                error=err, text_len=0,
            )
            return VertexResponse(
                text=text,   # fallback：回原文
                model=self.config.model,
                done=True,
                error=err,
                elapsed_seconds=elapsed,
                preset_name=preset_name,
            )

    # ── Polish log（跟 OllamaClient 同檔、同格式）──────────────────────────

    def _append_polish_log(self, **fields) -> None:
        try:
            entry = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "backend": "vertex",
                **fields,
            }
            with _POLISH_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            log_error("vertex_polish_log_write_failed")


# ── v2.19.0 觀測性 helper（module-level、跟 ollama_client.py 同形式）─────────
# pipeline_id / session_summary 由 Agent N 同步在建；可能還沒 ready 或 import
# 失敗。包成 helper 把所有觀測性 noise 吞掉，process() 主流程不被影響。


def _emit_polish_observability(
    backend: str,
    elapsed: float,
    error: Optional[str],
    text_len: int,
) -> None:
    """polish 完成時 emit pipeline event + record session summary。失敗 silent。"""
    try:
        from pipeline_id import event as pipeline_event  # type: ignore
        pipeline_event(
            "polish_done", backend=backend, elapsed_s=elapsed,
            error=error, text_len=text_len,
        )
    except Exception:
        pass
    try:
        import session_summary  # type: ignore
        session_summary.record_polish(elapsed, enabled=True)
    except Exception:
        pass
