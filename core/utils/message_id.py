"""消息 ID 生成。"""
from __future__ import annotations

import hashlib
import uuid

from core.schema.types import AttachmentId, GenerationId, MemoryId, MessageId, SegmentId, TaskId


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
