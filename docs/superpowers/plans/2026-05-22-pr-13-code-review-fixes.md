# Plan：PR #13 Code Review 修補（4 bug + 6 TODOS）

> 建立於：2026-05-22
> 分支：`feat/right-cmd-hotkey-and-tsm-fixes`（PR #13 in flight，25 commits）
> 範圍決議：**C — 只修 P1+P2、P3/P4 註 TODOS.md 後續**（D2 拍板）

---

## 1. 背景

PR #13 累積 25 commits（Fix 1-8）後，使用者要求對整個專案 code review。
範圍縮減到 PR #13 focus 後，4 個 sub-agent 並行審完，找到：

- **2 個 P1（real bug、9/10 confidence）**：必修
- **2 個 P2（worth fixing）**：必修
- **6 個 P3/P4（cleanup 性質）**：註 TODOS 後續
- **12 個 test GAP**：本輪不補（D2 範圍 C 不含）

---

## 2. P1 修補

### P1-A：左右 Cmd 同時按住、放開一顆會被誤判（hotkey_manager.py）

**症狀**：

```
情境：按住 Left Cmd → 再按住 Right Cmd → 放開 Left Cmd
位置：hotkey_manager.py:540-551 _modifier_bit_for()
根因：_modifier_bit_for("cmd_l") 與 _modifier_bit_for("cmd_r") 都回
      NSEventModifierFlagCommand（device-independent bit）
觸發：Left Cmd 放開時 keyCode=55，但 modifierFlags & cmd_bit 仍為 True
      （因 cmd_r 還按著）→ 被分到 _on_p(Key.cmd_l) 而非 _on_r
後果：
  (1) _pressed 累積 stale Key.cmd_l
  (2) lone-mode 下一次乾淨 Right Cmd press 因 len(_pressed) != 0
      無法 arm → 要等 10s self-heal 才恢復
  (3) combo 模式 _pressed 永遠多一個 entry，可能誤觸 issubset()
```

**修法**：用 `_pressed` 集合本身做 state machine：

```python
def _handle_ns_event(self, event):
    evt_type = event.type()
    if evt_type == 12:  # FlagsChanged
        keycode = event.keyCode()
        sided_name = _KEYCODE_TO_SIDED_MOD.get(keycode)
        if sided_name is None:
            return
        sided_key = _sided_name_to_pynput_key(sided_name)

        # 新邏輯：用 _pressed 集合判斷是 press 還是 release
        # FlagsChanged 對同一個 sided keycode 是 toggle —— 若已在 set 就是 release，否則 press
        if sided_key in self._pressed:
            self._on_r(sided_key)
        else:
            self._on_p(sided_key)
    elif evt_type == 10:  # KeyDown
        ...
```

**改動範圍**：`hotkey_manager.py` `_handle_ns_event` FlagsChanged 分支重寫 ~10 行
**回歸測試**：unit test 模擬左右 Cmd 同時按住、放開一顆，驗證 `_on_r` 被呼叫且 `_pressed` 乾淨。

---

### P1-B：Stale transcription callback 踩平第二段錄音 UI（gui.py）

**症狀**：

```
時間軸：
  T+0s   使用者開始錄音（state=recording）
  T+5s   使用者按 hotkey 停止 → state=processing
  T+65s  Fix 4 _processing_timeout_check 觸發 force-idle
         → _transition_to_idle 設 state=idle，toast「轉錄超時」
  T+66s  使用者按 hotkey 開始第二段錄音 → state=recording
  T+85s  原本第一段的 Whisper inference 終於完成
         → _on_transcription_done 被 marshalled 到主 thread
         → ❌ 無條件呼叫 _transition_to_idle(result)
         → 第二段錄音的 UI/timer 被踩平、state=idle
         → 但 recorder 還在跑、_state 跟 recorder 不同步
```

**修法**：`_on_transcription_done` 加 state check：

```python
def _on_transcription_done(self, result: TranscriptionResult) -> None:
    """主執行緒：轉錄完成後決定是否走潤飾流程..."""
    # P1-B / Fix 9：避免 stale callback 踩平 idle/recording 狀態
    # 場景：_processing_timeout_check 已 force-idle 後，背景 Whisper thread
    # 慢慢回來，這個 callback 會無條件覆寫 UI 跟 _state
    if self._state != "processing":
        log.warning(
            f"_on_transcription_done dropped: state={self._state} (expected 'processing'). "
            f"Likely a stale callback from a timed-out inference."
        )
        log_action("transcription_late_dropped", state=self._state)
        return

    self._transition_to_idle(result)
    ...既有邏輯...
```

同理 `_on_transcription_failed` 也加（避免使用者剛收到「轉錄超時」toast 又冒出「轉錄失敗」）。

**改動範圍**：`gui.py` `_on_transcription_done` 開頭 +6 行、`_on_transcription_failed` +3 行
**回歸測試**：mock state=idle，呼叫 `_on_transcription_done`，驗證它**沒**呼叫 `_transition_to_idle`。

---

## 3. P2 修補

### P2-A：PROCESSING_TIMEOUT 60s 對長音檔 CPU backend 必誤殺（gui.py）

**症狀**：

| 後端 | 60s 音訊推論 | 300s 音訊推論 | 結論 |
|---|---|---|---|
| MLX（預設）| ~6s | ~30s | 60s timeout 安全 |
| faster-whisper CPU | 18-30s | 90-150s | **300s 音訊必中 60s timeout** |

CLAUDE.md §11 提到「會議紀錄」是 v3 規劃，意味使用者實際會錄 5+ 分鐘音訊。CPU backend 路徑 + 長音訊 = 100% 誤殺。

**修法**：以音訊長度動態算 timeout：

```python
# gui.py constants
PROCESSING_TIMEOUT_BASE_MS = 60_000      # 至少 60s 基礎緩衝
PROCESSING_TIMEOUT_RTF_BUDGET = 1.0      # 預留 1.0 倍 RTF 上限（CPU faster-whisper 約 0.3-0.5）

def _transition_to_processing(self, audio_duration_s: float = 0.0) -> None:
    """進入處理中狀態。動態 timeout = max(60s, audio_duration * RTF_BUDGET)。"""
    ...
    # 動態 timeout：CPU 後端錄 5 分鐘也不會誤殺
    dynamic_timeout_ms = max(
        self.PROCESSING_TIMEOUT_BASE_MS,
        int(audio_duration_s * self.PROCESSING_TIMEOUT_RTF_BUDGET * 1000)
    )
    self._processing_started_at = time.monotonic()
    self.after(dynamic_timeout_ms, self._processing_timeout_check)
    ...
```

呼叫端 `_try_stop()` 改傳 audio_duration。

**改動範圍**：`gui.py` `_transition_to_processing` 簽名 + 呼叫端 ~12 行
**回歸測試**：模擬 300s audio，驗證 timeout = 300_000ms (5min) 而非 60_000ms。

---

### P2-B：MiniHUD title-based NSWindow lookup race（gui.py）

**症狀**：使用者快速 toggle MiniHUD（設定面板 OFF→ON→OFF），`Tk.destroy()` 同步回傳但 NSWindow autorelease 不一定同 tick 釋放。`NSApp.windows()` 可能短暫返回兩個 title 同為 `WhisperProMiniHUD` 的 window，迴圈取第一個就拿到即將被釋放的舊 handle，後續 `setLevel_` 無效。

**修法**：每實例獨一 title：

```python
class MiniRecordingWindow(ctk.CTkToplevel):
    # P2-B / Fix 9：title 加 id(self) 避免 toggle 快速 destroy/recreate 時
    # NSApp.windows() 拿到舊 instance 的 NSWindow handle
    def __init__(self, master, ...):
        super().__init__(master, ...)
        self._ns_title = f"WhisperProMiniHUD-{id(self):x}"
        self.title(self._ns_title)
        ...

    def _upgrade_to_panel_level(self):
        ...
        for w in NSApp.windows():
            if w.title() == self._ns_title:   # 改用 instance attribute 而非 class const
                ns_window = w
                break
        ...
```

**改動範圍**：`gui.py` MiniRecordingWindow class，移除 `_NS_TITLE` class const、改用 `self._ns_title`，~8 行
**回歸測試**：mock NSApp.windows 返回兩個同名 window，驗證 lookup 比對 instance-specific title 後拿到對的那個。

---

## 4. P3/P4 註 TODOS.md（不修，後續處理）

新建（或補進）`TODOS.md`：

```markdown
# TODOS

## PR #13 Code Review 延後項目（2026-05-22）

### P3 — 中優先

- [ ] **DRY：`_on_dock_reopen` 重複實作 `_restore_root_if_minimized` 邏輯**
  - 位置：gui.py:1922-1933
  - 修法：改成 `self._restore_root_if_minimized("ReopenApplication"); self.winfo_toplevel().focus_force()`
  - 為什麼後延：cosmetic、不影響功能。但兩處 if-state 邏輯漂移風險。

- [ ] **`_processing_timeout_check` timer leak**
  - 位置：gui.py:726
  - 每次 `_transition_to_processing` schedule 一個 60s pending callback，沒 cancel。
  - 快速連續 N 次錄音 → N 個 pending 都 alive 60s。
  - 修法：存 id 到 `self._processing_timeout_id`，下次 schedule 前 `after_cancel`。

- [ ] **`_restore_root_if_minimized` 漏 `'icon'` legacy state**
  - 位置：gui.py:1906
  - Tk 文件列舉 wm_state 合法值含 `'icon'`（legacy），雖然 macOS 26.4 實測都是 `'iconic'`。
  - 修法：`if state in ("iconic", "icon", "withdrawn"):` 零成本防禦。

- [ ] **MiniHUD `deiconify()` 後 NSWindow level 可能被重設**
  - 位置：gui.py:3771-3774 + 3740-3762
  - Tk-on-Aqua 某些版本 `deiconify` 會重設 styleMask → 跨 Space 能力默默丟失。
  - 修法：`show_recording` / `show_processing` 開頭 re-apply `setLevel_` + `setCollectionBehavior_`（cheap）。

### P4 — 低優先

- [ ] **`_install_cocoa_activation_observer` NSObject 沒 removeObserver_**
  - 位置：gui.py（cocoa observer install 處）
  - AppWindow.on_close 沒 cleanup observer。單視窗 App 退出時 OS 收回沒事，但未來 multi-window 會 leak。
  - 修法：on_close 加 `NSNotificationCenter.defaultCenter().removeObserver_(...)`。

- [ ] **MiniHUD `_upgrade_to_panel_level` 升級時點偏早**
  - 位置：gui.py:3577-3607
  - `overrideredirect(True)` + `attributes("-alpha", ...)` + `title()` + `geometry()` 後 Tk 可能 deferred commit 蓋掉 setLevel。
  - 修法：升級前 `self.withdraw(); self.update()` 強制 commit 一輪。

- [ ] **MiniHUD 游標螢幕命中測試 closed interval bug**
  - 位置：gui.py:3655-3660
  - 游標剛好在兩螢幕共用邊上時匹配迴圈順序第一個螢幕。實際幾乎察覺不到。
  - 修法：右/上邊用 `<` 開區間。
```

---

## 5. 改動檔案總覽

| 檔案 | 行數變動 | 性質 |
|---|---|---|
| `hotkey_manager.py` | +15 / -5 | P1-A：FlagsChanged dispatch 改用 `_pressed` 做 state machine |
| `gui.py` | +30 / -2 | P1-B：transcription callback state guard、P2-A：動態 timeout、P2-B：instance-unique title |
| `tests/test_stability.py` | +60 | 4 個 P1/P2 對應的 regression test |
| `TODOS.md` | +50（新檔）| 6 個 P3/P4 條目 |
| `CLAUDE.md` | +3 | §9 加坑 #15（左右 modifier dispatch 必須走 `_pressed` 不可只看 modifier flag bit）|

**總計：~110 行新增、4 個既有檔案動到、1 個新檔。**

---

## 6. 實作順序（按風險升序）

| # | Fix | 風險 | 行數 |
|---|---|---|---|
| 1 | P1-B：transcription callback state guard | 極低 — 加 early-return | ~9 |
| 2 | P2-A：動態 timeout | 低 — 純算法調整 | ~12 |
| 3 | P2-B：instance-unique title | 低 — 字串改動 | ~8 |
| 4 | P1-A：FlagsChanged dispatch 改 `_pressed` state machine | 中 — 改變 NSEvent 核心 dispatch 邏輯 | ~15 |
| 5 | 4 個對應 regression test | 中 — 寫真實場景模擬 | ~60 |
| 6 | TODOS.md + CLAUDE.md §9 | 極低 | ~53 |

每個 fix 各自一個 commit、可單獨 revert。

---

## 7. NOT in scope

| 項目 | 為何不做 |
|---|---|
| 6 個 P3/P4 cleanup | D2 決議：註 TODOS 不修 |
| 12 個 test GAP（除 4 個 fix 對應 regression 外） | D2 決議：覆蓋率本輪不補 |
| gui.py 3800 行拆分 | 大重構不在 PR #13 範疇 |
| 整個專案 Architectural review | D1 決議：B 範圍只審 PR #13 |

---

## 8. 失敗模式分析

| 失敗模式 | 有測試 | 有錯誤處理 | 使用者可見 |
|---|---|---|---|
| 左右 Cmd 同按、放開一顆（P1-A）| ✅ 補測 | ✅ `_pressed` state machine | ❌（log only）|
| Stale transcription callback（P1-B）| ✅ 補測 | ✅ early-return + log_action | ❌（log only，無 UI 訊息因為不該打擾）|
| 長音檔 CPU 60s 誤殺（P2-A）| ✅ 補測 | ✅ 動態 timeout | ✅（不誤殺就不會看到 toast）|
| MiniHUD race（P2-B）| ✅ 補測 | ✅ instance-unique lookup | ❌（升級失敗 log only）|

---

## 9. 已存在可重用元件

| 需要 | 既有 | 位置 |
|---|---|---|
| structured log | `log_action / log_state / log_settings / log_error` | logger.py |
| state guard pattern | `_processing_timeout_check` 自己有 state check | gui.py:768 |
| state machine 自我修復 | `_STALE_COMBO_SEC` healed flag | hotkey_manager.py:462-468 |
| 動態 timeout 既有設計 | 無 | 本次新增 |

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR (PLAN) | 18 findings (2 P1 + 2 P2 + 6 P3/P4 + 12 test gaps); D2 範圍 C 拍板修 P1+P2 共 4 個，P3/P4 註 TODOS、test gaps 不補 |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

**UNRESOLVED**: 0
**VERDICT**: ENG CLEARED — 4 bug fix + 6 TODOS + 補 4 個 regression test，6 個 commit 內可完成
