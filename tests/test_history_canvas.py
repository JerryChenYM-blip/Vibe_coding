"""v2.28.0 歷史清單 canvas 重寫 — 版面時序回歸測試。

## 為什麼這支測試必須真的開 Tk 視窗（與本專案其他測試的慣例不同）

本專案其他 GUI 測試一律 mock 掉 Tk（見 `test_theme.py` 開頭的策略說明），
因為那些測的是「邏輯分支」。但這裡要擋的 bug 是**版面時序**類型，mock 在
定義上抓不到：

歷史清單從「每列一張 CTkFrame 卡片」改成「畫在單一 tk.Canvas 上的繪圖
item」之後，每個 item 的 x 座標在繪製當下就算死了，不像 widget 有
`pack(fill="x")` 會自己跟著容器變寬。而 `HistoryWindow.__init__` 裡第一次
`_refresh()` 跑在視窗被 map 之前，`canvas.winfo_width()` 只會回 1。

實測症狀（2026-08-15，重寫當天）：
  - `scrollregion` = (0, 0, **1**, 12884)
  - 第一列卡片 x 範圍 = 8 ~ **-7**（負的），外框與長度橫條全被壓在最左邊
  - `_row_ranges` 的 x 區間跟著反向 → `_entry_at_point()` 在使用者實際會點的
    位置（列中央）回 None，**選取功能完全失效**

這個 bug 當時通過了「列數 > 0」「offsets 遞增」「itemcget 顏色正確」「命中
測試」等一整輪功能檢查——命中測試之所以會過，是因為它拿 `_row_ranges` 自己
存的 x 去點它自己，輸入與輸出來自同一個壞掉的來源，自我一致但全錯。所以
下面 `test_click_hits_at_visually_plausible_x` 刻意用**寫死的座標**，不從
被測資料推導。

修法是 `_on_list_canvas_configure`（等 Tk 送出帶真實寬度的 <Configure>
再重畫），不是舊版那種 `after(0)/after(150)` 定時重試。
"""

from __future__ import annotations

import time

import pytest


# ── Tk 可用性守衛 ─────────────────────────────────────────────────────────────
# CI／無視窗環境沒有 display，開視窗會直接 TclError；skip 而不是 fail。

def _tk_available() -> bool:
    try:
        import customtkinter as ctk
        root = ctk.CTk()
        root.destroy()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _tk_available(), reason="無法開啟 Tk 視窗（無 display）"
)


@pytest.fixture(scope="module")
def history_window(tmp_path_factory):
    """在暫存 DB 上開一個真的 HistoryWindow，整個模組共用，測完關掉。

    `HistoryStore(db_path=...)` 指向 tmp 目錄——**絕不能碰使用者真實的
    ~/.whisper_app/history.db**（前例：曾有自動化流程為了「實測」寫進使用者
    真實資料夾，把 40 個設定欄位打回出廠值）。

    為什麼是 module scope 而不是每個 test 開一次：同一個行程裡建立第二個
    `CTk()` root，第一個 root 註冊的 PhotoImage（`icons.py` 產的圖示）會跟著
    失效，後續 test 全部炸 `TclError: image "pyimage1" doesn't exist`。
    下面的 test 都只讀不改視窗狀態，共用一個實例是安全的。
    """
    import customtkinter as ctk
    import gui
    import history

    tmp_path = tmp_path_factory.mktemp("history_canvas")
    store = history.HistoryStore(db_path=tmp_path / "history.db")
    for i in range(6):
        store.insert(
            duration_s=2.0 + i,
            raw_text=f"第 {i} 筆測試逐字稿內容",
            model_whisper="qwen3-asr",
            target_app="TestApp",
        )

    root = ctk.CTk()
    root.geometry("1x1+0+0")
    root.withdraw()
    win = gui.HistoryWindow(root, store, lambda *a, **k: None)
    win.geometry("980x640+100+100")
    win.deiconify()
    # 讓 Tk 真的跑完 map + <Configure>，這正是被測的時序
    deadline = time.time() + 3.0
    while time.time() < deadline and win._rendered_width <= 1:
        win.update()
        time.sleep(0.02)
    win.update()

    yield win

    try:
        win.destroy()
        root.destroy()
    except Exception:
        pass


def test_rows_drawn_at_real_canvas_width(history_window):
    """繪製用的寬度必須是 canvas 真實寬度，不是還沒 map 時的 1px。"""
    win = history_window
    real_w = win._list_canvas.winfo_width()
    assert real_w > 100, f"canvas 沒被正確配置寬度：{real_w}"
    assert win._rendered_width == real_w, (
        f"繪製用寬度 {win._rendered_width} 與 canvas 實寬 {real_w} 不符"
        "（<Configure> 重畫沒生效）"
    )


def test_scrollregion_width_matches_canvas(history_window):
    """scrollregion 的寬度是繪製當下算的，壞掉時會是 (0, 0, 1, ...)。"""
    win = history_window
    parts = str(win._list_canvas.cget("scrollregion")).split()
    assert len(parts) == 4, f"scrollregion 格式非預期：{parts}"
    assert float(parts[2]) > 100, f"scrollregion 寬度異常：{parts}"


def test_card_spans_most_of_the_width(history_window):
    """卡片要橫跨整欄，不是被壓在最左邊（壞掉時 x = 8 ~ -7）。"""
    win = history_window
    assert win._row_ranges, "沒有可點的列"
    _top, _bot, x0, x1 = win._row_ranges[0][:4]
    real_w = win._list_canvas.winfo_width()
    assert x1 > x0, f"卡片 x 區間反向：{x0} ~ {x1}"
    assert (x1 - x0) > real_w * 0.85, (
        f"卡片只佔 {x1 - x0:.0f}px、欄寬 {real_w}px —— 被壓扁了"
    )


@pytest.mark.parametrize("click_x", [30, 100, 170, 250, 320])
def test_click_hits_at_visually_plausible_x(history_window, click_x):
    """點列中央要選得到。

    座標刻意寫死、不從 `_row_ranges` 推導——當初的命中測試就是拿被測資料
    自己的 x 去點自己，所以 x 區間整個反向了還是「3/3 通過」。
    """
    win = history_window
    top, bot = win._row_ranges[0][:2]
    expected = win._row_ranges[0][4]
    hit = win._entry_at_point(click_x, (top + bot) / 2)
    assert hit is not None, f"x={click_x} 點不到任何一列（選取功能失效）"
    assert hit.id == expected.id, f"x={click_x} 點到錯的列"
