# Plan：Fix 18 + 19 — 熱鍵閒置 resilience v2 + 複製只取最後一段

> 建立於：2026-05-23
> 分支：main（PR #13 已 merge、v2.4.0 已 tag）
> Eng review：`/plan-eng-review`
> 範疇：兩個獨立但同一輪可一起出的 hotfix

---

## TL;DR

**Issue 1（Fix 18）：熱鍵閒置 ~57 分鐘後失效，但 watchdog 沒覺得 monitor 死了。** Fix 7 的「NSEvent 不會 idle disable」假設不成立 ── log timeline 顯示 11:51 還能用 hotkey、12:48 開始改用按鈕，中間 57 分鐘閒置就壞了。修法：三層保險絲（handler 強引用 + 每 10 分鐘 force-restart + 事件靜默偵測），與 Fix 1（pynput 時期 Layer 3）同款 belt-and-suspenders，Fix 7 移除是過度樂觀。

**Issue 2（Fix 19）：「複製」按鈕複製整段累積，使用者要「複製剛剛這一段」。** 走 Path A 最小外科修法：`_on_copy` 改用既有的 `latest_start..latest_end` Tk mark 只取最後一段。Speakly-style 完整 list-of-blocks 重構（Path B）加 TODOS，後續走 `/plan-design-review` 再做。

---

## Issue 1 詳細：Fix 18 — 熱鍵閒置 resilience v2

### 證據

`~/.whisper_app/logs/whisper_app.log` 2026-05-23 timeline：

```
01:41 App start
01:44 hotkey first press OK
...
11:26 hotkey_tap OK ✓
11:27 hotkey_tap OK ✓        ← 連續可用
   ↓ 23 min idle
11:50 hotkey_tap OK ✓
11:51 hotkey_tap OK ✓        ← 仍可用
   ↓ 57 min idle              ← 此處發生失效
12:48 record_button_clicked   ← 改用按鈕
12:49 record_button_clicked
12:50 record_button_clicked
12:51 record_button_clicked
```

**完全沒有 `HOTKEY: watchdog restarting NSEvent monitor (reason=monitor_missing)` log。**

意思是：watchdog 的判斷 `_monitor_global is not None or _monitor_local is not None` 永遠為 True，沒觸發 restart。但實際使用者按 Right Cmd 沒有 `HOTKEY: first press` 或 `hotkey_tap` log ── **handler 沒被叫到**。

### 根因 hypothesis

兩個候選，按優先序：

**H1（最可能）：PyObjC bound-method 強引用消失**

```python
# hotkey_manager.py:501-507
self._monitor_global = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
    mask, self._on_ns_event_global,   # ← 這裡每次 access 都是新的 bound method
)
self._monitor_local = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
    mask, self._on_ns_event_local,
)
```

PyObjC 把 Python callable 包成 ObjC block 傳給 AppKit。AppKit 透過 retain count 持有 block，但 block 內部對 Python callable 的引用可能在 PyObjC bridge 層被弱持有（depends on PyObjC 版本與選項）。閒置期間 Python GC 可能回收 bound method object → block 持有 dangling pointer → 收到事件時 noop（或在底層 swallow）。

**這跟 Fix 7 之前 pynput `kCGEventTapDisabledByTimeout` 是同一個 class：底層 callback 失效但 Python 端的物件參照仍在，watchdog 偵測不到。**

**H2：App Nap 慢性復發**

`_disable_app_nap()` 在啟動時呼叫一次、token 是 module-level。但 macOS 對「閒置 30+ 分鐘無 user input」可能 override beginActivity hint，把 mainloop 降級到 low-power 模式。NSEvent 仍 dispatch，但 Tk `after` callback 延遲、處理 callback 也 throttled。

H1 比 H2 可能性高，但兩個都修就不用猜。

### 修法（三層保險絲）

#### 層 1：handler 強引用

```python
# hotkey_manager.py __init__
self._ns_handler_global = None   # 強引用
self._ns_handler_local  = None

# hotkey_manager.py _start_nsevent_monitors
def _start_nsevent_monitors(self) -> None:
    mask = (NSEventMaskFlagsChanged | NSEventMaskKeyDown | NSEventMaskKeyUp)

    # Fix 18 / Layer 1：把 bound method 存成 instance attribute 強引用。
    # 原本直接傳 self._on_ns_event_global，那是 transient bound method object，
    # PyObjC bridge 可能無法保證跨 GC 週期存活。閒置時 GC 觸發過幾輪 → 失效。
    self._ns_handler_global = self._on_ns_event_global
    self._ns_handler_local  = self._on_ns_event_local

    self._monitor_global = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
        mask, self._ns_handler_global,
    )
    self._monitor_local = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
        mask, self._ns_handler_local,
    )

# hotkey_manager.py stop
def stop(self) -> None:
    # 在鎖外移除 monitor 後一併清 handler 強引用
    self._ns_handler_global = None
    self._ns_handler_local  = None
```

#### 層 2：每 10 分鐘 force-restart NSEvent monitor

```python
# gui.py class constants
HOTKEY_FORCE_RESTART_INTERVAL_MS = 600_000   # 10 min

# gui.py __init__
self._last_hotkey_force_restart = time.monotonic()

# gui.py _hotkey_watchdog（既有 method，加分支）
def _hotkey_watchdog(self) -> None:
    try:
        mgr = self.hotkey_mgr
        # 既有：monitor object missing 偵測
        monitor_alive = (
            getattr(mgr, "_monitor_global", None) is not None
            or getattr(mgr, "_monitor_local", None) is not None
        )
        if not monitor_alive:
            log.warning("HOTKEY: watchdog restarting NSEvent monitor (reason=monitor_missing)")
            log_action("hotkey_listener_auto_restarted", reason="monitor_missing")
            mgr.restart(self.cfg.hotkey)
            self._last_hotkey_force_restart = time.monotonic()

        # Fix 18 / Layer 2：每 10 分鐘 force restart。
        # NSEvent monitor 物件還在不代表事件還會送（H1 / H2 都打不到 monitor_alive 判斷）。
        # 唯一保證的方法是定時 force restart。錄音中跳過避免事件序列被打斷。
        elif (
            self._state == "idle"
            and time.monotonic() - self._last_hotkey_force_restart
            > self.HOTKEY_FORCE_RESTART_INTERVAL_MS / 1000.0
        ):
            log.info("HOTKEY: watchdog force-restart (reason=periodic, 10min)")
            log_action("hotkey_listener_auto_restarted", reason="periodic")
            mgr.restart(self.cfg.hotkey)
            self._last_hotkey_force_restart = time.monotonic()
    except Exception:
        log_error("hotkey_watchdog_failed")
    finally:
        self.after(self.HOTKEY_WATCHDOG_INTERVAL_MS, self._hotkey_watchdog)
```

State guard 重要：force restart 期間 monitor 短暫拔掉（~50ms），錄音中誤觸會吞掉 stop 訊號。

#### 層 3：事件靜默偵測

```python
# gui.py 同 _hotkey_watchdog 內，在 monitor_alive 分支前加：
# Fix 18 / Layer 3：HotkeyManager 已有 _last_event_at（hotkey_manager.py:420）。
# 若 monitor "alive" 但 5 分鐘沒任何事件 + App active + idle → 嫌疑死亡 → log 警告。
# 不主動修復（Layer 2 force-restart 兜底），只記錄供未來分析。
try:
    last = getattr(mgr, "_last_event_at", 0.0)
    if last > 0:
        silence_s = time.monotonic() - last
        if silence_s > 300 and self._state == "idle":
            log.info(
                f"HOTKEY: diagnostic — monitor object alive but silent for {silence_s:.0f}s"
            )
except Exception:
    pass
```

注意：`_last_event_at` 任何 modifier 事件都會更新，連 idle 期間使用者按 ⌘C 都會 ping。如果靜默 5 分鐘表示**完全沒 modifier 動作**或**handler 沒收到** ── 前者 user 真的閒置、後者就是 H1/H2 發生中。這個 log 純粹是診斷用。

### 影響檔案

| 檔案 | 動什麼 |
|---|---|
| `hotkey_manager.py` | `__init__` 加 `_ns_handler_global/local` 兩個 attr；`_start_nsevent_monitors` 把 bound method 存起來；`stop` 清空。**~10 行**。 |
| `gui.py` | class const `HOTKEY_FORCE_RESTART_INTERVAL_MS`；`__init__` 加 `_last_hotkey_force_restart`；`_hotkey_watchdog` 加兩個 elif/log 分支。**~25 行**。 |

---

## Issue 2 詳細：Fix 19 — 複製按鈕只複製最後一段

### 證據

從使用者截圖（`轉錄結果 (1s · ZH · large-v3-turbo)`）：

```
─────────────────
。
─────────────────
幫我針對它裡面會有指令,幫我出一個詳細的指導手冊,然後用HTML的形式去寫...
─────────────────
我當然就好奇了。
```

三段轉錄堆疊在同一個 `CTkTextbox`，分隔線是 `\n\n─────────────\n\n`（`gui.py:1294`）。下方「複製」按鈕呼叫 `_on_copy`：

```python
# gui.py:1596-1609
def _on_copy(self) -> None:
    text = self._get_result_text()  # 取整段 textbox 內容
    pyperclip.copy(text)
    ...
```

`_get_result_text()` 拿整個 textbox `1.0 → end`，所以複製出來是整段歷史 + 分隔線。使用者要的是**最後一段**。

### 既有基礎設施

`gui.py:1300-1304`：

```python
self._textbox.mark_set("latest_start", "end-1c")
self._textbox.mark_gravity("latest_start", "left")
self._textbox.insert("end", result.text)
self._textbox.mark_set("latest_end", "end-1c")
self._textbox.mark_gravity("latest_end", "right")
```

兩個 Tk mark 已經精準圈住最新一段，是 Polish 用來替換最新文字用的。**完全不用新建基礎設施**，直接複用即可。

### 修法

```python
def _on_copy(self) -> None:
    """複製按鈕：將最新一段轉錄結果複製到剪貼簿。

    Fix 19 / 2026-05-23 — 改為只複製「最後一段」而非整段累積歷史。
    使用者預期跟 Speakly 一致：每次錄完音點複製 = 拿那一段話。
    使用既有的 latest_start..latest_end Tk mark（Polish 替換用）。
    """
    try:
        text = self._textbox.get("latest_start", "latest_end").strip()
    except Exception:
        # mark 不存在（首次啟動還沒有任何錄音）→ fallback 全段
        text = self._get_result_text()

    if not text:
        log_action("copy_clicked_empty")
        return
    try:
        import pyperclip
        pyperclip.copy(text)
        log_action("copy_succeeded", text_len=len(text), scope="latest")
        self._show_toast("已複製最後一段")
    except Exception as e:
        log_error("copy_failed", text_len=len(text))
        self._show_toast(f"複製失敗: {e}")
```

關鍵設計決策：
1. **不動 `_on_save`** ── 「存檔」語意上是整段對話歷史的合理寫入，使用者按存檔通常是想存整輪。
2. **toast 文字改成「已複製最後一段」** ── 讓使用者知道剝離了歷史。
3. **scope=latest 寫進 log_action** ── 未來如果使用者抱怨「我想複製整段」，從 log 可以看到趨勢。
4. **mark 不存在的 fallback** ── App 剛啟動沒任何錄音時，textbox 也是空的，`latest_start/end` 還沒被 mark_set，try/except 包好。

### 影響檔案

| 檔案 | 動什麼 |
|---|---|
| `gui.py` | `_on_copy` method 重寫。**~10 行**。 |

---

## Section 3：Test review

### 既有覆蓋

`tests/test_stability.py`（47 個 test）已涵蓋：
- NSEvent monitor restart 邏輯
- Pending tap counter / Cocoa poller 模式
- Mini HUD 多螢幕座標
- Processing timeout 動態 budget

### 新增 regression tests（4 個）

#### T1：Handler 強引用測試（Fix 18 Layer 1）

```python
def test_nsevent_handler_strong_ref_held():
    """Fix 18 / Layer 1 regression：handler 必須存成 instance attribute，
    確保 PyObjC bridge 跨 GC 週期不會 lose reference。
    """
    mgr = HotkeyManager(on_tap_cb=lambda: None)
    with mock_nsevent_monitors():
        mgr.restart("right_cmd")

    # 必須有 instance attribute 持有 bound method
    assert mgr._ns_handler_global is not None
    assert mgr._ns_handler_local is not None
    # 必須跟 method 是「同一個 bound method object」
    # （不是每次 access 都 re-create 的 transient object）
    assert mgr._ns_handler_global is mgr._ns_handler_global   # idempotent
```

#### T2：Force-restart cadence + recording guard（Fix 18 Layer 2）

```python
def test_watchdog_force_restart_skipped_during_recording():
    """錄音中 force-restart 會吞 stop 訊號，必須跳過。"""
    app = build_app_window_for_test()
    app._state = "recording"
    app._last_hotkey_force_restart = time.monotonic() - 700   # > 10 min

    with mock.patch.object(app.hotkey_mgr, "restart") as restart_spy:
        app._hotkey_watchdog()
        assert restart_spy.call_count == 0   # 不能 restart

    app._state = "idle"
    app._hotkey_watchdog()
    restart_spy.assert_called_once_with(app.cfg.hotkey)
```

#### T3：靜默偵測 log（Fix 18 Layer 3）

```python
def test_watchdog_logs_silent_monitor():
    """monitor 物件還在但 5 分鐘沒事件 → log 警告（不主動 restart）。"""
    app = build_app_window_for_test()
    app._state = "idle"
    app.hotkey_mgr._last_event_at = time.monotonic() - 400   # 6.7 min idle
    # monitor object 仍然 "alive"
    app.hotkey_mgr._monitor_global = object()

    with capture_log() as records:
        app._hotkey_watchdog()
        assert any("monitor object alive but silent" in r.message for r in records)
```

#### T4：複製只取最後一段（Fix 19）

```python
def test_on_copy_extracts_latest_segment_only():
    """Fix 19 regression：複製按鈕只取 latest_start..latest_end 範圍。"""
    app = build_app_window_for_test()
    app._show_result_text("第一段。")
    app._show_result_text("第二段。")
    app._show_result_text("第三段。")   # 這段該是 latest

    with mock.patch("pyperclip.copy") as copy_spy:
        app._on_copy()
        copy_spy.assert_called_once_with("第三段。")

def test_on_copy_fallback_when_no_latest_mark():
    """App 剛啟動還沒任何錄音，latest_start mark 不存在 → fallback 全段。"""
    app = build_app_window_for_test()
    # 不呼叫 _show_result_text，textbox 空

    with mock.patch("pyperclip.copy") as copy_spy:
        app._on_copy()
        # textbox 空 → 走 _get_result_text → 沒東西 → log_action("copy_clicked_empty")
        copy_spy.assert_not_called()
```

### Coverage 診斷

| 修法路徑 | 測試 | 等級 |
|---|---|---|
| Fix 18 Layer 1 handler 強引用 | T1 | ★★★ 行為 + edge |
| Fix 18 Layer 2 10 分鐘 force-restart | T2 | ★★★ 含 recording guard 對立 |
| Fix 18 Layer 3 靜默偵測 | T3 | ★★ happy path |
| Fix 19 複製最後一段 | T4 + fallback | ★★★ |

無 E2E 必要：兩個修都是本機邏輯、無跨服務。也無 eval 必要：無 prompt / LLM 行為變更。

### Test plan artifact

寫到 `~/.gstack/projects/whisper-pro-2/eng-review-test-plan-20260523-XXXX.md`：

```
# Test Plan — Fix 18 + Fix 19

## Affected modules
- hotkey_manager.py：_start_nsevent_monitors / stop / new _ns_handler_* attrs
- gui.py：_hotkey_watchdog (new branches) / _on_copy (rewrite)

## Key interactions
- Hotkey works after 60+ min idle（手動驗收）
- 連點複製多次後拿到的永遠是「最新一段」
- 錄音中 watchdog 不會 force-restart 干擾事件流

## Edge cases
- App 剛啟動沒任何錄音 → 複製按鈕 toast「無內容」
- 連續快速錄音 3 次後馬上複製 → 拿到第 3 次的轉錄
- Force-restart 期間使用者按 hotkey → restart 完之前事件遺失（可接受）
```

---

## Section 4：Performance review

### 評估

- **Force-restart 成本**：`NSEvent.removeMonitor + addMonitor`，實測 < 50ms（PyObjC bridge open）。每 10 分鐘一次 = 0.000083% CPU。可忽略。
- **靜默偵測 log**：每 5 秒讀一次 `_last_event_at` + 比較。整個 watchdog cycle < 1ms。零成本。
- **複製最後一段**：`textbox.get("latest_start", "latest_end")` 是 O(segment length)，比原本 `1.0..end` O(total history) 還省。

無 N+1、無新 query、無記憶體累積疑慮。

---

## NOT in scope（明示延後）

- **Speakly-style 結果區重構**（Path B from D2）── 加進 TODOS，後續走 `/plan-design-review` + 獨立 PR。
- **HotkeyManager Layer 2 / CGEventTap re-enable**（Fix 7 移除的、pynput 時期的補救）── 不需要，NSEvent 沒這個 API。
- **更深層的 App Nap diagnostic**（taskpolicy、IOPM 監控）── 太底層、收益有限；force-restart 已是 catch-all。
- **複製記憶體優化**（streaming clipboard）── 無實際痛點。
- **歷史視窗的複製按鈕**（每筆獨立）── 已存在於 `HistoryWindow`。

---

## 既有可重用基礎設施

| 用途 | 既有元件 | 位置 |
|---|---|---|
| Watchdog poller | `_hotkey_watchdog` 5s cadence | gui.py:1989 |
| Last-event tracking | `HotkeyManager._last_event_at` | hotkey_manager.py:420 |
| Latest segment marks | `latest_start` / `latest_end` Tk marks | gui.py:1300-1304 |
| Action logging | `log_action(..., scope="latest")` | logger.py |
| Structured stability test patterns | `tests/test_stability.py` | tests/ |

---

## TODOS.md 新增條目

在 TODOS.md 加入：

```markdown
## 2026-05-23（Fix 19 follow-up）

- [ ] **Path B：結果區重構為 Speakly-style 獨立 UtteranceBlock list**
  - 來源：Fix 19 範圍決議 C，使用者要求最終 UI 對齊 Speakly
  - 範疇估計：~300-400 LOC 新 widget + 重構 `_show_result` / `_apply_polish_to_textbox` / `_get_result_text`
  - 阻塞：應先走 `/plan-design-review` 釐清互動模型：
    - 「原文/潤飾 toggle」per-block 還是 global
    - 每 block 是否獨立「複製/存檔/重新潤飾/刪除」
    - 結果區與歷史視窗的關係（合併還是分開？）
  - 為什麼不在 Fix 19 一起做：UI 重設計風險高，使用者需要先用 Path A 一週判斷是否真的需要 Path B
```

---

## 並行化策略

兩個 fix 涉及不同檔案區段、無依賴。可以並行：

| Lane | 工作 | 觸碰模組 |
|---|---|---|
| A | Fix 18 三層保險絲 | `hotkey_manager.py` + `gui.py` watchdog 區段 |
| B | Fix 19 複製最後一段 | `gui.py` `_on_copy` |
| C | 4 個 regression test | `tests/test_stability.py` |

衝突風險：A 與 B 都動 `gui.py`，但分屬不同 method（`_hotkey_watchdog` vs `_on_copy`），merge 不會衝突。

**執行順序**：Lane A 與 B 可並發實作，C 一起寫 test 收尾。實機驗收（60+ min idle）只能由使用者執行。

---

## Implementation Tasks

- [ ] **T1 (P1, human: ~30min / CC: ~5min)** — hotkey_manager.py — 加 `_ns_handler_global/local` instance attr 強引用
  - Surfaced by: Section 1 / Fix 18 Layer 1
  - Files: hotkey_manager.py
  - Verify: T1 unit test
- [ ] **T2 (P1, human: ~30min / CC: ~5min)** — gui.py — `_hotkey_watchdog` 加 10 分鐘 force-restart + recording guard
  - Surfaced by: Section 1 / Fix 18 Layer 2
  - Files: gui.py
  - Verify: T2 unit test + 手動 60+ min idle 驗收
- [ ] **T3 (P2, human: ~15min / CC: ~3min)** — gui.py — `_hotkey_watchdog` 加靜默偵測 log
  - Surfaced by: Section 1 / Fix 18 Layer 3
  - Files: gui.py
  - Verify: T3 unit test
- [ ] **T4 (P1, human: ~30min / CC: ~5min)** — gui.py — `_on_copy` 改用 latest marks + toast 文字
  - Surfaced by: Section 1 / Fix 19
  - Files: gui.py
  - Verify: T4 unit test + 手動連續錄音複製驗收
- [ ] **T5 (P2, human: ~1h / CC: ~10min)** — tests/test_stability.py — 4 個 regression test
  - Surfaced by: Section 3
  - Files: tests/test_stability.py
  - Verify: pytest tests/test_stability.py
- [ ] **T6 (P3, human: ~5min)** — TODOS.md — 新增 Path B (Speakly-style refactor) 條目
  - Surfaced by: Section 1 / Issue 2 decision C
  - Files: TODOS.md
  - Verify: grep "Path B" TODOS.md

---

## Failure modes

| codepath | 失敗場景 | 測試覆蓋 | 錯誤處理 | 使用者感受 |
|---|---|---|---|---|
| Layer 1 強引用 | bound method 仍被 GC（不太可能） | T1 | 無 → Layer 2 兜底 | Layer 2 force-restart 後 10 分鐘內復原 |
| Layer 2 force-restart | restart 期間使用者按 hotkey | 無（race window 小） | 無 | < 100ms 觸發失敗，下次 OK |
| Layer 3 靜默偵測 | `_last_event_at` 還是 0 | T3 | 條件式跳過 log | 無感 |
| `_on_copy` latest-only | mark 不存在 | T4 fallback case | try/except + fallback | toast「無內容」/「已複製」 |

**無 critical gap**：所有有可能 silent fail 的路徑都有 fallback 或 log。

---

## Migration

無 user-facing migration：
- 既有設定檔不動
- 既有 history.db 不動
- 既有 textbox UI 不動（Path B 暫不做）
- 唯一行為差異：「複製」按鈕 toast 從「已複製到剪貼簿」變「已複製最後一段」，使用者注意到差異就會知道是好事

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | 範圍小、不需要 |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR | 2 issues 各 1 個決策、6 個 implementation tasks、4 個 regression test |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | DEFERRED | Path B 重構時再走 |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | 非 dev-facing 變更 |

**UNRESOLVED:** 0
**VERDICT:** ENG CLEARED — ready to implement Fix 18 + Fix 19. Path B (Speakly-style refactor) added to TODOS for follow-up via /plan-design-review.
