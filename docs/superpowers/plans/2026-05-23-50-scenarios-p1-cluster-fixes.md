# Plan：50 情境 P1 cluster 修補（6 agent 並行 audit）

> 建立於：2026-05-23
> Skill：`/plan-eng-review`（六 agent 架構：1 coordinator + 5 specialist）
> 分支：`fix/p1-cluster-50scenarios`（base on PR #15 `fix/light-theme-v260`）
> 範圍決議：P1 集中修（8 cluster ≈ 12 個獨立 issue）

---

## TL;DR

5 個 specialist agent 並行深度 audit 5 個 domain、各列 10 個情境 = **50 個情境統整**。經 `/plan-eng-review` 拍板「P1 集中修」、最終解 **8 個 cluster ≈ 12 個獨立 bug / race / silent failure**。+220 LOC fix、+250 LOC test、77 tests 全綠（含 14 個新 regression test）。

---

## 6 Agent 架構

1. **Coordinator（我）** — dispatch + consolidate + 寫 plan + 修
2. **Domain 1 specialist** — Hotkey / NSEvent / Watchdog
3. **Domain 2 specialist** — Recording / Transcription pipeline
4. **Domain 3 specialist** — UI state / UtteranceBlock / Polish
5. **Domain 4 specialist** — Settings / Theme / Lifecycle
6. **Domain 5 specialist** — Config / History / Logger / 邊緣整合

每 specialist 獨立 context 讀 ~10 個檔案、產 10 個情境（severity + confidence + test design + fix sketch）。

---

## 50 個情境（按 domain 集中、★ 標已修）

### Domain 1：Hotkey / NSEvent / Watchdog

| # | Sev | Conf | Title | 狀態 |
|---|---|---|---|---|
| D1-S1 | P1 | 9 | on_close 沒 removeObserver → relaunch 後雙觀察者 leak | ★ 修（Cluster A）|
| D1-S2 | P1 | 8 | Force-restart 與正在 fire 的 tap race | ★ 修（Cluster G）|
| D1-S3 | P1 | 8 | lone-modifier dead-key 殘留 _pressed 卡 arm | ★ 修（Cluster F）|
| D1-S4 | P2 | 7 | _ns_held_modifiers 與 _pressed KeyDown/KeyUp 不同步 | TODOS |
| D1-S5 | P2 | 7 | Force-restart 在 processing 永遠跳過、極端情境永不修復 | TODOS |
| D1-S6 | P2 | 6 | handler 強引用是 bound method、self retain cycle | TODOS |
| D1-S7 | P3 | 6 | execv 後 NSEvent monitor 沒 removeMonitor 殘留 | TODOS |
| D1-S8 | P3 | 7 | AppleEventReopen Layer 6 在 dev 模式 dispatch 失敗 | TODOS |
| D1-S9 | P3 | 6 | _poll_cocoa_pending_actions dedupe 順序丟資訊 | TODOS |
| D1-S10 | P3 | 5 | _last_event_at 不更新 → silent threshold log 永遠不 fire | TODOS |

### Domain 2：Recording / Transcription

| # | Sev | Conf | Title | 狀態 |
|---|---|---|---|---|
| D2-S1 | P1 | 9 | Stale frames 滲入下一段錄音（PortAudio in-flight callback） | TODOS（深度需 recorder 重構） |
| D2-S2 | P1 | 9 | Recorder.start() 失敗 UI 仍進 recording state | ★ 修（Cluster C）|
| D2-S3 | P1 | 8 | MLX fallback 後 dictionary_terms mid-flight 改動 race | TODOS |
| D2-S4 | P2 | 8 | _is_hallucination 短句邊界誤殺（< 20 字 + 子字串） | TODOS |
| D2-S5 | P2 | 9 | MLX warmup 非 turbo 模型 huggingface 下載阻塞 splash | TODOS |
| D2-S6 | P2 | 7 | Fix 9 P2-A timeout budget 對 CPU backend 不足 | TODOS |
| D2-S7 | P2 | 8 | 短音檔 fast path 對純英文 / 中英混講降品質 | TODOS |
| D2-S8 | P2 | 9 | Stale callback drop 與 P1-B 衝突：force-idle 後第二段卡 lock | TODOS |
| D2-S9 | P3 | 7 | 麥克風權限未授權時例外類型分類缺乏 | TODOS |
| D2-S10 | P3 | 8 | 空音訊 buffer 走 transcribe 觸發誤導 toast | TODOS |

### Domain 3：UI / UtteranceBlock / Polish

| # | Sev | Conf | Title | 狀態 |
|---|---|---|---|---|
| D3-S1 | P1 | 9 | `_on_ollama` 與 auto-polish 並存寫進同個 latest block | ★ 修（Cluster B）|
| D3-S2 | P1 | 9 | `_repolish_from_history` 跑時錄新音 → 誤覆寫 block | ★ 修（Cluster B）|
| D3-S3 | P1 | 8 | `_on_block_delete` 刪 latest 時 polish 仍跑 → 寫倒數第二 | ★ 修（Cluster B）|
| D3-S4 | P2 | 8 | `_set_showing_polished` `_last_polished` 與 block 真相不同步 | TODOS |
| D3-S5 | P2 | 7 | auto-scroll 50ms 競賽吃掉、新 block 看不到 | TODOS |
| D3-S6 | P2 | 8 | Mini HUD show_processing 不重定位、多螢幕 user 看不到 | TODOS |
| D3-S7 | P2 | 7 | wraplength=600 fixed、視窗 resize 不 reflow | TODOS |
| D3-S8 | P2 | 8 | Ambient Chamber render_tick 50ms 視窗 minimize 仍跑 | TODOS |
| D3-S9 | P3 | 7 | _finish_polish drop 時 _title_status 卡「潤飾中…」 | TODOS |
| D3-S10 | P3 | 7 | Chamber state 切換中間幀視覺撕裂 | TODOS |

### Domain 4：Settings / Theme / Lifecycle

| # | Sev | Conf | Title | 狀態 |
|---|---|---|---|---|
| D4-S1 | P1 | 9 | on_close 沒 removeObserver（同 D1-S1） | ★ 修（Cluster A）|
| D4-S2 | P1 | 9 | `_export_settings` manifest hard-coded "v2.2.0" | ★ 修（Cluster D-2）|
| D4-S3 | P1 | 8 | Theme save 在 spawn 前落地、relaunch 失敗無回滾 | ★ 修（Cluster D-1）|
| D4-S4 | P2 | 8 | Splash 期間 user ⌘Q、`_on_done` 對已銷毀 root 拋 tkinter exception | TODOS |
| D4-S5 | P2 | 7 | HotkeyBindDialog 開時 NSEvent monitor 仍 active → 試按誤觸錄音 | ★ 修（Cluster H）|
| D4-S6 | P2 | 8 | 重綁後 hotkey_mgr 沒真的 re-parse 新 lone 目標 | TODOS |
| D4-S7 | P2 | 8 | tccutil reset 只 reset shim bundle、不 reset 主 bundle | TODOS |
| D4-S8 | P3 | 7 | `_disable_app_nap` token execv 後沒 endActivity_ | TODOS |
| D4-S9 | P3 | 7 | set_appearance_mode 與 tokens.py palette 不一致 leak | TODOS |
| D4-S10 | P3 | 6 | Onboarding 一鍵複製命令覆蓋使用者剪貼簿無警告 | TODOS |

### Domain 5：Config / History / Logger / 邊緣

| # | Sev | Conf | Title | 狀態 |
|---|---|---|---|---|
| D5-S1 | P2 | 8 | Config corrupt 兩次→ .bak 被覆寫、失去歷史 | TODOS |
| D5-S2 | P2 | 9 | Forward-compat 降版 save 全量覆寫、丟新欄位 | TODOS |
| D5-S3 | P3 | 7 | 磁碟滿時 save() 失敗無使用者可見錯誤 | TODOS |
| D5-S4 | P2 | 8 | History.delete_before() 與 insert() 競爭 lock | TODOS |
| D5-S5 | P2 | 9 | FTS5 trigram 對全形空白 / 特殊字元 query 邊界 | TODOS |
| D5-S6 | P3 | 7 | Logger rotation 多 instance theme switch 瞬間 race | TODOS |
| D5-S7 | P2 | 8 | Ollama health cache 永不過期 | TODOS |
| D5-S8 | P1 | 9 | Prompt hot reload 中讀 `prompts.X` 拿到半 reload 狀態 | ★ 修（Cluster E）|
| D5-S9 | P2 | 9 | Dictionary file mtime 沒被 hot reload 涵蓋 | TODOS |
| D5-S10 | P2 | 8 | Auto-paste osascript timeout/奇怪格式 → activate 錯 App | TODOS |

---

## 8 個 Cluster 修法

### Cluster A — Cocoa observer / AppleEvent handler cleanup
**檔案**：`gui.py` `on_close`
**修法**：on_close 加 `NSNotificationCenter.defaultCenter().removeObserver_(...)` 與 `NSAppleEventManager.sharedAppleEventManager().removeEventHandlerForEventClass_andEventID_(...)`，配合 cleanup-first 順序避免 relaunch 後雙觀察者。

### Cluster B — Polish block identity 保護（B/C/D三 race 統一解）
**檔案**：`gui.py` `_start_polish` / `_finish_polish` / `_on_ollama`
**修法**：`_start_polish` 抓 `target_block = self._utterance_blocks[-1] if ... else None` 一起傳進 `_finish_polish`；`_finish_polish` 加入 identity check `target_block in self._utterance_blocks AND target_block is self._utterance_blocks[-1]` 才 `set_polished`。`_on_ollama` 同樣加 `_polish_generation += 1` + `target_block` ref + 雙重 check。

### Cluster C — Recorder.start() 失敗 UI 回退
**檔案**：`gui.py` `_transition_to_recording`
**修法**：把 `recorder.start()` 從尾端搬到開頭、檢查 return value；False 時 toast「⚠ 麥克風無法啟動」+ 不進 recording state。

### Cluster D-1 — Theme rollback on relaunch fail
**檔案**：`gui.py` `_trigger_theme_relaunch` / `_do_theme_relaunch`
**修法**：`_do_theme_relaunch` 加 `on_failure` callback 參數；spawn 失敗時呼叫 callback；SettingsWindow 提供 rollback callback 把 `cfg.theme = old_theme; cfg.save()`。

### Cluster D-2 — Export manifest version
**檔案**：`gui.py` `_export_settings`
**修法**：從 `from _version import __version__` 讀、不是 hardcoded `"v2.2.0"`。

### Cluster E — Prompt reload atomic lock
**檔案**：`prompt_reloader.py` / `ollama_client.py`
**修法**：`prompt_reloader` 加 `_RELOAD_LOCK = threading.RLock()` + `@contextlib.contextmanager reload_lock()`。`_reload_one` 用 `with _RELOAD_LOCK`。`ollama_client.process()` 讀 `prompts.X` 時 `with reload_lock():`。

### Cluster F — Lone-mode dead-key arm 條件
**檔案**：`hotkey_manager.py` `_on_p_lone`
**修法**：arm 條件從「`_pressed` 為空」改成「無其他 sided modifier 在 `_pressed`」── 字母 / 數字殘留不影響 arm。

### Cluster G — Force-restart 不撞 in-flight 按鍵
**檔案**：`gui.py` `_hotkey_watchdog`
**修法**：force-restart 前檢查 `_pressed` 為空 + `_combo_active=False`；in-flight 時 defer 到下個 5s watchdog tick。

### Cluster H — HotkeyBindDialog 暫停 hotkey monitor
**檔案**：`gui.py` `HotkeyBindDialog.__init__` / `destroy`
**修法**：`__init__` 結尾 `hotkey_mgr.stop()`；`destroy` 內統一 restart。WM_DELETE_WINDOW / 取消 / `_apply` / 紅× 都走同個 `destroy()`。

---

## Implementation Tasks

- [x] **T1 (P1, ~5min)** — gui.py `_export_settings` manifest version
- [x] **T2 (P1, ~20min)** — gui.py `on_close` 清 Cocoa observer / AppleEvent handler
- [x] **T3 (P1, ~10min)** — hotkey_manager.py `_on_p_lone` arm 條件
- [x] **T4 (P1, ~15min)** — gui.py `_hotkey_watchdog` force-restart defer
- [x] **T5 (P1, ~25min)** — gui.py `HotkeyBindDialog` pause/resume hotkey monitor
- [x] **T6 (P1, ~20min)** — gui.py `_trigger_theme_relaunch` + `_do_theme_relaunch` rollback callback
- [x] **T7 (P1, ~25min)** — gui.py `_transition_to_recording` start-first guard
- [x] **T8 (P1, ~30min)** — prompt_reloader.py + ollama_client.py atomic lock
- [x] **T9 (P1, ~40min)** — gui.py `_start_polish` / `_finish_polish` / `_on_ollama` block identity
- [x] **T10 (P1, ~60min)** — tests/test_p1_clusters.py 14 個 regression test

---

## NOT in scope（明示延後）

剩 ~38 個 P2/P3 情境放 TODOS.md。Cluster 化的好處：未來分批做時可挑 D2/D3/D4/D5 各取一個 cluster ship 一個 PR。

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | bug fix scope clear |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | 出 PR 前可選擇跑 |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR | 50 個情境 audit、修 8 cluster ≈ 12 issue、14 regression test 全綠 |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | 非 UI feature |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

**UNRESOLVED:** 0
**VERDICT:** ENG CLEARED — ready to ship. P2/P3 ~38 個情境留 TODOS.md 後續分批。
