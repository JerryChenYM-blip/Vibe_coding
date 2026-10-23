"""全部測試共用的前提。"""

import pytest


@pytest.fixture(scope="session", autouse=True)
def _load_sounddevice_once():
    """v2.32.3 起 Mac 上 sounddevice 不在 import recorder 時載入（改由 App 在背景載入）。
    舊測試都假設 import 完就能用 recorder.sd，這裡先載入一次恢復這個前提。
    測試是從終端機跑，沒有 App 身分，不受麥克風模式影響，載入約 0.2 秒。
    要測「還沒載入」的情況，各測試自己用 monkeypatch 把 recorder.sd 設回 None。"""
    import recorder
    recorder.ensure_sounddevice()
    yield
