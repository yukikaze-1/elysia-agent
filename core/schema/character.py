"""CharacterState — CharacterEngine 完整状态，合并 mood + reflector 字段。"""

from typing import TypedDict

from pydantic import BaseModel, Field

from core.schema.types import SessionId, IsoTimestamp
from core.schema.utils import now_iso


class StateHistoryEntry(TypedDict):
    timestamp: IsoTimestamp
    content: str


class CharacterState(BaseModel):
    """CharacterEngine 完整状态。"""
    session_id: SessionId
    self_l2: str = ""
    state_l3: str = ""
    state_history: list[StateHistoryEntry] = Field(default_factory=list)
    last_awareness_time: float | None = None       # epoch float，非 IsoTimestamp
    approved_awareness_text: str = ""               # AwarenessJudge 批准的觉察文本
    approved_awareness_at: float | None = None       # 批准时间戳（epoch）
    awareness_show_count: int = 0                    # 已展示次数
    compression_last_at: float | None = None         # epoch float，非 IsoTimestamp
    compression_last_count: int = 0
    mood: str = "平静"
    mood_intensity: float = 0.5
    speaking_style_c6: str = ""  # C6 动态文本。空串 = 使用 L1 默认模板
    reflector_buffer: list[tuple[str, str]] = Field(default_factory=list)
    reflector_context: list[dict] = Field(default_factory=list)
    self_perception_history: list[dict] = Field(default_factory=list)
    updated_at: IsoTimestamp = Field(default_factory=now_iso)


class CharacterRuntimeState(BaseModel):
    """instance-scoped 角色运行时状态（阶段 E，§5.8 扁平迁移）。与 CharacterState 字段同构
    （子组件 0 改动），身份 instance_id（权威），保留 session_id（版本历史键）。"""
    instance_id: str
    session_id: SessionId
    source_revision: int = 1
    self_l2: str = ""
    state_l3: str = ""
    state_history: list[StateHistoryEntry] = Field(default_factory=list)
    last_awareness_time: float | None = None
    approved_awareness_text: str = ""
    approved_awareness_at: float | None = None
    awareness_show_count: int = 0
    compression_last_at: float | None = None
    compression_last_count: int = 0
    mood: str = "平静"
    mood_intensity: float = 0.5
    speaking_style_c6: str = ""
    reflector_buffer: list[tuple[str, str]] = Field(default_factory=list)
    reflector_context: list[dict] = Field(default_factory=list)
    self_perception_history: list[dict] = Field(default_factory=list)
    # 阶段 F（§2.6/架构稿 §8.5）：MemoryReflector 计数器持久化——per-runtime 化后
    # session.save_state() 不再承载，改经 runtime.state（回合边界 flush）。
    mr_round_count: int = 0
    mr_new_memory_count: int = 0
    mr_last_summary: dict[str, object] = Field(default_factory=dict)
    updated_at: IsoTimestamp = Field(default_factory=now_iso)
