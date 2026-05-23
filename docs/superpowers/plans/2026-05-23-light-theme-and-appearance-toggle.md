# Plan：淺色主題 + Appearance 切換（v2.6.0）

> 建立於：2026-05-23
> Skill：`/plan-design-review`
> Variant 決議：**C — Hybrid（蘋果結構 + Claude 珊瑚主 + Apple 藍輔）**
> UX 決議：**Restart 自動重啟**（~2 秒、splash 過場）
> Mockup：`~/.gstack/projects/JerryChenYM-blip-Vibe_coding/designs/light-theme-20260523/`

---

## TL;DR

加一個淺色主題、Settings 內可切換。色調走「Apple 結構 + Claude 溫度」混合 ── 微暖白 `#FAFAF7` 背景、純白 `#FFFFFF` 卡片、Claude 珊瑚 `#D97757` 作為主 CTA、Apple 藍 `#007AFF` 作為輔助連結。切換 UX 走 Restart auto-relaunch（~2 秒），原因：Ambient Chamber 是手繪 Canvas、`tokens.py` 被 30+ 處 import 為單值常數、live switch 風險高。`tokens.py` 改成 theme-aware 模組、`config.py` 加 `theme` 欄位、`main.py` 加 `_relaunch_app()` helper、Settings 加「外觀」section 帶 toast + 自動 relaunch。暗色仍維持 zinc + cyan 不動。

---

## Approved Mockups

| Screen | Mockup Path | Direction | Notes |
|---|---|---|---|
| 主視窗（淺色 Hybrid）| `~/.gstack/projects/JerryChenYM-blip-Vibe_coding/designs/light-theme-20260523/variant-C-hybrid.html` | 微暖白 + Claude 珊瑚 + Apple 藍輔 | HTML/CSS mockup，可直接 inspect 量測 hex |
| 比較頁（3 variant 並排）| `~/.gstack/projects/.../light-theme-20260523/comparison.html` | A vs B vs C 同 viewport 並排 | 決策依據 |
| 色票對比 | `~/.gstack/projects/.../light-theme-20260523/palette-comparison.png` | 12 個 token slot × 3 variant 並排 | PIL 手繪、便於離線參考 |

實作時主要對齊 Variant C HTML。

---

## 完整 Palette 規格

### 暗色主題（**不動**、現狀 v2.5.0 維持）
| Token | Value | 用途 |
|---|---|---|
| BG | `#000000` | 視窗背景 |
| SURF_1 | `#0E0E10` | 卡片主體 |
| SURF_2 | `#18181B` | 狀態列、hover |
| SURF_3 | `#27272A` | pressed |
| SURF_4 | `#3F3F46` | 邊框、分隔線 |
| TEXT_1 | `#FAFAFA` | 標題 |
| TEXT_2 | `#E4E4E7` | 內文 |
| TEXT_3 | `#A1A1AA` | 輔助 |
| TEXT_4 | `#71717A` | 停用 |
| ACCENT | `#06B6D4` | cyan，主 CTA |
| SUCCESS | `#22C55E` | 綠 |
| DANGER | `#EF4444` | 紅 |
| WARN | `#F59E0B` | 琥珀 |
| INDIGO | `#818CF8` | 自動貼上 |

### 淺色主題 — Variant C Hybrid（**新增**）
| Token | Value | 用途 | WCAG vs SURF_1 |
|---|---|---|---|
| BG | `#FAFAF7` | 視窗背景（微暖白）| — |
| SURF_1 | `#FFFFFF` | 卡片（Apple-clean）| — |
| SURF_2 | `#F1F0EC` | raised / 副表面 | — |
| SURF_3 | `#E5E3DC` | pressed / hover | — |
| SURF_4 | `#D4D2C8` | 邊框 / 分隔線 | — |
| TEXT_1 | `#1A1612` | 標題（warm 近黑）| ~16:1 AAA ✓ |
| TEXT_2 | `#3D362B` | 內文（warm dark）| ~10:1 AAA ✓ |
| TEXT_3 | `#6E6E73` | 輔助（Apple-cool 灰、刻意冷以平衡暖度）| ~5.5:1 AA ✓ |
| TEXT_4 | `#A8A8AD` | 停用 | ~2.7:1（停用態本就低對比、AA 對停用態不強求）|
| ACCENT | `#D97757` | Claude coral（主 CTA / active）| ~3.4:1（AA Large Text & UI ✓，body 不用） |
| ACCENT_HV | `#C66445` | 珊瑚 hover | — |
| ACCENT_BG | `#F6E8DE` | 珊瑚 chip 淡底 | — |
| LINK | `#007AFF` | Apple 藍（連結 / info icon）| ~4.5:1 AA ✓ |
| SUCCESS | `#2E8B57` | balanced green | ~4.6:1 AA ✓ |
| SUCCESS_HV | `#246E47` | — | — |
| SUCCESS_DIM | `#E0EFE5` | success chip 淡底 | — |
| DANGER | `#D14B41` | warm Apple red | ~4.5:1 AA ✓ |
| DANGER_HV | `#B53A30` | — | — |
| DANGER_DIM | `#F8E2DF` | danger chip 淡底 | — |
| WARN | `#C7842B` | muted amber | ~3.6:1（AA Large / UI ✓）|
| WARN_HV | `#A56E22` | — | — |
| WARN_DIM | `#F5E9D4` | warn chip 淡底 | — |
| INDIGO | `#6366F1` | 自動貼上 active | ~4.5:1 AA ✓ |
| INDIGO_HV | `#4F46E5` | — | — |
| INDIGO_DIM | `#E0E1FA` | indigo chip 淡底 | — |

### 字型 / 間距 / 圓角（兩主題共用）
不動。`FONT_FAMILY_UI` / `FONT_FAMILY_TEXT` / `FONT_FAMILY_MONO` 與 `SPACE_*` / `RADIUS_*` 都是中性 token、不需要 theme variant。

---

## 架構：tokens.py 變成 theme-aware

### Before（現狀）
```python
# tokens.py
BG     = "#000000"
SURF_1 = "#0E0E10"
ACCENT = "#06B6D4"
...
```

30+ 個檔案直接 `from tokens import BG, SURF_1, ACCENT, ...`，是 module-level 常數。

### After
```python
# tokens.py
import config as _config

_PALETTES = {
    "dark": {
        "BG":     "#000000",
        "SURF_1": "#0E0E10",
        # ...（既有 zinc + cyan）
    },
    "light": {
        "BG":     "#FAFAF7",
        "SURF_1": "#FFFFFF",
        "ACCENT": "#D97757",   # Claude coral
        "LINK":   "#007AFF",   # Apple blue
        # ...（完整 Variant C palette）
    },
}

def _active_theme() -> str:
    """讀 config.json 的 theme 欄位，預設 dark。"""
    try:
        return _config.Config.load().theme
    except Exception:
        return "dark"

_THEME = _active_theme()
# Eng Review Issue 2 / 2026-05-23：使用者實驗性手改 config.theme 為 "purple"
# 或舊版 typo（"Light"/"LIGHT"）→ _PALETTES[_THEME] KeyError 啟動 crash。
# 一行 fallback 保護：未知值靜默降級為 "dark" + log 警告（讓使用者查 log
# 看到「為什麼是深色？」能查到原因）。
if _THEME not in _PALETTES:
    import logging as _logging
    _logging.getLogger("whisper_pro.tokens").warning(
        f"tokens: unknown theme {_THEME!r} in config, fallback to 'dark'"
    )
    _THEME = "dark"
_P = _PALETTES[_THEME]

# 對外仍是 module-level 常數，所有既有 `from tokens import BG` 都不需要改
BG       = _P["BG"]
SURF_1   = _P["SURF_1"]
SURF_2   = _P["SURF_2"]
SURF_3   = _P["SURF_3"]
SURF_4   = _P["SURF_4"]
TEXT_1   = _P["TEXT_1"]
TEXT_2   = _P["TEXT_2"]
TEXT_3   = _P["TEXT_3"]
TEXT_4   = _P["TEXT_4"]
ACCENT   = _P["ACCENT"]
ACCENT_HV = _P["ACCENT_HV"]
ACCENT_BG = _P["ACCENT_BG"]
SUCCESS  = _P["SUCCESS"]
# ... 完整一份

# 新增 token（淺色才有，暗色 fallback 同 ACCENT）
LINK     = _P.get("LINK", _P["ACCENT"])
```

**關鍵特性**：
- 既有 `from tokens import ...` 一行都不用改、全部 30+ files 受惠
- `_THEME` 是 module-level、import 時就鎖定，**不**支援 live switch
- 切換靠 restart：使用者改 config → relaunch → 新 process import tokens 時讀到新 theme

### Circular import 風險
`tokens.py` import `config`，但 `config.py` 不能 import `tokens`（會循環）。目前 config 沒這個依賴，安全。

### CustomTkinter 全域 appearance mode
```python
# main.py 開頭，在 build AppWindow 之前
from _version import __version__
from config import Config
cfg = Config.load()
ctk.set_appearance_mode("Light" if cfg.theme == "light" else "Dark")
```

讓 CTk 內建的 light/dark token（部分 widget 預設樣式）也對齊。

---

## Config 變更

```python
# config.py
@dataclass
class Config:
    # ... 既有欄位
    theme: str = "dark"   # "dark" | "light"，預設沿用既有 zinc/cyan
```

`valid_data` filter 機制讓舊使用者的 config.json 沒這個欄位也能載入（fallback dark）。

---

## Restart UX 流程

### User Journey
```
 STEP                          | USER DOES                | USER FEELS / SEES
 ──────────────────────────────|──────────────────────────|──────────────────────────────
 1 進入設定                    | 點底部「⚙ 設定」         | 設定視窗開啟（暗色）
 2 找到「外觀」section          | 自然滑到第一個 section   | 看到「外觀」標題 + 兩顆 chip
 3 切換 dark/light             | 點「淺色」chip           | chip 點亮、顯示 confirm 對話框
 4 確認重啟                    | 點「確認、自動重啟」     | 主視窗 fade 0.5s → 自動 quit
 5 等 splash                   | 等 ~2 秒                 | 看到 Whisper Pro v2.6.0 splash
 6 落地淺色介面                | 自然繼續使用             | 介面是淺色 Hybrid、設定保留所有狀態
```

### 互動細節

**Settings「外觀」section**（放在 Settings 第一個 section、位置最顯眼）：

```
┌─ 外觀 ──────────────────────────┐
│ 主題                            │
│ ┌──────┐ ┌──────┐               │
│ │ 深色 │ │ 淺色 │               │
│ └──────┘ └──────┘               │
│ 深色：zinc + cyan（目前）       │
│ 淺色：暖白 + Claude 珊瑚         │
└─────────────────────────────────┘
```

**點切換 chip 之後**（如果選的跟現在不一樣）：彈出 modal confirm dialog：

```
┌─ 切換主題 ─────────────────────┐
│ 切換到「淺色」需要重新啟動 App │
│                                │
│ • 進行中的錄音會被中斷         │
│ • 所有設定 / 歷史紀錄都會保留  │
│ • Splash 過場後落地新主題      │
│                                │
│       [取消]   [確認、重啟]    │
└────────────────────────────────┘
```

點「確認、重啟」：
1. `cfg.theme = "light"; cfg.save()`（落地新 config）
2. `log_action("theme_switched", from=old, to=new)`
3. Toast：「主題切換中、~2 秒...」（淡入）
4. `app.after(800, _do_relaunch)`（給 toast 時間顯示）
5. `_do_relaunch()`（**Eng Review Issue 1 / 2026-05-23 修法：cleanup-first 順序**）：
   - **Step A — cleanup 完整跑完**（避免 spawn-quit 之間的 race window）：
     - `app.on_close()` 內部完成：`hotkey_mgr.stop()` 移除 NSEvent monitor、`recorder.stop()` 釋放麥克風、`mini_window.destroy()`、`history_store.close()` 關 SQLite、`prompt_reloader.stop()`、Cocoa observers `removeObserver_`
     - 完成後本 process 不再持有任何 system resource（mic / NSEvent monitor / 檔案 handle）
   - **Step B — spawn 新 process**：
     - 從 .app bundle：`subprocess.Popen(["open", "-n", str(_APP_BUNDLE)])`
     - dev 模式：`os.execv(sys.executable, [sys.executable, str(Path(__file__))])`
   - **Step C — 舊 process 終止**：
     - `.app` path：`sys.exit(0)`（execv path 不需要、execv 已 replace process image）

  **為什麼 cleanup-first**：舊 process 仍持有 NSEvent monitor + mic 時 spawn 新 process →
  新 process 同時 register hotkey listener → 雙觸發 / mic 衝突 / config.json
  race。zero race window 換 ~50-100ms cleanup 延遲、總重啟時間仍在 2s 預算內。

點「取消」：不動 config、chip 視覺上回到原本主題的 active。

### Edge cases

| 場景 | 處理 |
|---|---|
| 錄音進行中按重啟 | confirm dialog 明示「進行中的錄音會被中斷」、使用者確認才執行 |
| Ollama 正在潤飾 | 同上，dialog 加一句「進行中的潤飾結果會遺失」 |
| .app bundle 路徑找不到（執行 .py from venv）| fallback 用 `os.execv` re-exec 自己 |
| relaunch 自己也失敗 | toast「重啟失敗、請手動重新啟動」+ 不 quit（讓使用者手動） |
| 多 instance 同時開啟 | `open -n` 會開新 instance；舊 instance 自己 quit；無雙開風險 |

### Auto-relaunch 實作細節

```python
# main.py
import sys, os, subprocess
from pathlib import Path

_APP_BUNDLE = Path.home() / "Applications" / "WhisperPro.app"

def _relaunch_app() -> None:
    """重啟自己。從 .app bundle 啟動就 open -n 開新 instance；
    從 CLI 啟動就 os.execv 同進程 re-exec。"""
    try:
        # 偵測是否從 .app bundle 啟動
        # sys.executable 在 .app shim 模式下會含 "WhisperPro.app"
        is_bundled = "WhisperPro.app" in sys.executable
        if is_bundled and _APP_BUNDLE.exists():
            subprocess.Popen(
                ["open", "-n", str(_APP_BUNDLE)],
                start_new_session=True,
            )
        else:
            # Dev 模式 — re-exec 同個 python + main.py
            os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve())])
    except Exception:
        log_error("relaunch_failed")
        # 不 quit、讓使用者手動處理
        return False
    return True
```

關鍵：`open -n` 的 `-n` 強制開新 instance；避免 LaunchServices 因為「同 bundle id 已在跑」而把舊 process 拉到前景而不開新的。

---

## Settings UI 結構

新 section 放 Settings 第一個（最顯眼）：

```python
# gui.py SettingsWindow._build_ui
def _build_appearance_section(self):
    section = ctk.CTkFrame(self._scroll, fg_color=SURF_1, corner_radius=RADIUS_LG)
    section.pack(fill="x", pady=(0, SPACE_MD))

    # Section title
    ctk.CTkLabel(
        section, text="外觀",
        font=ctk.CTkFont(FONT_FAMILY_UI, 14, "bold"),
        text_color=TEXT_1,
    ).pack(anchor="w", padx=SPACE_LG, pady=(SPACE_MD, SPACE_XS))

    # Theme segmented control
    wrap = ctk.CTkFrame(section, fg_color="transparent")
    wrap.pack(anchor="w", padx=SPACE_LG, pady=(0, SPACE_MD))

    self._theme_var = ctk.StringVar(value=self.cfg.theme)
    for value, label in [("dark", "深色"), ("light", "淺色")]:
        btn = ctk.CTkButton(
            wrap, text=label, width=80, height=32,
            command=lambda v=value: self._on_theme_clicked(v),
            ...
        )
        btn.pack(side="left", padx=(0, SPACE_SM))

    # Description
    ctk.CTkLabel(
        section,
        text=(
            "深色：zinc + cyan（v2.5.0 起的預設）\n"
            "淺色：暖白 + Claude 珊瑚（v2.6.0 新增）"
        ),
        font=ctk.CTkFont(FONT_FAMILY_TEXT, 11),
        text_color=TEXT_3,
        justify="left",
    ).pack(anchor="w", padx=SPACE_LG, pady=(0, SPACE_MD))


def _on_theme_clicked(self, new_theme: str):
    if new_theme == self.cfg.theme:
        return  # 沒變、不彈 confirm
    # 警告：錄音 / 潤飾進行中的狀態
    state_warning = ""
    if self._app._state == "recording":
        state_warning = "\n\n⚠ 你正在錄音、重啟會中斷錄音。"
    elif self._app._polish_busy:
        state_warning = "\n\n⚠ AI 潤飾進行中、重啟會遺失結果。"

    confirm = self._build_confirm_dialog(new_theme, state_warning)
    if confirm.confirmed:
        self.cfg.theme = new_theme
        self.cfg.save()
        log_action("theme_switched", **{"from": self.cfg.theme, "to": new_theme})
        self._app._show_toast(f"主題切換中、~2 秒...")
        self._app.after(800, main._relaunch_app)
```

---

## Implementation Tasks

### Phase 1：基礎建設（必做）

- [ ] **T1 (P1, human: ~1h / CC: ~10min)** — `tokens.py` — theme-aware refactor
  - 加 `_PALETTES = {"dark": {...}, "light": {...}}`
  - import 時讀 `Config.load().theme` 決定 active palette
  - module-level 常數 expose 既有 API 不變
  - 新增 `LINK` token（dark fallback 為 ACCENT）
  - Verify：`from tokens import BG; assert BG in ("#000000", "#FAFAF7")` 跟著 config

- [ ] **T2 (P1, human: ~10min / CC: ~3min)** — `config.py` — 加 `theme: str = "dark"` 欄位
  - dataclass 加欄位、預設 dark
  - `valid_data` filter 確保舊 config.json 無此欄位也能載入
  - Verify：`tests/` 加一個「config 沒 theme 欄位時預設 dark」test

- [ ] **T3 (P1, human: ~30min / CC: ~5min)** — `main.py` — `_relaunch_app()` helper + `ctk.set_appearance_mode` 對齊
  - 加 `_relaunch_app()` 含 .app bundle vs dev mode 兩條路徑
  - main 入口讀 cfg.theme 設 `ctk.set_appearance_mode`
  - Verify：unit test mock `subprocess.Popen` + `os.execv` 路徑分支

- [ ] **T4 (P1, human: ~1.5h / CC: ~20min)** — `gui.py` SettingsWindow — 加「外觀」section
  - `_build_appearance_section`
  - segmented control（深色 / 淺色）
  - confirm dialog 含錄音 / 潤飾進行中警告
  - 點確認 → save config → toast → relaunch
  - Verify：手動測 4 個分支（idle/recording/polishing 切換 vs 取消）

### Phase 2：tokens.py LINK token 採用（強化品牌語意）

- [ ] **T5 (P2, human: ~30min / CC: ~10min)** — 把現有「連結 / info icon」用 ACCENT 的地方改用 LINK
  - 找：歷史視窗的「點此重新潤飾」連結（如有）、Ollama 連線提示
  - 暗色主題下 LINK == ACCENT，視覺不變
  - 淺色主題下 LINK 是 Apple 藍、ACCENT 是 Claude 珊瑚 → 視覺有差別
  - Verify：grep 找「connect」「link」「info」相關 widget、檢視是否 fit LINK 語意

### Phase 3：版本與 release

- [ ] **T6 (P1, human: ~5min)** — `_version.py` bump 到 `2.6.0`
  - `__version__ = "2.6.0"`
  - 跑 `bash build_app.sh` 更新 bundle Info.plist
  - splash 自動跟著顯示

- [ ] **T7 (P2, human: ~15min)** — `docs/changelog/2026-05-23-v2.6.0-light-theme.md`
  - 簡短 release notes，主打「淺色主題、Apple 結構 + Claude 暖度」
  - 附 mockup 截圖（可從 comparison.html screen-capture）

### Phase 4：testing

- [ ] **T8 (P1, human: ~1.5h / CC: ~25min)** — `tests/test_theme.py` 新檔（10 個 test、Eng Review Issue 3 升級到 91% 覆蓋）
  - **原 5 個（core）**：
    - test_palette_keys_consistent_across_themes：每個 theme 有完全一樣的 key 集合（避免 import 漏 key 出 KeyError）
    - test_default_theme_is_dark：sanity check 不亂換預設
    - test_relaunch_helper_chooses_app_bundle_when_bundled：mock sys.executable 含 "WhisperPro.app"
    - test_relaunch_helper_falls_back_to_execv_when_not_bundled：mock 反之
    - test_settings_recording_state_warns_in_confirm：reorganized test_stability stub
  - **新 5 個（Eng Review Issue 1/2/3 補的 gap）**：
    - **test_unknown_theme_falls_back_to_dark**（Issue 2）：mock Config 回 `theme="purple"`、import tokens → `tokens.BG == "#000000"`（dark 的值）+ log 有 warning
    - **test_relaunch_calls_cleanup_before_spawn**（Issue 1）：用 `unittest.mock.call_args_list` 驗證呼叫順序 `on_close → subprocess.Popen → sys.exit`
    - **test_polish_busy_state_warns_in_confirm**：與 recording 平行的 edge case、`_polish_busy=True` 時 confirm dialog 有警告字
    - **test_same_theme_click_no_confirm**：點當前主題的 chip → 不彈 confirm dialog、不 save、不 restart
    - **test_confirm_sequence_save_toast_relaunch_order**：模擬整段流程、驗證 cfg.save 在 toast 之前、toast 在 after(800) 之前、after callback 才呼叫 relaunch

  覆蓋目標：10/11 paths = 91%（剩 1 為 E2E spawn .app 驗證、out of scope）

---

## NOT in scope（明示延後）

- **Live switch（不重啟、即時切換）** ── 延後到 v2.7.0+ 視真實使用需求決定。詳見 TODOS。
- **System theme follow（跟系統明暗自動切換）** ── 多一個「auto」選項看 `ctk.set_appearance_mode("System")` 即可，但需要 macOS 在運行時的 dark mode change notification 處理。一週後評估。
- **個別 widget 自訂色** ── 例如「我想把錄音按鈕改成紫色」這種個人化。沒人要、不做。
- **第三主題（high-contrast / colorblind）** ── 可訪問性後續工作、不在這 PR。
- **動畫過場切換** ── 例如 fade-out 主視窗再 fade-in 新主題版。restart auto-relaunch 已經有 splash 過場、不需要再加。

---

## 既有可重用基礎設施

| 用途 | 既有元件 | 位置 |
|---|---|---|
| 設定持久化 | `Config.save()` 原子性寫入 | config.py |
| Splash 過場 | `SplashScreen` 已存在 | splash.py |
| CustomTkinter dark/light | `ctk.set_appearance_mode("Light"|"Dark")` | 框架內建 |
| .app bundle 啟動 | `subprocess.Popen(["open", ...])` | macOS LaunchServices |
| Toast 通知 | `_show_toast(msg)` | gui.py |
| Config 向前相容 | `valid_data` filter | config.py |

---

## Failure modes

| 路徑 | 失敗場景 | 測試覆蓋 | 錯誤處理 | 使用者感受 |
|---|---|---|---|---|
| `_relaunch_app()` 從 .app | `open -n` 失敗（罕見：bundle 損毀）| T3 mock test | log_error + 不 quit | toast「重啟失敗、請手動」、保留主視窗 |
| `_relaunch_app()` 從 dev | `os.execv` 失敗 | T3 mock test | 同上 | 同上 |
| `tokens.py` 載入 config 失敗 | config.json 損毀 / 沒寫權限 | T1 unit test fallback | except → fallback "dark" | 看到暗色主題、設定面板可重設 |
| Theme key 不存在於 palette dict | KeyError on import | T8 cross-theme test | __setattr__ fallback 為 "" | UI 部分元件變透明、明顯錯誤、log 一秒抓到 |
| 切到 light 之後使用者卻看到部分元件仍是暗色 | 哪個檔案漏 import tokens / 寫死 hex | 手動 visual diff | — | 視覺 bug，要回頭修 |

**無 critical gap**：所有路徑都有 fallback。最壞情況是視覺部分變色、不會崩。

---

## 設計原則對齊

| 原則 | 這個 plan 的對應 |
|---|---|
| **Don't make me think**（Krug）| Settings 第一個 section 是「外觀」、3 秒掃描就找到。Confirm dialog 一行就講清楚要重啟。 |
| **Conventions over innovation** | 用 macOS 慣用的 segmented control（深色 / 淺色）、不發明新 widget。Confirm dialog 用 native modal。 |
| **Visual hierarchy** | 標題 > segmented control > 說明文字三層 hierarchy、每層字級遞減、視覺重量明確。 |
| **Subtraction default**（Maeda）| 沒加「auto」「跟隨系統」「自訂色」這些可加可不加的功能。一個 toggle 解決問題。 |
| **Design for trust**（Gebbia）| Confirm dialog 講清楚「會被中斷的東西」、不騙使用者；relaunch 保留所有設定 / 歷史。 |
| **WCAG AA contrast** | TEXT_1/2/3 全部過 AA；ACCENT 對 UI / Large Text 過 AA；停用態（TEXT_4）合理低對比。 |

---

## TODOS.md 新增

```markdown
## 2026-05-23（v2.6.0 follow-up：live theme switch）

來源：`/plan-design-review` 決議 — 本次主題切換走 Restart 自動重啟（風險最小、~100 行）；
Live switch（不重啟即時切換）需要 ~300 行 + Ambient Chamber 重繪邏輯、unit test 難覆蓋，
延後到使用者抱怨「重啟太出戲」之後再做。

- [ ] **Live theme switch（v2.7.0 候選）**
  - 觸發條件：使用者使用一週後實際抱怨「重啟太突兀」
  - 範疇估計：
    - `tokens.py` 加 `subscribe(callback)` 機制、theme 變更時通知所有訂閱者
    - 所有 widget 在 __init__ 時 subscribe、收到 event 後 reconfigure
    - Ambient Chamber Canvas 重繪邏輯獨立出函式、theme 變更時呼叫
    - Mini HUD palette 重套（PyObjC NSWindow 重 setLevel + bg recolor）
    - Configure event 改 wraplength（既有 TODO）一起做
  - ~300 LOC + 1-2 輪迭代修「漏改的 widget」bug

- [ ] **System theme follow（auto mode）**
  - 加 `cfg.theme = "auto"`、跟 macOS 系統 dark/light 自動切
  - 需要 NSNotification `AppleInterfaceThemeChangedNotification` 監聽
  - 同樣走 restart auto-relaunch（與 v2.6.0 一致）
  - ~50 行追加
```

---

## 預計影響檔案

| 檔案 | 變動 | 行數估計 |
|---|---|---|
| `tokens.py` | theme-aware refactor、加 LIGHT palette、加 LINK | +80 / -0 |
| `config.py` | 加 `theme` 欄位 | +1 |
| `main.py` | `_relaunch_app()` helper + `set_appearance_mode` | +30 |
| `gui.py` | SettingsWindow 加 appearance section + confirm dialog | +120 |
| `_version.py` | bump v2.6.0 | +1 / -1 |
| `tests/test_theme.py` | 新檔 5 個 test | +120 |
| `docs/changelog/2026-05-23-v2.6.0-light-theme.md` | 新檔 release notes | +60 |
| `TODOS.md` | 加 follow-up section | +20 |
| `build_app.sh` | 不動（已從 `_version.py` 讀）| 0 |

**總計**：~430 行新增 / ~1 行修改、9 個檔案

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | 範圍清楚、不需要 |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | 出 PR 前可選擇跑（outside voice 在 eng review 也被跳過） |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR | 3 issues 全部解決：(1) Restart cleanup-first 順序 (2) 未知 theme 值一行 fallback (3) Test 覆蓋從 45% → 91%、加 5 個 gap test |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 | CLEAR | score: 5/10 → 9/10、變更 4 項（palette / UX / settings 結構 / edge cases）、Variant C + Restart 拍板 |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | 非 dev-facing |

**UNRESOLVED:** 0
**VERDICT:** DESIGN + ENG CLEARED — ready to implement v2.6.0. 開分支 `fix/light-theme-v260`、依 T1-T8 順序實作（T1/T2/T3 可並行；T4 依賴 T2/T3；T5 獨立；T6/T7 收尾；T8 邊做邊補）。
