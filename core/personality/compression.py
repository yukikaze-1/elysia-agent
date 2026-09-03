"""CompressionManager — L3→L2 性格压缩 + C6 说话风格刷新。

触发条件:
  1. state_history 新增 >= COMPRESSION_COUNT_THRESHOLD 条
  2. 距上次压缩 > COMPRESSION_TIME_THRESHOLD 秒
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any, TYPE_CHECKING

from core.schema.compression import CompressionTrigger
from core.schema.utils import now_iso
from core.personality._utils import _extract_tag
from paths import ProjectPath

if TYPE_CHECKING:
    from core.adapter.llm_adapter import LLMAdapter
    from core.config.character_config import CharacterConfig
    from core.schema.character import CharacterState, StateHistoryEntry
    from core.storage.character_version_repo import CharacterVersionRepository
    from Module.system_prompt import PromptManager

logger = logging.getLogger(__name__)

# L3→L2 压缩配置
COMPRESSION_COUNT_THRESHOLD = 5    # state_history 新增 N 条触发压缩
COMPRESSION_TIME_THRESHOLD = 604800  # 距上次压缩 > 7 天也触发
COMPRESSION_MIN_ENTRIES = 3        # 首次压缩最少需要 state_history 条目数


class CompressionManager:
    """L3→L2 性格压缩 + C6 说话风格刷新。

    _refresh_c6 通过 _refresh_cb 回调参数与 CharacterEngine 的委托方法连接，
    确保测试中 mock engine._refresh_c6 时能正确拦截。
    """

    def __init__(
        self,
        *,
        llm: "LLMAdapter",
        state: "CharacterState",
        character_name: str,
        l1_core: str,
        pm: "PromptManager | None",
        version_repo: "CharacterVersionRepository | None",
        character_config: CharacterConfig,  # CharacterConfig — 拿 emotion.idle_expressions / speaking_style
    ):
        """初始化压缩管理器。

        Args:
            llm: LLM 适配器。
            state: 角色状态对象。
            character_name: 角色名。
            l1_core: L1 核心性格文本。
            pm: PromptManager 实例。
            version_repo: 角色版本仓库，用于 L2/C6 版本历史持久化。
            character_config: CharacterConfig 配置对象。
        """
        self._llm = llm
        self._state = state
        self._character_name = character_name
        self._l1_core = l1_core
        self._pm = pm
        self._version_repo = version_repo
        self._character = character_config
        self._compression_lock = asyncio.Lock()

    # ---- 公共接口 ----

    async def check_and_compress(
        self, *, _refresh_cb: "Callable[[], Coroutine[Any, Any, None]] | None" = None,
    ) -> None:
        """检查压缩条件，满足时触发压缩。带双重检查锁。

        Args:
            _refresh_cb: 压缩完成后调用的 C6 刷新回调。由 engine 委托方法传入，
                确保 mock 能正确拦截 C6 刷新。
        """
        if self._pm is None:
            return
        if not self._should_compress():
            return
        if self._compression_lock.locked():
            logger.debug("CompressionManager: 压缩已在运行中，跳过")
            return

        async with self._compression_lock:
            # 双重检查 — 上一个压缩可能刚好完成了
            if not self._should_compress():
                return
            await self._compress(_refresh_cb=_refresh_cb)

    async def flush(self) -> None:
        """Shutdown 时强制压缩（如有未处理条目）。带超时保护。"""
        if self._pm is None:
            return
        history = self._state.state_history
        last_count: int = self._state.compression_last_count
        new_count = len(history) - last_count
        if new_count < COMPRESSION_MIN_ENTRIES:
            logger.debug("CompressionManager flush: 仅 %d 新条目，跳过压缩", new_count)
            return
        if self._compression_lock.locked():
            logger.debug("CompressionManager flush: 压缩已在运行中")
            return

        async with self._compression_lock:
            try:
                await asyncio.wait_for(
                    self._compress(trigger=CompressionTrigger.FLUSH), timeout=5.0)
                await self._refresh_c6()
            except asyncio.TimeoutError:
                logger.warning("CompressionManager: 压缩 flush 超时")
            except Exception:
                logger.warning("CompressionManager: 压缩 flush 失败", exc_info=True)

    # ---- 内部方法（private，但 engine 委托方法暴露给测试）----

    def _should_compress(self) -> bool:
        """检查是否应触发 L3→L2 压缩。

        触发条件（任一满足）:
        1. state_history 新增 >= COMPRESSION_COUNT_THRESHOLD 条
        2. 距上次压缩 > COMPRESSION_TIME_THRESHOLD 秒

        同时要求 state_history 至少有 COMPRESSION_MIN_ENTRIES 条。

        Returns:
            True 表示满足压缩触发条件。
        """
        history = self._state.state_history
        total = len(history)
        if total < COMPRESSION_MIN_ENTRIES:
            return False

        last_count: int = self._state.compression_last_count
        last_at = self._state.compression_last_at or 0.0

        # 计数触发：新增 >= 阈值
        new_count = total - last_count
        if new_count >= COMPRESSION_COUNT_THRESHOLD:
            logger.debug("CompressionManager: 计数触发压缩 (%d 新条目)", new_count)
            return True

        # 时间触发：距上次 > 阈值（仅限已有压缩记录时）
        if last_at > 0 and (time.time() - last_at) >= COMPRESSION_TIME_THRESHOLD:
            logger.debug("CompressionManager: 时间触发压缩 (%.1f 天)",
                         (time.time() - last_at) / 86400)
            return True

        return False

    def _get_new_entries(self) -> "list[StateHistoryEntry]":
        """取上次压缩后新增的 state_history 条目。

        Returns:
            上次压缩后新增的 StateHistoryEntry 列表。
        """
        history = self._state.state_history
        last_count: int = self._state.compression_last_count
        return history[last_count:]

    async def _compress(
        self,
        trigger: CompressionTrigger = CompressionTrigger.COMPRESSION,
        *,
        _refresh_cb: "Callable[[], Coroutine[Any, Any, None]] | None" = None,
    ) -> None:
        """执行 L3→L2 压缩：渲染模板 → 调 LLM → 解析 <new_self> + <changes> → 写版本历史。

        Args:
            trigger: 压缩触发类型（COMPRESSION / FLUSH）。
            _refresh_cb: 写版本历史后调用的 C6 刷新回调。
                由 engine._compress 委托方法或 check_and_compress 传入；
                flush 路径不传，单独调用 _refresh_c6。
        """
        if self._pm is None:
            return

        current_self = self._state.self_l2
        new_entries = self._get_new_entries()
        history = self._state.state_history

        logger.info("CompressionManager: 开始 L3→L2 压缩 (%d 新条目, 当前 L2 %d chars)",
                    len(new_entries), len(current_self))

        try:
            system = self._pm.render_macro(
                "PersonalityCompression.j2", "CompressionSystemPrompt",
                character_name=self._character_name,
                l1_core=self._l1_core,
            )
            user_prompt = self._pm.render_macro(
                "PersonalityCompression.j2", "CompressionUserPrompt",
                character_name=self._character_name,
                current_self=current_self,
                recent_changes=new_entries,
            )
        except Exception:
            logger.warning("CompressionManager: 压缩模板渲染失败", exc_info=True)
            return

        t0 = time.perf_counter()
        try:
            result = await self._llm.think(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
                extra_body={"thinking": {"type": "disabled"}},
            )
            text = result.text.strip()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("CompressionManager: 压缩 LLM 调用失败 — 下次重试", exc_info=True)
            return

        elapsed_ms = (time.perf_counter() - t0) * 1000

        new_self = _extract_tag(text, "new_self")
        if not new_self:
            logger.warning("CompressionManager: 压缩 LLM 返回无 <new_self>")
            # 仍更新追踪，避免重复在同一批条目上重试
            self._state.compression_last_at = time.time()
            self._state.compression_last_count = len(history)
            return

        changes_raw = _extract_tag(text, "changes")
        changes = [c.strip("- ") for c in changes_raw.strip().split("\n") if c.strip()]

        # 更新 L2
        self._state.self_l2 = new_self
        self._state.compression_last_at = time.time()
        self._state.compression_last_count = len(history)

        logger.info("CompressionManager: L3→L2 压缩完成 (%d chars, %.0f ms, %d 条改动)",
                    len(new_self), elapsed_ms, len(changes))

        # 写版本历史
        if self._version_repo is not None:
            await self._version_repo.append_version(
                session_id=self._state.session_id,
                text=new_self,
                timestamp=now_iso(),
                trigger=trigger,
                change_summary=changes,
            )
        else:
            logger.info("CompressionManager: version_repo 未配置，版本历史未保存")

        # 触发 C6 刷新（通过回调，确保 engine 委托方法的 mock 可拦截）
        if _refresh_cb is not None:
            await _refresh_cb()

    async def _refresh_c6(self) -> None:
        """L2 更新后刷新 C6 说话风格。LLM 编辑模式 + 版本历史。失败不阻塞。"""
        if self._pm is None:
            return

        # 1. 获取当前 C6 文本
        current = self._state.speaking_style_c6
        if not current:
            # 首次：渲染 L1 默认模板获取基准文本
            try:
                from Module.system_prompt.sections.speaking_style import SpeakingStyleSection, _C6_L1_DEFAULT
                speaking_style = self._character.speaking_style or _C6_L1_DEFAULT
                sec = SpeakingStyleSection(
                    str(ProjectPath.SYSTEM_PROMPT_TEMPLATES.resolve() / "c6_speaking_style.j2"),
                    speaking_style=speaking_style,
                )
                sec.set_context(user_name="")
                current = await sec.render()
            except Exception:
                logger.warning("CompressionManager: C6 默认模板渲染失败，跳过刷新")
                return

        if not current.strip():
            return

        # 2. 渲染 prompt
        try:
            prompt = self._pm.render(
                "SpeakingStyleEdit.j2",
                character_name=self._character_name,
                self_l2=self._state.self_l2,
                current_speaking_style=current,
            )
        except Exception:
            logger.warning("CompressionManager: C6 prompt 渲染失败", exc_info=True)
            return

        # 3. LLM 调用
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
            logger.warning("CompressionManager: C6 refresh LLM 调用失败", exc_info=True)
            return

        # 4. 解析 XML 标签
        new_style = _extract_tag(text, "new_speaking_style")
        if not new_style:
            logger.warning("CompressionManager: C6 refresh <new_speaking_style> 解析失败")
            return

        changes_raw = _extract_tag(text, "changes")
        changes = [l.strip("- ").strip() for l in (changes_raw or "").splitlines() if l.strip()]

        elapsed_ms = (time.perf_counter() - t0) * 1000

        # 5. 更新 state
        self._state.speaking_style_c6 = new_style

        logger.info("CompressionManager: C6 刷新完成 (%d chars, %.0f ms, %d 条改动)",
                    len(new_style), elapsed_ms, len(changes))

        # 6. 写版本历史
        if self._version_repo is not None:
            await self._version_repo.append_version(
                session_id=self._state.session_id,
                text=new_style,
                timestamp=now_iso(),
                trigger=CompressionTrigger.C6_REFRESH,
                change_summary=changes,
                content_type="c6",
            )
