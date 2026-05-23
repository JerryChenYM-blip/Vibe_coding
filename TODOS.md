# TODOS

> 累積但未排入當期 sprint 的工作清單。新項目請加日期與來源（PR / Fix / 報告編號）。

---

## PR #13 Code Review 延後項目（2026-05-22，Fix 9 範圍 C）

PR #13 累積 25 commits 後做了全專案 code review（4 個 sub-agent 並行）。
P1（real bug）+ P2（worth fixing）共 4 個 bug 已在 Fix 9 修掉並補 7 個 regression test。
以下 6 個 P3/P4 條目是當期決議「註 TODOS 後續」的部分，cleanup 性質，不影響 v2.3.0 功能。

### P3 — 中優先

- [ ] **DRY：`_on_dock_reopen` 重複實作 `_restore_root_if_minimized` 邏輯**
  - 位置：`gui.py:1922-1933`
  - 修法：改成 `self._restore_root_if_minimized("ReopenApplication"); self.winfo_toplevel().focus_force()`
  - 為什麼後延：cosmetic、不影響功能。但兩處 if-state 邏輯漂移風險。

- [ ] **`_processing_timeout_check` timer leak**
  - 位置：`gui.py` `_transition_to_processing` 內 `self.after(...)`
  - 每次 `_transition_to_processing` schedule 一個 pending callback，沒 cancel。
  - 快速連續 N 次錄音 → N 個 pending 都 alive 至各自 timeout 觸發。
  - 修法：存 id 到 `self._processing_timeout_id`，下次 schedule 前 `after_cancel`。

- [ ] **`_restore_root_if_minimized` 漏 `'icon'` legacy state**
  - 位置：`gui.py:1906` 附近
  - Tk 文件列舉 wm_state 合法值含 `'icon'`（legacy），雖然 macOS 26.4 實測都是 `'iconic'`。
  - 修法：`if state in ("iconic", "icon", "withdrawn"):` 零成本防禦。

- [ ] **MiniHUD `deiconify()` 後 NSWindow level 可能被重設**
  - 位置：`gui.py` `MiniRecordingWindow.show_recording` / `show_processing` + `_upgrade_to_panel_level`
  - Tk-on-Aqua 某些版本 `deiconify` 會重設 styleMask → 跨 Space 能力默默丟失。
  - 修法：`show_recording` / `show_processing` 開頭 re-apply `setLevel_` + `setCollectionBehavior_`（cheap）。

### P4 — 低優先

- [ ] **`_install_cocoa_activation_observer` NSObject 沒 `removeObserver_`**
  - 位置：`gui.py` cocoa observer install 處
  - AppWindow.on_close 沒 cleanup observer。單視窗 App 退出時 OS 收回沒事，但未來 multi-window 會 leak。
  - 修法：on_close 加 `NSNotificationCenter.defaultCenter().removeObserver_(...)`。

- [ ] **MiniHUD `_upgrade_to_panel_level` 升級時點偏早**
  - 位置：`gui.py` `MiniRecordingWindow.__init__` → `_upgrade_to_panel_level`
  - `overrideredirect(True)` + `attributes("-alpha", ...)` + `title()` + `geometry()` 後 Tk 可能 deferred commit 蓋掉 setLevel。
  - 修法：升級前 `self.withdraw(); self.update()` 強制 commit 一輪。

- [ ] **MiniHUD 游標螢幕命中測試 closed interval bug**
  - 位置：`gui.py` `_position_at_cursor_screen_bottom` for-loop
  - 游標剛好在兩螢幕共用邊上時匹配迴圈順序第一個螢幕。實際幾乎察覺不到。
  - 修法：右/上邊用 `<` 開區間。

---

## 2026-05-23（Fix 19 follow-up：結果區 Speakly-style 重構）

來源：`/plan-eng-review` D2 三明治決議 ── Fix 19 PR 出 Path A（複製只取最後一段）、
Path B（完整 Speakly-style list-of-blocks UI 重構）延後到本條。先用一週判斷
Path A 是否已足夠，再決定 Path B 是否真的要做。

- [ ] **Path B：結果區重構為 Speakly-style UtteranceBlock list**
  - 來源：Fix 19 範圍決議 C（2026-05-23），使用者要求最終 UI 對齊 Speakly 截圖
  - 範疇估計：~300-400 LOC 新 widget + 重構：
    - 新增 `UtteranceBlock(ctk.CTkFrame)`：timestamp + text + 個別 copy/polish icon
    - `ScrollableFrame` 取代現有 `_textbox`
    - 重寫 `_show_result` / `_apply_polish_to_textbox` / `_get_result_text`
    - 移除 `latest_start..latest_end` Tk marks（改 list index）
  - 阻塞：必須先走 `/plan-design-review` 釐清互動模型：
    - 「原文/潤飾 toggle」per-block 還是 global
    - 每 block 獨立按鈕還是 hover-only
    - 主視窗結果區與歷史視窗的關係（合併 / 分開 / 雙視圖）
  - 為什麼不在 Fix 19 一起做：UI 重設計風險高，使用者需要先用 Path A 一週判斷需求

