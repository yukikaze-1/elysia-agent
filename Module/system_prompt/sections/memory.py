"""C4: 记忆。"""
import logging
import time
from typing import Any
from Module.system_prompt.protocol import Jinja2Section
logger = logging.getLogger(__name__)


class MemorySection(Jinja2Section):
    name = "Memory"
    version = "2.0.0"
    description = "她记得的过去 + 可靠性规则"

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
        limit = kwargs.get("memory_limit")
        if limit is not None:
            self._memory_limit = max(0, int(limit))

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
