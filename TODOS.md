# TODOS

> 累積但未排入當期 sprint 的工作清單。新項目請加日期與來源（PR / Fix / 報告編號）。

---

## PR #13 Code Review 延後項目（2026-05-22，Fix 9 範圍 C）

PR #13 累積 25 commits 後做了全專案 code review（4 個 sub-agent 並行）。
P1（real bug）+ P2（worth fixing）共 4 個 bug 已在 Fix 9 修掉並補 7 個 regression test。
以下 6 個 P3/P4 條目是當期決議「註 TODOS 後續」的部分，cleanup 性質，不影響 v2.3.0 功能。

**狀態（2026-05-23 v2.7.0 PR #17）**：P3/P4 全部處理完畢。Observer leak 在 P1 Cluster A 修，其餘 6 條在 B1-B6 修。

### P3 — 中優先（全部 ✅ 完成 @ v2.7.0 PR #17）

- [x] ~~**DRY：`_on_dock_reopen` 重複實作 `_restore_root_if_minimized` 邏輯**~~ — B1
- [x] ~~**`_processing_timeout_check` timer leak**~~ — B2（加 3 個 regression test）
- [x] ~~**`_restore_root_if_minimized` 漏 `'icon'` legacy state**~~ — B3
- [x] ~~**MiniHUD `deiconify()` 後 NSWindow level 可能被重設**~~ — B4（抽 `_apply_panel_level` helper + `_reapply_panel_level`）

### P4 — 低優先（全部 ✅ 完成）

- [x] ~~**`_install_cocoa_activation_observer` NSObject 沒 `removeObserver_`**~~ — P1 Cluster A（PR #16）
- [x] ~~**MiniHUD `_upgrade_to_panel_level` 升級時點偏早**~~ — B5（升級前 `withdraw + update_idletasks`）
- [x] ~~**MiniHUD 游標螢幕命中測試 closed interval bug**~~ — B6（右/上邊改 `<` 開區間）

---

## 2026-05-23（Fix 19 follow-up：結果區 Speakly-style 重構）

來源：`/plan-eng-review` D2 三明治決議 ── Fix 19 PR 出 Path A（複製只取最後一段）、
Path B（完整 Speakly-style list-of-blocks UI 重構）延後到本條。先用一週判斷
Path A 是否已足夠，再決定 Path B 是否真的要做。

- [x] **Path B：結果區重構為 Speakly-style UtteranceBlock list** ✅ 2026-05-23 完成
  - 落地版本：v2.5.0（PR #14）
  - 實際 commit：`a102dc3 feat: Fix 19 Path B — Speakly-style 獨立 UtteranceBlock 結果區`
  - 決議跳過 `/plan-design-review` 直接做（使用者要求保持節奏）。互動模型決策：
    - 原文/潤飾 toggle：保持 global（操作最新 block；舊段落鎖在當時狀態）
    - 每 block 動作圖示：always-visible（copy + delete）
    - 主視窗 vs 歷史視窗：維持兩個獨立視圖（主視窗 = 當前 session）
  - 落地範疇：gui.py +448/-122 行（含新 UtteranceBlock class、5 個方法重寫、
    delete/copy per-block handler）
  - 後續觀察：使用一週看看
    - 多段超過 10 個之後的 scroll 體驗
    - delete 圖示誤觸頻率
    - wraplength=600 固定 vs 動態（視窗 resize 沒跟）
    - 是否需要 per-block polish（重新潤飾舊段落）

### Path B 後續可能的小改進（未排）

- [ ] **UtteranceBlock 動態 wraplength**：視窗 resize 時 text label 沒跟著
  reflow，永遠 wrap 在 600px。`<Configure>` event 綁定 → 更新 wraplength。
- [ ] **per-block 潤飾按鈕**（hover-only）：目前只能對最新段潤飾，舊段沒辦法重新潤。
  可加第三個 icon 跟 copy/delete 同一行。
- [ ] **delete 動作的 undo toast**（5 秒內可救回）：避免誤刪整段語音內容無法挽回。


---

## 2026-05-23（v2.7.0 PR #17：UI 打磨 + P3/P4 cleanup）

來源：使用者選擇「繼續做 v2.7.0 候選清單」→ A 批（UI 打磨）+ B 批（P3/P4 cleanup）一起做。

**已完成（PR #17、84 tests）**：
- A1 間距統一（59/73 4pt grid）
- A2 等寬數字（splash 版本號補 mono）
- A3 reduce-motion 偵測 + 3-way pref（auto/always/never，4 tests）
- A4 舊 token 別名移除（v2.6.0 已完成、確認）
- B1-B6 cleanup（observer leak 在 P1 Cluster A 已修）
- B2 timer leak + 3 regression test

**仍開放（v2.8.0+ 候選）**：

- [ ] **A1 殘留 ~14 個非 4pt grid 值** — 主要 `padx=20`（12 hits）與 6/18/13/14/40
  各 1-2 hits。要嘛加 `SPACE_LG_PLUS=20`（破壞 4pt grid 純度）、要嘛逐個改 24
  （視覺微寬 4px、有風險）。建議實機看 v2.7.0 後再評估。
- [ ] **Phase 4.2 menu bar icon** — `rumps` + tkinter event loop PoC，30 min。
  CLAUDE.md §11 唯一未交付的 Speakly Phase 4 項目。
- [ ] **App Icon 視覺微調** — 膠囊轉角已平，剩優化項目見 `assets/icon.png`。

