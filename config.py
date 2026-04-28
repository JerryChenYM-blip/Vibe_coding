"""
應用程式設定管理。

設定檔路徑：~/.whisper_app/config.json

讀寫策略：
  • 載入：解析 JSON，無效欄位靜默忽略（向前相容新版設定）
  • 儲存：先寫 .tmp 再原子性 replace()，防止寫入到一半時斷電毀檔
  • 損毀自救：JSON 解析失敗時，舊檔改名 .bak，用預設值重建

匯出：
  Config      設定 dataclass（讀寫介面）
  MODEL_INFO  模型名稱 → 說明文字的對照表
  LANGUAGE_OPTIONS  語言選項 → Whisper 語言代碼的對照表
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from logger import get_logger, log_error

log = get_logger("config")

# 設定檔所在目錄與路徑
CONFIG_DIR  = Path.home() / ".whisper_app"
CONFIG_PATH = CONFIG_DIR / "config.json"

# 每個 Whisper 模型的說明文字，顯示在 UI 的模型選單中
MODEL_INFO: dict[str, str] = {
    "tiny":           "速度最快，適合英文速記（39M 參數）",
    "base":           "速度與精準度平衡，推薦入門（74M 參數）",
    "small":          "較高精準度，中文效果更佳（244M 參數，GPU 4bit 量化）",
    "medium":         "高精準度，需較多記憶體（769M 參數，GPU 4bit 量化）",
    "large-v3-turbo": "★ 推薦：中英混合最佳，Metal GPU 加速（809M 參數）",
    "large-v3":       "最高精準度，Metal GPU 加速（1550M 參數，4bit 量化）",
}

# 語言顯示名稱 → Whisper 語言代碼（None 代表自動偵測）
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
    """所有使用者設定的 dataclass 容器。

    每個欄位都有合理的預設值，確保初次使用者無需手動設定即可正常運作。
    欄位分組：基本操作、Ollama 潤飾、Phase 2 preset 路由、個人字典、熱重載、Polish log。
    """

    # ── 基本操作設定 ──────────────────────────────────────────────────────────

    hotkey:       str           = "cmd+alt+r"        # 全域錄音快捷鍵
    model:        str           = "large-v3-turbo"   # Whisper 模型大小
    language:     str           = "自動偵測"          # 轉錄語言
    input_device: Optional[str] = None               # 麥克風裝置名稱（None = 系統預設）
    append_results: bool        = True               # 是否追加結果（vs. 覆蓋）
    auto_copy:    bool          = False              # 轉錄完成後是否自動複製到剪貼簿
    auto_paste:   bool          = True              # 轉錄完成後是否自動 ⌘V 貼入游標

    # ── Ollama AI 潤飾 ────────────────────────────────────────────────────────
    # 預設關閉；使用者需在設定中確認 Ollama 服務可用後才啟用。

    ollama_enabled:       bool  = False                    # 是否啟用 AI 潤飾
    ollama_model:         str   = "qwen2.5:3b-instruct"   # 潤飾用的 LLM 模型（3B 為 16GB Mac 最佳）
    ollama_base_url:      str   = "http://localhost:11434" # Ollama 服務地址
    ollama_timeout:       int   = 30                       # 潤飾超時秒數（超時降級回原文）
    # 貼上策略：
    #   "wait" — 等潤飾完成再貼（規劃書 6.4 建議預設，品質優先）
    #   "raw"  — 先貼原文，潤飾結果不再覆蓋（速度優先）
    ollama_paste_strategy: str  = "wait"

    # ── Phase 2 情境 preset 路由 ─────────────────────────────────────────────

    # 關閉時所有轉錄走 default preset（等同 Phase 1 行為）
    preset_routing_enabled: bool = True
    # 個別 preset 啟停：{"code_comment": False} = 不路由到 code_comment
    # 未列出的 preset 視為啟用（True）
    preset_overrides: dict = field(default_factory=dict)

    # ── #4 個人字典 ──────────────────────────────────────────────────────────

    # 啟用後字典術語注入 Whisper initial_prompt 與 Ollama polish prompt
    dictionary_enabled: bool = True
    # 自訂字典路徑；空字串代表使用預設路徑 ~/.whisper_app/dictionary.json
    dictionary_path: str = ""

    # ── #2 Prompt 熱重載 ─────────────────────────────────────────────────────

    # 啟用時 prompts.py / presets.py 的 mtime 變化會觸發自動 importlib.reload
    prompt_hot_reload: bool = True

    # ── #3 Polish log ────────────────────────────────────────────────────────

    # 啟用時每次潤飾完成後在 ~/.whisper_app/polish_log.jsonl 追加一行記錄
    polish_log_enabled: bool = True

    # ── Phase 3.2 歷史紀錄 ──────────────────────────────────────────────────

    # 啟用時每次轉錄（含潤飾）寫入 ~/.whisper_app/history.db
    history_enabled: bool = True
    # 保留天數；0 = 永久保留，>0 = 主視窗啟動時刪除 N 天前的紀錄
    history_retention_days: int = 0

    # ── Phase 4.3 浮動 mini 錄音窗 ──────────────────────────────────────────

    # 啟用後錄音 / 處理中會在螢幕右下角顯示小型 HUD（120×40 always-on-top）
    mini_recording_window: bool = False

    # ── 讀寫介面 ──────────────────────────────────────────────────────────────

    @classmethod
    def load(cls) -> "Config":
        """從 config.json 讀取設定，失敗時自救並回傳預設值。

        自救策略：
          1. 檔案不存在 → 建立預設值並儲存
          2. JSON 解析失敗 → 舊檔改名 .bak，重建預設值
          3. 含有舊版不安全熱鍵 → 自動重設為 cmd+alt+r

        Returns:
            Config 實例（保證可用，不會拋例外）。
        """
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

        # 第一次執行：設定檔不存在，用預設值建立
        if not CONFIG_PATH.exists():
            cfg = cls()
            cfg.save()
            return cfg

        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            # 只取 dataclass 有定義的欄位，忽略未知欄位（向前相容）
            valid_data = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
            cfg = cls(**valid_data)

            # 安全檢查：cmd+shift+space 與 macOS 輸入法熱鍵衝突，強制重設
            if cfg.hotkey == "cmd+shift+space":
                log.warning(
                    "CONFIG: Detecting unstable hotkey 'cmd+shift+space', "
                    "auto-resetting to 'cmd+alt+r'"
                )
                cfg.hotkey = "cmd+alt+r"
                cfg.save()

            return cfg
        except Exception as e:
            # 設定檔損毀：備份舊檔，重建預設值
            log.critical(f"CONFIG: corrupt ({e}). Backing up and resetting.")
            log_error("config_corrupt", path=str(CONFIG_PATH))
            try:
                CONFIG_PATH.rename(CONFIG_PATH.with_suffix(".json.bak"))
            except Exception:
                log_error("config_backup_rename_failed")
            cfg = cls()
            cfg.save()
            return cfg

    def save(self) -> None:
        """將目前設定原子性寫入 config.json。

        先寫 .tmp 再 replace()，確保即使寫到一半斷電，舊設定檔仍完整可讀。
        任何 I/O 失敗只 print 不拋例外，避免 UI 因設定儲存失敗而卡死。
        """
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            temp_path = CONFIG_PATH.with_suffix(".tmp")
            # 先寫暫存檔
            temp_path.write_text(
                json.dumps(asdict(self), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            # 原子性替換：POSIX 保證 rename 為原子操作
            temp_path.replace(CONFIG_PATH)
            log.debug(f"CONFIG: saved to {CONFIG_PATH}")
        except Exception:
            log_error("config_save_failed", path=str(CONFIG_PATH))

    # ── 查詢輔助 ──────────────────────────────────────────────────────────────

    def get_whisper_language(self) -> str | None:
        """回傳傳給 Whisper 的語言代碼，自動偵測時回傳 None。"""
        return LANGUAGE_OPTIONS.get(self.language)

    def format_hotkey_display(self) -> str:
        """將熱鍵字串格式化為 macOS 符號表示，例如 'cmd+alt+r' → '⌘⌥R'。

        支援 macOS 慣稱別名（option/command/control/opt 等）。為避免與
        hotkey_manager._SYMBOL_MAP 重複維護，此處 inline 同樣表。
        """
        symbols = {
            "cmd": "⌘", "command": "⌘",
            "ctrl": "⌃", "control": "⌃",
            "alt": "⌥", "option": "⌥", "opt": "⌥",
            "shift": "⇧",
            "space": "Space",
            "return": "↩", "enter": "↩",
            "tab": "⇥",
            "esc": "⎋", "escape": "⎋",
        }
        parts = self.hotkey.lower().split("+")
        return "".join(symbols.get(p, p.upper()) for p in parts)
