"""
統一的日誌系統 — Whisper Pro。

設計目標：
1. 檔案位置：`~/.whisper_app/logs/whisper_app.log`（不汙染專案目錄）
2. 自動 rotation：單檔 5 MB，保留 5 份（.log, .log.1, .log.2, ...）
3. 雙通道：檔案（DEBUG 以上）+ 終端（INFO 以上）
4. 結構化：時間戳 + level + 模組/函式 + 訊息
5. Helper：`log_action()` 統一格式化使用者動作與狀態轉換

使用方式：
    from logger import get_logger, log_action, log_error

    log = get_logger(__name__)
    log.info("something happened")
    log.warning("watch out")

    # 使用者動作
    log_action("record_button_clicked", state="idle", trigger="button")

    # 帶 stack trace 的錯誤
    try:
        ...
    except Exception:
        log_error("auto_paste_failed", app=target)

匯入 logger.py 時會立即初始化根 logger（setup_logging()），
所以任何檔案只要 `from logger import get_logger` 就可用。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

# ── 常數 ──────────────────────────────────────────────────────────────────────

LOG_DIR  = Path.home() / ".whisper_app" / "logs"
LOG_FILE = LOG_DIR / "whisper_app.log"

# 單檔最大 5 MB，保留最近 5 份（加起來約 25 MB 上限）
MAX_BYTES    = 5 * 1024 * 1024
BACKUP_COUNT = 5

# Root logger 名稱 — 所有子 logger 繼承此設定
ROOT_LOGGER_NAME = "whisper_pro"

# Format：時間精確到毫秒，方便排查 race condition
_FILE_FORMAT = (
    "[%(asctime)s.%(msecs)03d] [%(levelname)-7s] "
    "[%(name)s.%(funcName)s:%(lineno)d] %(message)s"
)
_CONSOLE_FORMAT = "[%(asctime)s] [%(levelname)s] %(message)s"
_DATEFMT        = "%Y-%m-%d %H:%M:%S"


_initialized = False


def setup_logging(
    console_level: int = logging.INFO,
    file_level:    int = logging.DEBUG,
) -> None:
    """初始化根 logger。多次呼叫安全（第二次之後直接 return）。"""
    global _initialized
    if _initialized:
        return

    # 建立 log 目錄（已存在不報錯）
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        # 退到 stderr — 沒有 log 目錄也不能讓 App 崩
        sys.stderr.write(f"[logger] Failed to create log dir {LOG_DIR}: {e}\n")

    root = logging.getLogger(ROOT_LOGGER_NAME)
    root.setLevel(logging.DEBUG)
    # 清除舊 handler（熱重載場景安全）
    root.handlers.clear()
    # 不要 propagate 到 Python 根 logger，避免 double log
    root.propagate = False

    # ── File handler（rotation）─────────────────────────────────────────
    try:
        fh = logging.handlers.RotatingFileHandler(
            LOG_FILE,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        fh.setLevel(file_level)
        fh.setFormatter(logging.Formatter(_FILE_FORMAT, datefmt=_DATEFMT))
        root.addHandler(fh)
    except Exception as e:
        sys.stderr.write(f"[logger] Failed to attach file handler: {e}\n")

    # ── Console handler（開發時看得到）──────────────────────────────────
    # 注意：使用 sys.__stderr__ 而非 sys.stderr，避免被 main.py 的
    # Logger 重導向時發生循環寫入（logger → stderr → Logger → log.info → ...）。
    ch = logging.StreamHandler(sys.__stderr__ or sys.stderr)
    ch.setLevel(console_level)
    ch.setFormatter(logging.Formatter(_CONSOLE_FORMAT, datefmt=_DATEFMT))
    root.addHandler(ch)

    _initialized = True


def get_logger(name: str) -> logging.Logger:
    """取得子 logger。name 通常傳 `__name__`，例如 `"gui"` → `whisper_pro.gui`。"""
    if not _initialized:
        setup_logging()
    # 把呼叫方的模組名掛在根 logger 之下
    short = name.replace("__main__", "main").split(".")[-1]
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{short}")


# ─────────────────────────────────────────────────────────────────────────────
#  Structured helpers — 使用者動作、設定變動、錯誤
# ─────────────────────────────────────────────────────────────────────────────

def _kv(details: dict[str, Any]) -> str:
    """把 dict 格式化成 `key=value key2=value2` 的字串，方便 grep。"""
    if not details:
        return ""
    parts: list[str] = []
    for k, v in details.items():
        # 字串有空白就加引號，讓 log 好 parse
        if isinstance(v, str) and (" " in v or "=" in v):
            parts.append(f'{k}="{v}"')
        else:
            parts.append(f"{k}={v}")
    return " " + " ".join(parts)


def log_action(action: str, **details: Any) -> None:
    """記錄「使用者動作」— 按鈕點擊、熱鍵觸發、設定變動等。

    Format: `USER_ACTION: <action> key1=value1 key2=value2 ...`
    """
    log = get_logger("action")
    log.info(f"USER_ACTION: {action}{_kv(details)}")


def log_state(transition: str, **details: Any) -> None:
    """記錄 UI 狀態轉換。Format: `STATE: <transition> ...`"""
    log = get_logger("state")
    log.info(f"STATE: {transition}{_kv(details)}")


def log_settings(action: str, **details: Any) -> None:
    """記錄設定變動。Format: `SETTINGS: <action> ...`

    例：log_settings("changed", field="model", old="small", new="large-v3-turbo")
    """
    log = get_logger("settings")
    log.info(f"SETTINGS: {action}{_kv(details)}")


def log_error(what: str, exc_info: bool = True, **details: Any) -> None:
    """記錄錯誤並保留 stack trace（如在 except 區塊中呼叫）。

    Format: `ERROR: <what> ...` + traceback（若有）
    """
    log = get_logger("error")
    log.error(f"ERROR: {what}{_kv(details)}", exc_info=exc_info)


# ─────────────────────────────────────────────────────────────────────────────
#  Self-init — 只要 import 就能用
# ─────────────────────────────────────────────────────────────────────────────

setup_logging()
