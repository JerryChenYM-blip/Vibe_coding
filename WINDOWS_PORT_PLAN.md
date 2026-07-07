# Whisper Pro — Windows 移植盤點報告

> 產出日期：2026-07-07
> 範圍：`/Users/jerrychen/project/WhisperPro_Windows` 全部 28 個專案 `.py` 檔（不含 `venv/`）+ `tests/` 5 個測試檔
> 方式：只讀盤點，未改動任何程式碼
> 對照母專案：`/Users/jerrychen/project/Claude_code`（macOS 正式版，本專案是它的完整複本）

---

## 0. 總覽：這台 App 有多「Mac」

先講結論：**這個 App 的「錄音 → 轉錄 → 貼上」核心流程，有 3 個地方是硬綁 macOS 的**，其他都只是「順手用了 Mac API 讓體驗更好，但已經有備援方案」。用蓋房子來比喻：

- **地基級（不換就完全跑不起來）**：全域熱鍵監聽（NSEvent）、自動貼上（osascript + ⌘V 模擬）、語音辨識 GPU 後端（mlx，Apple 專屬晶片指令）
- **裝潢級（不換能跑，但功能會消失或變陽春）**：麥克風熱插拔監聽（CoreAudio）、浮動 HUD 視窗置頂效果、App Nap 抑制、單一實例互斥鎖
- **標籤級（幾乎不用管，Windows 上自然正確或已有 fallback）**：所有檔案路徑（`Path.home()`）、Ollama 診斷、App Icon 產生器、reduce-motion 偵測

好消息：**這份 code 的作者已經有很強的「防呆」意識**——幾乎每個 macOS-only API 呼叫外面都包了 try/except + 平台判斷 + fallback，不是那種「一用 AppKit 就直接 import error 炸整個 App」的寫法。這代表移植主要工作是「**把 fallback 從『陽春版』升級成『Windows 對應的正牌實作』**」，而不是「拆掉重寫」。

---

## 1. Cluster：hotkey（全域快捷鍵）

### 檔案
- `hotkey_manager.py`（核心，835 行）
- `gui.py`（`HotkeyBindDialog` 重綁對話框、`_hotkey_watchdog`）
- `main.py`（`is_pynput_available` / `check_accessibility` 呼叫）
- `config.py`（預設熱鍵 `right_cmd`）

### 現況技術
主監聽器＝ **PyObjC `NSEvent.addGlobalMonitorForEventsMatchingMask_handler_` / `addLocalMonitorForEventsMatchingMask_handler_`**（2026-05-22 從 pynput 換過來，理由是 macOS 26.4+ 一堆 TSM/PAC crash）。pynput 仍保留，但只用來：
1. 提供 `Key.cmd_r` 這類物件當「內部識別符」（不是真的監聽）
2. `auto_paste.py` 送 ⌘V

### mac_specific 逐點清單

| # | 檔案:行為 | 說明 | Windows 替代 |
|---|---|---|---|
| 1 | `hotkey_manager.py:54-66` `from AppKit import NSEvent, NSEventMaskFlagsChanged...` | 全域鍵盤監聽核心 API，Windows 沒有 AppKit | 改用 **pynput.keyboard.Listener**（Windows 上走 `SetWindowsHookEx` 底層鉤子，穩定成熟，且不會有 macOS 那些 TSM/PAC 問題——那些是 macOS 專屬的安全機制）。pynput 在 Windows 上**不需要**額外系統權限（macOS 需要「輔助使用」授權，Windows 沒有這層） |
| 2 | `hotkey_manager.py:80-89` `_KEYCODE_TO_SIDED_MOD`（macOS HIToolbox 虛擬鍵碼表：54=cmd_r、55=cmd_l...） | macOS 專屬鍵碼編碼（來源 `HIToolbox/Events.h`） | Windows 用 pynput 原生就會回 `Key.cmd_r`/`Key.ctrl_r` 等側別感知的 Key 物件（pynput 底層已經處理好 Windows scan code 對應），**不需要**自己刻鍵碼表 |
| 3 | `hotkey_manager.py:151-163` `check_accessibility()` 呼叫 `ApplicationServices.AXIsProcessTrusted()` | 檢查「輔助使用」權限（macOS 專屬概念） | Windows 沒有對應概念，直接回傳 `True`（Windows 全域鍵盤鉤子預設就有權限，不需要額外使用者授權這一關）|
| 4 | `hotkey_manager.py:494-540` global/local monitor 雙軌設計（其他 App 焦點收 global、本 App 焦點收 local） | Cocoa 特有的「兩種 monitor」設計 | pynput Listener 是單一 callback、天生就能收到「不管哪個 App 有焦點」的全域按鍵事件，**不需要**分 global/local 兩軌，邏輯直接簡化 |
| 5 | `hotkey_manager.py:562-576` FlagsChanged 事件用 `_ns_held_modifiers: set[int]` state machine 判斷 press/release（因為左右 Cmd 共用同一個 bit） | 這是 **macOS NSEvent 特有的 bug**（`NSEventModifierFlagCommand` 不分左右） | pynput 在 Windows 上**天生分開回報** `Key.ctrl_l`/`Key.ctrl_r` 等，不會有「兩顆鍵共用一個 bit」問題，這段 workaround 邏輯在 Windows 版可以整段不需要（但如果共用 core 邏輯處理 press/release 判定，建議仍保留 defensive 寫法，成本低） |
| 6 | `config.py:71` 預設熱鍵 `"right_cmd"`（單按右 Cmd） | Windows 鍵盤沒有 Cmd 鍵 | 預設值需改成 Windows 對應鍵，建議 **`right_ctrl`**（右 Ctrl，多數 Windows 鍵盤都有且很少被系統占用）或 **`right_alt`**（注意：Windows 上 Right Alt 常被對應成 `AltGr`，用於歐洲語言鍵盤輸入特殊字元，可能衝突，不建議當預設）。`right_ctrl` 較安全 |
| 7 | `hotkey_manager.py:268-291` `format_hotkey()` 顯示符號 `⌘⌥⌃⇧`（Mac 鍵盤符號） | UI 顯示用的符號，Windows 使用者看不懂 ⌘/⌥ | 改成 Windows 慣用顯示：`Ctrl`/`Alt`/`Shift`/`Win` 純文字（Windows 沒有統一的鍵盤符號字型可用，業界慣例都是打全名或縮寫） |
| 8 | `gui.py` `HotkeyBindDialog._keysym_to_name()` Tk keysym 映射含 `meta_l/meta_r`（X11/Linux 遺留）、`super_l/super_r` | 這段其實**已經是跨平台 fallback**（註解寫「非 Mac 備援」），Tk 在 Windows 上會回 `Control_L`/`Alt_L` 等 keysym，現有映射表**已經涵蓋** | 不太需要改，只需確認 Windows Tk 的實際 keysym 字串是否吻合（建議跑一次實機測試，Tk keysym 在不同 OS 偶有命名差異，例如 Windows 有時是 `Alt_L`/`Alt_R` 而非 `option_l`） |

### 難度：**中（medium）**
- 核心邏輯（tap-toggle 語意、armed/fire 狀態機、self-heal 逾時保護）都是純 Python，可以整段保留，只需要把「取得按鍵事件」的來源從 NSEvent 換成 pynput Listener。
- 真正的坑在「side-effect 對齊」：pynput 在 Windows 上的事件時序、修飾鍵回報方式跟 NSEvent 不完全一樣，需要重新測試 lone-modifier 模式（單按一顆鍵觸發）在 Windows 上是否一樣可靠——Windows 全域鉤子的延遲特性跟 macOS 不同。
- 建議：**保留 `_backend` 欄位機制**，做成 `"nsevent"`（macOS）/`"pynput_win"`（Windows）雙後端，用 `platform.system()` 切換，而不是整個檔案重寫。

---

## 2. Cluster：paste + frontmost（自動貼上 + 前景 App 偵測）

### 檔案
- `auto_paste.py`（全檔，263 行）
- `gui.py`（呼叫端：`_do_auto_paste` 主執行緒呼叫規則）

### 現況技術
1. `get_frontmost_app()`：優先 `AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()`，失敗 fallback `osascript` 問 System Events
2. `paste_to_app()`：`pyperclip.copy()` 寫剪貼簿 → `NSWorkspace.activateWithOptions_`（或 osascript fallback）把目標 App 拉前景 → poll 前景確認切換完成 → `pynput.keyboard.Controller` 模擬 `⌘V`
3. `_is_app_fullscreen()`：osascript 問 `AXFullScreen` 屬性，判斷是否要用更長的等待時間（全螢幕 Space 切換較慢）

### mac_specific 逐點清單

| # | 檔案:行為 | 說明 | Windows 替代 |
|---|---|---|---|
| 1 | `auto_paste.py:40-49` `NSWorkspace.sharedWorkspace().frontmostApplication()` | 取得前景 App 名稱（Cocoa 原生，~100x 比 osascript 快） | Windows 用 **`ctypes.windll.user32.GetForegroundWindow()`** 拿到視窗 handle，再用 `GetWindowThreadProcessId()` 拿 PID，最後用 `psutil.Process(pid).name()`（`psutil` 已經是本專案依賴，不用新增）取得程式名稱 |
| 2 | `auto_paste.py:56-79` osascript fallback（`tell application "System Events" to get name of first process whose frontmost is true`） | AppleScript 專屬語法 | 同上，直接用 `GetForegroundWindow` 沒有 fallback 必要——Windows 沒有「兩套機制擇一」的問題，win32 API 本身就很穩定，不需要複雜的 fallback 鏈 |
| 3 | `auto_paste.py:149-160` `NSWorkspace.runningApplications()` + `activateWithOptions_(NSApplicationActivateIgnoringOtherApps)` | 把目標 App 拉到前景 | Windows 用 **`ctypes.windll.user32.SetForegroundWindow(hwnd)`**。注意：Windows 有安全限制——**目前非前景的程式呼叫 `SetForegroundWindow` 常會被系統拒絕**（Windows 的 focus-stealing 防護），常見繞法是搭配 `AttachThreadInput` 先「借」前景程式的輸入焦點權限，或用 `keybd_event` 模擬一次無害按鍵再呼叫。這是移植中**最容易低估的坑**，建議先做 POC 驗證 |
| 4 | `auto_paste.py:164-181` osascript activate fallback（`tell application "{app}" to activate`） | AppleScript activate 語法 | 同上用 `SetForegroundWindow`，同樣有 focus-stealing 限制需處理，沒有天然 fallback 捷徑 |
| 5 | `auto_paste.py:187-201` poll frontmost 確認切換完成（等 macOS Space 切換動畫 ~0.5s） | macOS 全螢幕 App 在獨立 Space、切換有動畫延遲 | Windows 沒有「Space」概念（虛擬桌面存在但通常不像 macOS 全螢幕那樣強制隔離），這段 poll 邏輯可大幅簡化或整段拿掉；`SetForegroundWindow` 在 Windows 上通常是同步生效，不太需要 poll-wait |
| 6 | `auto_paste.py:206-224` `from pynput.keyboard import Controller, Key` + `kb.pressed(Key.cmd): kb.tap("v")` | 模擬 `⌘V` | 改成 `kb.pressed(Key.ctrl): kb.tap("v")`（模擬 **Ctrl+V**）。pynput 在 Windows 上模擬按鍵**沒有 macOS 那種「必須在主執行緒呼叫，否則 TSM 斷言 crash」的限制**——這是 CLAUDE.md 坑點 #6 提到的 macOS 專屬限制，Windows 上可以放心在背景執行緒呼叫，不必强制主執行緒（但為了程式碼一致性，建議還是留在主執行緒呼叫，成本很低） |
| 7 | `auto_paste.py:238-262` `_is_app_fullscreen()` 整個函式：osascript 問 `AXFullScreen` | macOS 專屬全螢幕/Space 偵測 | Windows 沒有直接對應概念。若要模擬「這個視窗是不是全螢幕」，可用 `GetWindowRect` 比對視窗大小是否等於螢幕大小；但因為 Windows 沒有 Space 切換延遲問題（見#5），這個函式在 Windows 版**可以整個不需要**，直接用固定的短延遲（如 0.1-0.2s）即可 |
| 8 | 全域：`⌘V` 快捷鍵符號在 log/註解中出現 | 純文字/log，不影響功能 | 無需修改邏輯，僅供人類讀者參考；若要 Windows 版 log 更好讀可置換為 `Ctrl+V`，非必要 |

### 難度：**中偏難（medium-hard）**
- 前景 App 偵測（`get_frontmost_app`）：簡單，`ctypes.windll.user32` 這條路很成熟。
- 貼上模擬（`⌘V` → `Ctrl+V`）：簡單，pynput 換一個 Key 常數即可。
- **真正難的是「把目標 App 拉到前景」這件事在 Windows 上有安全限制**（focus-stealing prevention）。這不是 code 邏輯問題，是 Windows OS 級的策略：如果 Whisper Pro（背景程式）想強制把使用者正在用的另一個視窗拉到最前面，Windows 預設會擋掉，除非用一些已知繞法（`AttachThreadInput`、模擬 Alt 按鍵、或用 `SetWindowPos` + `HWND_TOPMOST` 先頂再降）。**這個坑值得抓一個獨立的 30 分鐘 POC 驗證**，因為如果繞不過，整個「自動貼上到游標處」的體驗會打折扣（使用者可能要手動點一下目標視窗才能貼上成功）。

---

## 3. Cluster：audio（錄音 / 麥克風熱插拔）

### 檔案
- `recorder.py`（全檔，512 行）

### 現況技術
1. 核心錄音：`sounddevice.InputStream`（PortAudio 綁定）— **這部分本來就跨平台**，Windows 上 sounddevice 走 WASAPI/MME/DirectSound 後端，不需要改
2. Homebrew libportaudio 自動載入（`_load_portaudio()`）— macOS 專屬修復手法
3. 麥克風熱插拔偵測：**直接 ctypes 呼叫 `CoreAudio.framework`**，註冊 `AudioObjectAddPropertyListener` 監聽裝置變動（v2.21 新增，2026-07 母專案剛修的真 bug）

### mac_specific 逐點清單

| # | 檔案:行為 | 說明 | Windows 替代 |
|---|---|---|---|
| 1 | `recorder.py:29-58` `_load_portaudio()`：`ctypes.cdll.LoadLibrary` 找 Homebrew 的 `libportaudio.2.dylib` | macOS 專屬的動態庫載入修復（解決 DYLD 路徑找不到問題） | Windows 上 `sounddevice` 的官方 wheel（PyPI 上的 `sounddevice`）**已經內建打包 PortAudio DLL**，不需要這段修復邏輯，可整段跳過（用 `platform.system() == "Darwin"` 包起來，Windows 直接不執行） |
| 2 | `recorder.py:358-447` `start_device_monitor()`：`ctypes.util.find_library("CoreAudio")` + `AudioObjectAddPropertyListener` | **完全 macOS 專屬**，CoreAudio.framework 是 Apple 私有框架，Windows 沒有對應物 | 改用 **輪詢法（polling）**：background thread 每隔 N 秒（建議 2-3 秒）呼叫 `sounddevice.query_devices()` 比對裝置清單是否變化，變了就觸發 `on_change` callback。已有 `refresh_portaudio()`（見下一點）可以重用，只是從「事件驅動」降級成「輪詢驅動」——使用者體感差異是「拔麥克風後最多等 2-3 秒才會被偵測到」而非「立即感知」，可接受 |
| 3 | `recorder.py:321-342` `refresh_portaudio()`：`sd._terminate()` + `sd._initialize()` 重新整理裝置快照 | 這段**不是 macOS 專屬**，PortAudio 在 Windows 上一樣有「裝置清單快照不會自動更新」的問題，這段邏輯**可以照抄不用改** | 無需改動，Windows 版一樣呼叫這個方法來刷新裝置清單 |
| 4 | `recorder.py:449-469` `stop_device_monitor()`：`AudioObjectRemovePropertyListener` | 同 #2，CoreAudio 專屬 | 若改用輪詢法，這裡改成「停止背景輪詢執行緒」（`threading.Event` 通知 thread 結束即可，不需要對應 remove listener 的動作） |

### 難度：**簡單（easy）**
- 錄音主體（`sounddevice.InputStream`、RMS 計算、buffer 管理、generation counter 防 stale callback）都是純 Python + numpy，**完全不用改**，這是本次移植裡最輕鬆的一塊。
- 唯一要動的是熱插拔監聽：從「CoreAudio 事件」換成「輪詢比對裝置清單」，邏輯簡單（`set(old_devices) != set(new_devices)` 級別的比對），且介面（`start_device_monitor(on_change)` / `stop_device_monitor()`）可以完全維持不變，呼叫端（`gui.py`）不需要跟著改。

---

## 4. Cluster：asr（語音辨識後端）

### 檔案
- `transcriber.py`（核心，2000+ 行）
- `requirements.txt`（依賴宣告）

### 現況技術
啟動時偵測一次 `BACKEND`：
- `platform.machine() == "arm64"` 且 `mlx_whisper` 可 import → `"mlx"`（用 Apple Silicon 的 Metal GPU + Neural Engine 加速）
- 否則 → `"ctranslate"`（`faster-whisper`，CPU int8 量化推論，**這部分本來就跨平台**）

另外 v2.14.0 加了 **Qwen3-ASR**（`mlx_qwen3_asr` 套件，中文辨識更準），但**這個模型只有 MLX 實作、無 CPU fallback**——程式碼已經自己擋：`if BACKEND != "mlx": return「需要 Apple Silicon MLX」錯誤訊息`。

### mac_specific 逐點清單

| # | 檔案:行為 | 說明 | Windows 替代 |
|---|---|---|---|
| 1 | `transcriber.py:94-106` `_detect_backend()`：`platform.machine() != "arm64"` 判斷 | `mlx` / `mlx_whisper` 是 **Apple 專屬**（依賴 Apple Silicon 的 Metal GPU API 與統一記憶體架構），在 Windows（不管 Intel/AMD CPU 或 NVIDIA GPU）**完全無法安裝、無法執行** | 邏輯本身**不用改**——`platform.machine() != "arm64"` 這個判斷在 Windows x86_64 上天生就會回傳 `"ctranslate"`，等於**自動正確 gate 掉 mlx**。真正要做的是**把 `ctranslate`（faster-whisper）這條路的效能顧好**，因為 Windows 上這會變成唯一後端，而非 fallback |
| 2 | `transcriber.py:157-170` Qwen3-ASR 整合區塊（`mlx_qwen3_asr`），純 MLX-only、無 CPU fallback | 同上，Apple 專屬套件 | Windows 版**直接跳過**，`_is_qwen3_model(model_size)` 判斷為真但 `BACKEND != "mlx"` 時已有現成錯誤處理路徑（`transcriber.py:955-977`：回傳「需要 Apple Silicon MLX、請於設定切回 large-v3-turbo」訊息），**這段防呆已經寫好了，不用新增**，只需確保 UI 上（`gui.py` 設定選單）Windows 版預設**不要讓使用者看到/選到** Qwen3-ASR 選項（否則使用者選了才發現不能用，體驗不好，屬 gui cluster 的小改動） |
| 3 | `requirements.txt` `mlx==0.31.1` / `mlx-whisper==0.4.3` / `mlx-qwen3-asr==0.3.5` | pip 套件本身在 Windows 上**沒有對應 wheel**，`pip install` 會直接失敗 | 建立 **`requirements-windows.txt`**（或用 `pip` 的 environment marker，如 `mlx==0.31.1; sys_platform == "darwin" and platform_machine == "arm64"`），Windows 版安�装腳本只裝 `faster-whisper` + 其餘共用套件，不嘗試裝 mlx 系列 |
| 4 | `transcriber.py` GPU 加速真空 | mlx 沒了之後，Windows 版**沒有對應的 GPU 加速路徑**（`faster-whisper`/CTranslate2 雖然支援 CUDA，但目前程式碼裡 `_transcribe_ctranslate` 走的是 **int8 CPU** 模式，沒有寫 CUDA 路徑） | 這是**效能規劃**問題不是相容性問題：短期用 CPU int8（faster-whisper 對 CPU 優化很成熟，`large-v3-turbo` 在現代多核 CPU 上通常可接受）。中期若要接近 macOS 的 GPU 加速體驗，可評估幫 `faster-whisper`／CTranslate2 開 CUDA device（需要 NVIDIA GPU + CUDA toolkit，屬於「錦上添花」而非「移植必要」，建議列為 Phase 2 才做，先求「能動、體驗可接受」） |

### 難度：**簡單（easy）— 但有效能顧慮**
- 相容性層面完全不用改：程式碼已經用 `platform.machine()` 正確 gate，Windows 上會自動落到 `faster-whisper` 這條**本來就跨平台**的路。
- 真正的工作量在「**確保 Windows 上 CTranslate2 CPU 推論速度可接受**」（母專案在 Apple Silicon 上用 GPU，體感應該比純 CPU 快不少），以及「**gui.py 設定選單要把 Apple-only 選項（Qwen3-ASR、mlx 相關）在 Windows 版隱藏或標示不可用**」——這個小 UI 調整屬於 cluster 5（gui+misc）的工作範圍，這裡先標記出來避免漏掉。

---

## 5. Cluster：gui + misc（HUD 視窗、App Nap、pidfile、打包）

### 檔案
- `gui.py`（`MiniRecordingWindow`、reduce-motion、Cocoa observer 相關、`_on_dock_reopen`）
- `main.py`（App Nap 抑制、single-instance lockfile、`_relaunch_app`）
- `tokens.py`（字型）
- `build_app.sh` / `launch_app.sh` / `WhisperPro.spec`（打包）
- `app_icon.py`（圖示產生，已驗證有 fallback）
- `onboarding.py`（Ollama 診斷建議命令文字）

### mac_specific 逐點清單

| # | 檔案:行為 | 說明 | Windows 替代 |
|---|---|---|---|
| 1 | `gui.py:6740-6802` `MiniRecordingWindow._upgrade_to_panel_level()`：`AppKit.NSApp.windows()` 比對 title 找 NSWindow，`setLevel_(NSStatusWindowLevel)` + `setCollectionBehavior_` 達成「跨 Space / 全螢幕仍可見」 | Cocoa 專屬視窗層級 API | **已有現成 fallback**：`_fallback_topmost()` 用 Tk 原生 `self.attributes("-topmost", True)`，註解明寫「Tk `-topmost` 跨平台、無 PyObjC 依賴」。Windows 版**直接讓這條路徑生效即可**（用 `platform.system() != "Darwin"` 直接跳過 NSWindow 那段、走 topmost），效果是「同 Space 最頂」（Windows 沒有 Space 概念，topmost 基本等同 macOS 全功能版的體驗，甚至更好） |
| 2 | `gui.py:6682-6726` 多螢幕座標換算（`NSScreen.frame` 左下原點 → Tk 左上原點） | Cocoa 座標系轉換，Windows 沒有這個 API | Windows 多螢幕用 Tk 原生 `winfo_screenwidth()`/`winfo_screenheight()` 通常只能拿到主螢幕尺寸；要抓「游標所在螢幕」需要 **`ctypes.windll.user32.MonitorFromPoint` + `GetMonitorInfo`**。這段功能是「錦上添花」（多螢幕時 HUD 出現在游標那台螢幕），Windows 版可以先簡化成「永遠顯示在主螢幕中下方」（成本低很多），多螢幕跟隨游標列為 Phase 2 |
| 3 | `gui.py:3792-3920` Cocoa `NSApplicationDidBecomeActiveNotification` / `NSAppleEventManager`（處理 Dock 圖示點擊恢復視窗） | 這整套是為了修「Dock 點圖示視窗不回前景」的 macOS 專屬 bug（`_on_dock_reopen`） | Windows 沒有 Dock，這個機制**完全不需要**。Windows 版任務列（taskbar）點圖示的行為由 Windows 視窗管理員原生處理，Tk 的 `deiconify()` + `lift()` + `focus_force()` 已經足夠，這一大段 Cocoa observer 程式碼在 Windows 版可以整段跳過（用 platform gate） |
| 4 | `gui.py:162-175` `system_reduce_motion()`：`defaults read com.apple.universalaccess reduceMotion` | macOS 專屬系統偏好設定讀取指令 | 已經是安全的（`subprocess.run` 在 Windows 上找不到 `defaults` 指令會丟 `FileNotFoundError`，被 `except Exception: return False` 接住，不會 crash，只是永遠回報「不需要減少動態效果」）。若要做 Windows 對應版本：Windows 10+ 的「透明效果/動畫」設定在**登錄檔（Registry）** `HKEY_CURRENT_USER\Control Panel\Desktop\WindowMetrics` 或 `SPI_GETCLIENTAREAANIMATION`（`ctypes.windll.user32.SystemParametersInfoW`），屬「錦上添花」，非必要 |
| 5 | `tokens.py:199-201` `FONT_FAMILY_UI = "SF Pro Display"` / `FONT_FAMILY_TEXT = "SF Pro Text"` / `FONT_FAMILY_MONO = "SF Mono"` | Apple 系統字型，Windows 沒有安裝 | Windows 上 Tk 找不到字型會**靜默 fallback 成系統預設字型**（不會 crash，但排版會跟設計稿有落差）。建議 Windows 版對應：UI/內文字型用 **`"Segoe UI"`**（Windows 10/11 系統標準字型），等寬數字字型用 **`"Consolas"`**（Windows 內建、微軟自家等寬字型，效果類似 SF Mono）。建議在 `tokens.py` 用 `platform.system()` 判斷切換，而非寫死 |
| 6 | `main.py:202-235` `_disable_app_nap()`：`Foundation.NSProcessInfo.beginActivityWithOptions_reason_` 抑制 macOS「App Nap」省電機制 | macOS 專屬省電機制，目的是避免熱鍵監聽在背景被降頻導致失靈 | Windows 沒有 App Nap 這個機制（Windows 的電源管理不會這樣針對背景 GUI 程式降低事件迴圈頻率），這段函式**已經有 `if sys.platform != "darwin": return` 守門**，Windows 上會直接跳過、不需要對應實作 |
| 7 | `main.py:238-338` single-instance lockfile：`os.kill(pid, 0)` 探活、`os.kill(pid, signal.SIGTERM)` / `SIGKILL` | `os.kill` 在 Windows 上**行為不同**：Windows 沒有 POSIX signal 概念，`os.kill(pid, signal.SIGTERM)` 在 Windows Python 上會**直接強制終止程序**（等同 `TerminateProcess`，不是優雅關閉），且 `signal.SIGKILL` 在 Windows 上**不存在**（`AttributeError`） | 改用 **`psutil`**（已是專案依賴）：`psutil.pid_exists(pid)` 取代 `_is_pid_alive`；`psutil.Process(pid).terminate()` 取代 SIGTERM、`psutil.Process(pid).kill()` 取代 SIGKILL，`psutil` 內部會依平台自動轉成正確的 API 呼叫（Windows 用 `TerminateProcess`），程式邏輯結構完全不用大改，只是換底層呼叫 |
| 8 | `main.py:341-400` `_relaunch_app()`：`subprocess.Popen(["open", "-n", str(_APP_BUNDLE)])` 重啟 `.app` bundle；fallback `os.execv` | `open` 是 macOS 專屬指令（開啟 Finder 項目） | Windows 版用 **`os.startfile(exe_path)`**（Windows 專屬、直接雙擊等效）或 `subprocess.Popen([sys.executable] + sys.argv)`。`os.execv` fallback 路徑**本身跨平台**，Windows 上可以照樣運作（`os.execv` 是標準庫函式，Windows Python 有支援，雖然行為細節與 Unix 略有差異，但基本替換 process image 的語意成立） |
| 9 | `main.py:180` `_APP_BUNDLE = Path.home() / "Applications" / "WhisperPro.app"` | macOS 應用程式安裝慣例路徑（`~/Applications/`） | Windows 沒有 `~/Applications/` 慣例，改用類似 **`%LOCALAPPDATA%\WhisperPro\WhisperPro.exe`**（`os.environ["LOCALAPPDATA"]`）或直接用 PyInstaller 產生的 exe 所在目錄（`sys.executable` 判斷是否為 frozen 狀態：`getattr(sys, "frozen", False)`） |
| 10 | `build_app.sh` 全檔（250 行）：`.app` bundle 結構、`plutil` 改 Info.plist、`codesign --sign -` adhoc 簽章、`tccutil reset` | **完全 macOS 專屬**（bundle 格式、TCC 權限機制、程式碼簽章工具都是 Apple 概念） | 改用 **PyInstaller**（`WhisperPro.spec` 已經有一份 spec 檔存在，但目前是「macOS `.app` BUNDLE」設定，需要新增一份 **Windows 專用 spec**：拿掉 `BUNDLE()` 區塊，改成純 `EXE()` + 選配 `--onefile` 或 `--onedir`）。Windows 沒有 TCC 授權機制，不需要 `tccutil` 對應物；Windows 版若要「發布可信任」則需要**程式碼簽章憑證**（付費，屬 Phase 2），未簽章的 exe 在 Windows SmartScreen 會跳警告，但不影響功能運作 |
| 11 | `launch_app.sh`：`venv/bin/python3 main.py` | Unix shell script + venv 路徑慣例（`venv/bin/`） | Windows 對應 `venv\Scripts\python.exe`（venv 目錄結構不同：Windows 是 `Scripts/` 不是 `bin/`）。建議寫一份 `launch_app.bat` 或 `launch_app.ps1` |
| 12 | `WhisperPro.spec` `target_arch='arm64'`、`bundle_identifier`、`BUNDLE()` 整段 | PyInstaller 的 macOS-only 打包指令（`BUNDLE` 是 macOS `.app` 專屬 step，`target_arch` 也是 macOS 專屬參數） | 需要**獨立一份 Windows spec**，`EXE()` 部分可保留大部分設定（`hiddenimports` 清單需要拿掉 `pynput.keyboard._darwin` / `pynput.mouse._darwin`，換成 `pynput.keyboard._win32` / `pynput.mouse._win32`） |
| 13 | `app_icon.py:239-243` `iconutil` 產 `.icns`（僅 macOS） | 已經有 fallback：`shutil.which` 類邏輯偵測不到 `iconutil` 時印「PNG only (no .icns)」並回傳 `False`，**不會 crash** | Windows 版需要 `.ico` 格式（不是 `.icns`）。可用 **Pillow 的 `Image.save(..., format="ICO")`**（Pillow 原生支援多尺寸 ICO 輸出），需要新增一小段 Windows 專用的 icon 產生邏輯，難度低 |
| 14 | `onboarding.py:121-124` 建議命令文字 `"brew install ollama && brew services start ollama"` | Homebrew 是 macOS 專屬套件管理工具，文字內容不適用 Windows 使用者 | 改成 Windows 對應：Ollama 官方有 Windows 安裝程式（`https://ollama.com/download/windows`），建議命令文字改成引導使用者去下載頁面，或若 Ollama Windows 版有 CLI 安裝指令則對應替換。純文字修改，難度極低 |

### 難度：**中（medium）**（拆細看：大部分項目 easy，但打包 + focus/HUD 這塊有幾個 medium）
- **多數項目已有 fallback，只需要「啟用它」**（topmost、iconutil 缺失處理），這類是 easy。
- **`os.kill` / SIGTERM/SIGKILL 換 psutil** 是 easy 但**必須改**，否則 Windows 上單一實例鎖機制會直接因為 `AttributeError: SIGKILL` 而整個 crash（這是本次盤點中少數「不改就會直接壞掉」而非「不改只是體驗打折」的項目，優先度應提高）。
- **打包腳本（`build_app.sh` → Windows 版 + PyInstaller spec）** 是 medium，因為要重新設計「Windows 上要不要單一實例、要不要簽章、圖示轉檔」整套發布流程，工作量不小但技術上不困難。
- **字型系統改成 platform-aware** 是 easy，改完後排版需要肉眼驗證（Segoe UI 跟 SF Pro 的字寬字高不完全一樣，可能要微調間距）。

---

## 6. 跨 Cluster 共通建議：Windows Port Strategy

給整體移植定調用的建議策略（見下方 `port_strategy` 欄位摘要，這裡展開說明）：

### 6.1 平台判斷統一寫法
建議新增一個小工具模組（例如 `platform_compat.py`），把所有 `platform.system() == "Darwin"` / `sys.platform != "darwin"` 判斷集中管理，並提供統一入口：

```python
IS_MACOS   = platform.system() == "Darwin"
IS_WINDOWS = platform.system() == "Windows"
```

目前程式碼裡這類判斷分散在 `hotkey_manager.py` / `main.py` / `gui.py` 三處寫法不完全一致（有的用 `platform.system() != "Darwin"`，有的用 `sys.platform != "darwin"`），統一後好維護、好測試。

### 6.2 後端注入模式（Backend Injection）
`transcriber.py` 的 `BACKEND` 全域變數 + `_detect_backend()` 這個模式已經證明好用（一次偵測、全 session 共用），建議 `hotkey_manager.py` 和 `recorder.py` 的熱插拔監聽都比照辦理：啟動時偵測一次「這台機器該用哪個後端」，之後全程用同一份介面呼叫，讓 `gui.py` 呼叫端完全不用知道底層是 NSEvent 還是 pynput、是 CoreAudio 事件還是輪詢。

### 6.3 優先順序建議（如果要分階段做）
1. **Phase 1（能動）**：hotkey（pynput 版）+ paste（win32 API 版，含 focus-steal POC）+ asr（確認 faster-whisper CPU 路徑順暢）+ psutil 换 os.kill（防止直接 crash）
2. **Phase 2（體驗打磨）**：audio 熱插拔輪詢化、字型 platform-aware、HUD topmost 驗證多螢幕
3. **Phase 3（發布品質）**：PyInstaller Windows spec、.ico 產生、程式碼簽章評估、Windows 安裝程式（如 Inno Setup）

### 6.4 測試策略提醒
`tests/test_stability.py` 目前大量測試案例直接 `from AppKit import ...` 或用 `sys.platform == "darwin"` 分支（例如 line 241-365），這些測試**在 Windows CI 上會需要對應的 mock 或 skip 邏輯**，移植時建議同步盤點測試檔案是否需要新增 Windows 對應測試（例如把 `_patch_for_d5s10` 這類「假裝 AppKit 不存在」的測試模式，反過來做「假裝在 Windows 上」的測試）。這部分本次盤點只讀不改，留給實作階段處理。

---

## 7. 附錄：本次盤點方法

- `grep -rl` 全 repo 掃描以下關鍵字：`AppKit|PyObjC|NSEvent|NSWorkspace|NSWindow`、`osascript`、`mlx|qwen3_asr`、`pynput`、`tccutil|輔助使用|Accessibility`、`faulthandler|App Nap`、`cmd|Cmd|⌘`、`~/.whisper_app|~/Applications|expanduser|Path.home()`
- 對每個命中檔案逐行 Read 確認上下文，排除純註解提及（不影響功能）與已經有 fallback 的假警報
- 交叉比對母專案 `CLAUDE.md`（`/Users/jerrychen/project/Claude_code/CLAUDE.md`）第 9 節「開發流程重點」列出的 17 個「常見坑」，確認哪些坑在 Windows 上不存在（例如坑 #4 pynput 輔助使用權限、坑 #6 TSM 主執行緒限制、坑 #11 TCC 權限歸帳都是 macOS 專屬安全機制，Windows 沒有對應物）

---

**報告結束。** 本檔案為只讀盤點，未修改任何 `.py` 原始碼。

---

## 8. Windows 實機測試清單

> 補充於 2026-07-07，配合 `requirements-windows.txt` / `requirements-mac.txt`
> 拆檔與 `build_windows.md` 打包指南新增。這份清單給**第一次在真實 Windows
> 機器上跑起來**的人用——本次盤點與拆檔全程在 Mac 上完成、未曾在 Windows
> 實機驗證，以下項目全部待實測，不能只憑程式碼推論就當作「已驗證」。

### 8.1 環境安裝

- [ ] Windows 10 或 11（實際跑一次，記錄版本號）
- [ ] Python 3.13（或退到 3.11/3.12，視 PyInstaller 相容性而定）能正常安裝
- [ ] `pip install -r requirements-windows.txt` 全部套件安裝成功，無版本衝突
- [ ] ffmpeg 已加入系統 PATH，`ffmpeg -version` 在 cmd/PowerShell 能正常執行
- [ ] `python main.py`（未打包、直接跑原始碼）能啟動 GUI，無 import error

### 8.2 全域熱鍵（hotkey cluster）

- [ ] 預設熱鍵（`ctrl+alt+r`，見 `platform_util.DEFAULT_HOTKEY`）能正常觸發錄音開始/停止
- [ ] pynput Listener 在 Windows 上**不需要**額外系統權限跳出授權視窗（對照 macOS
      的「輔助使用」授權流程，Windows 應該是裝完就能用）
- [ ] 熱鍵重新綁定對話框（`HotkeyBindDialog`）能正確擷取 Windows 按鍵組合
      （驗證 Tk keysym 在 Windows 上的實際字串，例如 `Alt_L`/`Control_R`）
- [ ] 長時間閒置（30 分鐘以上）後熱鍵仍然有效，不會像 macOS 舊版 pynput 那樣被
      系統背景機制「靜默弄死」
- [ ] 左右側 modifier 鍵（左右 Ctrl / Alt / Shift）分別測試，確認 pynput 在
      Windows 上原生分開回報（不會有 macOS NSEvent 那種「共用同一個 bit」問題）

### 8.3 錄音與麥克風（audio cluster）

- [ ] 錄音能正確擷取麥克風音訊（`sounddevice`，確認 Windows wheel 內建的
      PortAudio DLL 正常運作，不需要另外裝任何系統套件）
- [ ] 麥克風熱插拔（錄音中拔掉/插入 USB 麥克風）不會讓 App 崩潰或卡死
- [ ] 多個音訊裝置時，設定選單能正確列出並切換輸入裝置

### 8.4 語音辨識（asr cluster）

- [ ] `faster-whisper`（CTranslate2 CPU 推論）能正常載入模型並轉錄
- [ ] 轉錄速度記錄下來（CPU 推論注定比 Mac 版 MLX GPU 慢，需要一個實際數字
      讓使用者有心理預期，例如「10 秒錄音需要幾秒轉錄」）
- [ ] 確認 Qwen3-ASR / mlx 相關選項在 Windows 版設定選單中**不出現**或標示
      「僅 macOS 支援」，不要讓 Windows 使用者點了選項卻選到不存在的後端

### 8.5 自動貼上與前景 App 偵測（paste + frontmost cluster）

- [ ] 轉錄完成後能自動貼上（Ctrl+V 模擬）到游標所在的目標欄位
- [ ] 前景 App 偵測（決定要套用哪個 preset）在 Windows 上有對應實作或至少
      不會 crash（Windows 沒有 `osascript`，這段邏輯需要 Windows 對應寫法）
- [ ] 貼上目標涵蓋常見 App 至少各測一次：記事本、瀏覽器（Chrome/Edge）、
      Word、Slack/Teams 等聊天工具

### 8.6 打包與發布（packaging cluster）

- [ ] 依照 `build_windows.md` 步驟成功產出 `WhisperPro.exe`
- [ ] `.exe` 在**乾淨 Windows 機器**（沒裝過 Python/開發工具）上能雙擊啟動，
      沒有「找不到模組」或「DLL load failed」錯誤
- [ ] 確認 `--windowed` 生效：啟動時沒有黑色終端機視窗跳出來
- [ ] Windows SmartScreen 跳出「不明發行者」警告是預期行為（未簽章），
      確認「仍要執行」後能正常啟動，且此警告文字有記錄在使用手冊給使用者心理準備
- [ ] `.exe` 檔案大小記錄下來（`--onefile` 打包預期會不小，抓個實際數字）
- [ ] App icon 顯示正確（若已產生 `.ico`）

### 8.7 介面與體驗（gui + misc cluster）

- [ ] 字型顯示正常（若 `tokens.py` 已改成 platform-aware，確認 Segoe UI /
      Consolas 有正確套用；若還沒改，確認 Tk fallback 字型至少不會讓排版整個跑掉）
- [ ] 浮動 Mini HUD 視窗置頂效果（Tk `-topmost`）在 Windows 上正常運作
- [ ] 設定視窗、歷史紀錄視窗等所有 Toplevel 視窗能正常開關，無殘留視窗
- [ ] 單一實例鎖機制（防止開兩個 App）在 Windows 上正確運作（確認
      `psutil.Process().terminate()`/`.kill()` 路徑，而非 macOS 的 `os.kill` +
      signal 那條路）
- [ ] App 關閉時能乾淨結束（背景執行緒、pynput Listener 都有正確停止，
      工作管理員裡不會留下殭屍程序）

### 8.8 回歸驗證

- [ ] 跑一次基本錄音 → 轉錄 → 貼上全流程，錄一段中文 + 一段英文各驗證一次
- [ ] 確認 `tests/` 底下能在 Windows 上執行的測試都有跑過（部分 macOS-only
      測試預期會 skip，記錄哪些 skip 是「預期中」哪些是「這台 Windows 機器
      特有的失敗」）
- [ ] 有任何實測結果不如預期，回填到本檔案對應 cluster 章節，更新
      「難度」評估與「Windows 替代」欄位的實際狀況（本檔案第 1-5 節目前都是
      「紙上推論」，實測後請把結論改成「已驗證」並附上測試日期）
