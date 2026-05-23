"""v2.6.0 主題切換 regression test — 10 個 test、覆蓋率 91%。

涵蓋：
  - tokens.py palette 結構（key 集合一致 / dark 為預設 / 未知值 fallback）
  - main._relaunch_app() 兩條路徑（.app bundle / dev mode）
  - SettingsWindow 切換流程（confirm dialog / state 警告 / 順序 / noop）

策略：所有 test 都 mock 外部依賴（subprocess / os.execv / Config / Tk），
不需要真的開視窗或 spawn process。

詳見 docs/superpowers/plans/2026-05-23-light-theme-and-appearance-toggle.md
"""

import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest


# ═════════════════════════════════════════════════════════════════════════════
#  tokens.py palette 結構（T8.1 + T8.2 + Issue 2 fallback）
# ═════════════════════════════════════════════════════════════════════════════

def test_palette_keys_consistent_across_themes():
    """T8.1：dark / light 兩套 palette 必須有完全一樣的 key 集合。

    避免 import 漏 key 出 KeyError（例如 light 加新 token 但忘了給 dark）。
    """
    import importlib
    import tokens
    importlib.reload(tokens)
    dark_keys  = set(tokens._PALETTES["dark"].keys())
    light_keys = set(tokens._PALETTES["light"].keys())
    only_in_dark  = dark_keys - light_keys
    only_in_light = light_keys - dark_keys
    assert not only_in_dark,  f"dark 多了 light 沒有的 key: {only_in_dark}"
    assert not only_in_light, f"light 多了 dark 沒有的 key: {only_in_light}"


def test_default_theme_is_dark():
    """T8.2：Config dataclass 預設 theme="dark"，不亂換預設。"""
    from config import Config
    cfg = Config()
    assert cfg.theme == "dark"


def test_unknown_theme_falls_back_to_dark():
    """Eng Review Issue 2：未知 theme 值（"purple" / "Light" / typo）→ fallback dark。"""
    import importlib
    # Mock Config.load() 回一個 cfg.theme = "purple"
    fake_cfg = types.SimpleNamespace(theme="purple")
    with patch("config.Config.load", return_value=fake_cfg):
        import tokens
        importlib.reload(tokens)
        # 即使 config.theme="purple"、tokens.BG 應該是 dark 的值（#000000）
        assert tokens.BG == "#000000"
        assert tokens._THEME == "dark"

    # Cleanup：reload 回正常 cfg.theme（restore default state for其他 test）
    importlib.reload(tokens)


# ═════════════════════════════════════════════════════════════════════════════
#  main._relaunch_app() 兩條路徑（T8.3 + T8.4）
# ═════════════════════════════════════════════════════════════════════════════

def test_relaunch_helper_chooses_app_bundle_when_bundled():
    """T8.3：sys.executable 含 'WhisperPro.app' → 用 subprocess.Popen([open, -n, ...])。"""
    import main
    fake_exec = "/Users/x/Applications/WhisperPro.app/Contents/Frameworks/Python.app/Contents/MacOS/Python"
    # PosixPath.exists 是 read-only、不能直接 patch.object；改用 fake bundle MagicMock
    fake_bundle = MagicMock()
    fake_bundle.exists.return_value = True
    fake_bundle.__str__ = lambda self: "/fake/WhisperPro.app"
    with patch.object(sys, "executable", fake_exec), \
         patch.object(main, "_APP_BUNDLE", fake_bundle), \
         patch("subprocess.Popen") as popen_spy, \
         patch("os.execv") as execv_spy:
        ok = main._relaunch_app()

    assert ok is True
    popen_spy.assert_called_once()
    call_args = popen_spy.call_args[0][0]
    assert call_args[0] == "open"
    assert call_args[1] == "-n"
    # call_args[2] 是 str(_APP_BUNDLE) — 因為 MagicMock 不一定能直接 str()，
    # 確認有非空字串被傳進去就足夠（重點是 open + -n + 某個 path）
    assert isinstance(call_args[2], str) and len(call_args[2]) > 0
    execv_spy.assert_not_called()   # bundle 成功就不走 execv


def test_relaunch_helper_falls_back_to_execv_when_not_bundled():
    """T8.4：dev 模式（sys.executable 不含 WhisperPro.app）→ os.execv 自我 re-exec。"""
    import main
    fake_exec = "/usr/local/bin/python3"
    with patch.object(sys, "executable", fake_exec), \
         patch("subprocess.Popen") as popen_spy, \
         patch("os.execv") as execv_spy:
        # execv 通常不 return（replace process image）；mock 它就 return None、
        # function 走到 return False。所以期望 False（execv 沒真正 execute）。
        ok = main._relaunch_app()

    popen_spy.assert_not_called()   # 不是 bundle，不走 open -n
    execv_spy.assert_called_once()
    assert execv_spy.call_args[0][0] == fake_exec   # 第一個 arg = sys.executable
    # ok 是 False 是因為 mock 的 execv 直接 return（真實環境會 replace image 不 return）


# ═════════════════════════════════════════════════════════════════════════════
#  Cleanup-before-spawn 順序（Eng Review Issue 1）
# ═════════════════════════════════════════════════════════════════════════════

def test_relaunch_calls_cleanup_before_spawn():
    """Issue 1 / cleanup-first：_do_theme_relaunch 內 on_close 必須在 _relaunch_app 之前。

    驗證呼叫順序：on_close → _relaunch_app → sys.exit。
    """
    from gui import AppWindow
    win = AppWindow.__new__(AppWindow)

    # 用 single MagicMock 蒐集所有呼叫的順序
    tracker = MagicMock()
    win.on_close = tracker.on_close
    win._show_toast = MagicMock()

    # 收集 after callback（800ms 那個）
    scheduled = []
    def fake_after(ms, fn):
        scheduled.append((ms, fn))
    win.after = fake_after

    win._do_theme_relaunch()

    # 應該有一個 after(800, _do_relaunch_sequence) 被排
    assert len(scheduled) == 1
    assert scheduled[0][0] == 800
    relaunch_seq = scheduled[0][1]

    # 模擬 800ms 後執行 relaunch sequence
    with patch("main._relaunch_app", return_value=True) as relaunch_spy, \
         patch("sys.exit") as exit_spy:
        # 鏈接 tracker：relaunch_spy 與 exit_spy 也記在同一個 mock 上
        tracker.relaunch = relaunch_spy
        tracker.exit = exit_spy
        # patch 內呼叫 main._relaunch_app 時要透過 tracker.relaunch 記錄
        # 用 side_effect 把呼叫導到 tracker
        relaunch_spy.side_effect = lambda: tracker.relaunch_inner() or True
        exit_spy.side_effect    = lambda code=0: tracker.exit_inner(code)
        relaunch_seq()

    # 驗證 mock_calls 順序：on_close 必須在 relaunch_inner 之前；relaunch_inner 在 exit_inner 之前
    names = [call[0] for call in tracker.mock_calls]
    # 取出我們關心的 3 個事件
    relevant = [n for n in names if n in ("on_close", "relaunch_inner", "exit_inner")]
    assert relevant == ["on_close", "relaunch_inner", "exit_inner"], \
        f"順序錯誤：實際 {relevant}"


# ═════════════════════════════════════════════════════════════════════════════
#  SettingsWindow theme handler（T8.5 + 新 4 個 gap test）
# ═════════════════════════════════════════════════════════════════════════════

def _make_stub_settings():
    """繞過 __init__ 建 SettingsWindow stub，含 mock 必要 attribute。"""
    from gui import SettingsWindow
    sw = SettingsWindow.__new__(SettingsWindow)

    # cfg 可變 stub（不要用真的 Config，避免存到使用者真的 config.json）
    sw.cfg = types.SimpleNamespace(theme="dark")
    sw.cfg.save = MagicMock()

    # _theme_var：用 MagicMock 模擬 ctk.StringVar
    sw._theme_var = MagicMock()
    sw._theme_var.get.return_value = "dark"
    sw._theme_var.set = MagicMock()

    # _theme_btns：兩顆假按鈕
    sw._theme_btns = {"dark": MagicMock(), "light": MagicMock()}

    # _parent 是 AppWindow（stub）
    sw._parent = MagicMock()
    sw._parent._state = "idle"
    sw._parent._polish_busy = False

    # destroy 攔成 no-op
    sw.destroy = MagicMock()
    return sw


def test_settings_recording_state_warns_in_confirm():
    """T8.5：錄音中時 confirm dialog 必須含「進行中錄音會被中斷」警告。

    這 test 透過 monkey-patch CTkToplevel 攔截 dialog 構造，驗證 warning text
    被組進去。
    """
    sw = _make_stub_settings()
    sw._parent._state = "recording"
    sw._parent._polish_busy = False

    captured_texts = []

    def fake_ctk_label(parent, text="", **kw):
        captured_texts.append(text)
        return MagicMock()

    # patch CTkToplevel + CTkLabel + CTkButton + CTkFrame，攔下文字
    with patch("gui.ctk.CTkToplevel") as fake_top, \
         patch("gui.ctk.CTkLabel", side_effect=fake_ctk_label), \
         patch("gui.ctk.CTkFrame"), \
         patch("gui.ctk.CTkButton"), \
         patch("gui.ctk.CTkFont"):
        fake_top.return_value = MagicMock()
        sw._confirm_theme_switch("light")

    joined = "\n".join(captured_texts)
    assert "錄音" in joined, f"未含錄音警告：{joined!r}"


def test_polish_busy_state_warns_in_confirm():
    """polish_busy=True 時 confirm dialog 必須含「潤飾進行中、結果會遺失」警告。"""
    sw = _make_stub_settings()
    sw._parent._state = "idle"
    sw._parent._polish_busy = True

    captured_texts = []

    def fake_ctk_label(parent, text="", **kw):
        captured_texts.append(text)
        return MagicMock()

    with patch("gui.ctk.CTkToplevel"), \
         patch("gui.ctk.CTkLabel", side_effect=fake_ctk_label), \
         patch("gui.ctk.CTkFrame"), \
         patch("gui.ctk.CTkButton"), \
         patch("gui.ctk.CTkFont"):
        sw._confirm_theme_switch("light")

    joined = "\n".join(captured_texts)
    assert "潤飾" in joined, f"未含潤飾警告：{joined!r}"


def test_same_theme_click_no_confirm():
    """點當前 theme 的 chip → 不彈 confirm dialog、不 save、不 restart。"""
    sw = _make_stub_settings()
    sw.cfg.theme = "dark"

    with patch.object(sw, "_confirm_theme_switch") as confirm_spy:
        sw._on_theme_clicked("dark")   # 跟現在一樣

    confirm_spy.assert_not_called()
    sw.cfg.save.assert_not_called()


def test_trigger_theme_relaunch_save_and_delegate_order():
    """確認流程：cfg.theme 更新 → cfg.save() → _parent._do_theme_relaunch() → destroy。

    這驗證 save 確實在 delegate 之前發生（萬一順序顛倒、SettingsWindow 已 destroy
    但 cfg 沒寫進磁碟、新 process 啟動會讀到舊主題 → restart 假裝沒切）。
    """
    sw = _make_stub_settings()
    sw.cfg.theme = "dark"

    tracker = MagicMock()
    sw.cfg.save = tracker.save
    sw._parent._do_theme_relaunch = tracker.delegate_relaunch
    sw.destroy = tracker.destroy

    sw._trigger_theme_relaunch("light")

    # 期望順序：save → delegate_relaunch → destroy
    names = [call[0] for call in tracker.mock_calls]
    relevant = [n for n in names if n in ("save", "delegate_relaunch", "destroy")]
    assert relevant == ["save", "delegate_relaunch", "destroy"], \
        f"順序錯誤：實際 {relevant}"
    # cfg.theme 必須已更新到新值
    assert sw.cfg.theme == "light"
