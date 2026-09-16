"""C4: 记忆。"""
import logging
import time
from collections.abc import Callable
from typing import Any
from Module.system_prompt.protocol import Jinja2Section
logger = logging.getLogger(__name__)


class MemorySection(Jinja2Section):
    name = "Memory"
    version = "2.0.0"
    description = "她记得的过去 + 可靠性规则"

    #: opt-in：允许 `SystemPromptManager.render(budget_tokens=…)` 在**本轮 render 内**
    #: 临时下调记忆条数上限。它不是 per-request 上下文（`set_context` 的正式入参里
    #: 没有它），故只能经 `override_for_render` 进入——否则一次广播会连带清空别的字段。
    render_override_keys = frozenset({"memory_limit"})

    def __init__(self, template_path: str, long_term: Any = None, session: Any = None):
        """初始化 Memory section。

        Args:
            template_path: 模板文件路径。
            long_term: LongTermMemory 实例，用于搜索相关记忆。
            session: Session 实例。
        """
        super().__init__(template_path)
        self._long_term = long_term
        self._session = session
        self._user_message = ""
        self._memory_limit: int | None = None  # None = 用 search 的默认 top_k

    def set_context(self, **kwargs: Any) -> None:
        self._user_message = kwargs.get("user_message", "")
        # S2-H9：超预算时由 SystemPromptManager 逐级下调（5 → 3 → 2 → 1 → 0）。
        # 0 = 本节渲染为空（"这轮先不想起什么"），不是"渲染失败"。
        #
        # 注意：这里的 `limit is None → 不改` 是**刻意**的（正常 per-request 调用不带
        # memory_limit，不该把上一轮的降级带过来）。但预算梯子**不再走本方法**——它
        # 只带一个 key，会按"缺键 = 复位默认值"清掉 user_message 等字段（Issue A）。
        # 梯子走 `override_for_render`，见 `SystemPromptManager.render`。
        limit = kwargs.get("memory_limit")
        if limit is not None:
            self._memory_limit = max(0, int(limit))

    def override_for_render(self, key: str, value: Any) -> Callable[[], None] | None:
        """临时下调本轮记忆条数上限，返回恢复回调。

        覆盖只在 `SystemPromptManager.render` 的本次调用内有效：管理器在 `finally`
        里回调恢复（含 section 的 `render()` 抛异常时），故下一轮 render 拿到的仍是
        覆盖前的值（`None` = 用 search 的默认 top_k=5）。

        Args:
            key: 覆盖键；本 section 仅支持 "memory_limit"。
            value: 本轮记忆条数上限（负数按 0 处理）。

        Returns:
            恢复回调；key 不受支持时返回 None。
        """
        if key != "memory_limit":
            return None
        previous = self._memory_limit
        self._memory_limit = max(0, int(value))

        def _restore() -> None:
            self._memory_limit = previous

        return _restore

    async def render(self) -> str:
        if self._long_term is None or not self._user_message:
            return ""
        if self._memory_limit == 0:
            return ""
        try:
            top_k = 5 if self._memory_limit is None else self._memory_limit
            results = await self._long_term.search(query_text=self._user_message, top_k=top_k)
        except Exception:
            logger.warning("C4 Memory: LTM 搜索失败，跳过记忆注入", exc_info=True)
            return ""
        if not results:
            return ""
        now = time.time()
        micro = []
        for entry in results:
            micro.append({
                # S2-H2（与 CR-12 同根）：记忆文本源自用户对话经 LLM 提取，属不可信
                # 输入。转义收口在 `_render_template`（递归进 dict/list 叶子）。
                "content": entry.text,
                "type": entry.memory_type,
                "timestamp": entry.timestamp if entry.timestamp else now,
                "poignancy": entry.poignancy,
            })
        return self._render_template(memory_ctx={"micro": micro, "macro": []})
