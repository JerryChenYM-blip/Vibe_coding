# 異動紀錄：錄音按鈕 Ambient Light Chamber 重構

- **日期**：2026-04-19
- **開發 session**：UI/UX Pro Max 諮詢 → 設計 → 實作 → PR
- **PR**：[#11 feat/ambient-chamber](https://github.com/JerryChenYM-blip/Vibe_coding/pull/11)
- **已合併前置 PR**：#4、#5、#6

---

## 1. 動機

使用者 2026-04-19 回報：「那顆綠色的錄音按鈕看起來不夠 premium，說不上來就是不夠 premium」。經 design audit 歸納廉價感三大根源：

1. **Apple 系統綠 `#30D158`** 缺品牌辨識度，和市面 macOS 小工具撞衫
2. **波形 + 外環 + 按鈕三元件各自為政**，視覺上沒有主從關係
3. **狀態轉換粗糙**：色彩瞬切、脈衝用主體色 swap、emoji 🎤 違反 CLAUDE.md 鐵律

---

## 2. Session 流程總覽

```
使用者問 skill 機制
  └─► 解釋 ui-ux-pro-max 運作機制（CSV 資料庫 + Python CLI）
        └─► 使用者：「幫我跑，要調整錄音按鍵」
              └─► 跑 design-system + domain=ux + domain=style 查詢
                    └─► 提出 P0/P1/P2 audit（CLAUDE.md 鐵律違規清單）
                          └─► 使用者選 方案 C（整個 record card 重構）
                                └─► brainstorming skill 問 3 題（base path / 痛點 / 範圍）
                                      └─► 提出 3 個視覺方向（D1 環狀頻譜 / D2 水平波形 / D3 光場容器）
                                            └─► 使用者選 D3
                                                  └─► 4 個 Section 設計規格獲使用者確認
                                                        └─► 使用者授權全自動執行
                                                              └─► 寫 spec → 寫 plan → 合併 PR → 實作 → smoke test → 開 PR
```

---

## 3. 主要技術決策（決策紀錄 D1–D12）

| # | 決策 | 拍板者 | 原因 |
|---|---|---|---|
| D1 | 從乾淨 main 重構（先合 #4/#5/#6）| 使用者 Q1 | 避免堆疊 PR 衝突 |
| D2 | 主要訴求：消除綠色廉價感 | 使用者 Q2 | 其他複合問題都是次要 |
| D3 | 重構範圍：整個 record card | 使用者 Q3 | 只換顏色天花板太低 |
| D4 | 視覺方向：D3 Ambient Light Chamber | 使用者 Q4 | 唯一從根源消除「色塊按鈕」概念 |
| D5 | idle 色從 SUCCESS 綠改 ACCENT 青 | 使用者確認 | 對齊品牌主色 |
| D6 | 計時器 `SF Mono 18pt bold` | 使用者確認 | tabular 不跳動 |
| D7 | 啟用 RMS 擴散波紋 | 使用者確認 | D3 戲劇性細節 |
| D8 | 50ms tick（20 FPS）| 使用者確認 | 保持現狀效能預算 |
| D9 | `defaults read` 偵測 reduce-motion | 使用者確認 | macOS 慣例 |
| D10 | `canvas.delete("all")` 完整重繪 | 使用者確認 | 280×280 下成本可忽略 |
| D11 | 一個大 PR（feat/ambient-chamber）| Claude 自決 | 視覺變更耦合高，拆小無意義 |
| D12 | Claude 代合併 PR #4/#5/#6 | 使用者授權 | 「全部都自動化自己執行」 |

---

## 4. PR 合併紀錄（前置作業）

| # | 分支 | 合併時間 | 後續處理 |
|---|---|---|---|
| 4 | feat/record-button-breathing-glow | 2026-04-19 13:53 UTC | base=main，直接 merge |
| 5 | feat/design-tokens-zinc-palette | 2026-04-19 13:54 UTC | base 從 #4 retarget 為 main |
| 6 | feat/lucide-icons | 2026-04-19 13:54 UTC | base 從 #5 retarget 為 main |

合併後 main 新增 `tokens.py`（163 行）+ `icons.py`（235 行）+ `gui.py` 更新（±228 行）。

---

## 5. 新增檔案

| 路徑 | 行數 | 用途 |
|---|---|---|
| `animation.py` | 100 | 純函式動畫：`ease_out_cubic` / `ease_in_out_cubic` / `breathe` / `blend` / `Ripple` |
| `tests/test_animation.py` | 108 | 22 個 pytest（全 pass）|
| `tests/__init__.py` | 0 | pytest 套件標記 |
| `docs/superpowers/specs/2026-04-19-ambient-chamber-design.md` | 376 | 設計規格（13 章節 + 決策紀錄）|
| `docs/superpowers/plans/2026-04-19-ambient-chamber.md` | 1438 | 8 個 task 逐步實作計畫 |
| `docs/changelog/2026-04-19-ambient-chamber-session.md` | 本檔 | session-level 異動紀錄 |

---

## 6. 修改檔案

### 6.1 `tokens.py`（+11 行）

擴充 `ANIMATION` 區段：

```python
BREATHE_IDLE_MS       = 6000   # 呼吸週期：idle
BREATHE_RECORDING_MS  = 2500   # 呼吸週期：recording
BREATHE_PROCESSING_MS = 1800   # 呼吸週期：processing
ROTATE_PROCESSING_MS  = 1500   # 旋轉粒子週期
RENDER_TICK_MS        = 50     # 20 FPS canvas render loop
```

### 6.2 `icons.py`（+27 行）

- 新增 `_i_square` Lucide stop icon（12 → 13 個 icon）
- 新增 `get_canvas_icon()` API：回傳 `ImageTk.PhotoImage` 供 `tk.Canvas.create_image` 使用（CTkImage 無法直接用在 tk.Canvas）

### 6.3 `gui.py`（+290 / -240，淨 +50 行）

#### 刪除
- `_wave_canvas` + `_draw_idle_wave` + `_draw_live_wave` + `_update_wave`
- `_glow_canvas` + `_record_btn` + `_start_glow` + `_draw_glow`
- `_pulse_btn` + `_animate_spinner`
- 本地 `_blend` + `_hex_to_rgb`（改用 `animation.blend`）
- Status bar 冗餘 `_timer_label`
- 常數：`GLOW_SIZE`、`GLOW_CENTER`、`BTN_RADIUS`、`GLOW_FPS`、`GLOW_TICK_MS`、`WAVE_BARS`
- 3 處 hardcoded `"#4745C8"` → `INDIGO_HV`

#### 新增
- `_chamber` 單一 Canvas（280×280）
- `_draw_chamber`：5 圈同心光暈 + 中央 disc + icon + RMS 擴散波紋
- `_render_tick`：50ms 繪製迴圈，sleep-interruptible
- Canvas event handlers：click / hover / press / motion / enter / leave
- `_in_disc()` hit-testing helper
- `_timer_label`（SF Mono 18pt, tabular，獨立於光場下方）
- Module-level：`CHAMBER_SIZE`、`RING_RADII_5/4`、`RING_ALPHA_*`、`RIPPLE_*`、`PROC_PARTICLES`
- `system_reduce_motion()` 函式

#### 修改
- `_transition_to_recording / processing / idle`：保留所有 business logic，只改 UI 設定
- `_update_timer`：不再寫 `_record_btn.configure`，只寫 `_timer_label`

---

## 7. 三態視覺規格（實作後）

| 狀態 | 主色 | 光暈 | 呼吸週期 | 中央 icon | 計時器 |
|---|---|---|---|---|---|
| idle | `ACCENT` #06B6D4 | 5 圈，alpha 0.25→0.03 | 6s | Lucide mic (TEXT_2) | 隱藏 |
| recording | `DANGER` #EF4444 | 5 圈 + 擴散波紋 | 2.5s + RMS 擴張 ±18% | Lucide square (TEXT_1) | `mm:ss` mono |
| processing | `WARN` #F59E0B | 4 圈 + 12 顆旋轉粒子 | 1.8s + 粒子 1.5s | Lucide mic (blended WARN) | 隱藏 |

---

## 8. 驗證結果

### 8.1 靜態檢查（Task 6）

```
=== hardcoded hex ===  ✓ 0 筆
=== functional emoji ===  ✓ 0 筆（title 🎙 為品牌 emoji，CLAUDE.md §12.3 允許）
=== obsolete symbols ===  ✓ 0 筆
=== mono font count ===  2（import + timer label）
=== pytest ===  ✓ 22/22 pass
```

### 8.2 動態測試

- AppWindow 可正常 instantiate（headless）
- 三態 `_draw_chamber()` 全部成功渲染無 TclError
- `venv/bin/python3 main.py` 啟動 > 4 秒無 crash

### 8.3 未完成的使用者驗證

- 手動按 ⌘⌥R 測錄音流程（快捷鍵 + 自動貼上）
- macOS Reduce Motion 降級行為（`defaults write com.apple.universalaccess reduceMotion 1` 後重啟）
- 活動監視器 CPU 使用率實測

---

## 9. Git Commits（feat/ambient-chamber 分支）

依時序：

```
c4126ee  docs: 新增錄音按鈕 Ambient Chamber 重設計 spec
0ed1ac7  docs: 新增 Ambient Chamber 實作計畫（逐步 bite-sized）
8b07bb2  feat(tokens): 擴充 ANIMATION 區段提供呼吸週期與 render tick
e12f66d  feat(animation): 新增純函式動畫模組（easing / breathe / blend / Ripple）
4883267  feat(icons): 新增 Lucide 'square' 停止圖示
d17f1f4  feat(gui): 新增 chamber 幾何常數 + reduce-motion 偵測
6f12b98  refactor(gui): 錄音按鈕改為 Ambient Light Chamber
```

前兩個 doc commit 源自 `feat/transcription-completeness` 分支，透過 cherry-pick 帶入。

---

## 10. 使用的 Skills / Tools

| 工具 | 用途 |
|---|---|
| `ui-ux-pro-max` | Design system 查詢、style / color / ux 規格檢索 |
| `superpowers:brainstorming` | 逐題釐清使用者需求與設計方向 |
| `superpowers:writing-plans` | 產出 8-task bite-sized 實作計畫 |
| `superpowers:executing-plans` | 逐 task 執行 + verification |
| `pytest` | `animation.py` 單元測試（首次在本專案使用，已 `pip install pytest`）|
| `gh` | PR 檢視、retarget、合併、建立 |

---

## 11. 未處理項目

### 11.1 使用者手動驗證

PR #11 的 test plan 最後一項仍待使用者確認：

- [ ] 手動 3 態切換 / 快捷鍵 / 自動貼上流程
- [ ] macOS Reduce Motion 行為
- [ ] CPU 使用率實測

### 11.2 舊分支清理

- `feat/transcription-completeness`：含**未提交的大量 `node_modules/*` + 舊 `.md` 刪除**，那是你自己在做的清理。本 session 完全未觸碰。建議後續自行決定要開 PR 合進 main、還是 rebase 到 main、還是丟棄。
- `feat/record-button-breathing-glow`、`feat/design-tokens-zinc-palette`、`feat/lucide-icons`：PR 已合併但分支未刪除（`--delete-branch=false`），安全起見保留。可視需要手動刪除。

### 11.3 遺留的 pyrefly 警告

非 blocker，pre-existing 或 Tkinter callback 必要的 unused 參數：

- `gui.py:859/879`：Canvas event handler 的 `event` 參數（Tkinter callback 要求簽名必須收）
- `gui.py:1309`：`current_combo` 參數（快捷鍵對話框，pre-existing，不在本次 scope）

### 11.4 技術債

- `gui.py` 仍保留 `SURF1..3` / `TEXT1..3` / `BLUE` / `GREEN` / `RED` 等舊別名的使用（約 15+ 處）。本次只在 record card 區域 migrate 到正規名稱；其他區塊（topbar / result card / action bar / status bar）的 migrate 留待後續。

---

## 12. Rollback 計畫

- **整包回退**：在 `main` 上 revert PR #11
- **單 commit 回退**：`git revert 6f12b98`（只回退 `refactor(gui)` 那一個 commit）— 其他 commits（animation.py 新檔、tokens 擴充、icons 新增 square）皆為 additive，保留不影響
- 核心 modules（`recorder.py` / `transcriber.py` / `hotkey_manager.py` / `auto_paste.py` / `config.py` / `vad.py` / `ollama_client.py`）**完全未觸碰** → 零風險影響錄音/轉錄/快捷鍵/自動貼上/設定等功能

---

## 13. 前後對比（快速摘要）

| 面向 | Before | After |
|---|---|---|
| 按鈕結構 | wave canvas + ring frame + CTkButton 三件套 | 單一 tk.Canvas |
| 按鈕顏色（idle）| `#30D158` Apple system 綠 | `#06B6D4` ACCENT cyan（品牌主色）|
| 按鈕圖示 | `🎤` emoji | Lucide `mic` vector icon |
| 狀態切換 | 瞬切（`configure()` 瞬間 swap）| 呼吸漸變 + RMS 擴張 + ease-in-out-cubic |
| 錄音視覺回饋 | 長條波形（上方）+ 按鈕顏色脈衝 | 光場隨 RMS 鼓動 + 峰值觸發擴散波紋 |
| 處理中指示 | Braille spinner (⠋⠙⠸⠴⠦⠇) 字元動畫 | 12 顆 WARN 色粒子圓周旋轉（彗尾 alpha 梯度）|
| 計時器 | 按鈕文字第二行 + status bar label（雙份）| 單一 `_timer_label`（SF Mono 18pt）光場下方 |
| 動畫系統 | glow loop + wave loop + pulse loop 三個獨立迴圈 | 單一 `_render_tick` 50ms loop |
| 色彩來源 | 3 處 hardcoded `#4745C8` + GREEN/RED legacy | 100% `tokens.py`（零硬編 hex）|
| Reduce Motion 支援 | 無 | 有（啟動時偵測）|

---

**異動紀錄結束。** 任何爭議與後續調整請於 PR #11 留言。
