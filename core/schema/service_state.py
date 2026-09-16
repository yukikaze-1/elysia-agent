"""服务运行时状态（session 专用字段）。"""
from dataclasses import dataclass

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


@dataclass(frozen=True)
class ServiceRuntimeState:
    """服务**运行期**状态快照（F2，Batch 1.6）——供"自述"类消费者读取。

    与 `ServiceState` **不是**一回事，两者刻意分开：

    - `ServiceState` 是**持久化权威**：每个会话一份、写进 `sessions` 集合、重启后按
      `prefer_config_model` 规则参与恢复，字段是三态（None = 未显式设置）；
    - 本类是**运行期权威的投影**：此刻真正在用的模型对象与派生开关，构造一次即冻结，
      **没有持久化面**、不参与任何恢复规则。

    为什么要有这个类型（F2 的根因）：`get_runtime_config` 工具此前没有任何运行期句柄，
    只能抓到 `ToolContext.services["config"]`（bootstrap/YAML），于是热切换之后它向
    用户报告的是启动时的模型与开关。判据必须从"配置里写了什么"换成"现在在用什么"。

    建模约定：`*_model is None` ⇔ 该服务的后端对象不存在（未连接 / 降级为纯文本），
    与 `core/services/capabilities.py` 的 `ServiceCapability.model`（available=False →
    null）**同判据**——两处报告器因此不可能对"有没有连上"给出不同答案。
    """

    llm_model: str
    stt_model: str | None
    stt_enabled: bool
    tts_model: str | None
    tts_enabled: bool
