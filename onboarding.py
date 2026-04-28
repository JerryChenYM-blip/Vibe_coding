"""
Phase 4.5 首次啟動引導 / Ollama 診斷。

純函式模組，不碰 UI；提供「Ollama 環境目前是什麼狀態 + 下一步建議命令」
讓 gui.py 的設定視窗、首次啟動 toast 共用同一個事實來源。

API：
    diagnose_ollama(base_url, recommended_model) -> OllamaDiagnostic
    summarize(diag) -> 給人看的一行 + 多行細節 + 建議命令
"""

from __future__ import annotations

import shutil
import socket
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import requests

from logger import get_logger

log = get_logger("onboarding")


# ── 診斷結果 ──────────────────────────────────────────────────────────────────

@dataclass
class OllamaDiagnostic:
    """Ollama 環境診斷快照。

    四個狀態（嚴重度遞減）：
      • missing_binary  — 連 ollama CLI 都沒裝（需要 `brew install ollama`）
      • not_running     — 二進位有但 daemon 沒跑（需要 `ollama serve`）
      • no_models       — daemon 跑著但 `/api/tags` 是空的（需要 `ollama pull`）
      • ready           — 一切就位（可選地檢查推薦模型是否在清單中）
    """

    status:           str               # "missing_binary" | "not_running" | "no_models" | "ready"
    binary_present:   bool              # which("ollama") 找得到
    daemon_reachable: bool              # GET /api/tags 200
    models:           list[str]         # /api/tags 回的模型清單（可能空）
    recommended:      Optional[str]     # 推薦模型名（用於缺模型訊息）
    has_recommended:  bool              # 推薦模型是否已 pull
    base_url:         str               # 探測用的 URL（log 用）

    @property
    def is_ready(self) -> bool:
        return self.status == "ready"


# ── 診斷主入口 ────────────────────────────────────────────────────────────────

def diagnose_ollama(
    base_url:    str = "http://localhost:11434",
    recommended: str = "qwen2.5:3b-instruct",
    timeout_s:   float = 1.5,
) -> OllamaDiagnostic:
    """非阻塞快速健診（最多 ~timeout_s × 2 秒）。

    步驟：
      1. `which ollama` 檢查 CLI（無 → missing_binary）
      2. socket 探一下 base_url 的 port（不通 → not_running，不浪費 HTTP timeout）
      3. GET /api/tags（失敗 → not_running；空清單 → no_models）
      4. 比對 recommended 是否在 models 中（決定 has_recommended）
    """
    binary_present = shutil.which("ollama") is not None

    # 解 base_url 取 host:port，用 socket 快速探（避開 requests 的 1.5s timeout）
    parsed = urlparse(base_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 11434
    daemon_reachable = _socket_probe(host, port, timeout_s=0.5)

    if not binary_present and not daemon_reachable:
        # 兩者都沒：判定使用者完全沒裝
        return OllamaDiagnostic(
            status="missing_binary",
            binary_present=False, daemon_reachable=False,
            models=[], recommended=recommended,
            has_recommended=False, base_url=base_url,
        )

    if not daemon_reachable:
        return OllamaDiagnostic(
            status="not_running",
            binary_present=binary_present, daemon_reachable=False,
            models=[], recommended=recommended,
            has_recommended=False, base_url=base_url,
        )

    # daemon 通了，問模型清單
    models = _list_models(base_url, timeout_s)
    has_recommended = (recommended in models) if recommended else True

    if not models:
        return OllamaDiagnostic(
            status="no_models",
            binary_present=binary_present, daemon_reachable=True,
            models=[], recommended=recommended,
            has_recommended=False, base_url=base_url,
        )

    return OllamaDiagnostic(
        status="ready",
        binary_present=binary_present, daemon_reachable=True,
        models=models, recommended=recommended,
        has_recommended=has_recommended, base_url=base_url,
    )


# ── 描述（給 UI 用）───────────────────────────────────────────────────────────

def summarize(diag: OllamaDiagnostic) -> tuple[str, str, Optional[str]]:
    """把診斷結果翻成 (一行標題, 多行細節, 建議命令)。

    建議命令可為 None（已就位時不需要動作）。
    """
    if diag.status == "missing_binary":
        return (
            "✗ 找不到 Ollama",
            "Ollama 是本地 AI 潤飾的後端，沒裝 App 的潤飾按鈕會永遠灰著。",
            "brew install ollama && brew services start ollama",
        )
    if diag.status == "not_running":
        return (
            "⚠ Ollama 服務沒在跑",
            f"已安裝但 {diag.base_url} 沒回應。先把 daemon 起起來。",
            "ollama serve  # 或  brew services start ollama",
        )
    if diag.status == "no_models":
        return (
            "⚠ Ollama 沒有模型",
            "服務跑著但模型清單是空的。需要 pull 一個才能潤飾。",
            f"ollama pull {diag.recommended}" if diag.recommended else "ollama pull qwen2.5:3b-instruct",
        )
    # ready
    if not diag.has_recommended and diag.recommended:
        return (
            "✓ Ollama 就緒（建議再裝推薦模型）",
            f"{len(diag.models)} 個模型可用：{', '.join(diag.models[:4])}"
            + ("…" if len(diag.models) > 4 else "")
            + f"\n推薦模型 `{diag.recommended}` 還沒 pull（中文體驗最佳）。",
            f"ollama pull {diag.recommended}",
        )
    return (
        "✓ Ollama 就緒",
        f"{len(diag.models)} 個模型可用：{', '.join(diag.models[:6])}"
        + ("…" if len(diag.models) > 6 else ""),
        None,
    )


# ── 內部 helpers ──────────────────────────────────────────────────────────────

def _socket_probe(host: str, port: int, timeout_s: float = 0.5) -> bool:
    """純 socket 試連線；通了就 close，不發 HTTP request。"""
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except (OSError, socket.timeout):
        return False


def _list_models(base_url: str, timeout_s: float) -> list[str]:
    try:
        resp = requests.get(f"{base_url}/api/tags", timeout=timeout_s)
        resp.raise_for_status()
        data = resp.json()
        return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []
