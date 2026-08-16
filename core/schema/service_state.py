"""服务运行时状态（session 专用字段）。"""
from pydantic import BaseModel


class ServiceState(BaseModel):
    """服务运行时状态（SessionMemory 专用字段，sessions 集合持久化——无 Mongo 自动 TTL，显式删除才清理；重启存活）。

    stt_enabled / tts_enabled 三态：None = 用户未显式改，回退 config.<svc>.enabled。
    """
    llm_model: str = ""
    stt_model: str = ""
    tts_model: str = ""
    stt_enabled: bool | None = None
    tts_enabled: bool | None = None
    # 批次 5（Task 11，BE-051）：用户设置的声音覆盖（每 turn 注入 TTS 合成）。
    # None = 未显式设置，走后端默认（character 解析）；经 SessionMemory 泛化
    # model_fields 遍历自动 save/load round-trip。
    tts_voice: str | None = None
