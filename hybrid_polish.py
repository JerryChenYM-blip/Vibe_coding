"""
v2.19.x Hybrid polish backend：rule + pinyin guard + optional Gemini Flash-Lite。

動機：
  • 地端 Ollama polish 慢、會 cold load、會 thermal throttle
  • 雲端 Vertex polish 每次都打 API、有費用、有網路延遲
  • 但很多 polish 任務只需要修錯字（不需要 LLM 推理）——既有的
    apply_corrections（字串替換）+ apply_pinyin_guard（拼音 fuzzy）就能解決
    80% case，極快、零成本、隱私 100% 本地

設計：
  Layer 1 — apply_corrections（規則替換、~1ms、字典 corrections 段）
  Layer 2 — apply_pinyin_guard（拼音 fuzzy、~5-10ms、字典 terms 同音字保護）
  Layer 3 — optional Gemini Flash-Lite via VertexPolishClient（雲端、~1-3s）
            * 只在 hybrid_use_gemini=True + vertex_project_id 設好時跑
            * 用極短 prompt（HYBRID_POLISH_PROMPT）「只修錯字、不改寫」
            * 失敗時 silent fallback 回 Layer 1+2 結果

介面：duck-type 跟 OllamaClient / VertexPolishClient 一致（gui._refresh_polish_backend
可一鍵路由）。所有方法（process / warmup / unload / health_check_sync /
set_enabled / apply_app_config）跟 OllamaClient 同名同形。
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

log = get_logger("hybrid")


# ── Polish log 路徑（跟 Ollama / Vertex 共用同檔、便於統計比較）────────────────
_POLISH_LOG_PATH = Path.home() / ".whisper_app" / "polish_log.jsonl"


@dataclass
class HybridConfig:
    """Hybrid polish 設定。"""
    enabled:     bool = False
    use_gemini:  bool = True               # Layer 3 啟用否（要 vertex_project_id 才生效）
    # 以下欄位給 Layer 3 用、直接 forward 給 VertexPolishClient
    vertex_project_id: str = ""
    vertex_location:   str = "us-central1"
    vertex_model:      str = "gemini-2.5-flash-lite"   # Hybrid 預設用最便宜的 flash-lite
    timeout_seconds:   int = 15            # Layer 3 超時、超時降級回 Layer 1+2 結果
    log_enabled: bool = True


@dataclass
class HybridResponse:
    """跟 OllamaResponse / VertexResponse 同欄位、duck-type 兼容。"""
    text:   str
    model:  str
    done:   bool
    error:  Optional[str] = None
    elapsed_seconds: float = 0.0
    preset_name:     str   = "default"


# ─────────────────────────────────────────────────────────────────────────────


class HybridPolishClient:
    """Hybrid polish client、duck-type 兼容 OllamaClient / VertexPolishClient。

    執行緒安全：config 在 init 後變動透過 apply_app_config 同步、process()
    讀 config snapshot；Layer 3 的 VertexPolishClient lazy init 並用 lock 保護。
    """

    def __init__(self, config: Optional[HybridConfig] = None) -> None:
        self.config = config or HybridConfig()
        # Layer 3 client lazy init、避免 ADC 或 google-genai import 失敗炸 App 啟動
        self._vertex_client = None
        self._vertex_lock = threading.Lock()
        self._health_ok: Optional[bool] = None
        self._health_lock = threading.Lock()

    # ── 設定同步（跟 OllamaClient 同介面）──────────────────────────────────

    def set_enabled(self, enabled: bool) -> None:
        self.config.enabled = enabled
        with self._health_lock:
            self._health_ok = None

    def apply_app_config(self, cfg) -> None:
        """從 app Config 物件同步 hybrid 參數。"""
        new_enabled = getattr(cfg, "polish_backend", "local") == "hybrid"
        new_use_gemini   = getattr(cfg, "hybrid_use_gemini", True)
        new_project_id   = getattr(cfg, "vertex_project_id", "")
        new_location     = getattr(cfg, "vertex_location", "us-central1")
        # Hybrid 預設用 flash-lite；但若 user 在設定明確改 vertex_model，沿用
        new_vertex_model = getattr(cfg, "vertex_model", "gemini-2.5-flash-lite")

        # 任何 vertex 相關欄位變動就清掉 vertex client cache、下次 process 重 init
        vertex_changed = (
            self.config.vertex_project_id != new_project_id
            or self.config.vertex_location != new_location
            or self.config.vertex_model    != new_vertex_model
        )

        self.config.enabled = new_enabled
        self.config.use_gemini = new_use_gemini
        self.config.vertex_project_id = new_project_id
        self.config.vertex_location   = new_location
        self.config.vertex_model      = new_vertex_model
        self.config.log_enabled = getattr(cfg, "polish_log_enabled", True)

        if vertex_changed:
            with self._vertex_lock:
                self._vertex_client = None
        with self._health_lock:
            self._health_ok = None

    # ── Health check ──────────────────────────────────────────────────────

    @property
    def health_ok(self) -> Optional[bool]:
        with self._health_lock:
            return self._health_ok

    def health_check_sync(self) -> bool:
        """Layer 1+2 永遠 OK（純本地、無依賴）—— hybrid 視為健康。
        Layer 3 健康狀況不影響 hybrid health（Layer 3 失敗會 fallback 回 1+2）。
        """
        ok = self.config.enabled
        with self._health_lock:
            self._health_ok = ok
        return ok

    def health_check_async(self, callback=None) -> None:
        def _run():
            ok = self.health_check_sync()
            if callback:
                try:
                    callback(ok)
                except Exception:
                    log_error("hybrid_health_callback_failed")
        threading.Thread(target=_run, daemon=True).start()

    def is_available(self) -> bool:
        """向後相容 shim（跟 OllamaClient 同名）。"""
        cached = self.health_ok
        if cached is not None:
            return cached
        return self.health_check_sync()

    # ── Warmup / unload（Layer 1+2 不需要、Layer 3 透過 VertexPolishClient）──

    def warmup(self) -> bool:
        """Layer 1+2 純本地不需要 warmup。Layer 3 若啟用、跑一次 vertex warmup 預熱 ADC。"""
        if not self.config.enabled:
            return False
        if not (self.config.use_gemini and self.config.vertex_project_id):
            return True   # 純 rule + pinyin 模式、視為已就緒
        try:
            vc = self._get_vertex_client()
            if vc is None:
                return False
            return vc.warmup()
        except Exception:
            log_error("hybrid_warmup_failed")
            return False

    def unload(self) -> bool:
        """Layer 1+2 沒東西要 unload。Layer 3 透過 VertexPolishClient.unload 釋放 cache。"""
        with self._vertex_lock:
            if self._vertex_client is not None:
                try:
                    self._vertex_client.unload()
                except Exception:
                    pass
                self._vertex_client = None
        return True

    def get_models(self) -> list[str]:
        """Hybrid 沒有「主模型」概念；回 Layer 3 可選的 Gemini 模型清單。"""
        return [
            "gemini-2.5-flash-lite",   # 預設、最便宜
            "gemini-2.5-flash",         # 中等
            "gemini-2.5-pro",           # 慢、貴
        ]

    # ── Layer 3 lazy init ─────────────────────────────────────────────────

    def _get_vertex_client(self):
        """取得 Layer 3 用的 VertexPolishClient；lazy init、失敗回 None。"""
        if not self.config.use_gemini or not self.config.vertex_project_id:
            return None
        with self._vertex_lock:
            if self._vertex_client is not None:
                return self._vertex_client
            try:
                from vertex_polish import VertexPolishClient, VertexConfig
                vc = VertexPolishClient(VertexConfig(
                    enabled=True,
                    project_id=self.config.vertex_project_id,
                    location=self.config.vertex_location,
                    model=self.config.vertex_model,
                    timeout_seconds=self.config.timeout_seconds,
                    # Hybrid 自己會寫一行 hybrid polish log；不需要 Vertex 再寫一行
                    log_enabled=False,
                ))
                self._vertex_client = vc
                return vc
            except Exception as e:
                log.warning(f"HYBRID: vertex client init failed ({type(e).__name__}: {e})")
                return None

    # ── 核心 process ───────────────────────────────────────────────────────

    def process(
        self,
        text: str,
        prompt_template: Optional[str]            = None,
        dictionary_terms: Optional[Iterable[str]] = None,
        preset_name: str                          = "default",
    ) -> HybridResponse:
        """執行 hybrid polish；介面跟 OllamaClient.process 一致。

        流程：
          1. apply_corrections（規則替換）
          2. apply_pinyin_guard（拼音 fuzzy）
          3. (optional) Gemini Flash-Lite via VertexPolishClient

        無論哪層失敗、都至少回得到上一層的結果（safe degradation）。
        """
        if not text or not text.strip():
            return HybridResponse(
                text=text, model="hybrid", done=True,
                error="輸入為空", preset_name=preset_name,
            )

        if not self.config.enabled:
            return HybridResponse(
                text=text, model="hybrid", done=True,
                error="hybrid_disabled", preset_name=preset_name,
            )

        t0 = time.perf_counter()
        current = text
        layers_run: list[str] = []

        # ── Layer 1：apply_corrections（規則替換）──────────────────────────
        try:
            from dictionary import load_corrections, apply_corrections
            corrections = load_corrections()
            if corrections:
                before = current
                current = apply_corrections(current, corrections)
                if before != current:
                    layers_run.append("corrections")
        except Exception:
            log_error("hybrid_corrections_failed")

        # ── Layer 2：apply_pinyin_guard（拼音 fuzzy）──────────────────────
        try:
            from dictionary import apply_pinyin_guard
            before = current
            current, pinyin_fixes = apply_pinyin_guard(current)
            if pinyin_fixes:
                layers_run.append(f"pinyin({len(pinyin_fixes)})")
        except Exception:
            log_error("hybrid_pinyin_guard_failed")

        # ── Layer 3：Gemini Flash-Lite（optional）──────────────────────────
        layer3_error: Optional[str] = None
        layer3_model = "hybrid"
        vc = self._get_vertex_client() if self.config.use_gemini else None
        if vc is not None:
            try:
                # 用 hybrid 專屬極短 prompt（HYBRID_POLISH_PROMPT）；
                # caller 若顯式傳 prompt_template、尊重 caller（preset 用情境 prompt）
                tmpl = prompt_template if prompt_template is not None else getattr(
                    prompts, "HYBRID_POLISH_PROMPT", prompts.OLLAMA_POLISH_PROMPT,
                )
                vresp = vc.process(
                    current, tmpl,
                    dictionary_terms=dictionary_terms,
                    preset_name=preset_name,
                )
                layer3_model = vresp.model
                if vresp.error:
                    layer3_error = vresp.error
                else:
                    current = vresp.text or current
                    layers_run.append("gemini")
            except Exception as e:
                layer3_error = f"{type(e).__name__}: {e}"
                log_error("hybrid_layer3_failed", error=layer3_error)

        elapsed = time.perf_counter() - t0
        layers_str = "+".join(layers_run) if layers_run else "passthrough"
        log.info(
            f"HYBRID: layers={layers_str} elapsed={elapsed:.3f}s "
            f"len {len(text)}→{len(current)}"
        )

        # ── Polish log（共用既有 jsonl 格式）──────────────────────────────
        if self.config.log_enabled:
            self._append_polish_log(
                model=layer3_model, preset=preset_name,
                elapsed_s=elapsed, len_in=len(text), len_out=len(current),
                layers=layers_str, error=layer3_error,
            )

        # 觀測性 emit（跟 ollama_client / vertex_polish 同形）
        _emit_polish_observability(
            backend="hybrid", elapsed=elapsed,
            error=layer3_error, text_len=len(current),
        )

        return HybridResponse(
            text=current, model=layer3_model, done=True,
            error=layer3_error,
            elapsed_seconds=elapsed,
            preset_name=preset_name,
        )

    # ── Polish log（跟 OllamaClient / VertexPolishClient 同檔、同格式）─────

    def _append_polish_log(self, **fields) -> None:
        try:
            entry = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "backend": "hybrid",
                **fields,
            }
            with _POLISH_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            log_error("hybrid_polish_log_write_failed")


# ── 觀測性 helper（跟 ollama_client.py / vertex_polish.py 同形式）──────────────


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
