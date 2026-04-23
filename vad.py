"""
Voice Activity Detection（語音活動偵測）包裝器。

使用 Google WebRTC VAD 演算法，判斷一段 float32 音訊是否包含真實語音。
主要用途：在送入 Whisper 之前過濾靜音片段，避免無效推論與幻覺輸出。

依賴：webrtcvad（Python binding for libwebrtc VAD）
"""

import webrtcvad
import numpy as np


class VAD:
    """WebRTC VAD 的薄包裝，接收 float32 音訊並回傳是否含語音。

    WebRTC VAD 原生只接受 int16 PCM 格式，本類別負責格式轉換與
    固定幀長（30 ms）的切片迭代，對外暴露更友善的 float32 介面。

    Args:
        mode: VAD 積極程度，0（最寬鬆） ~ 3（最嚴格）。
              預設 2：在我們的場景（16 kHz 單聲道）平衡靈敏度與誤判率。
    """

    def __init__(self, mode: int = 2) -> None:
        self.vad = webrtcvad.Vad(mode)
        self.sample_rate = 16000   # 固定 16 kHz，與 recorder.py 一致

    def is_speech(self, float_audio: np.ndarray) -> bool:
        """判斷 float32 音訊片段是否包含語音。

        只要有任何一個 30 ms 幀被判為語音就回 True，適合用於
        「這段錄音值不值得送 Whisper」的二元判斷。

        Args:
            float_audio: float32 numpy array，振幅範圍 [-1.0, 1.0]，16 kHz 單聲道。

        Returns:
            True 代表偵測到語音，False 代表靜音或輸入為空。
        """
        if float_audio.size == 0:
            return False

        # webrtcvad 只接受 int16 PCM；clip 確保不溢位（正常音訊不會超出 ±1.0）
        audio_int16 = (float_audio * 32767).clip(-32768, 32767).astype(np.int16)
        pcm_data = audio_int16.tobytes()

        # webrtcvad 只支援 10 / 20 / 30 ms 幀長；選 30 ms 以降低呼叫次數。
        # 16000 Hz × 0.03 s = 480 samples = 960 bytes（int16 每 sample 2 bytes）
        frame_duration_ms = 30
        samples_per_frame = int(self.sample_rate * (frame_duration_ms / 1000.0))
        bytes_per_frame   = samples_per_frame * 2

        for i in range(0, len(pcm_data) - bytes_per_frame + 1, bytes_per_frame):
            frame = pcm_data[i : i + bytes_per_frame]
            if len(frame) == bytes_per_frame:
                if self.vad.is_speech(frame, self.sample_rate):
                    return True
        return False
