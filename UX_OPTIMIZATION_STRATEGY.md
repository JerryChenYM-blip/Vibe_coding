# Whisper Pro — UX / UI 優化策略 v2.0

> **產出日期：** 2026-04-18
> **目標：** 將現有 Python customtkinter 桌面 App，升級為具備 2026 年專業產品質感的語音轉文字工具
> **研究來源：** Apple HIG、Linear Design System、MacWhisper/Aiko 競品分析、ui-ux-pro-max skill、2026 UI 趨勢報告

---

## 執行摘要（TL;DR）

目前 App 已採用 Apple MacBook Pro 深色風格（#000000 基底 + SF Pro 字型），但在**五個關鍵維度**仍有顯著提升空間：

| 維度 | 現況 | 目標 |
|---|---|---|
| **色彩階層** | 2 層（black + surface1）| 4 層表面深度 + LCH 色彩空間 |
| **微動效** | 脈衝閃爍（2 狀態）| Spring physics、呼吸感、狀態轉場 |
| **圖示系統** | Emoji（🎤 📋 ⚙）| Lucide SVG 向量圖示 |
| **資訊密度** | 單頁塞滿 | 漸進式揭露（主畫面極簡）|
| **技術天花板** | tkinter Canvas | 評估遷移到 PyQt6 / Flet |

**三階段推進建議：**

1. **Tier 1 — Quick Wins（1–2 天）**：色彩 token 化、圖示替換、spacing 節奏整頓、reduced-motion 支援
2. **Tier 2 — Visual Polish（3–5 天）**：波形動畫升級、呼吸感脈衝、toast 位移動畫、結果卡片進場效果
3. **Tier 3 — Framework 決策（可選，1–2 週）**：評估遷移到 PyQt6 解鎖真正的毛玻璃、陰影、springs

---

## 一、現況診斷

### 1.1 目前做得不錯的部分 ✅

- 深色主題（符合 2026 dark-first 趨勢）
- SF Pro 字型（macOS 原生感）
- 核心流程極簡（錄音按鈕居中，單一主要動作）
- 自動貼上是**差異化功能**（MacWhisper、Aiko 都沒有）
- 狀態機清晰（idle / recording / processing）

### 1.2 明顯的質感落差 ❌

#### A. 色彩系統扁平
目前只有 `BG (#000000)` 和 `SURF1 (#1D1D1F)` 兩層，缺少 Apple / Linear 都採用的 **4 層表面深度**：

```
現況：        目標：
┌─────────┐   ┌─────────────────┐
│ BG      │   │ BG (dp=0)       │ ← 視窗背景
│ ┌─────┐ │   │ ┌─ Surface 1 ─┐ │ ← 卡片
│ │SURF1│ │   │ │ ┌─ Surf 2 ─┐│ │ ← 次要卡片
│ └─────┘ │   │ │ │ Surf 3   ││ │ ← hover / 點擊
└─────────┘   │ │ └──────────┘│ │
              │ └─────────────┘ │
              │ Overlay         │ ← modal scrim
              └─────────────────┘
```

#### B. Emoji 當圖示
`🎤 📋 💾 ⌨ ⚙` 作為 UI 功能圖示是 2026 年的 **anti-pattern**：
- 跨字型渲染不一致（系統 emoji 更新會改變外觀）
- 無法跟主題色、間距、stroke width 對齊
- 在深色底上對比度不穩定
- 無法 token 化

**解法：** 採用 Lucide Icons（SVG），stroke-width 1.5px，與字型 baseline 對齊。

#### C. 微動效 shortage
目前只有：
- 按鈕脈衝（RED ↔ 深紅切換，550ms，**線性切換感生硬**）
- Spinner 字元動畫（⠋⠙⠸⠴⠦⠇）
- 波形動畫

**缺少的關鍵動效：**
- 狀態轉場（idle → recording）沒有平滑過渡
- Toast 出現/消失是 `place()` + `destroy()`，沒有淡入淡出
- 結果文字直接塞進 textbox，缺少「打字」或「漸進顯示」感
- 按鈕點擊沒有 scale feedback（0.96x 壓下感）

#### D. 資訊密度過高
主畫面同時顯示：
- Logo + 兩個下拉
- 波形 + 錄音按鈕 + 目標 App + 快捷鍵提示
- 結果標題 + 清除按鈕 + 分隔線 + textbox
- 5 顆動作按鈕
- 狀態列（狀態點 + 文字 + 快捷鍵 + 計時）

MacWhisper 和 Aiko 的**共同哲學**是「主畫面只有一個任務：錄音」，其他功能藏在漸進式層級。

#### E. 技術天花板
tkinter Canvas 的**根本限制**：
- 無法真正的背景模糊（backdrop-filter）
- 無法軟陰影（box-shadow 只能偽造）
- 沒有子像素渲染平滑度
- 動畫必須手動 `after()` 輪詢，無法用 CSS-like transition
- 無 GPU 加速

這些限制解釋了為何**同樣設計語彙下，web/原生 app 質感更好**。

---

## 二、2026 UI/UX 趨勢洞察

來自網路研究（Midrocket、Medium、Linear Blog、Stan Vision 等）的關鍵結論：

### 2.1 Dark-first 成為標準
> *「深色模式不再是 afterthought，設計團隊直接以深色為起點。」*

- OLED 最佳化的色彩調校
- 對比度針對深色背景重新調校（不是反轉淺色值）
- **4 層表面深度**建立視覺層次，而非邊框

### 2.2 Glassmorphism 有條件回歸
Apple 2025 推出 **Liquid Glass** 後 7 週即加入「關閉毛玻璃」開關，給設計師的教訓：

> ✅ **用在有意義的地方**：modal / sheet / floating elements（暗示可 dismiss）
> ❌ **避免濫用**：背景裝飾、所有卡片都加毛玻璃

### 2.3 色彩空間升級：LCH > HSL
Linear 2026 重新設計時改用 **LCH 色彩空間**：
- 相同 L（亮度）值視覺上真的一樣亮（HSL 不是）
- 更精準控制對比度
- 暖灰（warm gray）取代冷藍灰

### 2.4 Motion 四大原則
1. **Motion conveys meaning**：每個動畫都要有因果關係
2. **Spring physics > cubic-bezier**：自然感
3. **Exit 比 Enter 快 30–40%**：進場 300ms、出場 200ms
4. **Interruptible**：使用者一操作就能打斷動畫

### 2.5 Typography 的「technical precision」
開發工具類產品標準字型：**Inter**（或 SF Pro）。
- 可變字重（100–900）
- Tabular figures 用於數字對齊
- letter-spacing: -0.01em 在大字時

---

## 三、新設計語彙（Design Tokens）

### 3.1 色彩 Token（LCH-based 深色）

```python
# ─── Semantic tokens (Dark, forced) ──────────────────────
# Background stack (elevation levels)
BG        = "#000000"   # dp=0  void black (window body)
SURF_1    = "#0E0E10"   # dp=1  primary surface (main card)
SURF_2    = "#18181B"   # dp=2  secondary (header, status bar)
SURF_3    = "#27272A"   # dp=3  tertiary (hover, active)
SURF_4    = "#3F3F46"   # dp=4  quaternary (borders, dividers)

# Text hierarchy (warm white)
TEXT_1    = "#FAFAFA"   # Primary (100% weight)
TEXT_2    = "#E4E4E7"   # Secondary (75%)
TEXT_3    = "#A1A1AA"   # Tertiary (55%) — hints, timestamps
TEXT_4    = "#71717A"   # Quaternary (35%) — disabled

# Semantic accents (desaturated for dark bg)
ACCENT    = "#06B6D4"   # cyan-500 — new primary (fresher than Apple blue)
ACCENT_HV = "#22D3EE"
ACCENT_BG = "#164E63"   # tinted bg for accent chips

SUCCESS   = "#22C55E"   # recording idle
DANGER    = "#EF4444"   # recording active
WARN      = "#F59E0B"   # processing
INDIGO    = "#818CF8"   # auto-paste (softer than #5E5CE6)
```

**為何這組色比現況好：**

| 指標 | 現況 `#1D1D1F` | 新 `#0E0E10`→`#3F3F46` |
|---|---|---|
| 表面層級 | 2 層 | 4 層 |
| 黑色基底 | 純黑 #000 | 純黑 #000（OLED 省電）|
| Accent | 皇家藍 #0071E3 | 青色 #06B6D4（更有「科技/AI」感）|
| 暖度 | 冷黑灰 | Zinc 色系（微暖，Linear 風格）|
| 對比 (TEXT_1 vs BG) | 17.9:1 | 19.6:1（AAA）|

### 3.2 字型系統

```python
# 字型堆疊（從原生 → 降級）
FONT_STACK = '"SF Pro Display", "Inter", "Helvetica Neue", sans-serif'

# Type scale（固定節奏）
TYPE = {
    "display":  (28, "bold",   -0.4),   # (size, weight, letter-spacing)
    "title":    (17, "bold",   -0.2),   # 視窗標題
    "headline": (15, "semibold", -0.1),
    "body":     (14, "regular",  0),
    "caption":  (12, "regular",  0),
    "micro":    (11, "medium",   0.1),  # labels
    "mono":     (13, "regular",  0),    # 計時器、hotkey display
}
```

**關鍵改動：** 計時器、RMS、模型名等數字改用 **SF Mono**（tabular figures），避免跳動。

### 3.3 間距與節奏（4pt baseline）

```python
SPACING = {
    "xs":  4,
    "sm":  8,
    "md":  12,
    "lg":  16,
    "xl":  24,
    "2xl": 32,
    "3xl": 48,
}
RADIUS = {
    "sm":  6,
    "md":  10,
    "lg":  14,
    "xl":  20,
    "pill": 999,
}
```

目前 gui.py 混用 `pady=4, 6, 8, 10, 12, 14, 16, 18, 20, 22`，沒有系統。**統一為 4/8/12/16/24/32**。

### 3.4 動效 Token

```python
DURATION = {
    "fast":   120,   # hover, tap feedback
    "normal": 240,   # state transitions
    "slow":   400,   # card enter, modal
}
EASING = {
    "out":    "ease-out",     # entering
    "in":     "ease-in",      # exiting
    "spring": (0.34, 1.56, 0.64, 1),   # bouncy CTA
}
```

---

## 四、核心元件重設計

### 4.1 錄音按鈕 — 從「圓形按鈕」到「生命體」

**靈感：** Siri 的呼吸光暈、Apple Voice Memos 的動態圓環。

#### 現況問題
- 三個狀態用「貼不同顏色」表達，缺少質感
- 脈衝是兩色切換，感覺像壞掉的 LED
- 外環只是靜態邊框

#### 目標設計

```
                    ╭───────────────╮
                   ╱                 ╲          ← 外層光暈
                  │  ╭─────────────╮  │            （呼吸 2s 週期，opacity 30→70%）
                  │ ╱               ╲ │
                  ││    ●           ││         ← 內圓（跟隨音量縮放 1.0→1.05）
                  │╲                 ╱│
                  │ ╰─────────────╯  │
                   ╲                 ╱
                    ╰───────────────╯
```

| 狀態 | 設計 |
|---|---|
| **Idle** | 內圓 SUCCESS 色，外環靜止（2px, 10% opacity），hover 時外環擴散 |
| **Recording** | 內圓 DANGER，**呼吸光暈**（外圈半徑 192→210px，opacity 脈動）|
| **Processing** | 外環變成**旋轉 gradient**（conic gradient 模擬，每 1.5s 轉一圈）|

#### customtkinter 實作方式（不需遷移就能做）
- 用 `tk.Canvas` 畫同心圓，每 16ms 更新半徑/透明度（60fps）
- Alpha compositing 用 `stipple` + 分層畫布模擬
- **限制：** 無法真正的 blur glow，但同心圓疊加 + 顏色漸變可以接近

### 4.2 波形視覺 — 從「隨機柱狀」到「頻譜感」

#### 現況問題
- 42 根柱子 + 隨機 jitter，看起來像雜訊
- 波形色 idle 時太灰（#3A3A3C），存在感低
- 柱高用 RMS × 常數，變化扁平

#### 目標設計

```
Idle:  微弱呼吸（所有柱高 4–8px，整體 opacity 從 20% 到 40% 呼吸）

Recording:
     ▁▂▃▅█▆▃▁▂▃▄▆█▇▅▃▂▁▁▂▄▆█▇▄▂▁▁▂▃▄▆▇█▇▅▃▂▁
     ↑                                         ↑
     低頻（左）                                   高頻（右）
     偏冷色 (#06B6D4)                           偏暖色 (#EF4444)
```

#### 設計重點
1. **頻率感**：左低右高的柱高分佈（模擬頻譜分析，即使我們沒做真的 FFT）
2. **顏色漸層**：X 軸從 ACCENT 過渡到 DANGER（當 RMS 高時整體偏紅）
3. **平滑化**：新柱高 = 0.7 × 舊柱高 + 0.3 × 新值（lerp，避免跳動）
4. **Idle 呼吸**：用正弦波驅動 opacity，不只靜態

```python
# 偽程式碼
bar_heights = [0.7 * old[i] + 0.3 * new[i] for i in range(N)]
hue = lerp(HUE_CYAN, HUE_RED, min(rms * 5, 1))
```

### 4.3 結果卡片 — 從「分隔線」到「Timeline」

#### 現況問題
- 多次錄音用 `─────` 分隔，像 console log
- 無法區分每段錄音的時間、時長
- Scroll 沒有位置指示

#### 目標設計

每次錄音產生一張「訊息卡片」，類似 iMessage：

```
┌──────────────────────────────────────────────┐
│  14:23 · 12s · ZH · large-v3-turbo           │ ← micro label
│                                              │
│  這是轉錄出來的文字內容...                  │ ← body text
│  可以多行，支援 wrap                        │
│                                              │
│  [⧉] [⤴] [✦]                                 │ ← hover 才出現的行動
└──────────────────────────────────────────────┘
            3s 前貼入 LINE ✓                    ← 貼上確認 chip

┌──────────────────────────────────────────────┐
│  14:19 · 8s · EN · large-v3-turbo            │
│  Hello this is a test recording...           │
└──────────────────────────────────────────────┘
```

**好處：**
- 每段錄音都是獨立 object，可針對單段「重新潤飾」「刪除」「匯出」
- 視覺上明確區隔，不再是長文字河
- Hover 才出現工具列，主畫面更乾淨

### 4.4 設定面板 — 從「表單」到「System Preferences」

#### 現況問題
- 用 `CTkScrollableFrame` 裝 row，間距不夠、視覺節奏斷裂
- Switch 開關跟 macOS 系統控制風格不一致
- 沒有搜尋（設定項多時難找）

#### 目標設計：側邊分類（macOS System Settings 風格）

```
┌──────────┬──────────────────────────────────────┐
│  Search  │                                      │
├──────────┤  語音辨識                            │
│ ◉ 一般   │  ┌────────────────────────────────┐ │
│ ◉ 語音   │  │  模型       large-v3-turbo  ▾  │ │
│ ◉ 快捷鍵 │  │  語言       自動偵測        ▾  │ │
│ ◉ 輸出   │  └────────────────────────────────┘ │
│ ◉ 進階   │                                      │
│ ◉ 關於   │  快捷鍵                              │
│          │  ...                                 │
└──────────┴──────────────────────────────────────┘
```

**好處：**
- 可擴充（未來加 20 個設定項也不擠）
- 跟 macOS 使用者的心智模型一致
- 搜尋框讓設定項目可發現性提升

---

## 五、技術遷移評估

### 5.1 選項比較

| 維度 | customtkinter（現況） | PyQt6 / PySide6 | Flet（Flutter）|
|---|---|---|---|
| **模糊/陰影** | ❌ 無 | ✅ QGraphicsBlurEffect | ✅ 原生 |
| **動畫** | ❌ 手動 `after()` | ✅ QPropertyAnimation + easing | ✅ AnimatedContainer |
| **字型渲染** | ⚠️ tkinter 子像素渲染差 | ✅ Qt native | ✅ 完美 |
| **圖示系統** | ❌ 需手刻 SVG loader | ✅ QIcon + SVG | ✅ Material Icons 內建 |
| **Canvas 繪圖** | ✅ 夠用 | ✅ QPainter（更強）| ⚠️ 需 CustomPaint |
| **打包體積** | 小（~30MB）| 中（~80MB）| 大（~120MB）|
| **學習曲線** | 🟢 低 | 🟡 中（signal/slot）| 🟢 低（類 Flutter）|
| **Python 生態** | 🟢 相容 | 🟢 相容 | 🟢 相容 |
| **macOS 質感** | 🟡 像素風 | 🟢 接近原生 | 🟢 現代 flat |
| **你的程式碼量改寫** | - | ~70% UI 層 | ~90% |

### 5.2 我的建議：**分階段遷移**

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│  Tier 1     │ →  │  Tier 2     │ →  │  Tier 3     │
│  token 化   │    │  動效升級   │    │  PyQt6 遷移 │
│  圖示替換   │    │  波形/按鈕  │    │  （可選）   │
│  2 天       │    │  5 天       │    │  2 週       │
└─────────────┘    └─────────────┘    └─────────────┘
     ↑
 立即執行
```

**為何推薦 PyQt6 而非 Flet？**
1. PyQt6 的 QML 支援原生 macOS 毛玻璃（`NSVisualEffectView`）
2. Flet 基於 Flutter，是 web-tech 渲染，雖然現代但**不夠 Mac 原生**
3. 語音 App 體積敏感（整合 Whisper 模型已經大），Flet 多 90MB 不值

---

## 六、優先實作清單（Roadmap）

### Tier 1 — Quick Wins（2 天內）

| # | 任務 | 檔案 | 難度 |
|---|---|---|---|
| 1 | 建立 design token 模組 `tokens.py` | 新增 | 🟢 |
| 2 | 引入 Lucide SVG 圖示載入器 | 新增 | 🟢 |
| 3 | 替換 gui.py 中所有 emoji icon | `gui.py` | 🟢 |
| 4 | 統一 spacing 到 4/8/12/16/24 | `gui.py` | 🟢 |
| 5 | 加入 4 層 surface elevation | `gui.py` | 🟢 |
| 6 | 改用 Zinc 暖色系（從純黑冷灰 → 暖黑）| `gui.py` | 🟡 |
| 7 | 計時器/RMS 改 SF Mono tabular | `gui.py` | 🟢 |
| 8 | 支援 `prefers-reduced-motion`（環境變數）| `gui.py` | 🟢 |

### Tier 2 — Visual Polish（3–5 天）

| # | 任務 | 檔案 | 難度 |
|---|---|---|---|
| 9 | 錄音按鈕呼吸光暈（Canvas 多層同心圓）| `gui.py` | 🟡 |
| 10 | 波形平滑化（lerp）+ 頻譜漸層色 | `gui.py` | 🟡 |
| 11 | Toast 淡入淡出動畫 | `gui.py` | 🟢 |
| 12 | 結果卡片化（Timeline 風格）| `gui.py` | 🔴 |
| 13 | 按鈕 press scale feedback (0.96x) | `gui.py` | 🟡 |
| 14 | 狀態轉場動畫（idle→rec 顏色 crossfade）| `gui.py` | 🟡 |
| 15 | Processing 狀態用旋轉 gradient 外環 | `gui.py` | 🔴 |

### Tier 3 — Framework 升級（1–2 週，可選）

| # | 任務 | 難度 |
|---|---|---|
| 16 | 建立 PyQt6 POC（一個主視窗）| 🟡 |
| 17 | 遷移 hotkey_manager + recorder（純邏輯）| 🟢 |
| 18 | 用 QGraphicsBlurEffect 實作毛玻璃 modal | 🟡 |
| 19 | 用 QPropertyAnimation 重寫所有動畫 | 🔴 |
| 20 | 整合 NSVisualEffectView（macOS 原生毛玻璃）| 🔴 |
| 21 | 側邊欄式設定面板 | 🟡 |

---

## 七、成功指標（KPI）

### 視覺質感（主觀）
- [ ] 請 5 位非設計師看 App 截圖，主觀評分 ≥ 8/10（滿分 10，基準線 MacWhisper）
- [ ] 在 OLED 外接螢幕上不會有「光汙染」感（純黑 BG 關鍵）

### 可用性（客觀）
- [ ] 所有文字對比度 ≥ 4.5:1（WCAG AA）
- [ ] 所有可點擊元素 ≥ 44×44pt
- [ ] 所有動畫 ≤ 400ms
- [ ] reduced-motion 環境下動畫降級正常
- [ ] 鍵盤完全可操作（不靠滑鼠）

### 技術健康度
- [ ] Design token 單一來源（不再有散落 hex 值）
- [ ] 零 emoji 作為功能圖示
- [ ] 60fps 波形動畫（無卡頓）
- [ ] App 啟動到可用時間 ≤ 1.5s（目前 ~2s）

### 產品差異化
- [ ] 自動貼上功能在首屏 5 秒內可被使用者發現（onboarding）
- [ ] 多次錄音 Timeline 視覺化（MacWhisper/Aiko 都沒有）
- [ ] 單一 window，無多餘視窗干擾

---

## 八、建議立即動手的三件事

若你只能今天做 3 件事，就做這些：

1. **建立 `tokens.py`** — 把所有色彩、間距、字型集中管理（影響後續所有改動）
2. **錄音按鈕呼吸光暈** — 最高辨識度、最能「被感受到質感」的一個元件
3. **結果卡片 Timeline 化** — 產品差異化最強的功能，MacWhisper/Aiko 都沒做

---

## 參考資料

- [UI Design Trends for 2026 — Midrocket](https://midrocket.com/en/guides/ui-design-trends-2026/)
- [Dark Glassmorphism: The Aesthetic That Will Define UI in 2026 — Medium](https://medium.com/@developer_89726/dark-glassmorphism-the-aesthetic-that-will-define-ui-in-2026-93aa4153088f)
- [How we redesigned the Linear UI (part Ⅱ)](https://linear.app/now/how-we-redesigned-the-linear-ui)
- [Dark Mode Design Systems: A Complete Guide — Muzli](https://muz.li/blog/dark-mode-design-systems-a-complete-guide-to-patterns-tokens-and-hierarchy/)
- [Which Python GUI library should you use in 2026? — PythonGUIs](https://www.pythonguis.com/faq/which-python-gui-library/)
- [7 Best MacWhisper Alternatives in 2026](https://transcriber.craftby.dev/blog/best-macwhisper-alternatives)
- Apple Human Interface Guidelines — macOS
- ui-ux-pro-max skill（本地 skill 產出之設計系統建議）

---

*本策略文件由 AI 協助生成，基於網路研究 + 設計系統 skill + 競品分析綜合產出。實作階段可隨時調整。*
