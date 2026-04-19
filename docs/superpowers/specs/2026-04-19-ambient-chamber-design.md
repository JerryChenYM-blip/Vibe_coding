# 錄音按鈕 Ambient Chamber 重設計 — Design Spec

- **日期**：2026-04-19
- **狀態**：Approved（Sections 1–4 已逐段獲使用者確認，最終三題由 Claude 依授權自決）
- **作者**：Claude（使用者明確授權全權執行）
- **目標分支**：`feat/ambient-chamber`（從 main 拉出，**先合併 PR #4/#5/#6 再開始**）

---

## 1. 背景與動機

Whisper Pro v2.0 目前的錄音按鈕由三個獨立元件組成：上方 `_wave_canvas`（72px 高的長條波形）、中層 `_btn_ring`（196×196 裝飾外框）、核心 `_record_btn`（164×164 的 CTkButton）。

使用者 2026-04-19 回報「整顆按鈕看起來不夠 premium，尤其是那顆綠色的錄音按鈕」。Design audit 得出廉價感根源有三：

1. **Apple 系統綠 `#30D158` 缺品牌辨識度**，和市面上 macOS 小工具長得一樣
2. **波形與按鈕視覺上各自為政**，缺乏主從關係
3. **狀態切換粗糙**：色彩瞬切、脈衝是「主體色 swap」（RED ↔ #8B1A15）、emoji 🎤 違反 CLAUDE.md 鐵律

## 2. 目標

用**光場容器（Ambient Light Chamber）**的視覺隱喻重構整個 record card：

- 把「波形 + 外環 + 按鈕」合併成**單一 Canvas**，RMS 直接驅動光強
- 廢除實體色塊按鈕的概念，改用**光**作為狀態語言（青 / 紅 / 琥珀）
- 以 `tokens.py` 的設計系統為唯一真相來源，**零硬編 hex**
- 所有圖示來自 `icons.py` 的 Lucide 手繪版本，**零 emoji**

## 3. 非目標（Non-Goals）

- **不改任何核心功能**：recorder / transcriber / hotkey_manager / auto_paste / stream_tick / config 完全不動
- **不改其他 UI 區塊**：status bar、action bar、textbox、toast 皆保留現狀
- **不重構狀態機**：`idle / recording / processing` 三態以及 `_transition_to_*` 方法簽章不變
- **不做 light mode**：此次維持 dark-only，light mode 另案處理

## 4. 架構變更

### 4.1 元件替換

| 原件 | 處理 | 原因 |
|---|---|---|
| `_wave_canvas` (72×W 長條) | **合併** | D3 的光就是波形 |
| `_btn_ring` (CTkFrame 196×196) | **刪除** | 不再需要實體 frame |
| `_record_btn` (CTkButton 164×164) | **刪除** | CTkButton 方形 hit-test 和光場衝突 |
| `card` (CTkFrame) | 保留 | 容器不變 |
| `_target_label` | 保留 | 貼上目標顯示 |
| `_hotkey_hint` | 保留但動態變更文字 | 錄音時改「放開停止」 |

### 4.2 新元件

- **`_chamber` (tk.Canvas 280×280)**：取代三層元件，承擔光暈 / 中央 disc / icon / 擴散波紋全部繪製
- **`_timer_label` (CTkLabel)**：從按鈕文字拆出的獨立計時器，`SF Mono 18pt bold`，僅錄音時顯示

### 4.3 資訊流向（不變）

```
hotkey_manager (pynput) ─► on_hotkey_press ─► _transition_to_recording
                                                      │
                                                      ▼
                                          recorder.start() ──► sounddevice callback
                                                      │
                                     ┌────────────────┴──────────┐
                                     ▼                           ▼
                           _render_tick (50ms)          _stream_tick (1s/5s)
                           ← recorder.get_rms_level()    ← transcribe_fast
                                     │
                                     ▼
                           _chamber canvas redraw
```

Render loop 與 business logic 完全解耦，只讀 RMS，不改 recorder 狀態。

## 5. 三態視覺規格

Canvas 280×280，中心 (140, 140)。所有顏色用 `tokens.py` + `animation.blend()` 預計算成實色。

### 5.1 Idle（閒置）

| 屬性 | 值 |
|---|---|
| 主色 | `ACCENT` #06B6D4 （青）← 關鍵變更：**不再使用 SUCCESS 綠** |
| 光暈環 | 5 圈同心圓，半徑 `[80, 96, 112, 128, 140]`，alpha `[0.25, 0.18, 0.12, 0.07, 0.03]`，stroke 1.5px |
| 呼吸週期 | 6 秒 cosine，整體半徑 `0.97 ↔ 1.03` |
| 中央 disc | 半徑 60，填色 `SURF_2`，邊框 `SURF_4` 1.5px |
| 中央 icon | Lucide `mic`，36px，色 `TEXT_2` |
| Timer | 隱藏 |
| Hotkey hint | `按下 ⌘⌥R 即時錄音` |

**為什麼 idle 從 SUCCESS 綠改成 ACCENT 青**：SUCCESS 語意是「就緒/成功狀態」，保留給 status bar 狀態指示；ACCENT 是 app 品牌主 CTA 色，獨佔主按鈕。這是**最直接消除使用者「綠色廉價感」訴求**的一招。

### 5.2 Recording（錄音中）

| 屬性 | 值 |
|---|---|
| 主色 | `DANGER` #EF4444 |
| 光暈環 | 5 圈，半徑同 idle，alpha `[0.35, 0.24, 0.15, 0.08, 0.04]` |
| 呼吸週期 | 2.5 秒，RMS-modulated：`scale = breathe_phase + rms × 0.18`（最大 +18% 擴張）|
| **擴散波紋** | RMS peak 觸發（`rms > 0.15 且 > prev × 1.5`）→ 起始半徑 140，1.2s 擴張至 180，alpha 0.4→0 linear fade，最多 3 個並存（FIFO）|
| 中央 disc | 半徑 60，填色 `blend(DANGER, SURF_1, 0.12)` （深紅微染，非純紅），邊框 `DANGER` 2px |
| 中央 icon | Lucide `square`（停止），28px，色 `TEXT_1` |
| Timer | 顯示 `mm:ss`，`SF Mono 18pt bold`，色 `TEXT_2` |
| Hotkey hint | `放開 ⌘⌥R 停止錄音` |

### 5.3 Processing（轉錄中）

| 屬性 | 值 |
|---|---|
| 主色 | `WARN` #F59E0B |
| 光暈環 | **4 圈**（刻意少一圈強調「收斂」），半徑 `[80, 100, 120, 140]`，alpha `[0.30, 0.18, 0.10, 0.05]` |
| 呼吸週期 | 1.8 秒，振幅 `0.98 ↔ 1.02`（克制）|
| **旋轉粒子** | 12 顆 4px 小圓點均勻分布於半徑 140 外圈，WARN 色，整體旋轉 360° / 1.5s（ease-in-out），alpha 沿圓周梯度 `1.0 → 0.15`（彗尾）|
| 中央 disc | 半徑 60，填色 `SURF_2`，邊框 `WARN` 1.5px |
| 中央 icon | Lucide `mic`，36px，色 `blend(WARN, SURF_2, 0.6)` |
| 點擊 | **停用** |
| Timer | 隱藏 |
| Hotkey hint | `轉錄中…` |

### 5.4 狀態轉場

所有狀態切換為漸變，不瞬切：

| from → to | 時長 | easing |
|---|---|---|
| idle → recording | 240ms | ease-out-cubic |
| recording → processing | 200ms | ease-in-out-cubic |
| processing → idle | 320ms | ease-out-cubic |

轉場期間：主色用 `blend()` 在 `prev_state_color` 和 `curr_state_color` 之間線性插值，呼吸週期同步從舊週期過渡到新週期。

轉場期間**鎖定 click**（避免中間態觸發二次狀態機）。

## 6. 動畫與互動

### 6.1 Render Loop

- Tick rate：`RENDER_TICK_MS = 50`（20 FPS，與現有 `_update_wave` 相同）
- 策略：`canvas.delete("all")` + 完整重繪（簡單可靠，280×280 下成本可忽略）
- 單幀成本估算：約 1.5–2.5ms（< 5% 預算）

### 6.2 Easing

純函式集中在新檔 `animation.py`，無 tkinter 依賴：

```python
def ease_out_cubic(t):    return 1 - (1 - t) ** 3
def ease_in_out_cubic(t): return 4*t**3 if t < 0.5 else 1 - (-2*t+2)**3 / 2
def breathe(phase):       return (1 - math.cos(phase * 2π)) / 2
def blend(fg, bg, alpha): # 線性 RGB 插值，回傳 hex string
```

### 6.3 Hit-testing

沒有 CTkButton 後，改綁 Canvas event：

```python
self._chamber.bind("<Button-1>",       self._on_chamber_click)
self._chamber.bind("<Enter>",          self._on_chamber_enter)
self._chamber.bind("<Leave>",          self._on_chamber_leave)
self._chamber.bind("<Motion>",         self._on_chamber_motion)
self._chamber.bind("<ButtonPress-1>",  self._on_chamber_press)
self._chamber.bind("<ButtonRelease-1>",self._on_chamber_release)
```

- 有效點擊區：圓心 (140,140) 半徑 60 內
- Hover 游標：`<Motion>` 座標在 disc 內 → `cursor="hand2"`，否則清除
- Press 視覺：`<ButtonPress-1>` 設 `_pressed=True` → 整體縮放 0.97，120ms ease-out；`<ButtonRelease-1>` 回彈 180ms ease-out

### 6.4 Reduced Motion 支援

```python
def system_reduce_motion() -> bool:
    try:
        r = subprocess.run(
            ["defaults", "read", "com.apple.universalaccess", "reduceMotion"],
            capture_output=True, text=True, timeout=0.5,
        )
        return r.stdout.strip() == "1"
    except Exception:
        return False
```

啟動時讀一次，不動態監聽（macOS 偏好改變後重啟 app 生效是慣例）。

| 效果 | 正常 | Reduced motion |
|---|---|---|
| 光暈呼吸 | 循環動畫 | 靜止（固定在中間 scale）|
| RMS 擴張 | ±18% | **保留**（RMS 是資訊非裝飾）|
| 擴散波紋 | emit on peak | 停用 |
| 狀態色漸變 | tween | 瞬切 |
| Processing 旋轉粒子 | 1.5s 旋轉 | 靜止 12 顆點 |
| Press 縮放 | 0.97 ↔ 1.0 | 停用 |

### 6.5 效能預算

| 操作 | 每幀成本 |
|---|---|
| `canvas.delete("all")` | 0.1ms |
| 5 圈光暈 `create_oval` | 0.75ms |
| 中央 disc + icon | 0.45ms |
| 0–3 擴散波紋 | ≤0.45ms |
| 12 顆旋轉粒子（processing only） | 0.96ms |
| `blend()` 色彩計算 | <0.1ms |
| **單幀總計** | **約 1.5–2.5ms** |

對照現狀 `_draw_live_wave()` 每幀 ~1ms，新增約 1–1.5ms，遠低於 50ms 預算。

預先優化措施：
- Icon 在 state 切換時**預先繪製**成 PhotoImage（不是每幀 PIL 渲染）
- `blend()` 結果在 state 切換時**預計算**（每 state ~8 個 blended color，存 dict）

## 7. 實作計畫

### 7.1 檔案變更

| 檔案 | 變更類型 | 規模 |
|---|---|---|
| `tokens.py` | 擴充 motion 區段 | +40 行 |
| `icons.py` | 新增 `_i_square` + 註冊 | +15 行 |
| `animation.py` | **新檔**，easing / breathe / blend / Ripple class | +80 行 |
| `gui.py` | 重寫 `_build_record_card` + 相關方法 | ±300 行 |
| `docs/superpowers/specs/2026-04-19-ambient-chamber-design.md` | 本文件 | 新增 |

### 7.2 `tokens.py` 擴充

```python
# MOTION
DUR_FAST    = 120
DUR_NORMAL  = 240
DUR_SLOW    = 400

BREATHE_IDLE_MS       = 6000
BREATHE_RECORDING_MS  = 2500
BREATHE_PROCESSING_MS = 1800
ROTATE_PROCESSING_MS  = 1500
RENDER_TICK_MS        = 50
```

### 7.3 `animation.py` 新檔

純函式模組（無 tkinter 依賴，可單元測試）：

- `ease_out_cubic(t: float) -> float`
- `ease_in_out_cubic(t: float) -> float`
- `breathe(phase: float) -> float`
- `blend(fg: str, bg: str, alpha: float) -> str`
- `class Ripple`: 擴散波紋物件，`state(now) -> (radius, alpha) | None`

### 7.4 `icons.py` 新增

```python
def _i_square(p: _Pen) -> None:
    """Lucide 'square' — stop icon, slightly rounded corners."""
    p.rect(6, 6, 18, 18, r=1.5)

# 註冊
_REGISTRY["square"] = _i_square
```

### 7.5 `gui.py` 重構重點

**移除**：
- `_wave_canvas`, `_draw_idle_wave()`, `_draw_live_wave()`, `_update_wave()`
- `_btn_ring`, `_record_btn`
- `_pulse_btn()`, `_animate_spinner()`
- 硬編常數：`GREEN`, `GREEN_DIM`, `RED`, `RED_DIM`, `ORANGE`, `SPINNER`, `WAVE_IDLE_COL`, `WAVE_LIVE_COL`, `WAVE_BARS`

**新增**：
- `_build_record_card()` 全新實作（單 Canvas 版）
- `_draw_chamber()` 主繪製方法（讀 state + RMS + phase → 渲染）
- `_render_tick()` 50ms 迴圈
- Canvas event handlers（click / hover / press）
- `system_reduce_motion()` 偵測函式

**修改**：
- `_transition_to_recording / processing / idle`：保留所有 business logic，只改 UI 設定那幾行（替換為 `_timer_label.configure(...)` + `_hotkey_hint.configure(...)`）

### 7.6 **不動**的檔案（完整性保證）

- `recorder.py`
- `transcriber.py`
- `hotkey_manager.py`
- `auto_paste.py`
- `config.py`
- `vad.py`
- `ollama_client.py`
- `prompts.py`
- `main.py`

## 8. 驗證策略

### 8.1 靜態檢查（CI 友善）

```bash
# 確認沒有 emoji 當 icon
grep -nE '🎤|🎙|📝|⚙|📋|💾|⌨' gui.py
# 期望：只剩 toast 訊息等非功能性 emoji

# 確認沒有硬編 hex 色碼
grep -nE '#[0-9A-Fa-f]{6}' gui.py
# 期望：0 筆（全部從 tokens 來）

# 確認計時器使用 mono 字體
grep -n 'FONT_FAMILY_MONO' gui.py
# 期望：≥ 1 筆（_timer_label）
```

### 8.2 單元測試

`animation.py` 純函式可直接寫 pytest：
- `ease_out_cubic(0) == 0`, `ease_out_cubic(1) == 1`
- `blend("#000000", "#FFFFFF", 0.5) == "#808080"`
- `Ripple.state(now)` 超過 duration 回 `None`

### 8.3 手動 Smoke Test

| # | 步驟 | 期望 |
|---|---|---|
| 1 | 啟動 App | 青色呼吸光場，中央 mic icon |
| 2 | 按 ⌘⌥R | 光場 240ms 漸變成紅，計時器 `00:00` 出現並遞增 |
| 3 | 對麥克風說話 | 光場隨音量鼓動，大聲時看到擴散波紋 |
| 4 | 放開 ⌘⌥R | 光場 200ms 漸變成琥珀，12 顆粒子旋轉；計時器消失 |
| 5 | 轉錄完成 | 光場 320ms 漸回青，結果顯示於下方 |
| 6 | 系統開啟 Reduce Motion 後重啟 | 光場靜止，狀態瞬切，無擴散波紋，但計時器正常 |
| 7 | 快速連按鍵 | 轉場鎖生效，無中間態異常 |
| 8 | 活動監視器 | 錄音中 CPU < 5%（參考：現狀 3–4%）|

### 8.4 視覺 regression（建議）

- 在 main 截圖 idle / recording / processing 三態
- 實作完成後截圖對比
- 存檔於 `docs/superpowers/specs/record-button-before-after.png`，PR 描述引用

## 9. Rollout 時序

```
1. 合併 PR #4 → main  (呼吸光圈 record button)
2. 合併 PR #5 → main  (design tokens Zinc+Cyan)
3. 合併 PR #6 → main  (Lucide icons)
4. 從 main 開新分支 feat/ambient-chamber
5. 實作：
   5a. tokens.py motion 區段
   5b. animation.py 新檔 + 單元測試
   5c. icons.py _i_square
   5d. gui.py record card 重寫（一個 commit）
6. 靜態檢查 pass
7. 手動 smoke test 8 項全 pass
8. 前後對比截圖存檔
9. 開 PR：feat/ambient-chamber → main
```

## 10. Rollback 計畫

- 所有 gui.py 改動集中在 record card 區段 → `git revert <commit-sha>` 即可完全回到 pre-chamber 狀態
- `animation.py`、`tokens.py` motion 區段、`icons.py` `_i_square` 皆為 additive 新增，不影響其他 caller
- 核心 modules（recorder/transcriber/hotkey/auto_paste/config）完全不觸碰 → 零風險影響錄音與轉錄功能

## 11. 決策紀錄

| 編號 | 決策 | 誰拍板 | 時間 |
|---|---|---|---|
| D1 | 從現有 main 重構 vs 堆疊 PR vs 混合 → **合併 #4/#5/#6 後從乾淨 main 重構** | 使用者 | Q1 |
| D2 | 主要訴求 → **消除綠色按鈕廉價感** | 使用者 | Q2 (E) |
| D3 | 重構範圍 → **整個 record card 整合** | 使用者 | Q3 (C) |
| D4 | 視覺方向 → **D3 Ambient Light Chamber** | 使用者 | Q4 |
| D5 | Idle 從 SUCCESS 綠改成 ACCENT 青 | 使用者確認 | Section 2 |
| D6 | 計時器字型 `SF Mono 18pt bold` | 使用者確認 | Section 2 |
| D7 | 啟用 RMS 擴散波紋 | 使用者確認 | Section 2 |
| D8 | 50ms (20 FPS) render tick | 使用者確認 | Section 3 |
| D9 | `subprocess` 偵測 reduced-motion | 使用者確認 | Section 3 |
| D10 | `canvas.delete("all")` 完整重繪策略 | 使用者確認 | Section 3 |
| D11 | 一個大 PR（feat/ambient-chamber）| Claude 自決 | Section 4 |
| D12 | Claude 代合併 PR #4/#5/#6 | Claude 自決 | Section 4 |

---

**Spec 結束。** 接下來進 writing-plans skill 產生逐步實作計畫。
