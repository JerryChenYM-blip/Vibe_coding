# Plan：閒置後 hotkey 死亡修補（Fix 6 — Idle Resilience）

> 建立於：2026-05-22
> 分支：`feat/right-cmd-hotkey-and-tsm-fixes`（PR #13 in flight）
> 範圍決議：**Step 1+2+3 全做**（使用者 /plan-eng-review D1 拍板）

---

## 1. 故障描述

使用者實測：

> App 打開放後端閒置一段時間沒使用（觀察 1-2 小時 + 無視窗互動），再按右 Cmd 熱鍵 → 沒反應。沒 crash、沒 listener died log、watchdog 沒重啟。只能手動關掉重開 App 才恢復。

---

## 2. 根因（sub-agent 多源驗證，高信心）

**pynput 1.7.7 source 沒有處理 `kCGEventTapDisabledByTimeout`**：

```
pynput/_util/darwin.py L275   只在啟動時 CGEventTapEnable(tap, True) 一次
                              之後 while self.running 跑 CFRunLoopRunInMode
                              從不 check tap 是否還 enabled
pynput/keyboard/_darwin.py    _handle_message 只處理 KeyDown/KeyUp/FlagsChanged
                              kCGEventTapDisabledByTimeout (0xFFFFFFFE) 沒分支
                              event 被丟到 _emitter 然後忽略
```

當 macOS 因下列情境把 tap disable：
- **App Nap**：背景閒置 App 的 callback latency 升高 → 觸發 timeout disable
- **Sleep/wake**：系統休眠喚醒後 tap 不會自動 re-enable
- **CPU pressure**：系統負載高時主動 disable 慢 callback

**症狀全對得上**：
1. pynput thread **不死**（`while self.running` 繼續跑）
2. `listener.running` 旗標**仍是 True**
3. 既有 Fix 1 watchdog 只檢查 `listener.running` → **看不到**這個故障模式
4. Tap 已死 → 永遠收不到 key event → 使用者按熱鍵沒反應

**證據來源**：
- [Apple: CGEventType.tapDisabledByTimeout](https://developer.apple.com/documentation/coregraphics/cgeventtype/tapdisabledbytimeout) 明說「app 預期處理此 event 並重新 enable」
- [Ghostty #11819](https://github.com/ghostty-org/ghostty/discussions/11819) sleep/wake 後 CGEventTap 失效
- [Handy #840](https://github.com/cjpais/Handy/issues/840) 同類 voice-to-text Mac app 直接點名 pynput 沒 re-enable
- [pynput master source](https://github.com/moses-palmer/pynput/blob/master/lib/pynput/keyboard/_darwin.py) 親眼確認

---

## 3. 三層修補（按施工順序）

### Step 1：Watchdog 加「定時 force restart」（必做、極低風險）

**檔案**：`gui.py`
**改動**：~15 行

新增常數 + 升級既有 `_hotkey_watchdog`：

```python
HOTKEY_FORCE_RESTART_INTERVAL_S = 600  # 每 10 分鐘無條件 restart listener

# 既有 _hotkey_watchdog 加一層保險絲
def _hotkey_watchdog(self) -> None:
    try:
        mgr = self.hotkey_mgr
        if is_pynput_available() and mgr._listener is not None:
            now = time.monotonic()
            flag_dead = not mgr._listener.running

            # 既有：Layer 1 — 旗標檢查
            should_restart = flag_dead
            reason = "flag_dead" if flag_dead else None

            # NEW Layer 3 — 定時 force restart（無視狀態，保險絲）
            if not should_restart:
                last = getattr(self, "_last_listener_restart", 0.0)
                if (now - last) > HOTKEY_FORCE_RESTART_INTERVAL_S:
                    should_restart = True
                    reason = "scheduled_force_restart"

            if should_restart:
                log.warning(f"HOTKEY: watchdog restarting listener (reason={reason})")
                log_action("hotkey_listener_auto_restarted", reason=reason)
                mgr.restart(self.cfg.hotkey)
                self._last_listener_restart = now
    except Exception:
        log_error("hotkey_watchdog_failed")
    finally:
        self.after(self.HOTKEY_WATCHDOG_INTERVAL_MS, self._hotkey_watchdog)
```

**也要在 `__init__` 初始化 `self._last_listener_restart = time.monotonic()`**。

**為何 10 分鐘**：權衡使用者感知（< 5 分鐘可能在使用過程中被打斷）vs 救回時效（> 30 分鐘體感太差）。10 分鐘是 sweet spot。

**代價**：每 10 分鐘 restart 期間 ~50-200ms gap，使用者剛好那一刻按熱鍵會錯過，可接受。

---

### Step 2：抑制 App Nap（必做、低風險）

**檔案**：`main.py`
**改動**：~12 行 + module-level 全域變數

```python
# main.py — 在 root 視窗建立之後、mainloop 之前加
import sys

# 必須 module-level，token 被 GC 回收 = App Nap 復活
_APP_NAP_TOKEN = None

def _disable_app_nap() -> None:
    """抑制 macOS App Nap，避免閒置後 pynput CGEventTap callback latency
    升高觸發 timeout disable（Fix 6 / 2026-05-22）。

    出處：Apple Energy Guide / Lapcat Software / appnope library。
    用 NSActivityBackground | NSActivityLatencyCritical（後者單獨只在
    foreground 有效，必須 OR background flag 才能對 background app 生效）。
    """
    global _APP_NAP_TOKEN
    if sys.platform != "darwin":
        return
    try:
        from Foundation import NSProcessInfo
        NSActivityBackground = 0x000000FF
        NSActivityLatencyCritical = 0xFF00000000
        opts = NSActivityBackground | NSActivityLatencyCritical
        _APP_NAP_TOKEN = NSProcessInfo.processInfo() \
            .beginActivityWithOptions_reason_(
                opts,
                "Whisper Pro global hotkey listener must stay responsive",
            )
        log.info("APP_NAP: suppression activated (token kept module-level)")
    except Exception as e:
        log.warning(f"APP_NAP: suppression failed - {e}")

# 在 main() 開頭呼叫
_disable_app_nap()
```

**代價**：耗電量級 mW，使用者無感。

**Revert**：刪 `_disable_app_nap()` 呼叫即可。token 不持有時 macOS 自動回到預設行為。

---

### Step 3：CGEventTap 健康度體檢（必做、中風險）

**檔案**：`hotkey_manager.py` 暴露 `_tap` accessor、`gui.py` watchdog 加 Layer 2

`hotkey_manager.py`：
```python
def get_event_tap(self):
    """暴露底層 CGEventTap handle 給 watchdog 做健康度檢查（Fix 6）。
    pynput private API（_listener._tap），用 getattr defensive 取。"""
    listener = self._listener
    if listener is None:
        return None
    return getattr(listener, "_tap", None)
```

`gui.py` watchdog 加 Layer 2（插在 Layer 1 後、Layer 3 前）：
```python
# NEW Layer 2 — tap 健康度
if not should_restart:
    try:
        from Quartz import CGEventTapIsEnabled, CGEventTapEnable
        tap = mgr.get_event_tap()
        if tap is not None and not CGEventTapIsEnabled(tap):
            # 先試輕量 re-enable（Apple 標準做法）
            CGEventTapEnable(tap, True)
            if CGEventTapIsEnabled(tap):
                log.warning("HOTKEY: tap re-enabled in place (no restart needed)")
                log_action("hotkey_tap_reenabled")
            else:
                should_restart = True
                reason = "tap_dead_reenable_failed"
    except Exception:
        log_error("hotkey_tap_health_check_failed")
```

**為何先試輕量 re-enable 才 fallback restart**：CGEventTapEnable 是 µs 級操作，比整個 restart listener 快 1000 倍。如果只是 timeout disable，輕量 re-enable 就夠了。

**風險**：碰 `_listener._tap` 是 pynput private 屬性。未來 pynput 升版可能改名。已用 `getattr(..., None)` defensive 取，最壞情況退化成 Step 1+2，不會 crash。

**Revert**：刪 `get_event_tap()` 方法 + watchdog Layer 2 block，回到 Step 1+2 行為。

---

## 4. 既有 Fix 1 Watchdog 的去向

**取代並升級**，不是並存。

舊版 watchdog（commit `f4fa914`）只檢查 `listener.running`。Sub-agent 證明這個檢查**對主要故障模式無效**。新版三層保險絲是 superset，保留所有舊行為 + 新增兩條救回路徑。

---

## 5. 測試計畫

### 5.1 自動化測試（pytest）

新增到 `tests/test_stability.py`：

```python
def test_watchdog_force_restart_fires_after_interval():
    """Step 1：10 分鐘 force restart 該被觸發。"""
    win = make_test_window()
    win._last_listener_restart = time.monotonic() - 700  # 11 分鐘前
    # mock mgr.restart
    win.hotkey_mgr.restart = Mock()
    win._hotkey_watchdog()
    win.hotkey_mgr.restart.assert_called_once()

def test_watchdog_tap_reenable_in_place():
    """Step 3：tap disable 但 re-enable 成功 → 不該 restart listener。"""
    # 用 mock CGEventTapIsEnabled 模擬「先 False、re-enable 後 True」
    ...

def test_app_nap_token_is_held_module_level():
    """Step 2：token 必須 module-level，不能被 GC。"""
    import main
    main._disable_app_nap()
    assert main._APP_NAP_TOKEN is not None
```

### 5.2 人工測試（無法自動化的長閒置）

| 測試 | 步驟 | 預期 |
|---|---|---|
| T1 | 開 App，**閒置 30 分鐘**，按右 Cmd | 觸發錄音 |
| T2 | 開 App，**閒置 2 小時**，按右 Cmd | 觸發錄音 |
| T3 | 開 App，闔上筆電 10 分鐘，打開，按右 Cmd | 觸發錄音 |
| T4 | 撈 log 找 `tap_reenabled` 或 `scheduled_force_restart` | 應有出現 |

---

## 6. 改動檔案總覽

| 檔案 | 行數變動 | 性質 |
|---|---|---|
| `gui.py` | +25 | Step 1 + Step 3 watchdog 三層保險絲 |
| `main.py` | +15 | Step 2 App Nap 抑制 |
| `hotkey_manager.py` | +6 | Step 3 暴露 `get_event_tap()` |
| `tests/test_stability.py` | +30 | 3 個新 test |
| `CLAUDE.md` | +5 | §9 加「常見坑 #13：閒置後 CGEventTap 失效」|

**總計：~80 行新增、4 個既有檔案動到。**

---

## 7. NOT in scope

| 項目 | 為何不做 |
|---|---|
| 換掉 pynput | 影響太大，Step 1-3 已足夠救回 |
| 發 PR 給 pynput 修 timeout 處理 | 上游 review 慢，自救優先 |
| Sleep/wake notification 額外處理 | Step 1 force restart 已涵蓋 |
| 修現有 Fix 5 dock-reopen 五層保險絲 | 跟本 fix 正交，不動 |

---

## 8. 失敗模式分析

| 失敗模式 | 有測試 | 有錯誤處理 | 使用者可見 |
|---|---|---|---|
| Force restart 期間使用者剛好按熱鍵 | 否（人測）| ✅ 既有 | ❌（這次按沒反應，再按就好） |
| Quartz import 失敗 | ✅ | ✅ try/except + log_error | ❌（log 才有，退化成 Step 1+2）|
| `_listener._tap` 屬性消失（pynput 升版）| 部分 | ✅ getattr None | ❌（log，退化 Step 1+2）|
| App Nap token 被 GC | ✅ | ✅ 強 module-level | App Nap 復活，Step 1 force restart 救 |

---

## 9. 風險與回滾

每個 Step 各自一個 commit。出包 `git revert <commit>` 單獨退：
- Step 1 / 2 / 3 互相獨立、不依賴
- 最壞情境：3 個全 revert → 回到本 plan 之前狀態（Fix 5d v3 / `5e56d99`）

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR (PLAN) | 1 root cause confirmed (pynput timeout disable unhandled); 3-layer fix approved at scope-level D1 |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

**UNRESOLVED**: 0
**VERDICT**: ENG CLEARED — Step 1+2+3 全做，三檔案 ~80 行，可獨立 revert
