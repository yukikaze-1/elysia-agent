"""AwarenessJudge — LLM 语义判定 L2 vs L3，管理 C9 觉察文本缓存。

三重门控: 缓存存在 → 展示次数未达上限 → 冷却时间已过。
"""
import asyncio
import logging
import time
from typing import TYPE_CHECKING

from core.personality._utils import _extract_tag, _schedule

if TYPE_CHECKING:
    from core.adapter.llm_adapter import LLMAdapter
    from core.schema.character import CharacterRuntimeState
    from Module.system_prompt import PromptManager

logger = logging.getLogger(__name__)

# AwarenessJudge 配置
AWARENESS_JUDGMENT_COOLDOWN = 300  # 最小展示冷却 5 分钟
AWARENESS_MAX_SHOWS = 3            # 单条文本最大展示次数


class AwarenessJudge:
    """LLM 语义判定 — 比较 L2 Self vs L3 反思，批准或跳过 C9 觉察文本。"""

    def __init__(
        self,
        *,
        llm: "LLMAdapter",
        state: "CharacterRuntimeState",
        character_name: str,
        user_name: str,
        pm: "PromptManager | None",
    ):
        """初始化觉察判定器。

        Args:
            llm: LLM 适配器。
            state: 角色状态对象。
            character_name: 角色名。
            user_name: 用户名。
            pm: PromptManager 实例，用于模板渲染。
        """
        self._llm = llm
        self._state = state
        self._character_name = character_name
        self._user_name = user_name
        self._pm = pm

        # 冷却计时: 记录上次 C9 渲染的时间戳
        self._last_awareness_time: float = state.last_awareness_time or 0.0

    # ---- 公共接口 ----

    @property
    def last_awareness_time(self) -> float:
        """上次 C9 渲染时间戳（0.0 = 尚未渲染过）。"""
        return self._last_awareness_time

    @last_awareness_time.setter
    def last_awareness_time(self, value: float | None) -> None:
        """恢复/复位冷却计时（`None` 归零）。

        公开面：`CharacterEngine.restore_evolution_state()` 与
        `reset_evolution_state()` 经此读写，消费方不再直接戳 `_last_awareness_time`。
        """
        self._last_awareness_time = value or 0.0

    def is_allowed(self) -> bool:
        """判断当前是否允许 C9 渲染（缓存 + 展示次数 + 冷却三重门控）。

        必须同时满足：
        1. 有 judge 批准的觉察文本
        2. 展示次数未达上限
        3. 距上次 C9 渲染超过冷却时间（首次不受冷却限制）

        Returns:
            True 表示允许展示 C9 觉察文本。
        """
        if not self._state.approved_awareness_text:
            return False

        if self._state.awareness_show_count >= AWARENESS_MAX_SHOWS:
            return False

        if self._last_awareness_time > 0:
            if (time.time() - self._last_awareness_time) < AWARENESS_JUDGMENT_COOLDOWN:
                return False

        return True

    def maybe_judge(self) -> None:
        """触发异步 awareness 判定（fire-and-forget）。

        捕获当前 L3 snapshot 以避免后续 L3 更新的竞态。
        L3 为空时跳过。
        """
        l3_snapshot = self._state.state_l3
        if not l3_snapshot:
            return
        _schedule(self._judge(l3_snapshot))

    # ---- 私有实现 ----

    async def _judge(self, l3_text: str) -> None:
        """执行 LLM 判定：比较 L2 vs L3，更新 awareness 缓存。

        L2 为空 → 降级模式：L3 原文直接缓存，不调 LLM。
        L2 非空 → render prompt → call LLM → parse <approved>/<skip>。
        失败时保留旧缓存并记录 warning。

        Args:
            l3_text: 当前 L3 状态文本（来自 reflector 产出）。
        """
        if self._pm is None:
            return

        l2_self = self._state.self_l2
        if not l2_self:
            # 降级模式：无 L2 时直接缓存 L3 原文
            self._state.approved_awareness_text = l3_text
            self._state.approved_awareness_at = time.time()
            self._state.awareness_show_count = 0
            logger.debug("AwarenessJudge: L2 为空，降级使用 L3 原文 (%d chars)", len(l3_text))
            return

        last_approved = self._state.approved_awareness_text

        # 1. 渲染 prompt
        try:
            prompt = self._pm.render(
                "AwarenessJudge.j2",
                character_name=self._character_name,
                l2_self=l2_self,
                l3_reflection=l3_text,
                last_approved=last_approved,
            )
        except Exception:
            logger.warning("AwarenessJudge: 模板渲染失败", exc_info=True)
            return

        # 2. LLM 调用
        t0 = time.perf_counter()
        try:
            result = await self._llm.think(
                [{"role": "user", "content": prompt}],
                extra_body={"thinking": {"type": "disabled"}},
            )
            text = result.text.strip()
        except asyncio.CancelledError:
            raise
        except Exception:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.warning("AwarenessJudge: LLM 调用失败 (%.0f ms)", elapsed_ms, exc_info=True)
            return

        elapsed_ms = (time.perf_counter() - t0) * 1000

        # 3. 解析结果
        approved = _extract_tag(text, "approved")
        if approved:
            self._state.approved_awareness_text = approved
            self._state.approved_awareness_at = time.time()
            self._state.awareness_show_count = 0
            logger.info("AwarenessJudge: 批准觉察文本 (%d chars, %.0f ms)", len(approved), elapsed_ms)
            return

        skip_reason = _extract_tag(text, "skip")
        logger.debug("AwarenessJudge: 跳过 — %s (%.0f ms)",
                     skip_reason or "无原因", elapsed_ms)
        # 保留旧缓存不变
