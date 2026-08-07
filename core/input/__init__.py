"""输入层 —— 面向浏览器客户端的音频缓冲 + WebSocket 服务。"""

from .audio_buffer import AudioBuffer, pcm_to_wav
from .server import create_app
from .wakeword import WakeWordDetector

__all__ = ["AudioBuffer", "pcm_to_wav", "WakeWordDetector", "create_app"]
