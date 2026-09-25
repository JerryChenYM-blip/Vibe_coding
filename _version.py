"""Whisper Pro 版本字串 ── 單一真相來源（Single Source of Truth）。

任何顯示 / 打包版本的地方都從這裡讀：
- `main.py`         — splash 右下角文字
- `build_app.sh`    — `.app` bundle 的 `CFBundleShortVersionString` 與 `CFBundleVersion`

⚠️ 發版鐵律（每個 PR 都檢查）：
  1. **先改 `__version__`**（語意化版本：MAJOR.MINOR.PATCH），
     **並在下方 `_RELEASE_DATES` 加一行當天日期**（設定 →「關於」顯示在版本號底下）
  2. 跑測試 + commit
  3. merge PR → main
  4. `bash build_app.sh` 重建 `.app` bundle
  5. `git tag -a v<__version__> -m "..."` 對齊 release notes 與 Git tag

歷史：
- v2.4.1 以前 splash 字串 hard-code 在 `main.py:167`，從 v2.2.0 後 4 次發版
  （v2.3.0 → v2.4.0）都漏改。集中到本檔避免再發生（v2.4.1 重構）。
- **v2.6.0 → v2.11.0 連續 6 個 release 都沒同步本檔**（splash 一直顯示 v2.6.0）
  → 在 v2.12.0 修正、並在 `build_app.sh` 加 sanity check 比對 `_version.py`
  與 git latest tag，build 時不一致就警告。
- **v2.31.0、v2.31.1 又漏改一次**（2026-09-17／09-25，commit 標題寫了新版號，本檔
  一直停在 2.30.0，使用者在「關於」看到 2.30.0 以為沒更新到）→ v2.31.2 補上，
  同時加入更新日期與「查不到日期就紅燈」的測試。
"""

__version__ = "2.31.2"

# 每個版本的更新日期（YYYY-MM-DD）。改 __version__ 時一定要在這裡同時加一行。
#
# 刻意用「版本 → 日期」對照表，而不是兩個獨立常數：兩個常數的話，只改版本號、
# 忘了改日期，日期會停在舊的但看起來完全正常，沒有任何檢查抓得到。對照表的話，
# 新版本號查不到日期就是空字串，tests/test_version.py 一定紅燈——閘不能靠記性。
#
# 查不到時刻意回空字串而不是讓它拋 KeyError：忘了填日期不該讓 App 開不起來，
# 擋在測試那一關就夠了（「關於」頁面遇到空字串會直接不顯示日期那一行）。
_RELEASE_DATES = {
    "2.31.2": "2026-09-25",
}

__release_date__ = _RELEASE_DATES.get(__version__, "")
