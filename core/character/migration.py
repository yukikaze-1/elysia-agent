"""旧会话 CharacterState shadow migration（架构稿 §15.3）。

为每个旧 session 建确定性 legacy instance（sessionId 哈希），state 原样复制到
instance.state；session 加 character_binding 指向 legacy instance；旧会话行为/状态不变。
"""

from __future__ import annotations

import hashlib


def legacy_instance_id(session_id: str) -> str:
    """由 sessionId 哈希生成确定性 legacy instance id。

    Args:
        session_id: 会话 ID（旧会话以 session_id 为角色状态身份）。

    Returns:
        确定性 legacy instance id，前缀 ``chi_legacy_``。
    """
    h = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]
    return f"chi_legacy_{h}"


def migrate_legacy_sessions(sessions: list) -> list[dict]:
    """为每个旧 session 建 deterministic legacy instance；返回迁移记录。

    Args:
        sessions: SessionMemory 列表（旧会话，character_state 以 session_id 为身份）。

    Returns:
        迁移记录 list[dict]，每项含 session_id / legacy_instance_id / character_id。
    """
    records = []
    for session in sessions:
        sid = session.session_id
        iid = legacy_instance_id(sid)
        # 每旧 session 独立 legacy instance，state 原样（不合并）
        session.character_binding = {"instance_id": iid, "character_id": "legacy"}
        records.append({"session_id": sid, "legacy_instance_id": iid, "character_id": "legacy"})
    return records
