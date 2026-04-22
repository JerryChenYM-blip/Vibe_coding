# App Icon + 啟動畫面 設計文件

**日期**：2026-04-22  
**分支**：feat/phase1-ollama-polish  
**負責人**：Jerry Chen  

---

## 目標

為 Whisper Pro v2.0 建立：
1. 正式 macOS App Icon（Dock、⌘+Tab、視窗左上角）
2. 品牌感啟動畫面（純展示，1.5 秒，無載入進度）

---

## 方案選擇

採用**純 PIL 手繪**方案（與現有 `icons.py` 的 Lucide 風格統一），理由：
- 零新依賴
- 可在任何機器重新生成，不依賴外部設計檔
- 視覺風格 100% 一致

---

## 一、App Icon

### 視覺規格

| 屬性 | 值 |
|---|---|
| 尺寸 | 1024 × 1024 px（主圖），縮至 16/32/64/128/256/512 |
| 背景色 | `#09090B`（Zinc 950） |
| 主體元素 | Lucide 風格麥克風 + 右側 3 條音波弧線 |
| 主色 | `ACCENT` Cyan `#06B6D4` |
| 音波透明度 | 內 → 外：100% → 60% → 30% |
| 光暈 | 麥克風後方低透明度 Cyan 圓形 |
| 線寬 | 2px（4× 超採樣後縮放） |
| 超採樣 | 4×（與 icons.py 一致） |

### 輸出檔案

```
assets/
├── icon.png          # 1024×1024 主圖
├── icon.iconset/     # iconutil 所需目錄
│   ├── icon_16x16.png
│   ├── icon_16x16@2x.png
│   ├── icon_32x32.png
│   ├── icon_32x32@2x.png
│   ├── icon_128x128.png
│   ├── icon_128x128@2x.png
│   ├── icon_256x256.png
│   ├── icon_256x256@2x.png
│   ├── icon_512x512.png
│   └── icon_512x512@2x.png
└── WhisperPro.icns   # iconutil 輸出
```

### 新增檔案

**`app_icon.py`**（獨立腳本，可單獨執行）
- `draw_icon(size: int) -> PIL.Image`：畫出指定尺寸的 icon
- `generate_iconset()`：產生所有尺寸並呼叫 `iconutil`
- `main()`：CLI 入口，執行後自動產生 `assets/WhisperPro.icns`

### 整合點

`main.py`：
```python
from PIL import ImageTk
img = Image.open("assets/icon.png").resize((64, 64))
root.iconphoto(True, ImageTk.PhotoImage(img))
```

---

## 二、啟動畫面

### 視窗規格

| 屬性 | 值 |
|---|---|
| 尺寸 | 480 × 280 px |
| 位置 | 螢幕正中央 |
| 標題列 | 無（`overrideredirect(True)`） |
| 背景 | `SURF_1` (#0E0E10) |
| 外框 | `SURF_4` (#3F3F46)，1px |

### 內容排版

```
┌──────────────────────────────────────────┐
│                                          │
│         [Cyan 麥克風 icon, 56px]         │
│                                          │
│             Whisper Pro                  │
│        SF Pro Display, 28px, TEXT_1      │
│                                          │
│        本地語音轉文字，完全離線           │
│        SF Pro Text, 13px, TEXT_3         │
│                                          │
│                              v2.0        │
│                         TEXT_4, 11px     │
└──────────────────────────────────────────┘
```

### 行為流程

1. `main.py` 啟動時，主視窗先 `withdraw()`（隱藏）
2. `SplashScreen(root)` 建立並顯示
3. 1.5 秒（1500ms）後觸發淡出動畫（200ms，alpha 1.0 → 0.0）
4. 淡出完成後：`splash.destroy()` → `root.deiconify()`
5. Whisper 模型在背景靜默預熱，不阻塞 Splash 計時

### 新增檔案

**`splash.py`**（`SplashScreen` class）
- `__init__(master)`：建立視窗、繪製內容
- `_schedule_close()`：設定 1500ms 後開始淡出
- `_fade_out(step)`：遞迴縮減 alpha，完成後呼叫 `on_done`
- `on_done`：callback，由 `main.py` 傳入（執行 `deiconify`）

---

## 修改清單

| 檔案 | 動作 | 說明 |
|---|---|---|
| `app_icon.py` | 新增 | 生成 icon + .icns |
| `splash.py` | 新增 | SplashScreen class |
| `main.py` | 修改 | 整合 icon 設定 + Splash 流程 |
| `assets/` | 新增目錄 | 存放 icon 相關檔案 |

---

## 邊界條件

- **assets/ 不存在**：`app_icon.py` 自動建立
- **iconutil 不存在**：僅產生 PNG，跳過 .icns（非 macOS 環境）
- **`icon.png` 不存在**：`main.py` 捕捉例外，靜默略過 icon 設定
- **Splash 視窗被關閉**：`on_done` callback 仍執行，主視窗正常顯示
