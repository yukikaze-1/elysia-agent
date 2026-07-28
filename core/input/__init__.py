"""Input layer — audio buffer + WebSocket server for browser clients."""

from .audio_buffer import AudioBuffer, pcm_to_wav
from .server import create_app
from .wakeword import WakeWordDetector

__all__ = ["AudioBuffer", "pcm_to_wav", "WakeWordDetector", "create_app"]
