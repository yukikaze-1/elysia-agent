"""写入期角色完整性兜底 — assistant 消息缺角色快照时的显著告警。

## 为什么需要

`character_profiles=true` 是全局能力开关，前端据此要求「每条 assistant 消息都带角色
身份快照」，缺失即对消息历史**整批 fail-closed**。若缺口静默落库，故障只会在前端整页
拒绝时暴露（问题单 §5.2 ★★☆ 建议的第二道防线）。

## 设计取舍：告警但照常写入

归档是 append-only 永久日志（§13：message_archive 只增不删）——**丢弃比留下缺口更糟**，
因为历史不可恢复。故本兜底只负责**可见性**：一旦缺快照就发显著 warning，便于巡检发现；
真正的拒写/修复在源头（工具轮漏传已于 P3-1 修复）。

开启条件由调用方注入（`require_assistant_character`）——能力关闭时缺快照是 legacy 合法
状态，不应告警。
"""

from __future__ import annotations

import logging
from typing import Iterable

from core.schema.message import AssistantMessage, Message

logger = logging.getLogger(__name__)


def is_assistant_without_character(msg: object) -> bool:
    """该消息是否为「assistant 且缺角色身份快照」。

    user/tool 消息本无快照要求 → 恒 False。空正文（工具轮常态）不影响判定——缺的只是
    快照本身。
    """
    return isinstance(msg, AssistantMessage) and msg.character is None


def count_assistant_without_character(messages: Iterable[Message]) -> int:
    """统计一批消息里缺角色快照的 assistant 条数。"""
    return sum(1 for m in messages if is_assistant_without_character(m))


def warn_missing_character_snapshot(
    *, collection: str, session_id: str, missing: int, total: int,
) -> None:
    """发出显著告警（调用方须先确认 missing > 0）。

    多条缺口合并为**一条**告警并报数量——批量 save 时不刷日志。

    Args:
        collection: 落库目标集合名（message_archive / sessions）。
        session_id: 所属会话 ID（定位用）。
        missing: 缺快照的 assistant 条数。
        total: 本次落库消息总条数（提供比例上下文）。
    """
    logger.warning(
        "\n"
        "==================================================================\n"
        "  写入期角色完整性告警（character_profiles=true 已生效）\n"
        "    · 目标集合 : %s\n"
        "    · 会话     : %s\n"
        "    · 缺快照的 assistant 消息: %d / %d 条\n"
        "  该记录已照常落库（归档为 append-only 永久日志，丢弃会不可恢复），但前端对\n"
        "  消息历史实行整批 fail-closed——这类记录会让整个会话历史不可读。\n"
        "  排查：源头写入点应传 character=self._turn_character_snapshot()（工具轮漏传\n"
        "  已于 P3-1 修复）；存量可用 scripts/backfill_character_snapshots.py 回填。\n"
        "==================================================================",
        collection, session_id, missing, total,
    )
