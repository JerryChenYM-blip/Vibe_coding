import webrtcvad
import numpy as np

class VAD:
    def __init__(self, mode=2):
        self.vad = webrtcvad.Vad(mode)
        self.sample_rate = 16000

    def is_speech(self, float_audio: np.ndarray) -> bool:
        """Returns True if any speech is detected in the chunk"""
        if float_audio.size == 0:
            return False
            
        # Convert float32 [-1.0, 1.0] to int16
        # Need to ensure no out-of-bounds, though standard audio should be fine
        audio_int16 = (float_audio * 32767).clip(-32768, 32767).astype(np.int16)
        pcm_data = audio_int16.tobytes()
        
        # webrtcvad only supports 10, 20, or 30 ms frames. 
        # 16000 Hz * 30 ms = 480 samples = 960 bytes
        frame_duration = 30 # ms
        samples_per_frame = int(self.sample_rate * (frame_duration / 1000.0))
        bytes_per_frame = samples_per_frame * 2 
        
        for i in range(0, len(pcm_data) - bytes_per_frame + 1, bytes_per_frame):
            frame = pcm_data[i:i+bytes_per_frame]
            if len(frame) == bytes_per_frame:
                if self.vad.is_speech(frame, self.sample_rate):
                    return True
        return False
