# Plan：Whisper Pro 穩定性修補 — Listener Watchdog + 狀態機自癒

> 建立於：2026-05-21
> 分支：`feat/right-cmd-hotkey-and-tsm-fixes`（PR #13 in flight）
> 範圍決定：**B — 4 個獨立小手術**（使用者於 /plan-eng-review D1 拍板）

---

## 1. 背景與症狀

使用者實測回報（2026-05-21）：

> 多切幾個視窗以後它好像就會失效。就是它快捷鍵就不會動了。然後好像翻譯也不太正常。然後它整個的效果就失效了。然後我是把它重新關閉、重新打開以後它才恢復正常。

三個症狀，全部對應「**背景執行緒死了沒人重啟**」這個 anti-pattern。

---

## 2. 根因分析

### 證據 A：pynput Listener 無 watchdog

**`gui.py:1779-1783`**
```python
def _start_hotkey_listener(self) -> None:
    if not is_pynput_available():
        return
    self.hotkey_mgr.restart(self.cfg.hotkey)
```

只在 (1) App 啟動 + (2) 設定儲存時呼叫。**沒有定期 health check**。

**`hotkey_manager.py:399-407`**
```python
self._listener = _kb.Listener(...)
self._listener.daemon = True
self._listener.start()
log.info(f"HOTKEY: listener fully started at t=...")
# ↑ start() 完就走了，沒人持續看 self._listener.running
```

macOS 在以下情境會讓 CGEventTap 失效：
- Focus 切換到 secure input 欄位（密碼輸入框）
- TCC 權限狀態變更
- 系統休眠 / 喚醒
- Accessibility daemon 重啟

pynput 內部沒有自動恢復；Listener 安靜地 `run()` 結束，thread 退出，**沒有任何 log**（這是最坑的地方 — 你 log 撈不到死因）。

### 證據 B：pynput callback 完全沒包 try/except

**`hotkey_manager.py:434-512`**
```python
def _on_p(self, key) -> None:
    if not self._first_press_logged:
        self._first_press_logged = True
        log.info(...)  # 這行如果 throw（例如 logger handler 被砍），整個 listener 死

    if self._is_lone_mode:
        self._on_p_lone(key)
        return

    nk  = self._normalize(key)   # 罕見鍵事件可能 throw
    ...
```

整個函式體沒包 try/except。pynput 內部會接住 callback 例外，但行為是「停止 listener」（依 pynput 版本而異）。**任何 callback exception = listener 永久死亡**。

### 證據 C：Whisper 推論執行緒沒例外處理

**`gui.py:787-807`**
```python
def _run_transcription(self, audio, model: str, lang) -> None:
    result = self.transcriber.transcribe(audio, model_size=model, language=lang)
    # ↑ 模型載入失敗 / OOM / VAD 異常都會 throw
    prior = list(self._stream_chunks)
    ...
    self.after(0, self._on_transcription_done, result)  # 永遠不會到這
```

如果 `transcribe()` throw：
1. `self.after(0, self._on_transcription_done, ...)` 永不執行
2. 狀態機卡在 `processing`
3. 下一個 hotkey tap 觸發 `_on_record_btn` → 走到 `elif self._state == "recording"` 分支 → 但 state 是 `processing`，所有分支都不 match → **靜默吃掉**
4. 使用者：「按了沒反應」

### 證據 D：狀態機無超時自癒

**`gui.py:613, 676, 714`** — 三個 transition 方法各自有 re-entry guard：
```python
def _transition_to_recording(self) -> None:
    if self._state != "idle":
        log.debug(f"_transition_to_recording ignored (state={self._state})")
        return
```

合理（防重入），**但缺一個保護**：如果狀態被 stuck，沒有超時恢復機制。`HotkeyManager` 對 `_combo_active` 有 `_STALE_COMBO_SEC=10` 自癒，但 `_state` 沒有對應的保護。

---

## 3. 四個獨立修補（核心 deliverable）

### Fix 1 — Listener Watchdog

**檔案**：`gui.py`
**改動規模**：~15 行
**位置**：在 `_start_hotkey_listener` 後加 `_hotkey_watchdog` + 在 `__init__` 啟動

```python
# 新增（gui.py）
HOTKEY_WATCHDOG_INTERVAL_MS = 5000   # 5 秒查一次

def _hotkey_watchdog(self) -> None:
    """週期性檢查 pynput Listener 還活著嗎；死了就 restart。

    macOS CGEventTap 在 focus 切換、TCC state change、sleep/wake 後
    偶爾會失效。pynput 內部不會自動恢復，listener thread 安靜結束。
    這個 watchdog 是補救網。
    """
    try:
        mgr = self.hotkey_mgr
        if is_pynput_available() and mgr._listener is not None:
            if not mgr._listener.running:
                log.warning("HOTKEY: listener died unexpectedly — restarting")
                mgr.restart(self.cfg.hotkey)
                log_action("hotkey_listener_auto_restarted")
    except Exception:
        log_error("hotkey_watchdog_failed")
    finally:
        self.after(self.HOTKEY_WATCHDOG_INTERVAL_MS, self._hotkey_watchdog)

# __init__ 末段排程啟動
self.after(self.HOTKEY_WATCHDOG_INTERVAL_MS, self._hotkey_watchdog)
```

**為何 5 秒**：權衡使用者感知（10 秒+ 太久）vs CPU 開銷（< 1 秒太密集，無意義）。`_listener.running` 是 atomic bool 檢查，~µs 級。

**Revert 方式**：刪除 `_hotkey_watchdog` 方法 + `__init__` 那行 `self.after(...)`。零依賴變更。

**風險**：低。`mgr.restart()` 已在使用者改設定時用過、被驗證過。

---

### Fix 2 — pynput Callback 例外包覆

**檔案**：`hotkey_manager.py`
**改動規模**：~10 行（外層 wrap，內部不動）
**位置**：`_on_p`（line 434）、`_on_r`（line 514 後）

```python
def _on_p(self, key) -> None:
    try:
        # ── 既有 _on_p 整段內容 ──
        if not self._first_press_logged:
            ...
        if self._is_lone_mode:
            self._on_p_lone(key)
            return
        ...
    except Exception:
        log_error("hotkey_on_p_failed", key=repr(key))

def _on_r(self, key) -> None:
    try:
        # ── 既有 _on_r 整段內容 ──
        ...
    except Exception:
        log_error("hotkey_on_r_failed", key=repr(key))
```

**`_on_p_lone` / `_on_r_lone` 也同樣處理**（共 4 個函式）。

**為何不傳遞 exception**：pynput 內部行為依版本而異（有版本會 stop listener，有版本繼續但 swallow）。**統一在我們這邊吃掉 + log_error，配合 Fix 1 的 watchdog**，即使 logger 自己也壞掉 watchdog 還是會接住。

**Revert 方式**：拿掉 try/except 包覆。

**風險**：極低 — 純防護加法，不改任何邏輯。

---

### Fix 3 — Whisper 推論執行緒例外包覆

**檔案**：`gui.py`
**改動規模**：~20 行
**位置**：`_run_transcription`（line 787）

```python
def _run_transcription(self, audio, model: str, lang) -> None:
    try:
        result = self.transcriber.transcribe(audio, model_size=model, language=lang)

        prior = list(self._stream_chunks)
        if prior:
            ...
        self.after(0, self._on_transcription_done, result)
    except Exception as e:
        log_error("transcription_failed", model=model, error=str(e))
        # marshal 回主執行緒，把狀態從 processing 切回 idle
        self.after(0, self._on_transcription_failed, str(e))

def _on_transcription_failed(self, err_msg: str) -> None:
    """主執行緒：轉錄失敗時把狀態切回 idle，並顯示 toast。"""
    if self._state == "processing":
        # 用空結果觸發 idle transition（沿用既有的 _transition_to_idle 路徑）
        empty = TranscriptionResult(
            text="（轉錄失敗，請查看 log）",
            language="", duration_seconds=0.0,
            elapsed_seconds=0.0, segments=[],
        )
        self._transition_to_idle(empty)
    self._show_toast("⚠ 轉錄失敗")
```

**Revert 方式**：拿掉 try/except + `_on_transcription_failed` 方法。

**風險**：低 — 沿用既有 `_transition_to_idle` 路徑，沒新狀態。

---

### Fix 4 — Processing 狀態超時自癒

**檔案**：`gui.py`
**改動規模**：~25 行
**位置**：`_transition_to_processing`（line 676）加開排程；新增 `_processing_timeout_check`

```python
# 常數
PROCESSING_TIMEOUT_MS = 60_000   # 60 秒
# 60s 對 large-v3-turbo 是非常安全的上限：
# - 即使錄 60s 音檔，MLX 推論也只要 ~6s
# - cold start warmup 額外 ~5-10s
# - 60s 仍留 40s+ buffer 給極端情況

def _transition_to_processing(self) -> None:
    # ... 既有邏輯 ...
    self._state = "processing"
    self._processing_started_at = time.monotonic()
    self.after(self.PROCESSING_TIMEOUT_MS, self._processing_timeout_check)
    # ...

def _processing_timeout_check(self) -> None:
    """processing 狀態超過 60s 沒結束 → 強制切回 idle。"""
    if self._state != "processing":
        return   # 已正常結束，無事
    elapsed = time.monotonic() - getattr(self, "_processing_started_at", 0)
    if elapsed >= self.PROCESSING_TIMEOUT_MS / 1000:
        log.warning(f"STATE: processing stuck >{elapsed:.0f}s — force idle")
        log_action("state_processing_timeout_recovered")
        empty = TranscriptionResult(
            text="（轉錄超時，請重試）",
            language="", duration_seconds=0.0,
            elapsed_seconds=elapsed, segments=[],
        )
        self._transition_to_idle(empty)
        self._show_toast("⏱ 轉錄超時 — 已自動恢復")
```

**為何 60s**：
- MLX 對 large-v3-turbo：RTF ≈ 0.1，60s 音訊 → 6s 推論
- cold start：+5-10s
- 60s 有 40s 緩衝，避免誤殺正常推論

**Revert 方式**：拿掉 `PROCESSING_TIMEOUT_MS`、`_processing_started_at`、`_processing_timeout_check`，`_transition_to_processing` 拿掉那行排程。

**風險**：低-中
- 風險點：如果使用者刻意跑超長音檔（10+ 分鐘）+ 用大 model，60s 可能誤殺。
- 緩解：先用 60s 觀察 1-2 週，使用者沒誤殺再說。若有，改成 120s 或設計成可設定。

---

## 4. 實作順序（依風險升序）

| # | Fix | 改動行數 | 風險 | 建議順序 |
|---|---|---|---|---|
| 2 | pynput callback try/except | ~10 | 極低 | **第 1 個做** — 純防護加法 |
| 3 | Whisper thread try/except | ~20 | 低 | 第 2 個做 — 沿用既有狀態恢復 |
| 1 | Listener watchdog | ~15 | 低 | 第 3 個做 — 需要 Fix 2 在底下保護 |
| 4 | Processing 超時自癒 | ~25 | 低-中 | **最後做** — 唯一可能誤殺正常使用的（如果使用者刻意跑超長音）|

**驗收建議**：每個 fix 完成後**立刻測試一次**完整錄音 → 轉錄 → 貼上流程，確認沒回歸再進下一個 fix。

---

## 5. 測試計畫

### 5.1 自動化測試（unit-level，pytest）

新檔 `tests/test_stability.py`：

```python
# Fix 2 — callback 例外不殺死 listener
def test_pynput_callback_exception_swallowed():
    mgr = HotkeyManager(on_tap_cb=lambda: None)
    mgr._normalize = Mock(side_effect=RuntimeError("boom"))
    # 不應拋出
    mgr._on_p("fake_key")
    mgr._on_r("fake_key")
    # listener 還活著
    assert mgr._listener is None or mgr._listener.running  # 視 fixture

# Fix 3 — 轉錄失敗能切回 idle
def test_transcription_failure_recovers_state():
    win = make_test_window()
    win._state = "processing"
    win._on_transcription_failed("test error")
    assert win._state == "idle"

# Fix 4 — processing 超過 60s 自動恢復
def test_processing_timeout_recovers():
    win = make_test_window()
    win._transition_to_processing()
    win._processing_started_at = time.monotonic() - 70  # 偽造 70s 前
    win._processing_timeout_check()
    assert win._state == "idle"
```

### 5.2 人工 regression 測試

| 測試 | 步驟 | 預期 |
|---|---|---|
| **T1 切視窗 hotkey 不死** | 開 App → 在 5 個 App 之間切 20 次 → 按 hotkey | 仍能觸發錄音 |
| **T2 listener 自動 restart** | 強制 kill pynput thread（gdb 注入或硬等到自然死）→ 等 10 秒 | log 有 `HOTKEY: listener died unexpectedly — restarting`，hotkey 恢復 |
| **T3 轉錄失敗恢復** | 改 transcriber.py 暫時注入 `raise RuntimeError` → 錄音 → 觸發轉錄 | state 切回 idle，toast 顯示「⚠ 轉錄失敗」，下一次錄音正常 |
| **T4 processing 超時** | 模擬：在 `_transition_to_processing` 後不呼叫 `_on_transcription_done` → 等 60 秒 | log 有 `STATE: processing stuck >60s — force idle`，state 切回 idle |

---

## 6. NOT in scope（明確 deferred）

| 項目 | 為何不做 |
|---|---|
| 全面執行緒模型重構 | 範圍 B 決議：targeted fix，不擴大 |
| Focus/Visibility event 訂閱 | watchdog 已涵蓋；focus event 在 customtkinter 上的可靠性還沒實證 |
| Whisper 推論進度回報 | UI 改動範圍大，現在的「processing」spinner 夠用 |
| 設定可調 timeout | YAGNI — 60s 應該夠絕大多數情境；之後真有誤殺再加 |
| 自動 bug report | 改 log_error 介面，scope 大 |

---

## 7. 已存在可重用元件

| 需要 | 既有 | 位置 |
|---|---|---|
| 結構化 error log | `log_error(action, **kwargs)` | logger.py |
| 結構化 action log | `log_action(action, **kwargs)` | logger.py |
| Toast 通知 | `self._show_toast(text)` | gui.py |
| 狀態回 idle | `self._transition_to_idle(result)` | gui.py:714 |
| Self-heal pattern 參考 | `_STALE_COMBO_SEC` + healed flag | hotkey_manager.py:462-468 |

---

## 8. 失敗模式分析（critical gaps）

| 新增的失敗模式 | 有測試 | 有錯誤處理 | 使用者看得到錯誤 |
|---|---|---|---|
| Watchdog 自己 throw exception | 否（手測） | ✅ 包 try/except + log | ❌（log 才有，UI 無感）|
| 轉錄失敗 toast | ✅ T3 | ✅ | ✅ toast「⚠ 轉錄失敗」|
| 60s 誤殺正常推論 | 部分（T4 邊界） | ✅ idle + toast | ✅ toast「⏱ 轉錄超時」|
| Listener restart 失敗 | 否（手測） | ✅ log_error | ❌（log 才有）|

**Critical gap**：Watchdog 自己壞掉的話 UI 完全無感。但這比現在好（現在連 listener 死掉都無感）。**接受這個限制**，後續觀察 log 中 `hotkey_watchdog_failed` 出現頻率再加 UI 警示。

---

## 9. 改動檔案總覽

| 檔案 | 行數變動 | 性質 |
|---|---|---|
| `hotkey_manager.py` | +10 | Fix 2（callback try/except wrap）|
| `gui.py` | +60 | Fix 1 + 3 + 4 |
| `tests/test_stability.py` | +120（新檔）| 5.1 全部 |
| `CLAUDE.md` | +5（§9 加 1 條「常見坑」）| 文件補充 |

**總計：~195 行新增，0 行刪除，4 個既有檔案動到（其中 1 個只加註解）。**

---

## 10. 風險與回滾策略

每個 fix 都設計成「**獨立可 revert**」：
- 4 個 fix 各自一個 commit
- 若任一 fix 造成回歸 → `git revert <commit>` 單獨退掉即可
- 不互相依賴（Fix 1 不依賴 Fix 2 才能跑，只是 Fix 2 讓 Fix 1 更穩）

**最壞情境**：4 個 fix 全 revert，回到 PR #13 d8b3a6e 狀態，使用者損失 = 0。

---

## 11. 下一步

待使用者拍板：
1. 拍板「**開始實作**」→ 我會：
   - 依序做 Fix 2 → 3 → 1 → 4，每個一個 commit
   - 每個 fix 完成 sub-agent 驗證後 push
   - 補進 PR #13
2. 拍板「**先暫緩**」→ 留 plan 檔在這，之後再回來
3. 拍板「**只做 Fix 2 + 3**」（最低風險 subset）→ 不做 watchdog 和超時自癒，看 1-2 週使用情況再決定要不要做剩下

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR (PLAN) | 4 issues (listener watchdog, callback try/except, transcription try/except, processing timeout); 4 fixes proposed, all approved at scope-level (D1=B) |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

**UNRESOLVED**: 0
**VERDICT**: ENG CLEARED — 4 fix 已詳細規劃並等使用者拍板實作順序
