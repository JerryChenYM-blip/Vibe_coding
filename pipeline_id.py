"""Pipeline ID：一次「按 hotkey → 錄音 → 轉錄 → 潤飾 → 貼上」的全流程 UUID。

格式：'tx_' + 6 字 base36（不用 uuid4 完整版、太長、log 難讀）

用法：
    import pipeline_id
    pid = pipeline_id.new_pipeline_id()
    pipeline_id.set_current(pid)
    pipeline_id.event('hotkey_pressed', hotkey='right_cmd')
    ...
    pipeline_id.set_current(None)  # 清除
"""
from __future__ import annotations

import os
import threading
from typing import Optional, Any

from logger import get_logger, log_error

log = get_logger("pipeline")

# Thread-local storage：每個 thread 各自有一份「目前 pipeline id」
_local = threading.local()

# base36 字母表
_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _to_base36(n: int) -> str:
    """正整數轉 base36 字串（小寫）。"""
    if n == 0:
        return "0"
    out: list[str] = []
    while n > 0:
        n, r = divmod(n, 36)
        out.append(_BASE36[r])
    return "".join(reversed(out))


def new_pipeline_id() -> str:
    """產生新 ID。例: 'tx_a1b2c3'。

    用 os.urandom(4) → int → base36 → 取前 6 字（不足前面補 0）。
    32-bit 隨機 → base36 最長 7 字、最短 1 字、平均 6 字。
    這個 ID 給人讀的、不是安全用途，碰撞機率夠用（4e9 空間 + session 內 < 1000 次轉錄）。
    """
    n = int.from_bytes(os.urandom(4), "big")
    s = _to_base36(n).rjust(6, "0")[:6]
    return f"tx_{s}"


def set_current(pid: Optional[str]) -> None:
    """寫入 thread-local（None 代表清除）。"""
    if pid is None:
        # 清除
        if hasattr(_local, "pid"):
            try:
                del _local.pid
            except Exception:
                _local.pid = None
    else:
        _local.pid = pid


def get_current() -> Optional[str]:
    """從 thread-local 取；無則 None。"""
    return getattr(_local, "pid", None)


def event(event_name: str, **fields: Any) -> None:
    """便利函式：log + audit 寫一筆 pipeline 事件。

    log.info(f'PIPELINE: {event_name} (id={pid}, {fields_str})')
    pid 自動帶 get_current()；無 pid 仍照印（不會炸）。
    同時呼叫 audit_log.write_event(event_name, pid, **fields)。

    用法：pipeline_id.event('hotkey_pressed', hotkey='right_cmd')

    任何失敗皆 silent — log 系統不能影響真正流程。
    """
    pid = get_current()

    # ── Log 輸出 ──────────────────────────────────────────────────────────
    try:
        if fields:
            kv = ", ".join(f"{k}={v}" for k, v in fields.items())
            log.info(f"PIPELINE: {event_name} (id={pid}, {kv})")
        else:
            log.info(f"PIPELINE: {event_name} (id={pid})")
    except Exception:
        # log 自己炸了也不能 raise
        try:
            log_error("pipeline_event_log_failed", event=event_name)
        except Exception:
            pass

    # ── Audit 寫入 ────────────────────────────────────────────────────────
    # 延後 import 避免 import cycle（audit_log import session_summary、
    # 三者皆 import logger，但 pipeline_id 不主動相依 audit_log 的模組級狀態）
    try:
        import audit_log  # noqa: PLC0415 — local import 防 cycle
        audit_log.write_event(event_name, pid, **fields)
    except Exception:
        # audit 失敗 silent — 真正的轉錄流程比 log 重要
        try:
            log_error("pipeline_event_audit_failed", event=event_name)
        except Exception:
            pass
