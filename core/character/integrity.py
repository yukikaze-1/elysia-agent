"""多角色数据完整性自检（启动期告警）。

## 背景

`character_profiles=true` 是**全局能力开关**：一旦为真，前端就要求「全库每条会话都
带角色绑定、每条 assistant 消息都带角色身份快照」，并以此为 fail-closed 前提——任一
条不合格即**整页/整批拒绝**（避免「游标推进但静默丢历史」）。

后端此前没有任何环节检查该前提是否成立，于是历史数据、测试残留、异常中断产生的记录
会直接抵达前端，导致**单个坏记录 → 整个多角色功能面不可用**（见
`docs/review/2026-09-10-多角色数据完整性缺口导致前端整页拒绝-后端.md`：
50 条空绑定会话 + 14 条缺角色戳 assistant 消息各自触发一次整页拒绝）。

本模块把该故障从「用户报障」提前到「启动日志」：启动时统计两类缺口并显著告警。

## 语义

自检是**只读 + 建议性**的：绝不修改数据、绝不让启动失败。存在缺口时告警并给出处置
方向，是否修复由运维决定（补戳需按会话真实绑定，不能伪造归属）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CharacterIntegrityReport:
    """多角色数据完整性自检结果。"""

    sessions_missing_binding: int = 0
    """缺少/不完整 `character_binding` 的会话数。"""

    assistant_messages_missing_character: int = 0
    """缺少角色身份快照的 assistant 归档消息数。"""

    scanned: bool = False
    """是否真正完成了扫描。

    False 表示未扫描（能力开关关闭，或存储不可用 / 查询异常）——与「扫描后 0 缺口」
    严格区分，避免把「没查」误报成「数据健康」。
    """

    @property
    def has_gaps(self) -> bool:
        """是否发现数据缺口（未扫描时恒 False——不能断言有缺口）。"""
        return self.sessions_missing_binding > 0 or self.assistant_messages_missing_character > 0

    def summary(self) -> str:
        """人类可读摘要（供日志/调试面板）。"""
        if not self.scanned:
            return "多角色数据完整性自检未执行（能力关闭或存储不可用）"
        return (
            f"会话缺角色绑定 {self.sessions_missing_binding} 条；"
            f"assistant 消息缺角色快照 {self.assistant_messages_missing_character} 条"
        )


async def check_character_data_integrity(
    *,
    enabled: bool,
    session_repo: object | None,
    message_repo: object | None,
) -> CharacterIntegrityReport:
    """统计多角色数据缺口（只读，绝不抛异常）。

    Args:
        enabled: `character_profiles` 能力开关。关闭时不扫描（缺口对前端无害）。
        session_repo: SessionRepository（需有 count_incomplete_character_binding）。
        message_repo: MessageRepository（需有 count_assistant_without_character）。

    Returns:
        CharacterIntegrityReport。任何异常（存储不可用、查询失败、repo 缺失）都
        降级为 `scanned=False`，不向上抛——启动不能因自检失败而中断。
    """
    if not enabled:
        return CharacterIntegrityReport(scanned=False)

    sessions_getter = getattr(session_repo, "count_incomplete_character_binding", None)
    messages_getter = getattr(message_repo, "count_assistant_without_character", None)
    if sessions_getter is None or messages_getter is None:
        logger.debug("多角色完整性自检跳过：repo 未提供计数接口")
        return CharacterIntegrityReport(scanned=False)

    try:
        sessions_missing = await sessions_getter()
        messages_missing = await messages_getter()
    except Exception:
        # 存储不可用是常态（Mongo 可离线启动），不升级为错误
        logger.debug("多角色完整性自检未执行（存储不可用或查询失败）", exc_info=True)
        return CharacterIntegrityReport(scanned=False)

    return CharacterIntegrityReport(
        sessions_missing_binding=max(int(sessions_missing), 0),
        assistant_messages_missing_character=max(int(messages_missing), 0),
        scanned=True,
    )


def log_integrity_report(report: CharacterIntegrityReport) -> None:
    """把自检结果写入日志：无缺口 debug，有缺口显著 warning。"""
    if not report.scanned:
        logger.debug("%s", report.summary())
        return
    if not report.has_gaps:
        logger.debug("多角色数据完整性自检通过：无缺口")
        return
    logger.warning(
        "\n"
        "==================================================================\n"
        "  多角色数据完整性告警（character_profiles=true 已生效）\n"
        "    · 会话缺角色绑定            : %d 条\n"
        "    · assistant 消息缺角色快照  : %d 条\n"
        "  前端对会话列表/消息历史实行整页 fail-closed——上述任一类记录都会导致\n"
        "  整个多角色功能面不可用（列表空白、无法新建会话、聊天区空白）。\n"
        "  处置：按会话真实 character_binding 补齐缺失戳（不得伪造归属），或删除\n"
        "  无绑定的测试残留会话；详见 docs/review/2026-09-10-多角色数据完整性\n"
        "  缺口导致前端整页拒绝-后端.md §7 现场处置记录。\n"
        "==================================================================",
        report.sessions_missing_binding,
        report.assistant_messages_missing_character,
    )
