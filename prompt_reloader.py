"""
Prompt 熱重載（#2）。

背景執行緒輪詢 prompts.py 與 presets.py 的 mtime；發現變化就 importlib.reload
對應模組。呼叫端必須用 `prompts.ATTR` 動態查詢語法（不能 `from prompts import ATTR`），
否則 reload 後仍指向舊 string。ollama_client / presets 皆已遵守此約定。

設計取捨：不用 watchdog（避免新依賴）；2 秒輪詢對開發迭代完全夠用、對 CPU
近乎 0 負擔。
"""

from __future__ import annotations

import contextlib
import importlib
import os
import threading
import time
from pathlib import Path
from typing import Callable, Iterator, Optional

_POLL_INTERVAL_SEC = 2.0

# Fix Cluster E / 2026-05-23：reload 與讀取 prompts.X 同步用的 RLock。
# `importlib.reload(mod)` 不是 atomic — 中途 module attribute 可能不一致或
# 短暫 AttributeError。Consumer（ollama_client）讀 prompts.ATTR 時用 `reload_lock()`
# context manager 互斥；reload 期間 consumer 短暫阻塞、reload 完成才繼續。
_RELOAD_LOCK = threading.RLock()


@contextlib.contextmanager
def reload_lock() -> Iterator[None]:
    """Consumer 讀 prompts.X 時包這個 context manager、與 reload 互斥。

    用 RLock 而非 Lock：reload_one 自己也呼叫 on_reload callback、callback 內若也
    讀 prompts.X 不會自鎖死。
    """
    _RELOAD_LOCK.acquire()
    try:
        yield
    finally:
        _RELOAD_LOCK.release()


class PromptReloader:
    """偵測 prompts.py / presets.py mtime 變化 → 自動 reload。"""

    def __init__(
        self,
        module_names: tuple[str, ...] = ("prompts", "presets"),
        on_reload: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._module_names = module_names
        self._on_reload    = on_reload
        self._mtimes:      dict[str, float] = {}
        self._thread:      Optional[threading.Thread] = None
        self._stop:        threading.Event = threading.Event()

    # ── public API ────────────────────────────────────────────────────────

    def start(self) -> None:
        # 記下初始 mtime，避免啟動瞬間誤判
        for name in self._module_names:
            path = self._path_for(name)
            if path is not None and path.exists():
                self._mtimes[name] = path.stat().st_mtime
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="PromptReloader",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def reload_now(self) -> list[str]:
        """手動觸發 reload（回傳實際 reload 的模組名）。"""
        reloaded = []
        for name in self._module_names:
            if self._reload_one(name):
                reloaded.append(name)
        return reloaded

    # ── internal ──────────────────────────────────────────────────────────

    @staticmethod
    def _path_for(module_name: str) -> Optional[Path]:
        try:
            mod = importlib.import_module(module_name)
            spec_file = getattr(mod, "__file__", None)
            return Path(spec_file) if spec_file else None
        except Exception:
            return None

    def _run(self) -> None:
        while not self._stop.wait(_POLL_INTERVAL_SEC):
            for name in self._module_names:
                path = self._path_for(name)
                if path is None or not path.exists():
                    continue
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                prev = self._mtimes.get(name)
                if prev is not None and mtime > prev + 0.001:
                    # mtime 有變化 → reload
                    if self._reload_one(name):
                        self._mtimes[name] = mtime
                else:
                    self._mtimes[name] = mtime

    def _reload_one(self, name: str) -> bool:
        # Cluster E：reload 期間鎖住，讓 consumer（ollama_client）讀 prompts.X 等
        with _RELOAD_LOCK:
            try:
                mod = importlib.import_module(name)
                importlib.reload(mod)
                print(f"PROMPT_RELOADER: reloaded {name}")
                if self._on_reload is not None:
                    try:
                        self._on_reload(name)
                    except Exception as e:
                        print(f"PROMPT_RELOADER: on_reload callback error: {e}")
                return True
            except Exception as e:
                print(f"PROMPT_RELOADER: reload {name} failed: {e}")
                return False
