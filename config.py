"""
Persistent configuration stored at ~/.whisper_app/config.json
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

CONFIG_DIR = Path.home() / ".whisper_app"
CONFIG_PATH = CONFIG_DIR / "config.json"

MODEL_INFO: dict[str, str] = {
    "tiny":           "速度最快，適合英文速記（39M 參數）",
    "base":           "速度與精準度平衡，推薦入門（74M 參數）",
    "small":          "較高精準度，中文效果更佳（244M 參數，GPU 4bit 量化）",
    "medium":         "高精準度，需較多記憶體（769M 參數，GPU 4bit 量化）",
    "large-v3-turbo": "★ 推薦：中英混合最佳，Metal GPU 加速（809M 參數）",
    "large-v3":       "最高精準度，Metal GPU 加速（1550M 參數，4bit 量化）",
}

LANGUAGE_OPTIONS: dict[str, str | None] = {
    "自動偵測": None,
    "中文":     "zh",
    "English":  "en",
    "日本語":   "ja",
    "한국어":   "ko",
    "Español":  "es",
    "Français": "fr",
}


@dataclass
class Config:
    hotkey: str = "cmd+alt+r"
    model: str = "large-v3-turbo"
    language: str = "自動偵測"
    input_device: Optional[str] = None
    append_results: bool = True
    auto_copy: bool = False
    auto_paste: bool = True

    # ── Ollama 潤飾 ───────────────────────────────────────────────────────────
    # 預設關閉；使用者需在設定中打開並確認 Ollama 服務可用後才會生效。
    ollama_enabled: bool = False
    # 規劃書 6.2：3B 模型為 16GB Mac 首選，延遲 < 1s，中文品質可接受。
    ollama_model: str = "qwen2.5:3b-instruct"
    ollama_base_url: str = "http://localhost:11434"
    # 潤飾上限 30 秒；超時即降級回原文（不阻塞使用者）。
    ollama_timeout: int = 30
    # 貼上策略：
    #   "wait"   — 等潤飾完成再貼（規劃書 6.4 建議預設）
    #   "raw"    — 先貼原文、不做潤飾替換（潤飾失敗時的降級模式）
    ollama_paste_strategy: str = "wait"

    # ── Phase 2 preset 路由 ───────────────────────────────────────────────────
    # 關閉時所有轉錄走 default preset（相當於 Phase 1 行為）
    preset_routing_enabled: bool = True
    # 個別 preset 停用名單：例如 {"code_comment": False} 表示不路由到 code_comment
    # 未列出的 preset 視為啟用
    preset_overrides: dict = field(default_factory=dict)

    # ── #4 個人字典 ──────────────────────────────────────────────────────────
    # 啟用後字典內容會注入 Whisper initial_prompt 與 Ollama polish prompt
    dictionary_enabled: bool = True
    # 字典檔路徑；空字串代表用 ~/.whisper_app/dictionary.json 預設值
    dictionary_path: str = ""

    # ── #2 Prompt 熱重載 ─────────────────────────────────────────────────────
    # 啟用時 prompts.py / presets.py mtime 變化會自動 importlib.reload
    prompt_hot_reload: bool = True

    # ── #3 Polish log ────────────────────────────────────────────────────────
    # 啟用時每次潤飾完成會附加一行 JSONL 到 ~/.whisper_app/polish_log.jsonl
    polish_log_enabled: bool = True

    # ── persistence ──────────────────────────────────────────────────────────

    @classmethod
    def load(cls) -> "Config":
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if not CONFIG_PATH.exists():
            cfg = cls()
            cfg.save()
            return cfg
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            valid_data = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
            cfg = cls(**valid_data)
            
            # 安全檢查：強制修復會導致 macOS 衝突的熱鍵
            if cfg.hotkey == "cmd+shift+space":
                print("CONFIG: Detecting unstable hotkey 'cmd+shift+space', auto-resetting to 'cmd+alt+r'")
                cfg.hotkey = "cmd+alt+r"
                cfg.save()
                
            return cfg
        except Exception as e:
            print(f"CRITICAL: Config corrupt ({e}). Backing up and resetting.")
            try:
                CONFIG_PATH.rename(CONFIG_PATH.with_suffix(".json.bak"))
            except: pass
            cfg = cls()
            cfg.save()
            return cfg

    def save(self) -> None:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            temp_path = CONFIG_PATH.with_suffix(".tmp")
            temp_path.write_text(
                json.dumps(asdict(self), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            # 原子性替換，防止寫入一半斷電或崩潰
            temp_path.replace(CONFIG_PATH)
        except Exception as e:
            print(f"ERROR: Failed to save config: {e}")
            # 不拋出異常，避免 UI 因設定儲存失敗而卡死

    # ── helpers ───────────────────────────────────────────────────────────────

    def get_whisper_language(self) -> str | None:
        """Return the language code passed to Whisper, or None for auto."""
        return LANGUAGE_OPTIONS.get(self.language)

    def format_hotkey_display(self) -> str:
        """Format hotkey string for display, e.g. 'cmd+shift+space' → '⌘⇧Space'."""
        symbols = {
            "cmd":   "⌘",
            "ctrl":  "⌃",
            "alt":   "⌥",
            "shift": "⇧",
            "space": "Space",
        }
        parts = self.hotkey.lower().split("+")
        return "".join(symbols.get(p, p.capitalize()) for p in parts)
