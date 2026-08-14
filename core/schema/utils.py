"""core/schema 工具函数 — 类型定义之外的小型辅助函数（共享工具模块）。

now_iso:          跨包共享（core/personality、cognitive、input、storage、memory 等）。
_normalize_iso_ts / _validate_src: schema 内部私有 helper（下划线前缀，勿跨包引用）。

ID 生成器（new_message_id 等 + stable_tool_message_id，2026-08-14 从 core/utils/message_id
迁入）：返回类型全为本包 NewType，属类型权威附属，放此天经地义。core/utils/message_id
保留为薄重导出 shim 保持既有调用面（core.utils → core.schema 单向依赖，符合「叶子在最底」）。
"""

import hashlib
import uuid
from datetime import datetime, timezone

from core.schema.types import (
    AttachmentId,
    GenerationId,
    MemoryId,
    MessageId,
    SegmentId,
    TaskId,
)


def _normalize_iso_ts(ts: str | float) -> str:
    """归一化 ISO 8601 时间戳：补时区、校验格式。同时兼容旧 float Unix 时间戳。

    Args:
        ts: ISO 8601 字符串或 Unix 时间戳（float/int）。

    Returns:
        归一化后的 ISO 8601 UTC 时间戳字符串。
    """
    if isinstance(ts, (int, float)):
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.isoformat()
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def now_iso() -> str:
    """返回当前 UTC 时间 ISO 8601 字符串。

    Returns:
        当前 UTC 时间的 ISO 8601 格式字符串。
    """
    return datetime.now(timezone.utc).isoformat()


def _validate_src(v: str) -> str:
    """校验 Part src 字段：不能为空，必须是合法 URL / data URI / 绝对路径 / 存在的本地文件。

    Args:
        v: 待校验的 src 字符串。

    Returns:
        校验通过的原字符串。

    Raises:
        ValueError: src 为空或无法解析为有效路径/URL。
    """
    from pathlib import Path

    if not v:
        raise ValueError("src 不能为空")
    if v.startswith(("http://", "https://", "data:", "/")):
        return v
    if Path(v).exists():
        return v
    raise ValueError(f"无效的 src: {v!r}")


def _new_id(prefix: str) -> str:
    """生成 prefix_ + 26 位十六进制字符（v1 前缀 msg_；批次 2 独立前缀 gen_/seg_；批次 9 衍生 ID att_/mem_/task_，wire 可区分）。"""
    return f"{prefix}_{uuid.uuid4().hex[:26]}"


def new_message_id() -> MessageId:
    """生成新的消息 ID，格式为 'msg_' + 26 位十六进制字符。

    Returns:
        MessageId: 新生成的消息 ID
    """
    return MessageId(_new_id("msg"))


def new_generation_id() -> GenerationId:
    """生成一次生成实例 ID（'gen_' 前缀，与 messageId 语义区分，批次 2）。"""
    return GenerationId(_new_id("gen"))


def new_segment_id() -> SegmentId:
    """生成一个分段 ID（'seg_' 前缀，批次 2）。"""
    return SegmentId(_new_id("seg"))


def new_attachment_id() -> AttachmentId:
    """生成一个新的附件 ID，格式为 'att_' + 26 位十六进制字符。"""
    return AttachmentId(_new_id("att"))


def new_memory_id() -> MemoryId:
    """生成一个新的 LTM 记忆条目 ID（'mem_' 前缀，与消息 ID 值空间隔离；Milvus 旧 'msg_' 数据读回兼容）。"""
    return MemoryId(_new_id("mem"))


def new_task_id() -> TaskId:
    """生成一个新的调度任务 ID（'task_' 前缀，与消息 ID 值空间隔离）。"""
    return TaskId(_new_id("task"))


def stable_tool_message_id(tool_call_id: str, timestamp: str) -> MessageId:
    """历史工具消息的稳定 ID（spec D1）：msg_ + sha256(tool_call_id:timestamp)[:26]。

    存量空 message_id 工具消息的读侧派生与回填共用本函数：同一
    (tool_call_id, timestamp) 恒同一 ID，跨 archive / session 快照 / JSONL 一致；
    与 new_message_id() 同为 msg_ + 26hex 值空间，互不冲突。
    timestamp 参与哈希覆盖「同一 tool_call 未来一对多」场景。
    """
    digest = hashlib.sha256(f"{tool_call_id}:{timestamp}".encode("utf-8")).hexdigest()
    return MessageId("msg_" + digest[:26])


# 附件 MIME 规范化表（复验清单 §4.2 冻结）：(kind, 探测/历史别名) → 契约权威值。
# 探测值（filetype 容器判定）可作校验内部值，但 wire/持久化/下载必须与 kind 一致。
_ATTACHMENT_MIME_ALIASES = {
    ("audio", "video/webm"): "audio/webm",   # kind=audio 消歧后的容器探测值
    ("audio", "audio/x-wav"): "audio/wav",   # filetype 探测别名 → AudioMime 权威值
}


def normalize_attachment_mime(kind: str, mime: str) -> str:
    """附件记录 MIME 规范化：kind 确定后，探测/历史别名 → 契约权威值。

    写路径（上传）与读路径（消息恢复 / 下载 Content-Type）共用，保证同一附件
    各视角 mime 一致；未知别名原样返回（调用方决定回退/告警）。

    Args:
        kind: 附件最终 kind（image/video/audio/file）。
        mime: 待规范化的 MIME（探测值或历史记录值）。

    Returns:
        规范化后的 MIME；表中无匹配别名时原样返回。
    """
    return _ATTACHMENT_MIME_ALIASES.get((kind, mime), mime)
