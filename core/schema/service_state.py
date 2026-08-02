"""服务运行时状态（session 专用字段）。"""
from pydantic import BaseModel


class ServiceState(BaseModel):
    """服务运行时状态（SessionMemory 专用字段，MongoDB 30d TTL，重启存活）。

    stt_enabled / tts_enabled 三态：None = 用户未显式改，回退 config.<svc>.enabled。
    """
    llm_model: str = ""
    stt_model: str = ""
    tts_model: str = ""
    stt_enabled: bool | None = None
    tts_enabled: bool | None = None
