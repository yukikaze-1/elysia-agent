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

    def set_context(self, **kwargs: Any) -> None:
        self._user_message = kwargs.get("user_message", "")

    async def render(self) -> str:
        if self._long_term is None or not self._user_message:
            return ""
        try:
            results = await self._long_term.search(query_text=self._user_message, top_k=5)
        except Exception:
            logger.warning("C4 Memory: LTM 搜索失败，跳过记忆注入", exc_info=True)
            return ""
        if not results:
            return ""
        now = time.time()
        micro = []
        for entry in results:
            micro.append({
                "content": entry.text,
                "type": entry.memory_type,
                "timestamp": entry.timestamp if entry.timestamp else now,
                "poignancy": entry.poignancy,
            })
        return self._render_template(memory_ctx={"micro": micro, "macro": []})
