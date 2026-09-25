"""版本號與更新日期 unit tests（v2.31.2）。

_version.py 的歷史就是一部「漏改史」：v2.3.0→v2.4.0 連 4 版漏改、v2.6.0→v2.11.0
連 6 版漏改、v2.31.0/v2.31.1 又漏改（commit 標題寫了新版號，本檔停在 2.30.0）。

這組測試守兩件事：
  (a) 目前的版本號一定查得到更新日期——改了版本號卻忘了填日期，這裡會紅燈
  (b) build_app.sh 讀版本號的那行 grep/sed 仍然讀得出來——改 _version.py 的
      格式時，最容易在不知不覺間把打包腳本弄壞
"""

import datetime
import re
import subprocess
from pathlib import Path

import _version

ROOT = Path(__file__).resolve().parent.parent


def test_目前的版本號查得到更新日期():
    """這是整組測試的核心：改 __version__ 時忘了在 _RELEASE_DATES 加一行，就會停在這裡。"""
    assert _version.__version__ in _version._RELEASE_DATES, (
        f"版本 {_version.__version__} 在 _version._RELEASE_DATES 裡沒有對應的更新日期。"
        "改版本號時要同時加一行 \"版本\": \"YYYY-MM-DD\"。"
    )
    assert _version.__release_date__, "更新日期是空的"


def test_版本號是三段數字():
    assert re.fullmatch(r"\d+\.\d+\.\d+", _version.__version__)


def test_每一筆日期都是合法日期而且不在未來():
    """格式錯（2026/9/25）或手滑打成未來年份（2062-09-25）都要擋。"""
    today = datetime.date.today()
    for ver, date_str in _version._RELEASE_DATES.items():
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str), f"{ver} 的日期格式要是 YYYY-MM-DD：{date_str!r}"
        d = datetime.date.fromisoformat(date_str)
        assert d <= today, f"{ver} 的日期 {date_str} 在未來"


def test_打包腳本讀得出版本號():
    """直接跑 build_app.sh 裡那一行一模一樣的 grep + sed。

    build_app.sh 是用正規表示式從 _version.py 撈版本字串（不是 import），
    所以 _version.py 的格式一改，Python 端照樣正常、打包卻會壞——而且要等到
    有人真的去打包才會發現。
    """
    out = subprocess.run(
        ["bash", "-c",
         r"""grep -E '^__version__\s*=' _version.py | sed -E 's/.*"([^"]+)".*/\1/'"""],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out == _version.__version__


def test_關於頁面有顯示更新日期():
    """原始碼層級檢查：「關於」頁面要讀 __release_date__。

    比照 tests/test_p1_clusters.py 驗證匯出 manifest 讀 _version 的作法——
    不啟動整個 Tk 視窗，只確認資料來源接對了。
    """
    src = (ROOT / "gui.py").read_text(encoding="utf-8")
    start = src.index("def _build_page_about(")
    body = src[start:src.index("\n    def ", start + 1)]
    assert "__release_date__" in body
