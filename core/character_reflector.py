"""CharacterReflector — 周期性 LLM 自我反思，输出 self_perception。

缓冲对话轮次，攒够 50 对 OR 距上次反思 > 24h 时触发反思。
输出自然语言 self_perception，存入 session.params["self_perception_history"]。

遵循 MemoryConsolidator 的 buffer + asyncio.Lock + flush 模式。
"""

import asyncio
import logging
import time

from Module.prompt import PromptManager
from .llm_adapter import LLMAdapter
from .memory.session import SessionMemory

logger = logging.getLogger(__name__)


class CharacterReflector:
    """周期性格自我反思。

    缓冲对话 → 触发 LLM 反思 → 输出 self_perception → 存入 session。
    """

    BUFFER_THRESHOLD = 50       # 攒够 50 对对话触发
    TIME_THRESHOLD_SEC = 86400  # 距上次反思超过 24h 也触发
    MAX_HISTORY = 50            # self_perception_history 上限
    CONTEXT_ROUNDS = 30         # 反思时取最近 N 轮对话上下文

    def __init__(
        self,
        llm: LLMAdapter,
        session: SessionMemory,
        character_name: str = "Elysia",
        user_name: str = "妖梦",
        prompt_dir: str = "prompt",
    ):
        self._llm = llm
        self._session = session
        self._character_name = character_name
        self._user_name = user_name
        self._prompt_dir = prompt_dir
        self._buffer: list[tuple[str, str]] = []  # (user_msg, assistant_msg)
        self._reflect_lock = asyncio.Lock()
        self._context: list[dict] = []  # 滚动窗口：保留最近 N 轮对话用于反思
        try:
            self._pm = PromptManager(self._prompt_dir)
        except FileNotFoundError:
            logger.warning("CharacterReflector: 模板目录不存在，反思功能不可用")
            self._pm = None

        # ---- debug 状态 ----
        self._last_reflection_at: float | None = None
        self._last_success: bool = False
        self._last_duration_ms: float = 0.0
        self._last_text_len: int = 0
        self._last_prompt_system: str = ""
        self._last_prompt_user: str = ""

    async def buffer(self, user_msg: str, assistant_msg: str) -> None:
        """缓冲一轮对话。达到阈值时触发反思。"""
        if not assistant_msg.strip() or not user_msg.strip():
            return

        self._buffer.append((user_msg, assistant_msg))
        # 滚动窗口保留最近 N 轮
        self._context.append({"role": self._user_name, "content": user_msg, "inner_voice": ""})
        self._context.append({"role": self._character_name, "content": assistant_msg, "inner_voice": ""})
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
        """强制反思剩余缓冲（shutdown 时调用）。"""
        if not self._buffer:
            return

        if self._reflect_lock.locked():
            logger.debug("CharacterReflector flush: 反思已在进行中")
            return

        async with self._reflect_lock:
            await self._reflect()

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
        此偏差在 v1 中可接受。
        """
        if len(self._buffer) >= self.BUFFER_THRESHOLD:
            return True
        history = self._session.get_param("self_perception_history", [])
        if not history:
            return len(self._buffer) >= 10  # 首次：10 对即可触发（有上下文基础）
        last_ts = history[-1].get("timestamp", 0)
        return (time.time() - last_ts) > self.TIME_THRESHOLD_SEC

    async def _reflect(self) -> None:
        """执行 LLM 反思并存储结果。"""
        if self._pm is None:
            return

        pm = self._pm

        history = self._session.get_param("self_perception_history", [])
        last_perception = history[-1]["content"] if history else ""

        system = pm.render_macro(
            "CharacterReflector.j2", "CharacterReflectorSystemPrompt",
            character_name=self._character_name,
            user_name=self._user_name,
        )
        user_prompt = pm.render_macro(
            "CharacterReflector.j2", "CharacterReflectorUserPrompt",
            character_name=self._character_name,
            user_name=self._user_name,
            last_self_perception=last_perception,
            recent_conversations=self._context,
        )

        # 记录 prompt（截断到 1000 字符）
        self._last_prompt_system = system[:1000]
        self._last_prompt_user = user_prompt[:1000]

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
        except Exception:
            self._last_duration_ms = (time.perf_counter() - t0) * 1000
            self._last_success = False
            self._last_reflection_at = time.time()
            logger.warning("CharacterReflector: LLM 调用失败", exc_info=True)
            return

        elapsed_ms = (time.perf_counter() - t0) * 1000

        # LLM 调用成功后才清空 buffer — 若失败则保留计数，下次攒够再触
        self._buffer.clear()
        # _context 不清空 — 滚动窗口 [-(CONTEXT_ROUNDS*2):] 已有上界，
        # 下次反思触发时（50 轮后）旧条目已自然推出。清空会丢失 LLM 调用期间
        # buffer() 新到达的消息。

        if not text:
            self._last_duration_ms = elapsed_ms
            self._last_success = True
            self._last_text_len = 0
            self._last_reflection_at = time.time()
            logger.debug("CharacterReflector: 反思结果为空，跳过")
            return

        # 追加到历史
        entry = {"timestamp": time.time(), "content": text}
        current_history = self._session.get_param("self_perception_history", [])
        current_history.append(entry)
        if len(current_history) > self.MAX_HISTORY:
            current_history = current_history[-self.MAX_HISTORY:]
        self._session.set_param("self_perception_history", current_history)

        # 记录 debug 元数据
        self._last_success = True
        self._last_duration_ms = elapsed_ms
        self._last_text_len = len(text)
        self._last_reflection_at = time.time()

        logger.info("CharacterReflector: 性格反思完成 (%d chars, %.0f ms)",
                    len(text), elapsed_ms)
