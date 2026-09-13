"""CharacterReflector — 周期性 LLM 自我反思，作为 CharacterEngine 的子组件。

缓冲对话轮次，攒够 50 对 OR 距上次反思 > 24h 时触发反思。
反思结果通过 engine.consume_trigger 提交给 CharacterEngine。

遵循 MemoryConsolidator 的 buffer + asyncio.Lock + flush 模式。
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from paths import ProjectPath
from Module.system_prompt import PromptManager
from Module._common.task_scope import AsyncTaskScope
from core.adapter.llm_adapter import LLMAdapter

if TYPE_CHECKING:
    from core.personality.engine import CharacterEngine

logger = logging.getLogger(__name__)


def _coerce_timestamp(value: object) -> float:
    """时间戳归一化为 Unix 浮点（S3-18）：兼容 ISO 8601 字符串与数字。

    解析不出来时返回 0（= 很久以前），让时间阈值判定**倾向于触发**——
    宁可多反思一次，也不要因为一条脏数据把反思永久卡住。

    Args:
        value: 时间戳（float/int/ISO 字符串/其它）。

    Returns:
        Unix 秒（float）；无法解析返回 0.0。
    """
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            from datetime import datetime

            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return 0.0
    return 0.0


class CharacterReflector:
    """周期性格自我反思。

    缓冲对话 → 触发 LLM 反思 → 输出 self_perception → 存入 engine.state。
    """

    BUFFER_THRESHOLD = 50       # 攒够 50 对对话触发
    TIME_THRESHOLD_SEC = 86400  # 距上次反思超过 24h 也触发
    #: `self_perception_history` 上限——**写入方在 engine**（S3-18 前该字段没有写入者，
    #: 这个常量因此从未被读过）。保留同名常量以免调用方硬编码数字。
    MAX_HISTORY = 50
    CONTEXT_ROUNDS = 30         # 反思时取最近 N 轮对话上下文

    def __init__(
        self,
        *,
        engine: "CharacterEngine",
        l1_core: str,
        llm: LLMAdapter,
        character_name: str = "Elysia",
        user_name: str = "妖梦",
        prompt_dir: str | None = None,
    ):
        """初始化角色反射器。

        Args:
            engine: 父 CharacterEngine 实例，用于 consume_trigger。
            l1_core: L1 核心性格文本。
            llm: LLM 适配器。
            character_name: 角色名。
            user_name: 用户名。
            prompt_dir: prompt 模板目录路径，None 时使用默认路径。
        """
        self._engine = engine
        self._l1_core = l1_core
        self._llm = llm
        self._character_name = character_name
        self._user_name = user_name
        self._prompt_dir = prompt_dir
        self._buffer: list[tuple[str, str]] = []  # (user_msg, assistant_msg)
        self._reflect_lock = asyncio.Lock()
        # ARCH-INC-P2-03：consume_trigger 提交任务作用域（强引用 + 异常观察 + 可 drain）
        self._consume_tasks = AsyncTaskScope(f"reflector:{character_name}")
        self._context: list[dict] = []  # 滚动窗口：保留最近 N 轮对话用于反思
        _dir = self._prompt_dir
        if _dir is None:
            _dir = str(ProjectPath.PROMPTS.resolve())
        try:
            self._pm = PromptManager(_dir)
        except FileNotFoundError:
            logger.warning("CharacterReflector: 模板目录不存在，反思功能不可用")
            self._pm = None

        # 从 engine.state 恢复 buffer/context（如果有持久化数据）
        self.restore_from_state()

        # ---- debug 状态 ----
        self._last_reflection_at: float | None = None
        self._last_success: bool = False
        self._last_duration_ms: float = 0.0
        self._last_text_len: int = 0
        self._last_prompt_system: str = ""
        self._last_prompt_user: str = ""

    def set_user_name(self, user_name: str) -> None:
        """投影本轮身份的有效称呼（P0-2）——对话上下文角色标签 + 反思 prompt。"""
        if user_name:
            self._user_name = user_name

    # ---- 状态恢复 / 复位 ----

    def restore_from_state(self) -> None:
        """从 engine.state 恢复 buffer 和 context。

        公开面（`CharacterReflectorProtocol`）：实例/session 切换后由
        `CharacterEngine.restore_evolution_state()` 调用，消费方不需要直接触达。
        """
        state = self._engine.state
        if state.reflector_buffer:
            self._buffer = [(u, a) for u, a in state.reflector_buffer]
            logger.debug("CharacterReflector: 恢复 buffer (%d 对)", len(self._buffer))
        if state.reflector_context:
            self._context = list(state.reflector_context)

    def reset_state(self) -> None:
        """复位缓冲、上下文与遥测（角色重置流程）。

        公开面（`CharacterReflectorProtocol`）：与 `restore_from_state` 互为逆操作。
        此前由 `core/character/service.py` 逐个戳 `_buffer`/`_context`/`_last_*`
        七个私有字段完成——那些字段的读侧本来就有公开属性，写侧这里补齐。
        """
        self._buffer.clear()
        self._context.clear()
        self._last_reflection_at = None
        self._last_success = False
        self._last_duration_ms = 0.0
        self._last_text_len = 0
        self._last_prompt_system = ""
        self._last_prompt_user = ""

    @property
    def in_flight(self) -> bool:
        """是否有反思在途（busy 判定用）。"""
        return self._reflect_lock.locked()

    # ---- buffer / flush ----

    async def buffer(self, user_msg: str, assistant_msg: str) -> None:
        """缓冲一轮对话。达到阈值时触发反思。

        Args:
            user_msg: 用户消息文本。
            assistant_msg: 助手回复文本。
        """
        if not assistant_msg.strip() or not user_msg.strip():
            return

        self._buffer.append((user_msg, assistant_msg))
        # 滚动窗口保留最近 N 轮（带时间戳，用于计算对话跨度）
        ts = time.time()
        self._context.append({"role": self._user_name, "content": user_msg, "inner_voice": "", "timestamp": ts})
        self._context.append({"role": self._character_name, "content": assistant_msg, "inner_voice": "", "timestamp": ts})
        if len(self._context) > self.CONTEXT_ROUNDS * 2:
            self._context = self._context[-(self.CONTEXT_ROUNDS * 2):]

        if not self._should_trigger():
            return

        if self._reflect_lock.locked():
            logger.debug("CharacterReflector: 上次反思仍在进行中，跳过本轮")
            return

        async with self._reflect_lock:
            await self._reflect()

    async def flush(self) -> None:
        """强制反思剩余缓冲（shutdown 时调用）。

        少量对话不强制反思——产出质量低且会清空 buffer，
        导致重启后丢失累积进度。留到下次启动继续攒。

        Returns:
            无返回值。结果通过 engine.consume_trigger 异步提交。
        """
        if not self._buffer:
            return

        if len(self._buffer) < 10:
            logger.debug("CharacterReflector flush: 仅 %d 对，留待下次", len(self._buffer))
            return

        if self._reflect_lock.locked():
            logger.debug("CharacterReflector flush: 反思已在进行中")
            return

        async with self._reflect_lock:
            await self._reflect()

    async def drain_consume_tasks(self, *, timeout: float = 3.0) -> int:
        """等待本 reflector 提交的 ``consume_trigger`` 后台任务落定（ARCH-INC-P2-03）。

        Args:
            timeout: 总预算（秒）。

        Returns:
            超时前未落定的任务数（0 = 全部完成）。
        """
        return await self._consume_tasks.drain(timeout=timeout)

    # ---- debug 属性 ----

    @property
    def buffer_size(self) -> int:
        return len(self._buffer)

    @property
    def locked(self) -> bool:
        return self._reflect_lock.locked()

    @property
    def last_reflection_at(self) -> float | None:
        return self._last_reflection_at

    @property
    def last_success(self) -> bool:
        return self._last_success

    @property
    def last_duration_ms(self) -> float:
        return self._last_duration_ms

    @property
    def last_text_len(self) -> int:
        return self._last_text_len

    @property
    def last_prompt_system(self) -> str:
        return self._last_prompt_system

    @property
    def last_prompt_user(self) -> str:
        return self._last_prompt_user

    # ---- private ----

    def _should_trigger(self) -> bool:
        """判断是否应触发反思：≥50 对 OR 距上次 > 24h。

        规范规定首次无历史时"视为已超 24h"，但纯时间触发会导致第 1 轮
        对话就立即反思——此时上下文不足以产生有意义的 self_perception。
        因此首次改用 10 对 buffer 阈值，确保有足够对话上下文再反思。

        Returns:
            True 表示满足触发条件，应执行反思。
        """
        if len(self._buffer) >= self.BUFFER_THRESHOLD:
            return True

        # 获取上次触发时间
        last_ts = self._last_reflection_at
        if last_ts is None:
            # 兼容旧路径：从 self_perception_history 读时间戳。
            # S3-18 起写入方用 `time.time()` 浮点（与 `_last_reflection_at` 同基准），
            # 但**旧数据可能是 ISO 字符串**——这里必须能解析，否则
            # `time.time() - "2026-..."` 直接抛 TypeError（`_reflect` 有兼容，
            # 本函数原先没有：同一份数据两处解析口径不一致）。
            history = self._engine.state.self_perception_history
            if not history:
                return len(self._buffer) >= 10
            last_ts = _coerce_timestamp(history[-1].get("timestamp", 0))

        return (time.time() - last_ts) > self.TIME_THRESHOLD_SEC

    async def _reflect(self) -> None:
        """执行 LLM 反思并存储结果。"""
        if self._pm is None:
            return

        logger.info("CharacterReflector: 开始反思 (buffer=%d pairs, context=%d rounds)",
                    len(self._buffer), len(self._context))

        # 记录反思开始时 buffer 大小——成功后只清此前累积的部分，
        # 反思期间新 append 的对话对（未进入本次 prompt）保留到下一轮
        _reflect_buffer_snapshot = len(self._buffer)

        pm = self._pm
        state = self._engine.state

        # ---- 收集上下文变量 ----

        # L1 Core: 不可变底色
        l1_core = self._l1_core

        # L2 Self: 长期自我认知
        l2_self = state.self_l2

        # L3 History: 近期状态变化轨迹
        l3_raw = state.state_history
        if l3_raw:
            l3_lines = []
            for entry in l3_raw[-5:]:  # 取最近 5 条
                ts = entry.get("timestamp", 0)
                content = entry.get("content", "")
                if ts:
                    from datetime import datetime
                    # 兼容两种格式: ISO 8601 字符串 (新) 或 Unix float (旧)
                    if isinstance(ts, str):
                        ts = datetime.fromisoformat(ts).timestamp()
                    dt = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
                    l3_lines.append(f"[{dt}] {content}")
                else:
                    l3_lines.append(f"- {content}")
            l3_history = "\n".join(l3_lines)
        else:
            l3_history = ""

        # L4 Mood: 当前情绪快照
        mood = state.mood
        mood_intensity = state.mood_intensity
        mood_snapshot = f"当前情绪: {mood} (强度: {mood_intensity:.2f})"

        # 上次反思结果
        history = state.self_perception_history
        last_perception = history[-1]["content"] if history else ""

        # 时间跨度
        time_span = "未知"
        buffer_size = len(self._buffer)
        if self._context:
            timestamps = [e.get("timestamp", 0) for e in self._context if e.get("timestamp")]
            if len(timestamps) >= 2:
                span_sec = timestamps[-1] - timestamps[0]
                if span_sec < 3600:
                    time_span = f"约 {int(span_sec / 60)} 分钟"
                elif span_sec < 86400:
                    time_span = f"约 {span_sec / 3600:.1f} 小时"
                else:
                    time_span = f"约 {span_sec / 86400:.1f} 天"

        # ---- 渲染模板 ----
        try:
            system = pm.render(
                "CharacterReflector.j2",
                character_name=self._character_name,
                user_name=self._user_name,
                l1_core=l1_core,
                l2_self=l2_self,
                l3_history=l3_history,
                last_self_perception=last_perception,
                mood_snapshot=mood_snapshot,
                time_span=time_span,
                buffer_size=buffer_size,
                recent_conversations=self._context,
            )
        except Exception:
            logger.warning("CharacterReflector: 模板渲染失败", exc_info=True)
            return

        # 记录 prompt（截断到 2000 字符）
        self._last_prompt_system = system[:2000]
        self._last_prompt_user = ""

        t0 = time.perf_counter()
        try:
            result = await self._llm.think(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": "请进行本次反思。"},
                ],
                extra_body={"thinking": {"type": "disabled"}},
            )
            text = result.text.strip()
        except Exception:
            self._last_duration_ms = (time.perf_counter() - t0) * 1000
            self._last_success = False
            self._last_reflection_at = time.time()
            logger.warning("CharacterReflector: LLM 调用失败", exc_info=True)
            return

        elapsed_ms = (time.perf_counter() - t0) * 1000

        # LLM 调用成功后才清空 buffer
        # 只清反思开始前累积的部分——反思期间新 append 的对话对（未进入 prompt）保留到下一轮
        del self._buffer[:_reflect_buffer_snapshot]
        self._context.clear()

        if not text:
            self._last_duration_ms = elapsed_ms
            self._last_success = True
            self._last_text_len = 0
            self._last_reflection_at = time.time()
            logger.debug("CharacterReflector: 反思结果为空，跳过")
            return

        async def _consume_guarded():
            try:
                await self._engine.consume_trigger("reflector", {"content": text})
            except Exception:
                logger.warning("CharacterReflector: consume_trigger 失败", exc_info=True)

        # ARCH-INC-P2-03：登记入 scope 而非裸 create_task——此前任务既无强引用也无人
        # 观察。这条路径**也会从关机流程自身进入**（_flush_reflector → flush() →
        # _reflect() → 此处），任务随即返回而 ``_stop_persistence`` 立刻关闭
        # version repo；scope 让 Agent 关机能 drain 它，避免「写向已关闭的 repo」。
        self._consume_tasks.try_spawn(_consume_guarded(), name="reflector-consume")
        self._last_success = True
        self._last_duration_ms = elapsed_ms
        self._last_text_len = len(text)
        self._last_reflection_at = time.time()
        logger.info("CharacterReflector: 性格反思已提交 → CharacterEngine (%d chars, %.0f ms)",
                    len(text), elapsed_ms)
