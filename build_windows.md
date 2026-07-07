<!-- Mac/Windows 雙棲 — 移植自 macOS v2.21.6 -->

# Windows 打包指南（PyInstaller）

> 對應 macOS 版的 `build_app.sh`。macOS 用 `.app` bundle + `py2app`-like 流程
> （見 `WhisperPro.spec` 的 `BUNDLE()` 區塊），Windows 沒有 bundle 概念，
> 改用 PyInstaller 直接產生單一 `.exe`。
>
> 這份文件只說「怎麼打包」，不涉及簽章 / 安裝程式（Inno Setup 等屬 Phase 3，
> 見 `WINDOWS_PORT_PLAN.md` 第 6.3 節優先順序）。

---

## 0. 前提

- Python 3.11+（建議跟 Mac 版一致用 3.13，但 PyInstaller 對 3.11/3.12 支援更成熟，
  若遇到相容性問題可退到 3.12）
- 已在 Windows 機器上跑過 `pip install -r requirements-windows.txt`
- 額外安裝打包工具：
  ```powershell
  pip install pyinstaller
  ```
- ffmpeg 已加入系統 PATH（`faster-whisper` 執行期需要，PyInstaller **不會**自動幫你打包
  外部執行檔，使用者電腦上必須自己有 ffmpeg，或你額外把 `ffmpeg.exe` 塞進 `--add-binary`）

---

## 1. 基本打包指令

macOS 版是 `.app`（資料夾形式的 bundle），Windows 對應「單一雙擊執行檔」用
`--onefile`；GUI 程式（沒有終端機視窗）要加 `--windowed`（等同 `--noconsole`）：

```powershell
pyinstaller --windowed --onefile ^
  --name WhisperPro ^
  --icon assets\icon.ico ^
  main.py
```

- `--windowed`：不跳出黑色終端機視窗（GUI 應用程式標配，對應 macOS spec 裡的
  `console=False`）
- `--onefile`：打包成單一 `.exe`（啟動稍慢，因為每次執行要先解壓到暫存目錄，
  但對使用者來說「一個檔案」最單純，適合單機發送）
- 若日後發現啟動速度是痛點，可以改用 `--onedir`（產生一個資料夾，含 `.exe` +
  一堆 DLL，啟動快很多，但發布時要整個資料夾一起給，不能只丟一個檔案）

**注意**：目前 repo 沒有 `assets/icon.ico`（只有 macOS 用的 `.icns` 和
`.png`）。`app_icon.py` 目前只會產生 `.icns`（呼叫 `iconutil`，Windows 上這個指令
不存在）。Windows 版圖示需要另外產生 `.ico`——可以用既有的 `assets/icon.png`
透過 Pillow 轉檔（`Image.open("icon.png").save("icon.ico", format="ICO",
sizes=[(16,16),(32,32),(48,48),(256,256)])`），這屬於 `app_icon.py` 的改動範圍，
不在本次「只加檔案」任務內，這裡先記錄步驟。若還沒有 `.ico`，可以先拿掉
`--icon` 參數打包（會用 PyInstaller 預設圖示）。

---

## 2. 使用 .spec 檔（建議做法，取代上面的一行指令）

macOS 版有 `WhisperPro.spec` 管理完整設定。Windows 建議另外維護一份
`WhisperPro.windows.spec`（**本次任務不新增這個檔案**，因為它是 PyInstaller 的
建置設定檔、不是純文件，屬於後續實作階段的工作。以下列出內容應該長怎樣，
供下一輪工作直接套用）：

```python
# -*- mode: python ; coding: utf-8 -*-
# 範例草稿，尚未建立為實體檔案

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('assets', 'assets'),   # 把整個 assets/ 資料夾（icon.png 等）一起塞進去
    ],
    hiddenimports=[
        'webrtcvad',
        'customtkinter',
        'faster_whisper',
        'sounddevice',
        'pynput.keyboard._win32',   # 注意：Windows 版要換成 _win32，不是 _darwin
        'pynput.mouse._win32',
        # faster-whisper 底層 ctranslate2 動態載入 DLL，PyInstaller 的靜態分析
        # 抓不到，必須手動列 hidden import，見下方第 3 節詳細說明
        'ctranslate2',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='WhisperPro',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # 對應 --windowed
    icon='assets\\icon.ico',
    onefile=True,            # 對應 --onefile；用 --onedir 時改用 COLLECT() 區塊
)
```

打包指令改成：

```powershell
pyinstaller WhisperPro.windows.spec
```

**與 macOS 版 `WhisperPro.spec` 的差異對照**（供下一輪工作對照著改）：

| macOS spec 區塊 | Windows spec 對應 |
|---|---|
| `binaries=[...]` 找 Homebrew `libportaudio.2.dylib` | 不需要：`sounddevice` 的 Windows wheel 已內建 PortAudio DLL，不用手動塞 |
| `hiddenimports` 含 `pynput.keyboard._darwin` | 改成 `pynput.keyboard._win32` / `pynput.mouse._win32` |
| `target_arch='arm64'` | 整個參數拿掉（Windows EXE() 沒有這個參數，PyInstaller 會用執行打包指令那台機器的架構） |
| `BUNDLE(...)` 整個區塊（`.app` bundle、`Info.plist`、`bundle_identifier`） | 完全拿掉，Windows 沒有這個概念，`EXE()` 產生的 `.exe` 就是最終產物 |
| `codesign_identity` / `entitlements_file` | Windows 沒有 codesign 概念，程式碼簽章是完全不同機制（需要向 CA 買憑證，用 `signtool.exe`），屬 Phase 3，先不處理 |

---

## 3. faster-whisper / ctranslate2 hidden imports 注意事項（重點）

這是 Windows 打包**最常炸的地方**，原因：`faster-whisper` 底層依賴
`ctranslate2`，而 `ctranslate2` 是一個 C++ 編譯的推論引擎，透過 Python
binding 在**執行期動態載入**一堆 `.dll`（例如 Intel MKL / OpenMP 相關 DLL）。
PyInstaller 的靜態分析（掃 `import` 語句）**看不到**這種執行期才發生的載入行為，
結果就是：打包時不報錯、跑起來時才丟 `ImportError` 或直接 DLL load 失敗，
且錯誤訊息通常很難懂（`OSError: [WinError 126] 找不到指定的模組`）。

### 常見對策

1. **先確認能不能開箱正常跑**：`ctranslate2` 官方有維護 PyInstaller hook
   （新版 `ctranslate2` 套件通常會在安裝目錄下帶 `.dll`，PyInstaller 4.x+
   對很多套件有內建 hook 能自動抓到 binary）。**先跑一次陽春的
   `pyinstaller --onefile main.py`，實際執行看看是否已經正常**，很多時候
   不需要手動介入。

2. **若跑出來 DLL 找不到**，用 `--collect-all` 强制把整個套件的資料/binary
   都收進去（比手動列 hiddenimports 更暴力但更保險）：
   ```powershell
   pyinstaller --windowed --onefile --name WhisperPro ^
     --collect-all ctranslate2 ^
     --collect-all faster_whisper ^
     main.py
   ```

3. **若仍缺特定 DLL**，用 `--add-binary` 手動把 DLL 路徑塞進去。DLL 通常在
   `venv\Lib\site-packages\ctranslate2\` 目錄下，範例：
   ```powershell
   pyinstaller --windowed --onefile --name WhisperPro ^
     --add-binary "venv\Lib\site-packages\ctranslate2\*.dll;ctranslate2" ^
     main.py
   ```
   （Windows 上 `--add-binary` 用分號 `;` 分隔來源與目的地，macOS/Linux 用冒號
   `:`——這是 PyInstaller 本身的平台差異，不是我們程式碼的問題）

4. **numpy / MKL 相關 DLL 衝突**：如果打包後出現 `numpy` 相關的 DLL 版本衝突
   （常見錯誤字樣 `Intel MKL FATAL ERROR`），通常是虛擬環境裡混到多份
   numpy/MKL 造成，解法是用**乾淨的 venv**（只裝 `requirements-windows.txt`
   列出的套件，不要在同一個環境累積裝過其他專案的套件）重新打包。

5. **打包完一定要在乾淨的 Windows 機器（或至少乾淨帳號）測試**，不要只在
   開發機上測。開發機上因為裝過完整 Python + 一堆套件，很多「其實沒被
   PyInstaller 打包進去、但剛好系統 PATH 上有」的 DLL 會被誤判成正常。
   建議用一台沒裝過 Python 的 Windows 機器（或虛擬機）驗證。

### 為什麼 Mac 版沒遇到這問題

Mac 版目前的 `.app` 打包主要驗證過的是 `mlx-whisper`（Apple 專屬 GPU 後端），
`faster-whisper`（CTranslate2 CPU 後端）在 Mac 上是**次要 fallback 路徑**、
過去實機驗證量較少。Windows 版**沒有 mlx**，`faster-whisper` 變成**唯一**
語音辨識後端，代表這條路徑在 Windows 上是「主線」，DLL 打包必須確實驗證過，
不能沿用 Mac 那邊「反正也用不到」的心態。

---

## 4. assets／資源檔案打包

macOS 版用 `Path.home() / "Applications" / "WhisperPro.app" / "Contents" / ...`
這種 bundle 內部路徑抓資源。Windows `.exe`（尤其 `--onefile`）執行期資源會被解壓到
一個暫存資料夾（`sys._MEIPASS`），程式碼裡讀 `assets/` 底下檔案的地方，**如果
本次任務之後有人要動 `.py`**，要注意判斷：

```python
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    # PyInstaller 打包後執行，資源被解壓到暫存目錄
    BASE_DIR = Path(sys._MEIPASS)
else:
    BASE_DIR = Path(__file__).parent

ASSETS_DIR = BASE_DIR / "assets"
```

這段邏輯**本次任務不新增**（會動到 `.py`，超出「只加檔案」範圍），這裡只記錄
給下一輪實作參考。打包指令面要記得用 `--add-data`（或 spec 檔的 `datas=[...]`）
把 `assets/` 資料夾整個帶進去：

```powershell
pyinstaller --windowed --onefile --name WhisperPro ^
  --add-data "assets;assets" ^
  main.py
```

（同樣，Windows 用分號 `;` 分隔，macOS/Linux 用冒號 `:`）

---

## 5. 打包後驗證清單

打包出 `.exe` 後，逐項確認（詳細版見 `WINDOWS_PORT_PLAN.md` 結尾「Windows 實機
測試清單」）：

- [ ] 雙擊 `.exe` 能啟動 GUI，沒有跳出黑色終端機視窗（確認 `--windowed` 生效）
- [ ] 沒有跳出「找不到模組」或「DLL load failed」錯誤
- [ ] 錄音 → 轉錄流程能跑通（驗證 `ctranslate2` DLL 真的有被打包進去且能載入）
- [ ] App icon 有正確顯示（如果有做 `.ico`）
- [ ] 在**乾淨 Windows 機器**（沒裝過 Python）上也能跑，不只在開發機上測

---

## 6. 參考

- PyInstaller 官方文件：https://pyinstaller.org/en/stable/
- PyInstaller `--onefile` vs `--onedir` 取捨：https://pyinstaller.org/en/stable/operating-mode.html
- ctranslate2 官方 repo（打包相關 issue 常見討論串）：https://github.com/OpenNMT/CTranslate2
